# 当前产品目标

> **核验日期：2026-08-01（Asia/Shanghai）。** W19、W28、W29 与 W30/DeepSeek 产品堆栈已经按
> exact-head guard 全部合入 `agent/windows-development-baseline`。当前唯一活跃目标是关闭合并后的
> Windows CI 夹具竞态、同步正式状态，并在关闭变更自己的精确 head CI 全绿后受保护合并。本文不会递归记录
> 承载本文的关闭 PR 自身是否已合并；该交付状态必须从 GitHub 的实时 PR、远端 ref 与最终 baseline 核验。

## 已确认的合并事实

| PR | 被审计 head | 合并目标与 merge commit | 远端状态 |
| --- | --- | --- | --- |
| [#34 W29](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/34) | `d0765e22cc3595230c6e9b2991c5c52a817cd2f5` | W28 → `97c1a8f433ec67b0b7c482788a05260b43e3cf12` | `MERGED` |
| [#35 W30/DeepSeek 组合](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/35) | `13b8c750335fbe62f00476bb23a99c6ca68aae4b` | W28 → `05cf8a6adf0e65fe2ae5e96228ff8688e79e995c` | `MERGED` |
| [#33 W28](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/33) | `1e62266f553bd065262cd7045e2a523e4e2c28f6` | W19 → `51f2e05a2ae040e297b4c839c37920d8859760d9` | `MERGED` |
| [#32 W19](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/32) | `51f2e05a2ae040e297b4c839c37920d8859760d9` | baseline → `83f52e228d8df6df9abf08ce492192752ff113c2` | `MERGED` |

当前已确认产品 baseline 为
`83f52e228d8df6df9abf08ce492192752ff113c2`。#35、#33 与 #32 的最终 exact-head push/PR
workflows 分别为 `30647302622`/`30647308275`、`30650686378`/`30650689973`、
`30651198879`/`30651201100`，每组 macOS/Windows `quality` 与 `installed-wheel` 共 8 项全部成功。
#34 最终 head 的 push `30519958709` 与 PR `30519961163` 同样 8/8 成功。

所有者于 2026-07-31 表示人工审核顺利并明确授权合并全部 PR，包括 DeepSeek。这被记录为对各实现记录中
已列出的主观 Gate 与残余发布风险的接受；没有据此编造具体试听、DPI 或外部服务细节。

## 当前合并关闭工作

baseline push workflow
[30651854952](https://github.com/mizusawa-matsuri937/megumin_companion_ai/actions/runs/30651854952)
的 macOS quality 与双平台 installed-wheel 成功，但 Windows quality 在完整套件中出现两个
`tests/unit/test_mock_clients.py` 失败：

- registry 生命周期测试使用 80/80/300 ms 的非 SLA 预算，在满载 runner 上返回 `tts_total_timeout`；
- wave-generation cancellation 夹具在 writer 完成后才调度取消，存在 operation/cancellation 完成竞态，
  因而未必抛出 `CancelledError`。

baseline merge commit 与被审计 #32 head 的 tree ID 都是
`adfa7c9afc0df191faf524a04ee9f2d3ba47c55e`；同一树在 #32 exact-head CI 中 8/8 成功，因此没有
merge-tree 差异。关闭变更只调整上述测试意图：registry 测试使用 1/1/3 秒调度余量；取消测试以受控
writer barrier 确保取消发生在 writer 所有权仍存续时。产品默认 timeout、deadline 专项测试和运行时代码
均未改变。

当前关闭树的本地证据：

- 两个目标测试在 20 个独立 pytest 进程中 `runs=20 failures=0`；
- 完整 Mock 文件 `18 passed`；TTS/Gateway/结构化链路矩阵 `205 passed`；
- 完整隔离套件收集 1,542 项，`1539 passed, 3 skipped in 221.19s`，coverage `90.38%`；
- tracked Ruff lint、284 文件 format、strict mypy 277 source、根 65 包与 gateway 122 包 lock check 全部通过。

三个 skip 仍是 optional RapidOCR、optional Pillow 和当前账户不能创建目录 symlink，不是本次产品或夹具失败。
关闭分支仍须通过自己的 push/PR 双 workflow、live PR 审计、expected-head merge 与最终 baseline 全套复核，
不能借用上述本地结果或旧 PR 绿灯提前宣布完成。

## 保留的未验证边界

- 真实 DeepSeek Key、账号权限、当前模型/服务可用性、价格、限流、远端停止、地域、保留和政策没有由本任务验证。
- headless Qt 只证明受控逻辑视口中的滚动、可见性与焦点；不替代真实 Windows DPI、桌面 shell、鼠标或键盘体验。
- 所有者接受主观 Gate 不等于项目获得可公开分发的角色、声音、Live2D、模型或第三方服务许可。
- RDP、快速切用户、跨 session 和第二 Windows 用户继续属于当前单机、单用户、私人使用范围外。

## 隐私与工作区边界

- 声音权重、reference WAV、提示文本、生成音频、VTS 模型/入口名称和 ID、token、密钥、真实对话、截图、
  日志、备份与本机私有绝对路径不得进入 Git、fixture、artifact 或 PR。
- 本次关闭提交只允许包含测试夹具和正式状态文档；根 `AGENTS.md` 的用户改动、`.agents/`、coverage、pytest
  临时目录和构建产物必须排除。

## 后续顺序

1. 发布关闭 Draft PR，等待其精确 head 的 push/PR 双 workflow 全部成功并完成 live diff/review/thread 审计。
2. 以 expected-head guard 合并关闭 PR，从远端确认 merge commit 和 baseline ref。
3. 在最终 baseline 隔离运行完整 pytest/coverage、tracked Ruff/format、strict mypy、双 lock 与 wheel/source
   quarantine，并清理本轮精确临时目录。
4. 上述关闭完成后，W19～W30 不再是开放产品任务；下一项产品工作仍按
   [`../windows_development_plan.md`](../windows_development_plan.md) 的 W20～W27 范围另行启动。

## 相关资料

- [Windows 开发执行计划](../windows_development_plan.md)
- [W19 实现记录](../implementation/w19_provider_preflight.md)
- [W28 实现记录](../implementation/w28_avatar_runtime.md)
- [W29 实现记录](../implementation/w29_five_emotion_tts_vts.md)
- [W30 实现记录](../implementation/w30_deepseek_flash.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
