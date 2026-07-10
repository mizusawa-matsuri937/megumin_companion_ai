# Day 2：Python 工程与质量入口验收记录

> 日期：2026-07-10
>
> 状态：已通过
>
> 自动化验收：通过
>
> 人工依赖审阅：项目所有者已于 2026-07-10 确认

## 1. 已完成内容

- 在 `main` 分支初始化 Git 仓库，未自动暂存或提交文件。
- 用 `.python-version` 固定 Python 3.11，并由 `uv` 安装 CPython 3.11.15。
- 创建 `pyproject.toml`、`uv.lock` 和仓库内隔离环境 `.venv`。
- 创建最小 `app` 包与 `tests/unit/test_smoke.py`，未提前实现 Day 3/4 功能。
- 配置 pytest、Ruff formatter/linter 和 mypy strict mode。
- 增加 `.gitignore`，覆盖密钥、虚拟环境、运行时数据、数据库、日志、VTS token、用户资产以及常见音频/图片/Live2D 文件。
- 增加 README 的新环境安装、检查命令、目录说明和资产边界。

## 2. 直接依赖及必要性

| 类型 | 依赖 | 锁定版本 | 当前必要性 |
| --- | --- | ---: | --- |
| 构建 | Hatchling | `>=1.27,<2` | 让本地包可被标准构建和可编辑安装；不进入运行时依赖树 |
| 运行时 | FastAPI | 0.139.0 | Day 4 的 `/health` 和 WebSocket 本地网关 |
| 运行时 | Pydantic | 2.13.4 | Day 3 配置模型和 Day 4 消息 Schema；项目会直接导入，所以显式声明 |
| 运行时 | Uvicorn | 0.51.0 | Day 4 运行本地 ASGI 应用 |
| 开发 | pytest | 8.4.2 | 单元、集成和冒烟测试统一入口 |
| 开发 | Ruff | 0.15.21 | 单一格式化与 lint 工具，避免同时引入 Black/isort/Flake8 |
| 开发 | mypy | 1.20.2 | 静态类型检查入口，当前启用 strict mode |

本日没有加入 pytest-asyncio、HTTP 客户端、STT、TTS、VTS、音频、SQLite ORM、截图、OCR、桌面 UI 或打包依赖；需要时在对应开发日逐项审查。

## 3. 可复现验证结果

首次项目环境：

```text
Python 3.11.15
pytest: 2 passed
ruff check: All checks passed
ruff format --check: 2 files already formatted
mypy: Success: no issues found in 2 source files
```

随后使用 `/tmp` 下第二个空虚拟环境执行 `uv sync --frozen --all-groups`，从 `uv.lock` 安装 24 个包，再次运行相同四项检查，结果全部通过。这证明安装和检查结果不依赖原 `.venv` 的残留状态。

## 4. 仓库与敏感文件检查

- `.DS_Store`、`.venv`、pytest/Ruff/mypy 缓存均被 Git 忽略。
- `git add --dry-run .` 只列出源码、文档、工程配置和 `uv.lock`。
- 工作区未发现真实密钥模式。
- 工作区源码范围未发现声音、图片、Live2D 模型或用户数据库；命中的 `.mypy_cache/3.11/cache.db` 属于已忽略的工具缓存。
- 没有读取或写入任何真实 API key、聊天、截图、记忆或角色资产。

## 5. Day 2 完成清单

- [x] Python 3.11 工程可在隔离环境安装。
- [x] Git 仓库已建立，默认分支为 `main`。
- [x] `pyproject.toml` 和锁文件已生成。
- [x] pytest、Ruff lint、Ruff format check、mypy 命令可运行且通过。
- [x] 最小冒烟测试通过。
- [x] README 已说明安装、检查和当前不可运行状态。
- [x] `.gitignore` 覆盖敏感数据、运行时数据和用户资产。
- [x] 未提前创建 Day 3/4 或 Post-MVP 的空实现。
- [x] 项目所有者人工确认上述直接依赖均有必要。

Day 2 已按计划口径正式关闭。
