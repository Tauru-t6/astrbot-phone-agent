# AstrBot Phone Agent

AstrBot plugin with two phone-control modes:

- `app`: Tauru Phone Agent App, Shizuku, and deterministic structured commands.
- `operit`: OperitAI HTTP for natural-language UI tasks.

Use `app` for the companion experience, reminders, timeline, status, screenshots, and allowlisted device actions. Use `operit` when a task needs OperitAI to inspect, click, type, and reason about a screen.

The app direct route is ordered home LAN first, then Tailscale, then relay. Example:

```
http://192.168.2.110:8260,http://PHONE_TAILSCALE_IP:8260
```

Android app: https://github.com/Tauru-t6/astrbot-phone-agent-app

Install:

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

Set `control_backend` to `app` or `operit`. Configure tokens and addresses in AstrBot; this repository contains no deployment credentials.

Run tests:

```bash
python -m pytest tests -q
python -m py_compile main.py device_app.py
```

