from __future__ import annotations

import asyncio
import functools
import importlib
import json
import math
import re
import sqlite3
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


PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
ALLOWED_ACTIONS = {
    "open_app",
    "close_app",
    "tap",
    "swipe",
    "input_text",
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
POLICY_ACTIONS = {"disable", "enable", "suspend", "unsuspend"}
PROTECTED_POLICY_PACKAGES = frozenset({
    "com.android.settings",
    "com.android.systemui",
    "com.ai.assistance.operit",
    "com.tauru.healthbridge",
})


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


class PhoneAgentPlugin(Star):
    def __init__(self, context: Any, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._active_serial: str | None = None
        self._guard_suspended: set[str] = set()
        self._manual_guard_until: datetime | None = None
        self._manual_guard_packages: set[str] | None = None
        self._manual_guard_exempt: set[str] = set()
        self._policy_expiry_tasks: dict[str, asyncio.Task[Any]] = {}
        self._policy_state: dict[str, float | None] = {}
        self._operit_tasks: dict[str, dict[str, Any]] = {}
        self._operit_task_handles: dict[str, asyncio.Task[Any]] = {}
        self._reminder_tasks: dict[str, asyncio.Task[Any]] = {}
        self._reminders: dict[str, dict[str, Any]] = {}
        self._load_reminders()
        self._load_policy_state()
        self._register_web_api()

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no", "disabled"}
        return bool(value)

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
            ("/app_policy", self._web_app_policy, ["POST"], "Apply an on-demand app policy"),
            ("/sleep_mode", self._web_sleep_mode, ["POST"], "Control temporary sleep mode"),
            ("/location", self._web_location, ["GET"], "Read phone location on demand"),
            ("/health", self._web_health, ["GET"], "Read phone health summary"),
            ("/tasks", self._web_tasks, ["GET"], "List Operit tasks"),
            ("/reminders", self._web_reminders, ["GET"], "List phone reminders"),
            ("/audit", self._web_audit, ["GET"], "List phone action audit"),
        )
        for route, handler, methods, description in routes:
            register(prefix + route, handler, methods, description)

    def _web_config_view(self) -> dict[str, Any]:
        keys = (
            "enabled", "control_backend", "operit_base_url", "allowed_user_ids",
            "use_private_companion_auth", "app_aliases_json", "sleep_guard_packages",
            "sleep_guard_exempt_apps", "operit_timeout_seconds",
        )
        result = {key: self.config.get(key) for key in keys}
        result["operit_token_configured"] = bool(self._operit_token())
        result["sleep_guard_mode"] = "on_demand"
        return result

    async def _web_get_config(self):
        return jsonify({"success": True, "config": self._web_config_view()})

    async def _web_save_config(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        bool_keys = {"enabled", "use_private_companion_auth"}
        int_keys = {"operit_timeout_seconds"}
        text_keys = {
            "control_backend", "operit_base_url", "operit_token", "allowed_user_ids",
            "app_aliases_json", "sleep_guard_packages", "sleep_guard_exempt_apps",
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
                if key == "control_backend" and value not in {"operit", "adb"}:
                    return jsonify({"success": False, "error": "control_backend must be operit or adb"}), 400
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
        if not self._operit_token():
            return jsonify({"success": False, "error": "Operit HTTP token is not configured"})
        try:
            high_accuracy = str(request.args.get("high_accuracy", "false")).lower() in {"1", "true", "yes", "on"}
            include_address = str(request.args.get("include_address", "false")).lower() in {"1", "true", "yes", "on"}
            timeout_seconds = max(3, min(int(request.args.get("timeout", "10")), 30))
        except (TypeError, ValueError):
            high_accuracy, include_address, timeout_seconds = False, False, 10
        try:
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

    async def _web_status(self):
        try:
            operit = await asyncio.wait_for(asyncio.to_thread(self._operit_health_sync), timeout=3)
        except asyncio.TimeoutError:
            operit = {"available": False, "error": "Operit health check timed out"}
        health = await asyncio.to_thread(self._read_health_db_sync, 1)
        return jsonify({
            "success": True,
            "config": self._web_config_view(),
            "operit": operit,
            "health": {
                "available": health.get("available", False),
                "fresh": health.get("fresh", False),
                "days": len(health.get("days", [])),
                "latest": (health.get("days") or [None])[0],
                "sync": health.get("sync"),
                "error": health.get("error"),
            },
            "sleep_mode": {"manual_active": self._manual_guard_active(), "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None, "suspended": sorted(self._guard_suspended)},
            "tasks": len(self._operit_tasks),
            "reminders": len(self._reminders),
        })

    async def _web_sleep_mode(self):
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
        elif mode in {"stop", "off", "disable", "unlock"}:
            self._manual_guard_until = None
            self._manual_guard_packages = None
            self._manual_guard_exempt = set()
            await self._restore_guard_apps()
            self._audit("web_sleep_mode_stopped")
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
        return _text(self.config.get("audit_log_path"), 500) or "phone_agent_audit.jsonl"

    def _audit(self, operation: str, **fields: Any) -> None:
        record = {"time": datetime.now().isoformat(timespec="seconds"), "operation": operation, **fields}
        try:
            path = Path(self._audit_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("phone agent audit write failed: %s", exc)

    def _reminder_path(self) -> str:
        return _text(self.config.get("reminders_path"), 500) or "phone_agent_reminders.json"

    def _policy_state_path(self) -> str:
        return _text(self.config.get("policy_state_path"), 500) or "phone_agent_policies.json"

    def _save_policy_state(self) -> None:
        try:
            path = Path(self._policy_state_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                package: {"until": until}
                for package, until in sorted(self._policy_state.items())
                if package in self._guard_suspended
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("phone agent policy state save failed: %s", exc)

    def _load_policy_state(self) -> None:
        try:
            path = Path(self._policy_state_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return
            now = datetime.now().timestamp()
            for raw_package, item in values.items():
                package = self._resolve_package(raw_package)
                if not PACKAGE_RE.fullmatch(package) or self._protected_policy_error(package):
                    continue
                until: float | None
                if isinstance(item, dict) and item.get("until") is not None:
                    try:
                        until = float(item.get("until"))
                    except (TypeError, ValueError):
                        continue
                else:
                    until = None
                self._guard_suspended.add(package)
                self._policy_state[package] = until
                if until is not None:
                    self._policy_expiry_tasks[package] = asyncio.create_task(
                        self._expire_app_policy(package, max(0, until - now))
                    )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
            logger.warning("phone agent policy state file is invalid; ignoring it")

    def _save_reminders(self) -> None:
        try:
            path = Path(self._reminder_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._reminders, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("phone agent reminder save failed: %s", exc)

    def _load_reminders(self) -> None:
        try:
            path = Path(self._reminder_path())
            if not path.exists():
                return
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                return
            now = datetime.now().timestamp()
            for reminder_id, item in values.items():
                if isinstance(item, dict) and float(item.get("due", 0)) > now and item.get("text") and item.get("session"):
                    self._reminders[str(reminder_id)] = item
                    self._reminder_tasks[str(reminder_id)] = asyncio.create_task(self._run_reminder(str(reminder_id)))
        except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError):
            logger.warning("phone agent reminders file is invalid; ignoring it")

    async def _run_reminder(self, reminder_id: str) -> None:
        item = self._reminders.get(reminder_id)
        if not item:
            return
        try:
            delay = max(0.0, float(item.get("due", 0)) - datetime.now().timestamp())
            await asyncio.sleep(delay)
            item = self._reminders.pop(reminder_id, None)
            self._reminder_tasks.pop(reminder_id, None)
            self._save_reminders()
            if item:
                await self.context.send_message(item["session"], MessageChain().message("提醒：" + str(item["text"])))
                self._audit("reminder_sent", reminder_id=reminder_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("phone agent reminder failed: %s", exc)

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

    def _control_backend(self) -> str:
        value = _text(self.config.get("control_backend"), 30).lower()
        return value if value in {"operit", "adb"} else "operit"

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

    def _parse_location_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = _first_json_object(raw.get("ai_response")) if raw.get("success") else None
        if not payload:
            return {"success": False, "backend": "operit", "error": "Operit did not return a location JSON result"}
        latitude = self._float_in_range(payload.get("latitude"), -90, 90)
        longitude = self._float_in_range(payload.get("longitude"), -180, 180)
        if latitude is None or longitude is None:
            return {"success": False, "backend": "operit", "error": "Operit location result did not contain valid coordinates"}
        result: dict[str, Any] = {
            "success": True,
            "backend": "operit",
            "location": {"latitude": latitude, "longitude": longitude},
        }
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

    def _operit_task_sync(self, task: str, show_floating: bool, initial_mode: str) -> dict[str, Any]:
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
        try:
            result = await asyncio.to_thread(self._operit_task_sync, task, show_floating, initial_mode)
            if self._operit_tasks.get(task_id, {}).get("status") != "cancelled":
                self._operit_tasks[task_id].update({"status": "completed" if result.get("success") else "failed", "result": result})
        except asyncio.CancelledError:
            self._operit_tasks.get(task_id, {}).update({"status": "cancelled"})
        except Exception as exc:
            self._operit_tasks.get(task_id, {}).update({"status": "failed", "result": {"success": False, "error": str(exc)[:200]}})
        self._audit("operit_task_finished", task_id=task_id, status=self._operit_tasks.get(task_id, {}).get("status"))

    @filter.llm_tool(name="phone_health")
    @_tolerate_nested_args
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
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
        task = f"读取手机最近 {days} 天的应用使用时长，按应用列出分钟数和总计。只读取，不打开、不关闭、不修改任何应用。"
        result = await asyncio.to_thread(self._operit_task_sync, task, False, "WINDOW")
        result["backend"] = "operit"
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="phone_reminder")
    @_tolerate_nested_args
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
            }
            self._reminder_tasks[reminder_id] = asyncio.create_task(self._run_reminder(reminder_id))
            self._save_reminders()
            self._audit("reminder_created", reminder_id=reminder_id, minutes=delay)
            return json.dumps({"success": True, "reminder_id": reminder_id, "minutes": delay}, ensure_ascii=False)
        if action in {"cancel", "remove", "delete"}:
            item = self._reminders.pop(reminder_id, None)
            task = self._reminder_tasks.pop(reminder_id, None)
            if task:
                task.cancel()
            self._save_reminders()
            self._audit("reminder_cancelled", reminder_id=reminder_id)
            return json.dumps({"success": bool(item), "reminder_id": reminder_id, "status": "cancelled" if item else "not_found"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "reminders": {key: {"text": value.get("text"), "due": value.get("due")} for key, value in self._reminders.items()},
        }, ensure_ascii=False)

    @filter.llm_tool(name="phone_audit")
    @_tolerate_nested_args
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
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
        initial_mode = _text(initial_mode, 30).upper()
        if initial_mode not in {"WINDOW", "BALL", "VOICE_BALL", "FULLSCREEN", "RESULT_DISPLAY", "SCREEN_OCR"}:
            initial_mode = "WINDOW"
        if background:
            task_id = uuid.uuid4().hex[:12]
            self._operit_tasks[task_id] = {"status": "queued", "task": task, "confirmed": bool(confirmed), "created_at": datetime.now().isoformat(timespec="seconds")}
            self._operit_task_handles[task_id] = asyncio.create_task(
                self._run_operit_background(task_id, task, bool(show_floating), initial_mode)
            )
            self._audit("operit_task_started", task_id=task_id, background=True, task_length=len(task))
            return json.dumps({"success": True, "accepted": True, "task_id": task_id, "status": "queued"}, ensure_ascii=False)
        self._audit("operit_task_started", background=False, task_length=len(task))
        result = await asyncio.to_thread(self._operit_task_sync, task, bool(show_floating), initial_mode)
        return json.dumps(result, ensure_ascii=False)

    def _task_requires_confirmation(self, task: str) -> bool:
        return bool(re.search(r"(发消息|发送|私信|回复|评论|点赞|转发|删除|卸载|支付|付款|send|message|reply|comment|like|share|delete|uninstall|pay)", task, re.I))

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
        self._operit_task_handles[new_id] = asyncio.create_task(
            self._run_operit_background(new_id, item["task"], False, "WINDOW")
        )
        return json.dumps({"success": True, "accepted": True, "task_id": new_id, "retry_of": task_id}, ensure_ascii=False)

    @filter.llm_tool(name="phone_observe")
    @_tolerate_nested_args
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
        workflow_action: str = "",
        extras_json: str = "{}",
        confirmed: bool = False,
        **_kwargs: Any,
    ) -> str:
        """Execute one allowlisted phone action through Operit and Shizuku.

        Operit is the primary backend and performs screen-aware execution. ADB
        is only an optional diagnostic fallback when control_backend=adb.
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
        async with self._lock:
            ready, detail = await self._ensure_device()
            if not ready:
                return json.dumps({"success": False, "action": action, "error": detail}, ensure_ascii=False)
            result = await self._execute(action, kwargs)
        return json.dumps(result, ensure_ascii=False)

    def _protected_policy_error(self, package: str) -> str:
        if package in PROTECTED_POLICY_PACKAGES:
            return "refusing to change a protected control or system package"
        return ""

    @staticmethod
    def _parse_suspended_state(output: str) -> bool | None:
        matches = re.findall(r"\bsuspended\s*=\s*(true|false)\b", output or "", re.I)
        if not matches:
            return None
        return matches[-1].lower() == "true"

    def _cancel_policy_expiry(self, package: str) -> None:
        task = self._policy_expiry_tasks.pop(package, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_policy_expiry(self, package: str, minutes: int) -> datetime:
        self._cancel_policy_expiry(package)
        due = datetime.now() + timedelta(minutes=minutes)
        self._policy_expiry_tasks[package] = asyncio.create_task(
            self._expire_app_policy(package, max(0, (due - datetime.now()).total_seconds()))
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

    async def _execute_app_policy(self, package: str, action: str, minutes: int = 0) -> dict[str, Any]:
        action = _text(action, 20).lower()
        if action not in {"disable", "suspend", "enable", "unsuspend", "restore"}:
            return {"success": False, "action": action, "package": self._resolve_package(package), "error": "action must be disable or enable"}
        normalized = "disable" if action in {"disable", "suspend"} else "enable"
        minutes = max(0, min(int(minutes or 0), 1440))
        result = await self._apply_app_policy(package, normalized)
        if not result.get("success"):
            return result
        package = _text(result.get("package") or package, 160)
        if normalized == "disable":
            self._guard_suspended.add(package)
            if minutes:
                due = self._schedule_policy_expiry(package, minutes)
                self._policy_state[package] = due.timestamp()
                result.update({"mode": "temporary", "until": due.isoformat(timespec="minutes")})
            else:
                self._cancel_policy_expiry(package)
                self._policy_state[package] = None
                result["mode"] = "disabled"
            self._save_policy_state()
        else:
            self._cancel_policy_expiry(package)
            self._guard_suspended.discard(package)
            self._policy_state.pop(package, None)
            self._save_policy_state()
            result["mode"] = "enabled"
        return result

    async def _expire_app_policy(self, package: str, delay_seconds: float) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0, delay_seconds))
            result = await self._apply_app_policy(package, "enable")
            if result.get("success"):
                self._guard_suspended.discard(package)
                self._policy_state.pop(package, None)
                self._save_policy_state()
            self._audit(
                "phone_app_policy_expired",
                package=package,
                success=bool(result.get("success")),
                error=_text(result.get("error"), 180),
            )
        except asyncio.CancelledError:
            return
        finally:
            if self._policy_expiry_tasks.get(package) is current:
                self._policy_expiry_tasks.pop(package, None)
            if (
                not self._policy_expiry_tasks
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

    async def _restore_guard_apps(self, packages: set[str] | None = None) -> None:
        targets = set(packages) if packages is not None else set(self._guard_suspended)
        tasks = []
        for package in targets:
            task = self._policy_expiry_tasks.pop(package, None)
            if task is not None:
                tasks.append(task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for package in targets:
            result = await self._apply_app_policy(package, "enable")
            if result.get("success"):
                self._guard_suspended.discard(package)
                self._policy_state.pop(package, None)
        self._save_policy_state()

    @filter.llm_tool(name="phone_sleep_mode")
    @_tolerate_nested_args
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

    async def terminate(self) -> None:
        # Keep explicitly permanent policies in effect across plugin restarts.
        # Only policies with an expiry timestamp are temporary and need cleanup
        # during shutdown; their expiry tasks cannot survive the event loop.
        temporary = {
            package for package in self._guard_suspended
            if self._policy_state.get(package) is not None
        }
        await self._restore_guard_apps(temporary)
