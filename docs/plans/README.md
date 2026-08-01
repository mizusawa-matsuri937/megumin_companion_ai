# 执行计划

## 当前权威计划

- [`../windows_development_plan.md`](../windows_development_plan.md) 是当前唯一的 Windows 分阶段执行计划。
  它定义 W 编号、依赖、验收、发布和人工 Gate；旧的按日顺序不能覆盖它。
- W19、W28、W29 与 W30 已在 2026-07-31 按堆栈顺序合入
  `agent/windows-development-baseline`；当前可复核基线为
  `83f52e228d8df6df9abf08ce492192752ff113c2`。以下 W28～W30 文件现作为已实施范围、验收边界和历史失败证据保留，
  不再表示开放 Draft PR。
- [`w30_deepseek_flash_execution_plan.md`](w30_deepseek_flash_execution_plan.md) 记录固定 DeepSeek Flash 文本
  Provider、专用 DPAPI 密钥、既有上下文开关的受限复用、Gateway compatibility 和回滚边界；最终 W29/W30
  组合由 PR #35 合并。当前事实见 [`../implementation/w30_deepseek_flash.md`](../implementation/w30_deepseek_flash.md)。
- [`w28_avatar_runtime_execution_plan.md`](w28_avatar_runtime_execution_plan.md) 是上位 Windows 计划中
  W28 的详细执行包，冻结 Avatar Runtime、程序微动作、实际播放音量口型、主体动作/红眼生命周期、
  自动化、真实 VTS/音频 Gate 和 Draft PR 交付。它保留为既有计划，不是 W30 的完成证据；其实现证据见
  [`../implementation/w28_avatar_runtime.md`](../implementation/w28_avatar_runtime.md)。
- [`w29_five_emotion_tts_vts_execution_plan.md`](w29_five_emotion_tts_vts_execution_plan.md) 是 W28
  之上的结构化回合、五声音槽、私有 GPT-SoVITS 网关、VTS 联动和发布执行包；已实施证据见
  [`../implementation/w29_five_emotion_tts_vts.md`](../implementation/w29_five_emotion_tts_vts.md)。

## 历史计划

- [`../daily_development_plan.md`](../daily_development_plan.md) 已明确失效，仅保留历史背景和跳转说明。

## 使用规则

开始 Wxx 前，先读当前目标快照，再阅读当前计划中对应 Wxx 的完整范围、依赖、自动验收、人工 Gate、回滚和
风险条目；随后读取对应 ADR/实现记录及当前代码。计划中的旧完成状态需要在目标提交和当前 PR 上重新核验。

W28 还必须完整读取本地 `.agents/NEW_CHAT_AVATAR_HANDOFF_2026-07-28.md` 和
`.agents/W28_AVATAR_RUNTIME_EXECUTION_PACKAGE.md`。两者含私有需求和恢复状态，不得提交。
