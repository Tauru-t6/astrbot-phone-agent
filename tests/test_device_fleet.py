"""Device selection must not cross personal/Bot transport or persisted state."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
package = types.ModuleType("fleet_backend_test")
package.__path__ = [str(ROOT)]
api = types.ModuleType("astrbot.api")
api.logger = Mock()
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                             "fleet_backend_test": package}):
    fleet_module = importlib.import_module("fleet_backend_test.device_fleet")
    app = importlib.import_module("fleet_backend_test.device_app")


class DeviceFleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"app_shared_token": "synthetic-token",
                       "app_state_path": str(Path(self.temp.name) / "personal.json"),
                       "app_direct_urls": "http://personal-configured:8260"}
        self.audit = Mock()
        self.primary, self.fleet = self.new_fleet()

    def new_fleet(self):
        primary = app.DeviceAppBackend(self.config, self.audit)
        return primary, fleet_module.DeviceFleet(self.config, primary, self.audit)

    def register(self, device_id="personal-1", kind=None, **fields):
        payload = {"device_id": device_id, "base_url": "http://" + device_id + ":8260",
                   "app_version": "0.4.0", **fields}
        if kind is not None:
            payload["device_kind"] = kind
        return self.fleet.register(payload)

    def assert_registered(self, device_id="personal-1", kind=None, **fields):
        result = self.register(device_id, kind, **fields)
        self.assertTrue(result["success"], result)
        return result

    def test_legacy_registration_is_personal_and_keeps_original_backend(self):
        result = self.assert_registered()
        self.assertEqual(result["device_role"], "personal")
        self.assertEqual(result["device_kind"], "personal")
        self.assertEqual(result["personal_device_id"], "personal-1")
        self.assertIs(self.fleet.backend("personal-1"), self.primary)
        self.assertIs(self.fleet.backend(), self.primary)

    def test_bot_can_register_first_and_never_claims_personal_backend(self):
        result = self.assert_registered("bot-1", "bot")
        self.assertEqual(result["device_role"], "bot")
        self.assertEqual(result["personal_device_id"], "")
        self.assertEqual(self.primary.binding(), ("", 0))
        self.assertIsNot(self.fleet.backend("bot-1"), self.primary)
        self.assertIs(self.fleet.backend(), self.primary)
        self.assert_registered()
        self.assertEqual(self.fleet.primary_id, "personal-1")
        self.assertEqual(self.fleet.role("bot-1"), "bot")

    def test_bot_only_registry_survives_restart_offline_until_reregistered(self):
        self.assert_registered("bot-1", "bot")
        self.fleet.assign("bot-1", "bot", "专用设备")
        primary, restarted = self.new_fleet()
        self.assertEqual(restarted.primary_id, "")
        self.assertEqual(restarted.role("bot-1"), "bot")
        self.assertIsNone(restarted.backend("bot-1"))
        self.assertEqual(restarted.public(), [{"device_id": "bot-1", "label": "专用设备",
                         "role": "bot", "device_kind": "bot", "online": False, "app_version": ""}])
        self.assertTrue(restarted.register({"device_id": "bot-1", "device_kind": "bot"})["success"])
        self.assertEqual(restarted.backend("bot-1").direct_urls(), [])
        self.assertEqual(primary.binding(), ("", 0))

    def test_second_personal_app_is_unassigned_and_cannot_replace_personal(self):
        self.assert_registered()
        result = self.assert_registered("personal-2", "personal")
        self.assertEqual(result["device_role"], "unassigned")
        self.assertEqual(self.primary.binding()[0], "personal-1")
        for device_id, role in [("personal-1", "bot"), ("personal-1", "unassigned"),
                                ("personal-2", "personal")]:
            with self.subTest(device_id=device_id, role=role), self.assertRaisesRegex(ValueError, "personal_device_pinned"):
                self.fleet.assign(device_id, role)

    def test_bot_app_cannot_take_personal_role_even_without_personal_device(self):
        self.assert_registered("bot-1", "bot")
        with self.assertRaisesRegex(ValueError, "bot_device_cannot_be_personal"):
            self.fleet.assign("bot-1", "personal")
        result = self.register("bot-1", "personal")
        self.assertEqual(result["error_code"], "device_kind_mismatch")
        self.assertEqual(self.fleet.primary_id, "")

    def test_reregister_cannot_change_app_kind_or_primary_identity(self):
        self.assert_registered()
        original = self.primary.registration()
        result = self.register(kind="bot")
        self.assertEqual(result["error_code"], "device_kind_mismatch")
        self.assertEqual(self.primary.registration(), original)
        self.assert_registered("bot-1", "bot")
        result = self.assert_registered("bot-1")
        self.assertEqual(result["device_kind"], "bot")

    def test_only_one_bot_role_and_explicit_role_switch_invalidates_revision(self):
        self.assert_registered("bot-1", "bot")
        self.assert_registered("bot-2", "bot")
        self.assertEqual(self.fleet.role("bot-2"), "unassigned")
        with self.assertRaisesRegex(ValueError, "bot_role_in_use"):
            self.fleet.assign("bot-2", "bot")
        self.fleet.assign("bot-1", "unassigned")
        self.fleet.assign("bot-2", "bot")
        self.assertEqual(self.fleet.revision("bot-1"), 2)
        self.assertEqual(self.fleet.revision("bot-2"), 2)
        self.fleet.assign("bot-2", "bot", "另一个 Bot")
        self.assertEqual(self.fleet.revision("bot-2"), 2)
        _, restarted = self.new_fleet()
        self.assertEqual(restarted.revision("bot-1"), 2)
        self.assertEqual(restarted.revision("bot-2"), 2)

    def test_bot_transport_never_inherits_personal_configured_routes(self):
        self.assert_registered()
        self.assert_registered("bot-1", "bot")
        bot = self.fleet.backend("bot-1")
        self.assertEqual(bot.direct_urls(), ["http://bot-1:8260"])
        self.assertIn("http://personal-configured:8260", self.primary.direct_urls())
        self.config["app_direct_urls"] = "http://another-personal-route:8260"
        self.assertEqual(bot.direct_urls(), ["http://bot-1:8260"])

    def test_bot_status_and_timeline_are_separate_and_durable(self):
        self.assert_registered()
        self.assert_registered("bot-1", "bot")
        bot = self.fleet.backend("bot-1")
        self.primary.record_device_status({"battery_percent": 11, "reported_at": time.time()})
        bot.record_device_status({"battery_percent": 82, "reported_at": time.time()})
        self.primary.timeline_append("reminder", "Personal event", "personal synthetic text")
        bot.timeline_append("task", "Bot event", "bot synthetic text")
        self.assertEqual(self.primary.last_status()["battery_percent"], 11)
        self.assertEqual(bot.last_status()["battery_percent"], 82)
        self.assertEqual([row["title"] for row in bot.timeline_since(0)], ["Bot event"])
        self.assertRegex(bot._timeline_path().name, r"^device_[0-9a-f]{24}_timeline\.json$")
        self.assertEqual(bot._timeline_path().parent, self.fleet.path.parent)
        primary, restarted = self.new_fleet()
        self.assertTrue(restarted.register({"device_id": "bot-1", "device_kind": "bot"})["success"])
        self.assertEqual([row["title"] for row in restarted.backend("bot-1").timeline_since(0)], ["Bot event"])
        self.assertEqual([row["title"] for row in primary.timeline_since(0)], ["Personal event"])

    def test_invalid_or_unknown_explicit_id_never_selects_primary(self):
        self.assert_registered()
        for value in ["unknown", "../personal-1", " personal-1", False, 0, [], {}, "a" * 65]:
            with self.subTest(value=value):
                self.assertIsNone(self.fleet.backend(value))
                self.assertEqual(self.fleet.role(value), "unassigned")
                self.assertEqual(self.fleet.revision(value), 0)

    def test_invalid_registration_does_not_create_registry_or_change_binding(self):
        cases = [None, [], {}, {"device_id": []}, {"device_id": "../phone"},
                 {"device_id": "phone", "device_kind": []}, {"device_id": "phone", "device_kind": "unknown"},
                 {"device_id": "phone", "base_url": "file:///synthetic"},
                 {"device_id": "phone", "token_fingerprint": "sha256:wrong"}]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(self.fleet.register(value)["success"])
                self.assertEqual(self.fleet.rows, {})
                self.assertEqual(self.primary.binding(), ("", 0))
                self.assertFalse(self.fleet.path.exists())

    def test_capacity_rejects_new_identity_but_allows_reregistration(self):
        self.assert_registered()
        for index in range(1, self.fleet.MAX_DEVICES):
            self.assert_registered("bot-" + str(index), "bot")
        self.assertEqual(self.register("extra", "bot")["error_code"], "device_limit")
        self.assert_registered("bot-1", "bot")
        self.assertEqual(len(self.fleet.rows), self.fleet.MAX_DEVICES)

    def test_failed_first_registry_write_never_publishes_backend_binding(self):
        with patch.object(self.fleet, "_save", side_effect=OSError("synthetic disk failure")):
            result = self.register()
        self.assertEqual(result["error_code"], "storage_unavailable")
        self.assertEqual(self.fleet.rows, {})
        self.assertEqual(self.fleet.backends, {})
        self.assertEqual(self.primary.binding(), ("", 0))
        self.audit.assert_not_called()

    def test_failed_reregistration_preserves_old_routes_status_generation_and_disk(self):
        self.assert_registered()
        self.primary.record_device_status({"battery_percent": 71, "reported_at": time.time()})
        before_binding = self.primary.binding()
        before_registration = self.primary.registration()
        before_status = self.primary.last_status()
        before_disk = self.fleet.path.read_bytes()
        with patch.object(self.fleet, "_save", side_effect=OSError("synthetic disk failure")):
            result = self.register(base_url="http://new-endpoint:8260")
        self.assertEqual(result["error_code"], "storage_unavailable")
        self.assertEqual(self.primary.binding(), before_binding)
        self.assertEqual(self.primary.registration(), before_registration)
        self.assertEqual(self.primary.last_status(), before_status)
        self.assertEqual(self.fleet.path.read_bytes(), before_disk)

    def test_failed_bot_registration_does_not_change_personal_or_publish_bot(self):
        self.assert_registered()
        original = self.primary.registration()
        with patch.object(self.fleet, "_save", side_effect=OSError("synthetic disk failure")):
            result = self.register("bot-1", "bot")
        self.assertEqual(result["error_code"], "storage_unavailable")
        self.assertEqual(self.primary.registration(), original)
        self.assertIsNone(self.fleet.backend("bot-1"))
        self.assertNotIn("bot-1", self.fleet.rows)

    def test_failed_role_save_does_not_revoke_previous_role_or_revision(self):
        self.assert_registered("bot-1", "bot")
        before_disk = self.fleet.path.read_bytes()
        with patch.object(self.fleet, "_save", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.fleet.assign("bot-1", "unassigned", "new label")
        self.assertEqual(self.fleet.role("bot-1"), "bot")
        self.assertEqual(self.fleet.revision("bot-1"), 1)
        self.assertEqual(self.fleet.path.read_bytes(), before_disk)

    def test_invalid_role_update_values_are_rejected_without_state_change(self):
        self.assert_registered("bot-1", "bot")
        before_disk = self.fleet.path.read_bytes()
        for role, label in [([], None), ("owner", None), ("bot", []), ("bot", "x" * 61),
                            ("bot", "multiline\nlabel"), ("bot", "\ud800")]:
            with self.subTest(role=role, label=repr(label)), self.assertRaises(ValueError):
                self.fleet.assign("bot-1", role, label)
        self.assertEqual(self.fleet.path.read_bytes(), before_disk)

    def test_corrupt_registry_blocks_registration_without_overwriting_file(self):
        bot = {"role": "bot", "device_kind": "bot", "label": "Bot", "revision": 1}
        cases = ["{", "[]", '{"devices": {}, "personal_device_id": "missing"}',
                 json.dumps({"devices": {"bot-1": bot, "bot-2": bot}, "personal_device_id": ""}),
                 json.dumps({"devices": {"bot-1": {**bot, "role": "personal"}}, "personal_device_id": "bot-1"}),
                 json.dumps({"devices": {"bot-1": {**bot, "revision": True}}, "personal_device_id": ""}),
                 json.dumps({"devices": {"bot-1": {**bot, "role": []}}, "personal_device_id": ""}),
                 json.dumps({"devices": {}, "personal_device_id": "", "version": 100})]
        for value in cases:
            with self.subTest(value=value):
                self.fleet.path.write_text(value, encoding="utf-8")
                primary, restarted = self.new_fleet()
                result = restarted.register({"device_id": "new-phone"})
                self.assertEqual(result["error_code"], "device_registry_invalid")
                self.assertIsNone(restarted.backend())
                self.assertEqual(restarted.public(), [])
                self.assertEqual(primary.binding(), ("", 0))
                self.assertEqual(self.fleet.path.read_text(encoding="utf-8"), value)

    def test_version_one_roles_migrate_without_assuming_bot_build_kind(self):
        self.fleet.path.write_text(json.dumps({"version": 1, "personal_device_id": "personal-1", "devices": {
            "personal-1": {"role": "personal", "label": "Mine", "revision": 3},
            "secondary": {"role": "bot", "label": "Secondary", "revision": 2},
        }}), encoding="utf-8")
        _, restarted = self.new_fleet()
        self.assertEqual(restarted.primary_id, "personal-1")
        self.assertEqual(restarted.revision("secondary"), 2)
        self.assertEqual(restarted.rows["secondary"]["device_kind"], "personal")
        self.assertTrue(restarted.register({"device_id": "secondary"})["success"])
        self.assertEqual(json.loads(self.fleet.path.read_text(encoding="utf-8"))["version"], 2)

    def test_direct_legacy_backend_registration_is_durably_adopted(self):
        self.primary.register({"device_id": "legacy"})
        self.assertIs(self.fleet.backend("legacy"), self.primary)
        self.assertEqual(self.fleet.role("legacy"), "personal")
        _, restarted = self.new_fleet()
        self.assertEqual(restarted.primary_id, "legacy")

    def test_direct_primary_rebind_is_not_silently_used_by_existing_selection(self):
        self.assert_registered()
        self.primary.register({"device_id": "different-identity"})
        self.assertIsNone(self.fleet.backend("personal-1"))
        self.assertIsNone(self.fleet.backend())
        self.assertEqual(self.register("bot-1", "bot")["error_code"], "device_binding_mismatch")

    def test_public_view_reports_fresh_status_without_exposing_endpoints(self):
        self.assert_registered()
        self.primary.record_device_status({"battery_percent": 23, "reported_at": time.time()})
        row = self.fleet.public()[0]
        self.assertTrue(row["online"])
        self.assertEqual(row["app_version"], "0.4.0")
        self.assertEqual(set(row), {"device_id", "label", "role", "device_kind", "online", "app_version"})


if __name__ == "__main__":
    unittest.main()
