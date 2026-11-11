"""UI task approval and multi-device routing through real Quart endpoints.

Every phone response and model completion is synthetic; these tests do not call
a real device, a model provider, or another user's account.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_app_api as fixture

module = fixture.module
PACKAGE = "tv.danmaku.bili"


class UiApiTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup helpers, not inheritance, so existing route tests run once.
    create_plugin = fixture.AppApiTests.create_plugin
    post = fixture.AppApiTests.post
    asyncTearDown = fixture.AppApiTests.asyncTearDown

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"enabled": True, "control_backend": "app", "enable_task_execute": True,
                       "enable_ui_tasks": True, "enable_observe_tool": True,
                       "enable_autonomy_scheduler": False, "enable_night_guard": False,
                       "enable_reminder_tools": True, "enable_notification_tool": True,
                       "allowed_user_ids": "synthetic-owner",
                       "task_execute_allowed_actions": "open_app,home",
                       "ui_task_allowed_packages": PACKAGE}
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path",
                    "notification_path", "task_runs_path", "night_guard_state_path", "care_status_path", "health_connect_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        self.plugins = []
        with patch.object(module.PhoneAgentPlugin, "_warm_device_route"):
            self.create_plugin()
        self.provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"action":"done","evidence_node_id":"n1"}')))
        self.plugin.context.get_using_provider = Mock(return_value=self.provider)
        self.plugin.context.get_provider_by_id = Mock(return_value=self.provider)
        self.calls = []
        self.snapshot_counter = 0

    async def register(self, device_id="personal-1", kind="personal", version="0.4.0"):
        response = await self.post("/device/register", {"device_id": device_id, "device_kind": kind,
            "base_url": "http://" + device_id + ":8260", "app_version": version})
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        return self.plugin._devices.backend(device_id)

    def snapshot(self):
        self.snapshot_counter += 1
        return {"schema_version": 1, "snapshot_id": "snapshot-" + str(self.snapshot_counter),
                "captured_at": time.time(), "package": PACKAGE, "width": 1080, "height": 2400,
                "truncated": False, "nodes": [{"id": "n1", "text": "synthetic result private screen marker",
                    "description": "", "resource_id": PACKAGE + ":id/result", "class_name": "android.widget.TextView",
                    "package": PACKAGE, "bounds": [0, 0, 250, 100], "clickable": True,
                    "editable": False, "focused": False, "enabled": True, "scrollable": False, "password": False}]}

    def simulate_phone(self, backend):
        def execute(action, args=None, timeout=None, **kwargs):
            self.assertEqual(kwargs.get("expected_binding"), backend.binding())
            self.calls.append((backend.binding()[0], action, args or {}))
            if action == "ui_snapshot":
                return {"success": True, "output": self.snapshot()}
            return {"success": True}
        patcher = patch.object(backend, "execute", side_effect=execute)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def block_model(self, decision=None):
        entered, release = asyncio.Event(), asyncio.Event()
        async def completion(**kwargs):
            entered.set()
            await release.wait()
            return types.SimpleNamespace(completion_text=json.dumps(decision or {"action": "done", "evidence_node_id": "n1"}))
        self.provider.text_chat.side_effect = completion
        return entered, release

    def scope(self, **updates):
        return {"goal": "打开哔哩哔哩并显示合成验收结果", "package": PACKAGE, "device_id": "personal-1",
                "requester_device_id": "personal-1", "allowed_texts": [],
                "success_texts": ["synthetic result"], "completion_mode": "visible_text", "max_steps": 5, **updates}

    async def propose(self, **updates):
        response = await self.post("/app/ui-task/propose", self.scope(**updates))
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        return await response.get_json()

    async def approve(self, proposal):
        return await self.post("/app/task/approve", {"approval_id": proposal["approval"]["approval_id"]})

    async def finished(self, proposal):
        run_id = proposal["run"]["run_id"]
        handle = self.plugin._ui_handles.get(run_id)
        if handle is not None:
            await asyncio.wait_for(asyncio.gather(handle, return_exceptions=True), 2)
        return self.plugin._task_runs.run(run_id)

    async def test_bot_first_then_personal_status_and_personal_features_are_scoped(self):
        bot = await self.register("bot-1", "bot")
        self.assertEqual(self.plugin.device_app.binding()[0], "")
        personal = await self.register()
        personal.record_device_status({"battery_percent": 11, "reported_at": time.time()})
        bot.record_device_status({"battery_percent": 82, "reported_at": time.time()})
        personal.timeline_append("reminder", "Personal synthetic event", "personal body")
        bot.timeline_append("task", "Bot synthetic event", "bot body")
        self.plugin._reminders["synthetic-reminder"] = {"text": "synthetic reminder", "due": time.time() + 60,
                                                        "status": "pending", "source": "app"}
        bot_state = await (await self.client.get("/astrbot_plugin_phone_agent/app/state?device_id=bot-1")).get_json()
        personal_state = await (await self.client.get("/astrbot_plugin_phone_agent/app/state?device_id=personal-1")).get_json()
        self.assertEqual(bot_state["device_last_status"]["battery_percent"], 82)
        self.assertEqual(personal_state["device_last_status"]["battery_percent"], 11)
        self.assertEqual([item["title"] for item in bot_state["timeline"]], ["Bot synthetic event"])
        self.assertEqual(bot_state["reminders"], [])
        self.assertEqual(bot_state["care"], {})
        self.assertEqual(bot_state["night_guard"], {})
        self.assertEqual(len(personal_state["reminders"]), 1)
        self.assertEqual({row["device_kind"] for row in bot_state["devices"]}, {"personal", "bot"})

    async def test_requester_sees_bot_task_but_unrelated_device_does_not(self):
        await self.register()
        await self.register("bot-1", "bot")
        await self.register("other-1", "personal")
        proposal = await self.propose(device_id="bot-1")
        run_id = proposal["run"]["run_id"]
        for device, visible in [("personal-1", True), ("bot-1", True), ("other-1", False)]:
            with self.subTest(device=device):
                state = await (await self.client.get("/astrbot_plugin_phone_agent/app/state?device_id=" + device)).get_json()
                self.assertEqual(run_id in {row["run_id"] for row in state["task_runs"]}, visible)
                self.assertEqual(run_id in {row["run_id"] for row in state["task_approvals"]}, visible)

    async def test_proposal_discloses_complete_scope_and_executes_nothing(self):
        backend = await self.register()
        execute = self.simulate_phone(backend)
        proposal = await self.propose(allowed_texts=["synthetic query"])
        self.assertEqual(proposal["run"]["status"], "waiting_approval")
        self.assertEqual(proposal["approval"]["scope"], {"device_id": "personal-1", "device_role": "personal",
            "package": PACKAGE, "allowed_texts": ["synthetic query"], "success_texts": ["synthetic result"],
            "completion_mode": "visible_text", "max_steps": 5})
        self.assertEqual(proposal["approval"]["mode"], "ui")
        self.assertEqual(proposal["approval"]["goal"], self.scope()["goal"])
        execute.assert_not_called()
        self.provider.text_chat.assert_not_awaited()
        self.assertEqual(self.plugin._task_runs.grants(), [])

    async def test_disabled_switches_block_proposals_without_model_or_device(self):
        backend = await self.register()
        execute = self.simulate_phone(backend)
        for key in ["enabled", "enable_ui_tasks", "enable_task_execute", "enable_observe_tool"]:
            with self.subTest(key=key):
                self.config[key] = False
                response = await self.post("/app/ui-task/propose", self.scope())
                self.assertEqual(response.status_code, 400)
                self.assertEqual((await response.get_json())["error_code"], "ui_tasks_disabled")
                self.config[key] = True
        execute.assert_not_called()
        self.provider.text_chat.assert_not_awaited()

    async def test_old_app_unknown_device_and_unassigned_role_cannot_propose(self):
        await self.register(version="0.3.28")
        response = await self.post("/app/ui-task/propose", self.scope())
        self.assertEqual((await response.get_json())["error_code"], "app_upgrade_required")
        response = await self.post("/app/ui-task/propose", self.scope(device_id="unknown"))
        self.assertEqual((await response.get_json())["error_code"], "unknown_device")
        await self.register("second-personal", "personal")
        response = await self.post("/app/ui-task/propose", self.scope(device_id="second-personal"))
        self.assertEqual((await response.get_json())["error_code"], "device_role_required")

    async def test_malformed_scope_is_rejected_without_creating_any_run(self):
        await self.register()
        invalid = [{"device_id": []}, {"device_id": ""}, {"device_id": None}, {"device_id": False},
                   {"package": "com.unapproved.app"}, {"allowed_texts": "query"}, {"allowed_texts": ["x\ny"]},
                   {"success_texts": []}, {"success_texts": [123]}, {"success_texts": ["x" * 201]},
                   {"max_steps": True}, {"max_steps": 0}, {"max_steps": 13},
                   {"completion_mode": "anything"}, {"completion_mode": []}, {"requester_device_id": "unknown"}]
        for values in invalid:
            with self.subTest(values=values):
                response = await self.post("/app/ui-task/propose", self.scope(**values))
                self.assertEqual(response.status_code, 400, await response.get_data(as_text=True))
        self.assertEqual(self.plugin._task_runs.runs(), [])

    async def test_approval_returns_202_then_completes_only_after_fresh_evidence(self):
        self.simulate_phone(await self.register())
        entered, release = self.block_model()
        proposal = await self.propose()
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 202, await response.get_data(as_text=True))
        self.assertEqual((await response.get_json())["status"], "running")
        await asyncio.wait_for(entered.wait(), 2)
        run = self.plugin._task_runs.run(proposal["run"]["run_id"])
        self.assertNotEqual(run.status, "done")
        self.assertIn("personal-1", self.plugin._ui_busy_devices)
        release.set()
        run = await self.finished(proposal)
        self.assertEqual(run.status, "done", run.last_error)
        self.assertTrue(run.summary["completion_verified"])
        self.assertEqual([action for _, action, _ in self.calls], ["open_app", "ui_snapshot", "ui_snapshot"])
        self.assertNotIn("personal-1", self.plugin._ui_busy_devices)
        self.assertNotIn("private screen marker", Path(self.config["task_runs_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("private screen marker", Path(self.config["audit_log_path"]).read_text(encoding="utf-8"))

    async def test_approval_acts_only_on_selected_bot_and_cannot_be_replayed(self):
        personal_execute = self.simulate_phone(await self.register())
        self.simulate_phone(await self.register("bot-1", "bot"))
        proposal = await self.propose(device_id="bot-1")
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        run = await self.finished(proposal)
        self.assertEqual(run.status, "done")
        personal_execute.assert_not_called()
        self.assertEqual({device for device, _, _ in self.calls}, {"bot-1"})
        calls_before = len(self.calls)
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "approval_not_pending")
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual(len(self.plugin._task_runs.grants()), 1)

    async def test_pending_ui_task_can_be_cancelled_after_global_feature_disable(self):
        self.simulate_phone(await self.register())
        proposal = await self.propose()
        self.config.update(enabled=False, enable_ui_tasks=False, enable_task_execute=False)
        response = await self.post("/app/task/cancel", {"run_id": proposal["run"]["run_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["run"]["status"], "cancelled")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.plugin._task_runs.approval(proposal["approval"]["approval_id"]).pending())

    async def test_running_ui_task_can_be_cancelled_while_model_is_waiting(self):
        self.simulate_phone(await self.register())
        entered, _ = self.block_model({"action": "tap", "node_id": "n1"})
        proposal = await self.propose()
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        await asyncio.wait_for(entered.wait(), 2)
        self.config.update(enable_ui_tasks=False, enable_task_execute=False)
        response = await self.post("/app/task/cancel", {"run_id": proposal["run"]["run_id"]})
        self.assertEqual(response.status_code, 200)
        run = await self.finished(proposal)
        self.assertEqual(run.status, "cancelled")
        self.assertEqual([a for _, a, _ in self.calls if a != "ui_snapshot"], ["open_app"])
        self.assertFalse(self.plugin._ui_busy_devices)

    async def test_cancel_keeps_device_busy_until_inflight_worker_has_returned(self):
        backend = await self.register()
        loop = asyncio.get_running_loop()
        entered, returned = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        self.provider.text_chat.return_value = types.SimpleNamespace(completion_text='{"action":"tap","node_id":"n1"}')
        def execute(action, args=None, timeout=None, **kwargs):
            if action == "ui_snapshot":
                return {"success": True, "output": self.snapshot()}
            if action == "ui_action":
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=3)
                loop.call_soon_threadsafe(returned.set)
            return {"success": True}
        with patch.object(backend, "execute", side_effect=execute):
            proposal = await self.propose()
            self.assertEqual((await self.approve(proposal)).status_code, 202)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                response = await asyncio.wait_for(self.post("/app/task/cancel", {"run_id": proposal["run"]["run_id"]}), 1)
                self.assertEqual(response.status_code, 200)
                await self.finished(proposal)
                self.assertIn("personal-1", self.plugin._ui_busy_devices)
                second = await self.propose()
                response = await self.approve(second)
                self.assertEqual(response.status_code, 409)
                self.assertEqual((await response.get_json())["error_code"], "device_busy")
            finally:
                release.set()
                await asyncio.wait_for(returned.wait(), 2)
                # The thread sets returned immediately before its task resolves;
                # await one loop tick for the reservation's completion callback.
                for _ in range(10):
                    if "personal-1" not in self.plugin._ui_busy_devices:
                        break
                    await asyncio.sleep(0.01)
            self.assertNotIn("personal-1", self.plugin._ui_busy_devices)

    async def test_grant_revoked_during_route_probe_prevents_command_dispatch(self):
        backend = await self.register()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        requests = []
        def http(url, token, method="GET", payload=None, timeout=15):
            requests.append((url, method))
            if url.endswith("/health"):
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=3)
                return {"success": True}
            return {"type": "result", "schema_version": 1, "command_id": payload["command_id"],
                    "action": payload["action"], "success": True, "output": self.snapshot()}
        with patch.object(backend, "_http_json", side_effect=http):
            proposal = await self.propose()
            self.assertEqual((await self.approve(proposal)).status_code, 202)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                run = self.plugin._task_runs.run(proposal["run"]["run_id"])
                self.assertTrue(run.grant_id)
                self.plugin._task_runs.revoke_grant(run.grant_id)
                self.plugin._task_runs.save()
            finally:
                release.set()
            run = await self.finished(proposal)
        self.assertNotEqual(run.status, "done")
        self.assertFalse(any(method == "POST" for _, method in requests))

    async def test_plugin_termination_waits_for_inflight_transport(self):
        backend = await self.register()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        def execute(action, args=None, timeout=None, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=3)
            return {"success": True}
        with patch.object(backend, "execute", side_effect=execute):
            proposal = await self.propose()
            self.assertEqual((await self.approve(proposal)).status_code, 202)
            await asyncio.wait_for(entered.wait(), 2)
            termination = asyncio.create_task(self.plugin.terminate())
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(termination.done())
                self.assertIn("personal-1", self.plugin._ui_busy_devices)
            finally:
                release.set()
                await asyncio.wait_for(termination, 2)
        self.assertEqual(self.plugin._ui_transports, set())
        self.assertEqual(self.plugin._ui_busy_devices, set())

    async def test_policy_revoked_during_model_await_stops_next_write(self):
        self.simulate_phone(await self.register())
        entered, release = self.block_model({"action": "tap", "node_id": "n1"})
        proposal = await self.propose()
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        await asyncio.wait_for(entered.wait(), 2)
        self.config["enable_observe_tool"] = False
        release.set()
        run = await self.finished(proposal)
        self.assertNotEqual(run.status, "done")
        self.assertEqual([a for _, a, _ in self.calls if a != "ui_snapshot"], ["open_app"])

    async def test_role_revision_changed_while_model_waits_stops_next_write(self):
        await self.register()
        self.simulate_phone(await self.register("bot-1", "bot"))
        entered, release = self.block_model({"action": "tap", "node_id": "n1"})
        proposal = await self.propose(device_id="bot-1")
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        await asyncio.wait_for(entered.wait(), 2)
        self.plugin._devices.assign("bot-1", "unassigned")
        release.set()
        run = await self.finished(proposal)
        self.assertNotEqual(run.status, "done")
        self.assertEqual(run.summary["ui_reason"], "device_changed")
        self.assertEqual([a for _, a, _ in self.calls if a != "ui_snapshot"], ["open_app"])

    async def test_endpoint_or_app_version_change_invalidates_pending_approval(self):
        backend = await self.register()
        execute = self.simulate_phone(backend)
        proposal = await self.propose()
        self.plugin._devices.register({"device_id": "personal-1", "device_kind": "personal",
            "app_version": "0.4.0", "base_url": "http://replacement-endpoint:8260"})
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "device_changed")
        proposal = await self.propose()
        self.plugin._devices.register({"device_id": "personal-1", "device_kind": "personal",
            "app_version": "0.3.28", "base_url": "http://replacement-endpoint:8260"})
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await response.get_json())["error_code"], "authorization_changed")
        execute.assert_not_called()

    async def test_app_version_downgraded_during_model_await_stops_next_write(self):
        self.simulate_phone(await self.register())
        entered, release = self.block_model({"action": "tap", "node_id": "n1"})
        proposal = await self.propose()
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        await asyncio.wait_for(entered.wait(), 2)
        await self.register(version="0.3.28")
        release.set()
        run = await self.finished(proposal)
        self.assertNotEqual(run.status, "done")
        self.assertEqual([a for _, a, _ in self.calls if a != "ui_snapshot"], ["open_app"])

    async def test_ui_approval_is_blocked_by_another_ui_or_planned_run_on_same_device(self):
        backend = await self.register()
        execute = self.simulate_phone(backend)
        proposal = await self.propose()
        self.plugin._ui_busy_devices.add("personal-1")
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "device_busy")
        self.plugin._ui_busy_devices.clear()
        planned = module.PhoneTaskRun.new("synthetic planned task", subject="synthetic-owner", device_id="personal-1")
        self.plugin._task_runs.add_run(planned)
        self.plugin._task_executor._executing.add(planned.run_id)
        self.addCleanup(self.plugin._task_executor._executing.discard, planned.run_id)
        response = await self.approve(proposal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "device_busy")
        execute.assert_not_called()
        self.assertEqual(self.plugin._task_runs.grants(), [])

    async def test_planned_approval_cannot_write_while_ui_owns_same_device(self):
        backend = await self.register()
        execute = self.simulate_phone(backend)
        planned = module.PhoneTaskRun.new("synthetic planned task", subject="synthetic-owner", device_id="personal-1")
        planned.add_step("open_app", {"package": PACKAGE})
        planned.summary["device_generation"] = backend.binding()[1]
        planned.transition(module.RUN_WAITING_APPROVAL)
        approval = module.ApprovalRequest.create(run_id=planned.run_id, subject=planned.subject,
            device_id=planned.device_id, actions=["open_app"], packages=[PACKAGE], ttl_seconds=180)
        self.plugin._task_runs.add_run(planned)
        self.plugin._task_runs.add_approval(approval)
        self.plugin._ui_busy_devices.add("personal-1")
        response = await self.post("/app/task/approve", {"approval_id": approval.approval_id})
        body = await response.get_json()
        self.assertNotEqual(body.get("run", {}).get("status"), "done")
        self.assertFalse(any(call.args and call.args[0] == "open_app" for call in execute.call_args_list))

    async def test_direct_ui_commands_require_task_approval_even_if_feature_is_enabled(self):
        execute = self.simulate_phone(await self.register())
        for action in ["ui_snapshot", "ui_action"]:
            with self.subTest(action=action):
                response = await self.post("/device/command", {"device_id": "personal-1", "action": action,
                    "args": {"expected_package": PACKAGE, "snapshot_id": "synthetic"}})
                self.assertEqual(response.status_code, 403)
                self.assertEqual((await response.get_json())["error_code"], "ui_task_approval_required")
        execute.assert_not_called()

    async def test_cross_origin_proposal_and_cancel_are_rejected(self):
        await self.register()
        response = await self.post("/app/ui-task/propose", self.scope(), headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        proposal = await self.propose()
        response = await self.post("/app/task/cancel", {"run_id": proposal["run"]["run_id"]},
                                   headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.plugin._task_runs.run(proposal["run"]["run_id"]).cancel_requested)

    def event(self, subject="synthetic-owner", group=False):
        return types.SimpleNamespace(get_sender_id=lambda: subject,
            unified_msg_origin="synthetic:" + ("GroupMessage:synthetic-group" if group else "FriendMessage:" + subject))

    async def test_chat_bot_selection_never_falls_back_to_personal_phone(self):
        execute = self.simulate_phone(await self.register())
        result = json.loads(await self.plugin.phone_ui_task(self.event(), goal="synthetic search", package=PACKAGE,
            device_role="bot", success_texts_json='["synthetic result"]'))
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "device_not_registered")
        self.assertEqual(self.plugin._task_runs.runs(), [])
        execute.assert_not_called()

    async def test_chat_proposal_selects_bot_scope_and_status_reports_actual_completion(self):
        self.simulate_phone(await self.register())
        self.simulate_phone(await self.register("bot-1", "bot"))
        result = json.loads(await self.plugin.phone_ui_task(self.event(), goal="synthetic search", package=PACKAGE,
            device_role="bot", allowed_texts_json='[]', success_texts_json='["synthetic result"]'))
        self.assertTrue(result["success"], result)
        self.assertTrue(result["awaiting_approval"])
        self.assertEqual(result["scope"]["device_id"], "bot-1")
        self.assertEqual(self.calls, [])
        status = json.loads(await self.plugin.phone_task_status(self.event(), run_id=result["run_id"]))
        self.assertEqual(status["status"], "waiting_approval")
        self.assertFalse(status["verified"])
        response = await self.post("/app/task/approve", {"approval_id": result["approval_id"]})
        self.assertEqual(response.status_code, 202)
        await self.finished({"run": {"run_id": result["run_id"]}})
        status = json.loads(await self.plugin.phone_task_status(self.event(), run_id=result["run_id"]))
        self.assertEqual(status["status"], "done")
        self.assertTrue(status["verified"])
        self.assertNotIn("synthetic result", json.dumps(status))
        self.assertEqual({device for device, _, _ in self.calls}, {"bot-1"})

    async def test_chat_ui_scope_and_task_status_are_private_and_owner_bound(self):
        await self.register()
        proposal = await self.propose()
        self.config["allowed_user_ids"] = "synthetic-owner,another-owner"
        for event in [self.event(group=True), self.event("unknown-owner")]:
            with self.subTest(event=event):
                result = json.loads(await self.plugin.phone_ui_task(event, goal="synthetic", package=PACKAGE,
                    success_texts_json='["synthetic result"]'))
                self.assertFalse(result["success"])
        self.config["group_allowed_tools"] = "phone_task_status"
        for event, code in [(self.event(group=True), "unauthorized"), (self.event("another-owner"), "not_found")]:
            with self.subTest(event=event):
                result = json.loads(await self.plugin.phone_task_status(event, run_id=proposal["run"]["run_id"]))
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("scope", result)
        self.assertEqual(len(self.plugin._task_runs.runs()), 1)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_malformed_chat_text_arrays_never_create_proposal(self):
        await self.register()
        for value in ["{", "null", '"text"', '["line\\nfeed"]', '[1]', "[]"]:
            with self.subTest(value=value):
                result = json.loads(await self.plugin.phone_ui_task(self.event(), goal="synthetic", package=PACKAGE,
                    success_texts_json=value))
                self.assertFalse(result["success"])
        self.assertEqual(self.plugin._task_runs.runs(), [])

    async def test_bot_cannot_upload_a_personal_health_snapshot(self):
        await self.register()
        await self.register("bot-1", "bot")
        self.config["enable_health_tools"] = True
        response = await self.post("/app/health/snapshot", {"device_id": "bot-1", "schema_version": 1, "days": []})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await response.get_json())["error_code"], "device_changed")

    async def test_bot_notifications_do_not_read_or_write_personal_store(self):
        await self.register()
        await self.register("bot-1", "bot")
        self.config["enable_notification_tool"] = True
        self.plugin._notifications = [{"key": "synthetic-personal", "title": "personal fixture"}]
        response = await self.client.get("/astrbot_plugin_phone_agent/app/notifications?device_id=bot-1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await response.get_json())["error_code"], "personal_device_required")
        response = await self.post("/app/notifications", {"device_id": "bot-1", "notifications": [{
            "key": "synthetic-bot", "title": "Bot fixture", "text": "Bot text", "package": PACKAGE}]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await response.get_json())["error_code"], "personal_device_required")
        self.assertEqual(self.plugin._notifications, [{"key": "synthetic-personal", "title": "personal fixture"}])

    async def test_failed_approval_persistence_does_not_start_phone_or_background_job(self):
        execute = self.simulate_phone(await self.register())
        proposal = await self.propose()
        with patch.object(self.plugin._task_runs, "save", return_value=False):
            response = await self.approve(proposal)
        self.assertEqual(response.status_code, 503)
        self.assertEqual((await response.get_json())["error_code"], "storage_unavailable")
        execute.assert_not_called()
        self.assertEqual(self.plugin._ui_handles, {})
        self.assertEqual(self.plugin._ui_busy_devices, set())


if __name__ == "__main__":
    unittest.main()
