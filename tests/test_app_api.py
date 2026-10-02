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
event_api.filter = types.SimpleNamespace(llm_tool=lambda **_: lambda function: function)


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
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path"):
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
        self.assertEqual(data["api_version"], 2)
        self.assertEqual(data["features"], {"reminders": True, "audit": True})
        self.assertEqual(data["reminders"], [])

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
        with patch.object(self.plugin, "_push_phone_notification", side_effect=AssertionError("no direct push")):
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
            result = json.loads(await self.plugin.phone_action(object(), action="tap"))
            self.assertEqual(result["error_code"], "unsupported_action")
            self.config["control_backend"] = "operit"
            await self.plugin.phone_action(object(), action="tap")
            operit.assert_awaited_once()

    async def test_backend_alias_save_and_selected_connection_test(self):
        response = await self.post("/config", {"control_backend": "phone_buddy", "app_direct_urls": "http://phone.example:8260"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["control_backend"], "app")
        with patch.object(self.plugin.device_app, "health", return_value={"available": True}), \
                patch.object(self.plugin, "_operit_health_sync", side_effect=AssertionError("wrong backend")):
            response = await self.post("/test_backend", {})
            self.assertTrue((await response.get_json())["success"])
