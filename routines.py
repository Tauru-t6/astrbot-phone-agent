"""Task-library HTTP handlers and an opt-in daily Bot scheduler.

Mixed into the plugin by main.py. All command execution still goes through its
existing proposal, approval, device binding and runtime-policy checks.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
from pathlib import Path
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
from quart import jsonify, request, g

from .task_library import TaskLibrary, resume_scope


class RoutinesMixin:
    def _init_routines(self):
        custom = self.config.get("task_library_path")
        path = (self._plugin_data_path("task_library_path", "phone_agent_task_library.json") if custom else
                Path(self._task_run_path()).with_name("phone_agent_task_library.json"))
        self._task_library = TaskLibrary(path)
        self._routine_task = None
        self._routine_tick_lock = asyncio.Lock()

    def _routine_routes(self):
        return (
            ("/app/routines", self._web_routines, ["GET", "POST"], "Saved tasks and approved Bot schedules", None),
            ("/app/routines/propose", self._web_routine_propose, ["POST"], "Propose one saved task", None),
            ("/app/task/resume", self._web_task_resume, ["POST"], "Re-observe a paused task with fresh approval", None),
        )

    def _routines_enabled(self):
        return (self._enabled() and self._feature("task_routines") and self._feature("ui_tasks")
                and self._feature("task_execute") and self._feature("observe_tool")
                and self._control_backend() == "app" and not getattr(self, "_terminating", False))

    def _routine_actor(self, payload=None):
        principal_hook = getattr(self, "_routine_principal", None)
        principal = principal_hook() if callable(principal_hook) else None
        if principal is not None:
            if not isinstance(principal, dict): raise ValueError("unauthorized")
            subject, device = principal.get("subject", ""), principal.get("device_id", "")
        else:
            subject, device = next(iter(sorted(self._allowed_users())), ""), ""
        if not subject or subject not in self._allowed_users(): raise ValueError("unauthorized")
        requester = payload.get("requester_device_id", device) if isinstance(payload, dict) else device
        if device and requester != device: raise ValueError("requester_device_mismatch")
        if requester and (not isinstance(requester, str) or self._devices.backend(requester) is None):
            raise ValueError("unknown_requester")
        return {"subject": subject, "device_id": device, "requester_device_id": requester}

    @staticmethod
    def _routine_visible(row, actor):
        device = actor.get("device_id", "")
        if not device: return True
        if hasattr(row, "summary"):
            return row.device_id == device or row.summary.get("requester_device_id") == device
        return row.get("device_id") == device or row.get("requester_device_id") == device

    def _routine_public(self, requested_device_id="", subject=None):
        subject = subject or next(iter(sorted(self._allowed_users())), "")
        return {"supported": True, "enabled": self._enabled() and self._feature("ui_tasks"),
                "schedules_enabled": self._routines_enabled(), "storage_available": not self._task_library.invalid,
                "templates": self._task_library.templates_for(subject, requested_device_id or ""),
                "schedules": self._task_library.schedules_for(subject, requested_device_id or "")}

    async def _routine_payload(self):
        if not self._web_write_allowed(): raise ValueError("forbidden")
        if request.content_length is not None and request.content_length > 32768:
            raise ValueError("payload_too_large")
        async def read():
            verified = getattr(g, "phone_verified_body", None)
            if verified is not None:
                if len(verified) > 32768: raise ValueError("payload_too_large")
                return verified
            raw = bytearray()
            async for chunk in request.body:
                raw.extend(chunk)
                if len(raw) > 32768: raise ValueError("payload_too_large")
            return bytes(raw)
        try:
            raw = await asyncio.wait_for(read(), timeout=10)
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except asyncio.TimeoutError: raise ValueError("invalid_json") from None
        except (UnicodeError, ValueError, RecursionError) as error:
            if str(error) == "payload_too_large": raise
            raise ValueError("invalid_json") from None
        if not isinstance(value, dict): raise ValueError("invalid_json")
        return value

    @staticmethod
    def _routine_error(error):
        reason = str(error) if isinstance(error, ValueError) else "storage_unavailable"
        code = 403 if reason in {"forbidden", "unauthorized", "requester_device_mismatch", "task_not_visible"} else 400
        if reason in {"storage_unavailable", "task_library_invalid"}: code = 503
        if reason == "payload_too_large": code = 413
        return jsonify({"success": False, "error_code": reason}), code

    def _routine_private_session(self, subject):
        row = getattr(self, "_night_guard_states", {}).get(subject, {})
        session = row.get("session", "")
        # A schedule never accepts a chat destination from its JSON payload.
        return session if isinstance(session, str) and ":FriendMessage:" in session and len(session) <= 240 else ""

    def _routine_template_approved(self, run):
        if run.status == "done": return True
        return any(row.get("run_id") == run.run_id and row.get("status") == "approved"
                   for row in self._task_runs.approvals(limit=50))

    async def _web_routines(self):
        try:
            if request.method == "GET":
                actor = self._routine_actor()
                target = request.args.get("device_id", actor["device_id"])
                if actor["device_id"] and target != actor["device_id"]: raise ValueError("requester_device_mismatch")
                return jsonify({"success": True, **self._routine_public(target, actor["subject"])})
            payload = await self._routine_payload(); actor = self._routine_actor(payload)
            subject = actor["subject"]
            action = payload.get("action")
            if not isinstance(action, str): raise ValueError("invalid_routine_action")
            if action == "save":
                run = self._task_runs.run(payload.get("run_id", ""))
                if run is None or run.subject != subject: raise ValueError("task_not_found")
                if not self._routine_visible(run, actor): raise ValueError("task_not_visible")
                row = self._task_library.save_from_run(run, payload.get("name"), payload.get("parameters"),
                    approved=self._routine_template_approved(run))
                self._audit("task_template_saved", template_id=row["template_id"], source_run_id=run.run_id)
                return jsonify({"success": True, "template": row})
            if action == "delete":
                row = self._task_library.template(payload.get("template_id"), subject)
                if not self._routine_visible(row, actor): raise ValueError("task_not_visible")
                self._task_library.delete_template(row["template_id"], subject)
                return jsonify({"success": True})
            if action == "schedule_save":
                if not self._routines_enabled(): raise ValueError("task_routines_disabled")
                template = self._task_library.template(payload.get("template_id"), subject)
                if not self._routine_visible(template, actor): raise ValueError("task_not_visible")
                device = payload.get("device_id")
                backend = self._devices.backend(device)
                if backend is None or not backend.registration() or not self._ui_app_supported(backend):
                    raise ValueError("unknown_device")
                if actor["device_id"] and device not in {actor["device_id"], template["device_id"]}:
                    raise ValueError("requester_device_mismatch")
                current = dict(payload, requester_device_id=actor["requester_device_id"])
                row = self._task_library.save_schedule(current, subject, role=self._devices.role(device),
                    role_revision=self._devices.revision(device), private_session=self._routine_private_session(subject))
                self._audit("task_schedule_approved", schedule_id=row["schedule_id"], device_id=device,
                            max_runs=row["max_runs"], max_model_calls=row["max_model_calls"])
                self._start_routines()
                return jsonify({"success": True, "schedule": row})
            if action in {"schedule_enable", "schedule_delete"}:
                schedule_id = payload.get("schedule_id")
                row = self._task_library.schedules.get(schedule_id) if isinstance(schedule_id, str) else None
                if row is None or row["subject"] != subject: raise ValueError("schedule_not_found")
                if not self._routine_visible(row, actor): raise ValueError("task_not_visible")
                if action == "schedule_delete": self._task_library.delete_schedule(schedule_id, subject)
                else:
                    if payload.get("enabled") is not False: raise ValueError("schedule_approval_required")
                    self._task_library.disable_schedule(schedule_id, subject)
                self._audit("task_schedule_revoked", schedule_id=schedule_id)
                return jsonify({"success": True})
            raise ValueError("invalid_routine_action")
        except (ValueError, OSError) as error:
            return self._routine_error(error)

    def _routine_propose(self, template_id, parameters, target, subject, *, requester_device_id="", umo=""):
        scope, recipe, _ = self._task_library.instantiate(template_id, parameters, target, subject)
        run, approval = self._create_ui_proposal(scope, subject, requester_device_id=requester_device_id, umo=umo)
        run.summary.update(template_id=template_id)
        if recipe: run.summary["recipe_hint"] = recipe
        if not self._task_runs.save():
            approval.decide(False, reason="storage_unavailable"); run.cancel("storage_unavailable")
            raise ValueError("storage_unavailable")
        return run, approval

    async def _web_routine_propose(self):
        try:
            payload = await self._routine_payload(); actor = self._routine_actor(payload)
            template = self._task_library.template(payload.get("template_id"), actor["subject"])
            if not self._routine_visible(template, actor): raise ValueError("task_not_visible")
            target = payload.get("device_id", template["device_id"])
            if actor["device_id"] and target not in {actor["device_id"], template["device_id"]}:
                raise ValueError("requester_device_mismatch")
            run, approval = self._routine_propose(template["template_id"], payload.get("parameters"), target,
                actor["subject"], requester_device_id=actor["requester_device_id"])
            return jsonify({"success": True, "run": run.to_dict(), "approval": self._approval_public(approval.to_dict())})
        except (ValueError, OSError) as error:
            return self._routine_error(error)

    async def _web_task_resume(self):
        try:
            payload = await self._routine_payload(); actor = self._routine_actor(payload)
            source = self._task_runs.run(payload.get("run_id", ""))
            if source is None or source.subject != actor["subject"]: raise ValueError("task_not_found")
            if not self._routine_visible(source, actor): raise ValueError("task_not_visible")
            if self._device_task_busy(source.device_id): raise ValueError("device_busy")
            backend = self._devices.backend(source.device_id)
            if backend is None or not backend.registration(): raise ValueError("unknown_device")
            scope, metadata = resume_scope(source, current_device_id=backend.binding()[0],
                current_role=self._devices.role(source.device_id), manual_state_confirmed=payload.get("manual_state_confirmed") is True)
            # A parent can create only one follow-up proposal. Rejecting the new
            # proposal is intentional; another retry must be a new explicit task.
            if source.summary.get("resume_child"): raise ValueError("resume_already_created")
            run, approval = self._create_ui_proposal(scope, actor["subject"],
                requester_device_id=actor["requester_device_id"], umo=source.summary.get("session_umo", ""))
            run.summary.update(metadata)
            source.summary["resume_child"] = run.run_id
            if not self._task_runs.save():
                source.summary.pop("resume_child", None)
                approval.decide(False, reason="storage_unavailable"); run.cancel("storage_unavailable")
                raise ValueError("storage_unavailable")
            self._audit("ui_task_resume_proposed", parent_run_id=source.run_id, run_id=run.run_id)
            return jsonify({"success": True, "run": run.to_dict(), "approval": self._approval_public(approval.to_dict())})
        except (ValueError, OSError) as error:
            return self._routine_error(error)

    def _start_routines(self):
        if not self._routines_enabled() or self._task_library.invalid: return
        if self._routine_task is not None and not self._routine_task.done(): return
        try: self._routine_task = asyncio.get_running_loop().create_task(self._routine_loop())
        except RuntimeError: return

    async def _stop_routines(self):
        task = self._routine_task; self._routine_task = None
        if task is not None:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def _routine_loop(self):
        while True:
            try:
                await self._routine_tick()
            except asyncio.CancelledError: raise
            except Exception:
                # Never log template parameters, model data or exception text.
                logger.warning("Phone Control scheduled task tick failed")
            await asyncio.sleep(15)

    async def _routine_tick(self, now=None):
        if not self._routines_enabled(): return
        stamp = time.time() if now is None else now
        async with self._routine_tick_lock:
            def available(row):
                device = row["device_id"]
                backend = self._devices.backend(device)
                if (row["subject"] not in self._allowed_users() or self._devices.role(device) != "bot"
                        or self._devices.revision(device) != row["device_role_revision"]
                        or row["scope"]["package"] not in self._ui_allowed_packages()
                        or backend is None or not backend.registration() or not self._ui_app_supported(backend)
                        or self._device_task_busy(device)):
                    return False
                state = backend.last_status() or {}
                received = state.get("received_at")
                return (type(received) in (float, int) and math.isfinite(received) and 0 <= stamp - received <= 120
                        and state.get("shizuku_ready") is True and state.get("screen_interactive") is True)
            reservations = self._task_library.due(now=stamp, available=available)
            for entry in reservations:
                row, claim = entry["schedule"], entry["claim"]
                if not self._routines_enabled() or available(row) is not True:
                    self._task_library.finish_claim(claim["claim_id"], status="skipped", now=stamp)
                    continue
                run = approval = None
                try:
                    run, approval = self._create_ui_proposal(copy.deepcopy(row["scope"]), row["subject"],
                        requester_device_id=row.get("requester_device_id", ""), umo=row.get("private_session", ""))
                    run.summary.update(routine_schedule_id=row["schedule_id"], routine_revision=row["revision"],
                        routine_claim_id=claim["claim_id"], routine_model_limit=row["per_run_model_calls"],
                        routine_approved_at=row["approved_at"], template_id=row["template_id"])
                    if row.get("recipe"): run.summary["recipe_hint"] = copy.deepcopy(row["recipe"])
                    self._task_library.bind_claim(claim["claim_id"], run.run_id)
                    if not self._task_runs.save(): raise ValueError("storage_unavailable")
                    if self._routine_run_allowed(run) is not True: raise ValueError("schedule_revoked")
                    # Full, finite standing consent was recorded by schedule_save.
                    # The regular launcher still issues a fresh run-bound grant.
                    response, status = await self._launch_ui_task(run, approval)
                    if status != 202:
                        approval.decide(False, reason="schedule_launch_failed")
                        run.cancel("schedule_launch_failed")
                        self._task_runs.save()
                        self._task_library.finish_claim(claim["claim_id"], status="skipped")
                    else:
                        self._audit("scheduled_ui_task_started", schedule_id=row["schedule_id"], run_id=run.run_id, device_id=run.device_id)
                except asyncio.CancelledError:
                    if run is not None: run.cancel_requested = True
                    raise
                except (ValueError, OSError):
                    if approval is not None: approval.decide(False, reason="schedule_unavailable")
                    if run is not None: run.cancel("schedule_unavailable"); self._task_runs.save()
                    self._task_library.finish_claim(claim["claim_id"], status="skipped")

    def _routine_run_allowed(self, run):
        claim_id = run.summary.get("routine_claim_id")
        if not claim_id: return True
        if not self._routines_enabled(): return "schedule_revoked"
        return self._task_library.claim_allowed(claim_id, run.summary.get("routine_revision"), run.device_id,
            role=self._devices.role(run.device_id), role_revision=self._devices.revision(run.device_id))

    async def _routine_run_finished(self, run):
        claim_id = run.summary.get("routine_claim_id")
        if not claim_id: return
        try:
            notification = self._task_library.finish_claim(claim_id, status=run.status,
                model_calls=min(16, max(0, int(run.summary.get("model_calls", 0)))), result=run.summary.get("ui_results"))
        except (OSError, ValueError, TypeError, OverflowError):
            logger.warning("Phone Control could not persist a scheduled task outcome")
            return
        if notification is None or not self._routines_enabled() or run.subject not in self._allowed_users(): return
        row = self._task_library.schedules.get(notification["schedule_id"])
        if row is None or row["revision"] != run.summary.get("routine_revision") or not row["notify"]: return
        session = notification["session"]
        if session != self._routine_private_session(run.subject): return
        # Notification delivery is only enabled by a dedicated human-approved
        # schedule switch. No model can name a recipient or turn it on.
        lines = ["Bot 手机已完成预约任务。"]
        results = run.summary.get("ui_results") or {}
        if results.get("verified") is True:
            for index, item in enumerate(results.get("items", [])[:10], 1):
                if not isinstance(item, dict): continue
                title = item.get("title")
                if not isinstance(title, str): continue
                detail = " · ".join(item[key][:200] for key in ("author", "duration") if isinstance(item.get(key), str))
                lines.append(f"{index}. {title[:300]}" + (" · " + detail if detail else ""))
        lines.append("运行记录：" + run.run_id)
        try:
            await asyncio.wait_for(self.context.send_message(session, MessageChain().message("\n".join(lines))), timeout=20)
            self._audit("task_schedule_notified", schedule_id=row["schedule_id"], run_id=run.run_id)
        except asyncio.CancelledError: raise
        except Exception:
            self._audit("task_schedule_notification_failed", schedule_id=row["schedule_id"], run_id=run.run_id)
