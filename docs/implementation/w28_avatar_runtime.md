# W28：Avatar Runtime、程序微动作与音量口型

> 状态：产品代码、完整本地质量门、真实 VTS 与真实 MediaWorker/输出设备验收已完成；focused commit、
> stacked Draft PR 和 exact-head CI 待完成。真实中文 GPT-SoVITS 因当前用户配置未启用而未验证，
> 主观自然度 Gate 仍待所有者判断。
>
> 最后核验：2026-07-29（Asia/Shanghai）。工作分支 `codex/w28-avatar-runtime`，stacked base 为
> `codex/w19-provider-preflight@a64f5ac12a4b14175ecbfd2ac0d76168ac01f589`。

## 实现范围

- 扩展 VTS client：参数能力查询/注入、Expression state/activation、当前 event subscription response、
  unsolicited event 路由、模型事件和当前模型唯一 hotkey 语义解析。
- 新建单写者 `AvatarRuntime`、参数控制器、turn plan mapper 和 event sink，统一微动作、口型、主体动作、
  Neutral、取消、重连、切模、关闭与红眼所有权。
- 整轮只冻结一次现有 EmotionEngine 结果；多个 segment 不重复动作。同情绪不重播，情绪变化
  release→new，Neutral 只 release，cancel release，cancel 后同情绪可重播。
- MediaWorker 改为 frame-aligned PCM 分块、8/16/24/32-bit RMS 与 attack/release envelope；helper/supervisor
  增加有界 latest-wins `job.progress`，父侧在全部 terminal path 独立归零。
- 正常输出在 `RawOutputStream.stop()` 完成 pending-buffer drain 后才发送 terminal；取消仍走 abort/drop，
  相同格式/设备/latency 的 inactive stream 可重启复用。
- Avatar 设置页提供安全开关与口型校准；管理调试页分别展示 parameter control、lip sync、body motion 和
  automatic red-eye 的 availability/reason code，不显示私有语义或路径。
- `app.main` 只组装一个 Avatar owner，并把同一 runtime listener 接到 MediaWorker；Avatar 故障不阻断文字/音频。

完整架构和失败语义见 [ADR-W28](../adr/ADR-W28-avatar-runtime.md) 与
[正式执行计划](../plans/w28_avatar_runtime_execution_plan.md)。

## 关键自动化证据

修改前相关基线：

```text
uv run pytest --no-cov --basetemp .pytest-w28-baseline \
  tests/unit/test_vts_client.py tests/unit/test_vts_bridge.py \
  tests/unit/test_vts_event_sink.py tests/integration/test_vts_fake_server.py \
  tests/unit/test_worker_protocol.py tests/unit/test_worker_helper.py \
  tests/unit/test_worker_supervisor.py tests/unit/test_media_worker.py \
  tests/integration/test_mock_pipeline.py tests/integration/test_w07_bounded_pipeline.py \
  tests/unit/test_bootstrap.py
```

结果：`147 passed in 12.37s`。

扩展 W28 矩阵：

```text
uv run pytest --no-cov --basetemp .pytest-w28-expanded \
  tests/unit/test_vts_client.py tests/unit/test_vts_bridge.py \
  tests/unit/test_vts_event_sink.py tests/integration/test_vts_fake_server.py \
  tests/unit/test_worker_protocol.py tests/unit/test_worker_helper.py \
  tests/unit/test_worker_supervisor.py tests/unit/test_media_worker.py \
  tests/unit/test_media_entrypoint.py tests/unit/test_bootstrap.py \
  tests/unit/test_config.py tests/unit/test_user_settings.py \
  tests/unit/test_avatar_controller.py tests/unit/test_avatar_runtime.py \
  tests/unit/test_avatar_event_sink.py tests/unit/emotion/test_integration.py \
  tests/integration/test_avatar_runtime_fake_vts.py \
  tests/integration/test_avatar_media_pipeline.py tests/integration/test_mock_pipeline.py \
  tests/integration/test_w07_bounded_pipeline.py tests/integration/test_api.py \
  tests/integration/test_w11_health_routes.py tests/unit/ui/test_w16_management.py \
  tests/unit/ui/test_chat_runtime.py
```

结果：`267 passed in 27.36s`。

代表性的 test-first 失败与修复：

- 当前 VTS event subscription response：旧 parser 缺字段，红测 `1 failed, 18 deselected`；修复后 VTS
  client/fake-runtime 矩阵通过。
- 真实 VTS event envelope 带 `requestID`：旧 dispatch 错误按 response correlation 丢弃，红测
  `1 failed, 30 deselected`；按 `messageType` 优先路由后转绿，并补 request collision/malformed ID。
- worker terminal/progress 竞争：红测证明 terminal 开始后迟到 publisher 仍可成功；显式关闭 job progress
  generation 后 helper/supervisor 为 `55 passed`，flood 矩阵为 `9 passed`。
- 输出 drain：红测
  `test_media_worker_drains_pending_output_before_reporting_playback_terminal`
  为 `1 failed, 22 deselected`，正常播放后 `stop_count=0`；修复后 `1 passed, 22 deselected`，
  media/entrypoint/Avatar 集成矩阵 `29 passed`。
- 红眼 event overflow：红测证明 fail-safe 可能关闭人工状态；改为查询真实状态并保守放弃系统所有权后
  `2 passed, 7 deselected`。
- Avatar 长时稳定：固定 seed 连续 20,000 fake-clock frames 可复现；慢注入时合并超过 10,000 个旧帧，
  task 数稳定且关闭后无命名 Avatar task。
- Avatar failure isolation：真实 TurnService/DialoguePipeline/父侧 MediaWorker job 在 Avatar sink 抛错时
  仍产生文字、播放和完成事件，聚焦矩阵 `12 passed`。

最终本地 pytest/branch coverage：

```text
uv run pytest --basetemp .pytest-w28-full-coverage-final \
  --cov-report=json:.agents/w28_full_coverage_final.json
```

结果：收集 1,331 项，`1328 passed, 3 skipped in 249.20s`，aggregate branch coverage
`90.09%`，满足 `fail-under=90`。三个 skip 分别是 optional RapidOCR、optional Pillow 和当前账户无法创建
directory symlink；没有 W28 断言失败。

最终静态、锁文件与差异检查：

```text
$trackedPython = @(git ls-files -- '*.py')
uv run ruff check -- $trackedPython
uv run ruff format --check -- $trackedPython
uv run mypy
uv lock --check
git diff --check
```

结果依次为 `All checks passed!`、`244 files already formatted`、
`Success: no issues found in 250 source files`、`Resolved 65 packages in 2ms` 和通过。直接对 `.` 运行
Ruff 会扫描必须保留但不提交的 `.agents/` 及 pytest 故障 fixture；因此本地检查使用与干净 CI checkout
等价的 tracked Python 集合。55 个候选文件、8,759 个新增行的 denylist 扫描没有绝对私有路径、私有 VTS
存储标识、私有模型名、已知私有 ID 或凭据字面量。

最终 wheel/source-quarantine/installed-artifact smoke：

```text
uv build --wheel --out-dir dist/w28-wheel-final
uv run --no-project --python 3.11 python tools/w05_ci_smoke.py \
  --wheel-dir dist/w28-wheel-final \
  --source-root . \
  --lock-file uv.lock \
  --evidence dist/w28-evidence-final/provenance.json
```

结果：installed smoke `status=ok`、`source_tree_imported=false`、145 members，manifest SHA-256
`45b0a6f266fa1afcd579ec99507daa401eca1ca3483474cc131c4484010d242d`。额外内容扫描没有私有存储、
私有模型或凭据字面量；evidence 不含绝对 Windows 路径。两个 W28 专用临时产物目录核验后已精确删除，
未触碰 `dist` 中其他历史内容。exact-head CI 仍必须在发布后独立通过。

## 完成前上游复核

2026-07-29 再次核对官方/上游仓库，实施前选择没有发生漂移：

- VTubeStudio `882ba5f`、pyvts `54263f4`、wallie-V2 `08caa1c`、
  python-sounddevice `9e55e73` 和 PortAudio `d1852d5` 均未归档或禁用。
- VTS、pyvts、wallie-V2 和 python-sounddevice 的仓库许可证识别仍为 MIT；PortAudio 继续使用仓库中的
  MIT-style PortAudio license。
- 官方 VTS 参数重发、Expression/event 契约和 sounddevice `stop()` 等待 pending output 的语义未变。
- GitHub global advisory API 对 `sounddevice` 与 `pyvts` 的精确包查询均为 0；这只表示定向检索未发现
  项目专属公告，不是独立安全认证。

因此继续以官方 VTS 文档为协议权威，复用项目既有 sounddevice/PortAudio 路径；不新增 pyvts、wallie-V2、
NumPy 或其他 native 依赖。

## 真实 VTS 证据

以下均使用真实 VTube Studio 和产品 client/runtime；Mock/fake 证据未计入本节：

- API/auth/current model、event subscription、参数能力、嘴参数、release 入口与 Expression state 分阶段探针通过。
- Neutral idle 连续 20 秒与 30 秒分别发送 426/645 帧；实际画面观察到眨眼、低幅头部与视线变化。该观察
  证明参数生效，不等于所有者已批准自然度。
- 主体生命周期日志顺序证明 startup release、首次 emotion、同 emotion 不重复、release→new emotion、
  Neutral release、cancel release、cancel 后同 emotion 重播和 close release；错误/异常计数为 0。
- 系统红眼首次开启、续期越过旧截止和最终到期均通过；真实断线后重连计数为 1，Expression 清理为关闭。
- 严格时序的 manual/system 交错通过：系统态可被人工关闭；人工开启后系统不能接管，超过系统时长仍保持；
  人工关闭与 shutdown 清理均生效。该轮为 2,584 参数帧、26 coalesced、0 dropped/reconnect/error。
- 真实 VTS 当前 event 形状包含 string `requestID`；修复后舞台人工入口可被产品订阅为非 API hotkey event。

退出后分阶段探针再次确认嘴归零、Expression 关闭，四层 capability 无错误码。发布前最终 stage probe
再次通过 connect/API/token/auth/current model、两类 event subscription、参数、release 与红眼入口：
127 个实际参数、`MouthOpen` 存在、Expression inactive。

## 真实 MediaWorker、声卡与口型证据

本地探针只生成短时合成 PCM 到应用私有临时音频根，不记录/提交 WAV、路径、设备名或逐样本值。它走生产
`MediaWorkerAudioPlayer`、`WorkerSupervisor`、helper entrypoint、真实 `sounddevice.RawOutputStream` 和真实
AvatarRuntime/VTS。

- 枚举到 16 个实际输出 endpoint；当前没有持久选择，播放后与取消后仍可枚举。
- silence：20 progress + terminal，包络与真实 VTS `MouthOpen` 最大值为 0，最终严格归零。
- constant：包络/VTS 最大值约 0.139234，真实 VTS 有 33 个非零帧，最终归零。
- amplitude ramp：首/末四分位均值约 0.001917/0.249411，最大值约 0.285796；真实 VTS 有 33 个非零帧，
  最终归零。
- cancel：观察到非零后取消，16 ms 收束；真实 VTS 最大值约 0.186353、9 个非零帧，最终归零。
- drain 修复前 constant/ramp 墙钟比 WAV 标称时长短约 56～59 ms；官方 sounddevice/PortAudio 语义确认
  `write()` 不保证设备排空。修复后墙钟为约 1,046/1,344 ms（标称 900/1,200 ms），包含约
  144～146 ms 输出尾部/停止延迟，terminal 不再早于 drain。
- runtime 全程 ready，146 accepted frames、6 coalesced、0 dropped/reconnect/error。

该证据证明真实设备路径、音量包络、取消和设备 drain，不证明采样级音画同步。没有 loopback 录音时，设备输出
latency 与 VTS 显示 latency 仍属于主观自然度 Gate。

## 未通过或未验证项

- 当前生效用户设置虽然存在，但 `tts.provider` 仍为 Mock、preset 数量为 0，默认 loopback GPT-SoVITS
  endpoint 没有监听者。因此真实中文 TTS 探针在配置阶段以 `tts_not_configured` 结束，未生成或播放 WAV。
  不能用合成音或 W19 fake preflight 代替该 Gate；本任务也不安装、启动、切换或猜测外部服务/私有 reference。
- 当前没有受信任的 LLM 结构化 Avatar 输出。W28 只冻结现有 EmotionEngine 的整轮状态；真正的结构化
  `AvatarTurnPlan` 是后续任务。
- 所有者尚未最终判断嘴部 gain/attack/release、眨眼、视线、呼吸、头部和中文整体观感。
- 本次真实动作证据覆盖配置的代表语义与生命周期，不扩张为所有私有动作、任意时点或其他 VTS 版本。

## 隐私、私有配置与恢复

- 私有模型、动作、Expression、声音、reference、录音、截图、日志、备份、token 和绝对路径没有进入
  tracked diff、fixture 或文档；本地探针与私有 verifier 全部留在未跟踪 `.agents/`。
- 合成播放探针结束后临时 WAV 数为 0。发布前复核仍为 `w28-probe-*.wav=0`，中文 TTS 专用临时根不存在。
- 真实动作前 exact-byte 私有模型树基线为 14 个模型、506 个 JSON、1,028 个文件。最终 exact guard 如实
  检出一个配置文件的等价序列化/metadata 字节差异，故不能声称逐字节完全相同。
- 内容无关 semantic guard 证明 14/14 的 hotkey、hotkey settings 和 parameter settings 与基线语义一致；
  唯一五个 nonvolatile leaf 差异与 W28 动作前已有的位置/idle 外部漂移相同。W28 没有新增或改写私有
  hotkey、屏幕按钮、参数映射或位置，也没有擅自恢复用户私有配置。
- 发布前最终 API 比较确认当前模型与任务开始模型一致；红眼关闭，VTS 进程与唯一主窗口仍在，窗口已最小化，
  W28 live probe 进程为 0。

## 回滚

将 Avatar 参数、lip sync、body motion 与 automatic red-eye 关闭；令 `MouthOpen=0` 并尝试 release。
文字和原有安全音频路径继续。回滚不删除私有模型、声音、配置或备份。

## 发布状态

W28 必须只暂存本任务文件，排除用户修改的根 `AGENTS.md`、整个 `.agents/` 和所有私有资产。最终提交、
stacked Draft PR URL、exact head 和 CI 将在完整质量门后回填；未获授权不得合并。
