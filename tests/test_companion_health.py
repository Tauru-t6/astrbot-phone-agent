"""Health consent/source changes must reach proactive context without a cache delay."""
import asyncio
import copy
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from test_companion_decision import make_config, module, USER, USER_DICT


class CompanionHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = module.PhoneCompanionContext(types.SimpleNamespace(), make_config(self.temp.name))
        self.plugin._install_task.cancel()
        await asyncio.gather(self.plugin._install_task, return_exceptions=True)
        self.generation = 1
        self.current = {"source": "health_connect", "available": True, "days": [{"steps": 123}]}
        self.phone = types.SimpleNamespace(
            config={"health_data_source": "auto", "enabled": True, "enable_health_tools": True}, _health_connect=object(),
            device_app=types.SimpleNamespace(registration_generation=lambda: self.generation),
            _read_health_db_sync=Mock(side_effect=lambda days: copy.deepcopy(self.current)),
        )
        self.phone._enabled = lambda: self.phone.config["enabled"]
        self.phone._tool_enabled = lambda name: name == "phone_health" and self.phone.config["enable_health_tools"]

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_cleared_or_revoked_health_is_not_kept_for_cache_ttl(self):
        for mode in ("auto", "health_connect"):
            for reason in ("no_snapshot", "permission_denied"):
                with self.subTest(mode=mode, reason=reason):
                    self.phone.config["health_data_source"] = mode
                    self.current = {"source": "health_connect", "available": True, "days": [{"steps": 123}]}
                    self.assertTrue((await self.plugin._health_snapshot(self.phone))["available"])
                    self.current = {"source": "health_connect", "available": False, "days": [], "error": reason}
                    result = await self.plugin._health_snapshot(self.phone)
                    self.assertFalse(result["available"])
                    self.assertEqual(result["days"], [])
                    self.assertEqual(result["error"], reason)

    async def test_rebound_phone_does_not_reuse_previous_health(self):
        self.assertTrue((await self.plugin._health_snapshot(self.phone))["available"])
        self.generation += 1
        self.current = {"source": "health_connect", "available": False, "days": [], "error": "no_snapshot"}
        self.assertEqual((await self.plugin._health_snapshot(self.phone))["days"], [])

    async def test_switching_source_discards_database_cache_and_phone_values(self):
        self.phone.config["health_data_source"] = "health_db"
        self.current = {"source": "health_db", "available": True, "days": [{"steps": 999}]}
        self.assertEqual((await self.plugin._health_snapshot(self.phone))["source"], "health_db")
        await self.plugin._health_snapshot(self.phone)
        self.phone._read_health_db_sync.assert_called_once()
        self.phone.config["health_data_source"] = "auto"
        self.current = {"source": "health_connect", "available": True, "days": [{"steps": 123}]}
        self.assertEqual((await self.plugin._health_snapshot(self.phone))["days"][0]["steps"], 123)
        self.phone.config["health_data_source"] = "health_db"
        self.current = {"source": "health_db", "available": True, "days": [{"steps": 456}]}
        self.assertEqual((await self.plugin._health_snapshot(self.phone))["days"][0]["steps"], 456)

    async def test_binding_change_during_read_discards_in_flight_snapshot(self):
        def read_and_rebind(days):
            self.generation += 1
            return {"source": "health_connect", "available": True, "days": [{"steps": 123}]}
        self.phone._read_health_db_sync.side_effect = read_and_rebind
        self.assertEqual(await self.plugin._health_snapshot(self.phone), {})

    async def test_legacy_database_keeps_cache_but_reload_invalidates_it(self):
        del self.phone._health_connect
        self.current = {"source": "health_db", "available": True, "days": [{"steps": 999}]}
        await self.plugin._health_snapshot(self.phone)
        await self.plugin._health_snapshot(self.phone)
        self.phone._read_health_db_sync.assert_called_once()
        replacement = types.SimpleNamespace(
            config={}, _enabled=lambda: True, _tool_enabled=lambda name: name == "phone_health",
            _read_health_db_sync=Mock(return_value={"source": "health_db", "days": []}))
        self.assertEqual((await self.plugin._health_snapshot(replacement))["days"], [])
        replacement._read_health_db_sync.assert_called_once()

    async def test_disabled_main_plugin_or_health_tool_never_enters_care_context(self):
        self.plugin.config["include_screen"] = False
        for key in ("enabled", "enable_health_tools"):
            with self.subTest(key=key):
                self.phone.config[key] = False
                self.phone._read_health_db_sync.reset_mock()
                with patch.object(self.plugin, "_phone", return_value=self.phone):
                    enriched = await self.plugin._enrich(USER_DICT, "")
                self.phone._read_health_db_sync.assert_not_called()
                self.assertNotIn("health=", enriched)
                self.assertEqual(self.plugin._latest_signals[USER]["health"], {})
                self.phone.config[key] = True

    async def test_disabling_main_health_clears_database_cache_before_it_can_be_reused(self):
        self.phone.config["health_data_source"] = "health_db"
        self.current = {"source": "health_db", "available": True, "days": [{"steps": 123}]}
        self.assertTrue((await self.plugin._health_snapshot(self.phone))["available"])
        self.assertIsNotNone(self.plugin._health_cache)
        self.phone.config["enable_health_tools"] = False
        self.assertEqual(await self.plugin._health_snapshot(self.phone), {})
        self.assertIsNone(self.plugin._health_cache)
        self.phone._read_health_db_sync.assert_called_once()
        self.phone.config["enable_health_tools"] = True
        self.current["days"] = [{"steps": 456}]
        self.assertEqual((await self.plugin._health_snapshot(self.phone))["days"][0]["steps"], 456)

    async def test_disable_during_health_read_discards_completed_values(self):
        for key in ("enabled", "enable_health_tools"):
            with self.subTest(key=key):
                def read_and_disable(days):
                    self.phone.config[key] = False
                    return copy.deepcopy(self.current)
                self.phone._read_health_db_sync.side_effect = read_and_disable
                self.assertEqual(await self.plugin._health_snapshot(self.phone), {})
                self.assertIsNone(self.plugin._health_cache)
                self.phone.config[key] = True

    async def test_missing_failing_or_nonboolean_permission_guards_fail_closed(self):
        reader = Mock(return_value=self.current)
        for enabled, tool in ((None, lambda name: True), (lambda: True, None),
                              (lambda: "false", lambda name: True), (lambda: True, lambda name: "false"),
                              (Mock(side_effect=RuntimeError("unavailable")), lambda name: True)):
            with self.subTest(enabled=enabled, tool=tool):
                phone = types.SimpleNamespace(_enabled=enabled, _tool_enabled=tool, _read_health_db_sync=reader)
                self.assertEqual(await self.plugin._health_snapshot(phone), {})
        reader.assert_not_called()

    async def test_disable_while_other_collector_runs_does_not_inject_already_read_health(self):
        self.plugin.config["include_screen"] = True
        finished = asyncio.Event()
        async def health_read(phone):
            result = copy.deepcopy(self.current)
            finished.set()
            return result
        async def screen_read(phone):
            await finished.wait()
            self.phone.config["enable_health_tools"] = False
            return {}
        with patch.object(self.plugin, "_phone", return_value=self.phone), \
             patch.object(self.plugin, "_health_snapshot", side_effect=health_read), \
             patch.object(self.plugin, "_screen_snapshot", side_effect=screen_read):
            enriched = await self.plugin._enrich(USER_DICT, "")
        self.assertNotIn("health=", enriched)
        self.assertEqual(self.plugin._latest_signals[USER]["health"], {})

    async def test_health_connect_sleep_and_heart_rate_reach_care_context(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        self.plugin.config["include_screen"] = False
        self.current = {
            "source": "health_connect", "zone_id": "Asia/Shanghai", "available": True, "fresh": True,
            "sync": {"ok": True, "stale": False},
            "days": [{"date": "2026-11-11", "sleep_minutes": 180, "heart_rate_avg": 70}],
        }
        with patch.object(module, "datetime", FixedDateTime), patch.object(self.plugin, "_phone", return_value=self.phone):
            enriched = await self.plugin._enrich(USER_DICT, "")
            health = self.plugin._latest_signals[USER]["health"]
            self.assertEqual(health["sleep_total_min"], 180)
            self.assertEqual(health["hr_avg"], 70)
            self.assertTrue(health["fresh"])
            self.assertIn("睡眠明显不足", enriched)
            self.assertNotIn("sleep_total_min", self.current["days"][0])
            self.current["days"][0]["date"] = "2026-11-10"
            historical = await self.plugin._enrich(USER_DICT, "")
            self.assertFalse(self.plugin._latest_signals[USER]["health"]["fresh"])
            self.assertNotIn("睡眠明显不足", historical)
