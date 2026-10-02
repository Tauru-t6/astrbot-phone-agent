import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("device_app_test", PLUGIN_DIR / "device_app.py")
device_app = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(device_app)


class RecordingAudit:
    def __init__(self):
        self.events = []

    def __call__(self, operation, **fields):
        self.events.append((operation, fields))


class FakeConfig(dict):
    def __init__(self, **values):
        super().__init__(**values)

    def get(self, key, default=None):
        return super().get(key, default)


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.backend = device_app.DeviceAppBackend(FakeConfig(app_shared_token="t"), RecordingAudit())

    def test_envelope_shape_and_idempotent_id(self):
        envelope = self.backend.build_envelope("open_app", {"package": "tv.danmaku.bili"})
        self.assertEqual(envelope["type"], "command")
        self.assertEqual(envelope["schema_version"], device_app.COMMAND_SCHEMA_VERSION)
        self.assertTrue(envelope["command_id"].startswith("c-"))
        self.assertEqual(envelope["action"], "open_app")
        self.assertEqual(envelope["args"], {"package": "tv.danmaku.bili"})
        self.assertFalse(envelope["dangerous"])

    def test_dangerous_actions_flagged(self):
        for action in ("location", "close_app", "suspend_app", "suspend_video_apps", "send_notification"):
            envelope = self.backend.build_envelope(action, {})
            self.assertTrue(envelope["dangerous"], action)

    def test_unknown_action_rejected(self):
        result = self.backend.execute("format_device", {})
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "unsupported_action")

    def test_none_args_stripped(self):
        envelope = self.backend.build_envelope("open_app", {"package": "a.b", "minutes": None})
        self.assertEqual(envelope["args"], {"package": "a.b"})


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.audit = RecordingAudit()
        self.backend = device_app.DeviceAppBackend(FakeConfig(app_shared_token="secret"), self.audit)

    def test_register_stores_endpoint(self):
        result = self.backend.register({
            "device_id": "phone-abcd1234",
            "base_url": "http://100.113.6.85:8260",
            "app_version": "0.1.0",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["schema_version"], device_app.COMMAND_SCHEMA_VERSION)
        registration = self.backend.registration()
        self.assertEqual(registration["base_url"], "http://100.113.6.85:8260")
        self.assertEqual([e[0] for e in self.audit.events], ["device_app_registered"])

    def test_register_rejects_bad_payload(self):
        self.assertFalse(self.backend.register({"device_id": "", "base_url": "http://x"})["success"])
        self.assertFalse(self.backend.register({"device_id": "ok1", "base_url": "ftp://x"})["success"])
        self.assertFalse(self.backend.register({"device_id": "bad id!", "base_url": "http://x"})["success"])

    def test_fingerprint_mismatch_rejected(self):
        self.assertFalse(self.backend.register({
            "device_id": "phone-abcd1234",
            "base_url": "http://100.113.6.85:8260",
            "token_fingerprint": "sha256:deadbeefdeadbeef",
        })["success"])

    def test_fingerprint_match_accepted(self):
        fingerprint = self.backend._token_fingerprint()
        self.assertTrue(self.backend.register({
            "device_id": "phone-abcd1234",
            "base_url": "http://100.113.6.85:8260",
            "token_fingerprint": fingerprint,
        })["success"])


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.audit = RecordingAudit()
        self.backend = device_app.DeviceAppBackend(
            FakeConfig(app_shared_token="t", app_command_timeout_seconds=30),
            self.audit,
        )

    def _register(self):
        self.backend.register({"device_id": "phone-x", "base_url": "http://phone:8260"})

    def test_direct_success(self):
        self._register()

        def fake_http(url, token, method="GET", payload=None, timeout=15.0):
            if url.endswith("/command"):
                return {
                    "type": "result",
                    "command_id": payload["command_id"],
                    "success": True,
                    "action": payload["action"],
                    "output": {"model": "Pixel 9"},
                    "finished_at": 1790000000.0,
                }
            raise AssertionError("unexpected url " + url)

        with patch.object(self.backend, "_http_json", side_effect=fake_http):
            result = self.backend.execute("status", {})
        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "direct")
        self.assertEqual(result["output"]["model"], "Pixel 9")
        self.assertEqual(len(self.backend.recent_results()), 1)

    def test_relay_fallback_when_direct_down(self):
        # No registration: direct fails with not_registered, relay takes over.
        responses = []

        def fake_relay(url, token, method="GET", payload=None, timeout=15.0):
            if url.endswith("/task") and method == "POST":
                responses.append("push")
                return {"success": True, "task_id": "r1"}
            if url.endswith("/result/r1"):
                return {
                    "success": True,
                    "result": {
                        "success": True,
                        "ai_response": json.dumps({
                            "type": "result",
                            "command_id": "c-x",
                            "success": True,
                            "action": "ping",
                            "output": {"pong": True},
                            "finished_at": 1790000000.0,
                        }),
                    },
                }
            raise AssertionError("unexpected url " + url)

        with patch.object(self.backend, "_http_json", side_effect=fake_relay), \
                patch.object(device_app.time, "sleep", lambda _: None):
            self.backend._config["relay_base_url"] = "https://relay.example"
            self.backend._config["relay_token"] = "rt"
            result = self.backend.execute("ping", {})
        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "relay")

    def test_status_cache_roundtrip(self):
        self.backend.record_device_status({
            "battery_percent": 73,
            "charging": False,
            "foreground_package": "tv.danmaku.bili",
            "screen_on_seconds_today": 10800,
            "shizuku_ready": True,
            "app_version": "0.1.0",
        })
        status = self.backend.last_status()
        self.assertEqual(status["battery_percent"], 73)
        self.assertTrue(status["shizuku_ready"])
        self.assertEqual(status["screen_on_seconds_today"], 10800)

    def test_status_cache_ignores_garbage(self):
        self.backend.record_device_status({"battery_percent": "not-a-number"})
        self.assertIsNone(self.backend.last_status()["battery_percent"])
        self.backend.record_device_status(None)
        self.assertIsNotNone(self.backend.last_status())


if __name__ == "__main__":
    unittest.main()
