# 当前产品目标

> 最后核验：2026-07-23（Asia/Shanghai）。当前唯一活跃任务仍为 W18「Push-to-talk、麦克风 ring buffer 与
> whisper Job」，本轮补齐真实、受管的轻量本地中文 STT runtime。分支为 `codex/w18-ptt-whisper`，基线为 W17
> merge commit [`351da92`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/351da92bfd0232ce03a90a97b75c13ba8ee6a51b)。
> Draft PR [#31](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/31) 仍未合并。
>
> 本轮受管 runtime 聚焦提交
> [`224e06f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/224e06f9cbb1d2ab0cc2260fb244b1f74cd7dfbc)
> 已推送。其 exact head 的 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977299022)
> 与 [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29977296872) 均通过 macOS/Windows
> `quality` 与 `installed-wheel`。该 push workflow 的第一次 Windows `quality` 仅在
> `tests/unit/ui/test_chat_runtime.py` 显示失败标记，随后 job 在输出断言栈前结束；同一 SHA 的 PR workflow 和第 2 次尝试
> 通过，因此这一个 `test_chat_runtime` 现象的根因仍**未验证**。
>
> 这与后续仅文档 head [`28df5fd`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/28df5fde0fc726d65ffb4e1527a9795dd9efdf66)
> 的 [PR Windows quality failure](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29978065326)
> 不同：该日志明确记录 `test_successful_handshake_job_and_orderly_shutdown` 的 `ShutdownReport.exit_code=None`。
> 当前已在 `WorkerSupervisor` 关闭报告前补读完成的 watcher 结果，并用受控回归测试覆盖该顺序。修复代码 head
> [`30f265b`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/30f265b8820104b01f0cef5079a31d7f15157436)
> 的 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29979630456) 与
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29979628710) 均通过双 OS 的
> `quality` / `installed-wheel`。Draft PR #31 未合并；真实设备 Gate 与每个后续 head 的独立核验仍不可省略。
>
> 后续 status-record head [`b150110`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/b15011019c167062f1f6b3e887a7c9513fab78e6)
> 的 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29979958354) 首次 Windows
> quality 在无关的 GPT-SoVITS fake-response 测试将预期 `tts_invalid_audio` 误报为 `tts_first_byte_timeout`；同一 head 的
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29979956583) 成功，失败 job 重跑也成功。
> 本机该参数化测试连续 20 次通过；80 ms MockTransport first-byte fixture 在 Windows 满载时调度敏感是**合理推测**，不是已证明永久稳定。

## 本轮已确认的实现范围

- 沿用已存在的 `MediaWorker` / Job Object / `WhisperCppRunner` 调用链，而不是另建 STT 架构。
- 固定 CPU 本地离线 profile：[`whisper.cpp` v1.9.1](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.1)
  的 `whisper-bin-x64.zip` 与
  [`ggml-base-q5_1.bin` 的不可变 revision](https://huggingface.co/ggerganov/whisper.cpp/blob/87cd18b47b941d2f65d09981dad23bb7d0481c77/ggml-base-q5_1.bin)。
  清单锁定 URL、版本、archive/CLI/model SHA-256、下载上限与受管目录；详情见
  [W18 runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)。
- 产品层只支持中文：默认与旧 `auto` 配置均归一为 `zh`；显式其他语言由配置或 worker 返回稳定失败，实际 CLI 固定
  `--language zh`。`threads=null` 时解析为 `min(max(os.cpu_count(), 1), 4)`。
- 默认仍为 `stt.enabled=false`，无启动下载、无云端 STT、无模型常驻。仅确认后的设置操作或
  `--install-chinese-stt` 下载；安装也不启用麦克风、设备或线程设置。
- 受管资产安装到 `%LOCALAPPDATA%\MeguminCompanion\models\stt\whispercpp\v1.9.1\`：HTTPS、有限重定向、
  超时/大小上限、SHA-256、ZIP Slip/link/reparse 拒绝、私有 staging 与原子切换。手工路径保持兼容并显示为非受管。
- 每个 MediaWorker 首次使用 canonical 受管路径时完整校验 CLI 与模型 SHA-256；不匹配在启动 CLI 前返回
  `stt_runtime_integrity_failed`。UI/CLI 只输出有限状态或 reason code，且不输出音频、转写、下载令牌或完整本地路径。
- `tools/stt_smoke.py --mode microphone --measure-working-set` 是显式设备诊断：只输出状态、语言、段数和 Windows
  `PeakWorkingSetSize`，不输出转写正文。

## 已完成的自动化证据（提交前本地树）

- `uv run pytest --no-cov tests/unit/test_stt_runtime.py tests/unit/test_stt_factory.py tests/unit/test_whisper_cpp.py
  tests/unit/test_media_voice.py tests/unit/test_media_entrypoint.py tests/unit/test_cli.py tests/unit/test_user_settings.py
  tests/unit/test_w05_ci.py tests/unit/ui/test_w16_management.py` → **192 passed in 10.79s**。测试只使用合成 bytes、fake ZIP、
  fake CLI 和 fake device；没有下载
  模型、录音或访问麦克风。
- 覆盖的可自动化断言包括：无启动下载、legacy `auto`→`zh`、非中文拒绝、下载 hash/timeout 失败、Zip Slip、取消、同一
  service 并发安装、HTTPS→HTTP 降级拒绝、Zip Slip/reparse 拒绝、原子修复、受管/手工路径差异、受管 hash 失败不启动 CLI、UI/CLI command bridge、
  Windows `PeakWorkingSetSize` 读取、wheel 禁止 STT model/binary/archive 成员，以及安装流程不进入音频 worker。
- 提交前本地树的 `uv run pytest` → **1223 passed, 3 skipped in 191.56s**，coverage **90.01%**。三个 skip 是
  optional RapidOCR、optional Pillow 和当前账户没有 directory-symlink 权限；均不是 W18 断言失败。
- 提交前本地树的 `uv run ruff check .`、`uv run ruff format --check .`（241 files）、`uv run mypy`（234 source）、
  `uv lock --check`、`git diff --check` 与 docs 相对链接检查均通过。临时构建 wheel 的隔离 smoke 也通过，确认 source tree
  没有被导入且 runtime/model/binary/archive 没有进入 wheel。
- `224e06f` 的 PR/push workflows 均通过 macOS/Windows `quality` 与 `installed-wheel`。push 首次 Windows quality 的失败与
  重跑通过均绑定同一 SHA，详情和未验证根因见本页开头；它不改变本地自动化结论。
- 当前 `WorkerSupervisor` 关闭报告修复已在本地验证：`uv run pytest --no-cov tests/unit/test_worker_supervisor.py` →
  **46 passed in 2.26s**；原有 orderly-shutdown 测试与新增受控 race 测试连续运行 20 次均通过；随后
  `uv run pytest` → **1224 passed, 3 skipped in 182.81s**，coverage **90.03%**。这些测试仍只使用 fake worker/CLI，
  不下载模型、不录音也不访问麦克风。

## 已知安全与隐私状态

- 受管 profile 不接受用户 URL、模型名或 hash；模型、原生 binary、真实录音和转写不进入仓库、wheel、安装包、CI artifact
  或日志。MIT 许可证和来源已记录，但没有独立法律/安全审计结论。
- [CVE-2026-10298](https://nvd.nist.gov/vuln/detail/CVE-2026-10298) 的 NVD 描述列出范围至 1.8.2，未把 v1.9.1
  列为受影响版本；但 [上游 issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 本次复核仍为 open。不能据此
  声称 v1.9.1 已修复。受管模型的不可变 hash 是输入完整性缓解，而不是上游漏洞修复或对非受管模型的保证。
- 模型页面标示约 57 MiB（59.7 MB）文件；这不是峰值内存。Windows 实际峰值工作集尚未测量。

## 未完成 Gate、范围外与下一步

1. `30f265b` 已完成修复代码的双 workflow exact-head 核验；CI 证据仍只能用于其对应 head，任何后续 head（包括本条状态
   记录）都须独立完成相同核验。未获授权不合并。
2. 唯一保留的真实设备 Gate：一次约 30 秒中文 PTT，确认离线转写、真实麦克风/系统提示和
   `PeakWorkingSetSize ≤512 MiB`。任何超限阻止交付并重新选型；不得保存真实录音或转写正文。

真实 IME/高 DPI、锁屏和系统级 hotkey 没有通过结论，且不被 fake/headless 条件冒充；它们是后续桌面体验工作，不是本轮
受管 runtime 的额外发布 Gate。RDP、快速切换用户、跨 session 和跨用户访问属于单机单用户私人范围外，不能写作通过或
转为人工 Gate。

## 相关资料

- [W18 实现记录](../implementation/w18_push_to_talk_whisper.md)
- [W18 受管 runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
- [ADR-W08：包与升级边界](../adr/ADR-W08-packaging-upgrade.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
