"""Durable task templates and explicitly approved, device-bound Bot schedules.

The library never dispatches a command. Due slots and model budgets are reserved
on disk before callers receive a claim, so restarts cannot replay a schedule.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import write_json_atomic
from .ui_recipes import validate_recipe
from .ui_results import validate_result_spec

ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
PARAM = re.compile(r"[a-z][a-z0-9_]{0,23}")
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]{0,23})\}\}")
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
SCOPE_FIELDS = frozenset({"goal", "package", "allowed_texts", "success_texts", "max_steps",
    "completion_mode", "result_spec", "allow_vision", "remember_recipe", "entry_point"})


def _text(value, limit, code="invalid_text", *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError(code)
    return value.strip()


def _integer(value, lower, upper, code):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(code)
    return value


def _stamp(value, code="invalid_schedule_expiry"):
    if type(value) not in (int, float):
        raise ValueError(code)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(code) from None
    if not math.isfinite(result):
        raise ValueError(code)
    return result


def validated_scope(scope, *, allow_placeholders=False):
    if not isinstance(scope, dict):
        raise ValueError("invalid_template_scope")
    goal = _text(scope.get("goal"), 500, "invalid_template_goal")
    package = _text(scope.get("package"), 160, "invalid_template_package")
    if not PACKAGE.fullmatch(package):
        raise ValueError("invalid_template_package")
    result = {"goal": goal, "package": package}
    for key in ("allowed_texts", "success_texts"):
        values = scope.get(key, [])
        if not isinstance(values, list) or len(values) > 5 or (key == "success_texts" and not values):
            raise ValueError("invalid_template_texts")
        result[key] = [_text(v, 200, "invalid_template_texts") for v in values]
    result["max_steps"] = _integer(scope.get("max_steps", 8), 1, 12, "invalid_template_steps")
    mode = scope.get("completion_mode", "visible_text")
    if not isinstance(mode, str) or mode not in {"visible_text", "search_results"}:
        raise ValueError("invalid_completion_mode")
    result["completion_mode"] = mode
    entry=scope.get("entry_point", "app_home")
    if entry not in ("app_home", "display_settings", "wifi_settings") or (entry != "app_home" and package != "com.android.settings"):
        raise ValueError("invalid_entry_point")
    if entry != "app_home": result["entry_point"] = entry
    for key in ("allow_vision", "remember_recipe"):
        value = scope.get(key, False)
        if type(value) is not bool:
            raise ValueError("invalid_template_scope")
        result[key] = value
    spec = scope.get("result_spec")
    if spec is not None:
        try: result["result_spec"] = validate_result_spec(spec)
        except ValueError: raise ValueError("invalid_result_spec") from None
    if not allow_placeholders and any("{{" in s or "}}" in s for s in [goal, *result["allowed_texts"], *result["success_texts"]]):
        raise ValueError("template_placeholders_reserved")
    return result


def _scope_strings(scope):
    return [scope["goal"], *scope["allowed_texts"], *scope["success_texts"]]


def _clock(value, *, empty=False):
    if empty and value == "":
        return ""
    if not isinstance(value, str) or not CLOCK.fullmatch(value):
        raise ValueError("invalid_schedule_time")
    return value


def _quiet(local_time, start, end):
    if not start or not end or start == end:
        return False
    return start <= local_time < end if start < end else local_time >= start or local_time < end


def result_fingerprint(result):
    """Fingerprint only verified user-requested results, never a raw screen."""
    if not isinstance(result, dict) or result.get("verified") is not True:
        return ""
    items = result.get("items")
    if not isinstance(items, list) or len(items) > 10:
        return ""
    public = []
    for item in items:
        if not isinstance(item, dict):
            return ""
        public.append({key: value for key, value in item.items()
                       if key in {"title", "author", "duration", "url"} and isinstance(value, str)})
    return hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class TaskLibrary:
    MAX_TEMPLATES = 50
    MAX_SCHEDULES = 30
    MAX_CLAIMS = 250
    MAX_FILE_BYTES = 4 * 1024 * 1024

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.invalid = False
        self.templates = {}
        self.schedules = {}
        self.claims = {}
        try:
            if self.path.stat().st_size > self.MAX_FILE_BYTES:
                raise ValueError("task_library_too_large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("invalid_library")
            for key, maximum in (("templates", self.MAX_TEMPLATES), ("schedules", self.MAX_SCHEDULES), ("claims", self.MAX_CLAIMS)):
                value = data.get(key, {})
                if not isinstance(value, dict) or len(value) > maximum:
                    raise ValueError("invalid_library")
                if any(not isinstance(k, str) or not ID.fullmatch(k) or not isinstance(v, dict) for k, v in value.items()):
                    raise ValueError("invalid_library")
                setattr(self, key, value)
            for template_id, row in self.templates.items():
                if row.get("template_id") != template_id:
                    raise ValueError("invalid_library")
                validated_scope(row.get("scope"), allow_placeholders=True)
                self._parameter_specs(row.get("parameters"))
                _text(row.get("subject"), 120)
                _text(row.get("device_id"), 64)
                _text(row.get("name"), 80)
                _text(row.get("requester_device_id", ""), 64, empty=True)
                declared = {v["name"] for v in row["parameters"]}
                if any(set(PLACEHOLDER.findall(value)) - declared for value in _scope_strings(row["scope"])):
                    raise ValueError("invalid_library")
                if row.get("recipe") is not None:
                    self._safe_recipe(row["recipe"])
            for schedule_id, row in self.schedules.items():
                if row.get("schedule_id") != schedule_id or row.get("template_id") not in self.templates:
                    raise ValueError("invalid_library")
                self._validate_loaded_schedule(row)
                if self.templates[row["template_id"]]["subject"] != row["subject"]:
                    raise ValueError("invalid_library")
                if row["scope"].get("device_id") != row["device_id"]:
                    raise ValueError("invalid_library")
            for claim_id, row in self.claims.items():
                if row.get("claim_id") != claim_id or row.get("status") not in {"reserved", "started", "done", "failed", "cancelled", "waiting_approval", "skipped", "interrupted"}:
                    raise ValueError("invalid_library")
                _stamp(row.get("reserved_at"), "invalid_library")
                for key in ("schedule_id", "device_id", "slot"):
                    _text(row.get(key), 128, "invalid_library")
                _text(row.get("run_id", ""), 128, "invalid_library", empty=True)
                _integer(row.get("revision"), 1, 2**63-1, "invalid_library")
                _integer(row.get("device_role_revision"), 1, 2**63-1, "invalid_library")
                _integer(row.get("model_limit"), 1, 16, "invalid_library")
            # In-flight reservations are never replayed after a restart. They
            # remain charged against both budgets, and their daily slot is used.
            for row in self.claims.values():
                if row.get("status") in {"reserved", "started"}:
                    row["status"] = "interrupted"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError, ZoneInfoNotFoundError):
            self.invalid = True
            self.templates, self.schedules, self.claims = {}, {}, {}

    @staticmethod
    def _parameter_specs(values):
        if not isinstance(values, list) or len(values) > 5:
            raise ValueError("invalid_template_parameters")
        names = set()
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("invalid_template_parameters")
            name = value.get("name")
            if not isinstance(name, str) or not PARAM.fullmatch(name) or name in names:
                raise ValueError("invalid_template_parameters")
            names.add(name)
            _text(value.get("default"), 200, "invalid_template_parameters")
            _integer(value.get("max_length", 200), 1, 200, "invalid_template_parameters")
        return names

    def _ensure(self):
        if self.invalid:
            raise ValueError("task_library_invalid")

    def _commit(self, *, templates=None, schedules=None, claims=None):
        self._ensure()
        templates = self.templates if templates is None else templates
        schedules = self.schedules if schedules is None else schedules
        claims = self.claims if claims is None else claims
        payload = {"version": 1, "templates": templates, "schedules": schedules, "claims": claims}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > self.MAX_FILE_BYTES:
            raise ValueError("task_library_too_large")
        write_json_atomic(self.path, payload)
        self.templates, self.schedules, self.claims = templates, schedules, claims

    def save_from_run(self, run, name, parameters=None, *, approved=False, now=None):
        stamp = time.time() if now is None else _stamp(now)
        summary = run.summary
        if summary.get("mode") != "ui" or (run.status != "done" and approved is not True):
            raise ValueError("approved_run_required")
        scope = validated_scope({**summary.get("ui_scope", {}), "goal": run.goal})
        name = _text(name, 80, "invalid_template_name")
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters, dict) or len(parameters) > 5:
            raise ValueError("invalid_template_parameters")
        substitutions = {}
        specs = []
        for key, literal in parameters.items():
            if not isinstance(key, str) or not PARAM.fullmatch(key):
                raise ValueError("invalid_template_parameters")
            literal = _text(literal, 200, "invalid_template_parameters")
            if literal in substitutions or not any(literal in value for value in _scope_strings(scope)):
                raise ValueError("invalid_template_parameters")
            substitutions[literal] = "{{" + key + "}}"
            specs.append({"name": key, "default": literal, "max_length": 200})
        if substitutions:
            pattern = re.compile("|".join(re.escape(s) for s in sorted(substitutions, key=len, reverse=True)))
            transform = lambda value: pattern.sub(lambda match: substitutions[match.group(0)], value)
            scope["goal"] = transform(scope["goal"])
            for key in ("allowed_texts", "success_texts"):
                scope[key] = [transform(value) for value in scope[key]]
        # Templates may contain placeholders which are longer than their source
        # literal; instantiated values are bounded again before proposal.
        validated_scope(scope, allow_placeholders=True)
        recipe = summary.get("ui_recipe") if run.status == "done" and scope.get("remember_recipe") else None
        if recipe is not None:
            recipe = self._safe_recipe(recipe)
            if recipe["package"] != scope["package"]: raise ValueError("invalid_recipe")
        row = {"template_id": "tpl-" + uuid.uuid4().hex[:20], "name": name,
               "subject": _text(run.subject, 120), "device_id": _text(run.device_id, 64),
               "source_run_id": run.run_id, "scope": scope, "parameters": specs,
               "requester_device_id": summary.get("requester_device_id", ""),
               "recipe": recipe, "created_at": stamp, "updated_at": stamp}
        with self.lock:
            self._ensure()
            if len(self.templates) >= self.MAX_TEMPLATES:
                raise ValueError("template_limit")
            templates = copy.deepcopy(self.templates)
            templates[row["template_id"]] = row
            self._commit(templates=templates)
        return self.template_public(row)

    @staticmethod
    def _safe_recipe(recipe):
        if not isinstance(recipe, dict) or recipe.get("version") != 1 or not isinstance(recipe.get("steps"), list) or len(recipe["steps"]) > 12:
            raise ValueError("invalid_recipe")
        if not isinstance(recipe.get("package"), str) or not PACKAGE.fullmatch(recipe["package"]):
            raise ValueError("invalid_recipe")
        encoded = json.dumps(recipe, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 16000:
            raise ValueError("invalid_recipe")
        # Engine validates selectors. Never let imported coordinate/shell/script
        # data masquerade as a learned task hint in the persistent library.
        def walk(value):
            if isinstance(value, dict):
                if any(k in {"x", "y", "bounds", "coordinates", "command", "shell", "snapshot_id", "node_id"} for k in value):
                    raise ValueError("invalid_recipe")
                for v in value.values(): walk(v)
            elif isinstance(value, list):
                for v in value: walk(v)
        walk(recipe)
        try: validate_recipe(recipe, recipe["package"])
        except ValueError: raise ValueError("invalid_recipe") from None
        return copy.deepcopy(recipe)

    @staticmethod
    def template_public(row):
        return {key: copy.deepcopy(value) for key, value in row.items() if key not in {"subject", "recipe"}} | {"has_recipe": bool(row.get("recipe"))}

    def template(self, template_id, subject):
        with self.lock:
            self._ensure()
            row = self.templates.get(template_id) if isinstance(template_id, str) else None
            if row is None or row.get("subject") != subject:
                raise ValueError("template_not_found")
            return copy.deepcopy(row)

    def templates_for(self, subject, device_id=""):
        with self.lock:
            return [self.template_public(v) for v in self.templates.values()
                    if v.get("subject") == subject and (not device_id or v.get("device_id") == device_id or v.get("requester_device_id") == device_id)]

    def instantiate(self, template_id, parameters, device_id, subject):
        row = self.template(template_id, subject)
        if not isinstance(device_id, str) or not ID.fullmatch(device_id) or len(device_id) > 64:
            raise ValueError("invalid_device_id")
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters, dict):
            raise ValueError("invalid_template_parameters")
        specs = row["parameters"]
        names = self._parameter_specs(specs)
        if set(parameters) - names:
            raise ValueError("unknown_template_parameter")
        values = {p["name"]: _text(parameters.get(p["name"], p["default"]), p["max_length"], "invalid_template_parameters") for p in specs}
        scope = copy.deepcopy(row["scope"])
        def replace(value):
            def matched(match):
                if match[1] not in values:
                    raise ValueError("invalid_template_parameters")
                return values[match[1]]
            return PLACEHOLDER.sub(matched, value)
        scope["goal"] = replace(scope["goal"])
        for key in ("allowed_texts", "success_texts"):
            scope[key] = [replace(v) for v in scope[key]]
        # Input substitutions are a single pass: braces inside a literal never
        # trigger a second expansion or change keys, packages or limits.
        validated_scope(scope, allow_placeholders=True)
        scope["device_id"] = device_id
        return scope, copy.deepcopy(row.get("recipe")), values

    def delete_template(self, template_id, subject):
        with self.lock:
            self.template(template_id, subject)
            templates = copy.deepcopy(self.templates)
            schedules = copy.deepcopy(self.schedules)
            del templates[template_id]
            schedules = {k: v for k, v in schedules.items() if v.get("template_id") != template_id}
            self._commit(templates=templates, schedules=schedules)

    @staticmethod
    def _validate_loaded_schedule(row):
        for key in ("subject", "device_id", "template_id"):
            _text(row.get(key), 128)
        _integer(row.get("revision"), 1, 2**63 - 1, "invalid_library")
        _integer(row.get("device_role_revision"), 1, 2**63 - 1, "invalid_library")
        for key in ("enabled", "notify", "notify_only_changed"):
            if type(row.get(key)) is not bool: raise ValueError("invalid_library")
        times = row.get("daily_times")
        if not isinstance(times, list) or not 1 <= len(times) <= 4: raise ValueError("invalid_library")
        for value in times: _clock(value)
        ZoneInfo(row["timezone"])
        _clock(row.get("quiet_start", ""), empty=True)
        _clock(row.get("quiet_end", ""), empty=True)
        _stamp(row.get("expires_at"))
        _stamp(row.get("approved_at"))
        _integer(row.get("max_runs"), 1, 1000, "invalid_library")
        _integer(row.get("max_runs_per_day"), 1, 4, "invalid_library")
        _integer(row.get("max_model_calls"), 1, 16000, "invalid_library")
        _integer(row.get("per_run_model_calls"), 1, 16, "invalid_library")
        _integer(row.get("used_runs", 0), 0, 1000, "invalid_library")
        _integer(row.get("model_calls_reserved", 0), 0, 16000, "invalid_library")
        _integer(row.get("actual_model_calls", 0), 0, 16000, "invalid_library")
        _text(row.get("private_session", ""), 240, empty=True)
        _text(row.get("requester_device_id", ""), 64, empty=True)
        slots = row.get("last_slots")
        counts = row.get("daily_counts")
        if not isinstance(slots, list) or len(slots) > 16 or any(not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", v) for v in slots):
            raise ValueError("invalid_library")
        if not isinstance(counts, dict) or len(counts) > 2 or any(not isinstance(k, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", k) or type(v) is not int or not 0 <= v <= 4 for k, v in counts.items()):
            raise ValueError("invalid_library")
        if row.get("notify") and ":FriendMessage:" not in row["private_session"]:
            raise ValueError("invalid_library")
        if row.get("recipe") is not None: TaskLibrary._safe_recipe(row["recipe"])
        validated_scope(row.get("scope"), allow_placeholders=True)

    def save_schedule(self, payload, subject, *, role, role_revision, private_session="", now=None):
        stamp = time.time() if now is None else _stamp(now)
        if not isinstance(payload, dict) or payload.get("approved") is not True:
            raise ValueError("schedule_approval_required")
        if role != "bot": raise ValueError("bot_device_required")
        role_revision = _integer(role_revision, 1, 2**63-1, "invalid_device_revision")
        scope, recipe, parameters = self.instantiate(payload.get("template_id"), payload.get("parameters", {}), payload.get("device_id"), subject)
        schedule_id = payload.get("schedule_id") or "sch-" + uuid.uuid4().hex[:20]
        if not isinstance(schedule_id, str) or not ID.fullmatch(schedule_id): raise ValueError("invalid_schedule_id")
        times = payload.get("daily_times", [payload.get("daily_time", "18:00")])
        if not isinstance(times, list) or not 1 <= len(times) <= 4:
            raise ValueError("invalid_schedule_time")
        times = sorted(set(_clock(value) for value in times))
        tz = _text(payload.get("timezone", "Asia/Shanghai"), 80, "invalid_schedule_timezone")
        try: ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError("invalid_schedule_timezone") from None
        expiry = _stamp(payload.get("expires_at"))
        if not stamp < expiry <= stamp + 90 * 86400: raise ValueError("invalid_schedule_expiry")
        quiet_start = _clock(payload.get("quiet_start", "23:00"), empty=True)
        quiet_end = _clock(payload.get("quiet_end", "07:00"), empty=True)
        if bool(quiet_start) != bool(quiet_end): raise ValueError("invalid_quiet_window")
        if any(_quiet(value, quiet_start, quiet_end) for value in times): raise ValueError("schedule_in_quiet_window")
        notify = payload.get("notify", False)
        notify_only_changed = payload.get("notify_only_changed", True)
        enabled = payload.get("enabled", True)
        if any(type(value) is not bool for value in (notify, notify_only_changed, enabled)):
            raise ValueError("invalid_schedule_switch")
        if notify and not private_session: raise ValueError("private_session_required")
        requester = _text(payload.get("requester_device_id", ""), 64, "invalid_device_id", empty=True)
        if requester and not ID.fullmatch(requester): raise ValueError("invalid_device_id")
        if notify and (not isinstance(private_session, str) or ":FriendMessage:" not in private_session or len(private_session) > 240):
            raise ValueError("private_session_required")
        maximum = _integer(payload.get("max_runs", 30), 1, 1000, "invalid_schedule_run_limit")
        daily_limit = _integer(payload.get("max_runs_per_day", 1), 1, 4, "invalid_schedule_run_limit")
        calls = _integer(payload.get("max_model_calls", 120), 1, 16000, "invalid_schedule_model_limit")
        per_run = _integer(payload.get("per_run_model_calls", 8), 1, 16, "invalid_schedule_model_limit")
        if calls < per_run: raise ValueError("invalid_schedule_model_limit")
        row = {"schedule_id": schedule_id, "template_id": payload["template_id"], "subject": subject,
               "device_id": scope["device_id"], "device_role_revision": role_revision,
               "scope": scope, "recipe": recipe, "parameters": parameters, "daily_times": times,
               "timezone": tz, "quiet_start": quiet_start, "quiet_end": quiet_end,
               "max_runs": maximum, "max_runs_per_day": daily_limit,
               "max_model_calls": calls, "per_run_model_calls": per_run,
               "expires_at": expiry, "approved_at": stamp, "enabled": enabled,
               "requester_device_id": requester,
               "notify": notify, "notify_only_changed": notify_only_changed,
               "private_session": private_session if notify else "", "used_runs": 0,
               "model_calls_reserved": 0, "actual_model_calls": 0,
               "last_slots": [], "daily_counts": {}, "last_status": "ready", "last_result_hash": ""}
        with self.lock:
            self._ensure()
            existing = self.schedules.get(schedule_id)
            if existing and existing.get("subject") != subject: raise ValueError("schedule_not_found")
            if not existing and len(self.schedules) >= self.MAX_SCHEDULES: raise ValueError("schedule_limit")
            # A renewed approval starts a new finite budget, but today's slots
            # stay used so editing at the trigger minute does not replay a job.
            row["revision"] = existing.get("revision", 0) + 1 if existing else 1
            if existing:
                row["last_slots"] = list(existing.get("last_slots", []))
                row["daily_counts"] = copy.deepcopy(existing.get("daily_counts", {}))
                row["last_result_hash"] = existing.get("last_result_hash", "")
            schedules = copy.deepcopy(self.schedules); schedules[schedule_id] = row
            self._commit(schedules=schedules)
        return self.schedule_public(row)

    @staticmethod
    def schedule_public(row):
        return {k: copy.deepcopy(v) for k, v in row.items() if k not in {
            "subject", "private_session", "recipe", "last_slots", "daily_counts", "last_result_hash"}} | {
            "remaining_runs": max(0, row["max_runs"] - row["used_runs"]),
            "remaining_model_calls": max(0, row["max_model_calls"] - row["model_calls_reserved"])}

    def schedules_for(self, subject, device_id=""):
        with self.lock:
            return [self.schedule_public(v) for v in self.schedules.values()
                    if v.get("subject") == subject and (not device_id or v.get("device_id") == device_id or v.get("requester_device_id") == device_id)]

    def disable_schedule(self, schedule_id, subject):
        with self.lock:
            self._ensure(); row = self.schedules.get(schedule_id)
            if row is None or row.get("subject") != subject: raise ValueError("schedule_not_found")
            schedules = copy.deepcopy(self.schedules)
            schedules[schedule_id].update(enabled=False, revision=row["revision"] + 1, last_status="disabled")
            self._commit(schedules=schedules)

    def delete_schedule(self, schedule_id, subject):
        with self.lock:
            self._ensure(); row = self.schedules.get(schedule_id)
            if row is None or row.get("subject") != subject: raise ValueError("schedule_not_found")
            schedules = copy.deepcopy(self.schedules); del schedules[schedule_id]
            self._commit(schedules=schedules)

    def due(self, *, now=None, available=None):
        """Reserve only this minute, once per local date/time (also during DST)."""
        stamp = time.time() if now is None else _stamp(now)
        available = available or (lambda row: False)
        with self.lock:
            self._ensure(); schedules = copy.deepcopy(self.schedules); claims = copy.deepcopy(self.claims)
            reserved = []
            busy_schedules = {v.get("schedule_id") for v in claims.values() if v.get("status") in {"reserved", "started"}}
            for row in schedules.values():
                if (not row["enabled"] or row["expires_at"] <= stamp or row["approved_at"] > stamp
                        or row["schedule_id"] in busy_schedules
                        or row["used_runs"] >= row["max_runs"]
                        or row["model_calls_reserved"] + row["per_run_model_calls"] > row["max_model_calls"]):
                    continue
                local = datetime.fromtimestamp(stamp, ZoneInfo(row["timezone"]))
                day, minute = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
                slot = day + "T" + minute
                if minute not in row["daily_times"] or _quiet(minute, row["quiet_start"], row["quiet_end"]): continue
                if slot in row["last_slots"] or row["daily_counts"].get(day, 0) >= row["max_runs_per_day"]: continue
                # Do not fire a newly approved schedule retrospectively within
                # the same minute; its first due slot must begin after consent.
                minute_start = stamp - local.second - local.microsecond / 1_000_000
                if minute_start < row["approved_at"]: continue
                if available(copy.deepcopy(row)) is not True: continue
                claim_id = "claim-" + uuid.uuid4().hex[:20]
                row["used_runs"] += 1
                row["model_calls_reserved"] += row["per_run_model_calls"]
                row["last_slots"] = (row["last_slots"] + [slot])[-16:]
                row["daily_counts"] = {k: v for k, v in row["daily_counts"].items() if k >= day}
                row["daily_counts"][day] = row["daily_counts"].get(day, 0) + 1
                row["last_status"] = "reserved"
                claim = {"claim_id": claim_id, "schedule_id": row["schedule_id"], "revision": row["revision"],
                         "device_id": row["device_id"], "device_role_revision": row["device_role_revision"],
                         "reserved_at": stamp, "slot": slot, "status": "reserved", "run_id": "",
                         "model_limit": row["per_run_model_calls"]}
                claims[claim_id] = claim
                reserved.append({"claim": copy.deepcopy(claim), "schedule": copy.deepcopy(row)})
            if not reserved: return []
            if len(claims) > self.MAX_CLAIMS:
                ordered = sorted(claims, key=lambda key: claims[key]["reserved_at"])
                for key in ordered:
                    if len(claims) <= self.MAX_CLAIMS: break
                    if claims[key]["status"] not in {"reserved", "started"}: del claims[key]
            if len(claims) > self.MAX_CLAIMS: raise ValueError("schedule_claim_limit")
            self._commit(schedules=schedules, claims=claims)
            return reserved

    def claim_allowed(self, claim_id, revision, device_id, *, role, role_revision, now=None):
        stamp = time.time() if now is None else _stamp(now)
        with self.lock:
            if self.invalid: return "task_library_invalid"
            claim = self.claims.get(claim_id)
            row = self.schedules.get(claim.get("schedule_id")) if claim else None
            if not row or not claim or row["revision"] != revision or claim["revision"] != revision:
                return "schedule_revoked"
            if not row["enabled"] or row["expires_at"] <= stamp: return "schedule_revoked"
            if claim["status"] not in {"reserved", "started"}: return "schedule_claim_consumed"
            if role != "bot" or row["device_id"] != device_id or row["device_role_revision"] != role_revision:
                return "device_changed"
            return True

    def bind_claim(self, claim_id, run_id):
        with self.lock:
            self._ensure(); row = self.claims.get(claim_id)
            if not row or row["status"] != "reserved" or row.get("run_id"):
                raise ValueError("schedule_claim_consumed")
            claims = copy.deepcopy(self.claims); claims[claim_id].update(status="started", run_id=run_id)
            self._commit(claims=claims)

    def finish_claim(self, claim_id, *, status, model_calls=0, result=None, now=None):
        stamp = time.time() if now is None else _stamp(now)
        with self.lock:
            self._ensure(); claim = self.claims.get(claim_id)
            if not claim or claim["status"] not in {"reserved", "started"}: return None
            if status not in {"done", "failed", "cancelled", "waiting_approval", "skipped", "interrupted"}: status = "failed"
            model_calls = _integer(model_calls, 0, 16, "invalid_model_count")
            claims = copy.deepcopy(self.claims); claims[claim_id].update(status=status, finished_at=stamp, model_calls=model_calls)
            schedules = copy.deepcopy(self.schedules); row = schedules.get(claim["schedule_id"])
            notify = None
            if row and row["revision"] == claim["revision"]:
                row["last_status"] = status
                row["actual_model_calls"] += model_calls
                fingerprint = result_fingerprint(result) if status == "done" else ""
                changed = bool(fingerprint and fingerprint != row["last_result_hash"])
                if fingerprint: row["last_result_hash"] = fingerprint
                if (row["enabled"] and row["expires_at"] > stamp and row["notify"] and row["private_session"]
                        and status == "done" and (changed or not row["notify_only_changed"])):
                    # Persist notification reservation first. A send failure is
                    # not automatically retried, preventing duplicate messages.
                    claims[claim_id]["notification_reserved"] = True
                    notify = {"session": row["private_session"], "subject": row["subject"],
                              "schedule_id": row["schedule_id"], "run_id": claim["run_id"],
                              "result_changed": changed}
            self._commit(schedules=schedules, claims=claims)
            return notify


def resume_scope(run, *, current_device_id, current_role, manual_state_confirmed=False):
    """Create a fresh proposal specification; no old action or grant is copied."""
    if (run.summary.get("mode") != "ui" or run.status not in {"failed", "waiting_approval"}
            or run.cancel_requested or not run.summary.get("ui_started")):
        raise ValueError("task_not_resumable")
    previous = run.summary.get("ui_scope", {})
    if current_device_id != run.device_id or current_role != previous.get("device_role"):
        raise ValueError("device_changed")
    uncertain = any(step.status in {"running", "unknown", "executed_unverified"}
        or (step.status == "failed" and step.error in {"ui_input_restore_failed", "ui_input_failed",
            "timeout", "interrupted", "connection_failed", "invalid_result", "internal"}) for step in run.steps)
    if uncertain and manual_state_confirmed is not True:
        raise ValueError("manual_state_confirmation_required")
    scope = validated_scope({**previous, "goal": run.goal}, allow_placeholders=True)
    spent = sum(step.status != "skipped" for step in run.steps)
    remaining = scope["max_steps"] - spent
    if remaining < 1: raise ValueError("resume_budget_exhausted")
    scope.update(device_id=run.device_id, max_steps=remaining)
    metadata = {"resume_from": run.run_id, "resume_root": run.summary.get("resume_root", run.run_id),
                "resume_prior_actions": sum(step.status == "done" for step in run.steps),
                "resume_completed_actions": [step.action for step in run.steps if step.status == "done"][-12:],
                "resume_manual_state_confirmed": manual_state_confirmed is True,
                "resume_open_target": False}
    return scope, metadata
