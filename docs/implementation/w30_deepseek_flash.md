# W30：DeepSeek V4 Flash 独立接入

> **状态：** 核心实现已作为 [`cd5cd43`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/cd5cd4333ac0ebd6ce0f97a9e5f63e0bdf4f1fb9)
> 推送至 [Draft PR #35](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/35)。本地完整自动化质量门及
> [审计 head `e35dbc7`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/e35dbc70a87588531c3134a56bf1c960fe6d2ebc)
> 的 Windows/macOS quality 与 installed-wheel CI 均已通过；真实 DeepSeek Key 连通性仍未验证。后续 head 变更必须重新核验 CI。
>
> **最后核验：** 2026-07-30（Asia/Shanghai）；本文创建时的基线为
> `codex/w30-deepseek-flash@b09841c13f1a733ec267027df62da6da7fc31fb6`。
>
> **隔离：** W29 为单独、暂停的用户工作树；其 TTS/VTS 改动、测试、PR 与验收不属于 W30，未被本任务改写。

## 已确认的范围

- 采用固定 DeepSeek Flash 文本 Provider：`https://api.deepseek.com/chat/completions`、`deepseek-v4-flash`、
  `thinking: {"type":"disabled"}`。[官方 API](https://api-docs.deepseek.com/api/create-chat-completion/)
- 使用独立 current-user DPAPI 密钥；`DEEPSEEK_API_KEY` 仅供显式非桌面开发运行面回退，桌面即使是 `dev` 也不读取它。
  不复用通用 LLM 密钥、YAML 或日志。
- 已有近期历史、长期记忆检索、视觉 feature 与提示词预算保持原有开关。W30 的视觉出口限于新鲜、非敏感、单次获准的
  `ApprovedVisualSummary` 有限语义标签；本地将其映射为固定摘要文本，不接受自由文本。泛用 `PerceptionContext`、
  observation ID、原始图片、image URL、OCR 原文、窗口标题和路径不在出口中，出站摘要含 URL/路径时也 fail closed。
- 聊天输入保留单次视觉摘要同意控件；由于 W30 未接入合规摘要生产器，它当前明确显示“尚未接入合规的视觉摘要来源”并
  保持不可选。后续任务只有在确有合规、可发送的语义标签时才能启用该控件，且服务端仍重新执行视觉 feature、同意、新鲜和
  敏感 gate。
- 不接入 `deepseek-v4-pro`，不做长期记忆候选提取/写入；候选分析开启时必须拒绝 DeepSeek。

## 开源复用调研与决策

- 已核对官方 [`openai/openai-python`](https://github.com/openai/openai-python) 项目：其当前维护状态、Apache-2.0
  许可证和 SSE/错误处理能力可作为选型对照。
- **决策：不新增 SDK，也不复制其代码。** W30 明确要求直连 HTTPS 且不新增 SDK；现有
  `OpenAICompatibleLLMProvider` 已使用项目既有 `httpx` 路径提供 TLS、显式代理、超时、SSE 和结构化 JSON
  处理。专用 DeepSeek 类只冻结 endpoint/model/thinking 与本地安全拒绝，因此没有引入新的许可证或依赖边界。
- 这项调研不证明 DeepSeek 服务兼容性；固定协议仍需由 MockTransport 自动化与用户显式的非敏感真实 Key 检查分别验证。

## 已执行的自动化和产物检查

- 聚焦 provider、通用兼容回归、bootstrap、DPAPI、prompt、CLI、桌面 safe-mode、设置管理和窗口测试：`172 passed in
  8.01s`。其中包含固定 URL/model/Bearer/thinking、SSE/JSON/错误映射、multipart/image/tool-call 拒绝、环境回退
  限制、candidate-analysis fail-closed、DPAPI replace 后回滚、无密钥快照和有限视觉标签 gate。
- 全套 `uv run pytest`：`1379 passed, 3 skipped`，总覆盖率 `90.39%`。三个 skip 都是既有可选环境条件（RapidOCR、Pillow
  和本地目录 symlink 权限），不是 W30 测试失败。
- `uv run ruff check .`、`uv run ruff format --check .`（`262 files already formatted`）、`uv run mypy`（`255 source files`）、
  `uv lock --check`（`65 packages`）和 `git diff --check`：全部通过。
- `uv build --wheel --out-dir dist/w30-wheel-final` 成功；随后以该 wheel 运行
  `tools/w05_ci_smoke.py`，安装后 smoke 的 `status` 为 `ok`，并确认 source tree 未被导入。该本地 provenance 标记为
  `local-unrecorded`，不是发布 artifact。
- W30 涉及文档的仓库内相对链接检查通过；对当前 39 个已改/未跟踪文件运行候选 secret/header 正则扫描未发现匹配。

以上为本地自动化证据，不替代真实 API、远端隐私政策或最终 PR head 的 CI。

## 当前证据状态

| 断言 | 状态 | 证据/限制 |
| --- | --- | --- |
| W30 需求、固定 Flash、Pro 延后、既有开关复用 | 已确认 | 所有者 2026-07-30 指令与 ADR-W30。 |
| DeepSeek API 的 chat-completion 形状与多轮请求方式 | 已确认 | 官方 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 与 [多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)。 |
| V4 仅作为本任务文本 Provider | 已确认 | 官方集成说明；不把图像支持写入 W30。 |
| 当前实现、MockTransport、DPAPI、UI 和 prompt gate 的自动化结果 | 本地与 PR 自动化已验证 | 核心实现为 `cd5cd43`；聚焦 `172 passed`；完整 `1379 passed, 3 skipped, 90.39%`，并通过 lint/type/lock/build/smoke/link/sensitive 扫描。PR #35 的审计 head `e35dbc7` 四项跨平台 CI 都通过；后续 head 需重审。 |
| 真实 Key、账户权限、服务可用性、计费和真实远端响应 | 未验证 | 本任务未持有或请求真实 Key，自动化不得联网。 |
| DeepSeek 远端处理/保留/地域政策 | 外部服务边界 | 以 [隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html) 为准；本项目不能替代该政策或作零保留承诺。 |

## 已执行的聚焦命令与后续交付项

```text
uv run pytest --no-cov \
  tests/unit/test_deepseek.py \
  tests/unit/test_openai_compatible.py \
  tests/unit/test_bootstrap.py \
  tests/unit/test_secret_store.py \
  tests/unit/test_perception_prompt_context.py \
  tests/unit/test_desktop_safe_mode.py \
  tests/unit/test_cli.py \
  tests/unit/ui/test_w30_deepseek_management.py \
  tests/unit/ui/test_window.py \
  tests/unit/ui/test_w16_management.py
```

上述聚焦命令得到 `172 passed in 8.01s`；完整质量门结果见上一节。核心变更已按 `cd5cd43` 推送到 Draft PR #35，
审计 head `e35dbc7` 的远端检查已通过。该 PR 保持 Draft，后续变更均需再次核验当前 head；不得用 MockTransport 绿灯替代真实 API
或隐私验收。

## 剩余风险与人工项

- 用户提供 Key 后，可显式进行一次不含历史、长期记忆或视觉摘要的非敏感连通性检查；不将 key、请求正文或响应正文
  放入日志、测试、文档或 PR。
- 真实服务的账号状态、限流、区域、价格、模型版本、保留与政策变更属于外部状态，需在使用前由所有者复核。
- 视觉摘要接线不是实际截图捕获、OCR 质量或敏感窗口 false-negative 的证据；这些仍属于独立视觉/隐私任务。

## 回滚

使用专用设置操作停用 DeepSeek，使 `llm.provider=none` 生效并重启；然后撤销专用 DPAPI 密钥。不得通过修改
通用 endpoint/model、复用通用密钥或重新启用图像内容来绕过该回滚。
