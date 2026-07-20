# 可复用工程经验

> 本页只收录能在现有计划、实现记录、测试或已发布 PR 中找到依据的结论。具体实现细节仍以对应 Wxx 记录为准。

## 1. 自动化绿灯不等于真实 Windows 产品证据

- **已确认：** Mock/headless、CI 和跨平台源码质量门不能证明真实设备、Windows ACL/DPAPI、IME、视觉体验、
  安装升级或屏幕阅读器听感。
- **来源：** [`../windows_development_plan.md`](../windows_development_plan.md)、
  [`../implementation/w13_pyside6_desktop_skeleton.md`](../implementation/w13_pyside6_desktop_skeleton.md)。
- **行动：** 先自动化可验证断言，再把不可替代的真实行为写成小而明确的人工 Gate；绝不以“测试全绿”宣布 MVP。

## 2. 发布和合并必须绑定 exact head

- **已确认：** PR 的 base、head、临时 merge ref 和检查结果可能变化；历史通过记录不能自动覆盖新提交。
- **来源：** [`../standards/AGENT_OPERATING_CONSTRAINTS.md`](../standards/AGENT_OPERATING_CONSTRAINTS.md)、
  [`../implementation/w05_installed_artifact_ci.md`](../implementation/w05_installed_artifact_ci.md)。
- **行动：** 审计证据记录目标提交；合并前重读 PR 状态和最终检查，以 expected-head guard 执行，合并后再验证远端。

## 3. 安装产物与源码运行是不同的证明对象

- **已确认：** 能从源码树运行或能构建 wheel，不证明 wheel 能在仓库外独立运行。
- **来源：** [`../implementation/w01_app_factory_and_resources.md`](../implementation/w01_app_factory_and_resources.md)、
  [`../implementation/w05_installed_artifact_ci.md`](../implementation/w05_installed_artifact_ci.md)。
- **行动：** 用隔离环境和源码 quarantine 验证安装产物，检查任意 CWD、资源解析和无源码 fallback；不要只看 build 成功。

## 4. 与时间有关的 fixture 必须可控

- **已确认：** 固定日历时间会随真实 wall clock 老化，导致 TTL、保留、replay 或幂等测试成为 date bomb。
- **来源：** 根目录 [`AGENTS.md`](../../AGENTS.md) 的 W 任务规则，以及 W06 的时钟确定性修复历史。
- **行动：** 能注入就使用共享可控时钟并显式推进；必须接系统时钟的集成测试，在执行前用当前 UTC 创建 fixture。

## 5. 当前状态不能由旧 handoff 推断

- **已确认：** 根目录 `HANDOFF.md` 停在 W05，而已核验的当前目标是 W14 Draft PR；两者并不等价。
- **来源：** [`../current/CURRENT_GOAL.md`](../current/CURRENT_GOAL.md) 和根目录 `HANDOFF.md`。
- **行动：** 新会话先读 `AGENTS.md`、`docs/README.md` 和 `docs/current/CURRENT_GOAL.md`，再按任务路由读取历史资料。
