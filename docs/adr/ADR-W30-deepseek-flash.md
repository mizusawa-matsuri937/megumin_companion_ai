# ADR-W30：DeepSeek V4 Flash 的固定文本 Provider

- **状态：** 已按所有者 2026-07-30 指令采用；实现、自动化和真实 API 验证分别记录在 W30 实现记录中
- **决策者：** 项目所有者（兼任架构、安全/隐私和许可证 reviewer）
- **关联计划：** [W30 执行计划](../plans/w30_deepseek_flash_execution_plan.md)
- **实现记录：** [W30 DeepSeek V4 Flash](../implementation/w30_deepseek_flash.md)
- **相关边界：** [Windows 数据流](../architecture/windows_data_flow_inventory.md)、[威胁模型](../security/windows_threat_model.md)

## 背景

现有通用 OpenAI-compatible Provider 能发送文本和可选图像 URL，但不能把“兼容”误解为 DeepSeek V4 已支持图像。
W30 需要让桌面用户只输入一个 DeepSeek API Key 即能启用固定的 Flash 对话路径，同时保留现有历史、长期记忆
检索和视觉 feature 的显式开关与提示词预算。DeepSeek 的 Chat Completions 使用 OpenAI-compatible 形状；多轮上下文由
客户端每次随请求提供。[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
[多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)

官方 DeepSeek V4 集成说明将 V4 描述为文本模型；图像截图由其他视觉模型/代理处理。因此本决策不能把原始屏幕
图片、图像 URL 或 OCR 原文伪装成 DeepSeek 已支持的视觉输入。[GitHub Copilot 集成说明](https://api-docs.deepseek.com/quick_start/agent_integrations/github_copilot/)

## 决策

### 固定 Provider 配置

- 新增专用 `DeepSeekFlashLLMProvider`，不接受用户可编辑的 endpoint、model 或 thinking 模式。
- 请求固定到 `https://api.deepseek.com/chat/completions`，模型固定为 `deepseek-v4-flash`，每次请求固定携带
  `thinking: {"type":"disabled"}`。通用 OpenAI-compatible Provider 的已有配置语义不改变。
- 保留既有 HTTPX、超时、TLS、显式代理、流式、结构化 JSON 和有界响应机制；不新增 SDK 依赖。
- `insufficient_system_resource` 必须映射为可重试的不可用状态（包括 HTTP 错误 JSON）；tool call、图像或任何
  non-string multipart content 都必须在本地拒绝，不能发送后再猜测服务端行为。DeepSeek 请求的每条 message content
  固定为 string。

### 上下文与视觉边界

- 近期历史、长期记忆**检索**和视觉总开关仍由现有 feature 状态控制；W30 不自动打开任一 feature、存储或持久化。
- 已获准的历史与记忆检索沿用既有 token 配额与固定降级顺序；所有外部上下文保持 `persistable=false`。
- 只有当视觉 feature 已启用、当前 `UserMessage.screen_context_allowed=true`、摘要在新鲜期内且未标记敏感时，才把
  future privacy producer 创建的 `ApprovedVisualSummary` 作为不可信的 `ExternalContextBlock` 发送。该能力只含有限的
  非敏感语义标签、时间和敏感标记；本地将标签映射为固定文本，绝不接受自由文本。泛用 `PerceptionContext`、
  `observation_id` 和其他资产标识一律不是 prompt 入口。
- 永不向 DeepSeek 发送原始截图、脱敏图像、图像 URL、窗口标题、OCR 原文、bbox、屏幕路径或未脱敏摘要；Provider
  对 image URL 作第二道本地拒绝。批准摘要在出站前再次脱敏，出现 URL 或路径形式即 fail closed；W30 不新增截图
  捕获、OCR 或云视觉上传生产链路。由于没有自由文本输入，即使未来 producer 使用本地 OCR 或窗口分类辅助决策，
  W30 的出站字段也只能是该有限标签词表。

### 长期记忆写入

- W30 不接入 `deepseek-v4-pro`，不实现候选提取、自动写入或长期记忆的 Pro 路由。
- 若候选分析已启用，DeepSeek 专用配置和 provider 组合边界都必须拒绝该组合（不依赖 storage 是否已开启），确保 Flash
  不会作为未来记忆写入模型被误用。
- 后续写入功能需要单独 ADR，明确 Pro 路由、最小化字段、保留、计费、确认与故障语义。

### 密钥和隐私

- DeepSeek 密钥使用独立的 current-user DPAPI key-id/purpose/file，不复用通用 LLM 密钥；桌面命令、快照、日志、
  YAML、异常和测试输出不得回显值。
- `DEEPSEEK_API_KEY` 只允许由显式标记的非桌面开发运行面（当前为 `megumin-companion-api --dev-api`）使用；桌面即使
  使用 `dev` 配置也只读取专用 DPAPI 密文。生产同样只读取专用 DPAPI 密文。
- 配置/停用顺序必须 fail closed：启用只写 `llm.provider=deepseek`，不覆盖通用兼容配置；停用先切到 `none` 并
  reload，再撤销专用密钥。启用在 DPAPI 写入前完成 provider reload 验证；DPAPI 在 replace 后校验失败时恢复此前的
  加密 envelope，管理层还会撤销此前不存在的部分写入槽。撤销失败也不得恢复远程 Provider。
- 发送到 DeepSeek 的文本由远端服务处理；项目无法保证其地域、保留或模型改进政策。启用前的 UI 和文档必须说明
  这一点，不能承诺“零保留”或“不用于训练”。[DeepSeek 隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)

### 既有本地 GPT-SoVITS Gateway 的启动兼容（2026-07-31）

- 所有者针对已经存在的本机配置，明确授权 W30 增加一个**受限兼容层**。它只识别
  `gpt-sovits-gateway`、`gpt_sovits_gateway` 和 `gateway`；这不是把它们改写为直连
  `gpt-sovits`，也不是自动回退到 Mock。
- 兼容层只连接固定数值 loopback `http://127.0.0.1:9880` 的私有 `/v1/health` 与 `/v1/tts`
  协议，使用独立 current-user DPAPI `tts-gateway-token` 槽和服务响应的协议版本头。它禁止
  缓存、代理、自定义 CA、重定向和可配置远端地址；缺少、损坏或 purpose 不匹配的令牌必须安全失败。
- W30 的直连 GPT-SoVITS 仍使用其原有 preset/reference 和 style 语义。Gateway 只接收有限
  `voice_slot`；兼容 client 在本地以 `TTSJob.emotion` 映射到五个 Gateway 槽，不能修改全局
  emotion mapper 或把未知标签发送出进程。
- 该路径只为了使已有本地配置能够启动。W30 不安装、启动、升级、管理或认证 Gateway 后端，也不把
  Gateway token、文本、WAV 或设置路径发给 DeepSeek。Gateway 后端及其下游处理仍是独立的本机/用户
  服务边界。

## 备选与取舍

- **拒绝复用通用 Provider 配置：** 会允许 endpoint/model/thinking 漂移，且可能把专用密钥写入通用槽。
- **拒绝上传 screenshot/image URL：** 与 V4 文本边界冲突，且扩大隐私出口。
- **拒绝 Flash 兼任记忆写入：** 用户指定写入将来使用 Pro；当前不存在安全的 Pro 数据最小化契约。
- **拒绝在保存密钥时做隐式真实请求：** 保存只改变本机受保护状态；真实远端调用必须由用户显式发起的非敏感聊天/验证触发。
- **拒绝自动启用历史、长期记忆或视觉：** 这些 feature 的同意、保留与关闭屏障属于既有独立语义。

## 验收与残余边界

- 自动化以 MockTransport、假 DPAPI protector、fake prompt context 和 headless Qt 覆盖固定请求、完成原因、拒绝、
  密钥隔离、配置/撤销顺序与上下文 gate；这些不证明真实服务可用或远端保留行为。
- 用户提供真实 Key 后，才可显式进行一次不含真实对话、记忆或视觉摘要的低风险连通性验证；该验证不得写入仓库。
- W29 工作树仍是独立、暂停且未被修改的工作；本 ADR 不改变其 TTS/VTS 代码、测试、PR 或验收结论。
  2026-07-31 的兼容层是 W30 中单独实现、可审计的最小协议适配，不是合并 W29 未完成改动。

## 回滚

停用 DeepSeek、将 `llm.provider` 切回 `none`、重启后确认文字远端 Provider 未构造，再撤销 `deepseek-api-key`。
不得以复用通用密钥、改回 image URL 或自动切换 Mock 的方式掩盖失败。
