import errno
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

import worker_control


class WorkerControlTests(unittest.TestCase):
    def make_root(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "results").mkdir()
        (root / "imports").mkdir()
        (root / "public").mkdir()
        (root / "worker_pool.py").write_text("print('dummy')\n", encoding="utf-8")
        (root / "public" / "worker-control.html").write_text("ok", encoding="utf-8")
        return tmp, root

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_worker_process_controller_fake_command_logs_and_status(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        c = worker_control.WorkerProcessController(root)
        ok, data = c.start({}, command=[sys.executable, "-c", "print('hello'); print('done full_success=1 phase1_failed=2 phase2_failed=3 cancelled=4 interrupted=5 attempts=6')"])
        self.assertTrue(ok, data)
        deadline = time.time() + 5
        while c.status()["running"] and time.time() < deadline:
            time.sleep(0.05)
        st = c.status()
        self.assertFalse(st["running"])
        self.assertEqual(st["summary"]["full_success"], 1)
        logs = c.log_since(0)
        self.assertTrue(any("hello" in x["text"] for x in logs["lines"]))

    def test_dashboard_summary_counts_pool_imports_and_accounts(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "号.json", [
            {"email": "a@example.test", "enabled": True, "used": False},
            {"email": "b@example.test", "enabled": True, "used": False},
            {"email": "c@example.test", "enabled": True, "used": True},
        ])
        self.write_json(root / "msoutlook_used.json", {"records": {
            "b@example.test": {"status": "used", "phone": "+2"},
            "c@example.test": {"status": "error", "error": "bad"},
        }})
        self.write_json(root / "imports" / "import_20260605.json", {
            "type": "sub2api-data",
            "accounts": [
                {"name": "a@example.test", "credentials": {"email": "a@example.test"}},
                {"name": "x@example.test", "credentials": {"email": "x@example.test"}},
            ],
        })
        self.write_json(root / "results" / "_all.json", [
            {"status": "ok", "phone": "+1", "password": "p", "bind_email": "a@example.test", "sub2api_id": "1", "saved_at": "2026-06-05T01:00:00"},
            {"status": "fail_phase1", "phone": "+2", "password": "p"},
            {"status": "fail_phase2", "phone": "+3", "password": "p", "session_token": "s"},
        ])
        self.write_json(root / "results" / "account_state.json", {"sub:1": {"usage_status": "used", "export_status": "exported"}})
        summary = worker_control.dashboard_summary(root)
        self.assertEqual(summary["pool"]["import_added_total"], 2)
        self.assertEqual(summary["pool"]["total"], 3)
        self.assertEqual(summary["accounts"]["registered_success"], 1)
        self.assertEqual(summary["accounts"]["registered_failed"], 2)
        self.assertEqual(summary["accounts"]["trial_success"], 1)

    def test_account_filters_batch_state_and_retry_info(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "results" / "_all.json", [
            {"status": "ok", "phone": "+1", "password": "p", "bind_email": "a@example.test", "sub2api_id": "1", "saved_at": "2026-06-05T01:00:00"},
            {"status": "fail_phase2", "phone": "+2", "password": "p", "session_token": "s", "bind_email": "b@example.test", "saved_at": "2026-06-05T02:00:00"},
            {"status": "fail_phase2", "phone": "+3", "password": "", "bind_email": "c@example.test", "saved_at": "2026-06-05T03:00:00"},
        ])
        store = worker_control.AccountStore(root)
        listed = store.list_accounts({"reg_status": "fail_phase2", "retryable": "true"})
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["retry"]["recommended_stage"], "phase2")
        bad = store.list_accounts({"q": "+3"})["items"][0]
        self.assertFalse(bad["retry"]["retryable"])
        self.assertIn("password", bad["retry"]["missing"])
        res = store.batch_patch_state("filtered", [], {"reg_status": "ok"}, {"usage_status": "reserved", "note": "hold"})
        self.assertEqual(res["updated"], 1)
        ok_item = store.list_accounts({"reg_status": "ok"})["items"][0]
        self.assertEqual(ok_item["usage_status"], "reserved")
        self.assertEqual(ok_item["note"], "hold")

    def test_success_and_failed_account_views_are_separated(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "results" / "_all.json", [
            {"status": "ok", "phone": "+1", "password": "p", "bind_email": "a@example.test", "sub2api_id": "1"},
            {"status": "fail_phase1", "phone": "+2", "password": "p"},
            {"status": "fail_phase2", "phone": "+3", "password": "p", "session_token": "s"},
        ])
        store = worker_control.AccountStore(root)
        success = store.list_accounts({"success_only": "1"})
        self.assertEqual(success["total"], 1)
        self.assertEqual(success["items"][0]["reg_status"], "ok")
        self.assertEqual(success["items"][0]["export_status"], "exported")
        failed = store.list_accounts({"failed_only": "1"})
        self.assertEqual(failed["total"], 2)
        self.assertTrue(all(x["reg_status"] != "ok" for x in failed["items"]))

    def test_export_sub_payload_marks_exported(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "results" / "_all.json", [
            {"status": "ok", "phone": "+1", "password": "p", "bind_email": "a@example.test", "sub2api_id": "1"},
            {"status": "ok", "phone": "+2", "password": "p", "bind_email": "b@example.test", "sub2api_id": "2"},
        ])
        self.write_json(root / "imports" / "import_20260605.json", {
            "type": "sub2api-data",
            "accounts": [
                {"name": "a@example.test", "credentials": {"email": "a@example.test", "access_token": "at", "refresh_token": "rt"}}
            ]
        })
        store = worker_control.AccountStore(root)
        payload, keys = worker_control.export_sub_payload(store, "filtered", [], {"reg_status": "ok"}, mark_exported=True)
        self.assertEqual(len(payload["accounts"]), 1)
        self.assertEqual(keys, ["sub:1"])
        state = worker_control.read_json(root / "results" / "account_state.json", {})
        self.assertEqual(state["sub:1"]["export_status"], "exported")

    def test_config_raw_rejects_invalid_json_without_overwrite(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        (root / "config.json").write_text('{"ok": true}\n', encoding="utf-8")
        app = Flask(__name__)
        app.register_blueprint(worker_control.create_worker_control_blueprint(root))
        client = app.test_client()
        r = client.put("/api/config/raw", json={"text": "{"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual((root / "config.json").read_text(encoding="utf-8"), '{"ok": true}\n')
        r = client.put("/api/config/raw", json={"text": '{"sms_provider":"smsbower"}'})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(json.loads((root / "config.json").read_text(encoding="utf-8"))["sms_provider"], "smsbower")

    def test_config_raw_falls_back_when_bind_mount_replace_is_busy(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        config = root / "config.json"
        config.write_text('{"ok": true}\n', encoding="utf-8")
        app = Flask(__name__)
        app.register_blueprint(worker_control.create_worker_control_blueprint(root))
        client = app.test_client()
        with mock.patch.object(Path, "replace", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
            r = client.put("/api/config/raw", json={"text": '{"sms_provider":"hero-sms"}'})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["sms_provider"], "hero-sms")
        self.assertEqual(list(root.glob("config.json.*.tmp")), [])

    def test_config_form_updates_nested_values_and_helper_bat(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "config.json", {
            "sms_provider": "smsbower",
            "msoutlook": {"helper_url": "http://127.0.0.1:17373", "email": ""},
            "sub2api": {"proxy_id": 0},
        })
        app = Flask(__name__)
        app.register_blueprint(worker_control.create_worker_control_blueprint(root))
        client = app.test_client()
        payload = {"values": {
            "sms_provider": "hero-sms",
            "msoutlook.helper_url": "http://127.0.0.1:17374",
            "msoutlook.helper_bat": r"E:\song\下载\GooGle Downloads\GuJumpgate-v0.1.3\GuJumpgate-v0.1.3\start-hotmail-helper.bat",
            "sub2api.proxy_id": "12",
        }}
        r = client.put("/api/config/form", json=payload)
        self.assertTrue(r.get_json()["ok"])
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["sms_provider"], "hero-sms")
        self.assertEqual(cfg["msoutlook"]["helper_url"], "http://127.0.0.1:17374")
        self.assertEqual(cfg["sub2api"]["proxy_id"], 12)

    def test_log_streams_split_worker_logs(self):
        tmp, root = self.make_root()
        self.addCleanup(tmp.cleanup)
        controller = worker_control.WorkerProcessController(root)
        controller._append_log("[info] [10:00:00] [W1] hello")
        controller._append_log("[info] [10:00:01] [W2] world")
        controller._append_log("[INFO] main")
        streams = {x["stream"]: x["count"] for x in controller.log_streams()["streams"]}
        self.assertEqual(streams["W1"], 1)
        self.assertEqual(streams["W2"], 1)
        self.assertEqual(streams["main"], 1)
        only_w1 = controller.log_since(0, "W1")
        self.assertEqual(len(only_w1["lines"]), 1)
        self.assertEqual(only_w1["lines"][0]["stream"], "W1")


if __name__ == "__main__":
    unittest.main()
