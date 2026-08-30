from __future__ import annotations

import asyncio
import importlib
import json
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


def _text(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


class PhoneAgentPlugin(Star):
    def __init__(self, context: Any, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._active_serial: str | None = None
        self._guard_task: asyncio.Task[Any] | None = None
        self._guard_suspended: set[str] = set()
        self._manual_guard_until: datetime | None = None
        self._manual_guard_packages: set[str] | None = None
        self._manual_guard_exempt: set[str] = set()
        self._operit_tasks: dict[str, dict[str, Any]] = {}
        self._reminder_tasks: dict[str, asyncio.Task[Any]] = {}
        self._reminders: dict[str, dict[str, Any]] = {}
        self._load_reminders()
        if self._bool_config("sleep_guard_enabled", False):
            try:
                self._guard_task = asyncio.create_task(self._sleep_guard_loop())
            except RuntimeError:
                logger.warning("sleep guard could not start: no running event loop")

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no", "disabled"}
        return bool(value)

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
        return _text(self.config.get("audit_log_path"), 500) or "/home/tauru/data/phone_agent_audit.jsonl"

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
        return _text(self.config.get("reminders_path"), 500) or "/home/tauru/data/phone_agent_reminders.json"

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
            return f"使用 Shizuku 执行 pm suspend --user 0 {package}，只操作这个应用。"
        if action == "unsuspend_app":
            return f"使用 Shizuku 执行 pm unsuspend --user 0 {package}，只恢复这个应用。"
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

    def _guard_window_active(self) -> bool:
        def parse(value: Any, fallback: str) -> dt_time:
            try:
                hour, minute = [int(part) for part in str(value or fallback).split(":", 1)]
                return dt_time(max(0, min(hour, 23)), max(0, min(minute, 59)))
            except (TypeError, ValueError):
                return dt_time.fromisoformat(fallback)

        start = parse(self.config.get("sleep_guard_start"), "00:30")
        end = parse(self.config.get("sleep_guard_end"), "07:00")
        now = datetime.now().time()
        return start <= now < end if start <= end else now >= start or now < end

    def _manual_guard_active(self) -> bool:
        return self._manual_guard_until is not None and datetime.now() < self._manual_guard_until

    def _ensure_guard_task(self) -> None:
        if self._guard_task is None or self._guard_task.done():
            try:
                self._guard_task = asyncio.create_task(self._sleep_guard_loop())
            except RuntimeError:
                logger.warning("sleep guard could not start: no running event loop")

    def _guard_poll_seconds(self) -> float:
        try:
            return max(10.0, min(float(self.config.get("sleep_guard_poll_seconds", 30)), 300.0))
        except (TypeError, ValueError):
            return 30.0

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

    def _read_health_db_sync(self, days: int) -> dict[str, Any]:
        days = max(1, min(int(days or 1), 30))
        end = date.today()
        start = end - timedelta(days=days - 1)
        db_path = self._health_db()
        result: dict[str, Any] = {"source": "xiaomi", "days": []}
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date DESC",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                # Xiaomi usually publishes the current day after the next sync;
                # expose the newest available day instead of returning an empty
                # answer for a "today" question during the early hours.
                if not rows:
                    rows = db.execute(
                        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (days,)
                    ).fetchall()
                for row in rows:
                    item = {key: row[key] for key in row.keys() if row[key] is not None}
                    item.pop("updated_at", None)
                    item.pop("source", None)
                    result["days"].append(item)
                weights = db.execute(
                    "SELECT time, weight, bmi, body_fat_rate FROM weight_log ORDER BY time DESC LIMIT 10"
                ).fetchall()
                result["weight"] = [dict(row) for row in weights]
                pressure = db.execute(
                    "SELECT time, systolic, diastolic, pulse FROM blood_pressure ORDER BY time DESC LIMIT 10"
                ).fetchall()
                result["blood_pressure"] = [dict(row) for row in pressure]
                segments = db.execute(
                    "SELECT date, kind, duration_min, deep_min, light_min, rem_min, awake_min, avg_hr, avg_spo2 "
                    "FROM sleep_segments WHERE date BETWEEN ? AND ? ORDER BY date DESC, bedtime_ts DESC",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                result["sleep_segments"] = [dict(row) for row in segments]
            result["available"] = bool(result["days"] or result["weight"] or result["blood_pressure"])
            return result
        except Exception as exc:
            logger.warning("health database read failed: %s", exc)
            return {"source": "xiaomi", "available": False, "error": str(exc)[:200]}

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
    async def phone_health(self, event: AstrMessageEvent, days: int = 1) -> str:
        """Read authorized Xiaomi health data. Use for steps, sleep, heart rate, SpO2, calories, stress, or activity questions."""
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
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
        return json.dumps({"success": True, "health": health, "telemetry": telemetry}, ensure_ascii=False)

    @filter.llm_tool(name="phone_usage")
    async def phone_usage(self, event: AstrMessageEvent, days: int = 1) -> str:
        """Read Android app usage time through Operit without changing the phone."""
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        days = max(1, min(int(days or 1), 30))
        health = await asyncio.to_thread(self._operit_health_sync)
        if not health.get("available"):
            return json.dumps({"success": False, "backend": "operit", "operit": health}, ensure_ascii=False)
        task = f"读取手机最近 {days} 天的应用使用时长，按应用列出分钟数和总计。只读取，不打开、不关闭、不修改任何应用。"
        result = await asyncio.to_thread(self._operit_task_sync, task, False, "WINDOW")
        result["backend"] = "operit"
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="phone_reminder")
    async def phone_reminder(
        self,
        event: AstrMessageEvent,
        action: str = "list",
        text: str = "",
        minutes: int = 0,
        reminder_id: str = "",
    ) -> str:
        """Create, list, or cancel a reminder in the current chat session."""
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
    async def phone_audit(self, event: AstrMessageEvent, limit: int = 20) -> str:
        """Read recent phone-agent action metadata without secrets or message contents."""
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        records: list[dict[str, Any]] = []
        try:
            for line in Path(self._audit_path()).read_text(encoding="utf-8").splitlines()[-max(1, min(int(limit or 20), 100)):]:
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
    async def operit_task(
        self,
        event: AstrMessageEvent,
        task: str = "",
        show_floating: bool = True,
        initial_mode: str = "WINDOW",
        background: bool = False,
        confirmed: bool = False,
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
            asyncio.create_task(self._run_operit_background(task_id, task, bool(show_floating), initial_mode))
            self._audit("operit_task_started", task_id=task_id, background=True, task_length=len(task))
            return json.dumps({"success": True, "accepted": True, "task_id": task_id, "status": "queued"}, ensure_ascii=False)
        self._audit("operit_task_started", background=False, task_length=len(task))
        result = await asyncio.to_thread(self._operit_task_sync, task, bool(show_floating), initial_mode)
        return json.dumps(result, ensure_ascii=False)

    def _task_requires_confirmation(self, task: str) -> bool:
        return bool(re.search(r"(发消息|发送|私信|回复|评论|点赞|转发|删除|卸载|支付|付款|send|message|reply|comment|like|share|delete|uninstall|pay)", task, re.I))

    @filter.llm_tool(name="operit_task_status")
    async def operit_task_status(self, event: AstrMessageEvent, task_id: str = "") -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        if task_id:
            return json.dumps({"success": True, "task": self._operit_tasks.get(task_id, {"status": "not_found"})}, ensure_ascii=False)
        return json.dumps({"success": True, "tasks": {key: {"status": value.get("status"), "created_at": value.get("created_at")} for key, value in self._operit_tasks.items()}}, ensure_ascii=False)

    @filter.llm_tool(name="operit_task_cancel")
    async def operit_task_cancel(self, event: AstrMessageEvent, task_id: str = "") -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        item = self._operit_tasks.get(task_id)
        if not item:
            return json.dumps({"success": False, "error": "task not found"}, ensure_ascii=False)
        item["status"] = "cancelled"
        self._audit("operit_task_cancelled", task_id=task_id)
        return json.dumps({"success": True, "task_id": task_id, "status": "cancelled"}, ensure_ascii=False)

    @filter.llm_tool(name="operit_task_retry")
    async def operit_task_retry(self, event: AstrMessageEvent, task_id: str = "") -> str:
        if not self._enabled() or not self._authorized(event):
            return json.dumps({"success": False, "error": "user is not authorized"}, ensure_ascii=False)
        item = self._operit_tasks.get(task_id)
        if not item or not item.get("task"):
            return json.dumps({"success": False, "error": "task not found or not retryable"}, ensure_ascii=False)
        if self._task_requires_confirmation(str(item["task"])) and not item.get("confirmed"):
            return json.dumps({"success": False, "needs_confirmation": True, "error": "Ask the user for confirmation before retrying this task."}, ensure_ascii=False)
        new_id = uuid.uuid4().hex[:12]
        self._operit_tasks[new_id] = {"status": "queued", "task": item["task"], "confirmed": bool(item.get("confirmed")), "created_at": datetime.now().isoformat(timespec="seconds"), "retry_of": task_id}
        asyncio.create_task(self._run_operit_background(new_id, item["task"], False, "WINDOW"))
        return json.dumps({"success": True, "accepted": True, "task_id": new_id, "retry_of": task_id}, ensure_ascii=False)

    @filter.llm_tool(name="phone_observe")
    async def phone_observe(self, event: AstrMessageEvent) -> str:
        """Observe the authorized phone before acting: foreground app, visible UI text, and battery."""
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
            "sleep_guard": {"scheduled": self._bool_config("sleep_guard_enabled", False), "manual_active": self._manual_guard_active(), "suspended": sorted(self._guard_suspended)},
        }, ensure_ascii=False)

    @filter.llm_tool(name="phone_action")
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
    ) -> str:
        """Execute one allowlisted phone action through Operit and Shizuku.

        Operit is the primary backend and performs screen-aware execution. ADB
        is only an optional diagnostic fallback when control_backend=adb.
        For phone requests, prefer this tool or operit_task over the generic
        astrbot_execute_shell tool.

        Use this only for explicit phone-control requests. Do not claim an action
        succeeded unless the returned success field is true.
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
        self._audit("phone_action", action=action, package=kwargs.get("package"), backend=self._control_backend())
        if self._control_backend() == "operit":
            result = await self._operit_action(action, kwargs)
            return json.dumps(result, ensure_ascii=False)
        async with self._lock:
            ready, detail = await self._ensure_device()
            if not ready:
                return json.dumps({"success": False, "action": action, "error": detail}, ensure_ascii=False)
            result = await self._execute(action, kwargs)
        return json.dumps(result, ensure_ascii=False)

    async def _sleep_guard_loop(self) -> None:
        """Suspend configured video apps only during the configured quiet hours."""
        while True:
            try:
                await asyncio.sleep(self._guard_poll_seconds())
                if not self._enabled():
                    continue
                active = self._manual_guard_active() or (
                    self._bool_config("sleep_guard_enabled", False) and self._guard_window_active()
                )
                if self._control_backend() == "operit":
                    packages = sorted(
                        (self._manual_guard_packages - self._manual_guard_exempt)
                        if self._manual_guard_active() and self._manual_guard_packages
                        else self._guard_packages()
                    )
                    if packages and (active or self._guard_suspended):
                        if active:
                            task = (
                                "检查当前前台应用；如果当前应用的包名是以下任意一个："
                                + ", ".join(packages)
                                + "，使用 Shizuku 执行 pm suspend --user 0 <当前包名>；否则只回复 no-op。"
                            )
                        else:
                            task = "使用 Shizuku 执行 pm unsuspend --user 0 " + " ".join(packages) + "，只恢复睡眠守护目标应用。"
                        result = await asyncio.to_thread(self._operit_task_sync, task, False, "WINDOW")
                        if active and result.get("success"):
                            self._guard_suspended.update(packages)
                        elif not active and result.get("success"):
                            self._guard_suspended.clear()
                    continue
                async with self._lock:
                    ready, _ = await self._ensure_device()
                    if not ready:
                        continue
                    serial = self._serial()
                    if active:
                        code, out, _ = await self._run("-s", serial, "shell", "dumpsys", "activity", "activities")
                        match = re.search(r"(?:mResumedActivity:|mCurrentFocus=Window\{[^}]+\s+)[^\s]+\s+(?:u\d+\s+)?([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", out)
                        package = match.group(1) if match else ""
                        packages = self._manual_guard_packages if self._manual_guard_active() else self._guard_packages()
                        if code == 0 and package in (packages or set()):
                            suspended, _, _ = await self._run("-s", serial, "shell", "pm", "suspend", "--user", "0", package)
                            if suspended == 0:
                                self._guard_suspended.add(package)
                                logger.info("sleep guard suspended %s", package)
                    elif self._guard_suspended:
                        for package in tuple(self._guard_suspended):
                            restored, _, _ = await self._run("-s", serial, "shell", "pm", "unsuspend", "--user", "0", package)
                            if restored == 0:
                                self._guard_suspended.discard(package)
                                logger.info("sleep guard restored %s", package)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("sleep guard iteration failed: %s", exc)

    async def _restore_guard_apps(self) -> None:
        if not self._guard_suspended:
            return
        if self._control_backend() == "operit":
            result = await asyncio.to_thread(
                self._operit_task_sync,
                "使用 Shizuku 执行 pm unsuspend --user 0 " + " ".join(sorted(self._guard_suspended)) + "，只恢复睡眠守护目标应用。",
                False,
                "WINDOW",
            )
            if result.get("success"):
                self._guard_suspended.clear()
            return
        async with self._lock:
            ready, _ = await self._ensure_device()
            if not ready:
                return
            serial = self._serial()
            for package in tuple(self._guard_suspended):
                restored, _, _ = await self._run("-s", serial, "shell", "pm", "unsuspend", "--user", "0", package)
                if restored == 0:
                    self._guard_suspended.discard(package)

    @filter.llm_tool(name="phone_sleep_mode")
    async def phone_sleep_mode(
        self,
        event: AstrMessageEvent,
        mode: str = "status",
        minutes: int = 480,
        packages: str = "",
        exempt: str = "",
    ) -> str:
        """Start or stop a temporary video-app sleep mode from natural language.

        Use mode=start for requests like "别让我刷视频两小时" and mode=stop
        for "解除视频限制". It expires automatically and restores suspended apps.
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
            self._manual_guard_until = datetime.now() + timedelta(minutes=minutes)
            self._ensure_guard_task()
            return json.dumps({
                "success": True,
                "mode": "active",
                "until": self._manual_guard_until.isoformat(timespec="minutes"),
                "packages": sorted(self._manual_guard_packages),
            }, ensure_ascii=False)
        if mode in {"stop", "off", "disable", "unlock"}:
            self._manual_guard_until = None
            self._manual_guard_packages = None
            self._manual_guard_exempt = set()
            await self._restore_guard_apps()
            return json.dumps({"success": True, "mode": "stopped"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "mode": "active" if self._manual_guard_active() else "scheduled" if self._guard_window_active() else "off",
            "until": self._manual_guard_until.isoformat(timespec="minutes") if self._manual_guard_until else None,
            "packages": sorted(self._manual_guard_packages or self._guard_packages()),
            "suspended": sorted(self._guard_suspended),
        }, ensure_ascii=False)

    async def terminate(self) -> None:
        if self._guard_task is not None:
            self._guard_task.cancel()
            try:
                await self._guard_task
            except asyncio.CancelledError:
                pass
        await self._restore_guard_apps()
