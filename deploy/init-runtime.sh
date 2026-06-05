#!/usr/bin/env bash
set -euo pipefail

mkdir -p results imports logs

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "created config.json from config.example.json; edit it before running real tasks"
fi

[ -f "?.json" ] || printf '[]
' > "?.json"
[ -f msoutlook_used.json ] || printf '{"records":{}}
' > msoutlook_used.json
[ -f email_blacklist.json ] || printf '[]
' > email_blacklist.json
[ -f icloud_cookies.json ] || printf '{}
' > icloud_cookies.json

if [ ! -f .env ]; then
  cat > .env <<'ENV'
IMAGE=ghcr.io/songzichen29/chatgpt-auto-register:worker-control-dashboard
HOST_BIND=127.0.0.1
WEB_PORT=7777
TZ=Asia/Shanghai
ENV
  echo "created .env; change HOST_BIND to 0.0.0.0 only if you add access control"
fi
