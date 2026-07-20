# 当前产品目标

> 最后核验：2026-07-21（Asia/Shanghai）。W15 的未提交工作树已完成本地自动化核验：
> `uv run pytest` 为 `1118 passed, 3 skipped`、总覆盖率 90.07%；严格类型、lint、格式和锁文件
> 检查亦已通过。该证据来自 W15 首次提交前的工作树；W15 尚未推送或创建 PR，因此它不是远端
> exact-head CI 证据。Draft PR [#27](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/27)
> 已创建；其最终 head 的远端 CI 仍待核验。

## 已确认事实

- W14 的 PR [#26](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/26) 已于
  2026-07-20 合并到 `agent/windows-development-baseline`，merge commit 为
  `0a9f199f01201e14a342a28798a2ca86e6287e25`。本地 W15 分支从该提交创建。
- 项目所有者已明确要求开始 **W15：单实例、托盘和统一生命周期**；这替代了本文件先前
  “等待 W14 人工 Gate 后不得启动 W15” 的旧状态。
- 当前实现分支为 `codex/w15-single-instance-lifecycle`；W15 Draft PR
  [#27](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/27) 的目标为
  `agent/windows-development-baseline`，尚未完成远端 exact-head CI 或人工 Windows Gate；不得把它
  表述为已关闭、已合并或可发布。
- W15 仍受 ADR-W01、ADR-W07、ADR-W08、Windows 数据流不变量与 P0-04 风险约束：Qt
  只拥有 UI/托盘，BackendThread 的应用 lifespan 仍拥有 turn、worker、VTS、memory 和日志。

## 当前目标与完成条件

在不扩大到 W16 设置 UI、W17 音频设备或 W25 安装器的前提下交付 W15：

1. 使用当前用户、当前 session 的 securable primitive 保证单实例；第二实例只能请求主窗口
   显示，不能传入任意命令或文本。
2. 交付托盘的显示/隐藏、停止当前 turn、静态隐私总览与退出，并将窗口关闭的默认行为明确为
   “隐藏到托盘”；没有可用托盘时走受控退出。
3. 将 UI 关闭、BackendThread、应用 lifespan 的 worker/VTS/memory/logs 关闭、异常标记与
   有界进程级 deadline 纳入同一 owner graph；不得将 Python thread timeout 表述为已安全停止。
4. 增加默认关闭的当前用户启动项、路径变更/stale value 清理 API 与 crash marker v1；异常后
   安全模式必须不自动恢复 vision/cloud vision/proactive。
5. 在最终 head 上完成自动化、严格类型/格式检查、聚焦审查、提交、推送与 Draft PR；随后明确
   列出无法自动化的真实 Windows Gate。

## 已完成的自动化证据（提交前工作树）

- `uv run pytest --no-cov tests\\unit\\ui\\test_w15_lifecycle.py tests\\unit\\test_desktop_startup.py`
  `tests\\unit\\test_desktop_safe_mode.py tests\\unit\\test_config.py::test_repository_config_matches_packaged_default -q`
  → `14 passed`。它覆盖 current-user/current-session primitive、opaque per-session marker scope、
  重复退出、关闭到托盘、hung backend deadline、固定 tray actions、safe-mode feature 状态、HKCU
  stale startup path 与配置默认值。
- `uv run pytest` → `1118 passed, 3 skipped in 157.23s`；总覆盖率 `90.07%`（达到 90% 门槛）。
- `uv run mypy app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` →
  `Success: no issues found in 215 source files`；`uv run ruff check .`、
  `uv run ruff format --check .` 与 `uv lock --check` 均通过。
- 上述记录对应提交前的同一工作树；提交、推送和 Draft PR 后必须重新核验远端 exact head。

## 未验证项与人工 Gate

- 自动化/current-user 测试不能证明不同 Windows 用户对 kernel object 的有效访问控制，也不能
  替代真实 RDP/快速切用户 session 行为；需要标准用户、多 session Windows Gate。
- Explorer 重启后的实际托盘重建、锁屏/注销/关机时序、任务栏通知区可见性和冻结包的无控制台窗口
  仍必须在真实 Windows 环境验证。模拟托盘只证明模拟条件。
- W25 才负责真正安装器/卸载流程；W15 只提供固定 HKCU Run value 的协调与卸载清理 API，不能
  宣称已验证真实卸载。
- 已推送并创建 W15 Draft PR #27；最终 head 的远端 exact-head CI 尚未完成/核验。

## 相关资料

- 权威计划的 W15 段落：[`../windows_development_plan.md`](../windows_development_plan.md)
- 进行中的实现记录：[`../implementation/w15_single_instance_lifecycle.md`](../implementation/w15_single_instance_lifecycle.md)
- 运行拓扑与关闭边界：[`../adr/ADR-W01-runtime-topology.md`](../adr/ADR-W01-runtime-topology.md)、
  [`../adr/ADR-W07-native-worker-isolation.md`](../adr/ADR-W07-native-worker-isolation.md)
- 包装/崩溃恢复边界：[`../adr/ADR-W08-packaging-upgrade.md`](../adr/ADR-W08-packaging-upgrade.md)

## 维护规则

当 W15 的目标提交、自动化结果、PR、Gate 或人工验证状态发生实质变化时，先更新本文件和对应实现记录，
再报告状态。不得以聊天结论、模拟结果或旧 PR 状态替代最终 head 上的可复核证据。
