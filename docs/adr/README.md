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

ADR 只冻结架构和失败语义，不表示对应代码、Windows VM、真实设备或发布 Gate 已完成。修改任一 ADR 必须更新所有者决策、威胁模型、数据流清单和 Gate 记录。
