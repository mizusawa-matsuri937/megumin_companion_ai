# W30：DeepSeek V4 Flash 独立接入

> **2026-07-31 后续状态（优先于下方较早快照）：** Gateway compatibility 已由 `84c92bb` 推送。夹具修复提交
> `a0ccfc6` 只调整测试夹具的事件同步和非 deadline 场景的时限余量；其 exact-code head 的 push workflow
> `30615939286` 和 PR workflow `30615942372` 均通过 Windows/macOS quality 与 installed-wheel 四项检查。此前
> `a0b5661` 的 PR workflow 发现的一项直连 GPT-SoVITS cancellation/settlement 与三项 Gateway MockTransport
> 夹具时序失败，已在该精确代码 head 上复核通过。

> **状态：** 核心实现已作为 [`cd5cd43`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/cd5cd4333ac0ebd6ce0f97a9e5f63e0bdf4f1fb9)
> 推送至 [Draft PR #35](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/35)。本地完整自动化质量门及
> [审计 head `e35dbc7`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/e35dbc70a87588531c3134a56bf1c960fe6d2ebc)
> 的 Windows/macOS quality 与 installed-wheel CI 均已通过。2026-07-31 所有者授权的既有本地 Gateway compatibility
> 跟进已完成原始本地自动化、静态检查、wheel/smoke 与敏感信息扫描；夹具修复 `a0ccfc6` 的 exact-code CI 也已通过。
> 真实 DeepSeek Key 连通性仍未验证，后续 head 变更必须重新核验 CI。
>
> **最后核验：** 2026-07-31（Asia/Shanghai，`a0ccfc6` exact-code CI 已通过；最终 PR 审计证据以 Draft PR 元数据为准）；本文创建时的基线为
> `codex/w30-deepseek-flash@b09841c13f1a733ec267027df62da6da7fc31fb6`。
>
> **隔离：** W29 为单独、暂停的用户工作树；其 TTS/VTS 改动、测试、PR 与验收不属于 W30，未被本任务改写。
> 2026-07-31 的 Gateway client 是在 W30 内单独实现的最小协议兼容边界，不能借用 W29 的验收证据。

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
- 兼容已有 `gpt-sovits-gateway` 配置：只接受三个既有别名、固定数值 loopback endpoint、专用
  `tts-gateway-token` current-user DPAPI 槽与 protocol header。它不能使用 cache、代理或自定义 CA，也不能
  改写 W30 的直连 GPT-SoVITS style 词表、回退 Mock、启动 Gateway 或向 DeepSeek 传递 token/音频。

## 2026-07-31 既有本地 Gateway compatibility 跟进

用户报告 W30 desktop 显示 `desktop_runtime_unavailable`，同时文字聊天不可用。经本机非秘密配置和 SQLite 完整性检查，
已确认根因是：现有 `tts.provider=gpt-sovits-gateway` 属于 W29 的私有本地 Gateway 协议，而 W30 原先只识别
`mock`、`gpt-sovits`、`gpt_sovits`。它不是名称拼写别名；已有设置也没有直连 GPT-SoVITS 必需的 preset，因此简单
改名或回退 Mock 都会掩盖配置错误。

经所有者 2026-07-31 明确授权，W30 在自己的工作树加入以下最小适配，而不修改 W29：

- `GPTSoVITSGatewayProvider` 只连接 `http://127.0.0.1:9880/v1/health` 与 `/v1/tts`，每个请求带专用 Bearer
  和 `X-TTS-Gateway-Protocol: 1`，同时验证响应 header、health JSON、内容类型、上限和 WAV。
- Gateway secret 固定为 `tts-gateway-token` / `tts.gateway-bearer` purpose-bound current-user DPAPI 槽；它不复用
  DeepSeek、通用 LLM 或 VTS token。缺少或 purpose 错误时安全失败，绝不改为 Mock。
- 只在 Gateway client 内把 W30 `TTSJob.emotion` 映射为五个 voice slot；未知值在网络前返回稳定错误。直连
  GPT-SoVITS 的 preset/reference 与 style 行为不变。
- 设置保存允许 Gateway 不填写 direct-provider 的 reference/preset；显式预检用 health 加固定 neutral 合成作为服务检查，
  将不适用的 preset/reference 显示为 `tts_gateway_active` skipped。UI/event/log 不接收 token、正文或临时音频路径。
- 该 client 禁用 `trust_env`、redirect、proxy、custom CA 和持久音频 cache；不安装、启动、升级、认证或证明 Gateway
  进程及其下游服务安全。

## 开源复用调研与决策

- 已核对官方 [`openai/openai-python`](https://github.com/openai/openai-python) 项目：其当前维护状态、Apache-2.0
  许可证和 SSE/错误处理能力可作为选型对照。
- **决策：不新增 SDK，也不复制其代码。** W30 明确要求直连 HTTPS 且不新增 SDK；现有
  `OpenAICompatibleLLMProvider` 已使用项目既有 `httpx` 路径提供 TLS、显式代理、超时、SSE 和结构化 JSON
  处理。专用 DeepSeek 类只冻结 endpoint/model/thinking 与本地安全拒绝，因此没有引入新的许可证或依赖边界。
- 这项调研不证明 DeepSeek 服务兼容性；固定协议仍需由 MockTransport 自动化与用户显式的非敏感真实 Key 检查分别验证。
- 为 Gateway compatibility，重新核对官方 [`RVC-Boss/GPT-SoVITS`](https://github.com/RVC-Boss/GPT-SoVITS) 的
  现行 API v2 与 [MIT 许可证](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/LICENSE)。上游 `/tts` 需要
  reference/prompt 等较宽字段，未提供本项目私有的 authenticated `/v1/health`/`/v1/tts` 协议，因此没有适合
  直接引入的开源 client；没有复制上游代码、添加依赖或启动其服务。既有 GPT-SoVITS security advisory/服务边界仍以
  W19 记录为准。

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
- 初始 W30 交付时，涉及文档的仓库内相对链接检查通过；对当时 39 个已改/未跟踪文件运行候选 secret/header
  正则扫描未发现匹配。
- **Gateway compatibility 本地跟进（2026-07-31）：** `84c92bb` 已推送，Gateway core、bootstrap/secret、设置保存、
  preflight 以及直连 GPT-SoVITS 回归的聚焦命令为 `196 passed in 8.97s`。此前 `a0b5661` 的 PR workflow `30614461677`
  在 Windows quality 发现一项直连 GPT-SoVITS cancellation/settlement 和三项 Gateway MockTransport 夹具时序失败；
  `a0ccfc6` 仅修正夹具同步与非 deadline 场景的时限余量。对该代码重跑 `uv run pytest` 得到
  `1432 passed, 3 skipped in 210.13s`，总覆盖率 `90.45%`；其 push `30615939286` 与 PR `30615942372` 均四项通过。
  三个 skip 仍是既有可选 RapidOCR、Pillow 和本地目录 symlink 权限条件。
- Gateway 跟进的最终静态门：`uv run ruff check .`、`uv run ruff format --check .`（`266 files already formatted`）、
  `uv run mypy`（`259 source files`）、`uv lock --check`（`65 packages`）与 `git diff --check` 均通过。
- `uv build --wheel --out-dir dist/w30-fixture-ci-stabilization` 成功；随后 `w05_ci_smoke.py` 的 installed-wheel `status`
  为 `ok`，且 `source_tree_imported=false`。该本地 provenance 为 `local-unrecorded`，不是发布 artifact。

以上为本地自动化证据，不替代真实 API、远端隐私政策或最终 PR head 的 CI。

## 当前证据状态

| 断言 | 状态 | 证据/限制 |
| --- | --- | --- |
| W30 需求、固定 Flash、Pro 延后、既有开关复用 | 已确认 | 所有者 2026-07-30 指令与 ADR-W30。 |
| DeepSeek API 的 chat-completion 形状与多轮请求方式 | 已确认 | 官方 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 与 [多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)。 |
| V4 仅作为本任务文本 Provider | 已确认 | 官方集成说明；不把图像支持写入 W30。 |
| 当前实现、MockTransport、DPAPI、UI 和 prompt gate 的自动化结果 | 本地与 PR 自动化已验证 | 核心实现为 `cd5cd43`；聚焦 `172 passed`；完整 `1379 passed, 3 skipped, 90.39%`，并通过 lint/type/lock/build/smoke/link/sensitive 扫描。PR #35 的审计 head `e35dbc7` 四项跨平台 CI 都通过；后续 head 需重审。 |
| W30 既有本地 Gateway compatibility | 本地与 `a0ccfc6` exact-code CI 已验证 | 40 项 Gateway MockTransport 测试，fake-DPAPI/bootstrap/secret，headless settings/preflight 及直连 GPT-SoVITS 回归均通过。`a0ccfc6` 只修改测试夹具；完整 `1432 passed, 3 skipped, 90.45%`、静态/lock/wheel/smoke 检查通过，其 push `30615939286` 与 PR `30615942372` 均四项通过。未访问真实 Gateway。 |
| 真实 Key、账户权限、服务可用性、计费和真实远端响应 | 未验证 | 本任务未持有或请求真实 Key，自动化不得联网。 |
| DeepSeek 远端处理/保留/地域政策 | 外部服务边界 | 以 [隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html) 为准；本项目不能替代该政策或作零保留承诺。 |

## 已执行的聚焦命令与后续交付项

```text
uv run pytest --no-cov \
  tests/unit/test_deepseek.py \
  tests/unit/test_openai_compatible.py \
  tests/unit/test_gpt_sovits_gateway.py \
  tests/unit/test_bootstrap.py \
  tests/unit/test_secret_store.py \
  tests/unit/test_perception_prompt_context.py \
  tests/unit/test_desktop_safe_mode.py \
  tests/unit/test_cli.py \
  tests/unit/ui/test_w30_deepseek_management.py \
  tests/unit/ui/test_window.py \
  tests/unit/ui/test_w16_management.py \
  tests/unit/ui/test_w19_provider_preflight.py \
  tests/unit/test_gpt_sovits.py \
  tests/unit/test_mock_clients.py
```

原 DeepSeek 聚焦命令得到 `172 passed in 8.01s`；加入 Gateway compatibility 的扩展聚焦集合得到 `196 passed in
8.97s`，当前夹具修复后的完整质量门结果见上一节。核心变更已按 `cd5cd43` 推送到 Draft PR #35，审计 head `e35dbc7`
的远端检查已通过；`a0ccfc6` 的 code head 也已由双工作流重新核验。不得用 MockTransport 绿灯替代真实 API、真实 Gateway
或隐私验收。

## 剩余风险与人工项

- 用户提供 Key 后，可显式进行一次不含历史、长期记忆或视觉摘要的非敏感连通性检查；不将 key、请求正文或响应正文
  放入日志、测试、文档或 PR。
- 真实服务的账号状态、限流、区域、价格、模型版本、保留与政策变更属于外部状态，需在使用前由所有者复核。
- 视觉摘要接线不是实际截图捕获、OCR 质量或敏感窗口 false-negative 的证据；这些仍属于独立视觉/隐私任务。
- Gateway 的固定 HTTP loopback endpoint 不证明监听进程身份或 TLS 机密性。同用户恶意进程可能抢占端口并接收 bearer 和
  待合成文本；固定地址、purpose-bound token、无 proxy/redirect 和响应校验只能限制误配置，不能消除此主机本地风险。

## 回滚

使用专用设置操作停用 DeepSeek，使 `llm.provider=none` 生效并重启；然后撤销专用 DPAPI 密钥。不得通过修改
通用 endpoint/model、复用通用密钥或重新启用图像内容来绕过该回滚。

Gateway compatibility 不删除或重写既有 token。若需停止它，可显式把 `tts.provider` 切为 `mock` 或恢复受支持的直连
GPT-SoVITS 配置；不得通过把 Gateway 名称伪装为 `gpt-sovits`、复用其他 provider secret 或让启动静默回退来掩盖失败。
