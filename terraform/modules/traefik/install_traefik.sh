#!/bin/bash
set -e

apt-get update
apt-get install -y curl

# Variables
TRAEFIK_VERSION=$(curl -s https://api.github.com/repos/traefik/traefik/releases/latest | grep -Po '"tag_name": "v\K[0-9.]+')
DOWNLOAD_URL="https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}/traefik_v${TRAEFIK_VERSION}_linux_amd64.tar.gz"

# Download Traefik binary
curl -L $DOWNLOAD_URL -o traefik.tar.gz

# Extract and move the binary
tar -xzf traefik.tar.gz
mv traefik /usr/local/bin/
chmod +x /usr/local/bin/traefik
rm traefik.tar.gz

# Create Traefik configuration directory
mkdir -p /etc/traefik
mkdir -p /etc/traefik/conf.d

# Create a systemd service file for Traefik
cat << EOF > /etc/systemd/system/traefik.service
[Unit]
Description=Traefik Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/traefik --configFile=/etc/traefik/traefik.yml
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd and start Traefik service
systemctl daemon-reload
systemctl enable traefik
systemctl start traefik
