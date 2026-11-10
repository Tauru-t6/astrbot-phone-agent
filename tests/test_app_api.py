"""Real Quart requests with only the AstrBot host interfaces stubbed."""
import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from quart import Quart

ROOT = Path(__file__).parents[1]
api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = logging.getLogger(__name__)
event_api = types.ModuleType("astrbot.api.event")
event_api.AstrMessageEvent = object


class _NoOpFilter:
    class EventMessageType:
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    def __getattr__(self, name):
        return lambda *args, **kwargs: (lambda function: function)


event_api.filter = _NoOpFilter()


class Chain:
    def message(self, text):
        self.text = text
        return self


event_api.MessageChain = Chain
star_api = types.ModuleType("astrbot.api.star")


class Star:
    def __init__(self, context):
        self.context = context


star_api.Star = Star
package = types.ModuleType("phone_agent_api_test")
package.__path__ = [str(ROOT)]
spec = importlib.util.spec_from_file_location("phone_agent_api_test.main", ROOT / "main.py")
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                             "astrbot.api.event": event_api, "astrbot.api.star": star_api,
                             "phone_agent_api_test": package}):
    spec.loader.exec_module(module)


class AppApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"enabled": True, "control_backend": "app", "enable_reminder_tools": True,
                       "enable_audit_tool": True, "enable_observe_tool": True}
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path",
                    "notification_path", "task_runs_path", "health_connect_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        self.plugins = []
        self.create_plugin()

    def create_plugin(self):
        self.app = Quart(__name__ + str(len(self.plugins)))
        context = types.SimpleNamespace(send_message=AsyncMock())
        def register(path, handler, methods, description):
            self.app.add_url_rule(path, str(len(self.app.view_functions)), handler, methods=methods)
        context.register_web_api = register
        self.plugin = module.PhoneAgentPlugin(context, self.config)
        self.plugins.append(self.plugin)
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        for plugin in self.plugins:
            await plugin.terminate()
        self.temp.cleanup()

    async def post(self, suffix, payload, **kwargs):
        return await self.client.post("/astrbot_plugin_phone_agent" + suffix, json=payload, **kwargs)

    async def add(self, text="喝水", minutes=1, request_id=None):
        payload = {"text": text, "minutes": minutes, "request_id": request_id or str(uuid.uuid4())}
        response = await self.post("/app/reminders", payload)
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        return payload, (await response.get_json())["reminder"]

    async def fire(self, reminder_id, offset=-1):
        task = self.plugin._reminder_tasks.pop(reminder_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.plugin._reminders[reminder_id]["due"] = time.time() + offset
        await asyncio.wait_for(self.plugin._run_reminder(reminder_id), 1)

    async def test_state_does_not_probe_offline_phone(self):
        with patch.object(self.plugin.device_app, "health", side_effect=AssertionError("must not probe")):
            response = await asyncio.wait_for(self.client.get("/astrbot_plugin_phone_agent/app/state"), 1)
        data = await response.get_json()
        self.assertEqual(data["api_version"], 3)
        self.assertEqual(data["features"], {
            "reminders": True, "audit": True,
            "policy": False, "observe": True, "screenshot": False, "notifications": False,
        })
        self.assertEqual(data["reminders"], [])

    async def test_app_state_exposes_night_guard_and_device_status(self):
        """G2: /app/state must include night_guard, device_last_status, and sleep_mode."""
        self.plugin.device_app.record_device_status(
            {"foreground_package": "tv.danmaku.bili", "battery_percent": 42,
             "shizuku_ready": True, "screen_interactive": True, "reported_at": time.time()})
        response = await asyncio.wait_for(self.client.get("/astrbot_plugin_phone_agent/app/state"), 1)
        data = await response.get_json()
        self.assertIn("night_guard", data)
        self.assertIn("device_last_status", data)
        self.assertIn("sleep_mode", data)
        self.assertEqual(data["device_last_status"]["battery_percent"], 42)
        self.assertEqual(data["device_last_status"]["foreground_package"], "tv.danmaku.bili")
        self.assertTrue(data["night_guard"]["dry_run"])

    async def test_create_cancel_and_idempotency_survive_restart(self):
        payload, reminder = await self.add(text="喝水\n休息一下")
        retry = await self.post("/app/reminders", payload)
        self.assertEqual((await retry.get_json())["reminder"], reminder)
        conflict = await self.post("/app/reminders", {**payload, "text": "different"})
        self.assertEqual(conflict.status_code, 409)
        cancelled = await self.post("/app/reminders/cancel", {"reminder_id": reminder["id"]})
        self.assertEqual((await cancelled.get_json())["status"], "cancelled")
        await self.plugin.terminate()
        self.create_plugin()
        retry = await self.post("/app/reminders", payload)
        self.assertEqual((await retry.get_json())["reminder"]["status"], "cancelled")
        self.assertEqual(self.plugin._reminders, {})
        self.assertFalse(any(item["notify"] for item in self.plugin.device_app.timeline_since(0, 100)))

    async def test_due_app_reminder_only_publishes_one_durable_phone_event(self):
        payload, reminder = await self.add()
        # The plugin has no direct phone-push path any more: a due reminder is
        # published as a timeline event only.
        await self.fire(reminder["id"])
        self.plugin.context.send_message.assert_not_called()
        self.assertEqual(self.plugin._reminders, {})
        events = self.plugin.device_app.timeline_since(0, 100)
        due = [item for item in events if item["event"] == "reminder_due"]
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0]["notify"])
        self.assertEqual(due[0]["notification_id"], "reminder-" + reminder["id"])
        await self.plugin.terminate()
        self.create_plugin()
        self.assertEqual(self.plugin.device_app.timeline_since(0, 100), events)
        retry = await self.post("/app/reminders", payload)
        self.assertEqual((await retry.get_json())["reminder"]["status"], "fired")

    async def test_crash_after_due_timeline_before_pending_delete_does_not_duplicate(self):
        _, reminder = await self.add()
        reminder_id = reminder["id"]
        await self.plugin.terminate()
        self.plugin._reminders[reminder_id]["due"] = time.time() - 1
        self.plugin._save_reminders()
        original = self.plugin.device_app.timeline_append(
            "reminder", "提醒", reminder["text"], event="reminder_due", notify=True,
            reminder_id=reminder_id, notification_id="reminder-" + reminder_id)
        self.create_plugin()
        await asyncio.gather(*list(self.plugin._reminder_tasks.values()))
        due = [item for item in self.plugin.device_app.timeline_since(0, 100) if item["event"] == "reminder_due"]
        self.assertEqual(due, [original])
        self.assertEqual(self.plugin._reminders, {})

    async def test_restart_recovers_overdue_and_expires_older_than_one_day(self):
        _, recent = await self.add()
        _, stale = await self.add()
        await self.plugin.terminate()
        self.plugin._reminders[recent["id"]]["due"] = time.time() - 10
        self.plugin._reminders[stale["id"]]["due"] = time.time() - 86401
        self.plugin._save_reminders()
        self.create_plugin()
        await asyncio.gather(*list(self.plugin._reminder_tasks.values()))
        events = self.plugin.device_app.timeline_since(0, 100)
        self.assertEqual(sum(item["notify"] for item in events), 1)
        self.assertTrue(any(item["event"] == "reminder_expired" for item in events))
        self.plugin.context.send_message.assert_not_called()
        self.assertEqual(self.plugin._reminders, {})

    async def test_legacy_chat_reminder_still_sends_originating_session(self):
        self.plugin._reminders["legacy"] = {"text": "休息", "session": "test:chat:1", "due": time.time() - 1}
        self.plugin._save_reminders()
        await self.plugin._run_reminder("legacy")
        self.plugin.context.send_message.assert_awaited_once()
        self.assertEqual(self.plugin.context.send_message.call_args.args[0], "test:chat:1")

    async def test_storage_failure_never_claims_success_or_drops_pending(self):
        payload = {"text": "喝水", "minutes": 1, "request_id": str(uuid.uuid4())}
        with patch.object(self.plugin, "_save_reminders", side_effect=OSError("disk full")):
            response = await self.post("/app/reminders", payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.plugin._reminders, {})
        _, reminder = await self.add()
        with patch.object(self.plugin, "_save_reminders", side_effect=OSError("disk full")):
            response = await self.post("/app/reminders/cancel", {"reminder_id": reminder["id"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn(reminder["id"], self.plugin._reminders)

    async def test_invalid_json_nan_boolean_and_fractional_minutes(self):
        url = "/astrbot_plugin_phone_agent/app/reminders"
        for data in ('{', 'null', '[]', '{"minutes":NaN}', '{"minutes":Infinity}'):
            response = await self.client.post(url, data=data, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)
        for minutes in (True, 1.5, "1", 0, 10081):
            response = await self.post("/app/reminders", {"text": "x", "minutes": minutes, "request_id": str(uuid.uuid4())})
            self.assertEqual(response.status_code, 400)
        response = await self.post("/app/reminders/cancel", {})
        self.assertEqual(response.status_code, 400)

    async def test_disabled_features_and_cross_origin_write_denied(self):
        payload = {"text": "x", "minutes": 1, "request_id": str(uuid.uuid4())}
        response = await self.post("/app/reminders", payload, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)
        self.config["enable_reminder_tools"] = False
        self.assertEqual((await self.post("/app/reminders", payload)).status_code, 403)
        self.config["enabled"] = False
        self.assertEqual((await self.client.get("/astrbot_plugin_phone_agent/app/state")).status_code, 503)

    async def test_app_action_never_uses_adb_and_operit_mode_uses_operit(self):
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute", return_value={"success": True}) as app_call, \
                patch.object(self.plugin, "_ensure_device", side_effect=AssertionError("no ADB")), \
                patch.object(self.plugin, "_operit_action", new=AsyncMock(return_value={"success": True})) as operit:
            result = json.loads(await self.plugin.phone_action(object(), action="open_app", package="a.b"))
            self.assertTrue(result["success"])
            app_call.assert_called_once()
            # UI automation is now part of the App command set.
            result = json.loads(await self.plugin.phone_action(object(), action="tap", x=10, y=20))
            self.assertTrue(result["success"])
            self.assertEqual(app_call.call_args.args[0], "tap")
            # Actions the App does not implement are still refused, not silently downgraded.
            result = json.loads(await self.plugin.phone_action(object(), action="trigger_workflow", confirmed=True))
            self.assertEqual(result["error_code"], "unsupported_action")
            self.config["control_backend"] = "operit"
            await self.plugin.phone_action(object(), action="tap")
            operit.assert_awaited_once()

    async def test_notification_forwarding_dedupes_and_persists(self):
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        batch = {"notifications": [
            {"key": "k1", "package": "com.tencent.mm", "title": "张三", "text": "在吗", "posted_at": 1000.0},
            {"key": "k2", "package": "com.android.dialer", "title": "未接来电", "text": "138****0000", "posted_at": 1001.0},
        ]}
        response = await self.post("/app/notifications", batch)
        self.assertEqual(response.status_code, 200)
        body = await response.get_json()
        self.assertEqual(body["added"], 2)
        # Replaying the same keys must not duplicate entries.
        body = await (await self.post("/app/notifications", batch)).get_json()
        self.assertEqual(body["added"], 0)
        self.assertEqual(body["total"], 2)
        listed = await (await self.client.get("/astrbot_plugin_phone_agent/app/notifications")).get_json()
        self.assertEqual(len(listed["notifications"]), 2)
        # A fresh instance loads the store from disk.
        self.create_plugin()
        reloaded = await (await self.client.get("/astrbot_plugin_phone_agent/app/notifications")).get_json()
        self.assertEqual(len(reloaded["notifications"]), 2)

    async def test_notification_route_respects_feature_flag_and_payload_shape(self):
        # Feature off: writes are refused, and the route is not even registered.
        self.config["enable_notification_tool"] = False
        self.create_plugin()
        response = await self.post("/app/notifications", {"notifications": []})
        self.assertEqual(response.status_code, 404)
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        self.assertEqual((await self.post("/app/notifications", {"notifications": "nope"})).status_code, 400)
        self.assertEqual((await self.post("/app/notifications", {"notifications": [{"key": "x", "title": float("nan")}]})).status_code, 400)

    async def test_phone_notifications_tool_filters_by_package(self):
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        await self.post("/app/notifications", {"notifications": [
            {"key": "k1", "package": "com.tencent.mm", "title": "张三", "text": "在吗", "posted_at": 1000.0},
            {"key": "k2", "package": "com.android.dialer", "title": "未接来电", "text": "电话", "posted_at": 1001.0},
        ]})
        with patch.object(self.plugin, "_authorized", return_value=True):
            result = json.loads(await self.plugin.phone_notifications(object()))
            self.assertTrue(result["success"])
            self.assertEqual(result["count"], 2)
            filtered = json.loads(await self.plugin.phone_notifications(object(), package="com.tencent.mm"))
            self.assertEqual(filtered["count"], 1)
            self.assertEqual(filtered["notifications"][0]["title"], "张三")

    async def test_failed_notification_save_is_retryable_and_survives_restart(self):
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        batch = {"notifications": [{"key": "retry-key", "title": "test", "posted_at": 1000}]}
        with patch.object(self.plugin, "_save_notifications", side_effect=OSError("disk full")):
            response = await self.post("/app/notifications", batch)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.plugin._notifications, [])
        response = await self.post("/app/notifications", batch)
        self.assertEqual((await response.get_json())["added"], 1)
        self.create_plugin()
        self.assertEqual(self.plugin._notifications[0]["key"], "retry-key")

    async def test_oversized_notification_timestamp_does_not_partially_mutate_batch(self):
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        batch = {"notifications": [
            {"key": "valid", "title": "test", "posted_at": 1000},
            {"key": "huge-time", "title": "test", "posted_at": 10 ** 400},
        ]}
        response = await self.post("/app/notifications", batch)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["added"], 2)
        self.create_plugin()
        self.assertEqual(len(self.plugin._notifications), 2)
        self.assertLess(self.plugin._notifications[0]["posted_at"], time.time() + 1)

    async def test_device_command_enforces_switch_backend_and_feature_flags(self):
        with patch.object(self.plugin.device_app, "execute") as execute:
            self.config["enabled"] = False
            self.assertEqual((await self.post("/device/command", {"action": "home"})).status_code, 503)
            self.config["enabled"] = True
            self.config["control_backend"] = "operit"
            self.assertEqual((await self.post("/device/command", {"action": "home"})).status_code, 409)
            self.config["control_backend"] = "app"
            for action in ("screenshot", "location", "usage_stats", "suspend_app", "suspend_video_apps"):
                self.assertEqual((await self.post("/device/command", {"action": action})).status_code, 403)
            execute.assert_not_called()

    async def test_device_command_rejects_non_object_args_and_non_finite_numbers(self):
        with patch.object(self.plugin.device_app, "execute") as execute:
            for args in ([], "value", None, {"x": float("inf")}, {"x": float("nan")}):
                response = await self.post("/device/command", {"action": "tap", "args": args})
                self.assertEqual(response.status_code, 400)
            execute.assert_not_called()

    async def test_device_command_cannot_suspend_protected_packages(self):
        self.config["enable_policy_tools"] = True
        with patch.object(self.plugin.device_app, "execute") as execute:
            response = await self.post("/device/command", {"action": "suspend_app", "args": {"package": "com.android.systemui"}})
            self.assertEqual(response.status_code, 403)
            execute.assert_not_called()

    async def test_config_rejects_non_finite_integer_without_mutating_config(self):
        before = dict(self.config)
        response = await self.post("/config", {"operit_timeout_seconds": float("inf")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.config, before)

    async def test_screen_text_cannot_bypass_feature_or_group_privacy(self):
        event = types.SimpleNamespace(unified_msg_origin="qq:GroupMessage:group")
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute") as execute:
            result = json.loads(await self.plugin.phone_action(event, action="screen_text"))
            self.assertFalse(result["success"])
            self.config["enable_observe_tool"] = False
            event.unified_msg_origin = "qq:FriendMessage:user"
            result = json.loads(await self.plugin.phone_action(event, action="screen_text"))
            self.assertFalse(result["success"])
            execute.assert_not_called()

    async def test_private_approval_works_without_night_guard(self):
        self.config.update({"enable_night_guard": False, "enable_task_execute": True, "allowed_user_ids": "user"})
        event = types.SimpleNamespace(
            unified_msg_origin="qq_official:PrivateMessage:user", get_sender_id=lambda: "user",
            get_messages=lambda: [types.SimpleNamespace(text="可以")], send=AsyncMock(),
        )
        with patch.object(self.plugin, "_chat_task_decision", new=AsyncMock(return_value="已执行")) as decide:
            await self.plugin.on_private_message(event)
            decide.assert_awaited_once_with("user", "可以")
            event.send.assert_awaited_once()
            self.assertEqual(self.plugin._night_guard_states, {})
            decide.reset_mock()
            event.unified_msg_origin = "qq:GroupMessage:group"
            await self.plugin.on_private_message(event)
            decide.assert_not_awaited()

    async def test_cancelled_adb_call_kills_and_reaps_process(self):
        entered = asyncio.Event()
        calls = 0

        async def communicate():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return b"", b""

        process = types.SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=communicate))
        from unittest.mock import Mock
        process.kill = Mock()
        with patch.object(module.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)):
            running = asyncio.create_task(self.plugin._run("devices"))
            await asyncio.wait_for(entered.wait(), 1)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        process.kill.assert_called_once()
        self.assertEqual(calls, 2)

    async def test_registered_optional_routes_recheck_feature_switch(self):
        self.config["enable_notification_tool"] = True
        self.create_plugin()
        self.config["enable_notification_tool"] = False
        response = await self.client.get("/astrbot_plugin_phone_agent/app/notifications")
        self.assertEqual(response.status_code, 403)

    async def test_operit_retry_respects_mode_status_and_concurrency(self):
        task = {"status": "failed", "task": "打开应用", "owner": "user"}
        self.plugin._operit_tasks["original"] = task
        response = await self.post("/operit_task_retry", {"task_id": "original"})
        self.assertEqual(response.status_code, 409)
        self.config["control_backend"] = "operit"
        for status in ("queued", "running", "completed"):
            task["status"] = status
            response = await self.post("/operit_task_retry", {"task_id": "original"})
            self.assertEqual(response.status_code, 409)
        task["status"] = "failed"
        self.config["max_background_tasks"] = 1
        self.plugin._operit_tasks["busy"] = {"status": "running"}
        response = await self.post("/operit_task_retry", {"task_id": "original"})
        self.assertEqual(response.status_code, 409)
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin, "_event_owner", return_value="user"):
            result = json.loads(await self.plugin.operit_task_retry(object(), task_id="original"))
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "retry_unavailable")
        self.assertEqual(len(self.plugin._operit_tasks), 2)

    async def test_operit_fallback_never_replays_uncertain_or_rejected_commands(self):
        self.config.update({"operit_token": "test", "operit_base_url": "http://phone.example", "relay_base_url": "https://relay.example", "relay_token": "test"})
        for direct in ({"success": False, "error": "timeout", "safe_to_retry": False},
                       {"success": False, "error": "explicit rejection"},
                       {"success": False, "error": "invalid response"}):
            with patch.object(self.plugin, "_operit_task_direct", return_value=direct), \
                    patch.object(self.plugin, "_relay_submit") as relay:
                self.assertEqual(self.plugin._operit_task_sync("home", False, "WINDOW"), direct)
                relay.assert_not_called()
        with patch.object(self.plugin, "_operit_task_direct", return_value={"success": False, "safe_to_retry": True}), \
                patch.object(self.plugin, "_relay_submit", return_value={"success": True}) as relay:
            self.assertTrue(self.plugin._operit_task_sync("home", False, "WINDOW")["success"])
            relay.assert_called_once()

    async def test_operit_transport_distinguishes_refused_from_unknown_execution(self):
        self.config.update({"operit_token": "test", "operit_base_url": "http://phone.example"})
        for error, safe in ((ConnectionRefusedError("refused"), True), (TimeoutError("timeout"), False),
                            (ConnectionResetError("reset"), False)):
            with patch.object(module, "open_http", side_effect=module.urllib.error.URLError(error)):
                result = self.plugin._operit_task_direct("home", False, "WINDOW")
            self.assertEqual(result["safe_to_retry"], safe)

    async def test_operit_redirect_does_not_replay_via_relay(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        requests = []

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://redirect-target.invalid/after-execution")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        self.config.update({"operit_token": "test", "operit_base_url": f"http://127.0.0.1:{server.server_port}",
                            "relay_base_url": "https://relay.example", "relay_token": "test"})
        try:
            with patch.object(self.plugin, "_relay_submit") as relay:
                result = self.plugin._operit_task_sync("home", False, "WINDOW")
            relay.assert_not_called()
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "Operit HTTP 302")
            self.assertEqual(requests, ["/api/external-chat"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

    async def test_phone_screenshot_decodes_image_and_survives_missing_delivery(self):
        import base64 as _b64
        self.config["enable_screenshot_tool"] = True
        self.create_plugin()
        raw = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
        payload = {"success": True, "output": {"format": "jpeg", "width": 8, "height": 6,
                                              "bytes": len(raw), "data_base64": _b64.b64encode(raw).decode()}}
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute", return_value=payload) as call:
            result = json.loads(await self.plugin.phone_screenshot(object(), caption="看看屏幕"))
            self.assertTrue(result["success"])
            self.assertEqual(result["bytes"], len(raw))
            self.assertEqual(result["width"], 8)
            self.assertEqual(call.call_args.args[0], "screenshot")
            # The test harness has no Image component, so delivery reports false
            # rather than claiming the picture arrived.
            self.assertFalse(result["delivered"])
            self.assertIn("note", result)

    async def test_phone_screenshot_rejects_bad_or_oversized_data(self):
        import base64 as _b64
        self.config["enable_screenshot_tool"] = True
        self.create_plugin()
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute",
                             return_value={"success": True, "output": {"data_base64": "!!!not-base64!!!"}}):
            result = json.loads(await self.plugin.phone_screenshot(object()))
            self.assertFalse(result["success"])
        oversized = _b64.b64encode(b"x" * (2200001)).decode()
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute",
                             return_value={"success": True, "output": {"data_base64": oversized}}):
            result = json.loads(await self.plugin.phone_screenshot(object()))
            self.assertFalse(result["success"])
            self.assertIn("too large", result["error"])
        with patch.object(self.plugin, "_authorized", return_value=True), \
                patch.object(self.plugin.device_app, "execute",
                             return_value={"success": False, "error": "no shizuku"}):
            result = json.loads(await self.plugin.phone_screenshot(object()))
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "no shizuku")

    async def test_backend_alias_save_and_selected_connection_test(self):
        response = await self.post("/config", {"control_backend": "phone_buddy", "app_direct_urls": "http://phone.example:8260"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["control_backend"], "app")
        with patch.object(self.plugin.device_app, "health", return_value={"available": True}), \
                patch.object(self.plugin, "_operit_health_sync", side_effect=AssertionError("wrong backend")):
            response = await self.post("/test_backend", {})
            self.assertTrue((await response.get_json())["success"])

    # -- P1-E stage 3 routes

    async def test_grant_requires_a_registered_device(self):
        self.plugin.config["enable_task_execute"] = True
        response = await self.post("/app/grant", {"actions": ["home"], "ttl_seconds": 60})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "no_device")

    async def test_grant_rejects_non_write_actions(self):
        self.plugin.config["enable_task_execute"] = True
        self.plugin.device_app.registration = lambda: {"device_id": "dev-1"}
        response = await self.post("/app/grant", {"actions": ["observe"], "ttl_seconds": 60})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await response.get_json())["error_code"], "invalid_actions")

    async def test_grant_issues_a_scoped_grant(self):
        self.plugin.config["enable_task_execute"] = True
        self.plugin.device_app.registration = lambda: {"device_id": "dev-1"}
        response = await self.post("/app/grant",
                                   {"actions": ["home"], "ttl_seconds": 120, "max_steps": 2})
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        grant = (await response.get_json())["grant"]
        self.assertTrue(grant["grant_id"])
        self.assertEqual(grant["max_steps"], 2)

    async def test_task_run_is_disabled_by_default(self):
        self.plugin.device_app.registration = lambda: {"device_id": "dev-1"}
        response = await self.post("/app/task/run", {"goal": "回到桌面", "grant_id": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await response.get_json())["error_code"], "disabled")

    async def test_task_run_requires_a_grant(self):
        self.plugin.device_app.registration = lambda: {"device_id": "dev-1"}
        self.plugin.config["enable_task_execute"] = True
        response = await self.post("/app/task/run", {"goal": "回到桌面"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "grant_required")
    # -- P1-E stage 4 routes

    def _register(self):
        self.plugin.device_app.registration = lambda: {"device_id": "dev-1"}
        self.plugin.config["enable_task_execute"] = True

    async def test_propose_waits_for_a_human(self):
        self._register()
        response = await self.post("/app/task/propose", {"goal": "回到桌面"})
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        body = await response.get_json()
        self.assertEqual(body["run"]["status"], "waiting_approval")
        self.assertIn("home", body["approval"]["actions"])
        self.assertGreater(body["approval"]["expires_at"], time.time())

    async def test_propose_without_device_is_rejected(self):
        self.plugin.config["enable_task_execute"] = True
        response = await self.post("/app/task/propose", {"goal": "回到桌面"})
        self.assertEqual(response.status_code, 409)

    async def test_reject_cancels_the_run(self):
        self._register()
        proposed = await (await self.post("/app/task/propose", {"goal": "回到桌面"})).get_json()
        approval_id = proposed["approval"]["approval_id"]
        response = await self.post("/app/task/reject", {"approval_id": approval_id, "reason": "不要"})
        self.assertEqual(response.status_code, 200)
        body = await response.get_json()
        self.assertEqual(body["approval"]["status"], "rejected")
        run = self.plugin._task_runs.run(proposed["run"]["run_id"])
        self.assertEqual(run.status, "cancelled")

    async def test_approve_issues_a_grant_and_runs_once(self):
        self._register()
        proposed = await (await self.post("/app/task/propose", {"goal": "回到桌面"})).get_json()
        approval_id = proposed["approval"]["approval_id"]
        with patch.object(self.plugin._task_executor, "execute", new=AsyncMock(side_effect=lambda run, **kw: run)) as execute:
            response = await self.post("/app/task/approve", {"approval_id": approval_id})
            self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
            self.assertEqual(execute.await_count, 1)
            grant = execute.await_args.kwargs["grant"]
            self.assertIn("home", grant.actions)
            self.assertEqual(grant.source, "approval")
            again = await self.post("/app/task/approve", {"approval_id": approval_id})
            self.assertEqual(again.status_code, 409)
            self.assertEqual(execute.await_count, 1)
    # -- chat-side approval

    async def test_chat_approval_runs_the_pending_task(self):
        self._register()
        await self.post("/app/task/propose", {"goal": "回到桌面"})
        approval = next(iter(self.plugin._task_runs._approvals.values()))
        with patch.object(self.plugin._task_executor, "execute",
                          new=AsyncMock(side_effect=lambda run, **kw: run)) as execute:
            reply = await self.plugin._chat_task_decision(approval.subject, "批准")
        self.assertIsNotNone(reply)
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(approval.status, "approved")

    async def test_chat_noise_is_not_an_approval(self):
        self._register()
        await self.post("/app/task/propose", {"goal": "回到桌面"})
        approval = next(iter(self.plugin._task_runs._approvals.values()))
        reply = await self.plugin._chat_task_decision(approval.subject, "今天天气不错")
        self.assertIsNone(reply)
        self.assertEqual(approval.status, "pending")

    async def test_llm_plan_stays_pending_until_approval(self):
        self._register()
        self.config.update(enable_llm_task_planner=True, task_execute_allowed_actions="open_app,home")
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text=json.dumps({"status": "ready", "steps": [{"action": "open_app", "args": {"package": "tv.danmaku.bili"}}, {"action": "home"}]}))))
        self.plugin.context.get_using_provider = lambda **kw: provider
        with patch.object(self.plugin._task_executor, "execute", new=AsyncMock()) as execute:
            response = await self.post("/app/task/propose", {"goal": "打开哔哩哔哩然后回桌面"})
            data = await response.get_json()
            self.assertEqual(response.status_code, 200, data)
            self.assertEqual(data["run"]["status"], "waiting_approval")
            self.assertEqual(set(data["approval"]["actions"]), {"open_app", "home"})
            self.assertEqual([s["action"] for s in data["run"]["steps"]], ["observe", "open_app", "home"])
            self.assertEqual(data["run"]["summary"]["planning"]["source"], "llm")
            execute.assert_not_awaited()

    async def test_llm_clarification_does_not_create_approval(self):
        self._register()
        self.config["enable_llm_task_planner"] = True
        self.plugin.context.get_using_provider = lambda **kw: types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"status":"needs_clarification","question":"哪一个应用？"}')))
        response = await self.post("/app/task/propose", {"goal": "打开那个"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.plugin._task_runs.approvals(), [])

    async def test_llm_device_change_during_planning_is_rejected(self):
        self._register()
        self.config["enable_llm_task_planner"] = True
        async def changed(**kw):
            self.plugin.device_app.registration = lambda: {"device_id": "replacement"}
            return types.SimpleNamespace(completion_text='{"status":"ready","steps":[{"action":"home"}]}')
        self.plugin.context.get_using_provider = lambda **kw: types.SimpleNamespace(text_chat=changed)
        response = await self.post("/app/task/propose", {"goal": "回桌面"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.plugin._task_runs.approvals(), [])

    async def test_model_unavailable_unknown_app_creates_no_active_run(self):
        self._register()
        self.config["enable_llm_task_planner"] = True
        self.plugin.context.get_using_provider = lambda **kw: None
        response = await self.post("/app/task/propose", {"goal": "打开一个未知应用"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.plugin._task_runs.runs(), [])

    async def test_multistep_approval_counts_repeated_actions(self):
        self._register()
        self.config.update(enable_llm_task_planner=True, task_execute_allowed_actions="open_app,home")
        rows=[{"action":"open_app","args":{"package":"tv.danmaku.bili"}}, {"action":"home"}, {"action":"open_app","args":{"package":"tv.danmaku.bili"}}]
        self.plugin.context.get_using_provider = lambda **kw: types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text=json.dumps({"status":"ready","steps":rows}))))
        for via_chat in (False,True):
            proposed=await (await self.post("/app/task/propose",{"goal":"打开哔哩哔哩回桌面再打开"})).get_json()
            with patch.object(self.plugin._task_executor, "execute", new=AsyncMock(side_effect=lambda run,**kw:run)) as execute:
                if via_chat:
                    await self.plugin._chat_task_decision(proposed["approval"]["subject"], "批准")
                else:
                    await self.post("/app/task/approve", {"approval_id":proposed["approval"]["approval_id"]})
                self.assertEqual(execute.await_args.kwargs["grant"].max_steps,3)

    async def test_care_preview_always_forces_preview_for_allowed_private_target(self):
        self.config["allowed_user_ids"] = "owner"
        provider = types.SimpleNamespace(evaluate_care=AsyncMock(return_value={
            "evaluated": True, "preview": True, "sent": False, "outcome": "preview"}))
        with patch.object(self.plugin, "_companion_care", return_value=provider), \
             patch.object(self.plugin, "_care_device_fresh", return_value=True), \
             patch.object(self.plugin, "_care_targets", return_value=[
                 {"user_id": "owner", "umo": "bot:FriendMessage:owner"}]):
            response = await self.post("/app/care/preview", {"preview": False, "subject": "other"})
        self.assertEqual(response.status_code, 200)
        provider.evaluate_care.assert_awaited_once_with("owner", "bot:FriendMessage:owner", "owner", preview=True)

    async def test_care_preview_rejects_stale_or_unscoped_target_before_model(self):
        provider = types.SimpleNamespace(evaluate_care=AsyncMock())
        with patch.object(self.plugin, "_companion_care", return_value=provider), \
             patch.object(self.plugin, "_care_device_fresh", return_value=False):
            response = await self.post("/app/care/preview", {})
            self.assertEqual(response.status_code, 409)
        with patch.object(self.plugin, "_companion_care", return_value=provider), \
             patch.object(self.plugin, "_care_device_fresh", return_value=True), \
             patch.object(self.plugin, "_care_targets", return_value=[
                 {"user_id": "other", "umo": "bot:FriendMessage:other"}]):
            response = await self.post("/app/care/preview", {})
            self.assertEqual(response.status_code, 409)
        provider.evaluate_care.assert_not_awaited()
