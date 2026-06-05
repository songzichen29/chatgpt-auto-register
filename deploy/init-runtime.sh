#!/usr/bin/env bash
set -euo pipefail

mkdir -p results imports logs helper-data

if [ -f '?.json' ] && [ ! -f '号.json' ]; then
  mv -- '?.json' '号.json'
fi

created_config=0
if [ ! -f config.json ]; then
  cp config.example.json config.json
  created_config=1
  echo "created config.json from config.example.json; edit it before running real tasks"
fi

if [ "$created_config" = "1" ]; then
  python3 - <<'PY' || true
import json
from pathlib import Path
p = Path('config.json')
cfg = json.loads(p.read_text(encoding='utf-8'))
ms = cfg.setdefault('msoutlook', {})
ms['helper_mode'] = 'http'
ms['helper_url'] = 'http://hotmail-helper:17373'
ms.setdefault('email', '')
ms['helper_bat'] = ''
ms['helper_script'] = ''
phase2 = cfg.setdefault('phase2', {})
phase2['msoutlook_helper_url'] = 'http://hotmail-helper:17373'
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
fi

[ -f "号.json" ] || printf '[]\n' > "号.json"
[ -f msoutlook_used.json ] || printf '%s\n' '{"records":{}}' > msoutlook_used.json
[ -f email_blacklist.json ] || printf '[]\n' > email_blacklist.json
[ -f icloud_cookies.json ] || printf '{}\n' > icloud_cookies.json

if [ ! -f .env ]; then
  cat > .env <<'ENV'
IMAGE=ghcr.io/songzichen29/chatgpt-auto-register:worker-control-dashboard
HELPER_IMAGE=ghcr.io/songzichen29/chatgpt-auto-register-hotmail-helper:worker-control-dashboard
HOST_BIND=127.0.0.1
WEB_PORT=7777
TZ=Asia/Shanghai
ENV
  echo "created .env; change HOST_BIND to 0.0.0.0 only if you add access control"
fi
