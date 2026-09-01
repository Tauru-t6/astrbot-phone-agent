from __future__ import annotations

import asyncio
import functools
import importlib
import json
import re
from contextlib import suppress
from datetime import datetime, timedelta
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
        self._observe_lock = asyncio.Lock()
        self._screen_cache: tuple[datetime, dict[str, Any]] | None = None
        self._health_cache: tuple[datetime, dict[str, Any]] | None = None

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
        return value if value in {"preview", "low_risk_auto"} else "preview"

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

    def _decision_allowed(self, user_id: str, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now()
        if self._in_quiet_hours(now):
            return False, "quiet_hours"
        day = now.date().isoformat()
        if self._decision_day.get(user_id) != day:
            self._decision_day[user_id] = day
            self._decision_count[user_id] = 0
        if now - self._decision_last.get(user_id, datetime.min) < self._decision_cooldown():
            return False, "cooldown"
        if self._decision_count.get(user_id, 0) >= self._decision_daily_limit():
            return False, "daily_limit"
        return True, "allowed"

    @staticmethod
    def _parse_decision(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"intent": "no_action"}
        intent = _clean(value.get("intent"), 40).lower()
        allowed = {"no_action", "greeting", "care", "teasing", "comfort", "gentle_reminder", "ask_permission"}
        if intent not in allowed:
            return {"intent": "no_action"}
        message = _clean(value.get("message"), 500)
        return {"intent": intent, "message": message} if message else {"intent": "no_action"}

    def _autonomous_decision_context(self, user: dict[str, Any], screen: dict[str, Any], health: dict[str, Any]) -> str:
        if not self._autonomous_enabled() or not self._eligible_user(user):
            return ""
        user_id = _clean(user.get("user_id") or user.get("id"), 120)
        allowed, reason = self._decision_allowed(user_id)
        if not allowed:
            return f"[autonomy] 本轮不主动联系（{reason}）。不要强行发消息。"
        signals = {"screen": screen, "health": health}
        style = f"主动性{self.config.get('autonomy_initiative', 45)}/100，亲密度{self.config.get('autonomy_intimacy', 55)}/100"
        return (
            "[autonomy] 你拥有一次关系决策机会。根据以下最小化状态和已有关系语气，决定是否主动联系。"
            "只允许输出自然的一条消息或 no_action，不要执行手机动作。"
            f"模式={self._decision_mode()}；风格={style}；状态={json.dumps(signals, ensure_ascii=False, separators=(',', ':'))}。"
            "输出严格 JSON：{\"intent\":\"no_action|greeting|care|teasing|comfort|gentle_reminder|ask_permission\",\"message\":\"...\"}。"
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
            if not self._enabled():
                return
            target = self._companion()
            phone = self._phone()
            if target is not None and phone is not None:
                current = target._generate_proactive_message_with_llm
                if target is not self._target or current is not self._wrapped:
                    if getattr(current, "_phone_context_bridge", False):
                        await asyncio.sleep(10)
                        continue
                    original = current

                    @functools.wraps(original)
                    async def wrapped(*args, __original=original, **kwargs):
                        positional = list(args)
                        user = positional[0] if positional else kwargs.get("user")
                        action_context = kwargs.get(
                            "action_context",
                            positional[3] if len(positional) > 3 else "",
                        )
                        try:
                            enriched = await self._enrich(user, str(action_context or ""))
                        except Exception as exc:
                            logger.warning(
                                "[PhoneCompanionContext] context enrichment failed; proactive message continues: %s",
                                _clean(exc, 180),
                            )
                            enriched = str(action_context or "")
                        if len(positional) > 3:
                            positional[3] = enriched
                        else:
                            kwargs["action_context"] = enriched
                        generated = await __original(*positional, **kwargs)
                        return self._amplify_proactive_text(user, generated)

                    wrapped._phone_context_bridge = True
                    target._generate_proactive_message_with_llm = wrapped
                    self._target = target
                    self._original = original
                    self._wrapped = wrapped
                    logger.info("[PhoneCompanionContext] installed optional proactive phone/health context bridge")
            await asyncio.sleep(10)

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
        ttl = self._cache_seconds("screen_cache_seconds", 90, 600)
        if self._screen_cache and datetime.now() - self._screen_cache[0] < timedelta(seconds=ttl):
            return dict(self._screen_cache[1])
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
            self._screen_cache = (datetime.now(), snapshot)
        return snapshot

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
            if self._feature_enabled("include_screen", True):
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
                screen_signal = result
            elif kind == "health":
                days = result.get("days") if isinstance(result.get("days"), list) else []
                latest = days[0] if days and isinstance(days[0], dict) else {}
                health_signal = {
                    key: latest.get(key)
                    for key in ("date", "steps", "sleep_total_min", "sleep_score", "hr_avg", "hr_resting", "hr_latest", "spo2_avg")
                    if latest.get(key) is not None
                }

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
        if self._feature_enabled("enable_health_care", True):
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
                choices.append("晚间语气可以更靠近一点，问饭、疲惫或今天有没有想她，但一次只选一个切口")

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
        if (
            self._target is not None
            and self._original is not None
            and self._wrapped is not None
            and getattr(self._target, "_generate_proactive_message_with_llm", None) is self._wrapped
        ):
            self._target._generate_proactive_message_with_llm = self._original
        logger.info("[PhoneCompanionContext] optional bridge stopped")
