# AstrBot Phone Agent

[中文](README.md) · [Android App](https://github.com/Tauru-t6/astrbot-phone-agent-app) · [App API v2](docs/APP_API_V2.md)

Control an Android phone from AstrBot through either **AstrBot Phone Agent App** or **OperitAI**. AstrBot interprets requests and delegates them to the selected backend. App mode executes a closed set of structured commands through Shizuku; Operit mode owns screenshot/OCR, UI interaction, text input, and Shizuku-backed natural-language operations.

The default control backend is Operit. Set `control_backend=app` for the standalone App, or `control_backend=operit` for OperitAI. The server does not need a persistent ADB connection, and the phone does not need to stay connected to a computer.

| | App mode | OperitAI mode |
| --- | --- | --- |
| Phone endpoint | AstrBot Phone Agent App, normally port `8260` | Operit External HTTP, normally port `8094` |
| Execution | Structured Android/Shizuku commands | Model-driven screen reasoning and tool calls |
| Supports | Status, app launch/close, navigation, lock/wake, usage, location, notifications, policies, reminders and timeline | Screen-aware tasks, taps, swipes, text input and workflows |
| Does not support | Arbitrary tap/swipe/text-input workflows | — |
| Phone model | Not required | A working model with tool calling is required |

Legacy backend aliases `phone_buddy`, `native_app`, and `own_frontend` map to `app`; `operitai` and `operit_ai` map to `operit`. `adb` remains diagnostic-only.

## 1. Architecture

```text
Chat platform -> AstrBot -> Phone Agent plugin
                           |
                           +-> LAN/Tailscale -> App command HTTP -> Android + Shizuku
                           |
                           +-> Tailscale -> Operit HTTP -> Operit model + tools + Shizuku
                           |
                           +-> Optional relay <- phone polls, executes and reports

App chat -> AstrBot chat API -> AstrBot model and enabled plugins
Xiaomi Fitness -> server-side xiaomi-sync -> SQLite -> phone_health
```

Health data is a separate path: the server-side `xiaomi-sync` deployment (the `xiaomi-health-sync` repository) synchronizes Xiaomi Fitness data into SQLite, and `phone_health` reads that database. Android Health Bridge is not part of this path.

## 2. Prerequisites

- A Linux server running AstrBot.
- Tailscale installed on both the server and the phone, joined to the same tailnet.
- The selected phone endpoint: AstrBot Phone Agent App or OperitAI.
- Shizuku installed and running on the phone.
- Shizuku access granted to the selected phone app's tools or workflows.
- For Operit mode, at least one working chat model configured in Operit. Verify that it can answer and call tools from a normal Operit chat before enabling remote control.
- AstrBot 4.22 or newer.
- A Xiaomi Fitness account and the server-side `xiaomi-sync` deployment if health queries are needed.

For App mode, install [AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app). For Operit mode, install [Operit](https://github.com/AAswordman/Operit), configure a working model, and enable External HTTP. Names such as `PHONE_TAILSCALE_IP`, `<astrbot-data>`, and `YOUR_ASTRBOT_USER_ID` below are placeholders; never copy a real private address, token, or password into this document.

The phone can use Wi-Fi or mobile data. Either phone HTTP endpoint works through Tailscale without an ongoing wireless ADB connection. A LAN-only App installation can use a local route; Tailscale provides a route after the phone leaves that LAN. Shizuku may need to be started again after a phone reboot.

## 3. Configure Tailscale

Use the order server -> phone -> connectivity test.

### 3.1 Install on the server

Use the official Tailscale installation method. For example, on Debian/Ubuntu:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Complete browser authorization, then run:

```bash
tailscale status
tailscale ip -4
```

Expected result: the server appears in the device list with a `100.x.y.z` address.

### 3.2 Install on the phone

1. Install Tailscale on the phone.
2. Sign in to the same account/tailnet as the server.
3. Enable the Tailscale VPN.
4. Allow Tailscale to run in the background so Android battery management does not disconnect it.
5. Record the phone's `100.x.y.z` address.

### 3.3 Verify connectivity

From the server:

```bash
tailscale ping PHONE_TAILSCALE_IP
```

Expected result: `pong from ...`. If the server cannot reach the phone, check the phone VPN state, Android battery restrictions, and tailnet ACLs.

The phone may switch between Wi-Fi and mobile data. It does not need to share a LAN with the server. Keep the real address out of public repositories.

## 4. Configure the phone backend and Shizuku

Start Shizuku for either mode, then follow the App subsection or all three Operit subsections. An open HTTP port alone cannot execute phone tasks.

### 4.1 Start Shizuku

1. Enable Android Developer options.
2. Start Shizuku using one of the methods shown in the Shizuku app. Android 11+ can usually use Wireless debugging pairing; a temporary computer connection also works.
3. Verify that Shizuku says it is running.
4. In App mode, use **Paper / 纸笺 -> Permissions / 权限管理 -> Request Shizuku permission**. In Operit mode, trigger a Shizuku-backed feature and allow its permission request.
5. Confirm that the selected app appears in Shizuku's authorized apps. Authorizing one backend does not authorize the other.

Wireless debugging here is only a way to start Shizuku. It is not the AstrBot server control path. Shizuku normally needs to be started again after reboot.

### 4.2 App mode setup

Install the App release, let its foreground connection service run, then open **Paper / 纸笺 -> Server connection**. Enter the AstrBot chat base URL, plugin route base URL, chat identity, and their separate credentials. The phone command token must match `app_shared_token`; App chat API credentials and the plugin Dashboard JWT are separate. Save, then use the App's independent connection test. Configure ordered LAN/Tailscale endpoints on the plugin:

```json
{
  "control_backend": "app",
  "app_shared_token": "YOUR_RANDOM_PHONE_COMMAND_TOKEN",
  "app_direct_urls": "http://PHONE_LAN_IP:8260,http://PHONE_TAILSCALE_IP:8260",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID"
}
```

The public App contains no preset service URL, token, display name, or avatar. Its UI supports status, structured actions, reminders, timeline, local name/avatar, and system permission entries. It does not provide arbitrary `click`, `type`, tap, swipe, or natural-language UI execution; use Operit mode for those operations. `phone_observe` in App mode is a structured device-status read.

| App field | What to enter |
| --- | --- |
| AstrBot chat address | Base URL exposing `POST /api/v1/chat`, for example `https://chat.example` |
| Chat API Key | Credential for the chat/OpenAPI endpoint |
| Chat identity | Identity used by your server to associate the conversation and memory |
| Plugin address | Full plugin route base, normally `https://astrbot.example/api/plug/astrbot_plugin_phone_agent` |
| Plugin Token | AstrBot Dashboard token/JWT accepted by its plugin router |
| Phone control Token | Random secret matching `app_shared_token` |
| Relay address and Token | Optional; matching the plugin's relay address and secret |

Include your reverse proxy's path prefix where applicable. The App appends `/app/state` and other paths to the plugin base; do not enter a specific route such as `/app/state` as the base URL. Chat API keys, plugin Dashboard JWTs and the shared phone command token are not interchangeable.

The settings UI never echoes stored secrets. Empty replacement fields preserve existing credentials. **Test saved connection** uses the last saved configuration, so save edits before testing them. The local name/avatar settings only affect display and do not replace a server persona or the chat identity.

Allow background operation and notification permission for reminders. App usage queries require Android Usage access; location requires location permission and enabled system location services. Sensitive commands may wait for the App's confirmation dialog: only explicit approval executes them, and dismissing rejects them.

Configured `app_direct_urls` are ordered and precede addresses announced during registration. Put LAN before Tailscale when it is your preferred route. Up to eight unique addresses are considered. Each candidate receives a health check of up to three seconds; direct attempts and relay fallback share one command timeout budget. Transport retries reuse the command ID, which the new App deduplicates. A matching phone result with `success=false` is final and is not replayed through another endpoint.

The command API includes status, app launch/close, foreground app, home/back, lock/wake, screenshot, screen text, location, usage, app restrictions, notifications and ping. Dedicated tools/API routes expose actions where appropriate; not every command is a `phone_action` argument. App requests never silently fall back to ADB. See [App API v2](docs/APP_API_V2.md).

### 4.3 Configure an Operit model

Add at least one working model in Operit's provider/model settings. The exact UI varies, but normally requires:

- API type or compatible protocol, such as OpenAI Compatible.
- API Base URL.
- API Key.
- Model name.

Choose a model with tool/function-calling support. Run two tests:

1. Send “reply with OK only” in a normal Operit chat.
2. Send “read the phone battery only; do not modify anything” and verify that a tool is called.

If test 1 fails, check the endpoint, Key, and model name. If test 1 works but test 2 fails, the model may not support tools, tools may be disabled, or Shizuku may not be authorized.

### 4.4 Enable Operit External HTTP calls

In Operit, open:

```text
Settings -> Data and permissions -> External HTTP calls
```

1. Enable the service.
2. Keep the default port `8094`, or use the same custom port everywhere else.
3. Copy the displayed Bearer Token.
4. Allow Operit to run in the background and remove strict battery restrictions.
5. The base URL is normally:

   ```text
   http://PHONE_TAILSCALE_IP:8094
   ```

The discovery endpoint is:

```text
http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json
```

`/api/health` and `/api/external-chat` require the Bearer Token.

### 4.5 Verify Operit from the server

Check the Agent Card:

```bash
curl --max-time 10 \
  "http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json"
```

Expected result: JSON containing `Operit`, `protocolVersion`, or `/a2a`.

Then test the authenticated health endpoint without putting the Token in shell history:

```bash
read -rsp "Operit Token: " OPERIT_TOKEN
printf '\n'
curl --max-time 10 \
  -H "Authorization: Bearer $OPERIT_TOKEN" \
  "http://PHONE_TAILSCALE_IP:8094/api/health"
unset OPERIT_TOKEN
```

Expected result: HTTP 200 with an enabled service. `401 Unauthorized` means the Token is wrong; connection refused means the service is not listening; timeout usually indicates Tailscale, battery management, or a stuck Operit service.

## 5. Install the AstrBot plugin

Before installation, verify AstrBot is running and identify its data directory, written as `<astrbot-data>` below.

You can also install `https://github.com/Tauru-t6/astrbot-phone-agent` through AstrBot's plugin manager and reload the plugin there.

### 5.1 Install from a GitHub ZIP

1. Open <https://github.com/Tauru-t6/astrbot-phone-agent/releases>.
2. Download the latest Release source ZIP.
3. Rename the extracted directory to `astrbot_plugin_phone_agent`.
4. Copy it to:

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

The correct structure is:

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent/
├── main.py
├── device_app.py
├── metadata.yaml
├── _conf_schema.json
├── pages/
│   └── phone-control/
│       └── index.html
└── README.en.md
```

An extra `astrbot-phone-agent-main` directory prevents AstrBot from loading the plugin correctly.

### 5.2 Install with Git (recommended)

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

Restart AstrBot. The plugin uses standard-library modules and packages already supplied by AstrBot, including Quart. Run it inside AstrBot's environment rather than launching `main.py` as a standalone service.

To update later:

```bash
cd <astrbot-data>/plugins/astrbot_plugin_phone_agent
git pull --ff-only
```

### 5.3 Restart and inspect logs

For an AstrBot systemd user service:

```bash
systemctl --user restart astrbot.service
systemctl --user --no-pager status astrbot.service
journalctl --user -u astrbot.service -n 100 --no-pager
```

Use the matching restart method for Docker, a hosting panel, or manual startup.

Use the actual unit name if yours differs. `systemctl --user` must run as the account that owns AstrBot; it is different from a system-level `sudo systemctl` service. Preserve configuration and state files when updating source code.

Expected log entries include:

```text
Loading plugin astrbot_plugin_phone_agent
Added llm tool: operit_task
Added llm tool: phone_observe (disabled by default; enable `enable_observe_tool` before it can call Operit to inspect the current app)
Added llm tool: phone_location
Added llm tool: phone_app_policy
Plugin astrbot_plugin_phone_agent (...)
```

If imports fail, verify the AstrBot version and ensure the plugin runs inside AstrBot's Python environment.

## 6. Open the WebUI

1. Sign in to AstrBot Dashboard.
2. Open Plugins.
3. Select `astrbot_plugin_phone_agent`.
4. Open “Phone Control” from its plugin Pages.
5. Select App or Operit in Settings, save, then click Refresh and **Test current backend**.

The page provides:

- Selected-backend online status and an explicit connection test.
- App/Operit mode selection with backend-specific fields: App direct URLs/shared command Token or Operit URL/Token, plus the user allowlist.
- App alias JSON editing.
- On-demand app policies and default target-app settings; no background polling.
- Start/stop controls for temporary restrictions with automatic restore.
- One-shot location lookup.
- Health summary, background tasks, reminders, and recent audit records.
- Reminders can be cancelled and the page supports 15-second auto-refresh. The old task cancel/retry buttons currently lack matching Web routes; use `operit_task_cancel` and `operit_task_retry` in chat for those actions.
- Background task metadata is persisted in `tasks_path`; tasks interrupted by an AstrBot restart are marked as interrupted.

The page uses AstrBot Dashboard authentication. The Operit Token is shown only as configured/not configured and is never echoed.

The same rule applies to App tokens: an empty replacement field preserves the configured secret. Feature flags, storage paths, detailed timeouts and relay settings are available in AstrBot's native plugin configuration. The old `adb` backend remains for diagnostics; it is not a normal mode selection.

If it reports that the plugin page bridge is unavailable, verify `pages/phone-control/index.html`, hard-refresh the browser, and restart AstrBot. The page source must load `/api/plugin/page/bridge-sdk.js`.

## 7. Configure the plugin

Use the Phone Control page or AstrBot's native plugin configuration. Start with the minimum required values:

```json
{
  "enabled": true,
  "control_backend": "operit",
  "operit_base_url": "http://PHONE_TAILSCALE_IP:8094",
  "operit_token": "YOUR_OPERIT_TOKEN",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "use_private_companion_auth": false
}
```

At minimum configure:

- `operit_base_url`: the phone Tailscale address plus `:8094`.
- `operit_token`: the Bearer Token shown by Operit.
- `allowed_user_ids`: AstrBot account IDs allowed to control the phone, separated by commas.
- `phone_location` reads one location fix on demand; high-accuracy fixes and address lookup require explicit confirmation.

This ID is the sender ID supplied by the chat platform. For QQ OneBot it is normally the QQ number; other platforms may use an openid. Check `user_id` or sender ID in AstrBot receive logs.

Validate in this order after saving:

1. WebUI shows the Token as configured.
2. Test Operit succeeds and shows the Operit version.
3. Refresh Health. “Not configured” is expected when no health database is configured.
4. If phone observation is needed, first set `enable_observe_tool=true`, then in an authorized private chat send:

   ```text
   Observe the phone only; do not click or type
   ```

5. Then test a low-risk action:

   ```text
   Open Android Settings
   ```

6. Finally test a Shizuku-backed action on a known test app.

Do not use messages, comments, deletes, uninstallations, or payments as the first test.

If you explicitly want to reuse Private Companion authorization:

```json
{
  "allowed_user_ids": "",
  "use_private_companion_auth": true
}
```

This is an optional extension. The phone agent does not change Private Companion personality, memory, proactive messages, or prompts. See [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md).

### 7.1 Installation acceptance checklist

- [ ] `tailscale ping PHONE_TAILSCALE_IP` succeeds from the server.
- [ ] Shizuku is running and Operit is authorized.
- [ ] A normal Operit chat can use the configured model.
- [ ] Operit can perform one harmless local tool call.
- [ ] External HTTP is enabled on the configured port.
- [ ] The server can read the Agent Card.
- [ ] `/api/health` returns HTTP 200 with the Bearer Token.
- [ ] AstrBot logs show the plugin and LLM tools loaded.
- [ ] WebUI Test Operit succeeds.
- [ ] `phone_observe` returns phone state.

Fix the first failing layer before debugging later layers. Reinstalling the AstrBot plugin cannot fix an unreachable `/api/health` endpoint.

## 8. Configure features

### App aliases

Built-in aliases include Bilibili, Kuaishou, Youku, Douyin, Douyin Lite, and WeChat. Add your own mappings with `app_aliases_json`:

```json
{
  "Douyin": "com.ss.android.ugc.aweme",
  "Douyin Lite": "com.ss.android.ugc.aweme.lite"
}
```

### On-demand app policies

```json
{
  "sleep_guard_packages": "Bilibili,Kuaishou,Youku",
  "sleep_guard_exempt_apps": "WeChat"
}
```

These fields are only the default target and exception lists for `phone_sleep_mode`. The plugin does not inspect the foreground app on a schedule or poll in the background. It acts only when AstrBot's LLM calls `phone_app_policy` or `phone_sleep_mode`. Examples:

```text
Do not let me watch videos for two hours
Unlock the video restriction
Disable Douyin Lite for 30 minutes
Restore Douyin Lite
```

`phone_location` is also on demand. Call it only when the user explicitly asks for location or the current task requires it; high-accuracy fixes and address lookup require an extra confirmation.

### Tasks, reminders, and audit

```text
Observe the phone
Open WeChat
Remind me to drink water in 30 minutes
Show the status of the last task
Cancel the current phone task
```

Background Operit tasks return a task ID and can be queried, cancelled, or retried. Reminders are stored in the file configured by `reminders_path`. `phone_audit` records metadata only, not tokens or message bodies.

### High-risk actions

Messages, comments, likes, shares, deletes, uninstallations, payments, typing, and button clicks require explicit confirmation. Opening apps, navigation, screen lock, and read-only status checks can run directly.

### Private Companion bridge

`companion-context` is optional and defaults to `enabled=false`. It is installed only when explicitly enabled. To let proactive messages inspect the current app, also enable `enable_observe_tool=true` in the phone plugin and `include_screen=true` in the bridge config. Otherwise it does not call Operit.

Relay supports device isolation. Set `RELAY_REQUIRE_DEVICE_ID=1` and send `X-Relay-Device-ID` from the phone workflow for polling, lease renewal, and result submission. The default is disabled for compatibility. Rate limits default to 120 requests per minute and 30 task creations per minute, configurable with `RELAY_RATE_LIMIT` and `RELAY_TASK_RATE_LIMIT`.

## 9. Health data (optional)

The phone plugin does not read Xiaomi credentials directly. Deploy the server-side `xiaomi-sync` (`xiaomi-health-sync`) separately:

1. Install the project and its dependencies on the server.
2. Use its QR login flow for the Xiaomi account.
3. Install the timer templates in [`deploy/xiaomi-sync`](deploy/xiaomi-sync), then set `health_db_path` to `/srv/xiaomi-health-sync/data/health.db` (a directory containing `health.db` is also accepted).
4. Run synchronization periodically with a systemd timer or another scheduler.

The plugin only reads the `xiaomi-sync` SQLite database. Xiaomi tokens are never uploaded to GitHub or chat. Health responses include the latest sync time, success state, and staleness; a failed sync is reported as unavailable instead of success. Example queries:

```text
How many steps did I take today?
How did I sleep last night?
Are my recent heart rate and SpO2 normal?
```

## 10. Available tools

- `operit_task`: delegate a natural-language UI task to Operit.
- `operit_task_status`, `operit_task_cancel`, `operit_task_retry`: manage background tasks.
- `phone_action`: execute allowlisted phone actions.
- `phone_observe`: inspect phone state.
- `phone_location`: read one phone location fix on demand.
- `phone_app_policy`: let the LLM disable or restore a selected app, optionally with automatic restore.
- `phone_sleep_mode`: start a temporary video restriction.
- `phone_usage`: query app usage time.
- `phone_reminder`: create, list, and cancel reminders.
- `phone_audit`: read metadata-only audit records.
- `phone_health`: query synchronized Xiaomi health data.

## 11. Troubleshooting

### `401 Unauthorized`

The Operit Token is invalid or was reset. Copy the current Token from Operit External HTTP settings, update the plugin configuration, and restart AstrBot.

### `Connection reset by peer` or HTTP timeout

Verify Tailscale reachability, then disable and re-enable Operit's External HTTP service. If needed, force-stop and reopen Operit and remove battery restrictions.

### Logs mention an old ADB port

Ensure `control_backend` is `operit`, clear `adb_serial`, and start a new chat. Older conversation context may still contain a previous ADB address.

### Shizuku is unavailable after reboot

Start Shizuku again using its on-screen instructions and verify that Operit's permissions are still granted.

### Operit connects but does not act

This usually means Operit has no model configured, the model is unavailable, or tool calling is not working. Verify:

1. Operit has a valid API endpoint, API Key, and model name.
2. The model replies in a normal Operit chat.
3. The model supports and is allowed to call tools.
4. Shizuku is running and Operit has permission.
5. The External HTTP service is running and the Token is correct.

Then test with:

```text
Observe the phone only; do not click or type
```

## Security

- Expose Operit HTTP only through Tailscale or a trusted private network.
- Never commit Operit Tokens, Xiaomi Tokens, SSH passwords, or server configuration.
- Restrict access with `allowed_user_ids`.
- This plugin does not accept arbitrary shell commands.
- Location is never collected in the background; it is read only for an explicit request or a task that needs it.
- Android may show “Managed by Shell” for `pm suspend`; this is an OS-owned label and cannot be changed by the plugin.

## Used projects

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [Operit](https://github.com/AAswordman/Operit)
- [Shizuku](https://github.com/RikkaApps/Shizuku)
- [Tailscale](https://github.com/tailscale/tailscale)
- [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)
- [mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge)
- [mi_fitness](https://pypi.org/project/mi-fitness/)

Keep deployment credentials and state files out of Git. `companion-context` is the optional bridge, not a copy of the Private Companion project.


## 12. Relay installation and complete Operit workflow

The relay uses Python's standard library. It is a separate HTTP service, not an AstrBot plugin. Select one execution protocol for a queue: an App queue contains structured command envelopes, whereas an Operit queue contains natural-language tasks. Do not have both consumers claim from the same queue.

A self-contained system service example (replace the randomly generated token in both the plugin and the phone workflow):

```bash
sudo install -d /opt/phone-agent-relay /var/lib/phone-agent-relay
sudo install -m 755 deploy/relay/relay_server.py /opt/phone-agent-relay/relay_server.py
sudo install -m 600 /dev/null /etc/phone-agent-relay.env
```

Edit `/etc/phone-agent-relay.env`:

```ini
RELAY_TOKEN=YOUR_RANDOM_RELAY_TOKEN
RELAY_HOST=127.0.0.1
RELAY_PORT=8791
RELAY_STATE=/var/lib/phone-agent-relay/relay_state.json
RELAY_REQUIRE_DEVICE_ID=1
RELAY_RATE_LIMIT=120
RELAY_TASK_RATE_LIMIT=30
```

Use the bundled `deploy/relay/phone-agent-relay.service` as a template, updating `ExecStart`, `RELAY_STATE`, `ReadWritePaths` and the token settings to match your installation. If using the environment file above, replace the template's `Environment=RELAY_TOKEN=...` with `EnvironmentFile=/etc/phone-agent-relay.env`; do not keep two conflicting token settings. Install it as `/etc/systemd/system/phone-agent-relay.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now phone-agent-relay.service
sudo systemctl status phone-agent-relay.service --no-pager
```

This service uses system-level `systemctl`, distinct from an AstrBot user unit. Put the loopback HTTP listener behind Caddy/nginx HTTPS and configure `relay_base_url` and `relay_token` in AstrBot.

App mode polls automatically after the relay fields are saved. Operit mode retains the scheduled workflow integration:

1. Add a schedule trigger. If backed by Android WorkManager, periodic work normally has a minimum 15-minute interval.
2. Use `http_request` to call `GET https://relay.example/poll` with `Authorization: Bearer YOUR_RELAY_TOKEN` and, in strict mode, `X-Relay-Device-ID: phone-1`.
3. End if `task` is null. Otherwise pass `task.message` to the Operit AI execution node.
4. Before the five-minute lease expires, renew using `POST /renew` with `{"task_id":"THE_ACTUAL_TASK_ID"}` and the same device ID. Repeat while long work continues.
5. Submit the actual outcome to `POST /result`:

```json
{"task_id":"THE_ACTUAL_TASK_ID","success":true,"ai_response":"actual phone result","error":""}
```

Use `task.id` from polling, not the task text or a different command identifier. Report `success=false` and an error when execution fails. The plugin reads `GET /result/<id>`.

`RELAY_REQUIRE_DEVICE_ID` actually defaults to 0 for old-workflow compatibility. Setting it to 1 enforces ownership after claiming, including renewals and result submission. It is not pre-routing to a specific phone; multiple unrelated devices should not share one queue. A lost lease allows another poll to claim the task. Defaults are 64 pending tasks, 24-hour task/result retention, a 64-KiB HTTP body cap, 120 requests/minute and 30 task creations/minute.

A 15-minute poll interval is longer than the default plugin wait. Operit may therefore execute after the caller times out. App envelopes have deadlines and duplicate-ID protection, but a timeout still does not prove execution failed. Always inspect the actual result before retrying an action with side effects.

Some Operit tool entry points perform a direct health check before calling the transport. A failure at that earlier check can return immediately without submitting to relay. The fallback transport remains implemented, but not every offline Operit tool call reaches it; the App's new multi-address execution path uses the documented fallback order.

## 13. Private Companion bridge: separate installation and limits

Authorization reuse and proactive context enrichment are separate features. `use_private_companion_auth` only reuses the authorized user list; the plugin works without Private Companion when `allowed_user_ids` is configured.

To install the optional context bridge, copy the contents of `companion-context/` into a separate plugin directory named `astrbot_plugin_phone_companion_context`. The nested source directory is not automatically a second installed plugin. Its own configuration defaults `enabled=false`.

Context injection currently requires `enabled=true`, `relationship_mode_enabled=true`, the bridge's explicit `allowed_user_ids`, and a Private Companion user recognized as owner/primary/self. The default `private_only=true` further restricts it to private conversations.

| Setting | Default / purpose |
| --- | --- |
| `include_health` | true; read coarse health summaries from the phone plugin |
| `include_screen` | false; additionally requires the main plugin observation flag |
| `health_cache_seconds` | 900 seconds |
| `screen_cache_seconds` | 90 seconds |
| `timeout_seconds` | 20 seconds |
| `relationship_mode_enabled` | false; enables relationship/context prompt enrichment |
| `autonomous_decision_enabled` | false; adds an autonomy decision prompt |
| `autonomous_decision_mode` | `preview` or `low_risk_auto` |
| `decision_cooldown_minutes` / `decision_daily_limit` | 180 / 3 configuration defaults |
| `quiet_hours` | 00:30–08:00 |

The remaining settings control tone, health care, daily rituals, playful wording, optional forms of address, fictional gestures, custom prompts and closing text. They remain available in the separate bridge schema. They are not mandatory for basic phone control.

The screen collector still calls Operit directly and is not an App-only screen bridge. Health context may include cached or stale values, so consumers must check the date and synchronization status. The autonomy fields mostly enrich prompts: decision result parsing and counters do not form a complete autonomous scheduler. This bridge does not execute phone commands or grant permission for uncontrolled phone actions. Compatibility depends on Private Companion's internal interfaces.

## 14. Data and capability details

Health reads support 1–30 days of `daily_metrics`, plus available weight/BMI/body fat, blood pressure and sleep-segment tables. If a requested date is absent, the plugin can return the newest available historical day. `available` means there are records; `fresh` separately requires a successful sync within 180 minutes. Missing sync records or failed syncs are stale.

App location returns native WGS84 under the structured command result's `output`; it does not supply Operit's BD09 conversion or implement address lookup. Operit retains its GCJ02-to-BD09 path for China-region coordinates, but the legacy parser infers the input system from location bounds. Verify your provider and `coord_type` before passing coordinates to Baidu MCP reverse geocoding, place search or routing.

`phone_app_policy(minutes=0)` keeps a restriction until explicit restoration. Timed restore is an attempt on expiry; an offline phone can miss it, and the code does not continuously retry at that point. Failed batch restrictions trigger rollback attempts, with results reported. Policy files survive restarts.

`tasks_path` stores the most recent 100 Operit task metadata entries. Cancellation stops local waiting, not necessarily a phone request already sent. Retry starts the full task under a new ID and may repeat side effects; it does not resume at an interrupted UI step. Completed App reminder request receipts remain for 30 days, while only 100 timeline entries are kept.

The audit log avoids message bodies and tokens, but reminder, timeline and background task state can contain user text. Preserve these private files during upgrades and exclude them from public source archives.

## 15. Additional troubleshooting

- **App state works but phone commands fail:** the App-to-server route and server-to-phone route differ. Check LAN/Tailscale routing, `app_direct_urls`, `:8260`, the shared token and Shizuku separately.
- **App reports an unsupported UI action:** use Operit mode for arbitrary taps/swipes/text input. Do not route around the error through an arbitrary shell tool.
- **Reminder is late:** check which source created it (App or chat), notification permissions, phone background limits and relay/sync cadence.
- **Health is available but stale:** verify the external synchronizer's latest successful run and the returned record date; old records are not real-time readings.
- **Private Companion has no context:** check separate bridge installation, both enable switches, its allowlist, user role/private scope, and the Private Companion API version.
