"""Opt-in reusable hints made from verified node actions and postconditions.

A recipe never carries coordinates, observation tokens, grants, or input text.
Each hint must resolve to exactly one node on the current screen; the ordinary
task policy still decides whether that freshly resolved action is allowed.
"""
from __future__ import annotations

import re

TOKEN = re.compile(r"\{\{input:(\d)\}\}")
SELECTOR_KEYS = frozenset({"resource_id", "class_name", "text", "description", "clickable", "editable", "focused", "scrollable"})
FLAGS = frozenset({"clickable", "editable", "focused", "scrollable"})


class RecipeError(ValueError):
    pass


def _encode(value, allowed_texts):
    if "{{input:" in value:
        raise RecipeError("ui_recipe_literal_conflict")
    literals = {literal: index for index, literal in reversed(list(enumerate(allowed_texts)))}
    if not literals: return value
    pattern = re.compile("|".join(re.escape(literal) for literal in sorted(literals, key=len, reverse=True)))
    return pattern.sub(lambda match: "{{input:" + str(literals[match.group()]) + "}}", value)


def _expand(value, allowed_texts):
    def substitute(match):
        index = int(match.group(1))
        if index >= len(allowed_texts):
            raise RecipeError("ui_recipe_parameter_missing")
        return allowed_texts[index]
    return TOKEN.sub(substitute, value)


def selector_for(node, allowed_texts, *, include_text=True):
    if node.get("source") == "ocr" or not node.get("enabled") or node.get("password"):
        return None
    row = {key: node.get(key, "") for key in ("resource_id", "class_name") if node.get(key)}
    # Empty editors often carry suggested input from a prior task; require a
    # stable resource identifier and match focus, never retain that old value.
    if node.get("editable"):
        if not row.get("resource_id"):
            return None
        row.update(editable=True, focused=node.get("focused", False))
    elif include_text:
        for key in ("text", "description"):
            if node.get(key):
                row[key] = _encode(node[key], allowed_texts)
    for key in ("clickable", "scrollable"):
        if node.get(key): row[key] = True
    if not row.get("resource_id") and not row.get("text") and not row.get("description"):
        return None
    return row


def match_selector(selector, snapshot, allowed_texts):
    if not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_KEYS:
        return None
    matches = []
    for node in snapshot["nodes"]:
        if node.get("source") == "ocr" or not node.get("enabled") or node.get("password"):
            continue
        if all(node.get(key, False if key in FLAGS else "") == (value if key in FLAGS else _expand(value, allowed_texts))
               for key, value in selector.items()):
            matches.append(node)
    return matches[0] if len(matches) == 1 else None


def validate_recipe(raw, package):
    if raw is None: return None
    if (not isinstance(raw, dict) or set(raw) != {"version", "package", "steps"}
            or type(raw["version"]) is not int or raw["version"] != 1 or raw["package"] != package
            or not isinstance(raw["steps"], list) or not 1 <= len(raw["steps"]) <= 12):
        raise RecipeError("ui_recipe_invalid")
    for step in raw["steps"]:
        if (not isinstance(step, dict) or set(step) - {"action", "selector", "parameter_index", "direction", "before", "postcondition"}
                or not isinstance(step.get("action"), str) or step.get("action") not in {"tap", "input_text", "scroll", "back"}):
            raise RecipeError("ui_recipe_invalid")
        selectors = ([step.get("selector")] if step["action"] != "back" else step.get("before", []))
        post = step.get("postcondition")
        if not isinstance(post, dict) or set(post) != {"selectors"} or not isinstance(post["selectors"], list) or not 1 <= len(post["selectors"]) <= 3:
            raise RecipeError("ui_recipe_invalid")
        if not isinstance(selectors, list) or not selectors or len(selectors) > 3:
            raise RecipeError("ui_recipe_invalid")
        for selector in selectors + post["selectors"]:
            if not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_KEYS:
                raise RecipeError("ui_recipe_invalid")
            for key, value in selector.items():
                if key in FLAGS:
                    if type(value) is not bool: raise RecipeError("ui_recipe_invalid")
                elif not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ord(char) < 32 for char in value):
                    raise RecipeError("ui_recipe_invalid")
        if step["action"] == "input_text" and (type(step.get("parameter_index")) is not int or not 0 <= step["parameter_index"] < 5):
            raise RecipeError("ui_recipe_invalid")
        if step["action"] == "scroll" and (not isinstance(step.get("direction"), str) or step.get("direction") not in {"up", "down"}):
            raise RecipeError("ui_recipe_invalid")
    return raw


def recipe_decision(step, snapshot, allowed_texts):
    if snapshot.get("source") == "ocr": return None
    action = step["action"]
    if action == "back":
        return {"action": "back"} if all(match_selector(row, snapshot, allowed_texts) for row in step["before"]) else None
    node = match_selector(step["selector"], snapshot, allowed_texts)
    if node is None: return None
    decision = {"action": action, "node_id": node["id"]}
    if action == "input_text":
        index = step["parameter_index"]
        if index >= len(allowed_texts): return None
        decision["text"] = allowed_texts[index]
    if action == "scroll": decision["direction"] = step["direction"]
    return decision


def postcondition_matches(step, snapshot, allowed_texts):
    return all(match_selector(row, snapshot, allowed_texts) for row in step["postcondition"]["selectors"])


def learn_step(decision, before, after, allowed_texts):
    if before.get("source") == "ocr" or after.get("source") == "ocr": return None
    action = decision["action"]
    result = {"action": action}
    if action == "back":
        anchors = [selector_for(node, allowed_texts) for node in before["nodes"] if node.get("resource_id")]
        anchors = [row for row in anchors if row and match_selector(row, before, allowed_texts)][:3]
        if len(anchors) < 2: return None
        result["before"] = anchors
    else:
        node = next((node for node in before["nodes"] if node["id"] == decision["node_id"]), None)
        selector = selector_for(node, allowed_texts) if node else None
        if selector is None or match_selector(selector, before, allowed_texts) is None: return None
        result["selector"] = selector
    if action == "input_text":
        result["parameter_index"] = allowed_texts.index(decision["text"])
        nodes = [node for node in after["nodes"] if node.get("editable") and node.get("focused") and node.get("text") == decision["text"]]
        if len(nodes) != 1: return None
        post = selector_for(nodes[0], allowed_texts)
        if post is None: return None
        post["text"] = _encode(decision["text"], allowed_texts)
        posts = [post]
    else:
        if action == "scroll": result["direction"] = decision["direction"]
        # Prefer a newly focused editor, then one newly visible stable control.
        ordered = sorted(after["nodes"], key=lambda node: not (node.get("editable") and node.get("focused")))
        posts = []
        for node in ordered:
            selector = selector_for(node, allowed_texts)
            if selector and match_selector(selector, after, allowed_texts) and not match_selector(selector, before, allowed_texts):
                posts.append(selector)
                break
        if not posts: return None
    result["postcondition"] = {"selectors": posts}
    return result
