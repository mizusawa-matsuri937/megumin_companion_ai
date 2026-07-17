# Gate W0：架构、隐私与资产决策记录

> Gate ID：W0
> 决策日期：2026-07-17
> 状态：**有条件批准（私人开发范围）**
> 下一允许工作：PR W01
> 禁止提前开始：PySide6 GUI、窗口捕获业务实现、安装器业务实现

## 签字角色

| 角色 | 签字/结论 | 独立性 |
| --- | --- | --- |
| 项目所有者 | 本人，批准 OD-W00-01～18 | — |
| 架构 reviewer | 同一项目所有者本人 | 非独立自审 |
| 安全/隐私 reviewer | 同一项目所有者本人 | 非独立自审 |
| 许可证/资产 reviewer | 同一项目所有者本人 | 非独立自审 |

项目所有者明确表示目前没有独立审查人员，并接受该残余风险。本文不把自审描述为独立审核。

## Gate 清单

| 条件 | 结果 | 证据/备注 |
| --- | --- | --- |
| ADR-W01～W08 逐项决定 | 通过 | [`../adr/README.md`](../adr/README.md)；安装器具体实现暂定 |
| 项目所有者阻塞问题 | 通过 | [`../decisions/w00_owner_decisions.md`](../decisions/w00_owner_decisions.md) |
| 目标数据流与保留清单 | 通过 | [`../architecture/windows_data_flow_inventory.md`](../architecture/windows_data_flow_inventory.md) |
| 威胁模型与反方检查 | 有条件通过 | [`../security/windows_threat_model.md`](../security/windows_threat_model.md)；无独立人工 reviewer |
| 支持矩阵 | 通过 | Windows 11 x64、标准用户、单交互会话 |
| 云视觉边界 | 有条件通过 | 允许设计本地隐私检查后上传；实际启用仍受 W21/W22 隐私 Gate |
| 资产/分发边界 | 通过 | 私人使用；安装包零受保护资产；公开分发重新 Gate |
| 代码签名 | 私人范围豁免 | 当前不需要；公开 RC 阻塞 |
| Windows/设备矩阵 | 仅声明可用 | “基本具备”尚无逐项实测证据；后续 Gate 记录 |
| 文档失效引用 | 通过 | 已迁移到新版计划、决策和 Gate 文档；旧 Day 1～7 文件只留 Git 历史 |

## 有条件批准的含义

允许：

- 开始 W01：消除 import side effect、建立 app factory/entry point、修复 package resources。
- 编写与已批准契约一致的测试夹具、schema 和 installed-wheel smoke。
- 按 W01→W05 顺序推进 Gate W1。

不允许据此声称：

- Windows MVP、Beta A/B 或 RC 已完成。
- GUI、截图、云视觉、真实音频/VTS、安装器或真实设备已经通过。
- 已获得独立安全/隐私/许可证审查。
- 私人未签名 artifact 可以公开发布。

## 进入 W01 的冻结约束

1. 生产桌面零监听；dev API 只能显式、安全启用。
2. package resource 与 LocalAppData 分类路径不得依赖 CWD。
3. secret 不得写入 settings、日志、fixture 或 artifact。
4. 新 queue/task/thread/process/file 必须记录 owner、上限、失败和关闭语义。
5. 云视觉默认关闭；W01 不实现截图或上传。
6. 当前文档修改不预先把 W01 或任何后续 PR 标为已实现。

## 2026-07-17 自动证据

- Windows / Python 3.11.15：`uv run pytest -q` → **459 passed, 1 skipped**；唯一跳过项为缺少确定性测试字体。
- branch coverage：**92.58%**，高于 90% 门槛。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：130 个文件已格式化。
- `uv run mypy`：128 个源码文件 strict 检查通过。
- `git diff --check`：通过。
- 已更改/新增 Markdown 的本地相对链接：全部可解析。
- 已删除历史文件名、旧固定日程措辞和错误 ADR 数量的残留引用：零匹配。

这些证据只证明当前源码质量门和 W00 文档一致性，不证明 installed wheel、Windows ACL/DPAPI、GUI、真实设备或安装器。

## 未关闭的残余风险

- RR-W00-01：没有独立人工 reviewer。
- RR-W00-02：云视觉本地检测存在不可消除的 false negative 风险。
- RR-W00-03：安装器具体实现尚未冻结。
- RR-W00-04：设备“基本具备”尚未转化为逐项证据。
- RR-W00-05：私人构建不签名，公开发布保持阻塞。

残余风险详见 [`../security/windows_threat_model.md`](../security/windows_threat_model.md)。任一风险扩大到公开分发、真实用户数据或默认开启云端能力时，必须暂停并重新人工批准。

## 回滚

W00 是纯文档决策。撤回任一 ADR 时，停止其依赖 PR，更新所有者决策、威胁模型和数据流，并把 Gate 状态改回待批准；不得以代码先行来倒逼 ADR。
