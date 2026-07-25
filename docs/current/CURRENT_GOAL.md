# 当前产品目标

> **状态更新（2026-07-25，Asia/Shanghai）：** 当前唯一活跃任务已切换为 W19「真实
> VTS/GPT-SoVITS 配置向导与联动」，工作分支为 `codex/w19-provider-preflight`，基线为已合并 W18 的
> [`90e758d`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/90e758d55a87e330a45260aaccd8c794069e3f7a)。
> 下方 W18 状态保留为历史交付证据，不再代表当前活跃任务。
>
> W19 将复用既有 W09 VTube Studio API 1.0 client/bridge、W08 GPT-SoVITS API v2 provider 和 W16
> BackendThread 管理面，新增可保存的默认 TTS preset/reference、显式联合 preflight 页面以及无内容的分阶段结果。
> VTS 首次授权仍必须由用户在 VTube Studio 内 Allow；GPT-SoVITS reference 端到端检查只在用户明确触发后发送固定
> 测试短语，生成的测试 WAV 不播放并立即登记清理。默认 mock、silent playback、VTS disabled 和文字可用性不改变。
>
> 已确认的调研边界：VTube Studio 官方 API 仓库为 MIT 且文档仍维护；GPT-SoVITS 官方仓库为 MIT 且主分支在
> 2026 年仍有维护记录，但 2025 年公开过多项命令注入和不安全反序列化/RCE。W19 不安装、打包、启动或管理 GPT-SoVITS，不调用会改变服务状态的
> `/set_refer_audio`，也不把服务自身安全写作本应用已证明。真实 VTS Allow、声音/延迟、表情和服务重启仍属于人工
> 体验 Gate；fake server、headless Qt 和不播放的测试 WAV 不能冒充这些事实。
>
> 当前工作树已实现七项 typed preflight、默认 preset/reference 设置、固定短语且不播放的 TTS 检查和 VTS
> 分阶段 snapshot。W19 + W09 fake server + W07/W08 降级 + W16/W17 UI/音频扩展矩阵为
> **60 passed in 9.70s**；完整 pytest 为 **1252 passed, 3 skipped**，raw branch coverage **90.13%**，
> Ruff、格式、strict mypy、lock、diff check、相对文档链接和临时 wheel/source-quarantine smoke 均通过；
> 临时 wheel/evidence 已删除。功能提交
> [`d641c29`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/d641c29b744bf400d46fe4453aefec0ed69044ee)
> 已推送，Draft PR [#32](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/32) 已创建且 base/head 已核对；
> exact-head CI 尚未完成。详见
> [W19 实现记录](../implementation/w19_provider_preflight.md)。

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
>
> 最终状态记录 head [`2b0b858`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/2b0b858bb01c684056d764bce460c6a213767091)
> 的 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29980501223) 与
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29980499672) 均通过双 OS 的
> `quality` / `installed-wheel`。在本机获得明确授权后，受管安装实际尝试未完成：第一次命令的外层终端在 64 秒超时，子进程随后
> 退出而未激活资产，因输出管道已关闭不能取得原因码；第二次受控单实例重试返回稳定
> `stt_download_failed`。随后状态为 `missing`、`stt.enabled=false`、零 staging 目录；没有访问麦克风、录音、转写或测量
> `PeakWorkingSetSize`。这不是设备 Gate 通过，而是固定来源下载失败的已记录阻塞；CLI 有意不公开更细的网络原因，不能臆测其根因。
>
> 随后所有者提供并明确授权使用与固定清单完全匹配的两个离线资产。一次性操作复用已有受管 staging、安全 ZIP 提取、CLI
> version probe、原子切换与最终 SHA-256 校验（不是产品新增的任意本地文件入口）；运行时状态变为 `verified`，语言为 `zh`，
> `stt.enabled` 仍为 `false`，项目根目录的重复源文件在验证后删除。真实 MediaWorker preflight 通过且未访问麦克风。
> 第一次 PTT 返回 `stt_empty_recording`（stream 已启动但 ring 未收到 PCM callback；开始后立即停止只是合理推测），诊断工具已改为只输出有限错误 JSON、且在 stream 启动后明确提示说话；
> 回归测试覆盖该路径。第二次由所有者操作的 microphone 诊断在工具明确提示“请说中文约 30 秒”后返回
> `{"status":"transcribed","language":"zh","segment_count":22,"peak_working_set_bytes":236609536}`，即约 225.7 MiB，
> 低于 512 MiB 上限。没有保存录音、转写正文或临时状态日志。该输出证明确实完成离线转写并获得了真实采集数据；Windows
> 麦克风权限提示/录音指示器的目视确认仍须由所有者明确反馈，不能由本记录臆测为已通过。终端摘要也不含实际录音时长，
> 因而不能独立证实录制已满约 30 秒。
>
> 随后的诊断工具加固提交 [`63d6684`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/63d668479bf6462a73f90378b1bacd3123562b40)
> 将 microphone-mode `VoiceCaptureError` / `EOFError` 收敛为有限 JSON reason code，并加入 `stt_empty_recording` 回归测试。
> 本地完整测试为 **1225 passed, 3 skipped**，coverage **90.03%**；Ruff、格式、mypy、lock、相对 Markdown 链接和临时 wheel
> 的隔离 smoke 均通过，wheel 未包含模型、原生 binary 或音频。该 SHA 的
> [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30004642052) 首次通过全部 4 个
> macOS/Windows checks；[PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30004645550)
> 首次 Windows quality 仅失败于未改动的 GPT-SoVITS cache fixture（80 ms MockTransport first-byte deadline），同一 SHA 的
> push Windows quality 已通过，PR 的失败 job 第 2 次重跑也通过。调度敏感是合理推测，不是已证明的永久稳定性；没有为此修改
> 无关的 TTS product code。

## 本轮已确认的实现范围

- 沿用已存在的 `MediaWorker` / Job Object / `WhisperCppRunner` 调用链，而不是另建 STT 架构。
- 内置受管 Whisper 配置档目录当前只含 CPU 本地离线的
  [`whisper.cpp` v1.9.1](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.1) `whisper-bin-x64.zip` 与
  [`ggml-base-q5_1.bin` 的不可变 revision](https://huggingface.co/ggerganov/whisper.cpp/blob/87cd18b47b941d2f65d09981dad23bb7d0481c77/ggml-base-q5_1.bin)。
  新增 `stt.managed_profile` 只允许已登记配置档；每一项锁定 URL、版本、archive/CLI/model SHA-256、下载上限、模型文件名
  与受管目录。以后更大 Whisper 模型必须新增独立 profile 并重做真实性能核验，不能以任意路径、URL 或 hash 替代。详情见
  [W18 runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)。
- 产品层只支持中文：默认与旧 `auto` 配置均归一为 `zh`；显式其他语言由配置或 worker 返回稳定失败，实际 CLI 固定
  `--language zh`。`threads=null` 时解析为 `min(max(os.cpu_count(), 1), 4)`。
- 默认仍为 `stt.enabled=false`，无启动下载、无云端 STT、无模型常驻。仅确认后的设置操作或
  `--install-chinese-stt` 下载；安装也不启用麦克风、设备或线程设置。
- 当前 base 受管资产安装到 `%LOCALAPPDATA%\MeguminCompanion\models\stt\whispercpp\v1.9.1\`；未来 profile 使用独立
  `profiles\<profile>\v<version>` 目录：HTTPS、有限重定向、超时/大小上限、SHA-256、ZIP Slip/link/reparse 拒绝、私有
  staging 与原子切换。手工路径保持兼容并显示为非受管。
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
- 本次受管 Whisper 配置档接口的提交前本地树：定向 profile/config/UI 回归为 **117 passed**；完整
  `uv run pytest` → **1241 passed, 3 skipped in 175.67s**，coverage **90.09%**。`uv run ruff check .`、
  `uv run ruff format --check .`（242 files）、`uv run mypy`（235 source）、`uv lock --check`、`git diff --check`、
  docs 相对链接检查，以及临时 wheel 的隔离安装 smoke 均通过；wheel/evidence 已删除，且未加入或下载任何更大模型。
  功能提交 [`7ca0067`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/7ca0067d42432a4ce8d68cd670309b6f3c08e7d4)
  的 [PR workflow 30012917744](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30012917744) 与
  [push workflow 30012917469](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30012917469) 均在
  macOS/Windows `quality` / `installed-wheel` 通过；任何后续文档或代码 head 仍须单独核验，Draft PR 仍未合并。

## 已知安全与隐私状态

- 受管 profile 不接受用户 URL、模型名或 hash；模型、原生 binary、真实录音和转写不进入仓库、wheel、安装包、CI artifact
  或日志。MIT 许可证和来源已记录，但没有独立法律/安全审计结论。
- [CVE-2026-10298](https://nvd.nist.gov/vuln/detail/CVE-2026-10298) 的 NVD 描述列出范围至 1.8.2，未把 v1.9.1
  列为受影响版本；但 [上游 issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 本次复核仍为 open。不能据此
  声称 v1.9.1 已修复。受管模型的不可变 hash 是输入完整性缓解，而不是上游漏洞修复或对非受管模型的保证。
- 模型页面标示约 57 MiB（59.7 MB）文件；这不是峰值内存。当前 base 的一次真实 PTT 已记录
  `PeakWorkingSetSize=236,609,536` bytes（约 225.7 MiB），但该测量不能外推给任何未来更大 profile；每个新增模型仍须
  单独通过真实设备峰值验证。

## 所有者授权的受保护合并记录（2026-07-23）

所有者已明确要求合并 Draft PR #31。本记录将该指令视为对下列**已知残余发布风险**的接受；它不把任何未验证项写作通过：

- 真实 PTT 已自动验证离线转写、`zh`、22 segments 和 225.7 MiB 峰值工作集，但终端摘要不证明实际录音达到约 30 秒；
  Windows 麦克风权限提示或录音指示器的目视确认也仍只能由所有者提供。
- [上游 issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 仍 open；NVD 仅列至 1.8.2 不是 v1.9.1 已修复的证明。
- 默认仍为 `stt.enabled=false`，没有新增模型、云端转写、后台下载或任意模型入口；如需回退，只须保持 STT 关闭。

合并仍受保护：本 closure-record head 必须先通过其自身 macOS/Windows `quality` 与 `installed-wheel`，随后必须重读
PR 的 base/head/diff/review/conversation/mergeability/draft 状态，并使用 expected-head guard 合并。合并后还须从远端确认
merge commit、同步基线并重跑完整测试；未获新的明确授权不得把这次合并扩展为对上述真实设备 Gate 的通过结论。

## 未完成 Gate、范围外与下一步

1. `63d6684` 已完成 exact-head 的 push/PR 双 workflow 核验，macOS/Windows `quality` 与 `installed-wheel` 共 8 项均通过；
   PR Windows quality 的首次失败及同 SHA 重跑通过已如实记录，不能把重跑结论写作已消除长期不稳定性。CI 证据仍只能用于其
   对应 head，任何后续 head 都须独立完成相同核验。当前合并授权和 closure 条件见上节。
2. 真实 PTT 的可自动验证部分已通过：离线转写成功、`zh`、22 segments、`236,609,536` bytes（约 225.7 MiB，≤512 MiB）。
   仍待所有者反馈的是实际录音是否达到约 30 秒，以及 Windows 麦克风权限提示或录音指示器的目视确认；终端摘要不能替代
   这两项真实桌面/设备事实，未获反馈不得把完整 Gate 写成通过。任何超限仍阻止交付并重新选型；不得保存真实录音或转写正文。

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
