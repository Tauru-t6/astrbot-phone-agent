# Health Connect 健康数据接入

配套：Phone Agent 0.9.3、Android App 0.3.28、关怀桥接 0.3.1。

## 使用

1. 手机上安装 Health Connect，并让你选择的健康 App 写入记录。“最近访问”只证明发生访问，不代表每类已有记录。
2. App → 我的 → Health Connect → 输入该 App 的 Android 包名 → 授权健康读取。在系统页面选择类别，支持部分授权。
3. 开启“同步健康摘要到服务器”，再点“立即同步健康摘要”。默认为关闭。
4. 服务端开启 enable_health_tools，health_data_source 选择 auto、health_connect 或 health_db。

每次只读取所选来源最近 7 天的步数、睡眠、心率、血氧、活动热量。不写入或删除 Health Connect，不读取运动路线或健康 App 凭据。按日汇总才上传；单条心率、血氧不上传，也不写进普通审计日志。摘要可供用户已开启的 AstrBot 健康查询/关怀使用，因此可能进入用户配置的模型服务。能读到哪些类别取决于所选 App 实际写入。当前只有 `com.mi.health` 完成过实机验证。

App 打开时及设备连接服务运行期间约每 15 分钟尝试同步。系统可能拒绝后台读取，界面会显示错误，需要回到 App 手动同步。没有申请健康后台读取或超过 30 天历史权限，不承诺所有系统的后台保活。

步数、热量、睡眠和心率使用 Health Connect 聚合；血氧分页后按来源/本地日期归类去重。睡眠按手机本地日界线统计，不保证与小米按主睡眠归属日期的数字完全一致。缺失值不补零，不推算血压、压力、体重等未覆盖字段。

## 服务端来源

- auto：有当前绑定手机的新鲜 Health Connect 摘要时选它，否则使用原 SQLite。
- health_connect：只返回手机快照，包括过期、无数据或撤权状态，不混入云数据。
- health_db：保持原 xiaomi-sync SQLite 来源，上传快照不作为查询结果。

来源从不相加。响应带实际 source（health_connect 或 health_db）、记录日期和 sync；Health Connect 另带 zone_id、granted_types。fresh 指快照采集距现在不超过 3 小时，不等于每条测量都是刚发生。问“今天”仍需核对日期。关怀桥接通过原 _read_health_db_sync 入口遵循同一选择；原 SQLite 定时同步不停止。

## API

沿用宿主 /api/plug/astrbot_plugin_phone_agent 的控制台 JWT 鉴权。Origin 是浏览器补充保护，不能代替 JWT；原生 App 使用现有插件 JWT。

POST /app/health/snapshot（需要健康功能开启）：

```json
{"schema_version":1,"source":"health_connect","device_id":"registered-device-id","snapshot_id":"unique-request-id","captured_at":1794312000.0,"zone_id":"Asia/Shanghai","origin_packages":["com.mi.health"],"status":"ok","granted_types":["steps"],"days":[{"date":"2026-11-10","metrics":{"steps":120}}]}
```

`origin_packages` 必须恰有一个合法 Android 包名。限制为当前注册手机、最多 7 天和白名单数值字段，拒绝未知指标、越界、非有限数字及未授权类别。相同 snapshot_id 重试幂等，不刷新采集时间；同 ID 不同内容冲突；旧快照不能覆盖新快照。换绑后旧手机快照不能作为新手机数据读取。

status 支持 ok、permission_denied、unavailable、read_failed、disabled。非 ok 时 days 必须为空，用于失效旧值；没有数据不等于零。服务器接受 read_failed 等状态报告不代表读到了健康记录，应同时核对状态、数据来源及实际日期。

POST /app/health/clear 请求体为 {device_id}。清除不要求健康功能仍开启，但不能删除其他手机的数据。快照只保留最新一份，在插件数据目录 phone_agent_health_connect.json 原子保存，不写入 xiaomi-sync 数据库。

关闭同步会停止读取并清除服务器快照，离线清除持久重试。系统撤权在下次读取时上传失效状态；离线无法立即删除服务器副本。快照过期会明确标记。

HTTP 状态、错误码和成功响应字段见 [App API 合同](APP_API_V2.md#health-connect-summaries)。上传失败不会刷新 App 的最近上传时间；排障时应区分系统读取失败、手机尚未注册、JWT 失效、服务端功能关闭和快照校验失败。

## 构建和验证

使用稳定 Health Connect SDK 1.1.0，其 AAR 要求 compileSdk 36。本次仅提高编译 SDK，targetSdk 仍为 34，现有 AGP 8.13.0 可用。隐私用途页面兼容 Android 13 和 Android 14 的 Health Connect 入口。

服务端测试覆盖设备隔离、授权类型、数值范围、幂等、旧上传、磁盘故障、撤权、清除、来源回退与日志隐私。App 测试覆盖血氧去重、缺失值和本地日期边界；完整构建、JUnit 和 lint 分别执行。
