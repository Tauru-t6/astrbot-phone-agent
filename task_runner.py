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

# Actions that change the phone. Read-only observation is not gated by a grant.
WRITE_ACTIONS = {
    "open_app",
    "close_app",
    "tap",
    "long_press",
    "input_text",
    "key_event",
    "scroll",
    "swipe",
    "suspend_app",
    "unsuspend_app",
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
STEP_STATUSES = {STEP_PENDING, STEP_RUNNING, STEP_DONE, STEP_FAILED, STEP_SKIPPED}


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
        if action in WRITE_ACTIONS and not package:
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


class TaskRunStore:
    """Bounded, atomic run history shared by the runner and the web API."""

    def __init__(self, path: str | Path, *, max_runs: int = 50) -> None:
        self._path = Path(path)
        self._max_runs = max(1, int(max_runs))
        self._runs: dict[str, PhoneTaskRun] = {}
        self._grants: dict[str, ScopedGrant] = {}

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
                self._runs[run.run_id] = run
        for raw in data.get("grants") or []:
            if isinstance(raw, dict):
                grant = ScopedGrant.from_dict(raw)
                if grant.active():
                    self._grants[grant.grant_id] = grant
        self._trim()

    def save(self) -> bool:
        """Atomic write; returns False instead of raising on IO failure."""
        self._trim()
        payload = {
            "version": 1,
            "runs": [run.to_dict() for run in self._ordered()],
            "grants": [grant.to_dict() for grant in self._grants.values() if grant.active()],
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
