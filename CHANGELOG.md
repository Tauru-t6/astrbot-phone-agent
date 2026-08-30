# Changelog

## [0.3.0] - 2026-08-30

### Added

- AstrBot Dashboard plugin page with live status, Operit test, configuration, sleep controls, health summary, tasks, reminders, and audit views.
- Server-side configuration validation and masked Token display.

### Documentation

- Clarified that Operit requires a configured, working model with tool-calling support before remote phone tasks can run.

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
