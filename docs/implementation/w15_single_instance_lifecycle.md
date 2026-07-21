# W15：单实例、托盘和统一生命周期

## 状态与范围

> 状态：实现与本地自动化已完成；Draft PR
> [#27](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/27) 的首次 macOS CI 类型失败已修复，
> 运行代码 head `ec46df9fd699af9ceb9581d96100de5d8a3dfce2` 的远端 CI 已通过。后续文档 head
> `819e782bac71e2521580be8054891963512eec08` 暴露既有取消屏障测试的时序前提错误，本工作树已将其
> 改为确定性门控；交付证据必须始终与最终 head 对应。
> 后续 `0c902494e9880151a0b1b59d3ae0154ccd6dbf86` 的 push CI 通过，但 PR CI 在 Windows 暴露另一项
> 非 deadline 测试对紧 deadline 的偶发依赖；本工作树仅放宽该测试 fixture，未改变产品超时行为。
> 实现与测试变更 head `de328c402b40086a9f671d10d6c11be3b7c43d94` 的 pull-request run
> `29793986896` 与 push run `29793985209` 均为 success，八项 macOS/Windows quality 与 installed-wheel
> 检查全部通过；PR 仍为 Draft，真实 Windows Gate 未完成。
> 最后更新：2026-07-21（Asia/Shanghai）。

W15 基于已合并的 W14 merge commit
`0a9f199f01201e14a342a28798a2ca86e6287e25` 开发。它只覆盖桌面进程的单实例、托盘、
启动项协调、crash recovery 和生命周期 owner graph；不实现 W16 的设置页、W17 的真实音频设备、
W20 的 OS 信号或 W25 的安装器。

## 已实现的设计边界

- Windows 使用受当前用户 DACL 保护的 named mutex 与 auto-reset named event。对象名称位于
  `Local\` namespace，并由 SID hash 派生；不同 session 不相互激活。次实例只可 `SetEvent`
  请求“显示窗口”，没有 socket、pipe、payload 或任意命令入口。
- Qt 主线程的托盘只提供固定动作：显示窗口、隐藏窗口、停止当前 turn、静态隐私总览、退出。
  主窗口关闭在托盘可用时默认隐藏；托盘不可用时请求受控退出。定时重申 tray `show()`，以支持
  Explorer 重启后的 Qt 重建尝试；真实行为仍属于人工 Gate。
- `DesktopLifecycle` 是进程级 owner：它驱动 BackendThread 停止，BackendThread 内 FastAPI
  lifespan 再按既有顺序关闭 TurnService、worker、VTS、memory 和日志。只有 backend 已报告停止、
  窗口敏感状态清空且 tray 释放后，才清除 crash marker。
- 生命周期 deadline 是进程级 hard boundary。若 backend/QThread 无法在 deadline 内结束，代码不
  假称 thread 已被安全停止：保留 marker、清空 UI、隐藏 tray，并在生产使用 `os._exit(1)`。
  W12 Job Object 负责 native worker 子树随进程边界终止。
- `desktop.startup_enabled` 默认 `false`。启用时仅协调
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MeguminCompanion`；路径变更替换固定值，
  禁用或 W25 卸载调用 API 时删除固定值。没有 HKLM、服务或计划任务。
- crash marker v1 仅保存 `schema_version` 与 `running` 状态，绝不保存路径、文本、secret 或错误正文。
  文件名使用当前用户与 Windows session 派生的不可逆摘要，因此同一用户的并行 session 不会互相
  清除 crash 证据；未清理/损坏 marker 会进入 safe mode，并持久化关闭 vision、cloud vision 与
  proactive，待 W16 的显式用户操作重新启用。

## 自动化证据（提交前工作树）

- `uv run pytest --no-cov tests\\unit\\ui\\test_w15_lifecycle.py tests\\unit\\test_desktop_startup.py`
  `tests\\unit\\test_desktop_safe_mode.py tests\\unit\\test_config.py::test_repository_config_matches_packaged_default -q`
  → `14 passed`，覆盖 crash marker、current-user Windows named primitive、同 session 次实例激活、
  opaque session marker、关闭到托盘、重复退出、hung backend deadline、固定 tray actions、safe-mode
  feature 状态、HKCU stale startup path 与配置默认值。
- `uv run pytest`（包含两项 CI 测试稳定性修复后）→ `1118 passed, 3 skipped in 128.11s`；总覆盖率
  `90.07%`，满足项目 90% 门槛。
- `uv run mypy app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` →
  215 source files 无类型问题；`uv run mypy --platform darwin app desktop_client tests`
  `tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` 也通过。`uv run ruff check .`、
  `uv run ruff format --check .` 与
  `uv lock --check` 均通过。
- PR #27 的首次 exact head 上，macOS strict mypy 因 typeshed 不公开 Windows-only `winreg` 和
  `ctypes.get/set_last_error` 而失败 16 项；Windows quality 与两个 installed-wheel 均通过。代码现在
  使用受控动态 platform adapter，Darwin mypy 已通过；修复后的运行代码 head `ec46df9` 在
  pull-request run `29770004752` 和 push run `29770000078` 的 macOS/Windows quality 及两项
  installed-wheel 检查全部通过。
- 后续文档 head `819e782bac71e2521580be8054891963512eec08` 的 pull-request run `29770563063` 中，
  Windows quality 只失败于 `test_new_input_is_a_hard_barrier_for_old_turn_events`：固定 50 ms sleep 并不
  保证第一轮仍处于活动状态。测试现在通过 `FirstTurnBarrierLLM` 明确等待第一轮停在可取消位置，再提交
  第二轮；修复后的该断言连续运行 20 次、整个受影响测试文件和完整本地套件均通过。
- `0c902494e9880151a0b1b59d3ae0154ccd6dbf86` 的 push run `29771649297` 全部通过，但同一 head 的
  pull-request run `29771653545` 仅在 Windows quality 的 GPT-SoVITS 临时资产追踪测试失败，返回
  `tts_cancel_timeout`。该测试只验证 `.part`/最终文件的 registry 生命周期，不验证 deadline；它先前
  无意中使用了共享的 300 ms total 与 50 ms cancellation deadline。由于相同 head 的 push 运行及 20 次
  本地隔离运行通过，CI 调度敏感是合理推测，而不是产品缺陷的已证实结论。测试现显式使用
  1000/1000/3000/500 ms deadline，所有 deadline 边界测试保持原样；`test_gpt_sovits.py` 57 项通过。
- 实现与测试变更 head `de328c402b40086a9f671d10d6c11be3b7c43d94` 的 pull-request run `29793986896`
  与 push run `29793985209` 均成功；各自的 macOS/Windows quality 与两项 installed-wheel，共八项
  远端检查全部通过。该 CI 证据不替代残余风险段列出的真实 Windows 验证。

## 残余风险与人工 Gate

- 机器测试不能证明不同用户 SID 的有效访问控制，也无法真实模拟 RDP/快速切用户、Explorer 崩溃恢复、
  锁屏、注销、关机、通知区行为或冻结包没有控制台窗口。
- 在 Windows 标准用户 VM 上至少验证：第二实例只唤醒同 session 主窗口；Explorer 重启后托盘恢复；
  锁屏/注销/关机期间不产生新 turn；托盘退出不留下 backend/worker；冻结包没有控制台窗口。
- W15 不声称已验证安装器 path change/uninstall；W25 必须调用 `remove_for_uninstall()` 并在真实
  安装/卸载矩阵中验证。

## 回滚

将桌面入口恢复为 W14 的直接 `BackendThreadHost`/`MainWindow` 组装，并删除 W15 的 tray、startup、
single-instance 和 marker 代码即可退回不常驻的窗口关闭语义。回滚不得恢复 network IPC、无 DACL 的
命名对象、提权启动项，或将 native worker 关闭重新降级为无限等待。
