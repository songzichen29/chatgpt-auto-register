---
doc_type: feature-design
feature: 2026-06-04-worker-control-dashboard
status: approved
summary: 新增 worker_pool Web 控制台，支持配置、号池导入查询、注册任务启动停止、账号状态管理和 SUB2API 导出
tags: [web-gui, worker-pool, msoutlook, accounts, export]
created: 2026-06-04
---

# worker_pool Web 控制台

## 0. 术语约定

| 术语 | 定义 | 防冲突结论 |
|---|---|---|
| worker_pool 任务 | 由前端触发的 `worker_pool.py` 子进程任务，实际注册逻辑仍由现有 CLI 执行。 | `worker_pool.py` 已是并发注册 CLI，本 feature 不复制其内部 worker 状态机。 |
| 控制台 | 新增单用户管理页面，用于配置、启动/停止任务、看号池、看注册结果和导出。 | 现有 `web_gui.py` 是单 worker GUI，`public/index.html` 是多用户平台；本 feature 新增独立控制页，避免混改旧页面。 |
| 号池状态 | MsOutlook 邮箱在 `号.json` 与 `msoutlook_used.json` 合并后的状态：`unused`、`used`、`error`、`reserved`。 | `msoutlook_pool.py` 已有 `stats()` / `get_records()`；需要补一个面向 UI 的聚合列表。 |
| 号池导入统计 | 记录每次导入邮箱池的新增、跳过、导入后总量、导入时间。 | 现有 `MsOutlookPool.import_accounts()` 返回 added/skipped/total，但没有历史；新增 `results/pool_import_history.json` 记录。 |
| 注册结果状态 | `results/_all.json` 中的业务状态：`ok`、`fail_phase1`、`fail_phase2`、`interrupted_after_phase1` 等。 | `worker_pool.ResultWriter` 已统一写这些状态；控制台只读取和展示，不改写注册事实。 |
| 账号状态 | 人工维护的成功账号后续状态：`unused` 未试用、`testing` 测试中、`used` 已使用、`bad` 不可用、`reserved` 暂占。 | 当前仓库没有这个概念；新增 `results/account_state.json` 持久化，不写回 `_all.json`，避免污染注册结果。 |
| 成功试用账号 | `reg_status=ok` 且人工 `usage_status=used` 的账号。 | “试用成功”来自用户人工确认，不由系统猜测。这个指标是控制台首页核心统计。 |
| 导出状态 | 成功账号的 SUB2API 导出标记：`not_exported` 未导出、`exported` 已导出、`ignored` 不导出。 | 这是账号状态的一部分，和注册状态分开；导出接口可选择导出后自动标记。 |
| 阶段重试 | 对已失败或中断的结果记录，按失败阶段重新执行可恢复的后续流程。 | `worker_pool.py` 已支持新任务的 Phase 1/Phase 2 重试；控制台新增“失败记录续跑”，优先复用已保存材料，不盲目重跑全流程。 |
| 续跑任务 | 由前端选中历史失败账号后触发的 retry job，包括 Phase 2-only 重试、换邮箱重试、重新导入 SUB2API 等。 | 与普通 `worker_pool` 注册任务互斥运行，避免同时占用号池和写结果文件。 |
| SUB2API 导出 | 聚合 `imports/import_*.json` 中的 `accounts`，按筛选条件输出 `type=sub2api-data` JSON。 | 现有 `openai_bind_email.py` / `worker_pool.ResultWriter` 已生成 SUB2API import payload；本 feature 只做聚合导出。 |

术语检索记录：已用 `rg` 检索 `worker_pool`、`/api/start`、`/api/stop`、`msoutlook-records`、`sub2api`、`export`、`used`、`未使用`、`号池`。命中显示现有 `web_gui.py` 已有基础配置/号池接口但运行的是旧 `_run()`；`worker_pool.py` 目前没有 Web 控制入口。

## 1. 决策与约束

### 1.1 需求摘要

**做什么**：新增一个面向 `worker_pool.py` 的 Web 控制台，支持：

1. 设置并保存注册配置；
2. 导入 MsOutlook 号池；
3. 查询号池总量、已用、错误、未使用列表；
4. 从前端启动 `worker_pool.py -n COUNT -c CONCURRENCY ...`；
5. 从前端停止正在运行的 worker_pool 任务；
6. 查看实时日志、任务运行状态和历史注册结果；
7. 给注册好的账号维护“未试用 / 测试中 / 已使用 / 不可用 / 暂占”状态；
8. 记录成功账号是否已经导出过 SUB2API 格式；
9. 按注册状态、账号状态、导出状态、关键词、时间范围等条件查询；
10. 支持当前页全选、按当前筛选条件全选、批量修改账号状态；
11. 支持批量导出 SUB2API JSON，并在导出成功后标记“已导出”；
12. 配置页展示完整 `config.json` 原文，支持 JSON 校验、保存和重新加载；
13. 支持对不同阶段失败的记录执行重试，以便尽量走完整个注册到导出的流程；
14. 首页必须突出记录和展示：导入邮箱池数量、注册成功账号数、注册失败账号数、成功试用账号数。

**为谁做**：单机或服务器上的管理员。程序部署到服务器后，管理员在自己电脑浏览器里访问服务器页面来操作。

**成功标准**：

- 访问新增页面后能看到配置、运行、号池、账号、导出几个区域。
- 保存配置写入现有 `config.json`，不把密钥写到其他新文件。
- “开始”按钮启动的是 `worker_pool.py` CLI 子进程，而不是旧 `web_gui._run()` 单 worker。
- 页面能显示 worker_pool stdout/stderr 日志；停止按钮优先发送可触发 `worker_pool.py` 安全收尾的信号。
- 任意时刻最多一个 worker_pool 任务运行；重复启动返回错误。
- 号池列表能区分 `unused` / `used` / `error` / `reserved`，且能搜索邮箱、手机号、错误原因。
- 号池总览能展示邮箱池总量、累计导入新增量、最近一次导入新增/跳过/总量。
- 注册账号列表从 `results/_all.json` 读取，能按注册状态、账号状态、导出状态、是否可导出、关键词、时间范围筛选。
- 注册总览能展示成功账号数、失败账号数、Phase 1 失败数、Phase 2 失败数、可重试失败数。
- 成功注册账号默认带 `usage_status=unused` 与 `export_status=not_exported`。
- 账号总览能展示成功试用账号数，也就是 `usage_status=used` 的成功账号数。
- 人工账号状态写入 `results/account_state.json`，不改写 `_all.json`。
- 批量操作支持“选中项”和“当前筛选条件全部匹配项”两种范围，避免只能批当前页。
- SUB2API 导出从 `imports/import_*.json` 聚合，输出符合现有 `type=sub2api-data` / `accounts` 结构的 JSON。
- 导出成功后可自动把本次实际导出的账号标记为 `exported`，记录 `exported_at` 和 `export_batch_id`。
- 配置页能展示完整 `config.json` 原文；保存前必须通过 JSON 解析，解析失败不覆盖原文件。
- 失败账号列表能展示“可重试阶段”和推荐动作：Phase 1 失败重跑完整注册，Phase 2 失败优先 Phase 2-only 续跑，导出失败只重试导出。
- 阶段重试写入新结果记录或状态记录，原失败记录保留可追溯，不原地伪装成成功。

**明确不做什么**：

- 不重写 `worker_pool.py` 内部 Phase 1 / Phase 2 / 邮箱租约逻辑。
- 不把 SUB2API access/refresh token 展示在普通账号列表里；只有导出接口按用户动作下载。
- 不做多用户权限系统；多用户版仍归 `server.py` / `runner.py`。
- 不自动判断账号是否真的“已试用”；账号状态由用户人工标记。
- “成功试用账号数”只统计用户标记为 `used` 的成功账号；不会自动按导出或注册成功推断。
- 不保证所有失败都可恢复；缺少手机号、密码、session_token、activation_id 或邮箱材料的记录只能提示不可重试或只能重跑完整注册。
- 不做真实注册资源消耗型自动化测试。
- 不删除或清空现有结果、号池、imports 文件。

### 1.2 关键决策

| 决策 | 方案 | 理由 |
|---|---|---|
| 运行 worker_pool | 通过 `subprocess.Popen([sys.executable, worker_pool.py, ...])` 启动独立子进程。 | 最小侵入，不破坏 `worker_pool.py` 已验证的 CLI 与信号处理；stdout 路由也保持在子进程内部。 |
| 停止语义 | Linux/macOS 用 `terminate()` 发送 SIGTERM；Windows 尽量创建新进程组并发送 CTRL_BREAK，失败再 terminate，最后超时 kill。 | `worker_pool.py` 已处理 SIGTERM/SIGINT；优先让它 draining stop。 |
| API 放置 | 新建 `worker_control.py` 或等价新模块，提供 Flask Blueprint；`web_gui.py` 只注册 Blueprint 和新增页面入口。 | `web_gui.py` 已 1200+ 行且职责很重，继续塞大量 API/HTML 会恶化维护。 |
| 前端文件 | 新建 `public/worker-control.html`，不继续使用 `_HTML` 巨型内联字符串。 | 独立静态文件更容易迭代，避免在 Python 字符串里维护大量 UI。 |
| 账号状态与导出状态 | 新建 `results/account_state.json`，以稳定 key 保存 `usage_status`、`export_status`、备注、导出批次。 | 注册结果事实与后续人工状态分离；既能查“是否试用”，也能查“是否导出”。 |
| 核心统计 | 新增总览 API 聚合号池导入、注册结果、账号状态。 | 用户最关心的是数量总览，列表和筛选是下钻能力。 |
| 导出 SUB 格式 | 读取 `imports/import_*.json` 聚合 accounts，按选中账号或当前筛选条件返回 JSON 文件，并可自动标记已导出。 | imports 已是 SUB2API 导入格式，避免从 `_all.json` 中拼 token；导出状态可追踪重复导出。 |
| 阶段重试入口 | 新增 retry API，不把 retry 逻辑塞进账号状态 PATCH。 | 状态修改是人工标记，retry 是会消耗资源的执行动作，必须分开。 |
| 配置复用 | 沿用 `web_gui.py` 当前 `_load_config()` / `_save_config_file()` 的字段结构，必要时补齐 `concurrency` 等 UI 参数只作为启动参数，不强制写入 config。 | 避免和 `auto_register.load_config()`、`worker_pool._preflight()` 冲突。 |

### 1.3 被拒方案

| 被拒方案 | 拒绝原因 |
|---|---|
| 把 worker_pool 直接改成 Flask 内部线程调用 | 需要暴露更多内部状态和 stop_event，改动面更大；当前 CLI 已可稳定作为进程边界。 |
| 直接替换现有 `/api/start` 为 worker_pool | 会破坏旧 `web_gui.py` 单 worker 行为，难以回退。 |
| 在账号列表直接显示 access_token / refresh_token | 页面展示风险高；导出动作才需要这些凭据。 |
| 只支持当前页全选 | 批量导出时用户通常要导出“所有未导出账号”，不只是当前 50 条；必须支持按筛选条件全选。 |
| 失败记录原地改成成功 | 会丢掉失败原因和审计路径；重试成功应追加 retry 成功记录，并在原记录状态里记录 `retry_of` / `retried_by` 关系。 |
| 把“已使用/未试用”写回 `results/_all.json` | `_all.json` 是注册结果事实文件，混入人工状态会影响后续导出与排查。 |
| 使用 `public/index.html` 多用户页改造 | 该页依赖 `server.py` / PostgreSQL / auth；本 feature 是单机 worker_pool 控制台。 |

### 1.4 主流程概述

1. 用户启动 `python web_gui.py`。
2. `web_gui.py` 注册 worker 控制 Blueprint，并提供 `/worker-control` 页面。
3. 用户在页面保存配置，后端写入 `config.json`。
4. 用户导入 MsOutlook 号池，后端调用 `MsOutlookPool.import_accounts()`。
5. 每次号池导入后写入 `results/pool_import_history.json`，用于统计累计导入数量和最近导入结果。
6. 用户点击开始，后端 preflight 基础参数并启动 `worker_pool.py` 子进程。
7. 后台 reader 线程持续读取子进程 stdout/stderr，写入内存 log buffer。
8. 页面轮询 `/api/worker/log-since/<cursor>` 和 `/api/worker/status`。
9. 用户点击停止，后端向子进程发送优雅停止信号，并继续展示 draining stop 日志。
10. 注册结果由 `worker_pool.ResultWriter` 写入 `results/` 和 `imports/`。
11. 页面首页总览聚合 `号.json`、`pool_import_history.json`、`_all.json`、`account_state.json`。
12. 页面账号列表读取 `_all.json` + `account_state.json`，号池列表读取 `号.json` + `msoutlook_used.json`。
13. 用户可选择当前页账号、手工勾选账号，或对当前筛选条件全选后批量修改状态。
14. 用户按筛选或选中账号导出 SUB2API JSON，后端聚合 `imports/import_*.json` 后下载，并可自动更新导出状态。
15. 用户可对失败记录点击重试；后端根据失败阶段和可用材料选择 Phase 2-only、重新导出、或完整重跑。

## 2. 接口契约

### 2.1 任务启动

```http
POST /api/worker/start
Content-Type: application/json

{
  "count": 10,
  "concurrency": 3,
  "retry": 2,
  "create_retry": 20,
  "cooldown": 60,
  "phase2_timeout": 300,
  "max_price": ""
}
```

成功：

```json
{
  "ok": true,
  "run": {
    "id": "20260604-153000",
    "pid": 12345,
    "command": "python worker_pool.py -n 10 -c 3 --config config.json",
    "started_at": "2026-06-04T15:30:00"
  }
}
```

已有任务运行：

```json
{"ok": false, "error": "已有运行中的 worker_pool 任务"}
```

来源：`worker_pool.py:858` CLI 参数、`worker_pool.py:836` preflight、`web_gui.py:178` 现有 start API。

### 2.2 核心统计总览

```http
GET /api/dashboard/summary
```

```json
{
  "ok": true,
  "pool": {
    "total": 1000,
    "available": 700,
    "used": 250,
    "error": 30,
    "reserved": 0,
    "import_added_total": 980,
    "import_batches": 8,
    "last_import": {
      "time": "2026-06-04T15:00:00",
      "added": 120,
      "skipped": 5,
      "total": 1000
    }
  },
  "accounts": {
    "registered_success": 80,
    "registered_failed": 12,
    "fail_phase1": 4,
    "fail_phase2": 8,
    "retryable_failed": 6,
    "trial_success": 31,
    "unused_success": 49,
    "exported": 50,
    "not_exported": 30
  }
}
```

统计口径：

- `pool.total`：当前 `号.json` 中邮箱总数。
- `pool.import_added_total`：`results/pool_import_history.json` 中历史 `added` 累加；如果历史文件不存在，则显示 `null` 或按当前总量作为初始估算并标记来源。
- `registered_success`：`results/_all.json` 中 `status=ok` 的记录数。
- `registered_failed`：`status` 不是 `ok` 的记录数，至少覆盖 `fail_phase1` / `fail_phase2` / `interrupted_after_phase1`。
- `trial_success`：`status=ok` 且 `account_state.usage_status=used` 的记录数。
- `unused_success`：`status=ok` 且 `usage_status=unused` 或没有人工状态记录的账号数。

这是首页最重要的 API，页面顶部固定展示这些数字，下面的号池列表、账号列表和日志作为下钻。

### 2.3 任务停止与状态

```http
POST /api/worker/stop
```

```json
{"ok": true, "stopping": true}
```

```http
GET /api/worker/status
```

```json
{
  "ok": true,
  "running": true,
  "stopping": false,
  "pid": 12345,
  "exit_code": null,
  "started_at": "2026-06-04T15:30:00",
  "ended_at": "",
  "summary": {
    "full_success": 2,
    "phase1_failed": 1,
    "phase2_failed": 0,
    "cancelled": 1,
    "attempts": 3
  }
}
```

说明：`summary` 从日志中 `done full_success=...` 行解析；没有解析到时为空对象。

### 2.4 日志读取

```http
GET /api/worker/log-since/0
```

```json
{
  "ok": true,
  "cursor": 2,
  "lines": [
    {"time": "15:30:01", "tag": "info", "text": "[INFO] concurrency=3 target_success=10 ..."},
    {"time": "15:30:02", "tag": "success", "text": "[success] [W1] Phase 1 成功 ..."}
  ]
}
```

### 2.5 号池列表

```http
GET /api/pool/accounts?status=unused&q=outlook&limit=50&offset=0
```

```json
{
  "ok": true,
  "stats": {"total": 1000, "enabled": 980, "unused": 700, "used": 250, "error": 30, "reserved": 0},
  "total": 700,
  "items": [
    {"email": "abc@example.test", "enabled": true, "status": "unused", "phone": "", "error": "", "used_at": ""}
  ]
}
```

来源：`msoutlook_pool.py:27` 默认号池文件、`msoutlook_pool.py:133` used/error 记录、`msoutlook_pool.py:193` stats。

### 2.6 注册账号列表、账号状态与导出状态

```http
GET /api/accounts?reg_status=ok&usage_status=unused&export_status=not_exported&exportable=1&q=outlook&date_from=2026-06-04&date_to=2026-06-04&limit=50&offset=0
```

```json
{
  "ok": true,
  "stats": {"ok": 20, "fail_phase1": 3, "fail_phase2": 2, "usage_unused": 15, "usage_used": 5, "export_not_exported": 12, "exported": 8},
  "total": 15,
  "items": [
    {
      "key": "sub:1105",
      "reg_status": "ok",
      "usage_status": "unused",
      "export_status": "not_exported",
      "exportable": true,
      "phone": "+569...",
      "password": "...",
      "bind_email": "abc@example.test",
      "sub2api_id": "1105",
      "phase2_error": "",
      "saved_at": "2026-06-04T15:31:00"
    }
  ]
}
```

```http
PATCH /api/accounts/sub%3A1105/state
Content-Type: application/json

{"usage_status": "used", "export_status": "exported", "note": "已发给客户 A"}
```

```json
{"ok": true}
```

### 2.7 批量状态修改

```http
PATCH /api/accounts/batch-state
Content-Type: application/json

{
  "scope": "filtered",
  "keys": [],
  "filters": {
    "reg_status": "ok",
    "usage_status": "unused",
    "export_status": "not_exported",
    "q": "outlook"
  },
  "patch": {
    "usage_status": "reserved",
    "note": "本批先留用"
  }
}
```

返回：

```json
{"ok": true, "matched": 42, "updated": 42}
```

批量规则：

- `scope=selected` 时只处理 `keys`。
- `scope=filtered` 时处理当前筛选条件命中的全部账号，不受分页限制。
- 批量修改只允许改 `usage_status`、`export_status`、`note`，不能改注册事实字段。

### 2.8 SUB2API 导出

```http
POST /api/export/sub
Content-Type: application/json

{
  "scope": "selected",
  "keys": ["sub:1105", "phone:+569..."],
  "filters": {
    "reg_status": "ok",
    "usage_status": "unused",
    "export_status": "not_exported",
    "exportable": true,
    "q": "outlook"
  },
  "mark_exported": true
}
```

返回下载文件：`sub2api_export_YYYYMMDD_HHMMSS.json`

```json
{
  "type": "sub2api-data",
  "version": 1,
  "exported_at": "2026-06-04T15:40:00Z",
  "proxies": [],
  "accounts": [
    {
      "name": "abc@example.test",
      "platform": "openai",
      "type": "oauth",
      "credentials": {
        "access_token": "...",
        "refresh_token": "...",
        "expires_at": 1781363307,
        "email": "abc@example.test",
        "play_type": "free"
      },
      "priority": 1,
      "concurrency": 10,
      "auto_pause_on_expired": true
    }
  ],
  "_meta": {
    "export_batch_id": "export-20260604-154000",
    "selected": 20,
    "exported": 18,
    "skipped": 2
  }
}
```

导出规则：

- `scope=selected`：只导出 `keys` 中的账号。
- `scope=filtered`：导出当前筛选条件命中的全部账号，不受分页限制。
- 只导出能在 `imports/import_*.json` 中按邮箱匹配到 token 的账号。
- `mark_exported=true` 时，只对实际写入导出文件的账号标记 `export_status=exported`。
- 已标记 `ignored` 的账号默认不导出，除非请求显式覆盖。

来源：`openai_bind_email.py:1052` import payload 结构、`worker_pool.ResultWriter.append_import()`。

### 2.9 配置原文读取与保存

```http
GET /api/config/raw
```

```json
{"ok": true, "path": "config.json", "text": "{\n  ...\n}"}
```

```http
PUT /api/config/raw
Content-Type: application/json

{"text": "{\n  \"sms_provider\": \"smsbower\"\n}"}
```

成功：

```json
{"ok": true, "config": {"sms_provider": "smsbower"}}
```

JSON 解析失败：

```json
{"ok": false, "error": "JSON 解析失败: line 3 column 1"}
```

说明：保存失败时不能覆盖原 `config.json`；保存成功后刷新结构化配置表单。

### 2.10 阶段重试与续跑

#### 2.10.1 可重试能力判断

账号列表每条记录增加 `retry` 字段：

```json
{
  "key": "sub:1105",
  "reg_status": "fail_phase2",
  "phone": "+569...",
  "password": "Account.Password123",
  "bind_email": "abc@example.test",
  "session_token": "present",
  "activation_id": "",
  "retry": {
    "retryable": true,
    "recommended_stage": "phase2",
    "reason": "Phase 1 已成功，Phase 2 失败；可复用手机号/密码/session_token 尝试继续绑定邮箱和 SUB2API",
    "missing": []
  }
}
```

判断规则：

| 注册状态 / 失败点 | 推荐动作 | 必需材料 | 不满足时 |
|---|---|---|---|
| `fail_phase1` | `full` 重跑完整注册 | SMS key、号池、SUB2API 配置 | 原记录不可续跑，只能创建新账号尝试。 |
| `interrupted_after_phase1` | `phase2` 续跑 | `phone`、`password`、`session_token`、可用邮箱 | 缺任一材料则只能完整重跑。 |
| `fail_phase2` | `phase2` 续跑 | `phone`、`password`、`session_token`、可用邮箱 | 如果错误是 `account_stuck_email_otp`，提示人工处理或换邮箱重试风险。 |
| `ok` 但 `export_status=not_exported` 且缺导出 token | `export` 重建导出 / 重新 exchange-code 不一定可行 | imports 中有该邮箱账号 payload | 没有 payload 时标记为不可导出，必要时重新跑 Phase 2。 |
| `ok` 且已导出 | 默认不重试 | 用户显式选择 | 避免重复导出。 |

说明：`worker_pool.py` 当前 `fail_phase2` 记录里有 `phone/password/session_token/bind_email/phase2_error`，但未必保留 `activation_id`。续跑 Phase 2 时不能再假设能激活原 SMS 订单；如果原手机号已经过期，续跑可能只能完成 OAuth/SUB2API，不做 SMS complete。

#### 2.10.2 触发单条或批量重试

```http
POST /api/retry/start
Content-Type: application/json

{
  "scope": "selected",
  "keys": ["phone:+569..."],
  "filters": {
    "reg_status": "fail_phase2",
    "retryable": true
  },
  "stage": "auto",
  "options": {
    "concurrency": 1,
    "max_items": 10,
    "email_retries": 10,
    "oauth_retries": 3,
    "force_new_email": true
  }
}
```

返回：

```json
{
  "ok": true,
  "retry_run": {
    "id": "retry-20260604-161000",
    "stage": "phase2",
    "total": 3,
    "started_at": "2026-06-04T16:10:00"
  }
}
```

规则：

- `stage=auto` 时由后端按上表选择；也允许用户显式选 `full`、`phase2`、`export`。
- retry job 与普通 worker_pool 任务互斥，同一时间只允许一个执行任务。
- 对 `phase2` 重试，优先复用 `batch_phase2.py` 已有思路：重新获取 OAuth URL、遇到 `email_already_in_use` 换邮箱、遇到 `exchange-code` 重新 OAuth。
- 对 `full` 重试，不修改原失败记录，只启动新的 `worker_pool.py` 目标数量任务。
- 对 `export` 重试，只重建/导出 SUB2API JSON，不触发注册。

#### 2.10.3 重试结果记录

`account_state.json` 中为原记录补充 retry 元数据：

```json
{
  "phone:+569...": {
    "usage_status": "unused",
    "export_status": "not_exported",
    "retry_status": "retried_success",
    "retried_at": "2026-06-04T16:20:00",
    "retried_by": "retry-20260604-161000",
    "retry_result_key": "sub:1120",
    "last_retry_error": ""
  }
}
```

新产生的成功记录正常进入 `results/_all.json` 和 `imports/import_*.json`；原失败记录保留。

### 2.11 前端组件拆分

新增页面 `public/worker-control.html` 内部按区域组织：

- `SummaryPanel`：顶部核心数字，总览导入邮箱池数量、注册成功、注册失败、成功试用、未导出账号。
- `ConfigPanel`：结构化配置保存、完整 `config.json` 原文查看/编辑、JSON 校验、余额检查。
- `RunPanel`：目标数、并发数、重试参数、开始/停止、状态。
- `PoolPanel`：号池统计、导入、筛选表格。
- `AccountsPanel`：注册结果表格、条件查询、当前页选择、按筛选全选、批量状态更新、导出状态展示。
- `ExportPanel`：按选中项或当前筛选条件批量导出 SUB2API JSON，可选择导出后标记 exported。
- `RetryPanel`：展示可重试失败记录、推荐阶段、缺失材料、单条/批量重试入口。
- `LogPanel`：实时日志。

状态归属：全部为页面本地 JS 状态；不引入构建工具或全局 store。

## 3. 实现提示

### 3.1 目标文件状况评估

`web_gui.py` 当前约 1236 行，包含 Flask API、旧 worker 编排和大型内联 HTML，已经承担多项职责。本 feature 不应继续把大量控制台逻辑塞进 `_HTML`。推荐第一步做小型抽取：新增独立模块和静态 HTML，只在 `web_gui.py` 中追加少量注册代码。

### 3.2 改动计划

- 新建 `worker_control.py`：封装 worker_pool 子进程控制、日志缓冲、核心统计、账号/号池/状态/导出/原始配置/阶段重试 API。
- 修改 `web_gui.py`：注册 Blueprint，增加 `/worker-control` 静态页面入口；旧 `/` 不强行替换。
- 新建 `public/worker-control.html`：控制台页面。
- 必要时给 `msoutlook_pool.py` 增加纯读取 helper，或在 `worker_control.py` 中实现聚合读取，避免影响现有类。
- 新建测试文件 `test_worker_control.py`：覆盖核心统计、纯数据聚合、账号状态持久化、批量操作、SUB2API 导出聚合、原始配置 JSON 校验、阶段重试判定和启动参数构造。

### 3.3 实现风险与约束

- 停止按钮无法保证立即退出；应显示“正在停止”，让 `worker_pool.py` 自己做安全收尾。
- Windows 发送 CTRL_BREAK 需要新进程组；Linux 用 SIGTERM。实现时要做平台分支。
- `imports/` 中有敏感 token，账号列表不展示；导出接口只在用户主动点击时下载。
- `号.json` 很大，号池列表必须支持分页和搜索，不能一次性把全部渲染到 DOM。
- `_all.json` 可能很大且可能有损坏历史项；读取函数要容错，坏项跳过或返回明确错误。
- 真实注册测试会消耗资源，本轮只做无资源消耗测试和手工页面启动检查。

### 3.4 推进顺序

1. **新增后端控制模块**：实现 `WorkerProcessController`、日志缓冲、启动/停止/状态 API。退出信号：能用 fake 命令或 `--help` 命令完成启动/日志/结束状态测试。
2. **新增核心统计与数据聚合 API**：实现 dashboard summary、号池列表、注册账号列表、账号状态/导出状态读写、原始配置读取保存。退出信号：单元测试可用临时 `号.json`、`pool_import_history.json`、`msoutlook_used.json`、`_all.json`、`account_state.json` 验证统计口径、状态计算和 JSON 校验。
3. **新增批量操作与 SUB2API 导出 API**：支持选中项/筛选全选的批量状态修改，聚合 `imports/import_*.json` 并按账号筛选导出，导出后可标记 exported。退出信号：测试验证导出 JSON 结构、筛选数量、状态写入和跳过不可导出账号。
4. **新增阶段重试 API**：实现 retryable 判定、Phase 2-only 续跑任务壳、完整重跑入口和导出重试入口。退出信号：测试验证不同失败状态给出正确推荐动作和缺失材料说明；fake retry job 可写入 retry 元数据。
5. **接入 web_gui.py**：注册 Blueprint 和 `/worker-control` 页面入口。退出信号：`python -m py_compile web_gui.py worker_control.py` 通过。
6. **实现前端页面**：顶部统计、配置原文编辑、运行、号池、账号条件查询/全选/批量状态、阶段重试、导出、日志几个区域联调 API。退出信号：浏览器能打开 `/worker-control`，顶部统计和按钮/表格能完成基本交互。
7. **验证与收尾**：运行单元测试、py_compile；不跑真实注册，除非用户明确要求。退出信号：列出已验证项和未覆盖风险。

### 3.5 测试设计

| 功能点 | 验证方式 | 关键用例 |
|---|---|---|
| 启动参数构造 | 单元测试 | count/concurrency/retry/max_price 转成正确 CLI 参数；并发 >10 由 worker_pool preflight 处理。 |
| 停止语义 | 单元测试 + 手工 | fake 长任务启动后 stop，状态变为 stopping，最终 exit_code 非空。 |
| 核心统计 | 单元测试 | 导入历史累计 added；`_all.json` 中 ok/fail_phase1/fail_phase2 计数；`account_state.usage_status=used` 计入成功试用账号。 |
| 号池状态聚合 | 单元测试 | `号.json` 三个邮箱 + used/error/reserved 记录，输出对应状态和统计。 |
| 账号状态与导出状态 | 单元测试 | 成功账号初始 `usage_status=unused`、`export_status=not_exported`；PATCH 后状态变更；刷新后仍存在。 |
| 条件查询与全选范围 | 单元测试 | reg_status/usage_status/export_status/q/date 条件组合正确；批量操作支持 keys 和 filtered 两种范围。 |
| 账号列表脱敏 | 单元测试 | 列表不返回 `access_token`。 |
| SUB 导出 | 单元测试 | 多个 import 文件聚合；按 usage_status/export_status 只导出未试用且未导出的账号；导出成功后标记 exported。 |
| 原始配置编辑 | 单元测试 | GET 返回完整 config.json；PUT 合法 JSON 保存成功；非法 JSON 不覆盖原文件。 |
| 阶段重试判定 | 单元测试 | fail_phase1 推荐 full；fail_phase2 材料齐全推荐 phase2；缺 password/session_token 时提示不可续跑；ok 缺导出 payload 推荐 export 或不可导出。 |
| 重试结果记录 | 单元测试 | fake retry 成功后原失败记录保留，并在 account_state 中记录 retried_by/retry_result_key；失败时记录 last_retry_error。 |
| 页面联调 | 手工 | 打开 `/worker-control`，保存配置、导入号池、查询列表、导出文件。 |

## 4. 与项目级架构文档的关系

需要更新 `easysdd/architecture/DESIGN.md`：

- 在 Web GUI 模块说明中补充 `worker_control.py` / `public/worker-control.html` 是单用户 worker_pool 控制台。
- 保留 `web_gui.py` 旧单 worker GUI 与 `server.py` 多用户服务的边界说明。
- 标注 `worker_pool.py` 仍是注册编排权威入口，控制台只是进程控制与结果管理层。
