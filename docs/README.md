# 文档中心

> 本目录是项目的长期知识库。`AGENTS.md` 保留不可协商的执行约束和快速路由；这里保存设计、计划、规范、
> 经验、证据与当前产品目标。不要用聊天记录或旧 handoff 替代本索引。

## 新会话的最短阅读路径

1. 根目录 [`AGENTS.md`](../AGENTS.md)：硬性约束、上下文恢复和任务路由。
2. [`current/CURRENT_GOAL.md`](current/CURRENT_GOAL.md)：当前产品目标、已核验状态、下一步和禁止事项。
3. [`standards/AGENT_OPERATING_CONSTRAINTS.md`](standards/AGENT_OPERATING_CONSTRAINTS.md)：证据、文档维护、隐私和审计的展开规则。
4. 按下方分类选择与任务直接相关的资料，并回到代码、测试、配置和 Git/CI 核验当前事实。

## 权威顺序与状态词

冲突时按下列优先级处理：

1. 用户最新明确指令与适用的平台/安全要求；
2. `AGENTS.md` 中的硬性约束；
3. 当前工作区、目标提交、测试、配置和可复现命令；
4. 已标明核验日期的 `current/` 状态快照与对应外部证据；
5. 已批准的 ADR、所有者决策、威胁模型、Gate 和当前执行计划；
6. 实现记录、审计报告、历史报告与 handoff。

文档必须把“已确认事实”“合理推测/建议”“未验证项”分开写。历史文档能解释背景，但不能覆盖当前代码或
宣称未重新验证的完成状态。

## 分类导航

| 需要什么 | 位置 | 用途 |
| --- | --- | --- |
| 当前目标与交付状态 | [`current/`](current/) | 当前 W 任务、PR/Gate、下一步、已核验与未核验边界 |
| 执行计划 | [`plans/`](plans/) | 当前计划入口与历史排期说明；权威计划见其链接 |
| 规范与约束 | [`standards/`](standards/) | AI 工作约束、证据语义、文档和审计规则 |
| 架构与数据流 | [`architecture/README.md`](architecture/README.md) | 目标架构、运行时边界、数据流和保留清单 |
| 架构决策 | [`adr/`](adr/) | 已批准的 Architecture Decision Records |
| 所有者决定 | [`decisions/`](decisions/) | 产品、隐私、资产和 Gate 的人工决定 |
| 安全与隐私 | [`security/`](security/) | 威胁模型和安全边界 |
| Wxx 实现证据 | [`implementation/`](implementation/) | 范围、自动验证、残余风险、人工 Gate、回滚 |
| Gate 与验收 | [`gates/`](gates/) | Gate 决策及条件 |
| 代码审计 | [`audits/`](audits/) | 审计入口和历史风险基线 |
| 阶段报告 | [`reports/`](reports/) | 实现报告与范围说明 |
| 可复用经验 | [`experience/`](experience/) | 已验证的工程教训和复盘入口 |
| 历史交接包 | [`pro_handoff/`](pro_handoff/) | 外部/Pro 模型交接背景；不是当前状态来源 |

## 已有跨领域权威文档

以下文件保留在 `docs/` 根目录以维持现有链接和进行中的 PR 的稳定性；它们在逻辑上已由上表分类。新文档
不要继续无分类地放到根目录。

- 当前唯一 Windows 执行计划：[`windows_development_plan.md`](windows_development_plan.md)
- 已失效的日程计划说明：[`daily_development_plan.md`](daily_development_plan.md)
- 历史/目标架构设计：[`project_architecture.md`](project_architecture.md)
- Windows 风险审计基线：[`full_code_windows_risk_audit_2026-07-15.md`](full_code_windows_risk_audit_2026-07-15.md)
- 后端实现范围报告：[`ai_backend_mac_implementation_report.md`](ai_backend_mac_implementation_report.md)

这些文件的实际路径不在本次改动中批量迁移：移动会造成大量历史链接重写，并给正在审查的 W14 分支增加
无价值的 rebase 冲突。需要迁移时必须单独做链接清点、迁移和校验。

## 新文档落位规则

- 当前产品目标、活跃任务快照：`current/`。
- 执行计划、里程碑、排期：`plans/`；若更新现有权威计划，更新其原文件并同步索引。
- 新的架构说明、协议或图：`architecture/`；影响已批准决策时同时更新 `adr/`。
- 新的工作规范、审计准则：`standards/`。
- 一个 Wxx 的实现/验收/回滚记录：`implementation/`。
- 可跨任务复用的失败原因、经验和反模式：`experience/`。
- 风险审计、阶段报告、对外 handoff 分别进入 `audits/`、`reports/`、`pro_handoff/`。

新增或显著修改文档后，更新本页的分类入口；链接必须使用仓库内相对路径，并在提交前做本地链接检查。

## 当前 Wxx 实现记录

- [W14：文字对话、streaming、取消与恢复](implementation/w14_text_chat_streaming.md)
- [W15：单实例、托盘和统一生命周期（已合并）](implementation/w15_single_instance_lifecycle.md)
- [W16：设置、feature 与记忆管理最小 UI（已合并）](implementation/w16_settings_feature_memory_ui.md)
- [W17：MediaWorker 播放、输出设备与 Gate A（已合并，PR #30）](implementation/w17_media_worker_audio.md)
- [W18：Push-to-talk、麦克风 ring buffer 与 Whisper Job（Draft PR #31；受管中文 STT 新 head 待独立核验）](implementation/w18_push_to_talk_whisper.md)

## 当前 W18 技术决策

- [受管本地中文 STT 运行时选型](decisions/w18_managed_chinese_stt_runtime.md)：固定 `whisper.cpp` v1.9.1 与
  `ggml-base-q5_1.bin` 的来源、hash、许可、隐私边界和未关闭的上游安全风险。
