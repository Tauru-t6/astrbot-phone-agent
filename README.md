# AstrBot Phone Agent

把 AstrBot 和 Android 手机连接起来的插件，提供两个产品模式：

| 模式 | Android 端 | 适用场景 |
| --- | --- | --- |
| `app` | Tauru Phone Agent App + Shizuku + 结构化命令 | 陪伴聊天、提醒、时间线、设备状态、截图和可预测的手机控制 |
| `operit` | OperitAI HTTP API | 需要识别屏幕、点击、输入和自然语言推理的 UI 任务 |

旧配置别名 `phone_buddy`、`native_app`、`operitai`、`operit_ai` 仍然兼容；新配置请使用 `app` 或 `operit`。

## Architecture

```text
Chat platform -> AstrBot -> astrbot-phone-agent
                              |-- app: structured HTTP -> Tauru Phone Agent App
                              |                  \-> Relay fallback
                              \-- operit: natural-language HTTP -> OperitAI
```

## Mode A: Tauru Phone Agent App

App 模式是项目自己的 Android 前端。手机端通过 Shizuku 执行白名单命令，插件只下发结构化 JSON，不把自然语言直接交给手机执行。

直连地址按顺序尝试：手机内网地址、手机 Tailscale 地址，最后才进入 Relay。例如：

```
http://192.168.2.110:8260,http://PHONE_TAILSCALE_IP:8260
```

Android 前端仓库和 Release：

<https://github.com/Tauru-t6/astrbot-phone-agent-app>

安装 App 后启动 Shizuku、授权手机搭子，并在 App 设置中配置 AstrBot 地址、插件地址、共享命令 Token 和可选 Relay。

服务器侧最小配置：

```json
{
  "enabled": true,
  "control_backend": "app",
  "app_shared_token": "与 App 完全相同的随机 Token",
  "app_direct_urls": "http://192.168.2.110:8260,http://PHONE_TAILSCALE_IP:8260",
  "relay_base_url": "https://你的 Relay 域名",
  "relay_token": "你的 Relay Token",
  "allowed_user_ids": "允许控制手机的 AstrBot 用户 ID",
  "enable_observe_tool": true,
  "enable_reminder_tools": true,
  "enable_audit_tool": true
}
```

App 模式支持 status、打开/关闭应用、前台应用、锁屏/唤醒、返回/主页、截图、屏幕文字、应用暂停/恢复、使用统计、定位、通知和 ping。提醒、时间线和设备状态也会同步到 App。

App 模式只接受固定命令集合和结构化参数，不执行任意 shell，也不接受自然语言任务。高危动作必须先经过 AstrBot 会话确认，手机端还会再次弹出确认框。

## Mode B: OperitAI

Operit 模式保留原来的 OperitAI 能力，适合识别屏幕、点击、输入和自然语言 UI 任务：

```json
{
  "enabled": true,
  "control_backend": "operit",
  "operit_base_url": "http://PHONE_TAILSCALE_IP:8094",
  "operit_token": "Operit External HTTP Bearer Token",
  "allowed_user_ids": "允许控制手机的 AstrBot 用户 ID"
}
```

OperitAI 必须已经配置可用模型、工具调用、Shizuku 权限以及 External HTTP。此模式可以使用 `operit_task`。App 模式下 `operit_task` 会被拒绝，请使用 `phone_action`。

## Install

要求 AstrBot 4.22 或更高版本：

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

重启 AstrBot 或在 WebUI 重载插件。插件本身不需要额外 Python 依赖。

## Optional features

| 功能 | 配置项 | 工具 |
| --- | --- | --- |
| 观察屏幕 | `enable_observe_tool` | `phone_observe` |
| 使用统计 | `enable_usage_tool` | `phone_usage` |
| 提醒 | `enable_reminder_tools` | `phone_reminder`、App 提醒 API |
| 定位 | `enable_location_tool` | `phone_location` |
| 应用限制 | `enable_policy_tools` | `phone_app_policy`、`phone_sleep_mode` |
| 审计 | `enable_audit_tool` | `phone_audit` |
| 小米健康库 | `enable_health_tools` | `phone_health` |

启用手机控制前请设置 `allowed_user_ids`，不要把控制入口开放给所有聊天用户。

## Relay fallback

`deploy/relay/` 提供标准库实现的 Relay 队列，包含 Bearer 鉴权、任务租约、可选设备隔离、请求限流和请求体大小限制。生产环境请放在 HTTPS 反代后，Token 只放服务器配置，不要提交到 GitHub。

## WebUI

Phone Control 页面可查看后端状态、App 注册、直连/Relay 诊断、提醒、应用限制、后台任务和不含正文的审计记录。Token 只显示“已配置”，不会回显。

## Security

- App 模式只接受固定命令集合和结构化参数。
- 高危操作双重确认。
- 包名严格校验。
- 定位只在明确请求时执行，不后台采集。
- 审计不记录 Token 和聊天正文。
- 不要提交 Operit Token、Relay Token、API Key、SSH 凭据或服务器配置。

## Development

```bash
python -m pytest tests -q
python -m py_compile main.py device_app.py
```

协议文档位于 Android 仓库的 `docs/APP_PLUGIN_CONTRACT_V1.md`。

