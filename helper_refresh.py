#!/usr/bin/env python3
"""
通过 hotmail_helper 服务刷新过期账号的 token。
服务需要先启动: start-hotmail-helper.bat
"""

import json
import os
import time
import urllib.request
import urllib.error

HELPER_URL = "http://127.0.0.1:17373/messages"
ACCOUNTS_FILE = r"D:\data\chatgpt-auto-register\号.json"

# 真正过期的账号（排除 refreshToken=封禁 的）
EXPIRED_EMAILS = []  # ????????????????????

REQUEST_TIMEOUT = 90


def call_helper(email, client_id, refresh_token):
    """调用 hotmail_helper /messages 接口刷新 token。"""
    payload = json.dumps({
        "email": email,
        "clientId": client_id,
        "refreshToken": refresh_token,
        "top": 1,
    }).encode("utf-8")
    request = urllib.request.Request(
        HELPER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("ok"):
                return True, {
                    "next_refresh_token": data.get("nextRefreshToken", ""),
                    "token_endpoint": data.get("tokenEndpoint", ""),
                    "transport": data.get("transport", ""),
                }
            else:
                return False, data.get("error", "unknown")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        return False, f"HTTP {exc.code}: {body}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)[:500]


def main():
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    account_map = {a["email"].lower(): a for a in accounts}

    print(f"通过 hotmail_helper 刷新 {len(EXPIRED_EMAILS)} 个过期账号的 token")
    print(f"服务地址: {HELPER_URL}")
    print("=" * 80)

    refreshed = []
    still_expired = []

    for i, email in enumerate(EXPIRED_EMAILS):
        account = account_map.get(email.lower())
        if not account:
            print(f"[{i+1}/{len(EXPIRED_EMAILS)}] {email} [SKIP] 不在号.json中")
            still_expired.append({"email": email, "reason": "not_found"})
            continue

        client_id = account.get("clientId", "")
        refresh_token = account.get("refreshToken", "")

        if refresh_token == "封禁":
            print(f"[{i+1}/{len(EXPIRED_EMAILS)}] {email} [SKIP] 已封禁")
            still_expired.append({"email": email, "reason": "banned"})
            continue

        print(f"[{i+1}/{len(EXPIRED_EMAILS)}] {email}", end=" ... ", flush=True)
        ok, result = call_helper(email, client_id, refresh_token)
        if ok:
            print(f"[OK] endpoint={result['token_endpoint']} transport={result['transport']}")
            refreshed.append({
                "email": email,
                "next_refresh_token": result["next_refresh_token"],
                "endpoint": result["token_endpoint"],
                "transport": result["transport"],
            })
        else:
            print(f"[FAIL] {str(result)[:200]}")
            still_expired.append({"email": email, "reason": str(result)[:300]})

        if i < len(EXPIRED_EMAILS) - 1:
            time.sleep(2)

    print("=" * 80)
    print(f"刷新成功: {len(refreshed)}")
    print(f"仍过期: {len(still_expired)}")

    if refreshed:
        for item in refreshed:
            account = account_map.get(item["email"].lower())
            if account:
                account["refreshToken"] = item["next_refresh_token"]
                account["status"] = "authorized"
                print(f"  已更新: {item['email']} (endpoint={item['endpoint']})")

        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        print(f"号.json 已更新")

    output = {
        "refreshed": refreshed,
        "still_expired": still_expired,
    }
    with open(os.environ.get("HELPER_REFRESH_OUTPUT", "_helper_refresh_result.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"结果保存到: _helper_refresh_result.json")


if __name__ == "__main__":
    main()
