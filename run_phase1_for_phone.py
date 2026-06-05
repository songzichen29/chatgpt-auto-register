#!/usr/bin/env python3
"""
用已有手机号完成注册/登录。

支持两条分支：
  A. 新号注册：register_user → send_otp → validate_otp → about-you → create_account → oauth_callback
  B. 已有账号：通过 openai_bind_email.run_second_half 跑 OAuth → 绑邮箱/验邮箱 → exchange-code → 写 imports

用法:
    python run_phase1_for_phone.py +573124078181 450625349

参数:
    手机号:          完整的国际格式手机号
    activation_id:   SMS 平台的订单 ID
"""

import json
import time
import sys
import requests
import base64
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from chatgpt_register import ChatGPTRegister
from openai_bind_email import AUTH, JSON_HEADERS
from msoutlook_pool import MsOutlookPool, load_used_set

# ===== 配置 =====
def _load_local_config() -> dict:
    try:
        from auto_register import load_config

        return load_config()
    except Exception:
        return {}


def _nested(cfg: dict, section: str, key: str, default=""):
    value = cfg.get(section, {})
    if isinstance(value, dict):
        return value.get(key, default)
    return default


_CONFIG = _load_local_config()
_REGISTER = _CONFIG.get("register", {}) if isinstance(_CONFIG.get("register"), dict) else {}
_SUB2API = _CONFIG.get("sub2api", {}) if isinstance(_CONFIG.get("sub2api"), dict) else {}
_PHASE2 = _CONFIG.get("phase2", {}) if isinstance(_CONFIG.get("phase2"), dict) else {}

PROXY = _CONFIG.get("proxy") or "http://127.0.0.1:7897"
PASSWORD = _REGISTER.get("password") or ""
HERO_SMS_API_KEY = _nested(_CONFIG, "hero_sms", "api_key") or ""
HERO_SMS_BASE = _nested(_CONFIG, "hero_sms", "base_url") or "https://hero-sms.com/stubs/handler_api.php"
NAME = _REGISTER.get("name") or "A"
BIRTHDATE = _REGISTER.get("birthdate") or "2000-01-01"
CODE_TIMEOUT = int(_CONFIG.get("code_timeout") or 120)  # 等待验证码超时（秒）
STEP_RETRIES = 2    # 每步重试次数

# SUB2API 配置（用于获取 OAuth URL）
SUB2API_URL = _SUB2API.get("url") or _PHASE2.get("sub2api_url") or "https://api.dwai.cloud"
SUB2API_EMAIL = _SUB2API.get("email") or _PHASE2.get("sub2api_email") or ""
SUB2API_PWD = _SUB2API.get("pwd") or _PHASE2.get("sub2api_password") or ""
MSOUTLOOK_HELPER = _nested(_CONFIG, "msoutlook", "helper_url") or _PHASE2.get("msoutlook_helper_url") or "http://127.0.0.1:17373"
BATCH_PHASE2_STATUS_FILE = Path("batch_phase2_status.json")


def _retry_call(fn, max_retries=STEP_RETRIES, delay=2, label=""):
    """重试包装器"""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_retries:
                raise
            print(f"  [{label}] 失败 ({e})，{delay}s 后重试 ({attempt+1}/{max_retries})...")
            time.sleep(delay)


def hero_sms_set_status(activation_id: str, status: str) -> str:
    """直接调用 hero-sms API 设置状态"""
    params = {
        "api_key": HERO_SMS_API_KEY,
        "action": "setStatus",
        "id": activation_id,
        "status": status,
    }
    for attempt in range(3):
        try:
            resp = requests.get(HERO_SMS_BASE, params=params, timeout=30)
            return resp.text.strip()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                raise RuntimeError(f"setStatus 失败: {e}")


def complete_about_you_via_chat_client(phone: str, password: str) -> dict:
    """用 ChatGPT 原始 OAuth client 补完 about_you。

    实测 Codex OAuth client 在部分半成品手机号账号上会出现：
      about_you -> create_account: missing_email
    但同一账号在 ChatGPT client 下可以直接 create_account 成功。
    这个函数只负责把账号资料补齐，不负责 Codex OAuth 导入。
    """
    from openai_bind_email import OAuthSecondHalf

    print("  [修复] 使用 ChatGPT client 补 about_you ...")
    reg = ChatGPTRegister(proxy=PROXY, verbose=True)
    reg.visit()
    csrf = reg.get_csrf()
    redirect = reg.signin(phone, csrf)
    if not redirect:
        return {"ok": False, "error": "ChatGPT client signin 无返回"}

    flow = OAuthSecondHalf(proxy=PROXY, verbose=True)
    ok, current_url, _html = flow.initiate_oauth(redirect)
    if not ok:
        return {"ok": False, "error": f"ChatGPT client OAuth 发起失败: {current_url[:120]}"}
    flow.sentinel_authorize()
    r = flow.submit_phone(phone)
    if r.get("error"):
        return {"ok": False, "error": f"ChatGPT client submit_phone: {r.get('error')}"}
    flow.sentinel_password()
    r = flow.verify_password(password)
    if r.get("error"):
        return {"ok": False, "error": f"ChatGPT client verify_password: {r.get('error')}"}

    page_type = (r.get("page") or {}).get("type", "")
    if "about_you" not in page_type:
        print(f"  [修复] ChatGPT client 当前 page={page_type or '?'}，无需补 about_you")
        return {"ok": True, "page": page_type, "skipped": True}

    headers = {
        **JSON_HEADERS,
        "referer": f"{AUTH}/about-you",
        "oai-device-id": flow.device_id,
    }
    st = flow._sentinel_token("oauth_create_account")
    if st:
        headers["OpenAI-Sentinel-Token"] = st
    r2 = flow.session.post(
        f"{AUTH}/api/accounts/create_account",
        json={"name": NAME, "birthdate": BIRTHDATE},
        headers=headers,
        allow_redirects=False,
    )
    try:
        data = r2.json()
    except Exception:
        data = {}
    next_page = (data.get("page") or {}).get("type", "")
    continue_url = data.get("continue_url", "")
    print(f"  [修复] create_account status={r2.status_code} page={next_page or '?'}")
    if r2.ok and continue_url:
        return {"ok": True, "page": next_page, "continue_url": continue_url}
    return {
        "ok": False,
        "error": f"ChatGPT client create_account 失败: status={r2.status_code} body={r2.text[:300]}",
    }


def update_batch_phase2_status(phone: str, result: dict) -> None:
    """
    run_phase1_for_phone 的已有账号分支会直接完成 Phase 2/OAuth。
    成功后同步 batch_phase2_status.json，避免 batch_phase2.py 再重复跑同一手机号。
    """
    try:
        if BATCH_PHASE2_STATUS_FILE.exists():
            status = json.loads(BATCH_PHASE2_STATUS_FILE.read_text(encoding="utf-8"))
            if not isinstance(status, dict):
                status = {}
        else:
            status = {}
    except Exception:
        status = {}

    status[phone] = {
        "status": "ok",
        "sub2api_id": result.get("sub2api_id", ""),
        "import_file": result.get("import_file", ""),
        "bind_email": result.get("bind_email", ""),
        "source": "run_phase1_for_phone",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    BATCH_PHASE2_STATUS_FILE.write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def wait_for_code(activation_id: str, timeout: int = CODE_TIMEOUT, verbose: bool = True) -> str:
    """轮询等待验证码"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(HERO_SMS_BASE, params={
                "api_key": HERO_SMS_API_KEY,
                "action": "getStatus",
                "id": activation_id,
            }, timeout=30)
            status = resp.text.strip()
        except Exception as e:
            if verbose:
                print(f"  [sms] 轮询异常: {e}")
            time.sleep(3)
            continue

        if verbose:
            print(f"  [sms] {activation_id}: {status[:80]}")

        if status.startswith("STATUS_OK:"):
            return status.split(":", 1)[1]
        elif status.startswith("STATUS_WAIT_RETRY"):
            time.sleep(3)
        elif status == "STATUS_WAIT_CODE":
            time.sleep(5)
        elif status == "STATUS_CANCEL":
            raise RuntimeError("号码已取消")
        elif status.startswith("STATUS_FINISH"):
            raise RuntimeError("号码已完成，无法收码")
        else:
            time.sleep(3)

    raise RuntimeError("验证码超时")


# ─────────── 分支 A: 新号注册 ───────────

def branch_new_account(reg, phone, activation_id):
    """新号注册流程: send_otp → validate_otp → about-you → create_account → oauth_callback → token"""
    # Step 1: 访问首页
    print(f"\n--- Step 1: 访问首页 ---")
    _retry_call(lambda: reg.visit(), label="访问首页")

    # Step 2: 获取 CSRF
    print(f"\n--- Step 2: 获取 CSRF ---")
    csrf = _retry_call(lambda: reg.get_csrf(), label="CSRF")
    print(f"  CSRF: {csrf[:20]}...")

    # Step 3: 发起登录
    print(f"\n--- Step 3: 发起登录 ---")
    redirect = _retry_call(lambda: reg.signin(phone, csrf), label="发起登录")
    if not redirect:
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "error": "signin 无返回(可能被 Cloudflare 拦截)",
        }
    print(f"  redirect: {redirect[:80]}...")

    # Step 4: 跳转 auth
    print(f"\n--- Step 4: 跳转 auth ---")
    location = _retry_call(lambda: reg.jump_to_auth(redirect), label="OAuth跳转")
    print(f"  location: {location[:120] if location else '空'}...")
    print(f"  DEBUG Step4: location is truthy: {bool(location)}, '/error' in location: {'/error' in (location or '')}")
    # 如果 location 包含 error，直接走已有账号流程
    if location and "/error" in location:
        print(f"\n  → jump_to_auth 走到 error 页面，走已有账号流程")
        return branch_contact_verification(reg, phone, activation_id)

    # Step 5: 注册用户
    print(f"\n--- Step 5: 注册用户 ---")
    result = _retry_call(lambda: reg.register_user(phone, PASSWORD), label="注册")
    page_type = (result.get("page") or {}).get("type", "")
    continue_url = result.get("continue_url", "")
    print(f"  page: {page_type}")
    print(f"  continue_url: {continue_url[:100] if continue_url else '空'}...")
    print(f"  DEBUG: full result = {json.dumps(result, ensure_ascii=False)[:500]}")

    # ─── 分支判断 ───
    error = result.get("error", {})
    error_msg = error.get("message", "") if isinstance(error, dict) else ""
    status_code = result.get("_status", 0)
    print(f"  DEBUG: error={error}, error_msg='{error_msg}', status_code={status_code}")
    print(f"  DEBUG: full result = {json.dumps(result, ensure_ascii=False)[:500]}")

    # 情况 B: 已有账号 → contact_verification 页面或 session invalid
    if "contact_verification" in page_type:
        print(f"\n  → 检测到 contact_verification，走已有账号流程")
        return branch_contact_verification(reg, phone, activation_id)

    if not continue_url and ("Invalid authorization step" in error_msg or "session is no longer valid" in error_msg.lower() or "start over" in error_msg.lower()):
        print(f"\n  → 检测到已有账号信号，走已有账号流程")
        return branch_contact_verification(reg, phone, activation_id)

    if not continue_url:
        # No continue_url means something went wrong — try branch_contact_verification anyway
        print(f"\n  → 无 continue_url，尝试走已有账号流程")
        return branch_contact_verification(reg, phone, activation_id)

    # ── 分支 A: 新号 ──

    # Step 6: 发送 OTP
    print(f"\n--- Step 6: 发送 OTP ---")
    _retry_call(lambda u=continue_url: reg.send_otp(u), label="发送验证码")
    print(f"  已请求发送验证码到 {phone}")

    # 等待验证码
    print(f"\n--- 等待验证码 (最多{CODE_TIMEOUT}秒) ---")
    code = wait_for_code(activation_id)
    print(f"  收到验证码: {code}")

    # Step 7: 验证 OTP
    print(f"\n--- Step 7: 验证 OTP ---")
    result = _retry_call(lambda c=code: reg.validate_otp(c), label="校验验证码")
    continue_url = result.get("continue_url", "")
    if not continue_url:
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "error": f"验证码校验失败(status={result.get('_status')})",
        }
    print(f"  OTP 验证成功!")
    print(f"  continue_url: {continue_url[:100]}...")

    # Step 8: 访问 about-you
    print(f"\n--- Step 8: 访问 about-you ---")
    _retry_call(lambda: reg.visit_about_you(continue_url), label="访问about-you")

    # Step 9: 创建账户
    print(f"\n--- Step 9: 创建账户 ---")
    result = _retry_call(lambda: reg.create_account(NAME, BIRTHDATE), label="创建账户")
    callback_url = result.get("continue_url", "")
    if not callback_url:
        detail = result.get("_body", "")[:200]
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "error": f"创建账户失败: {detail}",
        }
    print(f"  创建账户成功!")
    print(f"  callback_url: {callback_url[:100]}...")

    # Step 10: OAuth 回调
    print(f"\n--- Step 10: OAuth 回调 ---")
    session_token = _retry_call(lambda: reg.oauth_callback(callback_url), label="OAuth回调")
    if session_token:
        print(f"  session_token: {session_token[:50]}...")
    else:
        print(f"  [WARNING] 未获取到 session_token")

    # Step 11: 获取 access token
    print(f"\n--- Step 11: 获取 access token ---")
    access_token = _retry_call(lambda: reg.get_access_token(), label="获取Token")
    if access_token:
        print(f"  access_token: {access_token[:50]}...")
    else:
        print(f"  [WARNING] 未获取到 access_token")

    return {
        "ok": True, "phone": phone, "password": PASSWORD,
        "session_token": session_token or "",
        "access_token": access_token or "",
        "activation_id": activation_id,
        "branch": "new_account",
    }


# ─────────── 分支 B: 已有账号（通过 run_second_half） ───────────

def branch_contact_verification(reg, phone, activation_id):
    """
    已有账号流程：
    使用 run_second_half 从 openai_bind_email.py 来跑完整的 OAuth 流程。
    这样可以用正确的 OAuth session context。
    """
    from openai_bind_email import run_second_half

    # 1. 检查号码当前状态
    try:
        resp = requests.get(HERO_SMS_BASE, params={
            "api_key": HERO_SMS_API_KEY,
            "action": "getStatus",
            "id": activation_id,
        }, timeout=30)
        current_status = resp.text.strip()
        print(f"  号码当前状态: {current_status[:80]}")
        if current_status.startswith("STATUS_OK:"):
            code = current_status.split(":", 1)[1]
            print(f"  号码已有验证码: {code}")
        else:
            code = None
    except Exception as e:
        print(f"  查询状态失败: {e}")
        code = None

    def get_oauth_session():
        r = requests.post(f"{SUB2API_URL}/api/v1/auth/login",
                          json={"email": SUB2API_EMAIL, "password": SUB2API_PWD}, timeout=15)
        login_data = r.json()
        admin_token = login_data["data"]["access_token"]

        r = requests.post(f"{SUB2API_URL}/api/v1/admin/openai/generate-auth-url",
                          json={"redirect_uri": "http://localhost:1455/auth/callback"},
                          headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        oauth_data = r.json()
        oauth_url = oauth_data["data"]["auth_url"]
        session_id = oauth_data["data"]["session_id"]
        oauth_state = parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
        return oauth_url, session_id, oauth_state

    # 2. 获取 OAuth URL
    print(f"\n  获取 OAuth URL ...")
    try:
        oauth_url, session_id, oauth_state = get_oauth_session()
        print(f"  OAuth URL: {oauth_url[:120]}...")
    except Exception as e:
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "error": f"获取 OAuth URL 失败: {e}",
        }

    # 3. 获取可用邮箱（用于绑邮箱，Phase 2 需要）
    email = None
    ms_pool = None
    ms_helper_url = MSOUTLOOK_HELPER
    if ms_helper_url:
        try:
            used = load_used_set()
            ms_pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False, extra_used=used)
            email = ms_pool.get_available_email()
            if email:
                print(f"  邮箱: {email}")
        except Exception as e:
            print(f"  号池加载失败: {e}")

    if not email:
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "error": "无可用邮箱",
        }

    # 4. 跑 run_second_half。这个分支实际会完成 OAuth + 绑邮箱 + exchange-code，
    #    成功时需要写入 imports/import_YYYYMMDD.json，避免后续 batch_phase2 重复跑。
    print(f"\n  跑 Phase 2 OAuth 流程 ...")
    from phone_sms import PhoneSMS

    sms = PhoneSMS("hero-sms", HERO_SMS_API_KEY)

    result = {"ok": False, "error": "not started"}
    fixed_about_you = False
    for phase2_attempt in range(3):
        if phase2_attempt > 0:
            wait_sec = 20 if phase2_attempt == 1 else 60
            print(f"\n  Phase2 重试 {phase2_attempt + 1}/3，等待 {wait_sec}s 后重新获取 OAuth URL ...")
            time.sleep(wait_sec)
            try:
                oauth_url, session_id, oauth_state = get_oauth_session()
                print(f"  OAuth URL: {oauth_url[:120]}...")
            except Exception as e:
                result = {"ok": False, "error": f"重新获取 OAuth URL 失败: {e}"}
                continue

        result = run_second_half(
            oauth_url=oauth_url,
            phone=phone,
            password=PASSWORD,
            icloud_email=email,
            icloud_cookies={},
            sub2api_url=SUB2API_URL,
            sub2api_email=SUB2API_EMAIL,
            sub2api_password=SUB2API_PWD,
            sub2api_proxy_id=0,
            proxy=PROXY,
            verbose=True,
            sub2api_session_id=session_id,
            sub2api_state=oauth_state,
            msoutlook_helper_url=ms_helper_url,
            msoutlook_email=email,
            sms_obj=sms,
            phone_aid=activation_id,
            save_import=True,
            interactive_input=False,  # 不阻塞等 input
        )
        if result.get("ok"):
            break

        err = str(result.get("error", ""))
        if "codex_about_you_missing_email" in err and not fixed_about_you:
            fix = complete_about_you_via_chat_client(phone, PASSWORD)
            if not fix.get("ok"):
                result = {"ok": False, "error": f"{err}; ChatGPT about_you 修复失败: {fix.get('error')}"}
                break
            fixed_about_you = True
            print("  [修复] about_you 已补完，重新跑 Codex Phase2")
            continue

        if "rate_limit_exceeded" in err or "Too many requests" in err or "429" in err:
            print(f"  [WARN] Phase2 限流，可重试: {err[:160]}")
            continue

        break

    if result.get("ok"):
        session_token = result.get("session_token", "")
        access_token = result.get("access_token", "")
        if ms_pool and email:
            ms_pool.mark_used(email, phone=phone, password=PASSWORD)
        return {
            "ok": True, "phone": phone, "password": PASSWORD,
            "session_token": session_token or "",
            "access_token": access_token or "",
            "activation_id": activation_id,
            "branch": "contact_verification_via_phase2",
            "bind_email": email,
            "sub2api_id": result.get("sub2api_account_id", ""),
            "import_file": result.get("import_file", ""),
            "import_data": result.get("import_data"),
            "code": result.get("code", ""),
        }
    else:
        if ms_pool and email:
            ms_pool.mark_error(email, result.get("error", "run_second_half failed"), phone=phone, password=PASSWORD)
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "activation_id": activation_id,
            "bind_email": email,
            "error": f"run_second_half 失败: {result.get('error', 'unknown')}",
        }


# ─────────── 主流程 ───────────

def main():
    if len(sys.argv) < 3:
        print("用法: python run_phase1_for_phone.py <手机号> <activation_id>")
        print("示例: python run_phase1_for_phone.py +573124078181 450625349")
        sys.exit(1)

    phone = sys.argv[1]
    activation_id = sys.argv[2]

    print(f"=== Phase 1: 已有手机号注册/登录 ===")
    print(f"  手机号: {phone}")
    print(f"  激活ID: {activation_id}")
    print(f"  代理: {PROXY}")
    print(f"  密码: {PASSWORD}")
    print(f"  验证码超时: {CODE_TIMEOUT}s")
    print()

    # 检查号码状态
    try:
        resp = requests.get(HERO_SMS_BASE, params={
            "api_key": HERO_SMS_API_KEY,
            "action": "getStatus",
            "id": activation_id,
        }, timeout=30)
        status_text = resp.text.strip()
        print(f"  号码状态: {status_text[:80]}")
    except Exception as e:
        print(f"  查询号码状态失败: {e}")
    print()

    reg = ChatGPTRegister(proxy=PROXY, verbose=True)

    try:
        result = branch_new_account(reg, phone, activation_id)

        print(f"\n{'='*50}")
        if result.get("ok"):
            if result.get("branch") == "contact_verification_via_phase2":
                print(f"[SUCCESS] 已有账号 OAuth/Phase 2 完成!")
            else:
                print(f"[SUCCESS] Phase 1 完成!")
            print(f"  分支: {result.get('branch', '?')}")
            print(f"  手机号: {phone}")
            print(f"  密码: {PASSWORD}")
            if result.get("bind_email"):
                print(f"  绑定邮箱: {result.get('bind_email')}")
            if result.get("import_file"):
                print(f"  导入文件: {result.get('import_file')}")
            print(f"  session_token: {result.get('session_token', '无')[:50]}")
            print(f"  access_token: {result.get('access_token', '无')[:50]}")
            print(f"  激活ID: {activation_id}")

            # 激活号码
            print(f"\n  激活号码 (status=6)...")
            try:
                hero_sms_set_status(activation_id, "6")
                print(f"  号码已激活!")
            except Exception as e:
                print(f"  激活失败: {e}")
        else:
            print(f"[FAIL] {result.get('error', 'unknown')}")
        print(f"{'='*50}")

        # 保存结果：成功/失败都落盘，失败记录里也保留本次使用的 password/activation_id。
        ts = time.strftime("%Y%m%d_%H%M%S")
        clean_phone = phone.replace("+", "")
        suffix = "" if result.get("ok") else "_fail"
        p = Path(f"results/phase1_{clean_phone}_{ts}{suffix}.json")
        Path("results").mkdir(exist_ok=True)
        p.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"\n结果已保存到: {p}")

        if result.get("ok"):
            if result.get("branch") == "contact_verification_via_phase2" and (
                result.get("import_file") or result.get("import_data") or result.get("sub2api_id")
            ):
                update_batch_phase2_status(phone, result)
                print(f"Phase2 状态已标记为 OK: {BATCH_PHASE2_STATUS_FILE}")

    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
