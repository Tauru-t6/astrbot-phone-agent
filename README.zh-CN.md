# AstrBot 手机 Agent

让 AstrBot 通过 Tailscale 调用手机上的 Operit。AstrBot 负责理解自然语言和决定任务，Operit 负责截图、OCR、点击、输入以及 Shizuku 特权操作。

默认控制后端是 Operit，不要求服务器持续连接 ADB，也不要求手机一直插着电脑。

## 一、整体结构

```text
聊天平台 -> AstrBot -> Tailscale -> 手机 Operit HTTP -> Operit Agent + Shizuku
```

健康数据是独立链路：服务器上的 `xiaomi-sync`（仓库 `xiaomi-health-sync`）定时同步小米运动健康数据到 SQLite，然后由 `phone_health` 查询。Android Health Bridge 不参与这条链路。

## 二、前置条件

- 一台运行 AstrBot 的 Linux 服务器。
- 手机和服务器加入同一个 Tailscale tailnet。
- 手机安装 Operit 和 Shizuku。
- 在 Operit 中授予需要的 Shizuku 权限。
- 在 Operit 中配置至少一个可用的聊天模型，并在 Operit 对话页确认模型能正常回复和调用工具。
- AstrBot 4.22 或更高版本。
- 如果需要健康数据，再准备小米运动健康账号和服务器端 `xiaomi-sync`。

手机可以使用 Wi-Fi 或移动数据。Operit HTTP 通过 Tailscale 工作，不依赖 ADB 无线调试。Shizuku 在手机重启后可能需要重新启动一次。

## 三、配置 Tailscale

建议按“服务器 → 手机 → 连通测试”的顺序操作。

### 3.1 服务器安装

使用 Tailscale 官方安装方式，例如 Debian/Ubuntu：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

浏览器会打开授权页面。登录后执行：

```bash
tailscale status
tailscale ip -4
```

预期结果：服务器出现在设备列表中，并得到一个 `100.x.y.z` 地址。

### 3.2 手机安装

1. 在手机安装 Tailscale。
2. 使用和服务器相同的账号/tailnet 登录。
3. 打开 Tailscale VPN。
4. 在手机系统的电池设置中允许 Tailscale 后台运行，否则锁屏或移动数据下可能断线。
5. 在 Tailscale 设备列表中记下手机的 `100.x.y.z` 地址。

### 3.3 验证连通性

在服务器执行：

```bash
tailscale ping PHONE_TAILSCALE_IP
```

预期结果：能看到 `pong from ...`。如果只有手机能看到服务器、服务器不能 ping 手机，请检查手机 Tailscale 是否在线、VPN 是否被系统省电暂停，以及 tailnet ACL 是否允许两台设备互访。

手机可以在 Wi-Fi 和移动数据之间切换，插件不要求两台设备位于同一局域网。不要把真实 Tailscale 地址提交到公开仓库。

## 四、配置 Operit 和 Shizuku

这一部分必须全部完成。仅仅打开 Operit HTTP 端口并不能执行手机任务。

### 4.1 启动 Shizuku

1. 打开手机开发者选项。
2. 按 Shizuku 页面提供的方法启动服务。Android 11 及以上通常可以使用“无线调试配对”；也可以临时连接电脑启动。
3. 回到 Shizuku 首页，确认显示“Shizuku 正在运行”。
4. 打开 Operit，触发一次需要 Shizuku 的功能，在授权弹窗中选择允许。
5. 在 Shizuku 的“已授权应用”中确认 Operit 已被授权。

这里的无线调试只用于启动 Shizuku，不是 AstrBot 服务器的控制链路。手机重启后通常需要重新启动 Shizuku。

### 4.2 配置 Operit 模型

在 Operit 的模型/提供商设置中添加至少一个可用模型。不同提供商界面略有差异，通常需要填写：

- API 类型或兼容协议，例如 OpenAI Compatible。
- API Base URL。
- API Key。
- 模型名。

优先选择支持工具调用（tool/function calling）的模型。配置后做两次测试：

1. 在普通 Operit 对话中发送“只回复 OK”，确认模型能正常回复。
2. 发送“只读取当前手机电量，不要修改任何内容”，确认 Operit 能调用工具并给出结果。

如果第一步失败，检查 API 地址、Key 和模型名；如果第一步成功但第二步失败，通常是模型不支持工具调用、工具未启用，或 Shizuku 未授权。

### 4.3 打开外部 HTTP 服务

进入 Operit：

```text
设置 -> 数据和权限 -> 外部 HTTP 调用
```

1. 打开启用开关。
2. 端口建议保持默认 `8094`；如果修改，后续配置必须使用相同端口。
3. 记录页面显示的 Bearer Token。
4. 允许 Operit 后台运行，并关闭针对 Operit 的严格省电限制。
5. 服务地址通常是：

```text
http://PHONE_TAILSCALE_IP:8094
```

接口发现地址为：

```text
http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json
```

真正的 `/api/health` 和 `/api/external-chat` 请求需要 Bearer Token。

### 4.4 从服务器验证 Operit

先检查 Agent Card：

```bash
curl --max-time 10 \
  "http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json"
```

预期结果：返回包含 `Operit`、`protocolVersion` 或 `/a2a` 的 JSON。

再检查带鉴权的健康接口。为避免 Token 留在 shell 历史中：

```bash
read -rsp "Operit Token: " OPERIT_TOKEN; echo
curl --max-time 10 \
  -H "Authorization: Bearer $OPERIT_TOKEN" \
  "http://PHONE_TAILSCALE_IP:8094/api/health"
unset OPERIT_TOKEN
```

预期结果：HTTP 200，并显示服务已启用。`401 Unauthorized` 表示 Token 错误；连接拒绝表示 HTTP 服务没监听；超时通常表示 Tailscale、系统省电或 Operit 服务卡住。

## 五、安装 AstrBot 插件

安装前确认 AstrBot 正常运行，并知道 AstrBot 数据目录。常见目录是启动命令指定的 `data` 目录；本文统一写作 `<astrbot-data>`。

### 5.1 从 GitHub ZIP 安装

1. 打开 <https://github.com/Tauru-t6/astrbot-phone-agent/releases>。
2. 下载最新 Release 的 Source code ZIP。
3. 解压后将目录重命名为 `astrbot_plugin_phone_agent`。
4. 复制到：

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

正确目录结构：

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── pages/
│   └── phone-control/
│       └── index.html
└── README.zh-CN.md
```

如果变成 `astrbot_plugin_phone_agent/astrbot-phone-agent-main/main.py`，说明多套了一层目录，AstrBot 不会正确加载。

### 5.2 使用 Git 安装（推荐）

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

后续更新：

```bash
cd <astrbot-data>/plugins/astrbot_plugin_phone_agent
git pull --ff-only
```

插件本身不需要额外安装 Python 依赖。

### 5.3 重启并检查加载日志

如果 AstrBot 使用 systemd user service：

```bash
systemctl --user restart astrbot.service
systemctl --user --no-pager status astrbot.service
journalctl --user -u astrbot.service -n 100 --no-pager
```

如果使用 Docker、面板或手动命令启动，请用对应方式重启 AstrBot。

预期日志包含：

```text
Loading plugin astrbot_plugin_phone_agent
Added llm tool: operit_task
Added llm tool: phone_observe
Added llm tool: phone_location
Added llm tool: phone_app_policy
Plugin astrbot_plugin_phone_agent (...)
```

如果日志提示找不到 `quart` 或 AstrBot API，先确认 AstrBot 版本不低于插件声明版本，并确保插件运行在 AstrBot 自己的 Python 环境中。

## 六、打开 WebUI

1. 登录 AstrBot Dashboard。
2. 打开“插件”页面。
3. 找到 `astrbot_plugin_phone_agent`。
4. 打开插件 Pages 中的“Phone Control/手机 Agent 控制台”。
5. 首次进入后点击“刷新”，再点击“测试 Operit”。

页面提供：

- Operit 在线状态和一键连接测试。
- Operit 地址、Token、控制后端和用户白名单配置。
- App 别名 JSON 编辑。
- 按需 App 策略与默认目标 App 配置，不会后台轮询。
- 临时限制启动/解除，到期自动恢复。
- 一次性定位读取入口。
- 健康摘要、后台任务、提醒和最近审计记录。

页面使用 AstrBot Dashboard 自带的登录鉴权。Token 只会显示“已配置”，不会回显。

如果页面显示“插件页面桥接不可用”，先确认安装目录中存在 `pages/phone-control/index.html`，再强制刷新浏览器（`Ctrl+F5`）。仍然失败时重启 AstrBot，并确认页面源码加载了 `/api/plugin/page/bridge-sdk.js`。

## 七、配置插件

可以在手机 Agent 控制台或 AstrBot 原生插件配置页填写。推荐先只配置最小必需字段：

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

最少需要配置：

- `operit_base_url`：手机 Tailscale 地址加 `:8094`。
- `operit_token`：Operit 外部 HTTP 页面里的 Bearer Token。
- `allowed_user_ids`：允许控制手机的 AstrBot 用户 ID，多个 ID 用逗号分隔。
- `phone_location` 只按需读取一次位置；高精度定位和地址反查需要显式确认。

用户 ID 是聊天平台传给 AstrBot 的发送者 ID。QQ OneBot 通常是 QQ 号；其他平台可能是 openid。可以从 AstrBot 收到消息时的日志中确认，例如日志中的 `user_id` 或发送者 ID。

保存后按顺序验证：

1. WebUI 中 Token 状态显示“已配置”。
2. 点击“测试 Operit”，预期显示在线和 Operit 版本。
3. 点击“刷新健康”。未配置健康库时显示未配置是正常的。
4. 在和 AstrBot 的授权私聊中发送：

   ```text
   观察一下当前手机，只观察，不要点击
   ```

5. 成功后再测试一个低风险动作：

   ```text
   打开手机设置
   ```

6. 最后再测试需要 Shizuku 的动作，例如暂停一个测试 App。确认包名正确，避免误操作。

第一轮测试不建议直接发送消息、评论、删除内容或支付操作。

如果你明确要复用私人陪伴授权，可以改为：

```json
{
  "allowed_user_ids": "",
  "use_private_companion_auth": true
}
```

这是可选扩展。插件不会修改私人陪伴的性格、记忆、主动消息或提示词。详见 [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md)。

### 7.1 安装验收清单

全部满足后才算安装完成：

- [ ] 服务器 `tailscale ping PHONE_TAILSCALE_IP` 成功。
- [ ] 手机 Shizuku 显示运行中，Operit 位于已授权应用中。
- [ ] Operit 普通对话能正常使用模型。
- [ ] Operit 能在本机完成一次无害工具调用。
- [ ] Operit 外部 HTTP 服务已开启，端口与插件配置一致。
- [ ] 服务器访问 Agent Card 成功。
- [ ] 带 Bearer Token 请求 `/api/health` 返回 HTTP 200。
- [ ] AstrBot 日志显示手机 Agent 插件及 LLM 工具已加载。
- [ ] WebUI“测试 Operit”成功。
- [ ] `phone_observe` 能返回手机状态。

如果前一项不通过，不要继续排查后一项。例如 `/api/health` 都无法访问时，反复重装 AstrBot 插件没有意义。

## 八、功能配置

### App 别名

默认支持“B 站”“哔哩哔哩”“快手”“优酷”“抖音”“抖音极速版”“微信”等名称。通过 `app_aliases_json` 添加别名：

```json
{
  "抖音": "com.ss.android.ugc.aweme",
  "抖音极速版": "com.ss.android.ugc.aweme.lite"
}
```

### 按需 App 策略

```json
{
  "sleep_guard_packages": "哔哩哔哩,快手,优酷",
  "sleep_guard_exempt_apps": "微信"
}
```

这两个字段只作为 `phone_sleep_mode` 的默认目标和例外列表。插件不会按时间段检查前台 App，也不会后台轮询；只有 AstrBot LLM 实际调用 `phone_app_policy` 或 `phone_sleep_mode` 时才执行禁用/恢复。可以直接说：

```text
别让我刷视频两小时
解除视频限制
禁用抖音极速版 30 分钟
恢复抖音极速版
```

`phone_location` 也是按需工具，只在用户明确询问位置或当前任务确实需要时调用；高精度定位和地址反查需要额外确认。

### 任务、提醒和审计

```text
观察一下当前手机
打开微信
30 分钟后提醒我喝水
查看刚才那个任务的状态
取消刚才的手机任务
```

后台任务会返回任务 ID，可查询、取消或重试。提醒保存在 `reminders_path` 指定的文件中；`phone_audit` 只记录动作元数据，不记录 Token 和消息正文。

### 高风险操作

发消息、评论、点赞、转发、删除、卸载和支付类任务需要显式确认。普通的打开 App、返回、锁屏和读取状态可以直接执行。

## 九、健康数据（可选）

手机插件本身不读取小米账号。需要单独部署 `xiaomi-health-sync`：

1. 在服务器安装项目及依赖。
2. 用项目提供的二维码登录小米账号。
3. 使用仓库内 [`deploy/xiaomi-sync`](deploy/xiaomi-sync) 的定时任务，并将 `/home/tauru/data/xiaomi_health_sync/data/health.db` 配置到 `health_db_path`（也可以填包含该文件的目录）。
4. 用 systemd timer 或其他计划任务定期同步。

插件只读查询 `xiaomi-sync` 的 SQLite，不会把小米 Token 上传到 GitHub 或聊天平台。健康查询会同时返回最近一次同步时间、成功状态和是否过期；同步失败时不会伪装成成功。成功后可以问：

```text
我今天走了多少步？
昨晚睡得怎么样？
最近心率和血氧正常吗？
```

## 十、可用工具

- `operit_task`：把自然语言 UI 任务交给 Operit。
- `operit_task_status`、`operit_task_cancel`、`operit_task_retry`：管理后台任务。
- `phone_action`：执行白名单手机动作。
- `phone_observe`：观察手机状态。
- `phone_location`：按需读取一次手机位置。
- `phone_app_policy`：由 LLM 选择 App 并禁用或恢复，可选自动恢复时间。
- `phone_sleep_mode`：临时限制视频 App。
- `phone_usage`：查询应用使用时长。
- `phone_reminder`：创建、查看和取消提醒。
- `phone_audit`：读取不含 Token 和消息正文的审计记录。
- `phone_health`：查询同步后的小米健康数据。

## 十一、故障排查

### `401 Unauthorized`

Operit Token 已失效或被重置。重新打开 Operit 外部 HTTP 页面，复制当前 Token 到插件配置，重启 AstrBot。

### `Connection reset by peer` 或 HTTP 超时

确认手机 Tailscale 在线，然后在 Operit 中关闭并重新打开外部 HTTP 服务。必要时强制停止并重新打开 Operit，同时关闭系统省电限制。

### 日志出现旧的 ADB 端口

确认 `control_backend` 是 `operit`，清空旧的 `adb_serial`，并新建一个对话。旧对话上下文可能仍然记得以前的 ADB 地址。

### 手机重启后 Shizuku 不可用

按照 Shizuku 页面提示重新启动服务，并确认 Operit 的相关权限仍然存在。

### Operit 能连接但不会操作

这通常不是网络问题，而是 Operit 没有配置模型、模型不可用，或者模型不会调用工具。依次确认：

1. Operit 已配置 API 地址、API Key 和模型名。
2. 在 Operit 普通对话页中，模型能正常回复。
3. 模型支持并允许工具调用。
4. Shizuku 正在运行，Operit 已获得授权。
5. Operit HTTP 服务正在运行且 Token 正确。

然后先测试：

```text
观察一下当前手机，只观察，不要点击
```

## 安全说明

- 只在 Tailscale 或可信内网开放 Operit HTTP 服务。
- 不要提交 Operit Token、小米 Token、SSH 密码或服务器配置。
- 使用 `allowed_user_ids` 限制手机控制权限。
- 本插件不接受任意 shell 命令。
- 定位不是后台功能，只在用户明确请求或当前任务需要时读取。
- Android 可能显示“由 Shell 管理”，这是系统对 `pm suspend` 来源的标记，插件不能修改。

## 使用过的项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [Operit](https://github.com/AAswordman/Operit)
- [Shizuku](https://github.com/RikkaApps/Shizuku)
- [Tailscale](https://github.com/tailscale/tailscale)
- [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)
- [mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge)
- [mi_fitness](https://pypi.org/project/mi-fitness/)
