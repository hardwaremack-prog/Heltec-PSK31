#!/bin/bash
# Build the PC simulator from the real firmware source (uses this folder's board.h).
cd "$(dirname "$0")"
cp ../../firmware/LoPSK/LoPSK.ino LoPSK_host.ino
g++ -O2 -std=c++17 -I. -o sim sim.cpp
