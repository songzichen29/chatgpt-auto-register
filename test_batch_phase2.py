import unittest

import batch_phase2


class FakePool:
    def __init__(self, email="bind@example.com"):
        self.email = email
        self.used = []
        self.errors = []

    def get_available_email(self):
        return self.email

    def mark_used(self, email, **kwargs):
        self.used.append((email, kwargs))

    def mark_error(self, email, reason, **kwargs):
        self.errors.append((email, reason, kwargs))


class BatchPhase2Tests(unittest.TestCase):
    def test_missing_email_fallback_retries_codex_oauth_and_succeeds(self):
        old_get_oauth_url = batch_phase2.get_oauth_url
        old_run_second_half = batch_phase2.run_second_half
        old_fix = batch_phase2.complete_about_you_via_chat_client
        old_max_oauth_retries = batch_phase2.MAX_OAUTH_RETRIES
        try:
            calls = {"oauth": 0, "phase2": 0, "fix": 0}

            def fake_get_oauth_url():
                calls["oauth"] += 1
                return (f"https://auth.example/{calls['oauth']}", f"sess-{calls['oauth']}", f"state-{calls['oauth']}")

            def fake_run_second_half(**kwargs):
                calls["phase2"] += 1
                if calls["phase2"] == 1:
                    return {"ok": False, "error": "codex_about_you_missing_email: about_you"}
                return {"ok": True, "sub2api_account_id": "sub-123", "import_data": {"ok": True}}

            def fake_fix(phone, password):
                calls["fix"] += 1
                self.assertEqual(phone, "+100")
                return {"ok": True, "page": "add_email"}

            batch_phase2.get_oauth_url = fake_get_oauth_url
            batch_phase2.run_second_half = fake_run_second_half
            batch_phase2.complete_about_you_via_chat_client = fake_fix
            batch_phase2.MAX_OAUTH_RETRIES = 3

            pool = FakePool()
            result = batch_phase2.run_phase2_for_phone("+100", pool)

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("sub2api_account_id"), "sub-123")
            self.assertEqual(calls, {"oauth": 2, "phase2": 2, "fix": 1})
            self.assertEqual(pool.used[0][0], "bind@example.com")
            self.assertFalse(pool.errors)
        finally:
            batch_phase2.get_oauth_url = old_get_oauth_url
            batch_phase2.run_second_half = old_run_second_half
            batch_phase2.complete_about_you_via_chat_client = old_fix
            batch_phase2.MAX_OAUTH_RETRIES = old_max_oauth_retries


if __name__ == "__main__":
    unittest.main()
