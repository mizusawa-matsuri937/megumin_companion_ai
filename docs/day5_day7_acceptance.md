# Day 5～7 实现与 Gate A 验收记录

> 当前状态：**Gate A 已于 2026-07-12 通过。** 分句、连续有序播放、快速打断、设备释放、临时文件清理和关键并发代码均已完成人工审核。首次发现的句间间隔和尾音截断问题已通过持久低延迟输出流、正常排空关闭与打断立即中止修复，并通过最终复测。

## 1. 已实现范围

### Day 5：Mock LLM streaming 与 Segmenter

- `MockLLMProvider` 支持指定 delta、首 token 延迟、后续 token 延迟、chunk 大小和响应工厂。
- `DialogueSegmenter` 增量处理中文、英文、日文标点，避免在英文缩写和小数中误切。
- 短句低于最小长度时等待下一自然句；“嗯？”等短反应词允许立即输出。
- 超过最大字符数/英文词数时，优先在逗号、顿号、空格等自然位置切分。
- stream 结束时 `flush()` 输出剩余内容；segment index 在整个 turn 内严格递增。

### Day 6：Mock TTS 与有序播放

- `MockTTSProvider` 只生成单声道 16-bit PCM 纯正弦提示音，不含真人声音、角色声音或受保护素材。
- 每个 segment 生成合法 `AudioResult` 和临时 WAV；成功、失败、取消及关闭路径都会清理临时文件。
- TTS worker 可并发工作并故意乱序完成；播放协调器按 segment index 缓存并严格顺序播放。
- 默认配置使用 `silent` 播放器，避免启动后意外发声；人工验收工具才显式打开系统音频播放器。
- `SystemAudioPlayer` 使用 sounddevice 在同一 turn 内复用一个低延迟输出流，避免每句重新启动播放器；正常结束会先排空声卡缓冲区再关闭，用户打断则立即中止，二者都会释放流。

### Day 7：取消、隔离、指标与完整链路

- 每个 turn 使用独立 `CancellationToken`、TTS queue 和 audio queue。
- 新输入先取消并等待旧 turn 完成清理，再发布新 turn 的 `turn.accepted`，形成硬事件屏障。
- HTTP `POST /api/interrupt` 与 WebSocket `turn.cancel` 使用同一取消入口。
- WebSocket 输出 `assistant.delta`、`assistant.segment`、`tts.job`、`audio.ready`、`playback.started`、`playback.finished`、`assistant.completed` 和 `turn.cancelled`。
- 文字与模拟语音只在 `UserMessage.input_mode` 上不同，均进入同一个 `TurnService` 和 `DialoguePipeline`。
- `/debug/state` 只显示 turn id、状态、队列活动和延迟指标，不保存或返回用户/助手正文。
- 已记录 `llm_first_token_ms`、`llm_first_segment_ms`、`tts_first_audio_ms`、`first_sentence_play_ms`、`turn_total_ms`、逐句 TTS 延迟和音频排队延迟。

## 2. 自动化验收证据

在仓库根目录执行：

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

首次人工反馈优化后结果：34 个测试通过；Ruff、格式检查和严格 mypy 均通过。

自动化测试覆盖：

- 中/英/日标点、英文缩写、小数、省略号、短反应词、最大/最小长度和 flush。
- 可控 token 流与取消后不再产生后续 token。
- WAV 声道、采样宽度、采样率与删除行为。
- TTS 完成顺序为乱序时，播放顺序仍为 `0 → 1 → 2`。
- 快速新输入前旧 turn 先产生 `turn.cancelled`，新 turn accepted 后不再出现旧 turn 事件。
- text/voice 经 WebSocket 跑通同一条 token → segment → TTS → playback → completed 链路。
- HTTP interrupt、应用关闭和临时音频清理。
- 相邻 segment 复用同一个音频流，不会为每句话重复打开设备。

## 3. 人工验收步骤

所有命令都只使用仓库内生成的纯提示音和假文本，不需要 API key，也不要放入真实隐私数据。

### A. 阅读分句结果

```powershell
uv run python tools/gate_a_review.py --mode segments
```

请重点判断：

1. `你好。今天一起努力吧！` 合成一句是否自然，还是应该把“你好。”单独播放。
2. `Dr. Smith paid 3.14 dollars.` 是否没有在 `Dr.` 或 `3.14` 中间误切。
3. 日文两句、省略号台词和“嗯？”短反应是否自然；是否存在明显过碎或等待太久。

### B. 实际听取乱序合成后的有序播放

先把系统音量调低，再执行：

```powershell
uv run python tools/gate_a_review.py --mode audio
```

预期结果：控制台的 `audio.ready` index 故意乱序，但实际只听到 3 个不重叠、由低到高的提示音；`playback.started` 必须为 `0、1、2`。命令结束后，其他应用应能正常播放声音。

若默认音量仍偏大，可用：

```powershell
uv run python tools/gate_a_review.py --mode audio --volume 0.04
```

### C. 实际听取快速新输入的打断

```powershell
uv run python tools/gate_a_review.py --mode interrupt
```

预期结果：工具等待较低音的旧 turn 进入播放，再保留约 0.65 秒可闻时间后将其切断；随后只播放两个约 1 秒的较高音。旧低音不能恢复、重叠或在新 turn 完成后补播。建议连续运行 3 次，并确认命令退出后音频设备未被占用。

### D. 检查运行时残留

音频和打断命令都结束后，检查：

```powershell
Get-ChildItem data/cache/audio/gate-a-review -Recurse -Filter *.wav -ErrorAction SilentlyContinue
```

预期没有任何 `.wav` 输出。macOS/Linux 可使用：

```bash
find data/cache/audio/gate-a-review -name '*.wav' -print
```

## 4. 人工结论

- 分句体验：**通过。** 中文短句合并可接受；英文缩写和小数未误切；日文、短反应词、省略号自然，无明显过碎。
- 三段音频：**通过。** 数量、由低到高的顺序、连续性和完整性符合预期；持久输出流消除了明显句间等待，正常关闭会完整排空尾音。
- 快速输入/打断：**通过。** 连续测试 3 次一致；旧低音快速终止且未恢复、未补播，新轮次连续播放两个高音。
- 设备与残留：**通过。** 打断测试后设备正常释放；无残留 WAV。
- text/voice 共用链路及并发/清理代码：**人工同意通过。**
- 最终打断复测：**通过。** 旧 turn 在新 turn accepted 前取消；低音被中止后不恢复、不补播，新 turn 的两个高音按序完成。
- Gate A：**通过并关闭。** 可以进入依赖该主链路的下一阶段。
