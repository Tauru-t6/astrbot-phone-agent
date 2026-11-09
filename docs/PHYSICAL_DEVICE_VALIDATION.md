# 真机链路验收

2026-11-09，Honor RMO-AN00 / Android 13，App 0.3.26、Phone Agent 0.9.0、Phone Companion Context 0.3.0、Private Companion 7.1.9。用户手动启动 Tailscale 并激活 Shizuku 后验证。

| 检查 | 结果 |
| --- | --- |
| 设备注册与心跳 | 服务端收到真实设备状态，shizuku_ready=true |
| 直连 ping | 经服务器发起，成功，backend=direct |
| LLM 多步试运行 | source=llm，按顺序生成 observe、open_app、home；写步骤未授权，全部跳过 |
| 批准前检查 | 提案为 waiting_approval，未执行任何步骤 |
| 实际执行 | 批准限定计划后，打开已安装的哔哩哔哩，再返回桌面 |
| 逐步核验 | 打开应用后的前台包名匹配，home 后切换到桌面包名；使用动作发生后的新观察 |
| 结果 | done，2 个写步骤核验通过，0 失败、0 未核验，总执行约 3.75 秒 |
| Relay 队列 ping | 在服务器直接测试实际 Relay 队列，手机领取并返回成功，约 4.24 秒 |
| 收尾 | 重新打开 Phone Agent，未发送聊天消息，未输入文字或更改系统设置 |

上述耗时仅代表本次设备和网络条件，不作为性能保证。Relay 测试使用独立的只读 ping，不修改正在运行的直连配置。

Shizuku 未激活时，服务端曾收到 shizuku_ready=false；Tailscale 未启动时，App 无法连接服务端。恢复这两项后，同一组地址与凭据完成上述验收，未更换认证凭据或关闭授权机制。

关怀桥接的 installed=true / mode=v2 已从运行中接口确认。发送行为由原有静默时段、冷却、每日额度和新鲜状态约束；真实关怀预览已完成模型评估，返回 preview=true、sent=false、delivered=false。同时修复了超过 2 MB 的实际会话历史导致上下文被整体拒绝的问题。去重、权限变化与会话隔离行为由自动化测试覆盖。
