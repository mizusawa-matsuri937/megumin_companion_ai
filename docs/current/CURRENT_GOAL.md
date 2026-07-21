# 当前产品目标

> 最后核验：2026-07-21（Asia/Shanghai）。当前活跃任务是 **W16：设置、feature 与记忆管理最小 UI**，
> 开发分支为 `codex/w16-settings-feature-memory-ui`。下方记录的历史 head 均已完成远端 CI；W16 自动交付
> 只由 PR **当前** exact head 的必需检查全部 `success` 判定。任何新提交（包括本证据记录）都不继承旧 head 的绿灯。

## 已确认事实

- 所有者已明确授权开始 W16；这覆盖本文件旧版本中“尚未授权 W16”的历史状态。
- W15 已由 PR #27 合并到 `agent/windows-development-baseline`。W16 从其后当前基线
  `87801bc` 开始，开始前工作树干净。
- W16 初始实现提交 [`0dfad2f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/0dfad2f6039bc30c3273a004586fb0484f01c0d7)
  已推送到该开发分支；[Draft PR #28](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/28) 已打开，
  base 为 `agent/windows-development-baseline`。状态记录 head
  [`c1cce43`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/c1cce43bfdcd5d435a9ef9d1cb2c89b53e12ed49)
  的 [push CI #29809797158](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29809797158) 与
  [pull-request CI #29809800285](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29809800285)
  均为 `success`；每个 run 的 macOS/Windows quality 与 installed-wheel 均通过。核验时 PR 仍为 Draft、
  `OPEN`、`MERGEABLE/CLEAN`，且没有 review、issue conversation 或 inline review comment。任何后续提交
  都必须以新的精确 head 重新核验。
- 后续 CI-evidence head [`cd56622`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/cd5662242495dfe064f30c7c34818b96fbb71c58)
  的 [push CI #29810283413](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29810283413) 与
  [pull-request CI #29810286263](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29810286263)
  也均为 `success`；每个 run 的 macOS/Windows quality 与 installed-wheel 均通过。
- 2026-07-21 的实际 Windows Qt 审阅发现：用户在 feature 启用确认框选择“是”后，界面仍显示
  `希望停用 / 已停用 / ——`。本地 feature 元数据也未变化，证明请求没有送入后端。根因已复现：本机 PySide6
  原生 `QMessageBox.question()` 返回底层 `int`，它与 `StandardButton.Yes` 值相等但不是同一对象；旧
  `_confirm()` 使用 `is`，因而把明确的“是”静默当作取消。当前修复改用值比较，并加入该返回形态的回归测试；
  这个修复提交仍须通过新的 exact-head CI。
- W16 依赖 W10 与 W15，沿用 W03 的当前用户 DPAPI/受管路径边界、W10 的 feature 状态机和逻辑删除、
  W13/W14 的 Qt 主线程与有界 BackendThread bridge。它不启动、暴露或复用开发 HTTP API。
- 产品范围仍是单机、单 Windows 用户、个人私用。多用户、跨用户 DACL 有效访问、RDP、快速切用户和跨
  session 行为均为**范围外**，不得写作已验证或待 W16 人工 Gate。

## 当前 W16 目标与完成条件

1. 在 Qt 桌面中提供 LLM/TTS/VTS/STT 路径/设备和启动项设置；任何密钥或 VTS 令牌只可直写当前用户的
   DPAPI 加密存储，UI 不回填、不显示、不持久化明文。
2. 展示 feature 的 desired、actual、过渡与 failed 状态。关闭 Vision/Proactive 必须先等待既有强屏障，
   再报告最终状态。
3. 提供最近历史清空、长期记忆的 list/search/confirm/edit/delete/export；所有危险操作都要在用户点击后
   再次确认，并诚实呈现逻辑删除与底层清理待处理语义。
4. 在视觉开关旁固定披露指定窗口范围、cloud vision 独立开关和可能的云端出口；不能暗示 W16 已实现真实
   截图或云上传。Debug 只展示无内容 capability、稳定错误码、版本和队列占用。
5. 以自动化验证可复现的配置写入、DPAPI 命令边界、feature 屏障、记忆操作、导出、UI 状态与隐私边界；
   完成聚焦提交、推送、Draft PR、exact-head CI 后才可报告 W16 自动交付完成。

## 已完成的本地实现与自动化证据

- 已加入秘密值不可见的设置/管理 bridge 契约、BackendThread 内 `DesktopManagementRuntime`、四页 Qt
  设置对话框和主窗口入口。文件、DPAPI、SQLite、导出均在 BackendThread；Qt 只发有界 typed command。
- 设置写入只合并用户 YAML 层，不会把开发环境覆盖复制回持久配置；明文 secret 字段被配置写入层拒绝。
  真实 LLM 保存前要求已存 DPAPI 密钥和模型名；GPT-SoVITS 保存前要求已有有效 preset，真实服务预检仍由
  W19 负责。
- feature 过渡开始时会先发布完整 desired/actual 快照，随后等待状态机 barrier 并发布最终/failed 状态。
- `uv run pytest` → `1134 passed, 3 skipped in 161.77s`；总覆盖率 `90.37%`，达到项目 90% 门槛。三个 skip
  分别是未安装的可选 RapidOCR/Pillow 能力与当前用户不能创建目录符号链接，均由测试框架明确标记，非 W16
  失败。
- `uv run mypy app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` 和
  `uv run mypy --platform darwin app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py`
  均为 `Success: no issues found in 218 source files`。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv lock --check` 和 `git diff --check` 均通过。
- 聚焦测试覆盖了 secret 不回显、用户层写入不吸收开发覆盖、real-provider/preset 保存防线、过渡快照先于
  强屏障最终状态、memory CRUD/export、错误码不暴露原始异常、二次确认、可访问的 Qt 表面和最终 wipe。
- 新增 `test_w16_feature_enable_submits_when_pyside_returns_integer_yes`，并以
  `uv run pytest --no-cov tests\\unit\\ui\\test_w16_management.py -q` 验证 `13 passed in 2.71s`；它锁定了
  PySide6 返回 `int(Yes)` 时必须提交而非取消的行为。

## 未完成项与真实人工 Gate

- W16 自动交付的客观条件是 PR 当前 exact head 的所有必需检查均为 `success`；这项条件必须在最终报告前
  现场核验，且任何后续提交都要重新满足它。任何客观失败必须直接记录和修复，不能转交人工确认。
- 仅保留 AI 无法忠实复现的实际 Windows Qt 理解性审阅：用户是否正确理解历史、记忆、视觉、云端和
  删除语义。该 Gate 尚未执行，不能标记为通过。
- 真实音频设备、麦克风、VTS/GPT-SoVITS 服务与资源预检属于 W17–W19；真实指定窗口捕获/cloud vision
  属于 W21/W22；冻结包与安装/卸载属于 W24/W25。它们不是 W16 已实现能力。

## 相关资料

- 权威 W16 计划：[Windows 开发计划](../windows_development_plan.md)
- 当前实现记录：[W16 设置、feature 与记忆管理 UI](../implementation/w16_settings_feature_memory_ui.md)
- 运行时/desktop 所有权边界：[ADR-W01](../adr/ADR-W01-runtime-topology.md)、
  [ADR-W03](../adr/ADR-W03-windows-storage-security.md)、[ADR-W04](../adr/ADR-W04-control-protocol.md)
- W10 feature/删除语义：[W10 实现记录](../implementation/w10_memory_state_recovery.md)

## 维护规则

W16 的验证结果、提交、PR、CI、人工 Gate 或残余风险发生实质变化时，先更新本快照及
`implementation/w16_settings_feature_memory_ui.md`，再报告状态。计划、mock/headless 结果或旧 W15
证据不得替代当前 head 的可复核证据。
