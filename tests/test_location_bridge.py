"""Location truthfulness, MCP metadata boundaries and on-demand route behavior."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("phone_location_bridge_test", ROOT / "location_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def fix(**updates):
    return {"latitude": 39.9, "longitude": 116.4, "coord_type": "wgs84", "captured_at": time.time(), **updates}


def tool(name="map_reverse_geocode", active=True, **updates):
    schema = {"type": "object", "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}}, "required": ["latitude", "longitude"]}
    return SimpleNamespace(name=name, mcp_server_name="baidu-map", mcp_tool=SimpleNamespace(name=name),
                           parameters=updates.get("parameters", schema), active=active,
                           mcp_client=SimpleNamespace(api_key="private-secret"))

def other_map_tool():
    value = tool()
    value.mcp_server_name = "other-maps"
    return value


def context(*tools):
    return SimpleNamespace(get_llm_tool_manager=lambda: SimpleNamespace(func_list=list(tools)))


class LocationContractTests(unittest.TestCase):
    def test_coordinates_are_not_relabelled_or_shifted_from_geographical_bounds(self):
        for value in [fix(), fix(coord_type="gcj02"), fix(coord_type="bd09ll"), fix(coord_type="unknown")]:
            result = bridge.normalize_location(value, "operit")
            self.assertEqual(result["location"]["latitude"], value["latitude"])
            self.assertEqual(result["location"]["longitude"], value["longitude"])
            self.assertEqual(result["location"]["coord_type"], value["coord_type"])
            self.assertNotIn("bd09_latitude", result["location"])

    def test_failed_boolean_nonfinite_and_out_of_bounds_fixes_are_rejected(self):
        for values in [{"success": False}, {"success": "true"}, {"latitude": True}, {"latitude": float("nan")},
                       {"longitude": float("inf")}, {"latitude": 91}, {"longitude": "116.4"},
                       {"latitude": 10 ** 400}, {"longitude": -(10 ** 400)}]:
            with self.subTest(values=values): self.assertFalse(bridge.normalize_location(fix(**values), "app")["success"])

    def test_old_and_undated_fixes_are_not_marked_current(self):
        for stamp, expected in [(time.time() - 121, False), (None, None), (time.time() + 301, False)]:
            value = fix(captured_at=stamp)
            if stamp is None: value.pop("captured_at")
            result = bridge.normalize_location(value, "app")
            self.assertIs(result["location"]["fresh"], expected)
        result = bridge.normalize_location(fix(age_seconds=140), "app")
        self.assertFalse(result["location"]["fresh"])

    def test_known_zero_accuracy_is_preserved_but_missing_accuracy_is_unknown(self):
        self.assertEqual(bridge.normalize_location(fix(accuracy_m=0.0), "app")["location"]["accuracy_m"], 0)
        self.assertNotIn("accuracy_m", bridge.normalize_location(fix(), "app")["location"])

    def test_explicit_address_provider_status_is_preserved_without_fabricating_address(self):
        result = bridge.normalize_location(fix(address_status="not_supported"), "app")["location"]
        self.assertEqual(result["address_status"], "not_supported")
        self.assertNotIn("address", result)
        self.assertEqual(bridge.normalize_location(fix(address="Example", address_status="failed"), "app")["location"]["address_status"], "available")

    def test_registry_diagnostics_do_not_call_tools_or_expose_secrets_and_descriptions(self):
        fake = tool()
        fake.parameters["properties"]["latitude"]["description"] = "private-secret"
        fake.parameters["properties"]["latitude"]["default"] = "private-secret"
        result = bridge.map_capabilities(context(fake))
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertFalse(result["sent_to_map"])
        self.assertEqual(bridge.map_capabilities(context(tool(active=False)))["status"], "disabled")
        self.assertEqual(bridge.map_capabilities(context())["status"], "not_configured")
        self.assertEqual(bridge.map_capabilities(SimpleNamespace())["status"], "registry_unavailable")

    def test_incompatible_or_duplicate_reverse_tools_never_produce_direct_arguments(self):
        fake = tool(parameters={"properties": {"location": {"type": "string"}}, "required": ["location"]})
        for registry in [context(fake), context(tool(), tool())]:
            caps = bridge.map_capabilities(registry)
            self.assertFalse(caps["reverse_geocode_available"])
            handoff = bridge.map_handoff(bridge.normalize_location(fix(coord_type="bd09ll"), "app")["location"], caps)
            self.assertIsNone(handoff["next_step"])

    def test_only_baidu_registered_tools_are_reported_and_duplicate_baidu_is_ambiguous(self):
        unrelated = bridge.map_capabilities(context(other_map_tool()))
        self.assertEqual(unrelated["status"], "not_configured")
        mixed = bridge.map_capabilities(context(other_map_tool(), tool()))
        self.assertEqual(mixed["status"], "ready")
        duplicate = bridge.map_capabilities(context(tool(), tool()))
        self.assertEqual(duplicate["status"], "ambiguous_tools")
        self.assertIsNone(bridge.map_handoff(bridge.normalize_location(fix(coord_type="bd09ll"), "app")["location"], duplicate)["next_step"])

    def test_wgs84_and_gcj02_require_real_conversion_without_fabricated_tool_names(self):
        caps = bridge.map_capabilities(context(tool()))
        for kind in ["wgs84", "gcj02"]:
            result = bridge.map_handoff(bridge.normalize_location(fix(coord_type=kind), "app")["location"], caps)
            self.assertEqual(result["status"], "conversion_required")
            self.assertEqual(result["conversion_tools"], [])
            self.assertIsNone(result["next_step"])
            self.assertFalse(result["sent_to_map"])

    def test_bd09_handoff_preserves_argument_order_and_mock_stale_unknown_are_blocked(self):
        caps = bridge.map_capabilities(context(tool()))
        result = bridge.map_handoff(bridge.normalize_location(fix(coord_type="bd09"), "app")["location"], caps)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["next_step"]["arguments"], {"latitude": 39.9, "longitude": 116.4})
        for changes, expected in [({"is_mock": True}, "mock_location"), ({"captured_at": 100}, "location_stale"), ({"coord_type": "unknown"}, "coordinate_type_unknown")]:
            result = bridge.map_handoff(bridge.normalize_location(fix(**changes), "app")["location"], caps)
            self.assertEqual(result["status"], expected)
            self.assertIsNone(result["next_step"])


class LocationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture_spec = importlib.util.spec_from_file_location("location_api_fixture", ROOT / "tests/test_app_api.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        self.module = fixture.module
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"enabled": True, "control_backend": "app", "enable_location_tool": True, "enable_autonomy_scheduler": False}
        for key in ("reminders_path", "app_state_path", "audit_log_path", "tasks_path", "policy_state_path", "notification_path", "task_runs_path", "night_guard_state_path", "health_connect_path"):
            self.config[key] = str(Path(self.temp.name) / (key + ".json"))
        from quart import Quart
        self.app = Quart("location-api-" + self.temp.name)
        ctx = context(tool())
        ctx.send_message = None
        ctx.register_web_api = lambda path, handler, methods, desc: self.app.add_url_rule(path, str(len(self.app.view_functions)), handler, methods=methods)
        with patch.object(self.module.PhoneAgentPlugin, "_warm_device_route"):
            self.plugin = self.module.PhoneAgentPlugin(ctx, self.config)
        self.client = self.app.test_client()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_map_status_never_requests_location(self):
        with patch.object(self.plugin.device_app, "execute", side_effect=AssertionError("must not call device")):
            response = await self.client.get("/astrbot_plugin_phone_agent/map/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await response.get_json())["map"]["status"], "ready")

    async def test_app_location_response_has_the_same_contract_as_operit(self):
        value = fix()
        with patch.object(self.plugin.device_app, "execute", return_value={"success": True, "output": value}) as execute:
            response = await self.client.get("/astrbot_plugin_phone_agent/location?timeout=7")
        data = await response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["location"]["coord_type"], "wgs84")
        self.assertEqual(data["map_handoff"]["status"], "conversion_required")
        self.assertEqual(execute.call_args.args[1]["timeout_seconds"], 7)
        operit = self.plugin._parse_location_result({"success": True, "ai_response": json.dumps(value)})
        self.assertEqual(operit["location"]["latitude"], data["location"]["latitude"])
        self.assertEqual(operit["map_handoff"]["status"], data["map_handoff"]["status"])

    async def test_operit_cannot_hide_a_failed_fix_inside_successful_chat(self):
        result = self.plugin._parse_location_result({"success": True, "ai_response": json.dumps(fix(success=False))})
        self.assertFalse(result["success"])
        self.assertNotIn("map_handoff", result)
