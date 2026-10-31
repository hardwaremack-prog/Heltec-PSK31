#!/bin/bash
# LoPSK installer for Raspberry Pi OS (Bookworm / Trixie)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Installing LoPSK requirements…"
sudo apt-get update -qq
sudo apt-get install -y python3-tk python3-serial python3-numpy esptool libportaudio2 rtl-sdr librtlsdr0
# sound card input + RTL-SDR support
pip3 install --break-system-packages --quiet sounddevice "pyrtlsdr==0.2.93" || \
  pip3 install --quiet sounddevice "pyrtlsdr==0.2.93"
sudo usermod -aG dialout "$USER" || true
chmod +x "$DIR/lopsk_app.py"
LAUNCHER="[Desktop Entry]
Type=Application
Name=LoPSK
Comment=PSK31 from your Meshtastic LoRa board
Exec=python3 \"$DIR/lopsk_app.py\"
Path=$DIR
Icon=network-wireless
Terminal=false
Categories=HamRadio;Network;"
mkdir -p "$HOME/.local/share/applications"
echo "$LAUNCHER" > "$HOME/.local/share/applications/lopsk.desktop"
if [ -d "$HOME/Desktop" ]; then
  echo "$LAUNCHER" > "$HOME/Desktop/lopsk.desktop"
  chmod +x "$HOME/Desktop/lopsk.desktop"
fi
echo
echo "Done! Open LoPSK from the menu (Internet / Ham Radio) or the Desktop."
echo "If this is your first install, log out and back in once so the USB port permission takes effect."
echo "Using an RTL-SDR? Reboot once (or unplug/replug it) so the TV-tuner driver lets go of it."
