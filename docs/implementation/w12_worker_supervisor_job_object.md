# W12：Windows WorkerSupervisor、Job Object 与 helper protocol 基座

> 基线：`a8a4fdba2e64919b785393cdc3e0b2fd4ce4146e`（W09 已受控合并后的
> `agent/windows-development-baseline`）
> 分支：`codex/w12-worker-supervisor-job-object`
> 风险：P1-13、P1-14、P1-15
> Gate：W12 AI 实现审计完成后仍须并发/安全 reviewer 签字；综合 Gate W2 保持未关闭

2026-07-18 增量基线复核：W12 分支以 merge commit 纳入上述 W09 基线。相对先前已审计的 W12 head
`549b88fb33a0424d6886683b72a0bfe40afa97eb`，worker 实现、测试和探针在合并时均无内容变化；最终 PR diff
相对新基线仍仅包含 W12 文件。W09 对 settings、bootstrap、GPT-SoVITS 和 VTS 的变更与 W12 文件零重叠，
W12 worker 不导入这些接口。该结论经共享接口聚焦回归、完整质量门及增量安全反方审查重新验证，不能解释为
吸收或审计 W09 范围之外的功能。

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
| helper stdin/stdout/stderr 匿名 pipe | `ManagedProcess` | 只继承 3 个 std handle 和最多 16 个显式批准 handle；close 逐个验证 |
| helper frame buffer | `FrameDecoder` | 4-byte big-endian 长度；UTF-8 JSON；单 frame 64 KiB；buffer 最大 64 KiB + 4 |
| payload | protocol parser | 最多 16 keys/items、深度 3、字符串 4096 chars、整数限制在精确 JSON 范围 |
| worker stderr | `WorkerSupervisor` | drain 全部输入；只保留字节计数；诊断预算最多 1 MiB，不保留正文 |
| active jobs | `WorkerSupervisor` | 默认最多 8；job deadline 最大 120 秒；重复 ID/满载拒绝 |
| lifecycle task | `WorkerSupervisor` | handshake 5 秒、heartbeat 5 秒、soft grace 0.5 秒、terminate wait 2 秒 |
| crash history/event | `WorkerSupervisor` | crash deque 最多 33；event deque 最多 256；默认预算 3，窗口 60 秒 |
| Windows Job Object | `WindowsJobProcessAdapter` | unnamed、`KILL_ON_JOB_CLOSE`；整个子树 hard terminate；关闭后 active count 归零 |
| approved root/handle | trusted parent + worker-side policy | immutable authority；父端发送前和 worker handler 接收前各验证一次 |

helper envelope 只允许 `schema_version`、`message_type`、`request_id`、`payload`；schema version 当前为 1。
解析禁止重复 JSON key、NaN/Infinity、unknown envelope field、任意 Python object 和 pickle。控制消息覆盖 hello、
handshake、heartbeat、job start/cancel/terminal、shutdown/stopped；真实音频/STT/感知 job kind 未定义。

路径必须是批准 root 下的相对路径。父端和 worker 端均先拒绝 absolute/`..`/NUL/超长形式，再对 lexical 路径
做 reparse 检查、`resolve(strict=True)`、canonical root containment 和第二次 reparse 检查；只允许 regular
file。handle 在 wire 上只使用批准的 symbolic ID；Windows spawn 的 handle list 只包含 std pipe 与明确批准值。

## Windows 创建与关闭

Windows 使用 `CreateProcessW`，传独立 application name 和参数数组生成的 command line，不调用 shell。
创建顺序是：

1. 创建匿名 pipe，并清除 parent 端继承位；
2. 创建 unnamed Job，设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`；
3. 用 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 限制可继承 handle；
4. 以 `CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT` 创建 helper；
5. 在 primary thread 仍 suspended 时 assign Job；
6. assign 成功后才 resume，关闭 thread handle 和 parent 持有的 child pipe ends。

这样避免“child 先派生再逃出 Job”的窗口。关闭顺序固定为：停止接收新 job → 对 active job 发 soft cancel →
发 shutdown 并等待 grace → 未退出则 terminate Job → bounded wait → 查询 active process → 关闭 pipe/Job/process
handle → 等待 reader/reaper task → temp scavenger → 生成 content-free report。

一次 handle 反方检查发现 closed process 对象仍保留 `threading.Lock` 的 Windows `Semaphore` handle。修复后
`close()` 会清空写锁，关闭后的写入返回稳定 `worker_process_closed`；连续 Job 样本不再线性增长。
创建失败路径同时显式绑定 64-bit `TerminateProcess` 句柄签名，并回收中途完成 `open_osfhandle` 转换的 fd；
首个 helper `hello` 写失败也只返回稳定退出码，不向 stderr 泄漏 traceback。

## 自动证据

### 协议、故障与隐私

- Hypothesis/random-byte parser tests 覆盖任意 chunk boundary、random bytes、半截 header/body、0/超长 frame、
  invalid UTF-8/JSON、重复 key、版本不匹配、非法类型/深度/数量和多 frame 拼接。
- fake supervisor 覆盖 hanging job、heartbeat 丢失、crash loop、指数退避、quarantine、取消/关闭竞态、
  startup timeout、无 `CREATE_NO_WINDOW` 声明、spawn/write/query/scavenge failure 和 hard shutdown。
- 2 MiB synthetic stderr 被完全 drain；snapshot 只计入 1,048,576 bytes，正文不保留。
- `W12_TRANSCRIPT_OCR_PCM_BODY_SENTINEL`、synthetic secret 和用户路径同时注入 worker 日志字段；event、JSONL、
  managed temp 和最终 diagnostic ZIP 逐一扫描，均不含 sentinel、secret 或路径。
- resource tests 覆盖 root/handle 混淆、unknown field、absolute/parent escape、missing/non-regular file 和
  symlink/reparse escape。当前 Windows 用户不能创建普通 symlink 的 portable 单测会 skip；仓库既有真实
  junction/reparse tests 继续通过，不能用该 skip 冒充无风险。

### 2026-07-18 当前 Windows 主机实测

命令：

```powershell
uv run python tools/w12_windows_probe.py --helper tests/helpers/w12_worker_helper.py
```

最终 path-free 指标：

| 指标 | 实测 |
| --- | ---: |
| Job 内受控进程峰值 | 5（venv launcher 数量属环境实现，不固定为 2） |
| terminate 后 active process | 0 |
| child-tree kill latency | 16 ms |
| hard deadline | 请求 200 ms；观察 188 ms（hard upper bound，提前终止） |
| orderly shutdown latency | 140 ms；报告 deadline met |
| stderr 输入/诊断计入 | 2,097,152 / 1,048,576 bytes；truncated=true |
| parent handle samples | `228 → 228 → 228 → 228 → 228` |
| 第二次 child-tree handle 增长 | 0 |
| managed temp 残留文件 | 0 |
| crash budget | 3；第 4 次窗口内 crash quarantine |

真实 Windows pytest 另行覆盖：worker 再派生孙进程、Job hard kill、parent `os._exit` 后
`KILL_ON_JOB_CLOSE`、deadline、额外批准 handle 继承、重复 5 次 close、active process 归零和 helper 内
`GetConsoleWindow()==0`。mock/fake 测试不计入这组真机证据。
macOS 另用明确标记的 fake-kernel API 合约测试覆盖 ctypes 绑定、参数拒绝和 handle 生命周期，以避免平台专属
代码压低跨平台 coverage；它只证明 portable 控制流契约，不证明 macOS 具有或执行了 Windows Job Object。

新基线完整本机质量门：`856 passed, 3 skipped`，branch coverage `90.25%`；三项 skip 是缺少可选 PIL/RapidOCR 和
当前用户不能创建普通 symlink。共享接口与 W12 聚焦回归另为 `161 passed, 1 skipped`。Ruff lint、Ruff format
与 strict mypy（185 source files）通过；最终提交前
Windows wheel/source-quarantine smoke 通过：113 members，manifest SHA-256
`ec6904fc21e87757688348ee3e645dce6e284d5d28d1896709d992b7c68baef5`，仓库外安装结果
`source_tree_imported=false`。wheel/cache/venv 不上传；本地 provenance 不是 release artifact。
双平台 CI 只能由 Draft PR 精确 head 的 GitHub Actions 证明，不能由本机 Windows 结果替代。

## 安全反方审查

- **命令注入：** 无 shell；application name canonicalize 为绝对 regular file；参数数、NUL 和 command-line
  长度有上限。剩余信任边界是 parent 组装的 command 参数，不接受 helper frame 覆盖 executable/argv。
- **handle 泄漏：** thread/process/Job/pipe close 均检查；额外 handle 精确 allowlist；写锁 semaphore 泄漏已
  用 handle type inventory 定位并修复；连续样本和重复 5 次测试稳定。
- **命名对象：** 没有 named pipe、named Job、mutex、共享内存或可连接 listener；唯一 IPC 是继承匿名 pipe。
- **路径逃逸：** parent/worker 双重 canonicalization + reparse 拒绝；wire 不携带 absolute 用户路径。
- **子树逃逸：** suspended assign-before-resume；失败时不 resume，close Job；parent crash 真机测试通过。
- **无限 buffer：** frame/payload/job/event/crash/stderr 全部有硬上限；stderr 不保留原始 bytes。
- **假 timeout：** hard deadline 后实际调用 `TerminateJobObject` 并 wait/query；不把 thread cancellation 当 native
  stop。shutdown report 区分 hard terminate、process count、scavenge 和 deadline。

## 兼容、迁移与回滚

没有 settings schema、数据格式或数据库迁移。wheel 新增 `app.workers` 包；非 Windows import 可用但运行明确
unsupported。回滚是移除 `app/workers` 和 W12 测试/探针；因为本 PR 没有接线真实 feature，文字模式和既有
同进程原型行为不变。不得把回滚解释为允许后续真实 native feature 回到不可终止 thread。

## 未完成的人工项与 Gate W2

W12 自身仍需：

1. 并发/安全 reviewer 对 protocol、owner、handle、shutdown 顺序和残余风险签字；
2. `CREATE_NO_WINDOW` 和 helper 内 `GetConsoleWindow()==0` 已自动证明，但“绝无肉眼可见瞬时闪窗”仍只能保留
   一次人工观察项；静态截图不能可靠证明短暂事件；
3. 项目所有者已回复“W12 审计合格”，只关闭 W12 自身实现审计；只有另行收到明确的
   “W12 / Gate W2 审计合格”后才允许请求最终合并。

综合 Gate W2 **没有因本 PR 通过而关闭**。必须等待 W07、W08、W09、W10 各自审计合格、合并并进入综合
baseline，再重跑 10k turn、慢消费者、故障风暴、native hang、shutdown 和资源 owner 矩阵。本 PR 不开始
W13，也不自行合并。
