# AstrBot Phone Agent

Control an Android phone from AstrBot through Tailscale and Operit. AstrBot interprets natural language and delegates tasks; Operit owns screenshot/OCR, UI interaction, text input, and Shizuku-backed operations.

The default control backend is Operit. The server does not need a persistent ADB connection, and the phone does not need to stay connected to a computer.

## 1. Architecture

```text
Chat platform -> AstrBot -> Tailscale -> Operit HTTP -> Operit Agent + Shizuku
```

Health data is a separate path: `xiaomi-health-sync` synchronizes Xiaomi Fitness data into a server-side SQLite database, and `phone_health` reads that database.

## 2. Prerequisites

- A Linux server running AstrBot.
- Tailscale installed on both the server and the phone, joined to the same tailnet.
- Operit installed on the phone.
- Shizuku installed and running on the phone.
- Shizuku access granted to the Operit tools or workflows you intend to use.
- AstrBot 4.22 or newer.
- A Xiaomi Fitness account and `xiaomi-health-sync` if health queries are needed.

The phone can use Wi-Fi or mobile data. Operit HTTP works through Tailscale and does not require wireless ADB. Shizuku may need to be started again after a phone reboot.

## 3. Configure Tailscale

1. Install Tailscale on the phone and server.
2. Sign both devices into the same tailnet.
3. From the server, verify reachability:

   ```bash
   tailscale ping PHONE_TAILSCALE_IP
   ```

4. Continue only after receiving `pong`. Keep the real address out of public repositories.

## 4. Configure Operit and Shizuku

1. Start Shizuku and verify that it is running.
2. Open Operit and grant Shizuku access to the tools or workflows you will use.
3. In Operit, open:

   ```text
   Settings -> Data and permissions -> External HTTP calls
   ```

4. Enable the external HTTP service. The default port is `8094`.
5. Copy the displayed Bearer Token. Put it only in AstrBot configuration; never send it in chat or commit it to GitHub.
6. The base URL is normally:

   ```text
   http://PHONE_TAILSCALE_IP:8094
   ```

The discovery endpoint is:

```text
http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json
```

`/api/health` and `/api/external-chat` require the Bearer Token.

## 5. Install the AstrBot plugin

### Download from GitHub

Open <https://github.com/Tauru-t6/astrbot-phone-agent>, download the ZIP, and copy the extracted directory to:

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

The directory must directly contain `main.py`, `metadata.yaml`, and `_conf_schema.json`. Do not add an extra nested directory.

### Clone on the server

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

Restart AstrBot. The plugin itself has no extra Python dependencies.

## 6. Open the WebUI

After installation and restart, open “Phone Agent” from the AstrBot Dashboard plugin pages. The page provides:

- Operit online status and a connection test.
- Operit URL, Token, backend, and user allowlist settings.
- App alias JSON editing.
- Sleep guard hours, target apps, and exceptions.
- Start/stop controls for temporary sleep mode.
- Health summary, background tasks, reminders, and recent audit records.

The page uses AstrBot Dashboard authentication. The Operit Token is shown only as configured/not configured and is never echoed.

## 7. Configure the plugin

Configure the plugin in AstrBot WebUI:

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

If you explicitly want to reuse Private Companion authorization:

```json
{
  "allowed_user_ids": "",
  "use_private_companion_auth": true
}
```

This is an optional extension. The phone agent does not change Private Companion personality, memory, proactive messages, or prompts. See [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md).

## 8. Configure features

### App aliases

Built-in aliases include Bilibili, Kuaishou, Youku, and WeChat. Add your own mappings with `app_aliases_json`:

```json
{
  "Douyin": "com.ss.android.ugc.aweme",
  "Douyin Lite": "com.ss.android.ugc.aweme.lite"
}
```

### Sleep guard

```json
{
  "sleep_guard_enabled": true,
  "sleep_guard_start": "00:30",
  "sleep_guard_end": "07:00",
  "sleep_guard_packages": "Bilibili,Kuaishou,Youku",
  "sleep_guard_exempt_apps": "WeChat",
  "sleep_guard_poll_seconds": 30
}
```

During quiet hours, Operit checks the foreground app and uses Shizuku to suspend configured video apps. They are restored when the window ends. You can also say:

```text
Do not let me watch videos for two hours
Unlock the video restriction
```

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

The phone plugin does not read Xiaomi credentials directly. Deploy `xiaomi-health-sync` separately:

1. Install the project and its dependencies on the server.
2. Use its QR login flow for the Xiaomi account.
3. Set the plugin's `health_db_path` to the synchronized database.
4. Run synchronization periodically with a systemd timer or another scheduler.

The plugin only reads SQLite. Xiaomi tokens are never uploaded to GitHub or chat. Example queries:

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

Verify the HTTP service, Token, and Shizuku status. First test with:

```text
Observe the phone only; do not click or type
```

## Security

- Expose Operit HTTP only through Tailscale or a trusted private network.
- Never commit Operit Tokens, Xiaomi Tokens, SSH passwords, or server configuration.
- Restrict access with `allowed_user_ids`.
- This plugin does not accept arbitrary shell commands.
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
