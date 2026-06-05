#!/usr/bin/env python3
"""
ChatGPT Auto Register - Fully automated phone-based registration.

Combines three independent techniques:
  1. curl_cffi   - Chrome TLS fingerprint (bypasses Cloudflare network layer)
  2. Sentinel    - FNV-1a Proof-of-Work (bypasses JS anti-bot challenges)
  3. SMSBower    - Automated SMS verification code retrieval

Usage:
  python auto_register.py                  # interactive mode
  python auto_register.py -n 5             # register 5 accounts
  python auto_register.py --gui            # start web GUI
"""

import argparse
import json
import os
import secrets
import string
import sys
import time as _time
from datetime import datetime
from pathlib import Path

from chatgpt_register import ChatGPTRegister
from phone_sms import PhoneSMS

import file_logger
file_logger.init()

# ============================================================
# 随机资料
# ============================================================

_FIRST_NAMES = [
    "James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Daniel",
    "Matthew","Anthony","Mark","Christopher","Paul","Steven","Andrew","Joshua","Kenneth","Kevin",
    "Brian","George","Timothy","Edward","Ronald","Jason","Jeffrey","Ryan","Jacob","Gary",
    "Nicholas","Eric","Stephen","Jonathan","Larry","Justin","Scott","Brandon","Frank","Raymond",
]
_LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Miller","Davis","Garcia","Rodriguez","Wilson",
    "Martinez","Anderson","Taylor","Thomas","Hernandez","Moore","Martin","Jackson","Thompson","White",
    "Lopez","Lee","Gonzalez","Harris","Clark","Lewis","Robinson","Walker","Perez","Hall",
    "Young","Allen","Sanchez","Wright","King","Scott","Green","Baker","Adams","Nelson",
]

def random_name() -> str:
    return f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}"

def random_birthdate() -> str:
    y = secrets.choice(range(1982, 2003))
    m = secrets.choice(range(1, 13))
    d = secrets.choice(range(1, 29))
    return f"{y:04d}-{m:02d}-{d:02d}"

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(chars) for _ in range(length))

def _retry_call(fn, max_retries=2, delay=2, label=""):
    """重试包装器 — 失败自动重试"""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_retries:
                raise
            if label:
                print(f"  [{label}] 失败 ({e})，{delay}s 后重试 ({attempt+1}/{max_retries})...")
            _time.sleep(delay)

def _cancel_with_eta(sms, phone: str, reason: str, verbose: bool = True):
    """同步取消号码并打印资金安全提示。

    hero-sms / SmsBower 协议要求拿号后 ≥150s 才能 setStatus=8，
    这里必须等待平台真正确认取消；否则短生命周期 worker 进程退出后，
    进程内取消队列会丢失，平台号码状态不会被修改。
    """
    try:
        wait_sec = float(sms.cancel_wait_seconds())
        if verbose and wait_sec > 0:
            print(f"  [sms] {phone} 将等待约 {wait_sec:.0f}s 后取消（{reason}）")
        ok = bool(sms.cancel_blocking())
    except Exception as exc:
        if verbose:
            print(f"  [sms] {phone} 取消异常（{reason}）: {exc}")
        return
    if verbose:
        if ok:
            print(f"  [sms] {phone} 已取消（{reason}）")
        else:
            print(f"  [sms] {phone} 取消未确认，可能需平台自然过期退款（{reason}）")

# ============================================================
# 配置
# ============================================================

def load_config(path: str = None) -> dict:
    config = {
        "sms_provider": "smsbower",
        "smsbower": {"api_key": ""},
        "hero_sms": {"api_key": "", "base_url": ""},
        "fivesim": {"api_key": ""},
        "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
        "proxy": "",
        "country": "151",
        "service": "dr",
        "code_timeout": 30,
    }
    candidates = [path, "config.json", str(Path(__file__).parent / "config.json")]
    found = {}
    for p in candidates:
        if p and Path(p).exists():
            with open(p, "r", encoding="utf-8") as f:
                found = json.load(f)
            if "sms_provider" in found:
                config["sms_provider"] = found["sms_provider"]
            for k in ["smsbower", "hero_sms", "fivesim"]:
                if k in found and isinstance(found[k], dict):
                    config[k].update(found[k])
            if "register" in found and isinstance(found["register"], dict):
                config["register"].update(found["register"])
            for k in ["proxy", "country", "service", "code_timeout"]:
                if k in found:
                    config[k] = found[k]
            # Passthrough extra keys (e.g. "gui", "phase2")
            for k, v in found.items():
                if k not in {"sms_provider", "smsbower", "hero_sms", "fivesim",
                             "register", "proxy", "country", "service", "code_timeout"}:
                    config[k] = v
            break
    if os.environ.get("SMSBOWER_KEY"):
        config["smsbower"]["api_key"] = os.environ["SMSBOWER_KEY"]
    proxy_env = os.environ.get("PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_env:
        config["proxy"] = proxy_env
    return config


def _get_sms_api_key(config: dict, provider: str) -> str:
    """根据 provider 名称从 config 中获取对应的 API Key"""
    provider_map = {
        "smsbower": "smsbower",
        "hero-sms": "hero_sms",
        "5sim": "fivesim",
    }
    section = provider_map.get(provider, provider)
    return config.get(section, {}).get("api_key", "") or config.get("smsbower", {}).get("api_key", "")


# ============================================================
# 注册核心
# ============================================================

def register_one(
    config: dict,
    provider_ids: str = "",
    max_price: str = "",
    verbose: bool = True,
    step_retries: int = 2,
    create_account_max_retries: int = 20,
    auto_activate: bool = True,
    existing_phone: dict = None,
    otp_max_retries: int = 3,
) -> dict:
    """注册一个账号。使用 PhoneSMS 统一接口，支持 smsbower / hero-sms / 5sim。

    auto_activate=False 时不自动激活，由调用方决定激活或取消（避免 Phase 2 失败浪费号码）。

    existing_phone: 可选，传入已有手机号信息 {"phone": "+xxx", "activation_id": "xxx", "password": "xxx"}。
        传入时跳过 get_number 步骤，复用该号码完成注册流程（Phase 2 邮箱碰撞时从 Phase 1 重跑）。
    """
    service = config["service"]
    country = config["country"]
    reg_cfg = config["register"]
    sms_provider = config.get("sms_provider", "smsbower")

    # 如果传入了 existing_phone，复用密码；否则生成新密码
    if existing_phone and existing_phone.get("password"):
        password = existing_phone["password"]
    else:
        password = reg_cfg["password"] or random_password()
    name = reg_cfg.get("name") or random_name()
    birthdate = reg_cfg.get("birthdate") or random_birthdate()
    if name == "A" and birthdate == "2000-01-01":
        name = random_name()
        birthdate = random_birthdate()

    phone = "?"
    aid = ""
    reg = None
    sr = step_retries

    # 根据配置的 provider 创建 PhoneSMS
    sms = PhoneSMS(sms_provider, _get_sms_api_key(config, sms_provider))

    try:
        if existing_phone:
            # 复用已有手机号（Phase 2 邮箱碰撞重跑 Phase 1）
            aid = existing_phone["activation_id"]
            phone = existing_phone["phone"]
            if verbose:
                print(f"  复用手机号: {phone}  激活ID: {aid}  [平台:{sms_provider}]")
        else:
            aid, phone_raw = sms.get_number(service=service, country=country)
            phone = "+" + phone_raw if not phone_raw.startswith("+") else phone_raw
            if verbose:
                print(f"  手机号: {phone}  激活ID: {aid}  [平台:{sms_provider}]")

        def _start_registration_session(reason: str = "") -> tuple[ChatGPTRegister, str]:
            if verbose and reason:
                print(f"  [session] {reason}，重新开始登录会话")
            reg_obj = ChatGPTRegister(proxy=config["proxy"])
            _retry_call(lambda: reg_obj.visit(), sr, label="访问首页")
            csrf = _retry_call(lambda: reg_obj.get_csrf(), sr, label="CSRF")
            redirect = _retry_call(lambda: reg_obj.signin(phone, csrf), sr, label="发起登录")
            if not redirect:
                raise RuntimeError("signin 无返回(可能被 Cloudflare 拦截)")
            _retry_call(lambda: reg_obj.jump_to_auth(redirect), sr, label="OAuth跳转")
            register_result = _retry_call(lambda: reg_obj.register_user(phone, password), sr, label="注册")
            next_url = register_result.get("continue_url", "")
            if not next_url:
                raise RuntimeError(f"注册被拒(status={register_result.get('_status')})")
            return reg_obj, next_url

        try:
            reg, continue_url = _start_registration_session()
        except Exception as exc:
            if auto_activate:
                _cancel_with_eta(sms, phone, "注册会话失败", verbose)
            return {"ok": False, "phone": phone, "password": password, "activation_id": aid, "error": str(exc)}

        # ---- OTP 验证 ----
        # 保存注册步骤的 continue_url，校验失败重试时需要重新触发 send_otp
        send_otp_url = continue_url
        # 首次发送验证码
        _retry_call(lambda u=send_otp_url: reg.send_otp(u), sr, label="发送验证码")
        if verbose:
            print(f"  验证码已发送到 {phone}")

        code = sms.wait_code(timeout=config["code_timeout"])
        if not code:
            # 验证码超时：号码收不到验证码，重发也没用，加入延迟取消队列（≥150s 后 setStatus=8 退款）
            if auto_activate:
                _cancel_with_eta(sms, phone, "验证码超时", verbose)
            return {"ok": False, "phone": phone, "password": password, "activation_id": aid, "error": "验证码超时"}

        if verbose:
            print(f"  收到验证码: {code}")

        # 校验验证码，失败时通过 status=3 重发短信并重试
        _last_otp_error = ""
        used_otp_codes = {str(code).strip()} if code else set()
        for otp_attempt in range(otp_max_retries):
            result = _retry_call(lambda c=code: reg.validate_otp(c), sr, label="校验验证码")
            continue_url = result.get("continue_url", "")
            if continue_url:
                break  # 校验成功，跳出循环

            # 校验失败：请求 SMS 平台重发短信 (status=3)，复用同一号码
            detail = result.get("_error") or result.get("_body") or ""
            _last_otp_error = f"验证码校验失败(status={result.get('_status')})"
            if detail:
                _last_otp_error += f": {detail[:160]}"
            try:
                status_code = int(result.get("_status") or 0)
            except Exception:
                status_code = 0

            detail_lc = detail.lower()
            session_invalid = (
                status_code == 409
                and (
                    "session is no longer valid" in detail_lc
                    or "start over" in detail_lc
                    or "invalid authorization step" in detail_lc
                )
            )
            if session_invalid:
                if otp_attempt < otp_max_retries - 1:
                    used_otp_codes.add(str(code).strip())
                    try:
                        reg, send_otp_url = _start_registration_session("OTP 会话失效")
                        _retry_call(lambda u=send_otp_url: reg.send_otp(u), sr, label="重新发送验证码")
                    except Exception as exc:
                        _last_otp_error = f"OTP 会话重建失败: {exc}"
                        break
                    if verbose:
                        print(
                            f"  {_last_otp_error}，[OTP重试 {otp_attempt + 1}/{otp_max_retries - 1}] "
                            f"已重建登录会话并请求新验证码到 {phone}"
                        )
                    code = sms.wait_code(timeout=config["code_timeout"], exclude_codes=list(used_otp_codes))
                    if not code:
                        _last_otp_error = "会话重建后验证码超时"
                        break
                    used_otp_codes.add(str(code).strip())
                    if verbose:
                        print(f"  收到验证码: {code}")
                    continue
                if verbose:
                    print(f"  {_last_otp_error}，已用完重试次数")
                break

            # status=0 表示 validate 请求本身没有拿到 HTTP 响应，多半是连接/TLS/超时等
            # 传输层异常；这时不能把它当成"验证码错误"去请求短信平台重发，
            # 否则会浪费已收到的正确验证码，并让平台停在 STATUS_WAIT_RETRY。
            if status_code == 0:
                if otp_attempt < otp_max_retries - 1:
                    if verbose:
                        print(
                            f"  {_last_otp_error}，[OTP重试 {otp_attempt + 1}/{otp_max_retries - 1}] "
                            "未收到验证接口响应，继续使用同一验证码重试校验"
                        )
                    _time.sleep(2)
                    continue
                if verbose:
                    print(f"  {_last_otp_error}，已用完重试次数")
                break

            if otp_attempt < otp_max_retries - 1:
                sms.resend()
                _retry_call(lambda: reg.send_otp(send_otp_url), sr, label="重新发送验证码")
                if verbose:
                    print(f"  {_last_otp_error}，[OTP重试 {otp_attempt + 1}/{otp_max_retries - 1}] 已请求重发验证码到 {phone}")
                code = sms.wait_code(timeout=config["code_timeout"], exclude_codes=list(used_otp_codes))
                if not code:
                    _last_otp_error = "重发后验证码超时"
                    break  # 重发后仍收不到，不再继续
                used_otp_codes.add(str(code).strip())
                if verbose:
                    print(f"  收到验证码: {code}")
            else:
                if verbose:
                    print(f"  {_last_otp_error}，已用完重试次数")

        if not continue_url:
            if auto_activate:
                _cancel_with_eta(sms, phone, "OTP 失败", verbose)
            return {"ok": False, "phone": phone, "password": password, "activation_id": aid, "error": f"{_last_otp_error}(已重试{otp_max_retries}次)"}

        # ============================================================
        # 先访问 about-you 页面建立会话上下文
        # ============================================================
        _retry_call(lambda: reg.visit_about_you(continue_url), sr, label="访问about-you")

        # ============================================================
        # 创建账户 (用户名+生日) — 最多重试 create_account_max_retries 次
        # ============================================================
        last_create_error = ""
        for ca_attempt in range(create_account_max_retries):
            ca_name = random_name() if ca_attempt > 0 else name
            ca_birthdate = random_birthdate() if ca_attempt > 0 else birthdate
            if verbose:
                print(f"  创建账户 [{ca_attempt+1}/{create_account_max_retries}]: name={ca_name} birthdate={ca_birthdate}")

            result = reg.create_account(ca_name, ca_birthdate)
            callback_url = result.get("continue_url", "")
            if callback_url:
                name = ca_name
                birthdate = ca_birthdate
                break

            last_create_error = result.get("_body", "") or f"status={result.get('_status')}"
            if verbose:
                detail = last_create_error[:200]
                print(f"  创建账户失败 [{ca_attempt+1}]: {detail}")
            if ca_attempt < create_account_max_retries - 1:
                _time.sleep(1)

        if not callback_url:
            if auto_activate:
                _cancel_with_eta(sms, phone, "创建账户失败", verbose)
            return {"ok": False, "phone": phone, "password": password, "activation_id": aid, "error": f"创建账户失败(已重试{create_account_max_retries}次): {last_create_error[:200]}"}

        token = _retry_call(lambda: reg.oauth_callback(callback_url), sr, label="OAuth回调")
        access_token = _retry_call(lambda: reg.get_access_token(), sr, label="获取Token")
        if auto_activate:
            try:
                sms.complete()
            except Exception as e:
                if verbose:
                    print(f"  [WARNING] 激活失败: {e}")
        else:
            _log_info = True
            if verbose:
                print("  [注册] 跳过激活（由调用方控制）")

        return {
            "ok": True, "phone": phone, "password": password,
            "name": name, "birthdate": birthdate,
            "session_token": token, "access_token": access_token, "activation_id": aid,
        }

    except Exception as e:
        if auto_activate:
            try: sms.cancel_blocking()
            except Exception: pass
        return {"ok": False, "phone": phone, "password": password, "activation_id": aid, "error": str(e)}

# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="ChatGPT 自动注册")
    parser.add_argument("--config", "-c", type=str, help="配置文件路径")
    parser.add_argument("--count", "-n", type=int, default=1, help="目标成功数量")
    parser.add_argument("--sms-provider", type=str, default="",
                        choices=["smsbower", "hero-sms", "5sim"],
                        help="接码平台 (默认 smsbower)")
    parser.add_argument("--country", type=str, help="国家 ID (默认 151=智利)")
    parser.add_argument("--service", type=str, help="服务代码 (默认 dr=OpenAI)")
    parser.add_argument("--provider", type=str, default="", help="指定运营商 ID")
    parser.add_argument("--max-price", type=str, default="", help="最高价格")
    parser.add_argument("--proxy", type=str, help="代理地址")
    parser.add_argument("--password", type=str, help="密码 (留空随机)")
    parser.add_argument("--retry", "-r", type=int, default=2, help="各步骤重试次数")
    parser.add_argument("--create-retry", type=int, default=20, help="创建账户重试次数 (默认20)")
    parser.add_argument("--output", "-o", type=str, default="register_results.json")
    parser.add_argument("--gui", action="store_true", help="启动 Web GUI")
    # Phase 2
    parser.add_argument("--phase2", action="store_true")
    parser.add_argument("--bind-email", type=str)
    parser.add_argument("--icloud-cookies", type=str)
    parser.add_argument("--sub2api-url", type=str)
    parser.add_argument("--sub2api-email", type=str)
    parser.add_argument("--sub2api-pwd", type=str)
    parser.add_argument("--sub2api-proxy-id", type=int, default=0)
    parser.add_argument("--sub2api-group-id", type=int, default=1)

    args = parser.parse_args()

    if args.gui:
        from web_gui import start_gui
        start_gui()
        return

    config = load_config(args.config)
    if args.sms_provider: config["sms_provider"] = args.sms_provider
    if args.country: config["country"] = args.country
    if args.service: config["service"] = args.service
    if args.proxy: config["proxy"] = args.proxy
    if args.password: config["register"]["password"] = args.password

    provider = config.get("sms_provider", "smsbower")
    api_key = _get_sms_api_key(config, provider)
    if not api_key:
        print(f"错误: 需要 {provider} API Key (请在 config.json 中配置).")
        sys.exit(1)

    sms = PhoneSMS(provider, api_key)
    bal = sms.client.get_balance()
    try:
        pid, price = sms.get_cheapest_provider(config["service"], config["country"])
    except Exception:
        pid, price = "?", 0
    print(f"余额: {bal}  平台: {provider}  国家: {config['country']}  运营商: {pid} (${price:.4f})")
    print(f"代理: {config['proxy'] or '直连'}  目标: {args.count}个")
    print("-" * 50)

    results = []
    ok_count = 0
    attempt = 0
    max_attempts = args.count * 10

    while ok_count < args.count and attempt < max_attempts:
        attempt += 1
        print(f"\n第 {attempt} 次 [{ok_count}/{args.count}]")
        try:
            result = register_one(config, provider_ids=args.provider,
                                  max_price=args.max_price, step_retries=args.retry,
                                  create_account_max_retries=args.create_retry,
                                  verbose=True)
        except Exception as e:
            result = {"ok": False, "phone": "?", "error": str(e)}
        results.append(result)
        if result["ok"]:
            ok_count += 1
            phone = result.get("phone", "?")
            token = result.get("session_token", "")
            at = result.get("access_token", "")
            print(f"  成功: {phone}  名称: {result.get('name','?')}")
            if args.phase2 and args.sub2api_url:
                try:
                    from phase2_codex import upload_session
                    upload_session(token, args.bind_email or "", args.sub2api_url,
                                   args.sub2api_email, args.sub2api_pwd,
                                   sub2api_proxy_id=args.sub2api_proxy_id,
                                   group_ids=[args.sub2api_group_id], access_token=at)
                    print(f"  已上传到 SUB2API")
                except Exception as e:
                    print(f"  上传失败: {e}")
        else:
            print(f"  失败: {result.get('phone','?')} - {result.get('error','?')}")

    if ok_count < args.count:
        print(f"\n注意: 仅成功 {ok_count}/{args.count} (已达最大尝试次数)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = Path(args.output)
    output_path = raw.parent / f"{raw.stem}_{ts}{raw.suffix}"
    safe = [dict(r) for r in results if r.get("ok")]
    output_path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n已保存 {len(safe)} 条结果到 {output_path}")

if __name__ == "__main__":
    main()
