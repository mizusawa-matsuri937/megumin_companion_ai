# Megumin Desktop Companion AI

面向 Windows 单用户、个人私用的桌面陪伴 AI 原型。当前仓库已完成并通过 Day 3/4 验收：具备类型化配置、结构化脱敏日志、最小消息协议和可启动的本地 FastAPI 服务。

产品范围、隐私与资产边界以 [`docs/day1_scope_freeze.md`](docs/day1_scope_freeze.md) 为准，日计划见 [`docs/daily_development_plan.md`](docs/daily_development_plan.md)，Day 3/4 的实现与验收证据见 [`docs/day3_day4_acceptance.md`](docs/day3_day4_acceptance.md)。

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

默认配置不启用真实 LLM，因此不需要 API key 即可启动：

```powershell
uv run python app/main.py
```

启动后可访问 `http://127.0.0.1:8765/health`。按 `Ctrl+C` 会执行安全关闭生命周期并刷新日志。HTTP 对话入口为 `POST /api/chat`，WebSocket 入口为 `/ws/client`，独立 echo 冒烟入口为 `/ws/echo`。

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
app/core/               文字与语音共用的 turn 接收入口
app/api/                只负责 HTTP/WebSocket 协议转换
app/main.py             FastAPI 工厂、生命周期和本地启动入口
tests/unit/             配置、脱敏、Schema 单元测试
tests/integration/      HTTP、WebSocket、统一路由和生命周期测试
docs/                   架构、范围冻结记录和每日开发计划
config.yaml             当前阶段的非敏感默认配置
.env.example            密钥环境变量示例，不包含有效密钥
pyproject.toml          依赖、构建、pytest、Ruff、mypy 配置
.python-version         固定 Python 3.11
uv.lock                 可复现依赖锁文件，由 uv 生成
```

## 当前依赖边界

- 运行时只加入 Phase 0 需要的 FastAPI、Pydantic、Uvicorn、PyYAML 和 python-dotenv。
- 开发依赖只加入 pytest、Ruff、mypy、PyYAML 类型桩和 FastAPI 测试客户端所需的 httpx2。
- STT、GPT-SoVITS 客户端、VTS、音频、OCR、截图、数据库和桌面 UI 依赖将在对应开发日经过必要性审查后再加入。
- 当前 API 只接收和规范化消息、创建 `accepted` turn，不包含 Day 5 的 LLM streaming 或后续业务。

## 资产与数据边界

- 源码仓库不提交 API key、真实聊天、真实截图、真实记忆、声音、图片、Live2D 模型或其他受保护资产。
- 用户私有角色资产只能放在被 Git 忽略的 `assets/user_imported/` 或仓库外路径。
- 运行时数据库、日志、缓存和 VTS token 位于被 Git 忽略的 `data/`。
