import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path


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
        task = relay.push_task({"message": "observe"})
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


if __name__ == "__main__":
    unittest.main()
