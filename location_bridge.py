"""Validated phone fixes and read-only Baidu MCP handoff metadata.

This module never calls a map service, retains a fix, or reads MCP credentials.
Coordinates are never guessed or shifted based on geographical bounds.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any


def _number(value: Any, low: float, high: float) -> float | None:
    if type(value) not in (int, float):
        return None
    return float(value) if low <= value <= high and (type(value) is int or math.isfinite(value)) else None


def normalize_location(payload: Any, backend: str, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or ("success" in payload and payload["success"] is not True):
        return {"success": False, "backend": backend, "error_code": "location_failed", "error": "手机未确认定位成功"}
    latitude = _number(payload.get("latitude"), -90, 90)
    longitude = _number(payload.get("longitude"), -180, 180)
    if latitude is None or longitude is None:
        return {"success": False, "backend": backend, "error_code": "invalid_location", "error": "手机未返回有效的经纬度"}
    aliases = {"wgs84": "wgs84", "wgs-84": "wgs84", "gcj02": "gcj02", "gcj-02": "gcj02", "bd09": "bd09ll", "bd09ll": "bd09ll", "bd-09": "bd09ll"}
    raw_type = payload.get("coord_type")
    coord_type = aliases.get(raw_type.strip().lower(), "unknown") if isinstance(raw_type, str) else "unknown"
    location = {"latitude": latitude, "longitude": longitude, "coord_type": coord_type, "fresh": None}
    accuracy = _number(payload.get("accuracy_m"), 0, 100000)
    if accuracy is not None:
        location["accuracy_m"] = accuracy
    for key, limit in (("provider", 80), ("address", 400)):
        if isinstance(payload.get(key), str) and payload[key].strip():
            location[key] = payload[key].strip()[:limit]
    captured = _number(payload.get("captured_at"), 1, now + 300)
    age = _number(payload.get("age_seconds"), 0, 315360000)
    if captured is not None:
        location["captured_at"] = captured
        age = max(age or 0.0, now - captured, 0.0)
    if age is not None:
        location["age_seconds"] = round(age, 2)
        location["fresh"] = age <= 120
    if "captured_at" in payload and captured is None:
        location["fresh"] = False
        location["time_status"] = "invalid_capture_time"
    if type(payload.get("is_mock")) is bool:
        location["is_mock"] = payload["is_mock"]
    address_status = payload.get("address_status")
    supported_statuses = {"not_requested", "not_supported", "not_provided", "failed", "available"}
    location["address_status"] = "available" if "address" in location else (
        address_status if isinstance(address_status, str) and address_status in supported_statuses else "not_provided"
    )
    return {"success": True, "backend": backend, "location": location}


def _name(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,100}", value) else ""


def map_capabilities(context: Any) -> dict:
    """Inspect the host's already registered tools, without making an MCP call."""
    try:
        manager = context.get_llm_tool_manager()
        registered = manager.func_list
        if not isinstance(registered, (list, tuple)):
            raise TypeError
    except Exception:
        return {"status": "registry_unavailable", "tools": [], "reverse_geocode_available": False, "coordinate_convert_available": False}
    tools = []
    for tool in registered:
        server = _name(getattr(tool, "mcp_server_name", None))
        if "baidu" not in server.lower():
            continue
        original = getattr(tool, "mcp_tool", None)
        raw_name = _name(getattr(original, "name", None))
        name = _name(getattr(tool, "name", None))
        if not server or not name or not raw_name:
            continue
        if raw_name == "map_reverse_geocode": kind = "reverse_geocode"
        elif raw_name == "map_search_places": kind = "search_places"
        elif raw_name == "map_directions": kind = "directions"
        elif raw_name.startswith("map_") and any(word in raw_name.lower() for word in ("convert", "geoconv")): kind = "coordinate_convert"
        else: continue
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict): schema = getattr(original, "inputSchema", {})
        if not isinstance(schema, dict): schema = {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        valid_required = [k for k in required if _name(k)]
        # No endpoint, descriptions, default arguments or arbitrary schema text.
        item = {"name": name, "server": server, "kind": kind,
                "active": getattr(tool, "active", True) is True,
                "parameters": sorted(k for k in properties if _name(k)),
                "required": valid_required}
        if kind == "reverse_geocode":
            item["compatible"] = (len(valid_required) == len(required) and set(valid_required) <= {"latitude", "longitude"}
                                  and all(isinstance(properties.get(k), dict) and properties[k].get("type") == "number" for k in ("latitude", "longitude")))
        else:
            item["compatible"] = bool(properties)
        tools.append(item)
    reverse = [t for t in tools if t["kind"] == "reverse_geocode" and t["active"] and t["compatible"]]
    convert = [t for t in tools if t["kind"] == "coordinate_convert" and t["active"] and t["compatible"]]
    ambiguous = len(reverse) > 1
    status = ("ambiguous_tools" if ambiguous else "ready" if len(reverse) == 1
              else "not_configured" if not tools else "disabled" if not any(t["active"] for t in tools) else "incompatible")
    return {"status": status, "reason": "ambiguous_reverse_geocode_tools" if ambiguous else "no_registered_map_tools" if not tools else "",
            "tools": tools, "reverse_geocode_available": len(reverse) == 1,
            "coordinate_convert_available": bool(convert), "execution": "llm_handoff", "sent_to_map": False}


def map_handoff(location: dict, capabilities: dict) -> dict:
    result = {"status": "mcp_unavailable", "required_coord_type": "bd09ll", "sent_to_map": False,
              "next_step": None, "message": "未发现可衔接的百度地图 MCP 逆地理编码工具，请检查 AstrBot MCP 配置"}
    if location.get("is_mock") is True:
        result.update(status="mock_location", message="这是模拟位置，不作为真实当前位置交给地图工具")
    elif location.get("fresh") is False:
        result.update(status="location_stale", message="定位时间过期或无效，请重新获取位置")
    elif location.get("fresh") is None:
        result.update(status="location_time_unknown", message="手机未提供定位采集时间，请升级 App 后重新定位")
    elif location.get("coord_type") == "unknown":
        result.update(status="coordinate_type_unknown", message="定位来源未说明坐标系，不能直接交给百度地图")
    elif capabilities.get("reverse_geocode_available"):
        reverse = next(t for t in capabilities["tools"] if t["kind"] == "reverse_geocode" and t["active"] and t["compatible"])
        if location["coord_type"] == "bd09ll":
            result.update(status="ready", message="坐标可交给已注册的地图工具；本次尚未调用地图服务",
                          next_step={"tool": reverse["name"], "arguments": {"latitude": location["latitude"], "longitude": location["longitude"]}})
        else:
            converters = [t["name"] for t in capabilities["tools"] if t["kind"] == "coordinate_convert" and t["active"] and t["compatible"]]
            result.update(status="conversion_required", source_coord_type=location["coord_type"], conversion_tools=converters,
                          message="先使用已接入的坐标转换工具按其实际参数将原坐标转为 BD09，再调用地图查询；不要直接替换坐标系名称",
                          next_step={"tool": converters[0], "arguments": None, "action": "inspect_registered_schema"} if len(converters) == 1 else None)
    return result
