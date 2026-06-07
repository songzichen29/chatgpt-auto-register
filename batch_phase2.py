#!/usr/bin/env python3
"""
批量 Phase 2: 对已注册的手机号跑 OAuth + 绑邮箱 + 上传 SUB2API

用法:
    python batch_phase2.py          # 跑全部未完成的号码
    python batch_phase2.py +569...  # 跑指定号码
"""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from chatgpt_register import ChatGPTRegister
from msoutlook_pool import MsOutlookPool, load_used_set, DEFAULT_USED_FILE
from openai_bind_email import AUTH, JSON_HEADERS, OAuthSecondHalf, run_second_half
import requests

# ── 配置 ──
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

PASSWORD = _REGISTER.get("password") or ""
PROXY = _CONFIG.get("proxy") or "http://127.0.0.1:7897"
SUB2API_URL = _SUB2API.get("url") or _PHASE2.get("sub2api_url") or "https://api.dwai.cloud"
SUB2API_EMAIL = _SUB2API.get("email") or _PHASE2.get("sub2api_email") or ""
SUB2API_PWD = _SUB2API.get("pwd") or _PHASE2.get("sub2api_password") or ""
MSOUTLOOK_HELPER = _nested(_CONFIG, "msoutlook", "helper_url") or _PHASE2.get("msoutlook_helper_url") or "http://127.0.0.1:17373"
MAX_EMAIL_RETRIES = 10
MAX_OAUTH_RETRIES = 3  # exchange-code 失败后重跑 OAuth 的次数
CREATE_ACCOUNT_RETRIES = int(_REGISTER.get("create_account_max_retries") or _CONFIG.get("create_account_max_retries") or 20)
FATAL_EMAIL_KEYWORDS = (
    "msoutlook_unreadable",
    "compromised",
    "invalid_grant",
    "security interrupt",
    "aadsts70000",
    "refresh_token_or_scope_invalid",
    "刷新令牌无效",
)

# 状态文件
STATUS_FILE = Path("batch_phase2_status.json")

# 已注册的手机号列表
ALL_PHONES = ['+56946713024'] # 替换为实际手机号列表

# 加载已上传的号码（跳过）
def load_uploaded():
    p = Path('results/_all.json')
    if not p.exists():
        return set()
    data = json.loads(p.read_text())
    return {r.get('phone', '') for r in data if r.get('sub2api_id')}


def get_oauth_url():
    """登录 SUB2API 并获取 OAuth URL"""
    r = requests.post(f"{SUB2API_URL}/api/v1/auth/login",
                      json={"email": SUB2API_EMAIL, "password": SUB2API_PWD}, timeout=15)
    login_data = r.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API登录失败: {login_data}")
    admin_token = login_data["data"]["access_token"]

    r = requests.post(f"{SUB2API_URL}/api/v1/admin/openai/generate-auth-url",
                      json={"redirect_uri": "http://localhost:1455/auth/callback"},
                      headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
    oauth_data = r.json()
    if oauth_data.get("code") != 0:
        raise RuntimeError(f"获取OAuth URL失败: {oauth_data}")

    oauth_url = oauth_data["data"]["auth_url"]
    session_id = oauth_data["data"]["session_id"]
    oauth_state = parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
    return oauth_url, session_id, oauth_state


def _account_profile_for_attempt(attempt: int) -> tuple[str, str]:
    """生成 about_you/create_account 资料，避免使用占位 A/2000-01-01。"""
    configured_name = str(_REGISTER.get("name") or "").strip()
    configured_birthdate = str(_REGISTER.get("birthdate") or "").strip()
    if (
        attempt == 0
        and configured_name
        and configured_birthdate
        and configured_name != "A"
        and configured_birthdate != "2000-01-01"
    ):
        return configured_name, configured_birthdate

    from auto_register import random_birthdate, random_name

    return random_name(), random_birthdate()


def complete_about_you_via_chat_client(phone: str, password: str) -> dict:
    """batch_phase2 遇到 Codex OAuth missing_email 时，用 ChatGPT client 先补 about_you。

    现象：
      Codex OAuth: about_you -> create_account 返回 missing_email
      ChatGPT client: 同一账号可先补 about_you，然后重新跑 Codex OAuth 继续绑邮箱
    """
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

    last_error = ""
    for ca_attempt in range(max(1, CREATE_ACCOUNT_RETRIES)):
        ca_name, ca_birthdate = _account_profile_for_attempt(ca_attempt)
        print(
            f"  [修复] create_account [{ca_attempt + 1}/{CREATE_ACCOUNT_RETRIES}]: "
            f"name={ca_name} birthdate={ca_birthdate}"
        )
        headers = {
            **JSON_HEADERS,
            "referer": f"{AUTH}/about-you",
            "oai-device-id": flow.device_id,
        }
        st = flow._sentinel_token("oauth_create_account")
        if st:
            headers["OpenAI-Sentinel-Token"] = st
        resp = flow.session.post(
            f"{AUTH}/api/accounts/create_account",
            json={"name": ca_name, "birthdate": ca_birthdate},
            headers=headers,
            allow_redirects=False,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        next_page = (data.get("page") or {}).get("type", "")
        continue_url = data.get("continue_url", "")
        err = data.get("error") or {}
        err_code = err.get("code", "") if isinstance(err, dict) else ""
        last_error = resp.text[:300] if resp.text else f"status={resp.status_code}, page={next_page or '?'}"
        print(f"  [修复] create_account status={resp.status_code} page={next_page or '?'} code={err_code or '-'}")
        if resp.ok and continue_url:
            return {
                "ok": True,
                "page": next_page,
                "continue_url": continue_url,
                "name": ca_name,
                "birthdate": ca_birthdate,
            }
        if ca_attempt < CREATE_ACCOUNT_RETRIES - 1:
            time.sleep(20 if err_code == "rate_limit_exceeded" or resp.status_code == 429 else 1)

    return {
        "ok": False,
        "error": f"ChatGPT client create_account 失败(已重试{CREATE_ACCOUNT_RETRIES}次): {last_error}",
    }


def run_phase2_for_phone(phone, pool):
    """对一个手机号跑 Phase 2，循环换邮箱直到成功或耗尽"""
    email_retry = 0
    current_email = pool.get_available_email()
    fixed_about_you = False

    if not current_email:
        print(f"  [ERROR] 号池无可用邮箱")
        return {"ok": False, "phone": phone, "password": PASSWORD, "error": "号池无可用邮箱"}

    while email_retry < MAX_EMAIL_RETRIES:
        email_retry += 1
        print(f"  [重试{email_retry}] 邮箱: {current_email}")

        # 外层 OAuth 重试：exchange-code 失败后重跑 OAuth
        for oauth_attempt in range(MAX_OAUTH_RETRIES):
            if oauth_attempt > 0:
                print(f"  [OAuth重试{oauth_attempt}] 重新获取 OAuth URL ...")

            try:
                oauth_url, session_id, oauth_state = get_oauth_url()

                result = run_second_half(
                    oauth_url=oauth_url,
                    phone=phone,
                    password=PASSWORD,
                    icloud_email=current_email,
                    icloud_cookies={},
                    imap_user="",
                    imap_password="",
                    sub2api_url=SUB2API_URL,
                    sub2api_email=SUB2API_EMAIL,
                    sub2api_password=SUB2API_PWD,
                    proxy=PROXY,
                    verbose=True,
                    sub2api_session_id=session_id,
                    sub2api_state=oauth_state,
                    sub2api_proxy_id=0,
                    msoutlook_helper_url=MSOUTLOOK_HELPER,
                    msoutlook_email=current_email,
                    interactive_input=False,
                )

                if result.get("ok"):
                    aid = result.get("sub2api_account_id", "?")
                    result["phone"] = phone
                    result["password"] = PASSWORD
                    result["bind_email"] = current_email
                    pool.mark_used(current_email, phone=phone, password=PASSWORD)
                    print(f"  [OK] 上传成功! SUB2API id={aid}")
                    return result
                else:
                    err = result.get("error", "")
                    if "email_already_in_use" in err:
                        print(f"  [WARN] 邮箱被占用，换邮箱...")
                        pool.mark_error(current_email, "email_already_in_use", phone=phone, password=PASSWORD)
                        current_email = pool.get_available_email()
                        if not current_email:
                            print(f"  [ERROR] 号池无可用邮箱")
                            return {"ok": False, "phone": phone, "password": PASSWORD, "error": "号池无可用邮箱"}
                        break  # 跳出 OAuth 重试，进入下一个邮箱
                    elif any(kw in err.lower() for kw in FATAL_EMAIL_KEYWORDS):
                        print(f"  [WARN] 邮箱不可读/令牌失效，标记坏邮箱并换邮箱...")
                        pool.mark_error(current_email, "token_compromised", phone=phone, password=PASSWORD)
                        current_email = pool.get_available_email()
                        if not current_email:
                            print(f"  [ERROR] 号池无可用邮箱")
                            return {"ok": False, "phone": phone, "password": PASSWORD, "error": "号池无可用邮箱"}
                        break  # 跳出 OAuth 重试，进入下一个邮箱
                    elif "exchange-code" in err:
                        # exchange-code 失败，auth code 可能已过期，重跑 OAuth
                        print(f"  [WARN] exchange-code 失败，重跑 OAuth...")
                        continue
                    elif "codex_about_you_missing_email" in err and not fixed_about_you:
                        # Codex OAuth 在半注册账号上可能 about_you/create_account 返回 missing_email。
                        # 先用 ChatGPT 原始 client 补 about_you，再重新获取 Codex OAuth URL 继续 Phase 2。
                        fix = complete_about_you_via_chat_client(phone, PASSWORD)
                        if fix.get("ok"):
                            fixed_about_you = True
                            print(f"  [修复] about_you 已补完，重新跑 Codex Phase2 ...")
                            continue
                        fix_err = fix.get("error", "unknown")
                        print(f"  [ERROR] ChatGPT about_you 修复失败: {fix_err[:200]}")
                        return {
                            "ok": False,
                            "phone": phone,
                            "password": PASSWORD,
                            "bind_email": current_email,
                            "error": f"{err}; ChatGPT about_you 修复失败: {fix_err}",
                        }
                    elif "codex_about_you_missing_email" in err:
                        print(f"  [ERROR] about_you missing_email 已修复过一次仍失败，不再重复修复")
                        return {
                            "ok": False,
                            "phone": phone,
                            "password": PASSWORD,
                            "bind_email": current_email,
                            "error": err or "codex_about_you_missing_email",
                        }
                    elif "account_stuck_email_otp" in err:
                        # 账号状态问题，不证明当前选中的 Outlook 邮箱不可用。
                        print(f"  [ERROR] 账号卡在 email_otp 状态，当前邮箱不标记为坏邮箱")
                        return {
                            "ok": False,
                            "phone": phone,
                            "password": PASSWORD,
                            "bind_email": current_email,
                            "error": err or "account_stuck_email_otp",
                        }
                    elif "account_requires_contact_verification" in err or "contact_verification code timeout" in err:
                        # batch_phase2 是已注册账号的 Phase 2 续跑/上传，不持有 SMS activation_id。
                        # 如果登录触发 contact_verification，说明账号状态需要额外验证；
                        # 这不是当前 Outlook 邮箱的问题，不能标记邮箱错误，也不应在这里重发/等待短信。
                        print(f"  [ERROR] 账号触发 contact_verification，batch_phase2 跳过该账号")
                        return {
                            "ok": False,
                            "phone": phone,
                            "password": PASSWORD,
                            "bind_email": current_email,
                            "error": err or "account_requires_contact_verification",
                        }
                    else:
                        print(f"  [ERROR] Phase 2 失败: {err[:200]}")
                        return {
                            "ok": False,
                            "phone": phone,
                            "password": PASSWORD,
                            "bind_email": current_email,
                            "error": err or "Phase 2 failed",
                        }
            except Exception as e:
                print(f"  [ERROR] 异常: {e}")
                return {
                    "ok": False,
                    "phone": phone,
                    "password": PASSWORD,
                    "bind_email": current_email,
                    "error": str(e),
                }

        # OAuth 重试耗尽
        print(f"  [ERROR] OAuth 重试{MAX_OAUTH_RETRIES}次仍失败")
        return {
            "ok": False,
            "phone": phone,
            "password": PASSWORD,
            "bind_email": current_email,
            "error": f"OAuth 重试{MAX_OAUTH_RETRIES}次仍失败",
        }

    print(f"  [ERROR] 换邮箱{MAX_EMAIL_RETRIES}次仍失败")
    return {
        "ok": False,
        "phone": phone,
        "password": PASSWORD,
        "bind_email": current_email,
        "error": f"换邮箱{MAX_EMAIL_RETRIES}次仍失败",
    }


def load_status():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_status(status):
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")

def main():
    phones = sys.argv[1:] if len(sys.argv) > 1 else None
    status = load_status()

    if phones:
        print(f"指定号码: {len(phones)} 个")
    else:
        # 过滤掉已成功的
        phones = [p for p in ALL_PHONES if status.get(p, {}).get("status") != "ok"]
        ok = sum(1 for p in ALL_PHONES if status.get(p, {}).get("status") == "ok")
        fail = sum(1 for p in ALL_PHONES if status.get(p, {}).get("status") == "fail")
        print(f"需要跑: {len(phones)}/{len(ALL_PHONES)} 个 (已上传{ok} 已失败{fail})")

    # 显示当前状态
    print("\n当前状态:")
    for p in ALL_PHONES:
        s = status.get(p, {})
        st = s.get("status", "pending")
        sub_id = s.get("sub2api_id", "")
        err = s.get("error", "")
        if st == "ok":
            print(f"  [OK]   {p}  SUB2API#{sub_id}")
        elif st == "fail":
            print(f"  [FAIL] {p}  {err[:60]}")
        else:
            print(f"  [....] {p}")

    # 收集所有已使用的邮箱：msoutlook_used.json + results/_all.json 已绑定的邮箱
    used_emails = set()
    # 1. msoutlook_used.json（兼容新旧格式）
    used_emails = load_used_set()
    # 2. results/_all.json 中已成功绑定的邮箱
    try:
        all_data = json.loads(Path("results/_all.json").read_text())
        for r in all_data:
            if r.get("bind_email"):
                used_emails.add(r["bind_email"].lower())
    except Exception:
        pass

    pool = MsOutlookPool(helper_url=MSOUTLOOK_HELPER, verbose=True, extra_used=used_emails)

    # 显示过滤后的状态
    print(f"已加载 {len(used_emails)} 个已用邮箱")
    print(f"号池: 可用{pool.stats()['available']}/{pool.stats()['total']}")

    ok_count = 0
    fail_count = 0

    for i, phone in enumerate(phones, 1):
        print(f"\n{'='*50}")
        print(f"[{i}/{len(phones)}] {phone}")
        print(f"{'='*50}")
        result = run_phase2_for_phone(phone, pool)
        if result and result.get("ok"):
            ok_count += 1
            sub_id = result.get("sub2api_account_id", "")
            status[phone] = {
                "status": "ok",
                "sub2api_id": sub_id,
                "bind_email": result.get("bind_email", ""),
                "import_file": result.get("import_file", ""),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_status(status)
            print(f"  [状态] 已记录: OK, SUB2API#{sub_id}")
            # 保存结果
            safe = {
                "phone": phone,
                "password": PASSWORD,
                "bind_email": result.get("bind_email", ""),
                "sub2api_id": result.get("sub2api_account_id", ""),
                "import_file": result.get("import_file", ""),
                "import_data": result.get("import_data"),
                "ok": True,
            }
            ts = time.strftime("%Y%m%d_%H%M%S")
            clean_phone = phone.replace("+", "")
            p = Path(f"results/{clean_phone}_{ts}.json")
            p.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n")
            # 追加到 _all.json
            all_path = Path("results/_all.json")
            if all_path.exists():
                all_data = json.loads(all_path.read_text())
            else:
                all_data = []
            all_data.append(safe)
            all_path.write_text(json.dumps(all_data, indent=2, ensure_ascii=False) + "\n")
        else:
            fail_count += 1
            err_msg = result.get("error", "") if result else "unknown"
            status[phone] = {
                "status": "fail",
                "error": err_msg,
                "bind_email": result.get("bind_email", "") if result else "",
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_status(status)
            print(f"  [状态] 已记录: FAIL, {err_msg[:60]}")
            safe = {
                "phone": phone,
                "password": PASSWORD,
                "bind_email": result.get("bind_email", "") if result else "",
                "error": err_msg,
                "ok": False,
            }
            ts = time.strftime("%Y%m%d_%H%M%S")
            clean_phone = phone.replace("+", "")
            p = Path(f"results/{clean_phone}_{ts}_fail_phase2.json")
            Path("results").mkdir(exist_ok=True)
            p.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n")

        # 间隔，避免太快
        if i < len(phones):
            time.sleep(2)

    print(f"\n{'='*50}")
    print(f"完成: 成功{ok_count} 失败{fail_count}")
    print(f"\n全部状态:")
    for p in ALL_PHONES:
        s = status.get(p, {})
        st = s.get("status", "pending")
        sub_id = s.get("sub2api_id", "")
        err = s.get("error", "")
        if st == "ok":
            print(f"  [OK]   {p}  SUB2API#{sub_id}")
        elif st == "fail":
            print(f"  [FAIL] {p}  {err[:80]}")
        else:
            print(f"  [....] {p}  未处理")


if __name__ == "__main__":
    main()
