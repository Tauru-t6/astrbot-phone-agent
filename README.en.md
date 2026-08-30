# AstrBot Phone Agent

Control an Android phone from AstrBot through Tailscale and Operit. Operit owns screenshot/OCR, UI interaction, text input, and Shizuku-backed actions. AstrBot interprets natural language and delegates tasks.

## Features

- `operit_task`: delegate natural-language tasks to the phone Operit agent, such as opening apps, finding contacts, tapping buttons, and entering text.
- `phone_action`: allowlisted phone actions including app start/stop, back, home, screen lock, and app suspend/restore.
- `phone_observe`: inspect the foreground app, visible UI text, battery, and task state.
- `phone_sleep_mode`: start a temporary video-app restriction, for example “do not let me watch videos for two hours”.
- Sleep guard: check configured video apps during quiet hours and suspend them through Shizuku, then restore them afterward.
- `phone_health`: query Xiaomi band data synchronized from Xiaomi Fitness, including steps, sleep, heart rate, SpO2, and activity.
- `phone_usage`: query app usage time through Operit.
- `operit_task_status` / `operit_task_cancel` / `operit_task_retry`: manage background Operit tasks.
- `phone_reminder`: create, list, and cancel reminders for the current chat; reminders are persisted.
- `phone_audit`: read action metadata without tokens or message bodies.
- App aliases: say “Bilibili” or “WeChat” instead of remembering package names; extend them with `app_aliases_json`.
- Confirmation boundary: sending messages, comments, likes, deletes, and payments require explicit confirmation.
- ADB is an optional diagnostic backend only. Operit is the default control backend, so wireless ADB is not required for routine control.

## Installation

Place the plugin directory under the AstrBot data directory:

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

In Operit, open Settings -> Data and permissions -> External HTTP calls. Copy the displayed address and Bearer Token into the plugin configuration:

```json
{
  "control_backend": "operit",
  "operit_base_url": "http://PHONE_TAILSCALE_IP:8094",
  "operit_token": "YOUR_OPERIT_TOKEN"
}
```

Keep the Operit HTTP service reachable only through Tailscale or another trusted network. Do not expose it directly to the public Internet.

Set `background=true` to receive an Operit task ID, then use the status, cancel, or retry tools. Example: “remind me to drink water in 30 minutes”.

## Authorization

By default, authorization uses this plugin's own `allowed_user_ids` allowlist. `astrbot_plugin_private_companion` is not a required dependency and is disabled as an authorization source by default. See [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md) only when you explicitly want to opt in.

## Used projects

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [Operit](https://github.com/AAswordman/Operit)
- [Shizuku](https://github.com/RikkaApps/Shizuku)
- [Tailscale](https://github.com/tailscale/tailscale)
- [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)
- [mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge)
- [mi_fitness](https://pypi.org/project/mi-fitness/)

This repository contains no tokens, passwords, server configuration, or Private Companion source code.
