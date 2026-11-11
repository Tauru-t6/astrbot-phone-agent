"""Observation opt-out must block cached and in-flight bridge context reads."""
import asyncio
import json
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from test_companion_decision import make_config, module, USER, USER_DICT


class CompanionScreenPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = module.PhoneCompanionContext(
            types.SimpleNamespace(), make_config(self.temp.name, include_screen=True, include_health=False))
        self.plugin._install_task.cancel()
        await asyncio.gather(self.plugin._install_task, return_exceptions=True)
        self.enabled = True
        self.observe_enabled = True
        self.generation = 1
        self.status = {"foreground_package": "com.example.video", "battery_percent": 42,
                       "screen_interactive": True, "reported_at": time.time()}
        self.phone = types.SimpleNamespace(
            _enabled=lambda: self.enabled,
            _tool_enabled=lambda name: name == "phone_observe" and self.observe_enabled,
            _control_backend=lambda: "app",
            device_app=types.SimpleNamespace(last_status=Mock(side_effect=lambda: dict(self.status)),
                                              registration_generation=lambda: self.generation),
            _night_guard_packages=lambda: set(), _app_aliases=lambda: {},
            _operit_health_sync=Mock(return_value={"available": True}),
            _operit_task_sync=Mock(return_value={"success": True, "ai_response": json.dumps({
                "foreground": "com.example.video", "activity": "video", "battery": 42})}),
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def disable(self, kind):
        if kind == "plugin": self.enabled = False
        elif kind == "observe": self.observe_enabled = False
        else: self.plugin.config["include_screen"] = False

    def enable(self):
        self.enabled = self.observe_enabled = True
        self.plugin.config["include_screen"] = True

    async def test_each_permission_gate_discards_cache_and_blocks_further_reads(self):
        for gate in ("plugin", "observe", "bridge_screen"):
            with self.subTest(gate=gate):
                self.enable()
                self.assertTrue(await self.plugin._screen_snapshot(self.phone))
                self.assertIsNotNone(self.plugin._screen_cache)
                before = self.phone.device_app.last_status.call_count
                self.disable(gate)
                self.assertEqual(await self.plugin._screen_snapshot(self.phone), {})
                self.assertIsNone(self.plugin._screen_cache)
                self.assertEqual(self.phone.device_app.last_status.call_count, before)
        self.phone._operit_health_sync.assert_not_called()
        self.phone._operit_task_sync.assert_not_called()

    async def test_disabled_main_plugin_does_not_put_screen_in_model_context(self):
        self.enabled = False
        with patch.object(self.plugin, "_phone", return_value=self.phone):
            enriched = await self.plugin._enrich(USER_DICT, "")
        self.assertNotIn("screen=", enriched)
        self.assertEqual(self.plugin._latest_signals[USER]["screen"], {})
        self.phone.device_app.last_status.assert_not_called()

    async def test_shutdown_during_operit_health_check_prevents_observation_command(self):
        self.phone._control_backend = lambda: "operit"
        def health_and_shutdown():
            self.enabled = False
            return {"available": True}
        self.phone._operit_health_sync.side_effect = health_and_shutdown
        self.assertEqual(await self.plugin._screen_snapshot(self.phone), {})
        self.phone._operit_task_sync.assert_not_called()
        self.assertIsNone(self.plugin._screen_cache)

    async def test_permission_change_during_operit_observation_discards_result(self):
        self.phone._control_backend = lambda: "operit"
        for gate in ("plugin", "observe", "bridge_screen"):
            with self.subTest(gate=gate):
                self.enable()
                def observe_and_disable(*args):
                    self.disable(gate)
                    return {"success": True, "ai_response": json.dumps({
                        "foreground": "com.example.video", "activity": "video", "battery": 42})}
                self.phone._operit_task_sync.side_effect = observe_and_disable
                self.assertEqual(await self.plugin._screen_snapshot(self.phone), {})
                self.assertIsNone(self.plugin._screen_cache)

    async def test_shutdown_while_another_collector_waits_discards_completed_screen(self):
        self.plugin.config["include_health"] = True
        completed = asyncio.Event()
        async def screen(phone):
            completed.set()
            return {"foreground": "com.example.video", "activity": "video"}
        async def health(phone):
            await completed.wait()
            self.enabled = False
            return {}
        with patch.object(self.plugin, "_phone", return_value=self.phone), \
             patch.object(self.plugin, "_screen_snapshot", side_effect=screen), \
             patch.object(self.plugin, "_health_snapshot", side_effect=health):
            enriched = await self.plugin._enrich(USER_DICT, "")
        self.assertNotIn("screen=", enriched)
        self.assertEqual(self.plugin._latest_signals[USER]["screen"], {})

    async def test_missing_or_nonboolean_main_guard_cannot_read_cached_or_live_screen(self):
        self.assertTrue(await self.plugin._screen_snapshot(self.phone))
        for value in (None, lambda: "false", Mock(side_effect=RuntimeError("guard unavailable"))):
            with self.subTest(value=value):
                self.phone._enabled = value
                self.assertEqual(await self.plugin._screen_snapshot(self.phone), {})
        self.phone.device_app.last_status.assert_called_once()
