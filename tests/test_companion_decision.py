"""R01–R08: companion-context autonomy decision consumption, budget, and freshness."""
import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1] / "companion-context"
api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = logging.getLogger(__name__)
star_api = types.ModuleType("astrbot.api.star")


class Star:
    def __init__(self, context):
        self.context = context


star_api.Star = Star
package = types.ModuleType("companion_test")
package.__path__ = [str(ROOT)]
spec = importlib.util.spec_from_file_location("companion_test.main", ROOT / "main.py")
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                             "astrbot.api.star": star_api, "companion_test": package}):
    spec.loader.exec_module(module)

USER = "u-test-001"
USER_DICT = {"user_id": USER, "relationship_role": "owner", "umo": "aiocqhttp:FriendMessage:" + USER}


def make_config(temp_dir, **overrides):
    config = {
        "enabled": True,
        "relationship_mode_enabled": True,
        "autonomous_decision_enabled": True,
        "autonomous_decision_mode": "care_auto",
        "decision_cooldown_minutes": 15,
        "decision_daily_limit": 3,
        "decision_eval_daily_limit": 24,
        "decision_eval_min_interval_seconds": 0,
        "decision_reservation_ttl_seconds": 300,
        "quiet_hours": "00:00-00:00",  # disabled by default
        "include_screen": True,
        "include_health": True,
        "private_only": True,
        "allowed_user_ids": USER,
        "decision_budget_path": str(Path(temp_dir) / "budget.json"),
        # Keep the app-facing snapshot inside the temp dir, not the repo.
        "care_status_path": str(Path(temp_dir) / "care_status.json"),
    }
    config.update(overrides)
    return config


class AutonomyDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp.name)
        self.context = types.SimpleNamespace()
        self.plugin = module.PhoneCompanionContext(self.context, self.config)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def test_r01_no_action_blocks_send(self):
        """R01: _parse_decision consumes no_action and returns valid=True."""
        decision = module.PhoneCompanionContext._parse_decision(
            '{"intent":"no_action","reason_code":"user_busy","message":null}'
        )
        self.assertTrue(decision["valid"])
        self.assertEqual(decision["intent"], "no_action")

    def test_r01_send_message_parsed(self):
        decision = module.PhoneCompanionContext._parse_decision(
            '{"intent":"send_message","message":"早点睡"}'
        )
        self.assertTrue(decision["valid"])
        self.assertEqual(decision["intent"], "send_message")
        self.assertEqual(decision["message"], "早点睡")

    def test_r01_legacy_intent_mapped_to_send_message(self):
        """R01-v3: legacy intents (greeting/care/...) map to send_message."""
        decision = module.PhoneCompanionContext._parse_decision(
            '{"intent":"care","message":"早点休息"}'
        )
        self.assertTrue(decision["valid"])
        self.assertEqual(decision["intent"], "send_message")

    def test_r01_invalid_output_is_not_valid(self):
        """R01-v3: non-JSON and unknown intents are invalid, not silently no_action."""
        decision = module.PhoneCompanionContext._parse_decision("这不是JSON")
        self.assertFalse(decision["valid"])
        decision = module.PhoneCompanionContext._parse_decision('{"intent":"suspend_app","message":"pm suspend"}')
        self.assertFalse(decision["valid"])
        # send_message without a message is also invalid.
        decision = module.PhoneCompanionContext._parse_decision('{"intent":"send_message","message":""}')
        self.assertFalse(decision["valid"])

    async def test_r02_preview_mode_blocks_send_but_allows_eval(self):
        """R02: preview mode allows evaluation but blocks the send side effect."""
        self.config["autonomous_decision_mode"] = "preview"
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        result = await self.run_generation(original)
        # Model was called (evaluation allowed), but send was blocked.
        original.assert_awaited_once()
        self.assertEqual(result, "")
        # Budget should reflect an evaluation, not a send.
        self.assertEqual(self.plugin._decision_count.get(USER), 0)
        self.assertGreaterEqual(self.plugin._decision_eval_count.get(USER, 0), 1)

    async def test_r02_quiet_hours_blocks_evaluation(self):
        """R02: quiet_hours is a hard gate, not a prompt hint."""
        self.config["quiet_hours"] = "00:00-23:59"  # all day
        original = AsyncMock(return_value="hello")
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        original.assert_not_awaited()

    async def test_r02_daily_limit_zero_blocks(self):
        self.config["decision_daily_limit"] = 0
        original = AsyncMock(return_value="hello")
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        original.assert_not_awaited()

    async def test_p1c_screen_cache_drops_after_device_rebind(self):
        """P1-C: a cached observation dies with its binding, not just its TTL."""
        state = {"generation": 1, "calls": 0}
        status = {
            "foreground_package": "tv.danmaku.bilibilihd",
            "screen_interactive": True,
            "battery_percent": 42,
            "reported_at": time.time(),
        }

        def last_status():
            state["calls"] += 1
            return dict(status)

        device_app = types.SimpleNamespace(
            last_status=last_status,
            registration_generation=lambda: state["generation"],
        )
        phone = types.SimpleNamespace(
            _tool_enabled=lambda name: True,
            _control_backend=lambda: "app",
            device_app=device_app,
            _night_guard_packages=lambda: set(),
            _app_aliases=lambda: {},
        )
        first = await self.plugin._screen_snapshot(phone)
        self.assertTrue(first)
        await self.plugin._screen_snapshot(phone)
        self.assertEqual(state["calls"], 1)  # cache hit, no second read

        state["generation"] = 2
        await self.plugin._screen_snapshot(phone)
        self.assertEqual(state["calls"], 2)  # rebind forces a fresh read

    async def test_r03_budget_persists_across_restart(self):
        """R03: sent count survives restart via the budget file."""
        self.config["autonomous_decision_mode"] = "care_auto"
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        result = await self.run_generation(original)
        self.assertIn("在吗", result)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        # Restart: create a new plugin instance loading the same budget file.
        plugin2 = module.PhoneCompanionContext(self.context, self.config)
        self.assertEqual(plugin2._decision_count.get(USER), 1)
        await plugin2.terminate()

    async def test_r03_no_action_does_not_consume_send_budget(self):
        self.config["autonomous_decision_mode"] = "care_auto"
        original = AsyncMock(return_value='{"intent":"no_action","message":null}')
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        # no_action should not count as a sent message.
        self.assertEqual(self.plugin._decision_count.get(USER), 0)

    def test_r06_app_mode_reads_heartbeat_not_operit(self):
        """R06: screen snapshot from App heartbeat cache, not Operit."""
        import time as _time
        phone = types.SimpleNamespace(
            _tool_enabled=lambda name: True,
            _control_backend=lambda: "app",
            device_app=types.SimpleNamespace(
                last_status=lambda: {
                    "foreground_package": "tv.danmaku.bilibilihd",
                    "screen_interactive": True,
                    "battery_percent": 42,
                    "reported_at": _time.time(),
                    "received_at": _time.time(),
                }
            ),
            _night_guard_packages=lambda: {"tv.danmaku.bilibilihd"},
            _app_aliases=lambda: {"哔哩哔哩": "tv.danmaku.bilibilihd"},
            _operit_health_sync=lambda: {"available": False},
        )
        snapshot = self.plugin._screen_snapshot_from_app(phone)
        self.assertEqual(snapshot["activity"], "video")
        self.assertEqual(snapshot["foreground"], "tv.danmaku.bilibilihd")
        self.assertEqual(snapshot["battery"], 42)

    def test_r06_app_mode_screen_off_returns_idle(self):
        import time as _time
        phone = types.SimpleNamespace(
            _tool_enabled=lambda name: True,
            _control_backend=lambda: "app",
            device_app=types.SimpleNamespace(
                last_status=lambda: {
                    "foreground_package": "tv.danmaku.bilibilihd",
                    "screen_interactive": False,
                    "battery_percent": 42,
                    "reported_at": _time.time(),
                    "received_at": _time.time(),
                }
            ),
            _night_guard_packages=lambda: set(),
            _app_aliases=lambda: {},
        )
        snapshot = self.plugin._screen_snapshot_from_app(phone)
        self.assertEqual(snapshot["activity"], "idle")

    def test_r07_stale_health_does_not_trigger_care(self):
        """R07: health with fresh=False does not add sleep-care topics."""
        # Simulate the relationship context with stale health data.
        stale_health = {"sleep_total_min": 180, "sleep_score": 50, "fresh": False, "date": "2020-01-01"}
        ctx = self.plugin._relationship_context(USER_DICT, {}, stale_health, "")
        # "睡眠明显不足" should NOT appear because fresh=False.
        self.assertNotIn("睡眠明显不足", ctx)

    def test_r07_fresh_health_triggers_care(self):
        fresh_health = {"sleep_total_min": 180, "sleep_score": 50, "fresh": True, "date": datetime.now().date().isoformat()}
        ctx = self.plugin._relationship_context(USER_DICT, {}, fresh_health, "")
        self.assertIn("睡眠明显不足", ctx)

    def test_r08_no_action_json_not_amplified(self):
        """R08: _amplify_proactive_text skips JSON control objects."""
        self.config["force_affectionate_coda"] = True
        result = self.plugin._amplify_proactive_text(
            USER_DICT, '{"intent":"no_action","message":null}'
        )
        # Should return the original JSON, not append a coda.
        self.assertNotIn("想", result)

    def test_r08_plain_text_still_amplified(self):
        self.config["force_affectionate_coda"] = True
        self.plugin._last_affection_coda[USER] = datetime.min
        # Set the cue that _amplify_proactive_text reads.
        self.plugin._last_relation_cue[USER] = "他正在刷视频"
        result = self.plugin._amplify_proactive_text(USER_DICT, "在干嘛呢")
        # Plain text should get a coda appended.
        self.assertIn("视频", result)

    def test_r04_stale_heartbeat_returns_empty(self):
        """R04-v3: missing or old reported_at returns empty, not fabricated activity."""
        import time as _time
        phone = types.SimpleNamespace(
            _tool_enabled=lambda name: True,
            _control_backend=lambda: "app",
            device_app=types.SimpleNamespace(
                last_status=lambda: {
                    "foreground_package": "tv.danmaku.bilibilihd",
                    "screen_interactive": True,
                    "battery_percent": 42,
                    "reported_at": None,  # Missing — must not trust.
                    "received_at": _time.time(),
                }
            ),
            _night_guard_packages=lambda: {"tv.danmaku.bilibilihd"},
            _app_aliases=lambda: {},
        )
        snapshot = self.plugin._screen_snapshot_from_app(phone)
        self.assertEqual(snapshot, {})

    def test_r04_old_reported_at_returns_empty(self):
        import time as _time
        phone = types.SimpleNamespace(
            _tool_enabled=lambda name: True,
            _control_backend=lambda: "app",
            device_app=types.SimpleNamespace(
                last_status=lambda: {
                    "foreground_package": "tv.danmaku.bilibilihd",
                    "screen_interactive": True,
                    "battery_percent": 42,
                    "reported_at": _time.time() - 600,  # 10 min old
                    "received_at": _time.time(),
                }
            ),
            _night_guard_packages=lambda: set(),
            _app_aliases=lambda: {},
        )
        snapshot = self.plugin._screen_snapshot_from_app(phone)
        self.assertEqual(snapshot, {})

    async def test_r03_cross_day_cooldown_preserved(self):
        """R03-v3: last_at from yesterday still enforces cooldown today."""
        from datetime import datetime, timedelta
        self.plugin._decision_last[USER] = datetime.now() - timedelta(minutes=5)
        self.plugin._decision_day[USER] = (datetime.now() - timedelta(days=1)).date().isoformat()
        self.plugin._decision_count[USER] = 1
        self.plugin._save_decision_budget()
        plugin2 = module.PhoneCompanionContext(self.context, self.config)
        # last_at should be restored even though the day changed.
        self.assertIsNotNone(plugin2._decision_last.get(USER))
        allowed, reason = plugin2._decision_allowed(USER)
        self.assertFalse(allowed)
        self.assertEqual(reason, "cooldown")
        await plugin2.terminate()

    async def test_p1d_plain_text_is_accepted_as_the_message(self):
        """The host's persona prompt yields natural text; treat it as the message.

        Still gated: preview, quiet hours, budget and the post-model checks all
        run first, so this is not the old "invalid passthrough" bypass.
        """
        original = AsyncMock(return_value="视频比我还香是吧，眼睛先歇会儿？")
        result = await self.run_generation(original)
        self.assertIn("眼睛先歇会儿", result)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        status = json.loads(Path(self.config["care_status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(status["users"][USER]["recent"][-1]["outcome"], "sent_text")

    async def test_p1d_broken_json_is_still_rejected(self):
        original = AsyncMock(return_value='{"intent": "send_message", "message": ')
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        self.assertEqual(self.plugin._decision_count.get(USER), 0)

    async def test_v4_preview_blocks_all_autonomy_output(self):
        """v4: preview mode blocks ALL autonomy output including invalid and send_message."""
        self.config["autonomous_decision_mode"] = "preview"
        # send_message should be blocked.
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        # Invalid output should also produce nothing.
        original2 = AsyncMock(return_value="普通文本")
        result2 = await self.run_generation(original2)
        self.assertEqual(result2, "")
        self.assertEqual(self.plugin._decision_count.get(USER), 0)

    async def test_v4_non_eligible_user_host_path_restored(self):
        """v4: non-eligible user gets host behavior, not autonomy consumption."""
        # Use a user NOT in the allowlist.
        other_user = {"user_id": "u-stranger", "umo": "aiocqhttp:FriendMessage:u-stranger"}
        original = AsyncMock(return_value="host原始消息")
        result = await self.run_generation(original, user=other_user)
        # Host path: original text returned with amplification.
        self.assertIn("host原始消息", result)
        # No autonomy budget consumed.
        self.assertIsNone(self.plugin._decision_count.get("u-stranger"))

    async def test_v4_bridge_disabled_host_path_restored(self):
        """v4: when bridge enabled=false, host original behavior is restored."""
        self.config["enabled"] = False
        original = AsyncMock(return_value="host原始消息")
        result = await self.run_generation(original)
        self.assertIn("host原始消息", result)

    async def run_generation(self, original, user=None, action_context="ctx"):
        """Drive the production routing path; only the host/model is a stub.

        v4 §7: tests must not keep a private copy of the wrapper. This calls
        the same method the installed wrapper calls.
        """
        user = USER_DICT if user is None else user
        return await self.plugin._proxied_generation(
            original, [user], {"action_context": action_context}, user, action_context
        )


class BudgetReservationTests(unittest.IsolatedAsyncioTestCase):
    """P1-B: reservation ledger, concurrency, revocation and evaluation limits."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp.name)
        self.context = types.SimpleNamespace()
        self.plugin = module.PhoneCompanionContext(self.context, self.config)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def run_generation(self, original, user=None, action_context="ctx"):
        user = USER_DICT if user is None else user
        return await self.plugin._proxied_generation(
            original, [user], {"action_context": action_context}, user, action_context
        )

    async def test_p1b_concurrent_limit_one_admits_one_send(self):
        """v4 §4: two concurrent calls must not both pass a limit=1 gate."""
        self.config["decision_daily_limit"] = 1

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.05)
            return '{"intent":"send_message","message":"在吗"}'

        original = AsyncMock(side_effect=slow)
        results = await asyncio.gather(
            self.run_generation(original),
            self.run_generation(original),
        )
        self.assertEqual(len([item for item in results if item]), 1)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)

    async def test_p1b_budget_write_failure_blocks_send(self):
        """v4 §4: an unwritable budget must not let the message out."""
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        with patch.object(self.plugin, "_save_decision_budget", return_value=False):
            result = await self.run_generation(original)
        self.assertEqual(result, "")
        original.assert_not_awaited()
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)

    async def test_p1b_authorization_revoked_during_model_blocks_send(self):
        """v4 §3: revoking autonomy while the model runs must stop egress."""
        async def revoke(*args, **kwargs):
            self.config["autonomous_decision_enabled"] = False
            return '{"intent":"send_message","message":"在吗"}'

        result = await self.run_generation(AsyncMock(side_effect=revoke))
        self.assertEqual(result, "")
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)

    async def test_p1b_allowlist_revoked_during_model_blocks_send(self):
        async def revoke(*args, **kwargs):
            self.config["allowed_user_ids"] = "someone-else"
            return '{"intent":"send_message","message":"在吗"}'

        result = await self.run_generation(AsyncMock(side_effect=revoke))
        self.assertEqual(result, "")
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)

    async def test_p1b_no_action_releases_the_slot(self):
        original = AsyncMock(return_value='{"intent":"no_action","message":null}')
        result = await self.run_generation(original)
        self.assertEqual(result, "")
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)
        self.assertEqual(self.plugin._decision_eval_count.get(USER), 1)
        self.assertEqual(self.plugin._reservations_for(USER)[0]["status"], "released")

    async def test_p1b_eval_cap_bounds_no_action(self):
        """v4 §4: repeated no_action cannot call the model without bound."""
        self.config["decision_eval_daily_limit"] = 2
        original = AsyncMock(return_value='{"intent":"no_action","message":null}')
        for _ in range(2):
            self.assertEqual(await self.run_generation(original), "")
        self.assertEqual(await self.run_generation(original), "")
        self.assertEqual(original.await_count, 2)
        self.assertEqual(self.plugin._decision_eval_count[USER], 2)

    async def test_p1b_eval_interval_throttles(self):
        """v4 §4: a minimum interval between evaluations."""
        self.config["decision_eval_min_interval_seconds"] = 300
        original = AsyncMock(return_value='{"intent":"no_action","message":null}')
        await self.run_generation(original)
        await self.run_generation(original)
        self.assertEqual(original.await_count, 1)

    async def test_p1b_closed_bridge_passthrough_is_untouched(self):
        """v4 §3: a closed bridge must not observe or rewrite the host call."""
        self.config["enabled"] = False
        original = AsyncMock(return_value="host原始消息")
        enrich = AsyncMock(side_effect=AssertionError("closed bridge must not observe"))
        with patch.object(self.plugin, "_enrich", new=enrich):
            result = await self.run_generation(original, action_context="ctx")
        self.assertEqual(result, "host原始消息")
        self.assertEqual(original.await_args.kwargs.get("action_context"), "ctx")
        enrich.assert_not_awaited()

    async def test_p1b_uninstall_restores_host_method(self):
        target = types.SimpleNamespace()
        original = AsyncMock()
        wrapped = AsyncMock()
        target._generate_proactive_message_with_llm = wrapped
        self.plugin._target = target
        self.plugin._original = original
        self.plugin._wrapped = wrapped
        self.plugin._uninstall_bridge("test")
        self.assertIs(target._generate_proactive_message_with_llm, original)
        self.assertFalse(self.plugin._bridge_status["installed"])
        # Another owner's method must never be clobbered.
        foreign = AsyncMock()
        target._generate_proactive_message_with_llm = foreign
        self.plugin._target = target
        self.plugin._original = original
        self.plugin._wrapped = wrapped
        self.plugin._uninstall_bridge("test")
        self.assertIs(target._generate_proactive_message_with_llm, foreign)

    async def test_p1b_delivery_failure_releases_slot(self):
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        await self.run_generation(original)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        reservation = self.plugin._reservations_for(USER)[0]
        await self.plugin._mark_delivery_failed(reservation["id"])
        self.assertEqual(self.plugin._decision_count.get(USER), 0)
        self.assertEqual(reservation["status"], "failed")

    async def test_p1b_delivered_keeps_slot(self):
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        await self.run_generation(original)
        reservation = self.plugin._reservations_for(USER)[0]
        await self.plugin._mark_delivered(reservation["id"])
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        self.assertEqual(reservation["status"], "delivered")

    async def test_p1b_restart_frees_a_reserved_slot(self):
        reservation, reason = await self.plugin._reserve_decision(USER)
        self.assertEqual(reason, "allowed")
        self.assertIsNotNone(reservation)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        plugin2 = module.PhoneCompanionContext(self.context, self.config)
        # The process died mid-evaluation: nothing was produced, so the slot is
        # free again and the reservation is recorded as released.
        self.assertEqual(plugin2._decision_count.get(USER, 0), 0)
        self.assertEqual(plugin2._reservations_for(USER)[0]["released_reason"], "restart")
        await plugin2.terminate()

    async def test_p1b_reserve_is_atomic(self):
        """The check and the claim happen in one critical section."""
        self.config["decision_daily_limit"] = 1
        first, second = await asyncio.gather(
            self.plugin._reserve_decision(USER),
            self.plugin._reserve_decision(USER),
        )
        admitted = [item for item, _ in (first, second) if item is not None]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(self.plugin._decision_count.get(USER), 1)

    async def test_p1b_last_slot_prompt_does_not_forbid_sending(self):
        """The prompt must not say "don't send" on the slot it just admitted."""
        self.config["decision_daily_limit"] = 1
        phone = types.SimpleNamespace(
            _read_health_db_sync=lambda *args, **kwargs: {},
            _tool_enabled=lambda name: False,
            _control_backend=lambda: "app",
        )
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        with patch.object(self.plugin, "_phone", return_value=phone):
            result = await self.run_generation(original)
        self.assertIn("在吗", result)
        context = original.await_args.kwargs["action_context"]
        self.assertIn("[autonomy]", context)
        self.assertNotIn("本轮不主动联系", context)

    async def test_p1b_model_error_releases_the_slot(self):
        original = AsyncMock(side_effect=RuntimeError("model down"))
        with self.assertRaises(RuntimeError):
            await self.run_generation(original)
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)
        self.assertEqual(self.plugin._reservations_for(USER)[0]["status"], "released")

    async def test_p1b_install_and_hot_disable_cycle(self):
        """v4 §3: closing the bridge uninstalls; reopening installs again."""
        target = types.SimpleNamespace()

        async def host(user=None, action_context=""):
            return "host"

        target._generate_proactive_message_with_llm = host
        self.plugin._companion = lambda: target
        self.plugin._phone = lambda: object()

        await self.plugin._install_bridge_once()
        installed = target._generate_proactive_message_with_llm
        self.assertIsNot(installed, host)
        self.assertTrue(self.plugin._bridge_status["installed"])

        self.plugin._uninstall_bridge("disabled")
        self.assertIs(target._generate_proactive_message_with_llm, host)

        await self.plugin._install_bridge_once()
        self.assertIsNot(target._generate_proactive_message_with_llm, host)

    async def test_p1b_slow_model_past_ttl_drops_the_message(self):
        """A result that arrives after the slot expired must not be sent.

        The TTL sweep has already given the slot back, so the late message is
        dropped rather than delivered minutes after the moment it described.
        """
        async def slow(*args, **kwargs):
            for item in self.plugin._reservations_for(USER):
                item["expires_at"] = time.time() - 1
            self.plugin._sweep_reservations(USER)
            return '{"intent":"send_message","message":"在吗"}'

        result = await self.run_generation(AsyncMock(side_effect=slow))
        self.assertEqual(result, "")
        self.assertEqual(self.plugin._decision_count.get(USER, 0), 0)
        self.assertEqual(self.plugin._reservations_for(USER)[0]["status"], "released")

    async def test_p1b_release_is_idempotent(self):
        """Self-review: a double release must not debit the count twice."""
        reservation, _ = await self.plugin._reserve_decision(USER)
        await self.plugin._release_decision(reservation, "once")
        await self.plugin._release_decision(reservation, "twice")
        self.assertEqual(self.plugin._decision_count.get(USER), 0)

    async def test_p1b_commit_then_release_does_not_double_count(self):
        reservation, _ = await self.plugin._reserve_decision(USER)
        self.assertTrue(await self.plugin._commit_decision_generated(reservation))
        self.assertEqual(self.plugin._decision_count.get(USER), 1)
        await self.plugin._release_decision(reservation, "after_commit")
        self.assertEqual(self.plugin._decision_count.get(USER), 0)
        # A late host failure on an already-released slip must be a no-op.
        await self.plugin._mark_delivery_failed(reservation["id"])
        self.assertEqual(self.plugin._decision_count.get(USER), 0)

    async def test_p1d_evaluate_care_requires_installed_bridge(self):
        self.assertIsNone(self.plugin._wrapped)
        result = await self.plugin.evaluate_care(USER)
        self.assertFalse(result["evaluated"])
        self.assertEqual(result["reason"], "bridge_not_installed")

    async def test_p1d_care_status_records_preview(self):
        self.config["autonomous_decision_mode"] = "preview"
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        await self.run_generation(original)
        status = json.loads(Path(self.config["care_status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "preview")
        self.assertTrue(status["autonomy_enabled"])
        entry = status["users"][USER]
        self.assertEqual(entry["sent_today"], 0)
        self.assertEqual(entry["recent"][-1]["outcome"], "preview")

    async def test_p1d_gated_decision_is_recorded_with_reason(self):
        self.config["quiet_hours"] = "00:00-23:59"
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        await self.run_generation(original)
        original.assert_not_awaited()
        status = json.loads(Path(self.config["care_status_path"]).read_text(encoding="utf-8"))
        entry = status["users"][USER]["recent"][-1]
        self.assertEqual(entry["outcome"], "gated")
        self.assertEqual(entry["reason"], "quiet_hours")

    async def test_p1d_budget_file_records_the_reservation(self):
        original = AsyncMock(return_value='{"intent":"send_message","message":"在吗"}')
        await self.run_generation(original)
        data = json.loads(Path(self.config["decision_budget_path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        entry = data["users"][USER]
        self.assertEqual(entry["sent_count"], 1)
        self.assertEqual(entry["reservations"][-1]["status"], "generated")


if __name__ == "__main__":
    unittest.main()

class CareDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """The scheduler path must actually send; a status of "sent" is not proof."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp.name)
        self.context = types.SimpleNamespace(send_message=AsyncMock())
        self.plugin = module.PhoneCompanionContext(self.context, self.config)
        self.plugin._wrapped = AsyncMock(return_value="手机就剩24%电了，歇会儿？")

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_evaluate_care_delivers_the_message(self):
        result = await self.plugin.evaluate_care(USER, "tauru:FriendMessage:1125961157")
        self.assertTrue(result["delivered"])
        self.context.send_message.assert_awaited_once()
        umo, payload = self.context.send_message.await_args.args
        self.assertEqual(umo, "tauru:FriendMessage:1125961157")
        self.assertIn("歇会儿", str(payload))

    async def test_delivery_failure_is_recorded_not_claimed(self):
        self.context.send_message = AsyncMock(side_effect=RuntimeError("qq offline"))
        result = await self.plugin.evaluate_care(USER, "tauru:FriendMessage:1125961157")
        self.assertFalse(result["delivered"])
        status = json.loads(Path(self.config["care_status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(status["users"][USER]["recent"][-1]["reason"], "deliver_failed")