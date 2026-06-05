#!/usr/bin/env python3
"""
手机接码平台 — 纯协议实现
支持 hero-sms、5sim、nexsms 三种平台

用法:
    from phone_sms import PhoneSMS
    sms = PhoneSMS(provider="hero-sms", api_key="your_key")
    activation = sms.get_number(country="thailand")  # 获取号码
    code = sms.wait_for_code(activation.id, timeout=120)  # 等验证码
"""

import time
import threading
import requests
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Activation:
    id: str
    phone: str
    country: str
    service: str
    status: str = "pending"
    code: Optional[str] = None


# ============================================================
# hero-sms 平台
# ============================================================

HERO_SMS_BASE = "https://hero-sms.com/stubs/handler_api.php"
HERO_SMS_SERVICE_CODES = {
    "openai": "dr",
    "chatgpt": "dr",
    "google": "go",
    "telegram": "tg",
}
HERO_SMS_COUNTRIES = {
    "thailand": 52,
    "indonesia": 6,
    "usa": 187,
    "uk": 16,
    "japan": 151,
    "germany": 43,
    "france": 73,
    "vietnam": 10,
}


class HeroSMS:
    """hero-sms.com API"""

    def __init__(self, api_key: str, base_url: str = HERO_SMS_BASE):
        self.api_key = api_key
        self.base_url = base_url

    def _call(self, params: Dict[str, str], timeout: float = 30.0, retries: int = 3) -> str:
        params["api_key"] = self.api_key
        # 网络异常自动重试，避免 SSL/Connection 错误导致整个流程失败
        last_err = None
        for attempt in range(max(1, int(retries))):
            try:
                resp = requests.get(self.base_url, params=params, timeout=max(1.0, float(timeout)))
                return resp.text.strip()
            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < max(1, int(retries)) - 1:
                    time.sleep(min(2 * (attempt + 1), max(0.0, float(timeout))))
        raise last_err

    def get_balance(self) -> float:
        params = {"action": "getBalance"}
        result = self._call(params)
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"查询余额失败: {result}")

    def get_cheapest_provider(
        self, service: str = "dr", country: str = "151"
    ) -> tuple[str, float]:
        """获取最便宜的运营商 ID 和价格 (SmsBower/hero-sms 兼容)"""
        r = requests.get(
            self.base_url,
            params={
                "api_key": self.api_key,
                "action": "getPricesV3",
                "service": service,
                "country": country,
            },
            timeout=15,
        )
        data = r.json()
        providers = data.get(country, {}).get(service, {})
        cheapest, cheapest_price = "", 999.0
        for pid, info in providers.items():
            price = float(info.get("price", 999))
            if price < cheapest_price:
                cheapest_price = price
                cheapest = pid
        return cheapest, cheapest_price

    def get_number(
        self,
        service: str = "dr",
        country: str = "thailand",
        operator: Optional[str] = None,
    ) -> Activation:
        """
        获取手机号
        service: 服务代码 (dr=OpenAI, go=Google 等)
        country: 国家名或 ID
        """
        country_id = HERO_SMS_COUNTRIES.get(country, country)
        params = {
            "action": "getNumber",
            "service": service,
            "country": str(country_id),
        }
        if operator:
            params["operator"] = operator

        result = self._call(params)
        # 返回格式: ACCESS_NUMBER:activationId:phoneNumber
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            act_id = parts[1]
            phone = parts[2]
            return Activation(
                id=act_id,
                phone=phone,
                country=str(country),
                service=service,
                status="waiting",
            )
        raise RuntimeError(f"获取号码失败: {result}")

    def get_status(self, activation_id: str, timeout: float = 30.0, retries: int = 3) -> str:
        """
        查询激活状态
        返回: STATUS_WAIT_CODE | STATUS_OK:code | STATUS_CANCEL | STATUS_WAIT_RESEND
        """
        result = self._call({
            "action": "getStatus",
            "id": activation_id,
        }, timeout=timeout, retries=retries)
        return result

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = 180,
        interval: int = 5,
        verbose: bool = True,
        exclude_codes: Optional[List[str]] = None,
    ) -> Optional[str]:
        """轮询等待验证码，超时返回 None"""
        excluded = {str(code).strip() for code in (exclude_codes or []) if str(code).strip()}
        start = time.time()
        deadline = start + max(0.0, float(timeout))
        _net_errors = 0  # 连续网络错误计数
        while time.time() < deadline:
            try:
                remaining = max(0.0, deadline - time.time())
                if remaining <= 0:
                    break
                status = self.get_status(
                    activation_id,
                    timeout=min(8.0, remaining),
                    retries=1,
                )
                _net_errors = 0  # 成功后重置计数
            except requests.exceptions.RequestException as e:
                _net_errors += 1
                if verbose:
                    print(f"  [hero-sms] 轮询网络异常 ({_net_errors}): {e}")
                # 连续 5 次网络错误才放弃，否则继续轮询
                if _net_errors >= 5:
                    if verbose:
                        print(f"  [hero-sms] 连续 {_net_errors} 次网络异常，放弃轮询")
                    return None
                remaining = deadline - time.time()
                if remaining > 0:
                    time.sleep(min(interval, remaining))
                continue

            if verbose:
                print(f"  [hero-sms] 轮询 {activation_id}: {status}")

            if status.startswith("STATUS_OK:"):
                code = status.split(":", 1)[1]
                if code in excluded:
                    if verbose:
                        print(f"  [hero-sms] 忽略旧验证码: {code}")
                    remaining = deadline - time.time()
                    if remaining > 0:
                        time.sleep(min(interval, remaining))
                    continue
                return code
            elif status == "STATUS_CANCEL":
                return None
            elif status == "STATUS_WAIT_RESEND":
                # 等待重发
                remaining = deadline - time.time()
                if remaining > 0:
                    time.sleep(min(interval, remaining))
            else:
                remaining = deadline - time.time()
                if remaining > 0:
                    time.sleep(min(interval, remaining))

        # 不再主动 cancel。hero-sms / SmsBower 协议要求拿号后 ≥150s 才能
        # setStatus=8，否则平台拒绝且不退款。由上层 PhoneSMS 统一走延迟队列。
        return None

    def cancel(self, activation_id: str) -> bool:
        """取消激活（释放号码）。

        注意：调用方需自行保证距 getNumber 已 ≥150s，否则平台会拒绝。
        """
        result = self._call({
            "action": "setStatus",
            "id": activation_id,
            "status": "8",  # 取消
        })
        return "ACCESS_CANCEL" in result

    def finish(self, activation_id: str) -> bool:
        """标记激活完成"""
        result = self._call({
            "action": "setStatus",
            "id": activation_id,
            "status": "6",  # 完成
        })
        return "ACCESS_ACTIVATION" in result


# ============================================================
# 5sim.net 平台
# ============================================================

FIVE_SIM_BASE = "https://5sim.net/v1"
FIVE_SIM_PRODUCTS = {
    "openai": "openai",
    "chatgpt": "openai",
}


class FiveSim:
    """5sim.net API"""

    def __init__(self, api_key: str, base_url: str = FIVE_SIM_BASE):
        self.api_key = api_key
        self.base_url = base_url

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def get_balance(self) -> Dict:
        resp = requests.get(
            f"{self.base_url}/user/profile",
            headers=self._headers(),
            timeout=30,
        )
        data = resp.json()
        return data

    def get_number(
        self,
        product: str = "openai",
        country: str = "thailand",
        operator: str = "any",
    ) -> Activation:
        """
        购买激活号码
        country: thailand, vietnam, indonesia
        """
        resp = requests.get(
            f"{self.base_url}/user/buy/activation/{country}/{operator}/{product}",
            headers=self._headers(),
            timeout=30,
        )
        data = resp.json()
        if not data.get("id"):
            raise RuntimeError(f"5sim 获取号码失败: {data}")

        return Activation(
            id=str(data["id"]),
            phone=str(data.get("phone", "")),
            country=country,
            service=product,
            status="waiting",
        )

    def check_sms(self, activation_id: str) -> List[Dict]:
        """检查短信列表"""
        resp = requests.get(
            f"{self.base_url}/user/check/{activation_id}",
            headers=self._headers(),
            timeout=30,
        )
        return resp.json()

    def wait_for_code(
        self,
        activation_id: str,
        timeout: int = 180,
        interval: int = 5,
        verbose: bool = True,
        exclude_codes: Optional[List[str]] = None,
    ) -> Optional[str]:
        """轮询等待验证码"""
        excluded = {str(code).strip() for code in (exclude_codes or []) if str(code).strip()}
        start = time.time()
        _net_errors = 0
        while time.time() - start < timeout:
            try:
                messages = self.check_sms(activation_id)
                _net_errors = 0
            except requests.exceptions.RequestException as e:
                _net_errors += 1
                if verbose:
                    print(f"  [5sim] 轮询网络异常 ({_net_errors}): {e}")
                if _net_errors >= 5:
                    if verbose:
                        print(f"  [5sim] 连续 {_net_errors} 次网络异常，放弃轮询")
                    return None
                time.sleep(interval)
                continue

            if verbose:
                print(f"  [5sim] 轮询 {activation_id}: {len(messages)} 条短信")

            for msg in messages:
                text = str(msg.get("text", "") or msg.get("sms", "") or "")
                code = msg.get("code", "")
                if code:
                    code = str(code)
                    if code in excluded:
                        continue
                    return code
                # 尝试从文本提取 6 位数字
                import re
                match = re.search(r"\b(\d{6})\b", text)
                if match:
                    code = match.group(1)
                    if code in excluded:
                        continue
                    return code

            time.sleep(interval)

        # 不再主动 cancel，由上层 PhoneSMS 统一走延迟队列处理。
        return None

    def cancel(self, activation_id: str) -> bool:
        resp = requests.get(
            f"{self.base_url}/user/cancel/{activation_id}",
            headers=self._headers(),
            timeout=30,
        )
        return resp.status_code == 200

    def finish(self, activation_id: str) -> bool:
        resp = requests.get(
            f"{self.base_url}/user/finish/{activation_id}",
            headers=self._headers(),
            timeout=30,
        )
        return resp.status_code == 200


# ============================================================
# SMSBower 平台 (API 与 hero-sms 兼容)
# ============================================================

SMSBOWER_BASE = "https://smsbower.page/stubs/handler_api.php"

class SmsBower(HeroSMS):
    """SMSBower — API 与 hero-sms 完全兼容，仅 base URL 不同"""
    def __init__(self, api_key: str):
        super().__init__(api_key, base_url=SMSBOWER_BASE)


# ============================================================
# 统一接口
# ============================================================

class PhoneSMS:
    """统一的接码平台接口"""

    PROVIDERS = {
        "hero-sms": HeroSMS,
        "smsbower": SmsBower,
        "5sim": FiveSim,
    }

    # hero-sms / SmsBower 协议要求：getNumber 后 ≥150s 才能 setStatus=8
    # 否则平台直接拒绝，号码无法主动退款（只能等平台自然过期）。
    CANCEL_MIN_HOLD = 150.0

    def __init__(self, provider: str = "hero-sms", api_key: str = ""):
        if provider not in self.PROVIDERS:
            raise ValueError(f"不支持的接码平台: {provider}，可选: {list(self.PROVIDERS)}")
        self.provider = provider
        self.client = self.PROVIDERS[provider](api_key)
        self._activation_id: Optional[str] = None  # 记录最近一次激活的 ID
        self._activated_at: Optional[float] = None  # 拿号时间戳，用于冷却期计算

        # 延迟取消队列：[(aid, fire_at, attempts), ...]
        # 主流程的 cancel() 入队后立即返回，由后台线程到点发送 setStatus=8。
        # 这样既满足协议 150s 冷却期，又不阻塞主注册循环。
        self._pending_cancels: List[Tuple[str, float, int]] = []
        self._pending_lock = threading.Lock()
        self._cancel_thread = threading.Thread(
            target=self._cancel_worker, daemon=True, name="phonesms-cancel"
        )
        self._cancel_thread.start()

    def get_number(
        self,
        service: str = "openai",
        country: str = "thailand",
    ) -> tuple:
        """获取号码，返回 (activation_id, phone) 元组，兼容 SmsBower 接口"""
        act = self.client.get_number(service=service, country=country)
        self._activation_id = act.id
        self._activated_at = time.time()
        return act.id, act.phone

    def wait_for_code(
        self,
        activation_id: str = None,
        timeout: int = 180,
        verbose: bool = True,
        exclude_codes: Optional[List[str]] = None,
    ) -> Optional[str]:
        aid = activation_id or self._activation_id
        if not aid:
            raise RuntimeError("No active activation")
        return self.client.wait_for_code(aid, timeout=timeout, verbose=verbose, exclude_codes=exclude_codes)

    def wait_code(
        self,
        timeout: int = 300,
        interval: int = 3,
        exclude_codes: Optional[List[str]] = None,
    ) -> Optional[str]:
        """SmsBower 兼容别名：轮询等待验证码"""
        return self.wait_for_code(timeout=timeout, exclude_codes=exclude_codes)

    def _cancel_min_hold(self, min_hold: Optional[float] = None) -> float:
        if min_hold is not None:
            return float(min_hold)
        if self.provider in {"hero-sms", "smsbower"}:
            return self.CANCEL_MIN_HOLD
        return 0.0

    def cancel_wait_seconds(self, min_hold: Optional[float] = None) -> float:
        """返回按平台规则还需要等多久才能取消。

        若当前 PhoneSMS 实例不是拿号实例（例如 worker_pool 失败后重新构造
        一个实例只拿到 activation_id），本地没有 getNumber 时间，此时返回 0：
        调用方应该先尝试取消，若平台拒绝再轮询重试。
        """
        hold = self._cancel_min_hold(min_hold)
        if hold <= 0:
            return 0.0
        if self._activated_at is None:
            return 0.0
        return max(0.0, hold - (time.time() - self._activated_at))

    def cancel(self, activation_id: str = None, min_hold: Optional[float] = None) -> float:
        """请求取消激活。考虑 hero-sms / SmsBower 协议的 150s 冷却期。

        策略 B+C：
          - B（延迟取消）：把请求放入延迟队列，主流程立即返回，
            到点后由后台线程发送 setStatus=8。
          - C（自然过期兜底）：若平台仍然拒绝（号码已收过短信、已被服务端释放等），
            重试一次后放弃，依赖平台在保留期结束后自动退款。

        返回值：预计距真正发送 cancel 还需等待的秒数（≥0）。
                若没记录拿号时间或 aid 缺失，返回 0.0。
                调用方可忽略返回值；旧调用方保持兼容。
        """
        aid = activation_id or self._activation_id
        if not aid:
            return 0.0
        now = time.time()
        wait_sec = self.cancel_wait_seconds(min_hold)
        fire_at = now + wait_sec
        with self._pending_lock:
            # 避免重复入队
            if not any(item[0] == aid for item in self._pending_cancels):
                self._pending_cancels.append((aid, fire_at, 0))
        return wait_sec

    def cancel_blocking(
        self,
        activation_id: str = None,
        min_hold: Optional[float] = None,
        poll_interval: float = 10.0,
        max_wait: Optional[float] = None,
    ) -> bool:
        """同步取消号码，直到平台确认或超时。

        这个方法用于 worker_pool 这类短生命周期子进程。之前只把取消请求
        放进进程内 daemon 队列，进程退出后队列线程会被杀掉，导致平台号码
        状态没有真正改成取消。同步等待可以保证返回 True 时已经调用成功。
        """
        aid = activation_id or self._activation_id
        if not aid:
            return False
        wait_sec = self.cancel_wait_seconds(min_hold)
        if max_wait is None:
            max_wait = max(60.0, wait_sec + self._cancel_min_hold(min_hold) + 60.0)
        deadline = time.time() + max(0.0, float(max_wait))

        if wait_sec > 0:
            time.sleep(min(wait_sec, max(0.0, deadline - time.time())))

        while True:
            try:
                if bool(self.client.cancel(aid)):
                    return True
            except Exception:
                pass

            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(max(1.0, float(poll_interval)), remaining))

    def _cancel_worker(self):
        """后台守护线程：扫描延迟队列，到点发送 cancel。

        - 每 2 秒扫描一次。
        - 到点后调用 self.client.cancel(aid)。
        - 平台拒绝（hero-sms 在冷却期内会返回非 ACCESS_CANCEL / 5sim 返回非 200）
          时再重试 1 次，间隔 10 秒；仍失败则放弃（依赖平台自然过期退款）。
        """
        while True:
            time.sleep(2)
            now = time.time()
            ready: List[Tuple[str, float, int]] = []
            with self._pending_lock:
                remaining: List[Tuple[str, float, int]] = []
                for item in self._pending_cancels:
                    aid, fire_at, attempts = item
                    if fire_at <= now:
                        ready.append(item)
                    else:
                        remaining.append(item)
                self._pending_cancels = remaining

            for aid, _fire_at, attempts in ready:
                try:
                    ok = bool(self.client.cancel(aid))
                except Exception:
                    ok = False
                if ok:
                    continue
                # 平台拒绝：重试一次（10s 后），仍失败则放弃
                if attempts >= 1:
                    continue
                with self._pending_lock:
                    if not any(x[0] == aid for x in self._pending_cancels):
                        self._pending_cancels.append((aid, now + 10, attempts + 1))

    def finish(self, activation_id: str = None):
        aid = activation_id or self._activation_id
        if aid:
            self.client.finish(aid)

    def resend(self, activation_id: str = None):
        """请求重新发送短信 (status=3)，复用该号码"""
        aid = activation_id or self._activation_id
        if aid and hasattr(self.client, "_call"):
            self.client._call({"action": "setStatus", "id": aid, "status": "3"})

    # ---- SmsBower 兼容方法（无参，使用最近激活的 ID） ----

    def set_ready(self):
        """空操作。

        hero-sms / SmsBower 的 setStatus 协议只接受三种状态：
            3 — 请求重发短信
            6 — 完成激活（确认付款）
            8 — 取消激活（退款）
        不存在"号码就绪"的 setStatus 状态。getNumber 返回后号码已自动进入
        STATUS_WAIT_CODE，不需要客户端再做任何声明。
        保留此方法仅为兼容已有调用方（auto_register.py:168）。
        """
        return

    def complete(self, activation_id: str = None):
        """标记激活完成 (status=6)。

        可选传入 activation_id；不传则使用最近一次 get_number 拿到的 ID。
        兼容 web_gui 等场景：在 worker 注册完成后，用一个新 PhoneSMS 实例
        激活当时的号码（新实例的 self._activation_id 是空的，必须靠参数）。
        """
        aid = activation_id or self._activation_id
        if not aid:
            return
        if hasattr(self.client, "finish"):
            self.client.finish(aid)
        elif hasattr(self.client, "_call"):
            self.client._call({"action": "setStatus", "id": aid, "status": "6"})

    def get_cheapest_provider(self, service: str = "dr", country: str = "151") -> tuple:
        """获取最便宜的运营商 (仅 smsbower/hero-sms 支持)"""
        if hasattr(self.client, "get_cheapest_provider"):
            return self.client.get_cheapest_provider(service, country)
        return "?", 0


# ============================================================
# CLI 测试
# ============================================================

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--provider", default="hero-sms", choices=["hero-sms", "5sim"])
    p.add_argument("--api-key", required=True)
    p.add_argument("--command", default="balance", choices=["balance", "get-number", "wait-code"])
    p.add_argument("--country", default="thailand")
    p.add_argument("--service", default="dr")
    p.add_argument("--activation-id", default="")
    args = p.parse_args()

    sms = PhoneSMS(args.provider, args.api_key)
    if args.command == "balance":
        if args.provider == "hero-sms":
            print(f"余额: {sms.client.get_balance()}")
        else:
            print(sms.client.get_balance())
    elif args.command == "get-number":
        act = sms.get_number(args.service, args.country)
        print(f"ID={act.id} 号码={act.phone}")
    elif args.command == "wait-code":
        code = sms.wait_for_code(args.activation_id, timeout=180)
        print(f"验证码: {code}")
