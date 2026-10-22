"""Night guard state machine: deterministic observe -> warn -> enforce -> release."""
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
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class Chain:
    def message(self, text):
        self.text = text
        return self


event_api.filter = _NoOpFilter()
event_api.MessageChain = Chain
star_api = types.ModuleType("astrbot.api.star")


class Star:
    def __init__(self, context):
        self.context = context


star_api.Star = Star
package = types.ModuleType("phone_agent_night_guard_test")
package.__path__ = [str(ROOT)]
spec = importlib.util.spec_from_file_location("phone_agent_night_guard_test.main", ROOT / "main.py")
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                             "astrbot.api.event": event_api, "astrbot.api.star": star_api,
                             "phone_agent_night_guard_test": package}):
    spec.loader.exec_module(module)

VIDEO = "com.example.video"
OTHER = "com.example.other"
USER = "u-10001"
PRIVATE_SESSION = "aiocqhttp:FriendMessage:" + USER


def freeze_window(plugin, start="00:30", end="08:00", now=None):
    plugin.config["night_guard_start"] = start
    plugin.config["night_guard_end"] = end
    return now


class NightGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "enabled": True,
            "control_backend": "app",
            "enable_policy_tools": True,
            "enable_night_guard": True,
            "night_guard_dry_run": False,
            "night_guard_start": "00:30",
            "night_guard_end": "08:00",
            "night_guard_check_interval_seconds": 60,
            "night_guard_status_max_age_seconds": 90,
            "night_guard_grace_minutes": 10,
            "night_guard_suspend_minutes": 30,
            "night_guard_consecutive_hits": 2,
            "night_guard_max_enforcements_per_night": 2,
            "night_guard_warning_cooldown_minutes": 60,
            "night_guard_packages": "testvid," + VIDEO,
            "night_guard_exempt_packages": "",
            "night_guard_private_only": True,
            "allowed_user_ids": USER,
            "app_aliases_json": json.dumps({"testvid": VIDEO}),
        }
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path",
                    "policy_state_path", "notification_path", "night_guard_state_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        self.app = None
        self.send_message = AsyncMock(return_value=True)
        self.create_plugin()
        # Default: inside the night window (02:00 local) for every test.
        self.set_clock(datetime.now().replace(hour=2, minute=0, second=0, microsecond=0))

    def create_plugin(self):
        context = types.SimpleNamespace(send_message=self.send_message)
        context.register_web_api = lambda *args, **kwargs: None
        self.plugin = module.PhoneAgentPlugin(context, self.config)
        # The background loop is covered by tick-level tests; keep it quiet.
        task = self.plugin._night_guard_task
        self.plugin._night_guard_task = None
        if task is not None:
            task.cancel()
            self.loop_tear.append(task) if False else None
            self._pending_task = task

    async def asyncTearDown(self):
        task = getattr(self, "_pending_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.plugin.terminate()
        self.temp.cleanup()

    # ---------- helpers ----------

    def set_clock(self, when):
        self.now = when
        patcher = patch.object(module, "datetime")
        mock_dt = patcher.start()
        mock_dt.now = lambda: self.now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        self.addCleanup(patcher.stop)

    def heartbeats(self, *observations):
        """Feed observations in order; the last one repeats for further observe calls."""
        seq = list(observations)
        side_effects = []
        for item in seq:
            if item is None:
                side_effects.append(None)
            else:
                side_effects.append({
                    "fresh": True,
                    "foreground_package": item,
                    "shizuku_ready": True,
                    "screen_interactive": True,
                })
        async def fake_observe():
            if not side_effects:
                return {"fresh": False, "error": "no more observations"}
            if len(side_effects) == 1:
                current = side_effects[0]
            else:
                current = side_effects.pop(0)
            return current
        return fake_observe

    def user_state(self, user_id=USER):
        return self.plugin._night_guard_states.setdefault(user_id, {
            "session": PRIVATE_SESSION, "phase": module.NG_PHASE_WATCHING,
            "foreground_package": "", "consecutive_hits": 0, "owned_suspended": [],
            "enforcement_count": 0, "night_id": "",
            "first_seen_at": None, "last_observed_at": None,
            "warning_sent_at": None, "warning_deadline": None,
            "enforced_until": None, "released_until": None,
            "warned_date": "", "last_error": "",
        })

    async def tick(self):
        await self.plugin._night_guard_tick()

    def audit_operations(self):
        records = []
        path = Path(self.config["audit_log_path"])
        if not path.exists():
            return records
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return records

    # ---------- window ----------

    def test_window_normal_and_crossing_midnight(self):
        plugin = self.plugin
        freeze_window(plugin, "01:00", "07:00")
        self.assertFalse(plugin._night_guard_in_window(datetime(2026, 1, 1, 0, 30)))
        self.assertTrue(plugin._night_guard_in_window(datetime(2026, 1, 1, 1, 0)))
        self.assertFalse(plugin._night_guard_in_window(datetime(2026, 1, 1, 7, 0)))
        freeze_window(plugin, "23:30", "06:00")
        self.assertTrue(plugin._night_guard_in_window(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(plugin._night_guard_in_window(datetime(2026, 1, 1, 2, 0)))
        self.assertFalse(plugin._night_guard_in_window(datetime(2026, 1, 1, 12, 0)))
        self.assertFalse(plugin._night_guard_in_window(datetime(2026, 1, 1, 6, 0)))
        self.assertEqual(plugin._night_guard_minutes_to_end(datetime(2026, 1, 1, 23, 30)), 390)
        self.assertEqual(plugin._night_guard_minutes_to_end(datetime(2026, 1, 1, 2, 0)), 240)

    async def test_disabled_switch_does_nothing(self):
        self.config["enable_night_guard"] = False
        plugin2 = await self.make_second_plugin()
        self.assertIsNone(plugin2._night_guard_task)
        state = self.user_state()
        with patch.object(self.plugin, "_night_guard_observe") as observe:
            await self.tick()
        observe.assert_not_called()
        self.assertEqual(self.send_message.await_count, 0)

        # Empty package list disables enforcement even with the switch on.
        self.config["night_guard_packages"] = ""
        state["consecutive_hits"] = 5
        with patch.object(self.plugin, "_night_guard_observe") as observe:
            await self.tick()
        observe.assert_not_called()

    async def make_second_plugin(self):
        context = types.SimpleNamespace(send_message=AsyncMock())
        plugin2 = module.PhoneAgentPlugin(context, self.config)
        task = plugin2._night_guard_task
        plugin2._night_guard_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(plugin2.terminate)
        return plugin2

    async def test_dry_run_never_suspends(self):
        self.config["night_guard_dry_run"] = True
        state = self.user_state()
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO, VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=AsyncMock()) as execute:
            await self.tick()
            await self.tick()
            self.assertEqual(state["phase"], module.NG_PHASE_WARNED)
            state["warning_deadline"] = time.time() - 1
            await self.tick()
            await self.tick()
        execute.assert_not_called()
        self.assertEqual(state["owned_suspended"], [])
        self.assertTrue(any(r["operation"] == "night_guard_dry_run_suspend_skipped"
                            for r in self.audit_operations()))

    async def test_consecutive_hits_and_package_change_resets_count(self):
        state = self.user_state()
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO)), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock(return_value=True)) as warn:
            await self.tick()
            self.assertEqual(state["consecutive_hits"], 1)
            self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
            warn.assert_not_awaited()
            await self.tick()
            warn.assert_awaited_once()
            self.assertEqual(state["consecutive_hits"], 2)
        # A different package resets the streak.
        state["phase"] = module.NG_PHASE_WATCHING
        state["consecutive_hits"] = 0
        state["warning_sent_at"] = None
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, OTHER)), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock(return_value=True)) as warn:
            await self.tick()
            await self.tick()
        self.assertEqual(state["consecutive_hits"], 0)
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)

    async def test_warning_skipped_without_private_session(self):
        state = self.user_state()
        state["session"] = "aiocqhttp:GroupMessage:12345"
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=AsyncMock()) as execute:
            await self.tick()
            await self.tick()
        execute.assert_not_called()
        self.send_message.assert_not_awaited()
        self.assertIn("private chat session", state["last_error"])
    async def test_stale_status_never_triggers_anything(self):
        state = self.user_state()
        self.plugin.device_app.record_device_status(
            {"foreground_package": VIDEO, "shizuku_ready": True, "reported_at": time.time() - 600})
        with patch.object(self.plugin, "_night_guard_observe",
                          new=AsyncMock(return_value={"fresh": False, "error": "stale status"})):
            await self.tick()
            await self.tick()
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
        self.assertEqual(state["consecutive_hits"], 0)
        self.send_message.assert_not_awaited()

    async def test_warning_sent_once_and_enforced_after_grace(self):
        state = self.user_state()
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO, VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()  # hit 1
            await self.tick()  # hit 2 -> warning
            self.assertEqual(state["phase"], module.NG_PHASE_WARNED)
            self.send_message.assert_awaited_once()
            self.assertIn("刷", self.send_message.await_args.args[1].text)
            # Grace not reached yet: no suspend.
            await self.tick()
            execute.assert_not_called()
            state["warning_deadline"] = time.time() - 1
            await self.tick()  # grace passed + re-check same package -> enforce
            execute.assert_awaited_once()
            args = execute.await_args.args
            self.assertEqual((args[0], args[1]), (VIDEO, "disable"))
            self.assertEqual(state["phase"], module.NG_PHASE_ENFORCED)
            self.assertEqual(state["owned_suspended"], [VIDEO])

    async def test_enforcement_requires_verified_exact_package(self):
        state = self.user_state()
        execute = AsyncMock(return_value={"success": True, "verified": False, "package": VIDEO, "error": "unverified"})
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO, VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
            await self.tick()
            state["warning_deadline"] = time.time() - 1
            await self.tick()
            self.assertNotEqual(state["phase"], module.NG_PHASE_ENFORCED)
            self.assertEqual(state["owned_suspended"], [])
            self.assertIn("unverified", state["last_error"])

    async def test_leaving_app_during_grace_cancels_suspension(self):
        state = self.user_state()
        execute = AsyncMock()
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO, "com.android.launcher")), \
                patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
            await self.tick()
            state["warning_deadline"] = time.time() - 1
            await self.tick()
        execute.assert_not_called()
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)

    async def test_suspend_minutes_clamped_to_window_end(self):
        state = self.user_state()
        self.set_clock(datetime.now().replace(hour=7, minute=45))
        self.config["night_guard_grace_minutes"] = 1
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
            await self.tick()
            state["warning_deadline"] = time.time() - 1
            await self.tick()
        minutes = execute.await_args.args[2]
        self.assertLessEqual(minutes, 15)
        execute.assert_awaited_once()

    async def test_auto_restore_only_owned_packages(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        state["enforced_until"] = time.time() - 1
        # A manual policy from another source must survive the night guard.
        self.plugin._policy_leases[OTHER] = {
            "manual": module._PolicyLease(source="manual")
        }
        self.plugin._guard_suspended.add(OTHER)
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
        restored = [call.args[0] for call in execute.await_args_list]
        self.assertEqual(restored, [VIDEO])
        self.assertEqual(state["owned_suspended"], [])
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
        self.assertIn(OTHER, self.plugin._guard_suspended)

    async def test_window_end_restores_owned(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        self.set_clock(self.now.replace(hour=12))
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
        execute.assert_awaited_once_with(VIDEO, "enable", source="night_guard")
        self.assertEqual(state["owned_suspended"], [])
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)

    async def test_user_release_restores_only_owned_and_cools_down(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        self.plugin._guard_suspended.add(OTHER)
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            result = await self.plugin._night_guard_release(USER)
        self.assertTrue(result["success"])
        execute.assert_awaited_once_with(VIDEO, "enable", source="night_guard")
        self.assertEqual(state["phase"], module.NG_PHASE_RELEASED)
        self.assertGreater(state["released_until"], time.time())
        # During the cooldown no new warning fires.
        state["released_until"] = time.time() + 3600
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO)), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock()) as warn:
            await self.tick()
            await self.tick()
        warn.assert_not_awaited()

    async def test_release_during_warning_phase_without_owned(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_WARNED
        state["warning_sent_at"] = time.time()
        state["warning_deadline"] = time.time() + 600
        with patch.object(self.plugin, "_night_guard_observe", new=AsyncMock()) as observe:
            result = await self.plugin._night_guard_release(USER)
        self.assertTrue(result["success"])
        self.assertEqual(state["phase"], module.NG_PHASE_RELEASED)

    async def test_release_rejected_when_nothing_owned(self):
        self.user_state()
        result = await self.plugin._night_guard_release(USER)
        self.assertFalse(result["success"])

    async def test_max_enforcements_per_night(self):
        state = self.user_state()
        state["night_id"] = self.plugin._night_guard_night_id()
        state["enforcement_count"] = 2
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO)), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock()) as warn:
            await self.tick()
            await self.tick()
        warn.assert_not_awaited()

    async def test_state_persists_and_recovers_across_restart(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        state["enforced_until"] = time.time() + 600
        self.plugin._save_night_guard_state()
        plugin2 = await self.make_second_plugin()
        restored = plugin2._night_guard_states.get(USER)
        self.assertEqual(restored["phase"], module.NG_PHASE_ENFORCED)
        self.assertEqual(restored["owned_suspended"], [VIDEO])
        # Expired enforcement restores on the first tick, not before.
        restored["enforced_until"] = time.time() - 1
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(plugin2, "_execute_app_policy", new=execute):
            await plugin2._night_guard_tick()
        execute.assert_awaited_once_with(VIDEO, "enable", source="night_guard")

    async def test_state_file_holds_no_tokens_or_screen_text(self):
        state = self.user_state()
        state["foreground_package"] = VIDEO
        self.plugin._save_night_guard_state()
        raw = Path(self.config["night_guard_state_path"]).read_text(encoding="utf-8")
        for banned in ("Bearer", "shared_token", "operit_token", "屏幕", "password"):
            self.assertNotIn(banned, raw)
        data = json.loads(raw)
        self.assertEqual(data["version"], 1)
        self.assertIn("session", data["users"][USER])

    async def test_warning_send_failure_keeps_recoverable_state(self):
        state = self.user_state()
        self.send_message.side_effect = RuntimeError("platform down")
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO)), \
                patch.object(self.plugin, "_execute_app_policy", new=AsyncMock()) as execute:
            await self.tick()
            await self.tick()
        execute.assert_not_called()
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
        self.assertIn("platform down", state["last_error"])

    async def test_exempt_and_protected_packages_never_touched(self):
        self.config["night_guard_exempt_packages"] = VIDEO
        state = self.user_state()
        with patch.object(self.plugin, "_night_guard_observe", new=self.heartbeats(VIDEO, VIDEO)), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock()) as warn:
            await self.tick()
            await self.tick()
        warn.assert_not_awaited()
        self.config["night_guard_exempt_packages"] = ""
        self.config["night_guard_packages"] = "com.android.settings,com.tauru.astrbotphoneagent"
        self.assertEqual(self.plugin._night_guard_packages(), set())

    async def test_private_message_release_hook(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        event = types.SimpleNamespace(
            unified_msg_origin=PRIVATE_SESSION,
            get_sender_id=lambda: USER,
            get_messages=lambda: [types.SimpleNamespace(text="解开")],
            message_obj=types.SimpleNamespace(),
            send=AsyncMock(return_value=True),
        )
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.plugin.on_private_message(event)
        execute.assert_awaited_once_with(VIDEO, "enable", source="night_guard")
        self.assertEqual(state["phase"], module.NG_PHASE_RELEASED)
        self.assertTrue(event.send.await_count >= 1)

    async def test_private_message_hook_records_session_but_ignores_normal_chat(self):
        state = self.user_state()
        state["session"] = ""
        event = types.SimpleNamespace(
            unified_msg_origin=PRIVATE_SESSION,
            get_sender_id=lambda: USER,
            get_messages=lambda: [types.SimpleNamespace(text="今天天气不错")],
            message_obj=types.SimpleNamespace(),
            send=AsyncMock(),
        )
        await self.plugin.on_private_message(event)
        self.assertEqual(state["session"], PRIVATE_SESSION)
        event.send.assert_not_awaited()
        # A group session is never recorded when private_only is on.
        group_event = types.SimpleNamespace(
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            get_sender_id=lambda: USER,
            get_messages=lambda: [types.SimpleNamespace(text="解除限制")],
            message_obj=types.SimpleNamespace(),
            send=AsyncMock(),
        )
        await self.plugin.on_private_message(group_event)
        group_event.send.assert_not_awaited()
        self.assertEqual(state["session"], PRIVATE_SESSION)

    async def test_unauthorized_user_cannot_release(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        event = types.SimpleNamespace(
            unified_msg_origin=PRIVATE_SESSION,
            get_sender_id=lambda: "u-stranger",
            get_messages=lambda: [types.SimpleNamespace(text="解除限制")],
            message_obj=types.SimpleNamespace(),
            send=AsyncMock(),
        )
        with patch.object(self.plugin, "_execute_app_policy", new=AsyncMock()) as execute:
            await self.plugin.on_private_message(event)
        execute.assert_not_called()

    async def test_heartbeat_updates_status_cache(self):
        """The heartbeat handler validates device identity and updates the cache.

        The Quart request-proxy binding is covered by test_app_api; here we
        exercise the handler's validation and cache logic directly.
        """
        plugin = self.plugin
        plugin.device_app.register({"device_id": "phone-x", "base_url": "http://phone:8260"})

        class FakeRequest:
            def __init__(self, payload):
                self._payload = payload
                self.headers = {}

            async def get_json(self, silent=True):
                return self._payload

        def fake_jsonify(data, *args, **kwargs):
            return {"data": data, "status": kwargs.get("status", 200)}

        status_payload = {
            "device_id": "phone-x",
            "app_version": "0.3.0",
            "battery_percent": 42,
            "charging": False,
            "foreground_package": VIDEO,
            "screen_interactive": True,
            "shizuku_ready": True,
            "reported_at": time.time(),
        }
        with patch.object(module, "request", FakeRequest(status_payload)), \
                patch.object(module, "jsonify", fake_jsonify):
            result = await plugin._web_device_status_post()
        self.assertTrue(result["data"]["success"])
        status = plugin.device_app.last_status()
        self.assertEqual(status["foreground_package"], VIDEO)
        self.assertIs(status["screen_interactive"], True)
        self.assertEqual(status["battery_percent"], 42)

        # Mismatched device id is rejected.
        with patch.object(module, "request", FakeRequest({"device_id": "other"})), \
                patch.object(module, "jsonify", fake_jsonify):
            result = await plugin._web_device_status_post()
        self.assertEqual(result[1], 400)

        # Bad package name is sanitized to empty.
        with patch.object(module, "request", FakeRequest({
            "device_id": "phone-x", "foreground_package": "not a package!", "reported_at": time.time()})), \
                patch.object(module, "jsonify", fake_jsonify):
            await plugin._web_device_status_post()
        self.assertEqual(plugin.device_app.last_status()["foreground_package"], "")

        # Unregistered device is rejected.
        plugin.device_app._registration = None
        with patch.object(module, "request", FakeRequest({"device_id": "phone-x"})), \
                patch.object(module, "jsonify", fake_jsonify):
            result = await plugin._web_device_status_post()
        self.assertEqual(result[1], 400)

    async def test_observe_uses_heartbeat_then_probes_when_stale(self):
        self.plugin.device_app.record_device_status(
            {"foreground_package": VIDEO, "shizuku_ready": True, "reported_at": time.time()})
        observation = await self.plugin._night_guard_observe()
        self.assertTrue(observation["fresh"])
        self.assertEqual(observation["foreground_package"], VIDEO)
        # Stale heartbeat (no update for 10 minutes): falls back to one direct probe.
        self.plugin.device_app.record_device_status(
            {"foreground_package": VIDEO, "shizuku_ready": True, "reported_at": time.time()})
        with self.plugin.device_app._lock:
            self.plugin.device_app._last_status["reported_at"] = time.time() - 600
        probe = {"success": True, "output": {"foreground_package": VIDEO, "shizuku_ready": True}}
        with patch.object(self.plugin.device_app, "execute", return_value=probe) as run:
            observation = await self.plugin._night_guard_observe()
        self.assertTrue(observation["fresh"])
        run.assert_called_once_with("status", {})
        # B04: a phone clock far off yields reported_at=None (not back-filled
        # to server time). The sample is NOT fresh — observe must probe.
        self.plugin.device_app.record_device_status(
            {"foreground_package": VIDEO, "reported_at": time.time() + 3600})
        status = self.plugin.device_app.last_status()
        self.assertIsNone(status["reported_at"])
        self.assertIsNotNone(status["received_at"])

    async def test_operit_backend_never_enforces(self):
        self.config["control_backend"] = "operit"
        state = self.user_state()
        with patch.object(self.plugin, "_night_guard_observe") as observe:
            observe.return_value = {"fresh": False, "error": "night guard requires control_backend=app"}
            await self.tick()
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
        self.send_message.assert_not_awaited()


class CodexReviewFixTests(NightGuardTests):
    """Regression tests for the B01-B10 fixes from the Codex review."""

    async def test_b01_empty_allowlist_never_records_session(self):
        self.config["allowed_user_ids"] = ""
        state = self.user_state()
        state["session"] = ""
        event = types.SimpleNamespace(
            unified_msg_origin=PRIVATE_SESSION,
            get_sender_id=lambda: USER,
            get_messages=lambda: [types.SimpleNamespace(text="hi")],
            message_obj=types.SimpleNamespace(),
            send=AsyncMock(),
        )
        await self.plugin.on_private_message(event)
        self.assertEqual(state["session"], "")

    async def test_b01_removed_user_skipped_in_tick(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        self.config["allowed_user_ids"] = "someone-else"
        execute = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
        execute.assert_awaited_once_with(VIDEO, "enable", source="night_guard")

    async def test_b05_screen_off_does_not_warn(self):
        state = self.user_state()
        obs = {"fresh": True, "foreground_package": VIDEO,
               "shizuku_ready": True, "screen_interactive": False}

        async def fake_observe():
            return obs

        with patch.object(self.plugin, "_night_guard_observe", new=fake_observe), \
                patch.object(self.plugin, "_night_guard_send_warning", new=AsyncMock()) as warn:
            await self.tick()
            await self.tick()
        warn.assert_not_awaited()
        self.assertEqual(state["consecutive_hits"], 0)

    async def test_b05_shizuku_unavailable_skips_enforce(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_WARNED
        state["foreground_package"] = VIDEO
        state["warning_sent_at"] = time.time() - 600
        state["warning_deadline"] = time.time() - 1
        obs = {"fresh": True, "foreground_package": VIDEO,
               "shizuku_ready": False, "screen_interactive": True}

        async def fake_observe():
            return obs

        execute = AsyncMock()
        with patch.object(self.plugin, "_night_guard_observe", new=fake_observe), \
                patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
        execute.assert_not_awaited()

    async def test_b06_package_switch_in_warned_returns_to_watching(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_WARNED
        state["foreground_package"] = VIDEO
        state["warning_sent_at"] = time.time()
        state["warning_deadline"] = time.time() + 600
        other_target = "com.example.other_target"
        self.config["night_guard_packages"] = VIDEO + "," + other_target
        with patch.object(self.plugin, "_night_guard_observe",
                          new=self.heartbeats(other_target)), \
                patch.object(self.plugin, "_execute_app_policy", new=AsyncMock()) as execute:
            await self.tick()
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)
        self.assertEqual(state["foreground_package"], other_target)
        self.assertEqual(state["consecutive_hits"], 1)
        execute.assert_not_awaited()

    async def test_b06_warned_without_deadline_repaired_on_load(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_WARNED
        state["warning_deadline"] = None
        self.plugin._save_night_guard_state()
        plugin2 = await self.make_second_plugin()
        loaded = plugin2._night_guard_states.get(USER)
        self.assertEqual(loaded["phase"], module.NG_PHASE_WATCHING)

    async def test_b06_night_id_survives_midnight(self):
        self.set_clock(datetime.now().replace(hour=0, minute=30))
        self.config["night_guard_start"] = "23:00"
        self.config["night_guard_end"] = "06:00"
        night_id = self.plugin._night_guard_night_id()
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        self.assertEqual(night_id, yesterday)

    async def test_b03_restore_failure_keeps_enforced_and_retries(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        state["enforced_until"] = time.time() - 1
        execute = AsyncMock(return_value={"success": False, "verified": False, "error": "device offline"})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            await self.tick()
        self.assertEqual(state["phase"], module.NG_PHASE_ENFORCED)
        self.assertEqual(state["owned_suspended"], [VIDEO])
        self.assertIn("pending", state.get("last_error", ""))

    async def test_b03_release_failure_does_not_claim_success(self):
        state = self.user_state()
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        execute = AsyncMock(return_value={"success": False, "error": "device offline"})
        with patch.object(self.plugin, "_execute_app_policy", new=execute):
            result = await self.plugin._night_guard_release(USER)
        self.assertFalse(result["success"])
        self.assertIn("待重试", result.get("error", ""))
        self.assertEqual(state["phase"], module.NG_PHASE_ENFORCED)

    async def test_b02_manual_owner_blocks_guard_restore(self):
        state = self.user_state()
        self.plugin._policy_leases[VIDEO] = {
            "manual": module._PolicyLease(source="manual")
        }
        self.plugin._guard_suspended.add(VIDEO)
        state["phase"] = module.NG_PHASE_ENFORCED
        state["owned_suspended"] = [VIDEO]
        state["enforced_until"] = time.time() - 1
        # Exercise the real lease logic: only the device layer is mocked, so a
        # still_suspended result has to come from production code.
        device = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        with patch.object(self.plugin, "_apply_app_policy", new=device):
            await self.tick()
        device.assert_not_awaited()
        # Manual owner survives the guard's restore; the package is NOT restored.
        self.assertIn("manual", self.plugin._policy_leases.get(VIDEO, {}))
        self.assertEqual(state["owned_suspended"], [])
        self.assertEqual(state["phase"], module.NG_PHASE_WATCHING)

    async def test_b07_verified_uses_device_output_not_success(self):
        fake_result = {
            "success": True,
            "output": {"package": VIDEO, "state": "suspended", "verified": False},
        }
        with patch.object(self.plugin.device_app, "execute", return_value=fake_result):
            result = await self.plugin._apply_app_policy(VIDEO, "disable")
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])

    async def test_b07_verified_mismatched_package_rejected(self):
        fake_result = {
            "success": True,
            "output": {"package": "com.wrong.pkg", "state": "suspended", "verified": True},
        }
        with patch.object(self.plugin.device_app, "execute", return_value=fake_result):
            result = await self.plugin._apply_app_policy(VIDEO, "disable")
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])

    async def test_b04_missing_reported_at_is_stale(self):
        self.plugin.device_app.record_device_status(
            {"foreground_package": VIDEO, "shizuku_ready": True})
        status = self.plugin.device_app.last_status()
        self.assertIsNone(status["reported_at"])
        probe = {"success": True, "output": {"foreground_package": VIDEO, "shizuku_ready": True}}
        with patch.object(self.plugin.device_app, "execute", return_value=probe) as run:
            await self.plugin._night_guard_observe()
        run.assert_called_once_with("status", {})


class PolicyLeaseTests(unittest.IsolatedAsyncioTestCase):
    """P1-A: per-source leases, persistence, migration and honest restore.

    Deliberately does not inherit NightGuardTests: these cases drive the lease
    code directly, and inheriting would re-run every night guard case that
    already has its own coverage.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "enabled": True,
            "control_backend": "app",
            "enable_policy_tools": True,
            "enable_night_guard": False,
            "allowed_user_ids": USER,
        }
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path",
                    "policy_state_path", "notification_path", "night_guard_state_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        # Only the device layer is mocked: lease bookkeeping, timers and
        # persistence all run through production code.
        self.device = AsyncMock(return_value={"success": True, "verified": True, "package": VIDEO})
        self.plugin = self.make_plugin()

    def make_plugin(self):
        context = types.SimpleNamespace(send_message=AsyncMock())
        context.register_web_api = lambda *args, **kwargs: None
        plugin = module.PhoneAgentPlugin(context, self.config)
        task = plugin._night_guard_task
        plugin._night_guard_task = None
        if task is not None:
            task.cancel()
        plugin._apply_app_policy = self.device
        self.addAsyncCleanup(plugin.terminate)
        return plugin

    async def asyncTearDown(self):
        self.temp.cleanup()

    def leases(self, package=VIDEO):
        return self.plugin._policy_leases.get(package, {})

    async def test_two_sources_keep_independent_deadlines(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 30, source="manual")
        manual_lease = self.leases()["manual"]
        manual_task = manual_lease.task
        self.assertIsNotNone(manual_task)

        await self.plugin._execute_app_policy(VIDEO, "disable", 5, source="night_guard")
        # Scheduling the second source must not cancel the first source's timer.
        self.assertEqual(set(self.leases()), {"manual", "night_guard"})
        self.assertIs(self.leases()["manual"], manual_lease)
        self.assertFalse(manual_task.done())
        self.assertLess(self.leases()["night_guard"].until, manual_lease.until)

        # Releasing the guard's lease leaves the manual lock and timer alone.
        self.device.reset_mock()
        result = await self.plugin._execute_app_policy(VIDEO, "enable", source="night_guard")
        self.assertEqual(result["mode"], "still_suspended")
        self.assertEqual(result["remaining_owners"], ["manual"])
        self.device.assert_not_awaited()
        self.assertEqual(set(self.leases()), {"manual"})
        self.assertFalse(manual_task.done())

        # Releasing the last owner actually restores the app.
        result = await self.plugin._execute_app_policy(VIDEO, "enable", source="manual")
        self.assertEqual(result["mode"], "enabled")
        self.device.assert_awaited_once_with(VIDEO, "enable")
        self.assertEqual(self.leases(), {})
        self.assertNotIn(VIDEO, self.plugin._guard_suspended)

    async def test_manual_stop_does_not_cancel_guard_timer(self):
        """v4 §6: a manual stop must not cancel the night guard's countdown."""
        await self.plugin._execute_app_policy(VIDEO, "disable", 30, source="night_guard")
        guard_lease = self.leases()["night_guard"]
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        self.device.reset_mock()
        outcome = await self.plugin._restore_guard_apps(source="manual")
        self.assertEqual(outcome["still_suspended"], [VIDEO])
        self.device.assert_not_awaited()
        self.assertIs(self.leases()["night_guard"], guard_lease)
        self.assertFalse(guard_lease.task.done())

    async def test_restart_restores_owners_and_deadlines(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        await self.plugin._execute_app_policy(VIDEO, "disable", 30, source="night_guard")
        until = self.leases()["night_guard"].until
        self.plugin._save_policy_state()

        plugin2 = self.make_plugin()
        leases = plugin2._policy_leases.get(VIDEO, {})
        self.assertEqual(set(leases), {"manual", "night_guard"})
        self.assertIsNone(leases["manual"].until)
        self.assertAlmostEqual(leases["night_guard"].until, until, places=3)
        self.assertIsNotNone(leases["night_guard"].task)
        self.assertIsNone(leases["manual"].task)
        self.assertIn(VIDEO, plugin2._guard_suspended)

        # The restored manual lock still blocks the guard's release.
        self.device.reset_mock()
        result = await plugin2._execute_app_policy(VIDEO, "enable", source="night_guard")
        self.assertEqual(result["mode"], "still_suspended")
        self.device.assert_not_awaited()
        self.assertIn("manual", plugin2._policy_leases.get(VIDEO, {}))

    async def test_legacy_file_migrates_to_manual_owner(self):
        path = Path(self.config["policy_state_path"])
        path.write_text(json.dumps({VIDEO: {"until": None}}), encoding="utf-8")
        plugin2 = self.make_plugin()
        leaks = plugin2._policy_leases.get(VIDEO, {})
        self.assertEqual(set(leaks), {"manual"})
        self.assertIsNone(leaks["manual"].task)
        self.assertIn(VIDEO, plugin2._guard_suspended)
        # An explicit user stop can still release a migrated entry.
        result = await plugin2._execute_app_policy(VIDEO, "enable", source="manual")
        self.assertEqual(result["mode"], "enabled")
        self.assertNotIn(VIDEO, plugin2._policy_leases)

    async def test_legacy_temporary_entry_keeps_its_deadline(self):
        until = time.time() + 600
        path = Path(self.config["policy_state_path"])
        path.write_text(json.dumps({VIDEO: {"until": until}}), encoding="utf-8")
        plugin2 = self.make_plugin()
        lease = plugin2._policy_leases[VIDEO]["manual"]
        self.assertAlmostEqual(lease.until, until, places=3)
        self.assertIsNotNone(lease.task)

    async def test_failed_restore_keeps_its_lease(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        self.device.return_value = {
            "success": False, "verified": False, "package": VIDEO, "error": "device offline",
        }
        result = await self.plugin._execute_app_policy(VIDEO, "enable", source="manual")
        self.assertEqual(result["mode"], "restore_pending")
        self.assertIn("manual", self.leases())
        self.assertIn(VIDEO, self.plugin._guard_suspended)
        saved = json.loads(Path(self.config["policy_state_path"]).read_text(encoding="utf-8"))
        self.assertIn("manual", saved["packages"][VIDEO]["leases"])

    async def test_state_file_is_versioned_and_leaves_no_temp_files(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        await self.plugin._execute_app_policy(VIDEO, "disable", 30, source="night_guard")
        path = Path(self.config["policy_state_path"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], module.POLICY_STATE_VERSION)
        entry = saved["packages"][VIDEO]["leases"]
        self.assertIsNone(entry["manual"]["until"])
        self.assertIsInstance(entry["night_guard"]["until"], float)
        self.assertEqual(list(path.parent.glob(path.name + ".*.tmp")), [])

    async def test_expired_lease_restores_the_app(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="night_guard")
        self.plugin._cancel_policy_expiry(VIDEO, "night_guard")
        await self.plugin._expire_policy_lease(VIDEO, "night_guard", 0)
        self.assertEqual(self.leases(), {})
        self.assertNotIn(VIDEO, self.plugin._guard_suspended)
        self.device.assert_awaited_with(VIDEO, "enable")

    async def test_restore_guard_apps_reports_buckets(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="night_guard")
        outcome = await self.plugin._restore_guard_apps(source="night_guard")
        self.assertEqual(outcome["still_suspended"], [VIDEO])
        self.assertEqual(outcome["released"], [])
        self.assertIn(VIDEO, self.plugin._guard_suspended)
        outcome = await self.plugin._restore_guard_apps(source="manual")
        self.assertEqual(outcome["released"], [VIDEO])
        self.assertNotIn(VIDEO, self.plugin._guard_suspended)

    async def test_terminate_releases_only_temporary_leases(self):
        await self.plugin._execute_app_policy(VIDEO, "disable", 0, source="manual")
        await self.plugin._execute_app_policy(OTHER, "disable", 30, source="night_guard")
        await self.plugin.terminate()
        self.assertEqual(set(self.plugin._policy_leases), {VIDEO})
        self.assertEqual(set(self.plugin._policy_leases[VIDEO]), {"manual"})
        saved = json.loads(Path(self.config["policy_state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(saved["packages"]), {VIDEO})


class AutonomySchedulerTests(unittest.IsolatedAsyncioTestCase):
    """P1-D: the phone plugin decides when to ask, never what to say."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "enabled": True,
            "control_backend": "app",
            "enable_night_guard": False,
            "enable_autonomy_scheduler": False,
            "allowed_user_ids": USER,
            "care_status_path": str(Path(self.temp.name) / "care.json"),
        }
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path",
                    "policy_state_path", "notification_path", "night_guard_state_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        context = types.SimpleNamespace(send_message=AsyncMock())
        context.register_web_api = lambda *args, **kwargs: None
        self.plugin = module.PhoneAgentPlugin(context, self.config)
        self.addAsyncCleanup(self.plugin.terminate)

    def test_care_targets_only_private_sessions(self):
        self.plugin._night_guard_states[USER] = {"session": "aiocqhttp:FriendMessage:" + USER}
        self.plugin._night_guard_states["group-user"] = {"session": "aiocqhttp:GroupMessage:123"}
        targets = self.plugin._care_targets()
        self.assertEqual([item["user_id"] for item in targets], [USER])

    async def test_tick_skips_without_provider(self):
        calls = []

        class Provider:
            async def evaluate_care(self, *args):
                calls.append(args)
                return {"evaluated": True}

        self.plugin._companion_care = lambda: None
        self.plugin._night_guard_states[USER] = {"session": "aiocqhttp:FriendMessage:" + USER}
        self.plugin.device_app.record_device_status({"reported_at": time.time()})
        await self.plugin._autonomy_tick()
        self.assertEqual(calls, [])

    async def test_tick_calls_provider_on_fresh_device(self):
        calls = []

        class Provider:
            async def evaluate_care(self, user_id, umo, role):
                calls.append((user_id, umo, role))
                return {"evaluated": True, "outcome": "preview", "reason": "preview"}

        self.plugin._companion_care = lambda: Provider()
        self.plugin._night_guard_states[USER] = {"session": "aiocqhttp:FriendMessage:" + USER}
        self.plugin.device_app.record_device_status({"reported_at": time.time()})
        await self.plugin._autonomy_tick()
        self.assertEqual(calls, [(USER, "aiocqhttp:FriendMessage:" + USER, "owner")])

    async def test_tick_skips_stale_heartbeat(self):
        calls = []

        class Provider:
            async def evaluate_care(self, *args):
                calls.append(args)
                return {"evaluated": True}

        self.plugin._companion_care = lambda: Provider()
        self.plugin._night_guard_states[USER] = {"session": "aiocqhttp:FriendMessage:" + USER}
        self.plugin.device_app.record_device_status({"reported_at": time.time() - 600})
        await self.plugin._autonomy_tick()
        self.assertEqual(calls, [])

    def test_care_snapshot_reads_the_companion_file(self):
        Path(self.config["care_status_path"]).write_text(
            json.dumps({"mode": "preview", "autonomy_enabled": True, "users": {}}),
            encoding="utf-8",
        )
        snapshot = self.plugin._care_status_snapshot()
        self.assertEqual(snapshot["mode"], "preview")
        self.assertTrue(snapshot["autonomy_enabled"])

    def test_care_snapshot_missing_file_is_empty(self):
        self.assertEqual(self.plugin._care_status_snapshot(), {})


if __name__ == "__main__":
    unittest.main()