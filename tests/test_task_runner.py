"""P1-E stage 1-3: scoped grants, observations, run state, real execution."""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("task_runner_under_test", ROOT / "task_runner.py")
module = importlib.util.module_from_spec(spec)
sys.modules["task_runner_under_test"] = module
spec.loader.exec_module(module)

VIDEO = "tv.danmaku.bili"
USER = "u-10001"
DEVICE = "phone-a"


class GrantTests(unittest.TestCase):
    def issue(self, **overrides):
        params = dict(
            subject=USER,
            device_id=DEVICE,
            actions=["suspend_app"],
            packages=[VIDEO],
            ttl_seconds=300,
            now=1000.0,
        )
        params.update(overrides)
        return module.ScopedGrant.issue(**params)

    def test_grant_allows_a_covered_step(self):
        grant = self.issue()
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="suspend_app",
                                      package=VIDEO, now=1010.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_grant_expires(self):
        grant = self.issue()
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="suspend_app",
                                      package=VIDEO, now=2000.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "grant_expired")

    def test_grant_is_bound_to_subject_and_device(self):
        grant = self.issue()
        self.assertEqual(
            grant.check(subject="someone-else", device_id=DEVICE, action="suspend_app",
                        package=VIDEO, now=1010.0)[1],
            "grant_subject_mismatch",
        )
        self.assertEqual(
            grant.check(subject=USER, device_id="phone-b", action="suspend_app",
                        package=VIDEO, now=1010.0)[1],
            "grant_device_mismatch",
        )

    def test_grant_rejects_actions_outside_scope(self):
        grant = self.issue()
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="input_text",
                                      package=VIDEO, now=1010.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "grant_action_not_allowed")

    def test_write_action_requires_a_package(self):
        grant = self.issue(packages=[])
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="suspend_app", now=1010.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "grant_package_required")

    def test_package_allowlist_is_enforced(self):
        grant = self.issue()
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="suspend_app",
                                      package="com.other.app", now=1010.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "grant_package_not_allowed")

    def test_empty_package_allowlist_means_any_package(self):
        grant = self.issue(packages=[])
        allowed, reason = grant.check(subject=USER, device_id=DEVICE, action="suspend_app",
                                      package="com.any.app", now=1010.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_grant_roundtrip(self):
        grant = self.issue()
        restored = module.ScopedGrant.from_dict(grant.to_dict())
        self.assertEqual(restored, grant)
        self.assertTrue(restored.active(now=1010.0))


class ObservationTests(unittest.TestCase):
    def status(self, **overrides):
        base = {
            "foreground_package": "tv.danmaku.bili",
            "reported_at": 1000.0,
            "received_at": 1001.0,
            "screen_interactive": True,
            "sample_id": "s-1",
        }
        base.update(overrides)
        return base

    def test_observation_keeps_phone_sampling_time(self):
        ref = module.ObservationRef.from_status(self.status(), device_id=DEVICE, now=1005.0)
        self.assertEqual(ref.reported_at, 1000.0)
        self.assertEqual(ref.age(now=1005.0), 5.0)
        self.assertTrue(ref.fresh(90, now=1005.0))
        self.assertFalse(ref.fresh(3, now=1005.0))

    def test_missing_reported_at_is_not_fresh(self):
        ref = module.ObservationRef.from_status(self.status(reported_at=None), device_id=DEVICE)
        self.assertIsNone(ref.age())
        self.assertFalse(ref.fresh(90))

    def test_observation_roundtrip(self):
        ref = module.ObservationRef.from_status(
            self.status(), device_id=DEVICE, ui_nodes=["确定", "取消"], now=1005.0
        )
        restored = module.ObservationRef.from_dict(ref.to_dict())
        self.assertEqual(restored, ref)
        self.assertEqual(restored.ui_nodes, ("确定", "取消"))


class RunTests(unittest.TestCase):
    def new_run(self):
        return module.PhoneTaskRun.new("把 B 站停掉", subject=USER, device_id=DEVICE, now=1000.0)

    def test_run_records_the_full_step_chain(self):
        run = self.new_run()
        step = run.add_step("suspend_app", {"package": VIDEO}, grant_id="g-1", observation_id="o-1")
        step.start(command_id="cmd-1", now=1001.0)
        step.finish(ok=True, verify={"state": "suspended", "verified": True}, now=1002.0)
        self.assertEqual(run.next_pending(), None)
        self.assertEqual(step.status, module.STEP_DONE)
        chain = run.to_dict()["steps"][0]
        for key in ("step_id", "run_id", "grant_id", "observation_id", "command_id"):
            self.assertTrue(chain[key])
        self.assertTrue(chain["verify"]["verified"])

    def test_failed_step_is_marked_and_reported(self):
        run = self.new_run()
        step = run.add_step("suspend_app", {"package": VIDEO})
        step.finish(ok=False, error="device offline", now=1001.0)
        self.assertEqual(step.status, module.STEP_FAILED)
        self.assertTrue(run.has_failed_step())
        self.assertEqual(step.error, "device offline")

    def test_terminal_status_is_final(self):
        run = self.new_run()
        self.assertTrue(run.transition(module.RUN_DONE, now=1001.0))
        self.assertFalse(run.transition(module.RUN_RUNNING, now=1002.0))
        self.assertFalse(run.transition("not-a-status"))
        self.assertEqual(run.status, module.RUN_DONE)

    def test_cancel_skips_pending_steps(self):
        run = self.new_run()
        first = run.add_step("observe")
        second = run.add_step("suspend_app", {"package": VIDEO})
        first.start(now=1001.0)
        run.cancel("user_cancelled", now=1002.0)
        self.assertEqual(run.status, module.RUN_CANCELLED)
        self.assertTrue(run.cancel_requested)
        self.assertEqual(second.status, module.STEP_SKIPPED)
        self.assertEqual(first.status, module.STEP_SKIPPED)

    def test_run_roundtrip(self):
        run = self.new_run()
        step = run.add_step("suspend_app", {"package": VIDEO}, grant_id="g-1")
        step.finish(ok=True, verify={"verified": True}, now=1001.0)
        restored = module.PhoneTaskRun.from_dict(run.to_dict())
        self.assertEqual(restored.run_id, run.run_id)
        self.assertEqual(len(restored.steps), 1)
        self.assertEqual(restored.steps[0].verify, {"verified": True})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runs.json"
        self.store = module.TaskRunStore(self.path, max_runs=3)

    def tearDown(self):
        self.temp.cleanup()

    def test_save_and_load_roundtrip(self):
        run = module.PhoneTaskRun.new("目标", subject=USER, device_id=DEVICE, now=1000.0)
        run.add_step("observe")
        self.store.add_run(run)
        grant = module.ScopedGrant.issue(
            subject=USER, device_id=DEVICE, actions=["suspend_app"], packages=[VIDEO],
            ttl_seconds=300, now=time.time(),
        )
        self.store.add_grant(grant)
        self.assertTrue(self.store.save())

        restored = module.TaskRunStore(self.path, max_runs=3)
        restored.load()
        self.assertIsNotNone(restored.run(run.run_id))
        self.assertEqual(restored.run(run.run_id).steps[0].action, "observe")
        self.assertIsNotNone(restored.grant(grant.grant_id))
        self.assertEqual(list(self.path.parent.glob("runs.json.*.tmp")), [])

    def test_expired_grants_are_dropped_on_load(self):
        grant = module.ScopedGrant.issue(
            subject=USER, device_id=DEVICE, actions=["suspend_app"], packages=[VIDEO],
            ttl_seconds=1, now=time.time() - 100,
        )
        self.store.add_grant(grant)
        self.store.save()
        restored = module.TaskRunStore(self.path)
        restored.load()
        self.assertIsNone(restored.grant(grant.grant_id))

    def test_history_is_capped(self):
        for index in range(5):
            run = module.PhoneTaskRun.new(f"goal-{index}", subject=USER, device_id=DEVICE, now=1000.0 + index)
            self.store.add_run(run)
        self.store.save()
        restored = module.TaskRunStore(self.path, max_runs=3)
        restored.load()
        self.assertEqual(len(restored.runs(limit=10)), 3)

    def test_active_for_finds_the_open_run(self):
        run = module.PhoneTaskRun.new("goal", subject=USER, device_id=DEVICE, now=1000.0)
        self.store.add_run(run)
        self.assertEqual(self.store.active_for(USER).run_id, run.run_id)
        run.transition(module.RUN_DONE)
        self.assertIsNone(self.store.active_for(USER))

    def test_save_failure_returns_false(self):
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        store = module.TaskRunStore(blocked / "runs.json")
        self.assertFalse(store.save())


class PlannerTests(unittest.TestCase):
    def test_goal_with_app_maps_to_observe_then_suspend(self):
        planner = module.TaskPlanner()
        steps = planner.plan("帮我把B站暂停一会儿")
        self.assertEqual([step.action for step in steps], ["observe", "suspend_app"])
        self.assertEqual(steps[1].args["package"], "tv.danmaku.bili")

    def test_goal_without_known_app_only_observes(self):
        planner = module.TaskPlanner()
        steps = planner.plan("看看我现在在干嘛")
        self.assertEqual([step.action for step in steps], ["observe"])

    def test_custom_alias_overrides_defaults(self):
        planner = module.TaskPlanner({"某视频": "com.demo.video"})
        steps = planner.plan("把某视频关掉")
        self.assertEqual(steps[-1].args["package"], "com.demo.video")

    def test_screenshot_goal_adds_readonly_step(self):
        steps = module.TaskPlanner().plan("截个图看看")
        self.assertEqual([step.action for step in steps], ["observe", "screenshot"])

    def test_home_goal_plans_a_verifiable_step(self):
        steps = module.TaskPlanner().plan("回到桌面")
        self.assertEqual([step.action for step in steps], ["observe", "home"])


class ReadOnlyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = module.TaskRunStore(Path(self.temp.name) / "runs.json")
        self.fresh = {
            "foreground_package": "tv.danmaku.bili",
            "reported_at": time.time(),
            "received_at": time.time(),
        }
        self.runner = module.ReadOnlyTaskRunner(
            self.store, module.TaskPlanner(), lambda: self.fresh
        )

    def tearDown(self):
        self.temp.cleanup()

    def grant(self, *, actions=("suspend_app",), packages=("tv.danmaku.bili",), ttl=300):
        return module.ScopedGrant.issue(
            subject=USER, device_id=DEVICE, actions=list(actions), packages=list(packages),
            ttl_seconds=ttl, now=time.time(),
        )

    def test_run_simulates_write_without_executing_it(self):
        run = self.runner.start("把B站暂停", subject=USER, device_id=DEVICE)
        run = self.runner.execute(run, grant=self.grant())
        self.assertEqual([step.action for step in run.steps], ["observe", "suspend_app"])
        self.assertEqual(run.steps[0].status, module.STEP_DONE)
        self.assertEqual(run.steps[1].status, module.STEP_SKIPPED)
        self.assertTrue(run.steps[1].verify["would_execute"])
        self.assertEqual(run.status, module.RUN_DONE)
        self.assertEqual(run.summary["simulated"], 1)
        self.assertEqual(run.summary["blocked"], 0)

    def test_write_is_blocked_without_a_grant(self):
        run = self.runner.start("把B站暂停", subject=USER, device_id=DEVICE)
        run = self.runner.execute(run)
        self.assertFalse(run.steps[1].verify["would_execute"])
        self.assertEqual(run.steps[1].verify["reason"], "grant_missing")
        self.assertEqual(run.summary["blocked"], 1)

    def test_grant_for_another_package_is_reported(self):
        run = self.runner.start("把B站暂停", subject=USER, device_id=DEVICE)
        run = self.runner.execute(run, grant=self.grant(packages=("com.other",)))
        self.assertFalse(run.steps[1].verify["would_execute"])
        self.assertEqual(run.steps[1].verify["reason"], "grant_package_not_allowed")

    def test_stale_observation_fails_the_run(self):
        self.fresh["reported_at"] = time.time() - 600
        run = self.runner.start("把B站暂停", subject=USER, device_id=DEVICE)
        run = self.runner.execute(run, grant=self.grant())
        self.assertEqual(run.status, module.RUN_FAILED)
        self.assertEqual(run.steps[0].status, module.STEP_FAILED)
        self.assertEqual(run.steps[1].status, module.STEP_PENDING)
        self.assertEqual(run.last_error, "observation_stale")

    def test_run_is_persisted(self):
        run = self.runner.start("把B站暂停", subject=USER, device_id=DEVICE)
        self.runner.execute(run, grant=self.grant())
        restored = module.TaskRunStore(Path(self.temp.name) / "runs.json")
        restored.load()
        self.assertIsNotNone(restored.run(run.run_id))
        self.assertEqual(restored.run(run.run_id).summary["dry_run"], True)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """Stage 3: a command that returns success is not proof the step worked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = module.TaskRunStore(Path(self.temp.name) / "runs.json")
        self.status = {"foreground_package": "com.android.launcher", "reported_at": time.time(),
                       "screen_interactive": True}
        self.results = {}
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def make_device(self):
        status, results, calls = self.status, self.results, self.calls

        class Device:
            async def observe(self):
                return dict(status)

            async def act(self, action, args):
                calls.append((action, dict(args)))
                effect = results.get(action)
                if callable(effect):
                    effect()
                    return {"success": True, "verified": True}
                return effect if isinstance(effect, dict) else {"success": True, "verified": True}

        return Device()

    def build(self, actions, *, allowed=None, step_timeout=2.0):
        device = self.make_device()
        executor = module.TaskExecutor(
            self.store, module.TaskPlanner(),
            observe=device.observe, act=device.act, observe_live=device.observe,
            allowed_actions=allowed, observation_wait=0.3,
            step_timeout=step_timeout, observation_max_age=90.0,
        )
        run = module.PhoneTaskRun.new("goal", subject=USER, device_id=DEVICE, now=time.time())
        for action in actions:
            args = {"package": VIDEO} if action in {"open_app", "close_app", "suspend_app", "unsuspend_app"} else {}
            run.add_step(action, args)
        return executor, run

    def grant(self, actions=("open_app",), packages=(), max_steps=3):
        return module.ScopedGrant.issue(
            subject=USER, device_id=DEVICE, actions=list(actions), packages=list(packages),
            ttl_seconds=120, max_steps=max_steps, now=time.time(),
        )

    async def test_open_app_passes_only_when_foreground_changes(self):
        def launch():
            self.status["foreground_package"] = VIDEO
            self.status["reported_at"] = time.time() + 1

        self.results["open_app"] = launch
        executor, run = self.build(["observe", "open_app"])
        run = await executor.execute(run, grant=self.grant())
        self.assertEqual(run.steps[1].status, module.STEP_DONE)
        self.assertEqual(run.summary["verified"], 1)
        self.assertEqual(run.status, module.RUN_DONE)

    async def test_open_app_failure_is_reported_without_retry(self):
        executor, run = self.build(["observe", "open_app"])
        run = await executor.execute(run, grant=self.grant())
        self.assertEqual(run.steps[1].status, module.STEP_FAILED)
        self.assertEqual(run.steps[1].verify["reason"], "foreground_mismatch")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(run.status, module.RUN_FAILED)

    async def test_suspend_requires_device_verification(self):
        self.results["suspend_app"] = {"success": True, "verified": False}
        executor, run = self.build(["observe", "suspend_app"],
                                   allowed={"suspend_app"}, step_timeout=1.0)
        run = await executor.execute(run, grant=self.grant(actions=["suspend_app"]))
        self.assertEqual(run.steps[1].status, module.STEP_FAILED)
        self.assertEqual(run.steps[1].verify["reason"], "not_verified")

    async def test_suspend_rolls_back_when_grant_allows_it(self):
        self.results["suspend_app"] = {"success": True, "verified": False}
        self.results["unsuspend_app"] = {"success": True, "verified": True}
        executor, run = self.build(["observe", "suspend_app"], allowed={"suspend_app"})
        run = await executor.execute(run, grant=self.grant(actions=["suspend_app", "unsuspend_app"]))
        self.assertEqual([call[0] for call in self.calls], ["suspend_app", "unsuspend_app"])
        self.assertEqual(run.summary["rolled_back"], 1)

    async def test_suspend_without_unsuspend_grant_marks_restore_pending(self):
        self.results["suspend_app"] = {"success": True, "verified": False}
        executor, run = self.build(["observe", "suspend_app"], allowed={"suspend_app"})
        run = await executor.execute(run, grant=self.grant(actions=["suspend_app"]))
        self.assertEqual(run.summary["restore_pending"], 1)
        self.assertEqual([call[0] for call in self.calls], ["suspend_app"])

    async def test_action_outside_allowlist_never_runs(self):
        executor, run = self.build(["observe", "open_app"], allowed={"home"})
        run = await executor.execute(run, grant=self.grant())
        self.assertEqual(run.steps[1].verify["reason"], "action_not_enabled")
        self.assertEqual(self.calls, [])

    async def test_missing_grant_never_runs(self):
        executor, run = self.build(["observe", "open_app"])
        run = await executor.execute(run, grant=None)
        self.assertEqual(run.steps[1].verify["reason"], "grant_missing")
        self.assertEqual(self.calls, [])

    async def test_step_budget_is_enforced(self):
        def launch():
            self.status["foreground_package"] = VIDEO
            self.status["reported_at"] = time.time() + 1

        def go_home():
            self.status["foreground_package"] = "com.android.launcher"
            self.status["reported_at"] = time.time() + 2

        self.results["open_app"] = launch
        self.results["home"] = go_home
        executor, run = self.build(["observe", "open_app", "home"], allowed={"open_app", "home"})
        run = await executor.execute(run, grant=self.grant(actions=["open_app", "home"], max_steps=1))
        self.assertEqual(run.steps[2].verify["reason"], "grant_step_limit")

    async def test_timeout_marks_unknown_and_does_not_resend(self):
        async def slow(action, args):
            self.calls.append((action, dict(args)))
            await __import__("asyncio").sleep(0.5)
            return {"success": True}

        device = self.make_device()
        executor = module.TaskExecutor(
            self.store, module.TaskPlanner(), observe=device.observe, act=slow,
            observe_live=device.observe,
            allowed_actions={"open_app"}, observation_wait=0.2, step_timeout=0.05,
        )
        run = module.PhoneTaskRun.new("goal", subject=USER, device_id=DEVICE, now=time.time())
        run.add_step("observe", {})
        run.add_step("open_app", {"package": VIDEO})
        run = await executor.execute(run, grant=self.grant())
        self.assertEqual(run.steps[1].status, module.STEP_UNKNOWN)
        self.assertEqual(len(self.calls), 1)

    async def test_lock_screen_requires_screen_off(self):
        self.results["lock_screen"] = {"success": True, "verified": True}
        executor, run = self.build(["observe", "lock_screen"], allowed={"lock_screen"})
        run = await executor.execute(run, grant=self.grant(actions=["lock_screen"]))
        self.assertEqual(run.steps[1].verify["reason"], "screen_still_on")

    async def test_stale_observation_fails_before_execution(self):
        self.status["reported_at"] = time.time() - 600
        executor, run = self.build(["observe", "open_app"], allowed={"open_app"})
        run = await executor.execute(run, grant=self.grant())
        self.assertEqual(run.summary["refused"], "stale_observation")
        self.assertEqual(self.calls, [])


    async def test_refuses_when_the_user_is_using_the_phone(self):
        self.status["foreground_package"] = "com.tencent.mobileqq"
        executor, run = self.build(["observe", "home"], allowed={"home"})
        run = await executor.execute(run, grant=self.grant(actions=["home"]))
        self.assertEqual(run.status, module.RUN_FAILED)
        self.assertEqual(run.summary["refused"], "user_active")
        self.assertEqual(self.calls, [])
        self.assertEqual(run.steps[0].status, module.STEP_SKIPPED)

    async def test_explicit_override_allows_running_while_active(self):
        def go_home():
            self.status["foreground_package"] = "com.android.launcher"

        self.status["foreground_package"] = "com.tencent.mobileqq"
        self.results["home"] = go_home
        executor, run = self.build(["observe", "home"], allowed={"home"})
        run = await executor.execute(run, grant=self.grant(actions=["home"]), allow_active=True)
        self.assertEqual(run.status, module.RUN_DONE)
        self.assertTrue(run.summary["allow_active"])

    async def test_launcher_foreground_counts_as_idle(self):
        def launch():
            self.status["reported_at"] = time.time() + 1

        self.results["home"] = launch
        executor, run = self.build(["observe", "home"], allowed={"home"})
        run = await executor.execute(run, grant=self.grant(actions=["home"]))
        self.assertNotEqual(run.summary.get("refused"), "user_active")

    async def test_screen_off_counts_as_idle(self):
        self.status["screen_interactive"] = False
        executor, run = self.build(["observe", "home"], allowed={"home"})
        run = await executor.execute(run, grant=self.grant(actions=["home"]))
        self.assertNotEqual(run.summary.get("refused"), "user_active")


if __name__ == "__main__":
    unittest.main()
