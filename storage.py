"""Small, shared file primitives for durable state and bounded log reads."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_jsonl_tail(path: str | Path, limit: int, max_bytes: int = 1024 * 1024) -> list[dict[str, Any]]:
    """Read recent records without loading an ever-growing audit file in full.

    Only the last ``max_bytes`` are inspected, and a leading partial record is
    discarded. Invalid records are ignored, as they are in the audit API.
    """
    if limit <= 0 or max_bytes <= 0:
        return []
    chunks: list[bytes] = []
    newlines = 0
    read = 0
    try:
        with Path(path).open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            while position > 0 and newlines <= limit and read < max_bytes:
                size = min(8192, position, max_bytes - read)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
                read += size
        lines = b"".join(reversed(chunks)).splitlines()
        if position > 0:
            lines = lines[1:]
    except OSError:
        return []
    records = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
