# Changelog

## [0.8.6]

### Added

- **Group-chat commands with an out-of-band code.** An authorized user may ask
  for a phone action in a group; the proposal is created as usual and a 6-digit
  code is delivered to that user's **private chat**. Replying the code in the
  group approves it (3-minute window, single use, bound to that user and group).
  Data tools stay private-only so a group never sees health, location,
  notifications, screen text or screenshots (`group_allowed_tools`).
- **Screenshots are auditable.** Captures are written under the plugin data
  directory (`screenshot_dir`) and the newest 20 are kept, instead of being
  deleted from the system temp directory right after delivery.

### Fixed

- **Duplicate proposals.** A model that called the tool twice for one request
  used to leave two pending approvals; the fresh identical one is reused.
- **A single tailnet hiccup failed the whole run.** Live observation retries
  once before reporting `stale_observation`.

### Tests

- 233 passed; the harness gained the group message type and the screenshot and
  reminder tests were adjusted to the new paths.

## [0.8.5]

### Changed

- **The care scheduler moved into `care_scheduler.py`** as a mixin
  (`PhoneAgentPlugin(CareSchedulerMixin, Star)`). Pure move: no behaviour
  change, `main.py` drops from 4273 to ~4180 lines so the plugin file can stay
  about assembly. Three implicit dependencies (`_text`, `json`, `Path`) only
  became visible because of the move.
- **The unused `_push_phone_notification` helper is gone** (a reviewer noted it
  had no callers). The reminder test that asserted its absence now asserts the
  absence of a direct push path instead.
- **README opens with a five-minute quickstart**, and the release notes link the
  companion APK so a new user does not have to find the second repository.

### Tests

- Added a relay-only registration case (a device with no direct URL still
  registers; transport is modelled separately). 233 passed.

## [0.8.4]

### Changed

- **The plugin listing is bilingual** (Chinese + English), so the market page is
  readable without switching language.
- **README gained a "task and approval" section** describing the proposal flow:
  say what you want, reply 可以 in chat (or approve in the app / on the floating
  card), and every step is verified against a fresh observation. It also states
  that approval itself counts as consent, so the idle guard no longer applies to
  an approved run.
- **Release archives no longer ship `tests/`** (`.gitattributes` export-ignore),
  as the reviewer suggested.

## [0.8.3]

### Fixed

- **A task step could report success while nothing happened.** An empty
  foreground package was compared against the previous one and read as "the
  app changed"; a command that returned failure was not blocking the step
  either. Both now fail the step (`observation_empty`, `command_failed`).
- **Device commands could hang for minutes.** A dead candidate URL was given the
  full command budget; each attempt is now capped at 8 s and task calls pass
  their own 8 s budget.
- **A stale pinned URL stalled every command.** After the phone's Tailscale
  address changed, the configured `app_direct_urls` kept pointing at the old IP.

### Changed

- Examples and tests no longer contain a real QQ number.

## [0.8.2]

### Added

- **Approval proposals.** `POST /app/task/propose` plans a task and waits for a
  human (`task_approvals` in `/app/state`); `POST /app/task/approve` issues the
  grant and runs it; `POST /app/task/reject` cancels the run. A proposal alone
  never touches the phone.
- **`quick_status`.** Foreground plus screen state only, without the usage-stats
  aggregation that made a full status take ~12 s on a real device.
- **Per-step timings.** `step.verify` now carries `act_ms` and `verify_ms`.

### Fixed

- **Care messages were never delivered.** The scheduler path generated a message
  and returned it to nobody: delivery only happened on the host's own path. The
  bridge now sends through `context.send_message` and records `deliver_failed`
  instead of claiming success.
- **Interrupted runs stayed `executing` forever.** A run found in an active
  state on load is marked `failed/interrupted`.
- **An approved run was blocked by the idle guard.** Approval is the consent the
  guard exists to collect, so it no longer applies to an approved run.

### Changed

- **Command dispatch caches the URL that answered** and skips its health probe.
  Every command used to probe every candidate first, paying the timeout for each
  dead address (measured: ~9 s per command). Dead candidates now use 1.5 s, and
  the reachable URL is warmed at startup.

## [0.8.1]

### Added

- **Task execution (P1-E stage 3).** `POST /app/task/run` executes a granted
  task and verifies every step against a **live** observation taken after the
  command; the 45 s heartbeat is too slow to prove a step worked. Failure
  reasons never retry, and a failed `suspend_app` rolls back only when the grant
  covers `unsuspend_app`.
- **Scoped grants.** `POST /app/grant` issues a bound grant
  (`subject`/`device`/actions/packages/TTL 30–600 s/steps 1–3);
  `POST /app/task/cancel` flags a run.
- **Idle guard.** A run is refused with `user_active` while the screen is on and
  the foreground app is neither a launcher nor the task's own target. Callers
  may pass `allow_while_active`, which is recorded in the summary and audit.
- **Dashboard panels.** The plugin page now shows the night guard, the care
  snapshot and the latest task runs (`/status` gained `task_runs`).

### Changed

- `step.verify` carries `ok` next to `reason`; `verified` remains the device's
  own post-check, so success is `ok == true`, not `command_success`.

## [0.8.0]

### Added

- **Night guard.** A deterministic background state machine that observes the
  foreground app during a configured night window, sends one private-chat
  warning when a video app is detected, and suspends the package after a grace
  period if the user is still watching. All actions are audited; dry-run mode
  is the safe first deployment. The LLM never decides whether to suspend.
  New config keys: `enable_night_guard`, `night_guard_start/end`,
  `night_guard_check_interval_seconds`, `night_guard_status_max_age_seconds`,
  `night_guard_grace_minutes`, `night_guard_suspend_minutes`,
  `night_guard_consecutive_hits`, `night_guard_max_enforcements_per_night`,
  `night_guard_warning_cooldown_minutes`, `night_guard_packages`,
  `night_guard_exempt_packages`, `night_guard_private_only`,
  `night_guard_dry_run`, `night_guard_state_path`.
- **Device status heartbeat.** `POST /device/status` accepts a periodic
  foreground/battery/screen status from the app. The night guard reads this
  cache instead of polling; a stale heartbeat never triggers a suspension.
- **`screen_interactive`** field in the App status output and heartbeat, so the
  guard knows whether the screen is on. Unknown is never treated as on.
- **User release hook.** A private chat message with a release phrase
  ("解开", "解除限制") restores only the packages the guard suspended and
  enters a cooldown. It never unlocks packages from other sources.
- **Per-source policy leases.** Each suspension source keeps its own deadline
  and timer, persisted atomically and restored on restart. A manual stop no
  longer cancels the night guard's countdown, and a restore the device does not
  confirm keeps its lease instead of losing the only record of it.
- **Decision budget ledger.** Every autonomy evaluation reserves a slot under
  one lock, records a `reservation_id` with its state, gives the slot back when
  no message is produced, and refuses to send when the budget file cannot be
  written.
- **Autonomy scheduler.** The phone plugin ticks `evaluate_care()` when a fresh
  heartbeat and a recorded private session exist, and serves a read-only `care`
  snapshot (mode, quiet hours, quota, recent decisions) through `/app/state`.
- **Scoped task dry-run.** `ScopedGrant`, `ObservationRef` and `RunStep` records
  plus `POST /app/task/plan`, which observes for real, simulates every write and
  reports what a granted run would have done.

### Changed

- **Care decisions accept plain text.** The host's persona prompt produces a
  natural line rather than JSON, so a non-JSON answer is now treated as the
  message. It still passes preview, quiet hours, the daily budget and the
  post-model checks; an explicit `{"intent":"no_action"}` still means "do not
  disturb", and a broken JSON attempt is still rejected.

### Fixed

- **Duplicate `terminate()`.** The class had two `terminate` methods; the second
  silently overwrote the task-cancellation logic, leaving reminder, policy and
  operit background tasks running on shutdown. Merged into one method that
  cancels all tasks and restores only temporary policies.
- **NotificationRelayService flush.** Replaced the "flush only on next
  notification" pattern with a scheduled delayed flush. Failed batches are now
  retried up to 3 times instead of dropped immediately.
- **Screenshot quick-action copy.** The "保存到系统相册" label was misleading;
  the screenshot is sent to the conversation, not saved to the gallery.
- Removed the stale hardcoded `APP_VERSION = "0.2.0"` constant; the app version
  always comes from `BuildConfig.VERSION_NAME`.

## [0.7.0]

### Added

- **App-mode UI automation.** `tap`, `long_press`, `swipe`, `scroll`,
  `input_text` and `key_event` now run through the App backend, bringing it to
  parity with the OperitAI backend. Previously App mode refused them outright.
- **`phone_screenshot`.** Captures the phone screen in App mode and sends the
  picture into the conversation that asked. The app returns a downscaled JPEG
  inside the result envelope, so it works over both direct and relay transports.
- **Notification forwarding.** `phone_notifications` reads notifications the
  app forwards, so the assistant can answer "did anyone message me?".
  New routes: `GET/POST /app/notifications`.
- New config keys: `enable_screenshot_tool`, `enable_notification_tool`,
  `notification_path`.

### Security

- `screen_text`, `screenshot` and every UI-automation action now require
  on-device confirmation. Reading or capturing the screen was previously
  unconfirmed while reading the location was not.
- App: the command server enforces a per-address rate limit and a connection
  cap, so an unauthenticated peer on the LAN can no longer open unbounded
  concurrent connections.
- App: replaced the blanket `android:usesCleartextTraffic` flag with an
  explicit `networkSecurityConfig`, and dropped the unused
  `CHANGE_NETWORK_STATE` and `RECEIVE_BOOT_COMPLETED` permissions.

### Changed

- App: users can opt individual actions out of the confirmation dialog under
  纸笺 → 隐私与自动化. Only `screenshot` is pre-approved by default.
- App: every quick-action tile now maps to a real capability; the guard tiles
  drive the plugin's sleep-mode route instead of always failing locally.
- App: release builds enable R8 and resource shrinking with explicit keep rules
  for the reflective Shizuku and kotlinx.serialization paths.

### Removed

- App: ~1000 lines of unreachable UI code (`TabIcons`, `CompatTextField`,
  `ConnectionPill`, `DeviceStatusCard`, `GradientBackground`,
  `QuickActionTile`, `TimelineCard`, two unused icons) and the unused
  `logging-interceptor` dependency.

## [0.6.3] - 2026-10-02

- Store plugin state under `data/plugin_data/astrbot_plugin_phone_agent/` by default for predictable backup and migration.
- Remove the runtime `logging` fallback from `device_app.py`; the plugin now uses AstrBot's logger API directly.
- Add coverage for the managed default state directory.

## [0.6.2] - 2026-10-02

- Completed authenticated App state and reminder routes, atomic persistence,
  request idempotency, cancellation, and overdue recovery after restarts.
- App-created reminders publish a single due event for phone polling; chat
  reminders retain chat delivery. Expired reminders older than 24 hours are quiet.
- Fixed App actions accidentally falling through to ADB, added real App/Operit
  selection and backend-specific connection tests in the control page.
- Added ordered configured/registered direct addresses, shared timeout budgets,
  result identity validation, JSON-string output decoding and final device denials.
- Added route, restart, validation and failover tests; corrected feature documentation.

## [0.6.1] - 2026-10-02

### Added

- Added the AstrBot Phone Agent App backend scaffolding. Reminder lifecycle and mode dispatch fixes are completed in 0.6.2.
- Added LAN-first direct endpoints with Tailscale fallback through `app_direct_urls`.
- Reorganized documentation around the two supported modes: `app` and `operit`.

## [0.5.5] - 2026-09-12

### 变更

- 版本号更新至 `0.5.5`，收录后台任务持久化、Relay 多设备隔离、限流、自动化测试和 CI 等改进。

## [0.5.4] - 2026-09-12

### 新增

- Relay 支持可选的设备隔离、任务归属、租约续期、请求限流和请求体大小保护。
- 后台 Operit 任务元数据持久化，AstrBot 重启后可恢复查看，并标记中断任务。
- Private Companion 桥接状态检查和 GitHub Actions 自动检查。

## [0.5.3] - 2026-09-09

### 修复

- 修复未显式开启时 Operit 自动读取当前 App 的问题。
- Private Companion 桥接改为按需启用。
- 增加 Relay 租约恢复与续期、提醒重试、后台任务并发限制和更安全的 WebUI 写操作。
- WebUI 增加任务控制、提醒取消、自动刷新和更清晰的状态卡片。

## [0.5.2] - 2026-09-07

### Changed

- Market description rewritten in Chinese.
- Disabled `phone_observe` by default. It now requires `enable_observe_tool=true`.
- Disabled screen observation in `companion-context` by default so proactive companion generation does not call Operit unless `include_screen=true` is explicitly configured.
- Made the Private Companion bridge opt-in by default (`companion-context.enabled=false`).
- Added phone task confirmation for typing and clicking, relay lease renewal, relay request-size rejection, background-task limits, and same-origin checks for WebUI writes.

## [0.5.1] - 2026-09-07
## [0.5.1] - 2026-09-07

### Changed

- README rewritten in Chinese with a complete relay fallback guide; the plugin market page and the AstrBot in-app document viewer only render README.md.

## [0.5.0] - 2026-09-07
## [0.5.0] - 2026-09-07

### Changed

- Core vs optional split: a fresh install only exposes the Operit task tools (`operit_task*`, `phone_action`, `phone_observe`) and core WebUI routes. Health, usage, location, reminders, app-policy, and audit are now opt-in via `enable_health_tools`, `enable_usage_tool`, `enable_location_tool`, `enable_reminder_tools`, `enable_policy_tools`, and `enable_audit_tool`.
- `display_name` renamed to Phone Control (plugin id `astrbot_plugin_phone_agent` is unchanged).
- Removed the personal default `sleep_guard_packages` list; the field now defaults to empty.
- Removed the private `com.tauru.healthbridge` entry from the protected package list.

## [0.4.2] - 2026-09-07
## [0.4.2] - 2026-09-07

### Fixed

- `phone_location` now converts China-area GCJ02 fixes to BD09 and returns them as `bd09_latitude`/`bd09_longitude`, plus a `coord_type` field. Feeding raw GCJ02 coordinates to Baidu Map APIs resolved to a street ~500-800m away. Fixes outside China stay WGS84 and are passed through unchanged.

### Changed

- `phone_location` tool documentation now tells the LLM which coordinate fields to chain into Baidu Map MCP tools (`map_reverse_geocode`, `map_search_places`, `map_directions`, `map_road_traffic`).

## [0.4.1] - 2026-09-05

### Changed

- Health monitoring now documents and reports the server-side `xiaomi-sync` source, latest sync result, and stale data state.
- The health query returns an unsuccessful result when the configured database is missing or unavailable.

## [0.4.0] - 2026-09-05

### Added

- Added on-demand `phone_location` and `phone_app_policy` LLM tools.
- Added WebUI controls for one-shot location lookup and app policy execution.
- Added verified ADB policy state checks and rollback when a temporary policy partially fails.

### Changed

- Removed quiet-hours foreground polling. App restrictions now run only when an LLM or WebUI action requests them, with optional one-shot automatic restore.
- Protected the phone control app, Operit, Settings, and System UI from app policy changes.
- Health queries now identify the server-side `xiaomi-sync` source, expose the latest sync result, and report stale or unavailable data accurately.

## [0.3.0] - 2026-08-30

### Added

- AstrBot Dashboard plugin page with live status, Operit test, configuration, sleep controls, health summary, tasks, reminders, and audit views.
- Server-side configuration validation and masked Token display.

### Documentation

- Clarified that Operit requires a configured, working model with tool-calling support before remote phone tasks can run.
- Expanded the bilingual installation guide with step-by-step Shizuku, model, Tailscale, HTTP, plugin, WebUI, validation, and acceptance-checklist instructions.

## [0.2.0] - 2026-08-30

### Added

- Operit-first phone control over Tailscale HTTP.
- Allowlisted phone actions and natural-language `operit_task` delegation.
- Foreground/UI observation, battery status, and app aliases.
- Temporary sleep mode and quiet-hours video-app guard.
- Operit task status, cancellation, retry, reminders, usage queries, and metadata-only audit logs.
- Xiaomi health database queries for steps, sleep, heart rate, SpO2, and activity.
- Optional Private Companion authorization extension, disabled by default.
- Chinese and English installation documentation.

### Security

- No arbitrary shell executor is exposed by the plugin.
- Public defaults contain no server addresses, credentials, or tokens.
- High-risk external actions require explicit confirmation.
