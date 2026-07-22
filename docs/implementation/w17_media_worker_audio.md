# W17：MediaWorker 播放、输出设备与 Gate A

## 状态与范围

> 状态：初始实现提交 [`3d76b0b`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/3d76b0bc31214dfbd7f8423b287d096182629b8a)
> 已推送；[Draft PR #30](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/30) 已以
> `agent/windows-development-baseline` 为 base 创建。最后本地核验：2026-07-22（Asia/Shanghai）。基线为
> `agent/windows-development-baseline` 的
> `5df2fb2ad9c4402b674dfff7138c30880ac2c853`；工作分支为
> `codex/w17-media-worker-audio`。
>
> 本记录只描述当前工作树和已运行命令的事实。模拟设备/stream、headless Qt 与 `--dry-run` 不能冒充内置、USB
> 或蓝牙硬件的真实播放体验；远端 CI 和 Draft PR 在创建前也不写作已通过。

W17 落实 Windows 开发计划中 W4 的第一个 PR：将输出播放从 BackendThread 移入受 W12 监管的
`MediaWorker`，提供设备枚举/选择、有限的 latency fallback、热插拔降级和 Gate A interrupt dry-run。
它不实现 W18 的麦克风、push-to-talk 或 whisper；不实现 W19 的真实 TTS/VTS preflight；不改变 W12 的通用
helper protocol、Job Object 或资源授权模型。

## 已实现的边界与契约

| 项目 | 实现与唯一 owner | 失败/上限语义 |
| --- | --- | --- |
| PortAudio / `sounddevice` | 仅 `app/media/worker.py` 的 helper-side `SoundDeviceBackend` 导入和调用；父侧 `app.media` 不导入 native binding | worker 不可用、deadline 或协议失败映射为稳定 `AudioPlaybackResult`；文本继续 |
| 主进程→worker 的 WAV authority | 父侧仅把已批准根下的路径变成 `ResourceReference(root_id, relative_path)`；W12 policy 在 helper 打开并校验 descriptor | 不发送绝对/任意路径、PCM 或 WAV body；根外路径本地拒绝为 `audio_path_unapproved` |
| WAV 读取 | worker 从已批准 descriptor `dup` 出私有 fd 并 seek 到开头，使用 `wave` 解析受限 PCM | 不支持压缩/异常 header、短读或超过 32 MiB 的资产返回稳定错误码；descriptor 生命周期仍由 helper 资源 finally 收口 |
| 输出设备选择 | `MediaWorkerHandler` 枚举 PortAudio output，排除重复 stable ID；选择保存 ID，否则 default/首个可选 | 选定设备缺失时释放旧 stream、回退并报告 `audio_device_fallback`；无可选设备为 `audio_device_unavailable` |
| stream / latency | MediaWorker 持有一条匹配 format/device 的 stream；先 low，失败时只尝试一次 high，并复用成功 high stream | 两次都失败返回 `audio_device_unavailable`，不循环 retry；write 丢失会 drop/close 并报告 `audio_device_lost` |
| cancel / close | 父侧 `stop()` 请求 `media.cancel`，worker abort/close stream；W12 supervisor 在 deadline/cancel 失效时拥有 Job kill | 同一原生线程内的卡死不可被 Python 强制杀死；hard settlement 依赖 W12 helper process，而非宣称 `to_thread` 已停止 |
| 设备设置 | `output_device_id` 加入 `PipelineConfig` 和用户层 YAML；设置对话框显式刷新设备、保存 ID、显式启用 `playback_mode=system` | 默认 `playback_mode=silent`；不可用已保存 ID 不被静默改写，UI 提醒 fallback |

设备 ID 是 `host API + device name + max output channels + default sample rate` 的版本化 SHA-256 截断值。它的
目的是避免 PortAudio index 重排，不是 Windows endpoint GUID。相同指纹被刻意视为不可选择，以避免错误地把
一个设备当成另一个；驱动更名、采样率变化或碰撞仍可能导致 fallback，必须在真实设备 Gate 中确认。

父侧 job 仅有 `media.devices`、`media.play`、`media.release`。device list 只包含最多 16 个
`device_id` / clean label / default marker；主进程没有 native index。播放的成功、skip、取消与 fallback 都被
翻译为无内容的状态/错误码。`DialoguePipeline` 对音频 failure 发出降级或 skip 事件而非让整轮文字失败。

## Gate A 与设置接入

- `tools/gate_a_review.py` 的真实播放模式改用 `MediaWorkerAudioPlayer.for_review()` 和临时目录；不再创建旧的
  in-process `SystemAudioPlayer`。
- `--dry-run` 使用 realtime `SilentAudioPlayer`，能重复验证 pause/interrupt 的顺序而不打开设备或产生声音。
- 2026-07-22 的首次真实 Gate A 运行暴露了 review-only deadline 不匹配：`for_review()` 的 supervisor 上限为
  30 秒，而通用 player 固定提交 125 秒，故 supervisor 在 `media.play` 发出前拒绝该 job。review player 现使用
  25 秒 deadline；同时工具会输出 `audio.degraded` / `playback.skipped` 的无内容错误码，并在 Day 6 未完成全部
  三段时失败退出，避免把“已开始”误报为已播放。
- 修正 response factory、订阅/取消 API 以及 TTS deadline 参数，使工具可执行而不是依赖错误的
  `ChatRequest.input_mode` 前提。
- Qt 设置合约新增有界的 audio device command/event，管理 runtime 只在显式刷新命令时启动临时 MediaWorker。
  因此普通设置快照刷新不会隐式枚举或占用硬件。
- 枚举 worker/protocol 失败统一返回 `audio_device_enumeration_failed`，设置页据此显示无法读取设备和下次播放的
  default fallback，而不是把失败伪装成正常的空设备列表。

## 自动化证据

- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：`236 files already formatted`；`uv lock --check` 与 `git diff --check`：通过。
- `uv run mypy`：`Success: no issues found in 229 source files`。
- `uv run pytest`：`1154 passed, 3 skipped in 170.51s`，coverage `90.43%`（项目门槛为 90%）。skip 是 optional
  RapidOCR、Pillow 和当前账户目录 symlink 能力，均由测试明确标记。
- W17 的定向验证包括 `tests/unit/test_media_worker.py`、`tests/unit/test_media_entrypoint.py`、
  `tests/unit/test_gate_a_review.py` 和 `tests/unit/ui/test_w17_media_settings.py`，并回归
  bootstrap、mock pipeline、W07 bounded pipeline、W08 provider semantics 与 W16 management。最近 W17
  media/entrypoint 定向集合为 `15 passed in 3.33s`；局部 coverage 为 90%（client 88%、worker 89%、entrypoint
  96%）。
- deadline 修复后的 W17 相关定向集合为 `33 passed in 12.42s`；其中 `tests/unit/test_media_worker.py` 与
  `tests/unit/test_gate_a_review.py` 覆盖 review deadline 小于 supervisor 上限及 `playback.skipped` 错误码输出，
  且该集合在无真实设备条件下通过。随后以实际 MediaWorker 运行
  `uv run python tools/gate_a_review.py --mode all --volume 0`：Day 6 三段均完成、Day 7 旧轮次取消后新轮次完成；
  该静音探针不等同于真实可听的设备 Gate。
- 该集合模拟并断言：索引重排/重复 ID、设备消失、低延迟占用→high fallback、high stream reuse、write lost、
  cancel/abort、无设备、无效/超大 WAV、helper device/list/release protocol、根相对 descriptor、deadline/hang
  映射、lease 后文件删除、UI 保存/不可用选择和 Gate A dry-run。
- 初次全仓 pytest 没有断言失败但 coverage 为 `89.43%`，低于门槛，故明确判定失败；随后补充上述真实控制流
  测试，并修复发现的 high stream reuse 与 generic worker exception 降级问题。最终全仓重跑才获得 90.43%。

上述是 fake/合成 WAV/受控 fake backend 的证据，不包含真实音频、角色素材、用户路径、secret 或 token。

## 未验证项与人工 Gate

以下是 AI 无法忠实自动化的真实 Windows 设备体验，仍需在 exact PR head 上人工确认：

1. 内置输出、USB 输出和蓝牙输出分别播放正常结束的短/长 WAV，确认无尾音截断、爆音或异常延迟。
2. 三类设备上播放中 interrupt，确认声音停止、字幕/新 turn 不补播或重叠。
3. 正在播放时拔出/重连 USB 或蓝牙设备，确认实际 driver 行为与 UI 的 fallback 提示一致，且退出不残留设备占用。
4. 在真实 native write/driver 异常下确认 W12 Job 终止后的 UX；自动化只能证明 fake write 和 supervisor 代码路径。

RDP、快速切用户、跨 session 与跨用户 DACL 有效访问为当前单机单用户私人范围的**范围外**项；它们没有被标记为
通过，也不列为本 W17 的人工 Gate。

## 回滚与残余风险

- 回滚开关为 `pipeline.playback_mode: silent`；文字对话继续。无需删除用户设置或 cache，也不得通过无限 retry
  掩盖设备错误。
- 设置选择可以留空以让 worker 使用默认设备；保留无法枚举的旧 ID 是为了诚实呈现 fallback，而非证明设备仍存在。
- `sounddevice` / PortAudio / 驱动的真实行为会受设备和环境影响。worker 隔离降低了 BackendThread 卡死风险，但
  不承诺物理内存/磁盘擦除，也不证明所有 native driver 都可正常 abort。
- 因为身份不是 endpoint GUID，重复/变更指纹会降级到默认设备。这一选择优先避免错误设备选择，代价是需要用户在
  设备变化后重新确认选择。

## 发布状态

W17 的初始聚焦提交、推送和 Draft PR 已完成。远端 CI 与上列真实设备 Gate 尚未发生；它们必须在最终 exact
head 上补记，不能继承 W16、初始实现提交或更早 PR 的结果。
