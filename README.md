# Phone Control for AstrBot

让 AstrBot 通过 [Operit](https://github.com/Mavaebrook/Operit)（安卓 AI 助手）控制你的 Android 手机：在聊天里说"打开哔哩哔哩"、"手机电量多少"、"我附近有什么好吃的"，AstrBot 会调用 Operit 在手机上完成任务。支持截图/OCR/点击/输入、Shizuku 特权操作、按需读取位置、App 禁用策略，以及可选的小米健康数据查询。

> 本插件是"桥"：AstrBot 负责理解与决策，手机上的 Operit 负责实际执行。所有手机操作都有白名单与审计日志。

## 功能一览

| 功能 | 工具 | 默认状态 |
|---|---|---|
| 自然语言手机任务（开关应用、点按、输入等） | `operit_task` 系列、`phone_action` | 默认开启 |
| 观察手机状态（前台应用、屏幕文字、电量） | `phone_observe` | 默认开启 |
| App 禁用/恢复策略、临时限制视频 App | `phone_app_policy`、`phone_sleep_mode` | 按需开启 |
| 一次性读取手机位置（可联动百度地图 MCP 逆地理编码） | `phone_location` | 按需开启 |
| 应用使用时长统计 | `phone_usage` | 按需开启 |
| 聊天内提醒 | `phone_reminder` | 按需开启 |
| 操作审计查询 | `phone_audit` | 按需开启 |
| 小米健康数据（步数/睡眠/心率/血氧） | `phone_health` | 按需开启 |

可选功能通过插件配置里的 `enable_*` 开关控制，不配置就不注册对应工具，安装即用零噪音。

## 工作原理

```text
聊天平台 → AstrBot → Tailscale → 手机 Operit HTTP → Operit Agent + Shizuku
                └→（Tailscale 断线时）→ 公网 relay 队列 → 手机 Operit 定时轮询领取 → 回传结果
```

- **主链路**：服务器经 Tailscale 直连手机的 Operit HTTP 服务，实时执行任务。
- **兜底链路（relay）**：手机开不了 VPN 时（公司 Wi-Fi、网络限制等），插件自动把任务放进你自己服务器上的公网队列，手机上的 Operit 定时工作流轮询领取并回传结果，延迟取决于轮询间隔（WorkManager 最小 15 分钟）。
- 两条链路自动切换，主链路失败才走兜底，切换行为会记录在审计日志（`relay_fallback`）。

## 安装

### 前置条件

1. **手机端**：安装 [Operit](https://github.com/Mavaebrook/Operit) 和 Shizuku，在 Operit 中授予 Shizuku 权限，配置一个可用的聊天模型，并开启 Operit 的"外部 HTTP 调用"（设置 → 数据和权限，记下端口和 Bearer Token）。
2. **服务器端**：AstrBot 4.22+。
3. **网络**：服务器与手机加入同一 Tailscale tailnet（推荐，实时控制）；或按下方"Relay 兜底"自建公网队列。

### 从插件市场安装（推荐）

在 AstrBot WebUI 的插件市场搜索 **Phone Control** 一键安装。

### 从 GitHub 安装

```bash
cd <astrbot-data>/plugins/
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

然后在 WebUI 重载插件。注意目录名必须叫 `astrbot_plugin_phone_agent`。

### 最小配置

在 AstrBot WebUI → 插件配置里填：

- `operit_base_url`：手机 Operit 地址（Tailscale IP + 端口，如 `http://100.x.y.z:8094`）
- `operit_token`：Operit 外部 HTTP 的 Bearer Token
- `allowed_user_ids`：允许使用手机功能的用户 ID（**留空则所有功能拒绝调用，务必填写**）

完成后在聊天里发送"打开设置"即可验证链路。

## Relay 兜底（可选）

Tailscale 断开时想让手机继续接任务，需要两步：

### 1. 服务器端：启动 relay 队列

```bash
sudo mkdir -p /opt/phone-agent-relay
sudo cp deploy/relay/relay_server.py /opt/phone-agent-relay/
# 生成 token 并写入 systemd 服务
sudo env RELAY_TOKEN=$(openssl rand -hex 16)   envsubst '$RELAY_TOKEN' < deploy/relay/phone-agent-relay.service   | sudo tee /etc/systemd/system/phone-agent-relay.service
sudo systemctl daemon-reload && sudo systemctl enable --now phone-agent-relay
```

再把 relay 暴露到公网：用你的反代（nginx/Caddy）或 CDN 加速域名指向 `127.0.0.1:8791`，必须启用 HTTPS。

### 2. 手机端：Operit 定时工作流

在 Operit 工作流编辑器创建：

1. 触发器：`schedule` 类型，间隔 15 分钟（WorkManager 最小周期）。
2. 执行节点 `http_request`：`GET https://<你的域名>/poll`，请求头 `Authorization: Bearer <relay token>`。
3. 条件节点：响应 JSON 中 `task` 不为 null 才继续。
4. 执行节点 `send_message_to_ai`：内容引用 `task.message`，让 Operit 执行。
5. 执行节点 `http_request`：`POST https://<你的域名>/result`，body 传 `{"task_id": "<task.id>", "success": true, "ai_response": "<AI 回复>"}`。

### 3. 插件配置

- `relay_base_url`：`https://<你的域名>`
- `relay_token`：与 relay 服务相同的 token

配置后 AstrBot 会自动在 Tailscale 失联时降级到 relay；恢复后自动切回直连。

## 可选功能开启

健康数据、位置、提醒、App 策略、审计等默认关闭，在插件配置里打开对应 `enable_*` 开关即可：

- `enable_health_tools`：需要另部署 `xiaomi-health-sync`（小米运动健康数据同步工具，需自行部署）（小米运动健康 → SQLite），把生成的 `health.db` 路径填到 `health_db_path`。
- `enable_location_tool`：返回 GCJ02 坐标和转换后的 BD09 坐标，后者可直接喂给百度地图 MCP 的 `map_reverse_geocode` 等工具做"我在哪/附近有什么"。
- `enable_policy_tools`：禁用/恢复 App（通过 Shizuku `pm suspend`），带结果验证、到期自动恢复、系统包保护。

## 安全说明

- 所有手机操作白名单化，包名严格校验，系统包与 Operit 自身受保护。
- 每次操作写审计日志（不含消息正文与 Token），`phone_audit` 可查。
- 位置、外发消息类高危操作需要用户显式确认才会执行。
- 插件配置请勿泄露：`operit_token`、`relay_token` 等同手机控制权。
- Relay 队列只有持有 token 的一方读写；请用 HTTPS 暴露。

## 文档

- [中文完整教程](README.zh-CN.md)（Tailscale、Shizuku、xiaomi-sync 全流程）
- [English guide](README.en.md)
- [更新日志](CHANGELOG.md)

## 致谢

本项目由 GPT-5.6 与 GLM-5.3 协作编写，作者本人没有代码基础——如果你发现写得不合理的地方，欢迎 issue 指正。
