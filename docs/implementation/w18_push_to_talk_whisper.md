# W18：Push-to-talk、麦克风 ring buffer 与 Whisper Job

## 状态与范围

> 状态：实现及 CI 修复提交 [`439fa88`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/439fa88)、
> [`69b46dd`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/69b46dd) 和
> [`6c66dc0`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/6c66dc0) 已推送。功能 head 的
> [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29941194620) 与
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29941191361) 均通过。
> 仅文档验证记录 [`a577031`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/a577031) 的
> [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29942329034) 与
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29942326396) 也均通过。
> Draft PR [#31](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/31) 未合并；后续新 head 必须独立核验。
> 最后本地核验：2026-07-23（Asia/Shanghai），分支 `codex/w18-ptt-whisper`，基线为 W17 合并提交
> [`351da92`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/351da92bfd0232ce03a90a97b75c13ba8ee6a51b)。
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
| 本地转写 | helper 内 `WhisperCppRunner` | 无 shell argv、无内容 stdout/stderr；预检普通文件/重解析点、架构、可执行 SHA-256、模型采样指纹和 `--version` |
| 进程终止 | runner + W12 Job Object | 取消/超时先 terminate、等待 grace、再 kill；W12 Job Object `KILL_ON_JOB_CLOSE` 兜底收束 CLI 子树 |
| 临时文件 | MediaWorker 每次录音私有目录 | PCM 仅 ring；WAV/JSON 不跨 pipe。成功、失败、取消、watchdog 和 shutdown 都删除；删除未知时返回 `stt_temp_cleanup_pending`，不泄露成功 transcript |
| 结果回流 | typed helper payload → UI/backend | 只接受有界 text/language/segment_count；成功时恰好创建一条 `InputMode.voice` `UserMessage`，不传 raw audio |

`desktop_client/inputs/whisper_cpp.py` 的旧父进程实现已移除。`tools/stt_smoke.py` 也只提供 worker preflight 或显式
麦克风 PTT 路径，不提供任意本地文件转写入口。

## 自动化验收与证据

| 计划断言 | 已自动验证的证据 | 边界 |
| --- | --- | --- |
| callback 不逐帧向 loop 排队，overflow/device 事件明确 | `test_media_voice.py` 断言预分配 ring、无 `asyncio.Queue` / `call_soon_threadsafe`，并注入 overflow、status、非连续 frame | fake callback 不代表实际 PortAudio driver 时序 |
| start 才开设备，stop 生成一条 voice 消息 | worker test 断言 preflight 前没有 input stream；`test_w18_voice_ptt.py` 断言一次 stop 只进入一条 `InputMode.voice` `UserMessage` | headless Qt 不代表真实按钮手感或 IME |
| 120 秒、cancel、关闭与临时清理 | watchdog、start/cancel 竞争、empty/failure/cancel WAV、registry lease 和 cleanup-pending 分支均由合成测试覆盖 | best-effort wipe/删除不等于 SSD 物理擦除 |
| whisper runtime 安全预检与受控终止 | `test_whisper_cpp.py` 覆盖路径、架构、hash/fingerprint、version、中文空格路径、malformed/oversize JSON、timeout、cancel、terminate→kill | fake CLI 不能证明真实模型准确率 |
| helper 子进程树 | Windows-only `test_media_worker_job_closure_reaps_whisper_version_probe_child_tree` 启动 synthetic child，关闭 `WorkerSupervisor` 后检查 child PID 已消失 | Job Object 证明可控 synthetic tree；真实 driver/native 行为仍需设备 Gate |
| 配置与入口安全 | STT provider/language/device 边界、entrypoint 三参数原子性和 parent 无 raw-audio import 均由 unit test 覆盖 | 用户提供的真实模型/可执行文件尚未配置或验收 |

最终本地命令：

- `uv run pytest` → 最后一次完整重跑 `1195 passed, 3 skipped in 190.60s`，coverage `90.18%`。
- `uv run pytest --no-cov tests/unit/test_media_voice.py tests/unit/test_whisper_cpp.py` → `56 passed in 7.88s`；余下
  fixture 规范化后再次执行 `tests/unit/test_whisper_cpp.py` → `24 passed in 4.84s`。

完整测试的 skip 是可选 RapidOCR、可选 Pillow 和当前账户的 directory-symlink 权限限制，pytest 已明确标记；没有 W18
断言失败。提交前一次全仓运行曾单独失败既有的
`test_successful_handshake_job_and_orderly_shutdown`：随后该单测连续 20 次、完整 WorkerSupervisor 子套件 45 项、最终全仓
重跑和功能 head 跨 OS CI 都通过。失败根因仍**未验证**，故将其作为残余时序风险而不是报告为已修复。Ruff、格式、mypy、
`uv lock --check` 和 `git diff --check` 已在当前工作树通过。

## 人工 Gate、未验证项与范围外

AI 已覆盖可合成的 ring、worker lifecycle、进程树、路径、取消、超时、清理和 Qt command/event 断言。以下剩余项无法由
当前 fake/headless 条件忠实证明，且需要在 W18 exact PR head 上记录：

1. **麦克风授权、中文准确率与延迟。** 在实际 Windows 麦克风上，以可公开的无敏感短句按住、释放；通过标准是系统授权语义
   清楚、无授权时稳定失败、有授权时只在按住期间采集、转写内容/延迟符合所有者体验判断。不得把录音或转写正文写入仓库。
2. **设备灯/系统录音指示与桌面 UI。** 观察按住前无指示、按住期间有对应指示、释放/取消/关闭后消失，且按钮状态与实际一致。
   这验证真实 OS/hardware UX，不被 fake stream 覆盖。
3. **真实 IME、高 DPI 和焦点交互。** 在实际 Qt 桌面窗口确认鼠标按住/释放、焦点丢失和中文输入候选不会造成意外录音。
4. **锁屏后不录音。** 未验证：当前没有 W20 lock adapter，不能以 focus-cancel 模拟声称真实锁屏通过。

全局 hotkey 没有在此阶段注册；“hotkey 冲突/按键丢失”因此没有通过或失败结论。计划明确采用按钮优先、hotkey 后启用，
后续接入时必须单独提供注册冲突、key-up 丢失、focus/lock/shutdown 收束的自动化和真实 Windows 证据。

产品仍限单机、单 Windows 用户、个人私用。RDP、快速切换用户、跨 session 与跨用户 SID/DACL 有效访问均为范围外，
不得写作已通过，也不列为本 W18 人工 Gate。

## 回滚、残余风险与发布状态

- 将 `stt.enabled` 设为 `false` 即可关闭 PTT、麦克风和 whisper helper；文字输入保持可用。不得以持续监听、同进程
  native 调用或任意文件转写作为回退替代。
- 真正的 PortAudio/driver 卡死无法由 Python task cancellation 证明停止；安全收束依赖 W12 的 helper Job kill。删除/wipe
  是生命周期隔离和 best effort，不构成物理介质擦除承诺。
- 真实模型文件、可执行文件、麦克风权限和性能仍是用户环境依赖。运行时预检以稳定错误码失败，不复制/上传模型或音频。
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
  Draft PR 未获合并授权，真实设备 Gate 未完成；任何后续新 head 都须重新核验，因此不能报告为发布完成。

## 关联资料

- [W18 权威计划](../windows_development_plan.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
