"""Requested task results are copied from re-observed nodes, never model prose."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

FIELDS = frozenset({"title", "author", "duration", "url"})
NODE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ResultEvidenceError(ValueError):
    pass


def validate_result_spec(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"fields", "max_items", "required_items"}:
        raise ResultEvidenceError("ui_result_scope_invalid")
    fields = raw.get("fields", ["title"])
    maximum, required = raw.get("max_items", 3), raw.get("required_items", 1)
    if (not isinstance(fields, list) or not fields or len(fields) > len(FIELDS)
            or any(not isinstance(field, str) or field not in FIELDS for field in fields)
            or len(set(fields)) != len(fields) or "title" not in fields
            or type(maximum) is not int or type(required) is not int
            or not 1 <= required <= maximum <= 10):
        raise ResultEvidenceError("ui_result_scope_invalid")
    return {"fields": list(fields), "max_items": maximum, "required_items": required}


def _object(raw):
    if isinstance(raw, str):
        if len(raw) > 12000:
            raise ResultEvidenceError("ui_result_selection_invalid")
        def unique(pairs):
            row = {}
            for key, value in pairs:
                if key in row:
                    raise ResultEvidenceError("ui_result_selection_invalid")
                row[key] = value
            return row
        def invalid(_):
            raise ResultEvidenceError("ui_result_selection_invalid")
        try:
            raw = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
        except (ValueError, TypeError, RecursionError):
            raise ResultEvidenceError("ui_result_selection_invalid") from None
    return raw


def _eligible(node):
    return node.get("enabled") is True and not node.get("password") and not node.get("editable")


def _signature(node):
    # IDs and coordinates may move between observations. The actual visible
    # content, element identity and type must still match exactly once.
    return tuple(node.get(key, "") for key in ("resource_id", "class_name", "text", "description", "source"))


def _associated(title, field_node, title_nodes, snapshot):
    if field_node is title:
        return False
    a, b = title["bounds"], field_node["bounds"]
    # A field belongs to the nearest selected title in the same horizontal
    # column. Refuse ties or distant fields rather than invent card relations.
    if max(a[0], b[0]) >= min(a[2], b[2]):
        return False
    limit = min(snapshot["height"] * 0.18, max(96, (a[3] - a[1]) * 5))
    center = (b[1] + b[3]) / 2
    distance = abs(center - (a[1] + a[3]) / 2)
    if distance > limit:
        return False
    for other in title_nodes:
        if other is title: continue
        c = other["bounds"]
        if max(c[0], b[0]) >= min(c[2], b[2]): continue
        if abs(center - (c[1] + c[3]) / 2) <= distance:
            return False
    return True


def select_result_evidence(raw, snapshot, spec):
    """Parse field->node references; no 'value', free text, or inferred URL."""
    raw = _object(raw)
    if not isinstance(raw, dict) or set(raw) != {"items"}:
        raise ResultEvidenceError("ui_result_selection_invalid")
    rows = raw["items"]
    if not isinstance(rows, list) or not spec["required_items"] <= len(rows) <= spec["max_items"]:
        raise ResultEvidenceError("ui_results_incomplete")
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    used_titles, evidence = set(), []
    for row in rows:
        if not isinstance(row, dict) or not row or set(row) - set(spec["fields"]) or "title" not in row:
            raise ResultEvidenceError("ui_result_selection_invalid")
        item = {}
        for field, ref in row.items():
            if ref is None and field != "title":
                continue
            if (not isinstance(ref, dict) or set(ref) - {"node_id", "attribute"}
                    or not isinstance(ref.get("node_id"), str) or not NODE_ID.fullmatch(ref["node_id"])
                    or not isinstance(ref.get("attribute", "text"), str)
                    or ref.get("attribute", "text") not in {"text", "description"}):
                raise ResultEvidenceError("ui_result_selection_invalid")
            node = nodes.get(ref["node_id"])
            if node is None or not _eligible(node):
                raise ResultEvidenceError("ui_result_evidence_invalid")
            attribute = ref.get("attribute", "text")
            value = node.get(attribute, "").strip()
            if not value or len(value) > 240 or any(ord(char) < 32 for char in value):
                raise ResultEvidenceError("ui_result_evidence_invalid")
            if field == "url":
                try:
                    parts = urlsplit(value)
                    valid = parts.scheme in {"http", "https"} and bool(parts.hostname) and not parts.username and not parts.password and not any(char.isspace() for char in value)
                except ValueError:
                    valid = False
                if not valid:
                    raise ResultEvidenceError("ui_result_evidence_invalid")
            item[field] = {"node": node, "attribute": attribute, "value": value}
        title = item["title"]
        title_key = " ".join(title["value"].split()).casefold()
        if title_key in used_titles:
            raise ResultEvidenceError("ui_result_duplicate")
        used_titles.add(title_key)
        evidence.append(item)
    title_nodes = [item["title"]["node"] for item in evidence]
    for item in evidence:
        title = item["title"]["node"]
        for field, ref in item.items():
            if field != "title" and not _associated(title, ref["node"], title_nodes, snapshot):
                raise ResultEvidenceError("ui_result_association_ambiguous")
    return evidence


def verify_result_evidence(evidence, fresh, spec):
    """Return only requested values that survived a second independent read."""
    output, refreshed = [], []
    for item in evidence:
        row = {field: None for field in spec["fields"]}
        current = {}
        for field, ref in item.items():
            matches = [node for node in fresh["nodes"] if _eligible(node) and _signature(node) == _signature(ref["node"])]
            if len(matches) != 1 or matches[0].get(ref["attribute"], "").strip() != ref["value"]:
                raise ResultEvidenceError("ui_result_evidence_changed")
            row[field] = ref["value"]
            current[field] = matches[0]
        output.append(row)
        refreshed.append(current)
    title_nodes = [item["title"] for item in refreshed]
    for item in refreshed:
        for field, node in item.items():
            if field != "title" and not _associated(item["title"], node, title_nodes, fresh):
                raise ResultEvidenceError("ui_result_association_ambiguous")
    if not spec["required_items"] <= len(output) <= spec["max_items"]:
        raise ResultEvidenceError("ui_results_incomplete")
    return {"items": output, "verified": True, "captured_at": fresh["captured_at"],
            "source": fresh.get("source", "accessibility"), "missing_fields": sorted({field for row in output for field, value in row.items() if value is None})}
