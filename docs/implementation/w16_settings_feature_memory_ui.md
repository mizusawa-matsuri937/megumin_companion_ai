# W16：设置、feature 与记忆管理最小 UI

## 状态与范围

> 状态：初始实现提交 [`0dfad2f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/0dfad2f6039bc30c3273a004586fb0484f01c0d7)
> 已推送，[Draft PR #28](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/28) 已打开；历史 head
> `c1cce43` 与 `cd56622` 的远端 CI 已全绿。自动交付只由 PR 当前 exact head 的必需检查全部 `success`
> 判定，任何新提交均不继承旧 head 结果。最后本地核验：2026-07-21（Asia/Shanghai）。
> 本记录只描述当前工作树已实现和已验证的部分，不把计划、headless UI 或 fake DPAPI 当作真实 Windows
> 理解性/设备验收。

W16 在 W10/W15 已合并基线上实现最小桌面管理面：secret-free 设置、feature 状态、历史与长期记忆管理、
视觉/云端隐私披露和无内容 Debug。它不实现 W17–W19 的真实设备/服务闭环，也不实现 W21/W22 的真实
窗口捕获或云视觉上传。

## 已实现的边界

- Qt 主线程只渲染和提交有界 typed command；`DesktopManagementRuntime` 在既有 BackendThread 和应用
  lifespan 内运行。没有 TCP/HTTP、开发 API 或跨进程管理面。
- `DesktopSettingsForm` 和设置快照不含 secret；LLM 密钥与 VTS 令牌使用现有当前用户 DPAPI 文件直接写入。
  密码控件提交成功即清空，关闭对话框和最终窗口关闭也清空 Qt 所持副本；事件和结果只带稳定错误码。
- `patch_user_settings()` 仅读取并合并用户 YAML layer，拒绝明文 secret 字段，并复用既有原子写入/备份。
  因而开发环境覆盖不会被误持久化。
- 真正的 LLM 需要已存 DPAPI 密钥与非空模型名；TTS 只允许 `mock` 或已有默认 preset 的 GPT-SoVITS，防止
  W16 把下一次启动写入必然失败的配置。服务连通性、VTS Allow、模型/preset 可见性仍由 W19 preflight 负责。
- 复用 W10 feature 状态机。管理面在 `request_transition` 后发布完整状态快照，再等待已有 handler/barrier，
  最后发布 final/failed 状态；Vision/Proactive 的关闭按钮明确显示等待停用屏障。
- 记忆 list/search 受 20 项上限和预览上限约束；详情、编辑、逻辑删除、历史/记忆清空和导出均走既有 W10
  user-id 约束。导出要求用户选定绝对目标，拒绝符号链接/非普通文件，以临时同目录文件、`fsync` 与 replace
  完成。删除/清空结果显式带 `cleanup_pending`，不承诺 SSD、备份或快照物理擦除。
- 启用任何 feature，以及删除、清空、导出、编辑、secret revoke、记忆建议批准/拒绝都要求第二次确认。视觉
  文案固定说明指定窗口范围、cloud vision 独立开关与可能云端出口，并明确 W16 未接入真实截图/云上传。
- Debug 页只显示版本、capability、稳定错误码和 bridge 队列计数/容量；不显示消息、记忆、路径、原始异常或
  secret 内容。

## 自动化证据（本地当前工作树）

- `uv run pytest` → `1133 passed, 3 skipped in 177.36s`，总覆盖率 `90.37%`（项目要求为 90%）。三个 skip
  是未安装的可选 RapidOCR/Pillow 能力与当前用户不能创建目录符号链接，均为环境能力缺口，不是 W16
  断言失败。
- `uv run mypy app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py` 和
  `uv run mypy --platform darwin app desktop_client tests tools\\installed_wheel_smoke.py tools\\w05_ci_smoke.py`
  均为 `Success: no issues found in 218 source files`。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv lock --check`、`git diff --check` 已通过。
- 当前 PR status-record head `c1cce43` 的 cross-platform quality gate 在
  [push CI #29809797158](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29809797158) 与
  [pull-request CI #29809800285](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29809800285)
  均为 `success`；每个 run 的 macOS/Windows quality 和 installed-wheel job 均通过。
- 后续 CI-evidence head `cd56622` 的
  [push CI #29810283413](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29810283413) 与
  [pull-request CI #29810286263](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/29810286263)
  也均为 `success`；每个 run 的 macOS/Windows quality 和 installed-wheel job 均通过。
- W16 的聚焦覆盖包括 secret 不回显、用户层写入不吸收开发覆盖、真实 provider/preset 保存防线、过渡快照
  先于强屏障最终状态、memory CRUD/export、稳定错误码不回显后端异常、二次确认、可访问 Qt 表面、空态/边界
  输入和最终敏感状态 wipe；同时将既有 `CountingMemoryStore` 测试替身同步到 W16 新增的 get/limit 协议。

## 未验证项、风险与人工 Gate

- 聚焦提交、推送、Draft PR 和已记录的历史 head CI 均已完成；W16 自动交付仍只在 PR 当前 exact head
  全部必需检查为 `success` 时成立。最终报告必须现场确认该动态状态，不能把旧 head 或 headless 结果替代它。
- W16 的唯一候选人工项是实际 Windows Qt 中对历史、记忆、视觉、云端和删除语义的理解性审阅。它尚未执行。
  配置写入、状态机、取消/关闭竞争和可合成故障必须先由自动化证明，不能改列人工 Gate。
- 真实 VTS、GPT-SoVITS、音频输入/输出、屏幕捕获、cloud vision、冻结包和安装器均为后续 W 任务范围；
  本实现不声称它们可用。
- 用户手工写入并使配置 schema 本身无效时，应用无法安全构造设置 UI；W16 防止通过本 UI 新写入已知的
  real-LLM/TTS 启动前置条件错误，但不把任意损坏配置恢复能力冒充为已实现。

## 回滚

移除 W16 对话框、管理 bridge 契约和 BackendThread 管理 runtime，即可回到 W15 桌面 shell；该回滚不应删除
已有用户 YAML、DPAPI secret 或 W10 数据库。需要撤回单项设置时，使用现有配置/secret 的受控写入和 revoke
路径，不手工扫描或批量删除用户目录。
