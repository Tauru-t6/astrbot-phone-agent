# Phone Control · AstrBot 手机控制插件

[完整中文教程](README.zh-CN.md) · [English guide](README.en.md) · [界面任务与双设备](docs/UI_TASKS.md) · [更新日志](CHANGELOG.md) · [0.11 升级说明](docs/RELEASE_0_11.md) · [App API v2](docs/APP_API_V2.md)

DeepSeek V4.1 Flash 1017发布了后面可能会让这个模型继续更新，GPT 太贵了

让 AstrBot 通过聊天控制 Android 手机：开关应用、观察屏幕、查询电量和使用时长、临时限制应用、设置提醒，以及按需查询位置和小米健康数据。

插件保留原有 **OperitAI 模式**，新增 **自有 App 模式**。两种模式决定手机端如何执行任务；原有的后台任务、健康数据库、定位联动、应用策略、审计、Relay 和可选 Private Companion 扩展继续保留。

## 5 分钟上手（自有 App 模式）

**0.11.0（配套 App 0.5.0）**：在有范围的界面任务上增加已核验结果回传、参数化收藏、操作经验复用、接手后新审批续跑和限额 Bot 日程。另有可撤销扫码配对、设备自检及显式开启的截图/OCR 辅助；个人版保留简洁首页，健康同步区分最近尝试和成功。结果只复制新读屏核对过的页面事实，敏感步骤仍由人工处理。配置和限制见[界面任务与双设备](docs/UI_TASKS.md)，发布前验收要求见[0.11 说明](docs/RELEASE_0_11.md)。

个人手机安装 **Phone Control**，Bot 专用第二台手机安装独立的 **Phone Control Bot**。Bot 版有独立包名和存储，不包含 Health Connect SDK、健康权限或读取功能。设备按「我的手机 / Bot 的手机」分别选择，第二台离线不会转而操作第一台；角色划分不是不同账户之间的权限隔离。

1. **装插件**：在 AstrBot 插件市场搜索 `Phone Control` 安装，或按[安装与升级](#安装与升级)用 Git 拉取。
2. **装 App**：从 [App Releases](https://github.com/Tauru-t6/astrbot-phone-agent-app/releases/latest) 下载最新 APK，安装后把 Shizuku 启动并授权。
3. **连接服务器**：0.11.0 / App 0.5.0 可按[扫码设置](docs/UI_TASKS.md)生成 120 秒一次性配对码，手机只取得设备专用凭据。传统方式继续在 App「我的 / 设置 → 服务器连接」填三个字段：

   | 字段 | 填什么 |
   | --- | --- |
   | 插件地址 | `http://<你的服务器>:6185/api/plug/astrbot_plugin_phone_agent` |
   | 插件令牌 | AstrBot 面板登录后的 Dashboard JWT |
   | 手机共享 Token | 插件配置里的 `app_shared_token` |

4. **测连接**：App 里点「测试已保存连接」，显示「设备直连 / 已连接」就通了。
5. **说一句话**：在 QQ 里对机器人说「看看手机电量」；要改手机的动作（打开应用、回桌面、暂停应用）会先发一条待批准任务，回「可以」才执行。

卡住的话先看[排障与开发](#排障与开发)，三处最常见：服务器地址写成了局域网、JWT 过期、Shizuku 没启动。

## 先选手机执行端

| | 自有 App | OperitAI |
| --- | --- | --- |
| 配置值 | `control_backend=app` | `control_backend=operit`，默认值 |
| 手机端 | [AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app) + Shizuku | [OperitAI](https://github.com/Mavaebrook/Operit) + Shizuku |
| 执行方式 | 结构化命令；可选服务器模型驱动的单应用界面任务 | 手机端 Agent 解析自然语言并操作 UI |
| 手机端模型 | 执行指令不需要额外模型（聊天在 QQ 侧，App 不承载对话） | 需要可用模型及工具调用 |
| 结构化界面操作 | `phone_action` 保留固定动作；`phone_ui_task` 经批准后按页面节点点击、输入、滚动和返回 | 通过 `operit_task` 由手机端模型自行决定操作 |
| 第二台设备 | 独立 Phone Control Bot App；任务明确绑定设备 | 沿用 Operit 配置 |
| 网络 | 有序局域网 / Tailscale 直连 → Relay | Operit HTTP 直连 → Relay 工作流 |
| 前端能力 | 任务记录、提醒、时间线、连接设置、夜间守护与自主关怀诊断；名称与头像由用户设置 | 使用 Operit 自己的界面 |

`adb` 保留为旧配置的诊断后端，不是第三种推荐产品模式。旧值 `phone_buddy`、`native_app`、`own_frontend` 会映射为 `app`；`operitai`、`operit_ai` 会映射为 `operit`。升级不会自动把原来的 Operit 配置切换为 App。

## 原有功能一览

| 功能 | 工具 / 组件 | 两种模式的区别与开关 |
| --- | --- | --- |
| 自然语言手机任务 | `operit_task` | OperitAI 模式；App 模式明确拒绝自由文本执行 |
| 后台任务查询、取消、重试 | `operit_task_status` / `operit_task_cancel` / `operit_task_retry` | 保留 Operit 后台任务、并发限制与任务元数据持久化 |
| 白名单手机操作 | `phone_action` | 两模式支持各自执行端的动作，App 不会误走 ADB |
| 有范围的界面任务与结果 | `phone_ui_task` / `phone_task_status` | App 0.5.0 可申请 1–10 条页面结果；基础三个开关和任务审批仍生效 |
| 任务收藏、续跑与 Bot 日程 | App「任务收藏与自主日程」 | 参数化提案、当前状态续跑；日程另开 `enable_task_routines` 并批准有限额度 |
| 扫码配对与设备自检 | WebUI / App 连接设置 | `enable_device_pairing`；设备凭据可撤销，新配对当前直连，传统 Relay 兼容 |
| 截图/OCR 辅助 | `enable_ui_vision` + 每任务 `allow_vision` | 只选择可核验节点，OCR 只点击/返回，变化停止 |
| 观察前台与屏幕 | `phone_observe` | `enable_observe_tool`；Operit 可读屏，App 返回设备状态 |
| 截图回传聊天 | `phone_screenshot` | `enable_screenshot_tool`；仅 App 模式，图片直接发进当前会话 |
| 手机通知转发 | `phone_notifications` | `enable_notification_tool`；App 端需授予通知读取权限 |
| 单应用暂停 / 恢复 | `phone_app_policy` | `enable_policy_tools`；支持核验状态、按分钟恢复 |
| 临时限制视频应用 / 守夜 | `phone_sleep_mode` | `enable_policy_tools`；可指定目标、豁免及持续时间 |
| 一次性定位与地图联动 | `phone_location` | `enable_location_tool`；按返回的坐标系连接地图服务 |
| 应用使用统计 | `phone_usage` | `enable_usage_tool`；依赖执行端权限 |
| 聊天会话提醒 | `phone_reminder` | `enable_reminder_tools`；添加、列出、取消，到期回原会话 |
| 手机操作审计 | `phone_audit` | `enable_audit_tool`；动作元数据，不含聊天正文和 Token |
| 小米运动健康查询 | `phone_health` | `enable_health_tools`；选择 App 上传的 Health Connect 摘要或独立同步的 SQLite，和手机执行模式无关 |
| 私人陪伴授权复用 | `extensions/private_companion_auth.md` | 可选 `use_private_companion_auth`，不要求安装私人陪伴插件 |
| 主动陪伴中的手机 / 健康上下文 | `companion-context/` | 独立可选桥接插件，默认关闭；另有显式开启的关系语气和主动决策配置 |
| 无 VPN 的中继兜底 | `deploy/relay/` | Bearer 鉴权、任务租约、续租、领取者校验、限流及持久化 |
| WebUI 控制台 | Phone Control | 模式选择、连接测试、配置、健康、策略、任务、提醒及审计 |

`enable_*` 可选功能在配置模板中默认关闭，启用对应能力前请打开开关并重载插件。工具调用仍检查开关和调用者权限，不能因工具名称出现在列表里就视为可用。

## 整体结构

```text
聊天平台 ──> AstrBot：理解请求、选择工具、检查授权
                │
                └─ astrbot-phone-agent
                    ├─ app：结构化命令 ──> Android App / Shizuku
                    ├─ operit：自然语言任务 ──> OperitAI / Shizuku
                    └─ 直连传输失败 ──> Relay ──> 对应手机端轮询

小米运动健康 ──> Health Connect ──> Phone Agent App ──> 手机健康快照 ──┐
             └─> 独立 xiaomi-sync ──> SQLite ─────────────────────┴─> phone_health（选择一个来源）
Private Companion <── 可选 companion-context 手机 / 健康摘要
```

聊天在 QQ 等 AstrBot 会话中进行，App 专注设备控制、状态和提醒。手机指令、聊天、健康数据同步是不同链路，不需要为了健康查询保持手机 ADB 常连。

## 安装与升级

要求 AstrBot 4.22+。可在 AstrBot 插件管理中填写仓库地址安装，或使用 Git：

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

目录内应直接有 `main.py`、`device_app.py`、`task_runner.py`、`care_scheduler.py`、`storage.py`、`http_transport.py`、`metadata.yaml` 和 `pages/`。安装或升级时复制完整插件目录，避免遗漏内部模块。使用 [Release ZIP](https://github.com/Tauru-t6/astrbot-phone-agent/releases) 时同样保持这个布局，不要多套一层目录。

更新前备份插件配置和提醒、策略、任务状态文件，备份目录放在 `plugins/` 之外。更新后在 WebUI 重载插件；systemd user 部署可执行：

```bash
systemctl --user restart astrbot.service
journalctl --user -u astrbot.service -n 100 --no-pager
```

Git 安装更新：

```bash
cd <astrbot-data>/plugins/astrbot_plugin_phone_agent
git pull --ff-only
```

### Windows 部署

插件本体是纯 Python（只用 `pathlib`、`asyncio`、`json`），没有 POSIX 专用调用，可以跑在 Windows 上。差别只在"怎么装 AstrBot、怎么重启、可选服务怎么托管"这三件事：

```powershell
# 1. 安装并启动 AstrBot（二选一）
uv tool install astrbot
astrbot

# 或 pip
python -m pip install astrbot
astrbot
```

2. 插件目录是 `<astrbot-data>\plugins\astrbot_plugin_phone_agent`，配置在 `<astrbot-data>\config\`（`astrbot_plugin_phone_agent_config.json` 等）。用 Git 安装时 `git clone` 后同样要保持"目录内直接是 `main.py`"，不要多套一层。
3. **重启**：没有 systemd，结束控制台进程（Ctrl+C 或任务管理器结束 `python.exe`）后重新运行 `astrbot`。要开机自启就用「任务计划程序」建一个"登录时运行 `astrbot.exe`"的任务，或用 NSSM 注册成服务。
4. **Relay**（可选）：`deploy/relay/relay_server.py` 可直接运行，只是 `.service` 文件不适用。用环境变量传参：

```powershell
$env:RELAY_TOKEN = "<随机串>"
$env:RELAY_HOST  = "127.0.0.1"
$env:RELAY_PORT  = "8791"
$env:RELAY_STATE = "D:\astrbot-data\phone_agent_relay_state.json"
python deploy\relay\relay_server.py
```

5. **小米健康同步**（可选）：`deploy/xiaomi-sync/` 里的 `.sh` 和 systemd timer 只适用于 Linux；Windows 下用「任务计划程序」定时调用同一个 Python 入口，参数 `--days 7`、`--data-dir <路径>` 与 shell 脚本里一致。
6. ADB 诊断后端需要 `adb.exe` 在 `PATH` 中；App / Operit 两种模式与服务器操作系统无关，手机端照旧。

配置里的路径建议写正斜杠（`D:/astrbot-data/...`）或双反斜杠，`pathlib` 两种都能识别。

## 通用授权

先填写 `allowed_user_ids`，多个聊天平台用户 ID 用逗号分隔。默认没有白名单且没有开启可选授权扩展时，手机工具拒绝调用。

已有 Private Companion 的用户可以显式启用授权复用：

```json
{"allowed_user_ids":"","use_private_companion_auth":true}
```

扩展不可用时按拒绝处理，不会放开给全部用户。单独使用手机插件时无需 Private Companion，直接设置白名单即可。详见[可选授权说明](extensions/private_companion_auth.md)。

## 模式一：自有 App

下载 [App Release](https://github.com/Tauru-t6/astrbot-phone-agent-app/releases)，启动 Shizuku 并授权。可按[扫码配对说明](docs/UI_TASKS.md)使用独立设备凭据，或在 App 连接设置填写传统插件地址、Dashboard JWT 和手机共享 Token。聊天在 QQ 等 AstrBot 会话中进行，当前 App 不需要配置聊天 API。

```json
{
  "enabled": true,
  "control_backend": "app",
  "app_shared_token": "REPLACE_WITH_A_RANDOM_SHARED_TOKEN",
  "app_direct_urls": "http://phone-lan.example:8260,http://phone-tailnet.example:8260",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "enable_observe_tool": true,
  "enable_reminder_tools": true
}
```

以上地址是占位符。`app_direct_urls` 按逗号分隔顺序尝试，建议内网在前、Tailscale 在后；优先级高于 App 自动注册的地址。所有直连与 Relay 尝试共享命令总时限，明确的设备拒绝不会换路径重放。

**确认机制**：读屏（`screen_text`）、截图（`screenshot`）和全部界面操作（点击、长按、滑动、滚动、输入、按键）默认都需要在手机上确认；定位、关闭/暂停应用、发送通知同理。可以在 App 的「纸笺 → 隐私与自动化」里逐项关闭确认，默认只预先放行截图。

App 端凭据各有用途：

| 凭据 | 用途 |
| --- | --- |
| AstrBot Dashboard 认证令牌 | `/api/plug/astrbot_plugin_phone_agent/` 下的状态、注册、提醒接口 |
| `app_shared_token` | 插件访问手机 `:8260` 命令服务，两端必须一致 |

可选 Relay 另用独立 `relay_token`。App 初始不含任何角色名字、头像、服务器地址或密钥。

**当前能力边界**：底层 App 保留状态、应用管理、导航、截图、读屏、固定界面动作、定位、通知等指令。`phone_ui_task` 提供单应用的「观察 → 模型选择节点 → 执行 → 再观察」，默认最多 8 步，配置上限为 12 步。模型会收到目标页面的可见节点文字，只能输入已批准原文；它不是任意 App 的通用自动操作保证。`phone_observe` 仍返回设备状态；截图/OCR 辅助需服务端和本次任务分别开启，结果回传需明确选择字段与数量。完整准备、搜索验收与人工接手规则见[界面任务说明](docs/UI_TASKS.md)。

连续界面任务需在**执行设备**的「隐私与自动化」明确打开「读取界面节点」和「操作界面」免确认，并在 Android 设置手动启用 Phone Control 输入法。输入时临时切换、结束后尝试恢复原输入法。两项免确认默认关闭；开启代表信任已连接服务器发送该类命令，服务端每次任务的完整范围审批仍保留。

App 内的专注 / 批量应用限制按钮尚未具备完整的目标应用与恢复策略配置；插件侧的 `phone_app_policy`、`phone_sleep_mode` 能力仍保留。

## 模式二：OperitAI

原有 Operit 部署继续使用：

```json
{
  "enabled": true,
  "control_backend": "operit",
  "operit_base_url": "http://phone-tailnet.example:8094",
  "operit_token": "YOUR_OPERIT_HTTP_TOKEN",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID"
}
```

手机端先完成四项准备：

1. 安装并启动 Shizuku，在 Operit 内授权。
2. 在 Operit 中配置可用模型和工具调用，先验证普通聊天与一个只读工具。
3. 在「设置 → 数据和权限 → 外部 HTTP 调用」启用服务，记录端口与 Bearer Token。
4. 保证服务器能通过内网或 Tailscale 访问手机，并按手机厂商要求允许后台运行。

发现接口为 `/.well-known/agent-card.json`，鉴权探活为 `/api/health`，任务入口为 `/api/external-chat`。在 Phone Control 页面选择 OperitAI，再测试当前后端。详细的 Tailscale、Shizuku、模型和 HTTP 排障步骤见[完整中文教程](README.zh-CN.md)。

## 任务与操作

常见请求：

```text
打开设置
只看看手机状态，不要点击或输入
读取当前屏幕文字
帮我执行这个界面任务，并在后台运行
查询刚才的手机任务
取消这个手机任务
重试失败的手机任务
```

`operit_task(background=true)` 返回任务 ID。`max_background_tasks` 控制并发，默认 2；`tasks_path` 保存最近的任务元数据，AstrBot 重启后把未结束任务标成中断。

**取消的语义**：`operit_task_cancel` 停止服务器的本地等待，手机端已发出的 Operit 请求可能仍在执行。重试会创建新任务，不能当作撤销或保证恰好执行一次。输入、点击、外发消息、删除、付款等带副作用的请求要先取得用户明确确认。

`phone_action` 保留白名单动作，App 模式只下发其支持的结构化指令；不支持的动作返回错误，不会偷偷改走 ADB。

### 手机任务与审批（按范围授权）

下述固定动作提案继续保留。新的界面任务通过 App「让手机完成一件事」或聊天工具 `phone_ui_task` 发起，在完整任务详情中批准；不能用只显示一句话的悬浮卡片代替范围核对。它的取消会阻止后续步骤，已经发出的动作可能仍完成，结果未知时先检查手机，不自动重放。见[详细说明](docs/UI_TASKS.md)。

`task_execute_allowed_actions` 打开的那几类动作（默认 `open_app,home,back`）走**提案制**：模型不会直接执行，而是生成一条待批准任务，手机在你点头之前不会发生任何变化。

```text
你：帮我把抖音暂停
她：已在手机上发起待批准任务：暂停 com.ss.android.ugc.aweme.lite。回「批准」执行，180 秒内有效。
你：批准
她：照做了（核验通过 1 步）。
```

批准有三条等价的路，任选其一：

1. **聊天里回「批准」/「取消」**——手机须在线才能实际执行；
2. **App 的自主功能页 →「待你决定」**；
3. **手机屏幕上的悬浮卡片**（需要「显示在其他应用的上层」权限）。

执行后每一步都会重新取一次手机状态再判定成功，命令返回成功不算数。停止、超时、观察过期、你正在用手机（`user_active`）都会让任务停下并说明原因，不会重试写操作。批准动作本身视为知情同意，因此不再套"用户在用"那道门。

## 应用管控与守夜

开启 `enable_policy_tools` 后：

- `phone_app_policy` 支持查询、禁用、恢复单个应用，以及指定分钟数后自动恢复。
- `phone_sleep_mode` 支持开始、停止和查询临时视频应用限制，可单次指定 `packages` 和 `exempt`。
- `app_aliases_json` 将应用名称映射为包名；`sleep_guard_packages` 和 `sleep_guard_exempt_apps` 提供默认目标与豁免。
- 插件核验暂停状态；批量操作部分失败时尝试回滚已处理的应用，并返回失败信息。
- 策略状态写入 `policy_state_path`；自动恢复仍依赖手机可达和执行端成功，离线时不能承诺准时解锁。

```json
{
  "enable_policy_tools": true,
  "app_aliases_json": "{\"视频应用\":\"com.example.video\"}",
  "sleep_guard_packages": "com.example.video",
  "sleep_guard_exempt_apps": "com.example.chat"
}
```

```text
别让我刷视频两小时
解除视频限制
把这个应用停用 30 分钟，然后恢复
```

保护列表包含系统 UI、设置以及控制端相关包名。这些限制是按用户请求执行的，不是持续监控前台后自动封禁。Shizuku 重启、手机离线、厂商限制都会影响执行。Android 显示的“由 Shell 管理”是系统文案。

## 聊天提醒、App 提醒与时间线

原有 `phone_reminder` 的添加 / 列表 / 取消继续可用，到期发送到创建提醒的聊天会话：

```text
30 分钟后提醒我喝水
看看还没到期的提醒
取消刚才那条提醒
```

App 另外提供创建与取消接口，通过 `GET /app/state` 拉取待办、近期命令与最近 100 条时间线。App 创建的提醒不发送 QQ，由到期事件同步给手机；聊天创建的提醒保留会话投递，并记录时间线。

待办、请求回执和时间线原子持久化。App 请求用 UUID 去重；到期事件用 `notification_id` 去重。服务器重启后，24 小时内逾期的提醒补发，更早的只记过期；手机通知还受联网、后台轮询和权限影响。创建/取消事件不会当作到期通知。

## 定位与地图联动

开启 `enable_location_tool` 后，`phone_location` 按需读取一次位置，不后台追踪。高精度定位和地址请求需要会话确认；App 模式还需要手机系统权限和手机端确认。

App 返回原生 WGS84，Operit 返回其实际报告的坐标系；插件不按地理范围猜坐标，也不伪造转换值。WebUI 的「检查地图接入」只检查 AstrBot 已注册的 MCP 工具，不采集或发送手机坐标。

百度地图 MCP 的逆地理编码要求 BD09LL。当前已核验的百度 MCP 工具列表没有坐标转换工具，因此 WGS84 / GCJ02 定位会明确提示需要转换，不会直接把原坐标交给百度或误报地址查询成功。使用前需确认地图工具 schema 中的坐标系与参数；地址和高精度定位仍需用户明确请求和确认。更多接入状态见 [百度地图 MCP 对接说明](docs/BAIDU_MAP_MCP.md)。

## 小米运动健康

健康查询与 App / Operit 控制模式独立。服务端开启 `enable_health_tools` 后，可选择以下来源；同一次查询只返回一个来源，不叠加步数等指标。

支持对接可接入 Health Connect 的健康 App，小米运动健康国区默认隐藏了，需要的可自行探索。

**Health Connect（插件 0.9.3 + App 0.3.28）**：先在 Health Connect 中选定一个实际写入记录的 App，再到 Phone Agent App「我的 → Health Connect」填写该 App 的 Android 包名、授予需要的读取权限并开启「同步健康摘要到服务器」。每次只读取一个来源最近 7 天的步数、睡眠、心率、血氧和活动热量，发送按日摘要；缺失数据不补零。同步默认关闭，支持部分授权。可读取的数据类别取决于所选 App 实际写入的记录；目前只有 `com.mi.health` 完成过实机验证。

`health_data_source=auto` 默认优先使用当前手机的新鲜快照，否则回退 SQLite；`health_connect` 只使用手机快照；`health_db` 固定使用 SQLite。App 约每 15 分钟尝试同步，系统可能拒绝后台读取，此时请保持 App 在前台手动同步。关闭同步会停止读取并请求清除服务器快照，离线清除会重试；系统撤权在下次读取时上报。详见 [Health Connect 接入与数据使用](docs/HEALTH_CONNECT.md)。

**原有 xiaomi-sync / SQLite**：

1. 部署 [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync) 并完成它自己的登录/同步。
2. 使用 [`deploy/xiaomi-sync/`](deploy/xiaomi-sync/README.md) 中的脚本和 systemd timer 模板定期同步。
3. 将生成的 `health.db` 路径填入 `health_db_path`，开启 `enable_health_tools`。

选择 SQLite 时，插件只读数据库，支持步数、睡眠、心率、血氧等每日指标；数据库存在相应表时还返回体重/BMI/体脂、血压、睡眠分段。此来源查询支持最近 1–30 天，缺少当天数据时会标明并返回最近可用日期。

响应带实际 `source`（`health_connect` 或 `health_db`）、`available`、`fresh` 和同步状态。SQLite 同步失败、缺少同步记录或超过 180 分钟会标记过期；Health Connect 快照超过 180 分钟也会过期。新鲜快照不代表其中每条测量刚刚发生，应核对记录日期。小米登录凭据留在同步工具中。

```text
今天走了多少步
昨晚睡了多久，深睡和浅睡怎么样
最近一周心率和血氧记录怎么样
```

## Private Companion 扩展

这部分仍在仓库中，分为两个独立能力：

- **授权复用**：`use_private_companion_auth`，详见 [extensions/private_companion_auth.md](extensions/private_companion_auth.md)。
- **主动消息上下文桥接**：`companion-context/` 是独立可选插件，目录应单独安装为 `astrbot_plugin_phone_companion_context`。复制主插件不会自动启用它。

桥接的配置模板默认 `enabled=false`，还需开启 `relationship_mode_enabled`、配置独立的 `allowed_user_ids` 并满足私聊与用户角色检查。手机状态摘要需要主插件 `enable_observe_tool` 和桥接 `include_screen`：App 模式使用当前绑定手机的新鲜心跳，Operit 模式请求一次粗粒度观察；不会把屏幕原文交给关怀模型。健康读取需要主插件健康开关和桥接 `include_health`，遵循主插件的来源选择与撤权状态。

主动关怀需要显式开启 `autonomous_decision_enabled` 与主插件调度器。桥接使用 AstrBot 当前私聊的人格、近期对话和手机/健康摘要，解析 `no_action` / `send_message` 决策，并执行静默时段、冷却、评估与发送配额、去重和投递结果检查。`preview` 只预览，`care_auto` 允许符合授权范围的关怀投递；`low_risk_auto` 是兼容别名。Private Companion 7.x 通过扩展门面发现，关怀调度独立于其原生主动联系；不接管长期记忆，也不授予手机动作权限。详见 [7.x 适配与自主决策](docs/PRIVATE_COMPANION_719_ADAPTATION.md)。

## Relay 兜底部署与工作流

Relay 是 `deploy/relay/relay_server.py`，只用 Python 标准库。`phone-agent-relay.service` 是部署模板，其中脚本路径、状态目录和 Token 必须改为自己的实际值，不能直接照搬模板中的作者目录。

服务器侧流程：

1. 将脚本放到服务器固定目录，建立可写的状态目录。
2. 修改 service 模板：`ExecStart`、`RELAY_STATE`、`ReadWritePaths`，以及随机 `RELAY_TOKEN`。
3. 安装 systemd 服务，启动后确认只监听预期地址；通过 HTTPS 反向代理提供公网入口。
4. 插件填入同一 `relay_base_url` 和 `relay_token`。

App 直接轮询中继。OperitAI 保留定时工作流接法：

1. 定时触发工作流；若使用 Android WorkManager 周期任务，系统下限通常为 15 分钟。
2. `GET /poll`，请求头包含 Bearer Token；启用设备校验时同时发送 `X-Relay-Device-ID`。
3. `task` 为 `null` 时结束；否则把 `task.message` 交给 Operit 执行。
4. 长任务在 5 分钟租约到期前调用 `POST /renew`，携带实际 `task.id` 和同一个设备 ID。
5. 完成后 `POST /result`，提交 `task_id=task.id`、实际成功状态和执行结果。

队列租约、结果和待办持久化；手机中断后租约过期可重新领取。`RELAY_REQUIRE_DEVICE_ID=1` 校验领取者、续租者及回报者身份，默认 0 兼容旧工作流；它不是任意多设备路由保证。App 与 Operit 使用不同执行协议，不要让不同执行端消费同一队列。

默认限流为普通请求 120 次/分钟、创建任务 30 次/分钟，配置名分别为 `RELAY_RATE_LIMIT`、`RELAY_TASK_RATE_LIMIT`。队列结果不能替代最终手机执行确认：等待超时不表示成功，旧 Operit 队列任务仍可能稍后执行。短于工作流轮询周期的等待预算也可能先超时。

完整服务安装示例、HTTP 请求格式和排障见[中文教程](README.zh-CN.md)。

当前 Operit 部分工具在提交任务前先检查手机健康接口；如果在这一步返回离线，会直接结束，未必进入底层 Relay 降级。因此保留了 Relay 传输能力，并不等于所有 Operit 工具入口都能在手机失联时自动入队。App 的新多地址执行路径按上述顺序降级。

## WebUI 与文件配置

Phone Control 提供后端连接测试、配置修改、健康摘要、按需应用策略、守夜控制、一次性定位、Operit 任务取消/重试、提醒和审计，以及可选自动刷新。页面支持 App / Operit 模式选择、对应字段与 App 注册/直连信息。Operit 取消只停止本地等待；重试创建新任务，仍检查后端、任务状态、确认记录和并发预算。

| 配置 | 内容 |
| --- | --- |
| `app_command_timeout_seconds` | App 命令共享的总等待时限 |
| `operit_timeout_seconds` | Operit 任务等待时限 |
| `max_background_tasks` / `tasks_path` | Operit 并发上限与任务元数据 |
| `reminders_path` | 提醒与 App 幂等回执，含提醒正文 |
| `app_state_path` | 最近 100 条 App 时间线，含事件正文 |
| `policy_state_path` | 应用策略及恢复时间 |
| `audit_log_path` | 操作元数据审计 |
| `adb_*` / `command_timeout_seconds` | 旧 ADB 诊断配置 |

Token 字段不回显，输入留空保留原值。审计不含正文并不意味着所有状态文件都没有正文；提醒、时间线和 Operit 任务描述属于个人数据，备份与发布时应排除。

## 排障与开发

- **401/403**：先区分手机共享 Token、Operit Token、Relay Token、Dashboard 令牌、聊天 OpenAPI Key，不能互换。
- **手机离线**：检查当前执行端的 HTTP 服务、内网/Tailscale 路由和后台策略；App 状态同步成功不等于服务器能反向直连手机。
- **Operit 连接成功但不执行**：先在手机验证模型和工具调用，再验证 Shizuku；重装插件不能修复手机模型故障。
- **重启后 Shizuku 不可用**：按 Shizuku 页面重新启动并检查授权。
- **健康无数据/过期**：先核对 `health_data_source` 和实际返回来源。Health Connect 检查小米写入、App 授权和上传结果；SQLite 检查同步工具、数据库路径和同步记录。两者都需核对实际日期。
- **主动消息不带上下文**：核对桥接独立安装、开关、白名单和 Private Companion 版本兼容性。

```bash
python -m pip install quart pytest
python -m pytest tests -q
python -m py_compile main.py model_planning.py device_app.py task_runner.py care_scheduler.py storage.py http_transport.py companion-context/main.py companion-context/v2_adapter.py deploy/relay/relay_server.py
node --test tests/test_dashboard.js
```

详细操作和完整配置参阅[中文教程](README.zh-CN.md)或 [English guide](README.en.md)。项目在原有 Operit 链路之上增加 App 模式，功能列表中的保留项并未因新前端而移除。

## 致谢

本项目在 AI 编程助手协作下持续完善。感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[Operit](https://github.com/Mavaebrook/Operit)、[Shizuku](https://github.com/RikkaApps/Shizuku)、[Tailscale](https://github.com/tailscale/tailscale) 和健康同步相关开源项目。欢迎通过 Issue 提供可复现问题与脱敏日志。

## 许可证

本仓库自有代码采用 [MIT License](LICENSE)。可选联动插件及第三方依赖遵循各自的许可证，本仓库的 MIT 授权不覆盖 Private Companion 的上游代码或服务器本地修改。
