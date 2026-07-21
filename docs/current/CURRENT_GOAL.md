# 当前产品目标

> 最后核验：2026-07-21（Asia/Shanghai）。**W16：设置、feature 与记忆管理最小 UI** 已由
> [PR #28](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/28) 合并到
> `agent/windows-development-baseline`，merge commit 为 `dc84319d13e0330321a9f284d4b4685a12615a0f`。关闭 head
> `6c5fd83c5d3aff3f2fe58b584a456754564b89a6` 的 pull-request run `29825814677` 与 push run `29825811679`
> 均为 `success`；合并后 `uv sync --locked` 和 `uv run pytest` 也通过（`1134 passed, 3 skipped in 157.59s`，
> 总覆盖率 90.39%）。当前尚未收到 W17 或后续 W 项的实施授权。

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
  修复提交 [`7175588`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/7175588ec101398b0f8ef7201f10d82c79b4abb9)
  的 [push CI #29820123292](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29820123292) 与
  [pull-request CI #29820126134](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29820126134)
  均为 `success`。这只是该修复提交的历史证据，任何随后 head 仍须独立核验。
- 修复后，用户已在实际 Windows Qt 中重新执行该精确路径并报告通过：feature 启用、确认“是”、状态更新均正常。
  这是本次缺陷的实际桌面复测证据；它不替代历史、记忆、视觉、云端与删除语义的完整理解性审阅。
- 所有者已于 2026-07-21 授权受控合并 PR #28。closing head `6c5fd83` 的八项远端 macOS/Windows quality 与
  installed-wheel 检查均成功后，PR #28 于 2026-07-21T11:30:59Z 合并；合并后的本地基线已 fast-forward 到
  `dc84319` 并完成 `uv sync --locked` 和全量测试。该事实不把仍未执行的隐私语义理解性审阅写成已通过。
- 合并后的 docs-only 关闭记录 [`78eac32`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/78eac32bc154aadc5eb206566f2d92008e1c1ff3)
  首次 push run [#29826924703](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29826924703)
  的 Windows quality 在既有 `test_default_headless_smoke_uses_w14_runtime_and_keeps_stdout_as_json` 和
  `test_configured_desktop_runtime_restarts_with_a_body_free_snapshot` 处失败；GitHub 日志没有给出断言正文。该
  commit 相对已绿 merge tree 仅改两份 Markdown；两项精确测试本机连续 20 轮（40 个断言）通过，`--failed`
  重跑同一 job 也成功。因此**合理推测**为 Windows runner 时序波动，不能把一次重跑成功写成永久稳定。
- 后续状态记录 head [`8f87c73`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/8f87c733cd43c7d6714f26616f8542de4e53df2a)
  的 [pull-request CI #29820584704](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29820584704)
  已成功；[push CI #29820582676](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29820582676)
  第一次尝试只在既有
  `test_close_during_cache_promotion_removes_wav_and_partial_files` 的 1 秒异步等待超时。相同 SHA 的 PR Windows
  quality 和本机聚焦复现均通过，重跑失败 job 的第二次尝试也为 `success`。现有证据**合理推测**为 CI 调度波动，
  但不能据此宣称该既有测试已永久稳定；PR 当前 exact head 仍须现场读取检查状态。
- W16 依赖 W10 与 W15，沿用 W03 的当前用户 DPAPI/受管路径边界、W10 的 feature 状态机和逻辑删除、
  W13/W14 的 Qt 主线程与有界 BackendThread bridge。它不启动、暴露或复用开发 HTTP API。
- 产品范围仍是单机、单 Windows 用户、个人私用。多用户、跨用户 DACL 有效访问、RDP、快速切用户和跨
  session 行为均为**范围外**，不得写作已验证或待 W16 人工 Gate。

## W16 完成状态与边界

1. 在 Qt 桌面中提供 LLM/TTS/VTS/STT 路径/设备和启动项设置；任何密钥或 VTS 令牌只可直写当前用户的
   DPAPI 加密存储，UI 不回填、不显示、不持久化明文。
2. 展示 feature 的 desired、actual、过渡与 failed 状态。关闭 Vision/Proactive 必须先等待既有强屏障，
   再报告最终状态。
3. 提供最近历史清空、长期记忆的 list/search/confirm/edit/delete/export；所有危险操作都要在用户点击后
   再次确认，并诚实呈现逻辑删除与底层清理待处理语义。
4. 在视觉开关旁固定披露指定窗口范围、cloud vision 独立开关和可能的云端出口；不能暗示 W16 已实现真实
   截图或云上传。Debug 只展示无内容 capability、稳定错误码、版本和队列占用。
5. 已以自动化验证可复现的配置写入、DPAPI 命令边界、feature 屏障、记忆操作、导出、UI 状态与隐私边界，
   并完成聚焦提交、推送、PR exact-head CI、受控合并与合并后基线验证。

## 已完成的本地实现与自动化证据

- 已加入秘密值不可见的设置/管理 bridge 契约、BackendThread 内 `DesktopManagementRuntime`、四页 Qt
  设置对话框和主窗口入口。文件、DPAPI、SQLite、导出均在 BackendThread；Qt 只发有界 typed command。
- 设置写入只合并用户 YAML 层，不会把开发环境覆盖复制回持久配置；明文 secret 字段被配置写入层拒绝。
  真实 LLM 保存前要求已存 DPAPI 密钥和模型名；GPT-SoVITS 保存前要求已有有效 preset，真实服务预检仍由
  W19 负责。
- feature 过渡开始时会先发布完整 desired/actual 快照，随后等待状态机 barrier 并发布最终/failed 状态。
- `uv run pytest` → `1134 passed, 3 skipped in 154.51s`；总覆盖率 `90.37%`，达到项目 90% 门槛。三个 skip
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
- 2026-07-21 的 Windows CI 两次在相同 W16 代码之外的真实时间边界测试失败：一次为 GPT-SoVITS cache
  promotion 的一秒等待，另一次为首字节 50ms/81ms 边界及 mock WAV 300ms 总时限。当前修改仅扩大**测试**的
  调度余量，未改变产品超时逻辑：首字节成功/失败分别使用 2s/100ms 与 10s 延迟，mock WAV 验证使用 3s
  总时限。两组测试连续 20 轮、每轮 4 个断言均通过；失败 head 的
  [PR CI #29823189335](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29823189335) 是历史
  反例。测试稳定化 head [`523406d`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/523406dfc57bbf4e75aadfe407e5858495b4b976)
  的 [push CI #29825077879](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29825077879) 与
  [pull-request CI #29825080662](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29825080662)
  均为 `success`，各自的 macOS/Windows quality 和 installed-wheel job 也均通过。现场审计时 PR #28 为
  Draft / `OPEN` / `CLEAN`，没有 review、普通评论或 inline review comment；该 head 仍会随着后续证据提交
  成为历史记录，不能替代下一 head 的检查。

## 未完成项与真实人工 Gate

- W16 的自动交付、PR 合并和合并后基线验证均已完成。closing head `6c5fd83` 的远端检查和 merge commit
  `dc84319` 上的本地全量测试是可复核证据；任何后续 W 项仍须在自己的 exact head 上独立验证。客观失败
  必须直接记录和修复，不能转交人工确认。
- 实际 Windows Qt 已通过本次 feature 启用/确认/状态更新路径的复测。仍保留 AI 无法忠实复现的理解性审阅：
  用户是否正确理解历史、记忆、视觉、云端和删除语义；其余部分尚未执行，不能把整个 Gate 标记为通过。
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
