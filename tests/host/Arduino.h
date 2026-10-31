// Minimal host stand-ins so the real LoPSK.ino runs on a PC for testing.
#pragma once
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <cmath>
#include <cctype>
#include <string>
#include <deque>
#define HIGH 1
#define LOW 0
#define OUTPUT 1
extern uint64_t g_us;
inline uint32_t micros() { return (uint32_t)g_us; }
inline uint32_t millis() { return (uint32_t)(g_us / 1000); }
inline void delay(int ms) { g_us += (uint64_t)ms * 1000; }
inline void delayMicroseconds(int us) { g_us += us; }
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
template <class T> T constrain(T v, T a, T b) { return v < a ? a : (v > b ? b : v); }
class String {
 public:
  std::string s;
  String() {}
  String(const char* c) : s(c) {}
  void trim() { while (!s.empty() && isspace(s.back())) s.pop_back(); size_t i = 0; while (i < s.size() && isspace(s[i])) i++; s.erase(0, i); }
  size_t length() const { return s.size(); }
  void toCharArray(char* b, size_t n) { strncpy(b, s.c_str(), n - 1); b[n - 1] = 0; }
  String& operator+=(char c) { s += c; return *this; }
  String& operator=(const char* c) { s = c; return *this; }
};
struct HostSerial {
  std::deque<char> in;
  void begin(int) {}
  void setRxBufferSize(int) {}
  int available() { return !in.empty(); }
  char read() { char c = in.front(); in.pop_front(); return c; }
  template <class... A> void printf(const char* f, A... a) { std::printf("%10.3f SER ", g_us / 1e3); std::printf(f, a...); }
  void println(const char* s) { std::printf("%10.3f SER %s\n", g_us / 1e3, s); }
};
extern HostSerial Serial;
struct SPIClass { void begin(int, int, int, int) {} };
extern SPIClass SPI;
