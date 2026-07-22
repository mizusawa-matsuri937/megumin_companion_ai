# 当前产品目标

> 最后核验：2026-07-22（Asia/Shanghai）。当前活跃任务是 W18「Push-to-talk、麦克风 ring buffer 与
> whisper Job」。工作分支为 `codex/w18-ptt-whisper`，基线为 W17 合并提交
> [`351da92`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/351da92bfd0232ce03a90a97b75c13ba8ee6a51b)。
> W18 已完成本地实现和自动化验证，尚未创建 W18 提交、推送或 Draft PR；因此不能写作“已交付”或“已合并”。

## 已确认事实

- W17 的 PR [#30](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/30) 已在 2026-07-22 合并到
  `agent/windows-development-baseline`，实际 merge commit 是 `351da92`。旧的 W17 “Draft PR”表述属于历史状态，不能
  覆盖当前基线。
- W18 将本地 PTT 的 native 音频和 whisper.cpp 调用限制在受 W12 `WorkerSupervisor` / Job Object 监管的
  `MediaWorker`。父进程只保留状态、临时目录租约和有界转写元数据；不会接收 PCM、WAV、whisper JSON 或 subprocess
  stdout/stderr。
- `MediaWorkerHandler` 使用 16 kHz、mono、int16 的预分配有界 ring。callback 不创建逐帧 `asyncio.Queue` 或
  `call_soon_threadsafe`；溢出、PortAudio status、超时都映射为无内容的稳定 error code。录音最长 120 秒，默认没有
  持续监听。
- `WhisperCppRunner` 在 helper 内预检可执行文件和模型：常规文件/重解析点、架构、可执行 SHA-256、模型采样指纹和
  `--version`；运行使用无 shell 的 argv、无控制台窗口、超时或取消时 terminate→grace→kill。W12 的 Job Object 负责
  最终子进程树收束。
- PTT 按钮按下时才打开输入设备，释放后只产生一条 `InputMode.voice` 的 `UserMessage`。焦点丢失和窗口关闭会发送
  取消，不会让录音继续。全局热键尚未启用：这是计划中“UI 按钮先实现，global hotkey 后启用”的明确分阶段选择。
- 每次录音的 PCM/WAV/JSON 位于 MediaWorker 私有目录；正常、失败、取消和 parent watchdog 都走删除/registry 清理。
  清理不能确认时不会把转写报告为成功。

## 本地自动化证据

- `uv run pytest`：最终重跑为 `1195 passed, 3 skipped in 166.48s`，总 coverage `90.18%`，达到项目 90% 硬门槛。三个 skip 分别为
  未安装的可选 RapidOCR、Pillow，以及当前账户不能创建目录 symlink；pytest 已明确标记，均非 W18 断言失败。
- W18 定向集：`uv run pytest --no-cov tests/unit/test_whisper_cpp.py tests/unit/test_media_voice.py tests/unit/test_stt_factory.py`
  → `74 passed in 7.90s`。覆盖预检/架构/指纹/版本失败、中文空格路径、转写 timeout/cancel、ring overflow/device status、
  120 秒 watchdog、临时文件清理、start/cancel 竞争、Windows Job Object 子进程树、UI PTT 和焦点取消。
- 当前 exact 工作树的 `uv run ruff check .`、`uv run ruff format --check .`（239 files）、`uv run mypy`
  （232 source files）、`uv lock --check`、`git diff --check` 和 docs 相对链接检查均通过。
- 提交前的一次完整运行曾单独失败 `tests/unit/test_worker_supervisor.py::test_successful_handshake_job_and_orderly_shutdown`；
  随后该单测连续 20 次通过、完整 `test_worker_supervisor.py` 为 `45 passed in 2.14s`，最终全仓重跑也通过。失败原因目前
  **未验证**，不能把这次局部复现不足写作已解决；最终 PR head/CI 仍必须重新执行该套件。
- 测试使用合成 PCM、合成 WAV、fake whisper 和 fake device；没有把真实录音、模型、角色资产、用户路径、token 或 secret
  写入仓库、fixture 或日志。

## 未验证项、人工 Gate 与范围外

- **真实麦克风体验：** 需要在实际 Windows 麦克风上确认授权拒绝/授权后开始、中文准确率与端到端延迟、设备灯或系统录音
  指示。自动化仅证明模拟回调和 helper 生命周期，不能冒充真实硬件或驱动结果。
- **全局热键：** 当前未注册系统级 hotkey，故不存在可诚实报告的冲突、按键丢失或实际释放证据；该能力留待后续启用阶段。
- **锁屏：** 当前没有 W20 的真实 Windows session/lock adapter。焦点丢失和显式取消已自动验证，但“锁屏后不录音”仍未验证，
  不能记为通过。
- **实际桌面 UI/IME：** headless Qt 测试验证 command/event 和可见模型状态；真实高 DPI、IME 候选与按住/释放手感仍须人工
  视觉交互确认。
- 项目范围仍为单机、单 Windows 用户、个人私用。RDP、快速切换用户、跨 session 和跨用户访问为**范围外**，不得写作通过，
  也不转交为本任务人工 Gate。

## 回滚与下一步

- 回滚开关为 `stt.enabled=false`；文字输入继续，且不启动输入设备或 whisper helper。用户设置的模型/可执行路径不会被
  自动复制、上传或写入日志。
- 接下来：审查并创建 W18 聚焦提交，推送 `codex/w18-ptt-whisper` 并建立 Draft PR。PR 创建、最终 head 的远端
  检查和任何真实设备 Gate 发生前，W18 仍不是发布完成状态。

## 相关资料

- [W18 权威计划](../windows_development_plan.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
- [W17 实现记录（已合并的前置任务）](../implementation/w17_media_worker_audio.md)
