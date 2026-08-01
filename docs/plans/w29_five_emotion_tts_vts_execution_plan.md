# W29：五情绪 GPT-SoVITS 与 VTS 动作联动执行计划

> **2026-08-01 合并闭环：** W29 最终 head `d0765e22cc3595230c6e9b2991c5c52a817cd2f5`
> 已通过 push `30519958709` 与 PR `30519961163` 的 8/8 检查，并由
> [PR #34](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/34) 以 expected-head guard 合入
> W28，merge commit 为 `97c1a8f433ec67b0b7c482788a05260b43e3cf12`。所有者接受已记录的主观 Gate；
> 本文件现为历史执行与验收边界，不再表示开放 Draft PR。
>
> 历史状态：公共实现、私有安装、真实链路、本地质量门、功能提交、stacked Draft PR #34 和既有
> exact-head CI 已完成；2026-07-30 代码审计确认的 P0 gateway 稳定性缺陷已由
> `a790f47` 修复，其代码 head 的 push/PR CI 8/8 成功；本状态记录仍须接受自身 exact-head CI。所有者
> 主观试听 Gate 保留
> 最后核验：2026-07-30（Asia/Shanghai）

## 目标

在 W28 单写者 Avatar Runtime 上增加：

1. 五个固定声音槽的真实、中文为主 GPT-SoVITS 推理；
2. 严格流式结构化主情绪、focused 变体与分段红眼协议；
3. 本地情绪对声音、速率、动作语义和过渡的唯一裁决；
4. 仓库外受信运行时、私有 manifest、安全网关和桌面启动器；
5. 真实 VTS、MediaWorker、声卡、取消和清理闭环。

## 范围与禁止事项

- 只支持单机、单 Windows 用户、个人私用。
- 官方 GPT-SoVITS 固定提交；不启用 WebUI、原始 API、训练、ASR 或 UVR5。
- 不允许 LLM 输出模型路径、声音槽、动作入口或 VTS ID。
- 不提交声音权重、参考 WAV、提示词、生成音频、私有路径、VTS 模型/入口名称或 token。
- 不自动启动网关。原交付阶段保持 Draft；随后在所有者 2026-07-31 明确授权且最终 head 8/8 通过后受保护合并。

## 阶段

### A. 协议与领域映射

- [x] 增加 `FocusedVariant` 与本地 emotion/voice/speed/transition 映射。
- [x] 修正 `excited → excited_explosion@1.00`。
- [x] 实现严格增量 JSON parser、控制字段隔离、确定性超长重切和旧纯文本兼容。
- [x] 由本地 EmotionEngine 应用持续期、冷却和最终主情绪裁决。
- [x] 每轮红眼上限八次；爆裂状态和中二病 focused 的最低红眼约束由本地补足。

### B. 播放顺序与 Avatar 生命周期

- [x] 主体动作只在首个实际播放段开始时触发；TTS 失败时使用文字展示 fallback。
- [x] 红眼严格按实际播放顺序触发，不按合成完成顺序。
- [x] 主情绪或 focused 变体变化执行 release，再播放新候选；未变化则不重启。
- [x] 取消淘汰 generation，立即停止口型和主体动作，拒绝迟到音频、动作与红眼。
- [x] 修复 cooperative cancellation 在 TaskGroup 中不取消 sibling 的语义缺口。

### C. 安全 TTS 网关

- [x] 实现五槽严格 manifest、源码/树/文件 SHA、ACL 与 reparse-point 审计。
- [x] 实现单 owner 事务切模、pair 指针核对、回滚与 quarantine。
- [x] 实现 path-free loopback HTTP 面、DPAPI Bearer、Host/Origin/peer/body/admission 限制。
- [x] 实现应用侧 provider 的取消、deadline、first-byte、响应大小、WAV 验证和临时资产所有权。
- [x] 实现安全 ZIP 导入与日语文件名/提示文本恢复。
- [x] 建立 Python 3.11、Torch/Torchaudio 2.5.1 cu124 的 122 包 inference-only 锁文件。
- [x] 实现 Windows Job Object 启动器和当前用户桌面快捷方式；主程序不自动启动网关。

### D. 仓库外安装与真实验证

- [x] 五个用户确认可信的包完成安全导入、SHA-256 私有清单和 CurrentUser ACL。
- [x] 官方源码、公共模型、日语前端、Open JTalk 字典和 inference-only 依赖离线可用。
- [x] 五槽 batch `20 → 10 → 5 → 1` 校准在 20 即全部稳定；保存最大稳定值 20。
- [x] 五槽固定中文句均生成有效、非静音 WAV；20 次交替切模无 OOM、混合权重、quarantine 或持续显存增长。
- [x] 14 个当前外观的 release、红眼和必需候选均可唯一解析；单候选缺失只禁用该候选。
- [x] 生产 provider + MediaWorker + 真实输出 + VTS 链路通过中文播放、非零口型、动作、红眼和取消。
- [x] 启动器关闭后网关端口与整个子进程树清理；生成 WAV 和临时输出树恢复。

### E. 自动化与发布

- [x] 流式 JSON 任意 chunk、截断、重复键、未知字段、枚举、超限、取消和不泄漏。
- [x] 路由、候选防重复、focused 变体、release 和红眼顺序。
- [x] ZIP、manifest、ACL、hash、认证、Host/Origin、事务切模、回滚/quarantine 和日志边界。
- [x] 当前精确树完整 pytest/branch coverage、Ruff、format、mypy 与两个 lock check。
- [x] W29 ADR、数据流、威胁模型、当前目标、计划与实现记录完成终审。
- [x] wheel/source-quarantine、installed-artifact 和私有 denylist 通过。
- [x] 形成聚焦提交、推送并创建以 W28 分支为 base 的 stacked Draft PR #34。
- [x] 状态 head `4404460` 的 push/PR 两次 macOS/Windows quality 与 installed-wheel 共 8 项通过。
- [x] 所有者以“人工审核顺利”接受已记录的五种音色、情绪差异、中文自然度与动作观感主观 Gate；
  不扩写未提供的逐项试听细节。

最终 exact-head CI、live PR 审计与 guarded merge 已完成；交付报告仍必须以 GitHub 实时状态和远端 ref 为准。

### F. P0 gateway 稳定性修复（已合并）

- [x] 将 synthesis permit 的归还绑定到每一个已登记 worker 的唯一终态；不得因成功、失败、取消、关闭或
  task 在首次调度前取消而泄漏或重复归还 permit。
- [x] 增加容量为 1 的连续成功合成回归；保留原有 cancellation circuit、WAV/临时文件清理、关闭和 deadline
  断言，避免修复 permit 时破坏 W08 的 fail-closed worker ownership。
- [x] 本地聚焦与完整自动化质量门通过：`1468 passed, 3 skipped`，coverage `90.52%`；Ruff、format、
  strict mypy 与两个 lock check 通过。
- [x] 仅暂存本任务文件、形成并推送 P0 聚焦提交
  [`a790f47`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/a790f478426c876d366965afe9404d3d69af906b)；其
  [push](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30516799937) 与
  [PR](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30516802283) workflow 的
  macOS/Windows `quality` 和 `installed-wheel` 共 8 项均成功。
- [x] 状态记录和后续固定 4 秒眨眼 head 通过最终 exact-head CI，重新审计 base/head/diff/checks 后由 PR #34 合并。

下列属于后续优化，**不**由本 P0 修复冒充完成：provider 并发度与 pipeline 队列容量分离、首段优先、
真正的流式 PCM 播放、卡死 inference 的 gateway restart/self-healing。当前 cancellation circuit 会在
未结束 worker 持有 slot 时 fail closed；直接删除它只会让旧 GPU 工作填满三请求 admission 队列，不能构成恢复。
restart/self-healing 需要新的 gateway 生命周期 owner、readiness、close/cancel race 和重试边界；后两类变更会影响
私有网关协议、MediaWorker 和 launcher owner，实施前必须更新 ADR、数据流和威胁模型。

## 客观验收摘要

- 完整测试：收集 1,470 项，`1467 passed, 3 skipped`，aggregate branch coverage `90.55%`。
- 静态门：Ruff lint 通过；279 个 Python 文件格式一致；strict mypy 272 source 通过。
- 锁文件：根项目 65 包、网关运行时 122 包解析一致。
- 真实五槽：batch 20 稳定，20 次交替切模通过。
- 真实生产链：正常中文段观察到实际播放后的口型/动作/红眼；长音频取消后口型在有界时间归零，无迟到事件。

精确证据、失败修复和残余 Gate 见
[W29 实现记录](../implementation/w29_five_emotion_tts_vts.md) 与
[ADR-W29](../adr/ADR-W29-private-tts-gateway-and-structured-turns.md)。
