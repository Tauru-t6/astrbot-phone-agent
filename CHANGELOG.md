# Changelog

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
