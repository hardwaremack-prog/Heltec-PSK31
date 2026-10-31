#pragma once
#include "Arduino.h"
#define RADIOLIB_ERR_NONE 0
struct Module { Module(int, int, int, int, SPIClass&) {} };
struct ConfigFSK_t { uint32_t frequency; int power; };
enum TcxoV { Voltage1V8 };
class SX1262 {
 public:
  SX1262(Module*) {}
  TcxoV tcxoVoltage;
  int beginFSK(ConfigFSK_t) { return 0; }
  int setDio2AsRfSwitch(bool) { return 0; }
  int setFrequency(uint32_t) { return 0; }
  int setOutputPower(int) { return 0; }
  int standby() { std::printf("%10.3f RF OFF\n", g_us / 1e3); return 0; }
  int transmitDirect(uint32_t f) { setRfFrequency(f); std::printf("%10.3f RF CW\n", g_us / 1e3); return 0; }
  int setBitRate(int) { return 0; } int setFrequencyDeviation(int) { return 0; }
  int setRxBandwidth(int) { return 0; } int setRxBoostedGainMode(bool) { return 0; }
  int startReceive() { return 0; } float getRSSI(bool) { return -120; }
 protected:
  int setRfFrequency(uint32_t f) { g_us += 40; std::printf("%10.3f F %u\n", g_us / 1e3, f); return 0; }  // ~40 us SPI
};
