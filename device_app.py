# -*- coding: utf-8 -*-
"""Structured command transport to AstrBot Phone Agent App.

This module replaces the Operit bridge when control_backend=app. The phone
runs AstrBot Phone Agent App, which executes a
closed set of structured JSON commands through Shizuku, instead of asking an
on-device agent to interpret natural-language prompts.

Wire contract: docs/APP_API_V2.md. Command envelope remains schema v1;
the dashboard app API has its own version.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger


COMMAND_SCHEMA_VERSION = 1
PLUGIN_DATA_DIR = Path("data") / "plugin_data" / "astrbot_plugin_phone_agent"


def plugin_data_path(value: Any, default_name: str) -> Path:
    """Resolve plugin state below AstrBot's managed data directory by default."""
    raw = str(value or "").strip()
    path = Path(raw) if raw else Path(default_name)
    base = PLUGIN_DATA_DIR.parts
    if not path.is_absolute() and path.parts[:len(base)] == base:
        return path
    return path if path.is_absolute() else PLUGIN_DATA_DIR / path

# Closed command set. The app rejects anything not in this table; adding a
# new action requires a contract-doc update and an app-side implementation
# shipped before the plugin starts sending it.
COMMAND_ACTIONS = frozenset({
    "status",
    "quick_status",
    "open_app",
    "close_app",
    "foreground_app",
    "lock_screen",
    "wake_screen",
    "home",
    "back",
    "screenshot",
    "screen_text",
    "suspend_app",
    "unsuspend_app",
    "suspend_video_apps",
    "unsuspend_video_apps",
    "location",
    "usage_stats",
    "send_notification",
    "ping",
    # UI automation, at parity with the OperitAI backend. These mutate on-screen
    # state, so they are dangerous by default and need on-device confirmation.
    "tap",
    "long_press",
    "swipe",
    "scroll",
    "input_text",
    "key_event",
})

# Actions that change device state or expose screen content. The app prompts for
# on-device confirmation unless the user opts that specific action out.
DANGEROUS_ACTIONS = frozenset({
    "close_app",
    "suspend_app",
    "suspend_video_apps",
    "location",
    "send_notification",
    "tap",
    "long_press",
    "swipe",
    "scroll",
    "input_text",
    "key_event",
    "screen_text",
    "screenshot",
})

# Screenshots ride inside the result envelope as base64, so both ends need an
# explicit ceiling. _http_json reads at most 4 MiB, which this stays under.
MAX_SCREENSHOT_BASE64_CHARS = 3_000_000
# Decoded ceiling enforced on the plugin side before touching the disk.
MAX_SCREENSHOT_BYTES = 2_200_000

MAX_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = 200
_HEALTH_TIMEOUT_SECONDS = 1.5
_REGISTER_TIMEOUT_SECONDS = 8

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


def _text(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


class DeviceAppBackend:
    """Tracks the registered phone app and routes commands to it.

    Command path: try the app's direct HTTP endpoint first (Tailscale/LAN);
    if unreachable, queue through the public relay and wait for the phone's
    poller to execute and report. Both paths speak the same envelope.
    """

    MAX_TIMELINE_ITEMS = 100

    def __init__(self, config: Any, audit) -> None:
        self._config = config
        self._audit = audit
        self._lock = threading.Lock()
        self._registration: dict[str, Any] | None = None
        # P1-C: bumps when the binding changes identity, so cached observations
        # and status samples from the previous device can be invalidated.
        self._registration_generation = 0
        self._last_status: dict[str, Any] | None = None
        # The URL that answered last: try it first and skip its health probe.
        # Without this every command paid the health timeout for each dead
        # candidate (three dead addresses = 9 s per command).
        self._active_url = ""
        self._status_history: list[tuple[float, dict[str, Any]]] = []
        self._recent_results: list[dict[str, Any]] = []
        self._timeline: list[dict[str, Any]] = []
        self._load_timeline()

    # ---------- timeline store (served to the app's pull sync) ----------

    def timeline_append(
        self,
        item_type: str,
        title: str,
        body: str,
        related_task_id: str | None = None,
        *,
        event: str | None = None,
        notify: bool = False,
        notification_id: str | None = None,
        reminder_id: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "id": "tl-" + uuid.uuid4().hex[:12],
            "type": item_type,
            "title": title[:120],
            "body": body[:2000],
            "related_task_id": related_task_id,
            "timestamp": round(time.time(), 3),
            "event": event,
            "notify": bool(notify and event == "reminder_due"),
            "notification_id": notification_id,
            "reminder_id": reminder_id,
        }
        with self._lock:
            # A restart between durable delivery and removal from the pending
            # store must not produce a second phone notification.
            if reminder_id and event:
                existing = next((entry for entry in self._timeline
                                 if entry.get("reminder_id") == reminder_id
                                 and entry.get("event") == event), None)
                if existing:
                    return dict(existing)
            items = [item, *self._timeline][: self.MAX_TIMELINE_ITEMS]
            self._save_timeline(items)
            self._timeline = items
        return item

    def _timeline_path(self) -> Path:
        return plugin_data_path(self._config.get("app_state_path"), "phone_agent_app_state.json")

    def _save_timeline(self, items: list[dict[str, Any]]) -> None:
        path = self._timeline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "timeline": items}, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_timeline(self) -> None:
        try:
            path = self._timeline_path()
            if not path.exists():
                return
            value = json.loads(path.read_text(encoding="utf-8"))
            items = value.get("timeline", []) if isinstance(value, dict) else []
            if not isinstance(items, list):
                return
            for item in items[:self.MAX_TIMELINE_ITEMS]:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                try:
                    timestamp = float(item.get("timestamp", 0))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(timestamp) or timestamp <= 0:
                    continue
                item = dict(item)
                item["timestamp"] = timestamp
                item["notify"] = bool(item.get("notify") and item.get("event") == "reminder_due")
                self._timeline.append(item)
        except (OSError, ValueError, TypeError):
            logger.warning("phone agent app state file is invalid; ignoring it")

    def timeline_since(self, since: float, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = [item for item in self._timeline if item["timestamp"] > since]
        return [dict(item) for item in items[: max(1, min(limit, 100))]]

    # ---------- config ----------

    def shared_token(self) -> str:
        return str(self._config.get("app_shared_token") or "").strip()

    def command_timeout(self) -> float:
        try:
            value = float(self._config.get("app_command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_COMMAND_TIMEOUT_SECONDS
        if not math.isfinite(value):
            return DEFAULT_COMMAND_TIMEOUT_SECONDS
        return max(10.0, min(value, MAX_COMMAND_TIMEOUT_SECONDS))

    def relay_url(self) -> str:
        return _text(self._config.get("relay_base_url"), 500).rstrip("/")

    def relay_token(self) -> str:
        return str(self._config.get("relay_token") or "").strip()

    def relay_enabled(self) -> bool:
        return bool(self.relay_url() and self.relay_token())

    def direct_urls(self) -> list[str]:
        """Ordered direct endpoints; home LAN first, Tailscale as fallback."""
        raw = str(self._config.get("app_direct_urls") or "")
        configured = [item.strip().rstrip("/") for item in re.split(r"[,\s]+", raw) if item.strip()]
        registered = self.registration() or {}
        announced = registered.get("base_urls") or [registered.get("base_url", "")]
        values = configured + [str(item).strip().rstrip("/") for item in announced if str(item).strip()]
        return list(dict.fromkeys(item for item in values if self.valid_base_url(item)))[:8]

    @staticmethod
    def valid_base_url(value: str) -> bool:
        try:
            parts = urlsplit(value)
            return bool(parts.scheme in {"http", "https"} and parts.hostname and parts.port != 0
                        and not parts.username and not parts.password and not parts.query and not parts.fragment)
        except ValueError:
            return False

    # ---------- registration ----------

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = _text(payload.get("device_id"), 64)
        base_url = _text(payload.get("base_url"), 500).rstrip("/")
        base_urls = payload.get("base_urls") if isinstance(payload.get("base_urls"), list) else []
        base_urls = [str(item).strip().rstrip("/") for item in base_urls if str(item).strip()]
        if base_url and base_url not in base_urls:
            base_urls.insert(0, base_url)
        if not base_url and base_urls:
            base_url = base_urls[0]
        app_version = _text(payload.get("app_version"), 40)
        token_fingerprint = _text(payload.get("token_fingerprint"), 100)
        if not device_id or not re.fullmatch(r"[A-Za-z0-9._-]+", device_id):
            return {"success": False, "error": "device_id is required"}
        # A05: allow registration without direct URLs (pure-relay devices).
        # base_url and base_urls are both empty when the device connects only
        # through the relay; transport reachability is modeled separately.
        valid_urls = [url for url in base_urls if self.valid_base_url(url)]
        if base_url and not self.valid_base_url(base_url):
            return {"success": False, "error": "base_url must be an http(s) URL"}
        if any(not self.valid_base_url(url) for url in base_urls):
            return {"success": False, "error": "base_urls must contain http(s) URLs"}
        if not base_url and not valid_urls:
            # Relay-only device: accept with empty direct endpoints.
            base_url = ""
            base_urls = []
        else:
            base_urls = valid_urls
            if not base_url and base_urls:
                base_url = base_urls[0]
        expected_fingerprint = self._token_fingerprint()
        if (
            expected_fingerprint
            and token_fingerprint
            and token_fingerprint != expected_fingerprint
        ):
            return {"success": False, "error": "token fingerprint mismatch; update app_shared_token on one side"}
        with self._lock:
            previous = self._registration
            rebind = previous is None or previous.get("device_id") != device_id
            if rebind:
                # P1-C: a new binding must not inherit the old device's status.
                self._registration_generation += 1
                self._last_status = None
                self._status_history = []
            self._registration = {
                "device_id": device_id,
                "base_url": base_url,
                "base_urls": base_urls or [base_url],
                "app_version": app_version,
                "token_fingerprint": token_fingerprint,
                "registered_at": time.time(),
            }
        self._audit(
            "device_app_registered",
            device_id=device_id,
            base_url=base_url,
            app_version=app_version,
        )
        return {
            "success": True,
            "device_id": device_id,
            "schema_version": COMMAND_SCHEMA_VERSION,
            "accepted_actions": sorted(COMMAND_ACTIONS),
        }

    def registration(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._registration) if self._registration else None

    def registration_generation(self) -> int:
        """Monotonic binding id; changes when a different device registers."""
        with self._lock:
            return self._registration_generation

    def _token_fingerprint(self) -> str:
        import hashlib

        token = self.shared_token()
        return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""

    # ---------- status cache (from poll payloads / results) ----------

    MAX_STATUS_HISTORY = 20

    def record_device_status(self, status: dict[str, Any]) -> None:
        if not isinstance(status, dict) or not status:
            return
        # P1-C: a sample that names its device must belong to the current
        # binding. A late heartbeat from the previous phone must not overwrite
        # the new one's status.
        reported_device = _text(status.get("device_id"), 64)
        # B04: keep reported_at (phone's sampling time) separate from
        # received_at (server receive time). A skewed phone clock or a
        # missing timestamp must NOT be back-filled with time.time() —
        # that would make a stale sample look fresh. The consumer checks
        # reported_at for freshness; received_at is for diagnostics only.
        phone_reported = self._reported_at(status.get("reported_at"))
        with self._lock:
            current_device = (self._registration or {}).get("device_id", "")
            if reported_device and current_device and reported_device != current_device:
                return
            self._last_status = {
                "battery_percent": _int_or_none(status.get("battery_percent")),
                "charging": bool(status.get("charging")),
                "foreground_package": _text(status.get("foreground_package"), 160),
                "screen_on_seconds_today": _int_or_none(status.get("screen_on_seconds_today")),
                "screen_interactive": _opt_bool(status.get("screen_interactive")),
                "shizuku_ready": bool(status.get("shizuku_ready")),
                "app_version": _text(status.get("app_version"), 40),
                # App-side reason when the foreground probe returned nothing.
                "foreground_debug": _text(status.get("foreground_debug"), 160),
                "reported_at": phone_reported,
                "received_at": time.time(),
            }
            self._status_history.append((time.time(), dict(self._last_status)))
            self._status_history = self._status_history[-self.MAX_STATUS_HISTORY:]

    @staticmethod
    def _reported_at(value: Any) -> float | None:
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(stamp) or stamp <= 0:
            return None
        # A phone clock far from the server's cannot be trusted for freshness.
        now = time.time()
        if abs(now - stamp) > 300:
            return None
        return stamp

    def status_counts_last_hours(self, hours: float) -> int:
        cutoff = time.time() - max(0.0, hours) * 3600
        with self._lock:
            return sum(1 for received_at, _ in self._status_history if received_at >= cutoff)

    def last_status(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_status) if self._last_status else None

    def _record_result(self, result: dict[str, Any]) -> None:
        try:
            finished_at = float(result.get("finished_at") or time.time())
        except (TypeError, ValueError):
            finished_at = time.time()
        entry = {
            "command_id": _text(result.get("command_id"), 64),
            "action": _text(result.get("action"), 40),
            "success": bool(result.get("success")),
            "error_code": _text(result.get("error_code"), 40),
            "error": _text(result.get("error"), 200),
            "finished_at": finished_at if math.isfinite(finished_at) else time.time(),
        }
        with self._lock:
            self._recent_results.append(entry)
            self._recent_results = self._recent_results[-20:]

    def recent_results(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._recent_results]

    # ---------- transport ----------

    def _http_json(
        self,
        url: str,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else {"success": False, "error": "device returned invalid JSON"}
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(4 * 1024 * 1024).decode("utf-8"))
                if isinstance(data, dict) and data.get("type") == "result":
                    return data
            except (ValueError, OSError):
                pass
            return {"success": False, "error_code": "http_error", "error": f"device HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"success": False, "error": f"device connection failed: {str(exc)[:180]}"}

    def health(self) -> dict[str, Any]:
        registration = self.registration()
        urls = self.direct_urls()
        if not urls:
            return {"available": False, "error": "phone app has not registered; open the app once"}
        result = {"success": False, "error": "phone app direct endpoints unavailable"}
        active_url = ""
        deadline = time.monotonic() + min(self.command_timeout(), 12.0)
        for candidate in urls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = self._http_json(candidate + "/health", self.shared_token(), timeout=min(_HEALTH_TIMEOUT_SECONDS, remaining))
            if result.get("success"):
                active_url = candidate
                break
        return {
            "available": bool(result.get("success")),
            "base_url": active_url,
            "base_urls": urls,
            "device_id": (registration or {}).get("device_id", ""),
            "app_version": (registration or {}).get("app_version", ""),
            "error": "" if result.get("success") else _text(result.get("error"), 200),
        }

    def warm(self) -> str:
        """Discover the reachable URL once so the first command does not pay for
        every dead candidate in the list."""
        result = self.health()
        if result.get("available") and result.get("base_url"):
            self._active_url = result["base_url"]
        return self._active_url

    def _direct_command(self, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        urls = self.direct_urls()
        if not urls:
            return {"success": False, "error_code": "not_registered", "error": "phone app has not registered"}
        if self._active_url and self._active_url in urls:
            urls = [self._active_url] + [item for item in urls if item != self._active_url]
        last = {"success": False, "error_code": "direct_unavailable", "error": "phone app direct endpoints unavailable"}
        deadline = time.monotonic() + timeout
        for index, base_url in enumerate(urls):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if base_url == self._active_url:
                health = {"success": True}  # it answered a moment ago
            else:
                health = self._http_json(base_url + "/health", self.shared_token(), timeout=min(_HEALTH_TIMEOUT_SECONDS, remaining))
            if not health.get("success"):
                last = health
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            routes_left = len(urls) - index + int(self.relay_enabled())
            attempt_timeout = min(remaining, max(4.0, min(8.0, remaining / routes_left)))
            result = self._http_json(base_url + "/command", self.shared_token(), method="POST", payload=envelope, timeout=attempt_timeout)
            if result.get("command_id") or result.get("type") == "result" or result.get("success"):
                # A matching denial is final. A malformed result is also final:
                # the command may already have run, so do not replay it.
                self._active_url = base_url
                return self._normalize_result(result, envelope)
            if result.get("error_code") in {"denied", "bad_args", "unsupported_action", "unauthorized"}:
                return {**result, "command_id": envelope["command_id"], "action": envelope["action"]}
            last = result
        return last

    def _normalize_result(self, result: Any, envelope: dict[str, Any]) -> dict[str, Any]:
        if (not isinstance(result, dict) or result.get("type") != "result"
                or result.get("schema_version", 1) != COMMAND_SCHEMA_VERSION
                or result.get("command_id") != envelope["command_id"]
                or result.get("action") != envelope["action"]
                or not isinstance(result.get("success"), bool)):
            return {"success": False, "error_code": "invalid_result", "error": "phone result does not match the command",
                    "command_id": envelope["command_id"], "action": envelope["action"]}
        result = dict(result)
        output = result.get("output")
        if isinstance(output, str):
            try:
                parsed = json.loads(output)
                if isinstance(parsed, (dict, list)):
                    result["output"] = parsed
            except ValueError:
                pass
        self._record_result(result)
        return result

    def _relay_submit(self, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.relay_enabled():
            return {"success": False, "error_code": "relay_unconfigured", "error": "relay is not configured"}
        deadline = time.monotonic() + timeout
        push = self._http_json(
            self.relay_url() + "/task",
            self.relay_token(),
            method="POST",
            payload={"message": json.dumps(envelope, ensure_ascii=False)},
            timeout=min(15, timeout),
        )
        if not push.get("success"):
            return {"success": False, "error_code": "relay_push_failed", "error": _text(push.get("error"), 200)}
        task_id = str(push.get("task_id", ""))
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            result = self._http_json(
                f"{self.relay_url()}/result/{task_id}", self.relay_token(), timeout=min(15, remaining)
            )
            payload = result.get("result") or {}
            if not isinstance(payload, dict):
                return self._normalize_result(None, envelope)
            raw_message = str(payload.get("ai_response") or "")
            status = _text(payload.get("status"), 20)
            if status in {"pending", "claimed", "running"}:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
                continue
            if raw_message:
                try:
                    envelope_result = json.loads(raw_message)
                except json.JSONDecodeError:
                    envelope_result = {"success": False, "error_code": "bad_result", "error": "phone returned non-JSON result"}
                return self._normalize_result(envelope_result, envelope)
            if not result.get("success"):
                return {"success": False, "error_code": "relay_lost_task", "error": _text(result.get("error"), 200)}
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        return {
            "success": False,
            "error_code": "timeout",
            "task_id": task_id,
            "error": "queued via relay but the phone did not pick it up in time; it may still run later",
        }

    # ---------- command entry ----------

    def build_envelope(self, action: str, args: dict[str, Any] | None = None, dangerous: bool | None = None) -> dict[str, Any]:
        if action not in COMMAND_ACTIONS:
            raise ValueError(f"unknown action: {action}")
        args = {key: value for key, value in (args or {}).items() if value is not None}
        return {
            "type": "command",
            "schema_version": COMMAND_SCHEMA_VERSION,
            "command_id": "c-" + uuid.uuid4().hex[:12],
            "action": action,
            "args": args,
            "created_at": round(time.time(), 3),
            "dangerous": action in DANGEROUS_ACTIONS if dangerous is None else bool(dangerous),
            "deadline": int(self.command_timeout()),
        }

    def execute(self, action: str, args: dict[str, Any] | None = None,
                timeout: float | None = None) -> dict[str, Any]:
        """Run one command: direct first, relay as fallback."""
        try:
            envelope = self.build_envelope(action, args)
        except ValueError as exc:
            return {"success": False, "action": action, "error_code": "unsupported_action", "error": str(exc)[:200]}
        # Callers that cannot wait (task verification) pass a tighter budget.
        timeout = min(self.command_timeout(), float(timeout)) if timeout else self.command_timeout()
        deadline = time.monotonic() + timeout
        direct = self._direct_command(envelope, timeout)
        if direct.get("command_id") or direct.get("success") or not self.relay_enabled():
            self._audit("device_command", action=action, command_id=envelope["command_id"], backend="direct", success=bool(direct.get("success")), error=_text(direct.get("error"), 160))
            direct["backend"] = "direct"
            return direct
        if direct.get("error_code") != "not_registered":
            self._audit("relay_fallback", action=action, reason=_text(direct.get("error"), 160))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"success": False, "action": action, "command_id": envelope["command_id"], "backend": "direct",
                    "error_code": "timeout", "error": "command transport budget exhausted"}
        relay_result = self._relay_submit(envelope, remaining)
        relay_result.setdefault("action", action)
        relay_result["backend"] = "relay"
        self._audit("device_command", action=action, command_id=envelope["command_id"], backend="relay", success=bool(relay_result.get("success")))
        return relay_result


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _opt_bool(value: Any) -> bool | None:
    """A missing screen_interactive must stay unknown, never silently true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None
