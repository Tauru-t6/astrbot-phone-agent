"""Owner pairing controls and device-scoped forwarding authorization."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import base64
import io
import json
import functools
import re
import urllib.parse

from astrbot.api import logger
from quart import jsonify, request, g
from .pairing import PairingStore, DeviceGateway, PROOF_HEADERS, canonical_query, gateway_proof, strict_json, MAX_BODY, valid_device_id, GET_PATHS, POST_PATHS, PERSONAL_PATHS
from .device_app import plugin_data_path


class PairingMixin:
    def _init_pairing(self):
        path = plugin_data_path(self.config.get("app_state_path"), "phone_agent_app_state.json").with_name("phone_agent_pairing.json")
        self._pairing = PairingStore(path)
        self.device_app.token_lookup = self._pairing.credential
        self._devices.token_lookup = self._pairing.credential
        self._gateway = None
        self._gateway_start = None
        self._gateway_error = ""
        self._gateway_nonces = {}

    def _pairing_route_guard(self, handler):
        @functools.wraps(handler)
        async def guarded():
            if not any(request.headers.get(key) for key in PROOF_HEADERS):
                return await handler()
            denied = {"success": False, "error_code": "invalid_device_proof"}
            try:
                device = request.headers.get("X-Phone-Device-Principal", "")
                proof = request.headers.get("X-Phone-Gateway-Proof", "")
                stamp = request.headers.get("X-Phone-Gateway-Time", "")
                nonce = request.headers.get("X-Phone-Gateway-Nonce", "")
                credential = request.headers.get("X-Phone-Gateway-Credential", "")
                claimed_body = request.headers.get("X-Phone-Gateway-Body", "")
                if (not valid_device_id(device) or not re.fullmatch(r"[0-9]{1,12}", stamp)
                        or not re.fullmatch(r"[a-f0-9]{32}", nonce) or not re.fullmatch(r"[a-f0-9]{64}", credential)
                        or not re.fullmatch(r"[a-f0-9]{64}", claimed_body) or not re.fullmatch(r"[a-f0-9]{64}", proof)
                        or abs(time.time()-int(stamp)) > 30):
                    return jsonify(denied), 403
                raw = await request.get_data()
                if len(raw) > MAX_BODY or hashlib.sha256(raw).hexdigest() != claimed_body:
                    return jsonify(denied), 403
                payload = strict_json(raw) if raw else {}
                if not isinstance(payload, dict): return jsonify(denied), 403
                query_raw = request.query_string
                if isinstance(query_raw, bytes): query_raw = query_raw.decode("ascii")
                query = canonical_query(query_raw)
                marker = "/astrbot_plugin_phone_agent"
                path = request.path.split(marker, 1)[-1]
                principal = self._pairing.principal(device)
                if not principal or principal["credential_id"] != credential or not self._pairing.principal_active(principal):
                    return jsonify({"success": False, "error_code": "device_revoked"}), 403
                expected = gateway_proof(self._pairing.upstream_token, device, credential, request.method,
                                         path, query, claimed_body, stamp, nonce)
                if not self._pairing.upstream_token or not hmac.compare_digest(proof, expected):
                    return jsonify(denied), 403
                if query.get("device_id") != device or (request.method == "POST" and payload.get("device_id") != device):
                    return jsonify(denied), 403
                now = time.monotonic()
                self._gateway_nonces = {key: until for key, until in self._gateway_nonces.items() if until > now}
                nonce_key = (device, nonce)
                if nonce_key in self._gateway_nonces or len(self._gateway_nonces) >= 4096:
                    return jsonify(denied), 403
                if self._gateway_authorize(principal, request.method, path, payload) is not True:
                    return jsonify({"success": False, "error_code": "forbidden"}), 403
                self._gateway_nonces[nonce_key] = now + 60
                g.phone_device_principal = principal
                g.phone_verified_body = raw
            except (ValueError, TypeError, UnicodeError, AttributeError):
                return jsonify(denied), 403
            return await handler()
        return guarded

    def _pairing_routes(self):
        return [
            ("/app/pairing", self._web_pairing, ["GET", "POST"], "One-time owner pairing", None),
            ("/app/pairing/revoke", self._web_pairing_revoke, ["POST"], "Revoke a paired device", None),
            ("/app/diagnostics", self._web_diagnostics, ["GET"], "Device connection diagnostics", None),
        ]

    def _start_pairing(self):
        if not self._bool_config("enable_device_pairing", False): return
        async def start():
            try:
                self._gateway = DeviceGateway(self._pairing,
                    host=str(self.config.get("pairing_bind_host") or "127.0.0.1"),
                    port=int(self.config.get("pairing_port") or 8766),
                    upstream=str(self.config.get("pairing_upstream_url") or "http://127.0.0.1:6185/api/plug/astrbot_plugin_phone_agent"),
                    authorize=self._gateway_authorize, known_devices=lambda: tuple(self._devices.rows),
                    enabled=lambda:self._enabled() and self._bool_config("enable_device_pairing",False) and not self._terminating)
                await self._gateway.start()
            except Exception:
                self._gateway_error = "gateway_start_failed"
                logger.warning("Phone Control device pairing listener could not start")
        try: self._gateway_start = asyncio.create_task(start())
        except RuntimeError: pass

    async def _stop_pairing(self):
        if self._gateway_start:
            await asyncio.gather(self._gateway_start, return_exceptions=True)
        if self._gateway: await self._gateway.close()

    def _routine_principal(self):
        device = request.headers.get("X-Phone-Device-Principal", "")
        if any(request.headers.get(key) for key in PROOF_HEADERS):
            principal = getattr(g, "phone_device_principal", None)
            if not principal or principal["device_id"] != device or not self._pairing.principal_active(principal):
                return {"subject": "", "device_id": device, "admin": False}
            return {"subject": principal["subject"], "device_id": device, "admin": False}
        return {"subject": next(iter(sorted(self._allowed_users())), ""), "device_id": "", "admin": True}

    def _gateway_authorize(self, principal, method, path, payload):
        if not self._enabled() or self._terminating or not self._bool_config("enable_device_pairing",False): return "disabled"
        if not self._pairing.principal_active(principal): return "device_revoked"
        if method not in {"GET","POST"} or path not in (GET_PATHS if method=="GET" else POST_PATHS):return "forbidden"
        if principal["device_kind"]=="bot" and path in PERSONAL_PATHS:return "personal_device_required"
        if principal["subject"] not in self._allowed_users(): return "unauthorized"
        device = principal["device_id"]
        row=self._devices.rows.get(device)
        if row and row["device_kind"]!=principal["device_kind"]:return "device_kind_mismatch"
        if path == "/device/register":
            # Credentials are not transferable across App roles.
            row = self._devices.rows.get(device)
            if row and row["device_kind"] != principal["device_kind"]: return "device_kind_mismatch"
        if path in {"/app/task/approve", "/app/task/reject"}:
            approval = self._task_runs.approval(str(payload.get("approval_id", "")))
            if not approval: return "not_found"
            run = self._task_runs.run(approval.run_id)
            if run is None or run.device_id != device or run.subject != principal["subject"]: return "forbidden"
        if path in {"/app/task/cancel", "/app/task/resume"}:
            run = self._task_runs.run(str(payload.get("run_id", "")))
            if run is None or run.device_id != device or run.subject != principal["subject"]: return "forbidden"
        if path == "/app/ui-task/propose" and payload.get("notify", False):
            # Chat notifications require an actual captured chat session.
            return "private_session_required"
        return True

    async def _web_pairing(self):
        if not self._routine_principal().get("admin"):
            return jsonify({"success": False, "error_code": "forbidden"}), 403
        if request.method == "GET":
            return jsonify({"success": True, "enabled": self._bool_config("enable_device_pairing", False),
                "ready": bool(self._gateway and self._gateway.server), "error_code": self._gateway_error,
                "public_url": str(self.config.get("pairing_public_url") or ""), "devices": self._pairing.public()})
        if not self._enabled() or not self._web_write_allowed() or not self._bool_config("enable_device_pairing",False):
            return jsonify({"success": False, "error_code": "forbidden"}), 403
        if not self._gateway or not self._gateway.server:
            return jsonify({"success": False, "error_code": "pairing_gateway_disabled"}), 409
        raw = await request.get_data()
        try:payload = strict_json(raw) if len(raw)<=8192 else None
        except ValueError:payload=None
        if not isinstance(payload, dict): return jsonify({"success": False, "error_code": "invalid_json"}), 400
        authorization=request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token: return jsonify({"success": False, "error_code": "owner_reauthentication_required"}), 403
        subject = next(iter(sorted(self._allowed_users())), "")
        replace = payload.get("replace_device_id", "")
        kind = payload.get("device_kind", "bot")
        if not isinstance(replace,str) or not isinstance(kind,str) or kind not in {"bot","personal"}:
            return jsonify({"success": False, "error_code": "invalid_pairing_scope"}), 400
        if replace and (replace not in self._devices.rows or self._devices.rows[replace]["device_kind"] != kind):
            return jsonify({"success": False, "error_code": "device_kind_mismatch"}), 409
        try:
            code = self._pairing.create(endpoint=str(self.config.get("pairing_public_url") or ""), device_kind=kind,
                subject=subject, owner_token=token, replace_device_id=replace)
        except ValueError as exc: return jsonify({"success": False, "error_code": str(exc)}), 400
        except OSError: return jsonify({"success": False, "error_code": "storage_unavailable"}), 503
        svg = ""
        try:
            import qrcode
            import qrcode.image.svg
            image = qrcode.make(json.dumps(code, separators=(",", ":")), image_factory=qrcode.image.svg.SvgPathImage)
            stream = io.BytesIO(); image.save(stream)
            svg = "data:image/svg+xml;base64," + base64.b64encode(stream.getvalue()).decode()
        except (ImportError,ValueError,OverflowError): pass
        return jsonify({"success": True, "pairing": code, "qr_image": svg})

    async def _web_pairing_revoke(self):
        if not self._routine_principal().get("admin") or not self._web_write_allowed():
            return jsonify({"success": False, "error_code": "forbidden"}), 403
        raw=await request.get_data()
        try:payload=strict_json(raw) if len(raw)<=8192 else None
        except ValueError:payload=None
        device = payload.get("device_id") if isinstance(payload, dict) else ""
        try: self._pairing.revoke(device)
        except (TypeError, ValueError): return jsonify({"success": False, "error_code": "unknown_device"}), 404
        except OSError: return jsonify({"success": False, "error_code": "storage_unavailable"}), 503
        for item in self._task_runs.runs(limit=50):
            run = self._task_runs.run(item["run_id"])
            if run.device_id == device and run.status in {"executing", "verifying", "waiting_approval"}:
                run.cancel_requested = True
                handle=self._ui_handles.get(run.run_id)
                if handle:handle.cancel()
        for item in self._task_runs.approvals(limit=50):
            if item.get("device_id")==device:
                approval=self._task_runs.approval(item.get("approval_id",""))
                if approval:approval.decide(False,reason="device_revoked")
        for item in self._task_runs.grants():
            if item.get("device_id")==device:self._task_runs.revoke_grant(item["grant_id"])
        self._task_runs.save()
        return jsonify({"success": True, "device_id": device, "revoked": True})

    async def _web_diagnostics(self):
        device = request.args.get("device_id") or self._registered_device_id()
        principal=self._routine_principal()
        if not principal.get("admin") and principal.get("device_id")!=device:
            return jsonify({"success": False, "error_code": "device_mismatch"}),403
        backend = self._devices.backend(device)
        if backend is None: return jsonify({"success": False, "error_code": "unknown_device"}), 404
        status = backend.last_status() or {}
        received = status.get("received_at")
        age = max(0, time.time() - received) if isinstance(received, (float, int)) else None
        paired, secret = self._pairing.credential(device)
        checks = [
            {"id": "server", "status": "ok", "message": "服务器已响应"},
            {"id": "registration", "status": "ok" if backend.registration() else "error", "message": "设备已注册" if backend.registration() else "等待设备注册"},
            {"id": "heartbeat", "status": "ok" if age is not None and age <= 120 else "warning", "message": "心跳正常" if age is not None and age <= 120 else "心跳过期或尚未收到"},
            {"id": "shizuku", "status": "ok" if status.get("shizuku_ready") is True else "warning", "message": "Shizuku 已就绪" if status.get("shizuku_ready") is True else "请在执行设备启动并授权 Shizuku"},
            {"id": "credential", "status": "error" if paired and not secret else "ok", "message": "设备凭据已撤销" if paired and not secret else "设备专用凭据" if paired else "传统连接凭据"},
        ]
        return jsonify({"success": True, "device_id": device, "checks": checks, "heartbeat_age_seconds": age,
                        "app_version": (backend.registration() or {}).get("app_version", ""), "role": self._devices.role(device)})
