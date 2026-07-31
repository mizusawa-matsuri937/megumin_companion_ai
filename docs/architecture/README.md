# 架构与设计

## 当前边界与数据流

- [`windows_data_flow_inventory.md`](windows_data_flow_inventory.md)：Windows 数据流、保留清单与相关边界。
- [`chat_appearance_presentation_layer.md`](chat_appearance_presentation_layer.md)：聊天窗口的纯表现层主题与可替换消息渲染接口。
- [`../adr/ADR-W30-deepseek-flash.md`](../adr/ADR-W30-deepseek-flash.md)：固定 DeepSeek Flash 文本出口、专用密钥、
  不可信有限语义视觉摘要与长期记忆写入拒绝边界，以及 2026-07-31 所有者授权的既有本地 Gateway 最小兼容边界。
- [`../adr/ADR-W28-avatar-runtime.md`](../adr/ADR-W28-avatar-runtime.md)：单写者 Avatar Runtime、实际播放
  标量包络、generation、红眼所有权和关闭/降级决策。
- [`../adr/ADR-W29-private-tts-gateway-and-structured-turns.md`](../adr/ADR-W29-private-tts-gateway-and-structured-turns.md)：
  流式结构化回合、五声音槽、私有 path-free TTS 网关和有序 Avatar 播放决策。

## 目标/历史架构背景

- [`../project_architecture.md`](../project_architecture.md)：完整目标架构与历史设计背景。其目录树、技术栈建议、
  阶段/按日排期不自动构成当前实现事实；当前实施顺序以执行计划为准。

## 已批准决策与约束

- [`../adr/README.md`](../adr/README.md)：Architecture Decision Records。
- [`../decisions/w00_owner_decisions.md`](../decisions/w00_owner_decisions.md)：所有者决定。
- [`../decisions/w18_managed_chinese_stt_runtime.md`](../decisions/w18_managed_chinese_stt_runtime.md)：受管中文
  whisper.cpp runtime 的固定来源、hash、许可与残余风险。
- [`../security/windows_threat_model.md`](../security/windows_threat_model.md)：威胁模型。

修改架构、协议、数据流或隐私边界前，先阅读上述资料和当前计划，再以当前代码/测试核验实际实现范围。
