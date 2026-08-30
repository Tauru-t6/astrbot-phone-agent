# AstrBot 手机 Agent

让 AstrBot 通过 Tailscale 调用手机上的 Operit。Operit 负责截图、OCR、点击、输入和 Shizuku 操作，AstrBot 负责理解自然语言和下发任务。

## 功能

- `operit_task`：把自然语言任务交给手机 Operit，例如打开 App、找联系人、点击按钮、输入文字。
- `phone_action`：调用白名单手机动作，包括打开/关闭 App、返回、主页、锁屏、暂停/恢复 App 等。
- `phone_observe`：观察前台 App、屏幕文字、电量和任务状态。
- `phone_sleep_mode`：临时限制视频 App，例如“别让我刷视频两小时”。
- 夜间守护：在指定时间检查视频 App，并通过 Shizuku 暂停；结束后恢复。
- `phone_health`：查询小米手环同步的步数、睡眠、心率、血氧等数据。
- `phone_usage`：通过 Operit 查询应用使用时长。
- `operit_task_status` / `operit_task_cancel` / `operit_task_retry`：管理后台 Operit 任务。
- `phone_reminder`：在当前会话创建、查看和取消提醒，提醒数据会持久化。
- `phone_audit`：查看不含 Token 和消息正文的动作审计记录。
- App 别名：可以用“B 站”“微信”等名称，不必记包名；可用 `app_aliases_json` 扩展。
- 高风险任务确认：发消息、评论、点赞、删除、支付等任务必须显式确认。
- ADB 仅作可选诊断后端，默认控制后端是 Operit，不要求无线 ADB。

## 安装

将插件目录放入 AstrBot 数据目录：

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

在手机 Operit 中打开：设置 → 数据和权限 → 外部 HTTP 调用。记录地址和 Bearer Token，在插件配置中填写：

```json
{
  "control_backend": "operit",
  "operit_base_url": "http://PHONE_TAILSCALE_IP:8094",
  "operit_token": "YOUR_OPERIT_TOKEN"
}
```

建议只通过 Tailscale 暴露 Operit HTTP 服务，不要直接开放到公网。

后台任务可传 `background=true` 获取任务 ID，再用状态、取消或重试工具管理。提醒示例：“30 分钟后提醒我喝水”。

## 权限

默认使用本插件自己的 `allowed_user_ids` 白名单。私人陪伴插件不是必需依赖，也不会默认加载。需要复用私人陪伴授权时，参见 [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md)。

## 使用过的项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [Operit](https://github.com/AAswordman/Operit)
- [Shizuku](https://github.com/RikkaApps/Shizuku)
- [Tailscale](https://github.com/tailscale/tailscale)
- [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)
- [mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge)
- [mi_fitness](https://pypi.org/project/mi-fitness/)

本仓库不包含 Token、密码、服务器配置或私人陪伴插件代码。
