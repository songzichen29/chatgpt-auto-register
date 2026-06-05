"""
ChatGPT phone registration protocol engine.
Uses curl_cffi for TLS fingerprint to bypass Cloudflare,
and Sentinel PoW to bypass JS anti-bot challenges.

Based on reverse engineering via Anything Analyzer and open-reg-auto.
"""

import json
import uuid
from typing import Any
from urllib.parse import urlencode

import urllib3
from curl_cffi.const import CurlHttpVersion
from curl_cffi import requests as curl_requests

from sentinel import Sentinel

urllib3.disable_warnings()

CHATGPT = "https://chatgpt.com"
AUTH = "https://auth.openai.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# Headers for JSON API calls
COMMON_HEADERS = {
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
}

# Headers for page navigation
NAVIGATE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
    "sec-ch-ua": '"Google Chrome";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
}


class ChatGPTRegister:
    """ChatGPT phone registration protocol engine."""

    def __init__(self, proxy: str = "", verbose: bool = True):
        self.verbose = verbose
        self.proxy = proxy
        if proxy:
            # 走代理时全部用 requests（代理模式下 Cloudflare 通常不拦截）
            import requests as _req
            self.session = _req.Session()
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.verify = False
        else:
            # 直连时用 curl_cffi 保持 TLS 指纹
            self.session = curl_requests.Session(impersonate="chrome", verify=False)

        self.device_id = str(uuid.uuid4())
        self.sentinel = Sentinel(self.device_id)
        self._sentinel_cache: dict[str, dict] = {}

    def _sentinel(self, flow: str) -> dict:
        if flow not in self._sentinel_cache:
            try:
                self._sentinel_cache[flow] = self.sentinel.get(self.session, flow)
            except Exception:
                self._sentinel_cache[flow] = {"token": "", "so_token": ""}
        return self._sentinel_cache[flow]

    def _add_sentinel_headers(self, headers: dict, flow: str):
        """给 headers 添加 Sentinel-Token 和 Sentinel-SO-Token"""
        st = self._sentinel(flow)
        token = st.get("token", "")
        so_token = st.get("so_token", "")
        if token:
            headers["OpenAI-Sentinel-Token"] = token
        if so_token:
            headers["OpenAI-Sentinel-SO-Token"] = so_token

    def _log(self, step: int, msg: str):
        if self.verbose:
            print(f"  [{step:02d}] {msg}")

    # ---- Step 1: 访问 chatgpt.com ----
    def visit(self):
        self._log(1, "访问 chatgpt.com ...")
        self.session.get(
            f"{CHATGPT}/auth/login",
            headers=NAVIGATE_HEADERS,
            allow_redirects=True,
            timeout=30,
        )

    # ---- Step 2: 获取 CSRF token ----
    def get_csrf(self) -> str:
        self._log(2, "GET /api/auth/csrf ...")
        r = self.session.get(
            f"{CHATGPT}/api/auth/csrf",
            headers=COMMON_HEADERS,
            timeout=30,
        )
        try:
            csrf = r.json().get("csrfToken")
        except Exception:
            csrf = None
        if not csrf:
            raise RuntimeError("CSRF token 获取失败 (可能被 Cloudflare 拦截)")
        return csrf

    # ---- Step 3: 发起手机登录 ----
    def signin(self, phone: str, csrf: str) -> str:
        self._log(3, "POST /api/auth/signin/openai ...")
        encoded = phone.replace("+", "%2B")
        params = {
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "login_hint": encoded,
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            r = self.session.post(
                f"{CHATGPT}/api/auth/signin/openai?{qs}",
                data={"callbackUrl": "/", "csrfToken": csrf, "json": "true"},
                headers={
                    **COMMON_HEADERS,
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": CHATGPT,
                    "referer": f"{CHATGPT}/auth/login",
                },
                allow_redirects=False,
                timeout=30,
            )
            return r.json().get("url", "")
        except Exception:
            return ""

    # ---- Step 4: 跟随 OAuth 跳转到 auth.openai.com ----
    def jump_to_auth(self, redirect_url: str) -> str:
        self._log(4, "跳转 auth.openai.com ...")
        r = self.session.get(
            redirect_url,
            headers={**NAVIGATE_HEADERS, "referer": CHATGPT, "sec-fetch-site": "cross-site"},
            allow_redirects=False,
            timeout=30,
        )
        location = r.headers.get("Location", "")
        if location:
            # 处理相对路径
            url = location if location.startswith("http") else f"{AUTH}{location}"
            self.session.get(
                url,
                headers={**NAVIGATE_HEADERS, "referer": AUTH, "sec-fetch-site": "same-origin"},
                allow_redirects=True,
                timeout=30,
            )
        return location

    # ---- Step 5: 手机号 + 密码注册 ----
    def register_user(self, phone: str, password: str) -> dict:
        self._log(5, "POST /api/accounts/user/register ...")
        headers = {
            **COMMON_HEADERS,
            "referer": f"{AUTH}/create-account/password",
            "oai-device-id": self.device_id,
        }
        try:
            self._add_sentinel_headers(headers, "username_password_create")
        except Exception:
            pass

        r = None
        try:
            r = self.session.post(
                f"{AUTH}/api/accounts/user/register",
                json={"username": phone, "password": password},
                headers=headers,
                timeout=30,
            )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            data = {}
        data["_status"] = r.status_code if r is not None else 0
        return data

    # ---- Step 6: 发送手机验证码 ----
    def send_otp(self, continue_url: str):
        self._log(6, "GET /api/accounts/phone-otp/send ...")
        self.session.get(
            continue_url,
            headers={**NAVIGATE_HEADERS, "referer": f"{AUTH}/create-account/password"},
            allow_redirects=True,
            timeout=30,
        )

    def _rebuild_session(self):
        """重建 session，迁移 Cookie 和配置，解决 curl 55 错误"""
        # 保存旧 Cookie（含域、路径、安全属性）
        old_cookies = []
        try:
            for cookie in self.session.cookies:
                old_cookies.append((
                    cookie.name, cookie.value,
                    cookie.domain, cookie.path,
                    cookie.secure, cookie.expires,
                ))
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass
        if self.proxy:
            self.session = __import__("requests").Session()
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
            self.session.verify = False
        else:
            self.session = curl_requests.Session(impersonate="chrome", verify=False)
        # 迁移 Cookie，保留域和路径信息
        for name, value, domain, path, secure, expires in old_cookies:
            try:
                self.session.cookies.set(name, value, domain=domain, path=path, secure=secure)
            except Exception:
                pass
        # 清除 sentinel 缓存，session 重建后旧 token 可能失效
        self._sentinel_cache.clear()

    def _post_auth_json_with_fallback(
        self,
        path: str,
        payload: dict,
        *,
        referer: str,
        flow: str,
        allow_redirects: bool | None = None,
        timeout: int = 30,
    ) -> dict:
        """向 auth.openai.com 发送 JSON POST，并对 curl 55 做 HTTP/1.1 短连接兜底。

        关键点：必须先 rebuild session，再生成 Sentinel header。旧实现先生成
        Sentinel、再 rebuild session，会让 token 来自旧连接上下文；在 OTP validate
        这种敏感接口上更容易触发 curl_cffi 的 curl: (55) send failure。
        """
        # 第一枪必须使用当前会话：OTP send 和 validate 之间的授权步骤
        # 对 auth.openai.com 的当前 cookie/会话上下文很敏感。只有遇到
        # 传输层异常时，第二枪才 rebuild 并强制 HTTP/1.1 短连接兜底。
        attempts = [("current", False)] if self.proxy else [("current", False), ("http1-rebuild", True)]
        last_data: dict = {}
        errors = []

        for mode, force_http1 in attempts:
            if force_http1:
                # 兜底尝试用新连接，避免复用已经被对端关闭的 HTTP/2/TLS 连接。
                self._rebuild_session()
            headers = {
                **COMMON_HEADERS,
                "referer": referer,
                "oai-device-id": self.device_id,
            }
            if force_http1:
                headers["connection"] = "close"
            try:
                self._sentinel_cache.pop(flow, None)
                self._add_sentinel_headers(headers, flow)
            except Exception:
                pass

            r = None
            error = ""
            try:
                kwargs = {
                    "json": payload,
                    "headers": headers,
                    "timeout": timeout,
                }
                if allow_redirects is not None:
                    kwargs["allow_redirects"] = allow_redirects
                if force_http1:
                    kwargs["http_version"] = CurlHttpVersion.V1_1
                r = self.session.post(f"{AUTH}{path}", **kwargs)
                ct = r.headers.get("content-type", "") if r is not None else ""
                data = r.json() if ct.startswith("application/json") else {}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                data = {}

            data["_status"] = r.status_code if r is not None else 0
            data["_body"] = r.text[:500] if r is not None and r.text else ""
            if error:
                errors.append(f"{mode}: {error}")
                data["_error"] = " | ".join(errors)[-500:]
                last_data = data
                continue
            data["_transport"] = mode
            return data

        return last_data

    # ---- Step 7: 验证 OTP 验证码 ----
    def validate_otp(self, code: str) -> dict:
        self._log(7, "POST /api/accounts/phone-otp/validate ...")
        return self._post_auth_json_with_fallback(
            "/api/accounts/phone-otp/validate",
            {"code": code},
            referer=f"{AUTH}/contact-verification",
            flow="authorize_continue",
        )

    # ---- Step 8: 创建账户 (用户名+生日) ----
    def create_account(self, name: str, birthdate: str) -> dict:
        self._log(8, "POST /api/accounts/create_account ...")
        return self._post_auth_json_with_fallback(
            "/api/accounts/create_account",
            {"name": name, "birthdate": birthdate},
            referer=f"{AUTH}/about-you",
            flow="oauth_create_account",
            allow_redirects=False,
        )

    # ---- 访问 about-you 页面建立会话 ----
    def visit_about_you(self, continue_url: str):
        if self.verbose:
            print("  [^^] 访问 about-you 页面 ...")
        url = continue_url if continue_url.startswith("http") else f"{AUTH}{continue_url}"
        self.session.get(
            url,
            headers={**NAVIGATE_HEADERS, "referer": f"{AUTH}/contact-verification", "sec-fetch-site": "same-origin"},
            allow_redirects=True,
            timeout=30,
        )

    # ---- Step 9: OAuth 回调获取 session token ----
    def oauth_callback(self, callback_url: str) -> str:
        self._log(9, "OAuth 回调 ...")
        try:
            self.session.get(
                callback_url,
                headers={**NAVIGATE_HEADERS, "referer": AUTH, "sec-fetch-site": "cross-site"},
                allow_redirects=True,
                timeout=30,
            )
        except Exception:
            pass
        # 尝试多种 Cookie 名称，兼容服务端变更
        for name in [
            "__Secure-next-auth.session-token",
            "__Secure-next-auth.session-token.1",
            "next-auth.session-token",
        ]:
            token = self.session.cookies.get(name, "")
            if token:
                return token
        return ""

    # ---- 获取 access token ----
    def get_access_token(self) -> str:
        try:
            r = self.session.get(
                f"{CHATGPT}/api/auth/session",
                headers=COMMON_HEADERS,
                timeout=30,
            )
            body = r.json()
            if isinstance(body, dict):
                return body.get("accessToken", "")
        except Exception:
            pass
        return ""


def register_phone_account(
    phone: str,
    password: str,
    proxy: str = "",
    sms_wait_fn=None,
    name: str = "A",
    birthdate: str = "2000-01-01",
    verbose: bool = True,
) -> dict:
    """One-shot phone registration: gets number -> session_token + access_token."""
    import json
    reg = ChatGPTRegister(proxy=proxy, verbose=verbose)
    try:
        reg.visit()
        csrf = reg.get_csrf()
        redirect = reg.signin(phone, csrf)
        if not redirect:
            return {"ok": False, "phone": phone, "error": "signin 无返回(可能被 Cloudflare 拦截)"}
        reg.jump_to_auth(redirect)
        result = reg.register_user(phone, password)
        continue_url = result.get("continue_url", "")
        if not continue_url:
            return {"ok": False, "phone": phone, "error": f"注册失败(status={result.get('_status')})"}
        reg.send_otp(continue_url)
        if not sms_wait_fn:
            return {"ok": False, "phone": phone, "error": "no_sms_callback"}
        code = sms_wait_fn()
        if not code:
            return {"ok": False, "phone": phone, "error": "验证码超时"}
        result = reg.validate_otp(code)
        continue_url = result.get("continue_url", "")
        if not continue_url:
            detail = (result.get("_error") or result.get("_body") or "")[:160]
            suffix = f": {detail}" if detail else ""
            return {"ok": False, "phone": phone, "error": f"验证码校验失败(status={result.get('_status')}){suffix}"}
        reg.visit_about_you(continue_url)
        result = reg.create_account(name, birthdate)
        callback_url = result.get("continue_url", "")
        if not callback_url:
            detail = result.get("_body", "")
            detail_short = detail[:200] if detail else f"status={result.get('_status')}"
            return {"ok": False, "phone": phone, "name": name, "birthdate": birthdate,
                    "error": f"创建账户失败: {detail_short}"}
        session_token = reg.oauth_callback(callback_url)
        access_token = reg.get_access_token()
        return {"ok": True, "phone": phone, "password": password,
                "session_token": session_token, "access_token": access_token}
    except Exception as e:
        return {"ok": False, "phone": phone, "error": str(e)}
