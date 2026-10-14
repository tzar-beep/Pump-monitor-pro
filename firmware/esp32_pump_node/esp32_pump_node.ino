/*
  esp32_pump_node.ino — ESP32 acquisition node for the Pump Health Console.

  Prints ONE JSON object per line at 1 Hz on USB serial (115200 8N1, '\n' terminated):
    {"ts":123456,"seq":42,"rpm":1482.0,"temp_c":38.06,"current_a":2.12,"flow_lpm":18.40,
     "pressure_bar":2.301,"vib_x":0.1200,"vib_y":0.0900,"vib_z":0.1500,"vib_peak":0.412,"float":1}
  This is the contract in README.md ("Hardware-side contract"). The Pi validates every field.

  Arduino framework (Arduino-ESP32 core 2.x/3.x). Libraries: OneWire, DallasTemperature.

  Wiring (must match backend/config.py SENSORS):
    ADXL345   I2C SDA=GPIO21 SCL=GPIO22, addr 0x53, 3.3 V
    DS18B20   GPIO4, 4.7 kΩ pull-up to 3.3 V
    ACS712    OUT -> divider -> GPIO34 (ADC1_CH6)       ** 5 V output: MUST be divided, see README **
    Pressure  OUT -> divider -> GPIO35 (ADC1_CH7)       ** 0.5–4.5 V output: MUST be divided **
    YF-S201   signal -> level shift/divider -> GPIO27   (5 V sensor)
    A3144     open collector -> GPIO26, 10 kΩ pull-up to 3.3 V, one magnet on the shaft
    Float     switch between GPIO25 and GND (INPUT_PULLUP). Closed = water OK -> reads LOW -> float=1.

  Every constant marked CAL needs calibrating on the real rig.
*/
#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ------------------------------------------------------------------ pins
constexpr int PIN_SDA = 21, PIN_SCL = 22;
constexpr int PIN_DS18B20 = 4;
constexpr int PIN_ACS712 = 34;
constexpr int PIN_PRESSURE = 35;
constexpr int PIN_FLOW = 27;
constexpr int PIN_HALL = 26;
constexpr int PIN_FLOAT = 25;

// ------------------------------------------------------------------ timing
constexpr uint32_t REPORT_MS = 1000;      // JSON line period (1 Hz)
constexpr uint32_t SAMPLE_US = 2500;      // 400 Hz fast loop for ADXL345 + ACS712 + pressure

// ------------------------------------------------------------------ calibration (CAL)
constexpr float ADC_VREF = 3.3f;           // CAL: use analogReadMilliVolts() which applies eFuse cal
constexpr float ACS_DIVIDER = 2.0f / 3.0f; // CAL: 10k/20k divider (sensor V * 2/3 = pin V)
constexpr float ACS_SENS_V_PER_A = 0.100f; // ACS712-20A: 100 mV/A (05B: 0.185, 30A: 0.066)
float acsZeroV = 2.5f;                     // measured at boot with pump OFF (sensor-side volts)
constexpr float P_DIVIDER = 2.0f / 3.0f;   // CAL: divider on the pressure output
constexpr float P_V_MIN = 0.5f, P_V_MAX = 4.5f, P_BAR_MAX = 5.0f;  // CAL: transducer datasheet
constexpr float FLOW_HZ_PER_LPM = 7.5f;    // CAL: YF-S201 nominal F = 7.5 * Q (Q in L/min)
constexpr int   HALL_MAGNETS = 1;          // pulses per revolution
constexpr uint32_t HALL_DEBOUNCE_US = 2000;// ignore edges closer than this (max 30 000 rpm)
constexpr float ADXL_G_PER_LSB = 0.0039f;  // full-resolution mode, 3.9 mg/LSB

// ------------------------------------------------------------------ ISR state
volatile uint32_t flowPulses = 0;
volatile uint32_t hallLastUs = 0, hallPeriodSumUs = 0, hallPeriods = 0;

void IRAM_ATTR onFlow() { flowPulses++; }
void IRAM_ATTR onHall() {
  uint32_t now = micros(), dt = now - hallLastUs;
  if (dt < HALL_DEBOUNCE_US) return;
  if (hallLastUs) { hallPeriodSumUs += dt; hallPeriods++; }
  hallLastUs = now;
}

OneWire oneWire(PIN_DS18B20);
DallasTemperature ds(&oneWire);
float lastTempC = NAN;

// ------------------------------------------------------------------ ADXL345 (raw I2C, no library)
constexpr uint8_t ADXL = 0x53;
void adxlWrite(uint8_t reg, uint8_t v) { Wire.beginTransmission(ADXL); Wire.write(reg); Wire.write(v); Wire.endTransmission(); }
bool adxlInit() {
  Wire.beginTransmission(ADXL); Wire.write(0x00); Wire.endTransmission(false);
  Wire.requestFrom(ADXL, (uint8_t)1);
  if (!Wire.available() || Wire.read() != 0xE5) return false;   // DEVID
  adxlWrite(0x2C, 0x0C);   // BW_RATE: 400 Hz output data rate
  adxlWrite(0x31, 0x0B);   // DATA_FORMAT: full resolution, ±16 g
  adxlWrite(0x2D, 0x08);   // POWER_CTL: measure
  return true;
}
bool adxlRead(float &x, float &y, float &z) {
  Wire.beginTransmission(ADXL); Wire.write(0x32);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(ADXL, (uint8_t)6) != 6) return false;
  int16_t r[3];
  for (int i = 0; i < 3; i++) { uint8_t lo = Wire.read(), hi = Wire.read(); r[i] = (int16_t)((hi << 8) | lo); }
  x = r[0] * ADXL_G_PER_LSB; y = r[1] * ADXL_G_PER_LSB; z = r[2] * ADXL_G_PER_LSB;
  return true;
}

float pinVolts(int pin) { return analogReadMilliVolts(pin) / 1000.0f; }

// ------------------------------------------------------------------ window accumulators
struct Acc {
  uint32_t n = 0;
  double sx = 0, sy = 0, sz = 0, sxx = 0, syy = 0, szz = 0;   // ADXL per-axis sums
  float minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9, minz = 1e9, maxz = -1e9;
  double sii = 0;                                               // ACS712 (sensor volts - zero)^2
  double sp = 0;                                                // pressure pin volts
  uint32_t nAdc = 0;
} acc;

bool adxlOk = false;
uint32_t seq = 0, lastReportMs = 0, lastSampleUs = 0;

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_ACS712, ADC_11db);
  analogSetPinAttenuation(PIN_PRESSURE, ADC_11db);
  pinMode(PIN_FLOAT, INPUT_PULLUP);
  pinMode(PIN_FLOW, INPUT);
  pinMode(PIN_HALL, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_FLOW), onFlow, RISING);
  attachInterrupt(digitalPinToInterrupt(PIN_HALL), onHall, FALLING);   // A3144 pulls low at the magnet

  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  adxlOk = adxlInit();

  ds.begin();
  ds.setResolution(12);
  ds.setWaitForConversion(false);      // never block the 400 Hz loop for 750 ms
  ds.requestTemperatures();

  // ACS712 zero offset: average 500 samples. Pump MUST be off at boot (CAL).
  double s = 0;
  for (int i = 0; i < 500; i++) { s += pinVolts(PIN_ACS712) / ACS_DIVIDER; delayMicroseconds(500); }
  acsZeroV = s / 500.0;

  // Non-JSON lines are counted as "not_json" by the Pi and otherwise ignored.
  Serial.printf("# esp32_pump_node ready adxl=%d acs_zero=%.3fV\n", adxlOk, acsZeroV);
  lastReportMs = millis();
  lastSampleUs = micros();
}

void fastSample() {
  float x, y, z;
  if (adxlOk && adxlRead(x, y, z)) {
    acc.n++;
    acc.sx += x; acc.sy += y; acc.sz += z;
    acc.sxx += x * x; acc.syy += y * y; acc.szz += z * z;
    acc.minx = min(acc.minx, x); acc.maxx = max(acc.maxx, x);
    acc.miny = min(acc.miny, y); acc.maxy = max(acc.maxy, y);
    acc.minz = min(acc.minz, z); acc.maxz = max(acc.maxz, z);
  }
  // ACS712: true RMS over the window works for AC (mains motor) and DC.
  float iv = pinVolts(PIN_ACS712) / ACS_DIVIDER - acsZeroV;
  acc.sii += iv * iv;
  acc.sp += pinVolts(PIN_PRESSURE) / P_DIVIDER;
  acc.nAdc++;
}

// AC RMS of one axis = sqrt(E[x^2] - E[x]^2)  (gravity/DC removed)
float axisRms(double s, double ss, uint32_t n) {
  if (!n) return 0;
  double m = s / n, v = ss / n - m * m;
  return v > 0 ? sqrt(v) : 0;
}

void report() {
  uint32_t nowMs = millis();
  float dtS = (nowMs - lastReportMs) / 1000.0f;
  lastReportMs = nowMs;

  noInterrupts();
  uint32_t pulses = flowPulses; flowPulses = 0;
  uint32_t perSum = hallPeriodSumUs, perN = hallPeriods; hallPeriodSumUs = 0; hallPeriods = 0;
  uint32_t lastEdge = hallLastUs;
  interrupts();

  // RPM from the mean period between Hall edges (fine resolution even at 1 pulse/rev).
  float rpm = 0;
  if (perN && (micros() - lastEdge) < 500000) rpm = 60e6f / ((float)perSum / perN) / HALL_MAGNETS;

  float flow = (pulses / dtS) / FLOW_HZ_PER_LPM;

  float vx = axisRms(acc.sx, acc.sxx, acc.n), vy = axisRms(acc.sy, acc.syy, acc.n), vz = axisRms(acc.sz, acc.szz, acc.n);
  float pk = 0;
  if (acc.n) {   // peak absolute deviation from the per-axis mean
    float mx = acc.sx / acc.n, my = acc.sy / acc.n, mz = acc.sz / acc.n;
    pk = max(max(max(acc.maxx - mx, mx - acc.minx), max(acc.maxy - my, my - acc.miny)), max(acc.maxz - mz, mz - acc.minz));
  }

  float iRms = 0, pBar = 0;
  if (acc.nAdc) {
    // True RMS about the boot-time zero: correct for AC (mains motor) and for DC.
    iRms = sqrt(acc.sii / acc.nAdc) / ACS_SENS_V_PER_A;
    if (iRms < 0.05f) iRms = 0;
    float pv = acc.sp / acc.nAdc;
    pBar = (pv - P_V_MIN) / (P_V_MAX - P_V_MIN) * P_BAR_MAX;
  }
  acc = Acc();

  // DS18B20: read the conversion started a second ago, start the next one.
  float t = ds.getTempCByIndex(0);
  if (t != DEVICE_DISCONNECTED_C && t > -55) lastTempC = t;
  ds.requestTemperatures();

  int floatOk = digitalRead(PIN_FLOAT) == LOW ? 1 : 0;

  if (isnan(lastTempC) || !adxlOk) {
    // Report the fault instead of sending made-up numbers; the Pi counts it as invalid.
    Serial.printf("# sensor fault: temp=%d adxl=%d\n", !isnan(lastTempC), adxlOk);
    return;
  }
  seq++;
  Serial.printf("{\"ts\":%lu,\"seq\":%lu,\"rpm\":%.1f,\"temp_c\":%.2f,\"current_a\":%.3f,"
                "\"flow_lpm\":%.2f,\"pressure_bar\":%.3f,\"vib_x\":%.4f,\"vib_y\":%.4f,"
                "\"vib_z\":%.4f,\"vib_peak\":%.4f,\"float\":%d}\n",
                (unsigned long)nowMs, (unsigned long)seq, rpm, lastTempC, iRms,
                flow, pBar, vx, vy, vz, pk, floatOk);
}

void loop() {
  uint32_t now = micros();
  if (now - lastSampleUs >= SAMPLE_US) {
    lastSampleUs += SAMPLE_US;
    fastSample();
  }
  if (millis() - lastReportMs >= REPORT_MS) report();
}
