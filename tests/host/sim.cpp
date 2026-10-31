// Runs the real LoPSK firmware on a PC with a pretend radio that logs every
// frequency change.  Build:  g++ -O2 -std=c++17 -I. -o sim sim.cpp
// (use build.sh)  Run:    ./sim 432100000 31.25 bits.txt > log.txt   then  python3 ../test_firmware.py log.txt
#include "Arduino.h"
uint64_t g_us = 0;
HostSerial Serial;
SPIClass SPI;
#include "LoPSK_host.ino"   // copy of ../../firmware/LoPSK/LoPSK.ino (build.sh makes it)
// usage: sim <hz> <baud> <bitsfile>   -> event log on stdout
int main(int argc, char** argv) {
  setup();
  std::string cmd = std::string("PSK ") + argv[1] + " " + argv[2] + "\n";
  FILE* f = fopen(argv[3], "r"); char bits[100000]; size_t n = fread(bits, 1, sizeof(bits) - 1, f); bits[n] = 0;
  for (char c : cmd) Serial.in.push_back(c);
  size_t pos = 0; uint64_t lastSend = 0;
  while (g_us < 600e6) {
    // host sends 200 bits every 0.5 s while it has some, then END; also keeps the link alive
    if (g_us - lastSend > 500000) {
      lastSend = g_us;
      std::string l;
      if (pos < n) { l = "B " + std::string(bits + pos, std::min<size_t>(200, n - pos)) + "\n"; pos += 200; }
      else if (pos != (size_t)-1) { l = "END\n"; pos = (size_t)-1; }
      else l = "STATUS\n";
      for (char c : l) Serial.in.push_back(c);
    }
    loop();
    g_us += 7;   // each loop pass costs a few microseconds
    if (mode == MODE_IDLE && pos == (size_t)-1) break;
  }
}
