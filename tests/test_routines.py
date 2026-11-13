"""Real Quart routine routes with in-memory fake devices and no external sends."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quart import Quart

ROOT = Path(__file__).parents[1]
package = types.ModuleType("routines_test"); package.__path__ = [str(ROOT)]
api = types.ModuleType("astrbot.api"); api.logger = Mock()
events = types.ModuleType("astrbot.api.event")


class Chain:
    def message(self, text): self.text = text; return self


events.MessageChain = Chain
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                             "astrbot.api.event": events, "routines_test": package}):
    routines = importlib.import_module("routines_test.routines")
    tasks = importlib.import_module("routines_test.task_runner")

APP = "tv.danmaku.bili"
SUBJECT = "owner"
DEVICE = "bot-phone"


class Backend:
    def __init__(self, device):
        self.device = device
        self.status = {"received_at": time.time(), "shizuku_ready": True, "screen_interactive": True}
    def binding(self): return self.device, 1
    def registration(self): return {"device_id": self.device, "app_version": "0.5.0"}
    def last_status(self): return self.status


class Fleet:
    def __init__(self):
        self.backends = {DEVICE: Backend(DEVICE), "personal-phone": Backend("personal-phone")}
        self.roles = {DEVICE: "bot", "personal-phone": "personal"}
        self.revisions = {DEVICE: 1, "personal-phone": 1}
    def backend(self, device=None): return self.backends.get(device) if isinstance(device, str) else None
    def role(self, device): return self.roles.get(device, "unassigned")
    def revision(self, device): return self.revisions.get(device, 0)


class Host(routines.RoutinesMixin):
    def __init__(self, folder):
        self.config = {"enabled": True, "enable_task_routines": True, "enable_ui_tasks": True,
                       "enable_task_execute": True, "enable_observe_tool": True}
        self.folder = folder
        self._devices = Fleet()
        self._task_runs = tasks.TaskRunStore(folder / "runs.json")
        self._terminating = False
        self._night_guard_states = {SUBJECT: {"session": "synthetic:FriendMessage:owner"}}
        self.context = types.SimpleNamespace(send_message=AsyncMock())
        self.audit = []
        self.launched = []
        self.busy = set()
        self.principal = None
        self._init_routines()
        self.app = Quart(__name__ + str(id(self)))
        for index, (path, handler, methods, *_) in enumerate(self._routine_routes()):
            self.app.add_url_rule(path, str(index), handler, methods=methods)
    def _task_run_path(self): return self.folder / "runs.json"
    def _plugin_data_path(self, key, default): return self.folder / default
    def _enabled(self): return self.config["enabled"]
    def _feature(self, key): return self.config.get("enable_" + key, False)
    def _control_backend(self): return "app"
    def _allowed_users(self): return {SUBJECT}
    def _ui_allowed_packages(self): return {APP}
    def _ui_app_supported(self, backend): return True
    def _web_write_allowed(self): return True
    def _routine_principal(self): return self.principal
    def _device_task_busy(self, device): return device in self.busy
    def _audit(self, name, **fields): self.audit.append((name, fields))
    def _approval_public(self, row):
        run = self._task_runs.run(row["run_id"])
        return {**row, "scope": run.summary["ui_scope"]}
    def _create_ui_proposal(self, payload, subject, *, requester_device_id="", umo=""):
        if not self._enabled() or not self._feature("task_execute") or not self._feature("ui_tasks"):
            raise ValueError("ui_tasks_disabled")
        if payload.get("package") not in self._ui_allowed_packages(): raise ValueError("package_not_enabled")
        device = payload["device_id"]
        run = tasks.PhoneTaskRun.new(payload["goal"], subject=subject, device_id=device)
        run.summary.update(mode="ui", ui_scope={**payload, "device_role": self._devices.role(device)},
                           requester_device_id=requester_device_id, session_umo=umo, device_generation=1,
                           device_role_revision=self._devices.revision(device))
        run.transition("waiting_approval")
        approval = tasks.ApprovalRequest.create(run_id=run.run_id, subject=subject, device_id=device,
            actions=["open_app", "tap", "input_text", "scroll", "back"], packages=[payload["package"]])
        self._task_runs.add_run(run); self._task_runs.add_approval(approval); self._task_runs.save()
        return run, approval
    async def _launch_ui_task(self, run, approval):
        self.launched.append((run, approval))
        self.busy.add(run.device_id)
        approval.decide(True, reason="schedule_approved")
        return {"success": True}, 202


class RoutineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = Host(Path(self.temp.name))
        self.client = self.host.app.test_client()
    async def asyncTearDown(self):
        self.host._terminating = True
        await self.host._stop_routines()
        self.temp.cleanup()
    def source_run(self, status="done"):
        payload = {"goal": "搜索 Kotlin", "device_id": DEVICE, "package": APP, "allowed_texts": ["Kotlin"],
                   "success_texts": ["Kotlin", "综合"], "max_steps": 8, "completion_mode": "search_results",
                   "allow_vision": False, "remember_recipe": False}
        run, approval = self.host._create_ui_proposal(payload, SUBJECT, requester_device_id="personal-phone")
        approval.decide(True)
        run.status = status; run.summary["ui_started"] = True
        self.host._task_runs.save()
        return run
    async def save(self):
        source = self.source_run()
        response = await self.client.post("/app/routines", json={"action": "save", "run_id": source.run_id,
            "name": "搜索视频", "parameters": {"query": "Kotlin"}, "requester_device_id": "personal-phone"})
        self.assertEqual(response.status_code, 200, await response.get_data(as_text=True))
        return (await response.get_json())["template"]
    def schedule_body(self, template, **updates):
        now = time.time()
        return {"action": "schedule_save", "template_id": template["template_id"], "device_id": DEVICE,
                "parameters": {"query": "Android"}, "daily_times": ["18:00"], "timezone": "UTC",
                "expires_at": now + 86400, "approved": True, "enabled": True,
                "max_runs": 2, "max_model_calls": 16, "per_run_model_calls": 8,
                "requester_device_id": "personal-phone", **updates}

    async def test_save_and_parameterized_propose_are_reviewable_and_do_not_execute(self):
        template = await self.save()
        response = await self.client.post("/app/routines/propose", json={"template_id": template["template_id"],
            "parameters": {"query": "Android"}, "device_id": DEVICE, "requester_device_id": "personal-phone"})
        body = await response.get_json(); self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["run"]["status"], "waiting_approval")
        self.assertEqual(body["approval"]["scope"]["allowed_texts"], ["Android"])
        self.assertEqual(self.host.launched, [])
        listing = await (await self.client.get("/app/routines?device_id=personal-phone")).get_json()
        self.assertEqual(len(listing["templates"]), 1)
        self.assertFalse(listing["schedules"])
        self.assertEqual(self.host._task_library.path.parent, Path(self.temp.name))

    async def test_device_principal_cannot_spoof_requester_or_read_unrelated_library(self):
        template = await self.save()
        self.host._devices.backends["unrelated"] = Backend("unrelated")
        self.host.principal = {"subject": SUBJECT, "device_id": "unrelated"}
        listing = await self.client.get("/app/routines?device_id=personal-phone")
        self.assertEqual(listing.status_code, 403)
        response = await self.client.post("/app/routines/propose", json={"template_id": template["template_id"],
            "parameters": {}, "device_id": DEVICE, "requester_device_id": "personal-phone"})
        self.assertEqual(response.status_code, 403)

    async def test_schedule_requires_human_scope_consent_bot_and_opt_in(self):
        template = await self.save()
        for updates, error in [({"approved": False}, "schedule_approval_required"),
                               ({"device_id": "personal-phone"}, "bot_device_required")]:
            response = await self.client.post("/app/routines", json=self.schedule_body(template, **updates))
            self.assertEqual((await response.get_json())["error_code"], error)
        self.host.config["enable_task_routines"] = False
        response = await self.client.post("/app/routines", json=self.schedule_body(template))
        self.assertEqual((await response.get_json())["error_code"], "task_routines_disabled")
        self.assertEqual(self.host.launched, [])

    async def test_schedule_notice_cannot_target_arbitrary_chat(self):
        template = await self.save(); self.host._night_guard_states.clear()
        response = await self.client.post("/app/routines", json=self.schedule_body(template, notify=True,
            private_session="attacker:GroupMessage:all"))
        self.assertEqual((await response.get_json())["error_code"], "private_session_required")
        self.host.context.send_message.assert_not_called()

    async def test_resume_issues_new_approval_and_requires_unknown_action_ack(self):
        source = self.source_run("waiting_approval")
        source.add_step("input_text", {"text_length": 6}).status = "unknown"
        response = await self.client.post("/app/task/resume", json={"run_id": source.run_id})
        self.assertEqual((await response.get_json())["error_code"], "manual_state_confirmation_required")
        response = await self.client.post("/app/task/resume", json={"run_id": source.run_id, "manual_state_confirmed": True})
        body = await response.get_json(); self.assertEqual(response.status_code, 200, body)
        self.assertNotEqual(body["run"]["run_id"], source.run_id)
        self.assertFalse(body["run"]["steps"])
        self.assertEqual(body["run"]["grant_id"], "")
        self.assertEqual(body["approval"]["scope"]["max_steps"], 7)
        self.assertFalse(body["run"]["summary"]["resume_open_target"])
        self.assertFalse(self.host.launched)
        response = await self.client.post("/app/task/resume", json={"run_id": source.run_id, "manual_state_confirmed": True})
        self.assertEqual((await response.get_json())["error_code"], "resume_already_created")

    async def test_resume_rejects_cross_device_current_role_and_busy_device(self):
        source = self.source_run("failed")
        self.host.busy.add(DEVICE)
        response = await self.client.post("/app/task/resume", json={"run_id": source.run_id})
        self.assertEqual((await response.get_json())["error_code"], "device_busy")
        self.host.busy.clear(); self.host._devices.roles[DEVICE] = "unassigned"
        response = await self.client.post("/app/task/resume", json={"run_id": source.run_id})
        self.assertEqual((await response.get_json())["error_code"], "device_changed")

    async def test_scheduler_reserves_before_launch_and_revocation_stops_live_run(self):
        template = await self.save(); now = time.time(); minute = int(now // 60) * 60
        local_minute = time.strftime("%H:%M", time.gmtime(minute))
        row = self.host._task_library.save_schedule(self.schedule_body(template, daily_times=[local_minute],
            quiet_start="", quiet_end="", expires_at=now + 86400), SUBJECT,
            role="bot", role_revision=1, now=minute-60)
        self.host._devices.backend(DEVICE).status["received_at"] = minute
        await self.host._routine_tick(now=minute+2)
        self.assertEqual(len(self.host.launched), 1)
        run, _ = self.host.launched[0]
        claim = self.host._task_library.claims[run.summary["routine_claim_id"]]
        self.assertEqual(claim["status"], "started")
        self.assertEqual(run.summary["routine_model_limit"], 8)
        self.assertEqual(self.host._task_library.schedules[row["schedule_id"]]["model_calls_reserved"], 8)
        self.assertIs(self.host._routine_run_allowed(run), True)
        self.host._task_library.disable_schedule(row["schedule_id"], SUBJECT)
        self.assertEqual(self.host._routine_run_allowed(run), "schedule_revoked")
        self.host.context.send_message.assert_not_called()

    async def test_schedule_will_not_operate_sleeping_stale_or_busy_phone(self):
        template = await self.save(); now = time.time(); minute = int(now // 60) * 60
        self.host._task_library.save_schedule(self.schedule_body(template,
            daily_times=[time.strftime("%H:%M", time.gmtime(minute))], quiet_start="", quiet_end=""),
            SUBJECT, role="bot", role_revision=1, now=minute-60)
        backend = self.host._devices.backend(DEVICE)
        backend.status["screen_interactive"] = False
        await self.host._routine_tick(now=minute+2)
        backend.status["screen_interactive"] = True; backend.status["received_at"] = minute-200
        await self.host._routine_tick(now=minute+2)
        backend.status["received_at"] = minute; self.host.busy.add(DEVICE)
        await self.host._routine_tick(now=minute+2)
        self.assertFalse(self.host.launched)
        self.assertFalse(self.host._task_library.claims)

    async def test_deleting_template_revokes_schedule_and_disable_is_allowed_when_feature_off(self):
        template = await self.save()
        row = self.host._task_library.save_schedule(self.schedule_body(template), SUBJECT, role="bot", role_revision=1)
        self.host.config["enabled"] = False
        response = await self.client.post("/app/routines", json={"action": "schedule_enable", "schedule_id": row["schedule_id"], "enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.host._task_library.schedules[row["schedule_id"]]["enabled"])
        response = await self.client.post("/app/routines", json={"action": "delete", "template_id": template["template_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.host._task_library.schedules)

    async def test_large_and_invalid_json_have_no_side_effect(self):
        response = await self.client.post("/app/routines", data=b"x" * 33000, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        response = await self.client.post("/app/routines", data='{"action":NaN}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.host._task_library.templates)

    async def test_malformed_action_and_ids_are_rejected_without_500(self):
        for payload in [{"action": []}, {"action": {}}, {"action": "delete", "template_id": []},
                        {"action": "schedule_delete", "schedule_id": []}]:
            with self.subTest(payload=payload):
                response = await self.client.post("/app/routines", json=payload)
                self.assertEqual(response.status_code, 400)

    async def test_schedule_completion_requires_explicit_notice_and_durable_outcome(self):
        template = await self.save(); now = time.time(); minute = int(now // 60) * 60
        row = self.host._task_library.save_schedule(self.schedule_body(template, notify=True,
            daily_times=[time.strftime("%H:%M", time.gmtime(minute))], quiet_start="", quiet_end=""),
            SUBJECT, role="bot", role_revision=1, private_session="synthetic:FriendMessage:owner", now=minute-60)
        self.host._devices.backend(DEVICE).status["received_at"] = minute
        await self.host._routine_tick(now=minute+2)
        run, _ = self.host.launched[0]
        run.status = "done"; run.summary.update(model_calls=2,
            ui_results={"verified": True, "items": [{"title": "Synthetic result", "author": "Author"}]})
        with patch.object(self.host._task_library, "_commit", side_effect=OSError("disk")):
            await self.host._routine_run_finished(run)
        self.host.context.send_message.assert_not_called()
        await self.host._routine_run_finished(run)
        self.host.context.send_message.assert_awaited_once()
        session, chain = self.host.context.send_message.await_args.args
        self.assertEqual(session, "synthetic:FriendMessage:owner")
        self.assertIn("Synthetic result", chain.text)
        await self.host._routine_run_finished(run)
        self.host.context.send_message.assert_awaited_once()


if __name__ == "__main__": unittest.main()
