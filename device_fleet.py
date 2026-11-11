"""Persistent device roles with separate transport, status and timeline state.

The registry records identity and role only. Transport registrations are renewed
by each app after a server restart; they are never copied between devices.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from astrbot.api import logger
from .device_app import DeviceAppBackend, plugin_data_path
from .storage import write_json_atomic

DEVICE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
DEVICE_KINDS = frozenset({"personal", "bot"})
DEVICE_ROLES = frozenset({"personal", "bot", "unassigned"})


def _valid_id(value):
    return isinstance(value, str) and DEVICE_ID.fullmatch(value) is not None


def _valid_label(value):
    return (isinstance(value, str) and len(value) <= 60
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


class _DeviceConfig:
    def __init__(self, config, device_id, timeline):
        self.config, self.device_id, self.timeline = config, device_id, timeline

    def get(self, key, default=None):
        # The original personal phone's configured URLs must never route a
        # second device's command. Only that device's registration supplies URLs.
        if key == "app_direct_urls":
            return ""
        if key == "app_state_path":
            return str(self.timeline)
        return self.config.get(key, default)


class DeviceFleet:
    MAX_DEVICES = 4

    def __init__(self, config, primary: DeviceAppBackend, audit):
        self.config, self.primary, self.audit = config, primary, audit
        custom = config.get("device_registry_path")
        self.path = (plugin_data_path(custom, "phone_agent_devices.json") if custom else
                     plugin_data_path(config.get("app_state_path"), "phone_agent_app_state.json")
                     .with_name("phone_agent_devices.json"))
        self.lock = threading.RLock()
        self.primary_id = ""
        self.rows = {}
        self.backends = {}
        self._invalid = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version", 1)) is not int
                    or data.get("version", 1) not in {1, 2}
                    or not isinstance(data.get("devices"), dict)
                    or len(data["devices"]) > self.MAX_DEVICES):
                raise ValueError("invalid registry")
            primary_id = data.get("personal_device_id", "")
            if not isinstance(primary_id, str) or (primary_id and not _valid_id(primary_id)):
                raise ValueError("invalid personal device")
            rows = {}
            for key, value in data["devices"].items():
                if not _valid_id(key) or not isinstance(value, dict):
                    raise ValueError("invalid device")
                role = value.get("role")
                kind = value.get("device_kind", "personal")
                revision = value.get("revision", 1)
                label = value.get("label") or key
                if (not isinstance(role, str) or role not in DEVICE_ROLES
                        or not isinstance(kind, str) or kind not in DEVICE_KINDS
                        or type(revision) is not int or not 1 <= revision < 2**63
                        or not _valid_label(label)):
                    raise ValueError("invalid device metadata")
                if (role == "personal") != (key == primary_id):
                    raise ValueError("personal device mismatch")
                if role == "personal" and kind != "personal":
                    raise ValueError("bot app cannot be personal")
                rows[key] = {"role": role, "device_kind": kind, "label": label, "revision": revision}
            if primary_id and primary_id not in rows:
                raise ValueError("missing personal device")
            if sum(row["role"] == "bot" for row in rows.values()) > 1:
                raise ValueError("multiple bot roles")
            self.rows, self.primary_id = rows, primary_id
            if primary_id:
                self.backends[primary_id] = primary
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError, UnicodeError):
            # Do not silently assign the next connected app to a role after a
            # corrupt file. In particular, a Bot must never inherit personal data.
            self._invalid = True
            logger.warning("Phone Control device registry is invalid; registration is disabled")

    def _save(self, primary_id=None, rows=None):
        write_json_atomic(self.path, {
            "version": 2,
            "personal_device_id": self.primary_id if primary_id is None else primary_id,
            "devices": self.rows if rows is None else rows,
        })

    def _adopt_legacy_primary(self):
        """Adopt an explicitly registered legacy primary without changing its role.

        Some integrations register the original backend directly. Persist its
        identity before exposing it through the fleet, just as for new devices.
        """
        if self._invalid:
            return False
        device = self.primary.binding()[0]
        if self.primary_id:
            return not device or device == self.primary_id
        if not device:
            return True
        existing = self.rows.get(device)
        if (not _valid_id(device) or (existing and existing["device_kind"] == "bot")
                or (not existing and len(self.rows) >= self.MAX_DEVICES)):
            return False
        rows = dict(self.rows)
        rows[device] = {"role": "personal", "device_kind": "personal", "label": "我的手机",
                        "revision": (existing or {}).get("revision", 0) + 1}
        self._save(device, rows)
        self.primary_id, self.rows = device, rows
        self.backends[device] = self.primary
        return True

    def backend(self, device_id=None):
        with self.lock:
            try:
                if not self._adopt_legacy_primary():
                    return None
            except OSError:
                return None
            if device_id is None or device_id == "":
                # Omitted selection keeps legacy personal behavior; it never
                # silently starts controlling the Bot phone instead.
                return self.primary
            if not _valid_id(device_id) or device_id not in self.rows:
                return None
            backend = self.backends.get(device_id)
            if backend and backend.binding()[0] not in {"", device_id}:
                return None
            return backend

    def register(self, payload):
        if not isinstance(payload, dict) or not _valid_id(payload.get("device_id")):
            return {"success": False, "error_code": "invalid_device_id"}
        device_id = payload["device_id"]
        with self.lock:
            if self._invalid:
                return {"success": False, "error_code": "device_registry_invalid"}
            try:
                if not self._adopt_legacy_primary():
                    return {"success": False, "error_code": "device_binding_mismatch"}
            except OSError:
                return {"success": False, "error_code": "storage_unavailable"}
            existing = self.rows.get(device_id)
            # A known device may omit kind when reconnecting through an older
            # client. A new client without it retains the legacy personal kind.
            kind = payload.get("device_kind", (existing or {}).get("device_kind", "personal"))
            if not isinstance(kind, str) or kind not in DEVICE_KINDS:
                return {"success": False, "error_code": "invalid_device_kind"}
            if existing and kind != existing["device_kind"]:
                return {"success": False, "error_code": "device_kind_mismatch"}
            if not existing and len(self.rows) >= self.MAX_DEVICES:
                return {"success": False, "error_code": "device_limit"}

            primary_id = self.primary_id
            if not existing and kind == "personal" and not primary_id:
                primary_id = device_id
            primary = primary_id == device_id
            backend = self.primary if primary else self.backends.get(device_id)
            if backend is None:
                digest = hashlib.sha256(device_id.encode()).hexdigest()[:24]
                timeline = self.path.parent / ("device_" + digest + "_timeline.json")
                backend = DeviceAppBackend(_DeviceConfig(self.config, device_id, timeline), self.audit)

            rows = dict(self.rows)
            if not existing:
                bot_available = not any(row["role"] == "bot" for row in rows.values())
                role = "personal" if primary else "bot" if kind == "bot" and bot_available else "unassigned"
                rows[device_id] = {"role": role, "device_kind": kind, "revision": 1,
                                   "label": "我的手机" if primary else "Bot 的手机" if kind == "bot" else "新设备"}
            # DeviceAppBackend validates first and invokes this hook inside its
            # registration lock, before publishing any binding/status mutation.
            # A disk failure leaves both the previous registry and backend live.
            try:
                result = backend.register(payload, before_commit=lambda: self._save(primary_id, rows))
            except OSError:
                return {"success": False, "error_code": "storage_unavailable"}
            if not result.get("success"):
                return result
            self.primary_id, self.rows = primary_id, rows
            self.backends[device_id] = backend
            result.update(device_role=rows[device_id]["role"], device_kind=kind,
                          personal_device_id=self.primary_id)
            return result

    def assign(self, device_id, role, label=None):
        with self.lock:
            if self._invalid:
                raise ValueError("device_registry_invalid")
            if not self._adopt_legacy_primary():
                raise ValueError("device_binding_mismatch")
            if not _valid_id(device_id) or device_id not in self.rows:
                raise ValueError("unknown_device")
            if not isinstance(role, str) or role not in DEVICE_ROLES:
                raise ValueError("invalid_device_role")
            previous = self.rows[device_id]
            if previous["device_kind"] == "bot" and role == "personal":
                raise ValueError("bot_device_cannot_be_personal")
            # Personal state/leases belong to the initial personal device.
            # Replacing it needs migration, never an incidental role update.
            if (device_id == self.primary_id) != (role == "personal"):
                raise ValueError("personal_device_pinned")
            if role == "bot" and any(k != device_id and v["role"] == "bot" for k, v in self.rows.items()):
                raise ValueError("bot_role_in_use")
            if label is not None and not _valid_label(label):
                raise ValueError("invalid_device_label")
            rows = dict(self.rows)
            rows[device_id] = {**previous, "role": role, "label": label or previous["label"],
                               "revision": previous["revision"] + int(role != previous["role"])}
            self._save(rows=rows)
            self.rows = rows

    def role(self, device_id):
        with self.lock:
            try:
                if not self._adopt_legacy_primary() or not _valid_id(device_id):
                    return "unassigned"
            except OSError:
                return "unassigned"
            return self.rows.get(device_id, {}).get("role", "unassigned")

    def revision(self, device_id):
        with self.lock:
            return self.rows.get(device_id, {}).get("revision", 0) if _valid_id(device_id) else 0

    def public(self):
        with self.lock:
            try:
                if not self._adopt_legacy_primary():
                    return []
            except OSError:
                return []
            result = []
            for device_id, row in self.rows.items():
                backend = self.backends.get(device_id)
                status = backend.last_status() if backend else {}
                registration = backend.registration() if backend else {}
                stamp = (status or {}).get("received_at", 0)
                result.append({"device_id": device_id, "label": row["label"], "role": row["role"],
                               "device_kind": row["device_kind"],
                               "online": bool(stamp and 0 <= time.time() - stamp <= 120),
                               "app_version": (registration or {}).get("app_version", "")})
            return result
