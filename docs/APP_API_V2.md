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
  "api_version": 3,
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

## State additions in `api_version: 3`

`GET /app/state` reports `api_version: 3` and carries these extra blocks:

| Field | Meaning |
| --- | --- |
| `night_guard` | `enabled`, `window` (`HH:MM-HH:MM`), `in_window`, `dry_run`, `packages` (resolved watch list) and `states` keyed by user (`phase`, `foreground_package`, `owned_suspended`, `enforcement_count`, `last_error`). |
| `sleep_mode` | `manual_active`, `until`, `suspended` — the on-demand "守夜" restriction, separate from the night guard. |
| `device_last_status` | Last accepted heartbeat: `battery_percent`, `charging`, `foreground_package`, `screen_interactive`, `shizuku_ready`, `app_version`, `foreground_debug`, `reported_at`, `received_at`. `reported_at` is the phone's own sampling time; a sample older than `night_guard_status_max_age_seconds` must be treated as stale. |
| `care` | Read-only snapshot written by the optional companion bridge: `mode`, `quiet_hours`, `in_quiet_hours`, `daily_limit`, `eval_daily_limit`, and `users[uid]` = `sent_today`, `remaining`, `eval_today`, `last_sent_at`, `recent[]` (`outcome`, `reason`, `detail`, `at`). `null` until that bridge installs. |
| `task_runs` | Latest five dry-run task records (see below). |
| `task_grants` | Active scoped grants. |

`care` is written by the companion plugin, not by the phone plugin; a missing
block means "unknown", never "disabled".

## Task dry-run

`POST /app/task/plan` accepts:

```json
{"goal":"帮我把B站暂停一会儿","subject":"1000000001","grant_id":"optional-hex"}
```

- Requires a registered device (`409 no_device` when there is none) and the
  `observe_tool` feature.
- Plans the goal with a deterministic rule set, **observes for real** from the
  heartbeat cache, and **simulates** every write step. No command reaches the
  phone.
- Each simulated step carries
  `verify = {would_execute, grant_ok, reason, dry_run}`, where `reason` is one
  of `grant_missing`, `grant_expired`, `grant_subject_mismatch`,
  `grant_device_mismatch`, `grant_action_not_allowed`, `grant_package_required`
  or `grant_package_not_allowed`.
- `run.status` is `done` or `failed` (`observation_stale` when the cached
  observation is too old); `run.summary` is
  `{dry_run, planned, observed, simulated, blocked, failed}`.

The response is `{ "success": true, "run": { ... } }`. Runs are persisted under
`task_runs_path` with a capped history and also appear in `/app/state`.

## Task execution (stage 3)

`POST /app/task/run` accepts:

```json
{"goal":"回到桌面","grant_id":"...","allow_while_active":false}
```

- Requires `enable_task_execute` (default **false**), a registered device and a
  valid, unexpired grant; otherwise `403 disabled`, `409 no_device` or
  `409 grant_required`.
- The run is synchronous and bounded by `task_run_timeout_seconds` (default 90).
- Every write step is verified with a **live** observation taken after the
  command. The periodic heartbeat is far too slow to prove a step worked, so
  `_task_observe_live` asks the phone for its state directly.
- Postconditions: `open_app` → foreground equals the package; `close_app` and
  `suspend_app` → foreground differs (suspend also needs `device_verified`);
  `unsuspend_app` → device verified; `lock_screen` → `screen_interactive: false`;
  `home`/`back` → the foreground changed.
- Step failures never retry: `grant_missing`, `grant_expired`,
  `grant_step_limit`, `action_not_enabled`, `protected_package`, `command_error`,
  `timeout`, `foreground_mismatch`, `still_foreground`, `not_verified`,
  `no_change`, `screen_still_on`, `stale_observation`.
- `step.verify` is `{reason, detail, command_success, verified, observed_at}`.
  `command_success` is only the command's own acknowledgement and `verified` is
  the device's own post-check; **success is `reason == "ok"`**, nothing else.
- A `suspend_app` that fails rolls back only when the same grant covers
  `unsuspend_app`; otherwise the summary carries `restore_pending`.

### Idle guard

Unless the caller passes `allow_while_active: true`, a run is refused with
`refused: "user_active"` when the screen is interactive and the foreground app
is neither a launcher nor one of the task's own target packages. Nothing is sent
to the phone in that case. The override is recorded in `run.summary` and the
audit log so an execution during someone's active session stays traceable.

`POST /app/grant` accepts `{actions, packages, ttl_seconds, max_steps}` and
returns `{grant_id, expires_at}`. `actions` must name write actions; the
package-bound ones (`open_app`, `close_app`, `suspend_app`, `unsuspend_app`)
require `packages`. `ttl_seconds` is clamped to 30–600 and `max_steps` to 1–3.
`POST /app/task/cancel` accepts `{run_id}` and only flags the run.

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

## Device status heartbeat

`POST /device/status` accepts a periodic status heartbeat from the app:

```json
{
  "device_id": "phone-abcd1234",
  "app_version": "0.3.0",
  "battery_percent": 42,
  "charging": false,
  "foreground_package": "tv.danmaku.bilibilihd",
  "screen_on_seconds_today": 12345,
  "screen_interactive": true,
  "shizuku_ready": true,
  "reported_at": 1760000300.0
}
```

The app posts this every 30–60 seconds after the first local status refresh.
Upload failure updates connection state but does not clear the local snapshot
or block the command server. The `device_id` must match the registered device;
otherwise the heartbeat is rejected with HTTP 400.

`screen_interactive` is `true` when the screen is on, `false` when off, and
absent/null when the state cannot be read — the night guard never treats
unknown as on. `reported_at` is clamped to the server receive time when the
phone clock is more than 5 minutes off, so a skewed clock cannot伪造 freshness.

## Night guard

The night guard is a deterministic background state machine, not an LLM loop.
When `enable_night_guard` is on and the current local time is inside the
configured `[night_guard_start, night_guard_end)` window (crossing midnight if
start > end), the plugin periodically observes the foreground app from the
heartbeat cache and, if it matches a configured `night_guard_packages` entry
for `night_guard_consecutive_hits` observations in a row, sends one private-chat
warning. After `night_guard_grace_minutes` it re-checks; if the same package is
still foreground, it calls the existing verified `_execute_app_policy(disable)`
path to suspend that single package.

Key safety properties:

- `night_guard_dry_run=true` observes, warns and audits but never suspends.
  The first production deployment should keep this on for at least one night.
- The guard only restores packages in its own `owned_suspended` list. It never
  calls `phone_sleep_mode(stop)` or restores packages suspended by other
  sources (manual policies, other tasks).
- `night_guard_private_only=true` (default) rejects group chat sessions; the
  warning goes to the last recorded private `unified_msg_origin` for the user.
- State is persisted to `night_guard_state_path`; it contains only package
  names, timestamps and phase — never tokens, screen text or notification
  bodies.
- A private chat message containing a release phrase ("解开", "解除限制",
  "恢复视频", "让我继续用") triggers an immediate restore of guard-owned
  packages and enters a cooldown so the guard stays quiet for
  `night_guard_warning_cooldown_minutes`.
- The guard requires `control_backend=app`; in Operit mode it skips cleanly.

The `GET /status` response now includes a `night_guard` section with the
current window, dry-run flag, package list and per-user state summary.

