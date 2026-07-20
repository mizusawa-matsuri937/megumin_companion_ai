# 当前产品目标

> 最后核验：2026-07-20（Asia/Shanghai）。本快照的 PR 状态通过
> `gh pr view 26 --repo mizusawa-matsuri937/megumin_companion_ai` 核验；执行后续动作前仍须重新核验 exact head。

## 已确认事实

- 当前 Windows 开发主线的下一个待关闭任务是 **W14：文字对话、streaming、取消与恢复**。
- W14 已在 Draft PR [#26](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/26) 实现并发布：
  `codex/w14-text-chat-streaming` → `agent/windows-development-baseline`，head 为
  `09cf81e26b23e5e2ef793e9508a8a5fa7aac6df0`。
- PR #26 在核验时为 `OPEN`、`Draft`、`CLEAN`；两轮 macOS/Windows `quality` 与 `installed-wheel` 检查均成功。
- W14 将既有 `TurnService` 组装到进程内 desktop backend thread，复用 W06 的幂等、取消、顺序 event replay 和
  snapshot recovery；不启动 Uvicorn、不绑定 TCP、不创建 WebSocket client，也不新增持久化敏感数据。
- 当前基线分支在核验时为 `agent/windows-development-baseline` / `4b0370c`。本状态不是“W14 已合并”的声明。

## 当前目标与完成条件

当前目标是让 W14 在**不扩大范围、不自动合并**的前提下完成所有剩余审查和人工 Gate：

1. 等待项目所有者的明确审计/体验结论；在此之前保持 Draft，不启动 W15。
2. 人工只验证 AI 无法忠实复现的 Windows 真实体验：连续文字对话、中文/日文 IME composition 期间的
   Enter/Ctrl+Enter、快速发送/重试的感知、转录滚动与选中、错误文案的主观清晰度。
3. 收到批准后，重新核验 PR 的 base/head/diff、review、Draft 状态和**最终 head** 的全部检查，再以
   expected-head guard 合并；合并后从远端核验 merge commit，并在最终基线重跑要求的检查。
4. 合并完成后更新本文件、对应实现记录和执行计划，再由所有者决定是否启动 W15。

## 已完成的自动化证据范围

W14 的自动化已覆盖同一 `message_id` 的去重、副作用只执行一次、新输入抢占旧 turn、event gap 后清空并由
snapshot 恢复、配置化生命周期重启、W13 lifecycle/有界 bridge/input/focus/accessibility/close-wipe 回归。

这些证据仅适用于 mock/headless 条件，**不**证明真实 Windows、真实 IME、真实用户感受或屏幕阅读器听感。
详细契约和回滚方式见 W14 分支上的 `docs/implementation/w14_text_chat_streaming.md`；该记录在合并前不应被
误当作基线已拥有的文件。

## 未验证或需要重新核验的事项

- 所有者尚未给出 W14 的明确人工 Gate 结论；这不是自动化可替代的步骤。
- PR 检查、base/head、mergeability 和远端状态会随时间变化；任何合并决定都必须重新查询，而不能只引用本页。
- 本文件是共享的产品状态快照，不记录当前聊天会话的临时命令、未提交 diff 或私密信息；这些仅可写入本地
  `.agents/CONTEXT_MEMORY.md`。

## 进入 W14 前后应读取的资料

- 权威计划中的 W14/W15：[`../windows_development_plan.md`](../windows_development_plan.md)
- W13 已交付边界：[`../implementation/w13_pyside6_desktop_skeleton.md`](../implementation/w13_pyside6_desktop_skeleton.md)
- 幂等与恢复契约：[`../adr/ADR-W05-idempotency-replay.md`](../adr/ADR-W05-idempotency-replay.md)
- 发布/人工 Gate 规则：[`../standards/AGENT_OPERATING_CONSTRAINTS.md`](../standards/AGENT_OPERATING_CONSTRAINTS.md)

## 维护规则

当活动任务、PR、Gate、目标提交、下一步或已知阻塞发生实质变化时，先更新本文件的“最后核验”和对应段落，
再报告状态。仅凭历史 handoff、旧文档或聊天摘要不得改写本页为“已完成”。
