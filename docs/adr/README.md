# Gate W0 Architecture Decision Records

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
| [ADR-W28](ADR-W28-avatar-runtime.md) | 单写者 Avatar Runtime、标量音量包络与 VTS 生命周期 | 已批准并在 W28 本地工作树实现；发布/自然度 Gate 待关闭 |
| [ADR-W30](ADR-W30-deepseek-flash.md) | 固定 DeepSeek V4 Flash 文本 Provider、专用 DPAPI 密钥与脱敏视觉摘要出口 | 已按所有者指令采用；核心 `cd5cd43` 已在 Draft PR #35，本地自动化/质量门与审计 head `e35dbc7` CI 通过，真实 Key 验证仍待形成证据 |

ADR 只冻结架构和失败语义，不表示对应代码、Windows VM、真实设备或发布 Gate 已完成。修改任一 ADR 必须更新所有者决策、威胁模型、数据流清单和 Gate 记录。
