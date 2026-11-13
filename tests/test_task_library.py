"""Template substitution, finite standing consent and scheduling use no devices."""
import copy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
package = types.ModuleType("task_library_test")
package.__path__ = [str(ROOT)]
api = types.ModuleType("astrbot.api"); api.logger = Mock()
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api, "task_library_test": package}):
    library = importlib.import_module("task_library_test.task_library")
    tasks = importlib.import_module("task_library_test.task_runner")

APP = "tv.danmaku.bili"
OWNER = "owner"
DEVICE = "bot-phone"
START = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc).timestamp()


def run(status="done", **scope):
    value = tasks.PhoneTaskRun.new("搜索 Kotlin 并显示相关结果", subject=OWNER, device_id=DEVICE, now=START)
    value.status = status
    value.summary = {"mode": "ui", "ui_started": True, "requester_device_id": "personal-phone", "ui_scope": {
        "device_id": DEVICE, "device_role": "bot", "package": APP, "allowed_texts": ["Kotlin"],
        "success_texts": ["Kotlin", "综合"], "max_steps": 8, "completion_mode": "search_results",
        "allow_vision": False, "remember_recipe": False, **scope}}
    return value


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.json"
        self.store = library.TaskLibrary(self.path)

    def tearDown(self): self.temp.cleanup()

    def save(self, **overrides):
        return self.store.save_from_run(run(**overrides), "视频搜索", {"query": "Kotlin"}, now=START)

    def test_template_parameters_change_only_goal_and_approved_literals(self):
        saved = self.save()
        self.assertEqual(saved["parameters"], [{"name": "query", "default": "Kotlin", "max_length": 200}])
        self.assertEqual(saved["scope"]["allowed_texts"], ["{{query}}"])
        scope, recipe, values = self.store.instantiate(saved["template_id"], {"query": "Android; $()"}, "second-bot", OWNER)
        self.assertEqual(scope["package"], APP)
        self.assertEqual(scope["max_steps"], 8)
        self.assertEqual(scope["device_id"], "second-bot")
        self.assertEqual(scope["allowed_texts"], ["Android; $()"])
        self.assertIn("Android; $()", scope["goal"])
        self.assertIsNone(recipe)
        self.assertEqual(values, {"query": "Android; $()"})
        reloaded = library.TaskLibrary(self.path)
        self.assertFalse(reloaded.invalid)
        self.assertEqual(reloaded.templates_for(OWNER)[0]["template_id"], saved["template_id"])

    def test_unapproved_run_cannot_be_saved(self):
        for status in ["planning", "waiting_approval", "failed"]:
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "approved_run_required"):
                self.store.save_from_run(run(status), "test")
        saved = self.store.save_from_run(run("waiting_approval"), "approved", approved=True)
        self.assertTrue(saved["template_id"])

    def test_parameter_scope_rejects_unknown_names_controls_overflow_and_nested_expansion(self):
        saved = self.save()
        for parameters in [{"package": "com.other.app"}, {"query": "x\ny"}, {"query": "x" * 201}, ["x"]]:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.store.instantiate(saved["template_id"], parameters, DEVICE, OWNER)
        scope, _, _ = self.store.instantiate(saved["template_id"], {"query": "{{other}}"}, DEVICE, OWNER)
        self.assertEqual(scope["allowed_texts"], ["{{other}}"])
        for params in [{"bad-name": "Kotlin"}, {"query": "not in scope"}, {"a": "Kotlin", "b": "Kotlin"}]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.store.save_from_run(run(), "name", params)

    def test_private_owner_and_requester_filters(self):
        saved = self.save()
        self.assertEqual(self.store.templates_for("other"), [])
        self.assertEqual(len(self.store.templates_for(OWNER, DEVICE)), 1)
        self.assertEqual(len(self.store.templates_for(OWNER, "personal-phone")), 1)
        self.assertEqual(self.store.templates_for(OWNER, "unrelated"), [])
        with self.assertRaisesRegex(ValueError, "template_not_found"):
            self.store.template(saved["template_id"], "other")

    def test_failed_atomic_save_preserves_prior_library(self):
        saved = self.save()
        original = self.path.read_bytes()
        with patch.object(library, "write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.store.save_from_run(run(), "second")
            with self.assertRaises(OSError): self.store.delete_template(saved["template_id"], OWNER)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.store.templates_for(OWNER)), 1)

    def test_corrupt_storage_fails_closed_without_overwriting(self):
        self.path.write_text('{"version":1,"templates":[]}', encoding="utf-8")
        corrupted = self.path.read_bytes(); store = library.TaskLibrary(self.path)
        self.assertTrue(store.invalid)
        with self.assertRaisesRegex(ValueError, "task_library_invalid"):
            store.save_from_run(run(), "name")
        self.assertEqual(self.path.read_bytes(), corrupted)

    def test_verified_recipe_requires_opt_in_and_rejects_coordinate_or_shell_data(self):
        source = run(remember_recipe=True)
        source.summary["ui_recipe"] = {"version": 1, "package": APP, "steps": [
            {"action": "input_text", "parameter_index": 0, "selector": {"resource_id": APP + ":id/input", "editable": True, "focused": True},
             "postcondition": {"selectors": [{"resource_id": APP + ":id/input", "text": "{{input:0}}"}]}}]}
        saved = self.store.save_from_run(source, "recipe", now=START)
        self.assertTrue(saved["has_recipe"])
        self.assertNotIn("recipe", saved)
        for forbidden in ["x", "bounds", "shell", "command", "snapshot_id", "node_id"]:
            broken = copy.deepcopy(source); broken.summary["ui_recipe"]["steps"][0][forbidden] = "bad"
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(ValueError, "invalid_recipe"):
                self.store.save_from_run(broken, "unsafe")
        source.summary["ui_scope"]["remember_recipe"] = False
        self.assertFalse(self.store.save_from_run(source, "no recipe")["has_recipe"])


class ScheduleTests(unittest.TestCase):
    setUp = TemplateTests.setUp
    tearDown = TemplateTests.tearDown
    def schedule(self, *, now=START, **updates):
        template = self.store.save_from_run(run(), "search", {"query": "Kotlin"}, now=START)
        payload = {"template_id": template["template_id"], "device_id": DEVICE, "parameters": {"query": "Android"},
            "daily_times": ["18:00"], "timezone": "Asia/Shanghai", "expires_at": now + 30 * 86400,
            "max_runs": 20, "max_runs_per_day": 1, "max_model_calls": 40, "per_run_model_calls": 8,
            "approved": True, "enabled": True, "notify": False, "notify_only_changed": True,
            "quiet_start": "23:00", "quiet_end": "07:00", **updates}
        return self.store.save_schedule(payload, OWNER, role="bot", role_revision=1,
                                        private_session="test:FriendMessage:owner", now=now)

    @staticmethod
    def at(hour=10, minute=0, day=13):
        return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp()

    def test_current_minute_only_and_persistent_slot_prevents_restart_replay(self):
        row = self.schedule()
        self.assertFalse(self.store.due(now=self.at(10, 1), available=lambda _: True))
        self.assertFalse(self.store.due(now=self.at(9, 59), available=lambda _: True))
        claims = self.store.due(now=self.at(), available=lambda _: True)
        self.assertEqual(len(claims), 1)
        self.assertFalse(self.store.due(now=self.at() + 30, available=lambda _: True))
        reloaded = library.TaskLibrary(self.path)
        self.assertEqual(next(iter(reloaded.claims.values()))["status"], "interrupted")
        self.assertFalse(reloaded.due(now=self.at() + 45, available=lambda _: True))
        current = reloaded.schedules_for(OWNER)[0]
        self.assertEqual(current["used_runs"], 1)
        self.assertEqual(current["model_calls_reserved"], 8)
        self.assertEqual(current["remaining_model_calls"], 32)

    def test_new_approval_does_not_fire_retroactively_in_its_current_minute(self):
        self.schedule(now=self.at() + 12)
        self.assertFalse(self.store.due(now=self.at() + 30, available=lambda _: True))

    def test_offline_personal_quiet_expired_unapproved_and_overbudget_cannot_dispatch(self):
        row = self.schedule(max_model_calls=8)
        self.assertFalse(self.store.due(now=self.at(), available=lambda _: False))
        claim = self.store.due(now=self.at(), available=lambda _: True)[0]["claim"]
        self.store.finish_claim(claim["claim_id"], status="failed", model_calls=0)
        self.assertFalse(self.store.due(now=self.at(day=14), available=lambda _: True))
        for updates in [{"approved": False}, {"expires_at": START}, {"daily_times": ["23:00"]},
                        {"per_run_model_calls": 17}, {"max_runs": True}, {"timezone": "not/a_timezone"}]:
            with self.subTest(updates=updates), self.assertRaises(ValueError): self.schedule(**updates)
        template = self.store.templates_for(OWNER)[0]
        with self.assertRaisesRegex(ValueError, "bot_device_required"):
            self.store.save_schedule({"approved": True}, OWNER, role="personal", role_revision=1, now=START)

    def test_failed_slot_persistence_reserves_nothing_and_sends_nothing(self):
        self.schedule()
        with patch.object(library, "write_json_atomic", side_effect=OSError("disk")):
            with self.assertRaises(OSError): self.store.due(now=self.at(), available=lambda _: True)
        self.assertFalse(self.store.claims)
        self.assertEqual(self.store.schedules_for(OWNER)[0]["used_runs"], 0)
        self.assertEqual(len(self.store.due(now=self.at(), available=lambda _: True)), 1)

    def test_missing_or_malformed_loaded_schedule_metadata_fails_closed(self):
        row = self.schedule()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in [("timezone", None), ("last_slots", {}), ("daily_counts", []), ("actual_model_calls", -1)]:
            broken = copy.deepcopy(saved)
            if value is None: broken["schedules"][row["schedule_id"]].pop(key)
            else: broken["schedules"][row["schedule_id"]][key] = value
            self.path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(key=key): self.assertTrue(library.TaskLibrary(self.path).invalid)

    def test_revocation_role_change_and_expiry_invalidate_live_claim(self):
        row = self.schedule()
        claim = self.store.due(now=self.at(), available=lambda _: True)[0]["claim"]
        args = (claim["claim_id"], row["revision"], DEVICE)
        self.assertIs(self.store.claim_allowed(*args, role="bot", role_revision=1, now=self.at()), True)
        self.assertEqual(self.store.claim_allowed(*args, role="personal", role_revision=1, now=self.at()), "device_changed")
        self.assertEqual(self.store.claim_allowed(*args, role="bot", role_revision=2, now=self.at()), "device_changed")
        self.store.disable_schedule(row["schedule_id"], OWNER)
        self.assertEqual(self.store.claim_allowed(*args, role="bot", role_revision=1, now=self.at()), "schedule_revoked")

    def test_changed_result_notification_is_reserved_once_and_never_on_failure(self):
        self.schedule(notify=True, max_model_calls=40)
        result = {"verified": True, "items": [{"title": "Verified video", "author": "Creator"}]}
        first = self.store.due(now=self.at(), available=lambda _: True)[0]["claim"]
        self.store.bind_claim(first["claim_id"], "run-one")
        notice = self.store.finish_claim(first["claim_id"], status="done", model_calls=3, result=result, now=self.at()+30)
        self.assertEqual(notice["session"], "test:FriendMessage:owner")
        self.assertIsNone(self.store.finish_claim(first["claim_id"], status="done", result=result))
        second = self.store.due(now=self.at(day=14), available=lambda _: True)[0]["claim"]
        self.assertIsNone(self.store.finish_claim(second["claim_id"], status="done", result=result, now=self.at(day=14)+20))
        third = self.store.due(now=self.at(day=15), available=lambda _: True)[0]["claim"]
        self.assertIsNone(self.store.finish_claim(third["claim_id"], status="failed", result=result, now=self.at(day=15)+20))
        self.assertEqual(self.store.schedules_for(OWNER)[0]["model_calls_reserved"], 24)

    def test_dst_repeated_hour_does_not_repeat_local_slot(self):
        before = datetime(2026, 10, 31, 10, tzinfo=timezone.utc).timestamp()
        row = self.schedule(now=before, daily_times=["01:30"], timezone="America/New_York", quiet_start="", quiet_end="")
        first_time = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        second_time = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).timestamp()
        first = self.store.due(now=first_time, available=lambda _: True)[0]["claim"]
        self.store.finish_claim(first["claim_id"], status="done", now=first_time+10)
        self.assertFalse(self.store.due(now=second_time, available=lambda _: True))

    def test_dst_skipped_hour_is_not_caught_up(self):
        before = datetime(2026, 3, 7, 10, tzinfo=timezone.utc).timestamp()
        self.schedule(now=before, daily_times=["02:30"], timezone="America/New_York", quiet_start="", quiet_end="")
        skipped = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc).timestamp()
        self.assertFalse(self.store.due(now=skipped, available=lambda _: True))


class ResumeTests(unittest.TestCase):
    def test_resume_is_same_scope_remaining_budget_with_no_old_actions_or_grant(self):
        source = run("failed"); source.add_step("open_app", {"package": APP}).status = "done"
        source.add_step("tap", {"package": APP}).status = "done"
        payload, metadata = library.resume_scope(source, current_device_id=DEVICE, current_role="bot")
        self.assertEqual(payload["device_id"], DEVICE)
        self.assertEqual(payload["package"], APP)
        self.assertEqual(payload["max_steps"], 6)
        self.assertFalse(metadata["resume_open_target"])
        self.assertEqual(metadata["resume_from"], source.run_id)
        self.assertNotIn("steps", payload)
        self.assertNotIn("grant_id", payload)

    def test_unknown_result_requires_manual_current_state_confirmation(self):
        source = run("waiting_approval"); source.add_step("input_text").status = "unknown"
        with self.assertRaisesRegex(ValueError, "manual_state_confirmation_required"):
            library.resume_scope(source, current_device_id=DEVICE, current_role="bot")
        payload, metadata = library.resume_scope(source, current_device_id=DEVICE, current_role="bot", manual_state_confirmed=True)
        self.assertEqual(payload["max_steps"], 7)
        self.assertTrue(metadata["resume_manual_state_confirmed"])

    def test_done_cancelled_wrong_device_and_exhausted_tasks_cannot_resume(self):
        for status in ["done", "cancelled", "executing", "planning"]:
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "task_not_resumable"):
                library.resume_scope(run(status), current_device_id=DEVICE, current_role="bot")
        source = run("failed", max_steps=1); source.add_step("tap").status = "failed"
        with self.assertRaisesRegex(ValueError, "resume_budget_exhausted"):
            library.resume_scope(source, current_device_id=DEVICE, current_role="bot")
        with self.assertRaisesRegex(ValueError, "device_changed"):
            library.resume_scope(run("failed"), current_device_id="other", current_role="bot")


if __name__ == "__main__": unittest.main()
