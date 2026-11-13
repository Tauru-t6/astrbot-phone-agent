"""One-time pairing and revocable device credentials, confined to plugin APIs."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .http_transport import open_http

DEVICE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
MAX_BODY = 256 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
PROOF_HEADERS = ("X-Phone-Device-Principal", "X-Phone-Gateway-Proof", "X-Phone-Gateway-Time",
                 "X-Phone-Gateway-Nonce", "X-Phone-Gateway-Credential", "X-Phone-Gateway-Body")


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("duplicate_json_key")
            value[key] = item
        return value
    def invalid(_): raise ValueError("invalid_json")
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
        def check(item, depth=0):
            if depth > 24: raise ValueError("json_too_deep")
            if isinstance(item, float) and not math.isfinite(item): raise ValueError("invalid_json")
            if isinstance(item, dict):
                for key, val in item.items(): check(key, depth+1); check(val, depth+1)
            elif isinstance(item, list):
                for val in item: check(val, depth+1)
            elif isinstance(item, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in item): raise ValueError("invalid_json")
        check(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_json") from None


def canonical_query(raw):
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                                     encoding="utf-8", errors="strict", max_num_fields=20) if raw else []
    except (ValueError, UnicodeError): raise ValueError("invalid_query") from None
    values = {}
    for key, value in pairs:
        if key in values or not key or len(key) > 80 or len(value) > 1000: raise ValueError("invalid_query")
        if any(ord(char) < 32 for char in key+value): raise ValueError("invalid_query")
        values[key] = value
    return values


def gateway_proof(token, device, credential, method, path, query, body_hash, stamp, nonce):
    encoded = urllib.parse.urlencode(sorted(query.items()))
    material = "\n".join((device, credential, method, path, encoded, body_hash, stamp, nonce))
    return hmac.new(token.encode(), material.encode(), hashlib.sha256).hexdigest()


def valid_device_id(value):
    return isinstance(value, str) and 1 <= len(value) <= 64 and set(value) <= DEVICE_ID_CHARS


def safe_endpoint(value):
    """Require TLS except on a literal local/LAN/Tailscale address."""
    if (not isinstance(value, str) or len(value) > 500 or value != value.strip()
            or any(ord(char)<33 or ord(char)==127 for char in value)): raise ValueError("invalid_pairing_endpoint")
    try: p = urllib.parse.urlsplit(value)
    except ValueError: raise ValueError("invalid_pairing_endpoint") from None
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {"", "/"}:
        raise ValueError("invalid_pairing_endpoint")
    try:
        if p.port == 0: raise ValueError()
        if p.scheme == "http":
            address = ipaddress.ip_address(p.hostname)
            networks = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7")
            if not any(address in ipaddress.ip_network(n) for n in networks): raise ValueError()
    except ValueError:
        raise ValueError("pairing_requires_tls_or_private_address") from None
    return value.rstrip("/")


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class PairingStore:
    MAX_DEVICES = 32
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.pending = {}
        self.devices = {}
        self.upstream_token = ""
        self.valid = True
        try:
            if self.path.stat().st_size > 128 * 1024: raise ValueError()
            raw = strict_json(self.path.read_text(encoding="utf-8"))
            if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw.get("version") != 1
                    or not isinstance(raw.get("devices"), dict) or len(raw["devices"]) > self.MAX_DEVICES): raise ValueError()
            for device, row in raw["devices"].items():
                if (not valid_device_id(device) or not isinstance(row, dict) or not isinstance(row.get("device_kind"), str) or row["device_kind"] not in {"personal", "bot"}
                        or not isinstance(row.get("token_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["token_hash"])
                        or not isinstance(row.get("shared_token"), str) or not 32 <= len(row["shared_token"]) <= 100
                        or not isinstance(row.get("subject"), str) or not 1 <= len(row["subject"]) <= 120
                        or type(row.get("revoked")) is not bool or type(row.get("created_at")) not in {int, float}
                        or not math.isfinite(row["created_at"]) or row["created_at"] <= 0):
                    raise ValueError()
            if not isinstance(raw.get("upstream_token", ""), str) or len(raw.get("upstream_token", "")) > 16384: raise ValueError()
            self.devices = raw["devices"]
            self.upstream_token = raw.get("upstream_token") or ""
        except FileNotFoundError: pass
        except (OSError, ValueError, TypeError, UnicodeError, OverflowError): self.valid = False

    def _save(self, devices=None, token=None):
        value={"version":1,"devices":self.devices if devices is None else devices,
               "upstream_token":self.upstream_token if token is None else token}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+secrets.token_hex(8)+'.tmp')
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(value,stream,ensure_ascii=False,allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, *, endpoint, device_kind, subject, owner_token, replace_device_id="", now=None):
        if not self.valid: raise ValueError("pairing_storage_invalid")
        endpoint = safe_endpoint(endpoint)
        if (not isinstance(device_kind, str) or device_kind not in {"bot", "personal"}
                or not isinstance(subject, str) or not 1 <= len(subject) <= 120
                or not isinstance(owner_token, str) or not 1 <= len(owner_token) <= 16384
                or any(ord(char) < 32 or ord(char) == 127 for char in subject+owner_token)):
            raise ValueError("invalid_pairing_scope")
        if not isinstance(replace_device_id, str) or replace_device_id and not valid_device_id(replace_device_id): raise ValueError("invalid_device_id")
        now = time.time() if now is None else now
        if type(now) not in {int, float} or not math.isfinite(now): raise ValueError("invalid_pairing_scope")
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v["expires_at"] > now}
            if len(self.pending) >= 8: raise ValueError("pairing_limit")
            existing = self.devices.get(replace_device_id)
            if existing and existing["device_kind"] != device_kind: raise ValueError("device_kind_mismatch")
            self._save(token=owner_token)
            self.upstream_token = owner_token
            code = "pcp_" + secrets.token_urlsafe(32)
            self.pending[_digest(code)] = {"subject": subject, "device_kind": device_kind,
                "endpoint": endpoint, "replace_device_id": replace_device_id, "expires_at": now + 120}
            return {"type": "phone_control_pairing", "version": 1, "endpoint": endpoint,
                "code": code, "expires_at": now + 120, "device_kind": device_kind}

    def redeem(self, payload, *, known_devices=(), now=None):
        if not self.valid: raise ValueError("pairing_storage_invalid")
        if not isinstance(payload, dict): raise ValueError("invalid_json")
        code, device, kind = payload.get("code"), payload.get("device_id"), payload.get("device_kind")
        if (not isinstance(code, str) or not re.fullmatch(r"pcp_[A-Za-z0-9_-]{43}", code) or not valid_device_id(device)
                or not isinstance(kind, str) or kind not in {"bot", "personal"}): raise ValueError("invalid_pairing_code")
        injected_now = now is not None
        now = time.time() if now is None else now
        if type(now) not in {int,float} or not math.isfinite(now): raise ValueError("invalid_pairing_code")
        with self.lock:
            key = _digest(code)
            pending = self.pending.get(key)
            if pending is None or pending["expires_at"] <= now: raise ValueError("pair_expired_or_used")
            if pending["device_kind"] != kind: raise ValueError("device_kind_mismatch")
            replacing = pending["replace_device_id"]
            if replacing and replacing != device: raise ValueError("device_mismatch")
            if not replacing and (device in self.devices or device in known_devices): raise ValueError("device_already_exists")
            if device not in self.devices and len(self.devices) >= self.MAX_DEVICES: raise ValueError("pairing_limit")
            token = "pcd_" + secrets.token_urlsafe(32)
            shared = secrets.token_urlsafe(32)
            row = {"device_kind": kind, "subject": pending["subject"], "token_hash": _digest(token),
                "shared_token": shared, "created_at": now, "revoked": False}
            devices = {**self.devices, device: row}
            # Validate immediately before durable commit, not only on receipt.
            # Tests inject a clock; live requests use a new wall-clock sample.
            if pending["expires_at"] <= (now if injected_now else time.time()): raise ValueError("pair_expired_or_used")
            self._save(devices=devices)
            self.devices = devices
            self.pending.pop(key)
            return {"success": True, "plugin_base_url": pending["endpoint"] + "/device-api",
                "device_token": token, "shared_token": shared, "relay_base_url": "", "relay_token": "",
                "device_id": device, "device_kind": kind}

    def authenticate(self, token):
        if not self.valid or not isinstance(token, str) or not token.startswith("pcd_") or len(token) > 100: return None
        digest = _digest(token)
        with self.lock:
            for device, row in self.devices.items():
                if hmac.compare_digest(row["token_hash"], digest):
                    return {"device_id": device, "device_kind": row["device_kind"], "subject": row["subject"], "credential_id": row["token_hash"],
                            "revoked": row.get("revoked") is True}
        return None

    def principal(self, device):
        with self.lock:
            if not valid_device_id(device): return None
            row = self.devices.get(device) if self.valid else None
            if row is None: return None
            return {"device_id": device, "device_kind": row["device_kind"], "subject": row["subject"],
                    "credential_id": row["token_hash"], "revoked": row.get("revoked") is True}

    def principal_active(self, principal):
        if not isinstance(principal, dict): return False
        current = self.principal(principal.get("device_id"))
        return bool(current and not current["revoked"] and current == principal)

    def credential_id(self, device):
        """Opaque generation for invalidating in-flight grants after re-pairing."""
        current=self.principal(device)
        return current["credential_id"] if current and not current["revoked"] else ""

    def credential(self, device):
        with self.lock:
            if not self.valid: return True, ""
            if not valid_device_id(device): return False, ""
            row = self.devices.get(device)
            return (False, "") if row is None else (True, "" if row.get("revoked") else row["shared_token"])

    def revoke(self, device):
        with self.lock:
            if not self.valid: raise ValueError("pairing_storage_invalid")
            if not valid_device_id(device): raise ValueError("unknown_device")
            if device not in self.devices: raise ValueError("unknown_device")
            devices = {**self.devices, device: {**self.devices[device], "revoked": True}}
            self._save(devices=devices)
            self.devices = devices
            self.pending = {key: row for key, row in self.pending.items() if row["replace_device_id"] != device}

    def public(self):
        with self.lock:
            return [{"device_id": d, "device_kind": row["device_kind"], "created_at": row["created_at"],
                     "revoked": bool(row.get("revoked"))} for d, row in self.devices.items()]


GET_PATHS = frozenset({"/app/state", "/device/timeline", "/device/status", "/app/notifications", "/app/routines", "/app/diagnostics", "/reminders"})
POST_PATHS = frozenset({"/device/register", "/device/status", "/device/test", "/app/ui-task/propose", "/app/task/approve",
    "/app/task/reject", "/app/task/cancel", "/app/task/resume", "/app/routines", "/app/routines/propose",
    "/app/health/snapshot", "/app/health/clear", "/app/notifications", "/app/reminders", "/app/reminders/cancel",
    "/app_policy", "/sleep_mode", "/reminders", "/app/care/preview"})
PERSONAL_PATHS = frozenset({"/app/health/snapshot", "/app/health/clear", "/app/notifications", "/app/reminders", "/app/reminders/cancel",
    "/app_policy", "/sleep_mode", "/reminders", "/app/care/preview"})


class DeviceGateway:
    """Small bounded listener; forwarding is to one configured host only."""
    def __init__(self, store, *, host, port, upstream, authorize, known_devices, enabled=None):
        try:
            if not isinstance(upstream,str) or any(ord(char)<33 or ord(char)==127 for char in upstream):raise ValueError()
            parsed = urllib.parse.urlsplit(upstream)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path.rstrip("/").endswith("/astrbot_plugin_phone_agent"))
        except (ValueError, TypeError): valid = False
        if not valid:
            raise ValueError("invalid_pairing_upstream")
        self.store, self.host, self.port = store, host, port
        self.upstream, self.authorize, self.known_devices = upstream.rstrip("/"), authorize, known_devices
        self.enabled=enabled or (lambda:True)
        self.server = None
        self.claim_attempts = {}
        self.active = 0
        self.clients = set()
        self.closing = False

    async def start(self):
        self.closing = False
        self.server = await asyncio.start_server(self.handle, self.host, self.port, limit=8192)

    async def close(self):
        self.closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.clients): writer.close()

    def _rate_allowed(self, key, limit):
        now = time.monotonic()
        self.claim_attempts = {k: v for k, v in self.claim_attempts.items() if now-v[0] < 60}
        if key not in self.claim_attempts and len(self.claim_attempts) >= 512: return False
        stamp, count = self.claim_attempts.get(key, (now, 0))
        self.claim_attempts[key] = (stamp, count+1)
        return count < limit

    async def handle(self, reader, writer):
        self.active += 1
        self.clients.add(writer)
        code, value = 503, {"success": False, "error_code": "gateway_busy"}
        try:
            if self.active > 16 or self.closing:
                return
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(header) > 8192: raise ValueError("header_too_large")
            lines = header.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            if method not in {"GET", "POST"} or version != "HTTP/1.1" or len(target) > 2048 or not target.startswith("/") or any(ord(c)<33 or ord(c)==127 for c in target): raise ValueError("invalid_request")
            headers = {}
            for line in lines[1:]:
                if not line: continue
                key, value_ = line.split(":", 1)
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key): raise ValueError("invalid_header")
                key = key.lower()
                if key in headers: raise ValueError("duplicate_header")
                if any(ord(c)<32 and c!='\t' or ord(c)==127 for c in value_): raise ValueError("invalid_header")
                headers[key] = value_.strip()
            if "host" not in headers or "transfer-encoding" in headers or "origin" in headers or "expect" in headers: raise ValueError("unsupported_request")
            length = headers.get("content-length", "0")
            if not re.fullmatch(r"[0-9]{1,7}", length): raise ValueError("invalid_length")
            size = int(length)
            if not 0 <= size <= MAX_BODY: raise ValueError("payload_too_large")
            if method == "GET" and size: raise ValueError("get_body_unsupported")
            raw = await asyncio.wait_for(reader.readexactly(size), timeout=10) if size else b""
            payload = strict_json(raw) if raw else {}
            if not isinstance(payload, dict): raise ValueError("invalid_json")
            peer_info = writer.get_extra_info("peername")
            peer = peer_info[0] if peer_info else "unknown"
            if target == "/pair/redeem" and method == "POST":
                if self.enabled() is not True: code,value=403,{"success":False,"error_code":"pairing_disabled"}
                elif not self._rate_allowed(("redeem",peer),10): code, value = 429, {"success": False, "error_code": "rate_limited"}
                else: code, value = 200, self.store.redeem(payload, known_devices=self.known_devices())
            else:
                if not self._rate_allowed(("peer",peer),180):
                    code, value = 429, {"success": False, "error_code": "rate_limited"}
                    return
                authorization = headers.get("authorization", "")
                principal = self.store.authenticate(authorization[7:]) if authorization.startswith("Bearer ") else None
                if principal is None: code, value = 401, {"success": False, "error_code": "unauthorized"}
                elif principal["revoked"]: code, value = 403, {"success": False, "error_code": "device_revoked"}
                elif not self._rate_allowed(("device",principal["device_id"]),120): code,value=429,{"success":False,"error_code":"rate_limited"}
                else: code, value = await self.dispatch(principal, method, target, payload)
        except (ValueError, TypeError, UnicodeError, RecursionError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            code, value = 400, {"success": False, "error_code": "invalid_or_expired_request"}
        except OSError:
            code, value = 503, {"success": False, "error_code": "gateway_unavailable"}
        except Exception:
            code, value = 500, {"success": False, "error_code": "gateway_error"}
        finally:
            try:
                data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                writer.write(f"HTTP/1.1 {code} Response\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + data)
                await asyncio.wait_for(writer.drain(), timeout=5)
            except (ValueError, UnicodeError, OSError, asyncio.TimeoutError): pass
            finally:
                writer.close()
                self.clients.discard(writer)
                self.active -= 1

    async def dispatch(self, principal, method, target, payload):
        if self.closing or self.enabled() is not True: return 503,{"success":False,"error_code":"gateway_unavailable"}
        if not self.store.principal_active(principal): return 403,{"success":False,"error_code":"device_revoked"}
        if method not in {"GET", "POST"} or not isinstance(payload, dict): return 400,{"success":False,"error_code":"invalid_request"}
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/device-api/"):
            return 403, {"success": False, "error_code": "forbidden"}
        path = parsed.path[len("/device-api"):]
        if path not in (GET_PATHS if method == "GET" else POST_PATHS): return 403, {"success": False, "error_code": "forbidden"}
        if principal["device_kind"] == "bot" and path in PERSONAL_PATHS: return 403, {"success": False, "error_code": "personal_device_required"}
        try: query = canonical_query(parsed.query)
        except ValueError: return 400, {"success": False, "error_code": "invalid_query"}
        if query.get("device_id", principal["device_id"]) != principal["device_id"]:
            return 403, {"success": False, "error_code": "device_mismatch"}
        if payload.get("device_id", principal["device_id"]) != principal["device_id"]:
            return 403, {"success": False, "error_code": "device_mismatch"}
        payload = {**payload, "device_id": principal["device_id"], "requester_device_id": principal["device_id"]}
        if path == "/device/register":
            if payload.get("device_kind", principal["device_kind"]) != principal["device_kind"]:
                return 403, {"success": False, "error_code": "device_kind_mismatch"}
            payload["device_kind"] = principal["device_kind"]
        reason = self.authorize(principal, method, path, payload)
        if reason is not True: return 403, {"success": False, "error_code": str(reason)}
        query["device_id"] = principal["device_id"]
        encoded = urllib.parse.urlencode(sorted(query.items()))
        url = self.upstream + path + ("?" + encoded if encoded else "")
        token = self.store.upstream_token
        if not token: return 503, {"success": False, "error_code": "owner_reauthentication_required"}
        def forward():
            if self.closing or self.enabled() is not True:
                return 503, {"success": False, "error_code": "gateway_unavailable"}
            if not self.store.principal_active(principal):
                return 403, {"success": False, "error_code": "device_revoked"}
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if method == "POST" else None
            stamp, nonce = str(int(time.time())), secrets.token_hex(16)
            body_hash=hashlib.sha256(body or b"").hexdigest()
            req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": "Bearer " + token,
                "Content-Type": "application/json", "X-Phone-Device-Principal": principal["device_id"],
                "X-Phone-Gateway-Time":stamp,"X-Phone-Gateway-Nonce":nonce,"X-Phone-Gateway-Credential":principal["credential_id"],
                "X-Phone-Gateway-Body":body_hash,
                "X-Phone-Gateway-Proof": gateway_proof(token,principal["device_id"],principal["credential_id"],method,path,query,body_hash,stamp,nonce)})
            try:
                with open_http(req, timeout=60) as response: status, raw = response.status, response.read(MAX_RESPONSE + 1)
            except urllib.error.HTTPError as error: status, raw = error.code, error.read(MAX_RESPONSE + 1)
            if status == 401: return 503, {"success": False, "error_code": "owner_reauthentication_required"}
            if status < 200 or status >= 600 or 300 <= status < 400:return 502,{"success":False,"error_code":"invalid_upstream_response"}
            if len(raw) > MAX_RESPONSE: return 502, {"success": False, "error_code": "response_too_large"}
            if self.closing or self.enabled() is not True: return 503,{"success":False,"error_code":"gateway_unavailable"}
            if not self.store.principal_active(principal): return 403,{"success":False,"error_code":"device_revoked"}
            try: result = strict_json(raw)
            except ValueError: return 502,{"success":False,"error_code":"invalid_upstream_response"}
            if not isinstance(result,dict): return 502,{"success":False,"error_code":"invalid_upstream_response"}
            return status, scoped_response(result, principal, path)
        status, result = await asyncio.to_thread(forward)
        if self.closing or self.enabled() is not True: return 503,{"success":False,"error_code":"gateway_unavailable"}
        if not self.store.principal_active(principal): return 403,{"success":False,"error_code":"device_revoked"}
        if self.authorize(principal,method,path,payload) is not True: return 403,{"success":False,"error_code":"authorization_changed"}
        return status,result


def scoped_response(value, principal, path):
    secret_keys = {"token", "device_token", "shared_token", "app_shared_token", "relay_token", "plugin_jwt", "upstream_token", "token_hash", "password", "api_key", "openapi_key"}
    def clean(item):
        if isinstance(item,dict):return {key:clean(val) for key,val in item.items() if key.casefold() not in secret_keys}
        if isinstance(item,list):return [clean(val) for val in item]
        return item
    result=clean(value)
    if path == "/app/state":
        for key in ("task_runs","task_approvals","task_grants"):
            result[key]=[row for row in result.get(key,[]) if isinstance(row,dict) and row.get("device_id")==principal["device_id"]]
        if principal["device_kind"]=="bot":
            for key in ("care","night_guard","sleep_mode"):result[key]={}
            for key in ("reminders","notifications"):result[key]=[]
    return result
