# ADR-W28：单写者 Avatar Runtime 与实际播放音量口型

- **状态：** 2026-07-29 已批准并在 W28 本地工作树实现；完整质量门、Draft PR 与主观自然度 Gate 待关闭
- **决策者：** 项目所有者（兼任架构、安全/隐私和许可证 reviewer）
- **风险：** P1-03、P1-06、P1-07、P1-13、P2-04
- **关联计划：** [W28 执行计划](../plans/w28_avatar_runtime_execution_plan.md)
- **实现记录：** [W28 Avatar Runtime](../implementation/w28_avatar_runtime.md)

## 背景

W09 的 VTS bridge、W17 的 MediaWorker 播放和现有 emotion event sink 各自具备局部生命周期，但不能安全承担
逐帧参数、主体动作、红眼 Expression、取消和重连的统一所有权。若多个任务直接写 VTS 参数，会产生竞争、
旧 generation 重放、取消后重新张嘴以及人工红眼被系统计时器关闭等错误。原始 PCM 又必须留在 MediaWorker，
父进程不能为口型重新读取 WAV。

## 决策

### 单一 VTS owner

- `AvatarRuntime` 是程序参数、主体动作和自动 Expression 的唯一 VTS writer。
- 参数帧先在本地合成 blink、gaze、breathing、低幅 head、emotion bias 与 `MouthOpen`，再统一检查 finite、
  clamp 并注入。
- 离散动作使用有硬上限的优先队列；高频参数使用 latest-wins mailbox。最多一个参数请求在途，慢 VTS
  只合并旧帧，不追赶历史帧。
- 参数 keepalive、眨眼、平滑、随机目标、重试和效果截止使用可注入 monotonic clock；随机行为使用可注入 RNG。
- 眨眼从首个参数帧起固定每 4 秒执行一次短单眨，不受情绪活跃度或 RNG 影响；随机视线和动作候选保持可注入 RNG。

### generation 与生命周期

- turn、playback、VTS 连接和模型状态分别有 generation。取消、重连、切模和关闭后，旧动作、帧、包络和
  timer 全部失效。
- 每轮冻结一个现有 emotion plan；多个 segment 不重复触发主体动作。
- 同情绪且未取消时继续当前动作/末帧状态；情绪变化严格 release 后再触发新动作；真正 Neutral 只 release。
- 用户取消立即令嘴归零并 release；当前情绪底色保留。取消后同情绪的新一轮可以重新播放动作。
- 当前实现没有受信任的 LLM 结构化 `AvatarTurnPlan`；不能把现有 `EmotionEngine` 冻结冒充该能力。

### MediaWorker 标量边界

- MediaWorker 在按完整 PCM frame 分块写入真实输出流时计算 RMS，经 noise floor、gain 和 attack/release
  转换为 0～1 标量。
- helper `job.progress` 只允许 job 关联、单调 sequence 和 finite `mouth_envelope`。发送侧 latest-wins，
  不阻塞音频线程或饿死 heartbeat、cancel、terminal。
- PCM、WAV body、文本、路径、设备名和声音特征不进入父进程 progress。
- 正常播放在输出流 graceful drain 后才发送 terminal；取消使用 abort/drop。父侧对正常、取消、失败、
  timeout 和 worker crash 都独立强制 terminal zero，不能依赖最后一个 progress 一定送达。

### 红眼所有权

- 会话内状态为 `off | manual | system(deadline, generations)`。
- 系统只在 `off` 时显式开启；系统重触发只续期。人工持有时系统不得接管或关闭。
- 人工 hotkey event 后查询真实 Expression 状态；人工关闭会取消系统 timer，人工开启会取得 `manual` 所有权。
- 启动、重连、切模和正常退出是会话所有权之外的清理边界，均显式关闭。
- event 溢出或状态查询失败时禁用自动控制并保守保留可能的人工状态，不以错误的系统关闭换取一致性。

### 降级与关闭

- 可选参数缺失只禁用对应微动作层；嘴参数缺失只禁用口型。
- release 不可唯一解析时禁用主体动作；人工事件/Expression 状态不可验证时禁用自动红眼。
- Avatar 故障不阻断文字或音频。
- 关闭顺序为拒绝新 callback、嘴归零、release、关闭程序红眼、关闭 VTS；操作幂等。
- 管理面分别显示参数控制、口型、主体动作和自动红眼的 availability 与稳定 reason code，不显示模型名、
  hotkey ID、Expression 文件或路径。

## 备选与取舍

- **拒绝继续扩展旧 bridge 为多 writer：** 无法为参数帧、离散动作和用户人工所有权提供一个线性化 owner。
- **拒绝让父进程读取 WAV：** 违反 MediaWorker 对 PCM/WAV 的隐私和进程边界。
- **拒绝无界 progress FIFO：** 会在声卡或 VTS 变慢时增长，并可能饿死 terminal/heartbeat。
- **拒绝按 TTS 文件时长估算嘴型：** 无法代表真实设备队列、取消和 drain。
- **拒绝新增 NumPy/FFT/viseme 依赖：** 第一版只需整数 PCM RMS；频带比不等于中文音素识别。
- **拒绝用 Toggle 模拟系统 Expression 开关：** 无法保证幂等，也可能关闭人工状态。

## 隐私与安全

- runtime health、UI、日志和测试只允许稳定状态、reason code 与聚合计数；不记录逐帧值。
- 私有模型、动作、Expression、声音、reference、录音、截图、日志、备份、token 和绝对路径不进入仓库、
  fixture、wheel、诊断包或 PR。
- 配置只保存受限语义名称和有界数值；不保存私人 HotkeyID 或模型资产路径。
- VTS token 继续使用 current-user DPAPI；本 ADR 不改变外部 GPT-SoVITS 的信任与安全边界。

## 验收与残余边界

- 自动化必须覆盖 malformed/oversized VTS event、单 writer、latest-wins、慢 VTS、fake clock、seeded RNG、
  动作/Neutral/取消、红眼所有权、progress flood/stale/terminal、8/16/24/32-bit PCM 和 worker 故障。
- 真实 Gate 必须覆盖 VTS 参数、动作生命周期、人工/系统红眼、重连，以及真实 MediaWorker/输出设备的
  silence、固定幅度、ramp、取消和 MouthOpen 归零。
- 真实输出 drain 只能证明 terminal 不早于 PortAudio pending-buffer drain；没有 loopback 录音时不能宣称
  采样级音画同步。
- 当前生效用户设置没有可调用的真实 GPT-SoVITS preset，故现有授权中文 TTS 的实际播放仍未验证。
- 嘴部 gain/attack/release、眨眼、视线、呼吸、头部和中文整体观感保留为所有者主观自然度 Gate。

## 回滚

关闭 Avatar parameter control、lip sync、body motion 和 automatic red-eye；显式令嘴归零并尝试 release。
文字与原有安全音频路径继续。不得回滚为多 writer、父进程 PCM 或可能关闭人工红眼的 Toggle 推断。
