#!/usr/bin/env python3
"""用刷新后的新 token 更新号.json。"""

import json

ACCOUNTS_FILE = r"D:\data\chatgpt-auto-register\号.json"
TOKEN_CHECK_FILE = r"D:\data\chatgpt-auto-register\_unused_token_check.json"
BACKUP_FILE = r"D:\data\chatgpt-auto-register\号.json.backup"


def main():
    # 加载原始账号
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    # 加载 token 检查结果
    with open(TOKEN_CHECK_FILE, "r", encoding="utf-8") as f:
        check_result = json.load(f)

    # 构建 email -> next_refresh_token 映射
    new_tokens = {}
    for item in check_result.get("valid", []):
        new_tokens[item["email"].lower()] = item["next_refresh_token"]

    print(f"原始账号数: {len(accounts)}")
    print(f"可更新的 token 数: {len(new_tokens)}")

    # 先备份
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    print(f"已备份到: {BACKUP_FILE}")

    # 更新
    updated_count = 0
    not_found_count = 0
    for account in accounts:
        email = account.get("email", "").lower()
        if email in new_tokens:
            old_token = account.get("refreshToken", "")
            account["refreshToken"] = new_tokens[email]
            account["status"] = "authorized"
            updated_count += 1

    print(f"更新了 {updated_count} 个账号的 refreshToken")
    print(f"更新了 {updated_count} 个账号的 status 为 authorized")

    # 保存
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    print(f"已保存到: {ACCOUNTS_FILE}")


if __name__ == "__main__":
    main()
