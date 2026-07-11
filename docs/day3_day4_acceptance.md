# Day 3 / Day 4：配置、日志与最小 API 验收记录

> 日期：2026-07-11
>
> 最终状态：已通过
>
> 代码与自动化验收：通过
>
> 进程启动/关闭冒烟：通过
>
> Day 3 人工脱敏验收：已于 2026-07-11 通过
>
> Day 4 API 边界审阅：已于 2026-07-11 通过

## 1. Day 3 完成内容

- `config.yaml` 保存当前阶段实际使用的非敏感配置；未提前加入 Memory、Plugin、Screen、TTS 或 VTS 配置空壳。
- `.env.example` 只声明密钥变量名和无效占位值，`.env` 继续由 Git 忽略。
- YAML 配置经过严格 Pydantic 校验，拼错或多余字段不会被静默忽略。
- `.env` 后可由进程环境覆盖；支持当前 app、server、logging、LLM 字段的显式环境变量覆盖。
- 默认 `llm.provider: none`，所以 Phase 0 无密钥也能启动。改为真实 Provider 后，缺失密钥、`replace_me` 占位值或缺失密钥变量名都会给出中文操作提示。
- 密钥值存放在模型私有属性并用 `SecretStr` 返回，不进入 `Settings.model_dump()`。
- 控制台与 JSONL 文件使用同一个递归脱敏器，覆盖：已配置密钥值、常见 API key/Bearer token、邮箱、中国大陆手机号、验证码、中国大陆身份证号和常见银行卡号。
- turn 日志只记录 ID、输入模式和文字长度，不记录消息正文或 metadata。
- 提供 `python -m app.config.redaction_check` 人工检查入口；只接受以 `fake-day3-` 开头的假密钥，防止误用真实密钥。

## 2. Day 4 完成内容

- 新增 `UserMessage`、`DialogueSegment`、`TTSJob`、`AudioResult`、`TurnState` 五个 Pydantic Schema，以及这些契约直接需要的 `InputMode` / `TurnStatus` 枚举。
- `UserMessage.input_mode` 只开放当前实际使用的 `text` 和 `voice`；主动发话模式将在对应开发日加入。
- `UserMessage` 自动生成消息 ID、默认本地 session 和 UTC 时间，文字会去除首尾空白并拒绝纯空白输入。
- `TurnService.accept()` 是文字与语音输入的唯一后端汇合点，当前只创建 `accepted` 状态，不提前实现 Day 5 LLM 流水线。
- `POST /api/chat` 与 `WS /ws/client` 只做协议校验/转换，然后调用同一个 `TurnService`。
- `GET /health` 返回服务状态与版本；`WS /ws/echo` 提供独立 WebSocket 冒烟测试。
- FastAPI lifespan 初始化配置、日志和 TurnService，并在关闭时写入停止事件、刷新 handler。
- HTTP 与 WebSocket 校验错误不回显提交的原始输入值，降低草稿或私密内容进入错误响应的风险。
- 未创建 Memory、Plugin、Screen、AssistantMessage 或其他当前未使用 Schema/目录。

## 3. 依赖变更及必要性

| 类型 | 依赖 | 用途 |
| --- | --- | --- |
| 运行时 | PyYAML | 解析架构指定的 `config.yaml` |
| 运行时 | python-dotenv | 正确解析本机 `.env`，并让进程环境保持更高优先级 |
| 开发 | httpx2 | Starlette/FastAPI 当前 TestClient 的正式依赖，用于 HTTP 与 WebSocket 集成测试 |
| 开发 | types-PyYAML | 在 mypy strict 下检查 PyYAML 调用 |

没有加入 STT、LLM SDK、TTS、音频、VTS、数据库、截图、OCR 或桌面 UI 依赖。

## 4. 自动化证据

2026-07-11 执行：

```text
uv run pytest -q          20 passed
uv run ruff check .       All checks passed
uv run ruff format --check .  18 files already formatted
uv run mypy               Success: no issues found in 18 source files
```

覆盖内容包括：

- YAML、`.env` 和进程环境优先级；缺配置、错字段、缺密钥与占位密钥错误。
- 日志文件中的嵌套密钥、邮箱、手机号、验证码、身份证号和银行卡号脱敏。
- 五个 Schema 的序列化、两种输入模式和关键交叉字段校验。
- `/health`、WebSocket echo、HTTP chat、WebSocket user.message 与安全校验错误。
- 使用记录型 TurnService 证明 `text` 和 `voice` 依次进入同一个服务实例。
- lifespan 的 `application.started` / `application.stopped` 日志顺序。

真实进程冒烟使用 `uv run python app/main.py` 启动，`GET http://127.0.0.1:8765/health` 返回：

```json
{"status":"ok","service":"megumin-companion-ai","version":"0.1.0"}
```

随后发送 `Ctrl+C`，进程退出并记录 `application.stopped`。

## 5. 人工关卡状态

### A. Day 3 假密钥脱敏

**状态：已通过。** 项目所有者于 2026-07-11 按 README 步骤使用 `fake-day3-...` 无效值完成人工检查，确认控制台和 `data/logs/app.jsonl` 中的密钥、邮箱、手机号和验证码均无明文。检查过程未向 Codex 提供真实密钥。

### B. Day 4 协议边界

**状态：已通过。** 项目所有者于 2026-07-11 确认以下协议和 Schema 边界符合 Day 4 预期：

1. `app/api/routes.py` 只负责协议校验、响应封装和调用 TurnService，没有 LLM/TTS/记忆业务。
2. `app/core/turns.py` 让 `text` 与 `voice` 共用同一个 `accept()` 入口。
3. `app/schemas/messages.py` 只有 Day 4 要求的五个 BaseModel，没有提前创建 Memory、Plugin 或 Screen Schema。
4. 当前 HTTP/WS 接收消息后仅返回 `accepted` TurnState；真实回复属于 Day 5 以后。

Day 3、Day 4 的自动化验收、进程冒烟和全部人工关卡均已通过，本阶段正式关闭。
