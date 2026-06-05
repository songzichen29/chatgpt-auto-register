#!/usr/bin/env python3
"""检查未使用账号的 token 是否过期，并根据需要刷新 token。"""

import json
import sys
import os
import time
import urllib.request
import urllib.parse
import urllib.error

# 号.json 路径
ACCOUNTS_FILE = r"D:\data\chatgpt-auto-register\号.json"
# 已使用账号文件
USED_FILE = r"D:\data\chatgpt-auto-register\msoutlook_used.json"
# hotmail_helper 端口
HELPER_PORT = 17373
# token 刷新端点
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
ENTRA_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
ENTRA_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"

TOKEN_ENDPOINTS = [
    {"name": "live", "url": LIVE_TOKEN_URL, "extra_data": {}},
    {"name": "entra-consumers-delegated", "url": ENTRA_CONSUMERS_TOKEN_URL, "extra_data": {"scope": GRAPH_SCOPES}},
    {"name": "entra-common-delegated", "url": ENTRA_COMMON_TOKEN_URL, "extra_data": {"scope": GRAPH_SCOPES}},
]

REQUEST_TIMEOUT = 45


def mask_secret(value, keep=6):
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= keep:
        return "*" * len(raw)
    return raw[:keep] + "..." + raw[-keep:]


def post_form(url, data):
    """发送 application/x-www-form-urlencoded 请求。"""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(detail)
        except Exception:
            return {"error": detail}


def try_refresh_token(client_id, refresh_token):
    """尝试用多个端点刷新 token，返回 (True, token_payload) 或 (False, error)。"""
    for endpoint in TOKEN_ENDPOINTS:
        request_data = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            **(endpoint.get("extra_data") or {}),
        }
        try:
            payload = post_form(endpoint["url"], request_data)
        except Exception as exc:
            print(f"  [{endpoint['name']}] 网络错误: {exc}")
            continue

        access_token = str(payload.get("access_token") or "").strip()
        if access_token:
            return True, {
                "endpoint": endpoint["name"],
                "access_token": access_token,
                "next_refresh_token": str(payload.get("refresh_token") or "").strip(),
            }

        error = payload.get("error_description") or payload.get("error") or json.dumps(payload, ensure_ascii=False)
        print(f"  [{endpoint['name']}] 失败: {str(error)[:200]}")

    return False, "所有端点都刷新失败"


def load_used_emails():
    """加载已使用账号的 email 列表。"""
    if not os.path.exists(USED_FILE):
        return set()
    with open(USED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # msoutlook_used.json 格式可能是数组或字典
    if isinstance(data, list):
        return {item.get("email", "").lower() for item in data if item.get("email")}
    elif isinstance(data, dict):
        return {item.get("email", "").lower() for item in data.values() if isinstance(item, dict) and item.get("email")}
    return set()


def main():
    # 加载所有账号
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    print(f"总账号数: {len(accounts)}")

    # 加载已使用账号
    used_emails = load_used_emails()
    print(f"已使用账号数: {len(used_emails)}")

    # 找出未使用的账号
    unused_accounts = []
    for account in accounts:
        email = account.get("email", "").lower()
        if email not in used_emails:
            unused_accounts.append(account)

    print(f"未使用账号数: {len(unused_accounts)}")
    print("-" * 80)

    # 检查每个未使用账号的 token
    results = {
        "valid": [],
        "expired": [],
        "error": [],
    }

    for i, account in enumerate(unused_accounts):
        email = account.get("email", "")
        client_id = account.get("clientId", "")
        refresh_token = account.get("refreshToken", "")
        status = account.get("status", "")

        print(f"[{i+1}/{len(unused_accounts)}] {email} (status={status})")

        if not refresh_token:
            print(f"  [SKIP] 没有 refreshToken")
            results["error"].append({"email": email, "reason": "no_refresh_token"})
            continue

        ok, result = try_refresh_token(client_id, refresh_token)
        if ok:
            print(f"  [OK] token 有效，端点={result['endpoint']}")
            results["valid"].append({
                "email": email,
                "endpoint": result["endpoint"],
                "next_refresh_token": result["next_refresh_token"],
            })
        else:
            print(f"  [EXPIRED] token 已过期或无效: {result}")
            results["expired"].append({
                "email": email,
                "reason": result,
            })

        # 避免请求过快
        if i < len(unused_accounts) - 1:
            time.sleep(1)

    # 输出汇总
    print("=" * 80)
    print(f"汇总:")
    print(f"  有效 token: {len(results['valid'])}")
    print(f"  过期 token: {len(results['expired'])}")
    print(f"  错误/跳过: {len(results['error'])}")

    # 保存结果
    output_file = r"D:\data\chatgpt-auto-register\_unused_token_check.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已保存到: {output_file}")


if __name__ == "__main__":
    main()
