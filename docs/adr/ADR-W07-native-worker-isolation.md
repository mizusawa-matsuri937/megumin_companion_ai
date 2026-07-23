# ADR-W07：native worker 隔离、hard timeout 和敏感清理

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任安全/隐私 reviewer）
- **风险：** P0-04、P1-13～P1-16、P1-20、P1-21
- **后续 PR：** W12、W17、W18、W21、W22

## 背景

`asyncio.to_thread` 和 `wait_for` 无法停止卡死的 OCR/PortAudio native 调用；whisper 还可能创建子进程树。等待 drain 可以保护正常清理，却可能让退出或 vision disable 永久阻塞并继续持有截图、PCM 或设备。

## 决策

- `WorkerSupervisor` 使用 `CREATE_NO_WINDOW` 和 Job Object `KILL_ON_JOB_CLOSE` 启动 MediaWorker/PerceptionWorker。
- worker 必须有版本化 handshake、heartbeat、soft cancel、hard deadline、crash budget 和 quarantine。
- soft cancel 失败后允许终止整个 Job Object；终止后运行 temp scavenger 并验证子树归零。
- 原始 PCM、WAV、截图、OCR/raw buffer 只存在于 worker；主进程只接收 transcript 或脱敏摘要。
- helper protocol 不接受任意路径、reparse-point escape、Python object 或未批准 metadata；文件通过批准根或继承 handle 访问。
- worker stderr 使用 1 MiB 无内容诊断 ring；禁止记录 transcript、OCR、窗口标题、图像或用户路径。

## W17 MediaWorker 落地（2026-07-22）

- `media.play` 只接受一个 W12 已授权的 WAV descriptor。父侧把 TTS asset 转为批准根下的相对
  `ResourceReference`；helper policy 负责打开、最终路径复核和 terminal close，handler 只从 descriptor
  复制私有 fd 后读取受限 PCM WAV。
- 设备枚举和播放结果只返回有界 device identity / clean label / stable error code。wire 上不传绝对路径、
  PCM、WAV 内容或 native device index；重复 stable ID 不能被选择，避免碰撞时误播到错误设备。
- cancellation 时 handler abort/drop stream；若 native write 不响应，不能把 Python task cancellation 误称为
  native 停止，仍由 `WorkerSupervisor` 的 deadline / Job Object 终止 helper。自动化覆盖该控制流，真实驱动
  卡死与设备体验仍须人工 Gate。

## W18 PTT / whisper 落地（2026-07-22，本地验证）

- 麦克风 `RawInputStream` 和 whisper.cpp 只在 `MediaWorker` 内打开/启动。父侧 `MediaWorkerVoiceInput` 只发送
  typed job kind，并只接收有界 `text` / `language` / `segment_count`；它不取得 PCM、WAV、JSON、stderr 或 native
  device handle。
- callback 复制到预分配、固定上限的 16 kHz mono int16 ring，禁止每一帧向 parent event loop 排队。overflow、device
  status 和 120 秒上限是稳定 terminal error；ring 在 terminal path 被 wipe，录音 stream 随后关闭。
- `WhisperCppRunner` 在 helper 中复核 regular/reparse-free executable/model/audio/result 路径，记录私有的架构、
  executable SHA-256 与 model fingerprint，并用无 shell、无内容标准流的 `--version` 和实际转写进程运行。超时或取消
  采用 terminate→grace→kill；`WorkerSupervisor` Job Object 仍是 CLI 子树最终收束边界。
- PCM 仅存在 ring；WAV/JSON 仅存在 MediaWorker 私有录音目录。成功、失败、取消、watchdog 与 helper shutdown 都清理目录；
  若清理无法确认，wire 返回无内容错误而非成功 transcript。Windows synthetic 子进程树、中文空格路径、timeout/cancel 和
  cleanup 分支已有自动化证据；真实麦克风/系统指示/锁屏仍不在此处宣称通过。

## W18 受管中文 runtime 补充（2026-07-23，`224e06f` code head 已核验）

- 受管 profile 不改变父/worker 的音频边界：安装发生在用户确认的 UI BackendThread 或显式 CLI 中，之后仍由
  `MediaWorker` 以无 shell argv 启动 `whisper-cli`；安装流程不创建 input stream，也不接收 PCM/WAV/转写。
- 当 settings 正好指向 canonical managed 路径时，父侧仅向 helper command 传固定的 CLI/model SHA-256。helper 在其每个
  生命周期的首次 preflight 前完成完整 hash，缓存只基于文件状态；不匹配即返回 `stt_runtime_integrity_failed`，并在
  `--version` 或实际 CLI 前停止。手工路径保持兼容但明确不享受受管 hash 保证。
- runtime archive/model 仅在 staging 内完成 hash、ZIP/reparse 预检和 `--version` 后才原子切换；失败、取消或 hash 不匹配
  不触碰已验证资产。它不改变 Job Object 对 CLI 子树的终止责任。
- 相关来源、许可证、CVE/issue 的未关闭状态见
  [W18 受管中文 STT runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)。不得把 57 MiB 模型文件当作
  native 峰值工作集证据。

## 云视觉边界

项目所有者允许云视觉，但仅能在指定窗口捕获、本地 Guard、全 OCR bbox 遮挡和最终隐私检查全部成功后，由用户显式启用的路径上传脱敏图像。任一检查未知、错误、超时或 worker 重启都必须跳过上传；不得回退到全屏截图。实际启用仍受 W21/W22 隐私 Gate 约束。

## 备选与取舍

- **可行备选：** 永久关闭真实音频/OCR，只保留文字模式。安全性高但不满足完整产品目标，作为 quarantine/回滚路径。
- **拒绝：** 用 thread timeout 宣称 native 已停止；强杀前没有 owner/temp registry；worker 返回原始截图/PCM 给主进程；无提示捕获其他窗口。

## 失败、恢复和隐私

- crash budget 内可以重启；超过预算后 feature actual state 为 failed/quarantined，用户手动恢复。
- vision 的 disabled 只有在 worker 终止、队列清空、buffer/temp 清理和上传任务取消后成立。
- 杀进程不能证明内存或 SSD 物理擦除；只承诺生命周期隔离、best-effort wipe 和残留扫描。

## 验收

- 注入永久卡死、子进程孙树、文件锁和 crash loop，证明 hard deadline 和 Job 子树归零。
- 敏感 sentinel 图像/文字/PCM 不出现在主进程事件、日志、temp 残留或诊断包。
- 多显示器、DPI、锁屏、RDP、管理员窗口和指定窗口范围由真实 Windows Gate 验证。

## 回滚

强制 `vision=false`、`stt=false`、`playback=silent` 并移除 worker artifact；文字模式继续。不得回滚到同进程卡死线程或未脱敏云上传。
