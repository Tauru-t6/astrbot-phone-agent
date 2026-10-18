from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import importlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Star
from quart import jsonify, request

try:
    from .device_app import (
        COMMAND_ACTIONS,
        DANGEROUS_ACTIONS,
        MAX_SCREENSHOT_BYTES,
        DeviceAppBackend,
        plugin_data_path,
    )
except ImportError:  # pragma: no cover - direct-module test harness
    from device_app import (
        COMMAND_ACTIONS,
        DANGEROUS_ACTIONS,
        MAX_SCREENSHOT_BYTES,
        DeviceAppBackend,
        plugin_data_path,
    )

try:
    from .task_runner import (
        APPROVAL_PENDING,
        ApprovalRequest,
        GRANT_SOURCE_APPROVAL,
        ReadOnlyTaskRunner,
        RUN_CANCELLED,
        RUN_WAITING_APPROVAL,
        ScopedGrant,
        TaskExecutor,
        TaskPlanner,
        TaskRunStore,
        WRITE_ACTIONS,
    )
except ImportError:  # pragma: no cover - direct-module test harness
    from task_runner import (
        APPROVAL_PENDING,
        ApprovalRequest,
        GRANT_SOURCE_APPROVAL,
        ReadOnlyTaskRunner,
        RUN_CANCELLED,
        RUN_WAITING_APPROVAL,
        ScopedGrant,
        TaskExecutor,
        TaskPlanner,
        TaskRunStore,
        WRITE_ACTIONS,
    )

try:  # pragma: no cover - the test harness stubs only astrbot.api.event
    from astrbot.api.message_components import Image as _ImageComponent
except Exception:  # pragma: no cover
    _ImageComponent = None


PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
ALLOWED_ACTIONS = {
    "open_app",
    "close_app",
    "tap",
    "long_press",
    "swipe",
    "scroll",
    "input_text",
    "key_event",
    "back",
    "home",
    "lock_screen",
    "wake_screen",
    "trigger_workflow",
    "status",
    "foreground_app",
    "suspend_app",
    "unsuspend_app",
    "suspend_video_apps",
    "unsuspend_video_apps",
    "screen_text",
}

LOCATION_ACTIVITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
PLUGIN_DATA_DIR = Path("data") / "plugin_data" / "astrbot_plugin_phone_agent"
POLICY_ACTIONS = {"disable", "enable", "suspend", "unsuspend"}
PROTECTED_POLICY_PACKAGES = frozenset({
    "com.android.settings",
    "com.android.systemui",
    "com.ai.assistance.operit",
    "com.tauru.astrbotphoneagent",
})

# Deterministic night guard. The LLM never decides whether to suspend; it only
# sees the warning event if a companion bridge forwards one. Phases follow the
# handoff state machine: watching -> warned -> enforced -> user_released.
NG_PHASE_WATCHING = "watching"
NG_PHASE_WARNED = "warned"
NG_PHASE_ENFORCED = "enforced"
NG_PHASE_RELEASED = "user_released"
NG_PHASES = {NG_PHASE_WATCHING, NG_PHASE_WARNED, NG_PHASE_ENFORCED, NG_PHASE_RELEASED}
NG_DEFAULT_PACKAGES = "哔哩哔哩,快手极速版,优酷,抖音,抖音极速版"
NG_WINDOW_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# P1-A: a policy lease is one source's claim on a suspended package. Each lease
# owns its own deadline and its own timer, so releasing one source (a manual
# stop, the night guard's window ending) never cancels another source's
# countdown. Leases persist as {"version": 2, "packages": {pkg: {source: {...}}}}.
POLICY_STATE_VERSION = 2
POLICY_LEGACY_SOURCE = "manual"


class _PolicyLease:
    """One source's claim on a suspended package (P1-A).

    A plain class rather than a dataclass: the plugin module is loaded by path
    in tests, so ``__module__`` is not in ``sys.modules`` and the dataclass
    decorator cannot resolve string annotations there.
    """

    __slots__ = ("source", "until", "task")

    def __init__(self, source: str, until: float | None = None) -> None:
        self.source = source
        self.until = until
        self.task: asyncio.Task[Any] | None = None

    def __repr__(self) -> str:  # keep test failures readable
        return f"_PolicyLease(source={self.source!r}, until={self.until!r})"
NG_RELEASE_RE = re.compile(
    r"(解开|解除(限制|管控|暂停|视频限制)?|恢复(视频|应用|视频应用)?|"
    r"让我(继续|用)|(继续|接着)刷|unsuspend|release|unlock)"
)


def _text(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _first_json_object(value: Any) -> dict[str, Any] | None:
    """Extract the first JSON object without trusting surrounding model text."""

    text = re.sub(r"<meta[\s\S]*?</meta>", "", str(value or ""), flags=re.I).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tolerate_nested_args(handler):
    """Unwrap tool arguments that some models nest under an extra 'arguments' key."""

    @functools.wraps(handler)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        nested = kwargs.pop("arguments", None)
        if isinstance(nested, str) and nested.strip().startswith("{"):
            try:
                nested = json.loads(nested)
            except json.JSONDecodeError:
                nested = None
        if isinstance(nested, dict):
            for key, value in nested.items():
                kwargs.setdefault(key, value)
        return await handler(self, event, *args, **kwargs)

    return wrapper


def _feature_gate(tool_name: str):
    """Deny optional tools with a clear message when their feature is off."""

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(self, event, *args, **kwargs):
            if not self._tool_enabled(tool_name):
                return json.dumps({
                    "success": False,
                    "error": f"{tool_name} is disabled; enable its feature flag in the plugin config",
                }, ensure_ascii=False)
            return await handler(self, event, *args, **kwargs)

        return wrapper

    return decorator


class PhoneAgentPlugin(Star):
    def __init__(self, context: Any, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._active_serial: str | None = None
        self._guard_suspended: set[str] = set()
        # B02/P1-A: per-source leases. A package stays suspended while at least
        # one source holds a lease; the actual unsuspend is only sent when the
        # last owner releases. Each lease carries its own deadline and timer so
        # a manual stop never cancels the night guard's countdown (and vice
        # versa), and a restart restores both ownership and remaining time.
        self._policy_leases: dict[str, dict[str, _PolicyLease]] = {}
        self._manual_guard_until: datetime | None = None
        self._manual_guard_packages: set[str] | None = None
        self._manual_guard_exempt: set[str] = set()
        self._operit_tasks: dict[str, dict[str, Any]] = {}
        self._operit_task_handles: dict[str, asyncio.Task[Any]] = {}
        self._max_background_tasks = 2
        self._load_operit_tasks()
        self._reminder_tasks: dict[str, asyncio.Task[Any]] = {}
        self._reminders: dict[str, dict[str, Any]] = {}
        self._reminder_requests: dict[str, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._load_policy_state()
        self.device_app = DeviceAppBackend(config, self._audit)
        # P1-E stage 2: run records are persisted, but only read-only steps
        # (observe) ever touch the device; write steps are simulated.
        self._task_runs = TaskRunStore(self._task_run_path())
        self._task_runs.load()
        self._task_planner = TaskPlanner(self._app_aliases())
        self._task_runner = ReadOnlyTaskRunner(
            self._task_runs,
            self._task_planner,
            lambda: self.device_app.last_status() or {},
        )
        # P1-E stage 3: real execution, off by default until the operator opts in.
        self._task_executor = TaskExecutor(
            self._task_runs,
            self._task_planner,
            observe=self._task_observe_cached,
            observe_live=self._task_observe_live,
            act=self._task_act,
            allowed_actions=self._task_allowed_actions(),
            protected_packages=PROTECTED_POLICY_PACKAGES,
            step_timeout=self._task_float("task_step_timeout_seconds", 20.0, 5.0, 60.0),
            run_timeout=self._task_float("task_run_timeout_seconds", 90.0, 10.0, 300.0),
            observation_max_age=float(self._night_guard_int("night_guard_status_max_age_seconds", 90, 10, 3600)),
            require_user_idle=self._bool_config("task_require_user_idle", True),
        )
        self._load_reminders()
        self._load_notifications()
        self._night_guard_task: asyncio.Task[Any] | None = None
        self._night_guard_states: dict[str, dict[str, Any]] = {}
        self._night_guard_lock = asyncio.Lock()
        self._load_night_guard_state()
        self._autonomy_task: asyncio.Task[Any] | None = None
        self._register_web_api()
        self._start_night_guard()
        self._start_autonomy_scheduler()

    async def terminate(self) -> None:
        autonomy = self._autonomy_task
        self._autonomy_task = None
        if autonomy is not None:
            autonomy.cancel()
            await asyncio.gather(autonomy, return_exceptions=True)
        task = self._night_guard_task
        self._night_guard_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._save_night_guard_state()
        self._task_runs.save()
        lease_tasks = [
            lease.task
            for leases in self._policy_leases.values()
            for lease in leases.values()
            if lease.task is not None
        ]
        tasks = [*self._reminder_tasks.values(), *lease_tasks, *self._operit_task_handles.values()]
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Temporary leases cannot outlive the event loop, so release exactly the
        # sources that own them at shutdown. Permanent manual locks stay in the
        # state file and are reloaded on the next startup instead.
        temporary = [
            (package, source)
            for package, leases in list(self._policy_leases.items())
            for source, lease in list(leases.items())
            if lease.until is not None
        ]
        for package, source in temporary:
            await self._execute_app_policy(package, "enable", source=source)

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no", "disabled"}
        return bool(value)

    # Feature flags: core Operit phone control is always on; the rest is
    # opt-in per deployment so a fresh install only exposes what it uses.
    def _feature(self, key: str) -> bool:
        return self._bool_config("enable_" + key, False)

    def _tool_enabled(self, name: str) -> bool:
        groups = {
            "phone_observe": ("observe_tool",),
            "phone_health": ("health_tools",),
            "phone_usage": ("usage_tool",),
            "phone_location": ("location_tool",),
            "phone_reminder": ("reminder_tools",),
            "phone_audit": ("audit_tool",),
            "phone_app_policy": ("policy_tools",),
            "phone_sleep_mode": ("policy_tools",),
            "phone_screenshot": ("screenshot_tool",),
            "phone_notifications": ("notification_tool",),
        }
        keys = groups.get(name)
        if not keys:
            return True
        return all(self._feature(k) for k in keys)

    def _register_web_api(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        prefix = "/astrbot_plugin_phone_agent"
        routes = (
            ("/status", self._web_status, ["GET"], "Phone Agent status"),
            ("/config", self._web_get_config, ["GET"], "Phone Agent configuration"),
            ("/config", self._web_save_config, ["POST"], "Save Phone Agent configuration"),
            ("/test_operit", self._web_test_operit, ["POST"], "Test Operit connection"),
            ("/test_backend", self._web_test_backend, ["POST"], "Test the selected phone backend"),
            ("/app_policy", self._web_app_policy, ["POST"], "Apply an on-demand app policy", "policy_tools"),
            ("/sleep_mode", self._web_sleep_mode, ["POST"], "Control temporary sleep mode", "policy_tools"),
            ("/location", self._web_location, ["GET"], "Read phone location on demand", "location_tool"),
            ("/health", self._web_health, ["GET"], "Read phone health summary", "health_tools"),
            ("/tasks", self._web_tasks, ["GET"], "List Operit tasks", None),
            ("/reminders", self._web_reminders, ["GET"], "List phone reminders", "reminder_tools"),
            ("/reminders", self._web_reminders, ["POST"], "Cancel a phone reminder", "reminder_tools"),
            ("/audit", self._web_audit, ["GET"], "List phone action audit", "audit_tool"),
            ("/device/register", self._web_device_register, ["POST"], "Register the phone app device endpoint", None),
            ("/device/status", self._web_device_status, ["GET"], "Phone app device status and recent commands", None),
            ("/device/status", self._web_device_status_post, ["POST"], "Phone app periodic status heartbeat", None),
            ("/device/command", self._web_device_command, ["POST"], "Send one structured command to the phone app", None),
            ("/device/test", self._web_device_test, ["POST"], "Ping the phone app through the current backend", None),
            ("/device/timeline", self._web_device_timeline, ["GET"], "Timeline items for the phone app sync", None),
            ("/app/state", self._web_app_state, ["GET"], "AstrBot Phone Agent App state snapshot", None),
            ("/app/reminders", self._web_app_reminder_create, ["POST"], "Create an App reminder", None),
        ("/app/task/plan", self._web_app_task_plan, ["POST"],
         "Plan a task and dry-run it without touching the phone", "observe_tool"),
        ("/app/task/run", self._web_app_task_run, ["POST"],
         "Execute a granted task and verify each step", None),
        ("/app/task/cancel", self._web_app_task_cancel, ["POST"], "Cancel a running task", None),
        ("/app/task/propose", self._web_app_task_propose, ["POST"],
         "Plan a task and wait for a human decision", None),
        ("/app/task/approve", self._web_app_task_approve, ["POST"], "Approve a proposed task", None),
        ("/app/task/reject", self._web_app_task_reject, ["POST"], "Reject a proposed task", None),
        ("/app/grant", self._web_app_grant_create, ["POST"], "Issue a scoped action grant", None),
            ("/app/reminders/cancel", self._web_app_reminder_cancel, ["POST"], "Cancel an App reminder", None),
            ("/app/notifications", self._web_app_notifications, ["GET", "POST"],
             "Forwarded phone notifications", "notification_tool"),
        )
        for route in routes:
            gate = route[4] if len(route) > 4 else None
            if gate and not self._feature(gate):
                continue
            register(prefix + route[0], route[1], route[2], route[3])

    def _web_config_view(self) -> dict[str, Any]:
        keys = (
            "enabled", "control_backend", "operit_base_url", "allowed_user_ids",
            "use_private_companion_auth", "app_aliases_json", "sleep_guard_packages",
            "sleep_guard_exempt_apps", "operit_timeout_seconds", "max_background_tasks", "tasks_path",
            "app_command_timeout_seconds",
            "app_direct_urls",
        )
        result = {key: self.config.get(key) for key in keys}
        result["control_backend"] = self._control_backend()
        result["operit_token_configured"] = bool(self._operit_token())
        result["app_shared_token_configured"] = bool(self.device_app.shared_token())
        result["sleep_guard_mode"] = "on_demand"
        return result

    async def _web_get_config(self):
        return jsonify({"success": True, "config": self._web_config_view()})

    async def _web_save_config(self):
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        bool_keys = {"enabled", "use_private_companion_auth"}
        int_keys = {"operit_timeout_seconds", "max_background_tasks", "app_command_timeout_seconds"}
        text_keys = {
            "control_backend", "operit_base_url", "operit_token", "allowed_user_ids",
            "app_aliases_json", "sleep_guard_packages", "sleep_guard_exempt_apps",
            "tasks_path", "app_shared_token", "app_direct_urls",
        }
        updates: dict[str, Any] = {}
        for key, value in payload.items():
            if key in bool_keys:
                updates[key] = self._coerce_bool(value)
            elif key in int_keys:
                try:
                    updates[key] = max(1, min(int(value), 300))
                except (TypeError, ValueError):
                    return jsonify({"success": False, "error": f"invalid integer: {key}"}), 400
            elif key in text_keys:
                value = str(value or "").strip()
                if len(value) > 2000:
                    return jsonify({"success": False, "error": f"value too long: {key}"}), 400
                if key == "control_backend":
                    value = self._normalize_backend(value)
                    if value not in {"operit", "adb", "app"}:
                        return jsonify({"success": False, "error": "control_backend must be operit, app or legacy adb"}), 400
                if key == "app_direct_urls" and any(not self.device_app.valid_base_url(url.strip()) for url in value.split(",") if url.strip()):
                    return jsonify({"success": False, "error": "app_direct_urls must contain comma-separated http(s) base URLs"}), 400
                if key == "app_aliases_json" and value:
                    try:
                        parsed = json.loads(value)
                        if not isinstance(parsed, dict):
                            raise ValueError
                    except (json.JSONDecodeError, ValueError):
                        return jsonify({"success": False, "error": "app_aliases_json must be a JSON object"}), 400
                updates[key] = value
        if updates:
            self.config.update(updates)
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
        self._audit("web_config_saved", keys=sorted(updates))
        return jsonify({"success": True, "config": self._web_config_view()})

    async def _web_app_policy(self):
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        action = _text(payload.get("action"), 30).lower()
        package = self._resolve_package(payload.get("package"))
        try:
            minutes = max(0, min(int(payload.get("minutes", 0)), 1440))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "minutes must be an integer from 0 to 1440"}), 400
        if action not in POLICY_ACTIONS:
            return jsonify({"success": False, "error": "action must be disable or enable"}), 400
        if action in {"suspend", "unsuspend"}:
            action = "disable" if action == "suspend" else "enable"
        result = await self._execute_app_policy(package, action, minutes)
        self._audit(
            "web_app_policy",
            action=action,
            package=package,
            minutes=minutes,
            success=bool(result.get("success")),
        )
        return jsonify(result), 200 if result.get("success") else 502

    async def _web_location(self):
        if not self._enabled():
            return jsonify({"success": False, "error": "phone agent disabled"})
        if self._control_backend() != "app" and not self._operit_token():
            return jsonify({"success": False, "error": "Operit HTTP token is not configured"})
        try:
            high_accuracy = str(request.args.get("high_accuracy", "false")).lower() in {"1", "true", "yes", "on"}
            include_address = str(request.args.get("include_address", "false")).lower() in {"1", "true", "yes", "on"}
            timeout_seconds = max(3, min(int(request.args.get("timeout", "10")), 30))
        except (TypeError, ValueError):
            high_accuracy, include_address, timeout_seconds = False, False, 10
        try:
            if self._control_backend() == "app":
                return jsonify(await asyncio.to_thread(self.device_app.execute, "location", {
                    "high_accuracy": high_accuracy, "include_address": include_address, "timeout_seconds": timeout_seconds}))
            result = await asyncio.to_thread(self._operit_location_sync, high_accuracy, timeout_seconds, include_address)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"success": False, "error": _text(exc, 240)}), 502

    async def _web_test_operit(self):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._operit_health_sync), timeout=10)
        except asyncio.TimeoutError:
            result = {"available": False, "error": "Operit health check timed out"}
        return jsonify({"success": bool(result.get("available")), "operit": result})

    async def _web_test_backend(self):
        if not self._web_write_allowed() or not self._enabled():
            return jsonify({"success": False, "error": "backend test is disabled or cross-origin"}), 403
        backend = self._control_backend()
        if backend == "app":
            result = await asyncio.to_thread(self.device_app.health)
        elif backend == "operit":
            result = await asyncio.to_thread(self._operit_health_sync)
        else:
            result = {"available": False, "error": "Legacy ADB diagnostics are available through phone tools"}
        return jsonify({"success": bool(result.get("available")), "backend": backend, "connection": result})

    @staticmethod
    def _web_write_allowed() -> bool:
        origin = str(request.headers.get("Origin") or "").strip()
        if not origin:
            return True
        return origin.rstrip("/") == request.host_url.rstrip("/")

    async def _web_status(self):
        backend = self._control_backend()
        device_app: dict[str, Any] | None = None
        if backend == "app":
            device_app = await asyncio.to_thread(self.device_app.health)
        elif backend == "operit":
            try:
                operit = await asyncio.wait_for(asyncio.to_thread(self._operit_health_sync), timeout=3)
            except asyncio.TimeoutError:
                operit = {"available": False, "error": "Operit health check timed out"}
        else:
            operit = None
        health = await asyncio.to_thread(self._read_health_db_sync, 1)
        return jsonify({
            "success": True,
            "control_backend": backend,
            "config": self._web_config_view(),
            "device_app": device_app,
            "operit": None if backend == "app" else operit,
            "device_last_status": self.device_app.last_status(),
            "health": {
                "available": health.get("available", False),
                "fresh": health.get("fresh", False),
                "days": len(health.get("days", [])),
                "latest": (health.get("days") or [None])[0],
                "sync": health.get("sync"),
                "error": health.get("error"),
            },
            "sleep_mode": {"manual_active": self._manual_guard_active(), "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None, "suspended": sorted(self._guard_suspended)},
            "night_guard": self._night_guard_public_view(),
            # P1-D: read-only snapshot written by the companion bridge.
            "care": self._care_status_snapshot(),
            # P1-E: latest runs so the dashboard can show what executed.
            "task_runs": self._task_runs.runs(limit=5),
            "tasks": len(self._operit_tasks),
            "reminders": len(self._reminders),
        })

    async def _web_sleep_mode(self):
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}
        mode = _text(payload.get("mode", "status"), 20).lower()
        if mode in {"start", "on", "enable"}:
            try:
                minutes = max(5, min(int(payload.get("minutes", 480)), 1440))
            except (TypeError, ValueError):
                minutes = 480
            requested = {self._resolve_package(value) for value in re.split(r"[,\s]+", str(payload.get("packages", "") or "")) if PACKAGE_RE.fullmatch(self._resolve_package(value))}
            exempt = {self._resolve_package(value) for value in re.split(r"[,\s]+", str(payload.get("exempt", "") or "")) if PACKAGE_RE.fullmatch(self._resolve_package(value))}
            self._manual_guard_exempt = exempt
            self._manual_guard_packages = requested or self._guard_packages()
            self._manual_guard_until = datetime.now() + timedelta(minutes=minutes)
            result = await self._apply_sleep_mode(self._manual_guard_packages, minutes)
            if not result.get("success"):
                self._manual_guard_until = None
                self._manual_guard_packages = None
                self._manual_guard_exempt = set()
                return jsonify(result), 502
            self._audit("web_sleep_mode_started", minutes=minutes, packages=sorted(self._manual_guard_packages))
            self.device_app.timeline_append(
                "reminder",
                "守夜模式开启",
                f"{minutes} 分钟后自动恢复 {len(self._manual_guard_packages)} 个应用",
            )
        elif mode in {"stop", "off", "disable", "unlock"}:
            self._manual_guard_until = None
            self._manual_guard_packages = None
            self._manual_guard_exempt = set()
            outcome = await self._restore_guard_apps()
            self._audit(
                "web_sleep_mode_stopped",
                released=outcome["released"],
                still_suspended=outcome["still_suspended"],
                restore_pending=outcome["restore_pending"],
            )
            if outcome["still_suspended"] or outcome["restore_pending"]:
                # v4: releasing the manual intent is not the same as the device
                # being restored. Report what actually happened per package.
                parts: list[str] = []
                if outcome["released"]:
                    parts.append(f"已恢复 {len(outcome['released'])} 个")
                if outcome["still_suspended"]:
                    parts.append(f"{len(outcome['still_suspended'])} 个仍被其他限制持有")
                if outcome["restore_pending"]:
                    parts.append(f"{len(outcome['restore_pending'])} 个待重试")
                self.device_app.timeline_append("reminder", "守夜模式解除（部分）", "，".join(parts))
            else:
                self.device_app.timeline_append("reminder", "守夜模式解除", "受限应用已全部恢复")
        return jsonify({"success": True, "mode": "active" if self._manual_guard_active() else "off", "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None, "suspended": sorted(self._guard_suspended)})

    async def _web_health(self):
        try:
            days = max(1, min(int(request.args.get("days", "7")), 30))
        except ValueError:
            days = 7
        if not self._health_db():
            return jsonify({
                "success": True,
                "health": {
                    "source": "xiaomi-sync",
                    "available": False,
                    "fresh": False,
                    "error": "health_db_path is not configured; point it to xiaomi-sync/data/health.db",
                },
            })
        return jsonify({"success": True, "health": await asyncio.to_thread(self._read_health_db_sync, days)})

    async def _web_tasks(self):
        return jsonify({"success": True, "tasks": self._operit_tasks})

    async def _web_reminders(self):
        if request.method == "POST":
            if not self._web_write_allowed():
                return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
            payload = await request.get_json(silent=True)
            if not isinstance(payload, dict) or not self._finite_json(payload):
                return jsonify({"success": False, "error": "JSON object required"}), 400
            if not self._enabled() or not self._feature("reminder_tools"):
                return jsonify({"success": False, "error": "reminders disabled"}), 403
            reminder_id = _text(payload.get("reminder_id"), 80)
            if _text(payload.get("action"), 20).lower() not in {"cancel", "remove", "delete"} or not reminder_id:
                return jsonify({"success": False, "error": "action=cancel and reminder_id are required"}), 400
            cancelled = self._cancel_reminder(reminder_id)
            self._audit("web_reminder_cancelled", reminder_id=reminder_id, success=cancelled)
            return jsonify({"success": cancelled, "reminder_id": reminder_id, "status": "cancelled" if cancelled else "not_found"})
        return jsonify({"success": True, "reminders": self._reminders})

    async def _web_audit(self):
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        records: list[dict[str, Any]] = []
        try:
            lines = Path(self._audit_path()).read_text(encoding="utf-8").splitlines()[-limit:]
            for line in lines:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass
        return jsonify({"success": True, "records": records})

    # ==================== Device-app backend (control_backend=app) ====================

    async def _web_device_register(self):
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        result = self.device_app.register(payload)
        self._audit(
            "device_register",
            device_id=_text(payload.get("device_id"), 64),
            success=bool(result.get("success")),
        )
        return jsonify(result), 200 if result.get("success") else 400

    async def _web_device_status(self):
        registration = self.device_app.registration()
        health = None
        if registration:
            health = await asyncio.to_thread(self.device_app.health)
        return jsonify({
            "success": True,
            "backend": self._control_backend(),
            "registration": registration,
            "health": health,
            "last_status": self.device_app.last_status(),
            "recent_results": self.device_app.recent_results(),
            "schema_version": 1,
            "accepted_actions": sorted(COMMAND_ACTIONS),
        })

    async def _web_device_status_post(self):
        """Phone app heartbeat: fresh foreground state for the night guard.

        The GET view stays dashboard-oriented; this write endpoint only accepts
        a bounded device-status object, never screen text or tokens.
        """
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or not self._finite_json(payload):
            return jsonify({"success": False, "error": "valid JSON object required"}), 400
        registration = self.device_app.registration()
        device_id = _text(payload.get("device_id"), 64)
        if not device_id or not registration:
            return jsonify({"success": False, "error": "device has not registered"}), 400
        if device_id != registration.get("device_id"):
            return jsonify({"success": False, "error": "device_id does not match the registered device"}), 400
        foreground = _text(payload.get("foreground_package"), 160)
        if foreground and not PACKAGE_RE.fullmatch(foreground):
            foreground = ""
        status = {
            "battery_percent": payload.get("battery_percent"),
            "charging": payload.get("charging"),
            "foreground_package": foreground,
            "screen_on_seconds_today": payload.get("screen_on_seconds_today"),
            "screen_interactive": payload.get("screen_interactive"),
            "shizuku_ready": payload.get("shizuku_ready"),
            "app_version": _text(payload.get("app_version"), 40),
            # Diagnostic only: why the app could not read a foreground package.
            "foreground_debug": _text(payload.get("foreground_debug"), 160),
            "reported_at": payload.get("reported_at"),
        }
        self.device_app.record_device_status(status)
        self._audit(
            "device_status_received",
            device_id=device_id,
            foreground_package=foreground or "",
            screen_interactive=status["screen_interactive"],
        )
        return jsonify({"success": True, "received": True})

    async def _web_device_command(self):
        if not self._web_write_allowed():
            return jsonify({"success": False, "error": "cross-origin write rejected"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        action = _text(payload.get("action"), 40).lower()
        if action not in COMMAND_ACTIONS:
            return jsonify({"success": False, "error": f"unsupported action; allowed: {', '.join(sorted(COMMAND_ACTIONS))}"}), 400
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if action in {"suspend_app", "unsuspend_app", "open_app", "close_app"}:
            args = dict(args)
            args["package"] = self._resolve_package(args.get("package"))
        result = await asyncio.to_thread(self.device_app.execute, action, args)
        return jsonify(result), 200 if result.get("success") else 502

    async def _web_device_test(self):
        result = await asyncio.to_thread(self.device_app.execute, "ping", {})
        return jsonify({"success": bool(result.get("success")), "result": result})

    async def _web_device_timeline(self):
        try:
            since = float(request.args.get("since", "0"))
            if not math.isfinite(since):
                since = 0.0
        except (TypeError, ValueError):
            since = 0.0
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 100))
        except (TypeError, ValueError):
            limit = 50
        return jsonify({"success": True, "items": self.device_app.timeline_since(since, limit)})

    def _app_api_write_allowed(self, feature: str = "reminder_tools") -> tuple[bool, str]:
        if not self._enabled():
            return False, "phone agent disabled"
        if not self._feature(feature):
            return False, f"{feature} feature is disabled"
        if not self._web_write_allowed():
            return False, "cross-origin write rejected"
        return True, ""

    @staticmethod
    def _finite_json(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(PhoneAgentPlugin._finite_json(key) and PhoneAgentPlugin._finite_json(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return all(PhoneAgentPlugin._finite_json(item) for item in value)
        return True

    def _reminder_view(self, reminder_id: str, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": reminder_id,
            "text": str(item.get("text", ""))[:500],
            "due": float(item.get("due", 0)),
            "source": item.get("source") if item.get("source") in {"app", "chat"} else "chat",
            "status": item.get("status") or "pending",
        }

    async def _web_app_state(self):
        if not self._enabled():
            return jsonify({"success": False, "error": "phone agent disabled", "error_code": "disabled"}), 503
        reminders = [self._reminder_view(key, item) for key, item in self._reminders.items()
                     if item.get("status", "pending") == "pending"]
        # G2: expose night_guard and device_last_status in the App polling path
        # so the UI can show guard phase, device battery/screen, and sleep mode
        # without hitting the heavier /status dashboard endpoint.
        return jsonify({
            "success": True,
            "api_version": 3,
            "server_time": round(time.time(), 3),
            "backend": self._control_backend(),
            "features": {
                "reminders": self._feature("reminder_tools"),
                "audit": self._feature("audit_tool"),
                "policy": self._feature("policy_tools"),
                "observe": self._feature("observe_tool"),
                "screenshot": self._feature("screenshot_tool"),
                "notifications": self._feature("notification_tool"),
            },
            "reminders": reminders,
            "recent_commands": self.device_app.recent_results(),
            "timeline": self.device_app.timeline_since(0, 100),
            "device_last_status": self.device_app.last_status(),
            "night_guard": self._night_guard_public_view(),
            "sleep_mode": {
                "manual_active": self._manual_guard_active(),
                "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None,
                "suspended": sorted(self._guard_suspended),
            },
            # P1-D: read-only care snapshot written by the companion bridge.
            "care": self._care_status_snapshot(),
            # P1-E stage 2: dry-run task history; write steps are simulated.
            "task_runs": self._task_runs.runs(limit=5),
            "task_grants": self._task_runs.grants(),
            "task_execute_enabled": self._bool_config("enable_task_execute", False),
        })

    async def _web_app_task_propose(self):
        """Stage 4: plan a task and stop for a human decision."""
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        if not self._bool_config("enable_task_execute", False):
            return jsonify({"success": False, "error": "task execution is disabled",
                            "error_code": "disabled"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "valid JSON object required",
                            "error_code": "invalid_json"}), 400
        goal = _text(payload.get("goal"), 500)
        if not goal:
            return jsonify({"success": False, "error": "goal is required",
                            "error_code": "invalid_goal"}), 400
        registration = self.device_app.registration() or {}
        device_id = _text(registration.get("device_id"), 64)
        if not device_id:
            return jsonify({"success": False, "error": "no device registered",
                            "error_code": "no_device"}), 409
        subject = _text(payload.get("subject"), 120) or next(iter(sorted(self._allowed_users())), "")
        run = self._task_runner.start(goal, subject=subject, device_id=device_id, grant=None)
        actions = [step.action for step in run.steps if step.action in WRITE_ACTIONS]
        packages = [step.args.get("package") for step in run.steps if step.args.get("package")]
        if not actions:
            return jsonify({"success": False, "error": "the goal has nothing to approve",
                            "error_code": "nothing_to_approve"}), 409
        approval = ApprovalRequest.create(
            run_id=run.run_id, subject=subject, device_id=device_id,
            actions=actions, packages=packages,
            ttl_seconds=self._task_float("task_approval_ttl_seconds", 60.0, 10.0, 300.0),
        )
        run.transition(RUN_WAITING_APPROVAL)
        self._task_runs.add_run(run)
        self._task_runs.add_approval(approval)
        self._task_runs.save()
        self._audit("task_proposed", run_id=run.run_id, goal=goal[:120], actions=actions)
        return jsonify({"success": True, "run": run.to_dict(), "approval": approval.to_dict()}), 200

    async def _web_app_task_approve(self):
        """Stage 4: a human approved the proposal; issue the grant and run it."""
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        approval_id = _text((payload or {}).get("approval_id"), 64) if isinstance(payload, dict) else ""
        approval = self._task_runs.approval(approval_id) if approval_id else None
        if approval is None:
            return jsonify({"success": False, "error": "approval not found",
                            "error_code": "not_found"}), 404
        if not approval.pending():
            self._task_runs.save()
            return jsonify({"success": False, "error": "approval is no longer pending",
                            "error_code": "approval_not_pending",
                            "approval": approval.to_dict()}), 409
        run = self._task_runs.run(approval.run_id)
        if run is None:
            return jsonify({"success": False, "error": "run not found",
                            "error_code": "not_found"}), 404
        approval.decide(True, reason="approved")
        grant = ScopedGrant.issue(
            subject=approval.subject, device_id=approval.device_id,
            actions=list(approval.actions), packages=list(approval.packages),
            ttl_seconds=120.0, max_steps=max(1, len(approval.actions)),
            source=GRANT_SOURCE_APPROVAL,
        )
        self._task_runs.add_grant(grant)
        allow_active = bool(isinstance(payload, dict) and payload.get("allow_while_active") is True)
        run = await self._task_executor.execute(run, grant=grant, allow_active=allow_active)
        self._task_runs.save()
        self._audit("task_approved", run_id=run.run_id, approval_id=approval.approval_id,
                    status=run.status, verified=run.summary.get("verified", 0),
                    failed=run.summary.get("failed", 0), refused=run.summary.get("refused", ""))
        return jsonify({"success": True, "run": run.to_dict(), "approval": approval.to_dict()}), 200

    async def _web_app_task_reject(self):
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        approval_id = _text((payload or {}).get("approval_id"), 64) if isinstance(payload, dict) else ""
        approval = self._task_runs.approval(approval_id) if approval_id else None
        if approval is None:
            return jsonify({"success": False, "error": "approval not found",
                            "error_code": "not_found"}), 404
        reason = _text((payload or {}).get("reason"), 160) or "rejected"
        if not approval.decide(False, reason=reason):
            return jsonify({"success": False, "error": "approval is no longer pending",
                            "error_code": "approval_not_pending"}), 409
        run = self._task_runs.run(approval.run_id)
        if run is not None:
            run.transition(RUN_CANCELLED, error=reason)
        self._task_runs.save()
        self._audit("task_rejected", run_id=approval.run_id, approval_id=approval.approval_id,
                    reason=reason)
        return jsonify({"success": True, "approval": approval.to_dict()}), 200

    async def _web_app_task_run(self):
        """P1-E stage 3: execute a granted task and verify every step."""
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        if not self._bool_config("enable_task_execute", False):
            return jsonify({"success": False, "error": "task execution is disabled",
                            "error_code": "disabled"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "valid JSON object required",
                            "error_code": "invalid_json"}), 400
        goal = _text(payload.get("goal"), 500)
        if not goal:
            return jsonify({"success": False, "error": "goal is required",
                            "error_code": "invalid_goal"}), 400
        registration = self.device_app.registration() or {}
        device_id = _text(registration.get("device_id"), 64)
        if not device_id:
            return jsonify({"success": False, "error": "no device registered",
                            "error_code": "no_device"}), 409
        grant_id = _text(payload.get("grant_id"), 64)
        grant = self._task_runs.grant(grant_id) if grant_id else None
        if grant is None:
            return jsonify({"success": False, "error": "a valid grant_id is required",
                            "error_code": "grant_required"}), 409
        subject = _text(payload.get("subject"), 120) or next(iter(sorted(self._allowed_users())), "")
        run = self._task_runner.start(goal, subject=subject, device_id=device_id, grant=grant)
        allow_active = bool(payload.get("allow_while_active") is True)
        run = await self._task_executor.execute(run, grant=grant, allow_active=allow_active)
        self._audit(
            "task_execute",
            goal=goal[:120],
            subject=subject,
            status=run.status,
            allow_active=allow_active,
            verified=run.summary.get("verified", 0),
            failed=run.summary.get("failed", 0),
            refused=run.summary.get("refused", ""),
        )
        return jsonify({"success": True, "run": run.to_dict()}), 200

    async def _web_app_task_cancel(self):
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        run_id = _text((payload or {}).get("run_id"), 64) if isinstance(payload, dict) else ""
        run = self._task_executor.cancel(run_id) if run_id else None
        if run is None:
            return jsonify({"success": False, "error": "run not found", "error_code": "not_found"}), 404
        return jsonify({"success": True, "run": run.to_dict()}), 200

    async def _web_app_grant_create(self):
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "valid JSON object required",
                            "error_code": "invalid_json"}), 400
        raw_actions = payload.get("actions")
        actions = raw_actions if isinstance(raw_actions, list) else re.split(r"[,\s]+", str(raw_actions or ""))
        actions = [item for item in (_text(value, 40) for value in actions) if item in WRITE_ACTIONS]
        if not actions:
            return jsonify({"success": False, "error": "actions must name at least one write action",
                            "error_code": "invalid_actions"}), 400
        raw_packages = payload.get("packages")
        packages = raw_packages if isinstance(raw_packages, list) else re.split(r"[,\s]+", str(raw_packages or ""))
        packages = [item for item in (_text(value, 160) for value in packages) if PACKAGE_RE.fullmatch(item)]
        try:
            ttl = max(30, min(int(payload.get("ttl_seconds") or 120), 600))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "ttl_seconds must be an integer",
                            "error_code": "invalid_ttl"}), 400
        try:
            max_steps = max(1, min(int(payload.get("max_steps") or 1), 3))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "max_steps must be an integer",
                            "error_code": "invalid_steps"}), 400
        registration = self.device_app.registration() or {}
        device_id = _text(registration.get("device_id"), 64)
        if not device_id:
            return jsonify({"success": False, "error": "no device registered",
                            "error_code": "no_device"}), 409
        subject = _text(payload.get("subject"), 120) or next(iter(sorted(self._allowed_users())), "")
        grant = ScopedGrant.issue(
            subject=subject,
            device_id=device_id,
            actions=actions,
            packages=packages,
            ttl_seconds=ttl,
            max_steps=max_steps,
        )
        self._task_runs.add_grant(grant)
        self._task_runs.save()
        self._audit("grant_issued", subject=subject, actions=actions, packages=packages, ttl_seconds=ttl)
        return jsonify({"success": True, "grant": grant.to_dict()}), 200

    async def _web_app_task_plan(self):
        """P1-E stage 2: plan a goal and dry-run it without touching the phone."""
        allowed, error = self._app_api_write_allowed("observe_tool")
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "valid JSON object required",
                            "error_code": "invalid_json"}), 400
        goal = _text(payload.get("goal"), 500)
        if not goal:
            return jsonify({"success": False, "error": "goal is required",
                            "error_code": "invalid_goal"}), 400
        registration = self.device_app.registration() or {}
        device_id = _text(registration.get("device_id"), 64)
        if not device_id:
            return jsonify({"success": False, "error": "no device registered",
                            "error_code": "no_device"}), 409
        subject = _text(payload.get("subject"), 120) or next(iter(sorted(self._allowed_users())), "")
        grant = None
        grant_id = _text(payload.get("grant_id"), 64)
        if grant_id:
            grant = self._task_runs.grant(grant_id)
            if grant is None:
                return jsonify({"success": False, "error": "grant not found or expired",
                                "error_code": "grant_unusable"}), 409
        run = self._task_runner.start(goal, subject=subject, device_id=device_id, grant=grant)
        run = self._task_runner.execute(
            run,
            grant=grant,
            max_observation_age=self._night_guard_int("night_guard_status_max_age_seconds", 90, 10, 3600),
        )
        self._audit(
            "task_plan_dry_run",
            goal=goal[:120],
            subject=subject,
            simulated=run.summary.get("simulated", 0),
            blocked=run.summary.get("blocked", 0),
        )
        return jsonify({"success": True, "run": run.to_dict()}), 200

    async def _web_app_reminder_create(self):
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or not self._finite_json(payload):
            return jsonify({"success": False, "error": "valid JSON object required", "error_code": "invalid_json"}), 400
        text = payload.get("text")
        text = text.strip() if isinstance(text, str) else ""
        request_id = str(payload.get("request_id") or "").strip()
        if not text or len(text) > 500:
            return jsonify({"success": False, "error": "text must contain 1-500 characters", "error_code": "invalid_text"}), 400
        minutes = payload.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 10080:
            return jsonify({"success": False, "error": "minutes must be an integer from 1 to 10080", "error_code": "invalid_minutes"}), 400
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            return jsonify({"success": False, "error": "request_id must be a UUID", "error_code": "invalid_request_id"}), 400
        previous = self._reminder_requests.get(request_id)
        if previous:
            if previous["text"] != text or previous.get("minutes") != minutes:
                return jsonify({"success": False, "error": "request_id already belongs to a different reminder", "error_code": "request_conflict"}), 409
            return jsonify({"success": True, "reminder": {key: value for key, value in previous.items() if key not in {"minutes", "finished_at"}}, "idempotent": True})
        if len(self._reminders) >= 500:
            return jsonify({"success": False, "error": "too many pending reminders", "error_code": "reminder_limit"}), 429
        reminder_id = "app-" + uuid.uuid4().hex[:12]
        item = {"text": text, "session": "", "due": time.time() + minutes * 60,
                "source": "app", "status": "pending", "request_id": request_id}
        self._reminders[reminder_id] = item
        self._reminder_requests[request_id] = {**self._reminder_view(reminder_id, item), "minutes": minutes}
        try:
            self._save_reminders()
        except OSError:
            self._reminders.pop(reminder_id, None)
            self._reminder_requests.pop(request_id, None)
            return jsonify({"success": False, "error": "could not save reminder; please retry", "error_code": "storage_unavailable"}), 503
        self._reminder_tasks[reminder_id] = asyncio.create_task(self._run_reminder(reminder_id))
        self._audit("app_reminder_created", reminder_id=reminder_id, request_id=request_id, minutes=minutes)
        try:
            self.device_app.timeline_append("reminder", f"{minutes} 分钟后提醒你", text, event="reminder_created", reminder_id=reminder_id)
        except OSError:
            logger.warning("phone agent could not save reminder creation timeline")
        return jsonify({"success": True, "reminder": self._reminder_view(reminder_id, item)})

    async def _web_app_reminder_cancel(self):
        allowed, error = self._app_api_write_allowed()
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or not self._finite_json(payload):
            return jsonify({"success": False, "error": "valid JSON object required", "error_code": "invalid_json"}), 400
        reminder_id = _text(payload.get("reminder_id"), 80)
        if not reminder_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", reminder_id):
            return jsonify({"success": False, "error": "valid reminder_id required", "error_code": "invalid_reminder_id"}), 400
        try:
            cancelled = self._cancel_reminder(reminder_id)
        except OSError:
            return jsonify({"success": False, "error": "could not save cancellation; please retry", "error_code": "storage_unavailable"}), 503
        self._audit("app_reminder_cancelled", reminder_id=reminder_id)
        return jsonify({"success": True, "status": "cancelled" if cancelled else "already_finished", "reminder_id": reminder_id})

    def _enabled(self) -> bool:
        value = self.config.get("enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no"}
        return bool(value)

    def _adb(self) -> str:
        return _text(self.config.get("adb_path"), 300) or "adb"

    def _serial(self) -> str:
        return self._active_serial or _text(self.config.get("adb_serial"), 120)

    def _timeout(self) -> float:
        try:
            return max(3.0, min(float(self.config.get("command_timeout_seconds", 20)), 120.0))
        except (TypeError, ValueError):
            return 20.0

    def _health_db(self) -> str:
        return _text(self.config.get("health_db_path"), 500)

    def _audit_path(self) -> str:
        return str(self._plugin_data_path("audit_log_path", "phone_agent_audit.jsonl"))

    def _plugin_data_path(self, key: str, default_name: str) -> Path:
        raw = _text(self.config.get(key), 500)
        path = Path(raw) if raw else Path(default_name)
        base = PLUGIN_DATA_DIR.parts
        if not path.is_absolute() and path.parts[:len(base)] == base:
            return path
        return path if path.is_absolute() else PLUGIN_DATA_DIR / path

    def _audit(self, operation: str, **fields: Any) -> None:
        record = {"time": datetime.now().isoformat(timespec="seconds"), "operation": operation, **fields}
        try:
            path = Path(self._audit_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("phone agent audit write failed: %s", exc)

    def _night_guard_public_view(self) -> dict[str, Any]:
        running = self._night_guard_task is not None and not self._night_guard_task.done()
        states: dict[str, dict[str, Any]] = {}
        for user_id, state in self._night_guard_states.items():
            states[user_id] = {
                "phase": state.get("phase"),
                "foreground_package": state.get("foreground_package"),
                "owned_suspended": sorted(state.get("owned_suspended") or []),
                "warning_sent_at": state.get("warning_sent_at"),
                "enforced_until": state.get("enforced_until"),
                "enforcement_count": state.get("enforcement_count", 0),
                "last_error": _text(state.get("last_error"), 200),
            }
        return {
            "enabled": running,
            "window": f"{self._night_guard_window()[0]:02d}:{self._night_guard_window()[1]:02d}-"
                      f"{self._night_guard_window()[2]:02d}:{self._night_guard_window()[3]:02d}",
            "in_window": self._night_guard_in_window(),
            "dry_run": self._night_guard_dry_run(),
            "packages": sorted(self._night_guard_packages()),
            "states": states,
        }

    # ==================== night guard ====================

    def _night_guard_enabled(self) -> bool:
        return (
            self._enabled()
            and self._bool_config("enable_night_guard", False)
            and self._feature("policy_tools")
        )

    def _night_guard_dry_run(self) -> bool:
        return self._bool_config("night_guard_dry_run", True)

    @staticmethod
    def _parse_hhmm(value: Any, fallback: tuple[int, int]) -> tuple[int, int]:
        text = str(value or "").strip()
        match = NG_WINDOW_RE.fullmatch(text)
        if not match:
            return fallback
        return int(match.group(1)), int(match.group(2))

    def _night_guard_window(self) -> tuple[int, int, int, int]:
        start = self._parse_hhmm(self.config.get("night_guard_start", "00:30"), (0, 30))
        end = self._parse_hhmm(self.config.get("night_guard_end", "08:00"), (8, 0))
        return start[0], start[1], end[0], end[1]

    @staticmethod
    def _minutes_of_day(hour: int, minute: int) -> int:
        return hour * 60 + minute

    def _night_guard_in_window(self, now: datetime | None = None) -> bool:
        start_h, start_m, end_h, end_m = self._night_guard_window()
        start = self._minutes_of_day(start_h, start_m)
        end = self._minutes_of_day(end_h, end_m)
        current = self._minutes_of_day((now or datetime.now()).hour, (now or datetime.now()).minute)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        # A window crossing midnight is [start, 24:00) union [00:00, end).
        return current >= start or current < end

    def _night_guard_minutes_to_end(self, now: datetime | None = None) -> int:
        start_h, start_m, end_h, end_m = self._night_guard_window()
        start = self._minutes_of_day(start_h, start_m)
        end = self._minutes_of_day(end_h, end_m)
        now = now or datetime.now()
        current = self._minutes_of_day(now.hour, now.minute)
        if start < end:
            return max(0, end - current) if start <= current < end else 0
        if current >= start:
            return (24 * 60 - current) + end
        if current < end:
            return end - current
        return 0

    def _night_guard_packages(self) -> set[str]:
        raw = self.config.get("night_guard_packages", NG_DEFAULT_PACKAGES)
        values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
        packages = {self._resolve_package(value) for value in values}
        exempt_raw = self.config.get("night_guard_exempt_packages", "")
        exempt_values = exempt_raw if isinstance(exempt_raw, list) else re.split(r"[,\s]+", str(exempt_raw or ""))
        exempt = {self._resolve_package(value) for value in exempt_values}
        return {
            item for item in packages
            if PACKAGE_RE.fullmatch(item) and item not in exempt
            and not self._protected_policy_error(item)
        }

    def _start_night_guard(self) -> None:
        if self._night_guard_task is not None and not self._night_guard_task.done():
            return
        if not self._night_guard_enabled():
            return
        self._night_guard_task = asyncio.create_task(self._night_guard_loop())
        logger.info("phone agent night guard started (dry_run=%s)", self._night_guard_dry_run())

    # ---------- autonomy scheduler (P1-D) ----------
    #
    # The care decision itself lives in the companion bridge; this loop only
    # decides *when* to ask. It never fabricates a message and never bypasses
    # the bridge's own reservation/gating.

    def _autonomy_interval(self) -> int:
        try:
            return max(60, min(int(self.config.get("autonomy_check_interval_seconds", 300)), 3600))
        except (TypeError, ValueError):
            return 300

    def _companion_care(self) -> Any | None:
        """Find the companion bridge's evaluate_care(), if it is installed."""
        for name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            try:
                module = importlib.import_module(name)
            except Exception:
                continue
            provider = getattr(module, "_phone_companion_context", None)
            if provider is not None and callable(getattr(provider, "evaluate_care", None)):
                return provider
        return None

    def _care_targets(self) -> list[dict[str, Any]]:
        """Users the scheduler may evaluate: those with a recorded private session."""
        targets: list[dict[str, Any]] = []
        for user_id, state in sorted(self._night_guard_states.items()):
            session = _text(state.get("session"), 240)
            if session and ":FriendMessage:" in session:
                targets.append({"user_id": user_id, "umo": session})
        return targets

    def _care_device_fresh(self) -> bool:
        status = self.device_app.last_status() or {}
        reported = status.get("reported_at")
        if not isinstance(reported, (int, float)):
            return False
        max_age = self._night_guard_int("night_guard_status_max_age_seconds", 90, 10, 3600)
        return 0 <= time.time() - float(reported) <= max_age

    async def _autonomy_tick(self) -> None:
        provider = self._companion_care()
        if provider is None:
            return
        # Care is about the phone, so it only runs on a live observation.
        if not self._care_device_fresh():
            return
        for target in self._care_targets():
            try:
                result = await provider.evaluate_care(target["user_id"], target["umo"], "owner")
            except Exception as exc:
                logger.warning("phone agent autonomy tick failed: %s", _text(exc, 160))
                continue
            if result.get("evaluated"):
                logger.info(
                    "phone agent autonomy tick: user=%s outcome=%s reason=%s",
                    target["user_id"], result.get("outcome", ""), result.get("reason", ""),
                )

    async def _autonomy_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._autonomy_interval())
                try:
                    await self._autonomy_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("phone agent autonomy tick failed: %s", _text(exc, 200))
        except asyncio.CancelledError:
            return

    def _start_autonomy_scheduler(self) -> None:
        if self._autonomy_task is not None and not self._autonomy_task.done():
            return
        if not self._bool_config("enable_autonomy_scheduler", True):
            return
        self._autonomy_task = asyncio.create_task(self._autonomy_loop())
        logger.info("phone agent autonomy scheduler started (interval=%ss)", self._autonomy_interval())

    def _care_status_snapshot(self) -> dict[str, Any]:
        """Read the companion's read-only care snapshot, if it exists."""
        try:
            path = Path(self._care_status_path())
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _care_status_path(self) -> str:
        raw = _text(self.config.get("care_status_path", ""), 500)
        if raw:
            path = Path(raw)
            if path.is_absolute():
                return str(path)
            return str(PLUGIN_DATA_DIR / path.name)
        return str(self._plugin_data_path("care_status_path", "phone_agent_care_status.json"))

    def _night_guard_state_path(self) -> str:
        return str(self._plugin_data_path("night_guard_state_path", "phone_agent_night_guard.json"))

    def _save_night_guard_state(self) -> None:
        # B07: atomic write (temp + fsync + os.replace) matching the pattern
        # used by _save_reminders and device_app._save_timeline. A crash
        # between truncation and write must not produce a corrupt state file.
        path = Path(self._night_guard_state_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        users = {}
        for user_id, state in self._night_guard_states.items():
            users[user_id] = {
                key: state.get(key)
                for key in (
                    "session", "phase", "foreground_package", "first_seen_at",
                    "last_observed_at", "warning_sent_at", "warning_deadline",
                    "consecutive_hits", "owned_suspended", "enforced_until",
                    "released_until", "enforcement_count", "night_id",
                    "warned_date", "last_error",
                )
            }
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "users": users}, handle,
                          ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as exc:
            logger.warning("phone agent night guard state save failed: %s", exc)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_night_guard_state(self) -> None:
        try:
            path = Path(self._night_guard_state_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            users = values.get("users", {}) if isinstance(values, dict) else {}
            if not isinstance(users, dict):
                return
            for raw_user, raw_state in users.items():
                user_id = _text(raw_user, 120)
                if not user_id or not isinstance(raw_state, dict):
                    continue
                phase = raw_state.get("phase")
                state = dict(raw_state)
                state["phase"] = phase if phase in NG_PHASES else NG_PHASE_WATCHING
                owned = [
                    package for package in (state.get("owned_suspended") or [])
                    if isinstance(package, str) and PACKAGE_RE.fullmatch(package)
                ]
                state["owned_suspended"] = sorted(set(owned))
                state.setdefault("consecutive_hits", 0)
                state.setdefault("enforcement_count", 0)
                # B06: migrate old enforced_date to night_id; repair WARNED
                # states that have no valid deadline (deadlock guard).
                if "night_id" not in state:
                    state["night_id"] = state.pop("enforced_date", "")
                if state["phase"] == NG_PHASE_WARNED:
                    deadline = state.get("warning_deadline")
                    if not (isinstance(deadline, (int, float)) and math.isfinite(deadline) and deadline > 0):
                        state["phase"] = NG_PHASE_WATCHING
                        state["warning_sent_at"] = None
                        state["warning_deadline"] = None
                        state["consecutive_hits"] = 0
                self._night_guard_states[user_id] = state
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("phone agent night guard state file is invalid; ignoring it")

    def _night_guard_user_id(self, event: AstrMessageEvent) -> str:
        return self._event_user_id(event)

    def _night_guard_session(self, user_id: str) -> str:
        state = self._night_guard_states.get(user_id)
        session = _text((state or {}).get("session"), 240)
        if self._bool_config("night_guard_private_only", True) and ":FriendMessage:" not in session:
            return ""
        return session

    def _night_guard_ensure_state(self, user_id: str, session: str) -> dict[str, Any]:
        state = self._night_guard_states.get(user_id)
        if state is None:
            state = {
                "session": session,
                "phase": NG_PHASE_WATCHING,
                "foreground_package": "",
                "first_seen_at": None,
                "last_observed_at": None,
                "warning_sent_at": None,
                "warning_deadline": None,
                "consecutive_hits": 0,
                "owned_suspended": [],
                "enforced_until": None,
                "released_until": None,
                "enforcement_count": 0,
                "night_id": "",
                "warned_date": "",
                "last_error": "",
            }
            self._night_guard_states[user_id] = state
        else:
            state["session"] = session
        return state

    async def _night_guard_observe(self) -> dict[str, Any]:
        """Fresh foreground state, or an explicit skip reason. Never guesses."""
        status = self.device_app.last_status()
        max_age = self._night_guard_int("night_guard_status_max_age_seconds", 90, 30, 600)
        if isinstance(status, dict):
            reported_at = status.get("reported_at")
            # B04: reported_at is None when the phone clock was skewed or the
            # field was missing. That sample is NOT fresh — do not fall back
            # to received_at for freshness decisions.
            if reported_at is not None:
                try:
                    age = time.time() - float(reported_at)
                    fresh = math.isfinite(age) and 0 <= age <= max_age
                except (TypeError, ValueError):
                    fresh = False
            else:
                fresh = False
            if fresh:
                return {
                    "fresh": True,
                    "foreground_package": _text(status.get("foreground_package"), 160),
                    "shizuku_ready": bool(status.get("shizuku_ready")),
                    "screen_interactive": status.get("screen_interactive"),
                    "reported_at": reported_at,
                }
        # No heartbeat yet or it went stale: probe the phone once.
        if self._control_backend() != "app":
            return {"fresh": False, "error": "night guard requires control_backend=app"}
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.device_app.execute, "status", {}),
                timeout=20,
            )
        except asyncio.TimeoutError:
            return {"fresh": False, "error": "phone status probe timed out"}
        if not result.get("success"):
            return {"fresh": False, "error": _text(result.get("error"), 200)}
        output = result.get("output")
        if not isinstance(output, dict):
            return {"fresh": False, "error": "phone returned no status payload"}
        self.device_app.record_device_status(output.get("device") or output)
        return {
            "fresh": True,
            "foreground_package": _text(output.get("foreground_package"), 160),
            "shizuku_ready": bool(output.get("shizuku_ready")),
            "screen_interactive": output.get("screen_interactive"),
        }

    def _night_guard_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(self.config.get(key, default)), high))
        except (TypeError, ValueError):
            return default

    async def _night_guard_restore_owned(self, user_id: str, state: dict[str, Any], reason: str) -> tuple[list[str], list[str], list[str]]:
        """Restore only the packages this guard suspended. Never global unlocks.

        Returns (restored, still_suspended, still_pending):
        - restored: our lease was released AND the device confirmed the app is
          usable again.
        - still_suspended: our lease was released, but another source (for
          example a manual lock) still holds the package. The guard must not
          report it as restored, and must not release that other owner either.
        - still_pending: the device did not confirm; keep the package in
          owned_suspended so the next tick retries (B03).
        """
        restored: list[str] = []
        still_suspended: list[str] = []
        still_pending: list[str] = []
        for package in sorted(set(state.get("owned_suspended") or [])):
            result = await self._execute_app_policy(package, "enable", source="night_guard")
            if result.get("success") and result.get("mode") == "still_suspended":
                still_suspended.append(package)
                state["owned_suspended"] = sorted(
                    set(state.get("owned_suspended") or []) - {package}
                )
            elif result.get("success"):
                restored.append(package)
                state["owned_suspended"] = sorted(
                    set(state.get("owned_suspended") or []) - {package}
                )
            else:
                still_pending.append(package)
            self._audit(
                "night_guard_restore",
                user_id=user_id,
                package=package,
                reason=reason,
                success=bool(result.get("success")),
                verified=bool(result.get("verified")),
                still_suspended=bool(result.get("still_suspended")),
                restore_pending=bool(result.get("restore_pending")),
                error=_text(result.get("error"), 180),
            )
        # B03: only clear enforced_until when every package was released.
        if not still_pending:
            state["enforced_until"] = None
        if still_pending:
            state["last_error"] = f"{len(still_pending)} package(s) still pending restore"
        else:
            state["last_error"] = ""
        self._save_night_guard_state()
        return restored, still_suspended, still_pending

    async def _night_guard_send_warning(self, user_id: str, state: dict[str, Any], package: str) -> bool:
        session = self._night_guard_session(user_id)
        if not session:
            state["last_error"] = "no private chat session recorded for the target user"
            self._save_night_guard_state()
            self._audit("night_guard_warning_skipped", user_id=user_id, reason=state["last_error"])
            return False
        grace = self._night_guard_int("night_guard_grace_minutes", 10, 1, 120)
        app_name = package
        for alias, mapped in self._app_aliases().items():
            if mapped == package:
                app_name = alias
                break
        text = (
            f"已经很晚了，还在刷{app_name}。先放下手机，早点睡；"
            f"{grace}分钟后我再看一次。"
        )
        try:
            await asyncio.wait_for(
                self.context.send_message(session, MessageChain().message(text)),
                timeout=30,
            )
        except Exception as exc:
            state["last_error"] = _text(exc, 200)
            self._save_night_guard_state()
            self._audit("night_guard_warning_failed", user_id=user_id, error=state["last_error"])
            return False
        now = time.time()
        state["warning_sent_at"] = now
        state["warning_deadline"] = now + grace * 60
        state["phase"] = NG_PHASE_WARNED
        state["warned_date"] = date.today().isoformat()
        state["last_error"] = ""
        self._save_night_guard_state()
        self._audit("night_guard_warning_sent", user_id=user_id, package=package, session=session)
        return True

    async def _night_guard_enforce(self, user_id: str, state: dict[str, Any], package: str) -> bool:
        suspend_minutes = self._night_guard_int("night_guard_suspend_minutes", 30, 0, 480)
        window_left = self._night_guard_minutes_to_end()
        if suspend_minutes <= 0 or suspend_minutes > window_left > 0:
            suspend_minutes = window_left
        if suspend_minutes <= 0:
            self._audit("night_guard_suspend_skipped", user_id=user_id, package=package, reason="night window already ending")
            state["phase"] = NG_PHASE_WATCHING
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()
            return False
        if self._night_guard_dry_run():
            self._audit("night_guard_dry_run_suspend_skipped", user_id=user_id, package=package)
            state["phase"] = NG_PHASE_WATCHING
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()
            return False
        result = await self._execute_app_policy(package, "disable", suspend_minutes, source="night_guard")
        if not (
            result.get("success")
            and result.get("verified")
            and result.get("package") == package
        ):
            state["last_error"] = _text(result.get("error"), 200) or "suspension was not verified on the phone"
            self._save_night_guard_state()
            self._audit(
                "night_guard_suspend_failed",
                user_id=user_id,
                package=package,
                verified=bool(result.get("verified")),
                error=state["last_error"],
            )
            return False
        now = time.time()
        owned = sorted(set(state.get("owned_suspended") or []) | {package})
        state["owned_suspended"] = owned
        state["enforced_until"] = now + suspend_minutes * 60
        state["enforcement_count"] = int(state.get("enforcement_count") or 0) + 1
        state["night_id"] = self._night_guard_night_id()
        state["phase"] = NG_PHASE_ENFORCED
        state["consecutive_hits"] = 0
        state["last_error"] = ""
        self._save_night_guard_state()
        self._audit(
            "night_guard_enforced",
            user_id=user_id,
            package=package,
            minutes=suspend_minutes,
            verified=True,
        )
        session = self._night_guard_session(user_id)
        if session:
            try:
                await asyncio.wait_for(
                    self.context.send_message(
                        session,
                        MessageChain().message(f"你还在刷，我先替你把{package}暂停 {suspend_minutes} 分钟。想继续用就说“解除限制”。"),
                    ),
                    timeout=30,
                )
            except Exception as exc:
                logger.debug("phone agent night guard enforce notice failed: %s", _text(exc, 160))
        return True

    def _night_guard_night_id(self, now: datetime | None = None) -> str:
        """A stable id for one night, surviving the midnight boundary.

        For a window that crosses midnight (start > end), the night id is the
        calendar date at the window's start. 23:30–06:00 starting on Oct 16
        yields "2026-10-16" for both 23:59 and 03:00. For a same-day window,
        the night id is the current calendar date.
        """
        now = now or datetime.now()
        start_h, start_m, end_h, end_m = self._night_guard_window()
        start = self._minutes_of_day(start_h, start_m)
        end = self._minutes_of_day(end_h, end_m)
        current = self._minutes_of_day(now.hour, now.minute)
        if start > end and current < end:
            # After midnight in a cross-midnight window: the night started yesterday.
            return (now - timedelta(days=1)).date().isoformat()
        return now.date().isoformat()

    async def _night_guard_tick_user(self, user_id: str, state: dict[str, Any]) -> None:
        now = time.time()
        # B01: re-check authorization every tick. A user removed from the
        # allowlist must not receive new guard actions. Existing owned
        # packages are still restored (cleanup duty survives revocation).
        allowed = self._allowed_users()
        if not allowed or user_id not in allowed:
            if state.get("owned_suspended"):
                _, _, still_pending = await self._night_guard_restore_owned(
                    user_id, state, "authorization_revoked")
                if still_pending:
                    # B03: keep the ENFORCED phase so the next tick retries the
                    # restore — cleanup duty survives authorization revocation.
                    return
            state["phase"] = NG_PHASE_WATCHING
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()
            return

        # B06: reset enforcement count by night_id, not calendar date, so a
        # cross-midnight window does not double the budget at 00:00.
        current_night_id = self._night_guard_night_id()
        if state.get("night_id") != current_night_id:
            state["night_id"] = current_night_id
            state["enforcement_count"] = 0
        phase = state.get("phase")

        # B06: repair stale state loaded from disk — WARNED without a valid
        # deadline can never progress. Demote to WATCHING so evidence rebuilds.
        if phase == NG_PHASE_WARNED:
            deadline = state.get("warning_deadline")
            if not (isinstance(deadline, (int, float)) and math.isfinite(deadline) and deadline > 0):
                state["phase"] = NG_PHASE_WATCHING
                state["warning_sent_at"] = None
                state["warning_deadline"] = None
                state["consecutive_hits"] = 0
                self._save_night_guard_state()
                phase = NG_PHASE_WATCHING

        # Automatic restore when the guard's own suspension expires.
        if phase == NG_PHASE_ENFORCED:
            enforced_until = state.get("enforced_until")
            expired = False
            if isinstance(enforced_until, (int, float)) and math.isfinite(enforced_until):
                expired = now >= float(enforced_until)
            if not self._night_guard_in_window():
                expired = True
            if expired:
                restored, still_suspended, still_pending = await self._night_guard_restore_owned(
                    user_id, state, "expired_or_window_end")
                if still_pending:
                    # B03: device offline or restore failed — stay in ENFORCED
                    # and retry on the next tick. Do NOT transition to WATCHING.
                    self._audit("night_guard_restore_partial", user_id=user_id,
                                restored=restored, still_suspended=still_suspended,
                                pending=still_pending)
                else:
                    state["phase"] = NG_PHASE_WATCHING
                    state["warning_sent_at"] = None
                    state["warning_deadline"] = None
                    self._save_night_guard_state()
                    if restored:
                        self._audit("night_guard_auto_restored", user_id=user_id, packages=restored)
            return

        if not self._night_guard_in_window():
            # Night over: release anything the guard still owns, then go quiet.
            if state.get("owned_suspended"):
                _, _, still_pending = await self._night_guard_restore_owned(user_id, state, "night_window_end")
                if still_pending:
                    # B03: keep trying to restore even after the window ends.
                    # The packages were suspended by this guard; cleanup duty
                    # survives the window boundary.
                    self._audit("night_guard_restore_pending_after_window", user_id=user_id, pending=still_pending)
                    return
            state["phase"] = NG_PHASE_WATCHING
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()
            return

        released_until = state.get("released_until")
        if isinstance(released_until, (int, float)) and math.isfinite(released_until) and now < float(released_until):
            return

        if not self._night_guard_session(user_id):
            state["last_error"] = "target user has no recorded private chat session"
            self._save_night_guard_state()
            return

        max_enforcements = self._night_guard_int("night_guard_max_enforcements_per_night", 2, 1, 10)
        if int(state.get("enforcement_count") or 0) >= max_enforcements:
            return

        observation = await self._night_guard_observe()
        if not observation.get("fresh"):
            state["last_error"] = _text(observation.get("error"), 200)
            self._save_night_guard_state()
            self._audit("night_guard_observation_skipped", user_id=user_id, error=state["last_error"])
            return
        foreground = observation.get("foreground_package") or ""
        state["last_observed_at"] = now
        state["last_error"] = ""
        targets = self._night_guard_packages()
        hit = foreground if foreground in targets else ""

        # B05: screen off or Shizuku unavailable means the user is not
        # actively using the phone — never count this as continued use.
        screen_on = observation.get("screen_interactive")
        shizuku_ok = observation.get("shizuku_ready")
        if hit and (screen_on is not True or shizuku_ok is not True):
            self._audit(
                "night_guard_observation_screen_off",
                user_id=user_id,
                package=hit,
                screen_interactive=screen_on,
                shizuku_ready=shizuku_ok,
            )
            hit = ""

        if phase == NG_PHASE_WARNED:
            if not hit:
                # B06: leaving the target app (or screen off) returns to
                # WATCHING cleanly — no WARNED-without-deadline deadlock.
                state["phase"] = NG_PHASE_WATCHING
                state["warning_sent_at"] = None
                state["warning_deadline"] = None
                state["consecutive_hits"] = 0
                state["foreground_package"] = ""
                self._save_night_guard_state()
                return
            deadline = state.get("warning_deadline")
            grace_passed = isinstance(deadline, (int, float)) and math.isfinite(deadline) and now >= float(deadline)
            same_package = hit == state.get("foreground_package")
            if not same_package:
                # B06: switched to a different target app — rebuild evidence
                # for the new package from WATCHING, don't stay stuck in WARNED.
                state["phase"] = NG_PHASE_WATCHING
                state["foreground_package"] = hit
                state["warning_sent_at"] = None
                state["warning_deadline"] = None
                state["consecutive_hits"] = 1
                state["first_seen_at"] = now
                self._save_night_guard_state()
                return
            if not grace_passed:
                self._save_night_guard_state()
                return
            # B04: require a sample newer than the grace deadline, not the
            # same cached read. The observe above may have returned a stale
            # cache; force a fresh probe and check its timestamp.
            confirm = await self._night_guard_observe()
            confirm_fg = confirm.get("foreground_package") if confirm.get("fresh") else None
            if confirm_fg != hit:
                state["last_error"] = "pre-suspend re-check did not confirm the same package"
                self._save_night_guard_state()
                self._audit("night_guard_suspend_skipped", user_id=user_id, package=hit, reason="recheck_failed")
                return
            # B05: re-check screen/shizuku on the confirm sample too.
            if confirm.get("screen_interactive") is not True or confirm.get("shizuku_ready") is not True:
                state["last_error"] = "screen off or shizuku unavailable at pre-suspend re-check"
                self._save_night_guard_state()
                self._audit("night_guard_suspend_skipped", user_id=user_id, package=hit, reason="screen_or_shizuku_unavailable")
                return
            await self._night_guard_enforce(user_id, state, hit)
            return

        # watching phase
        required_hits = self._night_guard_int("night_guard_consecutive_hits", 2, 1, 10)
        if not hit:
            state["consecutive_hits"] = 0
            state["foreground_package"] = ""
            state["first_seen_at"] = None
            self._save_night_guard_state()
            return
        if state.get("foreground_package") != hit:
            state["foreground_package"] = hit
            state["consecutive_hits"] = 0
            state["first_seen_at"] = now
        state["consecutive_hits"] = int(state.get("consecutive_hits") or 0) + 1
        self._save_night_guard_state()
        if state["consecutive_hits"] >= required_hits:
            cooldown_minutes = self._night_guard_int("night_guard_warning_cooldown_minutes", 60, 5, 1440)
            last_warning = state.get("warning_sent_at")
            if isinstance(last_warning, (int, float)) and math.isfinite(last_warning) \
                    and now - float(last_warning) < cooldown_minutes * 60:
                return
            await self._night_guard_send_warning(user_id, state, hit)

    async def _night_guard_loop(self) -> None:
        try:
            while True:
                interval = self._night_guard_int("night_guard_check_interval_seconds", 300, 60, 3600)
                await asyncio.sleep(interval)
                try:
                    await asyncio.wait_for(self._night_guard_tick(), timeout=interval)
                except asyncio.TimeoutError:
                    logger.warning("phone agent night guard tick exceeded its budget")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("phone agent night guard tick failed: %s", _text(exc, 200))
        except asyncio.CancelledError:
            return

    async def _night_guard_tick(self) -> None:
        if not self._night_guard_enabled() or not self._night_guard_packages():
            # v4 §6: disabling the guard must not leave its suspensions behind.
            # Release whatever it still owns, then stay quiet.
            await self._night_guard_release_all_owned("guard_disabled")
            return
        async with self._night_guard_lock:
            for user_id in sorted(self._night_guard_states):
                state = self._night_guard_states.get(user_id)
                if state is None:
                    continue
                await self._night_guard_tick_user(user_id, state)

    async def _night_guard_release_all_owned(self, reason: str) -> None:
        for user_id in sorted(self._night_guard_states):
            state = self._night_guard_states.get(user_id)
            if not state or not state.get("owned_suspended"):
                continue
            _, _, still_pending = await self._night_guard_restore_owned(user_id, state, reason)
            if still_pending:
                logger.warning(
                    "phone agent night guard could not release %s for %s (%s)",
                    still_pending, user_id, reason,
                )
                continue
            state["phase"] = NG_PHASE_WATCHING
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()

    def _record_night_guard_session(self, user_id: str, session: str) -> None:
        """Remember the latest private session per allowed user for reminders."""
        if not user_id:
            return
        # B01: an empty allowlist is a closed door, not an open one. The
        # companion-auth fallback is evaluated in _authorized before this
        # runs; reaching here means the caller passed that gate.
        allowed = self._allowed_users()
        if not allowed or user_id not in allowed:
            return
        if self._bool_config("night_guard_private_only", True) and ":FriendMessage:" not in session:
            return
        state = self._night_guard_states.setdefault(user_id, {})
        if state.get("session") != session:
            state["session"] = session
            self._save_night_guard_state()

    async def _night_guard_release(self, user_id: str) -> dict[str, Any]:
        """User-requested release: restore only guard-owned packages."""
        async with self._night_guard_lock:
            state = self._night_guard_states.get(user_id)
            if state is None or not (state.get("owned_suspended") or state.get("phase") == NG_PHASE_WARNED):
                return {"success": False, "error": "夜间守护没有需要解除的限制"}
            phase = state.get("phase")
            # If only a warning was active (no packages suspended yet), cancel it.
            if not state.get("owned_suspended"):
                cooldown_minutes = self._night_guard_int("night_guard_warning_cooldown_minutes", 60, 5, 1440)
                state["phase"] = NG_PHASE_RELEASED
                state["released_until"] = time.time() + cooldown_minutes * 60
                state["warning_sent_at"] = None
                state["warning_deadline"] = None
                state["consecutive_hits"] = 0
                self._save_night_guard_state()
                self._audit("night_guard_user_released", user_id=user_id, restored=[], had_warning=True)
                return {"success": True, "restored": [], "message": "已取消提醒"}
            restored, still_suspended, still_pending = await self._night_guard_restore_owned(user_id, state, "user_release")
            if still_pending:
                # B03: do not claim success when packages are still suspended.
                self._audit("night_guard_user_release_partial", user_id=user_id,
                            restored=restored, pending=still_pending)
                return {
                    "success": False,
                    "error": "设备离线，部分应用恢复待重试",
                    "restored": restored,
                    "pending": still_pending,
                }
            cooldown_minutes = self._night_guard_int("night_guard_warning_cooldown_minutes", 60, 5, 1440)
            state["phase"] = NG_PHASE_RELEASED
            state["released_until"] = time.time() + cooldown_minutes * 60
            state["warning_sent_at"] = None
            state["warning_deadline"] = None
            state["consecutive_hits"] = 0
            self._save_night_guard_state()
            self._audit(
                "night_guard_user_released",
                user_id=user_id,
                restored=restored,
                still_suspended=still_suspended,
                had_warning=(phase == NG_PHASE_WARNED),
            )
            result = {"success": True, "restored": restored}
            if still_suspended:
                # v4 §6: our lease is released, but another source (a manual
                # lock) keeps these apps suspended — releasing the guard's
                # intent is not the same as the device being restored.
                result["still_suspended"] = still_suspended
                result["message"] = "已解除夜间限制；这些应用仍被你设置的其他限制保持暂停"
            return result

    def _reminder_path(self) -> str:
        return str(self._plugin_data_path("reminders_path", "phone_agent_reminders.json"))

    def _task_path(self) -> str:
        return str(self._plugin_data_path("tasks_path", "phone_agent_tasks.json"))

    def _save_operit_tasks(self) -> None:
        try:
            path = Path(self._task_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            items = list(self._operit_tasks.items())[-100:]
            path.write_text(json.dumps(dict(items), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("phone agent task save failed: %s", exc)

    def _load_operit_tasks(self) -> None:
        try:
            path = Path(self._task_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return
            for task_id, item in list(values.items())[-100:]:
                if not isinstance(item, dict) or not item.get("task"):
                    continue
                item = dict(item)
                if item.get("status") in {"queued", "running"}:
                    item["status"] = "interrupted"
                    item["error"] = "AstrBot restarted before this background task finished"
                item.pop("result", None)
                self._operit_tasks[str(task_id)] = item
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("phone agent task file is invalid; ignoring it")

    def _policy_state_path(self) -> str:
        return str(self._plugin_data_path("policy_state_path", "phone_agent_policies.json"))

    def _task_run_path(self) -> str:
        return str(self._plugin_data_path("task_runs_path", "phone_agent_task_runs.json"))

    def _task_float(self, key: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(float(self.config.get(key, default)), high))
        except (TypeError, ValueError):
            return default

    def _task_allowed_actions(self) -> set[str]:
        raw = _text(self.config.get("task_execute_allowed_actions", "open_app,home,back"), 240)
        return {item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()}

    async def _task_act(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one device action on behalf of the task executor."""
        if action in {"suspend_app", "unsuspend_app"}:
            return await self._apply_app_policy(
                _text(args.get("package"), 160),
                "disable" if action == "suspend_app" else "enable",
            )
        if self._control_backend() == "app":
            return await asyncio.to_thread(self.device_app.execute, action, args)
        return await self._execute(action, args)

    async def _task_observe_cached(self) -> dict[str, Any]:
        """Heartbeat-backed observation used for planning."""
        return self.device_app.last_status() or {}

    async def _task_observe_live(self) -> dict[str, Any]:
        """Ask the phone for its state right now; a heartbeat is too slow to
        verify a step that just ran."""
        if self._control_backend() != "app":
            return self.device_app.last_status() or {}
        try:
            result = await asyncio.to_thread(self.device_app.execute, "status", {})
        except Exception:
            return self.device_app.last_status() or {}
        output = result.get("output") if isinstance(result, dict) else None
        if not isinstance(output, dict) or not output:
            return self.device_app.last_status() or {}
        return {
            "foreground_package": _text(output.get("foreground_package"), 160),
            "screen_interactive": output.get("screen_interactive"),
            "battery_percent": output.get("battery_percent"),
            "reported_at": time.time(),
            "source": "live",
        }

    def _save_policy_state(self) -> None:
        # P1-A: persist source + deadline per package so a restart restores the
        # ownership map as well as the countdown. Atomic replace keeps a crash
        # from leaving a half-written file behind.
        path = Path(self._policy_state_path())
        payload: dict[str, Any] = {"version": POLICY_STATE_VERSION, "packages": {}}
        for package, leases in sorted(self._policy_leases.items()):
            if not leases:
                continue
            payload["packages"][package] = {
                "leases": {
                    source: {"until": lease.until}
                    for source, lease in sorted(leases.items())
                }
            }
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as exc:
            # Losing the file means losing the only record that an app is still
            # suspended, so this must not stay at debug level.
            logger.warning("phone agent policy state save failed: %s", exc)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_policy_state(self) -> None:
        try:
            path = Path(self._policy_state_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return
            if values.get("version") == POLICY_STATE_VERSION and isinstance(values.get("packages"), dict):
                entries = values["packages"]
            else:
                # Legacy file: {package: {"until": ts | null}} with no source.
                # Graceful shutdown already releases temporary policies, so an
                # entry that survived a restart is in practice a permanent
                # manual lock; migrating it to "manual" keeps an explicit user
                # stop able to release it. A crashed night lease still expires
                # on its recorded deadline, whichever name we file it under.
                entries = values
            now = datetime.now().timestamp()
            for raw_package, item in entries.items():
                package = self._resolve_package(raw_package)
                if not PACKAGE_RE.fullmatch(package) or self._protected_policy_error(package):
                    continue
                if isinstance(item, dict) and isinstance(item.get("leases"), dict):
                    raw_leases = item["leases"]
                else:
                    raw_leases = {
                        POLICY_LEGACY_SOURCE: {
                            "until": item.get("until") if isinstance(item, dict) else None
                        }
                    }
                leases: dict[str, _PolicyLease] = {}
                for raw_source, spec in raw_leases.items():
                    source = _text(raw_source, 40).lower() or POLICY_LEGACY_SOURCE
                    until = spec.get("until") if isinstance(spec, dict) else None
                    if until is not None:
                        try:
                            until = float(until)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(until):
                            continue
                    leases[source] = _PolicyLease(source=source, until=until)
                if not leases:
                    continue
                self._policy_leases[package] = leases
                self._guard_suspended.add(package)
                for source, lease in leases.items():
                    if lease.until is None:
                        continue
                    lease.task = asyncio.create_task(
                        self._expire_policy_lease(package, source, max(0.0, lease.until - now))
                    )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
            logger.warning("phone agent policy state file is invalid; ignoring it")

    def _save_reminders(self) -> None:
        path = Path(self._reminder_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        # Keep completed idempotency receipts for 30 days; active ones never expire.
        cutoff = time.time() - 30 * 86400
        requests = {key: value for key, value in self._reminder_requests.items()
                    if value.get("status") == "pending" or value.get("finished_at", time.time()) >= cutoff}
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": 2, "pending": self._reminders, "requests": requests},
                          handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._reminder_requests = requests
        finally:
            temporary.unlink(missing_ok=True)

    def _load_reminders(self) -> None:
        try:
            path = Path(self._reminder_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return
            pending = values.get("pending", {}) if values.get("version") == 2 else values
            receipts = values.get("requests", {}) if values.get("version") == 2 else {}
            if isinstance(receipts, dict):
                self._reminder_requests = {key: dict(value) for key, value in receipts.items()
                                           if isinstance(value, dict) and self._finite_json(value)
                                           and all(field in value for field in ("id", "text", "due", "status"))}
            if not isinstance(pending, dict):
                return
            for reminder_id, raw in pending.items():
                if not isinstance(raw, dict) or not raw.get("text") or not self._finite_json(raw):
                    continue
                try:
                    due = float(raw.get("due", 0))
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(due) or due <= 0:
                    continue
                item = dict(raw, due=due, source=raw.get("source", "chat"), status="pending")
                if item["source"] not in {"app", "chat"} or (item["source"] == "chat" and not item.get("session")):
                    continue
                self._reminders[str(reminder_id)] = item
                self._reminder_tasks[str(reminder_id)] = asyncio.create_task(self._run_reminder(str(reminder_id)))
        except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError):
            logger.warning("phone agent reminders file is invalid; ignoring it")

    def _finish_reminder(self, reminder_id: str, status: str) -> dict[str, Any] | None:
        item = self._reminders.pop(reminder_id, None)
        if item is None:
            return None
        request_id = item.get("request_id")
        previous = dict(self._reminder_requests.get(request_id, {}))
        if previous:
            self._reminder_requests[request_id] = {**previous, "status": status, "finished_at": time.time()}
        try:
            self._save_reminders()
        except OSError:
            self._reminders[reminder_id] = item
            if previous:
                self._reminder_requests[request_id] = previous
            raise
        return item

    def _cancel_reminder(self, reminder_id: str) -> bool:
        item = self._finish_reminder(reminder_id, "cancelled")
        task = self._reminder_tasks.pop(reminder_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        if item:
            try:
                self.device_app.timeline_append("reminder", "提醒已取消", str(item["text"]),
                                                event="reminder_cancelled", reminder_id=reminder_id)
            except OSError:
                logger.warning("phone agent could not save cancellation timeline")
        return item is not None

    async def _run_reminder(self, reminder_id: str) -> None:
        try:
            while reminder_id in self._reminders:
                item = self._reminders[reminder_id]
                await asyncio.sleep(max(0.0, float(item["due"]) - time.time()))
                if not self._enabled() or not self._feature("reminder_tools"):
                    await asyncio.sleep(60)
                    continue
                try:
                    # Catch up recent overdue reminders after a restart. Older
                    # reminders remain visible as expired, without a late alert.
                    expired = time.time() - float(item["due"]) > 86400
                    event = "reminder_expired" if expired else "reminder_due"
                    self.device_app.timeline_append(
                        "reminder", "提醒已过期" if expired else "提醒", str(item["text"]),
                        event=event, notify=not expired, reminder_id=reminder_id,
                        notification_id="reminder-" + reminder_id if not expired else None)
                    if not expired and item.get("source", "chat") == "chat" and not item.get("chat_delivered"):
                        # Existing chat reminders retain their originating chat.
                        # App reminders deliberately have no chat session.
                        await asyncio.wait_for(self.context.send_message(
                            item["session"], MessageChain().message("提醒：" + str(item["text"]))), timeout=30)
                        item["chat_delivered"] = True
                    self._finish_reminder(reminder_id, "expired" if expired else "fired")
                    self._audit("reminder_expired" if expired else "reminder_sent", reminder_id=reminder_id)
                    return
                except Exception as exc:
                    logger.warning("phone agent reminder delivery will retry: %s", _text(exc, 200))
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        finally:
            if self._reminder_tasks.get(reminder_id) is asyncio.current_task():
                self._reminder_tasks.pop(reminder_id, None)

    def _app_aliases(self) -> dict[str, str]:
        defaults = {
            "哔哩哔哩": "tv.danmaku.bilibilihd",
            "b站": "tv.danmaku.bilibilihd",
            "bilibili": "tv.danmaku.bilibilihd",
            "快手": "com.kuaishou.nebula",
            "快手极速版": "com.kuaishou.nebula",
            "优酷": "com.hihonor.youku.video",
            "抖音": "com.ss.android.ugc.aweme",
            "抖音极速版": "com.ss.android.ugc.aweme.lite",
            "微信": "com.tencent.mm",
            "小米运动健康": "com.mi.health",
            "operit": "com.ai.assistance.operit",
        }
        raw = self.config.get("app_aliases_json", "")
        try:
            values = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
            if isinstance(values, dict):
                defaults.update({str(key).strip().lower(): str(value).strip() for key, value in values.items()})
        except json.JSONDecodeError:
            logger.warning("app_aliases_json is invalid; using built-in aliases")
        return defaults

    def _resolve_package(self, value: Any) -> str:
        text = _text(value, 160)
        return self._app_aliases().get(text.lower(), text)

    def _operit_url(self) -> str:
        return _text(self.config.get("operit_base_url"), 500).rstrip("/") or "http://127.0.0.1:8094"

    def _operit_token(self) -> str:
        return str(self.config.get("operit_token") or "").strip()

    def _operit_timeout(self) -> float:
        try:
            return max(10.0, min(float(self.config.get("operit_timeout_seconds", 120)), 300.0))
        except (TypeError, ValueError):
            return 120.0

    @staticmethod
    def _normalize_backend(value: Any) -> str:
        value = _text(value, 30).lower()
        aliases = {
            "phone_buddy": "app",
            "native_app": "app",
            "own_frontend": "app",
            "operitai": "operit",
            "operit_ai": "operit",
        }
        return aliases.get(value, value)

    def _control_backend(self) -> str:
        value = self._normalize_backend(self.config.get("control_backend"))
        return value if value in {"operit", "adb", "app"} else "operit"

    def _operit_action_prompt(self, action: str, kwargs: dict[str, Any]) -> str:
        package = _text(kwargs.get("package"), 160)
        if action == "status":
            return "读取手机型号、在线状态和电池电量，只返回结果，不修改手机。"
        if action == "foreground_app":
            return "读取当前前台应用的包名和应用名称，只观察，不执行任何操作。"
        if action == "screen_text":
            return "读取当前屏幕可见文字和主要控件，只观察，不点击、不输入。"
        if action == "open_app":
            return f"打开 Android 应用 {package}。完成后告诉我结果。"
        if action == "close_app":
            return f"关闭 Android 应用 {package}。完成后告诉我结果。"
        if action == "suspend_app":
            return self._operit_policy_prompt(package, "disable")
        if action == "unsuspend_app":
            return self._operit_policy_prompt(package, "enable")
        if action == "suspend_video_apps":
            return "暂停配置中的所有视频应用，只操作视频应用。"
        if action == "unsuspend_video_apps":
            return "恢复配置中的所有视频应用，只操作视频应用。"
        if action == "tap":
            return f"点击屏幕坐标 ({kwargs.get('x')}, {kwargs.get('y')})，完成后告诉我结果。"
        if action == "swipe":
            return f"从 ({kwargs.get('x1')}, {kwargs.get('y1')}) 滑动到 ({kwargs.get('x2')}, {kwargs.get('y2')})，持续 {kwargs.get('duration_ms', 300)} 毫秒。"
        if action == "input_text":
            return f"在当前输入框输入以下文字，不要修改其它内容：{kwargs.get('text', '')}"
        if action == "back":
            return "执行一次返回操作。"
        if action == "home":
            return "回到手机桌面。"
        if action == "lock_screen":
            return "锁定手机屏幕。"
        if action == "wake_screen":
            return "唤醒手机屏幕。"
        if action == "trigger_workflow":
            return f"执行 Operit 工作流 {kwargs.get('workflow_action', '')}，参数为 {kwargs.get('extras_json', '{}')}。"
        return "完成这个已授权的手机操作，并返回执行结果。"

    @staticmethod
    def _json_line_prompt(schema: str) -> str:
        return (
            "只输出一行严格 JSON，不要 Markdown、解释、代码块或其它文字。"
            f"返回格式：{schema}"
        )

    def _operit_policy_prompt(self, package: str, action: str) -> str:
        operation = "pm suspend --user 0" if action == "disable" else "pm unsuspend --user 0"
        expected = "suspended" if action == "disable" else "unsuspended"
        return (
            f"使用 Shizuku 执行 `{operation} {package}`，只操作这个 Android 包名。"
            "完成后必须用 Shizuku 检查该包的实际 suspended 状态；"
            "如果无法执行、未改变、返回 no-op 或无法核验，都视为失败。"
            + self._json_line_prompt(
                '{"success":true|false,"package":"' + package
                + '","state":"suspended|unsuspended|unknown","verified":true|false,"error":""}'
            )
            + f"成功条件：package 必须是 {package}、state 必须是 {expected}、verified 必须为 true。"
        )

    @staticmethod
    def _float_in_range(value: Any, low: float, high: float) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and low <= number <= high else None

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
        return default

    def _parse_policy_result(self, package: str, action: str, raw: dict[str, Any]) -> dict[str, Any]:
        payload = _first_json_object(raw.get("ai_response")) if raw.get("success") else None
        expected = "suspended" if action == "disable" else "unsuspended"
        received_package = _text(payload.get("package"), 160) if payload else ""
        state = _text(payload.get("state"), 20).lower() if payload else "unknown"
        verified = bool(payload.get("verified")) if payload else False
        success = bool(payload and payload.get("success") is True and received_package == package and state == expected and verified)
        error = _text(payload.get("error"), 240) if payload else "Operit did not return a verifiable policy JSON result"
        if not success and not error:
            error = "Phone did not confirm the requested app state"
        return {
            "success": success,
            "backend": "operit",
            "package": package,
            "action": action,
            "state": state,
            "verified": verified,
            "error": error if not success else "",
        }

    @staticmethod
    def _gcj02_to_bd09(latitude: float, longitude: float) -> tuple[float, float]:
        """Baidu's published GCJ02 -> BD09 shift (api.map.baidu.com/geoconvert)."""
        x_pi = math.pi * 3000.0 / 180.0
        z = math.sqrt(longitude * longitude + latitude * latitude) + 0.00002 * math.sin(latitude * x_pi)
        theta = math.atan2(latitude, longitude) + 0.000003 * math.cos(longitude * x_pi)
        return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065

    def _parse_location_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = _first_json_object(raw.get("ai_response")) if raw.get("success") else None
        if not payload:
            return {"success": False, "backend": "operit", "error": "Operit did not return a location JSON result"}
        latitude = self._float_in_range(payload.get("latitude"), -90, 90)
        longitude = self._float_in_range(payload.get("longitude"), -180, 180)
        if latitude is None or longitude is None:
            return {"success": False, "backend": "operit", "error": "Operit location result did not contain valid coordinates"}
        in_china = 3.86 <= latitude <= 53.56 and 73.55 <= longitude <= 135.09
        location: dict[str, Any] = {"latitude": latitude, "longitude": longitude, "coord_type": "gcj02"}
        if in_china:
            # Android GPS/network fixes are GCJ02; Baidu Map APIs expect BD09.
            location["bd09_latitude"], location["bd09_longitude"] = (
                round(value, 6) for value in self._gcj02_to_bd09(latitude, longitude)
            )
        else:
            # Outside China the fix is plain WGS84 and Baidu's overseas APIs
            # accept it directly; no shift is applied.
            location["coord_type"] = "wgs84"
        result: dict[str, Any] = {"success": True, "backend": "operit", "location": location}
        accuracy = self._float_in_range(payload.get("accuracy_m"), 0, 100000)
        if accuracy is not None:
            result["location"]["accuracy_m"] = accuracy
        address = _text(payload.get("address"), 400)
        if address:
            result["location"]["address"] = address
        provider = _text(payload.get("provider"), 80)
        if provider:
            result["location"]["provider"] = provider
        return result

    def _operit_location_sync(self, high_accuracy: bool, timeout_seconds: int, include_address: bool) -> dict[str, Any]:
        task = (
            "在手机本机调用 Operit 系统定位工具 Tools.System.getLocation("
            f"{str(bool(high_accuracy)).lower()}, {max(3, min(int(timeout_seconds), 30))}, {str(bool(include_address)).lower()})。"
            "只读取这一次当前位置，不保存、不追踪、不打开地图、不发送给第三方。"
            + self._json_line_prompt(
                '{"latitude":0,"longitude":0,"accuracy_m":0,"address":"","provider":""}'
            )
        )
        return self._parse_location_result(self._operit_task_sync(task, False, "WINDOW"))

    async def _operit_action(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self._control_backend() == "app":
            args = {
                key: value
                for key, value in {
                    "package": self._resolve_package(kwargs.get("package")),
                    "x": kwargs.get("x"),
                    "y": kwargs.get("y"),
                    "x1": kwargs.get("x1"),
                    "y1": kwargs.get("y1"),
                    "x2": kwargs.get("x2"),
                    "y2": kwargs.get("y2"),
                    "text": kwargs.get("text"),
                }.items()
                if value is not None
            }
            if action in {"suspend_app", "unsuspend_app"}:
                mapped = {
                    "suspend_app": "suspend_app",
                    "unsuspend_app": "unsuspend_app",
                    "suspend_video_apps": "suspend_video_apps",
                    "unsuspend_video_apps": "unsuspend_video_apps",
                }.get(action, action)
            else:
                mapped = action
            result = await asyncio.to_thread(self.device_app.execute, mapped, args)
            result["action"] = action
            result["backend"] = "app"
            return result
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return {"success": False, "action": action, "backend": "operit", "operit": health}
        result = await asyncio.to_thread(
            self._operit_task_sync,
            self._operit_action_prompt(action, kwargs),
            False,
            "WINDOW",
        )
        if action in {"suspend_app", "unsuspend_app"}:
            policy = self._parse_policy_result(
                _text(kwargs.get("package"), 160),
                "disable" if action == "suspend_app" else "enable",
                result,
            )
            policy["legacy_action"] = action
            return policy
        result["action"] = action
        result["backend"] = "operit"
        return result

    def _guard_packages(self) -> set[str]:
        raw = self.config.get("sleep_guard_packages", "tv.danmaku.bilibilihd,com.kuaishou.nebula,com.hihonor.youku.video")
        values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
        packages = {self._resolve_package(value) for value in values}
        exempt = self._manual_guard_exempt | {
            self._resolve_package(value)
            for value in re.split(r"[,\s]+", str(self.config.get("sleep_guard_exempt_apps", "") or ""))
            if value
        }
        return {item for item in packages if PACKAGE_RE.fullmatch(item) and item not in exempt}

    def _manual_guard_active(self) -> bool:
        return self._manual_guard_until is not None and datetime.now() < self._manual_guard_until

    def _allowed_users(self) -> set[str]:
        raw = self.config.get("allowed_user_ids", "")
        if isinstance(raw, list):
            values = raw
        else:
            values = re.split(r"[,\s]+", str(raw or ""))
        return {_text(item, 120) for item in values if _text(item, 120)}

    def _event_user_id(self, event: AstrMessageEvent) -> str:
        for name in ("get_sender_id", "get_user_id"):
            getter = getattr(event, name, None)
            if callable(getter):
                try:
                    value = getter()
                    if value:
                        return _text(value, 120)
                except Exception:
                    pass
        sender = getattr(event, "sender", None)
        if isinstance(sender, dict):
            return _text(sender.get("user_id") or sender.get("id"), 120)
        return ""

    def _authorized(self, event: AstrMessageEvent) -> bool:
        allowed = self._allowed_users()
        user_id = self._event_user_id(event)
        if allowed:
            return user_id in allowed
        # The phone agent is independent by default. Private Companion can be
        # opted into as an authorization source, but is never required.
        if not self._bool_config("use_private_companion_auth", False):
            return False
        try:
            module = importlib.import_module("data.plugins.astrbot_plugin_private_companion.main")
            api = getattr(module, "get_private_companion_api", lambda: None)()
            getter = getattr(api, "get_reality_touch_authorized_user_ids", None)
            return bool(callable(getter) and user_id and user_id in set(getter() or []))
        except Exception:
            return False

    async def _run(self, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
        timeout = timeout or self._timeout()
        process = await asyncio.create_subprocess_exec(
            self._adb(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return 124, "", "adb command timed out"
        return process.returncode or 0, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()

    async def _ensure_device(self) -> tuple[bool, str]:
        serial = self._serial()
        if ":" in serial:
            code, out, err = await self._run("connect", serial, timeout=10)
            if code != 0 and "already connected" not in (out + err).lower():
                serial = await self._discover_serial()
                if not serial:
                    return False, err or out or "adb connect failed; wireless debugging port not found"
                self._active_serial = serial
        code, out, err = await self._run("-s", serial, "get-state", timeout=10)
        if code != 0 or out.strip() != "device":
            discovered = await self._discover_serial()
            if discovered and discovered != serial:
                self._active_serial = discovered
                code, out, err = await self._run("-s", discovered, "get-state", timeout=10)
            if code != 0 or out.strip() != "device":
                return False, err or out or "phone is not ready"
        return True, "connected"

    async def _discover_serial(self) -> str | None:
        """Find Android's dynamic wireless-debugging port over Tailscale."""
        configured = _text(self.config.get("adb_serial"), 120)
        host = configured.rsplit(":", 1)[0] if ":" in configured else configured
        if not host:
            return None

        async def probe(port: int) -> int | None:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.12)
                writer.close()
                await writer.wait_closed()
                return port
            except (OSError, asyncio.TimeoutError):
                return None

        ports = range(30000, 50001)
        semaphore = asyncio.Semaphore(400)

        async def limited(port: int) -> int | None:
            async with semaphore:
                return await probe(port)

        candidates = [port for port in await asyncio.gather(*(limited(port) for port in ports)) if port]
        # Android wireless debugging normally uses 30k-50k. If it was
        # configured to another dynamic port, make one broader recovery pass.
        if not candidates and self._bool_config("adb_full_port_scan", True):
            remaining = list(range(1, 30000)) + list(range(50001, 65536))
            candidates = [port for port in await asyncio.gather(*(limited(port) for port in remaining)) if port]
        for port in candidates:
            serial = f"{host}:{port}"
            code, out, err = await self._run("connect", serial, timeout=3)
            if code == 0 or "already connected" in (out + err).lower():
                state_code, state_out, _ = await self._run("-s", serial, "get-state", timeout=3)
                if state_code == 0 and state_out.strip() == "device":
                    return serial
        return None

    async def _execute(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        serial = self._serial()
        if action == "status":
            code, out, err = await self._run("-s", serial, "shell", "getprop", "ro.product.model")
            return {"success": code == 0, "action": action, "model": out, "error": err}

        if action == "foreground_app":
            code, out, err = await self._run("-s", serial, "shell", "dumpsys", "activity", "activities")
            match = re.search(r"(?:mResumedActivity:|mCurrentFocus=Window\{[^}]+\s+)[^\s]+\s+(?:u\d+\s+)?([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", out)
            return {"success": bool(match) and code == 0, "action": action, "package": match.group(1) if match else "", "error": err}

        if action == "screen_text":
            dump_code, _, dump_err = await self._run("-s", serial, "shell", "uiautomator", "dump", "--compressed", "/sdcard/window.xml")
            if dump_code != 0:
                return {"success": False, "action": action, "error": dump_err or "uiautomator dump failed"}
            read_code, xml_text, read_err = await self._run("-s", serial, "shell", "cat", "/sdcard/window.xml")
            if read_code != 0:
                return {"success": False, "action": action, "error": read_err or "screen dump read failed"}
            nodes: list[dict[str, str]] = []
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip()
                    desc = (node.attrib.get("content-desc") or "").strip()
                    resource = (node.attrib.get("resource-id") or "").strip()
                    if text or desc:
                        nodes.append({"text": text[:120], "description": desc[:120], "resource_id": resource[:160]})
                    if len(nodes) >= 80:
                        break
            except ET.ParseError:
                return {"success": False, "action": action, "error": "screen XML was invalid"}
            return {"success": True, "action": action, "nodes": nodes}

        if action == "open_app":
            package = _text(kwargs.get("package"), 160)
            if not PACKAGE_RE.fullmatch(package):
                return {"success": False, "action": action, "error": "invalid Android package"}
            command = ("-s", serial, "shell", "monkey", "-p", package, "1")
        elif action == "close_app":
            package = _text(kwargs.get("package"), 160)
            if not PACKAGE_RE.fullmatch(package):
                return {"success": False, "action": action, "error": "invalid Android package"}
            command = ("-s", serial, "shell", "am", "force-stop", package)
        elif action in {"suspend_app", "unsuspend_app"}:
            package = _text(kwargs.get("package"), 160)
            if not PACKAGE_RE.fullmatch(package):
                return {"success": False, "action": action, "error": "invalid Android package"}
            operation = "suspend" if action == "suspend_app" else "unsuspend"
            command = ("-s", serial, "shell", "pm", operation, "--user", "0", package)
        elif action in {"suspend_video_apps", "unsuspend_video_apps"}:
            packages = sorted(self._guard_packages())
            if not packages:
                return {"success": False, "action": action, "error": "no configured video packages"}
            operation = "suspend" if action == "suspend_video_apps" else "unsuspend"
            command = ("-s", serial, "shell", "pm", operation, "--user", "0", *packages)
        elif action == "tap":
            x, y = int(kwargs.get("x", -1)), int(kwargs.get("y", -1))
            if not (0 <= x <= 10000 and 0 <= y <= 10000):
                return {"success": False, "action": action, "error": "tap coordinates are out of range"}
            command = ("-s", serial, "shell", "input", "tap", str(x), str(y))
        elif action == "swipe":
            x1, y1 = int(kwargs.get("x1", -1)), int(kwargs.get("y1", -1))
            x2, y2 = int(kwargs.get("x2", -1)), int(kwargs.get("y2", -1))
            duration = max(50, min(int(kwargs.get("duration_ms", 300)), 10000))
            if not all(0 <= value <= 10000 for value in (x1, y1, x2, y2)):
                return {"success": False, "action": action, "error": "swipe coordinates are out of range"}
            command = ("-s", serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration))
        elif action == "input_text":
            text = str(kwargs.get("text") or "")
            if not text or len(text) > 500 or any(char in text for char in ("\n", "\r")):
                return {"success": False, "action": action, "error": "text is empty or contains a newline"}
            encoded = text.replace("%", "%25").replace(" ", "%s").replace("&", "%26")
            command = ("-s", serial, "shell", "input", "text", encoded)
        elif action == "back":
            command = ("-s", serial, "shell", "input", "keyevent", "4")
        elif action == "home":
            command = ("-s", serial, "shell", "input", "keyevent", "3")
        elif action == "lock_screen":
            command = ("-s", serial, "shell", "input", "keyevent", "26")
        elif action == "wake_screen":
            command = ("-s", serial, "shell", "input", "keyevent", "224")
        elif action == "trigger_workflow":
            workflow_action = _text(kwargs.get("workflow_action"), 160)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow_action):
                return {"success": False, "action": action, "error": "invalid workflow action"}
            try:
                extras = json.loads(str(kwargs.get("extras_json") or "{}"))
            except json.JSONDecodeError:
                return {"success": False, "action": action, "error": "extras_json must be a JSON object"}
            if not isinstance(extras, dict) or len(extras) > 16:
                return {"success": False, "action": action, "error": "extras_json must contain at most 16 fields"}
            command_parts = [
                "-s", serial, "shell", "am", "broadcast",
                "-n", "com.ai.assistance.operit/.integrations.tasker.WorkflowTaskerReceiver",
                "-a", workflow_action,
            ]
            for key, value in extras.items():
                key_text = _text(key, 80)
                value_text = _text(value, 500)
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", key_text) or not value_text:
                    return {"success": False, "action": action, "error": "invalid workflow extra"}
                command_parts.extend(("--es", key_text, value_text))
            command = tuple(command_parts)
        else:
            return {"success": False, "action": action, "error": "unsupported action"}

        code, out, err = await self._run(*command)
        return {"success": code == 0, "action": action, "output": out[-500:], "error": err[-500:]}

    def _reality_api(self) -> Any | None:
        try:
            metadata = self.context.get_registered_star("astrbot_plugin_reality_companion")
            instance = getattr(metadata, "star_cls", None) if metadata is not None else None
            api = getattr(instance, "extension_api", None)
            if api is not None:
                return api
        except Exception:
            pass
        try:
            module = importlib.import_module("data.plugins.astrbot_plugin_reality_companion.main")
            return getattr(module, "get_reality_companion_api", lambda: None)()
        except Exception:
            return None

    @staticmethod
    def _health_timestamp(value: Any) -> str | None:
        try:
            stamp = float(value)
            if stamp > 100_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def _read_health_db_sync(self, days: int) -> dict[str, Any]:
        try:
            days = max(1, min(int(days or 1), 30))
        except (TypeError, ValueError):
            days = 1
        end = date.today()
        start = end - timedelta(days=days - 1)
        db_path = self._health_db()
        if not db_path:
            return {
                "source": "xiaomi-sync",
                "available": False,
                "fresh": False,
                "days": [],
                "error": "health_db_path is not configured; point it to xiaomi-sync/data/health.db",
            }
        path = Path(db_path).expanduser()
        if path.is_dir():
            path = path / "health.db"
        if not path.is_file():
            return {
                "source": "xiaomi-sync",
                "available": False,
                "fresh": False,
                "days": [],
                "error": f"xiaomi-sync database not found: {path}",
            }
        result: dict[str, Any] = {"source": "xiaomi-sync", "days": [], "warnings": []}
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3) as db:
                db.row_factory = sqlite3.Row
                def fetch(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
                    try:
                        return db.execute(query, params).fetchall()
                    except sqlite3.Error as exc:
                        result["warnings"].append(_text(exc, 180))
                        return []

                rows = fetch(
                    "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date DESC",
                    (start.isoformat(), end.isoformat()),
                )
                # Xiaomi usually publishes the current day after the next sync;
                # expose the newest available day instead of returning an empty
                # answer for a "today" question during the early hours.
                if not rows:
                    rows = fetch(
                        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (days,)
                    )
                for row in rows:
                    item = {key: row[key] for key in row.keys() if row[key] is not None}
                    item.pop("updated_at", None)
                    item.pop("source", None)
                    result["days"].append(item)
                weights = fetch(
                    "SELECT time, weight, bmi, body_fat_rate FROM weight_log ORDER BY time DESC LIMIT 10"
                )
                result["weight"] = [dict(row) for row in weights]
                pressure = fetch(
                    "SELECT time, systolic, diastolic, pulse FROM blood_pressure ORDER BY time DESC LIMIT 10"
                )
                result["blood_pressure"] = [dict(row) for row in pressure]
                segments = fetch(
                    "SELECT date, kind, duration_min, deep_min, light_min, rem_min, awake_min, avg_hr, avg_spo2 "
                    "FROM sleep_segments WHERE date BETWEEN ? AND ? ORDER BY date DESC, bedtime_ts DESC",
                    (start.isoformat(), end.isoformat()),
                )
                result["sleep_segments"] = [dict(row) for row in segments]
                sync_rows = fetch(
                    "SELECT started_at, finished_at, ok, error, days_requested, target_uid "
                    "FROM sync_runs ORDER BY id DESC LIMIT 1"
                )
                sync_row = sync_rows[0] if sync_rows else None
            if sync_row is None:
                result["sync"] = {
                    "ok": False,
                    "finished_at": None,
                    "age_minutes": None,
                    "stale": True,
                    "error": "xiaomi-sync has not recorded a sync run",
                }
            else:
                finished_at = self._health_timestamp(sync_row["finished_at"] or sync_row["started_at"])
                try:
                    stamp = float(sync_row["finished_at"] or sync_row["started_at"])
                    if stamp > 100_000_000_000:
                        stamp /= 1000
                    age_minutes = max(0.0, (datetime.now().timestamp() - stamp) / 60)
                except (TypeError, ValueError, OverflowError):
                    age_minutes = None
                result["sync"] = {
                    "ok": bool(sync_row["ok"]),
                    "finished_at": finished_at,
                    "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
                    "stale": age_minutes is None or age_minutes > 180 or not bool(sync_row["ok"]),
                    "error": _text(sync_row["error"], 240) if sync_row["error"] else "",
                    "days_requested": sync_row["days_requested"],
                }
            result["available"] = bool(result["days"] or result["weight"] or result["blood_pressure"] or result["sleep_segments"])
            result["fresh"] = bool(result["available"] and result["sync"].get("ok") and not result["sync"].get("stale"))
            if not result["warnings"]:
                result.pop("warnings", None)
            return result
        except Exception as exc:
            logger.warning("health database read failed: %s", exc)
            return {"source": "xiaomi-sync", "available": False, "fresh": False, "days": [], "error": str(exc)[:200]}

    # ==================== Relay fallback (no VPN) ====================
    # When Tailscale is down the phone cannot be reached directly. The relay
    # queue on the public server lets the phone's Operit workflow pick tasks
    # up on its own schedule (deploy/relay/relay_server.py).

    def _relay_url(self) -> str:
        return _text(self.config.get("relay_base_url"), 500).rstrip("/")

    def _relay_token(self) -> str:
        return str(self.config.get("relay_token") or "").strip()

    def _relay_enabled(self) -> bool:
        return bool(self._relay_url() and self._relay_token())

    @staticmethod
    def _relay_request(url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read(64 * 1024).decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else {"success": False, "error": "relay returned invalid JSON"}
        except urllib.error.HTTPError as exc:
            return {"success": False, "error": f"relay HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"success": False, "error": f"relay connection failed: {str(exc)[:180]}"}

    def _relay_health(self) -> dict[str, Any]:
        if not self._relay_enabled():
            return {"available": False, "error": "relay is not configured"}
        result = self._relay_request(self._relay_url() + "/health", self._relay_token(), timeout=6)
        return {"available": bool(result.get("success")), "error": result.get("error", "")}

    def _relay_submit(self, task: str, timeout: float) -> dict[str, Any]:
        """Queue a task for the phone's polling workflow and wait for its result."""
        push = self._relay_request(
            self._relay_url() + "/task", self._relay_token(), "POST",
            {"message": task}, timeout=10,
        )
        if not push.get("success"):
            return {"success": False, "backend": "relay", "error": push.get("error", "relay task push failed")}
        task_id = str(push.get("task_id", ""))
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            await_time = 15 if datetime.now().timestamp() + 15 < deadline else max(1.0, deadline - datetime.now().timestamp())
            time.sleep(min(await_time, 15))
            result = self._relay_request(f"{self._relay_url()}/result/{task_id}", self._relay_token(), timeout=10)
            payload = result.get("result") or {}
            status = _text(payload.get("status"), 20)
            if status == "pending" or status == "claimed" or status == "running":
                self._relay_request(
                    self._relay_url() + "/renew", self._relay_token(), "POST",
                    {"task_id": task_id}, timeout=5,
                )
                continue
            if payload.get("ai_response") is not None or payload.get("finished_at") is not None:
                return {
                    "success": bool(payload.get("success")),
                    "backend": "relay",
                    "task_id": task_id,
                    "ai_response": _text(payload.get("ai_response"), 6000),
                    "error": _text(payload.get("error"), 300),
                }
            # 404: task vanished (relay restarted); stop waiting.
            if not result.get("success"):
                return {"success": False, "backend": "relay", "task_id": task_id, "error": result.get("error", "relay lost the task")}
            continue
        return {
            "success": False,
            "backend": "relay",
            "task_id": task_id,
            "error": "queued via relay but the phone did not pick it up in time; it may still run later",
        }

    def _operit_task_sync(self, task: str, show_floating: bool, initial_mode: str) -> dict[str, Any]:
        token = self._operit_token()
        if not token and not self._relay_enabled():
            return {"success": False, "error": "Operit HTTP token is not configured"}
        # Direct Operit first; if Tailscale is down, queue through the relay
        # so the phone's polling workflow can pick the task up without a VPN.
        if token:
            direct = self._operit_task_direct(task, show_floating, initial_mode)
            if direct.get("success") or not self._relay_enabled():
                return direct
            self._audit("relay_fallback", reason=_text(direct.get("error"), 180))
        return self._relay_submit(task, self._operit_timeout())

    def _operit_task_direct(self, task: str, show_floating: bool, initial_mode: str) -> dict[str, Any]:
        token = self._operit_token()
        if not token:
            return {"success": False, "error": "Operit HTTP token is not configured"}
        payload = json.dumps({
            "message": task,
            "response_mode": "sync",
            "show_floating": show_floating,
            "initial_mode": initial_mode if show_floating else None,
            "return_tool_status": False,
            "create_if_none": True,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._operit_url() + "/api/external-chat",
            data=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._operit_timeout()) as response:
                body = response.read(256 * 1024).decode("utf-8", errors="replace")
            data = json.loads(body)
            if isinstance(data, dict):
                return {"success": bool(data.get("success")), "request_id": data.get("request_id"), "chat_id": data.get("chat_id"), "ai_response": str(data.get("ai_response") or "")[-6000:], "error": data.get("error")}
            return {"success": False, "error": "Operit returned an invalid response"}
        except urllib.error.HTTPError as exc:
            return {"success": False, "error": f"Operit HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"success": False, "error": f"Operit connection failed: {str(exc)[:180]}"}
        except json.JSONDecodeError:
            return {"success": False, "error": "Operit returned non-JSON data"}

    def _operit_health_sync(self) -> dict[str, Any]:
        token = self._operit_token()
        if not token:
            return {"available": False, "error": "Operit HTTP token is not configured"}
        request = urllib.request.Request(
            self._operit_url() + "/api/health",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read(4096).decode("utf-8", errors="replace"))
            return {"available": bool(data.get("enabled") and data.get("service_running")), "port": data.get("port"), "version": data.get("version_name")}
        except urllib.error.HTTPError as exc:
            return {"available": False, "error": f"Operit HTTP {exc.code}"}
        except Exception as exc:
            return {"available": False, "error": f"Operit unavailable: {str(exc)[:120]}"}

    async def _run_operit_background(self, task_id: str, task: str, show_floating: bool, initial_mode: str) -> None:
        self._operit_tasks[task_id]["status"] = "running"
        self._save_operit_tasks()
        try:
            result = await asyncio.to_thread(self._operit_task_sync, task, show_floating, initial_mode)
            if self._operit_tasks.get(task_id, {}).get("status") != "cancelled":
                self._operit_tasks[task_id].update({"status": "completed" if result.get("success") else "failed", "result": result})
        except asyncio.CancelledError:
            self._operit_tasks.get(task_id, {}).update({"status": "cancelled"})
        except Exception as exc:
            self._operit_tasks.get(task_id, {}).update({"status": "failed", "result": {"success": False, "error": str(exc)[:200]}})
        self._save_operit_tasks()
        final = self._operit_tasks.get(task_id, {})
        status = final.get("status")
        if status in {"completed", "failed"}:
            summary = _text((final.get("result") or {}).get("ai_response") or final.get("result", {}).get("error"), 200)
            self.device_app.timeline_append(
                "task_result",
                "后台任务" + ("完成" if status == "completed" else "失败"),
                summary or str(final.get("task"))[:120],
                related_task_id=task_id,
            )
        self._audit("operit_task_finished", task_id=task_id, status=self._operit_tasks.get(task_id, {}).get("status"))

    @filter.llm_tool(name="phone_health")
    @_tolerate_nested_args
    @_feature_gate("phone_health")
    async def phone_health(self, event: AstrMessageEvent, days: int = 1, **_kwargs: Any) -> str:
        """Read authorized Xiaomi health data. Use for steps, sleep, heart rate, SpO2, calories, stress, or activity questions.

        Args:
            days(number): How many recent days of health data to read, from 1 to 30. Defaults to 1.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        try:
            days = max(1, min(int(days or 1), 30))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "days must be an integer from 1 to 30"}, ensure_ascii=False)
        health = await asyncio.to_thread(self._read_health_db_sync, days)
        api = self._reality_api()
        getter = getattr(api, "mobile_context", None) if api is not None else None
        telemetry = None
        if callable(getter):
            try:
                context = getter(self._event_user_id(event))
                telemetry = context.get("telemetry") if isinstance(context, dict) else None
            except Exception:
                telemetry = None
        return json.dumps({
            "success": bool(health.get("available")),
            "source": "xiaomi-sync",
            "health": health,
            "telemetry": telemetry,
            "error": health.get("error", "") if not health.get("available") else "",
        }, ensure_ascii=False)

    @filter.llm_tool(name="phone_usage")
    @_tolerate_nested_args
    @_feature_gate("phone_usage")
    async def phone_usage(self, event: AstrMessageEvent, days: int = 1, **_kwargs: Any) -> str:
        """Read Android app usage time through Operit without changing the phone.

        Args:
            days(number): How many recent days of app usage to read, from 1 to 30. Defaults to 1.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        try:
            days = max(1, min(int(days or 1), 30))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "days must be an integer from 1 to 30"}, ensure_ascii=False)
        if self._control_backend() == "app":
            result = await asyncio.to_thread(self.device_app.execute, "usage_stats", {"days": days})
            result["backend"] = "app"
            return json.dumps(result, ensure_ascii=False)
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
        task = f"读取手机最近 {days} 天的应用使用时长，按应用列出分钟数和总计。只读取，不打开、不关闭、不修改任何应用。"
        result = await asyncio.to_thread(self._operit_task_sync, task, False, "WINDOW")
        result["backend"] = "operit"
        return json.dumps(result, ensure_ascii=False)


    @filter.llm_tool(name="phone_reminder")
    @_tolerate_nested_args
    @_feature_gate("phone_reminder")
    async def phone_reminder(
        self,
        event: AstrMessageEvent,
        action: str = "list",
        text: str = "",
        minutes: int = 0,
        reminder_id: str = "",
        **_kwargs: Any,
    ) -> str:
        """Create, list, or cancel a reminder in the current chat session.

        Args:
            action(string): One of: add, cancel, list. Defaults to list.
            text(string): Reminder content, required for action=add.
            minutes(number): Minutes from now until the reminder fires, for action=add. Defaults to 0.
            reminder_id(string): Reminder id to cancel, required for action=cancel.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        action = _text(action, 20).lower()
        if action in {"add", "create", "set"}:
            try:
                delay = max(1, min(int(minutes), 10080))
            except (TypeError, ValueError):
                delay = 30
            text = str(text or "").strip()
            if not text or len(text) > 500:
                return json.dumps({"success": False, "error": "reminder text must contain 1-500 characters"}, ensure_ascii=False)
            reminder_id = uuid.uuid4().hex[:10]
            self._reminders[reminder_id] = {
                "text": text,
                "session": str(getattr(event, "unified_msg_origin", "")),
                "due": datetime.now().timestamp() + delay * 60,
                "source": "chat",
                "status": "pending",
            }
            try:
                self._save_reminders()
            except OSError:
                self._reminders.pop(reminder_id, None)
                return json.dumps({"success": False, "error": "could not persist reminder"})
            self._reminder_tasks[reminder_id] = asyncio.create_task(self._run_reminder(reminder_id))
            self._audit("reminder_created", reminder_id=reminder_id, minutes=delay)
            self.device_app.timeline_append(
                "reminder",
                f"{delay} 分钟后提醒你",
                text[:300],
                event="reminder_created", reminder_id=reminder_id,
            )
            return json.dumps({"success": True, "reminder_id": reminder_id, "minutes": delay}, ensure_ascii=False)
        if action in {"cancel", "remove", "delete"}:
            item = self._cancel_reminder(reminder_id)
            self._audit("reminder_cancelled", reminder_id=reminder_id)
            return json.dumps({"success": bool(item), "reminder_id": reminder_id, "status": "cancelled" if item else "not_found"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "reminders": {key: {"text": value.get("text"), "due": value.get("due")} for key, value in self._reminders.items()},
        }, ensure_ascii=False)

    @filter.llm_tool(name="phone_audit")
    @_tolerate_nested_args
    @_feature_gate("phone_audit")
    async def phone_audit(self, event: AstrMessageEvent, limit: int = 20, **_kwargs: Any) -> str:
        """Read recent phone-agent action metadata without secrets or message contents.
        Args:
            limit(number): Max number of recent records to return, from 1 to 100. Defaults to 20."""
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        records: list[dict[str, Any]] = []
        try:
            try:
                limit = max(1, min(int(limit or 20), 100))
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "limit must be an integer from 1 to 100"}, ensure_ascii=False)
            for line in Path(self._audit_path()).read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return json.dumps({"success": True, "records": records}, ensure_ascii=False)

    @filter.llm_tool(name="operit_task")
    @_tolerate_nested_args
    async def operit_task(
        self,
        event: AstrMessageEvent,
        task: str = "",
        show_floating: bool = True,
        initial_mode: str = "WINDOW",
        background: bool = False,
        confirmed: bool = False,
        **_kwargs: Any,
    ) -> str:
        """Delegate a natural-language UI task to the phone's Operit Agent.

        This is the preferred and only normal route for phone UI or Shizuku
        work. Do not use astrbot_execute_shell or construct ADB commands for
        phone tasks; Operit owns the phone connection.

        Use for screen-aware tasks such as opening an app, finding a contact,
        typing a message, or tapping a button. Operit performs the visual
        interaction through Shizuku; do not describe success until it responds.
        Sending messages or other external side effects still require explicit
        user intent.
        Args:
            task(string): Natural-language description of the phone task to execute on the device.
            show_floating(boolean): Keep Operit's floating bubble visible during the task if true.
            initial_mode(string): Operit observation mode, such as WINDOW.
            background(boolean): Run the task in background if true.
            confirmed(boolean): Set true only after the user explicitly confirmed a high-risk action.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        task = str(task or "").strip()
        if not task or len(task) > 2000:
            return json.dumps({"success": False, "error": "task must contain 1-2000 characters"}, ensure_ascii=False)
        if self._task_requires_confirmation(task) and not confirmed:
            return json.dumps({"success": False, "needs_confirmation": True, "error": "This task has an external side effect; ask the user for confirmation first."}, ensure_ascii=False)
        if self._control_backend() == "app":
            # The device app executes a closed set of structured commands; it
            # has no on-device agent to interpret free-form tasks. Point the
            # LLM at phone_action instead of failing deep inside the call.
            return json.dumps({
                "success": False,
                "backend": "app",
                "error": "control_backend=app executes structured commands only; use phone_action for allowlisted actions instead of operit_task",
            }, ensure_ascii=False)
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
        initial_mode = _text(initial_mode, 30).upper()
        if initial_mode not in {"WINDOW", "BALL", "VOICE_BALL", "FULLSCREEN", "RESULT_DISPLAY", "SCREEN_OCR"}:
            initial_mode = "WINDOW"
        if background:
            try:
                max_tasks = max(1, min(int(self.config.get("max_background_tasks", self._max_background_tasks)), 8))
            except (TypeError, ValueError):
                max_tasks = self._max_background_tasks
            active = sum(value.get("status") in {"queued", "running"} for value in self._operit_tasks.values())
            if active >= max_tasks:
                return json.dumps({"success": False, "error": "too many background phone tasks; wait for one to finish"}, ensure_ascii=False)
            task_id = uuid.uuid4().hex[:12]
            self._operit_tasks[task_id] = {"status": "queued", "task": task, "confirmed": bool(confirmed), "created_at": datetime.now().isoformat(timespec="seconds")}
            self._save_operit_tasks()
            self._operit_task_handles[task_id] = asyncio.create_task(
                self._run_operit_background(task_id, task, bool(show_floating), initial_mode)
            )
            self._audit("operit_task_started", task_id=task_id, background=True, task_length=len(task))
            return json.dumps({"success": True, "accepted": True, "task_id": task_id, "status": "queued"}, ensure_ascii=False)
        self._audit("operit_task_started", background=False, task_length=len(task))
        result = await asyncio.to_thread(self._operit_task_sync, task, bool(show_floating), initial_mode)
        return json.dumps(result, ensure_ascii=False)

    def _task_requires_confirmation(self, task: str) -> bool:
        return bool(re.search(r"(发消息|发送|私信|回复|评论|点赞|转发|删除|卸载|支付|付款|输入|填写|点击|点按|确认按钮|send|message|reply|comment|like|share|delete|uninstall|pay|type|tap|click)", task, re.I))

    @filter.llm_tool(name="operit_task_status")
    @_tolerate_nested_args
    async def operit_task_status(self, event: AstrMessageEvent, task_id: str = "", **_kwargs: Any) -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        if task_id:
            return json.dumps({"success": True, "task": self._operit_tasks.get(task_id, {"status": "not_found"})}, ensure_ascii=False)
        return json.dumps({"success": True, "tasks": {key: {"status": value.get("status"), "created_at": value.get("created_at")} for key, value in self._operit_tasks.items()}}, ensure_ascii=False)

    @filter.llm_tool(name="operit_task_cancel")
    @_tolerate_nested_args
    async def operit_task_cancel(self, event: AstrMessageEvent, task_id: str = "", **_kwargs: Any) -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        item = self._operit_tasks.get(task_id)
        if not item:
            return json.dumps({"success": False, "error": "task not found"}, ensure_ascii=False)
        item["status"] = "cancelled"
        item["cancel_requested"] = True
        item["cancel_note"] = "local wait cancelled; phone-side Operit request may still be running"
        self._save_operit_tasks()
        handle = self._operit_task_handles.get(task_id)
        if handle is not None and handle is not asyncio.current_task():
            handle.cancel()
        self._audit("operit_task_cancelled", task_id=task_id)
        return json.dumps({"success": True, "task_id": task_id, "status": "cancelled"}, ensure_ascii=False)

    @filter.llm_tool(name="operit_task_retry")
    @_tolerate_nested_args
    async def operit_task_retry(self, event: AstrMessageEvent, task_id: str = "", **_kwargs: Any) -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        item = self._operit_tasks.get(task_id)
        if not item or not item.get("task"):
            return json.dumps({"success": False, "error": "task not found or not retryable"}, ensure_ascii=False)
        if self._task_requires_confirmation(str(item["task"])) and not item.get("confirmed"):
            return json.dumps({"success": False, "needs_confirmation": True, "error": "Ask the user for confirmation before retrying this task."}, ensure_ascii=False)
        new_id = uuid.uuid4().hex[:12]
        self._operit_tasks[new_id] = {"status": "queued", "task": item["task"], "confirmed": bool(item.get("confirmed")), "created_at": datetime.now().isoformat(timespec="seconds"), "retry_of": task_id}
        self._save_operit_tasks()
        self._operit_task_handles[new_id] = asyncio.create_task(
            self._run_operit_background(new_id, item["task"], False, "WINDOW")
        )
        return json.dumps({"success": True, "accepted": True, "task_id": new_id, "retry_of": task_id}, ensure_ascii=False)

    @filter.llm_tool(name="phone_observe")
    @_tolerate_nested_args
    @_feature_gate("phone_observe")
    async def phone_observe(self, event: AstrMessageEvent, **_kwargs: Any) -> str:
        """Observe the authorized phone before acting: foreground app, visible UI text, and battery.

        Use this only for the current conversation or task. Never call it from a
        timer, quiet-hours watcher, or other background polling loop.
        Sleep scenario: when it is late at night and the user mentions watching
        videos, or otherwise seems to still be using the phone, call this first
        to confirm which video app is in the foreground before reminding them
        to sleep or suspending it.
        Args:
            (no parameters needed).
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        if self._control_backend() == "app":
            status = await asyncio.to_thread(self.device_app.execute, "status", {})
            if status.get("success"):
                output = status.get("output") or {}
                if isinstance(output, dict):
                    self.device_app.record_device_status(output.get("device") or output)
                return json.dumps({
                    "success": True,
                    "backend": "app",
                    "status": output,
                    "app_policy": {
                        "mode": "on_demand",
                        "temporary_active": self._manual_guard_active(),
                        "suspended": sorted(self._guard_suspended),
                    },
                }, ensure_ascii=False)
            return json.dumps({"success": False, "backend": "app", "error": _text(status.get("error"), 240)}, ensure_ascii=False)
        if self._control_backend() == "operit":
            health = await asyncio.to_thread(self._operit_health_sync)
            if not health.get("available"):
                return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
            result = await asyncio.to_thread(
                self._operit_task_sync,
                "观察当前手机：报告前台应用、电池电量和屏幕主要文字。只观察，不点击、不输入、不修改任何内容。",
                False,
                "WINDOW",
            )
            result["backend"] = "operit"
            return json.dumps(result, ensure_ascii=False)
        async with self._lock:
            ready, detail = await self._ensure_device()
            if not ready:
                # ADB can disappear when Android rotates its wireless-debugging
                # port. Operit remains usable over its Tailscale HTTP service;
                # expose that path so the LLM can continue with operit_task.
                operit = await asyncio.to_thread(self._operit_health_sync)
                return json.dumps({
                    "success": bool(operit.get("available")),
                    "adb": {"available": False, "error": detail},
                    "operit": operit,
                    "next_action": "use operit_task for screen-aware operations" if operit.get("available") else "enable Android wireless debugging",
                }, ensure_ascii=False)
            foreground = await self._execute("foreground_app", {})
            screen = await self._execute("screen_text", {})
            serial = self._serial()
            battery_code, battery_out, battery_err = await self._run("-s", serial, "shell", "dumpsys", "battery")
        level = re.search(r"level:\s*(\d+)", battery_out)
        status = re.search(r"status:\s*(\d+)", battery_out)
        return json.dumps({
            "success": True,
            "foreground": foreground,
            "screen": screen,
            "battery": {"level": int(level.group(1)) if level else None, "status_code": int(status.group(1)) if status else None, "error": battery_err if battery_code else ""},
            "app_policy": {
                "mode": "on_demand",
                "temporary_active": self._manual_guard_active(),
                "suspended": sorted(self._guard_suspended),
            },
        }, ensure_ascii=False)

    @filter.llm_tool(name="phone_location")
    @_tolerate_nested_args
    @_feature_gate("phone_location")
    async def phone_location(
        self,
        event: AstrMessageEvent,
        high_accuracy: bool = False,
        include_address: bool = False,
        timeout_seconds: int = 10,
        confirmed: bool = False,
        **_kwargs: Any,
    ) -> str:
        """Read the phone's current location on demand through Operit.

        Call this only when the user explicitly asks for their location or the
        current task needs it. It never runs in the background and does not
        retain the coordinates. Address lookup or high-accuracy location needs
        an explicit confirmation because they are more sensitive.
        Chain with Baidu Map MCP: inside China the fix is GCJ02 and
        location.bd09_latitude/bd09_longitude are already converted for Baidu
        tools — pass those two fields as latitude/longitude to map_reverse_geocode,
        map_search_places (location), map_directions, or map_road_traffic. Do
        not pass the raw gcj02 latitude/longitude to Baidu tools; outside China
        use the raw wgs84 values. When the user asks "我在哪", call
        phone_location first, then map_reverse_geocode with the bd09 pair.
        Args:
            high_accuracy(boolean): Request a high-accuracy fix when true.
            include_address(boolean): Ask the phone to reverse geocode the fix.
            timeout_seconds(number): Location timeout from 3 to 30 seconds.
            confirmed(boolean): Set true when the user confirmed sensitive location details.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        high_accuracy = self._coerce_bool(high_accuracy)
        include_address = self._coerce_bool(include_address)
        if (high_accuracy or include_address) and not self._coerce_bool(confirmed):
            return json.dumps({
                "success": False,
                "needs_confirmation": True,
                "error": "High-accuracy location or address lookup needs explicit confirmation.",
            }, ensure_ascii=False)
        try:
            timeout_seconds = max(3, min(int(timeout_seconds), 30))
        except (TypeError, ValueError):
            timeout_seconds = 10
        self._audit(
            "phone_location",
            high_accuracy=high_accuracy,
            include_address=include_address,
            timeout_seconds=timeout_seconds,
        )
        if self._control_backend() == "app":
            result = await asyncio.to_thread(
                self.device_app.execute,
                "location",
                {"high_accuracy": high_accuracy, "include_address": include_address},
            )
            result["action"] = "location"
            result["backend"] = "app"
            return json.dumps(result, ensure_ascii=False)
        available = await asyncio.to_thread(self._operit_health_sync)
        if not available.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": available}, ensure_ascii=False)
        self._audit(
            "phone_location",
            high_accuracy=high_accuracy,
            include_address=include_address,
            timeout_seconds=timeout_seconds,
        )
        result = await asyncio.to_thread(
            self._operit_location_sync,
            high_accuracy,
            timeout_seconds,
            include_address,
        )
        result["action"] = "location"
        result["backend"] = "operit"
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="phone_app_policy")
    @_tolerate_nested_args
    @_feature_gate("phone_app_policy")
    async def phone_app_policy(
        self,
        event: AstrMessageEvent,
        action: str = "",
        package: str = "",
        minutes: int = 0,
        reason: str = "",
        **_kwargs: Any,
    ) -> str:
        """Disable or restore one user-selected Android app on demand.

        The LLM may choose this tool when the conversation clearly calls for
        changing the user's own app state. It is event-driven: there is no
        foreground polling or automatic quiet-hours action. A positive minutes
        value schedules one automatic restore; zero keeps the requested state
        until the user asks to restore it. Never target system, control, or
         health-monitoring packages.
        Args:
            action(string): disable/suspend or enable/unsuspend/restore.
            package(string): Android package name or a configured friendly alias.
            minutes(number): Temporary disable duration, from 0 to 1440 minutes.
            reason(string): Short internal reason; it is not stored in the audit log.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        action = _text(action, 30).lower()
        if action in {"suspend", "disable"}:
            normalized = "disable"
        elif action in {"unsuspend", "enable", "restore"}:
            normalized = "enable"
        else:
            return json.dumps({"success": False, "error": "action must be disable or enable"}, ensure_ascii=False)
        package = self._resolve_package(package)
        try:
            minutes = max(0, min(int(minutes or 0), 1440))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "minutes must be an integer from 0 to 1440"}, ensure_ascii=False)
        result = await self._execute_app_policy(package, normalized, minutes)
        self._audit(
            "phone_app_policy",
            action=normalized,
            package=package,
            minutes=minutes,
            reason_provided=bool(_text(reason, 240)),
            success=bool(result.get("success")),
        )
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="phone_action")
    @_tolerate_nested_args
    async def phone_action(
        self,
        event: AstrMessageEvent,
        action: str = "",
        package: str = "",
        text: str = "",
        x: int = -1,
        y: int = -1,
        x1: int = -1,
        y1: int = -1,
        x2: int = -1,
        y2: int = -1,
        duration_ms: int = 300,
        key_code: int = 0,
        workflow_action: str = "",
        extras_json: str = "{}",
        confirmed: bool = False,
        **_kwargs: Any,
    ) -> str:
        """Execute one allowlisted phone action through the selected backend.

        App mode uses structured commands through Shizuku. Operit mode performs
        screen-aware execution. ADB is legacy diagnostics only.
        For phone requests, prefer this tool or operit_task over the generic
        astrbot_execute_shell tool.

        Use this only for explicit phone-control requests. Do not claim an action
        succeeded unless the returned success field is true.

        Never use a timer or quiet-hours watcher to invoke this tool. App policy
        changes should go through phone_app_policy when the conversation calls
        for a specific app decision.

        Sleep scenario: late at night, when the user says or admits they are
        watching videos and shows no intention of sleeping — with or without a
        prior reminder — first call phone_observe to get the foreground
        package, then suspend that video app with action=suspend_app (or
        action=suspend_video_apps for the configured guard list). A short
        playful warning is enough; you do not need to keep asking permission.
        Release the apps at once with unsuspend_app / unsuspend_video_apps
        whenever the user asks, and never suspend non-video apps.
        """
        if not self._enabled():
            return json.dumps({"success": False, "error": "phone agent disabled"}, ensure_ascii=False)
        if not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        action = _text(action, 40).lower()
        if action not in ALLOWED_ACTIONS:
            return json.dumps({"success": False, "error": "unsupported action"}, ensure_ascii=False)
        if action in {"suspend_app", "unsuspend_app", "suspend_video_apps", "unsuspend_video_apps"} and not self._tool_enabled("phone_app_policy"):
            return json.dumps({"success": False, "error": "app policy actions are disabled; enable policy_tools in the plugin config"}, ensure_ascii=False)
        if action == "trigger_workflow" and not confirmed:
            return json.dumps({"success": False, "needs_confirmation": True, "error": "Ask the user for confirmation before triggering an external workflow."}, ensure_ascii=False)

        kwargs = {
            "package": self._resolve_package(package),
            "text": text,
            "x": x,
            "y": y,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "duration_ms": duration_ms,
            "key_code": key_code,
            "workflow_action": workflow_action,
            "extras_json": extras_json,
        }
        if action in {"suspend_app", "unsuspend_app"}:
            protected_error = self._protected_policy_error(kwargs["package"])
            if protected_error:
                return json.dumps({"success": False, "action": action, "package": kwargs["package"], "error": protected_error}, ensure_ascii=False)
        self._audit("phone_action", action=action, package=kwargs.get("package"), backend=self._control_backend())
        if action == "suspend_app":
            result = await self._execute_app_policy(kwargs["package"], "disable")
            return json.dumps(result, ensure_ascii=False)
        if action == "unsuspend_app":
            result = await self._execute_app_policy(kwargs["package"], "enable")
            return json.dumps(result, ensure_ascii=False)
        if self._control_backend() == "operit":
            result = await self._operit_action(action, kwargs)
            return json.dumps(result, ensure_ascii=False)
        if self._control_backend() == "app":
            if action not in COMMAND_ACTIONS:
                return json.dumps({"success": False, "backend": "app", "error_code": "unsupported_action",
                                   "error": "App mode does not support this action; supported: "
                                            + ", ".join(sorted(COMMAND_ACTIONS))}, ensure_ascii=False)
            result = await asyncio.to_thread(self.device_app.execute, action, kwargs)
            return json.dumps(result, ensure_ascii=False)
        async with self._lock:
            ready, detail = await self._ensure_device()
            if not ready:
                return json.dumps({"success": False, "action": action, "error": detail}, ensure_ascii=False)
            result = await self._execute(action, kwargs)
        return json.dumps(result, ensure_ascii=False)

    # ---------- forwarded phone notifications ----------

    MAX_NOTIFICATIONS = 200

    def _notification_path(self) -> Path:
        return plugin_data_path(self.config.get("notification_path"), "phone_agent_notifications.json")

    def _load_notifications(self) -> None:
        try:
            path = self._notification_path()
            if not path.exists():
                return
            value = json.loads(path.read_text(encoding="utf-8"))
            items = value.get("notifications", []) if isinstance(value, dict) else []
            if isinstance(items, list):
                self._notifications = [item for item in items if isinstance(item, dict)][: self.MAX_NOTIFICATIONS]
        except (OSError, ValueError, TypeError):
            logger.warning("phone agent notification file is invalid; ignoring it")

    def _save_notifications(self) -> None:
        path = self._notification_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "notifications": self._notifications}, handle,
                          ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _web_app_notifications(self):
        if not self._enabled():
            return jsonify({"success": False, "error": "phone agent disabled", "error_code": "disabled"}), 503
        if request.method == "GET":
            try:
                limit = max(1, min(int(request.args.get("limit", "50") or 50), 200))
            except (TypeError, ValueError):
                limit = 50
            return jsonify({"success": True, "notifications": self._notifications[:limit]})
        allowed, error = self._app_api_write_allowed("notification_tool")
        if not allowed:
            return jsonify({"success": False, "error": error, "error_code": "forbidden"}), 403
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or not self._finite_json(payload):
            return jsonify({"success": False, "error": "valid JSON object required", "error_code": "invalid_json"}), 400
        items = payload.get("notifications")
        if not isinstance(items, list):
            return jsonify({"success": False, "error": "notifications must be a list", "error_code": "invalid_payload"}), 400
        added = 0
        for entry in items[:50]:
            if not isinstance(entry, dict):
                continue
            package = _text(entry.get("package"), 160)
            title = _text(entry.get("title"), 200)
            body = _text(entry.get("text"), 500)
            try:
                posted = float(entry.get("posted_at"))
            except (TypeError, ValueError):
                posted = time.time()
            if not math.isfinite(posted) or posted <= 0:
                posted = time.time()
            if not (package or title or body):
                continue
            key = _text(entry.get("key"), 160) or f"{package}|{title}|{int(posted)}"
            if any(existing.get("key") == key for existing in self._notifications):
                continue
            self._notifications.insert(0, {
                "key": key,
                "package": package,
                "title": title,
                "text": body,
                "posted_at": posted,
                "received_at": time.time(),
                "is_ongoing": bool(entry.get("is_ongoing")),
            })
            added += 1
        if added:
            self._notifications = self._notifications[: self.MAX_NOTIFICATIONS]
            try:
                self._save_notifications()
            except OSError:
                return jsonify({"success": False, "error": "could not store notifications",
                                "error_code": "storage_unavailable"}), 503
            self._audit("app_notifications_received", count=added)
        return jsonify({"success": True, "added": added, "total": len(self._notifications)})

    @filter.llm_tool(name="phone_notifications")
    @_tolerate_nested_args
    @_feature_gate("phone_notifications")
    async def phone_notifications(self, event: AstrMessageEvent, limit: int = 20, package: str = "", **_kwargs: Any) -> str:
        """Read notifications the phone app forwarded to AstrBot.

        Use when the user asks what notifications arrived, whether anyone
        messaged them, or about missed calls and messages on the phone.

        Args:
            limit(number): How many recent notifications to return, from 1 to 100. Defaults to 20.
            package(str): Optional Android package name filter, for example com.tencent.mm.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        try:
            limit = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        wanted = _text(package, 160)
        items = [item for item in self._notifications if not wanted or item.get("package") == wanted]
        return json.dumps({"success": True, "count": len(items[:limit]), "notifications": items[:limit]},
                          ensure_ascii=False)

    # ---------- screenshots ----------

    def _screenshot_dir(self) -> Path:
        directory = Path(tempfile.gettempdir()) / "astrbot_phone_agent"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    async def _deliver_image(self, event: AstrMessageEvent, path: Path, caption: str) -> bool:
        """Send one local image into the conversation that asked for it."""
        if _ImageComponent is None:
            return False
        session = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not session:
            return False
        chain = MessageChain()
        if caption:
            chain.message(caption)
        chain.chain.append(_ImageComponent.fromFileSystem(str(path)))
        try:
            return bool(await asyncio.wait_for(self.context.send_message(session, chain), timeout=30))
        except Exception:
            logger.warning("phone agent could not deliver the screenshot to the conversation")
            return False

    @filter.llm_tool(name="phone_screenshot")
    @_tolerate_nested_args
    @_feature_gate("phone_screenshot")
    async def phone_screenshot(self, event: AstrMessageEvent, caption: str = "", **_kwargs: Any) -> str:
        """Capture the phone screen and send the picture into this conversation.

        Use when the user asks what is on the phone screen, asks for a
        screenshot, or when answering needs the current screen.

        Args:
            caption(str): Optional short text to send together with the picture.
        """
        if not self._enabled():
            return json.dumps({"success": False, "error": "phone agent disabled"}, ensure_ascii=False)
        if not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        if self._control_backend() != "app":
            return json.dumps({"success": False,
                               "error": "phone_screenshot currently requires control_backend=app"},
                              ensure_ascii=False)
        result = await asyncio.to_thread(self.device_app.execute, "screenshot", {})
        self._audit("phone_screenshot", success=bool(result.get("success")))
        if not result.get("success"):
            return json.dumps({"success": False,
                               "error": result.get("error") or "screenshot failed",
                               "error_code": result.get("error_code") or ""}, ensure_ascii=False)
        output = result.get("output")
        if not isinstance(output, dict) or not output.get("data_base64"):
            return json.dumps({"success": False, "error": "phone did not return image data"}, ensure_ascii=False)
        try:
            raw = base64.b64decode(str(output["data_base64"]), validate=True)
        except (ValueError, binascii.Error):
            return json.dumps({"success": False, "error": "phone returned invalid image data"}, ensure_ascii=False)
        if not raw or len(raw) > MAX_SCREENSHOT_BYTES:
            return json.dumps({"success": False, "error": "phone image is empty or too large"}, ensure_ascii=False)
        path = self._screenshot_dir() / f"screen_{uuid.uuid4().hex[:12]}.jpg"
        try:
            path.write_bytes(raw)
        except OSError:
            return json.dumps({"success": False, "error": "could not store the screenshot"}, ensure_ascii=False)
        # B10: always clean up the temp file, whether delivery succeeds,
        # fails, or raises. The image data is already in memory if needed.
        try:
            delivered = await self._deliver_image(event, path, _text(caption, 200))
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("phone agent could not delete screenshot temp file: %s", path)
        return json.dumps({
            "success": True,
            "delivered": delivered,
            "bytes": len(raw),
            "width": output.get("width"),
            "height": output.get("height"),
            "note": "" if delivered else "captured, but this conversation could not receive the image",
        }, ensure_ascii=False)

    def _protected_policy_error(self, package: str) -> str:
        if package in PROTECTED_POLICY_PACKAGES:
            return "refusing to change a protected control or system package"
        return ""

    def _push_phone_notification(self, title: str, text: str) -> None:
        """Best-effort local notification on the phone; never raises."""
        try:
            if self._control_backend() == "app":
                self.device_app.execute(
                    "send_notification", {"title": title, "text": text}
                )
        except Exception as exc:
            logger.debug("phone notification push failed: %s", _text(exc, 160))

    @staticmethod
    def _parse_suspended_state(output: str) -> bool | None:
        matches = re.findall(r"\bsuspended\s*=\s*(true|false)\b", output or "", re.I)
        if not matches:
            return None
        return matches[-1].lower() == "true"

    def _cancel_policy_expiry(self, package: str, source: str | None = None) -> None:
        leases = self._policy_leases.get(package, {})
        targets = [source] if source is not None else list(leases)
        for item in targets:
            lease = leases.get(item)
            if lease is None:
                continue
            task = lease.task
            lease.task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    def _schedule_policy_expiry(self, package: str, minutes: int, source: str) -> datetime:
        # P1-A: only this source's deadline is rescheduled. Cancelling every
        # timer on the package used to drop another source's countdown, leaving
        # the app suspended with nobody responsible for restoring it.
        self._cancel_policy_expiry(package, source)
        due = datetime.now() + timedelta(minutes=minutes)
        lease = self._policy_leases.setdefault(package, {}).setdefault(
            source, _PolicyLease(source=source)
        )
        lease.until = due.timestamp()
        lease.task = asyncio.create_task(
            self._expire_policy_lease(
                package, source, max(0.0, (due - datetime.now()).total_seconds())
            )
        )
        return due

    async def _apply_app_policy(self, package: str, action: str) -> dict[str, Any]:
        package = self._resolve_package(package)
        action = _text(action, 20).lower()
        if action in {"suspend", "disable"}:
            normalized = "disable"
        elif action in {"unsuspend", "enable", "restore"}:
            normalized = "enable"
        else:
            return {"success": False, "action": action, "package": package, "error": "action must be disable or enable"}
        if not PACKAGE_RE.fullmatch(package):
            return {"success": False, "action": normalized, "package": package, "error": "invalid Android package"}
        protected_error = self._protected_policy_error(package)
        if protected_error:
            return {"success": False, "action": normalized, "package": package, "error": protected_error}
        if self._control_backend() == "app":
            action_name = "suspend_app" if normalized == "disable" else "unsuspend_app"
            result = await asyncio.to_thread(self.device_app.execute, action_name, {"package": package})
            # B07: parse the device's own output for package and state rather
            # than treating success==verified. The app's verifiedState() returns
            # {"package":"...","state":"suspended|unsuspended","verified":true};
            # an unverified or mismatched result must not be reported as success.
            output = result.get("output") if isinstance(result.get("output"), dict) else {}
            device_package = _text(output.get("package"), 160)
            device_state = _text(output.get("state"), 20).lower()
            # B07-v3: verified must be exactly boolean True, not a truthy
            # string like "false". package must be present and match exactly.
            raw_verified = output.get("verified")
            device_verified = raw_verified is True
            expected_state = "suspended" if normalized == "disable" else "unsuspended"
            verified = (
                bool(result.get("success"))
                and device_verified
                and device_package == package  # must be present and match
                and device_state == expected_state
            )
            return {
                "success": verified,
                "action": normalized,
                "package": package,
                "backend": "app",
                "state": device_state or ("unknown" if result.get("success") else ""),
                "verified": verified,
                "error": _text(result.get("error"), 240) if not verified else "",
            }
        if self._control_backend() == "operit":
            available = await asyncio.to_thread(self._operit_health_sync)
            if not available.get("available"):
                return {"success": False, "action": normalized, "package": package, "backend": "operit", "operit": available}
            raw = await asyncio.to_thread(
                self._operit_task_sync,
                self._operit_policy_prompt(package, normalized),
                False,
                "WINDOW",
            )
            return self._parse_policy_result(package, normalized, raw)
        async with self._lock:
            ready, detail = await self._ensure_device()
            if not ready:
                return {"success": False, "action": normalized, "package": package, "backend": "adb", "error": detail}
            command = "suspend" if normalized == "disable" else "unsuspend"
            serial = self._serial()
            code, out, err = await self._run("-s", serial, "shell", "pm", command, "--user", "0", package)
            verify_code, verify_out, verify_err = await self._run(
                "-s", serial, "shell", "dumpsys", "package", package
            )
        suspended = self._parse_suspended_state(verify_out)
        expected = normalized == "disable"
        verified = verify_code == 0 and suspended is not None and suspended == expected
        return {
            "success": code == 0 and verified,
            "action": normalized,
            "package": package,
            "backend": "adb",
            "state": "suspended" if suspended is True else "unsuspended" if suspended is False else "unknown",
            "verified": verified,
            "output": out[-500:],
            "error": (err or verify_err)[-500:] if not verified else "",
        }

    async def _execute_app_policy(self, package: str, action: str, minutes: int = 0, *, source: str = "manual") -> dict[str, Any]:
        """Apply a policy and track per-source ownership.

        B02/P1-A: each disable adds a lease for `source`; each enable releases
        only that source. The actual unsuspend is sent only when no owner
        remains, so the night guard's restore never lifts a manual suspension.
        A restore the device does not confirm keeps its lease instead of
        silently dropping the only record that the app is still suspended.
        """
        action = _text(action, 20).lower()
        if action not in {"disable", "suspend", "enable", "unsuspend", "restore"}:
            return {"success": False, "action": action, "package": self._resolve_package(package), "error": "action must be disable or enable"}
        normalized = "disable" if action in {"disable", "suspend"} else "enable"
        minutes = max(0, min(int(minutes or 0), 1440))
        source = _text(source, 40).lower() or POLICY_LEGACY_SOURCE
        # Resolve once, so the lease key, the guard set and the device command
        # all use the same package name (aliases must not create two keys).
        package = self._resolve_package(package)
        if normalized == "disable":
            # Register the lease before sending the command, so a crash between
            # the policy call and the bookkeeping does not lose the owner.
            lease = self._policy_leases.setdefault(package, {}).setdefault(
                source, _PolicyLease(source=source)
            )
            try:
                result = await self._apply_app_policy(package, normalized)
            except Exception as exc:  # a raising backend must not lose the lease
                result = {"success": False, "action": normalized, "package": package,
                          "error": _text(exc, 240)}
            if not result.get("success"):
                leases = self._policy_leases.get(package, {})
                if leases.get(source) is lease:
                    leases.pop(source, None)
                if not leases:
                    self._policy_leases.pop(package, None)
                return result
            self._guard_suspended.add(package)
            if minutes:
                due = self._schedule_policy_expiry(package, minutes, source)
                result.update({"mode": "temporary", "until": due.isoformat(timespec="minutes")})
            else:
                self._cancel_policy_expiry(package, source)
                lease.until = None
                result["mode"] = "disabled"
            result["lease_released"] = False
            result["owners"] = sorted(self._policy_leases.get(package, {}))
            self._save_policy_state()
            return result
        # enable: release this source's lease. Only send the actual unsuspend
        # when no other source still owns the package.
        released = self._policy_leases.get(package, {}).pop(source, None)
        if released is not None:
            self._cancel_policy_expiry(package, source)
        remaining = sorted(self._policy_leases.get(package, {}))
        if remaining:
            # Other sources still want this package suspended — do NOT enable.
            self._save_policy_state()
            return {
                "success": True,
                "action": normalized,
                "package": package,
                "mode": "still_suspended",
                "lease_released": released is not None,
                "still_suspended": True,
                "remaining_owners": remaining,
                "note": f"package remains suspended by {len(remaining)} other source(s)",
            }
        self._policy_leases.pop(package, None)
        try:
            result = await self._apply_app_policy(package, normalized)
        except Exception as exc:  # keep the restore responsibility on a raise
            result = {"success": False, "action": normalized, "package": package,
                      "error": _text(exc, 240)}
        if result.get("success"):
            self._guard_suspended.discard(package)
            result.update({"mode": "enabled", "lease_released": True, "owners": []})
            self._save_policy_state()
            return result
        # The device did not confirm the restore. Keep the lease (with its
        # recorded deadline) so responsibility for the suspension survives and
        # a later retry can still release it.
        lease = self._policy_leases.setdefault(package, {}).setdefault(
            source, released if released is not None else _PolicyLease(source=source)
        )
        lease.task = None
        self._guard_suspended.add(package)
        result.update({
            "mode": "restore_pending",
            "lease_released": False,
            "restore_pending": True,
            "owners": sorted(self._policy_leases.get(package, {})),
        })
        self._save_policy_state()
        return result

    async def _expire_policy_lease(self, package: str, source: str, delay_seconds: float) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
            # Route through _execute_app_policy so a package still owned by
            # another source is not enabled, and so a failed restore keeps its
            # lease for the next retry.
            result = await self._execute_app_policy(package, "enable", source=source)
            self._audit(
                "phone_app_policy_expired",
                package=package,
                source=source,
                success=bool(result.get("success")),
                mode=result.get("mode", ""),
                restore_pending=bool(result.get("restore_pending")),
                error=_text(result.get("error"), 180),
            )
        except asyncio.CancelledError:
            return
        finally:
            lease = self._policy_leases.get(package, {}).get(source)
            if lease is not None and lease.task is current:
                lease.task = None
            if (
                not any(
                    item.task
                    for leases in self._policy_leases.values()
                    for item in leases.values()
                )
                and self._manual_guard_until is not None
                and datetime.now() >= self._manual_guard_until
            ):
                self._manual_guard_until = None
                self._manual_guard_packages = None
                self._manual_guard_exempt = set()

    async def _apply_sleep_mode(self, packages: set[str], minutes: int) -> dict[str, Any]:
        if not packages:
            return {"success": False, "error": "no valid app packages configured"}
        applied: list[str] = []
        failures: list[dict[str, Any]] = []
        for package in sorted(packages):
            result = await self._execute_app_policy(package, "disable", minutes)
            if result.get("success"):
                applied.append(package)
            else:
                failures.append({"package": package, "error": _text(result.get("error"), 240)})
        if failures:
            rollback: list[dict[str, Any]] = []
            for package in applied:
                restored = await self._execute_app_policy(package, "enable")
                rollback.append({"package": package, "success": bool(restored.get("success"))})
            return {
                "success": False,
                "mode": "not_started",
                "applied": [],
                "failures": failures,
                "rollback": rollback,
                "error": "one or more apps could not be verified as suspended",
            }
        return {"success": True, "mode": "active", "packages": applied}

    async def _restore_guard_apps(self, packages: set[str] | None = None, *, source: str = "manual") -> dict[str, list[str]]:
        """Release one source's claim on each package.

        v4: releasing a source never cancels another source's timer; the timer
        belongs to the lease, not to the package. The returned buckets let
        callers stop claiming "受限应用已全部恢复" when a package is still held
        by another source or the device did not confirm the unsuspend.
        """
        targets = set(packages) if packages is not None else set(self._guard_suspended)
        released: list[str] = []
        still_suspended: list[str] = []
        restore_pending: list[str] = []
        for package in sorted(targets):
            result = await self._execute_app_policy(package, "enable", source=source)
            if not result.get("success"):
                restore_pending.append(package)
            elif result.get("mode") == "still_suspended":
                still_suspended.append(package)
            else:
                released.append(package)
        self._save_policy_state()
        return {
            "released": released,
            "still_suspended": still_suspended,
            "restore_pending": restore_pending,
        }

    @filter.llm_tool(name="phone_sleep_mode")
    @_tolerate_nested_args
    @_feature_gate("phone_sleep_mode")
    async def phone_sleep_mode(
        self,
        event: AstrMessageEvent,
        mode: str = "status",
        minutes: int = 480,
        packages: str = "",
        exempt: str = "",
        **_kwargs: Any,
    ) -> str:
        """Start or stop a temporary video-app sleep mode from natural language.

        Use mode=start for requests like "别让我刷视频两小时" and mode=stop
        for "解除视频限制". It expires automatically and restores suspended apps.
        Args:
            mode(string): status, start, or stop.
            minutes(number): Sleep mode duration in minutes for mode=start. Defaults to 480.
            packages(string): Comma-separated Android package names to restrict.
            exempt(string): Comma-separated packages to exempt from restriction.
        """
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        mode = _text(mode, 20).lower()
        if mode in {"start", "on", "enable"}:
            try:
                minutes = max(5, min(int(minutes), 1440))
            except (TypeError, ValueError):
                minutes = 480
            requested: set[str] = set()
            for value in re.split(r"[,\s]+", packages or ""):
                package = self._resolve_package(value)
                if PACKAGE_RE.fullmatch(package):
                    requested.add(package)
            self._manual_guard_exempt = set()
            for value in re.split(r"[,\s]+", exempt or ""):
                package = self._resolve_package(value)
                if PACKAGE_RE.fullmatch(package):
                    self._manual_guard_exempt.add(package)
            self._manual_guard_packages = requested or self._guard_packages()
            result = await self._apply_sleep_mode(self._manual_guard_packages, minutes)
            if not result.get("success"):
                self._manual_guard_packages = None
                self._manual_guard_exempt = set()
                return json.dumps(result, ensure_ascii=False)
            self._manual_guard_until = datetime.now() + timedelta(minutes=minutes)
            result.update({
                "until": self._manual_guard_until.isoformat(timespec="minutes"),
                "packages": sorted(self._manual_guard_packages),
            })
            return json.dumps(result, ensure_ascii=False)
        if mode in {"stop", "off", "disable", "unlock"}:
            packages_to_restore = set(self._manual_guard_packages or ())
            self._manual_guard_until = None
            self._manual_guard_packages = None
            self._manual_guard_exempt = set()
            await self._restore_guard_apps(packages_to_restore)
            return json.dumps({"success": True, "mode": "stopped"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "mode": "active" if self._manual_guard_active() else "off",
            "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None,
            "packages": sorted(self._manual_guard_packages or self._guard_packages()),
            "suspended": sorted(self._guard_suspended),
        }, ensure_ascii=False)

    # ==================== night guard user release ====================

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        """Deterministic night-guard release and session memory.

        Runs on every private chat message. It only reacts to an explicit
        release phrase; regular chat is never modified. The LLM has no role
        here: restoring guard-owned packages must stay a one-path decision.
        """
        if not self._enabled():
            return
        user_id = self._night_guard_user_id(event)
        session = _text(getattr(event, "unified_msg_origin", ""), 240)
        if not user_id or not session:
            return
        # Defense in depth beyond the PRIVATE_MESSAGE filter: never act on,
        # or record, a group session for night-guard purposes.
        if self._bool_config("night_guard_private_only", True) and ":FriendMessage:" not in session:
            return
        if not self._night_guard_enabled():
            return
        # B01: authorize BEFORE recording the session, so an empty allowlist or
        # a removed user never seeds the night-guard state file.
        if not self._authorized(event):
            return
        self._record_night_guard_session(user_id, session)
        message = ""
        try:
            components = event.get_messages() or []
        except Exception:
            components = []
        texts = []
        for component in components:
            text = getattr(component, "text", None)
            if isinstance(text, str):
                texts.append(text)
        if texts:
            message = " ".join(texts)
        if not message:
            plain = getattr(event.message_obj, "plain_result", None)
            message = plain if isinstance(plain, str) else ""
        message = _text(message, 300)
        if not message or not NG_RELEASE_RE.search(message):
            return
        result = await self._night_guard_release(user_id)
        if result.get("success"):
            restored = result.get("restored") or []
            if restored:
                reply = f"已解除夜间守护的限制（恢复 {len(restored)} 个应用）"
            else:
                reply = "已取消提醒；今晚我不会再管你了，早点睡。"
        else:
            reply = _text(result.get("error"), 200) or "当前没有需要解除的限制"
        try:
            await asyncio.wait_for(event.send(MessageChain().message(reply)), timeout=15)
        except Exception as exc:
            logger.debug("phone agent night guard release reply failed: %s", _text(exc, 160))
