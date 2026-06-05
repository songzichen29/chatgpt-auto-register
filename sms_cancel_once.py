#!/usr/bin/env python3
"""Background SMS activation cancellation helper.

Used by worker_pool.py so a failed attempt can move to the next phone without
blocking on hero-sms/SmsBower's cancel cooldown.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import auto_register as ar
from phone_sms import PhoneSMS


ROOT = Path(__file__).resolve().parent


def _log(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "sms_cancel_jobs.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cancel one SMS activation in background")
    parser.add_argument("--config", default="", help="config.json path")
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--phone", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--max-wait", type=float, default=240.0)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    args = parser.parse_args(argv)

    cfg = ar.load_config(args.config or None)
    provider = cfg.get("sms_provider", "smsbower")
    api_key = ar._get_sms_api_key(cfg, provider)
    label = f"{args.phone or '?'} activation_id={args.activation_id}"
    if args.reason:
        label += f" reason={args.reason}"

    _log(f"start cancel {label}")
    try:
        sms = PhoneSMS(provider, api_key)
        ok = sms.cancel_blocking(
            args.activation_id,
            poll_interval=args.poll_interval,
            max_wait=args.max_wait,
        )
    except Exception as exc:
        _log(f"cancel exception {label}: {exc}")
        return 2

    if ok:
        _log(f"cancel confirmed {label}")
        return 0
    _log(f"cancel not confirmed {label}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
