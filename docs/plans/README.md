# 执行计划

## 当前权威计划

- [`../windows_development_plan.md`](../windows_development_plan.md) 是当前唯一的 Windows 分阶段执行计划。
  它定义 W 编号、依赖、验收、发布和人工 Gate；旧的按日顺序不能覆盖它。

## 历史计划

- [`../daily_development_plan.md`](../daily_development_plan.md) 已明确失效，仅保留历史背景和跳转说明。

## 使用规则

开始 Wxx 前，先读当前目标快照，再阅读当前计划中对应 Wxx 的完整范围、依赖、自动验收、人工 Gate、回滚和
风险条目；随后读取对应 ADR/实现记录及当前代码。计划中的旧完成状态需要在目标提交和当前 PR 上重新核验。
