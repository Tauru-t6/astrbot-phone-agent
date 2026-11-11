"""Enforce logger ownership across all shipped AstrBot runtime modules."""
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]


class PluginLoggerTests(unittest.TestCase):
    def test_runtime_modules_use_host_logger(self):
        # Standalone relay executables and test host shims do not run as
        # AstrBot modules. Keep this check scoped to plugin-owned runtime code.
        paths = list(ROOT.glob("*.py")) + list((ROOT / "companion-context").rglob("*.py"))
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                logging_imports = [node for node in ast.walk(tree)
                    if (isinstance(node, ast.Import) and any(alias.name == "logging" or alias.name.startswith("logging.") for alias in node.names))
                    or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "logging")]
                self.assertEqual(logging_imports, [], "Import logger from astrbot.api")
                uses_logger = any(isinstance(node, ast.Name) and node.id == "logger" and isinstance(node.ctx, ast.Load) for node in ast.walk(tree))
                if uses_logger:
                    self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "astrbot.api"
                        and any(alias.name == "logger" for alias in node.names) for node in ast.walk(tree)))

    def test_task_runner_uses_the_exact_host_logger(self):
        api = types.ModuleType("astrbot.api")
        api.logger = Mock()
        spec = importlib.util.spec_from_file_location("logger_convention_runner", ROOT / "task_runner.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api, spec.name: module}):
            spec.loader.exec_module(module)
        self.assertIs(module.logger, api.logger)


if __name__ == "__main__":
    unittest.main()
