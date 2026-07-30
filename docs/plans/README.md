# 执行计划

## 当前权威计划

- [`../windows_development_plan.md`](../windows_development_plan.md) 是当前唯一的 Windows 分阶段执行计划。
  它定义 W 编号、依赖、验收、发布和人工 Gate；旧的按日顺序不能覆盖它。
- [`w30_deepseek_flash_execution_plan.md`](w30_deepseek_flash_execution_plan.md) 是当前 W30 的详细执行包：
  固定 DeepSeek Flash 文本 Provider、专用 DPAPI 密钥、既有上下文开关的受限复用和独立验证/回滚。
  当前实施状态见 [`../implementation/w30_deepseek_flash.md`](../implementation/w30_deepseek_flash.md)。W29 是暂停的
  独立工作树，既不是 W30 的代码来源，也不因本文而改变其验收状态。
- [`w28_avatar_runtime_execution_plan.md`](w28_avatar_runtime_execution_plan.md) 是上位 Windows 计划中
  W28 的详细执行包，冻结 Avatar Runtime、程序微动作、实际播放音量口型、主体动作/红眼生命周期、
  自动化、真实 VTS/音频 Gate 和 Draft PR 交付。它保留为既有计划，不是 W30 的完成证据；其实现证据见
  [`../implementation/w28_avatar_runtime.md`](../implementation/w28_avatar_runtime.md)。

## 历史计划

- [`../daily_development_plan.md`](../daily_development_plan.md) 已明确失效，仅保留历史背景和跳转说明。

## 使用规则

开始 Wxx 前，先读当前目标快照，再阅读当前计划中对应 Wxx 的完整范围、依赖、自动验收、人工 Gate、回滚和
风险条目；随后读取对应 ADR/实现记录及当前代码。计划中的旧完成状态需要在目标提交和当前 PR 上重新核验。

W28 还必须完整读取本地 `.agents/NEW_CHAT_AVATAR_HANDOFF_2026-07-28.md` 和
`.agents/W28_AVATAR_RUNTIME_EXECUTION_PACKAGE.md`。两者含私有需求和恢复状态，不得提交。
