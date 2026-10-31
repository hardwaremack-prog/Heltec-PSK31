// LoPSK board pin maps. Pick one with a -D flag (build.sh does this), or
// for the Arduino IDE just uncomment ONE of these lines:
// #define BOARD_HELTEC_V3
// #define BOARD_TBEAM_SX1262
// #define BOARD_T3S3_SX1262
//
//   BOARD_HELTEC_V3     Heltec WiFi LoRa 32 V3 / Wireless Stick Lite V3 / Wireless Tracker
//   BOARD_TBEAM_SX1262  LilyGO T-Beam v1.1 / v1.2 with the SX1262 radio
//   BOARD_T3S3_SX1262   LilyGO T3-S3 with the SX1262 radio
//
// Only SX1262/SX1268 radios can do PSK31: their synthesizer moves in ~0.95 Hz
// steps. Older SX1276/SX1278 boards step in 61 Hz, far too coarse for the
// gentle phase nudges PSK31 needs.
#pragma once

#if defined(BOARD_HELTEC_V3)
  #define BOARD_NAME   "Heltec V3"
  #define PIN_SCK      9
  #define PIN_MISO     11
  #define PIN_MOSI     10
  #define PIN_NSS      8
  #define PIN_RST      12
  #define PIN_BUSY     13
  #define PIN_DIO1     14
  #define PIN_LED      35
  #define RADIO_TCXO   Voltage1V8

#elif defined(BOARD_TBEAM_SX1262)
  #define BOARD_NAME   "T-Beam SX1262"
  #define PIN_SCK      5
  #define PIN_MISO     19
  #define PIN_MOSI     27
  #define PIN_NSS      18
  #define PIN_RST      23
  #define PIN_BUSY     32
  #define PIN_DIO1     33
  #define PIN_SDA      21
  #define PIN_SCL      22
  #define HAS_PMU      1
  #define RADIO_TCXO   Voltage1V8

#elif defined(BOARD_T3S3_SX1262)
  #define BOARD_NAME   "T3-S3 SX1262"
  #define PIN_SCK      5
  #define PIN_MISO     3
  #define PIN_MOSI     6
  #define PIN_NSS      7
  #define PIN_RST      8
  #define PIN_BUSY     34
  #define PIN_DIO1     33
  #define PIN_LED      37
  #define RADIO_TCXO   Voltage1V8

#else
  #error "Pick a board: -DBOARD_HELTEC_V3, -DBOARD_TBEAM_SX1262 or -DBOARD_T3S3_SX1262"
#endif
