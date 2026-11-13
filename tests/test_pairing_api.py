"""Pairing owner controls and signed scoped requests through real Quart routes."""
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_app_api as fixture

module=fixture.module
pairing=module.PairingMixin._init_pairing.__globals__["PairingStore"].__module__
PairingStore=module.PairingMixin._init_pairing.__globals__["PairingStore"]
proof_function=module.PairingMixin._pairing_route_guard.__globals__["gateway_proof"]


class PairingApiTests(unittest.IsolatedAsyncioTestCase):
    create_plugin=fixture.AppApiTests.create_plugin
    post=fixture.AppApiTests.post

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.plugins=[]
        self.config={"enabled":True,"control_backend":"app","allowed_user_ids":"owner",
            "enable_observe_tool":True,"enable_task_execute":True,"enable_ui_tasks":True,
            "enable_notification_tool":True,"enable_reminder_tools":True,"enable_policy_tools":True,
            "enable_device_pairing":True,"pairing_public_url":"http://100.64.1.2:8766",
            "enable_autonomy_scheduler":False,"enable_night_guard":False}
        for key in ('reminders_path','app_state_path','audit_log_path','tasks_path','policy_state_path','notification_path',
                    'task_runs_path','health_connect_path','night_guard_state_path','care_status_path'):
            self.config[key]=str(Path(self.temp.name)/(key+'.json'))
        with patch.object(module.PhoneAgentPlugin,'_warm_device_route'),patch.object(module.PhoneAgentPlugin,'_start_pairing'):
            self.create_plugin()
        self.plugin._gateway=types.SimpleNamespace(server=True,close=AsyncMock())
        self.owner_headers={'Authorization':'Bearer synthetic-owner-jwt'}

    async def asyncTearDown(self):
        for plugin in self.plugins:await plugin.terminate()
        self.temp.cleanup()

    def pair(self,device='bot-a',kind='bot'):
        code=self.plugin._pairing.create(endpoint='http://100.64.1.2:8766',device_kind=kind,subject='owner',owner_token='synthetic-owner-jwt')
        data=self.plugin._pairing.redeem({'code':code['code'],'device_id':device,'device_kind':kind})
        return self.plugin._pairing.authenticate(data['device_token'])

    async def register(self,device='bot-a',kind='bot'):
        response=await self.post('/device/register',{'device_id':device,'device_kind':kind,'base_url':'http://127.0.0.1:8260','app_version':'0.5.0'})
        self.assertEqual(response.status_code,200,await response.get_data(as_text=True))

    def signed(self,principal,path,payload=None,method='POST',nonce='ab'*16,stamp=None,query=None):
        payload={} if payload is None else payload
        body=json.dumps(payload).encode() if method=='POST' else b''
        query={'device_id':principal['device_id']} if query is None else query
        stamp=str(int(time.time())) if stamp is None else str(stamp)
        body_hash=hashlib.sha256(body).hexdigest()
        proof=proof_function(self.plugin._pairing.upstream_token,principal['device_id'],principal['credential_id'],method,path,query,body_hash,stamp,nonce)
        headers={**self.owner_headers,'Content-Type':'application/json','X-Phone-Device-Principal':principal['device_id'],
            'X-Phone-Gateway-Time':stamp,'X-Phone-Gateway-Nonce':nonce,'X-Phone-Gateway-Credential':principal['credential_id'],
            'X-Phone-Gateway-Body':body_hash,'X-Phone-Gateway-Proof':proof}
        return body,headers,query

    async def scoped(self,principal,path,payload=None,method='POST',**kwargs):
        data,headers,query=self.signed(principal,path,payload,method,**kwargs)
        return await self.client.open('/astrbot_plugin_phone_agent'+path,method=method,query_string=query,data=data,headers=headers)

    async def test_owner_can_create_qr_without_returning_long_lived_secrets(self):
        response=await self.post('/app/pairing',{'device_kind':'bot'},headers=self.owner_headers)
        self.assertEqual(response.status_code,200)
        body=await response.get_json()
        self.assertEqual(body['pairing']['device_kind'],'bot')
        self.assertEqual(body['pairing']['endpoint'],'http://100.64.1.2:8766')
        self.assertNotIn('synthetic-owner-jwt',json.dumps(body))
        self.assertNotIn('shared_token',body['pairing'])
        self.assertAlmostEqual(body['pairing']['expires_at']-time.time(),120,delta=3)

    async def test_malformed_owner_pair_fields_return_400_and_no_pending_qr(self):
        for body in [{'device_kind':[]},{'device_kind':{}},{'replace_device_id':[]},{'device_kind':'unknown'}]:
            with self.subTest(body=body):
                response=await self.post('/app/pairing',body,headers=self.owner_headers)
                self.assertEqual(response.status_code,400)
        self.assertFalse(self.plugin._pairing.pending)

    async def test_signed_device_cannot_access_owner_pairing_controls(self):
        principal=self.pair()
        for path in ['/app/pairing','/app/pairing/revoke']:
            response=await self.scoped(principal,path,{'device_id':'bot-a'})
            self.assertEqual(response.status_code,403)

    async def test_valid_signed_get_scopes_state_and_unauthenticated_proof_fails(self):
        principal=self.pair();await self.register()
        response=await self.scoped(principal,'/app/state',method='GET')
        self.assertEqual(response.status_code,200,await response.get_data(as_text=True))
        body=await response.get_json();self.assertEqual(body['reminders'],[])
        bad=await self.client.get('/astrbot_plugin_phone_agent/app/state?device_id=bot-a',headers={'X-Phone-Device-Principal':'bot-a'})
        self.assertEqual(bad.status_code,403)

    async def test_replayed_expired_wrong_path_and_tampered_body_proofs_are_rejected(self):
        principal=self.pair();await self.register()
        payload={'device_id':'bot-a','requester_device_id':'bot-a','battery_percent':50}
        good=await self.scoped(principal,'/device/status',payload)
        self.assertEqual(good.status_code,200)
        replay=await self.scoped(principal,'/device/status',payload)
        self.assertEqual(replay.status_code,403)
        expired=await self.scoped(principal,'/device/status',payload,nonce='ac'*16,stamp=time.time()-60)
        self.assertEqual(expired.status_code,403)
        raw,headers,query=self.signed(principal,'/device/status',payload,nonce='ad'*16)
        tampered=await self.client.post('/astrbot_plugin_phone_agent/device/status',query_string=query,data=raw.replace(b'50',b'99'),headers=headers)
        self.assertEqual(tampered.status_code,403)
        wrong=await self.client.post('/astrbot_plugin_phone_agent/device/test',query_string=query,data=raw,headers=headers)
        self.assertEqual(wrong.status_code,403)

    async def test_replaced_or_revoked_device_proof_is_invalidated(self):
        principal=self.pair();await self.register()
        replacement=self.plugin._pairing.create(endpoint='http://100.64.1.2:8766',device_kind='bot',subject='owner',owner_token='synthetic-owner-jwt',replace_device_id='bot-a')
        self.plugin._pairing.redeem({'code':replacement['code'],'device_id':'bot-a','device_kind':'bot'},known_devices=['bot-a'])
        response=await self.scoped(principal,'/app/state',method='GET')
        self.assertEqual(response.status_code,403)
        current=self.plugin._pairing.principal('bot-a');self.plugin._pairing.revoke('bot-a')
        response=await self.scoped(current,'/app/state',method='GET',nonce='ae'*16)
        self.assertEqual(response.status_code,403)

    async def test_signed_bot_cannot_approve_another_device_or_subject_task(self):
        principal=self.pair();await self.register()
        for device,subject in [('personal-a','owner'),('bot-a','someone-else')]:
            run=module.PhoneTaskRun.new('synthetic',subject=subject,device_id=device)
            approval=module.ApprovalRequest.create(run_id=run.run_id,subject=subject,device_id=device,actions=['open_app'],packages=['tv.danmaku.bili'])
            self.plugin._task_runs.add_run(run);self.plugin._task_runs.add_approval(approval)
            response=await self.scoped(principal,'/app/task/approve',{'device_id':'bot-a','approval_id':approval.approval_id},nonce=('aa' if device=='personal-a' else 'bb')*16)
            self.assertEqual(response.status_code,403)
            self.assertTrue(approval.pending())

    async def test_bot_is_denied_personal_legacy_routes_and_personal_request_keeps_scope(self):
        bot=self.pair();await self.register()
        for index,path in enumerate(['/app_policy','/sleep_mode','/reminders','/app/care/preview']):
            response=await self.scoped(bot,path,{'device_id':'bot-a'},nonce=f'{index:032x}')
            self.assertEqual(response.status_code,403)
        personal=self.pair('personal-a','personal');await self.register('personal-a','personal')
        response=await self.scoped(personal,'/reminders',method='GET',nonce='ee'*16)
        self.assertEqual(response.status_code,200)

    async def test_owner_revocation_cancels_run_and_removes_grants_and_pending_approval(self):
        self.pair();await self.register()
        run=module.PhoneTaskRun.new('synthetic',subject='owner',device_id='bot-a');run.transition('waiting_approval')
        approval=module.ApprovalRequest.create(run_id=run.run_id,subject='owner',device_id='bot-a',actions=['open_app'],packages=['tv.danmaku.bili'])
        grant=module.ScopedGrant.issue(subject='owner',device_id='bot-a',actions=['open_app'],packages=['tv.danmaku.bili'],run_id=run.run_id)
        self.plugin._task_runs.add_run(run);self.plugin._task_runs.add_approval(approval);self.plugin._task_runs.add_grant(grant)
        response=await self.post('/app/pairing/revoke',{'device_id':'bot-a'},headers=self.owner_headers)
        self.assertEqual(response.status_code,200)
        self.assertTrue(run.cancel_requested);self.assertFalse(approval.pending())
        self.assertIsNone(self.plugin._task_runs.grant(grant.grant_id))

    async def test_runtime_pairing_disable_blocks_new_codes_and_scoped_requests(self):
        principal=self.pair();await self.register();self.config['enable_device_pairing']=False
        self.assertEqual((await self.post('/app/pairing',{'device_kind':'bot'},headers=self.owner_headers)).status_code,403)
        self.assertEqual((await self.scoped(principal,'/app/state',method='GET')).status_code,403)

    async def test_signed_personal_health_upload_keeps_body_available_after_proof_validation(self):
        from test_health_connect import payload
        principal=self.pair('personal-a','personal');await self.register('personal-a','personal')
        self.config['enable_health_tools']=True
        body=payload(device_id='personal-a')
        body['requester_device_id']='personal-a'
        response=await self.scoped(principal,'/app/health/snapshot',body,nonce='ff'*16)
        self.assertEqual(response.status_code,200,await response.get_data(as_text=True))
        self.assertTrue((await response.get_json())['success'])


if __name__=='__main__':unittest.main()
