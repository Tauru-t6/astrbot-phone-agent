# Phone Control · AstrBot 手机控制插件

让 AstrBot 连接 Android 手机的插件，提供两个主控制模式：

- `app`：Phone Control / Phone Control Bot + Shizuku，执行结构化命令；可选启用服务器模型驱动、每步重新观察的单应用界面任务。
- `operit`：OperitAI External HTTP，执行自然语言手机任务、截图/OCR、点击、滑动、输入和工作流。

Android 前端：[AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app)。旧配置别名 `phone_buddy`、`native_app`、`own_frontend` 映射到 `app`，`operitai`、`operit_ai` 映射到 `operit`。默认 `control_backend` 仍为 `operit`。

[项目主页](README.md) · [English Guide](README.en.md) · [界面任务与双设备](docs/UI_TASKS.md) · [更新日志](CHANGELOG.md) · [App API v2](docs/APP_API_V2.md)

## 0.10.0 界面任务与第二台手机

配套 App 0.4.0 增加独立 **Phone Control Bot**：个人版保留健康同步，Bot 版仅承担设备控制，不包含 Health Connect SDK、健康权限、健康读取和同步实现。两者包名、存储和安装身份独立。服务端按设备分别保存注册、状态和任务路由，保留原个人主设备；Bot 手机离线不会回退到个人手机。这是同一部署内的设备分工，不是不同账户之间的安全隔离。

`phone_ui_task` / App「让手机完成一件事」先提出范围，再由你核对设备、单个应用、允许输入原文、完成条件与步数后批准。B 站搜索提供模板；浏览、非敏感设置和草稿使用自定义任务，敏感步骤人工接手，草稿不自动发送。默认最多 8 步、配置最多 12 步，不承诺所有 App 都可操作。

需同时开启 `enable_ui_tasks`、`enable_task_execute`、`enable_observe_tool`，并设置应用白名单；执行设备要手动启用任务输入法，连续操作还需明确打开两项 UI 免确认。模型会收到当前页面节点文字。配置、完成判定、取消与排障详见[界面任务与双设备](docs/UI_TASKS.md)。

## 0.9.1 审查修复

本版收紧设备绑定、授权累计预算、核验布尔值、取消恢复和聊天确认边界。旧短期 grant 需重新签发；受限任务通过 Relay 兜底时须一起更新 Relay。App 0.3.26 与关怀桥接 0.3.0 保持兼容。详见 [升级说明](docs/REVIEW_FIXES_0_9_1.md)。

## 0.9.0 自主决策与 7.x 适配

开启 `enable_llm_task_planner` 可生成有序多步提案；未知应用或含糊目标会要求澄清，现有审批、动作范围和逐步核验继续生效。关怀桥接 0.3.0 适配 Private Companion 7.x，参考对应私聊的近期对话，减少忙碌时和重复内容的打扰。详见[适配说明](docs/PRIVATE_COMPANION_719_ADAPTATION.md)和[真机验证记录](docs/PHYSICAL_DEVICE_VALIDATION.md)。

## 本地优化版的行为变化

- 私聊批准/拒绝任务只依赖任务执行开关与用户授权，不再要求开启夜间守护。
- 控制台的「任务与审计」可创建手机提醒；失败后重试复用请求 ID，避免重复提醒。刷新保留未保存的配置和策略输入，操作进行时防止重复提交，隐藏页面暂停自动轮询。
- 设备命令接口重新检查总开关、App 后端、各功能开关和受保护包名；`phone_action(screen_text)` 同样遵守观察开关和群聊隐私限制。
- Operit 直连超时、响应断开或明确拒绝时不自动重放到 Relay；只有明确连接未建立时才自动切换。执行结果未知时，先确认手机实际状态，再决定是否手动重试。
- 后台任务重试受后端、任务状态和并发额度限制。通知落盘失败可安全重试；任务、提醒和通知共用原子文件写入，审计仅从日志尾部读取，避免日志增大拖慢界面。
- Relay 仅允许有效租约的领取者续租/提交结果，已领取任务计入容量；空轮询不再写盘。短传输时限同步到设备命令期限，成功核验不能使用操作之前的旧状态。临时策略续期/提前恢复失败保留原自动恢复安排。

详细变更及验证范围见 [优化记录](docs/OPTIMIZATION_REPORT.md)。

## 一、整体结构

```text
聊天平台 -> AstrBot -> astrbot_plugin_phone_agent
                         |-- app: 结构化 HTTP -> AstrBot Phone Agent App -> Shizuku
                         |                  \-> Relay（可选）
                         \-- operit: External HTTP -> OperitAI -> Shizuku
```

健康数据是独立链路：App 0.3.28 可让用户选择一个 Health Connect 来源 App 并上传按日摘要；插件 0.9.3 同时保留服务器 `xiaomi-sync`（仓库 `xiaomi-health-sync`）同步到 SQLite 的方式。`phone_health` 按配置选择一个来源，独立 Android Health Bridge 不是必需组件。

## 二、前置条件

- 一台运行 AstrBot 的 Linux 服务器。
- 手机和服务器通过可信局域网或同一个 Tailscale tailnet 互访。也可配置后文的 Relay 作为异步兜底。
- 手机按所选模式安装 AstrBot Phone Agent App 或 Operit，并安装 Shizuku。
- 在 Shizuku 中授权实际使用的手机应用；不要求两个前端同时安装。
- 仅 Operit 模式需要在 Operit 配置可用的聊天模型，并确认模型能正常回复和调用工具；App 模式的聊天由 AstrBot 提供。
- AstrBot 4.22 或更高版本。
- 如果需要健康数据，选择能向 Health Connect 写入所需类型记录的健康 App → Phone Agent App，或单独部署服务器端 `xiaomi-sync`。

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

## 四、App 模式：AstrBot Phone Agent App

App 模式使用 [AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app) 和 Shizuku。安装 App 后按其引导启动 Shizuku、授权 App，并在连接设置中填写插件地址、Dashboard JWT 和共享命令 Token。聊天在 QQ 等 AstrBot 会话中进行，当前 App 不需要聊天 API 配置。App 命令服务通常监听 `8260`，具体以 App 页面为准。

先在手机开发者选项打开无线调试，按 Shizuku 的配对指引启动服务；确认 Shizuku 首页显示运行中，再启动 App 并允许授权。手机重启后通常要重新启动 Shizuku，手机与服务器之间的控制链路仍是 HTTP，不需要服务器持续连着 ADB。

三个凭据用途不同：聊天使用 AstrBot 聊天 API 凭据；插件状态、注册和提醒接口使用 AstrBot 管理端登录鉴权；`app_shared_token` 只用于插件访问手机命令服务。不要将聊天 API Key 当作管理端登录 Token 或共享命令 Token。

允许 App、Tailscale 在后台运行，并按使用功能授予通知、定位和使用情况访问等权限。Shizuku 可用并不等于系统已经给了定位或通知权限；状态页显示哪些能力不可用时，先按 App 提示补齐对应权限。

App 只接受受支持的命令和结构化参数，包括状态、应用管理、导航、截图、读屏、点击、长按、滑动、输入、定位和通知等。App 0.4.0 与插件 0.10.0 另提供按页面节点执行的单应用界面任务，由服务器模型规划下一步，App 本身不解释自由文本、不执行任意 shell。敏感命令仍受手机本地确认控制；两项 UI 免确认必须由执行设备用户明确开启，服务端任务审批不会替代这项设置。

App 通过 `POST /device/register` 注册地址。旧版本只发送一个 `base_url` 也兼容；服务器上的 `app_direct_urls` 不会被旧的 Tailscale 注册覆盖。`GET /app/state` 不探测手机，手机离线时也能返回提醒、最近命令和最近 100 条时间线。

App 提醒接口位于插件前缀 `/astrbot_plugin_phone_agent` 下：

- `POST /app/reminders`：`{"text":"喝水","minutes":15,"request_id":"UUID"}`，分钟数 1–10080，UUID 幂等。
- `POST /app/reminders/cancel`：`{"reminder_id":"app-..."}`，重复取消安全返回 `already_finished`。

App 提醒保存 `source=app`、空聊天会话。到期先写入 `reminder_due` 时间线，App 轮询并按 `notification_id` 去重后通知；不会发送 QQ，也不会额外直接 push。聊天创建的提醒继续投递原会话。重启后 24 小时内的过期提醒会补发，更早的提醒静默记为 `reminder_expired`。详见 [`docs/APP_API_V2.md`](docs/APP_API_V2.md)。

提醒与幂等回执保存在 `reminders_path`；已结束请求 ID 保留 30 天。时间线保存在 `app_state_path`，最近 100 条持久化，创建和取消事件不触发通知。手机离线、后台任务调度和通知权限都会影响通知到达时间；此功能不是 Android 精确闹钟。不要将含提醒文本的状态文件提交到 Git。

## 四-B、配置 Operit 和 Shizuku

选择 Operit 模式时必须完成本节；App 模式无需配置 Operit 模型或 External HTTP。仅仅打开 Operit HTTP 端口并不能执行自然语言手机任务。

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

也可在 AstrBot 插件市场搜索 **Phone Control**，或通过 GitHub 仓库地址安装。以下保留 ZIP 和 Git 两种手动安装方式，方便不同部署环境使用。

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
├── device_app.py
├── task_runner.py
├── care_scheduler.py
├── storage.py
├── http_transport.py
├── metadata.yaml
├── _conf_schema.json
├── pages/
│   └── phone-control/
│       └── index.html
└── README.zh-CN.md
```

安装或升级时复制完整插件目录，避免遗漏内部模块。如果变成 `astrbot_plugin_phone_agent/astrbot-phone-agent-main/main.py`，说明多套了一层目录，AstrBot 不会正确加载。

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

`systemctl --user` 必须由拥有 AstrBot 的用户执行；如果服务不叫 `astrbot.service`，先用 `systemctl --user list-units --type=service` 查找。出现“Failed to connect to bus”时检查用户服务会话，不要立刻改成重启一个不相关的系统服务。需要退出 SSH 后常驻运行时，由管理员按部署要求为该用户启用 linger。

加载成功时能在日志看到插件名称和工具注册信息，例如：

```text
Loading plugin astrbot_plugin_phone_agent
Added llm tool: operit_task
Plugin astrbot_plugin_phone_agent (...)
```

具体日志格式依 AstrBot 版本变化；可选工具的 `enable_*` 开关决定是否允许调用。即使宿主显示已发现某个工具，也要先开启相应功能。`phone_observe` 和主动上下文桥接默认关闭。如果日志提示找不到 `quart` 或 AstrBot API，先确认 AstrBot 版本不低于插件声明版本，并确保插件运行在 AstrBot 自己的 Python 环境中。

## 六、打开 WebUI

1. 登录 AstrBot Dashboard。
2. 打开“插件”页面。
3. 找到 `astrbot_plugin_phone_agent`。
4. 打开插件 Pages 中的“Phone Control/手机 Agent 控制台”。
5. 在配置中选择 App 或 Operit，保存配置后点击“测试当前后端”。

页面提供：

- 当前后端在线状态和连接测试；App 模式检测手机命令服务，Operit 模式检测 External HTTP。
- App 有序直连地址、App 共享命令 Token、Operit 地址与 Token、控制后端和用户白名单配置。
- App 别名 JSON 编辑。
- 按需 App 策略与默认目标 App 配置，不会后台轮询。
- 临时限制启动/解除，到期自动恢复。
- 一次性定位读取入口。
- 健康摘要、后台任务、提醒和最近审计记录。
- 提醒可直接取消，页面可开启 15 秒自动刷新。后台任务的取消等待和重试功能由 `operit_task_cancel`、`operit_task_retry` 工具提供；使用前确认当前页面版本的对应按钮可用，也可在聊天中指定任务 ID 操作。
- 后台任务元数据会保存到 `tasks_path`，AstrBot 重启后仍能查看；重启时正在执行的任务会标记为中断。

页面使用 AstrBot Dashboard 自带的登录鉴权。Token 只会显示“已配置”，不会回显。

如果页面显示“插件页面桥接不可用”，先确认安装目录中存在 `pages/phone-control/index.html`，再强制刷新浏览器（`Ctrl+F5`）。仍然失败时重启 AstrBot，并确认页面源码加载了 `/api/plugin/page/bridge-sdk.js`。

## 七、配置插件

可以在手机 Agent 控制台或 AstrBot 原生插件配置页填写。推荐先只配置最小必需字段：

Operit 模式配置：

```json
{
  "enabled": true,
  "control_backend": "operit",
  "operit_base_url": "http://phone-tailnet.example:8094",
  "operit_token": "YOUR_OPERIT_TOKEN",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "use_private_companion_auth": false
}
```

App 模式配置：

```json
{
  "enabled": true,
  "control_backend": "app",
  "app_shared_token": "同一个随机命令Token",
  "app_direct_urls": "http://phone-lan.example:8260,http://phone-tailnet.example:8260",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "enable_reminder_tools": true,
  "enable_audit_tool": true
}
```

`app_direct_urls` 是逗号分隔的有序列表，内网地址建议在 Tailscale 地址前。配置地址优先于 App 注册地址；每个候选地址会先做最多约 3 秒健康检查，所有地址和 Relay 共用一个命令总超时。

| 常用配置 | 用途及默认行为 |
| --- | --- |
| `enabled` | 总开关，默认 `true` |
| `app_command_timeout_seconds` | App 命令总超时默认 200 秒，运行时限制在 10–300 秒 |
| `operit_timeout_seconds` | Operit 单次请求等待默认 120 秒 |
| `app_state_path` | 时间线状态，默认 `data/plugin_data/astrbot_plugin_phone_agent/phone_agent_app_state.json` |
| `reminders_path` | 提醒和幂等回执，默认 `data/plugin_data/astrbot_plugin_phone_agent/phone_agent_reminders.json` |
| `tasks_path` | 后台任务元数据，默认 `data/plugin_data/astrbot_plugin_phone_agent/phone_agent_tasks.json` |
| `policy_state_path` | 限制策略及到期时间，默认 `data/plugin_data/astrbot_plugin_phone_agent/phone_agent_policies.json` |
| `audit_log_path` | 动作元数据日志，默认 `data/plugin_data/astrbot_plugin_phone_agent/phone_agent_audit.jsonl` |

这些默认路径统一位于 AstrBot 的 `data/plugin_data/astrbot_plugin_phone_agent/` 下，便于插件数据迁移和备份。自定义相对路径也会放在这个目录下；只有绝对路径才会写到外部位置。提醒、时间线和任务文件可能含个人文本，不要提交到 Git。

Operit 模式最少需要配置：

- `operit_base_url`：手机 Tailscale 地址加 `:8094`。
- `operit_token`：Operit 外部 HTTP 页面里的 Bearer Token。
- `allowed_user_ids`：允许控制手机的 AstrBot 用户 ID，多个 ID 用逗号分隔。
- `phone_location` 只按需读取一次位置；高精度定位和地址反查需要显式确认。

用户 ID 是聊天平台传给 AstrBot 的发送者 ID。QQ OneBot 通常是 QQ 号；其他平台可能是 openid。可以从 AstrBot 收到消息时的日志中确认，例如日志中的 `user_id` 或发送者 ID。

`allowed_user_ids` 留空且未启用可用的 Private Companion 授权时，工具拒绝调用，不是允许所有人。白名单优先；配置了明确白名单时，不会再用桥接扩展该列表。Dashboard 页面由管理员登录鉴权，不能把聊天白名单当作 Dashboard 访问控制。

保存后按顺序验证：

1. WebUI 中 Token 状态显示“已配置”。
2. 点击“测试当前后端”，预期显示当前配置模式在线；Operit 可返回其版本，App 使用手机命令服务的健康结果。
3. 点击“刷新健康”，核对返回的数据来源、日期和新鲜度。未接入 Health Connect 且未配置 SQLite 时，显示暂无可用健康数据是正常的。
4. 如果需要使用手机观察功能，先在配置中开启 `enable_observe_tool=true`，再在和 AstrBot 的授权私聊中发送：

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

### 7.1 Operit 模式验收清单

全部满足后才算安装完成：

- [ ] 服务器 `tailscale ping PHONE_TAILSCALE_IP` 成功。
- [ ] 手机 Shizuku 显示运行中，Operit 位于已授权应用中。
- [ ] Operit 普通对话能正常使用模型。
- [ ] Operit 能在本机完成一次无害工具调用。
- [ ] Operit 外部 HTTP 服务已开启，端口与插件配置一致。
- [ ] 服务器访问 Agent Card 成功。
- [ ] 带 Bearer Token 请求 `/api/health` 返回 HTTP 200。
- [ ] AstrBot 日志显示手机控制插件及 LLM 工具已加载。
- [ ] WebUI“测试当前后端”成功。
- [ ] `phone_observe` 能返回手机状态。

如果前一项不通过，不要继续排查后一项。例如 `/api/health` 都无法访问时，反复重装 AstrBot 插件没有意义。

### 7.2 App 模式验收清单

- [ ] Shizuku 正在运行，AstrBot Phone Agent App 在已授权列表中。
- [ ] 手机命令服务已启动，共享命令 Token 与插件 `app_shared_token` 一致。
- [ ] `app_direct_urls` 使用自己的地址，内网优先、Tailscale 次之。
- [ ] `control_backend=app`，WebUI“测试当前后端”成功。
- [ ] 允许的聊天用户可完成一次打开设置等低风险 `phone_action`。
- [ ] `enable_reminder_tools=true` 时，App 提醒能够创建、取消并同步到期事件。
- [ ] 手机暂时离线时，App 状态接口仍能返回已有提醒和时间线。

## 八、功能配置

### 可选功能开关

| 功能 | 开关 | 工具或入口 |
| --- | --- | --- |
| 按需观察 | `enable_observe_tool` | `phone_observe`；App 目前读取设备状态，Operit 可读取前台和屏幕摘要 |
| 使用统计 | `enable_usage_tool` | `phone_usage` |
| 提醒 | `enable_reminder_tools` | `phone_reminder` 和 App 提醒写入 |
| 定位 | `enable_location_tool` | `phone_location`、WebUI 一次性定位 |
| App 策略 | `enable_policy_tools` | `phone_app_policy`、`phone_sleep_mode` |
| 审计查询 | `enable_audit_tool` | `phone_audit`、WebUI 审计 |
| 健康查询 | `enable_health_tools` | `phone_health`、WebUI 健康 |

上述开关默认关闭；可选观察不会因打开插件而自动启动。动作审计写入与是否开放审计查询是不同事项。

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

`phone_app_policy` 的 `minutes` 为 0 表示保持到显式恢复，正数表示临时策略；`phone_sleep_mode` 支持 5–1440 分钟。系统设置、SystemUI、Operit 和 App 自身等控制包受到保护。包名必须存在且正确，内置别名未必匹配你的地区版、HD 版或极速版，请按手机实际包名覆盖。策略状态保存在 `policy_state_path`，临时限制到期会尝试自动恢复；永久策略不会因重启自动解除。

### 任务、提醒和审计

```text
观察一下当前手机
打开微信
30 分钟后提醒我喝水
查看刚才那个任务的状态
取消刚才的手机任务
```

Operit 后台任务会返回任务 ID，可查询、取消或重试。`background=true` 启用后台任务，默认并发上限 `max_background_tasks=2`，可配置为 1–8；元数据保存在 `tasks_path`，重启时未完成任务标为 `interrupted`，不会假装完成。`operit_task_cancel` 取消的是插件本地等待，手机端已经执行的 HTTP 请求可能仍在运行；重试会创建新任务 ID，先检查是否已产生副作用。

提醒保存在 `reminders_path`；App 创建的提醒和聊天提醒的投递路径见 App 章节。`phone_audit` 查询 `audit_log_path` 的动作元数据，不记录 Token 和消息正文；提醒和任务文件仍可能含个人文本，应限制读权限。

### 高风险操作

Operit 自然语言任务中的发消息、评论、点赞、转发、删除、卸载、支付、输入文字和点击按钮类任务会要求显式确认。App 支持封闭指令集内的点击、长按、滑动、滚动、输入和按键，由插件传入明确参数；它不具备自动看图规划任意界面任务的循环。界面操作、读屏、定位、关闭/暂停应用和通知默认需要手机端确认，截图默认预先允许。任务提案还须批准并逐步核验。结果 `success=false` 时应按错误处理，不应在聊天中宣称已经完成。

### Private Companion 桥接

`companion-context` 是独立可选插件，默认 `enabled=false`，并非安装主插件就自动启用。源码和配置位于 [`companion-context`](companion-context)。它与前文的 `use_private_companion_auth` 是两个不同功能：后者只复用授权名单，前者会为主动消息生成添加当次上下文和可选语气提示。

安装方式：从主插件目录复制 `companion-context` 为 AstrBot 的同级插件目录，目录名用其 `metadata.yaml` 声明的名称：

```bash
cd "$HOME/data/plugins"
cp -R astrbot_plugin_phone_agent/companion-context astrbot_plugin_phone_companion_context
```

如果你的数据目录不在 `$HOME/data`，替换命令中的路径。重载插件后可使用如下配置，先只开健康上下文：

```json
{
  "enabled": true,
  "relationship_mode_enabled": true,
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "private_only": true,
  "include_health": true,
  "include_screen": false,
  "autonomous_decision_enabled": false
}
```

当前实现还要求 `relationship_mode_enabled=true` 才会执行上下文扩充；`allowed_user_ids` 为空时不会观察任何用户。需要屏幕粗粒度信号时，必须同时开启主插件的 `enable_observe_tool=true` 和桥接的 `include_screen=true`。

手机状态桥接只采集前台包名、粗粒度活动类别和电量，不返回屏幕原文、联系人或消息文本。App 模式使用当前绑定手机的新鲜心跳，Operit 模式请求一次粗粒度观察；两者都受主插件观察开关与 `include_screen` 控制。健康上下文需要主插件健康开关和桥接 `include_health`，并遵循 `health_data_source`，选择 Health Connect 快照或 SQLite。

默认整体等待上限为 `timeout_seconds=20`，手机状态缓存最多 `screen_cache_seconds=90` 秒且受原始采样时间限制。固定 SQLite 来源可使用 `health_cache_seconds=900` 秒缓存；Health Connect 和 `auto` 每次核对当前快照，关闭、撤权或换绑后不继续使用旧摘要。接口不兼容或目标插件不可用时跳过桥接，可在日志检查桥接状态。

关系语气和主动关怀是额外选择：`autonomous_decision_enabled` 默认关闭，模式默认 `preview`。配合主插件调度器，关怀根据近期私聊和有效摘要解析 `no_action` / `send_message`，并执行冷却、静默时段、每日评估/发送额度、去重和投递核验。`care_auto` 允许范围内的真实投递，`low_risk_auto` 为兼容别名；不会因此取得手机动作权限。Private Companion 7.x 通过扩展门面发现，关怀调度独立于其原生主动联系。桥接不重写持久人格和记忆；关系提示仅影响当次生成。配置字段见 [`companion-context/_conf_schema.json`](companion-context/_conf_schema.json)。

## 九、健康数据（可选）

手机插件本身不读取健康 App 账号。App/Operit 控制和健康数据是独立链路。插件 0.9.3 提供两种健康数据来源，开启 `enable_health_tools` 后按需选择。

**Health Connect（App 0.3.28）**：

1. 选择实际写入数据的健康 App，并在 Health Connect 中核对来源包名；“最近访问”不代表已经写入每种数据。
2. 打开 App「我的 → Health Connect」，输入来源 App 包名并授权需要的类别，支持部分授权。
3. 开启默认关闭的「同步健康摘要到服务器」，点击「立即同步健康摘要」。上传只沿用已配置的插件地址与 Dashboard JWT。
4. 设置 `health_data_source`：`auto` 优先当前手机的新鲜快照，否则回退原 SQLite；`health_connect` 只用手机快照；`health_db` 只用 SQLite。不会把两个来源的指标相加。

支持对接可接入 Health Connect 的健康 App，小米运动健康国区默认隐藏了，需要的可自行探索。

选择 App 包名后只读取单一来源最近 7 天已授权且实际写入的步数、睡眠、心率、血氧和活动热量，按日汇总后上传，缺失数据不补零。系统授权与服务器同步是两个独立开关。App 约每 15 分钟尝试同步，没有申请后台健康读取权限，系统拒绝时请回到 App 手动同步。关闭同步会停止读取并请求清除服务器快照，离线清除会在恢复连接后重试；系统撤权在下次读取时上报。其他 App 写入类别与兼容性取决于其实现，目前仅 `com.mi.health` 完成过实机验证。摘要用于已开启的健康查询和关怀，可能交给用户配置的模型服务。详细边界见 [Health Connect 接入说明](docs/HEALTH_CONNECT.md)。

**原有 xiaomi-sync / SQLite**：需要单独部署 `xiaomi-health-sync`，下方 9.1–9.3 是这条来源的安装与验证步骤。

1. 在服务器安装项目及依赖。
2. 用项目提供的二维码登录小米账号。
3. 使用仓库内 [`deploy/xiaomi-sync`](deploy/xiaomi-sync) 的定时任务，并将 `/home/YOUR_USER/data/xiaomi_health_sync/data/health.db` 配置到 `health_db_path`（也可以填包含该文件的目录）。
4. 用 systemd timer 或其他计划任务定期同步。

选择此来源时，插件只读查询 `xiaomi-sync` 的 SQLite，不会把小米 Token 上传到 GitHub 或聊天平台。健康查询返回实际 `source`（`health_connect` 或 `health_db`）、最近同步时间、成功状态和是否过期；同步失败时不会伪装成成功。成功后可以问：

```text
我今天走了多少步？
昨晚睡得怎么样？
最近心率和血氧正常吗？
```

### 9.1 安装上游同步工具

上游地址为 [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)。按上游当前安装说明建立虚拟环境、安装依赖并完成小米账号二维码登录；不要把账号登录能力误认为手机插件自带。先手动完成一次同步，确认产生 `data/health.db` 和 `sync_runs` 记录，再交给 timer。

建议布局：

```text
$HOME/data/xiaomi_health_sync/
  .venv/bin/python
  data/token.json
  data/health.db
  sync-health.sh
```

仓库包装脚本执行 `python -m health_vault.cli sync --days N`，默认回看 7 天，允许 1–90 天；`HEALTH_SYNC_DAYS` 控制同步范围。它把 Token 文件权限设为 `600`，`umask 077` 限制新文件可见性。插件侧 `phone_health(days=...)` 查询范围为 1–30 天。

### 9.2 配置 systemd user timer

先复制包装脚本并安装权限：

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/xiaomi-sync/sync-health.sh "$HOME/data/xiaomi_health_sync/"
chmod 700 "$HOME/data/xiaomi_health_sync/sync-health.sh"
```

仓库中的 service 模板包含需要按部署修改的路径。可以将下面内容保存为 `~/.config/systemd/user/xiaomi-health-sync.service`，用 systemd 的 `%h` 引用当前用户主目录；这样也会覆盖脚本里的示例默认目录：

```ini
[Unit]
Description=Sync Xiaomi Fitness health data

[Service]
Type=oneshot
Environment=XIAOMI_SYNC_DIR=%h/data/xiaomi_health_sync
Environment=XIAOMI_SYNC_PYTHON=%h/data/xiaomi_health_sync/.venv/bin/python
Environment=HEALTH_DATA_DIR=%h/data/xiaomi_health_sync/data
Environment=HEALTH_SYNC_DAYS=7
ExecStart=%h/data/xiaomi_health_sync/sync-health.sh
UMask=0077
NoNewPrivileges=true
```

复制现有 timer 并启动：

```bash
cp deploy/xiaomi-sync/xiaomi-health-sync.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now xiaomi-health-sync.timer
systemctl --user start xiaomi-health-sync.service
systemctl --user list-timers xiaomi-health-sync.timer
journalctl --user -u xiaomi-health-sync.service -n 60 --no-pager
```

timer 在启动后约 3 分钟运行，此后每 30 分钟调度。对常驻服务器，可按系统设置为该用户启用 linger，让退出 SSH 后仍有用户服务管理器运行。Docker 部署则需要在容器中以只读卷映射健康数据库，并填写容器内可见路径。

### 9.3 判断数据是否新鲜

开启 `enable_health_tools=true`，设置 `health_db_path` 后，先用 WebUI“刷新健康”验证。插件以只读 SQLite 连接读取 `daily_metrics` 和 `sync_runs`，返回最近一次同步的 `ok`、`finished_at`、`age_minutes`、`stale` 和错误。

SQLite 最近一次同步失败、没有同步记录或完成时间超过 180 分钟时，数据会标为 stale；Health Connect 快照采集超过 180 分钟也会过期。`available=true` 只代表有可查询数据，不代表 `fresh=true`。某天暂无数据时可能返回最近可用一天，因此回答“今天”问题要核对返回的 `date`。这两条同步链路都不需要独立 Android Health Bridge。

## 位置坐标和百度地图链路

开启 `enable_location_tool=true` 后，`phone_location` 按需获取一次位置。高精度 `high_accuracy=true`、地址请求 `include_address=true` 需要用户确认。App 返回 WGS84，Operit 返回其实际报告的坐标系；插件不按地理范围猜坐标，也不伪造转换值。

WebUI 的「检查地图接入」只检查 AstrBot 已注册的 MCP 工具，不采集或发送手机坐标。百度地图国内逆地理编码要求 BD09LL。当前启用的 MCP 包含逆地理编码、地点和路线工具，但没有坐标转换工具，因此 WGS84 / GCJ02 定位会明确提示转换缺失，不会直接把原坐标交给百度或误报地址查询成功。`include_address` 在当前 App 定位端返回 `not_supported`；App 会报告采集时间、时效、精度与模拟位置标记。插件不保存坐标，也不后台追踪。更多接入状态见 [百度地图 MCP 对接说明](docs/BAIDU_MAP_MCP.md)。

## Relay 兜底部署

Relay 是一个由你自己托管的 Bearer 鉴权队列，源码位于 [`deploy/relay/relay_server.py`](deploy/relay/relay_server.py)，只使用 Python 标准库。它负责接收任务、让手机领取、保存回传结果，不负责执行手机动作，也不提供语义理解。

App 和 Operit 可以分别使用 Relay，但两者消费的消息格式不同：App 领取结构化 command envelope，Operit 工作流领取自然语言文本。**不要让 App 和 Operit 同时消费同一个队列**；多手机或两种执行器并存时，为每一条独立控制链部署不同端口、状态文件和 Token。

### 部署步骤一：建立服务目录和环境文件

下面使用运行服务的普通用户主目录；从插件仓库根目录执行：

```bash
mkdir -p "$HOME/phone-agent-relay" "$HOME/data" "$HOME/.config/phone-agent-relay"
cp deploy/relay/relay_server.py "$HOME/phone-agent-relay/"
chmod 700 "$HOME/.config/phone-agent-relay"
```

在 `~/.config/phone-agent-relay/relay.env` 保存以下配置，替换占位 Token：

```dotenv
RELAY_TOKEN=REPLACE_WITH_A_RANDOM_LONG_TOKEN
RELAY_HOST=127.0.0.1
RELAY_PORT=8791
RELAY_REQUIRE_DEVICE_ID=0
RELAY_RATE_LIMIT=120
RELAY_TASK_RATE_LIMIT=30
```

可用密码管理器生成随机 Token，或在自己的终端运行 `openssl rand -hex 32`。不要把实际 Token 放入文档或聊天。保存后执行：

```bash
chmod 600 "$HOME/.config/phone-agent-relay/relay.env"
mkdir -p "$HOME/.config/systemd/user"
```

### 部署步骤二：systemd user 服务

在 `~/.config/systemd/user/phone-agent-relay.service` 保存：

```ini
[Unit]
Description=Phone Agent relay queue
After=network.target

[Service]
Type=simple
EnvironmentFile=%h/.config/phone-agent-relay/relay.env
Environment=RELAY_STATE=%h/data/phone_agent_relay_state.json
ExecStart=/usr/bin/python3 %h/phone-agent-relay/relay_server.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

然后启动：

```bash
systemctl --user daemon-reload
systemctl --user enable --now phone-agent-relay.service
systemctl --user --no-pager status phone-agent-relay.service
journalctl --user -u phone-agent-relay.service -n 60 --no-pager
```

仓库也提供 [`phone-agent-relay.service`](deploy/relay/phone-agent-relay.service) 系统级模板。若使用它，必须按自己的部署改好 Token、`ExecStart`、状态文件、`ReadWritePaths` 和服务用户；不要将系统级模板的 `multi-user.target` 直接当成用户服务的启动目标。主文上面给出的 user unit 使用 `%h` 和 `default.target`，适合 Ubuntu 的 systemd user 服务。

退出 SSH 后要继续常驻运行，可由管理员为运行服务的账户设置 linger。检查用的是 `systemctl --user` 还是系统级 `sudo systemctl`，不要把两套服务混为同一个实例。

### 部署步骤三：HTTPS 反向代理

服务默认只监听 `127.0.0.1:8791`。用 Nginx 或 Caddy 对外提供 HTTPS，不要直接把无加密端口暴露公网。Caddy 站点片段示例：

```caddy
relay.example {
    reverse_proxy 127.0.0.1:8791
}
```

将 `relay.example` 替换为自己的域名并配置 DNS、证书和防火墙；示例域名本身不能用于生产。反向代理要透传 `Authorization` 和 `X-Relay-Device-ID`，不能把响应长期缓存。插件的 `relay_base_url` 填队列实际根地址；如果挂在 `/phone-relay` 子路径，需要反代正确剥离前缀。

验证健康接口：

```bash
read -rsp "Relay Token: " PHONE_RELAY_TOKEN; echo
curl --max-time 10 -H "Authorization: Bearer $PHONE_RELAY_TOKEN" \
  "https://relay.example/health"
unset PHONE_RELAY_TOKEN
```

预期返回 `success=true` 和 `service=phone-agent-relay`。401 检查 Token；404 检查反代路径；无法建立 TLS 检查域名和证书。

### 部署步骤四：手机消费端

**App 模式**：在 App 设置填写同一个 Relay 地址与 Token，启动 App 的执行服务。App 负责解析结构化 envelope、调用命令执行器、回传 result envelope，不需要再建 Operit 自然语言工作流。前后台调度受 Android 版本和省电策略影响。

**Operit 模式**：在 Operit 工作流编辑器创建定时工作流。工具节点的引用语法以安装的 Operit 版本为准：

1. `schedule` 触发器。WorkManager 周期性后台任务通常最短 15 分钟，不保证精确时刻执行。
2. `http_request` 节点：`GET https://relay.example/poll`，请求头带 `Authorization: Bearer <relay-token>`。
3. 条件节点：返回的 `task` 为 `null` 时结束；有任务则记录 `task.id` 和 `task.message`。
4. `send_message_to_ai` 节点：内容使用 `task.message`，让 Operit 在手机执行。
5. `http_request` 节点：`POST https://relay.example/result`，回传下列 JSON；`success` 应使用实际执行结果，失败时也回传错误：

```json
{
  "task_id": "从poll响应取得的task.id",
  "success": true,
  "ai_response": "实际AI结果",
  "error": ""
}
```

字符串需要按 JSON 正确转义，不能直接拼接包含引号或换行的 AI 回复。耗时可能超过 5 分钟时，需要在任务运行期间定期 `POST /renew`，例如每 2 分钟一次，请求为 `{"task_id":"已领取的任务ID"}`。

### 部署步骤五：插件配置

```json
{
  "relay_base_url": "https://relay.example",
  "relay_token": "与Relay服务和手机消费端一致的Token",
  "max_background_tasks": 2,
  "tasks_path": "data/plugin_data/astrbot_plugin_phone_agent/phone_agent_tasks.json"
}
```

保留直连地址作为首选。App 命令先按直连候选地址尝试，传输失败时才入 Relay，同一个命令 ID 在全部路径中复用；匹配的设备拒绝结果不会再入队。Operit 的传输函数也保留 Relay fallback，但部分聊天工具先做 Operit 健康预检；预检失败时可能直接返回不可用，不能把当前实现描述成“任何断线场景都保证自动入队”。以调用结果和 `relay_fallback` 审计为准。

### 租约、隔离、容量和时效

| 项目 | 当前默认或行为 |
| --- | --- |
| 领取租约 | 5 分钟，`POST /renew` 更新租约；超时未完成的任务会重新进入可领取队列 |
| 状态存储 | `RELAY_STATE` 指定的 JSON，保存队列和回传结果 |
| 待处理容量 | 最多 64 个 pending 任务 |
| HTTP 请求体 | 最大 64 KiB |
| 普通请求限流 | `RELAY_RATE_LIMIT=120`，每 60 秒窗口计数 |
| 创建任务限流 | `RELAY_TASK_RATE_LIMIT=30`，每 60 秒窗口计数 |
| 结果正文 | `ai_response` 最多保留末尾 6000 字符，大截图等结果应优先走直连 |

开启 `RELAY_REQUIRE_DEVICE_ID=1` 时，手机在 `/poll`、`/renew`、`/result` 都要带同一个 `X-Relay-Device-ID`。此机制校验**领取后的任务归属**，防止其他设备续租或回传它；当前创建接口没有目标设备路由字段，不能保证任务只被某个指定手机首先领取。需要严格指定手机时使用独立实例和 Token。

收到 HTTP 429 时按 `Retry-After` 退避，不要忙轮询。经过反向代理时多个调用方可能共享源 IP，对限流有影响。

App 的命令包含 `created_at + deadline` 时效，过期后会被手机拒绝；如果后台 15 分钟才轮询一次，默认约 200 秒的 App 命令早已过期，不能承诺任意长时间离线后还会执行。Operit 的自然语言 Relay 任务没有同样的 App 命令 deadline，插件等待超时后手机可能继续执行。超时或取消等待均不代表手机副作用已撤销，重试前先核实结果。

## 十、可用工具

- `operit_task`：在 Operit 模式把自然语言 UI 任务交给 Operit；App 模式明确拒绝。
- `operit_task_status`、`operit_task_cancel`、`operit_task_retry`：管理后台任务。
- `phone_action`：执行白名单手机动作。
- `phone_observe`：App 模式读取设备状态；Operit 模式观察前台应用、屏幕摘要和电量。
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

确认 `control_backend` 是实际使用的 `app` 或 `operit`，清空旧的 `adb_serial`，并新建一个对话。旧对话上下文可能仍然记得以前的 ADB 地址。不要为排查 App 命令 HTTP 错误而让服务器全端口扫描手机的无线调试接口。

### 手机重启后 Shizuku 不可用

按照 Shizuku 页面提示重新启动服务，并确认实际使用的 App 或 Operit 的相关权限仍然存在。

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

### QQ 能聊天，但 App 显示插件未连接或不能控制手机

QQ 聊天通道、插件管理端和手机命令服务分别连接。先检查 Dashboard JWT 和插件路由是否返回 401/404，再检查手机命令服务是否启动，最后检查 `app_shared_token` 两端是否一致。当前 App 不承载聊天，更换模型 API Key 不能修复手机命令 Token 不匹配。

### App 内网地址不可达，Tailscale 地址仍可用

更新 `app_direct_urls` 第一项为手机当前内网地址，并保留 Tailscale 第二项。手机换 Wi-Fi、DHCP 租约变化或访客网络隔离可能使内网地址失效；在路由器给手机设置固定租约可减少改动。插件会按顺序尝试，但防火墙超时仍会增加几秒延迟。

### App 提示 `invalid_result`、`denied` 或 `unsupported_action`

`invalid_result` 表示返回结果的类型、命令 ID、动作或协议版本不匹配，检查 App 与插件版本。`denied` 先检查手机确认和系统权限；它不是需要换 Relay 重做的网络故障。`unsupported_action` 先核对动作名称和版本；新的 `ui_snapshot` / `ui_action` 需要 App 0.4.0+，不能把自由文本直接当作 App 命令。界面任务暂停原因与准备步骤见[界面任务说明](docs/UI_TASKS.md)。

### App 状态正常，但手机离线

`/app/state` 返回的是插件快照，特意不探测手机。能看到已有提醒、历史时间线不代表手机可达。用控制台“测试当前后端”检查命令 HTTP 服务。

### 提醒没响或创建后立即响了一次

核对手机通知权限和渠道、App 是否正在同步、系统后台限制，以及时间线事件类型。只有 `event=reminder_due` 且 `notify=true` 才需要通知；创建和取消事件不应响铃。旧 App 按所有 `type=reminder` 通知的行为需升级。

App 提醒网络重试应复用同一个 `request_id`；重复请求会返回原状态，不会再创建。检查提醒来自 `app` 还是 `chat`，聊天提醒会投递原会话，App 提醒不会发 QQ。手机离线期间到期的提醒可能延后显示。

### 健康页面显示 stale、未配置或没有同步记录

先检查 `health_data_source` 与响应的 `source`。使用 Health Connect 时，在 App 检查健康权限、同步开关、最近上传及具体错误，并确认小米运动健康已写入记录；`auto` 回退 SQLite 不代表手机上传成功。使用 SQLite 时，确认 `health_db_path` 是 AstrBot 用户或容器真正可读的文件，检查 `systemctl --user status xiaomi-health-sync.timer` 和同步服务日志。超过 180 分钟、最后一次同步失败或没有 `sync_runs` 记录都不会标为新鲜。若账号过期，回到上游工具重新登录；不要把 Xiaomi Token 填入 AstrBot 配置。

### 地图定位有偏移

先核对 `coord_type` 和控制模式。App 返回 WGS84，当前 Operit 解析提供 GCJ02/BD09；不能混用或重复转换。地图工具若接受不同坐标类型，明确指定其文档要求的参数。

### 主动消息没有包含手机或健康摘要

检查 `companion-context` 是否独立加载，`enabled`、`relationship_mode_enabled` 和其自身 `allowed_user_ids` 是否都配置。手机状态摘要要求主插件观察开关与 `include_screen=true`：App 模式检查设备绑定和心跳新鲜度，Operit 模式检查连接；健康摘要还需主插件健康开关与 `include_health`。主动关怀另需调度器与主动决策开关；Private Companion 7.x 的扩展门面不可用时，桥接状态会显示未安装或不兼容。

### Relay 收到 403、429 或没有任务

403 优先检查当前 `X-Relay-Device-ID` 是否与领取任务时一致；429 按 `Retry-After` 退避并查看限流配置。`task=null` 是正常空队列。确认 App 与 Operit 没在抢同一队列，反代路径正确且保留鉴权头。

### Relay 或后台任务取消了，手机还在操作

取消本地等待不代表取消手机正在运行的请求。App 命令由其 deadline 限制；Operit 自然语言请求可能继续执行。先在手机检查实际结果，必要时手工停止执行器，再决定是否重试。

## 安全说明

- 只在 Tailscale 或可信内网开放 App/Operit 命令 HTTP 服务；公网 Relay 使用 HTTPS。
- 不要提交 App 命令 Token、Operit Token、Relay Token、小米 Token、SSH 密码或服务器配置。
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

更多仓库内文档：

- [Private Companion 授权扩展](extensions/private_companion_auth.md)
- [主动上下文插件源码和配置](companion-context)
- [xiaomi-sync 包装脚本和服务模板](deploy/xiaomi-sync)
- [Relay 服务和模板](deploy/relay)
- [App API v2](docs/APP_API_V2.md)
- [English Guide](README.en.md)
- [更新日志](CHANGELOG.md)

## 许可证

本仓库自有代码采用 [MIT License](LICENSE)。第三方依赖和可选联动插件保留各自许可；不对 Private Companion 上游代码及服务器本地修改重新授权。
