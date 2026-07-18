# W06 消息幂等、event replay 与有限状态契约

> 状态：审计拒绝项已修复，正在对修复后的 exact head 重新验证；Draft PR 在项目所有者明确回复
> “W06 审计合格”前保持未完成、不得请求合并。
>
> 权威来源：`docs/windows_development_plan.md` 第 4.3、4.6、4.8、6.1～6.3、PR W06、统一 DoD，
> 以及 `docs/adr/ADR-W05-idempotency-replay.md`。

## 范围与非目标

W06 只负责原子消息幂等、turn 状态有限保留、每 session 的 event seq/replay/reset snapshot，以及
backend 到单个 subscriber 的有界投递。W06 不修改 LLM→segment→TTS→audio→VTS 流水线内部
queue，不实现 W07 的全流水线预算、生产者背压或 deadline。

## 身份、兼容与幂等键

- 当前 development command 使用 protocol v1 的完整 envelope：`command_id/client_id/session_id/type/payload`；
  `received_at` 只由 backend 生成。
- 按冻结迁移计划，受认证的 W04 development client 保留一个版本的兼容读：缺少 client header 和
  envelope 身份字段时，`client_id` 只能从已验证的进程 token/session 主体派生；服务端仍只写当前 v1
  event。伪造 client/session、重复字段、未知字段和无认证 legacy 形状全部拒绝。生产 GUI 不使用该 shim。
- 用户消息的唯一幂等键是 `(client_id, session_id, message_id)`。同一 session 只能绑定一个 client；
  跨 client 的 claim/resume/snapshot/cancel 拒绝。
- claim 保存 SHA-256 语义 fingerprint，用于拒绝同键异文。fingerprint 覆盖规范化消息语义，不包含
  重发时可变化的 `created_at`。幂等表不保存第二份正文、完整 outcome、音频或路径。

## 原子 claim、状态与恢复

SQLite `BEGIN IMMEDIATE` 事务按以下顺序执行：

1. 绑定或核对 session owner；
2. 按固定 24 小时 TTL 和 200 个终态 LRU 清理；
3. `INSERT OR IGNORE` claim；
4. 冲突行核对 fingerprint，同文返回 existing，异文拒绝。

claim 成功是 provider/TTS/playback/VTS/observer 的前置条件。存储关闭、锁超时、I/O、迁移或保留
状态损坏统一 fail closed 为 `idempotency_unavailable`，不得先执行后补记。幂等记录覆盖 accepted、
streaming/speaking 和 completed/cancelled/failed；所有重复请求返回原 turn 最新状态且不重做副作用。

终态 TTL 从终态 `updated_at` 计算，读取不延长 24 小时；容量淘汰使用独立 `terminal_order`。duplicate、
terminal cancel/query 会同时刷新 SQLite 与内存 LRU，随后再清理，避免两层淘汰集合分叉后重建 turn。

启动时 `recover_incomplete()` 把残留 accepted/running 改成 `failed(service_restarted)`。reset snapshot 和
无 `turn_id` cancel 会从幂等存储枚举本 session 的保留状态并 hydrate 内存；存储是保留集合和 LRU 顺序
的权威来源。终态暂时写失败但当前内存较新，hydrate 最多进行一次受控 repair/re-read；冲突或第二次
不一致 fail closed，不重跑 provider。

`TurnService` 没有 pipeline 的测试/降级构造仍只保留一个 active accepted turn；下一条消息按同一
preemption 规则先取消旧 turn。因此最多是 200 个终态加 1 个 active，不形成无界 accepted 集合；这不
是 W07 queue/backpressure。

## 线性化顺序

- duplicate：原子 claim 返回 existing；在 preemption 前直接返回 snapshot。
- new turn：新 claim 成功后取消并 join 旧 generation；旧 terminal event 先于新 `turn.accepted`。
- cancel：在 command 临界区 hydrate/选定/校验目标；首次 cancel 只触发一次清理，重复 cancel join 或
  返回原终态。terminal cancel 先触碰持久 LRU，再同步内存 LRU。
- completion：终态内存提交与 active owner 释放不可被 cancel 拆开；完成后 cancel 仍返回 completed。
- disconnect：只移除 subscriber，不取消 turn、不回滚已播放内容。
- subscribe：完成存储核对/hydrate 后原子截取 replay 边界并登记 live subscriber；事件不会落在 replay
  与 live 之间。
- recovery：只用 replay 或 snapshot 重建状态；旧音频/VTS 不补播。

## Event、replay 与分块 snapshot

每个原始 event 含 `protocol_version=1/session_id/seq/event_id/turn_id/type/payload/emitted_at`。每 session
seq 单调递增；replay 保留最近 2,000 条且不超过 10 分钟，任一条件先到即淘汰。

- `last_seq` 在连续窗口内时，按原 `seq/event_id` replay。
- gap、client 领先、进程重启后连续性无法证明时，先发 `session.reset`，其中 snapshot 是只含
  `last_seq/active_turn_ids/turn_count/chunk_count` 的 manifest。
- 随后发送 0～5 个 `session.snapshot.chunk`，每块最多 50 个 `TurnState`，以 `reset_id` 关联；块不是
  replay event，不伪造 seq。合法最大 201 个状态也不会超过 64 KiB WebSocket 帧限制。
- manifest/chunk 只含当前认证 session 的有界状态，不含正文、完整 assistant outcome 或其他 session。

## 有界 subscriber 与慢消费者

- live queue 固定上限 512（测试可下调）；publish 不等待网络客户端。
- 连续 `assistant.delta` 可合并并记录覆盖 seq；session replay 始终保存未合并原 event。
- terminal/error/feature/playback boundary 等不可丢事件无法入队时，subscription 原子标记
  `slow_consumer`。
- 独立关闭监视任务不依赖 sender 再次读取 queue；即使 sender 卡在 `send_text`，协调器也会取消它、
  终止 peer/expiry task 并用 1013 关闭连接。客户端以 last_seq 重连，窗口内 replay，窗口外 reset。
- 初始 replay/chunk 使用已经有界的只读 cursor，不占 512 live queue；期间的新 live event 仍受上限。

## 迁移、备份、隐私与保留

schema v1→v2 在写 schema 前 checkpoint，并用 SQLite backup API 原子生成
`*.pre-v1-to-v2.backup`。备份通过 `quick_check` 和源版本核对；无效备份阻止迁移，有效旧备份会从
当前 v1 数据重新生成。

`migration_audit` 对每次实际 schema 前进记录：

- `migration_id`、`from_version`、`to_version`；
- `started_at`、`completed_at`；
- 仅备份文件名 `backup_name` 和实际文件 SHA-256 `backup_sha256`，不记录绝对用户路径或内容。

fresh 0→2 没有迁移前私人 DB，backup 字段为空；1→2 必须同时记录文件名和 hash。audit 与 schema 写
在同一事务提交。pre-v2 backup 可能含原本允许持久化的 history/memory 正文，因此沿用私人状态目录
ACL，不上传、不进入诊断包。本 PR 不自动删除该回滚副本：它保留到项目所有者完成升级/回滚与隐私
确认后显式删除；删除策略和 UI 属于后续安装/数据生命周期范围。该无限期人工确认窗口是明确残余风险，
不能描述成 24 小时幂等 TTL。

## 失败语义

| 场景 | 对外语义 | 可计费/播放副作用 |
| --- | --- | --- |
| 幂等存储不可用或状态不一致 | `503 idempotency_unavailable` | 新请求 0 次 |
| 同键异文 | `409 idempotency_conflict` | 0 次且不抢占 |
| 跨 client/session | `identity_forbidden` / `turn_forbidden` | 0 次且不泄漏存在性 |
| replay gap | reset manifest + chunks | 不补播旧音频/VTS |
| subscriber overflow | WebSocket `1013 slow_consumer` | turn 继续，可恢复 |
| 进程遗留 running | `failed(service_restarted)` | 不自动重跑 |

回滚可以关闭 replay 并退到 snapshot-only，但不得关闭原子消息幂等。若回退 schema，必须先停止所有
进程、核对 audit hash 与 backup `quick_check`、归档当前 v2 DB，再恢复 backup；禁止在线 downgrade SQL。

## 自动验证矩阵

- 32/128 路并发 duplicate，accepted/running/terminal duplicate、断线重发、同键异文；
- provider/TTS/playback/VTS/accepted observer/completed observer 调用计数恰为一次；
- cancel race、重复 cancel、disconnect/replay/preemption 顺序；
- 10,000 turns、200/24 小时 TTL/LRU、2,000/10 分钟 replay 平台；
- 内存/SQLite LRU 同步、重启 hydration、无 pipeline 250 turn 有界；
- 200 个最大 ID 状态的实际 WebSocket reset 分块与 64 KiB 上界；
- delta 合并、不可丢 overflow、sender 阻塞时主动取消/1013、重连恢复；
- 跨 client/session 与 `*` 拒绝、存储 fault injection、正文 sentinel 扫描；
- v1→v2 backup quick-check/hash/audit、迁移失败原子回滚；
- W04 authenticated compatibility read 与伪造身份拒绝；
- 聚焦 `pytest --no-cov`、全仓 branch coverage、Ruff、strict mypy、installed-wheel smoke、远端
  Windows/macOS CI。Mock 证据不得冒充真实 Windows runner。

修复内容提交前的本地 Windows 自动证据（提交后仍需核对 exact head，且不代替远端 runner）：

- `uv run pytest`：698 passed、2 个既有可选 RapidOCR/Pillow 依赖 skip，aggregate branch coverage
  90.33%，满足 90% 门槛；
- `uv run ruff check .`、`uv run ruff format --check .`、strict `uv run mypy`、`git diff --check`
  全部通过；mypy 检查 164 个 source file；
- 本机真实 loopback HTTP/WebSocket smoke 通过 auth/origin/64 KiB/1009/resume/port release；
- 仓库外隔离安装 wheel smoke 通过，`idempotent_chat=true`、source-tree import=false、arbitrary CWD
  unchanged；wheel 105 members，raw SHA-256
  `a8f62d65b33cf9dfcabd4ad8b7843ac578f23226d77b78f00391d0f18ae47466`，member manifest SHA-256
  `83eb669c1b42be2bd70c44fbbdadb9da0983a80f3283d09642401d8f9b54ebce`；
- wheel/runtime 使用 Mock provider；这些证据不是远端 Windows/macOS CI、真实 TTS/VTS/设备或人工签字。

最终 exact head/diff 和 PR CI URL 只在修复提交、推送及远端 CI 完成后记录，避免把中间工作树证据冒充
最终 head。

## 修复后人工/独立复审清单

1. **exact head/diff：** 核对 Draft PR base 是已验证 baseline
   `7eb82f14fe1be4395b8e73373c580dde334f8863`，记录最终 head、merge-base、name-status 和 diff hash；拒绝
   W07 文件或未解释的额外范围。
2. **保留与原子性：** 检查 24 小时 TTL 不因读取延长、200 LRU 内存/SQLite 集合一致、claim 的
   `BEGIN IMMEDIATE + INSERT OR IGNORE` 在线程/进程竞争下只有一个 winner。
3. **失败重试/计费播放：** 在 claim、running update、terminal update、repair、重启各点故障注入；任何
   duplicate 造成第二次 provider/TTS/playback/VTS/observer 即拒绝。
4. **cancel/preemption：** 逐条核对旧 cancel terminal 先于新 accepted、completed 不被改 cancelled、重复
   cancel 只清理一次、旧 generation 不污染新 turn。
5. **隔离与正文：** client/session 交叉矩阵、gap snapshot chunk 关联、DB/WAL/SHM/backup/log/error/
   pytest temp/wheel/provenance synthetic sentinel 扫描；任何跨 session 数据或新增正文副本即拒绝。
6. **慢消费者：** queue=1/3，验证 delta 完整、不可丢 event 导致 1013、blocked sender 被取消、窗口内
   replay 与窗口外 reset chunks 均恢复。
7. **迁移：** 手工核对 audit 版本/时间/文件名/hash，backup quick-check、私人 ACL 与显式保留；hash 不符、
   迁移后才出现的正文副本、或未记录 backup 即拒绝。
8. **产物/平台：** 仓库外安装 wheel，进行中和终态 duplicate、冲突、迁移 smoke；GitHub exact head 的
   Windows/macOS job 全绿。只有真实 runner 可作为对应平台证据。

## 通过标准、残余风险与签字门

只有最终 exact head 的全量/静态/installed-wheel/双 OS CI 全绿、上述复审无拒绝项，且项目所有者明确
回复“W06 审计合格”，才可记录 signoff、再次验证最终 head 并请求合并。Codex 自审可以减少人工步骤，
但项目所有者兼任 reviewer 的非独立性仍需披露。

必须保留的残余风险：超过 200/24 小时后同 message 允许再次执行；fingerprint 对低熵正文存在离线猜测
面；event replay/seq 为进程内状态，重启依靠 reset 而非持久 replay；已播放音频/VTS 与远端计算不可
回滚；pre-v2 backup 保留一份既有私人 DB 直到显式确认；W15 前只有进程内 migration-init lock；W07
前没有全 pipeline queue/backpressure/deadline。W06 不声称消除这些后续风险。
