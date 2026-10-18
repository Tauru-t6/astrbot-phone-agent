"""Autonomous phone-task primitives (P1-E stage 1).

This module is deliberately inert: it defines the objects a multi-step task
needs, the rules a scoped grant must satisfy, and the persistence for runs. It
does not execute anything and is not wired into the plugin yet — stage 2 adds
the read-only runner on top of these pieces.

Design notes
------------
* A ``ScopedGrant`` is bound to a subject, a device, a set of actions, an
  optional package allowlist and a validity window. Every write step must name
  a grant that still covers it; anything outside the grant is refused with a
  specific reason so the caller can report *why* instead of failing silently.
* An ``ObservationRef`` is evidence: a step may only run against an observation
  that names the same device and carries the phone's own sampling time, so a
  stale or foreign sample can never justify an action.
* A ``PhoneTaskRun`` records one goal attempt as a list of ``RunStep``s with
  every id needed to reconstruct the chain (run -> step -> grant -> observation
  -> command).
"""
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------- constants

GRANT_SOURCE_MANUAL = "manual"
GRANT_SOURCE_APPROVAL = "approval"
GRANT_SOURCES = {GRANT_SOURCE_MANUAL, GRANT_SOURCE_APPROVAL}

# Actions bound to one package: a grant for these must name the package.
PACKAGE_ACTIONS = {"open_app", "close_app", "suspend_app", "unsuspend_app"}

# Actions that change the phone. Read-only observation is not gated by a grant.
WRITE_ACTIONS = PACKAGE_ACTIONS | {
    "tap",
    "long_press",
    "input_text",
    "key_event",
    "scroll",
    "swipe",
    "home",
    "back",
    "lock_screen",
    "wake_screen",
    "screenshot",
}

RUN_PLANNING = "planning"
RUN_RUNNING = "running"
RUN_WAITING_APPROVAL = "waiting_approval"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = {RUN_PLANNING, RUN_RUNNING, RUN_WAITING_APPROVAL, RUN_DONE, RUN_FAILED, RUN_CANCELLED}
RUN_ACTIVE = {RUN_PLANNING, RUN_RUNNING, RUN_WAITING_APPROVAL}

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_EXECUTED_UNVERIFIED = "executed_unverified"
STEP_UNKNOWN = "unknown"
STEP_STATUSES = {
    STEP_PENDING, STEP_RUNNING, STEP_DONE, STEP_FAILED, STEP_SKIPPED,
    STEP_EXECUTED_UNVERIFIED, STEP_UNKNOWN,
}

RUN_EXECUTING = "executing"
RUN_VERIFYING = "verifying"
RUN_STATUSES = RUN_STATUSES | {RUN_EXECUTING, RUN_VERIFYING}
RUN_ACTIVE = RUN_ACTIVE | {RUN_EXECUTING, RUN_VERIFYING}


def _now() -> float:
    return time.time()


def _short(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---------------------------------------------------------------- grant


@dataclass(frozen=True)
class ScopedGrant:
    """One bounded permission: who, on which device, which actions, until when."""

    grant_id: str
    subject: str
    device_id: str
    actions: tuple[str, ...]
    packages: tuple[str, ...]
    issued_at: float
    expires_at: float
    max_steps: int = 1
    source: str = GRANT_SOURCE_MANUAL

    @classmethod
    def issue(
        cls,
        *,
        subject: str,
        device_id: str,
        actions: list[str] | tuple[str, ...],
        packages: list[str] | tuple[str, ...] = (),
        ttl_seconds: float = 300.0,
        max_steps: int = 1,
        source: str = GRANT_SOURCE_MANUAL,
        now: float | None = None,
    ) -> "ScopedGrant":
        stamp = _now() if now is None else now
        cleaned_actions = tuple(sorted({_short(item, 40) for item in actions if _short(item, 40)}))
        cleaned_packages = tuple(sorted({_short(item, 160) for item in packages if _short(item, 160)}))
        return cls(
            grant_id=uuid.uuid4().hex,
            subject=_short(subject, 120),
            device_id=_short(device_id, 64),
            actions=cleaned_actions,
            packages=cleaned_packages,
            issued_at=stamp,
            expires_at=stamp + max(1.0, float(ttl_seconds)),
            max_steps=max(1, int(max_steps)),
            source=source if source in GRANT_SOURCES else GRANT_SOURCE_MANUAL,
        )

    def active(self, now: float | None = None) -> bool:
        return (_now() if now is None else now) < self.expires_at

    def check(
        self,
        *,
        subject: str,
        device_id: str,
        action: str,
        package: str = "",
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). The first failing rule wins."""
        if (_now() if now is None else now) >= self.expires_at:
            return False, "grant_expired"
        if _short(subject, 120) != self.subject:
            return False, "grant_subject_mismatch"
        if _short(device_id, 64) != self.device_id:
            return False, "grant_device_mismatch"
        action = _short(action, 40)
        if action not in self.actions:
            return False, "grant_action_not_allowed"
        if action in PACKAGE_ACTIONS and not package:
            return False, "grant_package_required"
        if package and self.packages and _short(package, 160) not in self.packages:
            return False, "grant_package_not_allowed"
        return True, "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "subject": self.subject,
            "device_id": self.device_id,
            "actions": list(self.actions),
            "packages": list(self.packages),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_steps": self.max_steps,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScopedGrant":
        return cls(
            grant_id=_short(raw.get("grant_id"), 64) or uuid.uuid4().hex,
            subject=_short(raw.get("subject"), 120),
            device_id=_short(raw.get("device_id"), 64),
            actions=tuple(_short(item, 40) for item in raw.get("actions") or []),
            packages=tuple(_short(item, 160) for item in raw.get("packages") or []),
            issued_at=float(raw.get("issued_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            max_steps=max(1, int(raw.get("max_steps") or 1)),
            source=_short(raw.get("source"), 20) or GRANT_SOURCE_MANUAL,
        )


# ---------------------------------------------------------------- observation


@dataclass(frozen=True)
class ObservationRef:
    """A dated, device-bound snapshot that justifies the next step."""

    observation_id: str
    device_id: str
    sample_id: str
    foreground: str
    reported_at: float | None
    received_at: float
    screen_interactive: bool | None = None
    ui_nodes: tuple[str, ...] = ()
    created_at: float = field(default_factory=_now)

    @classmethod
    def from_status(
        cls,
        status: dict[str, Any],
        *,
        device_id: str,
        ui_nodes: list[str] | tuple[str, ...] = (),
        now: float | None = None,
    ) -> "ObservationRef":
        reported = status.get("reported_at")
        try:
            reported_at = float(reported) if reported is not None else None
        except (TypeError, ValueError):
            reported_at = None
        screen = status.get("screen_interactive")
        return cls(
            observation_id=uuid.uuid4().hex,
            device_id=_short(device_id, 64),
            sample_id=_short(status.get("sample_id"), 64),
            foreground=_short(status.get("foreground_package"), 160),
            reported_at=reported_at,
            received_at=float(status.get("received_at") or (_now() if now is None else now)),
            screen_interactive=screen if isinstance(screen, bool) else None,
            ui_nodes=tuple(_short(node, 120) for node in ui_nodes if _short(node, 120)),
            created_at=_now() if now is None else now,
        )

    def age(self, now: float | None = None) -> float | None:
        """Seconds since the phone sampled it, or None when the clock is unusable."""
        if self.reported_at is None:
            return None
        return (_now() if now is None else now) - self.reported_at

    def fresh(self, max_age: float, now: float | None = None) -> bool:
        age = self.age(now)
        return age is not None and 0 <= age <= max_age

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "device_id": self.device_id,
            "sample_id": self.sample_id,
            "foreground": self.foreground,
            "reported_at": self.reported_at,
            "received_at": self.received_at,
            "screen_interactive": self.screen_interactive,
            "ui_nodes": list(self.ui_nodes),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObservationRef":
        screen = raw.get("screen_interactive")
        return cls(
            observation_id=_short(raw.get("observation_id"), 64) or uuid.uuid4().hex,
            device_id=_short(raw.get("device_id"), 64),
            sample_id=_short(raw.get("sample_id"), 64),
            foreground=_short(raw.get("foreground"), 160),
            reported_at=float(raw["reported_at"]) if raw.get("reported_at") is not None else None,
            received_at=float(raw.get("received_at") or 0.0),
            screen_interactive=screen if isinstance(screen, bool) else None,
            ui_nodes=tuple(_short(node, 120) for node in raw.get("ui_nodes") or []),
            created_at=float(raw.get("created_at") or 0.0),
        )


# ---------------------------------------------------------------- run / steps


@dataclass
class RunStep:
    step_id: str
    run_id: str
    index: int
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = STEP_PENDING
    grant_id: str = ""
    observation_id: str = ""
    command_id: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    verify: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def new(cls, run_id: str, index: int, action: str, args: dict[str, Any] | None = None, **extra: Any) -> "RunStep":
        return cls(
            step_id=uuid.uuid4().hex,
            run_id=run_id,
            index=index,
            action=_short(action, 40),
            args=dict(args or {}),
            **extra,
        )

    def start(self, *, command_id: str = "", now: float | None = None) -> None:
        self.status = STEP_RUNNING
        self.command_id = _short(command_id, 64)
        self.started_at = _now() if now is None else now

    def finish(self, *, ok: bool, verify: dict[str, Any] | None = None, error: str = "",
               now: float | None = None) -> None:
        self.status = STEP_DONE if ok else STEP_FAILED
        self.verify = dict(verify or {})
        self.error = "" if ok else _short(error, 200)
        self.finished_at = _now() if now is None else now

    def skip(self, reason: str = "", now: float | None = None) -> None:
        self.status = STEP_SKIPPED
        self.error = _short(reason, 200)
        self.finished_at = _now() if now is None else now

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "index": self.index,
            "action": self.action,
            "args": dict(self.args),
            "status": self.status,
            "grant_id": self.grant_id,
            "observation_id": self.observation_id,
            "command_id": self.command_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "verify": dict(self.verify),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunStep":
        return cls(
            step_id=_short(raw.get("step_id"), 64) or uuid.uuid4().hex,
            run_id=_short(raw.get("run_id"), 64),
            index=int(raw.get("index") or 0),
            action=_short(raw.get("action"), 40),
            args=dict(raw.get("args") or {}),
            status=_short(raw.get("status"), 20) if raw.get("status") in STEP_STATUSES else STEP_PENDING,
            grant_id=_short(raw.get("grant_id"), 64),
            observation_id=_short(raw.get("observation_id"), 64),
            command_id=_short(raw.get("command_id"), 64),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            verify=dict(raw.get("verify") or {}),
            error=_short(raw.get("error"), 200),
        )


@dataclass
class PhoneTaskRun:
    run_id: str
    goal: str
    subject: str
    device_id: str
    status: str = RUN_PLANNING
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    steps: list[RunStep] = field(default_factory=list)
    grant_id: str = ""
    last_error: str = ""
    cancel_requested: bool = False
    summary: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, goal: str, *, subject: str, device_id: str, grant_id: str = "",
            now: float | None = None) -> "PhoneTaskRun":
        stamp = _now() if now is None else now
        return cls(
            run_id=uuid.uuid4().hex,
            goal=_short(goal, 500),
            subject=_short(subject, 120),
            device_id=_short(device_id, 64),
            grant_id=_short(grant_id, 64),
            created_at=stamp,
            updated_at=stamp,
        )

    def add_step(self, action: str, args: dict[str, Any] | None = None, **extra: Any) -> RunStep:
        step = RunStep.new(self.run_id, len(self.steps), action, args, **extra)
        self.steps.append(step)
        self.touch()
        return step

    def touch(self, now: float | None = None) -> None:
        self.updated_at = _now() if now is None else now

    def next_pending(self) -> RunStep | None:
        for step in self.steps:
            if step.status == STEP_PENDING:
                return step
        return None

    def has_failed_step(self) -> bool:
        return any(step.status == STEP_FAILED for step in self.steps)

    def transition(self, status: str, *, error: str = "", now: float | None = None) -> bool:
        """Move to a new status; terminal states are final."""
        if status not in RUN_STATUSES:
            return False
        if self.status in {RUN_DONE, RUN_FAILED, RUN_CANCELLED}:
            return False
        self.status = status
        if error:
            self.last_error = _short(error, 200)
        self.touch(now)
        return True

    def cancel(self, reason: str = "cancelled", now: float | None = None) -> None:
        self.cancel_requested = True
        for step in self.steps:
            if step.status in {STEP_PENDING, STEP_RUNNING}:
                step.skip(reason, now=now)
        self.status = RUN_CANCELLED
        self.last_error = _short(reason, 200)
        self.touch(now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "subject": self.subject,
            "device_id": self.device_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "grant_id": self.grant_id,
            "last_error": self.last_error,
            "cancel_requested": self.cancel_requested,
            "summary": dict(self.summary),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PhoneTaskRun":
        return cls(
            run_id=_short(raw.get("run_id"), 64) or uuid.uuid4().hex,
            goal=_short(raw.get("goal"), 500),
            subject=_short(raw.get("subject"), 120),
            device_id=_short(raw.get("device_id"), 64),
            status=_short(raw.get("status"), 20) if raw.get("status") in RUN_STATUSES else RUN_FAILED,
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            steps=[RunStep.from_dict(item) for item in raw.get("steps") or [] if isinstance(item, dict)],
            grant_id=_short(raw.get("grant_id"), 64),
            last_error=_short(raw.get("last_error"), 200),
            cancel_requested=bool(raw.get("cancel_requested")),
            summary=dict(raw.get("summary") or {}),
        )


# ---------------------------------------------------------------- store


APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"
APPROVAL_STATUSES = {APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_EXPIRED}


@dataclass
class ApprovalRequest:
    """Stage 4: a proposed run that waits for a human decision.

    The grant is only issued after approval, so a proposal alone can never touch
    the phone. Expiry is checked on read; nothing sweeps in the background.
    """

    approval_id: str
    run_id: str
    subject: str
    device_id: str
    actions: tuple[str, ...]
    packages: tuple[str, ...]
    created_at: float
    expires_at: float
    status: str = APPROVAL_PENDING
    decided_at: float = 0.0
    reason: str = ""

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        subject: str,
        device_id: str,
        actions: list[str] | tuple[str, ...],
        packages: list[str] | tuple[str, ...] = (),
        ttl_seconds: float = 60.0,
        now: float | None = None,
    ) -> "ApprovalRequest":
        stamp = _now() if now is None else now
        return cls(
            approval_id=uuid.uuid4().hex,
            run_id=_short(run_id, 64),
            subject=_short(subject, 120),
            device_id=_short(device_id, 64),
            actions=tuple(sorted({_short(item, 40) for item in actions if _short(item, 40)})),
            packages=tuple(sorted({_short(item, 160) for item in packages if _short(item, 160)})),
            created_at=stamp,
            expires_at=stamp + max(5.0, float(ttl_seconds)),
        )

    def expire_if_due(self, now: float | None = None) -> bool:
        stamp = _now() if now is None else now
        if self.status == APPROVAL_PENDING and stamp >= self.expires_at:
            self.status = APPROVAL_EXPIRED
            self.decided_at = stamp
            return True
        return False

    def pending(self, now: float | None = None) -> bool:
        stamp = _now() if now is None else now
        self.expire_if_due(stamp)
        return self.status == APPROVAL_PENDING

    def decide(self, approved: bool, *, reason: str = "", now: float | None = None) -> bool:
        if not self.pending(now):
            return False
        self.status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
        self.decided_at = _now() if now is None else now
        self.reason = _short(reason, 160)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "subject": self.subject,
            "device_id": self.device_id,
            "actions": list(self.actions),
            "packages": list(self.packages),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "decided_at": self.decided_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ApprovalRequest":
        status = _short(raw.get("status"), 20)
        return cls(
            approval_id=_short(raw.get("approval_id"), 64),
            run_id=_short(raw.get("run_id"), 64),
            subject=_short(raw.get("subject"), 120),
            device_id=_short(raw.get("device_id"), 64),
            actions=tuple(_short(item, 40) for item in (raw.get("actions") or [])),
            packages=tuple(_short(item, 160) for item in (raw.get("packages") or [])),
            created_at=float(raw.get("created_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            status=status if status in APPROVAL_STATUSES else APPROVAL_PENDING,
            decided_at=float(raw.get("decided_at") or 0.0),
            reason=_short(raw.get("reason"), 160),
        )


class TaskRunStore:
    """Bounded, atomic run history shared by the runner and the web API."""

    def __init__(self, path: str | Path, *, max_runs: int = 50) -> None:
        self._path = Path(path)
        self._max_runs = max(1, int(max_runs))
        self._runs: dict[str, PhoneTaskRun] = {}
        self._grants: dict[str, ScopedGrant] = {}
        self._approvals: dict[str, ApprovalRequest] = {}

    def load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for raw in data.get("runs") or []:
            if isinstance(raw, dict):
                run = PhoneTaskRun.from_dict(raw)
                # A run interrupted by a restart would otherwise sit in
                # "executing" forever and never report a result.
                if run.status in {RUN_EXECUTING, RUN_VERIFYING, RUN_RUNNING}:
                    run.transition(RUN_FAILED, error="interrupted")
                self._runs[run.run_id] = run
        for raw in data.get("grants") or []:
            if isinstance(raw, dict):
                grant = ScopedGrant.from_dict(raw)
                if grant.active():
                    self._grants[grant.grant_id] = grant
        for raw in data.get("approvals") or []:
            if isinstance(raw, dict):
                approval = ApprovalRequest.from_dict(raw)
                if approval.approval_id:
                    approval.expire_if_due()
                    self._approvals[approval.approval_id] = approval
        self._trim()

    def save(self) -> bool:
        """Atomic write; returns False instead of raising on IO failure."""
        self._trim()
        payload = {
            "version": 1,
            "runs": [run.to_dict() for run in self._ordered()],
            "grants": [grant.to_dict() for grant in self._grants.values() if grant.active()],
            "approvals": [item.to_dict() for item in self._approvals.values()][-50:],
        }
        temporary = self._path.with_name(self._path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            return True
        except (OSError, ValueError):
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _ordered(self) -> list[PhoneTaskRun]:
        return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    def _trim(self) -> None:
        ordered = self._ordered()
        for run in ordered[self._max_runs:]:
            self._runs.pop(run.run_id, None)

    # -- grants

    def add_grant(self, grant: ScopedGrant) -> None:
        self._grants[grant.grant_id] = grant

    def grant(self, grant_id: str) -> ScopedGrant | None:
        grant = self._grants.get(grant_id)
        if grant is not None and not grant.active():
            return None
        return grant

    def grants(self) -> list[dict[str, Any]]:
        return [grant.to_dict() for grant in self._grants.values() if grant.active()]

    def add_approval(self, approval: ApprovalRequest) -> None:
        self._approvals[approval.approval_id] = approval

    def approval(self, approval_id: str) -> ApprovalRequest | None:
        return self._approvals.get(_short(approval_id, 64))

    def approvals(self, limit: int = 5) -> list[dict[str, Any]]:
        ordered = sorted(self._approvals.values(), key=lambda item: item.created_at, reverse=True)
        return [item.to_dict() for item in ordered[: max(1, int(limit))]]

    # -- runs

    def add_run(self, run: PhoneTaskRun) -> PhoneTaskRun:
        self._runs[run.run_id] = run
        self._trim()
        return run

    def run(self, run_id: str) -> PhoneTaskRun | None:
        return self._runs.get(run_id)

    def active_for(self, subject: str) -> PhoneTaskRun | None:
        for run in self._ordered():
            if run.subject == subject and run.status in RUN_ACTIVE:
                return run
        return None

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [run.to_dict() for run in self._ordered()[: max(1, int(limit))]]


# ---------------------------------------------------------------- planning


PACKAGE_HINTS = {
    "b站": "tv.danmaku.bili",
    "哔哩哔哩": "tv.danmaku.bili",
    "哔哩": "tv.danmaku.bili",
    "抖音": "com.ss.android.ugc.aweme",
    "快手": "com.kuaishou.nebula",
    "优酷": "com.youku.phone",
}


@dataclass(frozen=True)
class PlannedStep:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def is_write(self) -> bool:
        return self.action in WRITE_ACTIONS


class TaskPlanner:
    """Deterministic, model-free planner for the read-only stage.

    It maps a goal sentence into at most one observation plus one candidate
    write step. No LLM is involved, so the plan is reproducible and cheap; a
    later stage can replace it without changing the run/step records.
    """

    def __init__(self, packages: dict[str, str] | None = None) -> None:
        self._packages = dict(PACKAGE_HINTS)
        for hint, package in (packages or {}).items():
            hint, package = _short(hint, 40), _short(package, 160)
            if hint and package:
                self._packages[hint] = package

    def resolve(self, text: str) -> str:
        lowered = text.lower()
        for hint, package in self._packages.items():
            if hint.lower() in lowered:
                return package
        return ""

    def plan(self, goal: str, *, foreground: str = "") -> list[PlannedStep]:
        text = _short(goal, 200)
        steps = [PlannedStep("observe", {}, "先拿到一次新鲜观察")]
        if not text:
            return steps
        if any(word in text for word in ("截图", "截屏", "截个图", "截一张")):
            steps.append(PlannedStep("screenshot", {}, "只读截图"))
        if any(word in text for word in ("回到桌面", "回桌面", "回主页", "打开桌面")):
            steps.append(PlannedStep("home", {}, "回到桌面，用前台变化核验"))
            return steps
        if any(word in text for word in ("返回上一页", "按返回")):
            steps.append(PlannedStep("back", {}, "按一次返回，用前台变化核验"))
            return steps
        package = self.resolve(text)
        if package and any(word in text for word in ("暂停", "禁掉", "关掉", "关闭", "限制", "别让")):
            steps.append(PlannedStep("suspend_app", {"package": package}, "需要 grant 覆盖该包"))
        elif package and "打开" in text:
            steps.append(PlannedStep("open_app", {"package": package}, "需要 grant 覆盖该包"))
        if not package and any(word in text for word in ("暂停", "禁掉", "限制")):
            steps.append(PlannedStep("observe", {}, "目标里没有可识别的应用，只观察"))
        return steps


# ---------------------------------------------------------------- read-only runner


class ReadOnlyTaskRunner:
    """Stage 2: observe for real, simulate every write, record the verdict.

    No command is sent to the phone here. A write step is marked ``skipped``
    with a ``would_execute`` verdict so the App can show exactly what a granted
    run would have done.
    """

    def __init__(self, store: TaskRunStore, planner: TaskPlanner,
                 observe: Callable[[], dict[str, Any]]) -> None:
        self._store = store
        self._planner = planner
        self._observe = observe

    def start(self, goal: str, *, subject: str, device_id: str,
              grant: ScopedGrant | None = None) -> PhoneTaskRun:
        run = PhoneTaskRun.new(goal, subject=subject, device_id=device_id,
                               grant_id=grant.grant_id if grant else "")
        for planned in self._planner.plan(goal):
            run.add_step(planned.action, planned.args)
        run.transition(RUN_RUNNING)
        self._store.add_run(run)
        return run

    def execute(self, run: PhoneTaskRun, *, grant: ScopedGrant | None = None,
                max_observation_age: float = 90.0, now: float | None = None) -> PhoneTaskRun:
        stamp = _now() if now is None else now
        observation: ObservationRef | None = None
        observed = simulated = blocked = failed = 0
        for step in run.steps:
            if step.action == "observe":
                step.start(now=stamp)
                status = self._observe() or {}
                observation = ObservationRef.from_status(status, device_id=run.device_id, now=stamp)
                fresh = observation.fresh(max_observation_age, now=stamp)
                step.finish(
                    ok=fresh,
                    verify={
                        "foreground": observation.foreground,
                        "fresh": fresh,
                        "reported_at": observation.reported_at,
                    },
                    error="" if fresh else "observation_stale",
                    now=stamp,
                )
                observed += 1
                if not fresh:
                    failed += 1
                    run.transition(RUN_FAILED, error="observation_stale", now=stamp)
                    break
                continue
            # A write step: validate it, then record what would have happened.
            step.start(now=stamp)
            allowed, reason = False, "grant_missing"
            if grant is not None:
                allowed, reason = grant.check(
                    subject=run.subject,
                    device_id=run.device_id,
                    action=step.action,
                    package=_short(step.args.get("package"), 160),
                    now=stamp,
                )
            step.grant_id = grant.grant_id if grant else ""
            step.observation_id = observation.observation_id if observation else ""
            step.skip(reason="dry_run" if allowed else reason, now=stamp)
            step.verify = {"would_execute": allowed, "grant_ok": allowed, "reason": reason, "dry_run": True}
            simulated += 1
            if not allowed:
                blocked += 1
        if run.status == RUN_RUNNING:
            run.transition(RUN_DONE, now=stamp)
        run.summary = {
            "dry_run": True,
            "planned": len(run.steps),
            "observed": observed,
            "simulated": simulated,
            "blocked": blocked,
            "failed": failed,
        }
        run.touch(stamp)
        self._store.add_run(run)
        self._store.save()
        return run


# ---------------------------------------------------------------- stage 3 executor


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Foreground packages that mean "the phone is idle at its home screen".
_LAUNCHER_HINTS = ("launcher", ".home", "homescreen", "com.android.settings")


def _looks_idle(foreground: str) -> bool:
    lowered = foreground.lower()
    return not lowered or any(hint in lowered for hint in _LAUNCHER_HINTS)


@dataclass(frozen=True)
class Verdict:
    """Outcome of one executed step, including why it passed or failed."""

    ok: bool
    reason: str
    detail: str = ""
    command_success: bool = False
    verified: bool = False
    observed_at: float | None = None


class TaskExecutor:
    """Stage 3: execute steps and verify each postcondition with a new observation.

    Two hard rules: a command that returns success is not proof the step worked,
    and every verification uses an observation that is newer than the command
    and inside the freshness window. Writes are never retried automatically.
    """

    def __init__(
        self,
        store: TaskRunStore,
        planner: TaskPlanner,
        *,
        observe: Callable[[], Any],
        act: Callable[[str, dict[str, Any]], Any],
        observe_live: Callable[[], Any] | None = None,
        allowed_actions: tuple[str, ...] | set[str] | None = None,
        protected_packages: tuple[str, ...] | set[str] = (),
        step_timeout: float = 20.0,
        run_timeout: float = 90.0,
        observation_max_age: float = 90.0,
        observation_wait: float = 3.0,
        require_user_idle: bool = True,
    ) -> None:
        self._store = store
        self._planner = planner
        self._observe = observe
        self._observe_live = observe_live
        self._act = act
        self._allowed = {item for item in (allowed_actions or ()) if item}
        self._protected = {item for item in protected_packages if item}
        self._step_timeout = max(0.05, float(step_timeout))
        self._run_timeout = max(5.0, float(run_timeout))
        self._observation_max_age = max(1.0, float(observation_max_age))
        self._observation_wait = max(0.5, float(observation_wait))
        self._require_user_idle = bool(require_user_idle)

    # -- helpers

    async def _read_observation(self, live: bool = False) -> dict[str, Any] | None:
        """A fresh status sample, or None when the phone has nothing recent.

        ``live`` asks the phone directly instead of reading the heartbeat cache:
        a heartbeat arrives every 45 s, far too slow to verify a step.
        """
        source = self._observe_live if (live and self._observe_live is not None) else self._observe
        try:
            status = await source() or {}
        except Exception:
            return None
        stamp = _number(status.get("reported_at"))
        if stamp is None:
            return None
        if _now() - stamp > self._observation_max_age:
            return None
        return status

    async def _settle(self, step: RunStep, result: dict[str, Any],
                      before: dict[str, Any] | None) -> tuple[Verdict, dict[str, Any] | None]:
        """Poll a live observation until the postcondition holds or time is up."""
        deadline = _now() + self._observation_wait
        observation = None
        verdict = self._verify(step.action, step.args, result, None, before)
        while True:
            observation = await self._read_observation(live=True)
            verdict = self._verify(step.action, step.args, result, observation, before)
            if verdict.ok or _now() >= deadline:
                return verdict, observation
            await asyncio.sleep(0.4)

    def _verify(
        self,
        action: str,
        args: dict[str, Any],
        result: dict[str, Any],
        observation: dict[str, Any] | None,
        before: dict[str, Any] | None,
    ) -> Verdict:
        package = _short(args.get("package"), 160)
        success = bool(result.get("success"))
        verified = result.get("verified") is True
        observed_at = _number((observation or {}).get("reported_at"))
        foreground = _short((observation or {}).get("foreground_package"), 160)
        before_foreground = _short((before or {}).get("foreground_package"), 160)

        if observation is None:
            return Verdict(False, "stale_observation", "", success, verified, None)
        if action == "open_app":
            ok = foreground == package
            return Verdict(ok, "ok" if ok else "foreground_mismatch",
                           f"expected={package} got={foreground}", success, verified, observed_at)
        if action == "close_app":
            ok = foreground != package
            return Verdict(ok, "ok" if ok else "still_foreground", f"foreground={foreground}", success, verified, observed_at)
        if action == "suspend_app":
            ok = success and verified and foreground != package
            reason = "ok" if ok else ("not_verified" if not verified else "still_foreground")
            return Verdict(ok, reason, f"foreground={foreground}", success, verified, observed_at)
        if action == "unsuspend_app":
            ok = success and verified
            return Verdict(ok, "ok" if ok else "not_verified", _short(result.get("error"), 120), success, verified, observed_at)
        if action == "lock_screen":
            ok = observation.get("screen_interactive") is False
            return Verdict(ok, "ok" if ok else "screen_still_on", "", success, verified, observed_at)
        if action in {"home", "back"}:
            ok = foreground != before_foreground
            return Verdict(ok, "ok" if ok else "no_change",
                           f"{before_foreground} -> {foreground}", success, verified, observed_at)
        if action == "screenshot":
            payload = result.get("output")
            ok = success and bool(payload)
            return Verdict(ok, "ok" if ok else "empty_capture", "", success, verified, observed_at)
        # Anything else is executed but cannot be judged from the phone state.
        return Verdict(True, "executed_unverified", "", success, verified, observed_at)

    async def _rollback(self, run: PhoneTaskRun, args: dict[str, Any], grant: ScopedGrant | None) -> bool:
        """Only suspend_app rolls back; anything else leaves a pending note."""
        package = _short(args.get("package"), 160)
        allowed, _reason = (False, "no_grant")
        if grant is not None:
            allowed, _reason = grant.check(
                subject=run.subject, device_id=run.device_id,
                action="unsuspend_app", package=package, now=_now(),
            )
        if not allowed or package in self._protected:
            return False
        try:
            result = await self._act("unsuspend_app", {"package": package})
        except Exception:
            return False
        return bool(isinstance(result, dict) and result.get("success"))

    # -- main loop

    async def _active_guard(self, run: PhoneTaskRun) -> str:
        """Refuse to touch a phone that someone is actively using.

        Without this, an assistant-triggered run could press Home (or launch an
        app) in the middle of the user's own session. Screen-off or a launcher
        foreground counts as idle; the task's own target packages are allowed;
        anything else is a refusal. Callers may override explicitly, and the
        override is recorded in the run summary.
        """
        observation = await self._read_observation(live=True)
        if observation is None:
            return "stale_observation"
        if observation.get("screen_interactive") is False:
            return ""
        foreground = _short(observation.get("foreground_package"), 160)
        if _looks_idle(foreground):
            return ""
        targets = {
            _short(step.args.get("package"), 160)
            for step in run.steps
            if step.args.get("package")
        }
        if foreground in targets or foreground == "com.tauru.astrbotphoneagent":
            return ""
        return "user_active"

    async def execute(self, run: PhoneTaskRun, *, grant: ScopedGrant | None = None,
                      now: float | None = None, allow_active: bool = False) -> PhoneTaskRun:
        started = _now() if now is None else now
        if self._require_user_idle and not allow_active:
            refusal = await self._active_guard(run)
            if refusal:
                for step in run.steps:
                    step.skip(refusal, now=started)
                run.transition(RUN_FAILED, error=refusal, now=started)
                run.summary = {"dry_run": False, "refused": refusal, "allow_active": False}
                run.touch(started)
                self._store.add_run(run)
                self._store.save()
                return run
        run.transition(RUN_EXECUTING, now=started)
        counts = {"executed": 0, "verified": 0, "unverified": 0, "failed": 0,
                  "rolled_back": 0, "restore_pending": 0, "cancelled": 0}
        before = await self._read_observation()
        steps_used = 0
        for step in run.steps:
            if run.cancel_requested:
                step.skip("cancelled", now=_now())
                counts["cancelled"] += 1
                continue
            if _now() - started > self._run_timeout:
                step.skip("run_timeout", now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="run_timeout")
                break
            if step.action == "observe":
                counts["executed"] += 1
                # The run already sampled the phone before the first step; reuse
                # it instead of paying for a second probe of the same instant.
                observation = before if before is not None else await self._read_observation(live=True)
                if observation is None:
                    step.finish(ok=False, verify={"reason": "stale_observation"}, error="stale_observation", now=_now())
                    counts["failed"] += 1
                    run.transition(RUN_FAILED, error="stale_observation")
                    break
                step.finish(ok=True, verify={"foreground": _short(observation.get("foreground_package"), 160),
                                             "reported_at": _number(observation.get("reported_at"))}, now=_now())
                before = observation
                continue

            if self._allowed and step.action not in self._allowed:
                step.finish(ok=False, verify={"reason": "action_not_enabled"}, error="action_not_enabled", now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="action_not_enabled")
                break
            package = _short(step.args.get("package"), 160)
            if package and package in self._protected:
                step.finish(ok=False, verify={"reason": "protected_package"}, error="protected_package", now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="protected_package")
                break
            allowed, reason = False, "grant_missing"
            if grant is not None:
                allowed, reason = grant.check(
                    subject=run.subject, device_id=run.device_id,
                    action=step.action, package=package, now=_now(),
                )
            if not allowed:
                step.grant_id = grant.grant_id if grant else ""
                step.finish(ok=False, verify={"reason": reason}, error=reason, now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error=reason)
                break
            steps_used += 1
            if grant is not None and steps_used > grant.max_steps:
                step.grant_id = grant.grant_id
                step.finish(ok=False, verify={"reason": "grant_step_limit"}, error="grant_step_limit", now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="grant_step_limit")
                break

            step.start(command_id="", now=_now())
            step.grant_id = grant.grant_id if grant else ""
            act_started = _now()
            try:
                result = await asyncio.wait_for(self._act(step.action, dict(step.args)), timeout=self._step_timeout)
            except asyncio.TimeoutError:
                verdict, observation = await self._settle(step, {"success": False}, before)
                step.status = STEP_DONE if verdict.ok else STEP_UNKNOWN
                step.verify = {"reason": "timeout", "observed": bool(observation)}
                step.finished_at = _now()
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="timeout")
                break
            except Exception as exc:
                step.finish(ok=False, verify={"reason": "command_error"}, error=_short(exc, 160), now=_now())
                counts["failed"] += 1
                run.transition(RUN_FAILED, error="command_error")
                break

            counts["executed"] += 1
            act_ms = round((_now() - act_started) * 1000)
            result = result if isinstance(result, dict) else {"success": False}
            step.command_id = _short(result.get("command_id"), 64)
            verdict, observation = await self._settle(step, result, before)
            verify_ms = round((_now() - act_started) * 1000) - act_ms
            step.verify = {
                "ok": verdict.ok,
                "reason": verdict.reason,
                "detail": verdict.detail,
                "command_success": verdict.command_success,
                "verified": verdict.verified,
                "observed_at": verdict.observed_at,
                "act_ms": act_ms,
                "verify_ms": verify_ms,
            }
            step.finished_at = _now()
            step.error = "" if verdict.ok else verdict.reason
            if verdict.ok and verdict.reason == "executed_unverified":
                step.status = STEP_EXECUTED_UNVERIFIED
                counts["unverified"] += 1
            elif verdict.ok:
                step.status = STEP_DONE
                counts["verified"] += 1
            else:
                step.status = STEP_FAILED
                counts["failed"] += 1
                if step.action == "suspend_app":
                    rolled = await self._rollback(run, step.args, grant)
                    if rolled:
                        counts["rolled_back"] += 1
                    else:
                        counts["restore_pending"] += 1
                run.transition(RUN_FAILED, error=verdict.reason)
                break
            if observation is not None:
                before = observation

        if run.status in {RUN_EXECUTING, RUN_VERIFYING}:
            run.transition(RUN_DONE)
        run.summary = {"dry_run": False, "planned": len(run.steps), **counts, "allow_active": allow_active}
        run.touch(_now())
        self._store.add_run(run)
        self._store.save()
        return run

    def cancel(self, run_id: str) -> PhoneTaskRun | None:
        run = self._store.run(run_id)
        if run is None:
            return None
        run.cancel_requested = True
        return run
