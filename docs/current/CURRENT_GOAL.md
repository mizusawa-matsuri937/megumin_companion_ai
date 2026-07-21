# 当前产品目标

> 最后核验：2026-07-21（Asia/Shanghai）。W15 的最新本地工作树已完成自动化核验：
> `uv run pytest` 为 `1118 passed, 3 skipped`、总覆盖率 90.07%；Windows 与 macOS 模拟的严格
> 类型检查、lint、格式和锁文件检查亦已通过。Draft PR
> [#27](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/27) 的首次 head 曾在 macOS
> strict mypy 失败，原因和修复见下；修复后的运行代码 head
> `ec46df9fd699af9ceb9581d96100de5d8a3dfce2` 已通过 macOS/Windows quality 和两项 installed-wheel。
> 随后的文档 head `819e782bac71e2521580be8054891963512eec08` 暴露一项既有集成测试的错误时序前提；
> 本工作树已改为确定性门控，最终交付仍只以匹配最终 head 的远端检查为证据。
> `0c902494e9880151a0b1b59d3ae0154ccd6dbf86` 的 push CI 全部通过，但同一 head 的 PR CI 在 Windows
> 暴露另一项既有 GPT-SoVITS 资源追踪测试的意外 deadline 依赖；本工作树只为该非 deadline 测试设置
> 明确且仍有界的宽松 deadline，生产逻辑与 deadline 边界测试均未改变。

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
- `uv run pytest`（包含两项 CI 测试稳定性修复后）→ `1118 passed, 3 skipped in 128.11s`；总覆盖率
  `90.07%`（达到 90% 门槛）。
- `uv run mypy app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` →
  `Success: no issues found in 215 source files`；另行执行
  `uv run mypy --platform darwin app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py`
  也在 215 个 source files 上通过。`uv run ruff check .`、
  `uv run ruff format --check .` 与 `uv lock --check` 均通过。
- PR #27 的首次 exact head `f53e04d241749d0a5370d05031f011e08e892c6f` 中，两个 installed-wheel
  和 Windows quality 已通过；macOS quality 的 strict mypy 有 16 个错误，因为 macOS typeshed 不公开
  Windows-only `winreg` 成员及 `ctypes.get/set_last_error`。W15 代码现以受控动态平台适配解决，
  并由上述 Darwin mypy 验证。修复后的运行代码 head `ec46df9fd699af9ceb9581d96100de5d8a3dfce2` 的
  pull-request run `29770004752` 和 push run `29770000078` 均为 success：macOS/Windows quality 以及
  两项 installed-wheel 共八个检查全部通过。
- 后续文档 head `819e782bac71e2521580be8054891963512eec08` 的 pull-request run `29770563063` 中，
  Windows quality 仅在 `tests/integration/test_mock_pipeline.py::test_new_input_is_a_hard_barrier_for_old_turn_events`
  失败：旧测试用固定 `asyncio.sleep(0.05)` 假定第一轮尚未结束，却没有建立该前提，CI 中因而未观察到
  `turn.cancelled`。这不足以证明 W15 产品代码回归。测试现用 `FirstTurnBarrierLLM` 明确等待第一轮进入
  可取消阻塞点；修复后的断言连续运行 20 次、受影响文件 4 项和完整套件均通过。
- `0c902494e9880151a0b1b59d3ae0154ccd6dbf86` 的 push run `29771649297` 全部通过；同一 head 的
  pull-request run `29771653545` 仅在 Windows quality 的
  `tests/unit/test_gpt_sovits.py::test_uncached_audio_part_and_final_are_tracked_until_discard` 失败，结果为
  `tts_cancel_timeout`。该测试的目标是临时资产追踪，却无意中继承 `timeout_ms=300` 和
  `cancellation_timeout_ms=50` 的共享 deadline；同一提交的另一远端运行和 20 次本地隔离运行均通过，
  因而这是 CI 调度敏感的合理推测，而非已证实的生产逻辑缺陷。该测试现在显式使用
  1000/1000/3000/500 ms 的 connect/first-byte/total/cancellation deadline；专门的 deadline 边界测试
  保持不变，整个 GPT-SoVITS 文件 57 项也通过。

## 未验证项与人工 Gate

- 自动化/current-user 测试不能证明不同 Windows 用户对 kernel object 的有效访问控制，也不能
  替代真实 RDP/快速切用户 session 行为；需要标准用户、多 session Windows Gate。
- Explorer 重启后的实际托盘重建、锁屏/注销/关机时序、任务栏通知区可见性和冻结包的无控制台窗口
  仍必须在真实 Windows 环境验证。模拟托盘只证明模拟条件。
- W25 才负责真正安装器/卸载流程；W15 只提供固定 HKCU Run value 的协调与卸载清理 API，不能
  宣称已验证真实卸载。
- W15 Draft PR #27 仍未合并。远端 CI 只能在其匹配当前最终 head 时作为交付证据；即使通过，仍须完成
  下述真实 Windows Gate 和评审，不能视为已合并或可发布。

## 相关资料

- 权威计划的 W15 段落：[`../windows_development_plan.md`](../windows_development_plan.md)
- 进行中的实现记录：[`../implementation/w15_single_instance_lifecycle.md`](../implementation/w15_single_instance_lifecycle.md)
- 运行拓扑与关闭边界：[`../adr/ADR-W01-runtime-topology.md`](../adr/ADR-W01-runtime-topology.md)、
  [`../adr/ADR-W07-native-worker-isolation.md`](../adr/ADR-W07-native-worker-isolation.md)
- 包装/崩溃恢复边界：[`../adr/ADR-W08-packaging-upgrade.md`](../adr/ADR-W08-packaging-upgrade.md)

## 维护规则

当 W15 的目标提交、自动化结果、PR、Gate 或人工验证状态发生实质变化时，先更新本文件和对应实现记录，
再报告状态。不得以聊天结论、模拟结果或旧 PR 状态替代最终 head 上的可复核证据。
