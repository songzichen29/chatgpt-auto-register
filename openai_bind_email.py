#!/usr/bin/env python3
"""
OpenAI 后半段 — 纯协议版（基于真实抓包端点）

真实流程:
  [1] POST /oauth/authorize          Cloudflare 挑战 → 302
  [2] POST sentinel/req              flow=authorize_continue → oai-sc
  [3] POST /api/accounts/authorize/continue  {"username":{"kind":"phone_number","value":"+56..."}}
  [4] POST sentinel/req              flow=password_verify
  [5] POST /api/accounts/password/verify     {"password":"xxx"}
  [6] POST /api/accounts/add-email/send      {"email":"alias@example.test"}
  [7] iCloud 收绑定验证码
  [8] POST /api/accounts/email-otp/validate  {"code":"796880"}
  [9] POST /api/accounts/workspace/select    {"workspace_id":"xxx"}
  [10] GET  /api/oauth/oauth2/auth?login_verifier=xxx  → 302 → code
  [11] code → token 交换 + SUB2API 上传
"""

import re
import os
import json
import time
import uuid
import datetime
import urllib3
from typing import Optional, Dict, Any, Tuple, Callable
from urllib.parse import urlparse, parse_qs, urljoin

from curl_cffi import requests as curl_requests

urllib3.disable_warnings()

AUTH = "https://auth.openai.com"
SENTINEL = "https://sentinel.openai.com/backend-api/sentinel/req"
CHATGPT = "https://chatgpt.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

JSON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": AUTH,
    "user-agent": UA,
    "sec-ch-ua": '"Google Chrome";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
}


def _log(msg: str):
    print(f"  [AUTH] {msg}")


MSOUTLOOK_UNREADABLE_ERROR = "msoutlook_unreadable"
MSOUTLOOK_FATAL_KEYWORDS = (
    "invalid_grant",
    "security interrupt",
    "account security interrupt",
    "compromised",
    "aadsts70000",
    "refresh_token_or_scope_invalid",
    "refresh token invalid",
    "refresh_token invalid",
    "刷新令牌无效",
)


def _is_msoutlook_fatal_error(error: object) -> bool:
    text = str(error or "").lower()
    return any(keyword in text for keyword in MSOUTLOOK_FATAL_KEYWORDS)


def _msoutlook_unreadable_error(error: object) -> str:
    return f"{MSOUTLOOK_UNREADABLE_ERROR}: {error}"


# ============================================================
# Sentinel PoW (简化版，内联用)
# ============================================================

class _Sentinel:
    """内联 Sentinel，避免额外依赖"""

    MAX_ATTEMPTS = 500000

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.sid = str(uuid.uuid4())
        self.user_agent = UA

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _config(self) -> list:
        import random
        perf = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime()),
            4294705152, random.random(), self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, "en-US", random.random(),
            random.choice(["plugins-undefined", "mimeTypes-undefined"]),
            random.choice(["location", "documentURI"]),
            random.choice(["Object", "parseFloat"]),
            perf, self.sid, "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf,
        ]

    def _b64(self, data) -> str:
        import base64
        return base64.b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode()

    def _requirements(self) -> str:
        d = self._config()
        d[3] = 1
        d[9] = 5
        return "gAAAAAC" + self._b64(d)

    def _pow(self, seed: str, difficulty: str) -> str:
        import random
        diff = str(difficulty or "0")
        t0 = time.time()
        for i in range(self.MAX_ATTEMPTS):
            d = self._config()
            d[3] = i
            d[9] = round((time.time() - t0) * 1000)
            p = self._b64(d)
            if self._fnv1a_32(seed + p)[:len(diff)] <= diff:
                return "gAAAAAB" + p + "~S"
        return "gAAAAAB" + "wQ8Lk5F" * 10 + self._b64(str(None))

    def get(self, session, flow: str) -> str:
        r = session.post(
            SENTINEL,
            data=json.dumps({"p": self._requirements(), "id": self.device_id, "flow": flow}),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://sentinel.openai.com",
                "User-Agent": self.user_agent,
            },
            verify=False, timeout=30,
        )
        if not r.ok:
            return ""
        data = r.json()
        token = str(data.get("token") or "")
        if not token:
            return ""
        pw = data.get("proofofwork") or {}
        if pw.get("required") and pw.get("seed"):
            p = self._pow(str(pw["seed"]), str(pw.get("difficulty", "0")))
        else:
            p = self._requirements()
        return json.dumps({"p": p, "t": "", "c": token, "id": self.device_id, "flow": flow})


# ============================================================
# 后半段引擎 (真实端点)
# ============================================================

class OAuthSecondHalf:
    """OpenAI OAuth 后半段 — 真实端点版"""

    def __init__(self, proxy: str = "", verbose: bool = True, device_id: str = ""):
        self.verbose = verbose
        self.device_id = device_id or str(uuid.uuid4())
        self._default_timeout = 30

        if proxy:
            import requests as r
            self.session = r.Session()
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.verify = False
            # Inject default timeout so proxy hangs don't block forever
            _orig = self.session.request
            def _req(method, url, **kw):
                kw.setdefault("timeout", self._default_timeout)
                return _orig(method, url, **kw)
            self.session.request = _req
        else:
            self.session = curl_requests.Session(impersonate="chrome", verify=False)

        self.sentinel = _Sentinel(self.device_id)
        self._sentinel_cache: Dict[str, str] = {}

    def _l(self, msg): 
        if self.verbose: _log(msg)

    def _sentinel_token(self, flow: str) -> str:
        if flow not in self._sentinel_cache:
            try:
                self._sentinel_cache[flow] = self.sentinel.get(self.session, flow)
            except Exception as e:
                self._l(f"Sentinel 跳过 ({flow}): {e}")
                self._sentinel_cache[flow] = ""
        return self._sentinel_cache[flow]

    # ---------- 解析 OAuth URL ----------

    @staticmethod
    def parse_oauth_url(oauth_url: str) -> Dict[str, str]:
        parsed = urlparse(oauth_url)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    # ---------- [1] 发起 OAuth + Cloudflare ----------

    def initiate_oauth(self, oauth_url: str):
        """
        ① GET oauth_url → 跟重定向到登录页
        """
        self._l("[1] 发起 OAuth (GET) ...")
        r = self.session.get(
            oauth_url,
            headers={
                **PAGE_HEADERS,
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
            },
            allow_redirects=True,
        )
        url = r.url
        html = r.text
        is_error = "/error" in url
        self._l(f"[1] 当前 URL: {url[:120]}")
        if is_error:
            self._l(f"[1] 重定向到了错误页!")
        return not is_error, url, html

    # ---------- [2] Sentinel authorize_continue ----------

    def sentinel_authorize(self) -> str:
        self._l("[2] Sentinel (authorize_continue) ...")
        return self._sentinel_token("authorize_continue")

    # ---------- [3] 提交手机号 ----------

    def submit_phone(self, phone: str) -> Dict:
        """
        [3] POST /api/accounts/authorize/continue
           {"username":{"kind":"phone_number","value":"+56947125968"}}
        返回: {continue_url, page:{type, payload}}
        设置: oai-client-auth-session cookie
        """
        self._l(f"[3] 提交手机号: {phone}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("authorize_continue")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/authorize/continue",
            json={"username": {"kind": "phone_number", "value": phone}},
            headers=h,
        )
        self._l(f"[3] 响应: {r.status_code}")
        return r.json() if r.ok else {"error": r.text}

    # ---------- [4] Sentinel password_verify ----------

    def sentinel_password(self) -> str:
        self._l("[4] Sentinel (password_verify) ...")
        return self._sentinel_token("password_verify")

    # ---------- [5] 验证密码 ----------

    def verify_password(self, password: str) -> Dict:
        """
        [5] POST /api/accounts/password/verify
           {"password":"xxx"}
        返回: {continue_url:"/add-email", page:{type:"add_email"}}
        """
        self._l("[5] 验证密码 ...")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/password/verify",
            json={"password": password},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[5] 响应: page={pt}")
        return data

    # ---------- [5.5] 联系方式验证 (contact_verification) ----------

    def resend_contact_otp(self) -> Dict:
        """
        在 contact_verification 页面，重新发送手机 OTP。
        POST /api/accounts/phone-otp/resend
        返回: {page:{type:"contact_verification"}} 或 error
        """
        self._l("[5.5] 重新发送手机 OTP ...")
        r = self.session.post(
            f"{AUTH}/api/accounts/phone-otp/resend",
            json={},
            headers=JSON_HEADERS,
            timeout=30,
        )
        if r.ok:
            self._l("[5.5] 重发成功 (200, 无body)")
            return {"ok": True}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}
        self._l(f"[5.5] 响应: {data}")
        return data

    def validate_contact_otp(self, code: str) -> Dict:
        """
        在 contact_verification 页面，验证手机 OTP。
        POST /api/accounts/phone-otp/validate
           {"code":"123456"}
        返回: {continue_url:"/add-email", page:{type:"add_email"}} 或 error
        """
        self._l(f"[5.5] 验证手机 OTP: {code}")
        r = self.session.post(
            f"{AUTH}/api/accounts/phone-otp/validate",
            json={"code": code},
            headers=JSON_HEADERS,
            timeout=30,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[5.5] 响应: page={pt}")
        return data

    # ---------- [6] 发送绑定邮箱 ----------

    def send_bind_email(self, email: str) -> Dict:
        """
        [6] POST /api/accounts/add-email/send
           {"email":"botch.sear_8w@icloud.com"}
        返回: {continue_url:"/email-verification", page:{type:"email_otp_verification"}}
        """
        self._l(f"[6] 发送绑定邮箱: {email}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/add-email/send",
            json={"email": email},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[6] 响应: page={pt}")
        return data

    # ---------- [7] 验证邮箱 OTP ----------

    def verify_email_otp(self, code: str) -> Dict:
        """
        [7] POST /api/accounts/email-otp/validate
           {"code":"796880"}
        返回: {continue_url:"/sign-in-with-chatgpt/codex/consent", page:{type:"consent"}}
        email 标记为 verified
        """
        self._l(f"[7] 验证邮箱 OTP: {code}")
        h = dict(JSON_HEADERS)
        r = self.session.post(
            f"{AUTH}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[7] 响应: page={pt}")
        return data

    # ---------- [8] 查询 session 状态 ----------

    def get_session_dump(self) -> Dict:
        """
        GET /api/accounts/client_auth_session_dump
        返回: {client_auth_session:{session_id, username, email, workspaces, ...}}
        """
        r = self.session.get(
            f"{AUTH}/api/accounts/client_auth_session_dump",
            headers=JSON_HEADERS,
        )
        return r.json() if r.ok else {}

    # ---------- [9] 选择工作区 ----------

    def select_workspace(self, workspace_id: str) -> Dict:
        """
        [9] POST /api/accounts/workspace/select
           {"workspace_id":"74461035-..."}
        返回: {continue_url:"...login_verifier...", page:{...}}
        """
        self._l(f"[9] 选择工作区: {workspace_id}")
        r = self.session.post(
            f"{AUTH}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=JSON_HEADERS,
        )
        data = r.json() if r.ok else {"error": r.text}
        return data

    # ---------- [10] 最终 OAuth → 获取 code ----------

    def follow_continue_until_code(self, continue_url: str, max_hops: int = 8) -> Optional[str]:
        """
        跟随 continue_url 链，直到捕获 redirect_uri 中的 code
        会自动处理 consent 页（获取 session_dump → 选 workspace → 再跟）
        """
        url = continue_url
        for hop in range(max_hops):
            self._l(f"[10] hop {hop+1}/{max_hops}: {url[:100]}...")
            r = self.session.get(
                url if url.startswith("http") else urljoin(AUTH, url),
                headers={**PAGE_HEADERS, "referer": AUTH, "sec-fetch-site": "same-origin"},
                allow_redirects=False,
            )
            location = r.headers.get("Location", "")
            ct = r.headers.get("content-type", "")
            self._l(f"[10]   -> {r.status_code} ct={ct[:30]} loc={location if location else 'none'}")

            # 检查 Location / URL 中的 code
            if location:
                parsed = urlparse(location)
                code = parse_qs(parsed.query).get("code", [None])[0]
                if code:
                    self._l(f"[10] code: {code[:30]}...")
                    return code
                url = location if location.startswith("http") else urljoin(AUTH, location)
                continue

            # 当前 URL 中的 code
            parsed = urlparse(r.url)
            code = parse_qs(parsed.query).get("code", [None])[0]
            if code:
                self._l(f"[10] code (url): {code[:30]}...")
                return code

            # HTML consent 页 → 需要选 workspace
            if "text/html" in ct and ("consent" in url.lower() or "consent" in r.text.lower()[:500]):
                self._l("[10] consent 页 → 选 workspace ...")
                dump = self.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    self._l(f"[10] workspace: {ws_id}")
                    ws_r = self.select_workspace(ws_id)
                    next_url = ws_r.get("continue_url", "")
                    if next_url:
                        url = next_url if next_url.startswith("http") else urljoin(AUTH, next_url)
                        continue
                # 回退：尝试从 HTML 提取 form
                action, fields = _extract_form(r.url, r.text)
                if action and fields:
                    self._l(f"[10] POST consent form: {action}")
                    fr = self._post_form(action, fields)
                    loc = fr.headers.get("Location", "")
                    if loc:
                        url = loc if loc.startswith("http") else urljoin(AUTH, loc)
                        continue

            # JSON → 提取 continue_url
            if "json" in ct:
                try:
                    data = r.json()
                    next_url = data.get("continue_url", "")
                    if next_url:
                        url = next_url if next_url.startswith("http") else urljoin(AUTH, next_url)
                        continue
                except Exception:
                    pass

            break

        return None

    def final_oauth(self, oauth_params: Dict[str, str]) -> Optional[str]:
        """
        [10] GET /api/oauth/oauth2/auth?client_id=...&login_verifier=...&...
           → 302 → redirect_uri?code=xxx&state=yyy
        返回: authorization code
        """
        self._l("[10] 最终 OAuth → 获取 code ...")

        # 构建完整参数
        params = dict(oauth_params)
        # 从 URL 拼接
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{AUTH}/api/oauth/oauth2/auth?{qs}"

        r = self.session.get(
            url,
            headers={**PAGE_HEADERS, "referer": AUTH},
            allow_redirects=False,
        )

        # 从 Location header 提取 code
        location = r.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            code = parse_qs(parsed.query).get("code", [None])[0]
            if code:
                self._l(f"[10] code: {code[:30]}...")
                return code

        # 跟随重定向后从 URL 提取
        r2 = self.session.get(
            url,
            headers={**PAGE_HEADERS, "referer": AUTH},
            allow_redirects=True,
        )
        parsed = urlparse(r2.url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if code:
            self._l(f"[10] code: {code[:30]}...")
            return code

        self._l("[10] 未捕获到 code")
        return None


# ============================================================
# 完整后半段入口
# ============================================================

def _poll_bind_code(icloud_email, icloud_cookies, imap_user, imap_password,
                    msoutlook_helper_url, msoutlook_email, verbose, timeout=240,
                    exclude_codes=None, msoutlook_helper_mode="http",
                    msoutlook_helper_script=""):
    """轮询获取绑定验证码，优先 msoutlook，回退 iCloud。

    exclude_codes: list[str] — 调用方在 send_bind_email 之前先取一次邮箱最新码
        作为 baseline，传到这里。MsOutlookPool.wait_for_code 会把这些码传给
        Hotmail Helper 的 excludeCodes 参数，跳过这些历史码，只返回 OpenAI
        本次新发的码。

        为什么需要：邮箱里可能残留上一次注册/绑定的 openai 邮件（旧码已失效），
        如果不过滤，wait_for_code 第一次轮询就会拿到旧码立即返回，
        verify_email_otp 必然 wrong_email_otp_code。
    """
    def _l(msg): _log(msg)

    # 优先尝试 msoutlook
    if (msoutlook_helper_url or str(msoutlook_helper_mode or "").strip().lower() == "direct") and msoutlook_email:
        try:
            from msoutlook_pool import MsOutlookPool
            _l(f"[7] MsOutlook 收验证码 ({msoutlook_email}) ...")
            pool = MsOutlookPool(
                helper_url=msoutlook_helper_url,
                verbose=verbose,
                helper_mode=msoutlook_helper_mode,
                helper_script=msoutlook_helper_script,
            )

            code = pool.wait_for_code(
                msoutlook_email, keyword="openai", timeout=timeout,
                exclude_codes=exclude_codes,
            )
            if code:
                _l(f"[7] 验证码: {code}")
                return code
            _l("[7] MsOutlook 超时，尝试回退到 iCloud")
        except Exception as e:
            if _is_msoutlook_fatal_error(e):
                raise RuntimeError(_msoutlook_unreadable_error(e)) from e
            _l(f"[7] MsOutlook 失败: {e}，回退到 iCloud")

    # 回退到 iCloud
    _l("[7] iCloud 收验证码 ...")
    from icloud_hme import ICloudHME
    icloud = ICloudHME(icloud_cookies or {}, verbose=verbose)
    code = icloud.poll_mail_for_code(
        target_email=icloud_email,
        sender_filters=["openai", "noreply", "verification", "no-reply"],
        timeout=timeout,
        imap_user=imap_user,
        imap_password=imap_password,
    )
    return code


def _get_email_history_codes(msoutlook_helper_url: str, msoutlook_email: str,
                             verbose: bool = False, msoutlook_helper_mode: str = "http",
                             msoutlook_helper_script: str = "") -> list:
    """在 send_bind_email 之前获取邮箱里所有历史 OpenAI 验证码列表。

    为什么需要全部历史码（而不只是最新一封）：
        Hotmail Helper /code 端点的 excludeCodes 只跳过列表里的码，
        但 /code 返回的不一定是"最新一封"——可能按邮件 ID 或其他顺序返回。
        所以只 exclude 最新一封，第二次轮询仍可能拿到更早的历史码。
        把所有历史码都 exclude，才能保证 wait_for_code 拿到的是 OpenAI 本次新发的码。

    为什么必须 send_bind_email 之前取：
        那时邮箱里只有历史码，列出来的一定全是旧码。
        send_bind_email 之后 OpenAI 会发新码，那时再取就会把真码也加进 exclude，
        导致 wait_for_code 永远拿不到新码。

    返回：去重后的历史码列表（字符串）。失败返回空列表。
    """
    if (not msoutlook_helper_url and str(msoutlook_helper_mode or "").strip().lower() != "direct") or not msoutlook_email:
        return []
    try:
        from msoutlook_pool import MsOutlookPool
        import re
        pool = MsOutlookPool(
            helper_url=msoutlook_helper_url,
            verbose=verbose,
            helper_mode=msoutlook_helper_mode,
            helper_script=msoutlook_helper_script,
        )
        # 拉取最近 30 封邮件，提取所有 OpenAI 验证码
        result = pool.get_messages(msoutlook_email, top=30)
        codes = []
        for msg in (result.get("messages") or []):
            text = (msg.get("bodyPreview", "") or "") + " " + (msg.get("subject", "") or "")
            # OpenAI 验证码是 6 位数字
            for m in re.finditer(r"\b(\d{6})\b", text):
                codes.append(m.group(1))
        # 去重保序
        seen = set()
        uniq = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq
    except Exception as e:
        if _is_msoutlook_fatal_error(e):
            raise RuntimeError(_msoutlook_unreadable_error(e)) from e
        return []


def run_second_half(
    oauth_url: str,
    phone: str,
    password: str,
    icloud_email: str,
    icloud_cookies: Dict[str, str],
    sub2api_url: str = "",
    sub2api_email: str = "",
    sub2api_password: str = "",
    sub2api_proxy_id: int = 0,
    proxy: str = "",
    verbose: bool = True,
    bind_code: str = "",
    imap_user: str = "",
    imap_password: str = "",
    sub2api_session_id: str = "",
    sub2api_state: str = "",
    msoutlook_helper_url: str = "",
    msoutlook_email: str = "",
    msoutlook_helper_mode: str = "http",
    msoutlook_helper_script: str = "",
    save_import: bool = True,
    interactive_input: bool = True,
    sms_obj=None,
    phone_aid: str = "",
) -> Dict:
    """
    完整后半段 (基于真实端点):

    [1] POST /oauth/authorize             发起OAuth
    [2] sentinel/req (authorize_continue) 安全检测
    [3] /api/accounts/authorize/continue  提交手机号
    [4] sentinel/req (password_verify)    刷新安全token
    [5] /api/accounts/password/verify     验证密码
    [6] /api/accounts/add-email/send      绑定iCloud邮箱
    [7] iCloud收验证码
    [8] /api/accounts/email-otp/validate  验证邮箱OTP
    [9] /api/accounts/workspace/select    选择工作区
    [10] /api/oauth/oauth2/auth            → code
    [11] code→token + SUB2API上传
    """

    def log(msg):
        if verbose: _log(msg)

    def _wait_contact_code_via_provider(timeout: int = 120, exclude_codes=None) -> str:
        excluded = {str(c).strip() for c in (exclude_codes or []) if str(c).strip()}
        if not (sms_obj and phone_aid):
            return ""

        def _call_provider(params: dict, per_timeout: float = 12.0) -> str:
            client = getattr(sms_obj, "client", None)
            if client is not None and hasattr(client, "_call"):
                try:
                    return str(client._call(params, timeout=per_timeout, retries=1) or "").strip()
                except TypeError:
                    return str(client._call(params) or "").strip()
            return ""

        def _get_status() -> str:
            try:
                status = _call_provider({"action": "getStatus", "id": phone_aid}, per_timeout=10.0)
                if status:
                    return status
            except Exception:
                pass
            try:
                return str(sms_obj.client.get_status(phone_aid, timeout=10, retries=1) or "").strip()
            except Exception:
                return ""

        def _extract_code(status: str) -> str:
            text = str(status or "").strip()
            if text.startswith(("STATUS_OK:", "STATUS_WAIT_RETRY:")):
                code = text.split(":", 1)[1].strip()
                if code and code not in excluded:
                    return code
            return ""

        deadline = time.time() + max(0, int(timeout))
        list_actions = (
            "getActiveActivations",
            "getActiveActivationsV2",
            "getActivations",
            "getCurrentActivations",
        )
        while time.time() < deadline:
            status = _get_status()
            code = _extract_code(status)
            if code:
                return code
            if status:
                log(f"[5.5] SMS 状态: {status[:80]}")

            # HeroSMS 页面/兼容接口有时会在列表 JSON 的 otpList 里带最新短信；
            # getStatus 对已收过码的订单可能固定返回旧码，所以这里补一层读取。
            for action in list_actions:
                try:
                    raw = _call_provider({"action": action}, per_timeout=12.0)
                    if not raw or not raw.lstrip().startswith(("[", "{")):
                        continue
                    data = json.loads(raw)
                    rows = data if isinstance(data, list) else (
                        data.get("data") or data.get("items") or data.get("activations") or []
                    )
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if str(row.get("id") or row.get("activationId") or "") != str(phone_aid):
                            continue
                        otp_list = row.get("otpList") or []
                        if not isinstance(otp_list, list):
                            otp_list = []
                        candidates = []
                        for otp in otp_list:
                            if isinstance(otp, dict):
                                candidates.append(otp.get("smsCode") or otp.get("code") or "")
                        candidates.append(row.get("smsCode") or "")
                        for candidate in reversed([str(x).strip() for x in candidates if str(x).strip()]):
                            if candidate and candidate not in excluded:
                                return candidate
                except Exception:
                    continue
            time.sleep(3)
        return ""

    flow = OAuthSecondHalf(proxy=proxy, verbose=verbose)

    try:
        # 解析 OAuth URL 参数
        oauth_params = OAuthSecondHalf.parse_oauth_url(oauth_url)
        log(f"OAuth params: client_id={oauth_params.get('client_id','?')[:20]}...")

        # ---- [1] 发起 OAuth ----
        log("=" * 40)
        ok, current_url, html = flow.initiate_oauth(oauth_url)
        if not ok:
            log(f"[1] OAuth 发起失败, URL: {current_url[:120]}")
            return {"ok": False, "error": f"initiate_oauth failed: {current_url[:120]}"}

        # ---- [2] Sentinel ----
        flow.sentinel_authorize()

        # ---- [3] 提交手机号 ----
        log("[3] 提交手机号 ...")
        r = flow.submit_phone(phone)
        if r.get("error"):
            log(f"[3] 失败: {r.get('error')}")
            return {"ok": False, "error": f"submit_phone: {r.get('error')}"}
        log(f"[3] page: {(r.get('page') or {}).get('type', '?')}")

        # ---- [4] Sentinel ----
        flow.sentinel_password()

        # ---- [5] 验证密码 ----
        log("[5] 验证密码 ...")
        r = flow.verify_password(password)
        if r.get("error"):
            log(f"[5] 失败: {r.get('error')}")
            return {"ok": False, "error": f"verify_password: {r.get('error')}"}
        page_type = (r.get("page") or {}).get("type", "")
        log(f"[5] page: {page_type}")
        log(
            f"[5] page flags: about_you={'about_you' in page_type}, "
            f"consent={'consent' in page_type}, contact_verification={'contact_verification' in page_type}"
        )
        code = None

        def _capture_code(continue_url: str, stage: str) -> Optional[str]:
            captured = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not captured:
                captured = flow.final_oauth(oauth_params)
            if not captured:
                log(f"[10] {stage}: 未捕获到 code (url={continue_url[:80] if continue_url else 'empty'})")
            return captured

        def _submit_about_you_once() -> Dict[str, Any]:
            h = {
                "accept": "application/json",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": AUTH,
                "user-agent": UA,
                "sec-ch-ua": '"Google Chrome";v="145"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer": f"{AUTH}/about-you",
                "oai-device-id": flow.device_id,
            }
            st = flow._sentinel_token("oauth_create_account")
            if st:
                h["OpenAI-Sentinel-Token"] = st
            log(f"[5] create_account headers: oai-device-id={flow.device_id[:20]}..., sentinel={'yes' if st else 'no'}")
            create_payload = {"name": "A", "birthdate": "2000-01-01"}
            bind_email = (msoutlook_email or icloud_email or "").strip()
            if bind_email:
                # 某些半注册账号在 Codex OAuth 的 about_you 阶段会要求先具备 email。
                # 既然 Phase2 调用方已经分配了待绑定邮箱，就不要先撞 missing_email
                # 再绕 ChatGPT client fallback；直接在 create_account 里携带 email。
                create_payload["email"] = bind_email
            log(f"[5] create_account payload fields: {','.join(create_payload.keys())}")
            resp = flow.session.post(
                f"{AUTH}/api/accounts/create_account",
                json=create_payload,
                headers=h,
                allow_redirects=False,
            )
            log(f"[5] create_account status: {resp.status_code}")
            log(f"[5] create_account body: {resp.text[:500] if resp.text else 'empty'}")
            ct = resp.headers.get("content-type", "")
            if ct.startswith("application/json"):
                try:
                    data = resp.json()
                except Exception:
                    data = {}
            else:
                data = {}
            data.setdefault("_status", resp.status_code)
            data.setdefault("_body", resp.text[:500] if resp.text else "")
            continue_url = data.get("continue_url", "")
            location = resp.headers.get("Location", "")
            if location:
                continue_url = location if location.startswith("http") else f"{AUTH}{location}"
            next_page = (data.get("page") or {}).get("type", "")
            err = data.get("error") or {}
            err_code = err.get("code", "") if isinstance(err, dict) else ""
            log(f"[5] create_account page: {next_page}")
            log(f"[5] continue_url: {continue_url[:100] if continue_url else 'empty'}")
            return {
                "ok": not bool(err_code),
                "status": resp.status_code,
                "page_type": next_page,
                "continue_url": continue_url,
                "error": err,
                "error_code": err_code,
            }

        def _handle_about_you(stage: str) -> Dict[str, Any]:
            about_r = _submit_about_you_once()
            next_page = about_r.get("page_type", "")
            continue_url = about_r.get("continue_url", "")
            err_code = about_r.get("error_code", "")
            if err_code == "missing_email":
                log(f"[5] {stage}: create_account missing_email; retry with ChatGPT client about_you before Codex OAuth")
                return {
                    "ok": False,
                    "error": f"codex_about_you_missing_email: {stage}",
                    "retry_after_chat_about_you": True,
                }
            if err_code:
                return {"ok": False, "error": f"{stage}: create_account: {about_r.get('error')}"}

            if "add_email" in next_page or "email_otp" in next_page:
                return {"ok": True, "page_type": next_page, "continue_url": continue_url}

            captured = _capture_code(continue_url, stage)
            if captured:
                return {"ok": True, "code": captured, "page_type": next_page, "continue_url": continue_url}
            return {
                "ok": False,
                "error": f"no authorization code after {stage} (status={about_r.get('status')}, page={next_page}, url={continue_url[:80] if continue_url else 'empty'})",
            }

        # 分支判断
        if "about_you" in page_type:
            # 新号没填资料 → 先填资料
            log("[5] about_you 页, 先填资料 ...")
            handled = _handle_about_you("about_you")
            if not handled.get("ok"):
                return {"ok": False, "error": handled.get("error", "about_you failed")}
            if handled.get("code"):
                code = handled["code"]
                page_type = "code_captured"
            else:
                page_type = handled.get("page_type", "")
                continue_url = handled.get("continue_url", "")

        if code:
            pass
        elif "consent" in page_type:
            # 已到同意页 → 选工作区 → 拿 code
            log("[5] 已到 consent 页，跳过绑邮箱")
            dump = flow.get_session_dump()
            workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
            if workspaces:
                ws_id = workspaces[0].get("id", "")
                log(f"[9] 工作区: {ws_id}")
                ws_r = flow.select_workspace(ws_id)
                log(f"[9] page: {(ws_r.get('page') or {}).get('type', '?')}")
                continue_url = ws_r.get("continue_url", "")
            else:
                continue_url = ""
            code = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        elif "contact_verification" in page_type:
            # 已有账号，密码验证后需要验证手机号 OTP
            log("[5] contact_verification，需要验证手机 OTP ...")
            if not (bind_code or (sms_obj and phone_aid) or interactive_input):
                # batch_phase2 这类“已注册账号续跑 Phase 2”场景没有 SMS activation_id，
                # 也不应该为了续跑主动重发短信或阻塞 input。
                # worker_pool 刚跑完 Phase 1，持有 activation_id，会通过 sms_obj/phone_aid
                # 进入下面的自动收码流程。
                # 这里直接返回账号状态异常，让外层跳过/记录，不触碰邮箱池。
                return {
                    "ok": False,
                    "error": "account_requires_contact_verification",
                }
            old_contact_codes = set()
            if sms_obj and phone_aid:
                try:
                    get_status = getattr(getattr(sms_obj, "client", None), "get_status", None)
                    if callable(get_status):
                        current_sms_status = str(get_status(phone_aid, timeout=10, retries=1) or "")
                        if current_sms_status.startswith(("STATUS_OK:", "STATUS_WAIT_RETRY:")):
                            old_code = current_sms_status.split(":", 1)[1].strip()
                            if old_code:
                                old_contact_codes.add(old_code)
                                log(f"[5.5] SMS 平台已有旧验证码，将忽略: {old_code}")
                except Exception as e:
                    log(f"[5.5] 查询 SMS 旧验证码失败，继续请求重发: {e}")
                try:
                    # hero-sms/SmsBower 需要先把订单置为“等待下一条短信”，否则
                    # getStatus 会一直返回旧 STATUS_OK/STATUS_WAIT_RETRY 验证码。
                    resend_result = str(sms_obj.resend(phone_aid) or "")
                    if resend_result and "ACCESS_RETRY_GET" not in resend_result:
                        log(f"[5.5] SMS 平台拒绝接收下一条短信: {resend_result[:120]}")
                    else:
                        log("[5.5] SMS 平台已请求接收下一条短信")
                except Exception as e:
                    log(f"[5.5] SMS 平台请求重发失败，继续尝试 OpenAI 重发: {e}")
            code_contact = str(bind_code or "").strip()
            contact_attempts = 2 if (sms_obj and phone_aid and not bind_code) else 1
            r = {}
            for contact_attempt in range(contact_attempts):
                if contact_attempt > 0:
                    old_contact_codes.add(str(code_contact or "").strip())
                    code_contact = ""
                    log(f"[5.5] 手机 OTP 重试 {contact_attempt + 1}/{contact_attempts} ...")
                    try:
                        resend_result = str(sms_obj.resend(phone_aid) or "")
                        if resend_result and "ACCESS_RETRY_GET" not in resend_result:
                            log(f"[5.5] SMS 平台拒绝接收下一条短信: {resend_result[:120]}")
                        else:
                            log("[5.5] SMS 平台已请求接收下一条短信")
                    except Exception as e:
                        log(f"[5.5] SMS 平台请求重发失败，继续尝试 OpenAI 重发: {e}")

                # 请求重发短信 OTP
                r_send = flow.resend_contact_otp()
                if r_send.get("error"):
                    log(f"[5.5] 重发失败: {r_send.get('error')}")
                    return {"ok": False, "error": f"resend_contact_otp: {r_send.get('error')}"}
                log("[5.5] 已请求重发短信，等待验证码 ...")
                # 从 SMS 平台收码
                if sms_obj and phone_aid and not bind_code:
                    try:
                        log("[5.5] 从 SMS 平台等待验证码 ...")
                        code_contact = _wait_contact_code_via_provider(
                            timeout=120,
                            exclude_codes=list(old_contact_codes),
                        )
                    except Exception as e:
                        log(f"[5.5] SMS 平台收码失败: {e}")
                if not code_contact and interactive_input:
                    code_contact = input("  [?] 输入手机验证码 (6位): ").strip()
                if not code_contact:
                    return {"ok": False, "error": "contact_verification code timeout"}
                log(f"[5.5] 收到验证码: {code_contact}")
                # 验证手机 OTP
                r = flow.validate_contact_otp(code_contact)
                if not r.get("error"):
                    break
                last_contact_error = str(r.get("error"))
                log(f"[5.5] 验证失败: {last_contact_error}")
                if "invalid_input" not in last_contact_error.lower() or contact_attempt >= contact_attempts - 1:
                    return {"ok": False, "error": f"validate_contact_otp: {r.get('error')}"}
            page_type = (r.get("page") or {}).get("type", "")
            log(f"[5.5] 验证后 page: {page_type}")
            # 继续后面的流程
            if "about_you" in page_type:
                log("[5.5] 验证后到 about_you，继续补资料 ...")
                handled = _handle_about_you("contact_verification_about_you")
                if not handled.get("ok"):
                    return {"ok": False, "error": handled.get("error", "about_you after contact_verification failed")}
                if handled.get("code"):
                    code = handled["code"]
                    page_type = "code_captured"
                else:
                    page_type = handled.get("page_type", "")
                    continue_url = handled.get("continue_url", "")
            if code:
                pass
            elif "consent" in page_type:
                log("[5.5] 已到 consent 页，跳过绑邮箱")
                dump = flow.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    log(f"[9] 工作区: {ws_id}")
                    ws_r = flow.select_workspace(ws_id)
                    continue_url = ws_r.get("continue_url", "")
                else:
                    continue_url = ""
                code = flow.follow_continue_until_code(continue_url) if continue_url else None
                if not code:
                    code = flow.final_oauth(oauth_params)
                if not code:
                    return {"ok": False, "error": "no authorization code after contact_verification"}

        elif code:
            pass

        elif "email_otp_verification" in page_type:
            # email_otp_verification 说明账号已经在待邮箱验证码状态。
            # 这个状态下下一步应是读取邮箱验证码并调用 email-otp/validate；
            # 再调用 add-email/send 会被服务端判定为 invalid_auth_step。
            log("[5] email_otp_verification，跳过 add-email/send，直接获取验证码 ...")

            code_bind = bind_code
            if not code_bind:
                code_bind = _poll_bind_code(
                    icloud_email, icloud_cookies, imap_user, imap_password,
                    msoutlook_helper_url, msoutlook_email, verbose, timeout=240,
                    exclude_codes=None,
                    msoutlook_helper_mode=msoutlook_helper_mode,
                    msoutlook_helper_script=msoutlook_helper_script,
                )
                if not code_bind:
                    print(f"\n  [!] 自动轮询超时, 目标邮箱: {icloud_email or msoutlook_email}")
                    if interactive_input:
                        code_bind = input("  [?] 输入6位验证码: ").strip()
            if not code_bind:
                return {"ok": False, "error": "binding code timeout"}
            log(f"[7] 验证码: {code_bind}")

            r = flow.verify_email_otp(code_bind)
            if r.get("error"):
                log(f"[8] 失败: {r.get('error')}")
                return {"ok": False, "error": f"verify_email_otp: {r.get('error')}"}
            verify_page_type = (r.get("page") or {}).get("type", "")
            log(f"[8] page: {verify_page_type or '?'}")
            continue_url = r.get("continue_url", "")

            if "about_you" in verify_page_type:
                log("[8] 邮箱验证后到 about_you，继续补资料 ...")
                handled = _handle_about_you("email_verified_about_you")
                if not handled.get("ok"):
                    return {"ok": False, "error": handled.get("error", "about_you after email verification failed")}
                if handled.get("code"):
                    code = handled["code"]
                    continue_url = ""
                else:
                    verify_page_type = handled.get("page_type", "")
                    continue_url = handled.get("continue_url", "")

            if not code and not continue_url:
                dump = flow.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    ws_r = flow.select_workspace(ws_id)
                    continue_url = ws_r.get("continue_url", "")

            code = code or (flow.follow_continue_until_code(continue_url) if continue_url else None)
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        else:
            # 需要绑定新邮箱 (add_email)
            log(f"[6] 绑定邮箱: {icloud_email} ...")
            # 在 send_bind_email 之前取邮箱所有历史码，作为 exclude 列表
            try:
                history_codes = _get_email_history_codes(
                    msoutlook_helper_url, msoutlook_email, verbose,
                    msoutlook_helper_mode=msoutlook_helper_mode,
                    msoutlook_helper_script=msoutlook_helper_script,
                )
            except RuntimeError as e:
                log(f"[7] 邮箱预检失败，停止发送绑定邮件: {e}")
                return {"ok": False, "error": str(e)}
            if history_codes:
                log(f"[7] 邮箱历史码 (将排除): {history_codes}")
            r = flow.send_bind_email(icloud_email)
            if r.get("error"):
                log(f"[6] 失败: {r.get('error')}")
                return {"ok": False, "error": f"send_bind_email: {r.get('error')}"}
            log(f"[6] page: {(r.get('page') or {}).get('type', '?')}")

            # ---- [7] 收验证码 (优先 msoutlook, 回退 iCloud) ----
            if bind_code:
                code_bind = bind_code
                log(f"[7] 使用手动验证码: {code_bind}")
            else:
                code_bind = _poll_bind_code(
                    icloud_email, icloud_cookies, imap_user, imap_password,
                    msoutlook_helper_url, msoutlook_email, verbose, timeout=240,
                    exclude_codes=history_codes or None,
                    msoutlook_helper_mode=msoutlook_helper_mode,
                    msoutlook_helper_script=msoutlook_helper_script,
                )
                if not code_bind:
                    print(f"\n  [!] 自动轮询超时, 目标邮箱: {icloud_email or msoutlook_email}")
                    if interactive_input:
                        code_bind = input("  [?] 输入6位验证码: ").strip()
                if not code_bind:
                    return {"ok": False, "error": "binding code timeout"}
            log(f"[7] 绑定验证码: {code_bind}")

            # ---- [8] 验证 + workspace + 取 code ----
            r = flow.verify_email_otp(code_bind)
            if r.get("error"):
                log(f"[8] 失败: {r.get('error')}")
                return {"ok": False, "error": f"verify_email_otp: {r.get('error')}"}
            log(f"[8] page: {(r.get('page') or {}).get('type', '?')}")
            continue_url = r.get("continue_url", "")

            if not continue_url:
                dump = flow.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    ws_r = flow.select_workspace(ws_id)
                    continue_url = ws_r.get("continue_url", "")

            code = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        log(f"[10] code 获取成功: {code[:30]}...")

        # ---- [11] code → exchange-code → SUB2API 账号 ----
        if sub2api_url and sub2api_email and sub2api_session_id:
            log("[11] SUB2API exchange-code ...")
            import requests as req_lib, time as _time

            # SUB2API 请求走代理（如果配置了 proxy）
            _sub_kwargs = {}
            if proxy:
                _sub_kwargs["proxies"] = {"http": proxy, "https": proxy}
                _sub_kwargs["verify"] = False

            resp = req_lib.post(
                f"{sub2api_url}/api/v1/auth/login",
                json={"email": sub2api_email, "password": sub2api_password},
                timeout=30,
                **_sub_kwargs,
            )
            d = resp.json()
            if d.get("code") != 0:
                log(f"[11] SUB2API 登录失败: {d}")
                return {"ok": False, "error": f"SUB2API login failed: {d}"}
            admin_token = d["data"]["access_token"]

            # 用 exchange-code 换 token (带重试)
            exchange_data = None
            retryable_status = {429, 500, 502, 503, 504}
            for attempt in range(3):
                log(f"[11] exchange-code 尝试 {attempt+1}/3 ...")
                try:
                    r = req_lib.post(
                        f"{sub2api_url}/api/v1/admin/openai/exchange-code",
                        json={
                            "session_id": sub2api_session_id,
                            "code": code,
                            "state": sub2api_state,
                        },
                        headers={"Authorization": f"Bearer {admin_token}"},
                        timeout=300,
                        **_sub_kwargs,
                    )
                except req_lib.exceptions.RequestException as e:
                    log(f"[11] 请求异常: {e}")
                    if attempt < 2:
                        log(f"[11] {attempt+1}s 后重试...")
                        _time.sleep(attempt + 1)
                        continue
                    return {"ok": False, "error": f"exchange-code request failed: {e}"}

                log(f"[11] response: {r.status_code}")
                if r.status_code == 200:
                    try:
                        exchange_data = r.json()
                    except Exception:
                        exchange_data = r.json()
                    break
                elif r.status_code in retryable_status:
                    log(f"[11] exchange-code 可重试失败: {r.status_code} {r.text[:200]}")
                    if attempt < 2:
                        log(f"[11] {attempt+1}s 后重试...")
                        _time.sleep(attempt + 1)
                        continue
                    return {"ok": False, "error": f"exchange-code retryable status after 3 retries: {r.status_code}"}
                else:
                    log(f"[11] exchange-code 失败: {r.status_code} {r.text[:200]}")
                    return {"ok": False, "error": f"exchange-code: {r.status_code}"}

            if not exchange_data:
                return {"ok": False, "error": "exchange-code failed after 3 retries"}

            # 用 exchange-code 返回的 credentials 生成 SUB2API 导入 JSON
            creds = exchange_data.get("data", exchange_data)
            email_from_creds = creds.get("email", "") or icloud_email

            # 构建符合 SUB2API 导入文件格式（直接对象，不带 data 包裹）
            import_payload = {
                "type": "sub2api-data",
                "version": 1,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "proxies": [],
                "accounts": [
                    {
                        "name": email_from_creds,
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "access_token": creds.get("access_token", ""),
                            "refresh_token": creds.get("refresh_token", ""),
                            "expires_at": creds.get("expires_at", 0),
                            "email": email_from_creds,
                            "play_type": "free",
                        },
                        "priority": 1,
                        "concurrency": 10,
                        "auto_pause_on_expired": True,
                    }
                ],
            }

            if save_import:
                # 保存到统一导入文件（追加模式）
                import datetime
                ts = datetime.datetime.now().strftime("%Y%m%d")
                filename = f"import_{ts}.json"
                filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imports", filename)
                os.makedirs(os.path.dirname(filepath), exist_ok=True)

                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    # 兼容两种格式：带 data 包裹 / 直接对象
                    if "data" in existing and isinstance(existing["data"], dict):
                        existing["data"]["accounts"].append(import_payload["accounts"][0])
                    else:
                        existing["accounts"].append(import_payload["accounts"][0])
                    payload_to_save = existing
                else:
                    payload_to_save = import_payload

                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(payload_to_save, f, indent=2, ensure_ascii=False)

                acc_count = len(payload_to_save.get("data", payload_to_save).get("accounts", []))
                log(f"[11] 已追加到导入文件: {filepath} (当前共 {acc_count} 个账号)")
                return {"ok": True, "code": code, "sub2api_account_id": "", "import_file": filepath, "import_data": import_payload}

            log("[11] save_import=False，跳过内部导入文件写入")
            return {"ok": True, "code": code, "sub2api_account_id": "", "import_file": "", "import_data": import_payload}

        log("[11] 无 SUB2API 配置, 仅返回 code")
        return {"ok": True, "code": code}

    except Exception as e:
        log(f"异常: {e}")
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print("OpenAI 后半段 — 真实端点版")
    print()
    print("流程: OAuth → sentinel → 手机号 → 密码 → 绑邮箱 → OTP验证 → workspace → code")
    print()
    print("使用: from openai_bind_email import run_second_half")
