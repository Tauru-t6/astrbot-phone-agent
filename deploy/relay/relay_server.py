#!/usr/bin/env python3
"""Phone Agent relay: a tiny public queue between AstrBot and the phone.

The phone's Operit workflow polls GET /poll; AstrBot's plugin pushes tasks
with POST /task and reads results with GET /result/<id>. All endpoints
require the shared bearer token. State is a single JSON file so a restart
never loses pending work.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATE_PATH = Path(os.environ.get("RELAY_STATE", Path(__file__).parent / "relay_state.json"))
TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8791"))
MAX_TASKS = 64
TASK_TTL_SECONDS = 24 * 3600
RESULT_TTL_SECONDS = 24 * 3600
# A pending command older than this is never delivered; it only blocks the queue.
# 30 min by default: a command issued while the phone was locked is still
# delivered when it wakes, while a long-offline queue cannot pile up forever.
STALE_PENDING_SECONDS = float(os.environ.get("RELAY_STALE_SECONDS", 1800))
CLAIM_LEASE_SECONDS = 5 * 60
# B08: must match the plugin's device_app._http_json 4 MiB read ceiling so a
# structured command or result envelope is never silently truncated. A body
# above the limit is rejected with 413 (see do_POST) — never cropped JSON.
MAX_BODY_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
DEVICE_HEADER = "X-Relay-Device-ID"
MAX_DEVICE_ID_LENGTH = 128
# Device identity is required by default so a second phone cannot submit a
# result for a task claimed by the first phone. Set to false only for legacy
# workflows that cannot send custom headers.
REQUIRE_DEVICE_ID = os.environ.get("RELAY_REQUIRE_DEVICE_ID", "0").strip().lower() not in {"0", "false", "no", "off"}
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_REQUESTS = max(1, int(os.environ.get("RELAY_RATE_LIMIT", "120")))
TASK_RATE_LIMIT_REQUESTS = max(1, int(os.environ.get("RELAY_TASK_RATE_LIMIT", "30")))

_lock = threading.Lock()
_state: dict = {"tasks": [], "results": {}}
_rate_lock = threading.Lock()
_rate_buckets: dict[tuple[str, str], deque[float]] = {}


def _persist() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(_state, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def _load() -> None:
    global _state
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("tasks"), list):
                def timestamp(value) -> bool:
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        return False
                    try:
                        return math.isfinite(value) and value > 0
                    except OverflowError:
                        return False

                tasks = []
                for task in loaded["tasks"]:
                    if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
                            or not task["id"] or not isinstance(task.get("message"), str)
                            or not isinstance(task.get("status"), str)
                            or task.get("status") not in {"pending", "claimed", "running", "done", "expired"}
                            or not timestamp(task.get("created_at"))):
                        continue
                    if task.get("status") in {"claimed", "running"} and not timestamp(task.get("claimed_at")):
                        task["status"] = "pending"
                        task.pop("claimed_at", None)
                        task.pop("device_id", None)
                    tasks.append(task)
                results = loaded.get("results")
                _state = {"tasks": tasks, "results": {
                    key: value for key, value in (results.items() if isinstance(results, dict) else [])
                    if isinstance(value, dict) and timestamp(value.get("finished_at"))
                }}
        except (OSError, json.JSONDecodeError):
            pass


def _replay_safe(task: dict) -> bool:
    try:
        command = json.loads(task.get("message", ""))
    except (TypeError, ValueError):
        return False
    return (isinstance(command, dict) and command.get("type") == "command"
            and command.get("schema_version") == 1 and bool(command.get("command_id")))


def _gc_locked(now: float) -> bool:
    changed = False
    for task in _state["tasks"]:
        # Claims created by older relay versions have no owner. Reset them so
        # strict device mode never leaves an uncompletable task behind.
        if task.get("status") == "claimed" and REQUIRE_DEVICE_ID and not task.get("device_id"):
            task["status"] = "pending"
            task.pop("claimed_at", None)
            changed = True
        if task.get("status") in {"claimed", "running"} and now - task.get("claimed_at", task.get("created_at", now)) >= CLAIM_LEASE_SECONDS:
            # Structured App commands have durable command-id deduplication.
            # Operit natural-language tasks do not: replay could repeat effects.
            task["status"] = "pending" if _replay_safe(task) else "unknown"
            task.pop("claimed_at", None)
            task.pop("device_id", None)
            changed = True
        if task.get("status") == "pending" and now - task.get("created_at", 0) > STALE_PENDING_SECONDS:
            task["status"] = "expired"
            task["expired_at"] = now
            changed = True
    old_tasks, old_results = len(_state["tasks"]), len(_state["results"])
    _state["tasks"] = [
        t for t in _state["tasks"]
        if now - t.get("created_at", 0) < TASK_TTL_SECONDS
    ]
    _state["results"] = {
        k: v for k, v in _state["results"].items()
        if now - v.get("finished_at", 0) < RESULT_TTL_SECONDS
    }
    return changed or len(_state["tasks"]) != old_tasks or len(_state["results"]) != old_results


def push_task(payload: dict) -> dict:
    target = payload.get("target_device_id")
    if target is not None and (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", target)):
        raise ValueError("invalid target_device_id")
    now = time.time()
    with _lock:
        _gc_locked(now)
        if sum(t.get("status") in {"pending", "claimed", "running"} for t in _state["tasks"]) >= MAX_TASKS:
            raise OverflowError("pending task queue is full")
        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "message": str(payload.get("message", "")),
            "created_at": now,
            "status": "pending",
        }
        if target:
            task["target_device_id"] = target
        _state["tasks"].append(task)
        _persist()
        return dict(task)


def poll_task(device_id: str | None = None, *, targeted_only: bool = False) -> dict | None:
    if targeted_only and not device_id:
        raise ValueError("device_id required for targeted polling")
    now = time.time()
    with _lock:
        changed = _gc_locked(now)
        for task in _state["tasks"]:
            if task["status"] != "pending":
                continue
            if targeted_only and task.get("target_device_id") != device_id:
                continue
            if task.get("target_device_id") and task["target_device_id"] != device_id:
                continue
            task["status"] = "claimed"
            task["claimed_at"] = now
            if device_id:
                task["device_id"] = device_id
            _persist()
            return dict(task)
        if changed:
            _persist()
    return None


def submit_result(task_id: str, success: bool, ai_response: str, error: str = "", device_id: str | None = None) -> bool:
    now = time.time()
    with _lock:
        if _gc_locked(now):
            _persist()
        task = next((t for t in _state["tasks"] if t["id"] == task_id), None)
        if task is None or task["status"] not in {"claimed", "running"}:
            return False
        if task.get("device_id") and task.get("device_id") != device_id:
            return False
        task["status"] = "done"
        _state["results"][task_id] = {
            "task_id": task_id,
            "message": task["message"],
            "success": bool(success),
            "ai_response": str(ai_response),
            "error": str(error)[:500],
            "finished_at": now,
        }
        _persist()
        return True


def renew_task(task_id: str, device_id: str | None = None) -> bool:
    now = time.time()
    with _lock:
        if _gc_locked(now):
            _persist()
        task = next((t for t in _state["tasks"] if t["id"] == task_id), None)
        if task is None or task.get("status") not in {"claimed", "running"}:
            return False
        if task.get("device_id") and task.get("device_id") != device_id:
            return False
        task["claimed_at"] = now
        _persist()
        return True


def get_result(task_id: str) -> dict | None:
    with _lock:
        result = _state["results"].get(task_id)
        if result is not None:
            return dict(result)
        task = next((t for t in _state["tasks"] if t["id"] == task_id), None)
        if task is not None:
            return {"task_id": task_id, "status": task["status"]}
    return None


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def _reply(self, code: int, body: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Error responses may precede consuming the body. Never parse those
        # unread bytes as another HTTP request on the same connection.
        if code >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return bool(TOKEN) and secrets.compare_digest(header, "Bearer " + TOKEN)

    def _device_id(self) -> str | None:
        value = self.headers.get(DEVICE_HEADER, "").strip()
        if not value or len(value) > MAX_DEVICE_ID_LENGTH:
            return None
        # Keep the value easy to inspect in logs/state and reject control
        # characters or whitespace that could corrupt headers and JSON.
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            return None
        return value

    def _require_device(self) -> str | None:
        device_id = self._device_id()
        if REQUIRE_DEVICE_ID and not device_id:
            self._reply(400, {"success": False, "error": f"{DEVICE_HEADER} header required"})
            return None
        return device_id

    def _rate_limited(self, bucket: str) -> bool:
        limit = TASK_RATE_LIMIT_REQUESTS if bucket == "task" else RATE_LIMIT_REQUESTS
        key = (self.client_address[0], bucket)
        now = time.monotonic()
        with _rate_lock:
            events = _rate_buckets.setdefault(key, deque())
            cutoff = now - RATE_LIMIT_WINDOW_SECONDS
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(events[0] + RATE_LIMIT_WINDOW_SECONDS - now))
                self._reply(429, {"success": False, "error": "rate limit exceeded"}, {"Retry-After": str(retry_after)})
                return True
            events.append(now)
        return False

    def log_message(self, fmt: str, *args) -> None:
        # Quiet access log: nginx already records the request line.
        pass

    def do_GET(self) -> None:
        if not self._authorized():
            self._reply(401, {"success": False, "error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if self._rate_limited("get"):
            return
        if path == "/health":
            self._reply(200, {"success": True, "service": "phone-agent-relay", "version": "0.10.0", "require_device_id": REQUIRE_DEVICE_ID,
                              "targeted_tasks": True, "targeted_polling": True})
            return
        if path == "/poll":
            query = parse_qs(parsed.query, keep_blank_values=True)
            targeted_values = query.get("targeted_only", ["false"])
            device_values = query.get("device_id", [])
            if (len(targeted_values) != 1 or targeted_values[0] not in {"true", "false"}
                    or len(device_values) > 1):
                self._reply(400, {"success": False, "error": "invalid poll parameters"})
                return
            targeted_only = targeted_values[0] == "true"
            header_device = self._device_id()
            query_device = device_values[0] if device_values else None
            if query_device is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", query_device):
                self._reply(400, {"success": False, "error": "invalid device_id"})
                return
            if header_device and query_device and header_device != query_device:
                self._reply(400, {"success": False, "error": "device_id disagrees with device header"})
                return
            device_id = query_device or header_device
            if (REQUIRE_DEVICE_ID or targeted_only) and not device_id:
                self._reply(400, {"success": False, "error": "device_id required"})
                return
            task = poll_task(device_id, targeted_only=targeted_only)
            self._reply(200, {"success": True, "task": task})
            return
        if path.startswith("/result/"):
            result = get_result(path.split("/")[-1])
            self._reply(200 if result else 404, {
                "success": result is not None,
                **({"result": result} if result else {"error": "task not found"}),
            })
            return
        self._reply(404, {"success": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._reply(401, {"success": False, "error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if self._rate_limited("task" if path == "/task" else "post"):
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, {"success": False, "error": "Transfer-Encoding is unsupported"})
            return
        try:
            if len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("duplicate Content-Length")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(413, {"success": False, "error": "request body too large"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete body")
            payload = json.loads(body.decode("utf-8")) if length else {}
        except TimeoutError:
            self._reply(408, {"success": False, "error": "request body timed out"})
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"success": False, "error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"success": False, "error": "JSON object required"})
            return
        if path == "/task":
            if not isinstance(payload, dict) or not str(payload.get("message", "")).strip():
                self._reply(400, {"success": False, "error": "message required"})
                return
            try:
                task = push_task(payload)
            except ValueError as exc:
                self._reply(400, {"success": False, "error": str(exc)})
                return
            except OverflowError as exc:
                self._reply(503, {"success": False, "error": str(exc)})
                return
            self._reply(200, {"success": True, "task_id": task["id"]})
            return
        if path == "/result":
            if not isinstance(payload.get("success"), bool):
                self._reply(400, {"success": False, "error": "success must be a boolean"})
                return
            task_id = str(payload.get("task_id", ""))
            device_id = self._require_device()
            if REQUIRE_DEVICE_ID and device_id is None:
                return
            if not task_id or not submit_result(
                task_id,
                bool(payload.get("success")),
                str(payload.get("ai_response", "")),
                str(payload.get("error", "")),
                device_id,
            ):
                self._reply(403, {"success": False, "error": "task not found or owned by another device"})
                return
            self._reply(200, {"success": True})
            return
        if path == "/renew":
            task_id = str(payload.get("task_id", ""))
            device_id = self._require_device()
            if REQUIRE_DEVICE_ID and device_id is None:
                return
            if not task_id or not renew_task(task_id, device_id):
                self._reply(403, {"success": False, "error": "task not found, not claimable, or owned by another device"})
                return
            self._reply(200, {"success": True})
            return
        self._reply(404, {"success": False, "error": "not found"})


def main() -> None:
    if not TOKEN:
        raise SystemExit("RELAY_TOKEN environment variable is required")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _load()
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)
    print(f"phone-agent relay listening on {HOST}:{PORT}, state={STATE_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
