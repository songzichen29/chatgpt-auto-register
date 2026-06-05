# ChatGPT Auto Register — 架构设计

> 更新日期：2026-06-03 | 维护者：项目贡献者

## 项目简介

ChatGPT 自动注册工具，基于手机号接码 + iCloud/MsOutlook 邮箱，自动化完成 OpenAI 账号注册、绑邮箱、上传 SUB2API 的全流程。

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 注册引擎 | `auto_register.py` | Phase 1：手机号注册到拿 session_token |
| 协议层 | `chatgpt_register.py` | ChatGPT/OpenAI 协议交互 |
| 接码平台 | `phone_sms.py` | 统一接码平台接口（smsbower/hero-sms/5sim） |
| Phase 2 | `openai_bind_email.py` | OAuth + 绑邮箱 + SUB2API 上传 |
| 并发注册 CLI | `worker_pool.py` | 独立命令行 worker 池，并发编排 Phase 1 + Phase 2，负责线程安全邮箱租约、结果写入和安全中断。 |
| 邮箱号池 | `msoutlook_pool.py` | MsOutlook 号池管理 |
| iCloud 别名 | `icloud_hme.py` | iCloud Hide My Email 客户端 |
| Web GUI | `web_gui.py` | 单用户 Web 界面 + 单 worker 编排 |
| 多用户服务 | `server.py` + `runner.py` | 多用户 Flask + 注册引擎 |
| 数据库 | `db.py` | PostgreSQL CRUD（多用户版） |
| 配置 | `config.py` / `config.json` | 全局配置 |
| 日志 | `file_logger.py` | stdout 拦截 + 文件日志 |

## 待补充

- [ ] 模块间调用关系图
- [ ] 数据流图
- [ ] 并发模型分析
- [ ] 部署架构
