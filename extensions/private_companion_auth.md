# Private Companion Authorization (Optional)

The phone agent does not require `astrbot_plugin_private_companion`.

For an installation that already uses Private Companion, set:

```json
{
  "allowed_user_ids": "",
  "use_private_companion_auth": true
}
```

With this setting, the phone agent asks Private Companion for its authorized
user IDs. If the companion plugin is missing or disabled, phone tools deny
access instead of silently opening control to everyone.

For a standalone installation, leave `use_private_companion_auth` false and set
`allowed_user_ids` to the account IDs that may control the phone. This extension
does not change the companion plugin's personality, prompts, memory, or
proactive behavior.
