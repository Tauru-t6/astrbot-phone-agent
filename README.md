# Phone Control · AstrBot 手机控制插件

让 AstrBot 通过 [Operit](https://github.com/Mavaebrook/Operit)（安卓 AI 助手）控制你的 Android 手机。在聊天里说"打开哔哩哔哩"、"手机还剩多少电"、"我附近有什么好吃的"，AstrBot 就会调用 Operit 在手机上完成任务。

> 本插件是一座"桥"：AstrBot 负责理解自然语言和决策，手机上的 Operit 负责实际执行。所有手机操作都有白名单和审计日志，高危操作需要你显式确认。

## 功能一览

| 功能 | 工具 | 默认状态 |
|---|---|---|
| 自然语言手机任务（开关应用、点按、输入等） | `operit_task` 系列、`phone_action` | ✅ 默认开启 |
| 观察手机状态（前台应用、屏幕文字、电量） | `phone_observe` | 关闭，需显式开启 `enable_observe_tool` |
| Private Companion 手机上下文桥接 | `companion-context` | 关闭，需显式开启 |
| App 禁用/恢复、临时限制视频 App | `phone_app_policy`、`phone_sleep_mode` | 🔧 按需开启 |
| 一次性读取手机位置（可联动百度地图 MCP） | `phone_location` | 🔧 按需开启 |
| 应用使用时长统计 | `phone_usage` | 🔧 按需开启 |
| 聊天内提醒 | `phone_reminder` | 🔧 按需开启 |
| 操作审计查询 | `phone_audit` | 🔧 按需开启 |
| 小米健康数据（步数/睡眠/心率/血氧） | `phone_health` | 🔧 按需开启 |

可选功能通过插件配置里的 `enable_*` 开关控制，不开就不注册对应工具——安装即用、零噪音。

## 工作原理

```text
聊天平台 → AstrBot → Tailscale → 手机 Operit HTTP → Operit Agent + Shizuku
                └→（Tailscale 断线时自动降级）→ 公网 relay 队列 → 手机定时轮询领取 → 回传结果
```

- **主链路（实时）**：服务器经 Tailscale 直连手机上的 Operit HTTP 服务，随叫随到。
- **兜底链路（relay）**：手机开不了 VPN 时（公司网络、运营商限制等），插件自动把任务放进你自己服务器上的公网队列；手机上的 Operit 定时工作流轮询领取、执行并回传结果。轮询间隔受 Android WorkManager 限制，最小 15 分钟。
- 两条链路自动切换：主链路失败才走兜底，恢复后自动切回。切换行为会记录在审计日志（`relay_fallback`）里。

## 安装

### 前置条件

1. **手机端**：安装 [Operit](https://github.com/Mavaebrook/Operit) 和 Shizuku，在 Operit 里授予 Shizuku 权限、配置一个可用的聊天模型，并开启"外部 HTTP 调用"（设置 → 数据和权限，记下监听地址和 Bearer Token）。
2. **服务器端**：AstrBot 4.22 或更高版本。
3. **网络**（二选一或都用）：
   - 服务器与手机加入同一 Tailscale tailnet（推荐，实时控制）；
   - 或按本文"Relay 兜底"一节自建公网队列（不需要 VPN）。

### 从插件市场安装（推荐）

在 AstrBot WebUI 的插件市场搜索 **Phone Control**，一键安装。

### 从 GitHub 安装

```bash
cd <astrbot-data>/plugins/
git clone https://github.com/Tauru-t6/astrbot-phone-agent.git astrbot_plugin_phone_agent
```

然后在 WebUI 里重载插件。注意：目录名必须是 `astrbot_plugin_phone_agent`。

### 最小配置

在 AstrBot WebUI → 插件配置中填写：

| 配置项 | 说明 |
|---|---|
| `operit_base_url` | 手机 Operit 地址（Tailscale IP + 端口，如 `http://100.x.y.z:8094`） |
| `operit_token` | Operit 外部 HTTP 的 Bearer Token |
| `allowed_user_ids` | 允许使用手机功能的用户 ID，逗号分隔。**留空则所有功能拒绝调用，务必填写** |

配置完成后在聊天里发一句"打开设置"，能收到手机执行结果就是通了。

## Relay 兜底（可选）

Tailscale 断开时也想让手机继续接任务？三步配置：

### 第一步：服务器上启动 relay 队列

relay 服务在本仓库 `deploy/relay/` 目录（纯 Python 标准库，无依赖）：

```bash
sudo mkdir -p /opt/phone-agent-relay
sudo cp deploy/relay/relay_server.py /opt/phone-agent-relay/

# 安装 systemd 服务（把模板里的 __RELAY_TOKEN__ 替换成随机 token）
sudo sed "s/__RELAY_TOKEN__/$(openssl rand -hex 16)/" deploy/relay/phone-agent-relay.service \
  | sudo tee /etc/systemd/system/phone-agent-relay.service
sudo systemctl daemon-reload && sudo systemctl enable --now phone-agent-relay
```

然后用 nginx/Caddy 反代或 CDN 加速域名把 `127.0.0.1:8791` 暴露到公网，**必须 HTTPS**。

### 第二步：手机上配置 Operit 定时工作流

在 Operit 工作流编辑器里创建：

1. **触发器**：`schedule` 类型，间隔 15 分钟（WorkManager 最小周期）。
2. **执行节点**（`http_request` 工具）：`GET https://<你的域名>/poll`，请求头 `Authorization: Bearer <relay token>`。
3. **条件节点**：响应 JSON 里 `task` 不为 null 才继续，否则结束。
4. **执行节点**（`send_message_to_ai` 工具）：内容引用 `task.message`，让 Operit 在手机上执行任务。
5. **执行节点**（`http_request` 工具）：`POST https://<你的域名>/result`，body 为 `{"task_id": "<task.id>", "success": true, "ai_response": "<AI 回复>"}`。

### 第三步：插件配置

| 配置项 | 值 |
|---|---|
| `relay_base_url` | `https://<你的域名>` |
| `relay_token` | 与 relay 服务相同的 token |

之后 AstrBot 会在 Tailscale 失联时自动降级到 relay，恢复后自动切回直连。

## 可选功能说明

- **健康数据**（`enable_health_tools`）：需另行部署 `xiaomi-health-sync`（小米运动健康 → SQLite 同步工具），把生成的 `health.db` 路径填到 `health_db_path`。插件只读查询，不上传任何小米凭据。
- **位置**（`enable_location_tool`）：返回 GCJ02 原始坐标和转换后的 BD09 坐标。BD09 可直接传给百度地图 MCP 的 `map_reverse_geocode`、`map_search_places` 等工具，实现"我在哪 / 附近有什么 / 怎么去"。高精度定位和地址反查需要你在聊天里显式确认。
- **App 策略**（`enable_policy_tools`）：通过 Shizuku 执行 `pm suspend`，禁用后自动核验实际状态，支持定时自动恢复；系统包和 Operit 自身受保护，不会被误禁。

## 安全说明

- 所有手机操作白名单化，包名严格校验，坐标限幅。
- 每次操作写入审计日志（不含消息正文与 Token），可用 `phone_audit` 查询。
- 位置、外发消息等高危操作需要用户显式确认。
- `operit_token`、`relay_token` 等同于手机控制权，请勿泄露；relay 暴露公网必须启用 HTTPS。

## 文档

- [完整中文教程](README.zh-CN.md)（Tailscale、Shizuku、健康数据全流程）
- [English Guide](README.en.md)
- [更新日志](CHANGELOG.md)

## 致谢

本项目由 GPT-5.6 与 GLM-5.3 协作完成，作者本人没有代码基础——如果你发现写得不对的地方，欢迎提 issue 指正。
