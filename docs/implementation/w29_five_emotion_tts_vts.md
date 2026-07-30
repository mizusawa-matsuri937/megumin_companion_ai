# W29：五情绪 GPT-SoVITS 与 VTS 动作联动

> 状态：公共实现、私有运行时、真实 VTS/MediaWorker 链路、功能提交和 stacked Draft PR #34 已完成。
> 2026-07-30 发现确定性的 gateway permit 泄漏后，P0 提交 `a790f47` 及其 push/PR CI 8/8 已完成；
> 此状态记录待形成新 exact head 并终审。PR 不合并，所有者主观试听 Gate 保留。
> 最后核验：2026-07-30（Asia/Shanghai）
> 工作分支：`codex/w29-five-emotion-tts`
> stacked base：`codex/w28-avatar-runtime@b09841c13f1a733ec267027df62da6da7fc31fb6`
> 功能提交：[`3de8bc5`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/3de8bc599b2dde2db42460f39dd231739c9422ec)
> Draft PR：[#34](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/34)

## 实现范围

- 新增严格增量结构化回合协议：整轮主情绪、focused 视觉变体和分段红眼；只释放完整且校验通过的正文。
- 本地 EmotionEngine 保留最终裁决；Avatar mapper 统一生成声音槽、速率、动作语义和 transition。
- `excited` 明确路由到 `excited_explosion`，速率固定为 `1.00`。
- DialoguePipeline 支持结构化流、旧纯文本兼容、确定性重切、播放顺序红眼、TTS 失败视觉 fallback 和
  generation-scoped 取消。
- Avatar Runtime 支持 focused 变体切换、部分候选降级和同情绪/同变体姿态延续。
- 新增应用侧 `GPTSoVITSGatewayProvider`，仅调用受认证的 path-free 健康与合成端点。
- 新增私有网关 manifest、事务引擎、官方 backend、HTTP server、entrypoint、安全 ZIP importer 和
  Windows Job Object launcher。
- 新增独立 inference-only `deploy/tts_gateway` 依赖锁；不引入 WebUI、训练、ASR 或 UVR5。
- bootstrap、设置、管理面与 preflight 支持显式 gateway provider；旧 provider 只作兼容路径保留。

完整决策见
[ADR-W29](../adr/ADR-W29-private-tts-gateway-and-structured-turns.md)，执行顺序见
[W29 计划](../plans/w29_five_emotion_tts_vts_execution_plan.md)。

## 2026-07-30 P0 gateway permit 修复

### 已确认事实与实施

- `GPTSoVITSGatewayProvider` 在取得 synthesis permit、创建并登记 worker 后，原 done callback 只清理
  task 集合，遗漏 `BoundedSemaphore.release()`。因此每个成功、失败或取消的已登记 worker 都会泄漏一个 permit；
  默认容量为 8，实际何时失声取决于每轮分句数量，不能写成固定轮次。
- callback 现以 task membership guard 执行 `remove → cancellation cleanup → release`。这既保证每个已登记 worker
  恰好归还一个 permit，也避免意外重复 callback 造成 `BoundedSemaphore` 过度归还；task 创建失败仍由既有外层
  `finally` 归还尚未 handoff 的 permit。
- 新增容量为 1 时连续三次成功合成的回归；扩展取消回归，确认取消后没有 `.part`/WAV 残留且下一次合成可成功。
- 全量首次运行客观失败于 `stream_large` 参数化 fixture：该用例本来只检查响应大小，但也把 first-byte deadline
  设为 20ms。该设置在 coverage run 调度下会偶发先报告 first-byte timeout。fixture 现仅为真正的 first-byte
  timeout 用例保留 20ms；其他错误分类使用 200ms first-byte / 500ms total deadline。此为测试意图隔离，
  不改变产品 deadline。

### 验证与边界

```text
uv run pytest --no-cov tests/unit/test_gpt_sovits_gateway.py
→ 26 passed in 1.12s

uv run pytest
→ 1468 passed, 3 skipped in 133.58s; coverage 90.52%

uv run ruff check app desktop_client tests tools
uv run ruff format --check app desktop_client tests tools
uv run mypy
uv lock --check
uv lock --check --project deploy/tts_gateway
→ 全部通过
```

- P0 聚焦提交 [`a790f47`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/a790f478426c876d366965afe9404d3d69af906b)
  已推送；其 [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30516799937) 与
  [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30516802283) 的 macOS/Windows
  `quality` 和 `installed-wheel` 共 8 项均成功。该实现记录的状态提交仍须以自己的 exact head 重跑 CI。

- `_synthesis_cancellations` circuit 未在本修复中删除：它是 W08 明确的 fail-closed worker ownership，不是已证明的
  permit 泄漏根因。当前单 owner gateway 的阻塞推理不能由协程真正中止；直接允许更多新请求只会填满 admission
  queue，并不能安全恢复。restart/self-healing 必须作为独立的生命周期/安全设计。
- 本次在实施前复核 GPT-SoVITS（MIT、固定 commit）、Open-LLM-VTuber（MIT）与 LiveTalking（Apache-2.0）。
  P0 未复制外部代码；未来流式实现只可复用固定 GPT-SoVITS 的 generator/packing 语义，其他项目仅作设计参考。

## 自动化证据

### test-first 修复

- 结构化取消首次暴露两个死锁：
  - `TurnCancelledError` 继承 `asyncio.CancelledError`，TaskGroup 把它当作正常任务取消，未取消 sibling；
  - 外部 `task.cancel()` 后无条件向已无消费者的满队列写 sentinel 会阻塞。
  修复后使用内部普通异常触发 sibling cancellation，并只在 stage clean exit 时关闭下游队列；W07 与结构化
  取消矩阵为 `9 passed`。
- W08 incompatible completion 的旧 mock 仍输出纯文本，但真实 provider 已声明结构化流。fixture 改成不完整
  但合法的结构化前缀，继续验证错误 completion 必须返回 `llm_protocol_error` 且 partial 不持久化。
- 生产 MediaWorker 冷导入发现 Avatar/Media 循环依赖；`FocusedVariant` 移到 emotion domain 后 fresh
  subprocess 冷导入转绿。
- Avatar 候选准备过去因一个候选缺失而丢弃整组；现只禁用缺失项，完全无候选时仍 fail closed。
- 初次完整 suite 的断言全部通过但 coverage 只有 `89.54%`。没有降低阈值；新增 path-free client、
  manifest、结构化 parser、事务引擎和 HTTP 边界测试后恢复跨平台余量。

### 当前精确树

```text
uv run pytest --basetemp .pytest-w29-full-coverage-final \
  --cov-report=json:.agents/w29_full_coverage_final.json
```

结果：收集 1,470 项，`1467 passed, 3 skipped in 183.17s`，aggregate branch coverage `90.55%`。
三个 skip 为 optional RapidOCR、optional Pillow 和当前账户不能创建目录 symlink；没有 W29 断言失败。

```text
uv run ruff check app desktop_client tests tools
uv run ruff format --check app desktop_client tests tools
uv run mypy
uv lock --check
uv lock --check --project deploy/tts_gateway
```

结果：Ruff lint 通过；279 个 Python 文件格式一致；`Success: no issues found in 272 source files`；
根项目 65 包和网关运行时 122 包锁文件一致。

最终 wheel/source-quarantine：

```text
uv build --wheel --out-dir dist/w29-wheel-final
uv run --no-project --python 3.11 python tools/w05_ci_smoke.py \
  --wheel-dir dist/w29-wheel-final \
  --source-root . \
  --lock-file uv.lock \
  --evidence dist/w29-evidence-final/provenance.json
```

结果：installed smoke `status=ok`、`source_tree_imported=false`、156 members；manifest SHA-256
`9716ad85b92ab5e0236e5931d5f08ba5e434035d861170eece503757bbcb5691`，wheel SHA-256
`d39b83b3b2f3869fa97a207c63dee221c37d606d9390f6440dedf5c51565092e`。成员扫描没有
`.agents`、权重、WAV/ZIP、VTS 私有资产或构建目录；文本扫描没有私有 runtime/微信路径、私有 VTS 入口、
声音包文件名或凭据值。两处 Windows 路径均为既有公开 CLI 示例，三个凭据样式命中均为代码标识符；
provenance 不含绝对路径、私有 marker 或 secret。

新增安全测试的聚焦覆盖：

| 模块 | 当前聚焦覆盖 | 相对旧完整树净增 line/branch 点 |
| --- | ---: | ---: |
| 应用 gateway client | 94.42% | 141 |
| 私有 manifest | 92.80% | 51 |
| 结构化回合 parser | 95.52% | 35 |
| 事务 engine | 98.53% | 35 |
| HTTP server | 100% | 30 |

## 仓库外真实安装证据

以下只记录内容无关的版本、计数和结果；私有路径、文件名、哈希、提示文本和资产内容不进入本文。

- Python 3.11.15、Torch/Torchaudio 2.5.1+cu124、CUDA 12.4 和目标 GPU 可用。
- 官方固定源码、BERT、CNHubert、G2PW、speaker verification、Open JTalk 与日语前端均通过固定
  size/hash、安全解压和离线 import。
- 五包均通过安全导入、严格一对权重/参考形状、CurrentUser ACL 和私有 manifest 全量 hash 审计。
- batch 20 对五槽均稳定，最大 reserved 显存约 3.7 GiB；无需回退到 10/5/1。
- 五槽固定中文句均产生有效、非静音 WAV；20 次交替切模无 OOM、混合 pair、quarantine 或持续显存增长。
- 真实 gateway 通过认证、安全拒绝、中文合成和 Job Object 清理；主程序未自动启动网关。
- 最终公共 wheel 在文档/产物终审后再次强制无依赖安装到私有 runtime，并从隔离 site-packages 导入；
  最终 gateway smoke 再次通过 ready、三项安全拒绝、中文 WAV 和 launcher/Job shutdown，端口无残留 listener。

## 真实 VTS 与生产播放证据

- 当前 14 个外观均能唯一解析 release、红眼和必需动作候选；缺失计数为 0。一个外观的第二兴奋候选只通过
  仓库外覆盖加入。
- 用户设置已保存 gateway、system playback、VTS 和 Avatar 四层；正式 runtime preflight 为 ready，
  四层 available，最终红眼关闭且嘴参数归零。
- 正常链路使用 production gateway provider、MediaWorkerAudioPlayer 和真实 VTS：
  - 中文合成约 12.1 秒，音频约 1.94 秒，实际播放约 2.17 秒；
  - 观察到 62 个非零口型样本；
  - 主体动作和红眼都严格晚于首个实际播放 progress；
  - 播放完成后嘴和红眼归零，WAV 删除。
- 取消链路：
  - 合成长音频后开始真实播放并触发主体动作；
  - 取消 terminal 立即可见，真实嘴参数约 0.47 秒内归零；
  - release 已执行，无迟到主体动作、红眼或音频口型；
  - 输出树恢复，launcher 关闭后网关端口和 MediaWorker 子进程均为零。
- 上述证据证明真实中文音频、播放、VTS 口型、动作、红眼、取消和清理，不证明五种声音的主观差异或自然度。

## 上游、许可与安全复核

2026-07-30 完成前复核固定源码：

- [官方 README](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/README.md)
  仍声明跨语言推理支持中文与日语，并列出 Python 3.11、PyTorch 2.5.1、CUDA 12.4 的测试环境；
- [固定提交 LICENSE](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/LICENSE)
  为 MIT；
- [原始 API](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/api_v2.py)
  仍包含控制、reference 和路径切换 GPT/SoVITS 权重的端点，因此继续不复用；
- [固定推理实现](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/GPT_SoVITS/inference_webui.py)
  仍有 `torch.load(..., weights_only=False)`；
- [GitHub Security Lab 公告](https://securitylab.github.com/advisories/GHSL-2025-049_GHSL-2025-053_RVC-Boss_GPT-SoVITS/)
  仍将多处不安全反序列化归为可导致 RCE。

因此继续只加载用户明确确认可信、SHA-256 固定且列入私有五槽清单的权重；这不是独立安全或许可证审计。

## 隐私审计边界

- 实际声音权重、参考 WAV、日语提示、生成 WAV、token、私有绝对路径、VTS 模型/入口名称和 ID 全部留在
  Git 外。
- 私有安装/校准/VTS/真实链路脚本与状态只在未跟踪 `.agents/`，不得暂存或打包。
- tracked 测试只使用合成 WAV、虚拟槽名、fake backend 和稳定 error code，不包含受保护角色资产。
- wheel/source quarantine、未暂存候选和 staged diff 的日志/secret/path denylist 均已通过；功能提交
  精确为 64 个公共文件，`AGENTS.md`、`.agents/`、私有资产、构建产物和 pytest 临时目录均未进入提交。

## Git 与 CI 交付证据

- 功能提交
  [`3de8bc5`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/3de8bc599b2dde2db42460f39dd231739c9422ec)
  和状态提交
  [`4404460`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/4404460c6b9fb4cd6140c070ab3831c491d0ca1b)
  已推送至 Draft PR [#34](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/34)。
- `4404460` 的
  [push workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30491796135)
  与 [PR workflow](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30491797289)
  均为 completed/success；macOS/Windows `quality` 与 `installed-wheel` 共 8 项全部通过。
- 本 CI 关闭记录只修改正式文档；最终报告仍须读取 PR live latest head/checks，不能借用
  `4404460` 的绿灯覆盖任何后续提交。

## 残余 Gate 与回滚

- 待完成：本 CI 关闭记录的独立 exact-head 检查和 PR 终审。
- 所有者主观 Gate：试听五种声音的音色/情绪差异、中文自然度，并观察随机动作与台词是否协调。
- 回滚：关闭 gateway 启动器，将 TTS provider 切回 Mock 或 silent；Avatar 和文字继续，嘴保持闭合。
  不删除私有声音、运行时、VTS 配置或备份。
