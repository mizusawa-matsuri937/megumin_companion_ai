# 当前产品目标

> 最后核验：2026-07-22（Asia/Shanghai）。用户已明确授权 W17。当前分支
> `codex/w17-media-worker-audio` 从 `agent/windows-development-baseline` 的
> `5df2fb2ad9c4402b674dfff7138c30880ac2c853` 开始；W17 的初始实现提交为
> [`3d76b0b`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/3d76b0bc31214dfbd7f8423b287d096182629b8a)，
> Gate A 打断竞态修复为 [`8ff9070`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/8ff9070d3e7d2cd9203787612918e69edffadef8)。
> [Draft PR #30](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/30) 已以
> `agent/windows-development-baseline` 为 base 创建。Windows 时序测试稳定化提交
> [`550671d`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/550671dba9a08b552ed3d79214f533139487ebb6)
> 已在该 exact code head 的 push 与 pull-request 工作流中通过；真实声卡体验仍尚未核验，不能写成已完成。

## 已确认事实

- W16 已合并到开发基线；用户随后明确授权开始 W17。该最新授权覆盖旧快照中“尚未授权 W17”的状态。
- W17 把 PortAudio 播放移到受 W12 `WorkerSupervisor` / Job Object 监管的 `MediaWorker`。主进程与
  `BackendThread` 不导入或调用 `sounddevice`；它们只提交批准根下的相对 `ResourceReference`，不会把任意
  文件路径、WAV 内容或 PCM 传入 helper protocol。
- 输出设备身份为 host API、名称、最大输出通道数和默认采样率的 SHA-256 截断指纹。它能抵抗通常的 PortAudio
  index 重排，但**不是** Windows endpoint GUID；发生相同指纹的重复设备会被拒绝选择并回退。这是已知设计限制，
  不是已证明的跨驱动永久身份。
- 选定设备消失时，worker 停止/释放旧 stream，使用默认（或首个可选）设备并返回稳定降级状态；low-latency
  打开失败只尝试一次 high-latency fallback，并复用成功的 high stream，不无限重试。播放失败只使音频降级，
  文本流程继续。
- 默认仍为 `playback_mode: silent`。设置界面只在用户显式点击刷新时枚举输出设备；用户可保存设备 ID 并显式
  启用本地系统播放。保存但目前不可用的设备 ID 会保留，以便提示实际 fallback，而不是悄悄改写用户选择。
- `tools/gate_a_review.py --dry-run` 使用 realtime silent player 覆盖 interrupt 路径；真实复核模式才启动临时
  MediaWorker，且临时 WAV 位于临时目录，不进入仓库或持久 cache。
- 2026-07-22 的实际 Gate A 打断日志曾出现旧轮次 `turn.failed`、新轮次首段
  `playback.skipped code=audio_worker_failed`，并在 `assistant.completed` 后没有回到 PowerShell 提示符。该次结果是
  **失败**，不能以新轮次第二段完成或文本 completed 伪装为通过。代码审计确认两个可复现的竞态：caller cancel 会把
  原 job deadline 作为 hard-fault 等待上限，且 crash recovery 与新的 `start()` 可并发 spawn。已将 caller cancel 的
  hard-fault 等待收紧为 supervisor `terminate_wait_seconds`，让新 `start()` join 现有 recovery，并使已明确取消的 turn
  在 cleanup error 竞争时仍以 `turn.cancelled` 收束。
- Gate A Day 7 现在直接打印 `turn.failed` 的稳定 error code，要求旧轮次 `turn.cancelled`、新轮次两段均
  `playback.finished` 且不存在 `playback.skipped`，并显式输出 MediaWorker 清理开始/完成。任一不满足均为非零失败，
  不再把 `assistant.completed` 单独当作人工 Gate 成功。
- 远端 `push` run `29904002937` 在 exact head `8ff9070` 的 Windows quality 中，仅在
  `test_hanging_job_hits_hard_deadline_and_terminates_entire_fake_job` 失败：固定等待 30 ms 后状态仍为 `failed`，
  尚未由异步 process watcher 变为 `quarantined`。同一 SHA 的 `pull_request` run `29904004777` 的 Windows 和 macOS
  quality、两项 installed-wheel 均通过。该对照支持“固定 sleep 的测试同步不足”的判断，但不把一次通过当作新 head 的 CI 结果。
- 测试稳定化提交 `550671d` 的 exact code head 已由 `push` run
  [`29905046336`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29905046336) 和 `pull_request` run
  [`29905048854`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29905048854) 分别核验：两组 Windows/macOS
  quality 及 installed-wheel 全部通过。它证明该代码 head 的 CI，不代替任何未来变更 head 的检查或真实声卡 Gate。

## 本地自动化证据

- `uv run ruff check .`：通过。
- `uv run mypy`：通过，`229 source files`。
- 最新 `uv run pytest`：`1158 passed, 3 skipped in 157.15s`，总 coverage `90.43%`，达到项目 90% 门槛。
  三项 skip 分别是未安装的可选 RapidOCR、Pillow，以及当前账户不能创建目录 symlink；均有 pytest 明确标记，
  不是 W17 断言失败。
- W17 定向套件（`test_media_worker.py`、`test_media_entrypoint.py`、`test_gate_a_review.py`、W17/W16 UI 及
  pipeline 回归）覆盖模拟设备顺序变化、重复身份、选定设备消失、低/高延迟 fallback、device-lost、取消、
  helper deadline/hang 收敛、root-relative descriptor、WAV lease 清理、设置保存与 Gate A dry-run。
- 2026-07-22 的真实 Gate A 首次运行发现 review player 向最大 job 时限为 30 秒的 supervisor 提交了固定
  125 秒 deadline，因此在提交播放前被拒绝为 `worker_job_deadline_invalid`；这不是声卡或 WAV 格式失败。已将
  review deadline 收紧为 25 秒，并让工具打印 `playback.skipped`/降级码且在 Day 6 三段未全部完成时以非零退出。
  修复后的 `uv run python tools/gate_a_review.py --mode all --volume 0` 以实际 MediaWorker 路径退出 0：Day 6
  的三段均 `playback.finished`、`playback_count=3`，Day 7 旧轮次被取消且新轮次两段完成。音量为 0 的探针只证明
  控制/资源/worker 路径，不证明人耳实际听感。
- 上述打断异常修复后的定向回归
  `uv run pytest --no-cov -q tests/unit/test_worker_supervisor.py tests/unit/test_turn_service.py tests/unit/test_gate_a_review.py tests/unit/test_media_worker.py tests/unit/test_media_entrypoint.py tests/integration/test_mock_pipeline.py tests/integration/test_w07_bounded_pipeline.py tests/integration/test_w08_provider_semantics.py`
  → `104 passed in 16.23s`。它覆盖 caller cancel 的强杀时间上限、crash recovery 与新 start 的串行化、取消胜过
  cleanup error，以及 Gate A 对 replacement playback skip 的拒绝。
- 修复后实际 MediaWorker 的 `uv run python tools/gate_a_review.py --mode interrupt --volume 0` 正常打印
  `turn.cancelled`、两段新轮次 `playback.finished` 与 `Gate A 清理完成。`；随后连续 3 次
  `--mode all --volume 0` 均以相同控制/清理顺序退出 0。该结果没有复现原问题，但只证明本机静音控制路径，不能证明
  每一种真实 driver 卡死或可听体验。
- 实施中第一次完整 pytest 没有断言失败，但 coverage 为 `89.43%`，因此没有被接受为通过。随后补充了原生适配器、
  helper entrypoint、失败降级和 high-latency stream reuse 的有意义模拟路径；最终完整重跑才达到上述 90.43%。
- 针对上述 Windows CI 失败，测试不再猜测 30 ms 内应完成状态转换，而是在 0.3 秒上限内轮询最终 `quarantined` 状态。
  该单测连续 20 次通过，完整 `test_worker_supervisor.py` 为 `45 passed in 2.02s`；`ruff format --check .`、`ruff check .`、
  `mypy`、`uv lock --check` 和 `git diff --check` 均通过。它只稳定测试同步，不改变 MediaWorker 产品逻辑。

## 未验证项、人工 Gate 与范围外

- 自动化 fake/headless 结果只证明模拟条件，不能证明真实 Windows 音频硬件。仍需在内置声卡、USB 和蓝牙设备上
  人工确认：正常结束无尾音截断、interrupt 后不补播/不重叠、设备热插拔时的实际听感和 UI 提示、以及退出时的
  原生驱动资源释放。
- W12 已提供 worker 的 Job Object hard-kill 机制；W17 的 fake 覆盖可中止 write 与 supervisor deadline 路径。
  这不能证明每一种真实 PortAudio/驱动卡死都能在同一线程内被中止；真正卡死仍依赖 supervisor 终止 helper。
- 项目范围仍为单机、单 Windows 用户、个人私用。RDP、快速切用户、跨 session 和跨用户 DACL 有效访问均为
  **范围外**；不得写成 W17 已通过，也不得作为本任务新增人工 Gate。
- 未向仓库、fixture、日志或文档加入真实 WAV、用户路径、令牌、密钥或受保护角色资产。

## 回滚与下一步

- 将 `playback_mode` 设为 `silent` 即可关闭本地播放；文字对话继续。无法恢复或异常设备不应触发无限重试。
- Draft PR #30 保持 Draft。W17 可执行代码的测试稳定化 head `550671d` 已完成远端 CI；在最终将要审计的任何新 head 上仍须
  重新核验检查，随后在该 head 上完成上列真实设备 Gate。不得继承 `8ff9070` 或更早 head 的 CI 结果。

## 相关资料

- [W17 实现记录](../implementation/w17_media_worker_audio.md)
- [W17 权威计划](../windows_development_plan.md)
- [ADR-W01：运行拓扑](../adr/ADR-W01-runtime-topology.md)
- [ADR-W06：有界流水线](../adr/ADR-W06-bounded-pipeline.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
