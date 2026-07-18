# W06 消息幂等、event replay 与有限状态契约

> 状态：实现与本地自动验证进行中；W06 Draft PR 在人工审计签字前保持未完成。
>
> 权威来源：`docs/windows_development_plan.md` 第 4.3、4.6、4.8 节、PR W06、统一 DoD，
> 以及 `docs/adr/ADR-W05-idempotency-replay.md`。

## 范围与非目标

W06 只负责消息幂等、turn 状态有限保留、每 session 的 event seq/replay/snapshot reset，
以及 backend 到单个 subscriber 的有界投递。W06 不修改 LLM→segment→TTS→audio→VTS
流水线内部 queue，也不实现 W07 的统一预算或生产者背压。

## 身份、协议与幂等键

- development API 冻结并扩展 protocol v1；缺少 W06 身份字段的旧命令形状明确拒绝，不做隐式解释。
- 认证主体同时绑定 `client_id` 与 `session_id`。WebSocket `CommandEnvelope` 包含
  `protocol_version`、`command_id`、`client_id`、`session_id`、`type`、`payload`；
  `received_at` 只由 backend 在解析成功后生成。
- 用户消息的唯一幂等键是 `(client_id, session_id, message_id)`。同一 `session_id` 只能绑定
  一个 `client_id`；跨 client 的 claim、resume、snapshot 和 cancel 均拒绝。
- claim 同时保存一个 SHA-256 语义 fingerprint，用于拒绝同键异文。fingerprint 覆盖规范化后的
  `text`、`user_id`、`input_mode`、中断策略、screen-context 许可和受限 metadata；不包含会在重发时
  重新生成的 `created_at`。数据库不保存第二份消息正文、完整 outcome、音频或任意路径。

## 原子 claim 与状态语义

SQLite `BEGIN IMMEDIATE` 事务按以下顺序执行：

1. 原子绑定/核对 session owner；
2. 清理该 session 已超过 24 小时或超出 200 个上限的终态记录；
3. `INSERT OR IGNORE` claim 幂等键；
4. 对冲突行读取原 fingerprint 与 `TurnState`，同文返回 existing，异文拒绝。

claim 成功是所有可计费或可播放副作用的前置条件。存储关闭、锁超时、I/O/迁移损坏或未知存储异常
统一映射为 `idempotency_unavailable`，请求以 503/稳定内部异常 fail closed；不得“先执行、后补记”。

幂等记录覆盖：

- `accepted`；
- running（现有 `streaming`/`speaking`）；
- terminal（`completed`/`cancelled`/`failed`）。

同键重复请求只返回原 `turn_id` 的最新权威状态，并生成面向客户端的 `turn.snapshot`；不再次调用
provider、TTS、audio playback、VTS event sink 或 turn observer。进程启动时残留的 accepted/running
记录改为 `failed(service_restarted)`，禁止自动重跑。终态持久化暂时失败时，当前进程仍保留已提交的
终态；后续创建请求继续经过存储并 fail closed，绝不通过重跑来“修复”。

终态 TTL 固定从终态 `updated_at` 计算，读取不会延长 24 小时时限；容量淘汰使用独立的最近访问顺序，
同文 duplicate 或按 turn 查询会刷新 LRU。这样 200 个容量限制不会误删刚被重连客户端确认的终态，
也不会把访问变成无限续期。

## 线性化顺序

显式命令共用一个短临界区，线性化点如下：

- duplicate：原子 claim 返回 existing；在任何 preemption 前直接返回原 snapshot；
- new turn：新 claim 成功后才取消并 join 旧 generation，旧终态 event 必须先于新
  `turn.accepted`；
- cancel：在命令临界区选定并校验 `(client_id, session_id, turn_id)`；首次 cancel 触发一次清理，
  重复 cancel join 同一清理或返回原终态；
- completion：terminal 内存提交和 active-owner 释放不可被 cancel 拆开；完成后 cancel 返回 completed；
- disconnect：只移除投递 subscriber，不取消 turn、不回滚已播放内容；所有 event 先进入 session
  replay log，再尝试投递；
- replay/resume：订阅建立时原子截取 replay 边界，再接收其后的 live event；因此事件不会落在两者
  之间；
- recovery：重发先命中同一 claim，旧音频/VTS 动作不补播，UI 仅以 replay 或 snapshot 重建状态。

## EventEnvelope、replay 与 reset

每个原始 session event 包含 `protocol_version=1`、`session_id`、严格递增的 `seq`、随机
`event_id`、可选 `turn_id`、`type`、`payload` 和 backend 生成的 `emitted_at`。replay 保留每 session
最新 2,000 条且不超过 10 分钟，任一条件先到即淘汰。

- `last_seq` 位于连续窗口内：按原 `seq/event_id` replay `last_seq` 之后的事件；replay 不再次通知
  provider、observer 或 event sink。
- `last_seq` 超出窗口、领先 server 或连续性无法证明：发送 `session.reset` marker 和该身份的
  authoritative `SessionSnapshot`。reset 不伪造缺失事件，也不补播旧音频。
- snapshot 只含该 session 当前 active turn、有限终态 `TurnState` 和 server `last_seq`；不得包含
  正文、完整 assistant outcome 或其他 session 数据。

## 有界 subscriber 与慢消费者

- 每个 subscriber 的 live queue 上限固定为 512（测试可下调）；publish 不等待慢客户端。
- 连续 `assistant.delta` 可在 subscriber queue 内合并；合并保留完整 delta 文本并携带覆盖的 seq
  边界。session replay log 始终保留未合并的原 event。
- terminal、error、feature state、playback boundary 和其他非 delta event 不可静默丢弃。
- live queue 无法容纳不可丢 event，或 delta 无法保持顺序地合并时，subscriber 标记
  `slow_consumer` 并断开。客户端使用最后确认的 `last_seq` 重连；窗口可用则 replay，否则 reset +
  snapshot。
- 初始 replay 直接引用已有的最多 2,000 条有界日志，不复制到 512 live queue；replay 期间新增事件
  仍受 512 上限约束。

## 保留、隐私与资源 owner

| 资源 | owner | 硬上限/淘汰 |
| --- | --- | --- |
| 原子幂等记录 | idempotency store | 每 session 200 个终态 LRU、固定 24 小时 TTL；active 不淘汰 |
| 内存 `TurnState`/安全 outcome 摘要 | `TurnService` | 每 session 200 个终态 LRU、固定 24 小时 TTL；不保留完整 outcome 正文 |
| replay log | `TurnService` | 每 session 2,000 events、10 分钟 |
| subscriber live queue | `EventSubscription` | 512；delta 合并，否则慢消费者断开 |

记录的 fingerprint 是必要的冲突检测数据，仍按私人状态保护；审计必须扫描 SQLite/WAL、日志、错误、
fixture 和 artifact，确认没有因 W06 新增正文副本。

schema v1→v2 启动迁移在写 schema 前执行 WAL checkpoint，并通过 SQLite backup API 在数据库同目录
生成 `*.pre-v1-to-v2.backup`。备份先做 `quick_check` 和源版本核对；损坏的既有备份会阻止迁移，有效
但陈旧的备份会从当前 v1 数据重新生成并原子替换，然后才运行事务化 v2 migration。该回滚副本按既有
私人状态规则保护，可能包含原本就允许持久化的 history/memory 正文；它不包含迁移后才写入的幂等
fingerprint 或额外的 W06 消息正文副本，人工隐私审计必须把该备份也纳入扫描和保留判断。

## 失败码与回滚

| 场景 | 对外语义 | 副作用 |
| --- | --- | --- |
| 幂等存储不可用 | `503 idempotency_unavailable` | 0 次 provider/TTS/VTS/observer |
| 同键异文 | `409 idempotency_conflict` | 不抢占，不产生新副作用 |
| 跨 client/session resume/snapshot/cancel | `403 identity_forbidden`/`turn_forbidden` | 不泄漏存在性或数据 |
| replay gap | `session.reset` + authoritative snapshot | 不补播旧音频/VTS |
| subscriber overflow | WebSocket `1013 slow_consumer` | turn 继续，event 可 replay/reset 恢复 |

回滚可关闭 replay 并退为 snapshot-only，但不得关闭原子消息幂等。schema v2 为加表迁移；旧代码不应在
已升级数据库上启动。协议 v1 不恢复写兼容。

## 验证矩阵（实现必须满足）

- 并发 duplicate、accepted/running/terminal duplicate、断线重发、同键异文；
- provider/TTS/playback/VTS 与 accepted/completed observer 调用计数各恰为一次；
- cancel race、重复 cancel、disconnect、replay 与 new-turn preemption 的 event 顺序；
- 10,000 turns 后状态、幂等记录和 replay 进入硬上限平台；
- 24 小时 TTL、200 LRU、2,000/10 分钟 replay 淘汰；
- delta 合并、不可丢 event overflow、慢消费者断开与 replay/reset 恢复；
- 跨 client/session 的 claim、resume、snapshot、cancel 拒绝；
- SQLite 并发 claim、迁移、启动恢复、存储故障 fail-closed 和数据库正文扫描；
- 聚焦 `pytest --no-cov`、全仓 branch coverage、Ruff、strict mypy、installed-wheel smoke，以及
  真实 Windows/macOS CI；Mock 结果不得标记为真实 Windows 证据。

installed-wheel smoke 必须在仓库外隔离环境中实际提交同一消息三次（进行中 duplicate 与终态
duplicate 均返回同一 turn），并验证同键异文为 409；只通过 `/health` 不足以证明 W06 代码和 v2
migration 已进入 wheel。

## 本地自动证据（未代替远端或人工 Gate）

- 测试先行红灯：最初 W06 聚焦测试因 `app.core.idempotency` 尚不存在而收集失败；迁移备份、真正
  LRU、静默 24 小时后次 cancel、duplicate产生确定性失败，再由对应实现转绿。
- 最终 W06/受影响聚焦集：`188 passed`，命 generation 的
   `turn.cancelled` 最终全仓：`679 passed, 2 skipped`；两个 skip 都是既有可选 RapidOCR/Pillow 依赖；aggregate branch
  coverage `90.16%`，满足 90% 门槛。
- `ruff check .` 通过；`ruff format --check .` 为 `169 files already formatted`；strict mypy 为
  `164 source files` 无问题。
- 本机 Windows loopback network smoke 通过认证/Origin/64 KiB 限制、protocol v1 resume 首帧、
  WebSocket 1009 与端口释放检查。
- 本地未提交 revision 构建的 wheel 含 105 个成员，raw SHA-256
  `4b1a2f96699ec0fc0239de72881231a88900c372fe50259495903befa9745370`，成员 manifest SHA-256
4. 任一 duplicate 触发第二次 provider/TTS/playback/VTS/observer、cancel 把 completed 改成
   cancelled、或旧 generation event 出现在新 accepted 之后，即拒绝。

### replay 隔离、正文与慢消费者恢复

1. 为 client A/session A 与 client A/session B 生成可区分的 synthetic event；再用 client B/session A、
   错误 envelope 身份、`*` 和跨 session turn ID 请求 resume/snapshot/cancel。只允许认证身份看到本
   session；拒绝响应不得泄漏另一 session 是否存在、turn ID 或 payload。
2. 记录连续 seq/event_id，断线后用窗口内 `last_seq` 验证原 ID replay；制造 2,001 events 与 10 分钟
   超时，必须得到 `session.reset` + 仅本 session 的 authoritative snapshot，不得伪造连续 seq 或补播。
3. 将 subscriber queue 降为 1/3：连续 delta 必须带覆盖 seq 范围且文本完整；不可丢事件溢出必须
   1013 断开。用最后确认 seq 重连，窗口内完整 replay，窗口外 reset/snapshot；turn 本身继续运行。
4. 用合成 sentinel 扫描 main DB、WAL/SHM、迁移 backup、日志、错误响应、pytest temp、wheel 与
   provenance。`idempotency_turns` 只允许 ID、fingerprint、状态和时间；不得出现新增 user message
   正文、完整 outcome、音频、路径或 credential。迁移 backup 中既有 history 正文是回滚副本的已知
   内容，必须验证其 ACL/保留与不上传策略，不能误报为 W06 idempotency 正文列。
5. 任一跨身份数据、无 gap reset、不可丢事件静默消失、正文进入幂等表/日志/artifact，或慢客户端
   阻塞 publisher，均拒绝。

### 通过标准、残余风险与回滚

只有 exact head 的全量/静态/installed-wheel 双 OS CI 全绿、上述手工步骤证据齐全、无拒绝条件，且
项目所有者明确回复“W06 审计合格”，才可记录 reviewer/signoff 并进入最终 head 核验与请求合并。

必须保留的残余风险：终态超过 200/24 小时会允许再次执行；fingerprint 对低熵正文存在离线猜测面；
event replay/seq 为进程内状态，重启依靠 reset 而非持久 replay；已播放音频/VTS 与远端 provider 计算
不可回滚；迁移 backup 保留一份既有私人 DB；W15 前只有进程内 migration-init lock，W07 前没有全
pipeline queue/backpressure/deadline。本 PR 不应声称消除这些后续风险。

回滚时可关闭 replay 并退为 snapshot-only，但原子幂等不得关闭。若必须回退 schema，先停止所有新旧
进程，验证 pre-v2 backup hash/`quick_check`，归档当前 v2 DB 后用备份恢复并运行旧版只读检查；禁止
在线 downgrade SQL。回滚后仍不得自动重放任何已付费/已播放 turn。
