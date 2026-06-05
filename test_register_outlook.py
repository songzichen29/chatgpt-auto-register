#!/usr/bin/env python3
"""
测试脚本: 用一个已注册的 OpenAI 手机号 + 一个 MsOutlook 邮箱，跑完 Phase 2 流程
  (OAuth -> 绑邮箱 -> 收验证码 -> 上传 SUB2API)

使用前必读:
  1. PHONE / PASSWORD 必须是已经 Phase 1 注册成功的 OpenAI 账号。
     否则 verify_password 会返回 invalid_username_or_password。
  2. MSOUTLOOK_EMAIL 必须在 号.json 池里, 且 clientId / refreshToken 有效。
  3. BIND_CODE 留空 — 让 wait_for_code 自动拿 OpenAI 这次新发的码。
     不要预填历史码: add_email 分支会先 send_bind_email 触发 OpenAI 发新码,
     历史码会被作废, verify_email_otp 必返回 wrong_email_otp_code。
  4. REDIRECT_URI / SUB2API_* / PROXY / MSOUTLOOK_HELPER_URL 按你的环境改。
"""

import sys
from urllib.parse import urlparse, parse_qs

import requests as req_lib

# ============================================================
# 配置 — 所有字段都是占位符, 跑之前请替换为你的真实参数
# ============================================================

# Phase 1 已注册成功的 OpenAI 账号
PHONE = "<填入已注册手机号, 如 +569xxxxxxxxx>"
PASSWORD = "<填入对应密码>"

# MsOutlook 邮箱 (必须在 号.json 池里)
MSOUTLOOK_EMAIL = "<填入 MsOutlook 邮箱, 如 xxx@outlook.com>"
BIND_CODE = ""  # 留空: 让 wait_for_code 自动拿新码

# SUB2API (用于 exchange-code 上传新账号)
SUB2API_URL = "<填入 SUB2API 地址, 如 https://api.example.com>"
SUB2API_EMAIL = "<填入 SUB2API 管理员邮箱>"
SUB2API_PASSWORD = "<填入 SUB2API 管理员密码>"

# 网络
PROXY = "<填入代理, 如 http://127.0.0.1:7897; 直连填空字符串>"
MSOUTLOOK_HELPER_URL = "<填入 Hotmail Helper 地址, 如 http://127.0.0.1:17373>"
REDIRECT_URI = "<填入 SUB2API 注册的 redirect_uri, 如 http://localhost:1455/auth/callback>"


def login_sub2api() -> str:
    """登录 SUB2API, 返回 admin_token"""
    resp = req_lib.post(
        f"{SUB2API_URL}/api/v1/auth/login",
        json={"email": SUB2API_EMAIL, "password": SUB2API_PASSWORD},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"SUB2API 登录失败: {data}")
    return data["data"]["access_token"]


def get_oauth_url(admin_token: str) -> dict:
    """从 SUB2API 获取 OAuth URL + session_id + state"""
    resp = req_lib.post(
        f"{SUB2API_URL}/api/v1/admin/openai/generate-auth-url",
        json={"redirect_uri": REDIRECT_URI},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成 OAuth URL 失败: {data}")
    result = data["data"]
    return {
        "oauth_url": result["auth_url"],
        "session_id": result["session_id"],
        "state": result.get("state", ""),
    }


def _check_placeholders():
    """跑之前检查是否还有占位符没替换"""
    placeholders = {
        "PHONE": PHONE, "PASSWORD": PASSWORD, "MSOUTLOOK_EMAIL": MSOUTLOOK_EMAIL,
        "SUB2API_URL": SUB2API_URL, "SUB2API_EMAIL": SUB2API_EMAIL,
        "SUB2API_PASSWORD": SUB2API_PASSWORD, "PROXY": PROXY,
        "MSOUTLOOK_HELPER_URL": MSOUTLOOK_HELPER_URL, "REDIRECT_URI": REDIRECT_URI,
    }
    missing = [k for k, v in placeholders.items() if not v or v.startswith("<")]
    if missing:
        print(f"[!] 以下参数未替换: {', '.join(missing)}")
        print("    打开 test_register_outlook.py 把 <填入...> 占位符替换为真实值后再跑。")
        sys.exit(1)


def main():
    _check_placeholders()

    print("=" * 50)
    print(f"测试: {PHONE} -> OAuth -> MsOutlook({MSOUTLOOK_EMAIL}) -> SUB2API")
    print("=" * 50)

    # ---- Step 1: 登录 SUB2API 获取 token ----
    print("\n[1/4] 登录 SUB2API ...")
    admin_token = login_sub2api()
    print(f"  admin_token: {admin_token[:30]}...")

    # ---- Step 2: 获取 OAuth URL ----
    print("\n[2/4] 获取 OAuth URL ...")
    oauth_info = get_oauth_url(admin_token)
    oauth_url = oauth_info["oauth_url"]
    session_id = oauth_info["session_id"]
    sub2api_state = parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
    print(f"  OAuth URL: {oauth_url[:100]}...")
    print(f"  Session ID: {session_id}")
    print(f"  State: {sub2api_state}")

    # ---- Step 3: 走 OAuth 流程 (绑定 MsOutlook 邮箱 + 验证码) ----
    print(f"\n[3/4] OAuth 流程: 手机号 {PHONE} 登录 -> 绑邮箱 {MSOUTLOOK_EMAIL} ...")
    from openai_bind_email import run_second_half

    # 关键参数说明:
    #   icloud_email = MSOUTLOOK_EMAIL
    #       run_second_half 在 add_email 分支用 icloud_email 作为绑定邮箱。
    #       不传或传空, send_bind_email 会触发 OpenAI 的 "empty string" 错误。
    #       (参数名历史遗留, 实际不限于 iCloud 域名。)
    #
    #   bind_code = ""
    #       留空让 _poll_bind_code -> MsOutlookPool.wait_for_code 自动拿新码。
    #       add_email 分支会先 send_bind_email 触发 OpenAI 发新码, 预填历史码
    #       会被作废。要预填 bind_code 只在 email_otp_verification 分支有效。
    result = run_second_half(
        oauth_url=oauth_url,
        phone=PHONE,
        password=PASSWORD,
        icloud_email=MSOUTLOOK_EMAIL,
        icloud_cookies={},
        sub2api_url=SUB2API_URL,
        sub2api_email=SUB2API_EMAIL,
        sub2api_password=SUB2API_PASSWORD,
        sub2api_proxy_id=0,
        proxy=PROXY,
        verbose=True,
        bind_code=BIND_CODE,
        imap_user="",
        imap_password="",
        sub2api_session_id=session_id,
        sub2api_state=sub2api_state,
        msoutlook_helper_url=MSOUTLOOK_HELPER_URL,
        msoutlook_email=MSOUTLOOK_EMAIL,
    )

    if not result.get("ok"):
        print(f"\n[FAIL] 流程失败: {result.get('error')}")
        sys.exit(1)

    # ---- Step 4: 结果 ----
    print("\n" + "=" * 50)
    print("[OK] 全流程完成!")
    print(f"  phone:   {PHONE}")
    print(f"  email:   {MSOUTLOOK_EMAIL}")
    print(f"  code:    {result.get('code', '')[:30]}...")
    if result.get("sub2api_account_id"):
        print(f"  SUB2API: id={result['sub2api_account_id']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
