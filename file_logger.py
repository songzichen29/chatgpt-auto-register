#!/usr/bin/env python3
"""文件日志系统 - 透明拦截 stdout 输出并写入按日分割的日志文件。

零侵入集成：现有 print() 调用无需任何修改，所有输出自动同时写入文件。
"""

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 默认配置 ──

_DEFAULTS = {
    "enabled": True,
    "dir": "logs",
    "retention_days": 30,
    "sanitize": True,
}

# ── 全局状态 ──

_initialized = False
_config = dict(_DEFAULTS)
_log_dir: Optional[Path] = None
_write_queue: queue.Queue = queue.Queue(maxsize=10000)
_writer_thread: Optional[threading.Thread] = None
_stdout_tee: Optional["_StdoutTee"] = None
_original_stdout = None
_current_file_handle = None
_current_file_date = ""
_flush_counter = 0
_flush_lock = threading.Lock()
_shutdown_event = threading.Event()

# ── 敏感信息正则（编译一次） ──

_SANITIZE_PATTERNS = [
    # session_token=xxx 或 session_token: xxx
    (re.compile(r'(session_token[=:]\s*)\S+'), r'\1***'),
    # password: xxx 或 密码: xxx
    (re.compile(r'((?:password|密码)[=:]\s*)\S+'), r'\1***'),
    # api_key": "xxx" 或 api_key=xxx 或 api_key: xxx
    (re.compile(r'((?:api_key)(?:"\s*:\s*"|[=:]\s*))\S+'), r'\1***'),
    # Authorization: Bearer xxx
    (re.compile(r'(Authorization:\s*Bearer\s+)\S+'), r'\1***'),
    # access_token: xxx（保留前 8 位）
    (re.compile(r'(access_token[=:]\s*)(\S{8})\S+'), r'\1\2***'),
    # refresh_token: xxx
    (re.compile(r'(refresh_token[=:]\s*)\S+'), r'\1***'),
    # imap_pass/imap_password 相关
    (re.compile(r'((?:imap_pass|imap_password)[=:]\s*)\S+'), r'\1***'),
    # sub2api_password 相关
    (re.compile(r'((?:sub2api_password)[=:]\s*)\S+'), r'\1***'),
]

# ── 模块检测规则（有序，匹配第一个即停止） ──

_MODULE_RULES = [
    (re.compile(r'\[\d{2}\]|\[\^\^\]'), "chatgpt_register"),
    (re.compile(r'\[iCloud\]'), "icloud"),
    (re.compile(r'\[AUTH\]'), "oauth"),
    (re.compile(r'\[sms\]|\[hero-sms\]|\[5sim\]'), "sms"),
    (re.compile(r'\[Scheduler\]'), "scheduler"),
    (re.compile(r'\[MsOutlook\]'), "msoutlook"),
    (re.compile(r'\[MailManage\]'), "mailmanage"),
    (re.compile(r'\[W\d\]'), "web_gui"),
]

# ── 级别检测规则（有序，匹配第一个即停止） ──

_LEVEL_RULES = [
    (re.compile(r'成功|完成|OK:|已创建|已保留|已删除|已上传'), "SUCCESS"),
    (re.compile(r'失败|Error|错误|超时|异常|未找到|FAIL'), "ERROR"),
    (re.compile(r'警告|跳过|回退|重试|注意|warn'), "WARN"),
]


# ============================================================
# 公共接口
# ============================================================


def init(config: dict = None):
    """初始化文件日志系统。幂等，多次调用无副作用。"""
    global _initialized, _config, _log_dir, _stdout_tee, _original_stdout, _writer_thread

    if _initialized:
        return

    # 加载配置
    _config = _load_config(config)

    if not _config.get("enabled", True):
        return

    # 创建日志目录
    _log_dir = Path(_config["dir"])
    if not _log_dir.is_absolute():
        _log_dir = Path(__file__).parent / _log_dir
    _log_dir.mkdir(parents=True, exist_ok=True)

    # 安装 stdout 包装器
    _original_stdout = sys.stdout
    _stdout_tee = _StdoutTee(_original_stdout)
    sys.stdout = _stdout_tee

    # 启动后台写线程
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="file-logger")
    _writer_thread.start()

    # 清理旧日志
    _cleanup_old_logs()

    _initialized = True


def shutdown():
    """刷盘并还原 stdout。"""
    global _initialized, _current_file_handle

    if not _initialized:
        return

    _shutdown_event.set()

    # 投递哨兵值让写线程退出
    _write_queue.put(None)

    # 等待写线程结束（最多 2 秒）
    if _writer_thread and _writer_thread.is_alive():
        _writer_thread.join(timeout=2)

    # 关闭文件句柄
    if _current_file_handle:
        try:
            _current_file_handle.close()
        except Exception:
            pass
        _current_file_handle = None

    # 还原 stdout
    if _original_stdout is not None:
        sys.stdout = _original_stdout

    _initialized = False
    _shutdown_event.clear()


def write_log(level: str, module: str, message: str):
    """直接写入结构化日志条目。

    Args:
        level: 级别，info/success/warn/error
        module: 模块标识，如 auto_register、runner
        message: 日志文本
    """
    if not _initialized:
        return
    line = _format_line(level.upper(), module, message)
    try:
        _write_queue.put_nowait(line)
    except queue.Full:
        pass  # 队列满时丢弃，避免阻塞调用方


# ============================================================
# stdout 包装器
# ============================================================


class _StdoutTee:
    """透明拦截 stdout：输出原样传递给终端，同时按行投递到日志队列。"""

    def __init__(self, original):
        self._original = original
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        # 原样输出到终端
        self._original.write(s)
        # 按行投递到日志队列
        if not _initialized or not s:
            return
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                idx = self._buf.index("\n")
                line = self._buf[:idx].rstrip()
                self._buf = self._buf[idx + 1:]
                if line:
                    _enqueue_line(line)

    def flush(self):
        with self._lock:
            if self._buf.rstrip():
                _enqueue_line(self._buf.rstrip())
                self._buf = ""
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


# ============================================================
# 内部函数
# ============================================================


def _load_config(explicit_config: dict = None) -> dict:
    """按优先级合并配置：显式参数 > config.json > 默认值。"""
    merged = dict(_DEFAULTS)

    # 尝试从 config.json 读取
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            if "logging" in file_cfg and isinstance(file_cfg["logging"], dict):
                merged.update(file_cfg["logging"])
        except Exception:
            pass

    # 显式参数最高优先级
    if explicit_config and "logging" in explicit_config:
        if isinstance(explicit_config["logging"], dict):
            merged.update(explicit_config["logging"])
    elif explicit_config and isinstance(explicit_config, dict):
        # 兼容直接传 logging 配置字典
        if any(k in explicit_config for k in ("dir", "retention_days", "enabled", "sanitize")):
            merged.update(explicit_config)

    return merged


def _enqueue_line(line: str):
    """将一行 stdout 文本投递到写入队列（自动检测级别和模块）。"""
    level = _detect_level(line)
    module = _detect_module(line)
    if _config.get("sanitize", True):
        line = _sanitize(line)
    formatted = _format_line(level, module, line)
    try:
        _write_queue.put_nowait(formatted)
    except queue.Full:
        pass


def _writer_loop():
    """后台线程：从队列取出日志行并追加到当日文件。"""
    global _current_file_handle, _current_file_date, _flush_counter

    while True:
        try:
            item = _write_queue.get(timeout=1)
        except queue.Empty:
            # 定期刷盘
            with _flush_lock:
                if _current_file_handle:
                    try:
                        _current_file_handle.flush()
                    except Exception:
                        pass
            continue

        # 哨兵值，退出信号
        if item is None:
            break

        try:
            fh = _get_file_handle()
            if fh:
                fh.write(item + "\n")
                _flush_counter += 1
                # 每 50 行刷盘一次
                if _flush_counter >= 50:
                    _flush_counter = 0
                    try:
                        fh.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    # 退出前最终刷盘
    with _flush_lock:
        if _current_file_handle:
            try:
                _current_file_handle.flush()
            except Exception:
                pass


def _get_file_handle():
    """获取当日日志文件句柄，跨日自动轮转。"""
    global _current_file_handle, _current_file_date

    today = datetime.now().strftime("%Y-%m-%d")

    if _current_file_date != today:
        # 关闭旧文件
        if _current_file_handle:
            try:
                _current_file_handle.close()
            except Exception:
                pass
        # 打开新文件
        log_path = _log_dir / f"{today}.log"
        try:
            _current_file_handle = open(log_path, "a", encoding="utf-8")
            _current_file_date = today
        except Exception:
            _current_file_handle = None
            _current_file_date = ""

    return _current_file_handle


def _detect_level(line: str) -> str:
    """从 stdout 文本推断日志级别。"""
    for pattern, level in _LEVEL_RULES:
        if pattern.search(line):
            return level
    return "INFO"


def _detect_module(line: str) -> str:
    """从 stdout 文本推断模块标识。"""
    for pattern, module in _MODULE_RULES:
        if pattern.search(line):
            return module
    return "auto_register"


def _sanitize(text: str) -> str:
    """过滤敏感信息。"""
    for pattern, replacement in _SANITIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _format_line(level: str, module: str, message: str) -> str:
    """格式化日志行。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{ts} [{level}] [{module}] {message}"


def _cleanup_old_logs():
    """清理超过保留天数的旧日志文件。"""
    if not _log_dir or not _log_dir.exists():
        return

    try:
        retention_days = int(_config.get("retention_days", 30))
    except (ValueError, TypeError):
        retention_days = 30

    cutoff = datetime.now() - timedelta(days=retention_days)

    try:
        for f in _log_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix != ".log":
                continue
            # 从文件名解析日期：YYYY-MM-DD.log
            try:
                date_str = f.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                continue
    except Exception:
        pass


# 启动时注册 atexit 自动关闭
import atexit
atexit.register(shutdown)
