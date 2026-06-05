#!/usr/bin/env python3
"""
微软 Outlook 号池管理 + Hotmail Helper 验证码获取

功能:
  - 从 号.json 加载可用账号池
  - 自动选择未使用的 Outlook 邮箱
  - 通过 Hotmail Helper HTTP API 轮询获取验证码

用法:
    from msoutlook_pool import MsOutlookPool
    pool = MsOutlookPool()
    email = pool.get_available_email()          # 获取一个可用邮箱
    code = pool.wait_for_code(email, timeout=60) # 轮询等验证码
    pool.mark_used(email)                        # 标记已使用
"""

import json
import importlib.util
import re
import time
import threading
from pathlib import Path
from typing import Optional, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_POOL_PATH = str(Path(__file__).parent / "号.json")
DEFAULT_HELPER_URL = "http://127.0.0.1:17373"
DEFAULT_USED_FILE = str(Path(__file__).parent / "msoutlook_used.json")
DEFAULT_HELPER_SCRIPT = ""
_HELPER_MODULE_CACHE = {}


class _Account:
    """单个微软号池账号"""
    def __init__(self, data: dict):
        self.email = data.get("email", "")
        self.password = data.get("password", "")
        self.refresh_token = data.get("refreshToken", "")
        self.client_id = data.get("clientId", "")
        self.id = data.get("id", "")
        self.enabled = data.get("enabled", True)
        self.status = data.get("status", "pending")
        self.used = data.get("used", False)
        self._raw = data


class MsOutlookPool:
    """微软 Outlook 号池管理"""

    def __init__(
        self,
        pool_path: str = DEFAULT_POOL_PATH,
        helper_url: str = DEFAULT_HELPER_URL,
        used_file: str = DEFAULT_USED_FILE,
        verbose: bool = False,
        extra_used: set = None,
        helper_mode: str = "http",
        helper_script: str = DEFAULT_HELPER_SCRIPT,
    ):
        self.pool_path = pool_path
        self.helper_url = helper_url.rstrip("/")
        self.used_file = used_file
        self.verbose = verbose
        self.helper_mode = str(helper_mode or "http").strip().lower()
        self.helper_script = str(helper_script or "").strip()
        self._lock = threading.Lock()
        self._records: dict = {}  # email -> {status, phone, used_at, error}
        self._used: set = self._load_used()
        # 合并外部已用邮箱（如 results/_all.json 中的 bind_email）
        if extra_used:
            self._used |= {e.lower() for e in extra_used}
        self._accounts: List[_Account] = self._load_pool()

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [MsOutlook] {msg}")

    # ---- 号池管理 ----

    def _load_pool(self) -> List[_Account]:
        if not Path(self.pool_path).exists():
            raise RuntimeError(f"号池文件不存在: {self.pool_path}")
        with open(self.pool_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = [_Account(item) for item in data if isinstance(item, dict)]
        self._log(f"号池加载完成: {len(accounts)} 个账号")
        return accounts

    def _load_used(self) -> set:
        if Path(self.used_file).exists():
            try:
                with open(self.used_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 新格式: {"records": {email: {status, phone, used_at, error}}}
                if "records" in data and isinstance(data["records"], dict):
                    self._records = data["records"]
                    return {e.lower() for e in self._records}
                # 旧格式兼容: {"used": [email, ...]}
                if "used" in data and isinstance(data["used"], list):
                    emails = set(data["used"])
                    self._records = {e: {"status": "used", "phone": "", "used_at": "", "error": ""} for e in emails}
                    return {e.lower() for e in emails}
            except Exception:
                pass
        return set()

    def _save_used(self):
        try:
            with open(self.used_file, "w", encoding="utf-8") as f:
                json.dump({"records": self._records}, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def get_available_email(self, keyword: str = "") -> Optional[str]:
        """获取一个可用邮箱（自动跳过已使用的）"""
        with self._lock:
            for acc in self._accounts:
                if not acc.enabled:
                    continue
                if acc.used:
                    continue
                if acc.email.lower() in self._used:
                    continue
                if keyword and keyword.lower() not in acc.email.lower():
                    continue
                self._log(f"选定邮箱: {acc.email}")
                return acc.email
        self._log("号池无可用邮箱")
        return None

    def get_account(self, email: str) -> Optional[_Account]:
        """根据邮箱获取账号信息"""
        for acc in self._accounts:
            if acc.email == email:
                return acc
        return None

    def mark_used(self, email: str, phone: str = "", password: str = ""):
        """标记邮箱为已使用（成功绑定），可记录关联手机号和账号密码。"""
        email = email.strip().lower()
        with self._lock:
            # 标记号池中的 used
            for acc in self._accounts:
                if acc.email.lower() == email:
                    acc.used = True
                    acc._raw["used"] = True
                    break
            self._used.add(email)
            self._records[email] = {
                "status": "used",
                "phone": phone,
                "password": password,
                "used_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": "",
            }
            self._save_used()
            self._log(f"标记已用: {email}" + (f" (手机: {phone})" if phone else ""))

    def mark_error(self, email: str, error: str = "", phone: str = "", password: str = ""):
        """标记邮箱有问题（被占用、验证失败等），不再选用。"""
        email = email.strip().lower()
        with self._lock:
            # 标记号池中的 used（防止重复选中）
            for acc in self._accounts:
                if acc.email.lower() == email:
                    acc.used = True
                    acc._raw["used"] = True
                    break
            self._used.add(email)
            record = self._records.get(email, {})
            self._records[email] = {
                "status": "error",
                "phone": phone or record.get("phone", ""),
                "password": password or record.get("password", ""),
                "used_at": record.get("used_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                "error": error,
            }
            self._save_used()
            self._log(f"标记错误: {email} ({error})")

    def mark_unused(self, email: str):
        """取消标记（恢复为可用）"""
        email = email.strip().lower()
        with self._lock:
            for acc in self._accounts:
                if acc.email.lower() == email:
                    acc.used = False
                    acc._raw["used"] = False
                    break
            self._used.discard(email)
            self._records.pop(email, None)
            self._save_used()

    def stats(self) -> dict:
        with self._lock:
            total = len(self._accounts)
            enabled = sum(1 for a in self._accounts if a.enabled)
            used_count = sum(1 for r in self._records.values() if r.get("status") == "used")
            error_count = sum(1 for r in self._records.values() if r.get("status") == "error")
            filtered = len(self._used)
            available = max(0, enabled - filtered)
            return {"total": total, "enabled": enabled, "used": used_count, "error": error_count, "available": available}

    def get_records(self) -> dict:
        """获取所有已用/错误邮箱记录"""
        with self._lock:
            return dict(self._records)

    @staticmethod
    def import_accounts(text: str, pool_path: str = DEFAULT_POOL_PATH) -> dict:
        """从文本批量导入账号到号池

        格式: 账号----密码----ID----Token (每行一个，4个短横线分隔)
        返回: {"ok": bool, "added": int, "skipped": int, "total": int}
        """
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if not lines:
            return {"ok": False, "error": "内容为空"}

        existing = []
        if Path(pool_path).exists():
            try:
                with open(pool_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        if not isinstance(existing, list):
            existing = []

        existing_emails = {a.get("email", "").lower() for a in existing if isinstance(a, dict)}
        # 从已有账号中提取 clientId（所有账号共享同一个）
        default_client_id = ""
        for a in existing:
            cid = a.get("clientId", "")
            if cid:
                default_client_id = cid
                break
        added = 0
        skipped = 0

        for line in lines:
            parts = line.split("----")
            if len(parts) < 4:
                skipped += 1
                continue
            email = parts[0].strip()
            password = parts[1].strip()
            account_id = parts[2].strip()
            token = parts[3].strip()
            if not email or not token:
                skipped += 1
                continue
            if email.lower() in existing_emails:
                skipped += 1
                continue
            account = {
                "clientId": default_client_id,
                "email": email,
                "enabled": True,
                "id": account_id,
                "lastAuthAt": 0,
                "lastError": "",
                "lastUsedAt": 0,
                "password": password,
                "paypalPaymentSuccess": True,
                "refreshToken": token,
                "status": "authorized",
                "used": False,
            }
            existing.append(account)
            existing_emails.add(email.lower())
            added += 1

        if added > 0:
            with open(pool_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)

        return {"ok": True, "added": added, "skipped": skipped, "total": len(existing)}

    # ---- Hotmail Helper API ----

    def _helper_account_payload(self, email: str):
        acc = self.get_account(email)
        if not acc:
            raise RuntimeError(f"账号不存在: {email}")
        if not acc.client_id or not acc.refresh_token:
            raise RuntimeError(f"账号缺少 clientId 或 refreshToken: {email}")
        return acc

    def _load_direct_helper(self):
        path_text = self.helper_script
        if not path_text:
            bat = Path(str(self.helper_url or ""))
            if bat.exists() and bat.name.lower().endswith(".bat"):
                candidate = bat.parent / "scripts" / "hotmail_helper.py"
                if candidate.exists():
                    path_text = str(candidate)
        if not path_text:
            raise RuntimeError("direct 模式需要 helper_script 指向 hotmail_helper.py")
        path = Path(path_text).expanduser()
        if not path.exists():
            raise RuntimeError(f"hotmail_helper.py 不存在: {path}")
        key = str(path.resolve())
        mtime = path.stat().st_mtime
        cached = _HELPER_MODULE_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        module_name = "msoutlook_direct_hotmail_helper_" + re.sub(r"\W+", "_", key)[-80:]
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 hotmail_helper.py: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("collect_messages", "select_latest_code"):
            if not hasattr(module, name):
                raise RuntimeError(f"hotmail_helper.py 缺少函数 {name}")
        _HELPER_MODULE_CACHE[key] = (mtime, module)
        return module

    def _direct_messages(self, email: str, top: int = 5) -> dict:
        acc = self._helper_account_payload(email)
        module = self._load_direct_helper()
        result = module.collect_messages(acc.email, acc.client_id, acc.refresh_token, ["INBOX"], max(1, min(int(top or 5), 30)))
        token_payload = result.get("token_payload", {}) if isinstance(result, dict) else {}
        return {
            "ok": True,
            "messages": result.get("messages", []) if isinstance(result, dict) else [],
            "mailboxResults": result.get("mailboxResults", []) if isinstance(result, dict) else [],
            "nextRefreshToken": token_payload.get("next_refresh_token") or "",
            "tokenEndpoint": token_payload.get("token_endpoint") or "",
            "transport": result.get("transport") or "",
            "errors": result.get("errors") or [],
        }

    def _direct_code(self, email: str, keyword: str = "openai", exclude_codes: list = None) -> dict:
        module = self._load_direct_helper()
        result = self._direct_messages(email, top=30)
        selected = module.select_latest_code(
            result.get("messages") or [],
            [keyword, "noreply@tm.openai.com", "noreply@openai.com"],
            [],
            exclude_codes or [],
            0,
            [],
            [],
        )
        return {
            **result,
            "code": selected.get("code") or "",
            "message": selected.get("message"),
            "usedTimeFallback": bool(selected.get("usedTimeFallback")),
        }

    def _helper_post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        url = f"{self.helper_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Hotmail Helper {path} 失败 HTTP {e.code}: {body}") from e
        except URLError as e:
            raise RuntimeError(f"Hotmail Helper 连接失败: {e}") from e

    def get_messages(self, email: str, top: int = 5) -> dict:
        """获取邮件列表"""
        if self.helper_mode == "direct":
            return self._direct_messages(email, top=top)
        acc = self._helper_account_payload(email)
        return self._helper_post("/messages", {
            "email": acc.email,
            "clientId": acc.client_id,
            "refreshToken": acc.refresh_token,
            "top": top,
        })

    def get_code_once(self, email: str, keyword: str = "openai", exclude_codes: list = None) -> Optional[str]:
        """单次尝试获取验证码"""
        if self.helper_mode == "direct":
            result = self._direct_code(email, keyword=keyword, exclude_codes=exclude_codes)
            code = result.get("code", "")
            if code:
                self._log(f"找到验证码: {code}")
            return code or None
        acc = self._helper_account_payload(email)
        result = self._helper_post("/code", {
            "email": acc.email,
            "clientId": acc.client_id,
            "refreshToken": acc.refresh_token,
            "top": 30,
            "senderFilters": [keyword, "noreply@tm.openai.com", "noreply@openai.com"],
            "excludeCodes": exclude_codes or [],
        })
        code = result.get("code", "")
        if code:
            self._log(f"找到验证码: {code}")
            return code
        return None

    def wait_for_code(
        self,
        email: str,
        keyword: str = "openai",
        timeout: int = 240,
        interval: int = 5,
        exclude_codes: list = None,
    ) -> Optional[str]:
        """轮询等待验证码，直接调用 Hotmail Helper /code 端点。

        语义：第一次拿到非空验证码即返回。
        调用方需保证：
          1. 在 OpenAI 触发发邮件之后再调用本方法；
          2. 若同一邮箱会被复用，请通过 exclude_codes 传入历史用过的验证码，
             或在每次注册完成后调 mark_used(email) 把邮箱从池中剔除。

        历史曾有 baseline 机制（取"当前最新码"作为旧码基准，等"比基准更新的码"），
        但 OpenAI 邮件通常在 wait_for_code 启动前已到达，导致 baseline 锁住的
        恰好是本次注册的真验证码，循环永远 code == baseline 不返回。已移除。
        """
        self._log(f"开始轮询 {email} 验证码 (keyword={keyword}, timeout={timeout}s)")
        start = time.time()

        while time.time() - start < timeout:
            try:
                code = self.get_code_once(email, keyword=keyword, exclude_codes=exclude_codes)
                if code:
                    self._log(f"找到验证码: {code}")
                    return code
            except Exception as e:
                self._log(f"轮询异常: {e}")
            time.sleep(interval)

        self._log(f"{timeout}s 超时未找到验证码")
        return None


def load_used_set(used_file: str = DEFAULT_USED_FILE) -> set:
    """从 used 文件加载已用邮箱集合（兼容新旧格式，供外部调用）"""
    if Path(used_file).exists():
        try:
            with open(used_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "records" in data and isinstance(data["records"], dict):
                return {e.lower() for e in data["records"]}
            if "used" in data and isinstance(data["used"], list):
                return {e.lower() for e in data["used"]}
        except Exception:
            pass
    return set()


# ---- CLI ----

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="微软 Outlook 号池管理工具")
    p.add_argument("--pool", default=DEFAULT_POOL_PATH, help="号池文件路径")
    p.add_argument("--helper-url", default=DEFAULT_HELPER_URL, help="Hotmail Helper 地址")
    p.add_argument("--command", default="stats", choices=["stats", "get", "wait", "mark-used", "mark-unused"])
    p.add_argument("--email", default="", help="指定邮箱")
    p.add_argument("--keyword", default="openai", help="验证码检索关键词")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    pool = MsOutlookPool(args.pool, args.helper_url, verbose=args.verbose)

    if args.command == "stats":
        s = pool.stats()
        print(f"总账号: {s['total']}  启用: {s['enabled']}  已用: {s['used']}  可用: {s['available']}")

    elif args.command == "get":
        email = pool.get_available_email()
        if email:
            print(f"可用邮箱: {email}")
        else:
            print("无可用邮箱")

    elif args.command == "wait":
        email = args.email or pool.get_available_email()
        if not email:
            print("无可用邮箱")
            exit(1)
        print(f"轮询 {email} 验证码...")
        code = pool.wait_for_code(email, keyword=args.keyword, timeout=120)
        if code:
            print(f"验证码: {code}")
        else:
            print("超时未获取到验证码")

    elif args.command == "mark-used":
        email = args.email
        if not email:
            print("请指定 --email")
            exit(1)
        pool.mark_used(email)
        print(f"已标记: {email}")

    elif args.command == "mark-unused":
        email = args.email
        if not email:
            print("请指定 --email")
            exit(1)
        pool.mark_unused(email)
        print(f"已取消标记: {email}")
