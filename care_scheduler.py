"""Autonomy scheduler: when the companion bridge gets a care tick.

Extracted from main.py so the plugin file keeps only assembly. This is a mixin:
every method assumes the plugin supplies config, audit and the night-guard
device helpers, so it must be mixed into PhoneAgentPlugin, not used alone.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


def _text(value: Any, limit: int = 240) -> str:
    """Same shape as main's helper: collapse whitespace, then truncate."""
    return " ".join(str(value or "").split())[:limit]


class CareSchedulerMixin:
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

