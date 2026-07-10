# Megumin Desktop Companion AI

面向 Windows 单用户、个人私用的桌面陪伴 AI 原型。当前仓库完成到 Day 2：只有 Python 3.11 工程、质量检查入口和最小冒烟测试，还没有可启动的 API 或桌面界面。

产品范围、隐私与资产边界以 [`docs/day1_scope_freeze.md`](docs/day1_scope_freeze.md) 为准，日计划见 [`docs/daily_development_plan.md`](docs/daily_development_plan.md)，Day 2 的环境与检查证据见 [`docs/day2_acceptance.md`](docs/day2_acceptance.md)。

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

## 当前目录

```text
app/                    最小 Python 包；Day 4 才加入 main.py 和 API
tests/unit/             当前阶段的冒烟测试
docs/                   架构、范围冻结记录和每日开发计划
pyproject.toml          依赖、构建、pytest、Ruff、mypy 配置
.python-version         固定 Python 3.11
uv.lock                 可复现依赖锁文件，由 uv 生成
```

## 当前依赖边界

- 运行时只加入 Phase 0 需要的 FastAPI、Pydantic 和 Uvicorn。
- 开发依赖只加入 pytest、Ruff 和 mypy。
- STT、GPT-SoVITS 客户端、VTS、音频、OCR、截图、数据库和桌面 UI 依赖将在对应开发日经过必要性审查后再加入。
- `app/main.py`、`/health`、WebSocket 和 Schema 属于 Day 4，本日不提前创建。

## 资产与数据边界

- 源码仓库不提交 API key、真实聊天、真实截图、真实记忆、声音、图片、Live2D 模型或其他受保护资产。
- 用户私有角色资产只能放在被 Git 忽略的 `assets/user_imported/` 或仓库外路径。
- 运行时数据库、日志、缓存和 VTS token 位于被 Git 忽略的 `data/`。
