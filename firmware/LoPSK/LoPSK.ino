/*
  LoPSK firmware — turns an SX1262 Meshtastic-style LoRa board into a
  PSK31 / PSK63 transmitter.

  How it makes PSK with a chip that has no phase control:
  the SX1262 synthesizer can be nudged in 0.95 Hz steps without breaking the
  carrier's phase. Running a little high for a moment adds phase, so a smooth
  nudge that adds exactly half a cycle is a 180 degree phase reversal — the
  "0" bit of PSK31. Every 1 ms tick we pick the offset (whole PLL steps) that
  keeps the accumulated phase on a raised-cosine path, carrying the rounding
  error forward. Reversals alternate up/down so the spectrum stays symmetric.
  The desktop app's psk31.PhaseSteer class is the same arithmetic in Python.

  Serial protocol (115200 baud, one command per line):
    HELLO                 -> LOPSK <version> <board> SX1262|NO-RADIO
    PWR <dBm>             -> OK PWR <dBm>                  (-9 .. 22)
    STEER <0|1>           -> OK STEER <n>  0 = frequency nudges only (default)
                                           1 = also re-key the carrier each nudge
                                               (try this if decodes are poor)
    PSK <hz> <baud>       -> PSKON   start transmitting (baud 31.25 or 62.5).
                             Sends idle (reversals) whenever the queue is empty.
    B <0/1 bits>          -> queue bits (up to 400 per line)
    END                   -> finish the queue, send 1 s of steady carrier, then
                             PSKDONE <ms>
    TUNE <hz> <seconds>   -> TUNEON ... TUNEDONE   (steady carrier, max 30 s)
    LISTEN <hz>           -> LISTENON, then "R <rssi x10>" 50 times a second
                             (signal detector only: the SX1262 can't demodulate PSK)
    STOP                  -> STOPPED
    STATUS                -> IDLE | PSK <queued bits> | TUNE | LISTEN
  While transmitting: "Q <queued bits>" 4 times a second, for flow control.
  Safety: the carrier drops after 60 s of idle with nothing queued, or if the
  computer stops talking for 10 s.
  Errors come back as: ERR <reason>

  Transmitting needs an amateur radio license for the frequency you use.
*/
#include <RadioLib.h>
#include <SPI.h>
#include <math.h>
#include "board.h"

#ifdef HAS_PMU
  #include <Wire.h>
  #include <XPowersLib.h>
#endif

#define FW_VERSION "1.0"

// gives us the raw "set frequency" command, so the carrier keeps running
class LoRadio : public SX1262 {
 public:
  using SX1262::SX1262;
  int16_t setFrf(uint32_t frf) { return setRfFrequency(frf); }
};

static const double   STEP_HZ     = 32000000.0 / 33554432.0;  // SX126x PLL step, ~0.9537 Hz
static const uint32_t TICK_US     = 1000;
static const double   TICK_S      = TICK_US * 1e-6;
static const uint32_t MAX_TUNE_MS = 30000;
static const int      QSIZE       = 16384;    // bit queue (~8.7 min of PSK31)
static const int      TAIL_BITS   = 32;       // 1 s steady carrier at the end
static const uint32_t IDLE_LIMIT_MS = 60000;
static const uint32_t HOST_LIMIT_MS = 10000;

LoRadio radio = new Module(PIN_NSS, PIN_DIO1, PIN_RST, PIN_BUSY, SPI);

enum Mode { MODE_IDLE, MODE_PSK, MODE_TUNE, MODE_LISTEN };
static Mode     mode = MODE_IDLE;
static int8_t   powerDbm = 10;
static double   calFreqHz = 0;
static bool     radioOk = false;
static String   line;
static uint32_t tuneEndMs = 0;
static int      steerMode = 0;

// bit queue
static uint8_t  q[QSIZE];
static int      qHead = 0, qTail = 0;
static int qCount() { return (qTail - qHead + QSIZE) % QSIZE; }

// PSK state
static uint32_t baseFrf = 0;
static int      ticksPerSym = 32, transTicks = 16, symTick = 0;
static uint32_t nextTickUs = 0, txStartMs = 0, lastQMs = 0, idleSinceMs = 0, lastHostMs = 0;
static double   phaseAcc = 0;     // cycles actually added by our nudges
static double   startPh = 0, targetPh = 0;
static int      dir = 1;
static int32_t  lastSteps = 0;
static bool     ending = false;
static int      tailLeft = 0;

// listen
static uint32_t lastRssiMs = 0;
static float    rssiAcc = 0;
static int      rssiN = 0;
static bool     rxConfigured = false;

static uint32_t frfFor(double hz) { return (uint32_t)llround(hz / STEP_HZ); }

static void led(bool on) {
#ifdef PIN_LED
  digitalWrite(PIN_LED, on ? HIGH : LOW);
#endif
}

#ifdef HAS_PMU
static void powerUpRadio() {
  Wire.begin(PIN_SDA, PIN_SCL);
  static XPowersAXP2101 axp2101;
  static XPowersAXP192  axp192;
  if (axp2101.begin(Wire, AXP2101_SLAVE_ADDRESS, PIN_SDA, PIN_SCL)) {
    axp2101.setALDO2Voltage(3300);   // LoRa radio rail on T-Beam v1.2
    axp2101.enableALDO2();
  } else if (axp192.begin(Wire, AXP192_SLAVE_ADDRESS, PIN_SDA, PIN_SCL)) {
    axp192.setLDO2Voltage(3300);     // LoRa radio rail on T-Beam v1.1
    axp192.enableLDO2();
  }
  delay(50);
}
#endif

static void stopRadio() {
  radio.standby();
  mode = MODE_IDLE;
  led(false);
}

static bool prepareFrequency(double hz) {
  if (hz < 150e6 || hz > 960e6) { Serial.println("ERR frequency out of range"); return false; }
  if (fabs(hz - calFreqHz) > 1e6) {           // re-run image calibration on big moves
    int st = radio.setFrequency((uint32_t)hz);
    if (st != RADIOLIB_ERR_NONE) { Serial.printf("ERR setFrequency %d\n", st); return false; }
    calFreqHz = hz;
  }
  return true;
}

// ------------------------------------------------------------------ PSK

static void beginSymbol(bool reverse) {
  startPh = targetPh;
  if (reverse) { targetPh = startPh + 0.5 * dir; dir = -dir; }
  symTick = 0;
}

static int nextBit() {
  if (qCount() > 0) { int b = q[qHead]; qHead = (qHead + 1) % QSIZE; idleSinceMs = millis(); return b; }
  if (ending) { if (tailLeft > 0) { tailLeft--; return 1; } return -1; }
  return 0;                                   // idle: reversals keep receivers locked
}

static void startPsk(double hz, float baud) {
  if (!radioOk) { Serial.println("ERR radio not found"); return; }
  if (!prepareFrequency(hz)) return;
  if (baud > 40) { ticksPerSym = 16; } else { ticksPerSym = 32; baud = 31.25f; }
  transTicks = ticksPerSym / 2;
  baseFrf = frfFor(hz);
  qHead = qTail = 0;
  phaseAcc = startPh = targetPh = 0;
  dir = 1; lastSteps = 0; ending = false; tailLeft = TAIL_BITS;
  radio.transmitDirect(baseFrf);
  mode = MODE_PSK;
  txStartMs = idleSinceMs = lastHostMs = lastQMs = millis();
  beginSymbol(nextBit() == 0);
  nextTickUs = micros() + TICK_US;
  led(true);
  Serial.println("PSKON");
}

static void pskTick() {
  int32_t steps = 0;
  if (symTick < transTicks) {
    double u = (double)(symTick + 1) / transTicks;
    double want = startPh + (targetPh - startPh) * 0.5 * (1.0 - cos(M_PI * u));
    steps = (int32_t)llround((want - phaseAcc) / (STEP_HZ * TICK_S));
    phaseAcc += steps * STEP_HZ * TICK_S;
  }
  if (steps != lastSteps) {
    if (steerMode == 1) radio.transmitDirect(baseFrf + steps);
    else radio.setFrf(baseFrf + steps);
    lastSteps = steps;
  }
  symTick++;
  if (symTick >= ticksPerSym) {
    int b = nextBit();
    if (b < 0) {
      stopRadio();
      Serial.printf("PSKDONE %lu\n", (unsigned long)(millis() - txStartMs));
      return;
    }
    beginSymbol(b == 0);
    led(b == 0);
  }
}

static void queueBits(const char* s) {
  int added = 0;
  for (const char* p = s; *p; p++) {
    if (*p != '0' && *p != '1') continue;
    if (qCount() >= QSIZE - 1) { Serial.println("ERR queue full"); break; }
    q[qTail] = (*p == '1'); qTail = (qTail + 1) % QSIZE; added++;
  }
  if (added) idleSinceMs = millis();
}

// ------------------------------------------------------------------ other modes

static void startTune(double hz, float seconds) {
  if (!radioOk) { Serial.println("ERR radio not found"); return; }
  if (!prepareFrequency(hz)) return;
  uint32_t ms = (uint32_t)(seconds * 1000);
  if (ms == 0 || ms > MAX_TUNE_MS) ms = MAX_TUNE_MS;
  radio.transmitDirect(frfFor(hz));
  tuneEndMs = millis() + ms;
  mode = MODE_TUNE;
  led(true);
  Serial.println("TUNEON");
}

static void startListen(double hz) {
  if (!radioOk) { Serial.println("ERR radio not found"); return; }
  if (!prepareFrequency(hz)) return;
  radio.setFrequency((uint32_t)hz);
  if (!rxConfigured) {
    radio.setBitRate(600);              // lets us pick the narrowest filter
    radio.setFrequencyDeviation(600);
    radio.setRxBandwidth(4800);         // narrowest the SX1262 has (~4.8 kHz)
    radio.setRxBoostedGainMode(true);
    rxConfigured = true;
  }
  int st = radio.startReceive();
  if (st != RADIOLIB_ERR_NONE) { Serial.printf("ERR startReceive %d\n", st); return; }
  mode = MODE_LISTEN;
  rssiAcc = 0; rssiN = 0; lastRssiMs = millis();
  Serial.println("LISTENON");
}

// ------------------------------------------------------------------ commands

static void handleLine(String& cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  lastHostMs = millis();
  static char buf[480];
  cmd.toCharArray(buf, sizeof(buf));
  char* verb = strtok(buf, " ");
  for (char* p = verb; *p; p++) *p = toupper(*p);

  if (!strcmp(verb, "B")) {
    char* bits = strtok(NULL, "");
    if (mode != MODE_PSK) { Serial.println("ERR not transmitting"); return; }
    if (bits) queueBits(bits);
  } else if (!strcmp(verb, "HELLO")) {
    Serial.printf("LOPSK %s %s %s\n", FW_VERSION, BOARD_NAME, radioOk ? "SX1262" : "NO-RADIO");
  } else if (!strcmp(verb, "STATUS")) {
    if (mode == MODE_PSK) Serial.printf("PSK %d\n", qCount());
    else Serial.println(mode == MODE_TUNE ? "TUNE" : (mode == MODE_LISTEN ? "LISTEN" : "IDLE"));
  } else if (!strcmp(verb, "STOP")) {
    stopRadio();
    Serial.println("STOPPED");
  } else if (!strcmp(verb, "END")) {
    if (mode == MODE_PSK) ending = true;
    Serial.println("OK END");
  } else if (!strcmp(verb, "PWR")) {
    char* a = strtok(NULL, " ");
    if (!a) { Serial.println("ERR PWR needs dBm"); return; }
    int p = constrain(atoi(a), -9, 22);
    if (radioOk && radio.setOutputPower(p) != RADIOLIB_ERR_NONE) { Serial.println("ERR power"); return; }
    powerDbm = p;
    Serial.printf("OK PWR %d\n", p);
  } else if (!strcmp(verb, "STEER")) {
    char* a = strtok(NULL, " ");
    steerMode = (a && atoi(a) == 1) ? 1 : 0;
    Serial.printf("OK STEER %d\n", steerMode);
  } else if (!strcmp(verb, "PSK")) {
    char* f = strtok(NULL, " ");
    char* b = strtok(NULL, " ");
    if (!f) { Serial.println("ERR PSK needs freq"); return; }
    if (mode != MODE_IDLE) stopRadio();
    startPsk(atof(f), b ? atof(b) : 31.25f);
  } else if (!strcmp(verb, "LISTEN")) {
    char* f = strtok(NULL, " ");
    if (!f) { Serial.println("ERR LISTEN needs freq"); return; }
    if (mode != MODE_IDLE) stopRadio();
    startListen(atof(f));
  } else if (!strcmp(verb, "TUNE")) {
    char* f = strtok(NULL, " ");
    char* s = strtok(NULL, " ");
    if (!f) { Serial.println("ERR TUNE needs freq"); return; }
    if (mode != MODE_IDLE) stopRadio();
    startTune(atof(f), s ? atof(s) : 10);
  } else {
    Serial.println("ERR unknown command");
  }
}

void setup() {
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);
#ifdef PIN_LED
  pinMode(PIN_LED, OUTPUT);
  led(false);
#endif
#ifdef HAS_PMU
  powerUpRadio();
#endif
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_NSS);
  radio.tcxoVoltage = RADIO_TCXO;
  ConfigFSK_t cfg;
  cfg.frequency = 432000000UL;
  cfg.power = powerDbm;
  int st = radio.beginFSK(cfg);
  if (st == RADIOLIB_ERR_NONE) {
    radio.setDio2AsRfSwitch(true);
    radioOk = true;
    calFreqHz = 432e6;
  }
  delay(200);
  Serial.printf("LOPSK %s %s %s\n", FW_VERSION, BOARD_NAME, radioOk ? "SX1262" : "NO-RADIO");
  if (!radioOk) Serial.printf("ERR radio init %d\n", st);
}

void loop() {
  // the 1 ms PSK tick comes first, so serial traffic never delays it much
  if (mode == MODE_PSK && (int32_t)(micros() - nextTickUs) >= 0) {
    nextTickUs += TICK_US;
    if ((int32_t)(micros() - nextTickUs) > 20000) nextTickUs = micros() + TICK_US;  // never spiral
    pskTick();
    if (mode == MODE_PSK) {
      uint32_t now = millis();
      if (now - lastQMs >= 250) { lastQMs = now; Serial.printf("Q %d\n", qCount()); }
      if (!ending && now - idleSinceMs > IDLE_LIMIT_MS) { ending = true; }
      if (now - lastHostMs > HOST_LIMIT_MS) { stopRadio(); Serial.println("STOPPED host quiet"); }
    }
  }

  // read a bounded amount per pass so a burst of bits can't starve the tick
  int budget = 64;
  while (Serial.available() && budget-- > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { handleLine(line); line = ""; }
    else if (line.length() < 470) line += c;
  }

  if (mode == MODE_LISTEN) {
    rssiAcc += radio.getRSSI(false);
    rssiN++;
    if (millis() - lastRssiMs >= 20) {
      lastRssiMs = millis();
      Serial.printf("R %d\n", (int)lroundf(rssiAcc / rssiN * 10));
      rssiAcc = 0; rssiN = 0;
    }
    delayMicroseconds(500);
  } else if (mode == MODE_TUNE) {
    if ((int32_t)(millis() - tuneEndMs) >= 0) {
      stopRadio();
      Serial.println("TUNEDONE");
    }
  }
}
