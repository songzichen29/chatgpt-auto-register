# Docker / GHCR deployment

Use this directory to deploy both the web app and the Hotmail Helper service.
Recommended server path:

```text
/data/docker-compose/chatgpt-auto-register
```

## Runtime layout

```text
/data/docker-compose/chatgpt-auto-register/
|-- docker-compose.yml
|-- .env
|-- config.json
|-- 号.json
|-- msoutlook_used.json
|-- email_blacklist.json
|-- icloud_cookies.json
|-- helper-data/
|-- imports/
|-- logs/
|-- results/
`-- src/                    # Git checkout
```

## Initialize

```bash
mkdir -p /data/docker-compose/chatgpt-auto-register
cd /data/docker-compose/chatgpt-auto-register
git clone --branch worker-control-dashboard https://github.com/songzichen29/chatgpt-auto-register.git src
cp src/deploy/docker-compose.ghcr.yml docker-compose.yml
cp src/config.example.json config.example.json
bash src/deploy/init-runtime.sh
```

Edit runtime secrets only on the server:

```bash
nano config.json
```

Do not commit runtime data:

- `config.json`
- `号.json`
- `msoutlook_used.json`
- `icloud_cookies.json`
- `email_blacklist.json`
- `helper-data/`
- `imports/`
- `logs/`
- `results/`

## Hotmail Helper connection

The old Windows script ran:

```bat
python scripts\hotmail_helper.py --port 17373
```

In Docker deployment, compose starts it as a separate service named `hotmail-helper`.
The app container must not use `127.0.0.1:17373`, because that points back to the app container itself.
Use compose DNS instead:

```json
"msoutlook": {
  "helper_mode": "http",
  "helper_url": "http://hotmail-helper:17373",
  "email": "",
  "helper_bat": "",
  "helper_script": ""
}
```

The helper is only exposed inside the compose network by default; normally there is no need to publish host port `17373`.

## Update / start

GitHub Actions publishes two GHCR images:

- `ghcr.io/songzichen29/chatgpt-auto-register:<branch>`
- `ghcr.io/songzichen29/chatgpt-auto-register-hotmail-helper:<branch>`

Server update:

```bash
cd /data/docker-compose/chatgpt-auto-register
git -C src fetch origin worker-control-dashboard --depth 1
git -C src reset --hard origin/worker-control-dashboard
cp src/deploy/docker-compose.ghcr.yml docker-compose.yml
cp src/config.example.json config.example.json
bash src/deploy/init-runtime.sh
docker compose pull
docker compose up -d
```

If the helper image is not available yet, build it from the checked-out source on the server:

```bash
docker compose up -d --build hotmail-helper
docker compose up -d
```

## Access

Default mapping:

```text
127.0.0.1:7777 -> chatgpt-auto-register:8080
```

Worker control page:

```text
http://server-address:7777/worker-control
```

The default `.env` binds to `127.0.0.1`. Use Nginx/Caddy/reverse proxy or SSH tunnel, or change `HOST_BIND=0.0.0.0` only after adding access control.
