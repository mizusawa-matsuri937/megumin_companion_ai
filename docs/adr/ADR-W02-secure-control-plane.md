# ADR-W02：安全控制面与 IPC

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任安全/隐私 reviewer）
- **风险：** P0-01、P1-01、P1-02、P1-17、P2-01
- **后续 PR：** W04、W06、W13

## 背景

现有 loopback HTTP/WS 缺少认证、Origin 校验、session authorization 和传输上限。loopback 不是可信边界，恶意网页或本机进程可以尝试发送、重放、读取或淹没消息。

## 决策

- 生产 Qt 仅通过进程内 `ApplicationBridge` 与 BackendThread 通信，不启动 Uvicorn、不监听 TCP。
- FastAPI 只用于测试和显式 `--dev-api`；非 loopback 地址直接拒绝启动。
- dev API 每次启动生成短期随机 token；HTTP 与 WS 都必须认证。
- 严格校验 Origin allowlist、client/session authorization、chat/admin scope、body/frame 大小和速率。
- helper 使用继承匿名 pipe、length-prefixed typed JSON 和版本化 envelope；不使用命名全局对象、pickle、shell 或任意 Python object。
- command/event 共用业务契约，但 API、bridge 和 helper 不共享隐式身份或永久 token。

## 备选与取舍

- **可行备选：** 所有组件改用带用户限定 DACL 的 Named Pipe。适合未来独立 backend 进程，但首版进程内 bridge 更小、更易审计。
- **拒绝：** 无认证 loopback；用 CORS 代替 WS Origin/身份验证；固定 token；全局 `*` session 订阅；pickle IPC。

## 失败与安全语义

- token、Origin、scope、session 或协议版本错误一律 fail closed，并只返回稳定错误码。
- 达到 64 KiB frame/body 或速率上限时，在 JSON 解析前拒绝。
- bridge queue 满时向 UI 返回 busy，不阻塞 Qt；不可丢终态事件满时重建 snapshot。
- 日志只记录 reason code、scope、session 指纹、计数和延迟，不记录正文、token 或用户路径。

## 迁移与兼容性

保留测试和显式开发入口；生产入口改为 app factory/desktop host。未来远程控制、多用户或插件 IPC 是 Post-MVP，不能复用当前 dev token 直接扩展。

## 验收

- 恶意网页 Origin、无 token、错误 token、跨 session、重放、超大 frame 和 flood 全部拒绝。
- 生产 artifact 端口扫描确认零监听。
- token 每次运行变化且退出后失效；错误日志通过敏感字段扫描。

## 回滚

dev API 可以整体关闭；生产路径不得回退到无认证 loopback。若 bridge 不稳定，退回文字 CLI 测试入口，不放宽控制面。
