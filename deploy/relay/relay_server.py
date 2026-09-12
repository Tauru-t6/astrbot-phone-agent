#!/usr/bin/env python3
"""Phone Agent relay: a tiny public queue between AstrBot and the phone.

The phone's Operit workflow polls GET /poll; AstrBot's plugin pushes tasks
with POST /task and reads results with GET /result/<id>. All endpoints
require the shared bearer token. State is a single JSON file so a restart
never loses pending work.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATE_PATH = Path(os.environ.get("RELAY_STATE", Path(__file__).parent / "relay_state.json"))
TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8791"))
MAX_TASKS = 64
TASK_TTL_SECONDS = 24 * 3600
RESULT_TTL_SECONDS = 24 * 3600
CLAIM_LEASE_SECONDS = 5 * 60
MAX_BODY_BYTES = 64 * 1024
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
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _load() -> None:
    global _state
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("tasks"), list):
                _state = loaded
        except (OSError, json.JSONDecodeError):
            pass


def _gc_locked(now: float) -> None:
    for task in _state["tasks"]:
        # Claims created by older relay versions have no owner. Reset them so
        # strict device mode never leaves an uncompletable task behind.
        if task.get("status") == "claimed" and REQUIRE_DEVICE_ID and not task.get("device_id"):
            task["status"] = "pending"
            task.pop("claimed_at", None)
        if task.get("status") in {"claimed", "running"} and now - task.get("claimed_at", task.get("created_at", now)) >= CLAIM_LEASE_SECONDS:
            task["status"] = "pending"
            task.pop("claimed_at", None)
            task.pop("device_id", None)
    _state["tasks"] = [
        t for t in _state["tasks"]
        if now - t.get("created_at", 0) < TASK_TTL_SECONDS
    ]
    _state["results"] = {
        k: v for k, v in _state["results"].items()
        if now - v.get("finished_at", 0) < RESULT_TTL_SECONDS
    }


def push_task(payload: dict) -> dict:
    now = time.time()
    with _lock:
        _gc_locked(now)
        if len([t for t in _state["tasks"] if t.get("status") == "pending"]) >= MAX_TASKS:
            raise OverflowError("pending task queue is full")
        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "message": str(payload.get("message", ""))[:2000],
            "created_at": now,
            "status": "pending",
        }
        _state["tasks"].append(task)
        _persist()
        return task


def poll_task(device_id: str | None = None) -> dict | None:
    now = time.time()
    with _lock:
        _gc_locked(now)
        for task in _state["tasks"]:
            if task["status"] == "pending":
                task["status"] = "claimed"
                task["claimed_at"] = now
                if device_id:
                    task["device_id"] = device_id
                _persist()
                return task
    return None


def submit_result(task_id: str, success: bool, ai_response: str, error: str = "", device_id: str | None = None) -> bool:
    now = time.time()
    with _lock:
        task = next((t for t in _state["tasks"] if t["id"] == task_id), None)
        if task is None or task["status"] not in {"pending", "claimed", "running"}:
            return False
        if task.get("device_id") and task.get("device_id") != device_id:
            return False
        task["status"] = "done"
        _state["results"][task_id] = {
            "task_id": task_id,
            "message": task["message"],
            "success": bool(success),
            "ai_response": str(ai_response)[-6000:],
            "error": str(error)[:500],
            "finished_at": now,
        }
        _persist()
        return True


def renew_task(task_id: str, device_id: str | None = None) -> bool:
    now = time.time()
    with _lock:
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

    def _reply(self, code: int, body: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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
        path = urlparse(self.path).path
        if self._rate_limited("get"):
            return
        if path == "/health":
            self._reply(200, {"success": True, "service": "phone-agent-relay", "require_device_id": REQUIRE_DEVICE_ID})
            return
        if path == "/poll":
            device_id = self._require_device()
            if REQUIRE_DEVICE_ID and device_id is None:
                return
            task = poll_task(device_id)
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                self._reply(413, {"success": False, "error": "request body too large"}, {"Connection": "close"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"success": False, "error": "invalid JSON"})
            return
        if path == "/task":
            if not isinstance(payload, dict) or not str(payload.get("message", "")).strip():
                self._reply(400, {"success": False, "error": "message required"})
                return
            try:
                task = push_task(payload)
            except OverflowError as exc:
                self._reply(503, {"success": False, "error": str(exc)})
                return
            self._reply(200, {"success": True, "task_id": task["id"]})
            return
        if path == "/result":
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
