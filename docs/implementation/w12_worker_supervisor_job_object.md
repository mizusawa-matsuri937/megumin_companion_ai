# W12：Windows WorkerSupervisor、Job Object 与 helper protocol 基座

> 基线：`5d2721f3621ce8b91ed79d2413688fdba29957ec`（W07/W08/W09/W10 与
> W06/W10 hotfix 均已受控合并后的
> `agent/windows-development-baseline`）
> 分支：`codex/w12-worker-supervisor-job-object`
> 风险：P1-13、P1-14、P1-15
> Gate：W12 AI 实现审计完成后仍须并发/安全 reviewer 签字；综合 Gate W2 保持未关闭

2026-07-19 最终综合基线复核：W12 以双亲 merge commit
`5ed6fa4228c26e8effc74640954f13e68022dbd2` 纳入上述精确 baseline；父提交为旧 W12 head
`f64aa742b85969ec5d4b5bb29a2c39bc2283dfbb` 与 baseline `5d2721f...`。合并无冲突，未重写既有 W12
提交，且 baseline 是 merge commit 的祖先。`0034e736...→5d2721f...` 的 9 个上游提交与旧 W12 净补丁
没有文件重叠或 worker 引用；`app/workers`、既有 W12 tests/helper 在合并时逐字节不变。此后只增强无路径
原生证据探针并新增合成 Gate W2 资源探针，不改变 worker 产品实现。

增量语义审查确认：W06 hotfix 以 SQLite authoritative terminal refresh 修复共享 claim 竞态；W07 的
TTS/audio 队列、lease 与清理保持有界；W08 的 provider deadline/cancel/TLS 仍由 provider owner 收口；
W10 cleanup 使用注入 clock；W11 health/logging 与 W12 actual-state provider 的接口未被覆盖。W12 仍未接入
main/bootstrap，也未实现或接线 W13/W17/W18/W21。该结论只审查组合边界，不吸收各上游 PR 的责任范围。

2026-07-19 对 exact head `0908594f1d2365d7d55665f2c70dc4831bf87f21` 的独立 AI 审计推翻了该
head 关于取消 settlement、绝对 deadline、pipe 写入上界、lifecycle 串行化、scavenger hard bound 和 Path
authority 的部分结论。本节之后的契约已按测试先行修复更新；旧 head 的 8/8 CI 仅保留为历史记录，不适用于
新 head，也不得作为 Gate W2 放行证据。

## 范围与边界

本 PR 只提供通用 worker 基座：versioned frame parser、批准资源策略、helper runtime、平台进程适配器和
`WorkerSupervisor`。没有把 `whisper_cpp.py`、PortAudio playback、麦克风、OCR 或 perception pipeline 接到
worker；这些仍分别属于 W17、W18、W21。根目录 `HANDOFF.md`、`agent.md`、总体 Windows 计划状态、shared
settings、bootstrap、`windows_security.py` 和 W11 health/logging 实现均未修改。

非 Windows 默认适配器明确返回 `worker_platform_unsupported`，`supports_job_objects=false`；测试 fake 名称和
文档均明确标为 fake，不作为 Windows Job Object 证据。`WorkerSupervisor` 实现 W11 `HealthProvider` 契约，
但本 PR 不把不存在的 Media/Perception worker 注册进应用 health 聚合器。

### 公开仓库与证据边界（2026-07-18）

项目所有者已把仓库改为 public，并声明其检查过没有隐私数据上传。这里将该声明记录为 **owner attestation**；
它不是由本 PR 完成的独立全历史安全审计。旧文档中“仓库必须保持 private”的前提已由所有者的新决定覆盖，
不再作为 W12 阻塞条件。

公开化没有自动建立合并保护：协调器只读检查确认 `agent/windows-development-baseline` 当前没有 branch
protection，仓库 ruleset 为空。因此本 PR 仍依赖 Draft PR、exact-head checks、expected-head guard 与合并后读回，
不得把仓库公开状态描述为受保护状态。

PR 分支、Actions 日志和上传 artifact 现在可能公开可见。W12 只允许合成 sentinel、计数型指标和无用户路径的
provenance/最小诊断证据进入这些表面；严禁真实 secret/token、用户正文、数据库、运行日志、截图、WAV、模型或
角色资产以及用户路径进入 commit、PR、CI 日志或 artifact。本 PR 的 sentinel 和 helper 输入均为合成数据；该
结论仅描述 W12 diff 与验证证据，不扩张为仓库全历史审计结论。

## 契约与 owner

| 资源 | 唯一 owner | 上限/终止语义 |
| --- | --- | --- |
| helper stdin/stdout/stderr 匿名 pipe | `ManagedProcess` | 只继承 3 个 std handle 和最多 16 个显式批准 handle；所有控制写受绝对预算约束；close 逐个验证 |
| helper frame buffer | `FrameDecoder` | 4-byte big-endian 长度；UTF-8 JSON；单 frame 64 KiB；buffer 最大 64 KiB + 4 |
| payload | protocol parser | 最多 16 keys/items、深度 3、字符串 4096 chars、整数限制在精确 JSON 范围 |
| worker stderr | `WorkerSupervisor` | drain 全部输入；只保留字节计数；诊断预算最多 1 MiB，不保留正文 |
| active jobs | parent `WorkerSupervisor` + helper 独立 ceiling | 两端默认最多 8；caller cancel 后 slot 保留到 terminal settlement 或 Job 子树确认归零；重复 ID/满载拒绝 |
| lifecycle task | `WorkerSupervisor` epoch barrier | handshake 5 秒、heartbeat 5 秒、soft grace 0.5 秒、terminate wait 2 秒；stop 失效所有旧 start/callback |
| crash history/event | `WorkerSupervisor` | crash deque 最多 33；event deque 最多 256；默认预算 3，窗口 60 秒 |
| Windows Job Object | `WindowsJobProcessAdapter` | unnamed、`KILL_ON_JOB_CLOSE`；整个子树 hard terminate；关闭后 active count 归零 |
| approved root/handle | trusted parent + worker-side policy | wire 只含 symbolic authority；helper 返回已打开、non-inheritable、按最终 handle path 复核且由 job handler 生命周期独占的 descriptor，不返回 Path capability |
| temp scavenger | `WorkerSupervisor` | 仅接受 cancellation-safe async callable；与 close 共用绝对 deadline；不创建不可终止 executor thread |

### 综合基线资源 owner 矩阵

| 资源 | 唯一 owner | 容量/deadline | 异常回收与观测证据 |
| --- | --- | --- | --- |
| client live queue | `EventSubscription`，注册表由 `TurnService` 持有 | 每订阅最多 512；replay 独立最多 2,000 | overflow 主动以 `slow_consumer` 断开；10k 与慢消费者探针证明不阻塞 publisher |
| turn state/replay | `TurnService` + `IdempotencyStore` | terminal 200/24 h；replay 2,000/10 min | cancel/preemption/shutdown 收口；10k 后 200/2,000/200，TTL 后均降至 1 |
| TTS job/ready audio queue | W07 dialogue `TaskGroup`；producer 各自唯一关闭 | 8/4；single 32 MiB、inflight 64 MiB、总音频 120 s | cancel/fault 取消 TaskGroup、drain queue/outstanding registry；故障风暴结束 depth/lease/temp 为 0 |
| VTS action queue | `VTSBridge` | 默认 16，可配置 1..256 | generation 变化、cancel、断线和 close purge；close join transport/task；10 批 fake-server 风暴通过 |
| asyncio task | 各 `TurnService` task set、dialogue `TaskGroup` 或 `WorkerSupervisor` lifecycle slot | active turn 单一；worker jobs 默认 8 | done callback 移除；shutdown drain/join；并发 start/cancel/close 与重复 close 风暴通过 |
| SQLite worker thread/connection | asyncio default executor；每个 `SQLiteDatabase.connect()` context 独占连接 | 无连接池；busy timeout 5 s；操作完成即 close | exception context 关闭连接；100 轮压力 thread peak/end 均 5，DB 文件每轮清除 |
| provider cancel slot/timer | GPT-SoVITS pending-operation slot；provider/worker owner 使用 event-loop monotonic timer | slot 受 TTS queue 8 限制；connect/first-byte/total/cancel 为 8/8/30/1 s；worker hard deadline最多 120 s | completion callback 才释放；close 等真正 settlement；missed cancel 开 circuit；10 批 deadline/cancel 风暴通过 |
| process/Job | `WindowsJobProcessAdapter`，生命周期编排由 `WorkerSupervisor` | 一个 unnamed Job 覆盖完整树；grace 0.5 s、terminate wait 2 s | deadline/parent crash 关闭或 terminate Job；实测 peak 5→active 0、pipe EOF、handle 归基线 |
| handle/fd/匿名 pipe | `ManagedProcess` | 3 个 std handle + 最多 16 个批准 handle | 所有创建失败/close 路径逐个回收；六次样本 227→227，重复树增长 0 |
| stderr ring/accounting | `WorkerSupervisor` | drain 无上限输入但只计数；诊断计入上限 1 MiB，不保存 bytes | EOF/reaper 完成后释放；2 MiB 输入计入 1 MiB、`truncated=true`，sentinel 不出现在任何正文表面 |
| temp/audio lease | `TempAssetRegistry` + W07 outstanding registry | 同 ready slot；inflight 64 MiB | playback/skip/cancel/close settle 后 scavenger；原生与 100 轮探针 residual=0 |

helper envelope 只允许 `schema_version`、`message_type`、`request_id`、`payload`；schema version 当前为 1。
解析禁止重复 JSON key、NaN/Infinity、unknown envelope field、任意 Python object 和 pickle。控制消息覆盖 hello、
handshake、heartbeat、job start/cancel/terminal、shutdown/stopped；真实音频/STT/感知 job kind 未定义。

路径必须是批准 root 下的相对路径。父端和 worker 端均先拒绝 absolute/`..`/NUL/超长形式，再对 lexical 路径
做 reparse 检查、`resolve(strict=True)` 和 canonical root containment。helper 随后先打开 regular file，再从
该 descriptor 对应的 kernel handle 取得最终路径并重新验证 root containment；handler 只消费稳定 descriptor，
helper 在 terminal `finally` 关闭它。Windows 实测在 authorize 与消费之间尝试把父目录换为 root 外 junction 时，
打开 descriptor 阻止换向且仍只能读取合成 `SAFE`；若换向抢先完成，最终 handle path 检查会拒绝。handle 在 wire
上只使用批准的 symbolic ID；Windows spawn 的 handle list 只包含 std pipe 与明确批准值。

## Windows 创建与关闭

Windows 使用 `CreateProcessW`，传独立 application name 和参数数组生成的 command line，不调用 shell。
创建顺序是：

1. 创建匿名 pipe，并清除 parent 端继承位；
2. 创建 unnamed Job，设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`；
3. 用 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 限制可继承 handle；
4. 以 `CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT` 创建 helper；
5. 在 primary thread 仍 suspended 时 assign Job；
6. assign 成功后才 resume，关闭 thread handle 和 parent 持有的 child pipe ends。

这样避免“child 先派生再逃出 Job”的窗口。控制 pipe 写入由每进程一个惰性、daemon、容量 1 的 writer owner
执行；event loop 等待的 Future 可被 absolute deadline 取消，Job terminate/pipe close 会解除底层写阻塞，close
必须等待 writer 退出并释放 queue/Event。进程 wait 改为非阻塞 `WaitForSingleObject(..., 0)` 轮询，不再生成
不可控的 executor waiter。关闭顺序固定为：停止接收新 job → 对 active job 发 soft cancel →
发 shutdown 并等待 grace → 未退出则 terminate Job → bounded wait → 查询 active process → 关闭 pipe/Job/process
handle → 等待 reader/reaper task → temp scavenger → 生成 content-free report。

一次 handle 反方检查发现 closed process 对象仍保留 `threading.Lock` 的 Windows `Semaphore` handle。后续
backpressure 修复完全移除了该写锁和 `asyncio.to_thread(os.write)`：writer 的 queue/Event/thread 在 close 后
显式释放，关闭后的写入返回稳定 `worker_process_closed`；完整 adapter/supervisor 预热后的连续 Job 样本不再
线性增长。
创建失败路径同时显式绑定 64-bit `TerminateProcess` 句柄签名，并回收中途完成 `open_osfhandle` 转换的 fd；
首个 helper `hello` 写失败也只返回稳定退出码，不向 stderr 泄漏 traceback。

## 自动证据

### 协议、故障与隐私

- Hypothesis/random-byte parser tests 覆盖任意 chunk boundary、random bytes、半截 header/body、0/超长 frame、
  invalid UTF-8/JSON、重复 key、版本不匹配、非法类型/深度/数量和多 frame 拼接。
- fake supervisor 覆盖 hanging job、heartbeat 丢失、crash loop、指数退避、quarantine、取消/关闭竞态、
  startup timeout、无 `CREATE_NO_WINDOW` 声明、spawn/write/query/scavenge failure 和 hard shutdown。
- 新增审计回归覆盖：忽略 soft cancel 时 parent slot 不提前释放且 Job 被强制归零；helper 自己拒绝超过协商
  ceiling 的第二个 job；显式 zero deadline 在写 `job.start` 前拒绝；blocked `job.start`/`shutdown` 写入均在
  absolute deadline 内触发 terminate；spawn/handshake barrier 后的旧 start 不得覆盖 disabled；hanging async
  scavenger 被同一 close deadline 取消且不创建 executor thread。
- 2 MiB synthetic stderr 被完全 drain；snapshot 只计入 1,048,576 bytes，正文不保留。
- `W12_TRANSCRIPT_OCR_PCM_BODY_SENTINEL`、synthetic secret 和用户路径同时注入 worker 日志字段；event、JSONL、
  managed temp 和最终 diagnostic ZIP 逐一扫描，均不含 sentinel、secret 或路径。
- resource tests 覆盖 root/handle 混淆、unknown field、absolute/parent escape、missing/non-regular file 和
  symlink/reparse escape。当前 Windows 用户不能创建普通 symlink 的 portable 单测会 skip；真实 junction
  TOCTOU 用例在旧实现先稳定读到 root 外合成 `OUTSIDE`，修复后换向被打开 descriptor 拒绝且只读到 `SAFE`。
- 旧 head 的 W12 fault suite 连续 20 批：每批 `64 passed, 1 skipped`，合计 1,280 passed、0 failed；覆盖 parser
  property/fuzz、frame、heartbeat、crash/backoff/quarantine、stderr、cancel/close race、shutdown 与路径边界。
  这是历史稳定性证据；新增修复后的重复风暴与全量 exact-head 结果必须另行记录，不能继承该计数。
- 修复后的 fault suite 从第 1 批完整重跑 20 批：每批 `74 passed, 1 skipped`，合计 1,480 passed、0 failed；
  唯一 skip 仍是当前账户无普通 symlink privilege，真实 junction TOCTOU 用例每批均执行且通过。
- W07/W08/VTS/observer/e2e fault suite 连续 10 批：每批 23 passed，合计 230 passed、0 failed；没有使用
  failed-only rerun。

### 10k turn 与共享 SQLite claim（2026-07-19）

`tools/w12_gate_w2_probe.py` 只使用合成文本并输出 path-free JSON。10,000 个 accept→cancel turn 在 7,891 ms
完成：terminal state 200、replay 2,000、idempotency record 200，TTL 后三者状态/记录降至 1；慢消费者明确断开，
disk/side-effect 为 0。进程 RSS 从 47,243,264 bytes 到 51,486,720 bytes，观测峰值 51,486,720；handle
190→190，thread peak/end 1。

W06 hotfix 压力为 100 个独立 SQLite 数据库、两个 `TurnService`、每阶段 128 并发、共 25,600 次 accept：
`failure_count=0`，provider/TTS/playback/VTS/accept observer/completion observer 六类副作用每轮均精确为 1；
最大数据库占用 159,744 bytes，residual=0。该阶段 RSS 51,486,720→53,862,400 bytes，峰值
54,816,768；handle 190→215、峰值 218（asyncio/SQLite executor 建立后的稳定水位），thread peak/end 5。
原生探针随后在独立进程边界验证 Job handle 六样本完全回到基线，不能把此 executor 水位误报为 Job 泄漏。

### 2026-07-19 当前 Windows 主机实测

命令：

```powershell
uv run python tools/w12_windows_probe.py --helper tests/helpers/w12_worker_helper.py
```

最终 path-free 指标：

| 指标 | 实测 |
| --- | ---: |
| Job 内受控进程峰值 | 5（venv launcher 数量属环境实现，不固定为 2） |
| terminate 后 active process | 0 |
| worker/孙进程退出 latency | 16 ms |
| parent crash 后子树退出 latency | 0 ms（首轮轮询即已退出） |
| hard deadline | 请求 200 ms；观察 94 ms（hard upper bound，提前终止） |
| orderly shutdown latency | 31 ms；报告 deadline met |
| stderr 输入/诊断计入 | 2,097,152 / 1,048,576 bytes；truncated=true |
| parent handle samples | `218 → 218 → 218 → 218 → 218 → 218` |
| 第二次 child-tree handle 增长 | 0 |
| stdout pipe EOF | true |
| `GetConsoleWindow` / 可见顶层窗口枚举 | `0 / 0` |
| managed temp 残留文件 | 0 |
| crash budget | 3；第 4 次窗口内 crash quarantine |

真实 Windows pytest 另行覆盖：worker 再派生孙进程、Job hard kill、parent `os._exit` 后
`KILL_ON_JOB_CLOSE`、deadline、额外批准 handle 继承、重复 5 次 close、active process 归零、helper 内
`GetConsoleWindow()==0`、caller cancel 被 handler 忽略后的容量防绕过，以及不读 stdin child 的 64 KiB+4
完整 frame backpressure。修复后该原生套件 7/7 通过；mock/fake 测试不计入这组真机证据。
macOS 另用明确标记的 fake-kernel API 合约测试覆盖 ctypes 绑定、参数拒绝和 handle 生命周期，以避免平台专属
代码压低跨平台 coverage；它只证明 portable 控制流契约，不证明 macOS 具有或执行了 Windows Job Object。

CI 平台修复与 portable fake-kernel owner 覆盖后的最终本机全仓复核为 `1055 passed, 3 skipped`，raw branch
coverage `90.32%`；当前 W12 focused 为 `88 passed, 1 skipped`，真实 Windows 原生套件仍为 `7 passed`；
Ruff check、
Ruff format、strict mypy（196 个源文件）和 `git diff --check` 均通过。最终 exact-head 的
wheel/source-quarantine 与双平台 CI 仍须逐项记录在 Draft PR #21。raw branch coverage 必须真实大于
90.00%，不能
利用 coverage `precision=0` 把 89.97% 四舍五入成 formal success。wheel/cache/venv 不上传；本地 provenance
不是 release artifact。双平台结论只能由 Draft PR 精确 head 的 GitHub Actions 证明，不能由本机 Windows 代替。

## 安全反方审查

- **命令注入：** 无 shell；application name canonicalize 为绝对 regular file；参数数、NUL 和 command-line
  长度有上限。剩余信任边界是 parent 组装的 command 参数，不接受 helper frame 覆盖 executable/argv。
- **handle 泄漏：** thread/process/Job/pipe close 均检查；额外 handle 精确 allowlist；写锁与 executor writer
  已移除；惰性 writer 的 queue/Event/thread 由 `ManagedProcess.close()` 唯一回收。handle table 诊断确认首次
  adapter 初始化的 5 个增量分属 5 种各一个的惰性进程对象，不是每轮同类线性增长；完整预热后的连续 5 次
  Job 样本保持在稳态阈值内。
- **命名对象：** 没有 named pipe、named Job、mutex、共享内存或可连接 listener；唯一 IPC 是继承匿名 pipe。
- **路径逃逸：** wire 不携带 absolute 用户路径；helper 以打开 descriptor 和 kernel final path 作为 authority，
  不把普通 `Path` 交给 handler；junction swap 回归证明无法消费 root 外对象。
- **子树逃逸：** suspended assign-before-resume；失败时不 resume，close Job；parent crash 真机测试通过。
- **无限 buffer：** frame/payload/job/event/crash/stderr 全部有硬上限；stderr 不保留原始 bytes。
- **假 timeout：** absolute deadline 从 `job.start` 写入前开始，并为 Job terminate/归零预留预算；所有控制写
  受限。shutdown 不依赖 child 继续读 pipe；hard deadline 后实际调用 `TerminateJobObject` 并 wait/query，
  不把 thread cancellation 当 native stop。立即 pipe write error 也会 hard-fault/terminate，确认子树归零后才释放
  capacity。shutdown report 区分 hard terminate、process count、scavenge 和 deadline。

## 兼容、迁移与回滚

没有 settings schema、数据格式或数据库迁移。wheel 新增 `app.workers` 包；非 Windows import 可用但运行明确
unsupported。回滚是移除 `app/workers` 和 W12 测试/探针；因为本 PR 没有接线真实 feature，文字模式和既有
同进程原型行为不变。不得把回滚解释为允许后续真实 native feature 回到不可终止 thread。

## 首次失败、rerun 与证据限制

- exact head `0908594f...` 的独立审计先真实复现 6 个 Gate 阻断：caller cancel 后 active slot=0 但 Job
  active process 仍为 5 且可接受第二棵树；zero deadline 被替换为默认值；64 KiB+4 pipe write 与 fake
  `job.start`/`shutdown` 写入可越过 deadline；旧 start 在 stop 返回后覆盖 disabled；sync scavenger thread
  可无限存活；authorize 后 junction swap 使同一 Path 读到合成 `OUTSIDE`。对应 8 个新增 focused tests 在
  旧实现上 8/8 红，junction 测试亦独立红；这些首次失败均保留，不以新 head 绿灯覆盖。
- 修复后的首次原生全套运行中，功能断言已通过，但 steady-state handle 测量失败且内部 Future 报出未消费
  exception。Future accounting 已修复；handle table 只读分类进一步证明 5 个首次增量分别属于 5 种惰性
  初始化对象，因此将 baseline 修正为先完整预热 adapter/supervisor、再测连续五轮稳态增量。修正测量后完整
  7-test 原生套件从头运行通过；没有用 failed-only rerun 冒充首次稳定通过。
- 修复后首轮 20-batch fault storm 在第 12 批发现 heartbeat 用例的测试时钟假设：固定等待 120 ms 时已观察到
  Job terminate，但 reaper 尚处于合法中间态 `failed`，未到 `quarantined`。测试改为在 300 ms 硬上界内轮询
  明确终态，仍保留 terminate/quarantine/error-code 断言；随后必须从第 1 批重新运行完整 20 批。
- 旧 exact head `f64aa742...` 的 duplicate push run 29642987621 attempt 1 曾在 W06 共享 SQLite claim
  抛出一次 `IdempotencyConflictError`（1 failed、877 passed、12 skipped，raw coverage 90.03%）；同一 run
  failed-job attempt 2 formal success（878 passed、12 skipped），但 raw coverage 只有 89.97%。pytest-cov
  `precision=0` 使 formal `fail_under=90` 的实际下限约为 89.5%，所以该绿灯不构成 Gate W2 raw>90 证据。
- 新合成 Gate 探针首次启动因 64-bit Windows handle 未声明 ctypes `argtypes` 失败；修正签名后才开始压力。
  原生汇总探针首次因 EOF bounded read 漏传 `max_bytes` 失败；修正后完整重跑。第一次完整原生指标的
  handle delta=2 来自探针自身仍持有 `Popen` process/thread handle；显式结束其生命周期后完整重跑，六样本
  delta=0。产品原生 pytest 独立首次运行 5/5 通过。
- 真实 junction 验证成功且 residual=0，但 PowerShell 在单独删除 junction 时发出一次 NullReference cleanup
  警告；在验证 absolute cleanup target 仍属于当前 worktree 后由同一 PowerShell 递归清理完成。
- 第一轮全仓 coverage 为 `1041 passed, 3 skipped`、raw `89.92%`，因此按 Gate 契约判定失败并补齐真实的
  access 安全分支测试。第二轮错误选用了过长的 `.tmp/pytest-full-local-2` basetemp，触发 4 个既有 W02
  Windows 长路径夹具的 `FileNotFoundError`（`1044 passed`、raw `89.88%`）；没有修改 W02 产品代码。改用
  worktree 内最短 `t` basetemp 后，从头完整运行得到 `1048 passed, 3 skipped`、raw `90.12%`，退出码 0。
- 最终反方 diff 审查又发现 `job.start` 若立即返回 pipe write error（而非超时），旧修复会抛错但保留 slot 且
  不终止 Job。新增回归先失败于 `process.terminated == false`；将该错误并入 hard-fault/Job-zero settlement 后
  focused `78 passed, 1 skipped`、真实 Windows `7 passed`，并从头重跑全仓得到 `1049 passed, 3 skipped`、
  raw `90.06%`。一次并行 focused/native/static 编排在所有 Python 子进程退出后仍未返回可读取结果，已终止且
  不计作通过；上述三组随后均以串行完整重跑和明确退出码取证。
- exact head `b16d0e3...` 的 push run 29677691632 和 PR run 29677692676 attempt 1 均失败并完整保留。
  两个 macOS quality job 都因 Darwin `fcntl.fcntl` 的 bytes 参数误用 4,096 bytes（Python 上限 1,024）导致
  2 个 access 失败及 2 个 helper 级联失败，raw 分别 89.29%/89.24%；修复改为 Darwin `F_GETPATH`
  `MAXPATHLEN=1024` 并增加可移植契约测试。PR Windows quality 另在 orderly shutdown 中观察到已完成 wait task
  但 soft grace 刚过，报告 `active_processes=0`、`exit_code=None`（1 failed、1050 passed、raw 90.08%）；新增
  确定性慢返回测试先复现，再允许同步读取已完成 task 的 result，未扩张 deadline。push Windows quality 与四个
  wheel job 在该旧 head 通过，但不能套用到修复后的新 head。
- 为使 macOS 对新增 Windows writer 代码保持真实 raw>90，新增明确标注为 portable fake-kernel、绝不冒充
  Job Object 真机证据的 backpressure/error/partial-owner tests，覆盖 queue-full、caller Future cancel、底层
  write error、pending close、WinAPI 失败和 partial fd/handle cleanup。覆盖扩展首次全仓运行因 synthetic
  last-error 未隔离而失败（1 failed、1052 passed、raw 90.20%）；显式注入 synthetic last-error 后最终全仓
  `1055 passed, 3 skipped`、raw `90.32%`。
- exact head `838ed8d...` 的 push run `29678564645` 与 pull-request run `29678565463` 均保留为失败证据。
  两次 Windows quality、四次 installed-wheel job 均通过；两次 macOS quality 的全量测试也分别以 raw branch
  coverage `90.17%` 与 `90.22%` 通过，但随后 strict mypy 在 `app/workers/access.py` 对 Darwin typeshed
  直接解析 Windows-only `ctypes.WinDLL`、`msvcrt.get_osfhandle` 与两处 `ctypes.get_last_error` 时产生 4 个
  `attr-defined` 错误。修复仅将 `ctypes`/`msvcrt` 模块视图显式 `cast(Any, ...)` 后调用这些既有 Windows API；
  不增加 ignore，不改变 final-handle 路径校验、descriptor ownership、异常语义、wire protocol 或公开接口。
  `838ed8d...` 的其余绿灯不能继承为新 head Gate 证据；修复后的本地全量、wheel smoke 与 push/PR 八项 CI
  必须从头重跑。
- 自动无闪窗证据包括 `CREATE_NO_WINDOW` flag、helper 内 `GetConsoleWindow()==0` 与运行期
  `EnumWindows` 可见窗口数 0；离散枚举仍不能绝对证明未出现比采样更短的瞬时窗口，因此只保留一次极小人工观察。

## 未完成的人工项与 Gate W2

W12 自身仍需：

1. 并发/安全 reviewer 对 protocol、owner、handle、shutdown 顺序和残余风险签字；
2. `CREATE_NO_WINDOW` 和 helper 内 `GetConsoleWindow()==0` 已自动证明，但“绝无肉眼可见瞬时闪窗”仍只能保留
   一次人工观察项；静态截图不能可靠证明短暂事件；
3. 项目所有者已回复“W12 审计合格”，只关闭 W12 自身实现审计；只有另行收到明确的
   “W12 / Gate W2 审计合格”后才允许请求最终合并。

W07/W08/W09/W10 与 hotfix 现已全部进入精确综合 baseline，10k turn、慢消费者、共享 SQLite 高重复压力、
故障风暴、native hang/parent crash、shutdown 和资源 owner 矩阵已由 AI 重跑。综合 Gate W2 仍**不由本 PR
自行关闭**：新 exact head 尚须完成本机 raw coverage>90、wheel 与双平台 CI 读回，并等待协调器独立审计。
本 PR 保持 Draft，不转 Ready、不合并、不开始 W13；只有用户对该 exact head 明确回复
“W12 / Gate W2 审计合格”后才允许请求受控合并。
