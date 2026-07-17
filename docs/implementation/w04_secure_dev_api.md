# W04 实现与审计记录：生产零监听与安全 dev API

> 实现日期：2026-07-17
>
> 状态：实现、自动验收与项目所有者人工安全审计均已通过；允许完成 PR #14 合并，合并后进入 W05
>
> 风险：P0-01、P1-17、P2-01
>
> ADR：[`../adr/ADR-W02-secure-control-plane.md`](../adr/ADR-W02-secure-control-plane.md)

## 已实现范围

- `megumin-companion-desktop` 不导入 Uvicorn，也不构造 ASGI server。当前 W13 前的 desktop preflight 返回稳定退出码 3；Windows `netstat` smoke 证明该进程没有 TCP socket。
- `create_app()` 未收到显式 `DevAPIConfig` 时保持锁闭，HTTP 返回 `dev_api_unavailable`，WebSocket 在 accept 前关闭。直接执行 `uvicorn app.main:create_app --factory` 不是受支持入口，也得不到可用 API。
- 只有 `--dev-api` 生成本次进程专用的 256-bit 随机 token 和随机 session，并启动 Uvicorn。旧 `--serve` 明确报错，不再启动未认证服务。
- listener host 和中间件看到的真实 client peer 都只接受数字 loopback（IPv4 `127.0.0.0/8` 或 IPv6 `::1`）；`0.0.0.0`、LAN 地址、域名、`localhost` 与非 loopback peer 均失败，没有“忽略风险继续”开关。即使调用者误把嵌入式 ASGI listener 绑定到通配地址，远端 peer 仍无法通过中间件。
- 默认 Origin 为当前 loopback listener 的精确 HTTP Origin；可重复传 `--dev-origin` 增加精确项，但拒绝 `*`、`null`、HTTPS、path/query、userinfo 和歧义语法。
- 默认 token 只有 `chat` scope；只有显式 `--dev-admin` 才额外授予 `admin`。`debug`、feature、memory、export、delete 和 history management 全部要求 admin。
- HTTP 与 WebSocket 共用 bearer、Origin、Host、protocol 和 session 校验。WebSocket 在 `accept()` 前完成校验，并只调用 `subscribe(<authorized-session>)`，不再从 API 使用 `subscribe("*")`。
- token TTL 固定一小时，不提供延长开关；过期后新请求和已建立 WebSocket 都 fail closed，重启生成新 token/session。token/session 只在启动首行有意显示一次，不持久化，并同时作为额外 redaction sentinel 注入运行时日志 formatter。
- HTTP body 在 FastAPI/JSON 解析前累计限制为 64 KiB；检查重复/冲突 body framing、JSON 嵌套和实际流式字节数。WebSocket 同时配置 Uvicorn `ws_max_size=64 KiB` 并在应用层解析前复核 UTF-8/bytes 长度；直接构造配置也只能收紧、不能放宽这两个硬上限。
- metadata 限制为最大深度 4、聚合 32 keys、128 nodes；ID 最大 128 字符；客户端时间最大偏差 5 分钟；非有限数字和重复 WS JSON key 被拒绝。
- HTTP 默认 120 requests/60s、8 个并发请求；WS 默认 2 个连接、120 messages/60s。安全拒绝日志自身限制为 10 条/60s，避免未认证 flood 放大当前 W11 前尚未轮转的日志。
- Uvicorn 关闭 proxy headers、per-message deflate、access log 和 server header；WS queue 为 4、listener backlog 为 32、总 concurrency 为 32、h11 incomplete event 为 16 KiB。
- 响应统一添加 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 和 `X-Megumin-Protocol: 1`。安全错误只返回稳定 code，不包含正文、token、Origin 原值、用户路径或认证 header。

## 认证与 protocol v1 契约

每个 HTTP 请求和 WebSocket handshake 必须同时提供：

```text
Authorization: Bearer <本次启动 token>
Origin: <精确 allowlist Origin>
Host: <实际 loopback listener authority>
X-Megumin-Protocol: 1
X-Megumin-Session-ID: <本次启动 session>
```

WS 写入只接受：

```json
{
  "protocol_version": 1,
  "type": "user.message | turn.cancel",
  "session_id": "<authorized session>",
  "payload": {}
}
```

`user.message.payload.session_id` 和 `turn.cancel.payload.session_id` 也必须匹配 handshake session。输出 event/error 同样带 `protocol_version=1` 与 authorized session。v0 客户端可在一个开发版本内保留读取旧 fixture 的能力，但向 W04 server 写入必须使用 v1；生产 GUI 从 W13 起只能实现 v1。

## scope 与路由

| 接口 | scope | 额外边界 |
| --- | --- | --- |
| `GET /health` | `chat` | 只有 TurnService 初始化后返回 `ready`；不公开路径或 provider secret |
| `POST /api/chat`、`POST /api/interrupt` | `chat` | body session、ID、时间和 metadata 受限 |
| `WS /ws/client` | `chat` | accept 前认证；只订阅 authorized session |
| `GET /debug/state`、`WS /ws/echo` | `admin` | 仅显式 dev API；OpenAPI/Swagger/Redoc 不注册 |
| feature、memory、confirmation、export/delete、history clear | `admin` | history 指定 session 时仍必须匹配 authorized session |

## 主要失败语义

| 条件 | HTTP | WebSocket |
| --- | --- | --- |
| 未显式启用 dev API | 503 `dev_api_unavailable` | accept 前 1008 |
| 真实 client peer 非 loopback 或缺失 | 403 `client_forbidden` | accept 前 1008 |
| token 缺失、错误或过期 | 401 `authentication_failed` | accept 前 1008 |
| Origin/Host/session 不允许 | 403 稳定 code | accept 前或跨 session command 后 1008 |
| protocol 非 v1 | 426 `protocol_version_unsupported` | accept 前 1002 |
| scope 不足 | 403 `scope_forbidden` | accept 前 1008 |
| body 超过 64 KiB | 413 `request_body_too_large` | 不适用 |
| WS frame 超过 64 KiB | 不适用 | 1009 |
| rate/connection 超限 | 429 + `Retry-After` | 1013 |

## 兼容性与迁移

- `--serve` 不再是兼容启动入口；调用会提示改用 `--dev-api`，不得回退无认证模式。
- 默认 `--dev-api` 是 chat-only。需要管理私有状态的开发者必须显式增加 `--dev-admin`，并重新启动获得新的 token。
- 开发客户端必须保存本次启动输出中的 token/session 于进程内，退出即丢弃；不得写入 user settings、`.env`、日志、fixture 或数据库。
- 直接 ASGI factory 仍保留 W01 的 wheel/import 契约，但没有显式凭据时只返回锁闭响应。`tools/installed_wheel_smoke.py` 同时验证 locked factory 和显式 authenticated health。
- 生产 desktop 当前仍是 W13 前的诚实 preflight；W13 引入 Qt 后必须继续使用进程内 bridge，不能把 dev API 变回生产控制面。

## 自动证据

- 最终 W04 聚焦矩阵：`115 passed`，覆盖真实 peer、认证畸形输入、不可放宽上限、最小 scope、TTL、HTTP/WS、CLI 与 memory/admin 边界。
- 全仓 `pytest -q`：`617 passed, 1 skipped`；skip 是既有“无确定性测试字体”，与 W04 无关。
- aggregate branch coverage：`90.84%`，高于 90% 门槛。
- `tools/w04_network_smoke.py` 使用真实 loopback Uvicorn 验证：正确 HTTP/WS v1 成功；默认 admin 拒绝；错 token、attacker Origin、65,537-byte HTTP body 全部拒绝；65,537-byte WS frame 以 1009 关闭；进程终止后端口释放；运行日志不含 token、session 或攻击 sentinel。
- `tools/w04_desktop_port_smoke.py` 在 Windows 隐藏子进程中运行 production desktop entry point：退出码 3、`uvicorn_loaded=false`、该 PID 的 TCP socket 数为 0。
- `uv build --wheel` 成功；在全新临时 Python 3.11 venv 中从 wheel 解析/安装 25 个依赖，确认 `app` 来自隔离 `site-packages` 而非源码树。installed smoke 先验证无凭据 factory 为 503 locked，再以显式进程内测试凭据完成 lifespan 与 authenticated `/health`，返回 `status=ready`。
- 最终质量门禁全部通过：`ruff check .`；`ruff format --check .`；strict mypy；W04 三个工具脚本的显式 strict mypy；wheel build/隔离安装 smoke；`git diff --check`。全量运行暴露的既有真实 Pillow 用例 30ms 偶发超时已只在该测试显式调整为 1 秒，并额外连续复跑 10 次通过；其他 timeout 故障注入仍保持 30ms。

## 人工安全审计方案

### 1. 环境、身份与证据处理

在干净 Windows 11 x64 标准账户执行，记录 commit SHA、Draft PR、Windows build、Python/uv 版本和测试时间。记录 reviewer 是否独立；项目所有者兼任时必须写“非独立审计”。

建立专用、无真实 provider key、无真实聊天/记忆的审计 LocalAppData。token、完整 session 和用户名路径不得出现在截图、PR、issue 或粘贴文本；证据只记录长度、SHA-256 前 12 位、稳定 code 和计数。审计结束立即终止进程并删除专用审计数据，删除仍不代表 SSD/备份物理擦除。

### 2. 关键 diff 与 trust boundary 审查

逐行审查以下文件，确认没有第二条旁路：

- `app/api/security.py`：真实 peer、header 去重、constant-time token/session、固定 TTL、Origin/Host canonicalization、不可放宽 body/frame 上限、body 前置读取、rate/connection/log limits；
- `app/api/protocol.py`：v1 envelope、重复 key/非有限数、metadata/time/session 限制；
- `app/api/routes.py`：每条 route scope、WS accept 顺序、authorized-session subscription、错误不回显；
- `app/main.py`：factory 默认 locked，middleware 覆盖所有 HTTP/WS，token/session 加入 redactor；
- `app/cli.py` 与 `desktop_client/entrypoint.py`：只有 `--dev-api` lazy-import Uvicorn，desktop 没有 server lifecycle；
- `tests/integration/test_dev_api_security.py`：攻击用例是否真正到达预期边界，而非 mock 伪证据。

搜索并确认生产/CLI 路径不存在 `subscribe("*")`、无条件 `websocket.accept()`、固定 token、query-string token、`allow_origins=["*"]`、`0.0.0.0` fallback 或未认证 router。

### 3. 生产 desktop 零监听

先运行：

```powershell
uv run python tools/w04_desktop_port_smoke.py
```

必须得到：

```json
{"desktop_return":3,"status":"ok","tcp_socket_count":0,"uvicorn_loaded":false}
```

再由 reviewer 独立启动 wheel/当前 desktop entry point，在进程存活窗口以管理员 PowerShell 执行：

```powershell
Get-NetTCPConnection -OwningProcess <PID> -ErrorAction SilentlyContinue
```

不得出现 Listen/Established TCP 项。`python -m app` 不带 `--dev-api` 只打印 help，不应监听。明确记录：开发者若主动运行原始 `uvicorn ... --factory` 仍可人为创建一个**锁闭**端口；这不是生产入口，也不能作为“系统禁止任何人启动 socket”的承诺。

### 4. loopback、token 生成/轮换与默认最小权限

分别尝试：

```powershell
uv run python -m app --dev-api --host 0.0.0.0
uv run python -m app --dev-api --host localhost
uv run python -m app --dev-api --dev-origin '*'
```

三项都必须在构造 app/server 前配置失败。正常启动两次 `--dev-api`，只在本机读取凭据行并记录 token/session 的长度与 hash 前缀；两次必须不同。旧进程终止后端口关闭，旧 token 不得认证新进程。默认凭据 `scopes` 必须只有 `chat`，`GET /debug/state` 返回 403；使用 `--dev-admin` 重启后新 token 才同时拥有 `chat/admin`。

审查 `test_non_loopback_peer_is_rejected_even_with_valid_credentials`：它必须在 token、Origin、Host、protocol 和 session 全部正确时，仅因模拟真实 peer 为 `192.0.2.10` 而让 HTTP 返回 403、WS 在 accept 前以 1008 关闭。人工不得为了演示此项而放宽受支持 CLI 的 listener host。

检查 TTL 固定 3600 秒且无 CLI 放宽项；自动 fake-clock 测试必须证明 3600 秒边界后新请求、已建立 WS 的下一条消息和空闲 WS 到期任务都返回/关闭为 `authentication_failed`。无需把 token 留存一小时作为人工证据。

### 5. Origin、Host 与恶意网页

用一次性审计 token、无真实数据的进程执行：

1. 原生客户端带正确 token 但 `Origin: http://attacker.example`，必须 403；WS 必须在 accept 前失败。
2. 正确 token/Origin 但 Host 改为未授权域名，必须 403。检查 DNS rebinding 情形不能只靠 CORS。
3. 缺 Origin、`Origin: null`、重复 Origin、大小写/默认端口等边界与 allowlist 规则一致。
4. 从另一个本地端口提供最小 attacker HTML，浏览器 `fetch`/`WebSocket` 不得读写 API；浏览器 WS 无法添加 Authorization header，预期先失败认证。为单独证明 Origin 规则，使用原生客户端，不把 token 嵌入或提交 attacker HTML。

不得把“loopback”“CORS”或“浏览器不能设 header”中的任一项单独当作信任边界；通过条件是 token + exact Origin + Host + protocol + session 全部 fail closed。

### 6. session、scope 与授权订阅

- HTTP header session 正确但 body session 错误：403 `session_forbidden`，正文不回显。
- WS handshake session 正确但 envelope 或 payload session 错误：先返回安全 error，再 1008 关闭。
- 默认 chat token 对 `/debug/state`、`/ws/echo`、feature、memory、export/delete/history management 全部 403/1008；显式 admin token 才允许。
- 在 debugger/聚焦测试中检查 subscriber map：只能出现本次随机 session，不得出现 `*`；断开后条目必须注销。
- admin history clear 指定 session 时仍不得跨 session；不带 session 的全量 clear 是显式 admin 操作，需单独确认产品语义。

### 7. body/frame、metadata 与 flood

运行：

```powershell
uv run pytest --no-cov tests/unit/test_dev_api_primitives.py tests/integration/test_dev_api_security.py -q
uv run python tools/w04_network_smoke.py
```

另以新进程检查 65,536/65,537-byte 边界、chunked body、重复 Content-Length/Content-Type、深层 JSON、129-char ID、过期/未来 client time、metadata 深度/总 keys/nodes、binary WS command。失败必须发生在 JSON/Pydantic/业务调用前，`TurnService.snapshot()` 不得新增 turn。

在新进程发送 121 个一分窗口内的 authenticated 请求：第 121 个必须 429 且有 `Retry-After`。同时打开第三个 WS：必须 1013。慢连接/并发请求不得超过 HTTP 8、Uvicorn 32 和 backlog 32 的既定边界。记录这些是开发期防滥用上限，不是抗同用户恶意软件的强隔离。

### 8. 错误响应、日志与 sentinel 扫描

选择仅用于审计的随机 sentinel，分别作为错误 token、body text、metadata value 和错误 Origin label。检查响应、stdout（除启动首行凭据）、`%LOCALAPPDATA%\MeguminCompanion\logs`、pytest 输出和导出 artifact：

- 只允许稳定 response code、内部 reason、transport、session fingerprint、计数/延迟；
- 不得出现 sentinel、token、Authorization header、原始 Origin/Host、正文、ciphertext 或完整用户路径；
- 11 次快速未认证失败最多新增 10 条 security rejection log；其余被静默限流，不能用 attacker 输入生成动态 reason key；
- 启动首行含 token 是唯一有意 secret 输出，必须从共享证据中完全删除，而不是只遮住一部分。

### 9. 审计签字条件

全部证据齐全后，reviewer 必须明确接受以下残余风险，才能回复“审计合格”：

1. 显式 dev API 仍扩大本机攻击面；只用于受控开发，不可成为生产 GUI 控制面。
2. 启动凭据行、进程内存、同用户恶意代码、管理员和调试器不在 token 保密保证内；疑似暴露只能重启轮换。
3. token 过期后 server 进程不会自动退出，但所有请求保持 locked；需要继续开发时必须重启取得新 token。
4. 默认 chat-only、显式 admin 是开发者权限边界，不是 Windows 多用户身份系统；远程控制和多用户属于 Post-MVP。
5. 当前安全日志已有拒绝采样，但轮转/retention 与完整诊断 allowlist 仍由 W11 完成。
6. 当前 desktop 只是 W13 前 preflight；W13/打包后的真实 Qt/onedir/exe 仍必须重新做 PID 端口扫描，不能把本次源码进程证据冒充最终产物证据。

若任一项在后续回归中失败，记录稳定复现、预期/实际 code、是否在 accept/parse/business 之前失败以及脱敏日志片段，并重新打开 W04 安全 Gate。

## 阶段关闭记录

- 关闭日期：2026-07-17。
- 自动证据：最终全仓 `617 passed, 1 skipped`、branch coverage `90.84%`；W04 聚焦矩阵 `115 passed`；Ruff、strict mypy、真实 Uvicorn、desktop 零监听、wheel 隔离安装和 GitHub Windows/macOS quality jobs 全部通过。
- 人工签字：项目所有者在收到第 1～9 节审计方案、通过条件与残余风险清单后明确回复“审计合格”。
- reviewer 独立性：项目所有者兼任安全 reviewer，属于**非独立审计**，不得表述为独立复核。
- 残余风险：第 9 节六项残余风险随本次“审计合格”一并接受；W11、W13、W24/W25 的后续重验责任不因此关闭。
- 证据边界：本记录不伪造未提交到仓库的截图、原始 token/session 或外部 VM 附件；这些材料如有，由项目所有者本地保管。
- 顺序授权：只允许先完成 PR #14 合并；确认合并后才可创建 W05 分支或修改 W05 范围文件。

## 回滚

- 最安全回滚是完全不传 `--dev-api`，保留 locked factory 和文字 Mock/单元测试。
- 可整体关闭 dev API；不得恢复 `--serve`、无认证 loopback、wildcard Origin/session、query token 或非 loopback 监听。
- 若 protocol v1 客户端有问题，只能修复/回退开发客户端；server 不接受 v0 写入。生产 GUI 尚未实现，不存在放宽 server 来兼容生产的理由。
