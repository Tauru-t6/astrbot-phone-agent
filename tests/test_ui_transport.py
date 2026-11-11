"""Bound UI commands never replay an uncertain POST on another route."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_device_app as fixture

device_app = fixture.device_app


class UiTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audit = fixture.RecordingAudit()
        self.config = fixture.FakeConfig(app_shared_token="synthetic-token", app_command_timeout_seconds=60,
            app_state_path=str(Path(self.temp.name) / "timeline.json"),
            relay_base_url="https://relay.example", relay_token="synthetic-relay-token")
        self.backend = device_app.DeviceAppBackend(self.config, self.audit)
        self.backend.register({"device_id": "bot-device", "app_version": "0.4.0",
                               "base_urls": ["http://first.example:8260", "http://second.example:8260"]})

    @staticmethod
    def result(envelope, **changes):
        return {"type": "result", "schema_version": 1, "command_id": envelope["command_id"],
                "action": envelope["action"], "success": True, "output": {}, **changes}

    def args(self, action):
        return {"expected_package": "com.example.synthetic", **(
            {"snapshot_id": "synthetic-snapshot", "kind": "tap", "node_id": "n1"} if action == "ui_action" else {})}

    def test_unknown_ui_post_stops_without_second_route_or_relay(self):
        for action in ["ui_snapshot", "ui_action"]:
            for error in ["timeout", "connection_failed", "http_error"]:
                with self.subTest(action=action, error=error):
                    requests = []
                    def http(url, token, method="GET", payload=None, timeout=15):
                        requests.append((url, method))
                        if method == "GET":
                            return {"success": True}
                        return {"success": False, "error_code": error}
                    with patch.object(self.backend, "_http_json", side_effect=http), \
                            patch.object(self.backend, "_relay_submit") as relay:
                        result = self.backend.execute(action, self.args(action), 55, expected_binding=self.backend.binding())
                    self.assertFalse(result["success"])
                    self.assertEqual(result["outcome"], "unknown")
                    self.assertEqual(result["error_code"], error)
                    self.assertEqual([url for url, method in requests if method == "POST"], ["http://first.example:8260/command"])
                    self.assertFalse(any("second.example" in url for url, _ in requests))
                    relay.assert_not_called()

    def test_malformed_or_mismatched_post_result_is_unknown_and_never_replayed(self):
        changes = [{"command_id": "different-command"}, {"action": "home"}, {"schema_version": 99},
                   {"success": "true"}, {"type": "other-envelope"}]
        for values in changes:
            with self.subTest(values=values):
                commands = []
                def http(url, token, method="GET", payload=None, timeout=15):
                    if method == "GET":
                        return {"success": True}
                    commands.append(payload)
                    return self.result(payload, **values)
                with patch.object(self.backend, "_http_json", side_effect=http), \
                        patch.object(self.backend, "_relay_submit") as relay:
                    result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                                  expected_binding=self.backend.binding())
                self.assertEqual(result["error_code"], "invalid_result")
                self.assertEqual(result.get("outcome"), "unknown")
                self.assertEqual(len(commands), 1)
                relay.assert_not_called()

    def test_matching_local_denial_is_final_and_does_not_fall_back(self):
        for full_envelope in [False, True]:
            with self.subTest(full_envelope=full_envelope):
                commands = []
                def http(url, token, method="GET", payload=None, timeout=15):
                    if method == "GET":
                        return {"success": True}
                    commands.append(payload)
                    denial = {"success": False, "error_code": "denied", "error": "synthetic local denial"}
                    return self.result(payload, **denial) if full_envelope else denial
                with patch.object(self.backend, "_http_json", side_effect=http), \
                        patch.object(self.backend, "_relay_submit") as relay:
                    result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                                  expected_binding=self.backend.binding())
                self.assertEqual(result["error_code"], "denied")
                self.assertNotEqual(result.get("outcome"), "unknown")
                self.assertEqual(len(commands), 1)
                relay.assert_not_called()

    def test_ui_envelope_binds_target_and_does_not_divide_slow_action_budget(self):
        commands = []
        def http(url, token, method="GET", payload=None, timeout=15):
            if method == "GET":
                return {"success": True}
            commands.append((payload, timeout))
            return self.result(payload)
        with patch.object(self.backend, "_http_json", side_effect=http):
            result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                          expected_binding=self.backend.binding())
        self.assertTrue(result["success"])
        envelope, timeout = commands[0]
        self.assertEqual(envelope["target_device_id"], "bot-device")
        self.assertEqual(envelope["deadline"], 55)
        self.assertTrue(envelope["dangerous"])
        self.assertGreater(timeout, 50)
        self.assertLessEqual(timeout, 55)

    def test_changed_binding_before_transport_never_makes_network_request(self):
        binding = self.backend.binding()
        self.backend.register({"device_id": "new-device", "base_url": "http://new.example:8260"})
        with patch.object(self.backend, "_http_json") as http, patch.object(self.backend, "_relay_submit") as relay:
            result = self.backend.execute("ui_action", self.args("ui_action"), 55, expected_binding=binding)
        self.assertEqual(result["error_code"], "device_changed")
        http.assert_not_called()
        relay.assert_not_called()

    def test_endpoint_changed_during_health_probe_never_posts_to_old_or_new_route(self):
        binding = self.backend.binding()
        requests = []
        def http(url, token, method="GET", payload=None, timeout=15):
            requests.append((url, method))
            self.backend.register({"device_id": "bot-device", "base_url": "http://new.example:8260"})
            return {"success": True}
        with patch.object(self.backend, "_http_json", side_effect=http), patch.object(self.backend, "_relay_submit") as relay:
            result = self.backend.execute("ui_action", self.args("ui_action"), 55, expected_binding=binding)
        self.assertEqual(result["error_code"], "device_changed")
        self.assertEqual([method for _, method in requests], ["GET"])
        relay.assert_not_called()

    def test_endpoint_changed_during_post_marks_unknown_without_retry(self):
        binding = self.backend.binding()
        commands = []
        def http(url, token, method="GET", payload=None, timeout=15):
            if method == "GET":
                return {"success": True}
            commands.append(payload)
            self.backend.register({"device_id": "replacement-device", "base_url": "http://new.example:8260"})
            return self.result(payload)
        with patch.object(self.backend, "_http_json", side_effect=http), patch.object(self.backend, "_relay_submit") as relay:
            result = self.backend.execute("ui_action", self.args("ui_action"), 55, expected_binding=binding)
        self.assertEqual(result["error_code"], "device_changed")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual([item["target_device_id"] for item in commands], ["bot-device"])
        relay.assert_not_called()

    def test_relay_fallback_before_any_direct_post_keeps_target_in_both_envelopes(self):
        queued = []
        def http(url, token, method="GET", payload=None, timeout=15):
            if url.startswith("http://"):
                self.assertEqual(method, "GET")
                return {"success": False, "error_code": "connection_failed"}
            if url.endswith("/health"):
                return {"success": True, "targeted_tasks": True}
            if url.endswith("/task"):
                queued.append(payload)
                return {"success": True, "task_id": "synthetic-relay-task"}
            if url.endswith("/result/synthetic-relay-task"):
                envelope = json.loads(queued[0]["message"])
                return {"success": True, "result": {"status": "done", "ai_response": json.dumps(self.result(envelope))}}
            raise AssertionError("unexpected synthetic URL")
        with patch.object(self.backend, "_http_json", side_effect=http):
            result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                          expected_binding=self.backend.binding())
        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "relay")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["target_device_id"], "bot-device")
        self.assertEqual(json.loads(queued[0]["message"])["target_device_id"], "bot-device")

    def test_relay_without_device_targeting_is_rejected_before_queueing(self):
        self.backend.register({"device_id": "bot-device"})
        requests = []
        def http(url, token, method="GET", payload=None, timeout=15):
            requests.append((url, method))
            return {"success": True, "targeted_tasks": False}
        with patch.object(self.backend, "_http_json", side_effect=http):
            result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                          expected_binding=self.backend.binding())
        self.assertEqual(result["error_code"], "relay_targeting_required")
        self.assertFalse(any(method == "POST" for _, method in requests))

    def test_cancelled_during_direct_probe_never_dispatches_or_tries_relay(self):
        for explicit_binding in [True, False]:
            with self.subTest(explicit_binding=explicit_binding):
                allowed = True
                requests = []
                def http(url, token, method="GET", payload=None, timeout=15):
                    nonlocal allowed
                    requests.append((url, method))
                    allowed = False
                    return {"success": True}
                self.backend._active_url = ""
                options = {"expected_binding": self.backend.binding()} if explicit_binding else {}
                with patch.object(self.backend, "_http_json", side_effect=http), patch.object(self.backend, "_relay_submit") as relay:
                    result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                                                  dispatch_allowed=lambda: allowed, **options)
                self.assertEqual(result["error_code"], "cancelled")
                self.assertFalse(any(method == "POST" for _, method in requests))
                relay.assert_not_called()

    def test_cancelled_during_relay_probe_never_queues_command(self):
        self.backend.register({"device_id": "bot-device"})
        allowed = True
        requests = []
        def http(url, token, method="GET", payload=None, timeout=15):
            nonlocal allowed
            requests.append((url, method))
            allowed = False
            return {"success": True, "targeted_tasks": True}
        with patch.object(self.backend, "_http_json", side_effect=http):
            result = self.backend.execute("ui_action", self.args("ui_action"), 55,
                expected_binding=self.backend.binding(), dispatch_allowed=lambda: allowed)
        self.assertEqual(result["error_code"], "cancelled")
        self.assertFalse(any(method == "POST" for _, method in requests))


if __name__ == "__main__":
    unittest.main()
