# AstrBot Phone Agent

[中文](README.md) · [App API v2](docs/APP_API_V2.md)

An AstrBot plugin with two Android control modes:

| Mode | Android endpoint | Use case |
| --- | --- | --- |
| `app` | AstrBot Phone Agent App + Shizuku | Structured actions, status, reminders and timeline |
| `operit` | OperitAI External HTTP | Natural-language UI tasks, screen reasoning, taps and text input |

Android frontend: [AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app). Chat in the App uses AstrBot's chat API; this plugin supplies phone tools and reminder synchronization.

## Install

Requires AstrBot 4.22+. Install the repository through the plugin dashboard, or:

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

Reload the plugin or restart AstrBot. Runtime dependencies are supplied by AstrBot.

## App mode

Install the App and authorize it in Shizuku. Replace these placeholder addresses:

```json
{
  "enabled": true,
  "control_backend": "app",
  "app_shared_token": "same-random-command-token-on-both-sides",
  "app_direct_urls": "http://phone-lan.example:8260,http://phone-tailnet.example:8260",
  "allowed_user_ids": "your-AstrBot-user-ID",
  "enable_observe_tool": true,
  "enable_reminder_tools": true,
  "enable_audit_tool": true
}
```

Configured direct URLs are ordered and precede registered URLs. Put LAN before Tailscale. Each endpoint is health-checked for up to three seconds; all direct attempts and relay share one command timeout budget. Matching device denials are final. Transport retries reuse the same command ID, which the new App deduplicates.

`phone_action` sends structured actions such as open/close app, home/back, lock/wake directly to Android; it does not fall through to ADB. Arbitrary taps, swipes, typing and workflows require Operit. The underlying command API also supports screenshots, screen text, location, usage, app restrictions, notifications and ping when permissions allow. `phone_observe` currently reads device status in App mode, rather than general screen reasoning.

The command token is distinct from AstrBot dashboard authentication used by plugin routes and from chat API authentication. Configure the appropriate credentials separately in App settings.

## Operit mode

Set `control_backend=operit`, `operit_base_url=http://phone-tailnet.example:8094`, `operit_token`, and `allowed_user_ids`. OperitAI must have its model, tool calling, Shizuku and External HTTP configured. `operit_task` handles natural-language work in this mode and is rejected in App mode.

Legacy aliases `phone_buddy`, `native_app`, `own_frontend`, `operitai` and `operit_ai` are normalized. `adb` remains available for old diagnostic configurations but is not a primary console choice.

## Reminders and state

`GET /app/state` returns features, pending reminders, recent command results and the latest 100 timeline events without probing the phone. App reminders support durations of 1 minute through 7 days, cancellation and UUID idempotency. Pending reminders, receipts and timeline use atomic persistent files.

When due, the plugin persists a `reminder_due` event before removing the pending reminder. The App polls and deduplicates `notification_id`; App-created reminders do not send QQ messages or trigger a second direct phone push. Chat-created reminders retain delivery to their originating chat. After restart, overdue reminders within 24 hours catch up; older reminders expire quietly. Completed request receipts are retained for 30 days.

Phone notification timing depends on connectivity, background polling and notification permission; this is not an exact system alarm. `reminders_path` stores reminder text and receipts; `app_state_path` stores the timeline. Keep these personal-data files private.

## Optional tools

Feature flags default off: `enable_observe_tool`, `enable_usage_tool`, `enable_reminder_tools`, `enable_location_tool`, `enable_policy_tools`, `enable_audit_tool`, and `enable_health_tools`. Configure the allowed user IDs and required phone permissions before enabling actions. Xiaomi health support reads a separately maintained health database.

## Console and relay

The Phone Control page selects App/Operit mode, shows backend-specific fields, tests the selected connection, and lists reminders, app policies, Operit tasks and audit metadata. Empty token fields preserve configured values; tokens are never echoed.

The optional service in `deploy/relay/` is enabled with `relay_base_url` and `relay_token`. It requires phone polling. A queued task can execute after the plugin's wait times out; a timeout is never reported as success.

## Development

```bash
python -m pip install quart pytest
python -m pytest tests -q
python -m py_compile main.py device_app.py
```

Tests use actual Quart requests, temporary files and stubbed AstrBot host interfaces. Network access, Shizuku permissions and Android notification behavior still require device validation.
