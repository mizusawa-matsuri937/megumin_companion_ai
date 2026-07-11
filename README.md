# Megumin Desktop Companion AI

面向 Windows 单用户、个人私用的桌面陪伴 AI 原型。当前仓库已完成 Day 5～7 并通过 Gate A：具备 Mock LLM streaming、多语言分句、Mock TTS、连续有序播放、turn cancellation、延迟指标以及 text/voice 共用的首条纵向链路。

产品范围、隐私与资产边界以 [`docs/day1_scope_freeze.md`](docs/day1_scope_freeze.md) 为准，日计划见 [`docs/daily_development_plan.md`](docs/daily_development_plan.md)，Day 3/4 的证据见 [`docs/day3_day4_acceptance.md`](docs/day3_day4_acceptance.md)，Day 5～7 的实现与人工 Gate A 步骤见 [`docs/day5_day7_acceptance.md`](docs/day5_day7_acceptance.md)。

## 环境要求

- Python 3.11；项目明确不使用 3.12 及以上版本。
- 推荐使用 `uv` 创建隔离环境和安装锁定依赖。
- Git。

## 新环境安装

```powershell
uv python install 3.11
uv sync --all-groups
```

`uv` 会根据 `.python-version` 创建或复用 Python 3.11，并把依赖安装到仓库内的 `.venv`。不要把真实 API key、聊天、截图、记忆、声音或 Live2D 资产放入源码或测试夹具。

## 质量检查

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

自动修复格式：

```powershell
uv run ruff format .
uv run ruff check --fix .
```

## 启动本地后端

默认配置启用完全本地、确定性的 Mock LLM/TTS，不连接真实 LLM，因此不需要 API key 即可启动：

```powershell
uv run python app/main.py
```

启动后可访问 `http://127.0.0.1:8765/health`。按 `Ctrl+C` 会取消活动 turn、清理临时 Mock WAV、释放播放器并刷新日志。HTTP 对话入口为 `POST /api/chat`，打断入口为 `POST /api/interrupt`，WebSocket 入口为 `/ws/client`，独立 echo 冒烟入口为 `/ws/echo`。默认 `playback_mode: silent`，因此后端会产生完整播放事件但不会意外打开音频设备。

## Day 5～7 Gate A 人工验收

先只阅读分句结果：

```powershell
uv run python tools/gate_a_review.py --mode segments
```

确认系统音量较低后，再实际听取有序播放和打断：

```powershell
uv run python tools/gate_a_review.py --mode audio
uv run python tools/gate_a_review.py --mode interrupt
```

完整预期、低音量参数和残留文件检查见 [`docs/day5_day7_acceptance.md`](docs/day5_day7_acceptance.md)。

非敏感设置写在 `config.yaml`。需要接入真实 Provider 时，复制 `.env.example` 为 `.env` 并只放专用、额度受限的密钥；`.env` 已被 Git 忽略。Provider 一旦从 `none` 改为真实名称而密钥缺失，启动会给出明确错误。

## Day 3 人工脱敏验收

此步骤只能使用以 `fake-day3-` 开头的无效假值，检查命令会拒绝其他值：

```powershell
Copy-Item .env.example .env
# 把 .env 中的值改为 COMPANION_LLM_API_KEY=fake-day3-review-20260711
uv run python -m app.config.redaction_check
```

确认控制台及 `data/logs/app.jsonl` 中没有上述假密钥、假邮箱、假手机号或假验证码的明文，只出现 `[REDACTED]`。检查后删除 `.env` 即可。

## 当前目录

```text
app/config/             配置加载、密钥检查、结构化日志和人工脱敏检查
app/schemas/            Day 4 实际使用的五个最小消息 Schema
app/core/               文字与语音共用的 turn、取消和事件入口
app/clients/llm/        可控的 Mock LLM token stream
app/clients/tts/        生成合法纯提示音 WAV 的 Mock TTS
app/pipelines/          增量分句、并发 TTS、有序播放和清理
app/api/                HTTP/WebSocket 协议转换、打断和 Debug 状态
app/main.py             FastAPI 工厂、生命周期和本地启动入口
tests/unit/             配置、脱敏、Schema 单元测试
tests/integration/      HTTP、WebSocket、统一链路、顺序、取消和生命周期测试
docs/                   架构、范围冻结记录和每日开发计划
config.yaml             当前阶段的非敏感默认配置
.env.example            密钥环境变量示例，不包含有效密钥
pyproject.toml          依赖、构建、pytest、Ruff、mypy 配置
.python-version         固定 Python 3.11
uv.lock                 可复现依赖锁文件，由 uv 生成
```

## 当前依赖边界

- 运行时包含 FastAPI、Pydantic、Uvicorn、PyYAML、python-dotenv，以及 Day 6 持久低延迟音频输出流需要的 sounddevice。
- 开发依赖只加入 pytest、Ruff、mypy、PyYAML 类型桩和 FastAPI 测试客户端所需的 httpx2。
- STT、GPT-SoVITS 客户端、VTS、OCR、截图、数据库和桌面 UI 依赖将在对应开发日经过必要性审查后再加入。
- 当前只实现 Phase 1 Mock 主链路；真实 OpenAI-compatible LLM、GPT-SoVITS 和 VTube Studio 仍按 Day 8～14 计划接入，生产失败不会伪装为 Mock 回复。

## 资产与数据边界

- 源码仓库不提交 API key、真实聊天、真实截图、真实记忆、声音、图片、Live2D 模型或其他受保护资产。
- 用户私有角色资产只能放在被 Git 忽略的 `assets/user_imported/` 或仓库外路径。
- 运行时数据库、日志、缓存和 VTS token 位于被 Git 忽略的 `data/`。
