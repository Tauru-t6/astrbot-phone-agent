import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import io
import hashlib
import time

package = types.ModuleType("pairing_test_package")
package.__path__ = [str(Path(__file__).parents[1])]
with patch.dict(sys.modules, {package.__name__: package}):
    pairing = importlib.import_module(package.__name__ + ".pairing")


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"pairing.json"
        self.store = pairing.PairingStore(self.path)

    def code(self, **kw):
        return self.store.create(endpoint="http://100.64.1.2:8766", device_kind="bot", subject="owner", owner_token="synthetic-owner-jwt", now=1000, **kw)

    def redeem(self, code, **kw):
        return self.store.redeem({"code":code["code"],"device_id":"bot-a","device_kind":"bot"},now=1001,**kw)

    def test_qr_contains_only_ephemeral_code_and_redemption_is_single_use(self):
        code=self.code()
        self.assertNotIn("synthetic-owner-jwt",json.dumps(code))
        result=self.redeem(code)
        principal=self.store.authenticate(result["device_token"])
        self.assertEqual(principal["device_id"],"bot-a")
        self.assertEqual(self.store.credential("bot-a"),(True,result["shared_token"]))
        with self.assertRaises(ValueError): self.redeem(code)
        self.assertNotIn(result["device_token"],self.path.read_text())
        self.assertNotIn(result["shared_token"],json.dumps(self.store.public()))

    def test_expired_or_wrong_role_cannot_redeem(self):
        code=self.code()
        with self.assertRaises(ValueError):self.store.redeem({"code":code["code"],"device_id":"bot-a","device_kind":"personal"},now=1001)
        with self.assertRaises(ValueError):self.store.redeem({"code":code["code"],"device_id":"bot-a","device_kind":"bot"},now=1121)

    def test_existing_device_requires_explicit_owner_replacement_scope(self):
        code=self.code()
        with self.assertRaises(ValueError):self.redeem(code,known_devices=["bot-a"])
        replacement=self.code(replace_device_id="bot-a")
        self.assertTrue(self.redeem(replacement,known_devices=["bot-a"])["success"])

    def test_revoke_persists_and_never_uses_shared_legacy_secret(self):
        result=self.redeem(self.code());self.store.revoke("bot-a")
        reloaded=pairing.PairingStore(self.path)
        self.assertTrue(reloaded.authenticate(result["device_token"])["revoked"])
        self.assertEqual(reloaded.credential("bot-a"),(True,""))

    def test_failed_write_does_not_consume_pair_code_or_change_device(self):
        code=self.code()
        with patch.object(self.store,"_save",side_effect=OSError()):
            with self.assertRaises(OSError):self.redeem(code)
        self.assertEqual(self.store.public(),[])
        self.assertTrue(self.redeem(code)["success"])

    def test_invalid_storage_fails_closed(self):
        self.path.write_text('{"version":1,"devices":{"bad":{"token_hash":[]}}}')
        corrupt=pairing.PairingStore(self.path)
        self.assertFalse(corrupt.valid)
        with self.assertRaises(ValueError):corrupt.create(endpoint="https://example.com",device_kind="bot",subject="owner",owner_token="x")

    def test_plain_http_restricted_to_private_literal_addresses(self):
        for value in ["http://example.com","http://8.8.8.8","https://user:pass@example.com","https://example.com?token=x","https://example.com/path"]:
            with self.subTest(value=value),self.assertRaises(ValueError):pairing.safe_endpoint(value)
        for value in ["http://100.64.1.2:8766","http://192.168.1.2:8766","https://example.com"]:
            self.assertEqual(pairing.safe_endpoint(value),value)

    def test_replacement_invalidates_captured_principal_and_old_token(self):
        initial=self.redeem(self.code());principal=self.store.authenticate(initial['device_token'])
        replacement=self.redeem(self.code(replace_device_id='bot-a'),known_devices=['bot-a'])
        self.assertFalse(self.store.principal_active(principal))
        self.assertIsNone(self.store.authenticate(initial['device_token']))
        self.assertNotEqual(initial['shared_token'],replacement['shared_token'])

    def test_malformed_fields_and_corrupt_store_never_raise_unhandled_type_errors(self):
        for value in [[],{},None,True]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.store.create(endpoint='https://example.com',device_kind=value,subject='owner',owner_token='synthetic')
        self.path.write_text('{"version":true,"devices":{},"upstream_token":""}')
        corrupt=pairing.PairingStore(self.path)
        self.assertFalse(corrupt.valid)
        self.assertEqual(corrupt.credential('legacy-id'),(True,''))

    def test_revoke_also_invalidates_outstanding_replacement_code(self):
        self.redeem(self.code())
        replacement=self.code(replace_device_id='bot-a')
        self.store.revoke('bot-a')
        with self.assertRaises(ValueError):self.redeem(replacement)

    def test_strict_json_rejects_duplicate_nan_deep_and_surrogate_values(self):
        for raw in ['{"x":NaN}','{"x":1,"x":2}','{"x":"\\ud800"}','{"x":'+('[ '*26)+'0'+(' ]'*26)+'}']:
            with self.subTest(raw=raw),self.assertRaises(ValueError):pairing.strict_json(raw)

    def test_pairing_code_expiring_before_commit_cannot_rotate_device(self):
        code=self.code()
        with patch.object(pairing.time,'time',side_effect=[1119,1121]):
            with self.assertRaisesRegex(ValueError,'expired'):
                self.store.redeem({'code':code['code'],'device_id':'bot-a','device_kind':'bot'})
        self.assertEqual(self.store.public(),[])


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=pairing.PairingStore(Path(self.temp.name)/"pair.json")
        self.gateway=pairing.DeviceGateway(self.store,host="127.0.0.1",port=0,
            upstream="http://127.0.0.1:6185/api/plug/astrbot_plugin_phone_agent",authorize=lambda *a:True,known_devices=lambda:[])
        await self.gateway.start()
        self.port=self.gateway.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.gateway.close();self.temp.cleanup()

    async def request(self,path,body=None,token="",extra=""):
        data=json.dumps(body or {}).encode()
        r,w=await asyncio.open_connection("127.0.0.1",self.port)
        w.write(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(data)}\r\n{extra}\r\n".encode()+data)
        await w.drain();raw=await r.read();w.close();await w.wait_closed()
        h,b=raw.split(b"\r\n\r\n",1)
        return int(h.split(b" ")[1]),json.loads(b)

    def paired(self,kind='bot'):
        code=self.store.create(endpoint='http://127.0.0.1:8766',device_kind=kind,subject='owner',owner_token='synthetic')
        return self.store.redeem({'code':code['code'],'device_id':'bot-a','device_kind':kind})

    async def raw_request(self,data):
        r,w=await asyncio.open_connection('127.0.0.1',self.port)
        w.write(data);await w.drain();raw=await asyncio.wait_for(r.read(),2)
        w.close();await w.wait_closed()
        head,body=raw.split(b'\r\n\r\n',1)
        return int(head.split(b' ')[1]),json.loads(body)

    async def test_unauthorized_non_whitelist_health_and_mismatched_device_are_rejected(self):
        code=self.store.create(endpoint="http://127.0.0.1:8766",device_kind="bot",subject="owner",owner_token="synthetic")
        status,result=await self.request('/pair/redeem',{"code":code["code"],"device_id":"bot-a","device_kind":"bot"})
        self.assertEqual(status,200);token=result["device_token"]
        for path,body in [("/device-api/config",{}),("/device-api/app/health/snapshot",{}),("/device-api/device/register",{"device_id":"personal-a"})]:
            with self.subTest(path=path):self.assertEqual((await self.request(path,body,token))[0],403)
        self.assertEqual((await self.request('/device-api/app/state'))[0],401)
        self.store.revoke('bot-a')
        self.assertEqual((await self.request('/device-api/device/status',{},token))[1]['error_code'],'device_revoked')

    async def test_duplicate_length_and_browser_origin_rejected_before_redemption(self):
        for extra in ['Content-Length: 2\r\n','Origin: https://untrusted.example\r\n']:
            self.assertEqual((await self.request('/pair/redeem',extra=extra))[0],400)

    async def test_malformed_http_and_nonfinite_json_never_forward(self):
        paired=self.paired();token=paired['device_token']
        for raw in [b'{"x":NaN}',b'{"x":1,"x":2}',b'{"x":"\\ud800"}']:
            request=f'POST /device-api/device/status HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(raw)}\r\n\r\n'.encode()+raw
            with self.subTest(raw=raw),patch.object(pairing,'open_http',side_effect=AssertionError('must not forward')):
                self.assertEqual((await self.raw_request(request))[0],400)
        for extra in ['Content-Length: -1','Transfer-Encoding: chunked','Host : localhost','Expect: 100-continue']:
            with self.subTest(extra=extra):
                req=f'POST /pair/redeem HTTP/1.1\r\nHost: localhost\r\n{extra}\r\n\r\n'.encode()
                self.assertEqual((await self.raw_request(req))[0],400)

    async def test_rate_limit_is_bounded_for_failed_pairing_attempts(self):
        for _ in range(10):self.assertEqual((await self.request('/pair/redeem',{'code':'bad'}))[0],400)
        self.assertEqual((await self.request('/pair/redeem',{'code':'bad'}))[0],429)
        self.assertLessEqual(len(self.gateway.claim_attempts),512)

    async def test_forwarded_request_is_self_scoped_signed_and_secrets_are_removed(self):
        pair=self.paired();principal=self.store.authenticate(pair['device_token']);captured=[]
        class Response(io.BytesIO):
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        def forward(req,timeout):
            captured.append(req)
            return Response(json.dumps({'success':True,'app_shared_token':'never-forward','nested':{'plugin_jwt':'secret'},
                'task_runs':[{'device_id':'other','goal':'private'},{'device_id':'bot-a','goal':'mine'}],
                'care':{'private':True},'reminders':[{'text':'private'}]}).encode())
        with patch.object(pairing,'open_http',side_effect=forward):
            status,body=await self.gateway.dispatch(principal,'GET','/device-api/app/state',{})
        self.assertEqual(status,200);self.assertNotIn('app_shared_token',body)
        self.assertEqual(body['nested'],{});self.assertEqual(body['care'],{})
        self.assertEqual(body['task_runs'],[{'device_id':'bot-a','goal':'mine'}])
        req=captured[0];headers={k.lower():v for k,v in req.header_items()}
        self.assertIn('device_id=bot-a',req.full_url)
        proof=pairing.gateway_proof('synthetic','bot-a',principal['credential_id'],'GET','/app/state',{'device_id':'bot-a'},
            hashlib.sha256(b'').hexdigest(),headers['x-phone-gateway-time'],headers['x-phone-gateway-nonce'])
        self.assertEqual(headers['x-phone-gateway-proof'],proof)

    async def test_revoke_or_replace_while_response_inflight_discards_private_response(self):
        pair=self.paired();principal=self.store.authenticate(pair['device_token'])
        class Response(io.BytesIO):
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        def revoke(req,timeout):
            self.store.revoke('bot-a')
            return Response(b'{"success":true,"private":"discard"}')
        with patch.object(pairing,'open_http',side_effect=revoke):
            status,body=await self.gateway.dispatch(principal,'GET','/device-api/app/state',{})
        self.assertEqual(status,403);self.assertNotIn('private',body)

    async def test_personal_legacy_routes_forward_self_scope_and_bot_cannot_reach_them(self):
        pair=self.paired();principal=self.store.authenticate(pair['device_token'])
        for path in ['/app_policy','/sleep_mode','/reminders','/app/care/preview']:
            with self.subTest(path=path):
                self.assertEqual((await self.gateway.dispatch(principal,'POST','/device-api'+path,{}))[0],403)
        self.assertEqual((await self.gateway.dispatch(principal,'GET','/device-api/app/state?device_id=other',{}))[0],403)
        self.assertEqual((await self.gateway.dispatch(principal,'GET','/device-api/app/state?device_id=bot-a&device_id=bot-a',{}))[0],400)

    async def test_gateway_disabling_prevents_redemption_and_forwarding(self):
        pair=self.paired();principal=self.store.authenticate(pair['device_token'])
        self.gateway.enabled=lambda:False
        self.assertEqual((await self.request('/pair/redeem',{}))[0],403)
        code,body=await self.gateway.dispatch(principal,'GET','/device-api/app/state',{})
        self.assertEqual(code,503)
        self.assertEqual(body['error_code'],'gateway_unavailable')
        self.assertTrue(self.store.principal_active(principal))

    async def test_gateway_reload_does_not_report_credential_revocation(self):
        pair=self.paired();principal=self.store.authenticate(pair['device_token'])
        self.gateway.closing=True
        code,body=await self.gateway.dispatch(principal,'GET','/device-api/app/state',{})
        self.assertEqual(code,503)
        self.assertNotEqual(body['error_code'],'device_revoked')
        self.assertTrue(self.store.principal_active(principal))

if __name__=='__main__':unittest.main()
