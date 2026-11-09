import asyncio
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from test_companion_decision import module, make_config, USER, USER_DICT

adapter = module.v2_adapter

class CompanionV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(self.temp.name, include_health=False, include_screen=False)
        self.host = types.SimpleNamespace(extension_api=types.SimpleNamespace(
            api_version='companion.product.v1', capabilities=lambda: {'lifecycle_state': 'ready'}))
        self.star = types.SimpleNamespace(root_dir_name='astrbot_plugin_private_companion',
            star_cls=self.host, version='7.1.9', activated=True)
        self.conv = types.SimpleNamespace(persona_id='persona-a', updated_at=time.time()-3600,
            history=json.dumps([{'role': 'user', 'content': '我今天在看书'}, {'role': 'assistant', 'content': '好，读完聊。'}]))
        self.provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text='{"intent":"no_action","reason_code":"user_busy","recheck_after_seconds":1800}')))
        self.context = types.SimpleNamespace(
            get_all_stars=lambda: [self.star], get_using_provider=lambda **kwargs: self.provider,
            conversation_manager=types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='conversation-a'),
                get_conversation=AsyncMock(side_effect=lambda *args: self.conv)),
            persona_manager=types.SimpleNamespace(get_persona_v3_by_id=lambda pid: {'prompt': '温柔、简洁'}),
            send_message=AsyncMock())
        self.phone = types.SimpleNamespace()
        self.plugin = module.PhoneCompanionContext(self.context, self.config)
        self.plugin._install_task.cancel()
        await asyncio.gather(self.plugin._install_task, return_exceptions=True)
        with patch.object(self.plugin, '_phone', return_value=self.phone):
            await self.plugin._install_bridge_once()
        self.plugin._enrich = AsyncMock(return_value='手机正在阅读，状态新鲜')

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def evaluate(self):
        return await self.plugin.evaluate_care(USER, USER_DICT['umo'], 'owner')

    async def test_discovers_719_without_patching_host(self):
        self.assertEqual(self.plugin.status()['mode'], 'v2')
        self.assertFalse(hasattr(self.host, '_generate_proactive_message_with_llm'))
        self.assertIs(self.phone._phone_companion_context, self.plugin)
        await self.plugin.terminate()
        self.assertFalse(hasattr(self.phone, '_phone_companion_context'))

    async def test_no_action_skips_unchanged_model_evaluation(self):
        await self.evaluate(); await self.evaluate()
        self.provider.text_chat.assert_awaited_once()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._decision_eval_count[USER], 1)
        self.assertEqual(self.plugin._last_outcome[USER]['reason'], 'unchanged_context')
        saved = json.loads(Path(self.config['decision_budget_path']).read_text())
        self.assertEqual(saved['users'][USER]['care_memory']['reason'], 'user_busy')

    async def test_new_conversation_content_rechecks(self):
        await self.evaluate()
        self.conv.history = json.dumps([{'role':'user','content':'我读完了，可以聊聊'}])
        await self.evaluate()
        self.assertEqual(self.provider.text_chat.await_count, 2)

    async def test_recent_chat_does_not_spend_model_budget(self):
        self.conv.updated_at = time.time()
        await self.evaluate()
        self.provider.text_chat.assert_not_awaited()
        self.assertEqual(self.plugin._last_outcome[USER]['reason'], 'recent_conversation')

    async def test_structured_message_delivered_and_scoped(self):
        self.provider.text_chat.return_value.completion_text = '{"intent":"send_message","message":"那本书读得怎么样了？"}'
        result = await self.evaluate()
        self.assertTrue(result['delivered'])
        self.assertEqual(self.context.send_message.call_args.args[0], USER_DICT['umo'])
        prompt = json.loads(self.provider.text_chat.call_args.kwargs['prompt'])
        self.assertEqual(prompt['recent_conversation'][0]['role'], 'user')

    async def test_model_text_and_malformed_json_are_never_delivered(self):
        for text in ['想你了', '{"intent":"send_message",}', '{"intent":"tap"}']:
            self.provider.text_chat.return_value.completion_text = text
            await self.evaluate()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._decision_count[USER], 0)

    async def test_duplicate_recent_assistant_suppressed(self):
        self.provider.text_chat.return_value.completion_text = '{"intent":"send_message","message":"好，读完聊。"}'
        await self.evaluate()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._last_outcome[USER]['reason'], 'duplicate_message')

    async def test_change_while_model_runs_blocks_delivery(self):
        async def response(**kwargs):
            self.conv.history = '[{"role":"user","content":"现在不要打扰我"}]'
            return types.SimpleNamespace(completion_text='{"intent":"send_message","message":"聊聊吗"}')
        self.provider.text_chat.side_effect = response
        await self.evaluate()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._last_outcome[USER]['reason'], 'conversation_changed')

    async def test_preview_does_not_send(self):
        self.config['autonomous_decision_mode'] = 'preview'
        self.provider.text_chat.return_value.completion_text = '{"intent":"send_message","message":"聊聊吗"}'
        await self.evaluate()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._decision_count[USER], 0)

    async def test_explicit_preview_never_sends_in_auto_mode(self):
        self.provider.text_chat.return_value.completion_text = '{"intent":"send_message","message":"聊聊吗"}'
        result = await self.plugin.evaluate_care(USER, USER_DICT['umo'], 'owner', preview=True)
        self.assertTrue(result['preview'])
        self.assertFalse(result['sent'])
        self.assertEqual(result['outcome'], 'preview')
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin._decision_count[USER], 0)
        self.assertNotIn(USER, self.plugin._decision_memory)

    async def test_large_history_reads_bounded_recent_messages(self):
        messages = [{'role':'user','content':'old' * 800_000}]
        messages.extend({'role':'user','content':'最近 '+str(i)} for i in range(7))
        self.conv.history = json.dumps(messages, ensure_ascii=False)
        material = await adapter.conversation_material(self.context, USER_DICT['umo'], USER)
        self.assertEqual(len(material['recent']), 6)
        self.assertEqual(material['recent'][-1]['content'], '最近 6')
        self.assertNotIn('old', str(material['recent']))

    def test_tail_handles_nested_objects_and_escaped_strings(self):
        messages = [{'role':'user','content':'old'*30000},
            {'role':'assistant','content':[{'type':'text','text':'escaped '+chr(34)+' [] {} '+chr(92)}]},
            {'role':'user','content':'last'}]
        tail = adapter.history_tail(json.dumps(messages), max_chars=1000)
        self.assertEqual(tail, messages[-2:])

    async def test_conversation_owner_mismatch_is_rejected(self):
        self.conv.user_id = 'bot:FriendMessage:someone-else'
        with self.assertRaisesRegex(ValueError, 'conversation_scope_mismatch'):
            await adapter.conversation_material(self.context, USER_DICT['umo'], USER)

    async def test_host_disabled_and_foreign_umo_fail_closed(self):
        self.star.activated = False
        self.assertIsNone(adapter.discover(self.context))
        await self.evaluate()
        self.provider.text_chat.assert_not_awaited()
        with self.assertRaises(ValueError):
            await adapter.conversation_material(self.context, 'bot:FriendMessage:someone-else', USER)
        self.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()

    async def test_false_delivery_receipt_not_counted_as_success(self):
        self.context.send_message.return_value = False
        self.provider.text_chat.return_value.completion_text = '{"intent":"send_message","message":"聊聊吗"}'
        self.assertFalse((await self.evaluate())['sent'])

    def test_parser_accepts_fenced_json_not_duplicate_fields(self):
        fence = chr(96) * 3
        self.assertEqual(adapter.parse_decision(fence+'json\n{"intent":"no_action"}\n'+fence)['intent'], 'no_action')
        for text in ['{"intent":"no_action","intent":"send_message"}', '{"intent":"no_action","x":NaN}']:
            with self.assertRaises(ValueError):
                adapter.parse_decision(text)
