# AstrBot Phone Agent

[English](README.en.md) · [App API v2](docs/APP_API_V2.md)

将 AstrBot 接入 Android 手机的插件，提供两种控制模式：

| 模式 | Android 端 | 适用场景 |
| --- | --- | --- |
| `app` | AstrBot Phone Agent App + Shizuku | 结构化手机指令、设备状态、提醒与时间线 |
| `operit` | OperitAI External HTTP | 自然语言任务、识别屏幕、点击和输入 |

Android 前端：[AstrBot Phone Agent App](https://github.com/Tauru-t6/astrbot-phone-agent-app)。App 内聊天使用 AstrBot 聊天 API；本插件为 AstrBot 提供手机工具和提醒同步。

## 安装

要求 AstrBot 4.22 或更高版本。在 AstrBot 插件市场通过仓库地址安装，或执行：

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

重启 AstrBot 或在管理页面重载插件。运行依赖由 AstrBot 提供，无需安装单独的手机 Agent 模型。

## App 模式

手机安装 AstrBot Phone Agent App，启动 Shizuku 并授权 App。配置示例中的域名是占位符，请替换成你的地址：

```json
{
  "enabled": true,
  "control_backend": "app",
  "app_shared_token": "在插件和手机两端填入同一个随机命令Token",
  "app_direct_urls": "http://phone-lan.example:8260,http://phone-tailnet.example:8260",
  "allowed_user_ids": "允许控制手机的AstrBot用户ID",
  "enable_observe_tool": true,
  "enable_reminder_tools": true,
  "enable_audit_tool": true
}
```

`app_direct_urls` 按逗号分隔顺序尝试；建议内网在前、Tailscale 在后。配置地址优先于手机注册的地址，服务器重启后仍可使用。每个地址先进行最多 3 秒的健康检查，所有直连和 Relay 尝试共享 `app_command_timeout_seconds` 总预算。

App 的共享命令 Token 只用于手机命令服务；读取插件路由使用 AstrBot 管理端认证，聊天使用聊天 API 认证。请分别在 App 设置中填写，不能互相替代。

`phone_action` 在 App 模式直接下发白名单结构化命令，例如打开/关闭 App、返回、主页、锁屏和唤醒；不会落到 ADB 执行。任意坐标点击、滑动、输入文字和工作流属于 Operit 模式。底层命令协议还包含截图、读屏、定位、使用统计、应用限制、通知和 ping，实际可用性取决于手机权限和配置。`phone_observe` 在 App 模式当前读取设备状态；它不执行通用屏幕推理。

如果手机返回明确拒绝或参数错误，插件会直接返回结果，不再换路径重复执行。传输失败切换地址或 Relay 时复用同一命令 ID；新 App 会去重。

## Operit 模式

```json
{
  "enabled": true,
  "control_backend": "operit",
  "operit_base_url": "http://phone-tailnet.example:8094",
  "operit_token": "Operit External HTTP Token",
  "allowed_user_ids": "允许控制手机的AstrBot用户ID"
}
```

OperitAI 需要已配置可用模型、工具调用、Shizuku 权限和 External HTTP。此模式的 `phone_action` 和 `operit_task` 由 Operit 执行；App 模式拒绝自然语言 `operit_task`。旧别名 `phone_buddy`、`native_app`、`own_frontend`、`operitai`、`operit_ai` 会归一化为 `app` 或 `operit`。`adb` 仅保留给旧配置诊断，不是控制台主选项。

## 提醒和时间线

App 通过 `GET /app/state` 读取功能开关、待办提醒、近期命令结果和最近 100 条时间线。该接口不探测手机，手机离线时也能迅速返回。

App 可创建 1 分钟至 7 天的提醒并取消；UUID 请求 ID 防止网络重试重复创建。提醒和时间线使用原子文件写入，重启不会清空。到期后先保存 `reminder_due` 时间线，再删除待办；手机轮询后根据 `notification_id` 去重并显示通知。App 创建的提醒不发送 QQ；聊天创建的提醒保留原会话投递。

服务器重启后，过期未满 24 小时的提醒会补发；更早的提醒只记为已过期，不突然响铃。取消和创建事件不会触发通知。手机离线、后台轮询限制或未授予通知权限时，通知可能延迟；这不是手机系统精确闹钟。已结束请求 ID 保留 30 天用于幂等重试。

状态文件配置：`reminders_path`（待办与幂等回执）、`app_state_path`（时间线）、`audit_log_path`（操作元数据）。提醒文件会保存提醒文本，请按个人数据保管。

## 可选功能

| 功能 | 配置项 | 工具 |
| --- | --- | --- |
| 观察 | `enable_observe_tool` | `phone_observe` |
| 使用统计 | `enable_usage_tool` | `phone_usage` |
| 提醒 | `enable_reminder_tools` | `phone_reminder`、App 提醒 API |
| 定位 | `enable_location_tool` | `phone_location` |
| 应用限制 | `enable_policy_tools` | `phone_app_policy`、`phone_sleep_mode` |
| 审计 | `enable_audit_tool` | `phone_audit` |
| 小米健康库 | `enable_health_tools` | `phone_health` |

这些功能默认关闭。开启控制前设置 `allowed_user_ids`，并在手机端完成相应权限或危险动作确认。

## 控制台和 Relay

Phone Control 页面可选择 App / Operit 模式、编辑对应地址和 Token、测试当前后端、查看提醒、应用限制、Operit 后台任务与审计。Token 输入框留空保留原值；页面仅显示是否已配置。

`deploy/relay/` 提供可选 Relay 服务。配置 `relay_base_url` 和 `relay_token` 后，直连传输失败可入队。Relay 需要手机轮询，离线时任务可能在插件等待超时后执行；插件返回超时不会宣称任务已完成。

## 开发验证

```bash
python -m pip install quart pytest
python -m pytest tests -q
python -m py_compile main.py device_app.py
```

测试使用真实 Quart 请求及临时状态文件，并 stub AstrBot 宿主接口。手机网络、Shizuku 权限和系统通知仍需在实际设备上验证。
