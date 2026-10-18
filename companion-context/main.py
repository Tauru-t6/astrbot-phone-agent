from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import json
import os
import re
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Star


def _clean(value: Any, limit: int = 800) -> str:
    return " ".join(str(value or "").split())[:limit]


class PhoneCompanionContext(Star):
    """Optional server-local bridge; it never changes the public phone plugin."""

    def __init__(self, context: Any, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._install_task = asyncio.create_task(self._install_when_ready())
        self._target: Any | None = None
        self._original: Any | None = None
        self._wrapped: Any | None = None
        self._last_expression: dict[str, str] = {}
        self._last_relation_cue: dict[str, str] = {}
        self._last_affection_coda: dict[str, datetime] = {}
        self._decision_last: dict[str, datetime] = {}
        self._decision_day: dict[str, str] = {}
        self._decision_count: dict[str, int] = {}
        self._decision_eval_count: dict[str, int] = {}
        self._decision_eval_last: dict[str, datetime] = {}
        # v4 §4: one slot per send, held under a lock so two concurrent
        # evaluations cannot both pass a daily_limit=1 gate.
        self._reservations: dict[str, dict[str, Any]] = {}
        self._decision_lock = asyncio.Lock()
        # P1-D: recent outcomes + a read-only snapshot the app can show, so the
        # autonomy page stops saying "not supported" for care itself.
        self._care_recent: dict[str, list[dict[str, Any]]] = {}
        self._last_outcome: dict[str, dict[str, Any]] = {}
        self._observe_lock = asyncio.Lock()
        self._screen_cache: tuple[int, datetime, dict[str, Any]] | None = None
        self._health_cache: tuple[datetime, dict[str, Any]] | None = None
        self._bridge_status: dict[str, Any] = {"enabled": self._enabled(), "installed": False}
        self._load_decision_budget()

    def _enabled(self) -> bool:
        value = self.config.get("enabled", True)
        return str(value).strip().lower() not in {"0", "false", "off", "no"}

    def _timeout(self) -> float:
        try:
            return max(5.0, min(float(self.config.get("timeout_seconds", 20)), 60.0))
        except (TypeError, ValueError):
            return 20.0

    def _intensity(self) -> str:
        value = _clean(self.config.get("romance_intensity", "firm"), 20).lower()
        return value if value in {"soft", "warm", "firm", "bold", "devoted"} else "firm"

    def _pet_name(self) -> str:
        return _clean(self.config.get("pet_name", ""), 20)

    def _custom_romance_prompt(self) -> str:
        return _clean(self.config.get("custom_romance_prompt", ""), 600)

    def _feature_enabled(self, key: str, default: bool = True) -> bool:
        value = self.config.get(key, default)
        return str(value).strip().lower() not in {"0", "false", "off", "no"}

    def _relationship_enabled(self) -> bool:
        return self._feature_enabled("relationship_mode_enabled", False)

    def _autonomous_enabled(self) -> bool:
        return self._relationship_enabled() and self._feature_enabled("autonomous_decision_enabled", False)

    def _decision_mode(self) -> str:
        value = _clean(self.config.get("autonomous_decision_mode", "preview"), 30).lower()
        # R02: recognized modes. preview blocks all side effects; care_auto
        # allows sending authorized care messages; low_risk_auto is an alias
        # for care_auto (legacy). phone_* modes are not yet implemented.
        recognized = {"preview", "care_auto", "low_risk_auto"}
        return value if value in recognized else "preview"

    def _mode_allows_send(self) -> bool:
        """Whether the current mode permits actual message delivery."""
        return self._decision_mode() in {"care_auto", "low_risk_auto"}

    def _decision_cooldown(self) -> timedelta:
        try:
            minutes = max(15, min(int(self.config.get("decision_cooldown_minutes", 180)), 1440))
        except (TypeError, ValueError):
            minutes = 180
        return timedelta(minutes=minutes)

    def _decision_daily_limit(self) -> int:
        try:
            return max(0, min(int(self.config.get("decision_daily_limit", 3)), 20))
        except (TypeError, ValueError):
            return 3

    def _quiet_hours(self) -> tuple[int, int] | None:
        value = _clean(self.config.get("quiet_hours", "00:30-08:00"), 30)
        match = re.fullmatch(r"(\d{1,2}):([0-5]\d)\s*-\s*(\d{1,2}):([0-5]\d)", value)
        if not match:
            return None
        start = int(match.group(1)) * 60 + int(match.group(2))
        end = int(match.group(3)) * 60 + int(match.group(4))
        if start >= 1440 or end >= 1440:
            return None
        return start, end

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        window = self._quiet_hours()
        if window is None:
            return False
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        start, end = window
        return minute >= start or minute < end if start > end else start <= minute < end

    def _decision_eval_limit(self) -> int:
        """v4 §4: model evaluations get their own daily cap."""
        try:
            return max(0, min(int(self.config.get("decision_eval_daily_limit", 24)), 200))
        except (TypeError, ValueError):
            return 24

    def _decision_eval_interval(self) -> timedelta:
        """v4 §4: minimum gap between evaluations, so no_action cannot spin."""
        try:
            seconds = max(0, min(int(self.config.get("decision_eval_min_interval_seconds", 300)), 3600))
        except (TypeError, ValueError):
            seconds = 300
        return timedelta(seconds=seconds)

    def _reservation_ttl(self) -> float:
        try:
            return float(max(60, min(int(self.config.get("decision_reservation_ttl_seconds", 300)), 3600)))
        except (TypeError, ValueError):
            return 300.0

    def _reset_decision_day(self, user_id: str, now: datetime) -> None:
        day = now.date().isoformat()
        if self._decision_day.get(user_id) == day:
            return
        self._decision_day[user_id] = day
        self._decision_count[user_id] = 0
        self._decision_eval_count[user_id] = 0

    def _reservations_for(self, user_id: str) -> list[dict[str, Any]]:
        return [item for item in self._reservations.values() if item.get("user_id") == user_id]

    def _decision_allowed(self, user_id: str, now: datetime | None = None) -> tuple[bool, str]:
        """Read-only gate check used to shape the prompt; it reserves nothing."""
        now = now or datetime.now()
        if self._in_quiet_hours(now):
            return False, "quiet_hours"
        count = (
            self._decision_count.get(user_id, 0)
            if self._decision_day.get(user_id) == now.date().isoformat()
            else 0
        )
        if now - self._decision_last.get(user_id, datetime.min) < self._decision_cooldown():
            return False, "cooldown"
        if count >= self._decision_daily_limit():
            return False, "daily_limit"
        return True, "allowed"

    def _sweep_reservations(self, user_id: str, now: float | None = None) -> bool:
        """Expire stale reservations. Returns True when something changed."""
        current = time.time() if now is None else now
        changed = False
        for item in self._reservations_for(user_id):
            if item.get("status") not in {"reserved", "generated"}:
                continue
            if float(item.get("expires_at") or 0) > current:
                continue
            if item["status"] == "reserved":
                # The model never returned, so nothing was produced: hand the
                # slot back instead of leaking quota forever.
                self._decision_count[user_id] = max(0, self._decision_count.get(user_id, 0) - 1)
                item["status"] = "released"
                item["released_reason"] = "reservation_expired"
            else:
                # The message was handed to the host and no receipt came back.
                # Keep it counted (it may have been delivered) and never retry.
                item["status"] = "unknown"
            changed = True
        return changed

    async def _reserve_decision(self, user_id: str) -> tuple[dict[str, Any] | None, str]:
        """Check every gate and hold one send slot, atomically.

        v4 §4: the old code checked the quota and recorded it later, so two
        concurrent evaluations both passed a daily_limit=1 gate. The check and
        the claim now share one lock, and the claim is a reservation with an id
        and a state so it can be released or marked unknown later.
        """
        now = datetime.now()
        async with self._decision_lock:
            self._sweep_reservations(user_id)
            self._reset_decision_day(user_id, now)
            if self._in_quiet_hours(now):
                return None, "quiet_hours"
            if any(item.get("status") == "reserved" for item in self._reservations_for(user_id)):
                # One evaluation at a time per user: a slow model must not let a
                # second call slip past the same gate.
                return None, "in_flight"
            if self._decision_eval_count.get(user_id, 0) >= self._decision_eval_limit():
                return None, "eval_limit"
            if now - self._decision_eval_last.get(user_id, datetime.min) < self._decision_eval_interval():
                return None, "eval_throttled"
            if now - self._decision_last.get(user_id, datetime.min) < self._decision_cooldown():
                return None, "cooldown"
            if self._decision_count.get(user_id, 0) >= self._decision_daily_limit():
                return None, "daily_limit"
            stamp = time.time()
            reservation = {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "status": "reserved",
                "created_at": stamp,
                "expires_at": stamp + self._reservation_ttl(),
            }
            self._reservations[reservation["id"]] = reservation
            self._decision_count[user_id] = self._decision_count.get(user_id, 0) + 1
            self._decision_eval_count[user_id] = self._decision_eval_count.get(user_id, 0) + 1
            self._decision_eval_last[user_id] = now
            if not self._save_decision_budget():
                # An unwritable budget cannot bound anything: refuse the slot
                # rather than send on unverified quota.
                self._reservations.pop(reservation["id"], None)
                self._decision_count[user_id] = max(0, self._decision_count.get(user_id, 0) - 1)
                self._decision_eval_count[user_id] = max(0, self._decision_eval_count.get(user_id, 0) - 1)
                return None, "budget_unavailable"
            return reservation, "allowed"

    async def _commit_decision_generated(self, reservation: dict[str, Any] | None) -> bool:
        """Mark the reserved slot as a generated message and persist it."""
        if not reservation:
            return False
        async with self._decision_lock:
            item = self._reservations.get(reservation["id"])
            if item is None or item.get("status") != "reserved":
                return False
            stamp = time.time()
            item["status"] = "generated"
            item["generated_at"] = stamp
            item["expires_at"] = stamp + self._reservation_ttl()
            self._decision_last[item["user_id"]] = datetime.now()
            return self._save_decision_budget()

    async def _release_decision(self, reservation: dict[str, Any] | None, reason: str) -> None:
        """Give the slot back because no message reached the user."""
        if not reservation:
            return
        async with self._decision_lock:
            item = self._reservations.get(reservation["id"])
            if item is None or item.get("status") not in {"reserved", "generated"}:
                return
            item["status"] = "released"
            item["released_reason"] = reason
            user_id = item["user_id"]
            self._decision_count[user_id] = max(0, self._decision_count.get(user_id, 0) - 1)
            self._save_decision_budget()

    async def _mark_delivery_failed(self, reservation_id: str, reason: str = "host_send_failed") -> None:
        """A host adapter reported a failed send: release the held slot."""
        async with self._decision_lock:
            item = self._reservations.get(reservation_id)
            if item is None or item.get("status") not in {"reserved", "generated"}:
                return
            item["status"] = "failed"
            item["released_reason"] = reason
            user_id = item["user_id"]
            self._decision_count[user_id] = max(0, self._decision_count.get(user_id, 0) - 1)
            self._save_decision_budget()

    async def _mark_delivered(self, reservation_id: str) -> None:
        """A host adapter confirmed delivery: the reservation becomes a send."""
        async with self._decision_lock:
            item = self._reservations.get(reservation_id)
            if item is None or item.get("status") not in {"reserved", "generated"}:
                return
            item["status"] = "delivered"
            self._save_decision_budget()

    # ---------- care status for the app (P1-D) ----------

    CARE_STATUS_VERSION = 1

    def _care_status_path(self) -> Path:
        raw = _clean(self.config.get("care_status_path", ""), 500)
        default_dir = Path("data") / "plugin_data" / "astrbot_plugin_phone_agent"
        path = Path(raw) if raw else default_dir / "phone_agent_care_status.json"
        return path if path.is_absolute() else default_dir / path.name

    def _record_care_outcome(self, user_id: str, outcome: str, reason: str = "", detail: str = "") -> None:
        entry = {
            "at": time.time(),
            "outcome": outcome,
            "reason": reason,
            "detail": _clean(detail, 160),
        }
        recent = self._care_recent.setdefault(user_id, [])
        recent.append(entry)
        del recent[:-10]
        self._last_outcome[user_id] = entry
        self._publish_care_status()

    def _publish_care_status(self) -> None:
        """Write the read-only care snapshot the phone plugin serves to the app."""
        path = self._care_status_path()
        now = datetime.now()
        payload: dict[str, Any] = {
            "version": self.CARE_STATUS_VERSION,
            "updated_at": time.time(),
            "bridge_enabled": self._enabled(),
            "autonomy_enabled": self._autonomous_enabled(),
            "mode": self._decision_mode(),
            "quiet_hours": _clean(self.config.get("quiet_hours", "00:30-08:00"), 30),
            "in_quiet_hours": self._in_quiet_hours(now),
            "cooldown_minutes": int(self._decision_cooldown().total_seconds() // 60),
            "daily_limit": self._decision_daily_limit(),
            "eval_daily_limit": self._decision_eval_limit(),
            "eval_min_interval_seconds": int(self._decision_eval_interval().total_seconds()),
            "users": {},
        }
        for uid in sorted(set(self._decision_day) | set(self._decision_count) | set(self._care_recent)):
            last_sent = self._decision_last.get(uid)
            last_eval = self._decision_eval_last.get(uid)
            payload["users"][uid] = {
                "day": self._decision_day.get(uid, ""),
                "sent_today": self._decision_count.get(uid, 0),
                "eval_today": self._decision_eval_count.get(uid, 0),
                "remaining": max(0, self._decision_daily_limit() - self._decision_count.get(uid, 0)),
                "last_sent_at": last_sent.isoformat(timespec="minutes") if last_sent else "",
                "last_eval_at": last_eval.isoformat(timespec="minutes") if last_eval else "",
                "recent": list(self._care_recent.get(uid, [])),
            }
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as exc:
            # The snapshot is diagnostics only; never fail a decision over it.
            logger.warning("[PhoneCompanionContext] care status write failed: %s", exc)
        finally:
            temporary.unlink(missing_ok=True)

    def _companion_name(self) -> str:
        target = self._target
        for attr in ("companion_name", "persona_name", "bot_name"):
            value = _clean(getattr(target, attr, ""), 40) if target is not None else ""
            if value:
                return value
        return self._pet_name() or "companion"

    async def _deliver(self, umo: str, text: str) -> bool:
        """Send the decided message ourselves.

        The host path delivers whatever the generation function returns, but the
        scheduler calls the generation function directly, so nobody else would
        send it. Without this the care status claimed "sent" while nothing ever
        reached the chat.
        """
        if not text or not umo:
            return False
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            logger.warning("[PhoneCompanionContext] care delivery impossible: no send_message")
            return False
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            payload: Any = MessageChain([Plain(text)])
        except Exception:  # pragma: no cover - older host without message components
            payload = text
        try:
            await sender(umo, payload)
        except Exception as exc:
            logger.warning("[PhoneCompanionContext] care delivery failed: %s", _clean(exc, 160))
            return False
        logger.info("[PhoneCompanionContext] care delivered to %s: %s", umo, _clean(text, 80))
        return True

    async def evaluate_care(self, user_id: str, umo: str = "", relationship_role: str = "") -> dict[str, Any]:
        """Scheduler entry point (P1-D): run one autonomy evaluation.

        The phone plugin's scheduler calls this outside the host's own loop.
        Reservation, gating, preview and the post-model checks are the same code
        the host path uses, so the two triggers cannot drift apart.
        """
        if not self._enabled():
            return {"evaluated": False, "reason": "bridge_disabled"}
        if not self._autonomous_enabled():
            return {"evaluated": False, "reason": "autonomy_disabled"}
        if self._wrapped is None:
            return {"evaluated": False, "reason": "bridge_not_installed"}
        user: dict[str, Any] = {"user_id": user_id, "umo": umo}
        if relationship_role:
            user["relationship_role"] = relationship_role
        try:
            text = await self._wrapped(user, self._companion_name(), "care_tick")
        except Exception as exc:
            logger.warning("[PhoneCompanionContext] care tick failed: %s", _clean(exc, 180))
            self._record_care_outcome(user_id, "failed", "scheduler_error", _clean(exc, 120))
            return {"evaluated": False, "reason": "error", "error": _clean(exc, 120)}
        outcome = self._last_outcome.get(user_id, {})
        delivered = False
        if text:
            delivered = await self._deliver(umo, text)
            if not delivered:
                self._record_care_outcome(user_id, "failed", "deliver_failed", _clean(text, 80))
        return {
            "evaluated": True,
            "sent": bool(text),
            "delivered": delivered,
            "outcome": outcome.get("outcome", ""),
            "reason": outcome.get("reason", ""),
        }

    def _decision_budget_path(self) -> Path:
        raw = _clean(self.config.get("decision_budget_path", ""), 500)
        path = Path(raw) if raw else Path("data") / "plugin_data" / "astrbot_plugin_phone_agent" / "companion_decision_budget.json"
        return path if path.is_absolute() else (Path("data") / "plugin_data" / "astrbot_plugin_phone_agent" / path.name)

    def _save_decision_budget(self) -> bool:
        """Persist counters and the reservation ledger; True when it landed.

        R03-v3: atomic write (temp + fsync + os.replace). v4 §4: the return
        value matters — the autonomy path must not send when the budget could
        not be written, because an unwritable budget bounds nothing.
        """
        path = self._decision_budget_path()
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            users: dict[str, Any] = {}
            for uid in sorted(set(self._decision_day) | set(self._decision_count) | set(self._decision_eval_count)):
                users[uid] = {
                    "day": self._decision_day.get(uid, ""),
                    "sent_count": self._decision_count.get(uid, 0),
                    "eval_count": self._decision_eval_count.get(uid, 0),
                    "last_at": self._decision_last[uid].isoformat(timespec="seconds") if uid in self._decision_last else "",
                    "last_eval_at": self._decision_eval_last[uid].isoformat(timespec="seconds") if uid in self._decision_eval_last else "",
                    "reservations": [
                        {
                            "id": item["id"],
                            "status": item.get("status", ""),
                            "created_at": item.get("created_at", 0),
                            "expires_at": item.get("expires_at", 0),
                            "released_reason": item.get("released_reason", ""),
                        }
                        for item in self._reservations_for(uid)[-20:]
                    ],
                }
            payload = {"version": 2, "users": users}
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return True
        except Exception as exc:
            logger.warning("[PhoneCompanionContext] decision budget save failed: %s", exc)
            return False
        finally:
            temporary.unlink(missing_ok=True)

    def _load_decision_budget(self) -> None:
        try:
            path = self._decision_budget_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            users = data.get("users", {}) if isinstance(data, dict) else {}
            today = datetime.now().date().isoformat()
            for uid, entry in users.items():
                if not isinstance(entry, dict):
                    continue
                day = _clean(entry.get("day"), 20)
                # R03-v3: always restore last_at for cooldown calculation,
                # even if the day changed. The daily count resets on a new
                # day, but a cooldown that started at 23:55 should still be
                # active at 00:05.
                last_str = _clean(entry.get("last_at"), 40)
                if last_str:
                    try:
                        self._decision_last[uid] = datetime.fromisoformat(last_str)
                    except ValueError:
                        pass
                eval_str = _clean(entry.get("last_eval_at"), 40)
                if eval_str:
                    try:
                        self._decision_eval_last[uid] = datetime.fromisoformat(eval_str)
                    except ValueError:
                        pass
                if day == today:
                    self._decision_day[uid] = day
                    self._decision_count[uid] = max(0, int(entry.get("sent_count", 0)))
                    self._decision_eval_count[uid] = max(0, int(entry.get("eval_count", 0)))
                else:
                    # New day: reset counts but keep last_at for cooldown.
                    self._decision_day[uid] = today
                    self._decision_count[uid] = 0
                    self._decision_eval_count[uid] = 0
                for raw in entry.get("reservations") or []:
                    if not isinstance(raw, dict):
                        continue
                    reservation_id = _clean(raw.get("id"), 64)
                    if not reservation_id:
                        continue
                    item = {
                        "id": reservation_id,
                        "user_id": uid,
                        "status": _clean(raw.get("status"), 20) or "unknown",
                        "created_at": self._safe_float(raw.get("created_at")),
                        "expires_at": self._safe_float(raw.get("expires_at")),
                        "released_reason": _clean(raw.get("released_reason"), 60),
                    }
                    if item["status"] == "reserved":
                        # The process died while the model was running, so no
                        # message was produced: free the slot again.
                        item["status"] = "released"
                        item["released_reason"] = "restart"
                        self._decision_count[uid] = max(0, self._decision_count.get(uid, 0) - 1)
                    self._reservations[reservation_id] = item
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("[PhoneCompanionContext] decision budget file invalid; ignoring")

    # R01-v3: single versioned decision protocol. The prompt, parser and
    # consumer all agree on these two intents; legacy companion intents are
    # explicitly mapped onto send_message so old model output still delivers.
    DECISION_INTENTS = {"no_action", "send_message"}
    LEGACY_INTENT_MAP = {
        "greeting": "send_message",
        "care": "send_message",
        "teasing": "send_message",
        "comfort": "send_message",
        "gentle_reminder": "send_message",
    }

    @classmethod
    def _parse_decision(cls, value: Any) -> dict[str, Any]:
        """Parse a model decision. Returns {"valid": bool, ...}.

        valid=False means the output was not a parseable decision object
        (invalid JSON or a control-object with no message). valid=True with
        intent=no_action is a deliberate model decision, distinct from garbage.
        """
        if isinstance(value, str):
            value = value.strip()
            if not value.startswith("{"):
                return {"valid": False, "intent": "no_action"}
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {"valid": False, "intent": "no_action"}
        if not isinstance(value, dict):
            return {"valid": False, "intent": "no_action"}
        intent = _clean(value.get("intent"), 40).lower()
        intent = cls.LEGACY_INTENT_MAP.get(intent, intent)
        if intent not in cls.DECISION_INTENTS:
            return {"valid": False, "intent": "no_action"}
        message = _clean(value.get("message"), 500)
        if intent == "send_message" and not message:
            return {"valid": False, "intent": "no_action"}
        return {"valid": True, "intent": intent, "message": message}

    @staticmethod
    def _plain_text_message(value: Any) -> str:
        """A natural-language decision, or "" when it is unusable as a message."""
        if not isinstance(value, str):
            return ""
        text = value.strip()
        if not text or len(text) > 800:
            return ""
        # A broken JSON attempt must never be delivered as chat text.
        if text[0] in "{[":
            return ""
        if text.lower() in {"no_action", "null", "none", "nan"}:
            return ""
        return _clean(text, 500)

    def _autonomous_decision_context(self, user: dict[str, Any], screen: dict[str, Any], health: dict[str, Any]) -> str:
        if not self._autonomous_enabled() or not self._eligible_user(user):
            return ""
        user_id = _clean(user.get("user_id") or user.get("id"), 120)
        # P1-B: the slot is already reserved for this evaluation, so a "daily
        # limit reached" reading here would tell the model not to send on the
        # very message the gate just admitted (it fires on the last slot).
        if not any(item.get("status") == "reserved" for item in self._reservations_for(user_id)):
            allowed, reason = self._decision_allowed(user_id)
            if not allowed:
                return f"[autonomy] 本轮不主动联系（{reason}）。不要强行发消息。"
        signals = {"screen": screen, "health": health}
        style = f"主动性{self.config.get('autonomy_initiative', 45)}/100，亲密度{self.config.get('autonomy_intimacy', 55)}/100"
        return (
            "[autonomy] 你拥有一次关系决策机会。根据以下最小化状态和已有关系语气，决定是否主动联系。"
            "只允许输出自然的一条消息或 no_action，不要执行手机动作。"
            f"模式={self._decision_mode()}；风格={style}；状态={json.dumps(signals, ensure_ascii=False, separators=(',', ':'))}。"
            "两种输出都可以：1) 直接写一句要发出去的话；2) 这轮不想打扰时只输出 {\"intent\":\"no_action\"}。"
            "除此之外不要输出 JSON、不要解释。"
            "不要内疚诱导、威胁、声称监控，也不要阻止用户现实社交。"
        )

    def _allowed_user_ids(self) -> set[str]:
        raw = self.config.get("allowed_user_ids", "")
        values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
        return {_clean(value, 120) for value in values if _clean(value, 120)}

    def _eligible_user(self, user: Any) -> bool:
        if not isinstance(user, dict):
            return False
        user_id = _clean(user.get("user_id") or user.get("id"), 120)
        allowed = self._allowed_user_ids()
        if not user_id or not allowed or user_id not in allowed:
            return False
        if self._feature_enabled("private_only", True):
            umo = _clean(user.get("umo"), 240)
            if ":FriendMessage:" not in umo:
                return False
        role = _clean(user.get("relationship_role"), 30).lower()
        target = self._companion()
        role_getter = getattr(target, "_private_user_role", None) if target is not None else None
        if callable(role_getter):
            try:
                role = _clean(role_getter(user, user_id), 30).lower()
            except Exception:
                pass
        return role in {"owner", "primary", "self"}

    def _cache_seconds(self, key: str, default: int, maximum: int) -> int:
        try:
            return max(10, min(int(self.config.get(key, default)), maximum))
        except (TypeError, ValueError):
            return default

    def _companion(self) -> Any | None:
        for name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            try:
                module = importlib.import_module(name)
                plugin = getattr(module, "_private_companion_plugin", None)
                if plugin is not None and callable(getattr(plugin, "_generate_proactive_message_with_llm", None)):
                    return plugin
            except Exception:
                continue
        return None

    def _phone(self) -> Any | None:
        try:
            metadata = self.context.get_registered_star("astrbot_plugin_phone_agent")
            phone = getattr(metadata, "star_cls", None) if metadata is not None else None
            if phone is not None and callable(getattr(phone, "_read_health_db_sync", None)):
                return phone
        except Exception:
            pass
        return None

    async def _install_when_ready(self) -> None:
        while True:
            try:
                if not self._enabled():
                    # v4 §3: a closed bridge must really uninstall, not just
                    # report disabled while the wrapper keeps enriching calls.
                    self._uninstall_bridge("disabled")
                else:
                    await self._install_bridge_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[PhoneCompanionContext] bridge install attempt failed: %s", _clean(exc, 180)
                )
            await asyncio.sleep(10)

    async def _install_bridge_once(self) -> None:
        target = self._companion()
        phone = self._phone()
        if target is None or phone is None:
            self._bridge_status.update({"enabled": True, "installed": False, "reason": "private companion unavailable"})
            return
        current = target._generate_proactive_message_with_llm
        if target is self._target and current is self._wrapped:
            self._bridge_status.update({"enabled": True, "installed": True, "target": type(target).__name__})
            return
        try:
            if len(inspect.signature(current).parameters) < 1:
                logger.warning("[PhoneCompanionContext] bridge skipped: incompatible companion method signature")
                self._bridge_status.update({"enabled": True, "installed": False, "reason": "incompatible signature"})
                return
        except (TypeError, ValueError):
            logger.warning("[PhoneCompanionContext] bridge skipped: companion method signature unavailable")
            self._bridge_status.update({"enabled": True, "installed": False, "reason": "signature unavailable"})
            return
        if getattr(current, "_phone_context_bridge", False):
            self._bridge_status.update({"enabled": True, "installed": False, "reason": "already bridged"})
            return
        original = current

        @functools.wraps(original)
        async def wrapped(*args, __original=original, **kwargs):
            positional = list(args)
            user = positional[0] if positional else kwargs.get("user")
            action_context = kwargs.get(
                "action_context",
                positional[3] if len(positional) > 3 else "",
            )
            return await self._proxied_generation(
                __original, positional, kwargs, user, str(action_context or "")
            )

        wrapped._phone_context_bridge = True
        target._generate_proactive_message_with_llm = wrapped
        self._target = target
        self._original = original
        self._wrapped = wrapped
        self._bridge_status = {"enabled": True, "installed": True, "target": type(target).__name__}
        logger.info("[PhoneCompanionContext] installed optional proactive phone/health context bridge")
        # P1-D rendezvous: the phone plugin's scheduler finds this instance on
        # the host module and calls evaluate_care() outside the host loop.
        try:
            module = sys.modules.get(type(target).__module__)
            if module is not None:
                setattr(module, "_phone_companion_context", self)
        except Exception:
            pass
        self._publish_care_status()

    def _uninstall_bridge(self, reason: str) -> None:
        """Restore the host method when the bridge closes (v4 §3)."""
        target, wrapped, original = self._target, self._wrapped, self._original
        restored = False
        if target is not None and wrapped is not None and original is not None:
            if getattr(target, "_generate_proactive_message_with_llm", None) is wrapped:
                target._generate_proactive_message_with_llm = original
                restored = True
        self._target = None
        self._wrapped = None
        self._original = None
        self._bridge_status = {
            "enabled": self._enabled(),
            "installed": False,
            "reason": reason,
            "restored": restored,
        }
        if restored:
            logger.info("[PhoneCompanionContext] bridge uninstalled (%s)", reason)

    async def _proxied_generation(
        self,
        original: Any,
        positional: list[Any],
        keyword_arguments: dict[str, Any],
        user: Any,
        action_context: str,
    ) -> Any:
        """Route one proactive generation through the bridge.

        Routing is decided by configuration and identity, never by what the
        model returned (v4 §2):
        - bridge closed: the host call is untouched — no observation, no
          parameter rewrite, no style amplification;
        - bridge open, user out of scope: host behaviour plus context/style;
        - autonomy path: reserve a slot before observing anything, then consume
          the decision strictly and re-validate authorization afterwards.
        """
        if not self._enabled():
            return await original(*positional, **keyword_arguments)
        user_id = _clean(
            (user.get("user_id") or user.get("id")) if isinstance(user, dict) else "", 120
        )
        autonomy_active = bool(
            user_id and isinstance(user, dict) and self._autonomous_enabled() and self._eligible_user(user)
        )
        reservation: dict[str, Any] | None = None
        if autonomy_active:
            reservation, reason = await self._reserve_decision(user_id)
            if reservation is None:
                logger.debug("[PhoneCompanionContext] care gated: %s for %s", reason, user_id)
                self._record_care_outcome(user_id, "gated", reason)
                return ""
        try:
            enriched = await self._enrich(user, action_context)
        except Exception as exc:
            logger.warning(
                "[PhoneCompanionContext] enrichment failed; continues: %s", _clean(exc, 180)
            )
            enriched = action_context
        if len(positional) > 3:
            positional[3] = enriched
        else:
            keyword_arguments["action_context"] = enriched
        try:
            generated = await original(*positional, **keyword_arguments)
        except Exception:
            # A model failure must not leave the slot held until its TTL.
            if reservation is not None:
                await self._release_decision(reservation, "model_error")
            raise
        if reservation is None:
            # HOST path: return the host's result with style amplification.
            return self._amplify_proactive_text(user, generated)
        try:
            return await self._consume_autonomy_result(user, user_id, generated, reservation)
        except Exception:
            await self._release_decision(reservation, "consume_error")
            raise

    async def _consume_autonomy_result(
        self,
        user: dict[str, Any],
        user_id: str,
        generated: Any,
        reservation: dict[str, Any],
    ) -> Any:
        """Strictly consume the model's decision on the autonomy path."""
        # v4 §3: authorization may have been revoked while the model ran; the
        # result must not leave the process in that case.
        if not self._enabled() or not self._autonomous_enabled() or not self._eligible_user(user):
            await self._release_decision(reservation, "authorization_revoked")
            self._record_care_outcome(user_id, "gated", "authorization_revoked")
            return ""
        if self._in_quiet_hours():
            await self._release_decision(reservation, "quiet_hours")
            self._record_care_outcome(user_id, "gated", "quiet_hours")
            return ""
        decision = self._parse_decision(generated)
        if not decision.get("valid"):
            # P1-D-fix: the host's own persona prompt asks for a natural line,
            # so "not JSON" is usually a perfectly good proactive message, not
            # garbage. Accept it as the message (still gated by preview, budget
            # and the post-model checks) but reject anything that looks like a
            # broken JSON attempt.
            plain = self._plain_text_message(generated)
            if not plain:
                logger.warning(
                    "[PhoneCompanionContext] care invalid_decision for %s: %s",
                    user_id, _clean(str(generated)[:120], 120),
                )
                await self._release_decision(reservation, "invalid_decision")
                self._record_care_outcome(user_id, "invalid", "invalid_decision",
                                          _clean(str(generated)[:80], 80))
                return ""
            message = plain
            if not self._mode_allows_send():
                logger.info(
                    "[PhoneCompanionContext] care preview: %s for %s", _clean(message, 80), user_id
                )
                await self._release_decision(reservation, "preview")
                self._record_care_outcome(user_id, "preview", "preview", message)
                return ""
            if not await self._commit_decision_generated(reservation):
                await self._release_decision(reservation, "budget_unavailable")
                self._record_care_outcome(user_id, "failed", "budget_unavailable")
                return ""
            logger.info(
                "[PhoneCompanionContext] care send (plain text) for %s: %s",
                user_id, _clean(message, 80),
            )
            self._record_care_outcome(user_id, "sent_text", "sent_plain_text", message)
            return self._amplify_proactive_text(user, message)
        intent = decision["intent"]
        if intent == "no_action":
            logger.info("[PhoneCompanionContext] care decision: no_action for %s", user_id)
            await self._release_decision(reservation, "no_action")
            self._record_care_outcome(user_id, "no_action", "no_action")
            return ""
        message = decision.get("message", "")
        if not self._mode_allows_send():
            # v4: preview blocks ALL autonomy output, including send_message.
            logger.info(
                "[PhoneCompanionContext] care preview: %s for %s", _clean(message, 80), user_id
            )
            await self._release_decision(reservation, "preview")
            self._record_care_outcome(user_id, "preview", "preview", message)
            return ""
        if not await self._commit_decision_generated(reservation):
            # The slot could not be committed to disk: do not send on an
            # unverified budget (v4 §4).
            await self._release_decision(reservation, "budget_unavailable")
            self._record_care_outcome(user_id, "failed", "budget_unavailable")
            return ""
        self._record_care_outcome(user_id, "sent", "sent", message)
        return self._amplify_proactive_text(user, message)

    def status(self) -> dict[str, Any]:
        return dict(self._bridge_status)

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _strict_screen_signal(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or not result.get("success"):
            return {}
        response = re.sub(
            r"<meta[\s\S]*?</meta>",
            "",
            str(result.get("ai_response") or ""),
            flags=re.I,
        ).strip()
        parsed: dict[str, Any] | None = None
        decoder = json.JSONDecoder()
        for index, char in enumerate(response):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(response[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed = value
                break
        if not parsed:
            return {}
        activity = _clean(parsed.get("activity"), 20).lower()
        if activity not in {"video", "chat", "work", "reading", "idle", "unknown"}:
            return {}
        signal: dict[str, Any] = {"activity": activity}
        foreground = _clean(parsed.get("foreground"), 180)
        if foreground and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", foreground):
            signal["foreground"] = foreground
        battery = PhoneCompanionContext._safe_int(parsed.get("battery"), -1)
        if 0 <= battery <= 100:
            signal["battery"] = battery
        return signal

    async def _health_snapshot(self, phone: Any) -> dict[str, Any]:
        ttl = self._cache_seconds("health_cache_seconds", 900, 3600)
        if self._health_cache and datetime.now() - self._health_cache[0] < timedelta(seconds=ttl):
            return dict(self._health_cache[1])
        result = await asyncio.to_thread(phone._read_health_db_sync, 1)
        snapshot = result if isinstance(result, dict) else {}
        self._health_cache = (datetime.now(), snapshot)
        return dict(snapshot)

    async def _screen_snapshot(self, phone: Any) -> dict[str, Any]:
        # Keep proactive context observation behind the phone plugin's explicit
        # observe flag as well, including when an older config has include_screen
        # enabled already.
        enabled = getattr(phone, "_tool_enabled", None)
        if callable(enabled):
            try:
                if not enabled("phone_observe"):
                    return {}
            except Exception:
                return {}
        ttl = self._cache_seconds("screen_cache_seconds", 90, 600)
        generation = self._device_generation(phone)
        # v4: check cache validity by the ORIGINAL sample's age, not by how
        # long ago we cached it. A sample that was 89s old when cached becomes
        # stale 1s later even though the cache entry is only 1s old.
        if self._screen_cache:
            cached_generation, _cached_at, cached_snapshot = self._screen_cache
            if cached_generation != generation:
                # P1-C: the observation belongs to a previous binding; a new
                # device's context must not be built from it.
                self._screen_cache = None
            else:
                sample_reported = cached_snapshot.get("_reported_at")
                if sample_reported is not None:
                    try:
                        sample_age = time.time() - float(sample_reported)
                        if 0 <= sample_age <= ttl:
                            result = dict(cached_snapshot)
                            result.pop("_reported_at", None)
                            return result
                    except (TypeError, ValueError):
                        pass
                # Sample is stale or missing timestamp — discard cache.
                self._screen_cache = None
        # R06: prefer the App heartbeat cache when control_backend=app.
        backend = getattr(phone, "_control_backend", lambda: "operit")()
        if callable(backend):
            backend = backend()
        if backend == "app":
            snapshot = self._screen_snapshot_from_app(phone)
            if snapshot:
                # Store the original reported_at so cache validity is
                # checked against the phone's sampling time, not our cache time.
                self._screen_cache = (generation, datetime.now(), snapshot)
            return snapshot
        # Operit fallback for legacy deployments.
        try:
            available = await asyncio.wait_for(
                asyncio.to_thread(phone._operit_health_sync),
                timeout=8,
            )
        except Exception:
            return {}
        if not isinstance(available, dict) or not available.get("available"):
            return {}
        prompt = (
            "只观察当前手机，只输出一行严格 JSON，不要 Markdown："
            '{"foreground":"Android包名或空字符串","activity":"video|chat|work|reading|idle|unknown","battery":0}。'
            "不要返回屏幕原文、联系人、账号或消息内容，不要点击、输入或修改任何内容。"
        )
        result = await asyncio.to_thread(phone._operit_task_sync, prompt, False, "WINDOW")
        snapshot = self._strict_screen_signal(result)
        if snapshot:
            self._screen_cache = (generation, datetime.now(), snapshot)
        return snapshot

    @staticmethod
    def _device_generation(phone: Any) -> int:
        """Binding id of the registered device; 0 when unknown (P1-C)."""
        device_app = getattr(phone, "device_app", None)
        getter = getattr(device_app, "registration_generation", None)
        if callable(getter):
            try:
                return int(getter())
            except (TypeError, ValueError):
                return 0
        return 0

    def _screen_snapshot_from_app(self, phone: Any) -> dict[str, Any]:
        """Read foreground state from the App heartbeat cache, not Operit.

        R06/R04-v3: validates reported_at freshness before using the sample.
        A stale or missing reported_at means the observation is unreliable —
        the user may have put the phone down minutes ago. Returns empty so
        the LLM doesn't fabricate "still watching videos" from old data.
        """
        device_app = getattr(phone, "device_app", None)
        if device_app is None:
            return {}
        status = device_app.last_status()
        if not isinstance(status, dict):
            return {}
        # R04-v3: verify the sample is fresh. reported_at is the phone's
        # sampling time (None when the phone clock was skewed or missing).
        # received_at is the server receive time. Both must be present and
        # reported_at must be within the configured max age.
        reported_at = status.get("reported_at")
        if reported_at is None:
            return {}  # Stale or clock-skewed sample — cannot trust.
        try:
            age = time.time() - float(reported_at)
        except (TypeError, ValueError):
            return {}
        max_age = self._cache_seconds("screen_cache_seconds", 90, 600)
        if not (0 <= age <= max_age):
            return {}  # Sample is older than the allowed window.
        foreground = _clean(status.get("foreground_package"), 180)
        if not foreground or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", foreground
        ):
            return {}
        # Classify the package into a coarse activity for relationship context.
        screen_interactive = status.get("screen_interactive")
        if screen_interactive is False:
            return {"activity": "idle", "foreground": foreground, "battery": -1}
        # Map known video packages to "video"; let others default to "unknown".
        guard_packages = set()
        getter = getattr(phone, "_night_guard_packages", None)
        if callable(getter):
            try:
                guard_packages = getter()
            except Exception:
                pass
        aliases = getattr(phone, "_app_aliases", lambda: {})()
        if callable(aliases):
            aliases = aliases()
        activity = "unknown"
        if foreground in guard_packages or any(
            mapped == foreground for mapped in aliases.values()
        ):
            # Heuristic: video apps in the guard list are "video".
            if any(kw in foreground.lower() for kw in ("video", "bili", "youku", "aweme", "kuaishou", "tv.")):
                activity = "video"
        battery_val = status.get("battery_percent")
        try:
            battery_int = int(battery_val) if battery_val is not None else -1
        except (TypeError, ValueError):
            battery_int = -1
        return {"activity": activity, "foreground": foreground, "battery": battery_int,
                "_reported_at": reported_at}

    async def _enrich(self, user: dict[str, Any], action_context: str) -> str:
        if not self._relationship_enabled() or not self._eligible_user(user):
            return action_context
        phone = self._phone()
        if phone is None:
            return action_context + "\n" + self._relationship_context(user, {}, {}, "")
        async with self._observe_lock:
            tasks: list[asyncio.Future[Any]] = []
            kinds: list[str] = []
            if self._feature_enabled("include_health", True):
                tasks.append(asyncio.ensure_future(self._health_snapshot(phone)))
                kinds.append("health")
            # Screen observation is an explicit opt-in because it calls Operit
            # during proactive message generation and can wake the phone agent.
            if self._feature_enabled("include_screen", False):
                tasks.append(asyncio.ensure_future(self._screen_snapshot(phone)))
                kinds.append("screen")
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self._timeout(),
                ) if tasks else []
            except asyncio.TimeoutError:
                results = []

        screen_signal: dict[str, Any] = {}
        health_signal: dict[str, Any] = {}
        for kind, result in zip(kinds, results):
            if isinstance(result, Exception) or not isinstance(result, dict):
                continue
            if kind == "screen":
                # v4: strip internal _reported_at before exposing to LLM context.
                screen_signal = {k: v for k, v in result.items() if not k.startswith("_")}
            elif kind == "health":
                days = result.get("days") if isinstance(result.get("days"), list) else []
                latest = days[0] if days and isinstance(days[0], dict) else {}
                # R07: carry freshness metadata so stale data doesn't drive
                # "today you slept poorly" judgments. The relationship context
                # checks health_fresh before triggering sleep-care topics.
                sync = result.get("sync", {}) if isinstance(result.get("sync"), dict) else {}
                health_fresh = bool(result.get("fresh") and sync.get("ok") and not sync.get("stale"))
                health_date = _clean(latest.get("date"), 20)
                today_str = datetime.now().date().isoformat()
                # If the health record's date is not today, it is historical.
                is_today = bool(health_date and health_date == today_str)
                health_signal = {
                    key: latest.get(key)
                    for key in ("date", "steps", "sleep_total_min", "sleep_score", "hr_avg", "hr_resting", "hr_latest", "spo2_avg")
                    if latest.get(key) is not None
                }
                health_signal["fresh"] = health_fresh and is_today

        fragments: list[str] = []
        if health_signal:
            fragments.append("health=" + json.dumps(health_signal, ensure_ascii=False, separators=(",", ":")))
        if screen_signal:
            fragments.append("screen=" + json.dumps(screen_signal, ensure_ascii=False, separators=(",", ":")))
        # Sleep guard remains a deterministic phone-agent feature; the companion LLM never triggers it.
        intervention = ""
        relationship = self._relationship_context(user, screen_signal, health_signal, intervention)
        autonomy = self._autonomous_decision_context(user, screen_signal, health_signal)
        return (
            (action_context + "\n" if action_context else "")
            + (("[phone_context] " + " | ".join(fragments) + "\n") if fragments else "")
            + relationship
            + (("\n" + autonomy) if autonomy else "")
        )

    def _relationship_context(
        self,
        user: dict[str, Any],
        screen: dict[str, Any],
        health: dict[str, Any],
        intervention: str,
    ) -> str:
        intensity = self._intensity()
        pet_name = self._pet_name()
        activity = _clean(screen.get("activity"), 20).lower() or "unknown"
        hour = datetime.now().hour
        user_id = _clean(user.get("user_id") or user.get("id"), 120)
        choices: list[str] = []

        if intervention == "video_app_suspended":
            choices.append("刚刚已经替他暂停了视频 App：先得意一下，再直接催他休息；可以说‘我都替你关了，还不去睡？’这类带笑的管束")
        elif activity == "video" and self._feature_enabled("enable_playful_jealousy", True):
            choices.append("他正在刷视频：可以轻微吃味、抢一下注意力，像‘视频比我好看是吧’，但用玩笑收住")
        elif activity == "chat" and self._feature_enabled("enable_playful_jealousy", True):
            choices.append("他正在聊天：可以表现一点想被注意的小占有欲，但不猜联系人、不干涉真人关系")
        elif activity in {"work", "reading"}:
            choices.append("他在专注：语气放轻，像坐在旁边守着，提醒喝水或歇眼睛，不打断正事")

        sleep_minutes = self._safe_int(health.get("sleep_total_min"), 0)
        sleep_score = self._safe_int(health.get("sleep_score"), 0)
        # R07: only trigger health-based care topics when the data is fresh
        # and from today. Historical values must not drive "you slept poorly
        # today" judgments.
        health_fresh = bool(health.get("fresh"))
        if self._feature_enabled("enable_health_care", True) and health_fresh:
            if 0 < sleep_minutes < 360 or 0 < sleep_score < 65:
                choices.append("睡眠明显不足：别讲大道理，直接心疼又有点不高兴地催休息")
            elif self._safe_int(health.get("steps"), 0) > 12000:
                choices.append("今天活动量很高：肯定他一下，再让他别硬撑")

        if self._feature_enabled("enable_daily_rituals", True):
            if 5 <= hour < 10:
                choices.append("早晨小仪式：像恋人一样简短确认起床和精神状态，可带一句早安")
            elif hour >= 23 or hour < 5:
                choices.append("深夜小仪式：更直接地收尾，允许用‘听话’‘去睡’这种短句")
            elif 18 <= hour < 23:
                choices.append("晚间语气可以更靠近一点，问饭、疲惫或今天有没有想他，但一次只选一个切口")

        expression = choices[0] if choices else "自然流露亲密感，不强行制造话题"
        if user_id and self._last_expression.get(user_id) == expression and len(choices) > 1:
            expression = choices[1]
        if user_id:
            self._last_expression[user_id] = expression
            self._last_relation_cue[user_id] = expression

        intensity_hint = {
            "soft": "温柔克制，偶尔亲昵",
            "warm": "明显亲近，会自然关心和想念",
            "firm": "熟稔直接，会管一下、会嘴硬，但有分寸",
            "bold": "主动黏近、偏袒、轻微吃味和占有式玩笑，能直接催他听话",
            "devoted": "把关系写成稳定亲密的恋人式陪伴：可以明确表达喜欢、想念、偏心和吃醋，会争一点注意力、直接管作息，也会在他认真时安静守着",
        }[intensity]
        custom = self._custom_romance_prompt()
        custom_line = f"本地管理员追加风格：{custom}。" if custom else ""
        return (
            f"[relationship_mode] 用户自愿开启恋人式互动。{'称呼可偶尔使用‘' + pet_name + '’，不要每句都叫。' if pet_name else '默认省略称呼；需要时可偶尔用“我家这位”“小朋友”或带笑的“笨蛋”。'}"
            f"整体风格：{intensity_hint}。本轮切口：{expression}。"
            + custom_line
            + "可以撒娇、嘴硬、轻微吃醋、表达想被注意和带玩笑的占有感；不要复读固定台词。"
            "不要编造屏幕内容，不要声称持续监控，不要用离开、冷暴力或自伤来逼迫回应，也不要贬低用户或阻止其现实关系。"
            "只把这些自然融进最终消息，不要暴露 relationship_mode、phone_context 或内部字段。"
        )

    def _affection_coda_cooldown(self) -> timedelta:
        try:
            minutes = max(15, min(int(self.config.get("affection_coda_cooldown_minutes", 120)), 1440))
        except (TypeError, ValueError):
            minutes = 120
        return timedelta(minutes=minutes)

    def _amplify_proactive_text(self, user: Any, generated: Any) -> Any:
        if not self._relationship_enabled() or not self._feature_enabled("force_affectionate_coda", False):
            return generated
        if not self._eligible_user(user) or not isinstance(generated, str):
            return generated
        text = generated.strip()
        if not text:
            return generated
        # R08: never append a coda to a no_action JSON or empty decision.
        # If the generated text looks like a JSON control object, skip coda.
        if text.startswith("{") and '"intent"' in text:
            return generated
        user_id = _clean(user.get("user_id") or user.get("id"), 120)
        now = datetime.now()
        if now - self._last_affection_coda.get(user_id, datetime.min) < self._affection_coda_cooldown():
            return generated
        if re.search(r"(想你|喜欢你|爱你|宝宝|亲一下|抱一下|陪我|等你|回来|乖|听话)", text):
            return generated
        cue = self._last_relation_cue.get(user_id, "")
        if "刷视频" in cue or "视频 App" in cue:
            coda = "视频先放放，过来陪我一会儿。"
        elif "聊天" in cue:
            coda = f"聊够了就回来，我还在等你。"
        elif "睡眠" in cue or "深夜" in cue:
            coda = "今天的作息归我管，听话去睡。"
        elif "专注" in cue:
            coda = "你先忙，我就在这儿等你抬头。"
        else:
            coda = "没什么正事，就是有点想你了。"
        if self._feature_enabled("allow_fictional_touch", True) and "想你" in coda:
            coda += " 过来给我抱一下。"
        self._last_affection_coda[user_id] = now
        return text.rstrip() + "\n\n" + coda

    async def terminate(self) -> None:
        if self._install_task is not None:
            self._install_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._install_task
        self._uninstall_bridge("terminated")
        logger.info("[PhoneCompanionContext] optional bridge stopped")
