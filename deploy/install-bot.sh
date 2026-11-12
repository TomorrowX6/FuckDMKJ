#!/bin/sh
set -eu
release="$1"
if [ ! -x /usr/bin/chromium ]; then
  echo 'Missing /usr/bin/chromium; install an ARM64 Chromium before deployment.' >&2
  exit 1
fi
if ! python3 -c 'import venv' >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3-venv
fi
if ! id dm-bot >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/dm-tg-bot --shell /usr/sbin/nologin dm-bot
fi
install -d -m 700 -o dm-bot -g dm-bot /var/lib/dm-tg-bot
if [ ! -x /opt/dm-tg-bot/venv/bin/python ]; then
  if ! python3 -m venv /opt/dm-tg-bot/venv; then
    apt-get update
    apt-get install -y python3-venv
    python3 -m venv /opt/dm-tg-bot/venv
  fi
fi
/opt/dm-tg-bot/venv/bin/python -m pip install --upgrade pip
/opt/dm-tg-bot/venv/bin/python -m pip install -r "$release/requirements-bot.txt"
/opt/dm-tg-bot/venv/bin/python "$release/tools/fetch_captcha_sdk.py"
ln -sfn "$release" /opt/dm-tg-bot/current
install -m 644 "$release/deploy/dm-tg-bot.service" /etc/systemd/system/dm-tg-bot.service
systemctl daemon-reload
echo 'Dependencies and service installed; awaiting validation before startup.'
