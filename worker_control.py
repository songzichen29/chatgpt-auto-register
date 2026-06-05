#!/usr/bin/env python3
"""worker_pool Web 控制台后端 API。"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request as UrlRequest, urlopen

from flask import Blueprint, jsonify, request, send_file


USAGE_STATUSES = {"unused", "testing", "used", "bad", "reserved"}
EXPORT_STATUSES = {"not_exported", "exported", "ignored"}
MUTABLE_STATE_FIELDS = {"usage_status", "export_status", "note"}
SENSITIVE_ACCOUNT_FIELDS = {"access_token", "api_key", "sub2api_password", "cookie", "cookies"}
CONFIG_FORM_FIELDS: tuple[tuple[tuple[str, ...], str, Any], ...] = (
    (("sms_provider",), "str", "smsbower"),
    (("smsbower", "api_key"), "str", ""),
    (("hero_sms", "api_key"), "str", ""),
    (("hero_sms", "base_url"), "str", ""),
    (("fivesim", "api_key"), "str", ""),
    (("register", "password"), "str", ""),
    (("register", "name"), "str", ""),
    (("register", "birthdate"), "str", ""),
    (("proxy",), "str", ""),
    (("country",), "str", ""),
    (("service",), "str", ""),
    (("max_price",), "str", ""),
    (("sms_timeout",), "int", 30),
    (("code_timeout",), "int", 30),
    (("icloud", "user"), "str", ""),
    (("icloud", "pass"), "str", ""),
    (("sub2api", "url"), "str", ""),
    (("sub2api", "email"), "str", ""),
    (("sub2api", "pwd"), "str", ""),
    (("sub2api", "group"), "str", ""),
    (("sub2api", "proxy_id"), "int", 0),
    (("bind_email",), "str", ""),
    (("msoutlook", "helper_mode"), "str", "direct"),
    (("msoutlook", "helper_url"), "str", "http://127.0.0.1:17373"),
    (("msoutlook", "email"), "str", ""),
    (("msoutlook", "helper_script"), "str", ""),
    (("msoutlook", "helper_bat"), "str", ""),
)

HELPER_MODULE_CACHE: dict[str, tuple[float, Any]] = {}


def utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def utc_filename_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return copy.deepcopy(default)
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(default)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_int(value: Any, default: int = 0, *, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    try:
        result = int(value)
    except Exception:
        result = default
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def parse_float(value: Any, default: float = 0.0, *, min_value: Optional[float] = None) -> float:
    try:
        result = float(value)
    except Exception:
        result = default
    if min_value is not None:
        result = max(min_value, result)
    return result


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def account_key(record: dict) -> str:
    sub_id = str(record.get("sub2api_id") or "").strip()
    if sub_id:
        return f"sub:{sub_id}"
    phone = str(record.get("phone") or "").strip()
    if phone:
        return f"phone:{phone}"
    email = normalize_email(record.get("bind_email") or record.get("email"))
    if email:
        return f"email:{email}"
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return f"record:{abs(hash(raw))}"


def normalize_reg_status(record: dict) -> str:
    status = str(record.get("status") or "").strip()
    if status:
        return status
    if record.get("ok") is True:
        return "ok"
    if record.get("ok") is False:
        return "fail"
    return "unknown"


def get_paths(root: Path) -> dict[str, Path]:
    results_dir = root / "results"
    return {
        "root": root,
        "config": root / "config.json",
        "pool": root / "号.json",
        "used": root / "msoutlook_used.json",
        "results_dir": results_dir,
        "all_results": results_dir / "_all.json",
        "account_state": results_dir / "account_state.json",
        "pool_import_history": results_dir / "pool_import_history.json",
        "imports_dir": root / "imports",
        "public_worker_control": root / "public" / "worker-control.html",
    }


def get_nested(data: dict, path: Iterable[str], default: Any = "") -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def set_nested(data: dict, path: Iterable[str], value: Any) -> None:
    parts = list(path)
    cur = data
    for key in parts[:-1]:
        child = cur.get(key)
        if not isinstance(child, dict):
            child = {}
            cur[key] = child
        cur = child
    cur[parts[-1]] = value


def load_config(root: Path) -> dict:
    data = read_json(get_paths(root)["config"], {})
    return data if isinstance(data, dict) else {}


def config_form(root: Path) -> dict:
    cfg = load_config(root)
    values = {".".join(path): get_nested(cfg, path, default) for path, _kind, default in CONFIG_FORM_FIELDS}
    return {"ok": True, "path": str(get_paths(root)["config"]), "values": values, "config": cfg}


def update_config_form(root: Path, values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    cfg = load_config(root)
    allowed = {".".join(path): (path, kind, default) for path, kind, default in CONFIG_FORM_FIELDS}
    for dotted, raw in values.items():
        if dotted not in allowed:
            continue
        path, kind, default = allowed[dotted]
        if kind == "int":
            value = parse_int(raw, int(default or 0), min_value=0)
        else:
            value = str(raw or "").strip()
        set_nested(cfg, path, value)
    write_json_atomic(get_paths(root)["config"], cfg)
    return {"ok": True, "path": str(get_paths(root)["config"]), "values": config_form(root)["values"], "config": cfg}


class WorkerProcessController:
    """管理一个 worker_pool.py 子进程及日志。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.logs: list[dict] = []
        self.run: Optional[dict] = None
        self.process: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None

    def _append_log(self, text: str, tag: Optional[str] = None) -> None:
        line = str(text).rstrip("\r\n")
        if not line:
            return
        item = {"time": time.strftime("%H:%M:%S"), "tag": tag or self._classify(line), "stream": self._stream_for(line), "text": line}
        with self.lock:
            self.logs.append(item)
            if len(self.logs) > 5000:
                self.logs = self.logs[-4000:]
            if self.run:
                summary = self._parse_summary(line)
                if summary:
                    self.run["summary"] = summary

    @staticmethod
    def _classify(line: str) -> str:
        low = line.lower()
        if "error" in low or "失败" in line or "❌" in line:
            return "error"
        if "warn" in low or "警告" in line or "⚠" in line:
            return "warn"
        if "success" in low or "成功" in line or "✅" in line or "[ok]" in low:
            return "success"
        return "info"

    @staticmethod
    def _stream_for(line: str) -> str:
        m = re.search(r"\[W(\d+)\]", line)
        if m:
            return f"W{m.group(1)}"
        if "[retry]" in line:
            return "retry"
        return "main"

    @staticmethod
    def _parse_summary(line: str) -> Optional[dict]:
        if "done " not in line or "full_success=" not in line:
            return None
        summary: dict[str, int] = {}
        for key in ["full_success", "phase1_failed", "phase2_failed", "cancelled", "interrupted", "attempts"]:
            m = re.search(rf"{key}=(\d+)", line)
            if m:
                summary[key] = int(m.group(1))
        return summary or None

    def is_running(self) -> bool:
        with self.lock:
            return bool(self.process and self.process.poll() is None)

    def build_worker_command(self, params: dict) -> list[str]:
        count = parse_int(params.get("count"), 1, min_value=1)
        concurrency = parse_int(params.get("concurrency"), 1, min_value=1, max_value=10)
        retry = parse_int(params.get("retry", params.get("retries")), 2, min_value=0)
        create_retry = parse_int(params.get("create_retry"), 20, min_value=1)
        cooldown = parse_float(params.get("cooldown"), 60.0, min_value=0.0)
        phase2_timeout = parse_float(params.get("phase2_timeout"), 300.0, min_value=1.0)
        config_path = str(params.get("config") or (self.root / "config.json"))
        cmd = [
            sys.executable, str(self.root / "worker_pool.py"),
            "-n", str(count), "-c", str(concurrency), "-r", str(retry),
            "--create-retry", str(create_retry), "--config", config_path,
            "--cooldown", str(cooldown), "--phase2-timeout", str(phase2_timeout),
        ]
        max_price = str(params.get("max_price") or "").strip()
        if max_price:
            cmd.extend(["--max-price", max_price])
        return cmd

    def start(self, params: dict, *, command: Optional[list[str]] = None) -> tuple[bool, dict]:
        with self.lock:
            if self.process and self.process.poll() is None:
                return False, {"error": "已有运行中的 worker_pool 任务"}
            cmd = command or self.build_worker_command(params)
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, creationflags=creationflags,
                )
            except Exception as exc:
                return False, {"error": str(exc)}
            self.process = proc
            self.run = {
                "id": run_id, "pid": proc.pid, "command": " ".join(cmd),
                "started_at": utc_now(), "ended_at": "", "exit_code": None,
                "stopping": False, "summary": {}, "kind": "worker_pool",
            }
            self._append_log(f"[worker-control] started pid={proc.pid} cmd={' '.join(cmd)}", "info")
            self.reader_thread = threading.Thread(target=self._reader_loop, args=(proc,), daemon=True)
            self.reader_thread.start()
            return True, {"run": dict(self.run)}

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout:
                for line in proc.stdout:
                    self._append_log(line)
                proc.stdout.close()
            code = proc.wait()
        except Exception as exc:
            code = None
            self._append_log(f"[worker-control] reader error: {exc}", "error")
        with self.lock:
            if self.run and self.process is proc:
                self.run["exit_code"] = code
                self.run["ended_at"] = utc_now()
                self.run["stopping"] = False
        self._append_log(f"[worker-control] exited code={code}", "info")

    def stop(self) -> dict:
        with self.lock:
            proc = self.process
            if not proc or proc.poll() is not None:
                return {"ok": True, "stopping": False}
            if self.run:
                self.run["stopping"] = True
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            self._append_log("[worker-control] stop signal sent", "warn")
        except Exception as exc:
            self._append_log(f"[worker-control] graceful stop failed, terminate: {exc}", "warn")
            try:
                proc.terminate()
            except Exception:
                pass
        return {"ok": True, "stopping": True}

    def status(self) -> dict:
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            run = dict(self.run or {})
        return {
            "ok": True, "running": running, "stopping": bool(run.get("stopping")),
            "pid": run.get("pid"), "exit_code": run.get("exit_code"),
            "started_at": run.get("started_at", ""), "ended_at": run.get("ended_at", ""),
            "summary": run.get("summary", {}), "run": run,
        }

    def log_since(self, cursor: int, stream: str = "") -> dict:
        with self.lock:
            start = max(0, min(cursor, len(self.logs)))
            if stream:
                lines = [line for line in self.logs[start:] if line.get("stream") == stream]
            else:
                lines = self.logs[start:]
            new_cursor = len(self.logs)
        return {"ok": True, "lines": lines, "cursor": new_cursor}

    def log_streams(self) -> dict:
        with self.lock:
            stats: dict[str, dict] = {}
            for item in self.logs:
                stream = item.get("stream") or "main"
                entry = stats.setdefault(stream, {"stream": stream, "count": 0, "last_time": "", "last_text": ""})
                entry["count"] += 1
                entry["last_time"] = item.get("time", "")
                entry["last_text"] = item.get("text", "")
        order = {"main": 0, "retry": 999}
        streams = sorted(stats.values(), key=lambda x: (order.get(x["stream"], 100), x["stream"]))
        return {"ok": True, "streams": streams}


@dataclass
class AccountStore:
    root: Path

    @property
    def paths(self) -> dict[str, Path]:
        return get_paths(self.root)

    def load_state(self) -> dict:
        data = read_json(self.paths["account_state"], {})
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict) -> None:
        write_json_atomic(self.paths["account_state"], state)

    def load_results(self) -> list[dict]:
        data = read_json(self.paths["all_results"], [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def load_import_accounts(self) -> dict[str, dict]:
        accounts: dict[str, dict] = {}
        imports_dir = self.paths["imports_dir"]
        if not imports_dir.exists():
            return accounts
        for path in sorted(imports_dir.glob("import_*.json")):
            payload = read_json(path, None)
            if not isinstance(payload, dict):
                continue
            root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            iterable = root.get("accounts", []) if isinstance(root, dict) else []
            for account in iterable:
                if not isinstance(account, dict):
                    continue
                creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
                email = normalize_email(creds.get("email") or account.get("name"))
                if email:
                    accounts[email] = copy.deepcopy(account)
        return accounts

    def import_summary(self) -> dict:
        imports_dir = self.paths["imports_dir"]
        files = 0
        accounts_total = 0
        unique_emails: set[str] = set()
        if not imports_dir.exists():
            return {"files": 0, "accounts_total": 0, "unique_accounts": 0}
        for path in sorted(imports_dir.glob("import_*.json")):
            payload = read_json(path, None)
            if not isinstance(payload, dict):
                continue
            root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            iterable = root.get("accounts", []) if isinstance(root, dict) else []
            if not isinstance(iterable, list):
                continue
            files += 1
            for account in iterable:
                if not isinstance(account, dict):
                    continue
                accounts_total += 1
                creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
                email = normalize_email(creds.get("email") or account.get("name"))
                if email:
                    unique_emails.add(email)
        return {"files": files, "accounts_total": accounts_total, "unique_accounts": len(unique_emails)}

    def make_item(self, record: dict, state: dict, import_accounts: Optional[dict[str, dict]] = None) -> dict:
        import_accounts = import_accounts if import_accounts is not None else self.load_import_accounts()
        key = account_key(record)
        reg_status = normalize_reg_status(record)
        st = state.get(key, {}) if isinstance(state.get(key), dict) else {}
        is_success = reg_status == "ok"
        usage_status = st.get("usage_status") or ("unused" if is_success else "")
        email = normalize_email(record.get("bind_email") or record.get("email"))
        exportable = bool(email and email in import_accounts)
        sub2api_id_present = bool(str(record.get("sub2api_id") or "").strip())
        stored_export_status = st.get("export_status")
        evidence_exported = bool(is_success and (exportable or sub2api_id_present))
        if not is_success:
            export_status = ""
        elif stored_export_status == "ignored":
            export_status = "ignored"
        elif evidence_exported:
            export_status = "exported"
        else:
            export_status = stored_export_status or "not_exported"
        return {
            "key": key,
            "reg_status": reg_status,
            "usage_status": usage_status,
            "export_status": export_status,
            "exportable": exportable,
            "phone": record.get("phone", ""),
            "password": record.get("password", ""),
        "bind_email": record.get("bind_email", ""),
        "sub2api_id": record.get("sub2api_id", ""),
            "sub2api_id_present": sub2api_id_present,
        "phase2_error": record.get("phase2_error", ""),
            "error": record.get("error", ""),
            "saved_at": record.get("saved_at", ""),
            "name": record.get("name", ""),
            "birthdate": record.get("birthdate", ""),
            "note": st.get("note", ""),
            "session_token_present": bool(record.get("session_token")),
            "activation_id_present": bool(record.get("activation_id")),
            "retry": retry_info_for_record(record, st, exportable),
        }

    def list_accounts(self, filters: dict) -> dict:
        state = self.load_state()
        imports = self.load_import_accounts()
        items = [self.make_item(r, state, imports) for r in self.load_results()]
        stats = account_stats(items)
        filtered = filter_account_items(items, filters)
        filtered = sort_account_items(filtered, filters)
        total = len(filtered)
        offset = parse_int(filters.get("offset"), 0, min_value=0)
        limit = parse_int(filters.get("limit"), 50, min_value=1, max_value=500)
        page = (offset // limit) + 1 if limit else 1
        pages = max(1, (total + limit - 1) // limit) if limit else 1
        return {
            "ok": True,
            "stats": stats,
            "total": total,
            "items": filtered[offset:offset + limit],
            "offset": offset,
            "limit": limit,
            "page": page,
            "pages": pages,
        }

    def resolve_items(self, scope: str, keys: Iterable[str], filters: dict) -> list[dict]:
        state = self.load_state()
        imports = self.load_import_accounts()
        items = [self.make_item(r, state, imports) for r in self.load_results()]
        if scope == "filtered":
            return filter_account_items(items, filters)
        wanted = {str(k) for k in keys}
        return [item for item in items if item["key"] in wanted]

    def patch_state(self, key: str, patch: dict) -> dict:
        key = unquote(key)
        state = self.load_state()
        entry = state.get(key, {}) if isinstance(state.get(key), dict) else {}
        apply_state_patch(entry, patch)
        entry["updated_at"] = utc_now()
        state[key] = entry
        self.save_state(state)
        return {"ok": True, "state": entry}

    def batch_patch_state(self, scope: str, keys: Iterable[str], filters: dict, patch: dict) -> dict:
        items = self.resolve_items(scope, keys, filters)
        state = self.load_state()
        updated = 0
        for item in items:
            key = item["key"]
            entry = state.get(key, {}) if isinstance(state.get(key), dict) else {}
            apply_state_patch(entry, patch)
            entry["updated_at"] = utc_now()
            state[key] = entry
            updated += 1
        self.save_state(state)
        return {"ok": True, "matched": len(items), "updated": updated}

    def mark_exported(self, keys: Iterable[str], batch_id: str) -> None:
        state = self.load_state()
        for key in keys:
            entry = state.get(key, {}) if isinstance(state.get(key), dict) else {}
            entry["export_status"] = "exported"
            entry["exported_at"] = utc_now()
            entry["export_batch_id"] = batch_id
            entry["updated_at"] = utc_now()
            state[key] = entry
        self.save_state(state)

    def mark_retry_result(self, source_key: str, *, retry_run_id: str, status: str, result_key: str = "", error: str = "") -> None:
        state = self.load_state()
        entry = state.get(source_key, {}) if isinstance(state.get(source_key), dict) else {}
        entry["retry_status"] = status
        entry["retried_at"] = utc_now()
        entry["retried_by"] = retry_run_id
        if result_key:
            entry["retry_result_key"] = result_key
        entry["last_retry_error"] = error
        entry["updated_at"] = utc_now()
        state[source_key] = entry
        self.save_state(state)


def apply_state_patch(entry: dict, patch: dict) -> None:
    for key, value in (patch or {}).items():
        if key not in MUTABLE_STATE_FIELDS:
            continue
        if key == "usage_status" and value not in USAGE_STATUSES:
            raise ValueError(f"invalid usage_status: {value}")
        if key == "export_status" and value not in EXPORT_STATUSES:
            raise ValueError(f"invalid export_status: {value}")
        entry[key] = value


def filter_account_items(items: list[dict], filters: dict) -> list[dict]:
    default_success_only = str(filters.get("success_only") or "").strip().lower() in {"1", "true", "yes"}
    failed_only = str(filters.get("failed_only") or "").strip().lower() in {"1", "true", "yes"}
    reg_status = str(filters.get("reg_status") or "").strip()
    usage_status = str(filters.get("usage_status") or "").strip()
    export_status = str(filters.get("export_status") or "").strip()
    exportable = str(filters.get("exportable") or "").strip().lower()
    retryable = str(filters.get("retryable") or "").strip().lower()
    q = str(filters.get("q") or "").strip().lower()
    date_from = str(filters.get("date_from") or "").strip()
    date_to = str(filters.get("date_to") or "").strip()

    def ok(item: dict) -> bool:
        status = item.get("reg_status")
        if default_success_only and status != "ok":
            return False
        if failed_only and status == "ok":
            return False
        if reg_status and reg_status != "all" and item.get("reg_status") != reg_status:
            return False
        if usage_status and usage_status != "all" and item.get("usage_status") != usage_status:
            return False
        if export_status and export_status != "all" and item.get("export_status") != export_status:
            return False
        if exportable in {"1", "true", "yes"} and not item.get("exportable"):
            return False
        if exportable in {"0", "false", "no"} and item.get("exportable"):
            return False
        if retryable in {"1", "true", "yes"} and not item.get("retry", {}).get("retryable"):
            return False
        if retryable in {"0", "false", "no"} and item.get("retry", {}).get("retryable"):
            return False
        saved = str(item.get("saved_at") or "")[:10]
        if date_from and saved and saved < date_from:
            return False
        if date_to and saved and saved > date_to:
            return False
        if q:
            hay = " ".join(str(item.get(k) or "") for k in ["phone", "bind_email", "sub2api_id", "phase2_error", "error", "note"]).lower()
            if q not in hay:
                return False
        return True

    return [item for item in items if ok(item)]


def sort_account_items(items: list[dict], filters: dict) -> list[dict]:
    sort_by = str(filters.get("sort") or "saved_at").strip()
    order = str(filters.get("order") or "desc").strip().lower()
    reverse = order != "asc"
    allowed = {
        "saved_at", "reg_status", "usage_status", "export_status",
        "phone", "bind_email", "sub2api_id",
    }
    if sort_by not in allowed:
        sort_by = "saved_at"
    status_rank = {
        "reg_status": {"ok": 0, "fail_phase2": 1, "interrupted_after_phase1": 2, "fail_phase1": 3, "fail": 4, "unknown": 5},
        "usage_status": {"unused": 0, "testing": 1, "used": 2, "reserved": 3, "bad": 4, "": 5},
        "export_status": {"not_exported": 0, "exported": 1, "ignored": 2, "": 3},
    }

    def key(item: dict) -> tuple:
        value = item.get(sort_by) or ""
        if sort_by in status_rank:
            rank = status_rank[sort_by].get(str(value), 99)
            return (rank, str(item.get("saved_at") or ""), str(item.get("phone") or ""))
        if sort_by == "sub2api_id":
            return (parse_int(value, 0, min_value=0), str(item.get("saved_at") or ""))
        return (str(value).lower(), str(item.get("saved_at") or ""))

    return sorted(items, key=key, reverse=reverse)


def account_stats(items: list[dict]) -> dict:
    stats = {
        "ok": 0, "fail_phase1": 0, "fail_phase2": 0, "failed": 0,
        "usage_unused": 0, "usage_used": 0, "usage_testing": 0, "usage_bad": 0, "usage_reserved": 0,
        "export_not_exported": 0, "exported": 0, "export_ignored": 0, "retryable_failed": 0,
    }
    for item in items:
        status = item.get("reg_status") or "unknown"
        if status == "ok":
            stats["ok"] += 1
        else:
            stats["failed"] += 1
            if item.get("retry", {}).get("retryable"):
                stats["retryable_failed"] += 1
        if status == "fail_phase1":
            stats["fail_phase1"] += 1
        if status == "fail_phase2":
            stats["fail_phase2"] += 1
        usage = item.get("usage_status")
        if usage == "unused":
            stats["usage_unused"] += 1
        elif usage == "used":
            stats["usage_used"] += 1
        elif usage == "testing":
            stats["usage_testing"] += 1
        elif usage == "bad":
            stats["usage_bad"] += 1
        elif usage == "reserved":
            stats["usage_reserved"] += 1
        export = item.get("export_status")
        if export == "not_exported":
            stats["export_not_exported"] += 1
        elif export == "exported":
            stats["exported"] += 1
        elif export == "ignored":
            stats["export_ignored"] += 1
    return stats


def retry_info_for_record(record: dict, state_entry: dict, exportable: bool) -> dict:
    status = normalize_reg_status(record)
    phone = bool(str(record.get("phone") or "").strip())
    password = bool(str(record.get("password") or "").strip())
    session = bool(record.get("session_token"))
    sub2api_id_present = bool(str(record.get("sub2api_id") or "").strip())
    stored_export_status = state_entry.get("export_status")
    export_status = "exported" if status == "ok" and (exportable or sub2api_id_present) else ("not_exported" if status == "ok" else "")
    if stored_export_status == "ignored":
        export_status = "ignored"
    elif stored_export_status and export_status != "exported":
        export_status = stored_export_status
    missing: list[str] = []
    if status == "fail_phase1":
        return {"retryable": True, "recommended_stage": "full", "reason": "Phase 1 失败，建议重跑完整注册", "missing": []}
    if status in {"fail_phase2", "interrupted_after_phase1"}:
        if not phone:
            missing.append("phone")
        if not password:
            missing.append("password")
        if not session:
            missing.append("session_token")
        return {
            "retryable": not missing,
            "recommended_stage": "phase2" if not missing else "full",
            "reason": "Phase 1 已成功，可尝试 Phase 2 续跑" if not missing else "缺少续跑材料，只能完整重跑",
            "missing": missing,
        }
    if status == "ok" and export_status == "not_exported":
        if str(record.get("sub2api_id") or "").strip():
            return {"retryable": False, "recommended_stage": "", "reason": "SUB2API 已有账号 ID，imports 中没有 payload 也视为已导入", "missing": []}
        return {
            "retryable": exportable,
            "recommended_stage": "export",
            "reason": "账号成功但尚未导出，可重试导出" if exportable else "imports 中没有可导出的 SUB2API payload",
            "missing": [] if exportable else ["import_payload"],
        }
    return {"retryable": False, "recommended_stage": "", "reason": "无需重试", "missing": []}


def load_pool_items(root: Path, filters: dict) -> dict:
    paths = get_paths(root)
    accounts = read_json(paths["pool"], [])
    if not isinstance(accounts, list):
        accounts = []
    used_data = read_json(paths["used"], {})
    records = {}
    old_used = set()
    if isinstance(used_data, dict):
        if isinstance(used_data.get("records"), dict):
            records = used_data["records"]
        elif isinstance(used_data.get("used"), list):
            old_used = {normalize_email(x) for x in used_data["used"]}
    items = []
    stats = {"total": len(accounts), "enabled": 0, "unused": 0, "used": 0, "error": 0, "reserved": 0, "disabled": 0}
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        email = normalize_email(acc.get("email"))
        enabled = bool(acc.get("enabled", True))
        stats["enabled" if enabled else "disabled"] += 1
        rec = records.get(email, {}) if isinstance(records.get(email), dict) else {}
        status = "unused"
        if not enabled:
            status = "disabled"
        if email in old_used or acc.get("used"):
            status = "used"
        if rec:
            status = rec.get("status") or "used"
            if str(rec.get("phone") or "").startswith("reserved:"):
                status = "reserved"
        if status in stats:
            stats[status] += 1
        items.append({
            "email": email, "enabled": enabled, "status": status,
            "phone": rec.get("phone", ""), "error": rec.get("error", ""), "used_at": rec.get("used_at", ""),
        })
    q = str(filters.get("q") or "").strip().lower()
    status_filter = str(filters.get("status") or "").strip()
    if status_filter and status_filter != "all":
        items = [x for x in items if x.get("status") == status_filter]
    if q:
        items = [x for x in items if q in " ".join(str(x.get(k) or "") for k in ["email", "phone", "error"]).lower()]
    items = sort_pool_items(items, filters)
    total = len(items)
    offset = parse_int(filters.get("offset"), 0, min_value=0)
    limit = parse_int(filters.get("limit"), 50, min_value=1, max_value=500)
    page = (offset // limit) + 1 if limit else 1
    pages = max(1, (total + limit - 1) // limit) if limit else 1
    return {"ok": True, "stats": stats, "total": total, "items": items[offset:offset + limit], "offset": offset, "limit": limit, "page": page, "pages": pages}


def sort_pool_items(items: list[dict], filters: dict) -> list[dict]:
    sort_by = str(filters.get("sort") or "status").strip()
    order = str(filters.get("order") or "asc").strip().lower()
    reverse = order == "desc"
    allowed = {"status", "email", "phone", "used_at", "error"}
    if sort_by not in allowed:
        sort_by = "status"
    status_rank = {"unused": 0, "reserved": 1, "used": 2, "error": 3, "disabled": 4, "": 5}

    def key(item: dict) -> tuple:
        value = item.get(sort_by) or ""
        if sort_by == "status":
            return (status_rank.get(str(value), 99), str(item.get("email") or ""))
        return (str(value).lower(), str(item.get("email") or ""))

    return sorted(items, key=key, reverse=reverse)


def append_pool_import_history(root: Path, result: dict) -> None:
    paths = get_paths(root)
    history = read_json(paths["pool_import_history"], [])
    if not isinstance(history, list):
        history = []
    history.append({
        "time": utc_now(),
        "added": int(result.get("added", 0) or 0),
        "skipped": int(result.get("skipped", 0) or 0),
        "total": int(result.get("total", 0) or 0),
    })
    write_json_atomic(paths["pool_import_history"], history)


def dashboard_summary(root: Path) -> dict:
    pool = load_pool_items(root, {"limit": 1})["stats"]
    store = AccountStore(root)
    all_accounts = store.list_accounts({"limit": 1})["stats"]
    import_summary = store.import_summary()
    return {
        "ok": True,
        "pool": {
            **pool,
            "available": pool.get("unused", 0),
            "import_added_total": import_summary["accounts_total"],
            "import_unique_total": import_summary["unique_accounts"],
            "import_batches": import_summary["files"],
            "last_import": {},
        },
        "accounts": {
            "registered_success": all_accounts["ok"],
            "registered_failed": all_accounts["failed"],
            "fail_phase1": all_accounts["fail_phase1"],
            "fail_phase2": all_accounts["fail_phase2"],
            "retryable_failed": all_accounts["retryable_failed"],
            "trial_success": all_accounts["exported"],
            "unused_success": all_accounts["export_not_exported"],
            "exported": all_accounts["exported"],
            "not_exported": all_accounts["export_not_exported"],
        },
    }


def configured_helper_url(root: Path, override: str = "") -> str:
    if str(override or "").strip():
        return str(override).strip().rstrip("/")
    return str(load_config(root).get("msoutlook", {}).get("helper_url") or "http://127.0.0.1:17373").strip().rstrip("/")


def helper_config(root: Path, override: Optional[dict] = None) -> dict:
    cfg = load_config(root)
    ms_cfg = cfg.get("msoutlook", {}) if isinstance(cfg.get("msoutlook"), dict) else {}
    override = override or {}
    mode = str(override.get("mode") or ms_cfg.get("helper_mode") or "direct").strip().lower() or "direct"
    url = str(override.get("helper_url") or override.get("url") or ms_cfg.get("helper_url") or "http://127.0.0.1:17373").strip().rstrip("/")
    script = str(override.get("helper_script") or ms_cfg.get("helper_script") or "").strip()
    bat = str(override.get("helper_bat") or ms_cfg.get("helper_bat") or "").strip()
    if not script and bat:
        candidate = Path(bat).parent / "scripts" / "hotmail_helper.py"
        if candidate.exists():
            script = str(candidate)
    return {
        "mode": mode,
        "url": url,
        "script": script,
        "bat": bat,
        "email": str(override.get("email") or ms_cfg.get("email") or "").strip(),
    }


def load_helper_module(script_path: str):
    path = Path(str(script_path or "").strip()).expanduser()
    if not path.exists():
        raise RuntimeError(f"hotmail_helper.py 不存在: {path}")
    if not path.is_file():
        raise RuntimeError(f"不是文件: {path}")
    mtime = path.stat().st_mtime
    key = str(path.resolve())
    cached = HELPER_MODULE_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    module_name = "direct_hotmail_helper_" + re.sub(r"\W+", "_", key)[-80:]
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 hotmail_helper.py: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("collect_messages", "select_latest_code"):
        if not hasattr(module, name):
            raise RuntimeError(f"hotmail_helper.py 缺少函数 {name}")
    HELPER_MODULE_CACHE[key] = (mtime, module)
    return module


def helper_account(root: Path, email: str):
    pool = make_msoutlook_pool(root, "")
    acc = pool.get_account(email)
    if not acc:
        raise RuntimeError(f"账号不存在: {email}")
    if not acc.client_id or not acc.refresh_token:
        raise RuntimeError(f"账号缺少 clientId 或 refreshToken: {email}")
    return acc


def direct_helper_messages(root: Path, email: str, top: int, data: Optional[dict] = None) -> dict:
    cfg = helper_config(root, data)
    module = load_helper_module(cfg["script"])
    acc = helper_account(root, email)
    result = module.collect_messages(acc.email, acc.client_id, acc.refresh_token, ["INBOX"], top)
    messages = result.get("messages") if isinstance(result, dict) else []
    token_payload = result.get("token_payload", {}) if isinstance(result, dict) else {}
    return {
        "ok": True,
        "messages": messages or [],
        "mailboxResults": result.get("mailboxResults", []) if isinstance(result, dict) else [],
        "nextRefreshToken": token_payload.get("next_refresh_token") or "",
        "tokenEndpoint": token_payload.get("token_endpoint") or "",
        "transport": result.get("transport") or "",
        "errors": result.get("errors") or [],
    }


def direct_helper_code(root: Path, email: str, keyword: str, top: int, data: dict) -> dict:
    cfg = helper_config(root, data)
    module = load_helper_module(cfg["script"])
    acc = helper_account(root, email)
    result = module.collect_messages(acc.email, acc.client_id, acc.refresh_token, ["INBOX"], top)
    messages = result.get("messages") if isinstance(result, dict) else []
    selected = module.select_latest_code(
        messages or [],
        data.get("senderFilters") or [keyword, "noreply@tm.openai.com", "noreply@openai.com"],
        data.get("subjectFilters") or [],
        data.get("excludeCodes") or [],
        int(data.get("filterAfterTimestamp") or 0),
        data.get("requiredKeywords") or [],
        data.get("codePatterns") or [],
    )
    token_payload = result.get("token_payload", {}) if isinstance(result, dict) else {}
    return {
        "ok": True,
        "code": selected.get("code") or "",
        "message": selected.get("message"),
        "usedTimeFallback": bool(selected.get("usedTimeFallback")),
        "nextRefreshToken": token_payload.get("next_refresh_token") or "",
        "tokenEndpoint": token_payload.get("token_endpoint") or "",
        "transport": result.get("transport") or "",
        "errors": result.get("errors") or [],
    }


def helper_status(root: Path, url: str = "", mode: str = "", script: str = "") -> dict:
    cfg = helper_config(root, {"helper_url": url, "mode": mode, "helper_script": script})
    if cfg["mode"] == "direct":
        try:
            module = load_helper_module(cfg["script"])
            api_base = str(getattr(module, "OUTLOOK_PROXY_API_BASE", "") or "")
            probe = {}
            if hasattr(module, "get_outlook_proxy_security_session"):
                try:
                    session = module.get_outlook_proxy_security_session()
                    probe = {"security_session": bool(session.get("sessionId"))}
                except Exception as exc:
                    return {
                        "ok": False,
                        "reachable": False,
                        "mode": "direct",
                        "script": cfg["script"],
                        "api_base": api_base,
                        "error": str(exc),
                    }
            return {
                "ok": True,
                "reachable": True,
                "mode": "direct",
                "script": cfg["script"],
                "api_base": api_base,
                "message": "直连接码 API 可用，无需启动 bat",
                **probe,
            }
        except Exception as exc:
            return {"ok": False, "reachable": False, "mode": "direct", "script": cfg.get("script", ""), "error": str(exc)}

    helper_url = configured_helper_url(root, cfg["url"])
    if not helper_url:
        return {"ok": False, "reachable": False, "url": "", "error": "helper_url 为空"}
    req = UrlRequest(f"{helper_url}/messages", data=b"{}", headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "reachable": True, "url": helper_url, "status": resp.status, "body": body[:300], "error": ""}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # helper 没有健康检查；/messages 空 payload 返回 500 Missing email/clientId/refreshToken
        # 说明 HTTP 服务已可达。
        reachable = "Missing email/clientId/refreshToken" in body or exc.code in {400, 500}
        return {"ok": reachable, "reachable": reachable, "url": helper_url, "status": exc.code, "body": body[:300], "error": "" if reachable else body[:300]}
    except URLError as exc:
        return {"ok": False, "reachable": False, "url": helper_url, "error": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "reachable": False, "url": helper_url, "error": str(exc)}


def start_helper_process(root: Path, data: dict) -> dict:
    cfg = load_config(root)
    ms_cfg = cfg.get("msoutlook", {}) if isinstance(cfg.get("msoutlook"), dict) else {}
    bat_path = str(data.get("bat_path") or ms_cfg.get("helper_bat") or "").strip()
    if not bat_path:
        return {"ok": False, "error": "请先填写 Hotmail Helper bat 路径"}
    bat = Path(bat_path)
    if not bat.exists():
        return {"ok": False, "error": f"bat 文件不存在: {bat_path}"}
    raw_ports = data.get("ports")
    if isinstance(raw_ports, list):
        ports = [str(parse_int(p, 17373, min_value=1, max_value=65535)) for p in raw_ports if str(p).strip()]
    else:
        ports_text = str(data.get("port") or raw_ports or "17373")
        ports = [str(parse_int(x, 17373, min_value=1, max_value=65535)) for x in re.split(r"[,;\s]+", ports_text) if x.strip()]
    if not ports:
        ports = ["17373"]
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        cmd = ["cmd", "/c", str(bat), *ports]
    else:
        flags = 0
        cmd = [str(bat), *ports]
    try:
        proc = subprocess.Popen(cmd, cwd=str(bat.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": cmd}
    return {"ok": True, "pid": proc.pid, "ports": ports, "bat_path": str(bat), "command": cmd}


def make_msoutlook_pool(root: Path, helper_url: str = ""):
    from msoutlook_pool import MsOutlookPool
    cfg = helper_config(root)

    return MsOutlookPool(
        pool_path=str(get_paths(root)["pool"]),
        helper_url=configured_helper_url(root, helper_url),
        used_file=str(get_paths(root)["used"]),
        verbose=False,
        helper_mode=cfg["mode"],
        helper_script=cfg["script"],
    )


def helper_messages(root: Path, data: dict) -> dict:
    cfg = helper_config(root, data)
    mode = str(data.get("mode") or cfg["mode"]).strip().lower() or "direct"
    email = normalize_email(data.get("email") or cfg["email"])
    top = parse_int(data.get("top"), 5, min_value=1, max_value=30)
    if not email:
        return {"ok": False, "error": "邮箱为空"}
    if mode == "direct":
        result = direct_helper_messages(root, email, top, data)
        messages = result.get("messages") if isinstance(result, dict) else []
        return {"ok": True, "mode": "direct", "email": email, "count": len(messages or []), "result": result}
    pool = make_msoutlook_pool(root, str(data.get("helper_url") or ""))
    result = pool.get_messages(email, top=top)
    messages = result.get("messages") if isinstance(result, dict) else []
    return {"ok": True, "mode": "http", "email": email, "count": len(messages or []), "result": result}


def helper_code(root: Path, data: dict) -> dict:
    cfg = helper_config(root)
    mode = str(data.get("mode") or cfg["mode"]).strip().lower() or "direct"
    email = normalize_email(data.get("email") or cfg["email"])
    keyword = str(data.get("keyword") or "openai").strip()
    top = parse_int(data.get("top"), 30, min_value=1, max_value=30)
    if not email:
        return {"ok": False, "error": "邮箱为空"}
    if mode == "direct":
        result = direct_helper_code(root, email, keyword, top, data)
        code = result.get("code") or ""
        return {"ok": True, "mode": "direct", "email": email, "keyword": keyword, "code": code, "message": "找到验证码" if code else "未找到验证码", "result": result}
    pool = make_msoutlook_pool(root, str(data.get("helper_url") or ""))
    code = pool.get_code_once(email, keyword=keyword)
    return {"ok": True, "mode": "http", "email": email, "keyword": keyword, "code": code or "", "message": "找到验证码" if code else "未找到验证码"}


def export_sub_payload(store: AccountStore, scope: str, keys: Iterable[str], filters: dict, *, mark_exported: bool) -> tuple[dict, list[str]]:
    items = store.resolve_items(scope, keys, filters)
    items = [item for item in items if item.get("reg_status") == "ok"]
    imports = store.load_import_accounts()
    batch_id = f"export-{utc_filename_ts()}"
    accounts = []
    exported_keys = []
    skipped = 0
    for item in items:
        if item.get("export_status") == "ignored" and not filters.get("include_ignored"):
            skipped += 1
            continue
        email = normalize_email(item.get("bind_email"))
        payload = imports.get(email)
        if not payload:
            skipped += 1
            continue
        accounts.append(copy.deepcopy(payload))
        exported_keys.append(item["key"])
    export_payload = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
        "_meta": {"export_batch_id": batch_id, "selected": len(items), "exported": len(accounts), "skipped": skipped},
    }
    if mark_exported and exported_keys:
        store.mark_exported(exported_keys, batch_id)
    return export_payload, exported_keys


class RetryController:
    def __init__(self, root: Path, worker_controller: WorkerProcessController):
        self.root = Path(root)
        self.worker_controller = worker_controller
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None
        self.run: Optional[dict] = None
        self.stop_event = threading.Event()

    def is_running(self) -> bool:
        with self.lock:
            return bool(self.thread and self.thread.is_alive())

    def start(self, store: AccountStore, request_data: dict) -> tuple[bool, dict]:
        scope = request_data.get("scope") or "selected"
        keys = request_data.get("keys") or []
        filters = request_data.get("filters") or {}
        stage = request_data.get("stage") or "auto"
        options = request_data.get("options") or {}
        items = store.resolve_items(scope, keys, filters)
        if not items:
            return False, {"error": "没有匹配的可重试账号"}
        if self.worker_controller.is_running() or self.is_running():
            return False, {"error": "已有运行中的任务"}
        if stage == "auto":
            stage = items[0].get("retry", {}).get("recommended_stage") or "full"
        run_id = f"retry-{utc_filename_ts()}"
        with self.lock:
            self.stop_event = threading.Event()
            self.run = {"id": run_id, "stage": stage, "total": len(items), "done": 0, "started_at": utc_now(), "ended_at": "", "running": True}
        if stage == "full":
            ok, data = self.worker_controller.start({"count": len(items), **options})
            if not ok:
                with self.lock:
                    self.run["running"] = False
                    self.run["ended_at"] = utc_now()
                return False, data
            return True, {"retry_run": dict(self.run), "worker_run": data.get("run", {})}
        self.thread = threading.Thread(target=self._run_retry, args=(store, items, stage, options, run_id), daemon=True)
        self.thread.start()
        return True, {"retry_run": dict(self.run)}

    def _run_retry(self, store: AccountStore, items: list[dict], stage: str, options: dict, run_id: str) -> None:
        self.worker_controller._append_log(f"[retry] started id={run_id} stage={stage} total={len(items)}", "info")
        try:
            if stage == "export":
                payload, exported_keys = export_sub_payload(store, "selected", [x["key"] for x in items], {}, mark_exported=True)
                out = get_paths(self.root)["results_dir"] / f"sub2api_retry_export_{utc_filename_ts()}.json"
                write_json_atomic(out, payload)
                self.worker_controller._append_log(f"[retry] export wrote {out} accounts={len(exported_keys)}", "success")
                for key in exported_keys:
                    store.mark_retry_result(key, retry_run_id=run_id, status="retried_success", result_key=key)
            elif stage == "phase2":
                max_items = parse_int(options.get("max_items"), len(items), min_value=1)
                for item in items[:max_items]:
                    if self.stop_event.is_set():
                        break
                    try:
                        result_key = self._retry_phase2_one(item)
                        store.mark_retry_result(item["key"], retry_run_id=run_id, status="retried_success", result_key=result_key)
                    except Exception as exc:
                        store.mark_retry_result(item["key"], retry_run_id=run_id, status="retried_failed", error=str(exc))
                        self.worker_controller._append_log(f"[retry] {item.get('phone') or item.get('key')} failed: {exc}", "error")
                    with self.lock:
                        if self.run:
                            self.run["done"] = int(self.run.get("done", 0)) + 1
            else:
                self.worker_controller._append_log(f"[retry] unsupported stage={stage}", "error")
        finally:
            with self.lock:
                if self.run:
                    self.run["running"] = False
                    self.run["ended_at"] = utc_now()
            self.worker_controller._append_log(f"[retry] ended id={run_id}", "info")

    def _retry_phase2_one(self, item: dict) -> str:
        import requests
        from urllib.parse import parse_qs, urlparse
        import auto_register as ar
        from msoutlook_pool import MsOutlookPool, load_used_set
        from openai_bind_email import run_second_half
        from worker_pool import ResultWriter

        cfg = ar.load_config(str(self.root / "config.json"))
        sub = cfg.get("sub2api", {})
        sub_url = (sub.get("url") or "").rstrip("/")
        sub_email = sub.get("email") or ""
        sub_pwd = sub.get("pwd") or ""
        helper_url = cfg.get("msoutlook", {}).get("helper_url", "")
        if not (sub_url and sub_email and sub_pwd and helper_url):
            raise RuntimeError("缺少 SUB2API 或 MsOutlook 配置")
        phone = item.get("phone") or ""
        password = item.get("password") or ""
        if not (phone and password):
            raise RuntimeError("缺少 phone/password，无法 Phase 2 续跑")

        pool = MsOutlookPool(helper_url=helper_url, verbose=False, extra_used=load_used_set())
        for _ in range(10):
            current_email = pool.get_available_email()
            if not current_email:
                raise RuntimeError("号池无可用邮箱")
            login = requests.post(f"{sub_url}/api/v1/auth/login", json={"email": sub_email, "password": sub_pwd}, timeout=30).json()
            if login.get("code") != 0:
                raise RuntimeError(f"SUB2API 登录失败: {login}")
            token = login["data"]["access_token"]
            body = {"redirect_uri": "http://localhost:1455/auth/callback"}
            proxy_id = int(sub.get("proxy_id", 0) or 0)
            if proxy_id:
                body["proxy_id"] = proxy_id
            oauth = requests.post(
                f"{sub_url}/api/v1/admin/openai/generate-auth-url",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            ).json()
            if oauth.get("code") != 0:
                raise RuntimeError(f"获取 OAuth URL 失败: {oauth}")
            oauth_url = oauth["data"]["auth_url"]
            session_id = oauth["data"]["session_id"]
            state = oauth["data"].get("state") or parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
            result = run_second_half(
                oauth_url=oauth_url, phone=phone, password=password,
                icloud_email=current_email, icloud_cookies={},
                imap_user=cfg.get("icloud", {}).get("user", ""),
                imap_password=cfg.get("icloud", {}).get("pass", ""),
                sub2api_url=sub_url, sub2api_email=sub_email, sub2api_password=sub_pwd,
                sub2api_proxy_id=proxy_id, proxy=cfg.get("proxy", ""), verbose=True,
                sub2api_session_id=session_id, sub2api_state=state,
                msoutlook_helper_url=helper_url, msoutlook_email=current_email,
                msoutlook_helper_mode=str(cfg.get("msoutlook", {}).get("helper_mode") or "http"),
                msoutlook_helper_script=str(cfg.get("msoutlook", {}).get("helper_script") or ""),
                save_import=False, interactive_input=False,
            )
            if result.get("ok"):
                pool.mark_used(current_email, phone=phone, password=password)
                writer = ResultWriter(self.root / "results", self.root / "imports")
                record = {
                    "status": "ok", "phone": phone, "password": password, "bind_email": current_email,
                    "sub2api_id": result.get("sub2api_account_id", ""), "phase2_error": "", "saved_at": utc_now(),
                }
                writer.append_account(record)
                if result.get("import_data"):
                    writer.append_import(result["import_data"])
                self.worker_controller._append_log(f"[retry] phase2 success phone={phone} email={current_email}", "success")
                return account_key(record)
            err = result.get("error", "")
            if "email_already_in_use" in err:
                pool.mark_error(current_email, "email_already_in_use", phone=phone, password=password)
                continue
            raise RuntimeError(err or "Phase 2 retry failed")
        raise RuntimeError("换邮箱重试耗尽")

    def status(self) -> dict:
        with self.lock:
            run = dict(self.run or {})
            running = bool(self.thread and self.thread.is_alive())
        run["running"] = running
        return {"ok": True, "retry_run": run}

    def stop(self) -> dict:
        self.stop_event.set()
        with self.lock:
            if self.run:
                self.run["stopping"] = True
        return {"ok": True, "stopping": True}


def create_worker_control_blueprint(root: Path, worker_controller: Optional[WorkerProcessController] = None) -> Blueprint:
    root = Path(root)
    bp = Blueprint("worker_control", __name__)
    controller = worker_controller or WorkerProcessController(root)
    retry_controller = RetryController(root, controller)
    store = AccountStore(root)

    @bp.route("/worker-control")
    def worker_control_page():
        return send_file(get_paths(root)["public_worker_control"])

    @bp.route("/api/worker/start", methods=["POST"])
    def api_worker_start():
        ok, data = controller.start(request.json or {})
        return jsonify({"ok": ok, **data})

    @bp.route("/api/worker/stop", methods=["POST"])
    def api_worker_stop():
        return jsonify(controller.stop())

    @bp.route("/api/worker/status")
    def api_worker_status():
        return jsonify(controller.status())

    @bp.route("/api/worker/log-since/<int:cursor>")
    def api_worker_logs(cursor: int):
        return jsonify(controller.log_since(cursor, request.args.get("stream", "")))

    @bp.route("/api/worker/log-streams")
    def api_worker_log_streams():
        return jsonify(controller.log_streams())

    @bp.route("/api/dashboard/summary")
    def api_dashboard_summary():
        return jsonify(dashboard_summary(root))

    @bp.route("/api/pool/accounts")
    def api_pool_accounts():
        return jsonify(load_pool_items(root, request.args.to_dict()))

    @bp.route("/api/pool/import", methods=["POST"])
    def api_pool_import():
        text = (request.json or {}).get("text", "")
        if not str(text).strip():
            return jsonify({"ok": False, "error": "内容为空"})
        try:
            from msoutlook_pool import MsOutlookPool
            result = MsOutlookPool.import_accounts(text)
            if result.get("ok"):
                append_pool_import_history(root, result)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/accounts")
    def api_accounts():
        return jsonify(store.list_accounts(request.args.to_dict()))

    @bp.route("/api/accounts/failed")
    def api_failed_accounts():
        args = request.args.to_dict()
        args["failed_only"] = "1"
        return jsonify(store.list_accounts(args))

    @bp.route("/api/accounts/<path:key>/state", methods=["PATCH"])
    def api_account_state(key: str):
        try:
            return jsonify(store.patch_state(key, request.json or {}))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/accounts/batch-state", methods=["PATCH"])
    def api_accounts_batch_state():
        d = request.json or {}
        try:
            return jsonify(store.batch_patch_state(d.get("scope") or "selected", d.get("keys") or [], d.get("filters") or {}, d.get("patch") or {}))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/export/sub", methods=["POST"])
    def api_export_sub():
        d = request.json or {}
        try:
            payload, _keys = export_sub_payload(
                store, d.get("scope") or "selected", d.get("keys") or [], d.get("filters") or {},
                mark_exported=bool(d.get("mark_exported")),
            )
            out = get_paths(root)["results_dir"] / f"sub2api_export_{utc_filename_ts()}.json"
            write_json_atomic(out, payload)
            return send_file(out, as_attachment=True, download_name=out.name, mimetype="application/json")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/config/raw", methods=["GET", "PUT"])
    def api_config_raw():
        path = get_paths(root)["config"]
        if request.method == "GET":
            return jsonify({"ok": True, "path": str(path), "text": path.read_text(encoding="utf-8") if path.exists() else "{}\n"})
        text = (request.json or {}).get("text", "")
        try:
            parsed = json.loads(text)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"JSON 解析失败: {exc}"})
        try:
            write_json_atomic(path, parsed)
            return jsonify({"ok": True, "config": parsed})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/config/form", methods=["GET", "PUT"])
    def api_config_form():
        try:
            if request.method == "GET":
                return jsonify(config_form(root))
            return jsonify(update_config_form(root, (request.json or {}).get("values") or {}))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/helper/status")
    def api_helper_status():
        return jsonify(helper_status(
            root,
            request.args.get("url", ""),
            request.args.get("mode", ""),
            request.args.get("script", ""),
        ))

    @bp.route("/api/helper/start", methods=["POST"])
    def api_helper_start():
        return jsonify(start_helper_process(root, request.json or {}))

    @bp.route("/api/helper/messages", methods=["POST"])
    def api_helper_messages():
        try:
            return jsonify(helper_messages(root, request.json or {}))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/helper/code", methods=["POST"])
    def api_helper_code():
        try:
            return jsonify(helper_code(root, request.json or {}))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @bp.route("/api/retry/start", methods=["POST"])
    def api_retry_start():
        ok, data = retry_controller.start(store, request.json or {})
        return jsonify({"ok": ok, **data})

    @bp.route("/api/retry/<path:key>/start", methods=["POST"])
    def api_retry_one(key: str):
        d = request.json or {}
        ok, data = retry_controller.start(store, {
            "scope": "selected",
            "keys": [key],
            "filters": {},
            "stage": d.get("stage") or "auto",
            "options": d.get("options") or {"concurrency": 1, "max_items": 1},
        })
        return jsonify({"ok": ok, **data})

    @bp.route("/api/retry/status")
    def api_retry_status():
        return jsonify(retry_controller.status())

    @bp.route("/api/retry/stop", methods=["POST"])
    def api_retry_stop():
        return jsonify(retry_controller.stop())

    bp.worker_controller = controller  # type: ignore[attr-defined]
    bp.retry_controller = retry_controller  # type: ignore[attr-defined]
    return bp
