# 界面任务与第二台设备

本文对应插件 **0.10.0** 与 Android App **0.4.0**。旧的固定动作、OperitAI、健康查询和关怀扩展继续保留；这里介绍新增的单应用界面任务。

## 两个 App 怎么选

| | Phone Control | Phone Control Bot |
| --- | --- | --- |
| 用途 | 你的个人手机 | Bot 专用的第二台手机 |
| Android 包名 | `com.tauru.astrbotphoneagent` | `com.tauru.phonecontrol.bot` |
| 设备控制、状态、界面任务 | 支持 | 支持 |
| 健康数据 | 保留可选 Health Connect 来源与摘要同步 | 不包含 Health Connect SDK、健康权限、读取或同步实现 |
| 界面重点 | 个人状态、健康、关怀与设备操作 | 设备执行、任务结果与连接设置 |

两个 APK 有独立安装身份和应用存储。个人版保留原包名，按原签名升级可沿用连接设置；新安装的 Bot 版要单独填写服务器连接、启动 Shizuku 并授权。建议分别安装在两台物理手机上，不要求更换现有个人 App。

Bot 仍使用手机控制所需权限；“无健康功能”不表示它无法看到你在 Bot 手机上打开的页面。请根据实际用途决定在第二台手机登录哪些应用。

## 设备用途和路由

- `personal` 是「我的手机」。原有个人主设备被固定，后来注册的设备不会顶替它。
- 首个 Bot App 通常登记为 `bot`（「Bot 的手机」）；用途已被占用或额外个人设备会成为 `unassigned`。
- `unassigned` 设备须先设置用途，才能发起界面任务。目前支持一个个人主设备和一个 Bot 角色。
- Bot App 不能改成个人主设备；主设备迁移不是简单改标签或用途。不要通过删状态文件绕过迁移检查。
- 任务明确绑定设备、应用和注册版本。目标离线、角色变化或连接绑定变化会使任务停止，不会改用另一台手机。
- 个人设备的旧直连配置不会被拿来执行 Bot 任务。第二台设备需有自己的有效注册和直连/Relay 路径；Bot 的 Relay 路径必须满足下节的定向轮询要求。

在任务页选择目标设备，再核对名称、用途和设备 ID。聊天工具的默认角色是 `personal`；只有明确请求操作 Bot 手机时才选择 `bot`，也可指定准确的 `device_id`。

这些角色是同一部署中的路由和功能分工。两台设备仍使用该部署的 Dashboard/命令鉴权，角色与独立包名**不构成不同账户之间的权限隔离**。不要据此把同一服务器凭据共享给互不信任的用户。

### 第二台手机的 Relay 前提

使用 Relay 连接 Bot 手机时，需更新正在运行的 Relay 至 **0.10.0 或支持相同协议的后续版本**，并确认其 `/health` 返回 `targeted_polling: true`。仅有旧版的 `targeted_tasks: true` 不够：投递时绑定设备和轮询时只领取本设备任务都必须生效。

Phone Control Bot 只使用按 `device_id` 定向的轮询，不参加没有设备筛选的共享领取。旧 Relay 未声明 `targeted_polling` 时，Bot 的 Relay 路径会明确显示不可用；不会改用无目标队列，也不会转由个人手机执行。已建立的 Bot 直连仍可使用。

更新 Relay 使用本插件同版本的 `deploy/relay/relay_server.py`，保留原有 Relay Token、监听地址和状态文件配置，再重启实际承载 Relay 的进程/服务。只更新插件文件、重载 AstrBot 插件或重启 FRP 隧道，都不能代替 Relay 进程升级。先检查运行中的 `/health` 能力，再让第二台 App 重新连接。

## 服务器准备

使用 App 后端，配置用户白名单及共享 Token，并同时开启三个开关：

```json
{
  "enabled": true,
  "control_backend": "app",
  "allowed_user_ids": "YOUR_ASTRBOT_USER_ID",
  "app_shared_token": "REPLACE_WITH_A_RANDOM_SHARED_TOKEN",
  "enable_ui_tasks": true,
  "enable_task_execute": true,
  "enable_observe_tool": true,
  "ui_task_allowed_packages": "tv.danmaku.bili,com.android.chrome,com.android.settings,com.tencent.mobileqq",
  "ui_task_max_steps": 8,
  "ui_task_provider_id": ""
}
```

| 配置 | 含义 |
| --- | --- |
| `enable_ui_tasks` | 新界面任务入口，默认关闭 |
| `enable_task_execute` | 允许经批准的任务实际执行，默认关闭 |
| `enable_observe_tool` | 允许观察设备/页面，默认关闭 |
| `ui_task_allowed_packages` | 逗号分隔的目标包名；每次任务仍只批准一个应用 |
| `ui_task_max_steps` | 默认 8，范围 1–12；打开目标应用也消耗一步 |
| `ui_task_provider_id` | 可选 AstrBot 模型提供商 ID；留空先沿用 `task_planner_provider_id`，再取请求会话/当前模型 |

保存并重载插件。界面任务需要可用的服务器模型，模型应能遵守单步 JSON 输出；手机 App 本身不需要配置模型。它与原有 `enable_llm_task_planner` 的固定动作规划是两条入口，开启后者不会自动开启界面任务。

## 执行手机准备

1. 安装 App 0.4.0+，完成服务器连接，确认设备注册和 Shizuku 授权。
2. 在 Android 系统输入法设置中，手动启用该 App 提供的 **Phone Control 输入**。任务页提供入口；启用不是把它永久设为默认键盘。
3. 在**实际执行的那台手机**打开「隐私与自动化」，如需连续执行，明确开启「读取界面节点」与「操作界面」免确认。
4. 保持屏幕亮起、已解锁，目标 App 已安装；先手动处理登录、验证码、首次权限弹窗等准备事项。
5. 在执行期间避免同时操作同一页面。前台切换、布局变化或系统窗口遮挡可能让任务暂停。

两项 UI 免确认默认关闭。不开启时仍会逐条要求本机确认，弹窗引起的页面变化、等待时间和快照期限可能使任务停止。远程发起端不能代替执行设备开启这些设置。

开启免确认意味着本机信任已连接服务器发送这两类命令。**手机端没有因此获得服务端任务审批的独立验签能力**；服务端仍负责逐次核对任务范围，本机继续检查设备 ID、当前应用、快照、目标节点和输入连接。不要把两层检查理解为跨账户授权系统。

输入文字时临时切换到 Phone Control 输入法，只写入已批准的原文，不使用共享剪贴板。结束、失败或取消后会尝试恢复之前的输入法；若显示恢复失败，文字可能已写入，应先手动恢复并检查页面，不重复点击执行。

## 每次任务如何批准

从 Bot App 首页「让手机完成一件事」、个人版设置中的自主功能或聊天工具 `phone_ui_task` 提出任务。生成提案本身不会操作手机。

在完整任务详情中核对：

- **执行设备**：个人手机还是 Bot 手机。
- **单个目标应用**：包名必须在服务器白名单中。
- **目标**：这一次要做的具体事情。
- **允许输入的原文**：最多 5 项，每项最多 200 字符；不需要输入可留空。模型不能另编要输入的内容。
- **完成时应出现的文字**：至少 1 项、最多 5 项，必须能在结果页面核验。
- **动作步数**：不能超过服务器上限，默认 8 步。

批准后任务异步运行，可在执行记录中查看进度。UI 任务需要完整范围核对，不能用只显示简短摘要的悬浮卡片跳过详情。只打开应用、命令返回成功或输入框里有关键词，都不等于任务完成。

服务端把当前页面节点发给模型，模型只选择当前节点的点击、输入、滚动或返回。每次操作后重新读取页面，并在结束前再次核验完成条件。任务有总时限和模型调用次数上限，不会无限循环。模型选择不可识别节点、重复无进展、快照失效、应用跳转或敏感页面都可能终止本次尝试。

## 三类使用示例

### B 站搜索

App 选择「B 站搜视频」，填写如 `哆啦A梦`，选择执行设备，再生成待确认任务。模板目标是打开 `tv.danmaku.bili`、输入该词并进入实际视频结果页。

验收要求包括搜索词、「综合」「视频」等结果页信息，以及相关结果标题。搜索历史、联想列表、输入框原文和首页分类不算完成。页面缺少可访问节点、出现登录/升级弹窗、结果标题没有所要求文字时，即使手机已搜索，也可能返回未核验或需人工处理；应检查页面，不宣称成功。

聊天请求示例：

```text
在 Bot 的手机上打开 B 站搜索“哆啦A梦”，停在结果页，不点赞、不评论。
```

模型应调用 `phone_ui_task` 生成提案，随后用返回的 `run_id` 调用 `phone_task_status` 查询。只有 `done` 且核验成功才报告完成；“提案已创建”不等于已执行。此示例是验收方法，不代表所有设备或 B 站版本均已验证兼容。

### 浏览与非敏感设置

自定义任务可要求进入一个指定页面、浏览当前应用内容，或尝试有清楚标签的非敏感设置。填写可见的结果条件，如进入显示设置后出现「亮度」页面信息。

当前仅在一个应用包名内执行。跳到系统权限页、登录页或另一个应用会暂停；权限授予、安装/卸载、清除数据、账号操作、付款和含糊的「确定/继续」按钮交人工处理。它不会绕过这些限制继续完成任务。

### 社交草稿

允许输入列表填写你确认过的整段原文，目标明确写“只填入草稿，停下交给我，不发送”。模型可以尝试定位输入框并填写，发送/发布/评论动作仍会交人工。

当前完成核验不把编辑框内容当成最终结果。因此“草稿已经填入”可以成为已核验的输入步骤，但整个任务可能停在需接手或未核验状态。到手机检查后自行决定是否发送；不要为了让状态变成完成而添加发送条件。

## 取消、失败与重试

任务进行中可以取消。取消阻止服务端继续下发后续步骤，但已发送到手机的动作、网络请求或正在执行的输入可能仍完成，取消不能撤销已发生的修改。响应丢失或超时后，当前步骤会保留结果未知的语义，不自动重放写操作。

人工接手或失败的运行不会自动恢复。先查看目标手机，处理需要你完成的步骤，再根据当前状态创建新的范围明确的任务。角色变更、设备重新绑定和服务器重启后，也不能沿用旧批准继续写操作。

常见提示：

| 提示或现象 | 处理 |
| --- | --- |
| 服务器未支持、`app_upgrade_required` | 核对插件 0.10.0 / App 0.4.0，并让执行设备重新注册 |
| 功能关闭 | 检查三个开关、App 后端和用户白名单 |
| `needs_input_method` | 到执行设备手动启用对应 App 的输入法 |
| `denied`、确认等待超时 | 检查本机确认或明确选择是否开启两项 UI 免确认 |
| 快照过期、页面变化、目标被遮挡 | 保持目标页面稳定，关闭遮挡窗口，查看实际状态后重新提出任务 |
| `device_changed`、目标离线 | 检查所选设备的注册、角色和网络；不会回退到另一设备 |
| Bot 显示 Relay 不支持或不可用 | 更新并重启实际 Relay 服务，确认 `/health` 的 `targeted_polling` 为 `true`；旧的 `targeted_tasks` 标志不够 |
| 输入后不能核验或输入法恢复失败 | 检查文字是否已写入并恢复键盘，不直接重放 |
| 已搜到内容但未完成 | 核对完成条件是否真的出现在结果节点；不能只用搜索框文字作证 |

## 数据与能力边界

模型会收到任务目标、批准的输入原文、完成条件，以及目标应用当前可见节点中的文字、描述、类型与位置。输入敏感页面前应考虑所选模型服务的数据处理方式。页面内容被当作不可信数据，不能扩大原任务授权。

插件的任务记录会保留审批范围、输入原文、条件与状态；原始屏幕节点和模型回复不作为任务历史持久保存。App 对 UI 命令回执使用哈希身份，成功的快照响应不会写入回执档案。这不等于外部模型服务不会记录请求。

当前依赖 Android 可访问的界面树，不提供通用视觉坐标猜测；游戏、画布、部分 WebView、自绘控件、过大的节点树或动态页面可能无法处理。单次观察和输入长度有上限，锁屏、密码框与不明前台状态会被拒绝。手机与服务器都有有限时限，慢模型可能使快照过期。

这是经过批准的有限尝试，不承诺任意 App 自动完成、自动登录、支付、发布消息或跨应用工作流。OperitAI 仍保留自己的手机端模型与工作流路径。

## English quick reference

Use Phone Control on the personal phone and Phone Control Bot on the dedicated second phone. Bot has a separate package/storage and excludes Health Connect. Roles select devices within one deployment; they do not isolate mutually untrusted accounts.

Bot relay fallback requires Relay 0.10.0+ with `targeted_polling: true` in `/health`; the older `targeted_tasks` flag alone is insufficient. Bot never polls an unfiltered shared queue. An older relay is explicitly unavailable for Bot, while a working direct route can still be used. Upgrade and restart the actual relay process, not just AstrBot or the FRP tunnel.

Enable `enable_ui_tasks`, `enable_task_execute` and `enable_observe_tool`; set a package allowlist and optionally `ui_task_provider_id`. On the execution device, manually enable its Phone Control input method and explicitly opt into the two UI no-confirmation switches for continuous operation. Each server task still requires full scope approval. The model receives visible UI-node text and may type only approved literals.

Default action budget is 8, configurable to at most 12, including app launch. Bilibili search needs actual result-page evidence. Drafts are never sent automatically; editable text alone does not prove overall completion. Sensitive settings, login, payment, deletion and publishing hand off to the user. Cancellation stops later steps, but an already dispatched action can have an unknown result. Check the phone before creating a new attempt.

填写了允许输入文字的自定义任务，必须至少完成一次输入并在新观察中核对原文，才允许结合页面条件判定完成；原先就存在的“聊天”等标题不能跳过这一步。允许文字列表表示可用原文范围，不代表全部文字都必须输入。

个人版首页保留个人状态和健康摘要，不显示新增的大块任务入口；Bot 专用版首页显示发起任务与审批进度。B 站版本之间的结果标签可能不同，模板只要求关键词和“综合”，同时仍检查结果页上下文与非输入框中的匹配结果，并以第二次读屏确认。
