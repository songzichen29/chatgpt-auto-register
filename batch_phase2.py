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
from msoutlook_pool import MsOutlookPool, load_used_set, DEFAULT_USED_FILE
from openai_bind_email import run_second_half
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

# 状态文件
STATUS_FILE = Path("batch_phase2_status.json")

# 已注册的手机号列表
ALL_PHONES = ['+56977453296', '+56959274643', '+56954413218', '+56973386874', '+56954698641', '+56972830345', '+56965205572']  # 替换为实际手机号列表

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


def run_phase2_for_phone(phone, pool):
    """对一个手机号跑 Phase 2，循环换邮箱直到成功或耗尽"""
    email_retry = 0
    current_email = pool.get_available_email()

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
                    elif "exchange-code" in err:
                        # exchange-code 失败，auth code 可能已过期，重跑 OAuth
                        print(f"  [WARN] exchange-code 失败，重跑 OAuth...")
                        continue
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
