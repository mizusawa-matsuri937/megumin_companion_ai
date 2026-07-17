# ADR-W05：消息幂等、turn 状态和事件恢复

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任架构 reviewer）
- **风险：** P1-01、P1-02、P1-17
- **后续 PR：** W06、W14

## 背景

当前相同 `message_id` 可以创建新 turn，事件没有 seq/ack/replay，内存状态和 subscriber queue 缺少硬上限。断线重发可能造成重复计费、重复 TTS/VTS、旧音频恢复和跨 session 泄露。

## 决策

- `(client_id, session_id, message_id)` 是用户消息的幂等键。
- 重复的进行中消息返回原 turn snapshot；重复的终态消息返回原终态，不再次调用 provider、TTS、VTS 或 observer。
- 每 session 使用单调 `seq`；客户端恢复时提交 `last_seq`。
- replay 窗口为 2,000 events 或 10 分钟，先到者淘汰；超出窗口返回 reset marker + authoritative snapshot。
- 内存终态每 session 最多 200 个或 24 小时；长期历史由 SQLite 策略管理且不保存幂等正文副本。
- assistant delta 可以合并；终态、错误、feature 状态和 playback 边界不可丢。
- cancel 是幂等命令，并与新 turn 抢占线性化；取消旧 generation 不得影响新 turn。

## 备选与取舍

- **可行备选：** 不保留事件 replay，只提供 snapshot。实现简单但 UI 重连体验较差；可作为临时降级，但幂等仍不可移除。
- **拒绝：** 依赖 UI debounce；仅按 `message_id` 全局去重；无限 event log；跨 session `*` 订阅；断线后透明重放整个 turn。

## 失败与产品语义

- 幂等存储不可用时拒绝创建可能重复计费的 turn，不采用“先执行后记录”。
- replay gap 明确发送 reset，不伪造连续事件。
- 已播放音频或已执行 VTS 动作不可回滚；恢复只重建状态，不补播旧内容。
- UI 显示 accepted/running/completed/cancelled/failed 的权威状态。

## 迁移和隐私

引入版本化 command/event envelope 和有限 idempotency store；可用 SQLite 表或有界持久层实现。幂等记录只保留标识、状态、时间和必要 fingerprint，不额外保存消息正文。

## 验收

- 并发重复提交、断线重发和 cancel race 均只产生一个 provider/TTS/VTS 副作用。
- 10k turn、慢消费者和 replay 淘汰下内存有上界。
- 跨 client/session replay 和 snapshot 请求全部拒绝。

## 回滚

可以禁用 replay 并退到 snapshot-only，但不得禁用消息幂等。旧协议只读兼容期结束后明确拒绝，不隐式解释缺失身份字段。
