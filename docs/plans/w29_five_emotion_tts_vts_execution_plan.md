# W29：五情绪 GPT-SoVITS 与 VTS 动作联动执行计划

> 状态：公共实现、私有安装、真实链路和本地质量门已完成；提交、stacked Draft PR 与 exact-head CI 待完成
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
- 不自动启动网关，不合并本任务 PR；最终试听是所有者主观 Gate。

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
- [ ] 形成聚焦提交、推送并创建以 W28 分支为 base 的 stacked Draft PR。
- [ ] PR 最新 exact head 的 macOS/Windows quality 与 installed-wheel 全部通过。
- [ ] 所有者试听五种音色、情绪差异、中文自然度与整体动作观感。

## 客观验收摘要

- 完整测试：收集 1,470 项，`1467 passed, 3 skipped`，aggregate branch coverage `90.55%`。
- 静态门：Ruff lint 通过；279 个 Python 文件格式一致；strict mypy 272 source 通过。
- 锁文件：根项目 65 包、网关运行时 122 包解析一致。
- 真实五槽：batch 20 稳定，20 次交替切模通过。
- 真实生产链：正常中文段观察到实际播放后的口型/动作/红眼；长音频取消后口型在有界时间归零，无迟到事件。

精确证据、失败修复和残余 Gate 见
[W29 实现记录](../implementation/w29_five_emotion_tts_vts.md) 与
[ADR-W29](../adr/ADR-W29-private-tts-gateway-and-structured-turns.md)。
