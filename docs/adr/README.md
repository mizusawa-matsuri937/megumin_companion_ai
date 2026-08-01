# Gate W0 Architecture Decision Records

> **2026-08-01 合并闭环：** W19～W30 产品堆栈已按 exact-head guard 合入
> `agent/windows-development-baseline@83f52e228d8df6df9abf08ce492192752ff113c2`。所有者接受的是各实现记录中
> 已列出的主观 Gate 与残余发布风险；真实 DeepSeek Key/账号/价格/远端保留、真实 Windows DPI 和其他未执行的
> 外部验证仍保持“未验证”。本次关闭分支只稳定 MockTTS 与 GPT-SoVITS 的 Windows CI 测试夹具并同步状态，
> 不改变任何 ADR 语义、产品 timeout 或运行时代码。
>
> **2026-07-31 后续状态：** ADR-W30 的 Gateway compatibility 已作为 `84c92bb` 推送；其 push workflow
> `30612675642` 全绿。此前 `a0b5661` 的 PR workflow `30614461677` 在 Windows quality 发现一项直连 GPT-SoVITS
> cancellation/settlement 与三项 Gateway MockTransport 夹具时序失败；`a0ccfc6` 仅修正测试夹具，并由其 push
> `30615939286` 和 PR `30615942372` 的四项跨平台检查复核通过。

> 决策日期：2026-07-17
> 范围：Windows 私人开发基线
> 决策者：项目所有者兼任架构、安全/隐私和许可证 reviewer
> 审查限制：没有独立人工 reviewer；详见 [`../gates/gate_w0.md`](../gates/gate_w0.md)

| ADR | 决策 | 状态 |
| --- | --- | --- |
| [ADR-W01](ADR-W01-runtime-topology.md) | Qt、BackendThread 与受监管 worker 拓扑 | 已批准 |
| [ADR-W02](ADR-W02-secure-control-plane.md) | 生产零端口、安全 dev API 与 helper pipe | 已批准 |
| [ADR-W03](ADR-W03-paths-resources-migration.md) | package resource、LocalAppData 和显式迁移 | 已批准 |
| [ADR-W04](ADR-W04-dacl-dpapi-uninstall.md) | DACL、DPAPI、秘密轮换和卸载 | 已批准 |
| [ADR-W05](ADR-W05-idempotency-replay.md) | 消息幂等、状态、事件序号与 replay | 已批准 |
| [ADR-W06](ADR-W06-bounded-pipeline.md) | 有界生成、TTS、音频和 VTS 流水线 | 已批准 |
| [ADR-W07](ADR-W07-native-worker-isolation.md) | native worker 隔离、hard kill 和清理 | 已批准 |
| [ADR-W08](ADR-W08-packaging-upgrade.md) | wheel、onedir、per-user 安装与升级 | 已批准；安装器实现暂定 |
| [ADR-W28](ADR-W28-avatar-runtime.md) | 单写者 Avatar Runtime、标量音量包络与 VTS 生命周期 | 已批准并随 PR #33 合并；主观 Gate 由所有者接受，记录中的未验证边界保留 |
| [ADR-W29](ADR-W29-private-tts-gateway-and-structured-turns.md) | 私有五槽 TTS 网关、流式结构化回合与有序 Avatar 联动 | 已批准并随 PR #34/#35 组合合并；P0 permit 修复已包含 |
| [ADR-W30](ADR-W30-deepseek-flash.md) | 固定 DeepSeek V4 Flash 文本 Provider、专用 DPAPI 密钥、脱敏视觉摘要出口与既有本地 Gateway 最小兼容 | 已按所有者指令采用并随 PR #35 合并；真实 Key、账号与远端行为仍未验证 |

ADR 只冻结架构和失败语义，不表示对应代码、Windows VM、真实设备或发布 Gate 已完成。修改任一 ADR 必须更新所有者决策、威胁模型、数据流清单和 Gate 记录。
