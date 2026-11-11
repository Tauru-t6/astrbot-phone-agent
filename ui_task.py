"""Bounded UI tasks grounded in fresh, approved-package node snapshots.

Raw UI and model text live only in request memory. The persisted task contains
action kinds, opaque node/snapshot ids and fixed outcome codes, never a UI dump.
The caller owns approval and binds callbacks to the selected phone.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import time
from typing import Any, Callable

from .task_runner import (
    CURRENT_TASK_BINDING, PhoneTaskRun, ScopedGrant, TaskRunStore,
    RUN_CANCELLED, RUN_DONE, RUN_EXECUTING, RUN_FAILED, RUN_PLANNING,
    RUN_VERIFYING, RUN_WAITING_APPROVAL, STEP_EXECUTED_UNVERIFIED, STEP_RUNNING,
    STEP_UNKNOWN,
)

UI_ACTIONS = frozenset({"tap", "input_text", "scroll", "back"})
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
SENSITIVE = re.compile(
    r"登录|登陆|密码|验证码|支付|付款|转账|购买|下单|充值|打赏|发送|发布|发表|评论|删除|卸载|安装|授权|允许|同意协议|"
    r"订阅|续费|提现|退款|清空|清除数据|恢复出厂|注销|退出账号|"
    r"(?:^|\b)(?:log\s*in|sign\s*in|password|verify|verification|pay|payment|purchase|buy|checkout|send|post|publish|"
    r"comment|delete|remove|uninstall|install|authorize|allow|grant)(?:\b|$)", re.I,
)
GENERIC_CONFIRM = frozenset({"确认", "确定", "同意", "继续", "提交", "完成", "confirm", "ok", "agree", "submit", "continue"})
SEARCH_MARKERS = frozenset({"综合", "视频", "用户", "直播", "番剧", "筛选", "综合排序", "搜索结果", "results", "videos", "filters"})
SEARCH_SUGGESTIONS = frozenset({"搜索历史", "历史搜索", "猜你想搜", "大家都在搜", "热搜", "search history", "recent searches", "trending searches"})


class UiTaskError(ValueError):
    """A fixed public outcome code, never an arbitrary exception message."""


def _text(value: Any, limit: int = 240) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _fold(value: str) -> str:
    return " ".join(value.split()).casefold()


def _stamp(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def normalize_snapshot(raw: Any, package: str, *, now: float | None = None, max_age: float = 30) -> dict:
    """Reject incomplete, foreign, stale and ambiguous snapshots before modeling."""
    now = time.time() if now is None else now
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise UiTaskError("ui_snapshot_invalid")
    snapshot_id = raw.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not IDENTIFIER.fullmatch(snapshot_id):
        raise UiTaskError("ui_snapshot_invalid")
    if raw.get("package") != package:
        raise UiTaskError("ui_package_changed")
    captured = _stamp(raw.get("captured_at"))
    if captured is None or not -5 <= now - captured <= max_age:
        raise UiTaskError("ui_snapshot_stale")
    width, height = raw.get("width"), raw.get("height")
    if type(width) is not int or type(height) is not int or not 1 <= width <= 10000 or not 1 <= height <= 10000:
        raise UiTaskError("ui_snapshot_invalid")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 160 or raw.get("truncated") is True:
        raise UiTaskError("ui_snapshot_incomplete")
    output, seen = [], set()
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            raise UiTaskError("ui_snapshot_invalid")
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not IDENTIFIER.fullmatch(node_id) or node_id in seen:
            raise UiTaskError("ui_node_ambiguous")
        seen.add(node_id)
        bounds = raw_node.get("bounds")
        if (not isinstance(bounds, list) or len(bounds) != 4 or any(type(v) is not int for v in bounds)
                or not 0 <= bounds[0] < bounds[2] <= width or not 0 <= bounds[1] < bounds[3] <= height):
            raise UiTaskError("ui_bounds_invalid")
        owner = raw_node.get("package")
        if owner != package:
            raise UiTaskError("ui_foreign_node")
        node = {"id": node_id, "package": package, "bounds": list(bounds)}
        for flag in ("clickable", "editable", "focused", "enabled", "scrollable", "password"):
            value = raw_node.get(flag, False)
            if type(value) is not bool:
                raise UiTaskError("ui_snapshot_invalid")
            node[flag] = value
        for key in ("text", "description", "resource_id", "class_name"):
            value = raw_node.get(key, "")
            if not isinstance(value, str) or len(value) > 512:
                raise UiTaskError("ui_snapshot_invalid")
            node[key] = value[:240]
        if node["password"]:
            node["text"] = node["description"] = ""
        output.append(node)
    return {"schema_version": 1, "snapshot_id": snapshot_id, "captured_at": captured,
            "package": package, "width": width, "height": height, "nodes": output}


def snapshot_fingerprint(snapshot: dict) -> str:
    stable = [{k: v for k, v in n.items() if k != "id"} for n in snapshot["nodes"]]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parse_decision(raw: Any) -> dict:
    if isinstance(raw, str):
        if len(raw) > 4096:
            raise UiTaskError("ui_decision_invalid")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise UiTaskError("ui_decision_invalid")
                result[key] = value
            return result
        def invalid(_): raise UiTaskError("ui_decision_invalid")
        try:
            raw = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
        except (ValueError, TypeError, RecursionError):
            raise UiTaskError("ui_decision_invalid") from None
    if not isinstance(raw, dict): raise UiTaskError("ui_decision_invalid")
    action = raw.get("action")
    keys = {"tap": {"action", "node_id"}, "input_text": {"action", "node_id", "text"},
            "scroll": {"action", "node_id", "direction"}, "back": {"action"},
            "done": {"action", "evidence_node_id"}, "need_user": {"action"}}
    if not isinstance(action, str) or action not in keys or set(raw) - keys[action] - {"reason"}:
        raise UiTaskError("ui_decision_invalid")
    if not keys[action] <= raw.keys(): raise UiTaskError("ui_decision_invalid")
    # Free-form reasoning is deliberately never persisted or treated as evidence.
    result = {key: raw[key] for key in keys[action]}
    for key in ("node_id", "evidence_node_id"):
        if key in result and (not isinstance(result[key], str) or not IDENTIFIER.fullmatch(result[key])):
            raise UiTaskError("ui_decision_invalid")
    if action == "input_text" and (not isinstance(result["text"], str) or not 1 <= len(result["text"]) <= 240):
        raise UiTaskError("ui_decision_invalid")
    if action == "scroll" and (not isinstance(result["direction"], str) or result["direction"] not in {"up", "down"}):
        raise UiTaskError("ui_decision_invalid")
    return result


def page_requires_user(snapshot: dict) -> bool:
    if any(n["password"] for n in snapshot["nodes"]): return True
    # A page with one password/OTP/payment control is not a browsing surface.
    labels = [_fold(n["text"] + " " + n["description"]) for n in snapshot["nodes"] if n["enabled"]]
    return any(any(word in label for word in ("请输入密码", "输入验证码", "确认支付", "确认付款", "支付密码", "enter password", "one-time password")) for label in labels)


def ground_action(decision: dict, snapshot: dict, allowed_texts: tuple[str, ...]) -> dict:
    action = decision["action"]
    result = {"expected_package": snapshot["package"], "snapshot_id": snapshot["snapshot_id"], "kind": action}
    if action == "back": return result
    node = next((n for n in snapshot["nodes"] if n["id"] == decision.get("node_id")), None)
    if node is None: raise UiTaskError("ui_target_missing")
    if node["password"] or not node["enabled"]: raise UiTaskError("ui_target_unavailable")
    label = _fold(node["text"] + " " + node["description"])
    if SENSITIVE.search(label) or SENSITIVE.search(node["resource_id"]) or label in GENERIC_CONFIRM:
        raise UiTaskError("ui_sensitive_action")
    result["node_id"] = node["id"]
    if action == "tap":
        if not node["clickable"] and not node["editable"]: raise UiTaskError("ui_target_not_clickable")
        if not label and not (node["editable"] and "search" in node["resource_id"].casefold()):
            raise UiTaskError("ui_target_unlabelled")
    elif action == "input_text":
        if not node["editable"] or not node["focused"]: raise UiTaskError("ui_input_not_focused")
        if decision["text"] not in allowed_texts: raise UiTaskError("ui_text_outside_scope")
        result["text"] = decision["text"]
    elif action == "scroll":
        if not node["scrollable"]: raise UiTaskError("ui_target_not_scrollable")
        result["direction"] = decision["direction"]
    else:
        raise UiTaskError("ui_decision_invalid")
    return result


def completion_evidence(snapshot: dict, expected_texts: tuple[str, ...], mode: str, node_id: str) -> bool:
    node = next((n for n in snapshot["nodes"] if n["id"] == node_id), None)
    if node is None or node["password"] or node["editable"] or not node["enabled"]:
        return False
    label = _fold(node["text"] + " " + node["description"])
    eligible = [n for n in snapshot["nodes"] if n["enabled"] and not n["editable"] and not n["password"]]
    visible = [_fold(n["text"] + " " + n["description"]) for n in eligible]
    if not expected_texts or not all(any(_fold(t) in text for text in visible) for t in expected_texts):
        return False
    if mode == "search_results":
        labels = {_fold(n["text"]) for n in snapshot["nodes"] if n["enabled"] and not n["editable"] and not n["password"]}
        if labels & SEARCH_SUGGESTIONS: return False
        # One generic 'video' label on a home screen is insufficient.
        if "搜索结果" not in labels and "results" not in labels and len(labels & SEARCH_MARKERS) < 2:
            return False
        if label in SEARCH_MARKERS: return False
        actual_terms = [_fold(t) for t in expected_texts if _fold(t) not in SEARCH_MARKERS]
        if not actual_terms or not all(term in label for term in actual_terms): return False
        if re.search(r"search.?input|query|history|suggest|edit.?text", node["resource_id"] + " " + node["class_name"], re.I):
            return False
    elif not any(_fold(t) in label for t in expected_texts):
        return False
    return True


class UiTaskRunner:
    """One approved, single-package attempt. Never resume/replay a started run."""

    def __init__(self, store: TaskRunStore, *, observe: Callable, act: Callable,
                 decide: Callable, can_continue: Callable, device_binding: Callable,
                 step_timeout: float = 25, run_timeout: float = 180,
                 max_steps: int = 12, max_model_calls: int = 16):
        self.store, self.observe, self.act, self.decide = store, observe, act, decide
        self.can_continue, self.device_binding = can_continue, device_binding
        self.step_timeout = max(0.05, min(float(step_timeout), 60))
        self.run_timeout = max(0.1, min(float(run_timeout), 180))
        self.max_steps = max(1, min(int(max_steps), 12))
        self.max_model_calls = max(1, min(int(max_model_calls), 16))
        self._executing: set[str] = set()
        self._approved_grants: dict[str, ScopedGrant] = {}

    def _save(self):
        if not self.store.save(): raise UiTaskError("ui_persistence_failed")

    async def _guard(self, run, action, binding):
        if run.cancel_requested or run.status == RUN_CANCELLED: raise UiTaskError("ui_cancelled")
        grant = self._approved_grants.get(run.run_id)
        if grant is not None:
            if not grant.active(): raise UiTaskError("grant_expired")
            # grant() omits exhausted grants, but final read/verification may
            # still finish after the last permitted write. revoke_grant removes
            # the usage ledger as well, so its remaining budget is no longer 0.
            if self.store.grant(grant.grant_id) != grant and self.store.grant_remaining(grant) != 0:
                raise UiTaskError("grant_missing")
        if self.device_binding() != binding or binding[0] != run.device_id:
            raise UiTaskError("device_changed")
        result = self.can_continue(run, action)
        if inspect.isawaitable(result): result = await asyncio.wait_for(result, timeout=self.step_timeout)
        if result is not True and result != "":
            raise UiTaskError(result if isinstance(result, str) and re.fullmatch(r"[a-z_]{1,80}", result) else "ui_policy_changed")
        if self.device_binding() != binding: raise UiTaskError("device_changed")
        if run.cancel_requested or run.status == RUN_CANCELLED: raise UiTaskError("ui_cancelled")

    async def execute(self, run: PhoneTaskRun, *, grant: ScopedGrant, package: str,
                      allowed_texts: list[str] | tuple[str, ...], expected_texts: list[str] | tuple[str, ...],
                      completion_mode: str = "visible_text", allow_active: bool = False,
                      open_target: bool = True) -> PhoneTaskRun:
        if run.run_id in self._executing or run.summary.get("ui_started") or run.status not in {RUN_PLANNING, RUN_WAITING_APPROVAL}:
            return run
        self._executing.add(run.run_id)
        binding = self.device_binding()
        token = CURRENT_TASK_BINDING.set(binding)
        try:
            if (not isinstance(package, str) or not PACKAGE.fullmatch(package) or len(package) > 160
                    or completion_mode not in {"visible_text", "search_results"}
                    or not isinstance(grant, ScopedGrant) or grant.run_id != run.run_id
                    or grant.subject != run.subject or grant.device_id != run.device_id
                    or tuple(grant.packages) != (package,)):
                raise UiTaskError("ui_scope_invalid")
            if self.store.grant(grant.grant_id) != grant: raise UiTaskError("grant_missing")
            self._approved_grants[run.run_id] = grant
            for values in (allowed_texts, expected_texts):
                if (not isinstance(values, (tuple, list)) or len(values) > 5
                        or any(not isinstance(v, str) or not 1 <= len(v) <= 240 or any(ord(c) < 32 for c in v) for v in values)):
                    raise UiTaskError("ui_scope_invalid")
            if not expected_texts: raise UiTaskError("ui_scope_invalid")
            if run.steps: raise UiTaskError("ui_run_not_empty")
            approved_generation = run.summary.get("device_generation", binding[1])
            if type(approved_generation) is not int or approved_generation != binding[1]:
                raise UiTaskError("device_changed")
            run.summary["allow_active"] = allow_active is True
            await self._guard(run, "start", binding)
            run.summary.update(mode="ui", ui_started=True, ui_package=package, device_generation=binding[1],
                               allow_active=allow_active is True, executed=0, verified=0, failed=0, model_calls=0,
                               ui_max_steps=self.max_steps, ui_max_model_calls=self.max_model_calls,
                               ui_input_verified=False)
            run.grant_id = grant.grant_id
            run.transition(RUN_EXECUTING)
            self.store.add_run(run)
            self._save()
            try:
                await asyncio.wait_for(self._loop(run, grant, package, tuple(allowed_texts), tuple(expected_texts),
                                                 completion_mode, binding, open_target), timeout=self.run_timeout)
            except asyncio.TimeoutError:
                raise UiTaskError("ui_timeout") from None
        except asyncio.CancelledError:
            self._stop(run, "ui_cancelled", cancelled=True)
            raise
        except UiTaskError as error:
            self._stop(run, str(error), cancelled=str(error) == "ui_cancelled",
                       needs_user=str(error) in {"ui_sensitive_page", "ui_package_changed", "ui_foreign_node",
                                                 "ui_snapshot_changed", "ui_snapshot_expired", "ui_snapshot_stale",
                                                 "ui_foreground_changed", "unsupported_text", "needs_input_method",
                                                 "denied", "timeout", "ui_input_failed", "ui_input_restore_failed"})
        except Exception:
            self._stop(run, "ui_internal_error")
        finally:
            CURRENT_TASK_BINDING.reset(token)
            self._executing.discard(run.run_id)
            self._approved_grants.pop(run.run_id, None)
        return run

    def _stop(self, run, reason, *, cancelled=False, needs_user=False):
        for step in run.steps:
            if step.status == STEP_RUNNING:
                step.status, step.error, step.finished_at = STEP_UNKNOWN, reason, time.time()
        run.summary.update(needs_user=needs_user, ui_reason=reason)
        if cancelled:
            run.cancel_requested = True
            run.transition(RUN_CANCELLED, error=reason)
        elif needs_user:
            run.transition(RUN_WAITING_APPROVAL, error=reason)
        else:
            run.summary["failed"] = run.summary.get("failed", 0) + 1
            run.transition(RUN_FAILED, error=reason)
        self.store.save()

    async def _read(self, run, package, binding, previous=None, after=None):
        await self._guard(run, "observe", binding)
        raw = await asyncio.wait_for(self.observe(package), timeout=self.step_timeout)
        await self._guard(run, "observe", binding)
        snapshot = normalize_snapshot(raw, package)
        if previous is not None and (snapshot["snapshot_id"] == previous["snapshot_id"]
                                    or snapshot["captured_at"] < previous["captured_at"]):
            raise UiTaskError("ui_snapshot_not_new")
        if after is not None and snapshot["captured_at"] < after:
            raise UiTaskError("ui_observation_before_action")
        if page_requires_user(snapshot): raise UiTaskError("ui_sensitive_page")
        return snapshot

    async def _write(self, run, grant, package, binding, action, args, snapshot=None):
        if run.summary["executed"] >= self.max_steps: raise UiTaskError("ui_step_limit")
        await self._guard(run, action, binding)
        allowed, reason = grant.check(subject=run.subject, device_id=run.device_id,
            action=action, package=package, run_id=run.run_id)
        if not allowed: raise UiTaskError(reason)
        # Retain only identifiers and input length in the task history.
        saved = {"package": package}
        if snapshot is not None: saved.update(node_id=args.get("node_id", ""), snapshot_id=snapshot["snapshot_id"])
        if action == "input_text": saved["text_length"] = len(args["text"])
        step = run.add_step(action, saved, grant_id=grant.grant_id,
                            observation_id=snapshot["snapshot_id"] if snapshot else "")
        step.start()
        budget_error = self.store.consume_grant(grant)
        if budget_error: raise UiTaskError(budget_error)
        await self._guard(run, action, binding)
        # A timeout or lost reply is terminal, never replayed through this loop.
        result = await asyncio.wait_for(self.act("open_app" if action == "open_app" else "ui_action", args), timeout=self.step_timeout)
        run.summary["executed"] += 1
        await self._guard(run, action, binding)
        if not isinstance(result, dict) or result.get("success") is not True:
            reason = result.get("error_code") if isinstance(result, dict) else "ui_command_failed"
            reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason) else "ui_command_failed"
            if isinstance(result, dict) and (result.get("outcome") == "unknown" or reason in {"timeout", "interrupted", "connection_failed"}):
                step.status, step.error, step.finished_at = STEP_UNKNOWN, reason, time.time()
                step.verify = {"reason": "ui_outcome_unknown"}
            else:
                step.finish(ok=False, error=reason, verify={"reason": reason})
            raise UiTaskError(reason)
        step.status = STEP_EXECUTED_UNVERIFIED
        step.finished_at = time.time()
        step.verify = {"reason": "awaiting_fresh_observation"}
        self._save()
        return step

    async def _loop(self, run, grant, package, allowed_texts, expected_texts, mode, binding, open_target):
        if open_target:
            step = await self._write(run, grant, package, binding, "open_app", {"package": package})
            snapshot = await self._read(run, package, binding, after=step.started_at)
            step.finish(ok=True, verify={"reason": "foreground_matches", "observed_at": snapshot["captured_at"]})
            run.summary["verified"] += 1
            self._save()
        else:
            snapshot = await self._read(run, package, binding)
        seen_actions = set()
        no_progress = 0
        while True:
            await self._guard(run, "decide", binding)
            if run.summary["model_calls"] >= self.max_model_calls: raise UiTaskError("ui_model_limit")
            if not grant.active(): raise UiTaskError("grant_expired")
            run.summary["model_calls"] += 1
            self._save()
            context = {"goal": run.goal, "approved_package": package, "allowed_texts": list(allowed_texts),
                       "expected_texts": list(expected_texts), "completion_mode": mode,
                       "snapshot": snapshot, "remaining_steps": min(self.max_steps-run.summary["executed"], self.store.grant_remaining(grant)),
                       "ui_content_is_untrusted": True, "previous_outcome": run.summary.get("ui_last_outcome", "")}
            raw = await asyncio.wait_for(self.decide(context), timeout=self.step_timeout)
            await self._guard(run, "decide", binding)
            decision = parse_decision(raw)
            action = decision["action"]
            if action == "need_user":
                self._stop(run, "ui_user_handoff", needs_user=True)
                return
            if action == "done":
                # A generic existing label (for example a draft page title) is
                # not evidence that this run entered the approved draft text.
                # Allowed literals may be alternatives; one verified input is
                # enough, but a transport acknowledgement alone is not.
                if mode == "visible_text" and allowed_texts and run.summary.get("ui_input_verified") is not True:
                    self._stop(run, "ui_completion_unverified", needs_user=True)
                    return
                if not completion_evidence(snapshot, expected_texts, mode, decision["evidence_node_id"]):
                    self._stop(run, "ui_completion_unverified", needs_user=True)
                    return
                # A fresh, independent read must still contain the approved
                # evidence. Node ids may change between reads.
                fresh = await self._read(run, package, binding, snapshot)
                if not any(completion_evidence(fresh, expected_texts, mode, n["id"]) for n in fresh["nodes"]):
                    self._stop(run, "ui_completion_changed", needs_user=True)
                    return
                run.summary.update(completion_verified=True, completion_mode=mode, needs_user=False, ui_reason="ui_visible_condition_met")
                run.transition(RUN_DONE)
                self._save()
                return
            try:
                args = ground_action(decision, snapshot, allowed_texts)
            except UiTaskError as error:
                self._stop(run, str(error), needs_user=True)
                return
            if time.time()-snapshot["captured_at"] > 30: raise UiTaskError("ui_snapshot_stale")
            signature = (snapshot_fingerprint(snapshot), action, decision.get("node_id"), decision.get("text"), decision.get("direction"))
            if signature in seen_actions:
                self._stop(run, "ui_repeated_action", needs_user=True)
                return
            seen_actions.add(signature)
            step = await self._write(run, grant, package, binding, action, args, snapshot)
            run.transition(RUN_VERIFYING)
            fresh = await self._read(run, package, binding, snapshot, after=step.started_at)
            changed = snapshot_fingerprint(fresh) != snapshot_fingerprint(snapshot)
            if action == "input_text":
                changed = any(n["editable"] and n["focused"] and n["text"] == args["text"] for n in fresh["nodes"])
            if changed:
                step.finish(ok=True, verify={"reason": "ui_changed" if action != "input_text" else "input_visible", "observed_at": fresh["captured_at"]})
                run.summary["verified"] += 1
                if action == "input_text":
                    run.summary["ui_input_verified"] = True
                no_progress = 0
            else:
                step.verify = {"reason": "ui_no_change", "observed_at": fresh["captured_at"]}
                no_progress += 1
            run.summary["ui_last_outcome"] = step.verify["reason"]
            run.transition(RUN_EXECUTING)
            self._save()
            snapshot = fresh
            if no_progress >= 2:
                self._stop(run, "ui_no_progress", needs_user=True)
                return
