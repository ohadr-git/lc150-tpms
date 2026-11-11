#!/usr/bin/env bash
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOT_DIR="/boot/firmware"
[ ! -d "$BOOT_DIR" ] && BOOT_DIR="/boot"

echo "=== Deploying LC150 TPMS ==="
if [ ! -f "$BOOT_DIR/tpms_config.json" ]; then
    sudo cp "$REPO_DIR/config/tpms_config.example.json" "$BOOT_DIR/tpms_config.json"
fi

if [ ! -f "$REPO_DIR/web/cert.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout "$REPO_DIR/web/key.pem" \
        -out "$REPO_DIR/web/cert.pem" -days 365 -nodes -subj "/CN=lc150-tpms.local" 2>/dev/null
fi

sudo ln -sf "$REPO_DIR/systemd/lc150-tpms.service" /etc/systemd/system/lc150-tpms.service
sudo ln -sf "$REPO_DIR/systemd/lc150-web.service" /etc/systemd/system/lc150-web.service

sudo systemctl daemon-reload
sudo systemctl enable --now lc150-tpms.service lc150-web.service
sudo systemctl restart lc150-tpms.service lc150-web.service
echo "=== Services Running! ==="
