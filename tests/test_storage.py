"""Persistence failures must preserve the last committed state."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from storage import read_jsonl_tail, write_json_atomic


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    def test_replace_failure_preserves_previous_state_and_cleans_temporary(self):
        write_json_atomic(self.path, {"value": "original"})
        with patch("storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_json_atomic(self.path, {"value": "new"})
        self.assertEqual(json.loads(self.path.read_text()), {"value": "original"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_state_does_not_replace_previous_file(self):
        write_json_atomic(self.path, {"value": "original"})
        with self.assertRaises(ValueError):
            write_json_atomic(self.path, {"value": float("nan")})
        self.assertEqual(json.loads(self.path.read_text()), {"value": "original"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_tail_handles_unicode_corruption_and_no_final_newline(self):
        self.path.write_bytes(b"broken\n" + json.dumps({"text": "中文"}, ensure_ascii=False).encode() + b'\n[]\n{"id":2}')
        self.assertEqual(read_jsonl_tail(self.path, 3), [{"text": "中文"}, {"id": 2}])
        self.assertEqual(read_jsonl_tail(self.path, 1), [{"id": 2}])

    def test_tail_respects_read_budget_and_drops_partial_records(self):
        self.path.write_text('x' * 100000 + '\n{"old":1}\n{"last":2}\n', encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read whole file")):
            self.assertEqual(read_jsonl_tail(self.path, 10, max_bytes=16), [{"last": 2}])

    def test_empty_missing_and_zero_limit(self):
        self.assertEqual(read_jsonl_tail(self.path, 10), [])
        self.path.write_bytes(b"")
        self.assertEqual(read_jsonl_tail(self.path, 10), [])
        self.assertEqual(read_jsonl_tail(self.path, 0), [])
