import inspect
import json
import requests
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import openai_bind_email
import auto_register
import chatgpt_register
import phone_sms
import worker_pool


class WorkerPoolComponentTests(unittest.TestCase):
    def test_validate_otp_uses_authorize_continue_fallback(self):
        reg = chatgpt_register.ChatGPTRegister(verbose=False)
        calls = []

        def fake_post(path, payload, **kwargs):
            calls.append((path, payload, kwargs))
            return {"continue_url": "/about-you", "_status": 200}

        reg._post_auth_json_with_fallback = fake_post
        result = reg.validate_otp("123456")

        self.assertEqual(result["continue_url"], "/about-you")
        self.assertEqual(calls[0][0], "/api/accounts/phone-otp/validate")
        self.assertEqual(calls[0][1], {"code": "123456"})
        self.assertEqual(calls[0][2]["flow"], "authorize_continue")
        self.assertEqual(calls[0][2]["referer"], "https://auth.openai.com/contact-verification")

    def test_create_account_uses_oauth_create_account_fallback(self):
        reg = chatgpt_register.ChatGPTRegister(verbose=False)
        calls = []

        def fake_post(path, payload, **kwargs):
            calls.append((path, payload, kwargs))
            return {"continue_url": "https://callback.example", "_status": 200}

        reg._post_auth_json_with_fallback = fake_post
        result = reg.create_account("A", "2000-01-01")

        self.assertEqual(result["continue_url"], "https://callback.example")
        self.assertEqual(calls[0][0], "/api/accounts/create_account")
        self.assertEqual(calls[0][1], {"name": "A", "birthdate": "2000-01-01"})
        self.assertEqual(calls[0][2]["flow"], "oauth_create_account")
        self.assertIs(calls[0][2]["allow_redirects"], False)

    def test_create_account_invalid_state_is_non_retryable_auth_error(self):
        self.assertTrue(auto_register._is_auth_session_invalid_error("invalid_state"))
        self.assertTrue(auto_register._is_auth_session_invalid_error("Your sign-in session is no longer valid. Please start over."))
        self.assertTrue(auto_register._is_auth_session_invalid_error({"error": {"code": "invalid_state"}}))
        self.assertFalse(auto_register._is_auth_session_invalid_error("name is invalid"))

    def test_auth_json_fallback_retries_transport_error_with_http1_rebuild(self):
        reg = chatgpt_register.ChatGPTRegister(verbose=False)
        calls = {"post": 0, "rebuild": 0, "sentinel": []}

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = '{"continue_url": "/ok"}'

            def json(self):
                return {"continue_url": "/ok"}

        class FakeSession:
            def post(self, _url, **_kwargs):
                calls["post"] += 1
                if calls["post"] == 1:
                    raise ConnectionError("curl: (55)")
                return FakeResponse()

        def fake_sentinel(headers, flow):
            calls["sentinel"].append(flow)
            headers["OpenAI-Sentinel-Token"] = "sentinel-token"

        reg.session = FakeSession()
        reg._add_sentinel_headers = fake_sentinel
        reg._rebuild_session = lambda: calls.__setitem__("rebuild", calls["rebuild"] + 1)

        result = reg._post_auth_json_with_fallback(
            "/api/test",
            {"x": 1},
            referer="https://auth.openai.com/test",
            flow="authorize_continue",
        )

        self.assertEqual(result["continue_url"], "/ok")
        self.assertEqual(result["_transport"], "http1-rebuild")
        self.assertEqual(calls["post"], 2)
        self.assertEqual(calls["rebuild"], 1)
        self.assertEqual(calls["sentinel"], ["authorize_continue", "authorize_continue"])

    def test_auto_register_cancel_with_eta_starts_background_job(self):
        calls = []

        class FakeSMS:
            _activation_id = "aid"
            _config_path = "config.json"

            def cancel_wait_seconds(self):
                return 123

            def cancel_blocking(self):
                raise AssertionError("cancel_blocking should not run inline")

        old_popen = subprocess.Popen
        subprocess.Popen = lambda cmd, **kwargs: calls.append((cmd, kwargs)) or object()
        try:
            auto_register._cancel_with_eta(FakeSMS(), "+100", "测试", verbose=False)
        finally:
            subprocess.Popen = old_popen

        self.assertEqual(len(calls), 1)
        self.assertIn("sms_cancel_once.py", " ".join(calls[0][0]))
        self.assertIn("--activation-id", calls[0][0])
        self.assertIn("aid", calls[0][0])

    def test_hero_sms_wait_for_code_respects_timeout_on_network_timeout(self):
        client = phone_sms.HeroSMS("key")

        def slow_status(_aid, timeout=30, retries=3):
            time.sleep(float(timeout) + 0.05)
            raise requests.exceptions.Timeout("simulated")

        client.get_status = slow_status
        started = time.time()
        code = client.wait_for_code("aid", timeout=1, interval=1, verbose=False)
        elapsed = time.time() - started

        self.assertIsNone(code)
        self.assertLess(elapsed, 1.5)

    def test_load_config_merges_register_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(
                json.dumps({
                    "register": {
                        "password": "Configured.Password123",
                        "name": "Tester",
                        "birthdate": "1999-01-02",
                    }
                }),
                encoding="utf-8",
            )

            cfg = auto_register.load_config(str(cfg_path))

            self.assertEqual(cfg["register"]["password"], "Configured.Password123")
            self.assertEqual(cfg["register"]["name"], "Tester")
            self.assertEqual(cfg["register"]["birthdate"], "1999-01-02")

    def test_run_second_half_compat_signature(self):
        sig = inspect.signature(openai_bind_email.run_second_half)
        self.assertIn("save_import", sig.parameters)
        self.assertIs(sig.parameters["save_import"].default, True)
        self.assertIn("interactive_input", sig.parameters)
        self.assertIs(sig.parameters["interactive_input"].default, True)

    def test_run_second_half_exchange_code_retries_retryable_status(self):
        class FakeFlow:
            def __init__(self, *args, **kwargs):
                self.session = None

            @staticmethod
            def parse_oauth_url(_url):
                return {"client_id": "cid"}

            def initiate_oauth(self, url):
                return True, url, ""

            def sentinel_authorize(self):
                return None

            def submit_phone(self, _phone):
                return {"page": {"type": "consent"}}

            def sentinel_password(self):
                return None

            def verify_password(self, _password):
                return {"page": {"type": "consent"}}

            def get_session_dump(self):
                return {"client_auth_session": {"workspaces": [{"id": "ws"}]}}

            def select_workspace(self, _ws_id):
                return {"page": {"type": "consent"}, "continue_url": "https://continue/"}

            def follow_continue_until_code(self, _continue_url):
                return "auth-code"

            def final_oauth(self, _params):
                return "auth-code"

        class FakeResponse:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload or {}
                self.text = text

            def json(self):
                return self._payload

        old_flow = openai_bind_email.OAuthSecondHalf
        old_post = requests.post
        exchange_statuses = [500, 503, 200]
        exchange_calls = []

        def fake_post(url, **_kwargs):
            if url.endswith("/api/v1/auth/login"):
                return FakeResponse(200, {"code": 0, "data": {"access_token": "admin-token"}})
            if url.endswith("/api/v1/admin/openai/exchange-code"):
                exchange_calls.append(url)
                status = exchange_statuses.pop(0)
                if status == 200:
                    return FakeResponse(
                        200,
                        {
                            "data": {
                                "access_token": "at",
                                "refresh_token": "rt",
                                "expires_at": 123,
                                "email": "bound@example.com",
                            }
                        },
                    )
                return FakeResponse(status, {}, f"HTTP {status}")
            raise AssertionError(f"unexpected URL: {url}")

        openai_bind_email.OAuthSecondHalf = FakeFlow
        requests.post = fake_post
        try:
            result = openai_bind_email.run_second_half(
                oauth_url="https://oauth/?state=s",
                phone="+100",
                password="pw",
                icloud_email="bound@example.com",
                icloud_cookies={},
                sub2api_url="https://sub2api.example",
                sub2api_email="admin@example.com",
                sub2api_password="pw",
                sub2api_session_id="sid",
                sub2api_state="s",
                verbose=False,
                save_import=False,
            )
        finally:
            openai_bind_email.OAuthSecondHalf = old_flow
            requests.post = old_post

        self.assertTrue(result["ok"])
        self.assertEqual(result["import_file"], "")
        self.assertEqual(len(exchange_calls), 3)
        self.assertEqual(result["import_data"]["accounts"][0]["credentials"]["email"], "bound@example.com")

    def test_run_second_half_exchange_code_does_not_retry_400(self):
        class FakeFlow:
            def __init__(self, *args, **kwargs):
                pass

            @staticmethod
            def parse_oauth_url(_url):
                return {"client_id": "cid"}

            def initiate_oauth(self, url):
                return True, url, ""

            def sentinel_authorize(self):
                return None

            def submit_phone(self, _phone):
                return {"page": {"type": "consent"}}

            def sentinel_password(self):
                return None

            def verify_password(self, _password):
                return {"page": {"type": "consent"}}

            def get_session_dump(self):
                return {"client_auth_session": {"workspaces": []}}

            def follow_continue_until_code(self, _continue_url):
                return None

            def final_oauth(self, _params):
                return "auth-code"

        class FakeResponse:
            def __init__(self, status_code, payload=None, text=""):
                self.status_code = status_code
                self._payload = payload or {}
                self.text = text

            def json(self):
                return self._payload

        old_flow = openai_bind_email.OAuthSecondHalf
        old_post = requests.post
        exchange_calls = []

        def fake_post(url, **_kwargs):
            if url.endswith("/api/v1/auth/login"):
                return FakeResponse(200, {"code": 0, "data": {"access_token": "admin-token"}})
            if url.endswith("/api/v1/admin/openai/exchange-code"):
                exchange_calls.append(url)
                return FakeResponse(400, {}, "bad request")
            raise AssertionError(f"unexpected URL: {url}")

        openai_bind_email.OAuthSecondHalf = FakeFlow
        requests.post = fake_post
        try:
            result = openai_bind_email.run_second_half(
                oauth_url="https://oauth/?state=s",
                phone="+100",
                password="pw",
                icloud_email="bound@example.com",
                icloud_cookies={},
                sub2api_url="https://sub2api.example",
                sub2api_email="admin@example.com",
                sub2api_password="pw",
                sub2api_session_id="sid",
                sub2api_state="s",
                verbose=False,
                save_import=False,
            )
        finally:
            openai_bind_email.OAuthSecondHalf = old_flow
            requests.post = old_post

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "exchange-code: 400")
        self.assertEqual(len(exchange_calls), 1)

    def test_thread_stdout_router_routes_by_thread(self):
        router = worker_pool.ThreadStdoutRouter.install_once()
        logs = []
        try:
            def task(wid):
                with router.capture_current_thread(lambda line: logs.append((wid, line))):
                    print(f"hello-{wid}")

            threads = [threading.Thread(target=task, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            router.restore()

        self.assertEqual(sorted(logs), [(0, "hello-0"), (1, "hello-1")])

    def test_email_allocator_concurrent_acquire_and_cooling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": f"user{i}@example.com", "enabled": True, "used": False}
                    for i in range(12)
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator(
                "",
                pool_path=str(pool_path),
                used_file=str(used_file),
            )
            leases = []
            lock = threading.Lock()

            def acquire_one(i):
                lease = allocator.acquire(i)
                with lock:
                    leases.append(lease)

            threads = [threading.Thread(target=acquire_one, args=(i,)) for i in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            emails = [lease.email for lease in leases]
            self.assertEqual(len(emails), 10)
            self.assertEqual(len(set(emails)), 10)

            first = leases[0].email
            leases[0].release(cooldown=60)
            self.assertNotEqual(allocator.acquire(99).email, first)

    def test_result_writer_concurrent_account_and_import_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")

            def write_account(i):
                writer.append_account({
                    "status": "ok",
                    "phone": f"+100{i}",
                    "password": "pw",
                    "access_token": "SECRET",
                })

            threads = [threading.Thread(target=write_account, args=(i,)) for i in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(len(all_data), 10)
            self.assertTrue(all("access_token" not in item for item in all_data))
            self.assertTrue(all(item["password"] == "pw" for item in all_data))

            payload = {
                "type": "sub2api-data",
                "version": 1,
                "accounts": [{"name": "a"}, {"name": "ignored"}],
            }
            threads = [threading.Thread(target=lambda: writer.append_import(payload)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            import_file = next((tmp_path / "imports").glob("import_*.json"))
            import_data = json.loads(import_file.read_text(encoding="utf-8"))
            self.assertEqual(len(import_data["accounts"]), 2)

    def test_result_writer_sanitizes_invalid_windows_filename_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")

            writer.append_account({
                "status": "fail_phase1",
                "phone": "?",
                "password": "pw",
                "error": "获取号码失败: NO_BALANCE",
            })

            files = list((tmp_path / "results").glob("*_fail_phase1.json"))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].name.startswith("unknown_"))
            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["phone"], "?")
            self.assertEqual(all_data[-1]["password"], "pw")

    def test_phase2_email_already_in_use_switches_to_new_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "old@example.com", "enabled": True, "used": False},
                    {"email": "new@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            lease = allocator.acquire(1)
            calls = {"run": 0}
            old_get = worker_pool._get_oauth_session_with_retry
            old_run = worker_pool.run_second_half

            def fake_run(**kwargs):
                calls["run"] += 1
                self.assertIs(kwargs["save_import"], False)
                self.assertIs(kwargs["interactive_input"], False)
                if calls["run"] == 1:
                    return {"ok": False, "error": "email_already_in_use"}
                return {
                    "ok": True,
                    "sub2api_account_id": "42",
                    "import_data": {"accounts": [{"name": kwargs["icloud_email"]}]},
                }

            worker_pool._get_oauth_session_with_retry = lambda sub, log: ("http://oauth/?state=s", "sid", "s")
            worker_pool.run_second_half = fake_run
            try:
                outcome = worker_pool._run_phase2_with_retry(
                    wid=1,
                    cfg={
                        "sub2api": {"url": "u", "email": "e", "pwd": "p"},
                        "icloud": {},
                        "msoutlook": {"helper_url": ""},
                    },
                    phase1_result={"phone": "+1", "password": "pw"},
                    initial_lease=lease,
                    allocator=allocator,
                    log=lambda msg, tag="info": None,
                    stop_event=threading.Event(),
                )
            finally:
                worker_pool._get_oauth_session_with_retry = old_get
                worker_pool.run_second_half = old_run

            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.final_email, "new@example.com")
            self.assertEqual(calls["run"], 2)

            records = json.loads(used_file.read_text(encoding="utf-8"))["records"]
            self.assertEqual(records["old@example.com"]["status"], "error")
            self.assertEqual(records["old@example.com"]["phone"], "+1")
            self.assertEqual(records["old@example.com"]["password"], "pw")

    def test_worker_phase2_failure_saves_fail_phase2_without_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "only@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()
            cancel_jobs = []

            old_register_one = worker_pool.ar.register_one
            old_phase2 = worker_pool._run_phase2_with_retry
            old_sms_action = worker_pool._sms_action
            old_cancel_async = worker_pool._sms_cancel_async
            worker_pool.ar.register_one = lambda *args, **kwargs: {
                "ok": True,
                "phone": "+100",
                "password": "pw",
                "session_token": "st",
                "access_token": "at",
                "activation_id": "aid",
            }
            def fake_phase2(**kwargs):
                return worker_pool.Phase2Outcome(
                    ok=False,
                    final_email=kwargs["initial_lease"].email,
                    lease=kwargs["initial_lease"],
                    error="exchange-code: 500",
                )
            worker_pool._run_phase2_with_retry = fake_phase2
            worker_pool._sms_action = lambda cfg, aid, action: True
            worker_pool._sms_cancel_async = lambda cfg, aid, phone, reason, log, state_obj: cancel_jobs.append((aid, phone, reason)) or state_obj.record_cancelled()
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=60,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                worker_pool._run_phase2_with_retry = old_phase2
                worker_pool._sms_action = old_sms_action
                worker_pool._sms_cancel_async = old_cancel_async
                router.restore()

            snapshot = state.snapshot()
            self.assertEqual(snapshot["full_success"], 0)
            self.assertEqual(snapshot["phase2_failed"], 1)
            self.assertEqual(cancel_jobs, [("aid", "+100", "Phase 2 失败")])
            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["status"], "fail_phase2")
            self.assertEqual(all_data[-1]["password"], "pw")

    def test_worker_phase1_failure_queues_cancel_without_blocking_sms_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "only@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()
            cancel_jobs = []

            old_register_one = worker_pool.ar.register_one
            old_sms_action = worker_pool._sms_action
            old_cancel_async = worker_pool._sms_cancel_async

            worker_pool.ar.register_one = lambda *args, **kwargs: {
                "ok": False,
                "phone": "+100",
                "password": "pw",
                "activation_id": "aid",
                "error": "验证码超时",
            }

            def fail_if_blocking_cancel(*_args, **_kwargs):
                raise AssertionError("_sms_action should not be used for failed cancellation")

            def fake_cancel_async(cfg, activation_id, phone, reason, log, state_obj):
                cancel_jobs.append((activation_id, phone, reason))
                state_obj.record_cancelled()
                stop.set()

            worker_pool._sms_action = fail_if_blocking_cancel
            worker_pool._sms_cancel_async = fake_cancel_async
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=0,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                worker_pool._sms_action = old_sms_action
                worker_pool._sms_cancel_async = old_cancel_async
                router.restore()

            self.assertEqual(cancel_jobs, [("aid", "+100", "Phase 1 失败")])
            snapshot = state.snapshot()
            self.assertEqual(snapshot["phase1_failed"], 1)
            self.assertEqual(snapshot["cancelled"], 1)
            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["status"], "fail_phase1")

    def test_worker_no_numbers_releases_email_without_failed_record_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "one@example.com", "enabled": True, "used": False},
                    {"email": "two@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()
            calls = {"register": 0}

            old_register_one = worker_pool.ar.register_one

            def fake_register_one(*args, **kwargs):
                calls["register"] += 1
                return {
                    "ok": False,
                    "phone": "?",
                    "password": "pw",
                    "activation_id": "",
                    "error": "获取号码失败: NO_NUMBERS",
                }

            worker_pool.ar.register_one = fake_register_one
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=60,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                router.restore()

            self.assertTrue(stop.is_set())
            self.assertEqual(calls["register"], 1)
            self.assertFalse((tmp_path / "results" / "_all.json").exists())
            files = list((tmp_path / "results").glob("*_fail_phase1.json"))
            self.assertEqual(files, [])
            records = json.loads(used_file.read_text(encoding="utf-8"))["records"]
            self.assertEqual(records, {})
            self.assertEqual(allocator.pool.get_available_email(), "one@example.com")

    def test_worker_complete_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "only@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()

            old_register_one = worker_pool.ar.register_one
            old_phase2 = worker_pool._run_phase2_with_retry
            old_sms_action = worker_pool._sms_action
            worker_pool.ar.register_one = lambda *args, **kwargs: {
                "ok": True,
                "phone": "+100",
                "password": "pw",
                "session_token": "st",
                "access_token": "at",
                "activation_id": "aid",
            }

            def fake_phase2(**kwargs):
                return worker_pool.Phase2Outcome(
                    ok=True,
                    final_email=kwargs["initial_lease"].email,
                    lease=kwargs["initial_lease"],
                    sub2api_id="sub-id",
                    import_data={"accounts": [{"name": "only@example.com"}]},
                )

            def fake_sms_action(_cfg, _aid, action):
                if action == "complete":
                    raise RuntimeError("complete api failed")
                return True

            worker_pool._run_phase2_with_retry = fake_phase2
            worker_pool._sms_action = fake_sms_action
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=60,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                worker_pool._run_phase2_with_retry = old_phase2
                worker_pool._sms_action = old_sms_action
                router.restore()

            snapshot = state.snapshot()
            self.assertEqual(snapshot["full_success"], 0)
            self.assertEqual(snapshot["phase2_failed"], 1)
            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["status"], "fail_phase2")
            self.assertIn("complete failed", all_data[-1]["phase2_error"])
            self.assertEqual(all_data[-1]["password"], "pw")

            records = json.loads(used_file.read_text(encoding="utf-8"))["records"]
            self.assertEqual(records["only@example.com"]["status"], "error")
            self.assertEqual(records["only@example.com"]["phone"], "+100")
            self.assertEqual(records["only@example.com"]["password"], "pw")

    def test_worker_ctrl_c_after_phase1_saves_interrupted_and_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "only@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()
            cancel_jobs = []

            old_register_one = worker_pool.ar.register_one
            old_phase2 = worker_pool._run_phase2_with_retry
            old_sms_action = worker_pool._sms_action
            old_cancel_async = worker_pool._sms_cancel_async
            def fake_register_one(*args, **kwargs):
                stop.set()
                return {
                    "ok": True,
                    "phone": "+100",
                    "password": "pw",
                    "session_token": "st",
                    "access_token": "at",
                    "activation_id": "aid",
                }
            worker_pool.ar.register_one = fake_register_one
            worker_pool._run_phase2_with_retry = lambda **kwargs: self.fail("Phase 2 should not start after stop")
            worker_pool._sms_action = lambda cfg, aid, action: True
            worker_pool._sms_cancel_async = lambda cfg, aid, phone, reason, log, state_obj: cancel_jobs.append((aid, phone, reason)) or state_obj.record_cancelled()
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=60,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                worker_pool._run_phase2_with_retry = old_phase2
                worker_pool._sms_action = old_sms_action
                worker_pool._sms_cancel_async = old_cancel_async
                router.restore()

            snapshot = state.snapshot()
            self.assertEqual(snapshot["full_success"], 0)
            self.assertEqual(snapshot["interrupted"], 1)
            self.assertEqual(cancel_jobs, [("aid", "+100", "中断收尾")])
            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["status"], "interrupted_after_phase1")
            self.assertEqual(all_data[-1]["password"], "pw")

    def test_worker_success_records_password_in_results_and_msoutlook_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_path = tmp_path / "pool.json"
            used_file = tmp_path / "used.json"
            pool_path.write_text(
                json.dumps([
                    {"email": "only@example.com", "enabled": True, "used": False},
                ]),
                encoding="utf-8",
            )

            allocator = worker_pool.EmailAllocator("", pool_path=str(pool_path), used_file=str(used_file))
            writer = worker_pool.ResultWriter(tmp_path / "results", tmp_path / "imports")
            router = worker_pool.ThreadStdoutRouter.install_once()
            state = worker_pool.RunState(target_success=1)
            stop = threading.Event()

            old_register_one = worker_pool.ar.register_one
            old_phase2 = worker_pool._run_phase2_with_retry
            old_sms_action = worker_pool._sms_action
            worker_pool.ar.register_one = lambda *args, **kwargs: {
                "ok": True,
                "phone": "+100",
                "password": "pw",
                "session_token": "st",
                "access_token": "at",
                "activation_id": "aid",
            }
            worker_pool._run_phase2_with_retry = lambda **kwargs: worker_pool.Phase2Outcome(
                ok=True,
                final_email=kwargs["initial_lease"].email,
                lease=kwargs["initial_lease"],
                sub2api_id="sub-id",
                import_data={"accounts": [{"name": "only@example.com"}]},
            )
            worker_pool._sms_action = lambda cfg, aid, action: True
            try:
                worker_pool.worker(
                    wid=1,
                    config={"sub2api": {"url": "u", "email": "e", "pwd": "p"}, "msoutlook": {"helper_url": ""}},
                    target_count=1,
                    global_stop=stop,
                    allocator=allocator,
                    result_writer=writer,
                    router=router,
                    log_lock=threading.Lock(),
                    state=state,
                    step_retries=0,
                    create_retries=1,
                    cooldown=60,
                    phase2_timeout=1,
                )
            finally:
                worker_pool.ar.register_one = old_register_one
                worker_pool._run_phase2_with_retry = old_phase2
                worker_pool._sms_action = old_sms_action
                router.restore()

            all_data = json.loads((tmp_path / "results" / "_all.json").read_text(encoding="utf-8"))
            self.assertEqual(all_data[-1]["status"], "ok")
            self.assertEqual(all_data[-1]["password"], "pw")

            records = json.loads(used_file.read_text(encoding="utf-8"))["records"]
            self.assertEqual(records["only@example.com"]["phone"], "+100")
            self.assertEqual(records["only@example.com"]["password"], "pw")


if __name__ == "__main__":
    unittest.main()
