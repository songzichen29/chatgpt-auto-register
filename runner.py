"""Registration engine - thread-safe, multi-user, with SSE streaming + Phase 2"""

import json
import threading
import time
import queue
from typing import Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests as _req_lib

import auto_register as ar
from phone_sms import PhoneSMS
from msoutlook_pool import MsOutlookPool

import db
import file_logger

# ── Global locks ──
icloud_lock = threading.Lock()
mailmanage_lock = threading.Lock()

# ── Active runners per user ──
active_runners: dict = {}  # user_id → {"thread": Thread, "stop": threading.Event}


def get_email_for_user(user_id: int, sse_q: queue.Queue) -> str:
    """Get email via iCloud (paid) / MsOutlook / MailManage (free). Returns email or raises."""
    icloud = db.check_icloud_access(user_id)
    if icloud and icloud.get("remaining_uses", 0) > 0:
        _sse_log(sse_q, "Using iCloud alias (paid)...", "info")
        with icloud_lock:
            try:
                from icloud_hme import ICloudHME
                cookies_raw = db.get_admin_asset("icloud_cookies")
                if not cookies_raw:
                    raise RuntimeError("Admin iCloud cookies not configured")
                c = json.loads(cookies_raw)
                ic = ICloudHME(c, verbose=False)
                alias = ic.create_alias()
                db.consume_icloud_use(icloud["id"])
                return alias
            except Exception as e:
                db.consume_icloud_use(icloud["id"])
                raise RuntimeError(f"iCloud failed: {e}")
    else:
        # 回退: msoutlook 号池 (free)
        ms_helper_url = db.get_admin_asset("msoutlook_helper_url") or ""
        if ms_helper_url:
            try:
                _sse_log(sse_q, "Using MsOutlook email (free)...", "info")
                pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                email = pool.get_available_email()
                if email:
                    _sse_log(sse_q, f"MsOutlook: {email}", "success")
                    return email
            except Exception as e:
                _sse_log(sse_q, f"MsOutlook failed: {e}, falling back to MailManage", "warn")

        # 最终回退: MailManage (free)
        _sse_log(sse_q, "Using MailManage email (free)...", "info")
        with mailmanage_lock:
            from mailmanage_client import MailManageClient
            mm_key = db.get_admin_asset("mailmanage_key") or ""
            if not mm_key:
                raise RuntimeError("Neither iCloud, MsOutlook nor MailManage configured")
            mm = MailManageClient(api_key=mm_key, verbose=False)
            email = mm.get_available_email(category="free")
            if not email:
                raise RuntimeError("No MailManage email available")
            return email


def start(user_id: int, count: int) -> str:
    """Start Phase 1+2 registration for a user. Returns 'ok' or error string."""
    if user_id in active_runners:
        return "Already running"

    sse_q = get_sse_queue(user_id)
    stop_ev = threading.Event()

    thr = threading.Thread(target=_run, args=(user_id, count, sse_q, stop_ev), daemon=True)
    active_runners[user_id] = {"thread": thr, "stop": stop_ev}
    thr.start()
    return "ok"


def start_phase2(user_id: int, phone: str, password: str, bind_email: str) -> str:
    """Start Phase 2 Only: use existing phone+password to run OAuth + bind email + SUB2API."""
    if user_id in active_runners:
        return "Already running"

    sse_q = get_sse_queue(user_id)
    stop_ev = threading.Event()

    thr = threading.Thread(target=_run_phase2_only, args=(user_id, phone, password, bind_email, sse_q, stop_ev), daemon=True)
    active_runners[user_id] = {"thread": thr, "stop": stop_ev}
    thr.start()
    return "ok"


def stop(user_id: int):
    if user_id in active_runners:
        active_runners[user_id]["stop"].set()


def stop_phase2(user_id: int):
    if user_id in active_runners:
        active_runners[user_id]["stop"].set()


# ── Phase 2 Only runner ──
def _run_phase2_only(user_id: int, phone: str, password: str, bind_email: str,
                     sse_q: queue.Queue, stop_ev: threading.Event):
    """Run Phase 2 only: existing phone+password → OAuth → bind email → SUB2API."""
    config_data = db.get_user_config(user_id)
    proxy = config_data.get("proxy", "") or "socks5h://127.0.0.1:10808"
    sub_url = config_data.get("sub2api_url", "") or ""
    sub_email = config_data.get("sub2api_email", "") or ""
    sub_pwd = config_data.get("sub2api_password", "") or ""
    sub_proxy_id = config_data.get("sub2api_proxy_id", 0) or 0
    ms_helper_url = db.get_admin_asset("msoutlook_helper_url") or ""

    if not (sub_url and sub_email and sub_pwd):
        _sse_log(sse_q, "请先配置 SUB2API 地址/邮箱/密码", "error")
        _cleanup_phase2_only(user_id)
        return

    _sse_log(sse_q, f"Phase 2 Only: {phone} -> {bind_email}", "info")

    try:
        phase2_ok = _run_phase2(
            sse_q,
            {"phone": phone, "password": password, "session_token": "", "access_token": ""},
            bind_email, sub_url, sub_email, sub_pwd, sub_proxy_id, "CHATGPT",
            proxy, ms_helper_url, "", user_id, None, "", "",
        )
        if phase2_ok:
            db.save_account(user_id, phone, password, bind_email, status="ok")
            _sse_log(sse_q, "Phase 2 完成!", "success")
        else:
            db.save_account(user_id, phone, password, bind_email, status="fail_phase2")
            _sse_log(sse_q, "Phase 2 失败", "error")
    except Exception as e:
        _sse_log(sse_q, f"Phase 2 error: {e}", "error")

    _cleanup_phase2_only(user_id)


def _cleanup_phase2_only(user_id: int):
    if user_id in active_runners:
        del active_runners[user_id]


def is_running(user_id: int) -> bool:
    r = active_runners.get(user_id)
    return r is not None and r["thread"].is_alive()


# ── SSE queues ──
_sse_queues: dict = {}

def get_sse_queue(user_id: int) -> queue.Queue:
    if user_id not in _sse_queues:
        _sse_queues[user_id] = queue.Queue()
    return _sse_queues[user_id]


# ── Internal runner ──
def _ts():
    return time.strftime("%H:%M:%S")


def _sse_log(sse_q: queue.Queue, msg: str, tag: str = "info"):
    """发送到 SSE 队列并同步写入文件日志。"""
    sse_q.put({"msg": msg, "tag": tag, "time": _ts()})
    try:
        file_logger.write_log(tag, "runner", msg)
    except Exception:
        pass


# ─ Phase 2 runner ──
def _run_phase2(sse_q, phase1_result, bind_email,
                sub_url, sub_email, sub_pwd, sub_proxy_id, sub_group,
                proxy, ms_helper_url, phone_aid, user_id,
                sms_obj, api_key, provider) -> bool:
    """Run Phase 2: OAuth + bind email + SUB2API upload. Returns True on success."""
    import urllib.parse as _up

    try:
        # [1/4] Login SUB2API
        _sse_log(sse_q, "  [1/4] 登录 SUB2API ...", "info")
        r = _req_lib.post(f"{sub_url}/api/v1/auth/login",
                          json={"email": sub_email, "password": sub_pwd}, timeout=15)
        login_data = r.json()
        if login_data.get("code") != 0:
            raise RuntimeError(f"SUB2API登录失败: {login_data.get('message','?')}")
        admin_token = login_data["data"]["access_token"]

        # [2/4] Get OAuth URL
        _sse_log(sse_q, "  [2/4] 获取 OAuth URL ...", "info")
        r = _req_lib.post(f"{sub_url}/api/v1/admin/openai/generate-auth-url",
                          json={"redirect_uri": "http://localhost:1455/auth/callback"},
                          headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
        oauth_data = r.json()
        if oauth_data.get("code") != 0:
            raise RuntimeError(f"获取OAuth URL失败: {oauth_data.get('message','?')}")
        oauth_url = oauth_data["data"]["auth_url"]
        session_id = oauth_data["data"]["session_id"]
        oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]

        # [3/4] Phase 2 with retry
        from openai_bind_email import run_second_half
        max_retries = 3
        current_email = bind_email
        oauth_result = None
        phase1_done_time = time.time()

        for _retry in range(1, max_retries + 1):
            if _retry > 1:
                _sse_log(sse_q, f"  [3/4] Phase2 重试 {_retry}/{max_retries}", "warn")

            oauth_result = run_second_half(
                oauth_url=oauth_url,
                phone=phase1_result["phone"],
                password=phase1_result["password"],
                icloud_email=current_email,
                icloud_cookies={},
                sub2api_url=sub_url,
                sub2api_email=sub_email,
                sub2api_password=sub_pwd,
                sub2api_proxy_id=sub_proxy_id,
                proxy=proxy,
                verbose=False,
                sub2api_session_id=session_id,
                sub2api_state=oauth_state,
                msoutlook_helper_url=ms_helper_url,
                msoutlook_email=current_email,
            )
            if oauth_result.get("ok"):
                break

            err = oauth_result.get("error", "")
            _sse_log(sse_q, f"  Phase2 失败: {err[:150]}", "error")

            if "email_already_in_use" in err:
                _sse_log(sse_q, f"  邮箱已被占用: {current_email}，换新邮箱重跑 Phase 2...", "warn")
                # Mark current email used
                if ms_helper_url:
                    try:
                        pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                        pool.mark_used(current_email)
                    except Exception:
                        pass
                # Get new email for this user
                try:
                    new_email = get_email_for_user(user_id, sse_q)
                    if new_email:
                        current_email = new_email
                        _sse_log(sse_q, f"  新邮箱: {current_email}，重跑 Phase 2...", "info")
                        # Re-login SUB2API and get new OAuth URL
                        r = _req_lib.post(f"{sub_url}/api/v1/auth/login",
                                          json={"email": sub_email, "password": sub_pwd}, timeout=15)
                        login_data = r.json()
                        if login_data.get("code") != 0:
                            raise RuntimeError(f"SUB2API登录失败: {login_data.get('message','?')}")
                        admin_token = login_data["data"]["access_token"]
                        r = _req_lib.post(f"{sub_url}/api/v1/admin/openai/generate-auth-url",
                                          json={"redirect_uri": "http://localhost:1455/auth/callback"},
                                          headers={"Authorization": f"Bearer {admin_token}"}, timeout=30)
                        oauth_data = r.json()
                        if oauth_data.get("code") != 0:
                            raise RuntimeError(f"获取OAuth URL失败: {oauth_data.get('message','?')}")
                        oauth_url = oauth_data["data"]["auth_url"]
                        session_id = oauth_data["data"]["session_id"]
                        oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]
                        # Continue loop: Phase 2 retry with new email
                        continue
                    else:
                        _sse_log(sse_q, "  号池无可用邮箱", "error")
                        break
                except Exception as e:
                    _sse_log(sse_q, f"  换邮箱失败: {e}", "error")
                    break
            elif "account_stuck_email_otp" in err:
                # 账号卡在旧邮箱验证状态，无法重新发码，只能换手机号
                _sse_log(sse_q, f"  账号卡在 email_otp_verification 状态，换手机号重跑...", "warn")
                # Mark current email used (the old bound email is wasted)
                if ms_helper_url:
                    try:
                        pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                        pool.mark_used(current_email)
                    except Exception:
                        pass
                # 释放当前手机号
                if phone_aid and sms_obj:
                    try:
                        sms_obj.cancel(phone_aid)
                        _sse_log(sse_q, f"  [取消] 释放当前手机号: {phase1_result.get('phone','?')}", "warn")
                    except Exception:
                        pass
                # 返回 False，让上层拿新手机号重试
                return False
            elif "account_requires_contact_verification" in err:
                _sse_log(sse_q, "  账号仍要求手机二次验证，Phase2 不处理手机 OTP，换手机号重跑...", "warn")
                return False
            else:
                # Non-email errors: network issues → retry; others → give up
                _err_lower = err.lower()
                if any(kw in _err_lower for kw in ("ssl", "connection", "timeout", "proxy", "eof")):
                    _sse_log(sse_q, f"  网络波动，5s后重试", "warn")
                    time.sleep(5)
                else:
                    break

        if oauth_result and oauth_result.get("ok"):
            _sse_log(sse_q, f"  [4/4] 上传成功! SUB2API id={oauth_result.get('sub2api_account_id','?')}", "success")
            phase1_result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
            return True
        else:
            # Phase 2 failed — cancel phone if >150s elapsed
            elapsed = time.time() - phase1_done_time
            _sse_log(sse_q, f"  Phase2耗时: {elapsed:.0f}s", "info")
            if elapsed > 150 and phone_aid and sms_obj:
                try:
                    sms_obj.cancel(phone_aid)
                    _sse_log(sse_q, f"  [取消] 超时释放号码: {phase1_result.get('phone','?')}", "warn")
                except Exception as e:
                    _sse_log(sse_q, f"  [取消] 释放失败: {e}", "error")
            # Release email
            if ms_helper_url:
                try:
                    pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                    pool.mark_unused(current_email)
                except Exception:
                    pass
            return False

    except Exception as e:
        _sse_log(sse_q, f"Phase 2 error: {e}", "error")
        # Release phone and email on exception
        if phone_aid and sms_obj:
            try:
                sms_obj.cancel(phone_aid)
            except Exception:
                pass
        if ms_helper_url:
            try:
                pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                pool.mark_unused(bind_email)
            except Exception:
                pass
        return False


def _run(user_id: int, target_count: int, sse_q: queue.Queue, stop_ev: threading.Event):
    config_data = db.get_user_config(user_id)
    proxy = config_data.get("proxy", "") or "socks5h://127.0.0.1:10808"
    country = config_data.get("country", "") or "151"
    max_price = config_data.get("max_price", "") or ""
    sms_timeout = config_data.get("sms_timeout", 30) or 30
    smsbower_key = config_data.get("smsbower_key", "") or ""
    hero_sms_key = config_data.get("hero_sms_key", "") or ""
    fivesim_key = config_data.get("fivesim_key", "") or ""

    # SUB2API config
    sub2api_url = config_data.get("sub2api_url", "") or ""
    sub2api_email = config_data.get("sub2api_email", "") or ""
    sub2api_password = config_data.get("sub2api_password", "") or ""
    sub2api_proxy_id = config_data.get("sub2api_proxy_id", 0) or 0
    sub2api_group = config_data.get("sub2api_group", "") or "CHATGPT"

    provider = "smsbower"
    if fivesim_key:
        provider = "5sim"
    elif hero_sms_key:
        provider = "hero-sms"

    api_key = fivesim_key if provider == "5sim" else (hero_sms_key if provider == "hero-sms" else smsbower_key)

    if not api_key:
        _sse_log(sse_q, "Please configure at least one SMS provider API key", "error")
        return

    sms = PhoneSMS(provider, api_key)
    reg_config = {
        "sms_provider": provider,
        "smsbower": {"api_key": smsbower_key},
        "hero_sms": {"api_key": hero_sms_key},
        "fivesim": {"api_key": fivesim_key},
        "service": "dr",
        "country": country,
        "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
        "proxy": proxy,
        "code_timeout": sms_timeout,
    }

    try:
        bal = sms.client.get_balance()
        _sse_log(sse_q, f"Balance: {bal} [平台:{provider}]", "info")
    except Exception as e:
        _sse_log(sse_q, f"Balance check failed: {e}", "error")

    ok_count = 0
    attempt = 0
    max_attempts = target_count * 15

    # MsOutlook helper for email pool
    ms_helper_url = db.get_admin_asset("msoutlook_helper_url") or ""
    bind_email = None

    while ok_count < target_count and attempt < max_attempts and not stop_ev.is_set():
        attempt += 1
        _sse_log(sse_q, f"[{attempt}] {ok_count}/{target_count}", "info")

        # Check quota
        user = db.get_user(user_id=user_id)
        if user.get("quota", 0) <= 0:
            _sse_log(sse_q, "Out of quota", "error")
            break

        # Get email — if previous attempt consumed one, get a fresh one
        try:
            email = get_email_for_user(user_id, sse_q)
            _sse_log(sse_q, f"Email: {email}", "success")
            bind_email = email
        except Exception as e:
            _sse_log(sse_q, f"Email failed: {e}, retrying...", "warn")
            continue  # 换下一个邮箱，不 break

        # Run Phase 1
        phone_aid = ""  # track activation_id for cancel
        phone_raw = ""
        try:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = ar.register_one(
                    reg_config, verbose=True, step_retries=2, max_price=max_price,
                    auto_activate=False,
                )
            for line in buf.getvalue().split("\n"):
                if line.strip():
                    _sse_log(sse_q, line.strip(), "info")

            phone_raw = result.get("phone", "?")
            phone_aid = result.get("activation_id", "")
            status = "ok" if result["ok"] else "fail"
            db.log_reg(user_id, phone_raw, status, bind_email or "", result.get("error", ""))
            db.consume_quota(user_id)

            if result["ok"]:
                ok_count += 1
                _sse_log(sse_q, f"Phase 1 OK: {phone_raw} -> {bind_email}", "success")
            else:
                _sse_log(sse_q, f"Phase 1 FAIL: {phone_raw} - {result.get('error','')}", "error")
                # Phase 1 失败，保存失败记录
                db.save_account(user_id, phone_raw, result.get("password", ""),
                                bind_email or "", status="fail_phase1",
                                sub2api_id="", session_token="", access_token="")

            # 无论成功失败，标记邮箱已用
            if ms_helper_url and (bind_email or "") and "@outlook.com" in (bind_email or "").lower():
                try:
                    pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                    pool.mark_used(bind_email)
                    _sse_log(sse_q, f"  [邮箱] 已标记占用: {bind_email}", "info")
                except Exception as e:
                    _sse_log(sse_q, f"  [邮箱] mark_used 失败: {e}", "warn")

            # ── Phase 2: Only if Phase 1 OK and SUB2API configured ──
            if result["ok"] and sub2api_url and sub2api_email and sub2api_password and result.get("session_token") and bind_email:
                # Store context for Phase 2 retry to re-run Phase 1
                result["_user_id"] = user_id
                result["_reg_config"] = reg_config
                result["_sms_provider"] = provider
                result["_smsbower"] = {"api_key": smsbower_key}
                result["_hero_sms"] = {"api_key": hero_sms_key}
                result["_fivesim"] = {"api_key": fivesim_key}
                result["_api_key"] = api_key

                _sse_log(sse_q, "=== Phase 2: OAuth + Bind Email + SUB2API ===", "info")
                phase2_ok = _run_phase2(
                    sse_q, result, bind_email, sub2api_url, sub2api_email,
                    sub2api_password, sub2api_proxy_id, sub2api_group,
                    proxy, ms_helper_url, phone_aid, user_id, sms, api_key, provider,
                )
                if phase2_ok:
                    # 激活号码
                    if phone_aid:
                        try:
                            sms2 = PhoneSMS(provider, api_key)
                            sms2.complete(phone_aid)
                            _sse_log(sse_q, f"  [激活] 号码已激活 (status=6): {phone_raw}", "success")
                        except Exception as e:
                            _sse_log(sse_q, f"  [激活] 失败: {e}", "warn")
                    # 保存成功账号
                    db.save_account(
                        user_id, phone_raw, result["password"], bind_email,
                        result.get("name", ""), result.get("birthdate", ""),
                        result.get("session_token", ""), result.get("access_token", ""),
                        result.get("sub2api_id", ""), status="ok",
                    )
                else:
                    # Phase 2 失败，保存失败记录
                    db.save_account(user_id, phone_raw, result.get("password", ""),
                                    bind_email or "", result.get("name", ""),
                                    result.get("birthdate", ""), result.get("session_token", ""),
                                    result.get("access_token", ""), status="fail_phase2",
                                    sub2api_id="")

            elif result["ok"] and not (sub2api_url and sub2api_email):
                # Phase 1 成功但没有 SUB2API 配置，直接保存账号
                db.save_account(
                    user_id, phone_raw, result["password"], bind_email or "",
                    result.get("name", ""), result.get("birthdate", ""),
                    result.get("session_token", ""), result.get("access_token", ""),
                    status="phase1_only",
                )

        except Exception as e:
            _sse_log(sse_q, f"Error: {e}", "error")
            # 外层异常：尝试释放号码和邮箱
            if phone_aid:
                try:
                    sms.cancel(phone_aid)
                    _sse_log(sse_q, f"  [取消] 异常释放号码: {phone_raw}", "warn")
                except Exception:
                    pass
            if ms_helper_url and bind_email:
                try:
                    pool = MsOutlookPool(helper_url=ms_helper_url, verbose=False)
                    pool.mark_unused(bind_email)
                except Exception:
                    pass

    _sse_log(sse_q, f"Done: {ok_count}/{target_count}", "success")

    # Cleanup
    if user_id in active_runners:
        del active_runners[user_id]
