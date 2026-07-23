# W18：Push-to-talk、麦克风 ring buffer 与 Whisper Job

## 状态与范围

> 状态：先前 W18 worker 实现与 CI 修复提交 [`439fa88`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/439fa88)、
> [`69b46dd`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/69b46dd) 和
> [`6c66dc0`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/6c66dc0) 的 CI 均为已确认**历史证据**。
> 2026-07-23 本轮新增受管中文 runtime 的聚焦提交
> [`224e06f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/224e06f9cbb1d2ab0cc2260fb244b1f74cd7dfbc)
> 已在 exact head 完成 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977299022)
> 和 [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977296872) 的双 OS
> `quality` / `installed-wheel`。push workflow 首次 Windows quality 仅在未修改的
> `tests/unit/ui/test_chat_runtime.py` 显示失败标记，随后 job 在输出断言栈前结束；本机定向复现、PR workflow 和同 SHA 第 2 次
> 重跑都通过，故这一个现象的根因仍未验证，不能称已修复。
>
> 与之独立，随后仅文档 head [`28df5fd`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/28df5fde0fc726d65ffb4e1527a9795dd9efdf66)
> 的 [PR Windows quality](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29978065326) 明确失败于
> `test_successful_handshake_job_and_orderly_shutdown`：`ShutdownReport` 已确认进程关闭和零 active process，却遗漏了
> `exit_code`。本轮已补上 watcher 结果 harvest 和受控回归测试；前序 head 的绿测不能替代这一修复的 exact-head CI。
> Draft PR [#31](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/31) 未合并，后续新 head 仍须独立核验。
> 基线为 W17 merge commit [`351da92`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/351da92bfd0232ce03a90a97b75c13ba8ee6a51b)。
> 本文不把本地绿测、headless Qt 或 fake PortAudio/whisper 结果表述成真实麦克风、锁屏、IME 或全局热键体验。

W18 实现按住说话的本地转写路径，解决计划中的 P1-15（whisper 子进程树）和 P1-16（逐帧 callback backlog）。
它依赖 W12 的 `WorkerSupervisor` / Job Object 和 W17 的 `MediaWorker`，但不实现云端 STT、持续监听、真实系统级
global hotkey 或 W20 的 Windows lock/session adapter。

## 已实现的边界与契约

| 项目 | 实现与唯一 owner | 失败、上限与隐私语义 |
| --- | --- | --- |
| PTT 交互 | Qt `MainWindow` 的可见“按住说话”按钮；按下发 start，释放发 stop，焦点丢失/关闭发 cancel | 默认不打开麦克风、没有持续监听；转写中按钮禁用；焦点/关闭不会留下录音会话 |
| 父进程语音控制 | `app.media.voice.MediaWorkerVoiceInput` | 仅持有状态、受限 `VoiceTranscription` 和 temp registry 租约；不导入音频 binding、不持有 PCM/WAV/JSON、也不启动 whisper CLI |
| 麦克风采集 | `MediaWorkerHandler` / `SoundDeviceBackend.RawInputStream` | 仅 start 后打开；16 kHz mono int16 预分配 ring，最多 120 秒（约 3.84 MiB）；callback 不逐帧排入 asyncio loop |
| callback 故障 | MediaWorker ring | overflow、非完整 sample 和 PortAudio status 变为 `stt_capture_overflow` / `stt_capture_device_lost`；ring 被 wipe，stream 收束 |
| 本地转写 | helper 内 `WhisperCppRunner` | 无 shell argv、无内容 stdout/stderr；预检普通文件/重解析点、架构和 `--version`；canonical 受管路径额外完整校验 CLI/model SHA-256，不匹配不启动 CLI |
| 受管中文 runtime | `app.stt_runtime.ManagedChineseSttRuntime`，由 BackendThread UI command 或显式 CLI 调用 | 固定 HTTPS URL/版本/hash/上限；拒绝 ZIP Slip/link/reparse，私有 staging 后原子切换；默认不下载、不启用 STT 或麦克风，手工路径显示非受管 |
| 进程终止 | runner + W12 Job Object | 取消/超时先 terminate、等待 grace、再 kill；W12 Job Object `KILL_ON_JOB_CLOSE` 兜底收束 CLI 子树 |
| 临时文件 | MediaWorker 每次录音私有目录 | PCM 仅 ring；WAV/JSON 不跨 pipe。成功、失败、取消、watchdog 和 shutdown 都删除；删除未知时返回 `stt_temp_cleanup_pending`，不泄露成功 transcript |
| 结果回流 | typed helper payload → UI/backend | 只接受有界 text/language/segment_count；成功时恰好创建一条 `InputMode.voice` `UserMessage`，不传 raw audio |

`desktop_client/inputs/whisper_cpp.py` 的旧父进程实现已移除。`tools/stt_smoke.py` 也只提供 worker preflight 或显式
麦克风 PTT 路径，不提供任意本地文件转写入口。

## 受管中文 STT runtime（`224e06f` exact head 已核验）

受管 profile 固定采用 CPU 离线的 [`whisper.cpp` v1.9.1](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.1)
`whisper-bin-x64.zip` 和 [immutable `ggml-base-q5_1.bin`](https://huggingface.co/ggerganov/whisper.cpp/blob/87cd18b47b941d2f65d09981dad23bb7d0481c77/ggml-base-q5_1.bin)。
安装位置是 `%LOCALAPPDATA%\MeguminCompanion\models\stt\whispercpp\v1.9.1\`，而不是仓库、wheel 或安装包。

- 设置默认路径指向该 profile，`language` 统一为 `zh`；旧 `auto` 在显式设置升级时迁移为 `zh`，其他语言以稳定失败拒绝。
  `threads=null` 时 worker 使用 `min(max(os.cpu_count(), 1), 4)`。
- `SttInstallCommand`、确认文案和 `--install-chinese-stt` 共用同一服务。安装成功只持久化 provider/path/language，绝不修改
  `stt.enabled`、设备或线程；UI 继续允许手工路径，但明确显示 `unmanaged`。
- 下载只在用户确认后发起，使用固定 URL、HTTPS、10 分钟总 timeout、大小上限、archive/model 全量 SHA-256；archive 内仅
  提取 CLI 所在目录的平级 runtime 文件，拒绝 traversal、link、encrypted ZIP 和 reparse path。验证和 `--version` 成功后
  才原子切换，取消/失败只删除 staging。
- 每个 MediaWorker 的首次 preflight 会为 canonical profile 全量重新 hash CLI 与模型；完整性失败返回
  `stt_runtime_integrity_failed`，在 `whisper-cli` 之前停止。完整来源、hash、许可证和安全残余风险见
  [W18 runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)。
- `tools/stt_smoke.py --mode microphone --measure-working-set` 是唯一显式峰值诊断入口；它不打印转写正文。模型约 57 MiB
  是文件体积，不能当作内存测量结论。

## 自动化验收与证据

| 计划断言 | 已自动验证的证据 | 边界 |
| --- | --- | --- |
| callback 不逐帧向 loop 排队，overflow/device 事件明确 | `test_media_voice.py` 断言预分配 ring、无 `asyncio.Queue` / `call_soon_threadsafe`，并注入 overflow、status、非连续 frame | fake callback 不代表实际 PortAudio driver 时序 |
| start 才开设备，stop 生成一条 voice 消息 | worker test 断言 preflight 前没有 input stream；`test_w18_voice_ptt.py` 断言一次 stop 只进入一条 `InputMode.voice` `UserMessage` | headless Qt 不代表真实按钮手感或 IME |
| 120 秒、cancel、关闭与临时清理 | watchdog、start/cancel 竞争、empty/failure/cancel WAV、registry lease 和 cleanup-pending 分支均由合成测试覆盖 | best-effort wipe/删除不等于 SSD 物理擦除 |
| whisper runtime 安全预检与受控终止 | `test_whisper_cpp.py` 覆盖路径、架构、hash/fingerprint、version、中文空格路径、malformed/oversize JSON、timeout、cancel、terminate→kill | fake CLI 不能证明真实模型准确率 |
| helper 子进程树 | Windows-only `test_media_worker_job_closure_reaps_whisper_version_probe_child_tree` 启动 synthetic child，关闭 `WorkerSupervisor` 后检查 child PID 已消失 | Job Object 证明可控 synthetic tree；真实 driver/native 行为仍需设备 Gate |
| 配置与入口安全 | STT provider/language/device 边界、entrypoint 三参数原子性和 parent 无 raw-audio import 均由 unit test 覆盖 | 用户提供的真实模型/可执行文件尚未配置或验收 |
| 受管 runtime 供应与修复 | `test_stt_runtime.py` 使用 fake HTTP/ZIP/CLI 覆盖无启动下载、HTTPS→HTTP 降级拒绝、hash/timeout、Zip Slip/既有 reparse tree 拒绝、取消、同 service 并发、原子修复、手工路径和安装不访问麦克风 binding；`test_whisper_cpp.py` 验证 hash 不匹配不会启动 CLI，并以 synthetic child 读取 Windows `PeakWorkingSetSize`；UI/CLI bridge 使用 fake installer | CI 不下载真实 runtime/model、录音或转写，不测真实 Windows DACL、网络 CDN、模型准确率或真实 PTT 峰值工作集 |

历史 exact head 的最终本地命令（不替代本轮新 head 验证）：

- `uv run pytest` → 最后一次完整重跑 `1195 passed, 3 skipped in 190.60s`，coverage `90.18%`。
- `uv run pytest --no-cov tests/unit/test_media_voice.py tests/unit/test_whisper_cpp.py` → `56 passed in 7.88s`；余下
  fixture 规范化后再次执行 `tests/unit/test_whisper_cpp.py` → `24 passed in 4.84s`。

上述历史完整测试的 skip 是可选 RapidOCR、可选 Pillow 和当前账户的 directory-symlink 权限限制，pytest 已明确标记；没有
W18 断言失败。后续 `28df5fd` 的 Windows CI 日志则客观记录了
`test_successful_handshake_job_and_orderly_shutdown` 的 `None == 0` 断言。代码分析将其归因为：soft-grace 探测和
hard-termination 判定之间，watcher 可能已经完成但其结果尚未被写入报告；新增受控测试固定该完成顺序，而不是以重复运行
掩盖它。这个已确认的关闭报告问题与 `224e06f` push 首次无断言栈的 `test_chat_runtime` 标记不同。Ruff、格式、mypy、
`uv lock --check` 和 `git diff --check` 已在受管 runtime 提交前本地树通过。

### 本轮受管 runtime 的提交前本地核验（2026-07-23）

- `uv run pytest --no-cov tests/unit/test_stt_runtime.py tests/unit/test_stt_factory.py tests/unit/test_whisper_cpp.py
  tests/unit/test_media_voice.py tests/unit/test_media_entrypoint.py tests/unit/test_cli.py tests/unit/test_user_settings.py
  tests/unit/test_w05_ci.py tests/unit/ui/test_w16_management.py` → **192 passed in 10.79s**。
- `uv run pytest` → **1223 passed, 3 skipped in 191.56s**，coverage **90.01%**；skip 为 optional RapidOCR、optional
  Pillow 与当前账户的 directory-symlink 权限，不是 W18 失败。
- `uv run ruff check .`、`uv run ruff format --check .`（241 files）、`uv run mypy`（234 source）、`uv lock --check`、
  `git diff --check` 和全部 `docs/` 相对 Markdown 链接检查通过。
- `uv build --wheel --out-dir dist/w18-wheel-check` 后的 `tools/w05_ci_smoke.py` 隔离安装 smoke 通过；wheel 由隔离环境
  导入，且 STT model/binary/archive denylist 生效。验证产物和为选择 archive hash 下载的临时 ZIP 已在检查后删除。
- 当前关闭报告修复：`uv run pytest --no-cov tests/unit/test_worker_supervisor.py` → **46 passed in 2.26s**；原有
  orderly-shutdown 与新增受控 race 测试连续运行 20 次均通过；`uv run pytest` → **1224 passed, 3 skipped in 182.81s**，
  coverage **90.03%**。这些均为 fake process/CLI 条件，不访问麦克风、真实录音或真实模型。

这些仅证明提交前本地树；`224e06f` 随后的 Draft PR exact-head macOS/Windows `quality`/`installed-wheel` 已通过，
但它们仍不替代真实设备 Gate 或任何后续 head 的独立核验。

### `224e06f` exact-head CI（2026-07-23）

- [PR workflow 29977299022](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977299022) 与
  [push workflow 29977296872](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977296872) 的
  macOS/Windows `quality` 和 `installed-wheel` 均在该 SHA 通过。
- push workflow 首次 Windows `quality` 在 `tests/unit/ui/test_chat_runtime.py` 报一个失败标记但未输出断言栈；该文件不在
  `224e06f` 的变更中，且本机定向测试、PR workflow 与 push workflow 的第 2 次尝试均通过。故这是已记录的客观 CI 现象，
  **根因未验证**，不被写作修复。

### 后续 `WorkerSupervisor` 关闭报告 race

- 仅文档 head `28df5fd` 的 [PR workflow 29978065326](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29978065326)
  在 Windows `quality` 失败：`test_successful_handshake_job_and_orderly_shutdown` 收到
  `ShutdownReport(... active_processes=0, exit_code=None, process_close_succeeded=True, ...)`。这是有断言栈的已确认事实。
- 代码路径分析表明，soft-grace 到期后 watcher 可在 hard-termination 判定附近完成，导致终止被跳过但其已完成的结果没有被读取。
  `WorkerSupervisor._stop_impl` 现在在生成报告前再次读取已完成 watcher 的 `result()`；新增的
  `test_shutdown_harvests_wait_result_settling_between_stop_probes` 用固定 event-loop clock 和双态 fake task 约束该顺序。
- 这是一项 W12 lifecycle 依赖修复，不改变 W18 的 STT 供应、模型、隐私边界或设备 Gate。它必须由包含该修复的 exact PR/push
  head 核验；不得将 `224e06f` 的历史绿测当作替代证据。

## 唯一真实设备 Gate、未验证项与范围外

AI 已覆盖可合成的 ring、worker lifecycle、进程树、路径、取消、超时、清理和 Qt command/event 断言。本轮受管 runtime
唯一保留的真实设备 Gate 是：在 W18 exact PR head 上执行一次约 30 秒、不含敏感内容的中文 PTT，并以
`tools/stt_smoke.py --mode microphone --measure-working-set` 确认离线转写、真实麦克风/系统录音提示，以及
`whisper-cli` 的 Windows `PeakWorkingSetSize`。验收上限为 **≤512 MiB**；未测量或超限均阻止交付并重新选型，且不得把
录音或转写正文写入仓库，也不能用模型约 57 MiB 文件体积代替。

真实 IME/高 DPI、锁屏与系统级 hotkey 当前都没有通过结论：前两项不由 fake/headless 条件冒充，后两项尚未在本阶段
启用/实现。这些是后续桌面体验或 W20 工作，不是本轮受管 runtime 的额外发布 Gate。

产品仍限单机、单 Windows 用户、个人私用。RDP、快速切换用户、跨 session 与跨用户 SID/DACL 有效访问均为范围外，
不得写作已通过，也不列为本 W18 人工 Gate。

## 回滚、残余风险与发布状态

- 将 `stt.enabled` 设为 `false` 即可关闭 PTT、麦克风和 whisper helper；文字输入保持可用。不得以持续监听、同进程
  native 调用或任意文件转写作为回退替代。
- 真正的 PortAudio/driver 卡死无法由 Python task cancellation 证明停止；安全收束依赖 W12 的 helper Job kill。删除/wipe
  是生命周期隔离和 best effort，不构成物理介质擦除承诺。
- 真实模型文件、可执行文件、麦克风权限和性能仍是用户环境依赖。运行时预检以稳定错误码失败，不复制/上传模型或音频。
- [CVE-2026-10298](https://nvd.nist.gov/vuln/detail/CVE-2026-10298) 的 NVD 记录仅列范围至 1.8.2，但
  [upstream issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 在本次复核仍为 open；不得宣称 v1.9.1
  已修复。受管模型 hash 只缓解替换/错误输入，不能替代上游修复或保护手工非受管模型。
- Draft PR [#31](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/31) 已创建，初始实现提交为
  [`439fa88`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/439fa88)。功能 head
  [`6c66dc0`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/6c66dc0) 的两套 workflow 已通过：
  [PR run 29941194620](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29941194620) 与
  [push run 29941191361](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29941191361)。
  初始 CI 失败是测试的 real-time watchdog 竞争和 macOS fake CLI 路径的 symlink 规范化问题；`69b46dd`/`6c66dc0`
  只修正测试确定性，不放宽 production reparse-point 拒绝策略。仅文档验证记录
  [`a577031`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/a577031) 的
  [PR run 29942329034](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29942329034) 与
  [push run 29942326396](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29942326396) 再次通过。
  本轮受管 runtime 提交 [`224e06f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/224e06f9cbb1d2ab0cc2260fb244b1f74cd7dfbc)
  的精确 CI 证据见上文；这不替代后续文档 head 的 CI 或真实设备 Gate。
  Draft PR 未获合并授权，真实设备 Gate 未完成；任何后续新 head 都须重新核验，因此不能报告为发布完成。

## 关联资料

- [W18 权威计划](../windows_development_plan.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
- [ADR-W08：包与升级边界](../adr/ADR-W08-packaging-upgrade.md)
- [W18 受管中文 STT runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
