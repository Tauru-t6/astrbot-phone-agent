# Contributing

Issues and pull requests are welcome.

Before opening a pull request:

1. Keep the plugin independent of `astrbot_plugin_private_companion` unless changing the documented optional extension.
2. Do not commit Operit tokens, Xiaomi tokens, SSH credentials, phone addresses, or local AstrBot configuration.
3. Keep phone operations allowlisted and avoid adding an arbitrary shell path.
4. Run `python -m py_compile main.py` before submitting.
5. Update both `README.zh-CN.md` and `README.en.md` when changing user-facing setup or behavior.

Please describe the Android version, Operit version, Shizuku mode, and AstrBot version when reporting a device-specific issue. Redact tokens and personal message content from logs.
