#!/usr/bin/env bash
# Install the AC43 simulator as a boot-time service on Raspberry Pi OS.
#   cd ac43-simulator && sudo ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Enabling I2C"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_i2c 0
fi
usermod -aG i2c "$RUN_USER" || true

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq python3-venv i2c-tools

echo "==> Creating virtualenv"
sudo -u "$RUN_USER" python3 -m venv "$DIR/.venv"
sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> Installing systemd service"
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_USER|g" "$DIR/ac43-sim.service" \
  > /etc/systemd/system/ac43-sim.service
systemctl daemon-reload
systemctl enable --now ac43-sim.service

PORT=$(grep -E '^\s*port\s*=' "$DIR/config.toml" | head -1 | grep -oE '[0-9]+')
echo
echo "Done. Devices on I2C bus 1:"
i2cdetect -y 1 || true
echo
echo "Open http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
echo "Logs:  journalctl -u ac43-sim -f"
