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
            config={"health_data_source": "auto"}, _health_connect=object(),
            device_app=types.SimpleNamespace(registration_generation=lambda: self.generation),
            _read_health_db_sync=Mock(side_effect=lambda days: copy.deepcopy(self.current)),
        )

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
            config={}, _read_health_db_sync=Mock(return_value={"source": "health_db", "days": []}))
        self.assertEqual((await self.plugin._health_snapshot(replacement))["days"], [])
        replacement._read_health_db_sync.assert_called_once()

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
