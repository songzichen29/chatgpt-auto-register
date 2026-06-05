# Docker / GHCR 部署说明

推荐流程：GitHub Actions 构建镜像并推送到 GHCR，服务器只执行 `docker compose pull && docker compose up -d`。

## 服务器初始化

```bash
mkdir -p /opt/chatgpt-auto-register
cd /opt/chatgpt-auto-register
git clone --branch worker-control-dashboard https://github.com/songzichen29/chatgpt-auto-register.git src
cp src/deploy/docker-compose.ghcr.yml docker-compose.yml
cp src/config.example.json config.example.json
bash src/deploy/init-runtime.sh
```

然后编辑：

```bash
nano config.json
```

需要放到部署目录的运行数据：

- `config.json`
- `号.json`
- `msoutlook_used.json`
- `icloud_cookies.json`，如使用 iCloud
- `email_blacklist.json`，可为空数组

## 启动 / 更新

```bash
cd /opt/chatgpt-auto-register
docker compose pull
docker compose up -d
docker compose logs -f --tail=100
```

默认只监听服务器本机：

```text
127.0.0.1:7777 -> container:8080
```

如果要直接公网访问，把 `.env` 的 `HOST_BIND=0.0.0.0`，但建议先加反向代理认证。

## 访问

```text
http://服务器地址:7777/worker-control
```

如果仍保持 `HOST_BIND=127.0.0.1`，请通过 Nginx/Caddy/面板反代，或用 SSH 隧道访问。
