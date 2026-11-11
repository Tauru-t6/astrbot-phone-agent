"""Bounded, device-scoped Health Connect summaries; never a raw-record archive."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

try:
    from .storage import write_json_atomic
except ImportError:
    from storage import write_json_atomic

METRICS = {
    "steps": ("steps", 0, 300000),
    "active_calories_kcal": ("calories", 0, 50000),
    "heart_rate_avg": ("heart_rate", 1, 350),
    "heart_rate_min": ("heart_rate", 1, 350),
    "heart_rate_max": ("heart_rate", 1, 350),
    "spo2_avg": ("oxygen_saturation", 0, 100),
    "spo2_min": ("oxygen_saturation", 0, 100),
    "sleep_minutes": ("sleep", 0, 1440),
}
TYPES = {spec[0] for spec in METRICS.values()}
STATUSES = {"ok", "permission_denied", "unavailable", "read_failed", "disabled"}
ORIGIN_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")


def finite(value: Any) -> bool:
    # JSON integers are finite even when converting them to float would
    # overflow; callers subsequently enforce the field's numeric range.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def normalize(payload: Any, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or type(payload.get("schema_version")) is not int:
        raise ValueError("invalid_schema")
    origins = payload.get("origin_packages")
    if (payload.get("source") != "health_connect" or not isinstance(origins, list) or len(origins) != 1
            or not isinstance(origins[0], str) or len(origins[0]) > 255
            or not ORIGIN_PACKAGE_RE.fullmatch(origins[0])):
        raise ValueError("invalid_source")
    device = payload.get("device_id")
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", device):
        raise ValueError("invalid_device")
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", snapshot_id):
        raise ValueError("invalid_snapshot_id")
    captured = payload.get("captured_at")
    if not finite(captured) or not now - 86400 <= captured <= now + 300:
        raise ValueError("invalid_capture_time")
    zone = payload.get("zone_id")
    if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,80}", zone):
        raise ValueError("invalid_zone")
    status = payload.get("status")
    types = payload.get("granted_types")
    days = payload.get("days")
    if not isinstance(status, str) or status not in STATUSES or not isinstance(types, list) or any(not isinstance(item, str) or item not in TYPES for item in types):
        raise ValueError("invalid_permissions")
    if not isinstance(days, list) or len(days) > 7 or (status != "ok" and days):
        raise ValueError("invalid_days")
    rows = []
    dates = set()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for raw in days:
        if not isinstance(raw, dict):
            raise ValueError("invalid_day")
        try:
            day = date.fromisoformat(raw["date"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid_date") from None
        if str(day) != raw["date"] or not today - timedelta(days=8) <= day <= today + timedelta(days=1) or day in dates:
            raise ValueError("invalid_date")
        dates.add(day)
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict) or not metrics or any(key not in METRICS for key in metrics):
            raise ValueError("invalid_metrics")
        row = {"date": str(day)}
        for key, value in metrics.items():
            kind, low, high = METRICS[key]
            if kind not in types or not finite(value) or not low <= value <= high or (key == "steps" and int(value) != value):
                raise ValueError("invalid_metric_value")
            row[key] = int(value) if key == "steps" else value
        if "heart_rate_min" in row and "heart_rate_max" in row and row["heart_rate_min"] > row["heart_rate_max"]:
            raise ValueError("invalid_metric_value")
        rows.append(row)
    return {"schema_version": 1, "source": "health_connect", "device_id": device,
            "snapshot_id": snapshot_id, "captured_at": captured, "zone_id": zone,
            "origin_packages": list(origins), "status": status,
            "granted_types": sorted(set(types)), "days": sorted(rows, key=lambda row: row["date"], reverse=True)}


class HealthConnectStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._generation = 0

    def generation(self) -> int:
        """Fence uploads already received before a successful deletion."""
        with self.lock:
            return self._generation

    def _load(self) -> dict:
        try:
            if self.path.stat().st_size > 65536:
                return {}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # Old captures remain inspectable as stale; normalize at their own
            # capture time to apply the same shape/range checks on disk reads.
            captured = raw.get("captured_at")
            if not finite(captured):
                return {}
            value = normalize({**raw, "days": [{"date": row["date"], "metrics": {k: v for k, v in row.items() if k != "date"}} for row in raw.get("days", [])]}, now=captured)
            value["received_at"] = raw.get("received_at")
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return {}

    def accept(self, payload: dict, *, now: float | None = None,
               binding_valid: Callable[[], bool] | None = None,
               expected_generation: int | None = None) -> dict:
        now = time.time() if now is None else now
        value = normalize(payload, now=now)
        with self.lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise ValueError("snapshot_invalidated")
            # Serialize the binding check with all snapshot writes. An old
            # request must not overwrite a new device after waiting for I/O.
            if binding_valid is not None and not binding_valid():
                raise ValueError("device_changed")
            old = self._load()
            if old.get("device_id") == value["device_id"]:
                comparable = {k: v for k, v in old.items() if k != "received_at"}
                if old.get("snapshot_id") == value["snapshot_id"]:
                    if comparable != value:
                        raise ValueError("snapshot_conflict")
                    return {"accepted": True, "duplicate": True, "received_at": old.get("received_at")}
                if old.get("captured_at", 0) > value["captured_at"]:
                    raise ValueError("snapshot_older_than_current")
            value["received_at"] = now
            write_json_atomic(self.path, value)
        return {"accepted": True, "duplicate": False, "received_at": now}

    def clear(self, device_id: str) -> None:
        with self.lock:
            old = self._load()
            if old and old.get("device_id") != device_id:
                # This device has no stored snapshot. Keep the other device's
                # data untouched without wedging the caller's clear retry queue.
                self._generation += 1
                return
            self.path.unlink(missing_ok=True)
            self._generation += 1

    def read(self, device_id: str, days: int = 7, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            value = self._load()
        reason = "no_snapshot"
        if value and value.get("device_id") == device_id:
            reason = value["status"]
        else:
            value = {}
        rows = value.get("days", [])[:max(1, min(int(days), 30))] if reason == "ok" else []
        age = now - value.get("captured_at", 0) if value else None
        available = bool(rows)
        fresh = available and age is not None and -300 <= age <= 10800
        return {"source": "health_connect", "available": available, "fresh": fresh,
                "days": rows, "zone_id": value.get("zone_id"), "origin_packages": value.get("origin_packages", []),
                "granted_types": value.get("granted_types", []),
                "sync": {"ok": reason == "ok", "finished_at": value.get("captured_at"),
                         "age_minutes": round(max(0, age) / 60, 1) if age is not None else None,
                         "stale": not fresh, "received_at": value.get("received_at")},
                "error": "" if available else ("no_records" if reason == "ok" else reason)}
