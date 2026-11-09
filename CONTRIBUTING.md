# Contributing

Issues and pull requests are welcome.

Before opening a pull request:

1. Keep the plugin independent of `astrbot_plugin_private_companion` unless changing the documented optional extension.
2. Do not commit Operit tokens, Xiaomi tokens, SSH credentials, phone addresses, or local AstrBot configuration.
3. Keep phone operations allowlisted and avoid adding an arbitrary shell path.
4. Run the checks below before submitting.
5. Update both `README.zh-CN.md` and `README.en.md` when changing user-facing setup or behavior.

Please describe the Android version, Operit version, Shizuku mode, and AstrBot version when reporting a device-specific issue. Redact tokens and personal message content from logs.

Use an isolated environment (`python -m venv .venv`) and install test dependencies
with `python -m pip install quart pytest`. Run with that environment activated:

```bash
python -m py_compile main.py model_planning.py device_app.py task_runner.py care_scheduler.py storage.py http_transport.py companion-context/main.py companion-context/v2_adapter.py deploy/relay/relay_server.py
python -m pytest tests -q
node --test tests/test_dashboard.js
```

The dashboard tests use Node's built-in test runner (Node 22+) and no npm dependencies.
Python API tests stub the AstrBot host and never issue real phone commands.
