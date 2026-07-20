# W15：单实例、托盘和统一生命周期

## 状态与范围

> 状态：实现与本地自动化已完成；Draft PR
> [#27](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/27) 的首次 macOS CI 类型失败已修复，
> 运行代码 head `ec46df9fd699af9ceb9581d96100de5d8a3dfce2` 的远端 CI 已通过。
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
- `uv run pytest`（跨平台 type-compatibility 修复后）→ `1118 passed, 3 skipped in 151.49s`；总覆盖率
  `90.09%`，满足项目 90% 门槛。
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
