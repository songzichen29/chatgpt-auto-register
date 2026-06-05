---
doc_type: feature-acceptance
feature: 2026-06-03-concurrent-register
status: accepted
summary: 验收并发注册 CLI worker 池实现，确认接口契约、行为约束、测试覆盖、术语一致性和架构归并均已闭环
tags: [cli, concurrency, register, acceptance]
created: 2026-06-04
design: 2026-06-03-concurrent-register-design.md
checklist: 2026-06-03-concurrent-register-checklist.yaml
---

# 并发注册 — 独立 worker 池程序验收报告

> 阶段：阶段 3（验收闭环）
> 验收日期：2026-06-04
> 关联方案 doc：`easysdd/features/2026-06-03-concurrent-register/2026-06-03-concurrent-register-design.md`

## 1. 接口契约核对

对照方案 doc 第 2 节接口契约，逐项核查实现与契约一致性。

**CLI 契约**：

- [x] `python worker_pool.py -n COUNT -c C --config config.json` 入口存在，`-n/--count`、`-c/--concurrency`、`--config`、`--cooldown`、`--phase2-timeout` 等参数可通过 `python worker_pool.py --help` 查看。
- [x] `concurrency` 硬上限 10 已落地：`worker_pool.py:_preflight()` 对 `<1` 或 `>10` 直接返回 `concurrency must be between 1 and 10`；实测 `python worker_pool.py -n 1 -c 11 --config config.example.json` 退出码为 2。
- [x] 缺少 SUB2API 或 MsOutlook 配置时 preflight 失败，不会进入 Phase 1：实测 `python worker_pool.py -n 1 -c 1 --config config.example.json` 只输出配置缺失错误并退出码为 2。

**ThreadStdoutRouter 契约**：

- [x] `ThreadStdoutRouter.install_once()` 只在 main 初始化阶段安装稳定 stdout 代理。
- [x] `capture_current_thread()` 按 thread id 分发输出，单测 `test_thread_stdout_router_routes_by_thread` 覆盖两个线程并发 `print()` 不串线。
- [x] `worker_pool.py` 无 `contextlib.redirect_stdout` / `redirect_stdout` 命中。

**EmailAllocator / EmailLease 契约**：

- [x] `EmailAllocator.acquire()` 在锁内执行冷却清理、取邮箱、临时 `mark_used(..., phone="reserved:W{wid}")`，返回唯一 `EmailLease`。
- [x] `EmailLease` 最终状态只允许三类：`mark_used(phone)`、`mark_error(reason)`、`release(cooldown)`；二次收尾直接忽略。
- [x] `email_already_in_use` 时旧 lease `mark_error("email_already_in_use")`，新 lease 成为 active lease；单测 `test_phase2_email_already_in_use_switches_to_new_lease` 覆盖旧邮箱 error、新邮箱成功。

**ResultWriter 契约**：

- [x] `append_account()` 线程安全写单账号文件与 `results/_all.json`。
- [x] `append_account()` 过滤 `access_token`、`api_key`、`sub2api_password`、`cookie`、`cookies`。
- [x] `append_account()` 成功、`fail_phase1`、`fail_phase2`、`interrupted_after_phase1` 都保留注册账号 `password`，便于后续人工续跑。
- [x] `append_import()` 线程安全写 `imports/import_YYYYMMDD.json`，兼容直接对象与 `{data:{accounts:[]}}` 两种格式。

**Phase2Outcome / `_run_phase2_with_retry()` 契约**：

- [x] `Phase2Outcome` 包含 `ok/final_email/lease/sub2api_id/import_data/error/retry_count`。
- [x] `_run_phase2_with_retry()` 对 SUB2API 登录、OAuth URL 生成、`run_second_half(save_import=False, interactive_input=False)`、换邮箱和可重试错误分层处理。
- [x] `run_second_half(save_import=False)` 在 worker 场景不直接写 imports，只返回 `import_data` 给 `ResultWriter.append_import()`。
- [x] `interactive_input=False` 时绑定验证码轮询超时直接返回错误，不进入 `input()` 阻塞；`worker_pool.py` 调用处固定传入 `interactive_input=False`。

**流程图 / 时序图落点核对**：

- [x] 方案中的 Main → Router → Allocator → Worker → Phase 1 → Phase 2 → SMS → Writer 均有代码落点：`ThreadStdoutRouter`、`EmailAllocator`、`ResultWriter`、`worker()`、`_run_phase2_with_retry()`、`_sms_action()`。
- [x] Ctrl+C draining stop 有代码落点：signal handler 设置 `global_stop`，worker 在 Phase 1 后、Phase 2 前检查 stop；已进入 Phase 2 的账号等待返回后按结果 `complete` 或 `cancel`。

## 2. 行为与决策核对

**需求摘要逐项验证**：

- [x] 新增独立 CLI 文件 `worker_pool.py`，不把并发入口塞进 `web_gui.py`。
- [x] 每个 worker 独立获取邮箱租约，Phase 1 调用 `auto_register.register_one(..., auto_activate=False)`，由 worker 在 Phase 2 后决定 `complete` / `cancel`。
- [x] `--count/-n` 只统计完整成功：当前代码只有在 Phase 2 成功、`complete()` 成功、`append_account(status=ok)` 和 `append_import()` 执行路径之后才 `record_success()`。
- [x] 验收中发现并修正一处边界偏差：原实现中 Phase 2 成功但 `complete()` 异常时仍可能计入完整成功；现已改为写 `status=fail_phase2`、记录 `phase2_error=complete failed...`，且不增加 `full_success`，并新增单测 `test_worker_complete_failure_is_not_counted_as_success`。
- [x] Phase 1 成功但 Phase 2 失败会保存 `status=fail_phase2`，号码 `cancel()`，不增加 `full_success`；单测 `test_worker_phase2_failure_saves_fail_phase2_without_success` 覆盖。
- [x] Phase 1 成功但 Ctrl+C 已触发且尚未进入 Phase 2 时，保存 `status=interrupted_after_phase1`、`cancel()` 号码、释放邮箱；单测 `test_worker_ctrl_c_after_phase1_saves_interrupted_and_cancels` 覆盖。

**明确不做逐项核对**：

- [x] 本 feature 未引入 Web UI；交付入口为 `worker_pool.py` CLI。
- [x] 本 feature 未重写 Phase 1 主流程；仍复用 `auto_register.register_one()`。
- [x] 本 feature 未做 iCloud alias 并发创建；并发邮箱来源限定为 `MsOutlookPool`。
- [x] 本 feature 未做 Phase 1-only 模式；preflight 要求 SUB2API 与 MsOutlook helper 配置完整。
- [x] 本 feature 未引入新的敏感信息持久化；账号结果写入过滤关键敏感字段。
- [x] 范围说明：当前仓库里 `web_gui.py`、`runner.py`、`server.py` 存在既有未提交/已暂存变更，但本次 `concurrent-register` 验收未继续改造这些文件；本 feature 的实现与验收聚焦 `worker_pool.py`、`openai_bind_email.py`、`test_worker_pool.py` 与 easysdd 文档。
- [x] 配置读取补正：`auto_register.load_config()` 已合并 `config.json` 的 `register` 段，`worker_pool.py` 现在能读取 `register.password`；若为空则仍走随机密码。
- [x] 邮箱状态补正：`msoutlook_used.json` 的 `used` / `error` 记录保留 `phone` 和注册账号 `password`，便于根据邮箱反查账号材料。

**关键决策落地**：

- [x] 新增入口：`worker_pool.py` 已落地。
- [x] worker 模型：使用 `threading.Thread(..., daemon=False)` 和共享 `RunState`、`EmailAllocator`、`ResultWriter`。
- [x] stdout 处理：稳定 `ThreadStdoutRouter`，不在线程内切换全局 stdout。
- [x] 成功计数：`RunState.record_success()` 只在完整成功路径调用。
- [x] 邮箱状态：`EmailLease` 三态收尾，`email_already_in_use` 使用 `mark_error()`，普通失败使用 `release(cooldown)`。
- [x] imports 写入：`run_second_half(save_import=False)` + `ResultWriter.append_import()` 单一写入点。
- [x] Ctrl+C：设置 `global_stop` 后安全排空，不使用 daemon worker 或固定超时强杀。

## 3. 测试约束核对

对照方案 doc 第 3.5 节测试设计：

- [x] stdout 路由：`test_thread_stdout_router_routes_by_thread` 覆盖两个线程输出归属。
- [x] 邮箱原子分配：`test_email_allocator_concurrent_acquire_and_cooling` 覆盖 10 线程并发 acquire 无重复。
- [x] 邮箱冷却：同一测试覆盖 release 后冷却期内不会重新 acquire 到该邮箱。
- [x] 邮箱最终状态：`test_phase2_email_already_in_use_switches_to_new_lease` 覆盖旧 lease error、新 lease 成功。
- [x] ResultWriter 并发：`test_result_writer_concurrent_account_and_import_writes` 覆盖 10 线程账号写入与 2 线程 imports 写入不丢记录。
- [x] 配置密码读取：`test_load_config_merges_register_password` 覆盖 `config.json` 的 `register.password/name/birthdate` 合并。
- [x] 密码落盘：`test_worker_success_records_password_in_results_and_msoutlook_used` 覆盖成功结果和 `msoutlook_used.json` 都记录 `password`；失败/中断测试也断言结果文件保留 `password`。
- [x] exchange-code 重试：`test_run_second_half_exchange_code_retries_retryable_status` 覆盖 500/503 后成功重试；`test_run_second_half_exchange_code_does_not_retry_400` 覆盖 400 不重试。
- [x] 成功计数：`test_worker_phase2_failure_saves_fail_phase2_without_success` 和 `test_worker_complete_failure_is_not_counted_as_success` 覆盖 Phase 2 失败 / complete 失败均不计入完整成功。
- [x] Ctrl+C：`test_worker_ctrl_c_after_phase1_saves_interrupted_and_cancels` 覆盖 Phase 1 后 stop 不进入 Phase 2、保存 interrupted、取消号码。
- [x] 范围守护：`rg "contextlib\.redirect_stdout|redirect_stdout" worker_pool.py` 无命中。

**实际执行的验证命令**：

```text
python -m py_compile openai_bind_email.py worker_pool.py test_worker_pool.py
python -m unittest test_worker_pool.py
python worker_pool.py --help
python worker_pool.py -n 1 -c 11 --config config.example.json
python worker_pool.py -n 1 -c 1 --config config.example.json
python easysdd\tools\validate-yaml.py --file easysdd\features\2026-06-03-concurrent-register\2026-06-03-concurrent-register-design.md --require doc_type --require feature --require status --require summary --require tags
python easysdd\tools\validate-yaml.py --file easysdd\features\2026-06-03-concurrent-register\2026-06-03-concurrent-register-checklist.yaml --yaml-only
rg "contextlib\.redirect_stdout|redirect_stdout" worker_pool.py
```

**未执行的资源型验证**：

- 未跑真实 `python worker_pool.py -n 1 -c 1` 完整注册、`-n 2 -c 2` 双 worker 真实注册、真实 Ctrl+C 注册中断，因为会消耗手机号、邮箱和 SUB2API 资源。当前验收用组件级 fake/stub 测试和 CLI preflight 验证覆盖并发、状态机、写入和中断语义。

## 4. 术语一致性

对照方案 doc 第 0 节术语约定：

- `Phase 1`：实现仍通过 `auto_register.register_one(..., auto_activate=False)` 表达；未改 Phase 1 主流程。
- `Phase 2`：实现由 `_run_phase2_with_retry()` 调用 `openai_bind_email.run_second_half()`。
- `完整成功` / `full_success`：代码中以 `RunState.full_success`、`record_success()` 表达；验收修正了 `complete()` 失败仍计数的边界偏差。
- `Phase 2 失败账号` / `fail_phase2`：`worker_pool.py` 和测试中均使用 `fail_phase2`，未出现替代状态名。
- `EmailLease`、`ThreadStdoutRouter`、`ResultWriter`、`interactive_input`、`draining stop`：代码与方案术语一致。
- 防冲突：`worker_pool.py` 中无 `contextlib.redirect_stdout` / `redirect_stdout`，无 `mark_failed`。

## 5. 架构归并

对照方案 doc 第 4 节，已实际更新架构中心入口：

- [x] `easysdd/architecture/DESIGN.md` 核心模块表新增：

```markdown
| 并发注册 CLI | `worker_pool.py` | 独立命令行 worker 池，并发编排 Phase 1 + Phase 2，负责线程安全邮箱租约、结果写入和安全中断。 |
```

归并结论：本 feature 新增长期有效的 CLI 编排模块，已进入架构总入口；未新增单独子系统架构文档，当前一行模块索引足够支撑后续 feature-design 定位。

## 6. 遗留

- 资源型验收未执行：真实注册、真实双 worker、真实 Ctrl+C 仍需在用户确认消耗资源后执行。
- `worker_pool.py` 是独立 CLI；Web GUI / 多用户服务是否复用该并发池属于后续新 feature，不纳入本轮。
- `ResultWriter` 仍按方案只保证并发读-改-写不丢数据，未优化 `results/_all.json` 文件体积。
- 当前仓库已有较多与本 feature 无关的未提交/已暂存变更；后续提交应做 scoped commit，只纳入本 feature 相关代码和文档。
