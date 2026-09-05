# AstrBot Phone Agent

Control an Android phone from AstrBot through Tailscale and Operit. AstrBot interprets natural language and delegates tasks; Operit owns screenshot/OCR, UI interaction, text input, and Shizuku-backed operations.

The default control backend is Operit. The server does not need a persistent ADB connection, and the phone does not need to stay connected to a computer.

## 1. Architecture

```text
Chat platform -> AstrBot -> Tailscale -> Operit HTTP -> Operit Agent + Shizuku
```

Health data is a separate path: the server-side `xiaomi-sync` deployment (the `xiaomi-health-sync` repository) synchronizes Xiaomi Fitness data into SQLite, and `phone_health` reads that database. Android Health Bridge is not part of this path.

## 2. Prerequisites

- A Linux server running AstrBot.
- Tailscale installed on both the server and the phone, joined to the same tailnet.
- Operit installed on the phone.
- Shizuku installed and running on the phone.
- Shizuku access granted to the Operit tools or workflows you intend to use.
- At least one working chat model configured in Operit. Verify that it can answer and call tools from a normal Operit chat before enabling remote control.
- AstrBot 4.22 or newer.
- A Xiaomi Fitness account and the server-side `xiaomi-sync` deployment if health queries are needed.

The phone can use Wi-Fi or mobile data. Operit HTTP works through Tailscale and does not require wireless ADB. Shizuku may need to be started again after a phone reboot.

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

## 4. Configure Operit and Shizuku

Every subsection below is required. An open HTTP port alone cannot execute phone tasks.

### 4.1 Start Shizuku

1. Enable Android Developer options.
2. Start Shizuku using one of the methods shown in the Shizuku app. Android 11+ can usually use Wireless debugging pairing; a temporary computer connection also works.
3. Verify that Shizuku says it is running.
4. Trigger a Shizuku-backed feature in Operit and allow the permission request.
5. Confirm that Operit appears in Shizuku's authorized apps.

Wireless debugging here is only a way to start Shizuku. It is not the AstrBot server control path. Shizuku normally needs to be started again after reboot.

### 4.2 Configure an Operit model

Add at least one working model in Operit's provider/model settings. The exact UI varies, but normally requires:

- API type or compatible protocol, such as OpenAI Compatible.
- API Base URL.
- API Key.
- Model name.

Choose a model with tool/function-calling support. Run two tests:

1. Send “reply with OK only” in a normal Operit chat.
2. Send “read the phone battery only; do not modify anything” and verify that a tool is called.

If test 1 fails, check the endpoint, Key, and model name. If test 1 works but test 2 fails, the model may not support tools, tools may be disabled, or Shizuku may not be authorized.

### 4.3 Enable External HTTP calls

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

### 4.4 Verify Operit from the server

Check the Agent Card:

```bash
curl --max-time 10 \
  "http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json"
```

Expected result: JSON containing `Operit`, `protocolVersion`, or `/a2a`.

Then test the authenticated health endpoint without putting the Token in shell history:

```bash
read -rsp "Operit Token: " OPERIT_TOKEN; echo
curl --max-time 10 \
  -H "Authorization: Bearer $OPERIT_TOKEN" \
  "http://PHONE_TAILSCALE_IP:8094/api/health"
unset OPERIT_TOKEN
```

Expected result: HTTP 200 with an enabled service. `401 Unauthorized` means the Token is wrong; connection refused means the service is not listening; timeout usually indicates Tailscale, battery management, or a stuck Operit service.

## 5. Install the AstrBot plugin

Before installation, verify AstrBot is running and identify its data directory, written as `<astrbot-data>` below.

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

Restart AstrBot. The plugin itself has no extra Python dependencies.

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

Expected log entries include:

```text
Loading plugin astrbot_plugin_phone_agent
Added llm tool: operit_task
Added llm tool: phone_observe
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
5. Click Refresh, then Test Operit.

The page provides:

- Operit online status and a connection test.
- Operit URL, Token, backend, and user allowlist settings.
- App alias JSON editing.
- On-demand app policies and default target-app settings; no background polling.
- Start/stop controls for temporary restrictions with automatic restore.
- One-shot location lookup.
- Health summary, background tasks, reminders, and recent audit records.

The page uses AstrBot Dashboard authentication. The Operit Token is shown only as configured/not configured and is never echoed.

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
4. In an authorized private chat, send:

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

Messages, comments, likes, shares, deletes, uninstallations, and payments require explicit confirmation. Opening apps, navigation, screen lock, and read-only status checks can run directly.

## 9. Health data (optional)

The phone plugin does not read Xiaomi credentials directly. Deploy the server-side `xiaomi-sync` (`xiaomi-health-sync`) separately:

1. Install the project and its dependencies on the server.
2. Use its QR login flow for the Xiaomi account.
3. Install the timer templates in [`deploy/xiaomi-sync`](deploy/xiaomi-sync), then set `health_db_path` to `/home/tauru/data/xiaomi_health_sync/data/health.db` (a directory containing `health.db` is also accepted).
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

This repository contains no tokens, passwords, server configuration, or Private Companion source code.
