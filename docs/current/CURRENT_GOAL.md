# 当前产品目标

> 最后核验：2026-07-22（Asia/Shanghai）。用户已明确授权 W17。当前分支
> `codex/w17-media-worker-audio` 从 `agent/windows-development-baseline` 的
> `5df2fb2ad9c4402b674dfff7138c30880ac2c853` 开始；W17 的本地实现和自动化验证已完成，
> 正在进行提交、推送与 Draft PR 交付。没有把尚未创建的 PR、远端 CI 或真实声卡体验写成已完成。

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

## 本地自动化证据

- `uv run ruff check .`：通过。
- `uv run mypy`：通过，`229 source files`。
- `uv run pytest`：`1153 passed, 3 skipped in 292.39s`，总 coverage `90.43%`，达到项目 90% 门槛。
  三项 skip 分别是未安装的可选 RapidOCR、Pillow，以及当前账户不能创建目录 symlink；均有 pytest 明确标记，
  不是 W17 断言失败。
- W17 定向套件（`test_media_worker.py`、`test_media_entrypoint.py`、`test_gate_a_review.py`、W17/W16 UI 及
  pipeline 回归）覆盖模拟设备顺序变化、重复身份、选定设备消失、低/高延迟 fallback、device-lost、取消、
  helper deadline/hang 收敛、root-relative descriptor、WAV lease 清理、设置保存与 Gate A dry-run。
- 实施中第一次完整 pytest 没有断言失败，但 coverage 为 `89.43%`，因此没有被接受为通过。随后补充了原生适配器、
  helper entrypoint、失败降级和 high-latency stream reuse 的有意义模拟路径；最终完整重跑才达到上述 90.43%。

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
- 完成最终 diff/static 审查后，只暂存 W17 预期改动，创建聚焦提交、推送分支并打开/更新 W17 Draft PR；远端 CI
  和人工设备 Gate 将在 exact head 上另行记录。

## 相关资料

- [W17 实现记录](../implementation/w17_media_worker_audio.md)
- [W17 权威计划](../windows_development_plan.md)
- [ADR-W01：运行拓扑](../adr/ADR-W01-runtime-topology.md)
- [ADR-W06：有界流水线](../adr/ADR-W06-bounded-pipeline.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
