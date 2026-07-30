# W30：DeepSeek V4 Flash 独立接入执行计划

> **状态：** 核心实现 `cd5cd43` 已推送至 Draft PR #35，且本地完整自动化质量门已通过；本文仍是范围与验收契约，不以此替代
> 最终 head CI 或真实 API 验证。
>
> **基线：** `codex/w30-deepseek-flash@b09841c13f1a733ec267027df62da6da7fc31fb6`
>
> **范围：** 单机、单 Windows 用户、个人私用；独立于 W29。

## 目标与不变量

- 用户在桌面输入一个 API Key 后可启用固定 `deepseek-v4-flash`，直接调用
  `https://api.deepseek.com/chat/completions`，并固定关闭 thinking。[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- 不新增 SDK、不改变通用 OpenAI-compatible Provider、默认离线/Mock 语义或现有 feature 默认状态。
- 历史、长期记忆检索和视觉摘要只能在各自既有开关已允许时随本轮文本请求发送；W30 不自动启用它们。
- DeepSeek V4 为文本路径：不发送截图、图像 URL、OCR 原文、窗口标题或任何未经脱敏的视觉数据。
- 不实现 Pro、记忆候选提取或长期记忆写入；候选分析开启时拒绝 DeepSeek 配置和启动组合。

## 实施顺序

1. **专用 Provider 与失败语义**
   - 固定 base URL、endpoint、Flash model 和 disabled thinking；保留通用流式/JSON/TLS/代理/限额行为。
   - 在请求离开进程前拒绝 image URL 和任何 non-string multipart content；将任何 response choice 的 tool call 视为拒绝，
     将 SSE、JSON 或 HTTP 错误体中的服务资源不足映射为可重试 unavailable。
   - 更新 Provider token-estimator profile，使现有提示词预算继续保守生效。

2. **专用密钥与启动组合**
   - 新建 DeepSeek current-user DPAPI key-id/purpose/file；仅显式非桌面开发运行面可使用 `DEEPSEEK_API_KEY` 回退，
     桌面即使为 `dev` 也只读专用槽。
   - bootstrap 按 `llm.provider=deepseek` 选择专用 Provider，并在通用 model 校验之前处理固定模型。
   - 在 provider 组合边界及 memory candidate analyzer 的实际构造点 fail closed，绝不让 Flash 充当写入模型。

3. **桌面设置与撤销**
   - 增加 `DeepSeekFlashConfigureCommand` 和 `DeepSeekFlashDisableCommand`；命令 payload 中的 key 不可 repr/回显。
   - 启用只持久化 provider 选择，保持通用 base/model/endpoint；先 reload 验证持久化设置、再写专用密钥，成功后发布
     无密钥配置状态并要求重启。DPAPI post-replace 校验失败必须还原旧密文；此前没有槽位时管理层另行 revoke 部分写入。
     命令在终态事件前锁定设置对话框，避免重复启停或草稿竞态。
   - 停用先切换为 `none`，再撤销专用密钥；失败时保持远程 Provider 已禁用。

4. **文本视觉摘要接线**
   - 组合现有 memory prompt context 与一个仅接受 future privacy producer 创建的 `ApprovedVisualSummary` 的上下文源；
     该类型只含有限、非敏感语义标签、时间和敏感标记，本地映射为固定文本，不接受自由文本。因此它不能承载 frame、
     OCR、窗口标题、路径或 `observation_id`；泛用 `PerceptionContext` 不能进入 prompt。
   - 发送条件同时包括 feature、单次 `screen_context_allowed`、新鲜度及 `sensitive=false`；摘要按现有 screen budget 裁剪、
     标记 untrusted/non-persistable，并在预算压力下最先被移除。
   - 不增加截图采集、OCR、云视觉或原图上传；摘要在出站前再次脱敏并拒绝 URL/路径形式。没有合规摘要时本轮不附加
     视觉内容。

5. **文档、验证和发布**
   - 更新 ADR、数据流、威胁模型、目标快照与索引；明确 DeepSeek 出站文本和远端政策风险。
   - 先运行聚焦 unit/UI/integration 测试，再运行完整质量门、类型/格式、依赖/产物/敏感信息扫描。
   - 只暂存 W30 文件、建立聚焦提交、推送 `codex/w30-deepseek-flash` 并创建/更新 Draft PR；W29 不进入 diff。

## 接口与验收

| 接口 | 约束 |
| --- | --- |
| `DeepSeekFlashLLMProvider` | 固定 endpoint/model/thinking；每条 message content 只接受 string；不允许 caller 覆盖。 |
| `DeepSeekFlashConfigureCommand` | key 为 write-only、`repr=False`；写专用 DPAPI 并切换 provider。 |
| `DeepSeekFlashDisableCommand` | 先禁用 provider，再撤销专用密钥。 |
| `SettingsSnapshot` | 只暴露 `deepseek_flash_configured` 等布尔状态，不暴露 key、header、路径或远端正文。 |
| 视觉 prompt source | 只接受有限语义标签的 `ApprovedVisualSummary`，不传递 source/observation ID 或自由文本；仅在 feature/单次同意/新鲜/非敏感同时满足时输出。 |

最小自动化矩阵：

- MockTransport 断言 URL、model、Authorization、disabled thinking、stream/JSON、TLS/重定向与完成原因。
- 图像、multipart 文本、tool call、空/错误密钥、限流、超时和资源不足均以稳定、无正文的错误失败；通用 Provider 回归不变。
- fake DPAPI 覆盖 key-id/purpose 隔离、存储、撤销、post-replace 回滚、配置失败/撤销失败顺序、YAML/事件/log 无密钥。
- 覆盖历史/长期记忆/视觉的开关、单次同意、新鲜度、敏感标记、预算降级、原始 `PerceptionContext` 以及 OCR/title/path
  自由文本无法构造成批准上下文、不传递 observation ID 的断言。
- 真实 API 不在自动化中调用。用户明确提供 Key 后才执行一次非敏感手动连通性检查；这只能证明当时的服务/账号组合，
  不证明隐私政策、保留或长期可用性。[隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)

## 回滚与发布阻断

- 回滚为 `llm.provider=none` 并撤销专用密钥；保留通用兼容配置和其密钥，不在 W30 中删除。
- 以下任何一项阻断交付声明：真实 API Key 未提供、真实连通性未验证、测试/质量门失败、密钥或视觉敏感数据出现在
  diff/artifact、或 W29 改动混入 W30。
- W30 的视觉摘要接口不是“实时截图理解”完成声明；生产截图捕获仍是独立任务。
