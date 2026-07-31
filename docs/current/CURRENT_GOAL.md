# 当前产品目标

> **2026-07-31 后续状态（优先于下方较早快照）：** Gateway compatibility 已作为 `84c92bb` 推送。相同 head 的
> push workflow `30612675642` 四项均通过；PR workflow `30612678482` 唯一失败为未被 Gateway 改动触及的 W08
> Mock TTS fixture：其 controller 在一秒内未观察到 writer，属于 runner 调度竞态而非 Gateway client 缺陷。该 fixture
> 已改用 `asyncio.Event`、受控 `loop.time` 和 finally 释放，仍覆盖“超时后 drain owned thread，再清理 registry/WAV”。
> 修复后的本地完整门为 `1432 passed, 3 skipped, 90.46%`；修复提交与其 exact-head CI 尚待形成证据。

> **最新状态更新：2026-07-31（Asia/Shanghai）。** 当前唯一活跃任务为 W30「DeepSeek V4 Flash
> 独立接入」。工作树为 `codex/w30-deepseek-flash`，基线为
> `b09841c13f1a733ec267027df62da6da7fc31fb6`。W30 核心实现已由
> [`cd5cd43`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/cd5cd4333ac0ebd6ce0f97a9e5f63e0bdf4f1fb9)
> 推送到 [Draft PR #35](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/35)，本地完整自动化质量门和
> 审计 head `e35dbc7` 的 Windows/macOS quality、installed-wheel CI 均已通过。所有者现已授权一项最小的既有本地
> GPT-SoVITS Gateway 兼容跟进；它已通过本地自动化、静态检查、wheel/smoke 与敏感扫描，但尚未形成新已推送 head 的
> CI 证据。真实 DeepSeek Key 连通性仍待完成，任何新 head 必须重新核验其 CI。

## W30 目标与已确认边界

- 为桌面用户提供仅需输入 API Key 的专用 DeepSeek 路径：固定
  `https://api.deepseek.com/chat/completions`、`deepseek-v4-flash` 和
  `thinking: {"type":"disabled"}`。不新增 SDK，也不改变通用 OpenAI-compatible Provider 的配置语义。
- DeepSeek 在 W30 中只作为文本 Provider。当前用户文字，以及用户已显式开启的近期历史、长期记忆**检索**和
  合规视觉摘要文本，才可随该轮请求发送；客户端负责随请求携带多轮历史。
  [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
  和[多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)是该协议边界的外部依据。
- 视觉支持仅限由未来隐私生产器创建的 `ApprovedVisualSummary` 有限非敏感语义标签：本地将标签映射为固定文本，
  不接受自由文本。视觉 feature 开启、单条 `UserMessage.screen_context_allowed=true`、摘要新鲜且 `sensitive=false`
  必须同时成立。泛用 `PerceptionContext`、observation ID 和其他资产标识不是 prompt 入口；摘要是不可信、不可持久化的
  参考上下文。
- 严禁把原始/脱敏截图、图像 URL、窗口标题、OCR 原文、bbox、屏幕路径、observation ID 或未脱敏摘要发送到 DeepSeek；
  出站摘要没有自由文本入口，并会额外拒绝 URL/路径。W30 不将 DeepSeek V4 表述为图像模型。
- 使用独立、purpose-bound 的 current-user DPAPI 槽保存 DeepSeek Key；不写入 YAML、通用 LLM 密钥槽、事件、
  日志、异常或测试输出。`DEEPSEEK_API_KEY` 仅是显式非桌面开发运行面的明确回退，桌面即使使用 `dev` 配置也不会读取，
  更不能成为桌面持久化方案。
- 不接入 `deepseek-v4-pro`，不做长期记忆候选提取或写入；若
  `memory.candidate_analysis_enabled=true`，配置或启动 DeepSeek 必须拒绝该组合。
- 为恢复既有本机配置，所有者已授权 W30 只兼容 `gpt-sovits-gateway` 及其两个既有别名。它是固定
  `127.0.0.1:9880`、专用 DPAPI bearer token、无 cache/proxy/custom-CA 的本地协议 adapter；它不是直连
  GPT-SoVITS 的改名、不会启动 Gateway、不会回退 Mock，也不改变 DeepSeek 的任何出站字段。

## 当前状态与隔离

| 项目 | 状态 | 可核验依据或限制 |
| --- | --- | --- |
| W30 范围、Flash 默认、Pro 延后与既有开关复用 | 已确认 | 所有者 2026-07-30 指令；[ADR-W30](../adr/ADR-W30-deepseek-flash.md)。 |
| 固定 endpoint/model、文本输入和多轮协议边界 | 已确认 | 官方 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 与[多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)。 |
| W30 代码、MockTransport/DPAPI/UI/prompt gate 的自动化结果 | 本地与 PR 自动化已验证 | `cd5cd43` 的聚焦测试为 `172 passed`；完整 `uv run pytest` 为 `1379 passed, 3 skipped, 90.39%`，且 lint/type/lock/build/smoke/link/sensitive 扫描通过。PR #35 的审计 head `e35dbc7` 四项跨平台 CI 都通过；没有评论或评审，后续 head 必须重审。 |
| W30 既有本地 Gateway compatibility | 本地自动化已验证；exact-head CI 待形成证据 | 2026-07-31 所有者授权；固定 loopback/protocol、专用 token/bootstrap、emotion slot adapter 与 UI/preflight 均已实现。Gateway 40 项 MockTransport、扩展聚焦 `196 passed`、最终完整 `1432 passed, 3 skipped, 90.45%`、Ruff/mypy/lock/wheel/smoke/link/sensitive 扫描通过；不访问真实 Gateway。新 commit/CI 尚待核验。 |
| 真实 API Key、账户权限、远端响应、计费/限流与服务可用性 | 未验证 | 本任务未持有或请求 Key；自动化不得发起真实网络请求。 |
| DeepSeek 的远端处理、保留、地域和政策 | 外部服务边界 | 项目无法保证；以 [DeepSeek 隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html) 为准。 |

W30 是以 W28 基线建立的独立 sibling 任务。W29 为暂停的独立工作树；其 TTS/VTS 改动、测试、PR 和验收状态不在
本任务范围内，也未被改写。2026-07-31 的 W30 adapter 是独立实现的最小协议兼容边界，不能借用 W29 的验收证据。

## 出站隐私提示与默认状态

默认不启用视觉、长期记忆写入或 Pro；W30 也不会自动启用历史、记忆检索或视觉。用户启用 DeepSeek 后，选中的文本
上下文会由 DeepSeek 远端处理。该远端保留、地域和模型改进风险不能由本项目消除，UI 与文档必须明确提示，且不得承诺
“零保留”或“不用于训练”。

## 完成前的验证与交付条件

1. 已完成：DeepSeek Provider、bootstrap、专用 secret store、视觉摘要和桌面设置的 Mock/fake 测试；覆盖固定请求、流式/
   JSON、错误映射、图像/tool-call 本地拒绝、candidate-analysis 拒绝、撤销顺序、无密钥泄漏及预算/gate。
2. 已完成（本地未提交工作树）：完整 pytest、Ruff、format、strict mypy、lock、文档链接、wheel 安装 smoke 与敏感信息扫描；
   三个可选环境 skip 已记录，未把任何客观失败转交人工。
3. 已完成本轮交付审计：核心 W30 变更已作为 `cd5cd43` 推送至 Draft PR #35；审计 head `e35dbc7` 的四项跨平台 CI 通过，
   PR base/head/diff 已复核且没有评论或评审；W29 或无关用户改动未混入。该 PR 仍为 Draft，任何后续 head 均需重新审计。
4. Gateway 跟进的 MockTransport/secret/bootstrap/UI 回归、完整质量门、wheel/sensitive 扫描已经在本地通过；仍须创建并推送
   聚焦 commit，随后重新审核 Draft PR #35 的最终 head CI、base/head/diff/review/mergeability。自动化不调用真实 Gateway
   或 DeepSeek。
5. 用户提供 Key 后，可由用户显式发起一次不带真实历史、长期记忆或视觉摘要的非敏感连通性验证。它只能证明当时的
   账号/网络/服务组合，不证明远端隐私政策或长期可用性；Key、请求正文和响应正文不入仓库或证据。

## 相关资料

- [W30 执行计划](../plans/w30_deepseek_flash_execution_plan.md)
- [W30 实现记录](../implementation/w30_deepseek_flash.md)
- [ADR-W30](../adr/ADR-W30-deepseek-flash.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
