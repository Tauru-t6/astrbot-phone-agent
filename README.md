# Phone Control · AstrBot 手机控制插件

[完整中文教程](README.zh-CN.md) · [English guide](README.en.md) · [更新日志](CHANGELOG.md) · [App API v2](docs/APP_API_V2.md)

让 AstrBot 通过聊天控制 Android 手机：开关应用、观察屏幕、查询电量和使用时长、临时限制应用、设置提醒，以及按需查询位置和小米健康数据。

插件保留原有 **OperitAI 模式**，新增 **自有 App 模式**。两种模式决定手机端如何执行任务；原有的后台任务、健康数据库、定位联动、应用策略、审计、Relay 和可选 Private Companion 扩展继续保留。

## 先选手机执行端

| | 自有 App | OperitAI |
| --- | --- | --- |
| 配置值 | `control_backend=app` | `control_backend=operit`，默认值 |
| 手机端 | [AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app) + Shizuku | [OperitAI](https://github.com/Mavaebrook/Operit) + Shizuku |
| 执行方式 | 固定指令集合、结构化 JSON | 手机端 Agent 解析自然语言并操作 UI |
| 手机端模型 | 执行指令不需要额外模型；App 聊天走 AstrBot | 需要可用模型及工具调用 |
| 任意界面任务 | 暂不支持通用点击、滑动、输入和自由文本任务 | `operit_task`，支持屏幕相关的多步任务 |
| 网络 | 有序局域网 / Tailscale 直连 → Relay | Operit HTTP 直连 → Relay 工作流 |
| 新增前端能力 | 聊天、任务记录、提醒、时间线、连接设置；名称与头像由用户设置 | 使用 Operit 自己的界面 |

`adb` 保留为旧配置的诊断后端，不是第三种推荐产品模式。旧值 `phone_buddy`、`native_app`、`own_frontend` 会映射为 `app`；`operitai`、`operit_ai` 会映射为 `operit`。升级不会自动把原来的 Operit 配置切换为 App。

## 原有功能一览

| 功能 | 工具 / 组件 | 两种模式的区别与开关 |
| --- | --- | --- |
| 自然语言手机任务 | `operit_task` | OperitAI 模式；App 模式明确拒绝自由文本执行 |
| 后台任务查询、取消、重试 | `operit_task_status` / `operit_task_cancel` / `operit_task_retry` | 保留 Operit 后台任务、并发限制与任务元数据持久化 |
| 白名单手机操作 | `phone_action` | 两模式支持各自执行端的动作，App 不会误走 ADB |
| 观察前台与屏幕 | `phone_observe` | `enable_observe_tool`；Operit 可读屏，App 当前返回设备状态 |
| 单应用暂停 / 恢复 | `phone_app_policy` | `enable_policy_tools`；支持核验状态、按分钟恢复 |
| 临时限制视频应用 / 守夜 | `phone_sleep_mode` | `enable_policy_tools`；可指定目标、豁免及持续时间 |
| 一次性定位与地图联动 | `phone_location` | `enable_location_tool`；按返回的坐标系连接地图服务 |
| 应用使用统计 | `phone_usage` | `enable_usage_tool`；依赖执行端权限 |
| 聊天会话提醒 | `phone_reminder` | `enable_reminder_tools`；添加、列出、取消，到期回原会话 |
| 手机操作审计 | `phone_audit` | `enable_audit_tool`；动作元数据，不含聊天正文和 Token |
| 小米运动健康查询 | `phone_health` | `enable_health_tools`；读取独立同步的 SQLite，和手机执行模式无关 |
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

小米运动健康 ──> 独立 xiaomi-sync ──> SQLite ──> phone_health
Private Companion <── 可选 companion-context 手机 / 健康摘要
```

App 内聊天直接连接 AstrBot OpenAPI。手机指令、聊天、健康数据同步是不同链路，不需要为了健康查询保持手机 ADB 常连。

## 安装与升级

要求 AstrBot 4.22+。可在 AstrBot 插件管理中填写仓库地址安装，或使用 Git：

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

目录内应直接有 `main.py`、`device_app.py`、`metadata.yaml` 和 `pages/`。使用 [Release ZIP](https://github.com/Tauru-t6/astrbot-phone-agent/releases) 时同样保持这个布局，不要多套一层目录。

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

## 通用授权

先填写 `allowed_user_ids`，多个聊天平台用户 ID 用逗号分隔。默认没有白名单且没有开启可选授权扩展时，手机工具拒绝调用。

已有 Private Companion 的用户可以显式启用授权复用：

```json
{"allowed_user_ids":"","use_private_companion_auth":true}
```

扩展不可用时按拒绝处理，不会放开给全部用户。单独使用手机插件时无需 Private Companion，直接设置白名单即可。详见[可选授权说明](extensions/private_companion_auth.md)。

## 模式一：自有 App

下载 [App Release](https://github.com/Tauru-t6/astrbot-phone-agent-app/releases)，启动 Shizuku 并授权。在 App 的连接设置中分别填写聊天 API、插件认证及手机共享 Token。

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

App 端三类凭据各有用途：

| 凭据 | 用途 |
| --- | --- |
| AstrBot OpenAPI Key | 聊天 `/api/v1/chat` |
| AstrBot Dashboard 认证令牌 | `/api/plug/astrbot_plugin_phone_agent/` 下的状态、注册、提醒接口 |
| `app_shared_token` | 插件访问手机 `:8260` 命令服务，两端必须一致 |

可选 Relay 另用独立 `relay_token`。App 初始不含任何角色名字、头像、服务器地址或密钥。

**当前能力边界**：底层 App 命令包括电量/状态、打开/关闭应用、返回/主页、锁屏/唤醒、截图保存、读屏、应用暂停/恢复、定位、使用统计、通知和 ping。不同 LLM 工具有自己的暴露范围；不能把底层命令存在理解为支持全部自然语言 UI 自动化。`phone_observe` 当前只返回 App 设备状态，通用屏幕推理、任意点击/滑动/输入请使用 OperitAI。

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

Operit 路径保留 GCJ02 → BD09 的处理，可返回 `bd09_latitude` / `bd09_longitude`，供百度地图 MCP 的 `map_reverse_geocode`、`map_search_places`、路线规划等工具继续使用。地图 MCP 是独立服务，需要在 AstrBot 另行配置。

**先核对 `coord_type`**：当前 App 使用 Android 原生位置接口，返回 `wgs84`；不可把它直接当 BD09 传入百度地图。旧 Operit 解析逻辑在中国范围内按 GCJ02 输入处理，也应核实你的定位提供方。没有地址或坐标转换结果时，不要把能力表理解为自动完成了地图查询。

## 小米运动健康

原有健康功能独立保留，不依赖选择 App 还是 Operit：

1. 部署 [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync) 并完成它自己的登录/同步。
2. 使用 [`deploy/xiaomi-sync/`](deploy/xiaomi-sync/README.md) 中的脚本和 systemd timer 模板定期同步。
3. 将生成的 `health.db` 路径填入 `health_db_path`，开启 `enable_health_tools`。

插件只读 SQLite，支持步数、睡眠、心率、血氧等每日指标；数据库存在相应表时还返回体重/BMI/体脂、血压、睡眠分段。查询支持最近 1–30 天，缺少当天数据时会标明并返回最近可用日期。

响应保留 `source=xiaomi-sync`、`available`、`fresh` 和最近同步状态；同步失败、缺少同步记录或超过 180 分钟会标记过期。不要把缓存数据当作实时测量。小米登录凭据留在同步工具中，不需要交给手机控制插件。

```text
今天走了多少步
昨晚睡了多久，深睡和浅睡怎么样
最近一周心率和血氧记录怎么样
```

## Private Companion 扩展

这部分仍在仓库中，分为两个独立能力：

- **授权复用**：`use_private_companion_auth`，详见 [extensions/private_companion_auth.md](extensions/private_companion_auth.md)。
- **主动消息上下文桥接**：`companion-context/` 是独立可选插件，目录应单独安装为 `astrbot_plugin_phone_companion_context`。复制主插件不会自动启用它。

桥接的配置模板默认 `enabled=false`。实际注入还要求 `relationship_mode_enabled=true`、它自己的 `allowed_user_ids`，以及 Private Companion 将用户识别为 owner/primary/self；默认限制私聊。满足条件后可把最小化的手机/健康摘要交给主动消息生成。读屏还需要同时启用主插件 `enable_observe_tool` 与桥接 `include_screen`，且此桥接目前仍直接调用 Operit，尚未适配 App-only 的读屏链路。健康与屏幕分别有缓存和超时。

桥接还保留显式开启的关系语气/日常关心和主动决策配置，包括 `relationship_mode_enabled`、`autonomous_decision_enabled`、`autonomous_decision_mode`（`preview` / `low_risk_auto`）、冷却时间、每日次数、安静时段、私聊范围、语气和自定义提示。目前自主决策主要是提示注入，决策结果解析与计数更新未形成完整调度闭环，不能当作成熟的自主消息调度器。它依赖所用 Private Companion 版本的内部接口，也不授予无人确认的手机控制权。默认不开启这些增强。

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

Phone Control 保留后端连接测试、配置修改、健康摘要、按需应用策略、守夜控制、一次性定位、Operit 任务列表、提醒取消、审计，以及可选自动刷新。现在还支持 App / Operit 模式选择、对应字段与 App 注册/直连信息。当前页面上的旧任务取消/重试按钮尚缺对应 Web 路由，应通过 `operit_task_cancel` / `operit_task_retry` 聊天工具操作。

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
- **健康无数据/过期**：检查同步工具、数据库路径、实际日期和同步记录。
- **主动消息不带上下文**：核对桥接独立安装、开关、白名单和 Private Companion 版本兼容性。

```bash
python -m pip install quart pytest
python -m pytest tests -q
python -m py_compile main.py device_app.py companion-context/main.py deploy/relay/relay_server.py
```

详细操作和完整配置参阅[中文教程](README.zh-CN.md)或 [English guide](README.en.md)。项目在原有 Operit 链路之上增加 App 模式，功能列表中的保留项并未因新前端而移除。

## 致谢

本项目在 AI 编程助手协作下持续完善。感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[Operit](https://github.com/Mavaebrook/Operit)、[Shizuku](https://github.com/RikkaApps/Shizuku)、[Tailscale](https://github.com/tailscale/tailscale) 和健康同步相关开源项目。欢迎通过 Issue 提供可复现问题与脱敏日志。
