#!/bin/bash
# Rebuild the LoPSK firmware images with arduino-cli (needs the esp32 core 3.3.x installed).
set -e
cd "$(dirname "$0")"
build() {
  arduino-cli compile --fqbn "$2" --libraries libraries \
    --build-property "compiler.cpp.extra_flags=-D$1" --output-dir "build/$1" LoPSK
  cp "build/$1/LoPSK.ino.merged.bin" "../app/firmware/$3"
}
build BOARD_HELTEC_V3    esp32:esp32:heltec_wifi_lora_32_V3                            lopsk-heltec-v3.bin
build BOARD_TBEAM_SX1262 esp32:esp32:t-beam                                            lopsk-tbeam-sx1262.bin
build BOARD_T3S3_SX1262  "esp32:esp32:lilygo_t3s3:Revision=Radio_SX1262,CDCOnBoot=cdc" lopsk-t3s3-sx1262.bin
echo "Firmware images updated in app/firmware/"
