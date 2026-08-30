# AstrBot 手机 Agent

让 AstrBot 通过 Tailscale 调用手机上的 Operit。AstrBot 负责理解自然语言和决定任务，Operit 负责截图、OCR、点击、输入以及 Shizuku 特权操作。

默认控制后端是 Operit，不要求服务器持续连接 ADB，也不要求手机一直插着电脑。

## 一、整体结构

```text
聊天平台 -> AstrBot -> Tailscale -> 手机 Operit HTTP -> Operit Agent + Shizuku
```

健康数据是独立链路：小米运动健康数据由 `xiaomi-health-sync` 同步到服务器 SQLite，然后由 `phone_health` 查询。

## 二、前置条件

- 一台运行 AstrBot 的 Linux 服务器。
- 手机和服务器加入同一个 Tailscale tailnet。
- 手机安装 Operit 和 Shizuku。
- 在 Operit 中授予需要的 Shizuku 权限。
- AstrBot 4.22 或更高版本。
- 如果需要健康数据，再准备小米运动健康账号和 `xiaomi-health-sync`。

手机可以使用 Wi-Fi 或移动数据。Operit HTTP 通过 Tailscale 工作，不依赖 ADB 无线调试。Shizuku 在手机重启后可能需要重新启动一次。

## 三、配置 Tailscale

1. 在手机和服务器安装 Tailscale。
2. 登录同一个 tailnet。
3. 在服务器执行：

   ```bash
   tailscale ping PHONE_TAILSCALE_IP
   ```

4. 确认能看到 `pong`，并记下手机的 Tailscale IPv4 地址。不要把真实地址写进公开仓库。

## 四、配置 Operit 和 Shizuku

1. 打开 Shizuku，完成启动并确认状态为运行中。
2. 打开 Operit，给需要的工具或工作流授权 Shizuku。
3. 进入 Operit：

   ```text
   设置 -> 数据和权限 -> 外部 HTTP 调用
   ```

4. 打开外部 HTTP 服务，默认端口为 `8094`。
5. 记录页面显示的 Bearer Token。Token 只填入 AstrBot 配置，不要发到聊天或提交到 GitHub。
6. 服务地址通常是：

   ```text
   http://PHONE_TAILSCALE_IP:8094
   ```

接口发现地址为：

```text
http://PHONE_TAILSCALE_IP:8094/.well-known/agent-card.json
```

真正的 `/api/health` 和 `/api/external-chat` 请求需要 Bearer Token。

## 五、安装 AstrBot 插件

### 从 GitHub 下载

打开 <https://github.com/Tauru-t6/astrbot-phone-agent>，下载 ZIP，解压后把目录复制到：

```text
<astrbot-data>/plugins/astrbot_plugin_phone_agent
```

目录中应该直接看到 `main.py`、`metadata.yaml` 和 `_conf_schema.json`，不要多套一层目录。

### 服务器使用 Git

```bash
cd <astrbot-data>/plugins
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

然后重启 AstrBot。插件本身不需要额外安装 Python 依赖。

## 六、打开 WebUI

安装并重启后，在 AstrBot Dashboard 的插件页面中打开“手机 Agent 控制台”。页面提供：

- Operit 在线状态和一键连接测试。
- Operit 地址、Token、控制后端和用户白名单配置。
- App 别名 JSON 编辑。
- 睡眠守护时间、目标 App 和例外 App 配置。
- 临时睡眠模式启动/解除。
- 健康摘要、后台任务、提醒和最近审计记录。

页面使用 AstrBot Dashboard 自带的登录鉴权。Token 只会显示“已配置”，不会回显。

## 七、配置插件

在 AstrBot WebUI 的插件配置中填写：

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

如果你明确要复用私人陪伴授权，可以改为：

```json
{
  "allowed_user_ids": "",
  "use_private_companion_auth": true
}
```

这是可选扩展。插件不会修改私人陪伴的性格、记忆、主动消息或提示词。详见 [`extensions/private_companion_auth.md`](extensions/private_companion_auth.md)。

## 八、功能配置

### App 别名

默认支持“B 站”“哔哩哔哩”“快手”“优酷”“微信”等名称。通过 `app_aliases_json` 添加别名：

```json
{
  "抖音": "com.ss.android.ugc.aweme",
  "抖音极速版": "com.ss.android.ugc.aweme.lite"
}
```

### 睡眠守护

```json
{
  "sleep_guard_enabled": true,
  "sleep_guard_start": "00:30",
  "sleep_guard_end": "07:00",
  "sleep_guard_packages": "哔哩哔哩,快手,优酷",
  "sleep_guard_exempt_apps": "微信",
  "sleep_guard_poll_seconds": 30
}
```

安静时段内，Operit 检查前台 App，并通过 Shizuku 暂停目标视频 App；时段结束后恢复。也可以直接说：

```text
别让我刷视频两小时
解除视频限制
```

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
3. 将数据库目录配置到 `health_db_path`。
4. 用 systemd timer 或其他计划任务定期同步。

插件只读查询 SQLite，不会把小米 Token 上传到 GitHub 或聊天平台。成功后可以问：

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

确认 Operit HTTP 服务正在运行、Token 正确、Shizuku 为运行状态，先测试：

```text
观察一下当前手机，只观察，不要点击
```

## 安全说明

- 只在 Tailscale 或可信内网开放 Operit HTTP 服务。
- 不要提交 Operit Token、小米 Token、SSH 密码或服务器配置。
- 使用 `allowed_user_ids` 限制手机控制权限。
- 本插件不接受任意 shell 命令。
- Android 可能显示“由 Shell 管理”，这是系统对 `pm suspend` 来源的标记，插件不能修改。

## 使用过的项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [Operit](https://github.com/AAswordman/Operit)
- [Shizuku](https://github.com/RikkaApps/Shizuku)
- [Tailscale](https://github.com/tailscale/tailscale)
- [xiaomi-health-sync](https://github.com/ridd1ot/xiaomi-health-sync)
- [mi_fitness_data_bridge](https://github.com/shkyyy18/mi_fitness_data_bridge)
- [mi_fitness](https://pypi.org/project/mi-fitness/)
