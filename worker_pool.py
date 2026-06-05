#!/usr/bin/env python3
"""独立 CLI worker 池：并发执行完整 ChatGPT 注册流程。"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

import auto_register as ar
from msoutlook_pool import DEFAULT_POOL_PATH, DEFAULT_USED_FILE, MsOutlookPool, load_used_set
from openai_bind_email import run_second_half
from phone_sms import PhoneSMS


ROOT = Path(__file__).resolve().parent
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
RETRYABLE_PHASE2_KEYWORDS = (
    "ssl",
    "connection",
    "timeout",
    "proxy",
    "eof",
    "429",
    "500",
    "502",
    "503",
    "504",
    "exchange-code",
)
FATAL_PHASE1_KEYWORDS = (
    "NO_BALANCE",
    "NO_NUMBERS",
    "BAD_KEY",
    "API Key",
    "余额",
)
FATAL_EMAIL_KEYWORDS = (
    "compromised",
    "invalid_grant",
    "security interrupt",
)


class ThreadStdoutRouter:
    """稳定 stdout 代理：按线程 id 把 print 输出分发给 worker logger。"""

    _installed: Optional["ThreadStdoutRouter"] = None
    _install_lock = threading.Lock()

    def __init__(self, original):
        self.original = original
        self._lock = threading.RLock()
        self._callbacks: dict[int, Callable[[str], None]] = {}
        self._buffers: dict[int, str] = {}
        self._local = threading.local()

    @classmethod
    def install_once(cls) -> "ThreadStdoutRouter":
        with cls._install_lock:
            if cls._installed is None:
                cls._installed = cls(sys.stdout)
                sys.stdout = cls._installed
            return cls._installed

    def restore(self) -> None:
        with self._install_lock:
            if sys.stdout is self:
                sys.stdout = self.original
            type(self)._installed = None

    @contextmanager
    def capture_current_thread(self, callback: Callable[[str], None]):
        tid = threading.get_ident()
        with self._lock:
            previous = self._callbacks.get(tid)
            self._callbacks[tid] = callback
        try:
            yield
        finally:
            self._flush_thread(tid)
            with self._lock:
                if previous is None:
                    self._callbacks.pop(tid, None)
                else:
                    self._callbacks[tid] = previous

    def write(self, text: str) -> int:
        if not text:
            return 0
        if getattr(self._local, "in_callback", False):
            return self.original.write(text)

        tid = threading.get_ident()
        with self._lock:
            callback = self._callbacks.get(tid)
        if callback is None:
            return self.original.write(text)

        with self._lock:
            buf = self._buffers.get(tid, "") + text
            lines = buf.split("\n")
            self._buffers[tid] = lines.pop() if lines else ""

        for line in lines:
            if line.strip():
                self._emit(callback, line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass

    def _flush_thread(self, tid: int) -> None:
        with self._lock:
            callback = self._callbacks.get(tid)
            pending = self._buffers.pop(tid, "")
        if callback and pending.strip():
            self._emit(callback, pending.rstrip("\r"))

    def _emit(self, callback: Callable[[str], None], line: str) -> None:
        self._local.in_callback = True
        try:
            callback(line)
        finally:
            self._local.in_callback = False


class EmailLease:
    """一次邮箱占用租约。"""

    def __init__(self, allocator: "EmailAllocator", email: str, wid: int):
        self.allocator = allocator
        self.email = email
        self.wid = wid
        self._done = False
        self._lock = threading.Lock()

    @property
    def finalized(self) -> bool:
        with self._lock:
            return self._done

    def mark_used(self, phone: str = "", password: str = "") -> None:
        with self._lock:
            if self._done:
                return
            self.allocator._mark_used(self.email, phone, password)
            self._done = True

    def mark_error(self, reason: str, phone: str = "", password: str = "") -> None:
        with self._lock:
            if self._done:
                return
            self.allocator._mark_error(self.email, reason, phone, password)
            self._done = True

    def release(self, cooldown: float = 60.0) -> None:
        with self._lock:
            if self._done:
                return
            self.allocator._release(self.email, cooldown)
            self._done = True


class EmailAllocator:
    """MsOutlook 号池的线程安全租约包装。"""

    def __init__(
        self,
        helper_url: str,
        extra_used: Optional[set[str]] = None,
        pool_path: str = DEFAULT_POOL_PATH,
        used_file: str = DEFAULT_USED_FILE,
        helper_mode: str = "http",
        helper_script: str = "",
    ):
        self.pool = MsOutlookPool(
            pool_path=pool_path,
            helper_url=helper_url,
            used_file=used_file,
            verbose=False,
            extra_used=extra_used or set(),
            helper_mode=helper_mode,
            helper_script=helper_script,
        )
        self.lock = threading.RLock()
        self._cooling: dict[str, float] = {}

    def acquire(self, wid: int) -> EmailLease:
        with self.lock:
            self.drain_cooling()
            email = self.pool.get_available_email()
            if not email:
                raise RuntimeError("无可用邮箱")
            self.pool.mark_used(email, phone=f"reserved:W{wid}")
            return EmailLease(self, email, wid)

    def drain_cooling(self, force: bool = False) -> None:
        with self.lock:
            now = time.time()
            expired = [
                email for email, expire_at in self._cooling.items()
                if force or expire_at <= now
            ]
            for email in expired:
                self.pool.mark_unused(email)
                self._cooling.pop(email, None)

    def close(self) -> None:
        # 进程退出前释放所有普通失败归还的邮箱，避免 reserved 状态永久残留。
        self.drain_cooling(force=True)

    def has_account(self, email: str) -> bool:
        return self.pool.get_account(email) is not None

    def stats(self) -> dict:
        return self.pool.stats()

    def _mark_used(self, email: str, phone: str = "", password: str = "") -> None:
        with self.lock:
            self._cooling.pop(email, None)
            self.pool.mark_used(email, phone=phone, password=password)

    def _mark_error(self, email: str, reason: str, phone: str = "", password: str = "") -> None:
        with self.lock:
            self._cooling.pop(email, None)
            self.pool.mark_error(email, reason, phone=phone, password=password)

    def _release(self, email: str, cooldown: float) -> None:
        with self.lock:
            seconds = max(0.0, cooldown)
            if seconds <= 0:
                self._cooling.pop(email, None)
                self.pool.mark_unused(email)
                return
            self._cooling[email] = time.time() + seconds


class ResultWriter:
    """线程安全结果写入器。"""

    def __init__(self, results_dir: str | Path = ROOT / "results", imports_dir: str | Path = ROOT / "imports"):
        self.results_dir = Path(results_dir)
        self.imports_dir = Path(imports_dir)
        self.results_dir.mkdir(exist_ok=True)
        self.imports_dir.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    @staticmethod
    def _safe_filename_part(value: object, default: str = "unknown") -> str:
        text = str(value or "").strip()
        if not text or text == "?":
            text = default
        text = text.replace("+", "")
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
        text = re.sub(r"_+", "_", text).strip(" ._")
        return text or default

    def append_account(self, record: dict) -> None:
        safe = {
            k: v for k, v in record.items()
            if k not in {"access_token", "api_key", "sub2api_password", "cookie", "cookies"}
        }
        status = self._safe_filename_part(safe.get("status"), "unknown")
        phone = self._safe_filename_part(safe.get("phone"), "unknown")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        with self.lock:
            single_path = self.results_dir / f"{phone}_{ts}_{status}.json"
            single_path.write_text(
                json.dumps(safe, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            all_path = self.results_dir / "_all.json"
            all_results = []
            if all_path.exists():
                try:
                    loaded = json.loads(all_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        all_results = loaded
                except Exception:
                    all_results = []
            all_results.append(safe)
            all_path.write_text(
                json.dumps(all_results, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def append_import(self, import_data: dict) -> None:
        account = self._extract_first_import_account(import_data)
        if not account:
            return
        ts = datetime.now().strftime("%Y%m%d")
        path = self.imports_dir / f"import_{ts}.json"

        with self.lock:
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    payload = None
            else:
                payload = None

            if not isinstance(payload, dict):
                payload = copy.deepcopy(import_data)
                root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                if isinstance(root, dict):
                    root["accounts"] = [account]
            elif "data" in payload and isinstance(payload.get("data"), dict):
                payload["data"].setdefault("accounts", [])
                payload["data"]["accounts"].append(account)
            else:
                payload.setdefault("accounts", [])
                payload["accounts"].append(account)

            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _extract_first_import_account(import_data: dict) -> Optional[dict]:
        if not isinstance(import_data, dict):
            return None
        root = import_data.get("data") if isinstance(import_data.get("data"), dict) else import_data
        accounts = root.get("accounts") if isinstance(root, dict) else None
        if isinstance(accounts, list) and accounts:
            return accounts[0]
        return None


@dataclass
class Phase2Outcome:
    ok: bool
    final_email: str
    lease: Optional[EmailLease] = None
    sub2api_id: str = ""
    import_data: Optional[dict] = None
    error: str = ""
    retry_count: int = 0


class RunState:
    def __init__(self, target_success: int):
        self.target_success = target_success
        self.lock = threading.RLock()
        self.full_success = 0
        self.phase1_failed = 0
        self.phase2_failed = 0
        self.interrupted = 0
        self.cancelled = 0
        self.attempts = 0

    def should_continue(self, stop_event: threading.Event) -> bool:
        with self.lock:
            return not stop_event.is_set() and self.full_success < self.target_success

    def record_attempt(self) -> int:
        with self.lock:
            self.attempts += 1
            return self.attempts

    def record_success(self) -> int:
        with self.lock:
            self.full_success += 1
            return self.full_success

    def record_phase1_failed(self) -> None:
        with self.lock:
            self.phase1_failed += 1

    def record_phase2_failed(self) -> None:
        with self.lock:
            self.phase2_failed += 1

    def record_interrupted(self) -> None:
        with self.lock:
            self.interrupted += 1

    def record_cancelled(self) -> None:
        with self.lock:
            self.cancelled += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "target_success": self.target_success,
                "full_success": self.full_success,
                "phase1_failed": self.phase1_failed,
                "phase2_failed": self.phase2_failed,
                "interrupted": self.interrupted,
                "cancelled": self.cancelled,
                "attempts": self.attempts,
            }


def _raw_print(router: Optional[ThreadStdoutRouter], msg: str) -> None:
    out = router.original if router else sys.__stdout__
    out.write(msg + "\n")
    out.flush()


def _make_worker_logger(wid: int, log_lock: threading.Lock, router: Optional[ThreadStdoutRouter]):
    def _log(msg: str, tag: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        with log_lock:
            _raw_print(router, f"[{tag}] [{ts}] [W{wid}] {msg}")
    return _log


def _is_retryable_phase2_error(error: str) -> bool:
    err = (error or "").lower()
    return any(keyword in err for keyword in RETRYABLE_PHASE2_KEYWORDS)


def _post_json_with_retry(
    url: str,
    *,
    label: str,
    log: Callable[[str, str], None],
    attempts: int = 3,
    **kwargs,
) -> dict:
    last_error = ""
    for attempt in range(attempts):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise RuntimeError(f"{label}: HTTP {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            return data
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            last_error = str(exc)
            if attempt >= attempts - 1:
                break
            wait = 2 ** (attempt + 1)
            log(f"{label} 失败，{wait}s 后重试 ({attempt + 1}/{attempts}): {exc}", "warn")
            time.sleep(wait)
    raise RuntimeError(f"{label} 失败，已达重试上限: {last_error}")


def _get_oauth_session_with_retry(sub: dict, log: Callable[[str, str], None]) -> tuple[str, str, str]:
    sub_url = (sub.get("url") or "").rstrip("/")
    sub_email = sub.get("email") or ""
    sub_pwd = sub.get("pwd") or ""
    proxy_id = int(sub.get("proxy_id", 0) or 0)

    login_data = _post_json_with_retry(
        f"{sub_url}/api/v1/auth/login",
        label="SUB2API 登录",
        log=log,
        json={"email": sub_email, "password": sub_pwd},
        timeout=30,
    )
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API 登录失败: {login_data.get('message', login_data)}")
    token = login_data["data"]["access_token"]

    body = {"redirect_uri": DEFAULT_REDIRECT_URI}
    if proxy_id:
        body["proxy_id"] = proxy_id
    oauth_data = _post_json_with_retry(
        f"{sub_url}/api/v1/admin/openai/generate-auth-url",
        label="获取 OAuth URL",
        log=log,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if oauth_data.get("code") != 0:
        raise RuntimeError(f"获取 OAuth URL 失败: {oauth_data.get('message', oauth_data)}")

    data = oauth_data["data"]
    oauth_url = data["auth_url"]
    session_id = data["session_id"]
    state = data.get("state") or parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
    return oauth_url, session_id, state


def _run_phase2_with_retry(
    *,
    wid: int,
    cfg: dict,
    phase1_result: dict,
    initial_lease: EmailLease,
    allocator: EmailAllocator,
    log: Callable[[str, str], None],
    stop_event: threading.Event,
    total_timeout: float = 300.0,
    max_retries: int = 3,
) -> Phase2Outcome:
    sub = cfg.get("sub2api", {})
    sub_url = (sub.get("url") or "").rstrip("/")
    sub_email = sub.get("email") or ""
    sub_pwd = sub.get("pwd") or ""
    active_lease: Optional[EmailLease] = initial_lease
    start = time.time()
    retry_count = 0
    last_error = ""

    while retry_count < max_retries and time.time() - start <= total_timeout:
        retry_count += 1
        if retry_count > 1:
            log(f"Phase2 重试 {retry_count}/{max_retries}", "warn")

        try:
            oauth_url, session_id, oauth_state = _get_oauth_session_with_retry(sub, log)
        except Exception as exc:
            last_error = str(exc)
            return Phase2Outcome(
                ok=False,
                final_email=active_lease.email if active_lease else "",
                lease=active_lease,
                error=last_error,
                retry_count=retry_count,
            )

        if active_lease is None:
            return Phase2Outcome(ok=False, final_email="", error="no active email lease", retry_count=retry_count)

        current_email = active_lease.email
        oauth_result = run_second_half(
            oauth_url=oauth_url,
            phone=phase1_result["phone"],
            password=phase1_result["password"],
            icloud_email=current_email,
            icloud_cookies={},
            imap_user=cfg.get("icloud", {}).get("user", ""),
            imap_password=cfg.get("icloud", {}).get("pass", ""),
            sub2api_url=sub_url,
            sub2api_email=sub_email,
            sub2api_password=sub_pwd,
            sub2api_proxy_id=int(sub.get("proxy_id", 0) or 0),
            proxy=cfg.get("proxy", ""),
            verbose=True,
            sub2api_session_id=session_id,
            sub2api_state=oauth_state,
            msoutlook_helper_url=cfg.get("msoutlook", {}).get("helper_url", "") if allocator.has_account(current_email) else "",
            msoutlook_email=current_email,
            msoutlook_helper_mode=str(cfg.get("msoutlook", {}).get("helper_mode") or "http"),
            msoutlook_helper_script=str(cfg.get("msoutlook", {}).get("helper_script") or ""),
            save_import=False,
            interactive_input=False,
        )

        if oauth_result.get("ok"):
            return Phase2Outcome(
                ok=True,
                final_email=current_email,
                lease=active_lease,
                sub2api_id=oauth_result.get("sub2api_account_id", ""),
                import_data=oauth_result.get("import_data"),
                retry_count=retry_count,
            )

        last_error = oauth_result.get("error", "") or "Phase 2 failed"
        if "email_already_in_use" in last_error:
            log(f"邮箱已被占用: {current_email}", "warn")
            active_lease.mark_error(
                "email_already_in_use",
                phone=phase1_result.get("phone", ""),
                password=phase1_result.get("password", ""),
            )
            active_lease = None
            if stop_event.is_set() or retry_count >= max_retries:
                break
            try:
                active_lease = allocator.acquire(wid)
                cfg["bind_email"] = active_lease.email
                log(f"新邮箱: {active_lease.email}", "info")
            except Exception as exc:
                last_error = f"选邮箱失败: {exc}"
                break
            continue

        if "account_stuck_email_otp" in last_error:
            log("账号卡在 email_otp 状态，无法重试", "error")
            break

        if any(kw in last_error.lower() for kw in FATAL_EMAIL_KEYWORDS):
            log(f"邮箱令牌不可恢复: {current_email}", "error")
            active_lease.mark_error(
                "token_compromised",
                phone=phase1_result.get("phone", ""),
                password=phase1_result.get("password", ""),
            )
            active_lease = None
            if stop_event.is_set() or retry_count >= max_retries:
                break
            try:
                active_lease = allocator.acquire(wid)
                cfg["bind_email"] = active_lease.email
                log(f"新邮箱: {active_lease.email}", "info")
            except Exception as exc:
                last_error = f"选邮箱失败: {exc}"
                break
            continue

        if _is_retryable_phase2_error(last_error):
            if stop_event.is_set():
                break
            if retry_count < max_retries and time.time() - start <= total_timeout:
                log(f"Phase2 可重试错误: {last_error}", "warn")
                time.sleep(5)
                continue

        break

    return Phase2Outcome(
        ok=False,
        final_email=active_lease.email if active_lease else "",
        lease=active_lease,
        error=last_error,
        retry_count=retry_count,
    )


def _sms_action(cfg: dict, activation_id: str, action: str) -> bool:
    if not activation_id:
        return False
    provider = cfg.get("sms_provider", "smsbower")
    api_key = ar._get_sms_api_key(cfg, provider)
    sms = PhoneSMS(provider, api_key)
    if action == "complete":
        sms.complete(activation_id)
    elif action == "cancel":
        return bool(sms.cancel_blocking(activation_id))
    else:
        raise ValueError(f"unknown sms action: {action}")
    return True


def _sms_cancel_async(
    cfg: dict,
    activation_id: str,
    phone: str,
    reason: str,
    log,
    state: RunState,
) -> None:
    """Start a detached cancellation job and let the worker continue.

    hero-sms/SmsBower only allow setStatus=8 after a cooldown. Waiting here
    makes a 30s OTP timeout look like the worker is still "stuck". The helper
    process keeps retrying independently and records details in logs/.
    """
    if not activation_id:
        return
    cmd = [
        sys.executable,
        str(ROOT / "sms_cancel_once.py"),
        "--activation-id",
        str(activation_id),
        "--config",
        str(cfg.get("_config_path") or (ROOT / "config.json")),
        "--phone",
        str(phone or ""),
        "--reason",
        str(reason or ""),
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(os.name != "nt"),
            start_new_session=(os.name != "nt"),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
    except Exception as exc:
        log(f"号码取消任务启动失败: {exc}", "error")
        return
    state.record_cancelled()
    log(f"号码取消任务已后台启动: {phone or '?'} ({reason})", "warn")


def _account_record(status: str, result: dict, bind_email: str, phase2_error: str = "", sub2api_id: str = "") -> dict:
    return {
        "status": status,
        "phone": result.get("phone", ""),
        "password": result.get("password", ""),
        "bind_email": bind_email,
        "name": result.get("name", ""),
        "birthdate": result.get("birthdate", ""),
        "session_token": result.get("session_token", ""),
        "access_token": result.get("access_token", ""),
        "sub2api_id": sub2api_id or result.get("sub2api_id", ""),
        "phase2_error": phase2_error,
        "saved_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _is_fatal_phase1_error(error: str) -> bool:
    text = str(error or "")
    return any(keyword in text for keyword in FATAL_PHASE1_KEYWORDS)


def _has_real_phone(result: dict) -> bool:
    phone = str((result or {}).get("phone") or "").strip()
    return bool(phone and phone not in {"?", "-", "unknown", "None"})


def worker(
    *,
    wid: int,
    config: dict,
    target_count: int,
    global_stop: threading.Event,
    allocator: EmailAllocator,
    result_writer: ResultWriter,
    router: ThreadStdoutRouter,
    log_lock: threading.Lock,
    state: RunState,
    step_retries: int,
    create_retries: int,
    cooldown: float,
    phase2_timeout: float,
) -> None:
    cfg = copy.deepcopy(config)
    log = _make_worker_logger(wid, log_lock, router)
    local_success = 0
    local_attempts = 0
    max_attempts = max(1, target_count * 15)
    log(f"Worker 启动 (proxy={cfg.get('proxy') or '直连'})", "info")

    while local_success < target_count and state.should_continue(global_stop) and local_attempts < max_attempts:
        local_attempts += 1
        global_attempt = state.record_attempt()
        snap = state.snapshot()
        log(
            f"第 {global_attempt} 次 [worker_success={local_success}/{target_count}, "
            f"full_success={snap['full_success']}/{snap['target_success']}]",
            "info",
        )

        try:
            lease = allocator.acquire(wid)
        except Exception as exc:
            log(f"获取邮箱失败: {exc}", "warn")
            break
        cfg["bind_email"] = lease.email
        log(f"选中邮箱: {lease.email}", "info")

        try:
            with router.capture_current_thread(lambda line: log(line, "info")):
                result = ar.register_one(
                    cfg,
                    verbose=True,
                    step_retries=step_retries,
                    create_account_max_retries=create_retries,
                    max_price=cfg.get("max_price", ""),
                    auto_activate=False,
                )
        except Exception as exc:
            result = {
                "ok": False,
                "phone": "?",
                "password": cfg.get("register", {}).get("password", ""),
                "error": str(exc),
            }
            log(f"Phase 1 异常: {exc}", "error")

        activation_id = result.get("activation_id", "") if isinstance(result, dict) else ""
        if not result or not result.get("ok"):
            state.record_phase1_failed()
            phase1_error = result.get("error", "") if result else ""
            failed_phone = result.get("phone", "?") if isinstance(result, dict) else "?"
            log(f"Phase 1 失败: {failed_phone} {phase1_error}", "error")
            if _has_real_phone(result):
                _sms_cancel_async(cfg, activation_id, failed_phone, "Phase 1 失败", log, state)
                result_writer.append_account(_account_record("fail_phase1", result, lease.email))
                lease.release(cooldown)
            else:
                # NO_NUMBERS/NO_BALANCE/BAD_KEY 发生在拿号前，邮箱没有真正参与注册，
                # 不应进入失败记录，也不应冷却/占用邮箱。
                lease.release(0)
            if _is_fatal_phase1_error(phase1_error):
                global_stop.set()
                log(f"检测到不可继续的 Phase 1 错误，停止所有 worker: {phase1_error}", "error")
                break
            continue

        log(f"Phase 1 成功: {result['phone']} -> {lease.email}", "success")
        if global_stop.is_set():
            result_writer.append_account(_account_record("interrupted_after_phase1", result, lease.email))
            _sms_cancel_async(cfg, activation_id, result.get("phone", "?"), "中断收尾", log, state)
            lease.release(cooldown)
            state.record_interrupted()
            break

        with router.capture_current_thread(lambda line: log(line, "info")):
            outcome = _run_phase2_with_retry(
                wid=wid,
                cfg=cfg,
                phase1_result=result,
                initial_lease=lease,
                allocator=allocator,
                log=log,
                stop_event=global_stop,
                total_timeout=phase2_timeout,
            )

        active_lease = outcome.lease
        if outcome.ok:
            final_email = outcome.final_email or (active_lease.email if active_lease else lease.email)
            try:
                if not _sms_action(cfg, activation_id, "complete"):
                    raise RuntimeError("activation_id missing")
                log(f"号码已激活: {result.get('phone', '?')}", "success")
            except Exception as exc:
                complete_error = f"complete failed after phase2 ok: {exc}"
                if active_lease and not active_lease.finalized:
                    active_lease.mark_error(
                        "complete_failed",
                        phone=result.get("phone", ""),
                        password=result.get("password", ""),
                    )
                result_writer.append_account(
                    _account_record(
                        "fail_phase2",
                        result,
                        final_email,
                        complete_error,
                        sub2api_id=outcome.sub2api_id,
                    )
                )
                state.record_phase2_failed()
                log(f"号码激活失败，不计入完整成功: {exc}", "error")
                continue
            if active_lease:
                active_lease.mark_used(
                    phone=result.get("phone", ""),
                    password=result.get("password", ""),
                )
            result["sub2api_id"] = outcome.sub2api_id
            result_writer.append_account(_account_record("ok", result, final_email, sub2api_id=outcome.sub2api_id))
            if outcome.import_data:
                result_writer.append_import(outcome.import_data)
            full_success = state.record_success()
            local_success += 1
            log(f"完整成功: worker={local_success}/{target_count} global={full_success}/{state.target_success} phone={result.get('phone', '?')}", "success")
        else:
            final_email = outcome.final_email or (active_lease.email if active_lease else "")
            if active_lease and not active_lease.finalized:
                active_lease.release(cooldown)
            _sms_cancel_async(cfg, activation_id, result.get("phone", "?"), "Phase 2 失败", log, state)
            result_writer.append_account(_account_record("fail_phase2", result, final_email, outcome.error))
            state.record_phase2_failed()
            log(f"Phase 2 失败: {outcome.error}", "error")

    log("Worker 结束", "info")


def _load_extra_used_from_results(results_dir: Path) -> set[str]:
    emails: set[str] = set()
    all_path = results_dir / "_all.json"
    if not all_path.exists():
        return emails
    try:
        data = json.loads(all_path.read_text(encoding="utf-8"))
    except Exception:
        return emails
    if not isinstance(data, list):
        return emails
    for item in data:
        if isinstance(item, dict) and item.get("bind_email"):
            emails.add(str(item["bind_email"]).strip().lower())
    return emails


def _preflight(config: dict, concurrency: int, count: int) -> list[str]:
    errors = []
    if count < 1:
        errors.append("count must be >= 1")
    if concurrency < 1 or concurrency > 10:
        errors.append("concurrency must be between 1 and 10")
    if errors:
        return errors
    sub = config.get("sub2api", {})
    ms = config.get("msoutlook", {})
    helper_mode = str(ms.get("helper_mode") or "http").strip().lower()
    helper_ready = bool(ms.get("helper_script")) if helper_mode == "direct" else bool(ms.get("helper_url"))
    if not (sub.get("url") and sub.get("email") and sub.get("pwd") and helper_ready):
        if helper_mode == "direct":
            errors.append("sub2api.url/sub2api.email/sub2api.pwd and msoutlook.helper_script are required for worker_pool.py direct mode")
        else:
            errors.append("sub2api.url/sub2api.email/sub2api.pwd and msoutlook.helper_url are required for worker_pool.py")
    provider = config.get("sms_provider", "smsbower")
    if not ar._get_sms_api_key(config, provider):
        errors.append(f"{provider} API Key is required")
    return errors


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ChatGPT 并发注册工具")
    parser.add_argument("-n", "--count", type=int, default=1, help="目标完整成功数量")
    parser.add_argument("-c", "--concurrency", type=int, default=1, help="并发 worker 数（1..10，默认1）")
    parser.add_argument("-r", "--retry", type=int, default=2, help="Phase 1 步骤重试次数")
    parser.add_argument("--create-retry", type=int, default=20, help="Phase 1 创建账户重试次数")
    parser.add_argument("--config", type=str, default="", help="配置文件路径（默认 config.json）")
    parser.add_argument("--max-price", type=str, default="", help="最高价格，覆盖 config.max_price")
    parser.add_argument("--cooldown", type=float, default=60.0, help="普通失败邮箱冷却秒数")
    parser.add_argument("--phase2-timeout", type=float, default=300.0, help="单次 Phase 2 总耗时上限秒数")
    args = parser.parse_args(argv)

    config = ar.load_config(args.config or None)
    config["_config_path"] = str(Path(args.config).resolve()) if args.config else str(ROOT / "config.json")
    if args.max_price:
        config["max_price"] = args.max_price

    errors = _preflight(config, args.concurrency, args.count)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        return 2

    router = ThreadStdoutRouter.install_once()
    global_stop = threading.Event()
    log_lock = threading.Lock()

    def main_log(msg: str, tag: str = "INFO") -> None:
        with log_lock:
            _raw_print(router, f"[{tag}] {msg}")

    def handle_signal(sig, _frame) -> None:
        main_log(f"收到停止信号 {sig}，进入 draining stop", "WARN")
        global_stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    allocator: Optional[EmailAllocator] = None
    try:
        results_dir = ROOT / "results"
        extra_used = load_used_set() | _load_extra_used_from_results(results_dir)
        allocator = EmailAllocator(
            helper_url=config.get("msoutlook", {}).get("helper_url", ""),
            extra_used=extra_used,
            helper_mode=str(config.get("msoutlook", {}).get("helper_mode") or "http"),
            helper_script=str(config.get("msoutlook", {}).get("helper_script") or ""),
        )
        stats = allocator.stats()
        main_log(
            f"concurrency={args.concurrency} target_success={args.count} "
            f"邮箱可用 {stats['available']}/{stats['total']} used={stats['used']} error={stats['error']}"
        )

        result_writer = ResultWriter()
        state = RunState(args.count)
        workers = []
        effective_workers = min(args.concurrency, args.count)
        per_worker = args.count // effective_workers
        remainder = args.count % effective_workers
        for idx in range(effective_workers):
            wid = idx + 1
            worker_target = per_worker + (1 if idx < remainder else 0)
            thread = threading.Thread(
                target=worker,
                kwargs={
                    "wid": wid,
                    "config": config,
                    "target_count": worker_target,
                    "global_stop": global_stop,
                    "allocator": allocator,
                    "result_writer": result_writer,
                    "router": router,
                    "log_lock": log_lock,
                    "state": state,
                    "step_retries": args.retry,
                    "create_retries": args.create_retry,
                    "cooldown": args.cooldown,
                    "phase2_timeout": args.phase2_timeout,
                },
                name=f"worker-{wid}",
                daemon=False,
            )
            workers.append(thread)
            thread.start()
            main_log(f"Worker-{wid} 启动 target={worker_target}")

        for thread in workers:
            thread.join()

        summary = state.snapshot()
        main_log(
            "done "
            f"full_success={summary['full_success']} "
            f"phase1_failed={summary['phase1_failed']} "
            f"phase2_failed={summary['phase2_failed']} "
            f"cancelled={summary['cancelled']} "
            f"interrupted={summary['interrupted']} "
            f"attempts={summary['attempts']}"
        )
        return 0 if summary["full_success"] >= args.count else 1
    finally:
        if allocator is not None:
            allocator.close()
        router.restore()


if __name__ == "__main__":
    raise SystemExit(main())
