import importlib.util
import http.client
import json
import tempfile
import time
import unittest
import threading
from pathlib import Path
from unittest.mock import patch


RELAY_PATH = Path(__file__).parents[1] / "deploy" / "relay" / "relay_server.py"
SPEC = importlib.util.spec_from_file_location("phone_agent_relay_test", RELAY_PATH)
relay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(relay)


class RelayQueueTests(unittest.TestCase):
    def setUp(self):
        self.state_file = Path(tempfile.mktemp(suffix=".json"))
        relay.STATE_PATH = self.state_file
        relay._state = {"tasks": [], "results": {}}

    def tearDown(self):
        self.state_file.unlink(missing_ok=True)
        self.state_file.with_suffix(".tmp").unlink(missing_ok=True)

    def test_claim_is_owned_and_can_be_renewed(self):
        task = relay.push_task({"message": "open settings"})
        claimed = relay.poll_task("phone-a")
        self.assertEqual(claimed["id"], task["id"])
        self.assertFalse(relay.renew_task(task["id"], "phone-b"))
        self.assertTrue(relay.renew_task(task["id"], "phone-a"))
        self.assertFalse(relay.submit_result(task["id"], True, "wrong", device_id="phone-b"))
        self.assertTrue(relay.submit_result(task["id"], True, "done", device_id="phone-a"))

    def test_expired_claim_returns_to_queue(self):
        task = relay.push_task({"message": json.dumps({"type": "command", "schema_version": 1,
                                                       "command_id": "replay-safe", "action": "ping"})})
        relay.poll_task("phone-a")
        relay._state["tasks"][0]["claimed_at"] = time.time() - relay.CLAIM_LEASE_SECONDS - 1
        reclaimed = relay.poll_task("phone-b")
        self.assertEqual(reclaimed["id"], task["id"])
        self.assertEqual(reclaimed["device_id"], "phone-b")

    def test_result_is_persisted(self):
        task = relay.push_task({"message": "hello"})
        relay.poll_task("phone-a")
        relay.submit_result(task["id"], False, "failed", "offline", "phone-a")
        result = relay.get_result(task["id"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "offline")

    def test_b08_long_command_not_truncated(self):
        """B08: a large structured command envelope must survive the relay intact."""
        envelope = json.dumps({
            "type": "command", "schema_version": 1, "command_id": "c-abc123",
            "action": "input_text",
            "args": {"text": "很长的中文输入内容" * 200},
            "created_at": time.time(), "dangerous": True, "deadline": 120,
        }, ensure_ascii=False)
        task = relay.push_task({"message": envelope})
        self.assertEqual(task["message"], envelope)
        self.assertEqual(len(task["message"]), len(envelope))

    def test_b08_long_result_not_truncated(self):
        """B08: a large result envelope (e.g. screen_text output) must not be cropped."""
        large_output = json.dumps({"nodes": [{"text": "屏幕文字内容" * 50} for _ in range(100)]},
                                  ensure_ascii=False)
        task = relay.push_task({"message": "screen_text"})
        relay.poll_task("phone-a")
        relay.submit_result(task["id"], True, large_output, "", "phone-a")
        result = relay.get_result(task["id"])
        self.assertEqual(result["ai_response"], large_output)
        self.assertEqual(len(result["ai_response"]), len(large_output))

    def test_b08_max_body_bytes_supports_4mb(self):
        """B08: MAX_BODY_BYTES must be 4 MiB to match the plugin's read ceiling."""
        self.assertEqual(relay.MAX_BODY_BYTES, 4 * 1024 * 1024)

    def test_empty_poll_does_not_rewrite_state(self):
        with patch.object(relay, "_persist") as persist:
            self.assertIsNone(relay.poll_task("phone-a"))
        persist.assert_not_called()

    def test_targeted_poll_skips_legacy_and_other_device_without_claiming_them(self):
        legacy = relay.push_task({"message": "legacy personal task"})
        personal = relay.push_task({"message": "personal task", "target_device_id": "personal-1"})
        bot = relay.push_task({"message": "bot task", "target_device_id": "bot-1"})
        claimed = relay.poll_task("bot-1", targeted_only=True)
        self.assertEqual(claimed["id"], bot["id"])
        self.assertEqual(claimed["device_id"], "bot-1")
        self.assertEqual(relay.get_result(legacy["id"])["status"], "pending")
        self.assertEqual(relay.get_result(personal["id"])["status"], "pending")
        self.assertIsNone(relay.poll_task("bot-1", targeted_only=True))
        self.assertEqual(relay.poll_task("personal-1")["id"], legacy["id"])
        self.assertEqual(relay.poll_task("personal-1")["id"], personal["id"])

    def test_targeted_poll_requires_identity_even_when_legacy_mode_is_enabled(self):
        task = relay.push_task({"message": "legacy"})
        for device_id in [None, ""]:
            with self.subTest(device_id=device_id), self.assertRaises(ValueError):
                relay.poll_task(device_id, targeted_only=True)
        self.assertEqual(relay.get_result(task["id"])["status"], "pending")

    def test_target_survives_restart_and_expired_claim_cannot_move_to_other_device(self):
        task = relay.push_task({"message": json.dumps({"type": "command", "schema_version": 1,
            "command_id": "targeted-retry", "action": "ping"}), "target_device_id": "bot-1"})
        relay.poll_task("bot-1", targeted_only=True)
        relay._state["tasks"][0]["claimed_at"] = time.time() - relay.CLAIM_LEASE_SECONDS - 1
        relay._persist()
        relay._state = {"tasks": [], "results": {}}
        relay._load()
        self.assertIsNone(relay.poll_task("personal-1"))
        self.assertEqual(relay.poll_task("bot-1", targeted_only=True)["id"], task["id"])

    def test_result_requires_a_live_claim(self):
        task = relay.push_task({"message": "hello"})
        self.assertFalse(relay.submit_result(task["id"], True, "done", device_id="phone-a"))
        relay.poll_task("phone-a")
        relay._state["tasks"][0]["claimed_at"] = time.time() - relay.CLAIM_LEASE_SECONDS - 1
        self.assertFalse(relay.submit_result(task["id"], True, "late", device_id="phone-a"))
        self.assertFalse(relay.renew_task(task["id"], "phone-a"))

    def test_claimed_tasks_count_toward_capacity(self):
        with patch.object(relay, "MAX_TASKS", 1):
            relay.push_task({"message": "hello"})
            relay.poll_task("phone-a")
            with self.assertRaises(OverflowError):
                relay.push_task({"message": "another"})

    def test_stale_pending_does_not_block_new_tasks(self):
        with patch.object(relay, "MAX_TASKS", 1):
            old = relay.push_task({"message": "old"})
            relay._state["tasks"][0]["created_at"] = time.time() - relay.STALE_PENDING_SECONDS - 1
            new = relay.push_task({"message": "new"})
            self.assertEqual(relay.get_result(old["id"])["status"], "expired")
            self.assertEqual(relay.poll_task("phone-a")["id"], new["id"])

    def test_returned_task_does_not_mutate_queue(self):
        task = relay.push_task({"message": "hello"})
        task["status"] = "done"
        claimed = relay.poll_task("phone-a")
        claimed["device_id"] = "phone-b"
        self.assertTrue(relay.submit_result(task["id"], True, "ok", device_id="phone-a"))

    def test_load_recovers_valid_tasks_from_malformed_state(self):
        valid = {"id": "valid", "message": "hello", "status": "pending", "created_at": time.time()}
        self.state_file.write_text(json.dumps({"tasks": [None, {}, valid], "results": []}), encoding="utf-8")
        relay._load()
        self.assertEqual(relay.poll_task("phone-a")["id"], "valid")

    def test_failed_atomic_replace_keeps_previous_state(self):
        relay.push_task({"message": "original"})
        previous = self.state_file.read_bytes()
        with patch.object(relay.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                relay._persist()
        self.assertEqual(self.state_file.read_bytes(), previous)
        self.assertEqual(list(self.state_file.parent.glob(self.state_file.name + ".*.tmp")), [])


class RelayHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for name, value in [("STATE_PATH", Path(temporary.name) / "relay.json"),
                            ("_state", {"tasks": [], "results": {}}), ("_rate_buckets", {})]:
            setting = patch.object(relay, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.token = patch.object(relay, "TOKEN", "test-only-token")
        self.token.start()
        self.server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.RelayHandler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.token.stop()

    def post(self, path, value, authorized=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer test-only-token"
        connection.request("POST", path, json.dumps(value), headers)
        response = connection.getresponse()
        result = response.status, response.getheader("Connection"), json.loads(response.read())
        connection.close()
        return result

    def get(self, path, device_header=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Authorization": "Bearer test-only-token"}
        if device_header is not None:
            headers[relay.DEVICE_HEADER] = device_header
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_health_advertises_targeted_polling_capability(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertIs(body["targeted_tasks"], True)
        self.assertIs(body["targeted_polling"], True)

    def test_query_targeted_poll_claims_only_exact_device_and_leaves_legacy_for_personal(self):
        legacy = relay.push_task({"message": "personal legacy"})
        bot = relay.push_task({"message": "bot command", "target_device_id": "bot-1"})
        status, body = self.get("/poll?device_id=bot-1&targeted_only=true", "bot-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], bot["id"])
        self.assertEqual(relay.get_result(legacy["id"])["status"], "pending")
        status, body = self.get("/poll?device_id=bot-1&targeted_only=true", "bot-1")
        self.assertEqual(status, 200)
        self.assertIsNone(body["task"])
        status, body = self.get("/poll", "personal-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], legacy["id"])

    def test_targeted_poll_accepts_query_identity_in_strict_mode(self):
        task = relay.push_task({"message": "bot command", "target_device_id": "bot-1"})
        with patch.object(relay, "REQUIRE_DEVICE_ID", True):
            status, body = self.get("/poll?device_id=bot-1&targeted_only=true")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], task["id"])

    def test_invalid_targeted_query_cannot_degrade_to_legacy_claim(self):
        task = relay.push_task({"message": "personal legacy"})
        cases = [("/poll?targeted_only=true", None), ("/poll?device_id=&targeted_only=true", None),
                 ("/poll?device_id=bot-1&targeted_only=yes", "bot-1"),
                 ("/poll?device_id=bot-1&targeted_only=true&targeted_only=false", "bot-1"),
                 ("/poll?device_id=bot-1&device_id=personal-1&targeted_only=true", None),
                 ("/poll?device_id=bot-1&targeted_only=true", "personal-1"),
                 ("/poll?device_id=bad%20id&targeted_only=true", None)]
        for path, header in cases:
            with self.subTest(path=path, header=header):
                self.assertEqual(self.get(path, header)[0], 400)
                self.assertEqual(relay.get_result(task["id"])["status"], "pending")

    def test_array_payloads_are_rejected_without_handler_crash(self):
        for path in ("/task", "/renew", "/result"):
            self.assertEqual(self.post(path, [1, 2])[0], 400)

    def test_string_success_is_not_coerced_to_true(self):
        self.assertEqual(self.post("/result", {"task_id": "id", "success": "false"})[0], 400)

    def test_unauthorized_body_closes_connection(self):
        status, connection, _ = self.post("/task", {"message": "hello"}, authorized=False)
        self.assertEqual(status, 401)
        self.assertEqual(connection, "close")

    def test_incomplete_body_has_a_bounded_read(self):
        with patch.object(relay, "REQUEST_TIMEOUT_SECONDS", 0.05):
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
            connection.request("POST", "/task", "{}", {
                "Authorization": "Bearer test-only-token", "Content-Length": "100",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 408)
            response.read()
            connection.close()


if __name__ == "__main__":
    unittest.main()
