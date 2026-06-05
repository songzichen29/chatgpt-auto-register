#!/usr/bin/env python3
"""
通过调用微软 token 端点，尝试多种策略刷新过期账号的 refresh_token。
参考 hotmail_helper.py 中的 TOKEN_ENDPOINTS。
"""

import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl

ACCOUNTS_FILE = r"D:\data\chatgpt-auto-register\号.json"
# 过期账号列表
EXPIRED_EMAILS = []  # ????????????????????

LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
ENTRA_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
ENTRA_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"

TOKEN_ENDPOINTS = [
    {"name": "live", "url": LIVE_TOKEN_URL, "extra_data": {}},
    {"name": "live-alt", "url": LIVE_TOKEN_URL, "extra_data": {}},
    {"name": "entra-consumers-delegated", "url": ENTRA_CONSUMERS_TOKEN_URL, "extra_data": {"scope": GRAPH_SCOPES}},
    {"name": "entra-common-delegated", "url": ENTRA_COMMON_TOKEN_URL, "extra_data": {"scope": GRAPH_SCOPES}},
    {"name": "entra-common-default", "url": ENTRA_COMMON_TOKEN_URL, "extra_data": {"scope": "https://graph.microsoft.com/.default"}},
]

REQUEST_TIMEOUT = 45


def post_form(url, data):
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(detail)
        except Exception:
            return {"error": detail, "raw": detail[:500]}


def try_refresh(client_id, refresh_token):
    results = []
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
            results.append({"endpoint": endpoint["name"], "ok": False, "error": str(exc)[:200]})
            continue

        access_token = str(payload.get("access_token") or "").strip()
        if access_token:
            return True, {
                "endpoint": endpoint["name"],
                "access_token": access_token,
                "next_refresh_token": str(payload.get("refresh_token") or "").strip(),
            }

        error = str(payload.get("error_description") or payload.get("error") or json.dumps(payload, ensure_ascii=False))[:300]
        results.append({"endpoint": endpoint["name"], "ok": False, "error": error})

    return False, results


def main():
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    account_map = {a["email"].lower(): a for a in accounts}

    print(f"尝试刷新 {len(EXPIRED_EMAILS)} 个过期账号的 token")
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

        if refresh_token in ("封禁", ""):
            print(f"[{i+1}/{len(EXPIRED_EMAILS)}] {email} [SKIP] refreshToken={refresh_token}")
            still_expired.append({"email": email, "reason": f"invalid_token:{refresh_token}"})
            continue

        print(f"[{i+1}/{len(EXPIRED_EMAILS)}] {email}")
        ok, result = try_refresh(client_id, refresh_token)
        if ok:
            print(f"  [OK] {result['endpoint']}")
            refreshed.append({
                "email": email,
                "endpoint": result["endpoint"],
                "next_refresh_token": result["next_refresh_token"],
            })
        else:
            reasons = [f"{r['endpoint']}: {r['error'][:100]}" for r in result]
            print(f"  [FAILED] {' | '.join(reasons[:2])}")
            still_expired.append({"email": email, "reasons": reasons})

        if i < len(EXPIRED_EMAILS) - 1:
            time.sleep(2)

    print("=" * 80)
    print(f"刷新成功: {len(refreshed)}")
    print(f"仍过期: {len(still_expired)}")

    if refreshed:
        # 更新号.json
        for item in refreshed:
            account = account_map.get(item["email"].lower())
            if account:
                account["refreshToken"] = item["next_refresh_token"]
                account["status"] = "authorized"
                print(f"  已更新: {item['email']}")

        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        print(f"号.json 已更新")

    # 保存结果
    output = {
        "refreshed": refreshed,
        "still_expired": still_expired,
    }
    with open(os.environ.get("EXPIRED_REFRESH_OUTPUT", "_expired_refresh_retry.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"详细结果保存到: _expired_refresh_retry.json")


if __name__ == "__main__":
    main()
