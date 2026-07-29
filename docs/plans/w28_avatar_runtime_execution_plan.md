# W28：Avatar Runtime、程序微动作与音量口型执行计划

> 状态：**产品实现、完整本地质量门与实机验收已完成；发布与自然度 Gate 待关闭**
>
> 最后核验：2026-07-29（Asia/Shanghai）
>
> 适用范围：Windows 单机、单用户、个人私用桌面伴侣
>
> 上位计划：[`../windows_development_plan.md`](../windows_development_plan.md)
>
> 私有补充：精确模型、动作映射、备份与真实 VTS 证据只保存在本地
> `.agents/NEW_CHAT_AVATAR_HANDOFF_2026-07-28.md`，不得复制到 Git、wheel、fixture、日志或诊断包。

## 0. 文档权威、恢复顺序和状态口径

本文件是 W28 的正式、可提交、可长期检索执行计划。它用于防止聊天上下文压缩后丢失范围、顺序、架构边界或
验收标准。它不是实现完成、测试通过、PR 已发布或人工体验 Gate 已通过的证据。

新对话或上下文恢复后，执行者必须按以下顺序建立当前事实：

1. 完整读取根目录 `AGENTS.md`。
2. 完整读取 `.agents/CONTEXT_MEMORY.md`。
3. 完整读取 `.agents/NEW_CHAT_AVATAR_HANDOFF_2026-07-28.md`，尤其第 16 节；不要重问其中已经最终确认的问题，
   不要恢复第 14 节列出的撤回结论。
4. 运行 `git status --short --branch`，保护无关用户改动。
5. 读取 [`../README.md`](../README.md)、[`../current/CURRENT_GOAL.md`](../current/CURRENT_GOAL.md)、
   [`../standards/AGENT_OPERATING_CONSTRAINTS.md`](../standards/AGENT_OPERATING_CONSTRAINTS.md) 和
   [`../windows_development_plan.md`](../windows_development_plan.md)。
6. 读取相关 ADR、数据流、威胁模型、W09/W17/W19 实现记录、当前代码、测试和远端 PR/CI 状态。
7. 将这些资料与当前工作区交叉核对；用户最新指令和当前机器证据优先于本文中的历史状态。

状态词必须严格区分：

- **已确认：** 用户最终要求、当前代码、当前配置或可复核证据能够证明。
- **计划：** 本文件要求下一步实施，但尚无实现证据。
- **建议初值：** 需要自动化和真实 VTS/音频校准，不能写成固定产品真理。
- **未验证：** 只能在后续真实运行环境或人工体验 Gate 中判断。

## 1. 任务治理、编号和分支

### 1.1 编号

- 现有 `W20`～`W27` 已用于 Windows 状态信号、感知、主动发话、安装和发布，不得复用或重编号。
- 本任务使用 `W28`，表示新增的 Avatar Runtime 闭环。
- W28 因当前产品优先级提前到 W19 之后执行；编号靠后不表示 W20～W27 已完成、取消或重排。
- 如果执行时当前上位计划已合法占用 W28，则使用下一个未占用编号，并同步修改本文、索引、实现记录和
  PR 标题；纯编号冲突不需要重问已确认的产品需求。

### 1.2 W19 隔离

- W19 的配置向导/preflight Draft PR 是独立工作，W28 不得混入其分支或 PR。
- 开始 W28 前必须重新查询 W19 是否已合并，不能沿用历史聊天状态。
- 若 W19 已合并，从同步后的正式基线创建 `codex/w28-avatar-runtime`。
- 若 W19 尚未合并且 W28 依赖其 head，则从准确的 W19 head 建立 stacked 分支，并创建以 W19 分支为 base
  的 Draft PR；PR 描述必须披露依赖。
- 本计划授权创建聚焦提交、推送和 Draft PR，不授权合并。

### 1.3 工作树保护

- 根 `AGENTS.md` 的用户改动属于无关改动，必须保留，不暂存、不提交、不回退。
- `.agents/` 是本地运行态和私有证据，必须保留但不得提交。
- 任何模型、纹理、动作文件、Expression、声音权重、参考 WAV、真实对话、截图、录音、token、日志或本机
  绝对私有路径都不得进入 W28 diff、测试 fixture、构建产物或 PR。

## 2. 当前已确认基线

以下只是开始 W28 时必须重新核验的已知基线：

- 14 套本地私有 VTS 测试外观已具备统一的语义释放入口 `MOTION_RELEASE`。
- 逐外观真实 VTS 验证已经证明：该入口可在一个代表主体动作播放期间释放；另有末帧保持释放证据。
- 红眼已经迁移为真正的 VTS Expression，人工入口为持久 Toggle；程序端尚未接入显式
  `ExpressionActivationRequest(active=true/false)`。
- 当前 VTS 外部状态在上一轮结束时为运行、最小化、红眼关闭；这不是未来对话可跳过重新核验的永久事实。
- 产品代码尚未完成：
  - 单一 Avatar Runtime；
  - VTS 参数注入和事件订阅；
  - `MOTION_RELEASE` 的取消/Neutral/情绪切换接线；
  - 红眼人工/系统所有权；
  - MediaWorker 实际播放包络；
  - 程序微动作和音量口型。

精确私有配置、外观名称、动作候选、备份、日志与排除的失败尝试以本地交接文件为准，不在本文重复。

### 2.1 当前实施进度（2026-07-29）

- 已实现单写者 AvatarRuntime、VTS 参数/event/Expression API、程序微动作、整轮动作生命周期、红眼所有权、
  MediaWorker 分块 RMS/`job.progress`、真实输出 drain、设置/管理面与故障隔离。
- 扩展聚焦矩阵为 `267 passed`；最终完整套件为 `1328 passed, 3 skipped`，aggregate branch coverage
  `90.09%`。Ruff、format、strict mypy、lock、diff、正式文档、候选隐私扫描和 CI 同款
  wheel/source-quarantine/installed-artifact smoke 均通过。
- 真实 VTS 已覆盖参数、idle、动作、红眼所有权和重连；真实 MediaWorker/输出设备已覆盖 silence、
  固定幅度、ramp、cancel、output drain 和 MouthOpen 归零。发布前最终 stage/semantic guard 与外部状态恢复
  已完成。
- 当前用户设置没有 GPT-SoVITS preset，默认 loopback 服务也未运行，因此现有授权中文 TTS 的实际播放尚未
  形成证据；不能用合成音替代。
- 当前只冻结现有 EmotionEngine 的整轮结果；受信任 LLM 结构化 `AvatarTurnPlan` 尚未实现。
- 正式证据、客观失败和剩余 Gate 见
  [W28 实现记录](../implementation/w28_avatar_runtime.md)。完成前上游复核未发现依赖选择漂移；focused commit、
  stacked Draft PR 和 exact-head CI 仍待执行。

## 3. 目标和完成定义

W28 要实现一个**单写者 `AvatarRuntime`**。它统一拥有 VTS 参数帧、离散动作和生命周期，接收整轮语义计划与
MediaWorker 的有界音量包络，生成自然、可取消、可降级的程序微动作和嘴部张合。

只有以下四类能力都完成，才能称为 W28 产品实现完成：

1. VTS API 能力和单写者调度；
2. 整轮情绪、主体动作、Neutral、取消和红眼生命周期；
3. MediaWorker 实际播放进度的音量包络；
4. 微动作、口型、自动化、真实 VTS/音频验收、文档和 Draft PR 交付。

不得在只完成 API 基础层、Mock、单个模型或合成音频后停止并宣称整个任务完成。

## 4. 冻结的产品行为

### 4.1 整轮语义

- 一整轮回复只有一个主情绪。
- 主情绪按整轮核心表达意图决定，不按句内最高强度或每个 segment 重选。
- 主情绪分别映射 `voice_slot` 和视觉语义；声音槽不能反向决定主体动作。
- 大模型只应提供整轮主情绪、声音槽、主体动作语义和红眼等离散事件；逐帧值由本地控制器生成。
- 当前 provider 尚无受信任的结构化 Avatar 指令时，W28 先在回复开始冻结现有 `EmotionEngine` 的整轮状态。
  不得解析自然语言中的隐藏标签或让模型直接输出任意 hotkey/参数名。
- 完整的 LLM 结构化 `AvatarTurnPlan` 若未在 W28 实际实现并验证，必须明确列为后续任务。

### 4.2 主体动作

- 有实际音频时，在第一次真实 `playback.started` 触发本轮主体动作一次。
- TTS 失败、静音或无音频时，仍切换主情绪并触发视觉一次，嘴保持闭合。
- 多个 `assistant.segment`、`audio.ready` 或播放片段不能重复触发。
- 相同情绪连续多轮，且动作未被取消：继续当前播放/末帧姿态，不重启、不重新随机。
- 情绪变化：严格先释放旧动作，再触发新动作。
- 真正 Neutral：只释放旧动作，不播放伪 Neutral/Wait 主体动作。
- 用户取消：立即 MouthOpen=0、释放主体动作，保留当前情绪与低强度微表情。
- 取消后若下一轮仍是同一情绪，需要重新播放一次主体动作。
- 动作候选随机选择使用注入 RNG，保持可测试，并避免连续重复。
- 精确私有动作映射从本地交接读取；运行时代码使用受限语义键和当前模型名称解析，不能硬编码私人 HotkeyID。

### 4.3 红眼所有权

会话内状态为：

```text
off | manual | system(deadline, vts_generation, model_generation)
```

规则：

- 系统在 `off` 时触发：显式 `active=true`，进入 `system`，截止时间约为当前 monotonic time + 2 秒。
- 系统持有期间重触发：只延长截止时间，不先关闭、不重开、不重新淡入。
- `manual` 持有时，系统事件不得取得所有权、不得安排系统关闭。
- 收到人工 hotkey 事件后查询 Expression 真实状态：
  - 已关闭：取消系统计时，进入 `off`，本次不得自动重开；
  - 仍开启：进入 `manual`，取消系统所有权。
- 系统计时到期只在 owner 仍为 `system` 且 VTS/model generation 匹配时显式 `active=false`。
- 用户取消回答不取消仍有效的系统红眼计时。
- 启动、VTS 重连、模型切换和正常退出无条件显式 `active=false`。
- 崩溃/强制结束由下次启动清理。
- 不得盲目触发同一个 Toggle hotkey 两次模拟开关。
- 无法可靠订阅人工 hotkey 或查询状态时，程序红眼必须安全禁用；人工入口继续可用。

### 4.4 微动作和微表情

本地控制器逐帧生成：

- 自然眨眼，允许低概率双眨；
- 呼吸；
- 小幅头部角度；
- 眼球移动；
- 低强度眉、眼和嘴角情绪偏置；
- 音量驱动 `MouthOpen`。

行为边界：

- 说话时眨眼、呼吸、轻微眼球和低幅头动继续。
- 说话时视线主要在中心，偶尔小幅扫视；空闲时允许更自由的低幅移动。
- 兴奋类可更活跃；Focused 更克制；悲伤/困倦类更慢、更小。
- 当前情绪持续影响微表情，直到下一次情绪改变。
- 长期 mood 只参与启动时选择 Neutral/Gentle 和主情绪选择；当前情绪确定后不再叠加长期 mood。
- 情绪偏置只作为低强度底色，不自动升级为夸张表情或额外主体动作。
- 情绪切换时主体动作立即切换；微表情平滑插值。
- 温和/低落的过渡较慢，惊讶/兴奋/极度害羞较快；精确时间常数是可配置、待实机校准的值。

### 4.5 口型

- 第一版只做音量包络驱动 `MouthOpen`。
- 不做 MouthForm、FFT 宽/圆嘴、音素或 viseme。
- 包络与语言无关；不能把面向其他语言的启发式写成中文音素识别。
- 音频只驱动嘴，不驱动头、眼、呼吸或身体按音节抖动。
- 包络来自 MediaWorker 实际即将写入输出设备的 PCM 分块，不能按 TTS 文件长度或“已放入播放队列”估算。
- 静音、失败、取消、Worker 崩溃、超时和正常结束都必须保证 MouthOpen 回到 0。

## 5. 冻结的架构边界

### 5.1 单一 AvatarRuntime

`AvatarRuntime` 是唯一 VTS 参数写入 owner：

```text
AvatarTurnPlan ─┐
                ├─> AvatarRuntime
Playback state ─┤      ├─ urgent bounded discrete-action queue
Mouth envelope ─┘      ├─ latest parameter-frame mailbox
                       └─ one VTS connection / one parameter writer
```

- VTSBridge、对话 event sink 和 MediaWorkerAudioPlayer 不得各自创建参数写入循环。
- 每帧先合成 blink/gaze/breath/head/emotion/mouth，再统一 clamp 和发送。
- 离散动作与高频参数帧分通道，不能共用无界 FIFO。

### 5.2 调度和背压

- 离散动作队列必须有硬上限；释放、显式关闭和模型重置优先于普通帧。
- 参数 mailbox 只保留最新帧，容量为 1 或等效 latest-wins。
- 最多一个正在进行的 `InjectParameterDataRequest`。
- VTS 响应慢时合并/丢弃旧帧，不追赶历史帧。
- 初始目标频率可在 20～30 Hz 范围选取，但必须通过测量和真实观感校准。
- keepalive 必须短于 VTS 参数控制失效时间，并遵循官方 API 当前语义。
- 所有值在发送前检查 finite、范围、weight 和参数数量。
- 使用 `mode=set` 时，只有该单写者拥有相关参数；健康运行时按官方语义设置 `faceFound`。

### 5.3 时钟、随机和 generation

- 动画、平滑、眨眼、红眼截止、keepalive 和重试使用注入的 monotonic clock。
- 测试使用 fake clock；不得用现实日期老化 fixture。
- 随机选择、眨眼和视线目标使用可注入 RNG。
- 每个 turn、playback job、VTS 连接和模型状态都有 generation。
- 取消、重连、模型切换和关闭后，旧 generation 的动作、帧、包络和 timer 全部失效。
- 模型切换后不重播旧回复；下一轮同情绪可按“动作已中断”规则重新播放。

### 5.4 Worker 隐私边界

- PCM、WAV 内容和声音特征始终留在 MediaWorker。
- 主进程只接收严格、有限的 `mouth_envelope` 标量进度。
- 进度消息不得包含文本、音频 ID、路径、设备名、模型名、原始样本或任意 metadata。
- progress 通道必须有界、可合并，不能阻塞音频线程，也不能饿死 heartbeat、cancel 或 terminal。
- terminal/cancel/failure 时父进程本地强制 MouthOpen=0，不能依赖最后一条 progress 一定送达。

## 6. 建议领域契约

名称可按当前代码风格调整，但职责不得合并成无类型字典。

### 6.1 `AvatarTurnPlan`

最小字段：

- 内部 turn 关联；
- 主情绪；
- voice slot；
- 主体动作语义键或受限候选集合；
- 微表情 profile；
- 过渡类别；
- 显式效果事件；
- 本轮是否应触发主体动作。

禁止携带完整回复正文、任意 VTS 参数名、任意 hotkey ID 或私人路径。

### 6.2 `AvatarParameterFrame`

- VTS/model generation；
- 有限参数列表；
- monotonic frame 标记；
- 只在聚合指标中统计，不逐帧记录。

### 6.3 `MouthEnvelopeSample`

- playback job/generation 关联；
- 单调 sequence；
- finite 且夹紧到 0～1 的 value。

### 6.4 `AvatarHealthSnapshot`

只允许：

- VTS 连接和授权状态；
- 参数控制、口型、主体动作、红眼自动控制是否可用；
- 稳定错误码；
- sent/coalesced/dropped 等聚合计数。

禁止模型名、路径、HotkeyID、Expression 私有文件信息和逐帧值。

## 7. 实施前开源调研 Gate

修改产品代码前，以及宣称完成前，各执行一次直接相关的开源调研。

至少核对：

1. DenchiSoft/VTubeStudio 官方 API 和官方示例：
   - 参数列表；
   - 参数注入；
   - Expression state/activation；
   - event subscription；
   - `HotkeyTriggeredEvent`；
   - 模型加载/配置事件的准确名称；
   - 频率、keepalive、set/add、weight、faceFound 和优先级。
2. `Genteki/pyvts`：
   - 只用于语义对照；
   - 现有 client 已有 DPAPI、取消和重连审计时，默认不新增依赖。
3. `Alradyin/wallie-V2`：
   - 只参考 RMS、attack/release、眨眼和视线等算法思想；
   - 不复制其 token、日志、多写者架构或未锁定依赖方案。
4. 其他维护活跃的：
   - PortAudio/sounddevice 分块播放；
   - PCM RMS；
   - latest-wins 实时消息；
   - 确定性微动作调度器。

每个候选记录：

- 检索日期和范围；
- 许可证；
- 维护状态；
- 安全公告；
- Windows/Python 兼容性；
- 依赖、包体积和隐私影响；
- 采用、仅参考或拒绝的理由。

默认使用标准库实现 PCM RMS；不得仅为 RMS 引入 NumPy、WebRTC VAD 或新的 Native 依赖。

## 8. 分阶段实施

### A. 基线、代码路由和失败测试

1. 查询当前 Git、W19 PR 和 CI；记录准确 base/head。
2. 用 `rg` 建立 VTS、emotion、dialogue、MediaWorker、worker protocol、settings、UI、health 和 shutdown
   的影响清单。
3. 运行当前相关测试，记录修改前基线。
4. 先写能暴露现有差距的测试：
   - 无 `requestID` 的 VTS 事件被忽略；
   - neutral hotkey 与真正 release 语义冲突；
   - 每 segment 重复动作；
   - 整段阻塞播放不能提供真实播放包络；
   - cancel/failure 缺少独立 MouthOpen=0 保证。

### B. 扩展 VTS client

实现并严格校验：

- 参数能力查询；
- 参数注入；
- Expression state；
- Expression activation；
- event subscription；
- 经过 schema 校验的 unsolicited events；
- 当前模型 hotkey 的唯一名称解析。

要求：

- 无 `requestID` 的事件走独立 event path，不能伪装成响应。
- 未知、超大、过深、非有限或 shape 错误消息安全拒绝。
- hotkey 缺失/重复给出稳定错误码。
- 私人 ID 不进入日志、UI、fixture 或文档。
- fake VTS server 覆盖并发、乱序、未知响应、事件、重连和 malformed input。

### C. AvatarRuntime 单写者

1. 新建清晰的 avatar runtime/controller 模块，不把全部逻辑继续堆进旧 bridge。
2. 实现：
   - bounded urgent queue；
   - latest-frame mailbox；
   - 单一 in-flight 注入；
   - coalescing；
   - keepalive；
   - idempotent close。
3. 实现 blink、gaze、breathing、head、emotion bias、mouth 分层。
4. 能力缺失按层降级：
   - 可选微动作参数缺失只禁用该层；
   - MouthOpen 缺失只禁用口型；
   - release 缺失/重复时禁用主体动作，避免无法收束；
   - Expression 所有权不可验证时禁用自动红眼。
5. 停止时：
   - 拒绝新 callback；
   - MouthOpen=0；
   - 释放主体动作；
   - 显式关闭红眼；
   - 再断开 VTS。

### D. 整轮计划和动作生命周期

1. 回复开始时冻结主情绪，不再让每个 segment 触发动作。
2. 建立受限 mapper：
   - emotion → voice slot；
   - emotion → body-motion semantic key/candidates；
   - emotion → micro-expression profile；
   - emotion → transition class。
3. 精确私有动作映射从本地交接读取，但产品代码只保存稳定语义键和配置，不保存资产路径。
4. 触发规则：
   - 首次真实播放开始一次；
   - silent/no-audio/failure fallback 一次；
   - 同情绪不重复；
   - 情绪变化 release→new motion；
   - Neutral 只 release；
   - cancel release；
   - cancel 后同情绪下一轮重播。
5. release→new motion 必须通过 fake server 和真实 VTS 验证顺序，不能假定零延迟排队自然正确。

### E. 红眼所有权状态机

1. 使用显式 Expression activation，不调用 Toggle 模拟系统开关。
2. 订阅人工 hotkey，查询真实 Expression state。
3. 用 monotonic timer 和 generation 实现系统 2 秒生命周期。
4. 覆盖 manual/system 交错、续期、人工关闭、取消、重连、切模、关闭、API 失败和 stale timer。
5. 事件能力不足时 fail safe 禁用程序红眼。

### F. Worker `job.progress`

1. 在 helper protocol 中新增精确 `job.progress` schema。
2. 只允许 `mouth_envelope`、单调 sequence、finite 0～1。
3. helper handler 通过非阻塞 emit 写入 latest-wins mailbox。
4. 独立 sender 合并发送；terminal/heartbeat/cancel 优先。
5. supervisor 验证 job、sequence、kind、value；忽略或拒绝 stale/duplicate/late input。
6. progress callback 抛错、flood 或关闭竞争不能泄漏 job 或阻塞 supervisor。
7. MediaWorkerAudioPlayer 将合法 sample 交给同一 AvatarRuntime，并在所有终态强制归零。

### G. 分块播放和 RMS

1. 将一次性 `stream.write(frames)` 改为按完整音频帧边界分块写。
2. 建议初始 chunk 20～40 ms，但保持可配置并按真实设备校准。
3. 保持现有 PCM 播放能力；至少覆盖当前支持的 8/16/24/32-bit 和多声道。
4. 正确处理：
   - 8-bit unsigned centered；
   - 16/24/32-bit little-endian signed；
   - 24-bit 符号扩展；
   - 多声道聚合；
   - 非整块尾部和 frame alignment。
5. RMS → noise floor → gain/软压缩 → attack/release → clamp。
6. 使用实际 chunk duration 计算平滑。
7. 不支持口型分析的旧格式仍应播放，并将 lip-sync 标为 unavailable，不能破坏原有音频。
8. 验证分块播放没有遗漏/重复帧、爆音、取消退化、死锁或错误 terminal 语义。

### H. 配置和管理面

配置必须版本化、向后兼容并使用语义名称：

- parameter control enable；
- micro-motion enable；
- lip-sync enable；
- target tick/keepalive；
- mouth noise floor/gain/attack/release；
- 微动作幅度上限；
- release 和红眼的受限语义名。

要求：

- 不存私人 HotkeyID 或模型路径；
- 旧 `expression_hotkeys` 能迁移或安全兼容；
- UI 至少显示参数控制、口型、主体动作和红眼自动控制是否可用及稳定原因码；
- 高级调优值可先留在严格配置中，但必须有安全开关和必要口型校准入口；
- 配置热切换不能遗留第二个 scheduler。

## 9. 自动化验收矩阵

### 9.1 VTS client

- capability query；
- parameter injection schema；
- Expression state/activation；
- event subscription/dispatch；
- 并发、乱序、未知 request；
- malformed/oversized/non-finite；
- reconnect 和 stale generation。

### 9.2 AvatarRuntime

- fake clock 下的单眨/双眨；
- speaking/idle gaze 边界；
- head/breathing 振幅；
- emotion transition；
- 分层能力降级；
- 单 in-flight；
- latest-wins；
- 离散动作优先；
- 无逐帧日志；
- close 幂等且无任务泄漏。

### 9.3 动作

- 整轮一次；
- 多 segment 不重复；
- 同情绪不重复；
- 情绪变化 release→new；
- Neutral 只 release；
- cancel release；
- cancel 后同情绪重播；
- silent/TTS failure 视觉一次且 MouthOpen=0；
- seeded random/no-repeat；
- 本地交接定义的外观特例；
- 被排除的私有动作语义永不进入自动映射。

### 9.4 红眼

- system on→约 2 秒 off；
- retrigger 只续期；
- manual 阻止系统关闭；
- manual close 取消 timer 且不重开；
- answer cancel 不取消系统红眼；
- startup/reconnect/model-switch/exit 强制 off；
- stale timer 无效；
- API/事件失败安全降级。

### 9.5 Worker protocol

- 合法 progress；
- NaN/Infinity/越界；
- 未知 kind/field；
- duplicate/out-of-order；
- stale job；
- terminal 后迟到；
- callback 异常；
- 高频 flood；
- heartbeat/terminal 不饥饿；
- mailbox/队列不增长。

### 9.6 PCM/RMS

- 8/16/24/32-bit；
- mono/stereo；
- silence/constant/ramp；
- 最大正负值；
- 尾部 chunk；
- normalize/clamp；
- noise floor/attack/release；
- cancel/terminal 归零。

### 9.7 集成和压力

- 正常 TTS/playback；
- TTS 失败；
- silent WAV；
- VTS 缺失/断线；
- Worker crash；
- playback cancel；
- 模型切换；
- TurnService shutdown；
- avatar 故障时文字和音频继续；
- 数万 fake-clock frame；
- 慢 VTS；
- 1000 Hz progress flood；
- 固定 seed 可重复；
- 内存、queue、task 数稳定。

## 10. 真实 VTS 和音频验收

Mock 通过后必须使用真实 VTS、MediaWorker 和实际输出路径验证。私有证据保留在本机，不提交。

### 10.1 开始前

- 只读核验 VTS 进程、当前模型、红眼和私有配置 verifier；
- 建立必要的只读哈希/基线；
- 不为测试覆盖私人配置；
- 若 VTS 出现首次 Allow，只让用户完成该实际点击。

### 10.2 参数与微动作

- 查询实际参数能力，不凭“标准名称”假定所有模型都有；
- Neutral idle 观察眨眼、视线、呼吸和头部；
- speaking 观察中心注视；
- 验证没有高频抖动和无界幅度；
- 验证主体动作覆盖冲突参数时不会产生反向补偿跳变；
- 动作结束/释放后微动作恢复。

### 10.3 动作

- 播放中 release；
- 末帧保持 release；
- release→new motion；
- 同情绪两轮不重复；
- cancel 后同情绪重播；
- Neutral 立即释放；
- 不扩张为“所有主体动作、任意时点、其他 VTS 版本”已通过。

### 10.4 口型

- 合成 silence、固定幅度和 amplitude ramp；
- 现有授权中文 TTS 的实际播放；
- 嘴随声音起止；
- silence 闭合；
- cancel 快速闭合；
- terminal/worker crash/断线不残留；
- 迟到 sample 不重新张嘴；
- 音量不驱动头、眼或身体。

### 10.5 红眼

- 系统约 2 秒关闭；
- 系统续期；
- 人工开启保持；
- 系统不能关闭人工状态；
- 系统期间人工关闭不重开；
- 回答取消不提前关闭；
- 启动、重连、切模、退出清理。

### 10.6 结束恢复

- 检查 VTS/应用日志中的 Error/Exception；
- 检查无逐帧/逐样本和私人信息日志；
- 用本地 verifier 确认私人配置未被意外改写；
- 临时文件为 0；
- 恢复本地交接指定的当前模型、红眼关闭和窗口最小化状态。

人工 Gate 只保留：

- 嘴部 gain/attack/release 的自然度；
- 眨眼、视线、呼吸和头部动作的舒适度；
- 中文说话整体观感。

协议、边界、取消、计时、generation、错误日志和队列有界性必须先由 AI 自动验证。

## 11. 文档和架构同步

实现过程中至少更新：

- [`../current/CURRENT_GOAL.md`](../current/CURRENT_GOAL.md)；
- [`../windows_development_plan.md`](../windows_development_plan.md)；
- `docs/implementation/w28_*.md`；
- [`../architecture/windows_data_flow_inventory.md`](../architecture/windows_data_flow_inventory.md)；
- [`../security/windows_threat_model.md`](../security/windows_threat_model.md)；
- ADR 索引及新的 Avatar Runtime ADR，或明确更新受影响的既有 ADR；
- 必要的所有者决定和 Gate；
- [`../README.md`](../README.md)；
- `.agents/CONTEXT_MEMORY.md`。

Avatar Runtime ADR 至少记录：

- 单 VTS writer；
- urgent queue + latest frame；
- MediaWorker 标量包络边界；
- generation/stale 规则；
- 红眼所有权；
- shutdown 和降级。

计划不能预写成实现证据；实现记录必须绑定准确命令、提交、PR、真实运行证据和未验证项。

## 12. 验证和发布

开始时用 `rg --files` 和 CI 配置确认准确命令，不盲写测试文件名。最低验证范围：

1. 新增/受影响单元测试；
2. VTS fake server；
3. worker protocol/helper/supervisor/media；
4. DialoguePipeline、TurnService、settings、management UI；
5. 完整 pytest 和覆盖率；
6. Ruff lint/format、strict mypy、lock check；
7. Markdown 相对链接；
8. `git diff --check`；
9. wheel/source-quarantine/installed-artifact smoke；
10. artifact/secret/path/private-asset denylist。

提交前：

- 重新执行完成前开源调研；
- 查看 `git status`、完整 diff 和 staged diff；
- 只暂存 W28 文件；
- 确认不含 `AGENTS.md`、`.agents/` 或私人资产；
- 创建聚焦提交；
- 推送并创建/更新 Draft PR；
- 不合并。

完成报告必须包含：

- 分支、base/head 和提交 SHA；
- Draft PR URL；
- 实际范围；
- 每条验证命令和结果；
- 真实 VTS/音频结果；
- 客观失败及修复；
- 未验证项和人工体验 Gate；
- VTS 最终恢复状态；
- 保留的无关用户改动。

## 13. 回滚和失败语义

- VTS 参数层失败：停止该层，文字/音频继续。
- 嘴部失败：MouthOpen=0，音频继续。
- release 能力不可靠：禁用主体动作，避免留下不可释放姿态。
- 红眼所有权不可靠：禁用程序红眼，保留人工入口。
- Worker progress 不可靠：丢弃并归零，不影响播放 terminal。
- 分块播放在真实设备回归：回滚到旧播放实现并保持 lip-sync disabled，不能以损坏音频换取口型。
- 配置迁移失败：保留旧配置和安全关闭状态。
- PR 回滚：VTS/Avatar 功能 disabled，文字和原有安全音频路径继续。

任何失败都要记录稳定错误码和聚合计数，不记录内容、路径或每帧数据。

## 14. 禁止虚报

没有相应证据时不得声称：

- LLM 已输出受信任结构化 AvatarTurnPlan；
- 所有私有外观支持相同参数；
- 一个代表动作证明全部动作和任意时点；
- fake VTS 证明真实 Expression 所有权；
- 合成 WAV 证明真实声卡无问题；
- 自动截图证明用户已认可自然度；
- Draft PR 等于发布或合并；
- 历史绿测证明当前 head 仍通过；
- 私有 hotkey/Expression 部署等于产品生命周期已经接线。

## 15. 最终验收清单

- [x] 当前 Git/W19/W28 base 已重新核验
- [x] 单一 AvatarRuntime 和单一 VTS 参数 writer
- [x] VTS 参数查询、注入、Expression state/activation、事件订阅
- [x] unsolicited events 严格校验
- [x] idle/speaking 微动作 profile
- [x] 音量只驱动 MouthOpen
- [x] PCM 始终留在 MediaWorker
- [x] `job.progress` 严格、有界、latest-wins
- [x] progress flood 不饿死 heartbeat/terminal
- [x] 正常结束、失败、崩溃和取消都归零
- [x] 整轮动作只触发一次
- [x] 同情绪不重播
- [x] 情绪变化先 release
- [x] Neutral 只 release
- [x] cancel 后同情绪可重播
- [x] 红眼 manual/system 所有权符合全部确认规则
- [x] 启动、重连、模型切换和退出清理
- [x] VTS/avatar 故障不阻断文字/音频
- [x] fake clock、seeded RNG、stale generation 测试
- [x] 自动化完整通过或有准确失败说明
- [x] 真实 VTS、MediaWorker 和实际输出播放已验证（真实中文 TTS 仍未配置）
- [x] 私有配置/资产未提交、semantic 未意外改写（exact-byte metadata 差异已如实记录）
- [x] ADR、数据流、威胁模型、计划、实现记录和索引同步
- [ ] 聚焦提交已推送，Draft PR 已创建
- [ ] 未合并
- [x] `.agents/CONTEXT_MEMORY.md` 已更新
