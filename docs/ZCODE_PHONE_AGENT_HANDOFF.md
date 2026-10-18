# Phone Agent 夜间守护对接与修复说明

> 交给 ZCode/后续维护者使用。本文描述当前代码真实状态、目标行为、缺口、推荐实现顺序、接口约定、测试和上线回滚步骤。
>
> 本文不包含服务器密码、API Key、Bearer Token、App Shared Token 或任何其他凭据。凭据只从运行环境或本地安全配置读取。

## 1. 目标行为

目标不是让 LLM 随意操作手机，而是做一个有边界的夜间守护流程：

1. 到达配置的深夜时间段后，后台定期读取手机当前前台应用。
2. 如果前台应用是配置白名单中的视频应用，给用户的 QQ 私聊发一条提醒，例如“已经很晚了，先别刷了，早点睡”。
3. 等待一个宽限期，再次确认前台应用仍是同一个视频应用。
4. 如果用户仍在刷视频，调用结构化的 App 策略接口暂停这个包；必须核验 `pm suspend` 的实际状态后才报告成功。
5. 用户在 QQ 中要求“解开”“解除限制”“恢复视频应用”时，立即恢复由夜间守护自己暂停的包。
6. 到达临时暂停期限或夜间窗口结束时，恢复由夜间守护暂停的包。
7. 如果手机离线、状态过期、前台包不明确、包不在白名单、目标用户没有可用私聊会话，流程只记录原因并跳过动作。

LLM 只负责提醒文案和可选的陪伴语气。是否观察、是否提醒、是否暂停、恢复哪个包由确定性的状态机决定。

## 2. 当前基线

### 本地仓库

- 后端：`D:/Work/astrbot-phone-agent`
- App：`D:/Work/astrbot-phone-agent-app`
- 后端当前本地提交比 GitHub 远端多 1 个提交：`c2ed544`。
- App 当前本地提交比 GitHub 远端多 1 个提交：`8505e20`。
- 当前工作区没有未提交的代码修改。
- 后端本地测试：`39 passed`。
- 当前 Windows 环境没有 Java，App Gradle 单测尚未在本机执行；CI 工作流需要继续跑 Debug、Release 和 lint。

### 生产服务器检查结果

生产上运行的是 systemd user 服务 `astrbot.service`，另有 `dsh-web.service`。生产安装副本中的后端文件与本地 `0.7.0` 版本文件一致。

已确认的生产配置事实：

- `control_backend=app`。
- `enable_observe_tool=true`。
- `enable_policy_tools=true`。
- `enable_screenshot_tool=true`。
- `enable_notification_tool=true`。
- `sleep_guard_packages` 当前只有三项默认包，需要按实际手机安装情况核对。
- `include_screen=true`，但 `autonomous_decision_enabled=false`。
- App 直连健康探测在检查时超时，不能把注册信息当作手机当前可达的证明。

生产目录不是 Git checkout，部署时要先备份，再替换插件安装副本；不要在生产目录直接执行 `git pull`。

## 3. 当前组件和真实职责

### 后端插件

主要文件：

- `main.py`：AstrBot 工具、Web API、策略状态、提醒、Operit 调用。
- `device_app.py`：App 命令协议、直连/Relay 路由、结果校验和状态缓存。
- `_conf_schema.json`：后端配置模板。
- `tests/test_app_api.py`、`tests/test_device_app.py`、`tests/test_relay.py`：Python 测试。

已有能力：

- App 命令 `status`、`foreground_app`、`screenshot`、`screen_text`。
- 单包 `suspend_app` / `unsuspend_app`，并核验 suspended 状态。
- `phone_app_policy` 和 `phone_sleep_mode`。
- 临时策略到期恢复、策略状态保存、批量失败回滚。
- App 截图回传和通知转发。
- `phone_observe`：在 App 模式调用 `status`；返回内容中可以包含 `foreground_package`，但它现在是按需调用，不是后台守护。

### Android App

主要文件：

- `logic/PluginBridge.kt`：注册、状态同步、Relay 轮询。
- `logic/CommandServer.kt`：本地 HTTP 命令服务。
- `logic/CommandRunner.kt`：命令幂等、确认、执行串行化。
- `logic/ShizukuCommandExecutor.kt`：Shizuku 命令执行和前台包读取。
- `logic/Protocol.kt`：App/插件闭合命令集合。
- `data/RealAppState.kt`：聊天、快捷操作、任务记录、设置。
- `logic/NotificationRelayService.kt`：通知监听和上送。

App 每 30 秒在本地刷新状态，但当前状态主要留在 App 的内存/UI 流中；插件端没有一个稳定的、带时间戳的后台状态推送链路。因此服务器端夜间守护不能只依赖 App 本地状态流。

### companion-context

`companion-context/main.py` 目前做的是：

- 包装 Private Companion 的主动消息生成。
- 可选地读取健康数据和 Operit 屏幕摘要。
- 把手机/健康状态加入主动消息的上下文。
- 提供关系语气、冷却和每日上限提示。

它没有自己的定时巡检器；`autonomous_decision_enabled` 也不会自动启动夜间观察，更不会直接触发手机暂停。现有 `_parse_decision()` 没有形成完整的执行链路，不要只打开配置就宣称夜间守护已完成。

## 4. 缺口清单

### P0：必须修复

1. **没有夜间守护状态机。** 当前只支持被 LLM 或 UI 入口调用，不会执行“观察 → 提醒 → 等待 → 再观察 → 暂停”。
2. **没有可靠的后台前台状态来源。** `phone_observe` 能读 App `status`，但只能按需调用；App 本地每 30 秒状态没有可靠推到插件。
3. **没有提醒目标会话管理。** 夜间任务必须明确知道发给哪个 QQ 私聊，不能让 LLM 猜目标，也不能误发群聊。
4. **没有夜间守护拥有权。** 恢复时必须只恢复“本次守护暂停的包”，不能解除用户手动设置的永久策略或其他任务暂停的包。
5. **没有重启恢复策略。** 需要区分守护拥有的临时策略、用户手动策略和普通 `phone_sleep_mode` 策略。
6. **确认策略不适合后台守护。** 后台暂停若每次弹 App 确认，用户不在手机旁时流程会卡住；需要独立的、明确开关的“夜间守护已授权”设置，并保留审计。

### P1：应一并修复

1. App 的“截屏”快捷操作文案仍写成保存到相册，但新协议是回传 JPEG；应改成实际行为，或补一个明确的本地保存动作。
2. App 的专注/守夜按钮调用服务器 `/sleep_mode`，但没有本地目标应用编辑和运行状态细节；需要展示目标包、剩余时间、来源和恢复结果。
3. App 市集仍是占位页，不应在文案中暗示功能已完成。
4. `NotificationRelayService` 在 4 秒窗口内可能把通知留在 `pending`，但没有可靠的延迟 flush；上送失败也会丢批次。需要定时 flush、有限重试和关闭时处理。
5. 后端截图临时文件在发送后没有统一清理，应在 `finally` 中删除。
6. App/插件协议版本和能力集合需要注册时协商，旧 App 应返回可理解的升级提示。
7. Release 构建需要在 CI 中执行，覆盖 R8、资源压缩、反射保留规则和 NotificationListenerService。

## 5. 推荐架构

### 原则

- 夜间守护属于后端确定性业务规则，不要让 LLM 决定是否执行 `pm suspend`。
- 不调用任意 shell；使用已有闭合命令 `suspend_app` / `unsuspend_app`，结果必须核验。
- LLM 只能生成提醒文字，或从有限的结构化事件中选择 `warn` / `no_message`；不能返回包名、shell 命令或任意动作。
- 每次观察都必须有 `observed_at`，过期状态不能触发暂停。
- 只对明确的私聊目标发送消息；没有目标会话时跳过并审计。
- 所有自动动作都要能通过配置关闭、被用户解除，并能审计和回滚。

### 建议的状态机

每个用户/设备一份状态：

```text
DISABLED
  └─ 开关开启且进入夜间窗口 → WATCHING

WATCHING
  ├─ 前台不是目标视频 App → WATCHING
  ├─ 连续命中目标 App → WARNED
  └─ 状态过期/手机离线 → WAITING_FOR_FRESH_STATUS

WARNED
  ├─ 用户离开目标 App → WATCHING
  ├─ 宽限期未到 → WARNED
  ├─ 宽限期到且再次连续命中 → ENFORCED
  └─ 用户要求解除 → USER_RELEASED

ENFORCED
  ├─ 自动到期 → WATCHING 或 NIGHT_ENDED
  ├─ 用户要求解除 → USER_RELEASED
  └─ 状态未知 → 保留 owned policy，等待核验，不重复下发

USER_RELEASED
  ├─ 冷却期内不重复提醒/暂停
  └─ 用户重新明确开启守夜后 → WATCHING
```

建议连续命中至少 2 次才进入 `WARNED`，避免一次过期状态误判；暂停前再读一次状态，确认仍是同一个包。

### 状态字段

状态应保存到独立文件，例如 `night_guard_state_path`：

```json
{
  "version": 1,
  "users": {
    "<user-id>": {
      "session": "<private-unified-message-origin>",
      "state": "watching|warned|enforced|user_released",
      "foreground_package": "com.example.video",
      "first_seen_at": 1760000000.0,
      "last_observed_at": 1760000300.0,
      "warning_sent_at": 1760000305.0,
      "warning_deadline": 1760000905.0,
      "owned_suspended": ["com.example.video"],
      "enforced_until": 1760002705.0,
      "enforcement_count": 1,
      "last_error": ""
    }
  }
}
```

不要把通知正文、屏幕全文或 Token 写入这个状态文件。只保存必要的包名、时间、状态和审计 ID。

## 6. 配置设计

新增配置建议全部默认关闭或保守：

```json
{
  "enable_night_guard": false,
  "night_guard_start": "00:30",
  "night_guard_end": "08:00",
  "night_guard_check_interval_seconds": 300,
  "night_guard_status_max_age_seconds": 90,
  "night_guard_grace_minutes": 10,
  "night_guard_suspend_minutes": 30,
  "night_guard_consecutive_hits": 2,
  "night_guard_max_enforcements_per_night": 2,
  "night_guard_warning_cooldown_minutes": 60,
  "night_guard_packages": "哔哩哔哩,快手极速版,优酷,抖音,抖音极速版",
  "night_guard_exempt_packages": "",
  "night_guard_private_only": true,
  "night_guard_dry_run": true,
  "night_guard_state_path": "data/plugin_data/astrbot_plugin_phone_agent/phone_agent_night_guard.json"
}
```

实现时要：

- 允许别名和完整包名，最终只保存完整包名。
- `night_guard_packages` 为空时不启用任何默认包，避免误停应用。
- 跨午夜时间段按 `[start, 24:00) ∪ [00:00, end)` 计算。
- 间隔、宽限期、暂停时长和每日次数设上下限。
- `dry_run=true` 只观察、审计和发送测试提醒，不执行暂停。
- `night_guard_private_only=true` 时拒绝群聊目标。

生产首轮应保持 `dry_run=true`，观察至少一晚日志后再切到 false。

## 7. 后端改造步骤

### 7.1 增加状态和任务生命周期

在 `PhoneAgentPlugin.__init__` 中增加：

- `_night_guard_task`。
- `_night_guard_states`。
- `_night_guard_lock`，避免多个 tick 同时处理同一用户。
- `_load_night_guard_state()` / `_save_night_guard_state()`。

在 `terminate()` 中取消任务并保存状态。不要调用普通临时策略的恢复逻辑来误伤其他来源的策略；恢复操作必须根据 `owned_suspended` 执行。

增加一个明确的启动方法，例如 `_start_night_guard()`，只在配置开关开启、插件启用且策略工具可用时创建后台任务。每个 tick 必须有总超时，不能阻塞 AstrBot 事件循环。

### 7.2 获取前台状态

优先顺序：

1. 使用新鲜的 App 状态心跳。
2. 心跳不存在或过期时，主动执行一次 App `status`。
3. 两者都不可用时跳过本轮，不尝试 Operit 自然语言猜测。

建议状态结果至少包含：

```json
{
  "success": true,
  "foreground_package": "com.example.video",
  "reported_at": 1760000300.0,
  "shizuku_ready": true,
  "screen_interactive": true
}
```

`screen_interactive` 可由 App 的 `dumpsys power` 读取；如果拿不到，必须明确标记未知，不要把未知当作 true。

### 7.3 增加状态心跳接口

建议新增：

```text
POST /astrbot_plugin_phone_agent/device/status
Authorization: Bearer <dashboard-jwt>
```

请求正文只包含设备状态，不包含屏幕全文：

```json
{
  "device_id": "device-...",
  "app_version": "0.3.0",
  "battery_percent": 42,
  "charging": false,
  "foreground_package": "com.example.video",
  "screen_on_seconds_today": 12345,
  "screen_interactive": true,
  "shizuku_ready": true,
  "reported_at": 1760000300.0
}
```

后端要验证：设备已注册、设备 ID 匹配、时间有限偏差、字段类型和包名长度。状态缓存要带 `reported_at`，并在 `/device/status`、`/status` 和夜间守护内部复用。

App 的 `PluginBridge` 每 30–60 秒上送一次；上送失败只更新连接状态，不阻塞本地命令服务。状态推送不要把共享 Token 放进新的日志或状态文件。

### 7.4 发送 QQ 提醒

不要让夜间 worker 自己构造群聊目标。推荐复用 Private Companion 已验证的私聊用户/会话映射：

- 仅接受目标用户最近一次合法的私聊 `unified_msg_origin`。
- 会话过期或不是私聊时跳过。
- 发送失败保留 `warning_sent_at` 和错误，不立即无限重试。
- 文案可以由模板生成；若调用 LLM，只传结构化事件：包名映射后的应用名、当前时间、是否首次提醒，不传屏幕全文。

建议提供确定性模板作为 fallback：

```text
已经很晚了，还在刷{app_name}。先放下手机，早点睡；{grace_minutes}分钟后我再看一次。
```

发送必须使用已有 `context.send_message(session, MessageChain(...))` 路径，并写入审计：`night_guard_warning_sent`。

### 7.5 执行暂停和恢复

暂停单个前台包时使用：

```text
POST /astrbot_plugin_phone_agent/app_policy
{
  "action": "disable",
  "package": "com.example.video",
  "minutes": 30
}
```

恢复使用：

```text
POST /astrbot_plugin_phone_agent/app_policy
{
  "action": "enable",
  "package": "com.example.video",
  "minutes": 0
}
```

后台 worker 应直接调用内部 `_execute_app_policy()`，不要通过 HTTP 回调自己。必须检查返回的 `success=true`、`verified=true` 和目标包完全匹配。

不要用 `phone_sleep_mode` 处理单个前台包；它是批量守夜接口，会操作配置中的多个包。夜间守护需要拥有自己的 `owned_suspended` 列表。

### 7.6 用户解除

需要一个确定性的低歧义入口，优先级高于普通 LLM 工具调用：

- 识别私聊中的“解开、解除限制、恢复视频、让我继续用”等明确指令。
- 只对已授权用户生效。
- 只恢复 `owned_suspended`。
- 清除当前警告/宽限期，进入 `user_released` 冷却状态。
- 回复实际恢复结果；失败时不要声称已解开。

如果暂时不改消息路由，可以先让 LLM 调用 `phone_sleep_mode(mode=stop)`，但这不是最终方案，因为它可能恢复其他来源的守夜包。

## 8. Android App 改造步骤

### 8.1 状态心跳

在 `PluginBridge.kt`：

1. 复用现有 `refreshLocalStatus()` 的结果。
2. 每 30–60 秒调用 `AstrBotClient.postDeviceStatus()`。
3. 上送失败不要清空本地状态；标记 `lastError`。
4. App 版本使用 `BuildConfig.VERSION_NAME`，不要保留与 Gradle 版本不一致的硬编码默认值。

在 `Protocol.kt` 增加状态字段时同步更新后端 `device_app.py` 和 `docs/APP_API_V2.md`。

### 8.2 前台包和交互状态

`ShizukuCommandExecutor.status()` 已经可以读取 `foreground_package`。补充：

- `screen_interactive`。
- 读取失败时返回 null/unknown，不要伪造空包为桌面。
- 包名提取要继续使用严格包名正则。

### 8.3 快捷操作补齐

- 截图快捷项改文案，说明图片会通过插件回传；如果要“保存到相册”，实现独立的本地保存行为，不要混淆两者。
- 专注/守夜显示当前目标包、来源、剩余时间和失败原因。
- 解除限制只调用带来源校验的后端恢复接口。
- 所有任务结果展示 `success`、`verified`、`error_code`；不能只显示“命令发出”。
- 市集占位页保留为占位，不把它列为已完成功能。

### 8.4 通知转发修复

`NotificationRelayService` 需要：

- 4 秒窗口内安排一次延迟 flush，而不是只在下一条通知到来时 flush。
- 上传失败保留有限次数的批次重试；超过次数再丢弃并记录原因。
- 服务销毁前尝试 flush，但不能阻塞主线程。
- 仍然过滤 App 自己、ongoing 和 group summary 通知。
- 允许用户按应用关闭转发，避免把所有通知正文上传。

## 9. companion-context 改造边界

不要把整个夜间守护塞进 `companion-context` 的文本包装器。推荐：

- 后端 Phone Agent 负责时间窗口、观察、状态机、暂停和恢复。
- Private Companion/companion-context 负责为已确定的 `warning` 事件生成自然语气。
- LLM 输入使用严格 JSON 事件，输出只允许：

```json
{"intent":"warn|no_action","message":"..."}
```

- 输出解析失败使用固定模板。
- LLM 不得输出包名、shell、动作参数或“已暂停”的未经验证声明。
- `autonomous_decision_enabled` 只能作为消息风格/主动提醒开关；在新的守护开关没有完成前，不要把它解释为手机自动控制开关。

## 10. 测试计划

### 后端单元测试

新增 `tests/test_night_guard.py`，至少覆盖：

1. 夜间窗口普通情况和跨午夜情况。
2. 开关关闭、dry-run、非私聊、未授权用户。
3. 空白/非法/未注册包名。
4. 状态过期、手机离线、Shizuku 不可用。
5. 连续命中计数，目标包变化后清零。
6. 首次提醒只发送一次，冷却期不重复发送。
7. 宽限期到后再次核验仍为同一个包才暂停。
8. 暂停失败或核验失败不进入 `enforced`。
9. 自动恢复只恢复 `owned_suspended`。
10. 用户解除只恢复守护拥有的包。
11. 每晚最大执行次数。
12. 插件重启后恢复状态，过期状态不会重复执行。
13. 发送 QQ 失败时保留可恢复状态，不假报成功。
14. 所有状态文件禁止出现 Token、屏幕全文和通知正文。

### App 单元测试

至少增加：

- `screen_interactive` 和前台包解析。
- 状态心跳 JSON 序列化、时间戳和空值。
- 状态上送失败后的重试/退避。
- 通知延迟 flush 和失败重试。
- Release R8 后反射调用、Service、Serializer 仍存在。

### 集成测试

使用假的 `DeviceAppBackend`，不要在 CI 对真实手机执行 `pm suspend`：

```text
status(video) -> warning sent -> status(video) -> suspend verified
status(video) -> warning sent -> status(home) -> no suspend
status(video) -> warning -> user release -> unsuspend verified
stale status -> no action
```

### 真实设备 Smoke Test

上线前按顺序执行：

1. App 中确认 Shizuku、通知权限、插件认证和共享 Token。
2. 后端 `POST /device/test`。
3. 后端 `GET /device/status`，确认 `health.available=true`、状态时间新鲜、前台包正确。
4. 手动执行一次 `phone_observe`，确认 App 模式返回 `foreground_package`。
5. 用测试包名开启 `night_guard_dry_run=true`，确认只发提醒不暂停。
6. 在确认日志正确后关闭 dry-run，测试单包暂停和核验。
7. 测试 QQ “解除限制”，确认只恢复守护拥有的包。
8. 重启 AstrBot，确认状态恢复和临时策略行为符合设计。

## 11. 部署步骤

### 部署前

1. 备份生产插件目录、配置 JSON、策略状态、审计日志和 App APK。
2. 记录当前 systemd user 服务状态和最近日志。
3. 确认本地 Git 工作区干净，记录提交号。
4. 不把服务器密码或 Token 写进命令历史、文档、Git 或测试输出。
5. 首次部署只启用 dry-run。

### 后端部署

1. 将后端文件复制到生产插件安装目录的临时目录。
2. 运行 Python 测试；生产环境缺 pytest 时，使用独立虚拟环境或 CI 测试结果，不要临时修改生产 Python 环境。
3. 原子替换插件文件。
4. 检查配置 schema 和配置文件加载成功。
5. 重启 `astrbot.service`。
6. 查看日志，确认插件加载、Web API 注册和夜间 worker 启动。
7. 访问 `/device/status` 和 `/status`，确认没有凭据泄漏。

### App 部署

1. 先构建 Debug 验证，再构建 Release。
2. 运行 `:app:testDebugUnitTest`、`:app:lintDebug`、`:app:assembleRelease`。
3. 安装到测试手机，确认版本号和协议能力集合。
4. 打开 App，确认前台状态、插件连接、Shizuku 和通知监听。
5. 再开始 dry-run 夜间测试。

### 启用真实暂停

仅在至少一晚 dry-run 日志确认无误后：

```json
{
  "enable_night_guard": true,
  "night_guard_dry_run": false
}
```

同时确认 `allowed_user_ids`、私聊会话、目标包清单和最大执行次数。

## 12. 回滚步骤

触发以下任一情况立即关闭守护：误暂停、无法恢复、消息发错会话、状态过期仍执行、服务内存/CPU 异常。

1. 先把 `enable_night_guard=false` 写入配置并保存。
2. 停止/恢复由状态文件中 `owned_suspended` 记录的包。
3. 恢复后端插件备份和配置备份。
4. 重启 `astrbot.service`。
5. 检查 `/device/status`、策略状态和审计日志。
6. 保留失败状态和日志，不要直接删除，以便定位。

不要用“恢复全部视频应用”代替按拥有权恢复；这可能解除用户手动设置的限制。

## 13. 运行命令和接口速查

### 观察

```text
phone_observe()
```

App 模式应返回设备状态和 `foreground_package`；状态必须带新鲜时间。后台 worker 不应依赖 LLM 调用这个工具。

### 单包策略

```text
phone_app_policy(action="disable", package="com.example.video", minutes=30)
phone_app_policy(action="enable", package="com.example.video")
```

### 批量守夜接口

```text
phone_sleep_mode(mode="start", minutes=480)
phone_sleep_mode(mode="stop")
```

这是现有人工/批量入口；新的夜间守护不要把它当作单包状态机实现。

### App 协议

App 和后端的闭合命令集合必须同步。新增命令时同时修改：

- `device_app.py: COMMAND_ACTIONS`
- App `Protocol.kt: COMMAND_ACTIONS`
- App 执行器
- `docs/APP_API_V2.md`
- 双方测试

## 14. 验收标准

完成后，以下场景必须全部成立：

1. 夜间开关关闭时，系统不读取前台、不发消息、不暂停。
2. dry-run 模式会记录和提醒，但绝不执行暂停。
3. 前台是白名单视频 App 时只提醒一次；离开视频 App 不暂停。
4. 宽限期后仍在同一个视频 App，才暂停该包。
5. 暂停返回未经核验或目标包不匹配时，系统报告失败且不进入成功状态。
6. 用户说“解开”后只恢复本次守护暂停的包。
7. 夜间结束和临时期限到达后自动恢复守护拥有的包。
8. 手机离线或状态过期时不会执行动作。
9. 群聊不会收到夜间守护消息。
10. AstrBot 重启、App 重启、网络短暂中断后不会重复暂停或丢失拥有权。
11. 审计可以回答：何时观察、观察到什么包、何时提醒、何时暂停、是否核验、何时恢复以及失败原因。
12. 所有 Python 测试通过，App Debug/Release/lint 通过，且生产 dry-run 观察结果与测试预期一致。

## 15. 当前明确不应做的事

- 不要只打开 `autonomous_decision_enabled` 就认为夜间守护完成。
- 不要让 LLM 直接生成 `pm suspend` 或任意 shell。
- 不要根据通知内容推断用户正在刷视频；通知只能作为可选辅助信号。
- 不要根据一次旧的前台包状态暂停应用。
- 不要把 App 注册成功当作 App 当前在线。
- 不要把 `phone_sleep_mode(stop)` 当作所有来源策略的全局解锁。
- 不要在没有私聊会话时退回群聊发送提醒。
- 不要把手机截图、通知正文、Token 写进守护状态或普通日志。

