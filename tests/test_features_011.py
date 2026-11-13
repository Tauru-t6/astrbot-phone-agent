"""End-to-end feature routes with synthetic phones and bounded model replies."""
import asyncio
import base64
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_ui_api as fixture


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.UiApiTests.asyncSetUp
    asyncTearDown = fixture.UiApiTests.asyncTearDown
    create_plugin = fixture.UiApiTests.create_plugin
    post = fixture.UiApiTests.post
    register = fixture.UiApiTests.register
    snapshot = fixture.UiApiTests.snapshot
    simulate_phone = fixture.UiApiTests.simulate_phone
    scope = fixture.UiApiTests.scope
    propose = fixture.UiApiTests.propose
    approve = fixture.UiApiTests.approve
    finished = fixture.UiApiTests.finished
    event = fixture.UiApiTests.event

    async def test_results_are_requested_in_scope_and_return_only_after_fresh_evidence(self):
        self.simulate_phone(await self.register(version='0.5.0'))
        self.provider.text_chat.side_effect = [types.SimpleNamespace(completion_text=json.dumps(v)) for v in [
            {"action": "done", "evidence_node_id": "n1"},
            {"items": [{"title": {"node_id": "n1", "attribute": "text"}, "author": None}]}]]
        proposal = await self.propose(result_spec={"fields": ["title", "author"], "max_items": 1, "required_items": 1})
        self.assertEqual(proposal["approval"]["scope"]["result_spec"]["required_items"], 1)
        self.assertNotIn("ui_results", proposal["run"]["summary"])
        self.assertEqual((await self.approve(proposal)).status_code, 202)
        run = await self.finished(proposal)
        self.assertEqual(run.status, "done", run.last_error)
        result = run.summary["ui_results"]
        self.assertTrue(result["verified"])
        self.assertIsNone(result["items"][0]["author"])
        self.assertEqual(result["items"][0]["title"], "synthetic result private screen marker")
        status = json.loads(await self.plugin.phone_task_status(self.event(), run_id=run.run_id))
        self.assertEqual(status["results"]["items"], result["items"])
        self.plugin.context.send_message.assert_not_awaited()

    async def test_notification_only_when_explicit_scope_and_private_session(self):
        self.simulate_phone(await self.register(version='0.5.0'))
        run, approval = self.plugin._create_ui_proposal(self.scope(notify=True), "synthetic-owner",
            umo="synthetic:FriendMessage:owner")
        result, code = await self.plugin._launch_ui_task(run, approval)
        self.assertEqual(code,202)
        await self.finished({"run":{"run_id":run.run_id}})
        self.plugin.context.send_message.assert_awaited_once()
        await self.plugin._notify_ui_result(run)
        self.plugin.context.send_message.assert_awaited_once()
        with self.assertRaisesRegex(ValueError,"private_session_required"):
            self.plugin._create_ui_proposal(self.scope(notify=True),"synthetic-owner",umo="synthetic:GroupMessage:group")

    async def test_resume_creates_fresh_approval_and_skips_open_target(self):
        self.simulate_phone(await self.register())
        original = await self.propose()
        source = self.plugin._task_runs.run(original["run"]["run_id"])
        source.summary["ui_started"] = True
        source.last_error = "ui_model_timeout"
        response = await self.post('/app/task/resume', {'run_id':source.run_id,'requester_device_id':'personal-1'})
        self.assertEqual(response.status_code,200,await response.get_data(as_text=True))
        next_ = await response.get_json()
        self.assertNotEqual(next_["run"]["run_id"],source.run_id)
        self.assertEqual(next_["run"]["status"],"waiting_approval")
        self.assertFalse(self.calls)
        self.assertEqual((await self.approve(next_)).status_code,202)
        done = await self.finished(next_)
        self.assertEqual(done.status,'done',done.last_error)
        self.assertNotIn('open_app',[action for _,action,_ in self.calls])

    async def test_visual_scope_requires_feature_version_and_never_calls_visual_by_default(self):
        backend = await self.register()
        response = await self.post('/app/ui-task/propose',self.scope(allow_vision=True))
        self.assertEqual(response.status_code,400)
        self.config['enable_ui_vision']=True
        response = await self.post('/app/ui-task/propose',self.scope(allow_vision=True))
        self.assertEqual(response.status_code,400)
        await self.register(version='0.5.0')
        self.simulate_phone(backend)
        proposal=await self.propose()
        await self.approve(proposal);await self.finished(proposal)
        self.assertFalse(any('visual' in a for _,a,_ in self.calls))

    async def test_old_app_cannot_approve_scope_fields_it_cannot_display(self):
        backend=await self.register(version='0.4.0')
        for extra in [{'result_spec':{'fields':['title'],'max_items':1,'required_items':1}}, {'remember_recipe':True}]:
            response=await self.post('/app/ui-task/propose',self.scope(**extra))
            self.assertEqual(response.status_code,400)
            self.assertEqual((await response.get_json())['error_code'],'app_upgrade_required')
        await self.register(version='0.5.0')
        proposal=await self.propose(result_spec={'fields':['title'],'max_items':1,'required_items':1})
        await self.register(version='0.4.0')
        execute=self.simulate_phone(backend)
        response=await self.approve(proposal)
        self.assertEqual(response.status_code,403)
        execute.assert_not_called()

    async def test_ocr_snapshot_sends_picture_only_with_approved_scope_and_does_not_persist_it(self):
        backend=await self.register(version='0.5.0');self.config['enable_ui_vision']=True
        encoded=base64.b64encode(b'\xff\xd8SYNTHETIC_PRIVATE_IMAGE').decode()
        def execute(action,args=None,timeout=None,**kwargs):
            self.calls.append(('personal-1',action,args or {}))
            if action=='ui_snapshot':return {'success':False,'error_code':'ui_snapshot_unavailable'}
            if action=='ui_visual_snapshot':
                snap=self.snapshot();snap.update(source='ocr',screenshot={'format':'jpeg','data_base64':encoded})
                return {'success':True,'output':snap}
            return {'success':True}
        with patch.object(backend,'execute',side_effect=execute):
            proposal=await self.propose(allow_vision=True)
            await self.approve(proposal);run=await self.finished(proposal)
        self.assertEqual(run.status,'done',run.last_error)
        self.assertTrue(any('image_urls' in call.kwargs for call in self.provider.text_chat.call_args_list))
        self.assertNotIn(encoded,json.dumps(run.to_dict()))

    async def test_revoked_paired_device_cannot_execute_or_fallback_to_legacy_relay(self):
        backend=await self.register()
        code=self.plugin._pairing.create(endpoint='http://127.0.0.1:8766',device_kind='personal',subject='synthetic-owner',
            owner_token='synthetic-token',replace_device_id='personal-1')
        self.plugin._pairing.redeem({'code':code['code'],'device_id':'personal-1','device_kind':'personal'})
        self.config.update(relay_base_url='http://relay.example',relay_token='synthetic-legacy')
        self.assertFalse(backend.relay_enabled())
        self.plugin._pairing.revoke('personal-1')
        with patch.object(backend,'_http_json',side_effect=AssertionError('must not dispatch')):
            self.assertEqual(backend.execute('ping')['error_code'],'device_revoked')

if __name__=='__main__':unittest.main()
