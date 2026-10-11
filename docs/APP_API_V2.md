# AstrBot Phone Agent App API v2

This document describes the app-facing dashboard routes. The command envelope
sent to Android remains schema version 1 and is unchanged.

The routes are registered below the plugin prefix
`/astrbot_plugin_phone_agent`. AstrBot's dashboard authentication protects the
routes; write routes additionally reject a different `Origin`. They do not
perform an Android health probe unless explicitly requested by a device route.

## State

`GET /app/state` returns immediately from plugin memory and disk:

```json
{
  "success": true,
  "api_version": 2,
  "server_time": 1760000000.123,
  "backend": "app",
  "features": {"reminders": true, "audit": true},
  "reminders": [{"id":"app-...","text":"喝水","due":1760000060,"source":"app","status":"pending"}],
  "recent_commands": [],
  "timeline": [{"id":"tl-...","type":"reminder","title":"提醒","body":"喝水","related_task_id":null,"timestamp":1760000000.123,"event":"reminder_due","notify":true,"notification_id":"reminder-app-...","reminder_id":"app-..."}]
}
```

`timeline` contains the latest 100 events. `notify` is true only for a
reminder due event. The app polls this list and deduplicates by
`notification_id`; the plugin does not push an app reminder directly.

## App reminders

`POST /app/reminders` accepts exactly the following useful fields:

```json
{"text":"喝水","minutes":15,"request_id":"550e8400-e29b-41d4-a716-446655440000"}
```

`minutes` is an integer from 1 through 10080. `request_id` is a UUID and is
idempotent. Repeating it with the same text and duration returns the original
reminder; changing either field returns HTTP 409. NaN, Infinity, invalid JSON,
booleans, fractional numbers and invalid parameter values are rejected. Unknown fields are ignored.

The response is `{ "success": true, "reminder": { ... } }`, where the reminder
has `id`, `text`, `due`, `source: "app"`, and `status: "pending"`.

`POST /app/reminders/cancel` accepts `{"reminder_id":"app-..."}` and returns
`status: "cancelled"`. A reminder already fired, expired, or cancelled returns
`status: "already_finished"` so cancellation is safe to retry.

App reminders are stored with an empty chat session. On due, the plugin first
atomically persists a timeline event, then removes the pending reminder. Recent
overdue reminders are recovered after a restart; reminders older than 24 hours
produce a non-notifying `reminder_expired` event. Chat-created reminders keep
their old QQ delivery and also write the timeline event.

## Command set

The envelope is schema version 1. `device_app.py` `COMMAND_ACTIONS` and the
app's `COMMAND_ACTIONS` in `Protocol.kt` are the same closed set; a command
outside it is refused on both sides.

| Group | Actions |
| --- | --- |
| Device | `status`, `ping`, `foreground_app` |
| Apps | `open_app`, `close_app`, `suspend_app`, `unsuspend_app`, `suspend_video_apps`, `unsuspend_video_apps` |
| Keys | `back`, `home`, `lock_screen`, `wake_screen`, `key_event` |
| Screen read | `screen_text`, `screenshot`, `usage_stats` |
| UI automation | `tap`, `long_press`, `swipe`, `scroll`, `input_text` |
| Other | `location`, `send_notification` |

UI automation brings App mode to parity with the OperitAI backend. Arguments:

- `tap`: `x`, `y` (0..10000)
- `long_press`: `x`, `y`, optional `duration_ms` (100..10000, default 500)
- `swipe`: `x1`, `y1`, `x2`, `y2`, optional `duration_ms` (50..10000, default 300)
- `scroll`: `direction` = `up` | `down` | `left` | `right`, optional `duration_ms`
- `input_text`: `text`, 1..500 characters, no control characters
- `key_event`: `key_code` from a fixed allowlist

Out-of-range or malformed arguments fail with `error_code: bad_args` and are
never interpolated into the shell. Text is single-quote escaped before it
reaches `sh -c`.

### Confirmation

Actions in `DANGEROUS_ACTIONS` change device state or expose screen content, so
the app shows an on-device dialog before running them. That now includes
`screen_text`, `screenshot`, and every UI-automation action. The user can opt
individual actions out in the app under 纸笺 → 隐私与自动化; the default
pre-approves only `screenshot`, which is delivered to the user's own
conversation.

## Screenshots

`screenshot` returns a downscaled JPEG inside the result envelope rather than
writing to the gallery:

```json
{"format":"jpeg","width":1080,"height":1920,"bytes":184320,"data_base64":"..."}
```

The app scales the longest edge to 1600 px and encodes JPEG at quality 75, and
refuses anything above 2.2 MB. The plugin decodes it to a temporary file and
sends it into the conversation that asked. The `phone_screenshot` tool is the
LLM entry point and requires `enable_screenshot_tool`.

## Forwarded notifications

`POST /app/notifications` accepts a batch from the app's notification listener:

```json
{"notifications":[{"key":"0|com.example|1|null|10001","package":"com.example",
  "title":"标题","text":"正文","posted_at":1760000000.0}]}
```

`key` is idempotent; a repeated key is ignored. The store keeps the latest 200
entries and requires `enable_notification_tool`. `GET /app/notifications`
returns them, and the `phone_notifications` tool exposes them to the LLM.
Notification text is personal data: exclude `phone_agent_notifications.json`
from backups you publish.

## Direct transport

`app_direct_urls` is a comma-separated ordered list. Configured URLs are tried
before URLs announced by app registration, so an old Tailscale-only registration
cannot displace the preferred LAN URL. Each candidate gets a three-second
health probe and shares one command timeout budget. A result must match
`type=result`, schema version, command ID and action. The `output` field may be
a JSON encoded string; when it contains an object or array it is decoded before
being returned to tools. A matching `success:false` result is final and is not
replayed through another endpoint or relay. Only transport failures try the
next URL or relay, reusing the same command ID.

