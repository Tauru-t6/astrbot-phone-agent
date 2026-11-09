import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock

pkg = types.ModuleType('model_plan_test')
pkg.__path__ = [str(Path(__file__).parents[1])]
sys.modules.setdefault('model_plan_test', pkg)
planner = importlib.import_module('model_plan_test.model_planning')

class ModelPlanTests(unittest.IsolatedAsyncioTestCase):
    def valid(self, rows, **extra):
        return planner.validate_plan(json.dumps({'status': 'ready', 'steps': rows, **extra}),
            {'tv.danmaku.bili'}, {'open_app', 'home', 'close_app', 'screenshot', 'lock_screen'}, 6)

    def test_ordered_plan_prepends_observation(self):
        result = self.valid([{'action': 'open_app', 'args': {'package': 'tv.danmaku.bili'}}, {'action': 'screenshot'}])
        self.assertEqual([s.action for s in result.steps], ['observe', 'open_app', 'screenshot'])

    def test_no_execution_or_grant_fields_accepted(self):
        self.assertEqual(self.valid([{'action': 'home'}], grant=True).reason, 'invalid_model_plan')

    def test_reject_ungrounded_and_disabled_actions(self):
        for row in [{'action': 'tap', 'args': {'x': 1, 'y': 2}},
                    {'action': 'suspend_app', 'args': {'package': 'tv.danmaku.bili'}},
                    {'action': 'open_app', 'args': {'package': 'com.invented.app'}},
                    {'action': 'home', 'args': {'shell': 'rm'}},
                    {'action': 'open_app', 'args': {'package': ['tv.danmaku.bili']}},
                    {'action': ['home']}]:
            with self.subTest(row=row):
                self.assertFalse(self.valid([row]).steps)

    def test_lock_cannot_precede_navigation(self):
        self.assertFalse(self.valid([{'action': 'lock_screen'}, {'action': 'home'}]).steps)

    def test_limits_and_clarification(self):
        self.assertFalse(self.valid([{'action': 'home'}] * 7).steps)
        self.assertFalse(self.valid([]).steps)
        r = planner.validate_plan('{"status":"needs_clarification","question":"要打开哪个应用？"}', set(), set(), 6)
        self.assertEqual(r.reason, 'needs_clarification')
        self.assertIn('哪个', r.clarification)

    def test_strict_json_and_code_fences(self):
        for raw in ['{"status":"ready","status":"unsupported"}', '{"v":NaN}', '[]', 'here is {"status":"ready"}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                planner.parse_object(raw)
        self.assertEqual(planner.parse_object('```json\n{"status":"ready"}\n```')['status'], 'ready')

    def test_fallback_is_only_unambiguous_single_action(self):
        self.assertTrue(planner.simple_fallback_goal('打开哔哩哔哩'))
        self.assertTrue(planner.simple_fallback_goal('回到桌面'))
        for goal in ['不要打开抖音', '先打开抖音然后截图', '如果还没睡再打开抖音', '关闭抖音', '随便弄一下']:
            self.assertFalse(planner.simple_fallback_goal(goal))

    async def test_one_bounded_model_call_and_no_tools(self):
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"status":"ready","steps":[{"action":"home"}]}')))
        r = await planner.plan_goal(provider, '回桌面', aliases={}, allowed={'home'})
        self.assertEqual(r.steps[-1].action, 'home')
        provider.text_chat.assert_awaited_once()
        self.assertEqual(set(provider.text_chat.call_args.kwargs), {'prompt', 'system_prompt'})

    async def test_cancel_propagates_and_failure_does_not_retry(self):
        provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await planner.plan_goal(provider, '回桌面', aliases={}, allowed={'home'})
        provider.text_chat = AsyncMock(side_effect=TimeoutError)
        self.assertEqual((await planner.plan_goal(provider, '回桌面', aliases={}, allowed={'home'})).reason, 'model_unavailable')
        provider.text_chat.assert_awaited_once()
