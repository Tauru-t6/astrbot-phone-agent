"""0.9.4 runtime review: policy changes during execution and ADB argument quoting."""
import asyncio
import json
from pathlib import Path
import shlex
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_app_api as fixture

module = fixture.module
PACKAGE = "com.example.review"


class RuntimeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"enabled": True, "control_backend": "app", "enable_task_execute": True,
                       "enable_observe_tool": True, "enable_autonomy_scheduler": False,
                       "task_execute_allowed_actions": "open_app,home,suspend_app,unsuspend_app",
                       "allowed_user_ids": "user"}
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path",
                    "notification_path", "task_runs_path", "night_guard_state_path", "care_status_path", "health_connect_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        self.app = fixture.Quart("runtime-review-" + self.temp.name)
        context = types.SimpleNamespace(send_message=AsyncMock())
        context.register_web_api = lambda path, handler, methods, desc: self.app.add_url_rule(
            path, str(len(self.app.view_functions)), handler, methods=methods)
        with patch.object(module.PhoneAgentPlugin, "_warm_device_route"):
            self.plugin = module.PhoneAgentPlugin(context, self.config)
        self.plugin.device_app.register({"device_id": "phone-a", "base_url": "http://phone-a:8260"})

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_workflow_extras_remain_single_literal_device_shell_arguments(self):
        self.plugin._run = AsyncMock(return_value=(0, "", ""))
        values = {"message": "hello; echo SYNTHETIC", "quoted": "' $(synthetic) & | < > `test`"}
        result = await self.plugin._execute("trigger_workflow", {
            "workflow_action": "example.action", "extras_json": json.dumps(values),
        })
        self.assertTrue(result["success"])
        args = self.plugin._run.await_args.args
        device_command = " ".join(args[args.index("shell") + 1:])
        words = shlex.split(device_command)
        self.assertEqual(words, ["am", "broadcast", "-n",
            "com.ai.assistance.operit/.integrations.tasker.WorkflowTaskerReceiver", "-a", "example.action",
            "--es", "message", values["message"], "--es", "quoted", values["quoted"]])

    async def test_pending_write_is_not_sent_after_runtime_switch_or_scope_revocation(self):
        cases = [{"enabled": False}, {"enable_task_execute": False},
                 {"task_execute_allowed_actions": "open_app"}]
        for update in cases:
            with self.subTest(update=update):
                self.config.update(enabled=True, enable_task_execute=True,
                                   task_execute_allowed_actions="open_app,home,suspend_app,unsuspend_app")
                backend = self.plugin.device_app
                foreground = "com.test.launcher"
                writes = []
                backend.record_device_status({"device_id": "phone-a", "reported_at": time.time(),
                                              "foreground_package": foreground, "screen_interactive": True})

                def execute(action, args=None, timeout=None, **kwargs):
                    nonlocal foreground
                    if action == "quick_status":
                        return {"success": True, "output": {"foreground_package": foreground, "screen_interactive": True}}
                    writes.append(action)
                    if action == "open_app":
                        foreground = PACKAGE
                        self.config.update(update)
                    elif action == "home":
                        foreground = "com.test.launcher"
                    return {"success": True}

                run = module.PhoneTaskRun.new("synthetic", subject="user", device_id="phone-a")
                run.add_step("observe")
                run.add_step("open_app", {"package": PACKAGE})
                run.add_step("home")
                grant = module.ScopedGrant.issue(subject="user", device_id="phone-a", actions=["open_app", "home"],
                    packages=[PACKAGE], max_steps=2, run_id=run.run_id)
                self.plugin._task_runs.add_grant(grant)
                with patch.object(backend, "execute", side_effect=execute):
                    result = await self.plugin._task_executor.execute(run, grant=grant, allow_active=True)
                self.assertEqual(writes, ["open_app"])
                self.assertNotEqual(result.status, "done")

    async def test_bound_rollback_can_restore_after_task_switch_is_disabled(self):
        calls = []
        backend = self.plugin.device_app
        backend.record_device_status({"device_id": "phone-a", "reported_at": time.time(),
                                      "foreground_package": PACKAGE, "screen_interactive": True})
        self.plugin._task_executor._observation_wait = 0.5

        async def apply(package, action, **kwargs):
            self.assertEqual(kwargs.get("expected_binding"), backend.binding())
            calls.append(action)
            if action == "disable":
                self.config["enable_task_execute"] = False
                return {"success": True, "verified": False}
            return {"success": True, "verified": True}

        def observe(action, *args, **kwargs):
            self.assertEqual(action, "quick_status")
            return {"success": True, "output": {"foreground_package": PACKAGE}}

        run = module.PhoneTaskRun.new("synthetic", subject="user", device_id="phone-a")
        run.add_step("suspend_app", {"package": PACKAGE})
        grant = module.ScopedGrant.issue(subject="user", device_id="phone-a",
            actions=["suspend_app", "unsuspend_app"], packages=[PACKAGE], max_steps=2, run_id=run.run_id)
        self.plugin._task_runs.add_grant(grant)
        with patch.object(self.plugin, "_apply_app_policy", side_effect=apply), patch.object(backend, "execute", side_effect=observe):
            result = await self.plugin._task_executor.execute(run, grant=grant, allow_active=True)
        self.assertEqual(calls, ["disable", "enable"])
        self.assertEqual(result.summary["rolled_back"], 1)
        self.assertEqual(result.status, "failed")

    async def test_stored_step_cannot_inject_the_recovery_bypass(self):
        self.config["enabled"] = False
        backend = self.plugin.device_app
        backend.record_device_status({"device_id": "phone-a", "reported_at": time.time(),
                                      "foreground_package": "com.test.launcher", "screen_interactive": True})
        run = module.PhoneTaskRun.new("synthetic", subject="user", device_id="phone-a")
        run.add_step("unsuspend_app", {"package": PACKAGE, "_task_recovery": True})
        grant = module.ScopedGrant.issue(subject="user", device_id="phone-a", actions=["unsuspend_app"],
            packages=[PACKAGE], max_steps=1, run_id=run.run_id)
        self.plugin._task_runs.add_grant(grant)
        with patch.object(self.plugin, "_apply_app_policy", new=AsyncMock()) as apply, patch.object(
                backend, "execute", return_value={"success": True, "output": {"foreground_package": "com.test.launcher"}}):
            result = await self.plugin._task_executor.execute(run, grant=grant, allow_active=True)
        apply.assert_not_awaited()
        self.assertNotEqual(result.status, "done")

    def audit_rows(self):
        path = Path(self.config["audit_log_path"])
        return [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()]

    async def test_task_goal_remains_in_authorized_history_but_not_in_audit(self):
        secret_goal = "打开合成私密任务标记"
        run = module.PhoneTaskRun.new(secret_goal, subject="user", device_id="phone-a")
        run.add_step("open_app", {"package": PACKAGE})
        with patch.object(self.plugin, "_start_planned_task", new=AsyncMock(return_value=run)):
            response = await self.app.test_client().post("/astrbot_plugin_phone_agent/app/task/propose", json={"goal": secret_goal})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["run"]["goal"], secret_goal)
        rows = self.audit_rows()
        self.assertNotIn(secret_goal, json.dumps(rows, ensure_ascii=False))
        entry = next(row for row in rows if row["operation"] == "task_proposed")
        self.assertEqual(entry["goal_length"], len(secret_goal))
        self.assertEqual(entry["actions"], ["open_app"])

    async def test_model_reply_and_sent_warning_body_are_not_written_to_audit(self):
        private_reply = "synthetic private reply\nsecond line"
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text=private_reply)))
        self.plugin.context.get_using_provider = lambda **kwargs: provider
        text, source = await self.plugin._night_guard_warning_text("Example", 10, "test:FriendMessage:user")
        self.assertEqual(source, "template")
        entry = next(row for row in self.audit_rows() if row["operation"] == "night_guard_warning_llm_fallback")
        self.assertEqual(entry["reason"], "multi_line")
        self.assertEqual(entry["response_length"], len(private_reply))
        self.assertNotIn("text", entry)
        self.assertNotIn(private_reply.splitlines()[0], json.dumps(self.audit_rows()))

        sent_body = "synthetic personal warning body"
        with patch.object(self.plugin, "_night_guard_session", return_value="test:FriendMessage:user"), patch.object(
                self.plugin, "_night_guard_warning_text", new=AsyncMock(return_value=(sent_body, "llm"))):
            self.assertTrue(await self.plugin._night_guard_send_warning("user", {}, PACKAGE))
        self.plugin.context.send_message.assert_awaited_once()
        entry = next(row for row in self.audit_rows() if row["operation"] == "night_guard_warning_sent")
        self.assertEqual(entry["message_length"], len(sent_body))
        self.assertEqual(entry["source"], "llm")
        self.assertNotIn(sent_body, json.dumps(self.audit_rows()))

    async def test_model_failure_logs_type_without_echoing_request_or_credentials(self):
        private_error = "synthetic request body and credential"
        provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=RuntimeError(private_error)))
        self.plugin.context.get_using_provider = lambda **kwargs: provider
        await self.plugin._night_guard_warning_text("Example", 10, "test:FriendMessage:user")
        entry = next(row for row in self.audit_rows() if row["operation"] == "night_guard_warning_llm_fallback")
        self.assertEqual(entry["error_type"], "RuntimeError")
        self.assertEqual(entry["reason"], "error")
        self.assertNotIn(private_error, json.dumps(self.audit_rows()))

    async def test_false_or_malformed_confirmation_never_dispatches_operit_or_workflow(self):
        self.config["control_backend"] = "operit"
        event = types.SimpleNamespace(unified_msg_origin="test:FriendMessage:user", get_sender_id=lambda: "user")
        for confirmation in ("false", "0", "no", "off", "invalid", [], {}, 1, False):
            with self.subTest(confirmation=confirmation):
                with patch.object(self.plugin, "_operit_health_sync") as health, patch.object(
                        self.plugin, "_operit_action", new=AsyncMock()) as action:
                    result = json.loads(await self.plugin.operit_task(event, task="点击合成按钮", confirmed=confirmation))
                    self.assertTrue(result.get("needs_confirmation"))
                    workflow = json.loads(await self.plugin.phone_action(event, action="trigger_workflow", confirmed=confirmation))
                    self.assertTrue(workflow.get("needs_confirmation"))
                health.assert_not_called()
                action.assert_not_awaited()

    async def test_explicit_true_confirmation_can_dispatch_the_expected_workflow(self):
        self.config["control_backend"] = "operit"
        event = types.SimpleNamespace(unified_msg_origin="test:FriendMessage:user", get_sender_id=lambda: "user")
        with patch.object(self.plugin, "_operit_action", new=AsyncMock(return_value={"success": True})) as action:
            result = json.loads(await self.plugin.phone_action(event, action="trigger_workflow", confirmed="true"))
        self.assertTrue(result["success"])
        action.assert_awaited_once()
