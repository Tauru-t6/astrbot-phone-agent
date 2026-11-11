"""Regression coverage for the 0.9.1 review fixes; no real device or host."""
import asyncio
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner_api = types.ModuleType("astrbot.api")
runner_api.logger = Mock()
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": runner_api}):
    runner = load("review_runner", ROOT / "task_runner.py")
api_fixture = load("review_api_fixture", ROOT / "tests/test_app_api.py")
plugin_module = api_fixture.module
relay = load("review_relay_server", ROOT / "deploy/relay/relay_server.py")
PACKAGE = "com.example.review"


class ExecutionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runs.json"
        self.store = runner.TaskRunStore(self.path)
        self.calls = []
        self.binding = ("phone-a", 1)
        self.foreground = "com.test.launcher"

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def observe(self):
        return {"reported_at": time.time(), "foreground_package": self.foreground,
                "screen_interactive": True}

    async def act(self, action, args):
        self.calls.append((self.binding, action, args))
        return {"success": True}

    def grant(self, *, steps=1, run_id="", actions=("input_text",)):
        grant = runner.ScopedGrant.issue(subject="user", device_id="phone-a", actions=actions,
                                         packages=[PACKAGE], max_steps=steps, run_id=run_id)
        self.store.add_grant(grant)
        return grant

    def make_run(self, writes=1):
        run = runner.PhoneTaskRun.new("synthetic", subject="user", device_id="phone-a")
        run.add_step("observe")
        for _ in range(writes):
            run.add_step("input_text", {"text": "synthetic"})
        return run

    def executor(self, *, idle=False, binding=False):
        return runner.TaskExecutor(self.store, runner.TaskPlanner(), observe=self.observe,
                                   observe_live=self.observe, act=self.act, require_user_idle=idle,
                                   observation_wait=0.5,
                                   device_binding=(lambda: self.binding) if binding else None)

    async def test_grant_budget_is_shared_across_runs_and_survives_restart(self):
        grant = self.grant()
        first, second = self.make_run(), self.make_run()
        executor = self.executor()
        await executor.execute(first, grant=grant)
        await executor.execute(second, grant=grant)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second.last_error, "grant_step_limit")
        restarted = runner.TaskRunStore(self.path)
        restarted.load()
        self.assertIsNone(restarted.grant(grant.grant_id))
        self.assertEqual(restarted.grants(), [])

    async def test_concurrent_runs_cannot_spend_the_same_last_step(self):
        grant = self.grant()
        executor = self.executor()
        await asyncio.gather(executor.execute(self.make_run(), grant=grant), executor.execute(self.make_run(), grant=grant))
        self.assertEqual(len(self.calls), 1)

    async def test_approval_grant_cannot_authorize_another_plan(self):
        original = self.make_run()
        grant = self.grant(run_id=original.run_id)
        other = await self.executor().execute(self.make_run(), grant=grant)
        self.assertEqual(other.last_error, "grant_run_mismatch")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.grant_remaining(grant), 1)

    async def test_failed_budget_persistence_prevents_dispatch(self):
        grant = self.grant()
        with patch.object(self.store, "save", return_value=False):
            run = await self.executor().execute(self.make_run(), grant=grant)
        self.assertEqual(run.last_error, "grant_persistence_failed")
        self.assertEqual(self.calls, [])

    async def test_inflight_write_is_unknown_after_a_simulated_crash(self):
        executor = self.executor()
        run = self.make_run()
        grant = self.grant()

        async def inspect_durable_intent(action, args):
            recovered = runner.TaskRunStore(self.path)
            recovered.load()
            interrupted = recovered.run(run.run_id)
            self.assertEqual(interrupted.last_error, "interrupted")
            self.assertEqual(interrupted.steps[1].status, runner.STEP_UNKNOWN)
            self.assertIsNone(recovered.grant(grant.grant_id))
            return {"success": True}

        executor._act = inspect_durable_intent
        await executor.execute(run, grant=grant)
        self.assertEqual(run.status, runner.RUN_DONE)

    async def test_verification_exception_is_terminal_and_releases_subject(self):
        executor = self.executor()
        with patch.object(executor, "_settle", AsyncMock(side_effect=RuntimeError("synthetic"))):
            run = await executor.execute(self.make_run(), grant=self.grant())
        self.assertEqual(run.status, runner.RUN_FAILED)
        self.assertEqual(run.steps[1].status, runner.STEP_UNKNOWN)
        self.assertIsNone(self.store.active_for("user"))
        restarted = runner.TaskRunStore(self.path)
        restarted.load()
        self.assertEqual(restarted.run(run.run_id).status, runner.RUN_FAILED)

    async def test_unknown_foreground_refuses_an_unattended_write(self):
        self.foreground = ""
        run = await self.executor(idle=True).execute(self.make_run(), grant=self.grant())
        self.assertEqual(run.summary["refused"], "observation_unknown")
        self.assertEqual(self.calls, [])

    async def test_rebind_after_first_write_never_dispatches_next_write(self):
        executor = self.executor(binding=True)
        original = self.act

        async def rebind(action, args):
            result = await original(action, args)
            self.binding = ("phone-b", 2)
            return result

        executor._act = rebind
        run = await executor.execute(self.make_run(writes=2), grant=self.grant(steps=2))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][0], "phone-a")
        self.assertEqual(run.last_error, "device_changed")
        self.assertEqual(run.steps[1].status, runner.STEP_UNKNOWN)
        self.assertNotEqual(run.status, runner.RUN_DONE)

    async def test_same_device_new_binding_generation_also_invalidates_run(self):
        executor = self.executor(binding=True)
        run = self.make_run()
        run.summary["device_generation"] = 0
        result = await executor.execute(run, grant=self.grant())
        self.assertEqual(result.last_error, "device_changed")
        self.assertEqual(self.calls, [])

    async def test_dry_run_simulates_budget_without_spending_it(self):
        grant = self.grant()
        run = self.make_run(writes=2)
        run.transition(runner.RUN_RUNNING)
        preview = runner.ReadOnlyTaskRunner(self.store, runner.TaskPlanner(),
            lambda: {"reported_at": time.time() - 1, "foreground_package": self.foreground})
        preview.execute(run, grant=grant)
        self.assertTrue(run.steps[1].verify["would_execute"])
        self.assertFalse(run.steps[2].verify["would_execute"])
        self.assertEqual(run.steps[2].verify["reason"], "grant_step_limit")
        self.assertEqual(self.store.grant_remaining(grant), 1)


class StateRecoveryTests(unittest.TestCase):
    def test_corrupt_record_does_not_hide_valid_neighbor(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runs.json"
            valid = runner.PhoneTaskRun.new("valid", subject="u", device_id="d")
            bad = {**valid.to_dict(), "created_at": "oops"}
            path.write_text(json.dumps({"runs": [bad, valid.to_dict()], "grants": 12, "approvals": {}}))
            store = runner.TaskRunStore(path)
            store.load()
            self.assertIsNotNone(store.run(valid.run_id))
            self.assertEqual(len(store.runs()), 1)

    def test_legacy_grants_without_usage_are_not_resurrected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runs.json"
            grant = runner.ScopedGrant.issue(subject="u", device_id="d", actions=["home"])
            path.write_text(json.dumps({"version": 1, "grants": [grant.to_dict()]}))
            store = runner.TaskRunStore(path)
            store.load()
            self.assertEqual(store.grants(), [])

    def test_unknown_approval_status_is_not_pending(self):
        approval = runner.ApprovalRequest.from_dict({"approval_id": "a", "status": "corrupt",
                                                     "expires_at": time.time() + 60})
        self.assertFalse(approval.pending())

    def test_expired_approval_does_not_leave_a_subject_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            store = runner.TaskRunStore(Path(folder) / "runs.json")
            run = runner.PhoneTaskRun.new("synthetic", subject="user", device_id="phone")
            run.transition(runner.RUN_WAITING_APPROVAL)
            store.add_run(run)
            approval = runner.ApprovalRequest.from_dict({"approval_id": "expired", "run_id": run.run_id,
                                                        "expires_at": time.time() - 1, "status": "pending"})
            store.add_approval(approval)
            store.save()
            self.assertIsNone(store.active_for("user"))
            self.assertEqual(run.last_error, "approval_expired")


class DeviceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = plugin_module.DeviceAppBackend({"app_shared_token": "synthetic",
            "app_state_path": str(Path(self.temp.name) / "state.json")}, lambda *a, **k: None)
        self.backend.register({"device_id": "phone-a", "base_url": "http://phone-a:8260"})

    def tearDown(self):
        self.temp.cleanup()

    def test_binding_change_during_health_probe_prevents_the_command(self):
        binding = self.backend.binding()
        calls = []

        def respond(url, *args, **kwargs):
            calls.append(url)
            self.backend.register({"device_id": "phone-b", "base_url": "http://phone-b:8260"})
            return {"success": True}

        with patch.object(self.backend, "_http_json", side_effect=respond):
            result = self.backend.execute("home", expected_binding=binding)
        self.assertEqual(result["error_code"], "device_changed")
        self.assertFalse(any(url.endswith("/command") for url in calls))
        self.assertFalse(any("phone-b" in url for url in calls))

    def test_changed_endpoint_invalidates_the_same_device_binding(self):
        binding = self.backend.binding()
        self.backend.record_device_status({"reported_at": time.time(), "foreground_package": PACKAGE})
        self.backend.register({"device_id": "phone-a", "base_url": "http://new-address:8260"})
        with patch.object(self.backend, "_http_json") as http:
            result = self.backend.execute("home", expected_binding=binding)
        self.assertEqual(result["error_code"], "device_changed")
        self.assertIsNone(self.backend.last_status())
        http.assert_not_called()

    def test_legacy_relay_cannot_receive_a_device_bound_task(self):
        self.backend._config.update({"relay_base_url": "http://relay:8791", "relay_token": "synthetic"})
        with patch.object(self.backend, "_http_json", return_value={"success": True}) as http:
            result = self.backend._relay_submit(self.backend.build_envelope("home"), 5, expected_binding=self.backend.binding())
        self.assertEqual(result["error_code"], "relay_targeting_required")
        self.assertEqual(http.call_count, 1)
        self.assertTrue(http.call_args.args[0].endswith("/health"))


class RelayTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        relay.STATE_PATH = Path(self.temp.name) / "relay.json"
        relay._state = {"tasks": [], "results": {}}

    def tearDown(self):
        self.temp.cleanup()

    def test_only_target_device_can_claim_bound_command(self):
        task = relay.push_task({"message": "synthetic", "target_device_id": "phone-a"})
        self.assertIsNone(relay.poll_task("phone-b"))
        self.assertIsNone(relay.poll_task())
        self.assertEqual(relay.poll_task("phone-a")["id"], task["id"])

    def test_invalid_target_is_rejected(self):
        for target in ["", [], "bad\nidentity"]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                relay.push_task({"message": "synthetic", "target_device_id": target})

    def test_expired_natural_language_claim_is_never_replayed(self):
        task = relay.push_task({"message": "synthetic Operit action"})
        relay.poll_task("phone-a")
        relay._state["tasks"][0]["claimed_at"] = time.time() - relay.CLAIM_LEASE_SECONDS - 1
        self.assertIsNone(relay.poll_task("phone-a"))
        self.assertEqual(relay.get_result(task["id"])["status"], "unknown")


class PolicyAndTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(plugin_module.PhoneAgentPlugin)
        self.plugin.config = {"policy_state_path": str(Path(self.temp.name) / "policy.json"),
                              "control_backend": "adb", "adb_serial": "synthetic-device"}
        self.plugin._active_serial = None
        self.plugin._policy_leases = {}
        self.plugin._guard_suspended = set()
        self.plugin._manual_guard_until = None
        self.plugin._manual_guard_exempt = set()
        self.plugin._audit = lambda *args, **kwargs: None

    async def asyncTearDown(self):
        tasks = [lease.task for leases in self.plugin._policy_leases.values() for lease in leases.values() if lease.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.temp.cleanup()

    def test_policy_accepts_only_boolean_true_for_verification(self):
        for verified in [False, "false", "true", 1, [], {}]:
            with self.subTest(verified=verified):
                payload = {"success": True, "package": PACKAGE, "state": "suspended", "verified": verified}
                result = self.plugin._parse_policy_result(PACKAGE, "disable", {"success": True, "ai_response": json.dumps(payload)})
                self.assertFalse(result["success"])
        payload["verified"] = True
        self.assertTrue(self.plugin._parse_policy_result(PACKAGE, "disable", {"success": True, "ai_response": json.dumps(payload)})["success"])

    async def test_cancelled_policy_keeps_durable_deadline_and_restore_task(self):
        accepted = asyncio.Event()

        async def delayed(*args):
            accepted.set()
            await asyncio.Event().wait()

        self.plugin._apply_app_policy = delayed
        task = asyncio.create_task(self.plugin._execute_app_policy(PACKAGE, "disable", 10))
        await accepted.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        persisted = json.loads(Path(self.plugin.config["policy_state_path"]).read_text())
        self.assertGreater(persisted["packages"][PACKAGE]["leases"]["manual"]["until"], time.time())
        self.assertIsNotNone(self.plugin._policy_leases[PACKAGE]["manual"].task)

    async def test_policy_never_dispatches_without_durable_recovery_intent(self):
        self.plugin._apply_app_policy = AsyncMock()
        with patch.object(self.plugin, "_save_policy_state", return_value=False):
            result = await self.plugin._execute_app_policy(PACKAGE, "disable", 10)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "state_persistence_failed")
        self.plugin._apply_app_policy.assert_not_awaited()
        self.assertNotIn(PACKAGE, self.plugin._policy_leases)

    async def test_failed_final_policy_save_preserves_recovery_timer(self):
        self.plugin._apply_app_policy = AsyncMock(return_value={"success": True, "verified": True})
        real_save = self.plugin._save_policy_state
        attempts = 0

        def fail_after_intent():
            nonlocal attempts
            attempts += 1
            return real_save() if attempts == 1 else False

        with patch.object(self.plugin, "_save_policy_state", side_effect=fail_after_intent):
            result = await self.plugin._execute_app_policy(PACKAGE, "disable", 0)
        self.assertFalse(result["success"])
        self.assertFalse(result["state_persisted"])
        self.assertIsNotNone(self.plugin._policy_leases[PACKAGE]["manual"].task)
        self.assertIsNotNone(self.plugin._policy_leases[PACKAGE]["manual"].until)

    async def test_adb_text_metacharacters_remain_one_literal_argument(self):
        self.plugin._run = AsyncMock(return_value=(0, "", ""))
        text = "quoted ' text; & $(synthetic) | percent%"
        await self.plugin._execute("input_text", {"text": text})
        argv = self.plugin._run.await_args.args
        remote = " ".join(argv[argv.index("shell")+1:])
        self.assertEqual(shlex.split(remote), ["input", "text", text.replace(" ", "%s")])

    async def test_operit_verified_task_never_falls_back_to_adb(self):
        self.plugin.config["control_backend"] = "operit"
        self.plugin._execute = AsyncMock()
        result = await self.plugin._task_act("home", {})
        self.assertEqual(result["error_code"], "unsupported_task_backend")
        self.plugin._execute.assert_not_awaited()

    def test_negation_and_ordinary_chat_do_not_change_guard_state(self):
        for text in ["不要解除限制", "能不能解除限制？", "他说要解除限制", "我没有要求恢复应用"]:
            with self.subTest(text=text):
                self.assertIsNone(plugin_module.NG_RELEASE_RE.search(text))
        self.assertIsNotNone(plugin_module.NG_RELEASE_RE.search("解除限制"))

    def test_ambiguous_replies_do_not_approve(self):
        for text in ["好", "嗯", "可以", "行", "批准？", "不要批准"]:
            with self.subTest(text=text):
                self.assertIsNone(plugin_module.TASK_APPROVE_RE.fullmatch(text))
        self.assertIsNotNone(plugin_module.TASK_APPROVE_RE.fullmatch("批准"))


class PluginBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"enabled": True, "control_backend": "app", "enable_task_execute": True,
                       "enable_reminder_tools": True, "enable_observe_tool": True,
                       "enable_autonomy_scheduler": False, "allowed_user_ids": "user"}
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path",
                    "notification_path", "task_runs_path", "night_guard_state_path", "care_status_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        self.app = api_fixture.Quart("review-boundary-" + self.temp.name)
        context = types.SimpleNamespace(send_message=AsyncMock())
        context.register_web_api = lambda path, handler, methods, desc: self.app.add_url_rule(
            path, str(len(self.app.view_functions)), handler, methods=methods)
        with patch.object(plugin_module.PhoneAgentPlugin, "_warm_device_route"):
            self.plugin = plugin_module.PhoneAgentPlugin(context, self.config)
        self.client = self.app.test_client()
        self.plugin.device_app.register({"device_id": "phone-a", "base_url": "http://phone-a:8260"})

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_api_refuses_empty_package_scope(self):
        response = await self.client.post("/astrbot_plugin_phone_agent/app/grant",
                                          json={"actions": ["open_app"], "packages": []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await response.get_json())["error_code"], "invalid_packages")

    async def test_api_does_not_return_a_grant_after_storage_failure(self):
        with patch.object(self.plugin._task_runs, "save", return_value=False):
            response = await self.client.post("/astrbot_plugin_phone_agent/app/grant", json={"actions": ["home"]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.plugin._task_runs.grants(), [])

    async def test_actual_plugin_callbacks_never_follow_a_rebound_device(self):
        module = plugin_module
        backend = self.plugin.device_app
        calls = []
        foreground = {"phone-a": "com.test.launcher", "phone-b": PACKAGE}
        backend.record_device_status({"reported_at": time.time() - 1,
                                      "foreground_package": "com.test.launcher"})

        def execute(action, args=None, timeout=None, *, expected_binding=None):
            current = backend.binding()
            self.assertEqual(expected_binding, current)
            calls.append((current[0], action))
            if action == "quick_status":
                return {"success": True, "output": {"foreground_package": foreground[current[0]]}}
            if action == "open_app":
                foreground[current[0]] = PACKAGE
                backend.register({"device_id": "phone-b", "base_url": "http://phone-b:8260"})
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
        self.assertEqual(result.last_error, "device_changed")
        self.assertEqual(calls, [("phone-a", "open_app")])

    async def test_group_cannot_read_private_reminders_without_explicit_scope(self):
        self.plugin._reminders["synthetic"] = {"text": "private synthetic note", "owner": "user", "due": time.time()}
        event = types.SimpleNamespace(unified_msg_origin="test:GroupMessage:group", get_sender_id=lambda: "user")
        result = json.loads(await self.plugin.phone_reminder(event, action="list"))
        self.assertFalse(result["success"])
        self.assertNotIn("private synthetic note", json.dumps(result))

    async def test_group_cannot_read_operit_results_without_explicit_scope(self):
        self.plugin._operit_tasks["synthetic"] = {"owner": "user", "status": "completed",
                                                   "result": {"ai_response": "private synthetic output"}}
        event = types.SimpleNamespace(unified_msg_origin="test:GroupMessage:group", get_sender_id=lambda: "user")
        result = json.loads(await self.plugin.operit_task_status(event, task_id="synthetic"))
        self.assertFalse(result["success"])
        self.assertNotIn("private synthetic output", json.dumps(result))
