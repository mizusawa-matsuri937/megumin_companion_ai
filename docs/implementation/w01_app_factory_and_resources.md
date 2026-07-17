# W01 实现记录：app factory 与 package resources

> 完成日期：2026-07-17
> 状态：自动验收通过；允许进入 W02
> 风险：P0-02
> ADR：[`../adr/ADR-W03-paths-resources-migration.md`](../adr/ADR-W03-paths-resources-migration.md)、[`../adr/ADR-W08-packaging-upgrade.md`](../adr/ADR-W08-packaging-upgrade.md)
>
> 历史说明：本文件中的 `--serve`/直接 ASGI health 是 W01 当时的过渡接口；W04 已将其替换为默认 locked factory 与认证 `--dev-api`。当前契约见 [`w04_secure_dev_api.md`](./w04_secure_dev_api.md)。

## 完成范围

- 删除 `app.main` 模块导入时的全局 `app = create_app()`，导入只定义 factory 和类型。
- `create_app(settings=None)` 保持为显式 ASGI factory；只有调用 factory 才读取配置，只有进入 lifespan 才创建日志、数据库、provider 和后台任务。
- 新增 `megumin-companion-api` console entry point 和 `python -m app`。
- 新增 `megumin-companion-desktop` GUI packaging entry point 和 `python -m desktop_client`；当前仅提供诚实的 package/config preflight，PySide6 shell 明确留到 W13。
- `--help`、`--version`、`--check-config` 不构造 FastAPI runtime，不打开数据库、设备或网络。
- 默认配置迁入 `app.resources/default_config.yaml` 并随 wheel 安装；仓库根 `config.yaml` 保留为显式开发配置，并由测试保证两者语义一致。
- 移除 `PROJECT_ROOT` 反推。相对数据库、日志、TTS/VTS/STT 路径统一经 `Settings.resolve_runtime_path()` 解析。
- 配置错误只显示内置来源或配置文件 basename，不输出完整用户路径。
- 新增 `tools/installed_wheel_smoke.py`，验证真正安装的模块、entry points、资源和 ASGI `/health`。

## 当前接口

```text
megumin-companion-api --help
megumin-companion-api --version
megumin-companion-api --check-config [--config FILE] [--env-file FILE]
megumin-companion-api --serve [--config FILE] [--env-file FILE] [--host HOST] [--port PORT]

python -m app <同上参数>
python -m desktop_client --help|--version|--check-config
uvicorn app.main:create_app --factory
```

`--serve` 是当前显式开发 API 入口，不是最终生产控制面。W04 负责生产零监听、`--dev-api`、token、Origin、session 和 frame/rate limits。

## 配置和路径迁移

- `load_settings()` 不再隐式读取源码树根的 `config.yaml`，而是读取包内默认资源。
- 继续使用仓库配置时必须显式传入 `--config config.yaml`；未指定 `--env-file` 时读取配置来源目录的 `.env`。
- 显式配置中的相对运行路径以配置文件目录为基准。
- 包内默认配置的相对运行路径暂时以启动 CWD 为基准，以消除对只读安装目录和源码树的依赖。
- CWD 只是 W01 兼容基线，不是最终用户数据模型；W02 必须替换为 `%LOCALAPPDATA%\MeguminCompanion` 分类路径、schema-aware 用户覆盖和显式旧数据迁移。

## 自动证据

### 修复前复现

从 `d56cfbd` 工作树构建 wheel，在仓库外执行 `import app.main`：

```text
ConfigurationError: ...site-packages\config.yaml
isolated_import_exit=1
```

### 修复后安装产物

- 在含中文和空格的临时根创建全新 Python 3.11 venv。
- 从新 wheel 安装 25 个运行依赖，`uv pip check` 通过。
- 从仓库外 CWD 导入的 `app` 位于该 venv 的 `site-packages`，并非源码树。
- `app.main` 不存在全局 `app` 对象。
- `app.resources/default_config.yaml` 存在，大小 3330 bytes；将资源文件标为只读后仍可加载。
- console/gui entry point metadata 均存在。
- installed `--help`、`--version`、`--check-config` 通过。
- installed 默认配置完成 FastAPI lifespan 并返回：

```json
{"service":"megumin-companion-ai","status":"ok","version":"0.1.0"}
```

### 全量质量门

- `uv run pytest -q`：473 passed，1 skipped；branch coverage 92.66%。
- 唯一跳过：环境没有可确定使用的测试字体。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：137 files already formatted。
- `uv run mypy`：134 source files strict 检查通过。
- `git diff --check`：通过。

## 未包含和残余风险

- **W02：** LocalAppData 路径服务、配置分层/schema、旧 `data/` copy-verify-switch 尚未实现。
- **W03：** DACL、DPAPI 和 temp registry 尚未实现。
- **W04：** 当前开发 HTTP/WS 仍未认证；不得作为生产桌面入口。
- **W05：** wheel smoke 尚未加入双 OS CI required check；当前证据为本机可复现工具和实际运行记录。
- **W13：** desktop entry point 只是明确标记的 preflight，不包含 PySide6、窗口、托盘或 IME。
- wheel 直接安装会按宽范围依赖解析；lock/constraints、SBOM 和 native bundle manifest 留给 W24/W26。
- 只读验证覆盖 package resource 文件；标准用户、完整只读安装树和 Program Files 场景继续由 W02/W05/W24/W25 VM Gate 证明。

## 回滚

W01 可整体回滚到上个源码版本，但会重新打开 P0-02，且 W02 及以后不得继续。若单独回滚 CLI 或 desktop preflight，必须保留无副作用 `create_app` 和包内默认配置；不得恢复全局 `app = create_app()`、源码根路径反推或 package 外默认配置。
