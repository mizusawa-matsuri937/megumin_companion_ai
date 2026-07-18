# Megumin Desktop Companion AI

面向单用户、个人私用的桌面陪伴 AI 原型。当前仓库已完成 Gate A，以及 Day 8～27 中可在 macOS 自动验证的 AI 后端：OpenAI-compatible LLM、GPT-SoVITS、VTube Studio、情绪与 Prompt、SQLite 历史/记忆、视觉隐私、主动发话和本地 whisper.cpp STT。

完整实现、测试证据、九个堆叠 Draft PR 与延期项见 [`docs/ai_backend_mac_implementation_report.md`](docs/ai_backend_mac_implementation_report.md)。这次交付不包含 Windows UI、前台窗口捕获、全局热键、打包或真实设备体验，也不宣称 Gate B～G 已通过。

Windows 当前基线、剩余风险、分阶段工程量与各阶段人工关卡见 [`docs/windows_development_plan.md`](docs/windows_development_plan.md)。Gate W0 的已批准决策和残余风险见 [`docs/gates/gate_w0.md`](docs/gates/gate_w0.md)。

## 架构

```text
HTTP / WebSocket / voice UserMessage
              ↓
          TurnService
              ↓
Prompt + 历史/记忆 → LLM stream → 分句 → 并发 TTS → 有序播放
                                      └→ 有界 VTS 事件队列

PerceptionPipeline（平台适配器注入）→ 脱敏 PerceptionContext
ProactiveRuntime（默认关闭）       → 内部 ProactiveIntent
PushToTalkRecorder（默认关闭）     → voice UserMessage
```

应用是 Python 3.11 模块化单体；GPT-SoVITS、VTube Studio 和用户配置的云端 LLM 是外部服务。真实 provider 失败时不会静默伪装成 Mock 成功。

## 默认行为与数据边界

仓库默认配置不会联网，也不会发声：

- LLM 与 TTS 使用确定性 Mock，播放模式为 `silent`。
- VTS、长期记忆、记忆候选 LLM、视觉、云端视觉、主动发话和 STT 均关闭。
- `storage.enabled` 和最近历史默认开启；显式发送的用户消息、成功的助手回复会保存在本机 SQLite，保留期为 7 天。
- SQLite 启用 `secure_delete`、WAL checkpoint 和清理流程，但数据库没有加密，也没有 Keychain/文件级加密；不能把软件清理描述为可证明的物理擦除。
- GPT-SoVITS 持久缓存默认关闭；开启后仍会跳过检测到的敏感文本。
- 截图、OCR、原始 PCM/WAV 只允许在处理生命周期内存在，不得写入普通日志、历史或长期记忆。

不要把真实 API key、聊天、截图、记忆、声音、模型或 Live2D 资产提交到仓库。Windows 运行数据位于 `%LOCALAPPDATA%\MeguminCompanion`；运行时以 current-user DACL 创建，LLM/VTS secret 另用 DPAPI current-user 密文保存。

## 安装与完整门禁

需要 Python 3.11、[`uv`](https://docs.astral.sh/uv/) 和 Git：

```bash
uv python install 3.11
uv sync --frozen --all-groups --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Windows 11 可在 PowerShell 中一键完成 `uv`、Python 3.11、锁定依赖和完整门禁初始化：

```powershell
powershell -ExecutionPolicy Bypass -File tools/setup_windows.ps1
```

若只需安装环境、不立即执行完整门禁，可增加 `-SkipQualityGate`。脚本不会创建 `.env`、下载 Whisper 模型、安装 GPT-SoVITS/VTube Studio，也不会写入真实密钥或用户资产；这些能力按需单独配置。

pytest 对 `app` 与 `desktop_client` 统计分支覆盖，并设置 90% 综合门槛。CI 在 GitHub `macos-latest` 与 `windows-latest` 执行同一组命令。

## 启动后端

```bash
uv run megumin-companion-api --check-config
uv run megumin-companion-api --dev-api
```

也可以使用 `uv run python -m app --dev-api`。`--help`、`--version` 和
`--check-config` 只读取/校验配置，不启动数据库、设备或网络。默认配置作为
`app.resources` 随 editable/wheel 安装；用户覆盖位于
`%LOCALAPPDATA%\MeguminCompanion\config\settings.yaml`。仓库根的 `config.yaml`
只是显式开发覆盖，使用方式为：

```bash
uv run megumin-companion-api --config config.yaml --dev-api
```

配置优先级固定为 package defaults → LocalAppData 用户设置 → 显式开发配置/
环境覆盖。程序不会从 CWD 或配置目录自动发现 `.env`；开发时必须显式传入：

```bash
uv run megumin-companion-api --config config.yaml --env-file .env --dev-api
```

旧仓库 `data/` 只通过显式迁移入口导入。迁移只复制数据库与模型，跳过日志、
缓存、临时录音和 secret，在 staging 校验后原子启用，并始终保留旧源：

```bash
uv run megumin-companion-api --migrate-from /path/to/old/data
```

迁移前若 LocalAppData 目标已存在会拒绝覆盖/合并。旧版无 `schema_version` 的
用户设置可先通过 `--check-config` 只读预检，再显式执行 `--upgrade-settings`；写入
使用原子替换并保留 `settings.yaml.bak`。

生产 secret 不从 `.env` 或进程环境直接读取。LLM key 只能从明确命名的环境变量
显式导入，值不放在命令行；子进程不能清除父 PowerShell，因此导入后仍需删除父
环境和旧 `.env`：

```powershell
$env:COMPANION_LLM_API_KEY = "<在本机交互输入>"
uv run python -m app --import-llm-key-env COMPANION_LLM_API_KEY
Remove-Item Env:COMPANION_LLM_API_KEY -ErrorAction SilentlyContinue
```

VTS 旧明文 token 只读取显式文件，默认保留到 VTS 认证人工确认；确认后才选择文件级
删除。若源文件就是密文目标，不带删除授权会拒绝原位覆盖：

```powershell
uv run python -m app --import-vts-token C:\explicit\legacy-vts-token.json
uv run python -m app --import-vts-token C:\explicit\legacy-vts-token.json --delete-import-source
uv run python -m app --revoke-secret llm-api-key
uv run python -m app --reset-secret vts-token
```

DPAPI 密文不是可移植的凭据备份；换账户、换机或丢失原用户上下文时可能无法解密，
必须重新输入。管理员、同用户恶意代码和进程内存不在该边界内；revoke/reset/源文件
删除也不保证 SSD、备份、shell 历史或旧环境中的物理擦除。完整威胁模型与审计步骤见
[`docs/implementation/w03_windows_security_and_temp_assets.md`](docs/implementation/w03_windows_security_and_temp_assets.md)。

生产 desktop 入口不导入或启动 Uvicorn，默认 ASGI factory 也处于锁闭状态；只有
`--dev-api` 会生成可用的进程内凭据并监听数字 loopback 地址。旧 `--serve` 已拒绝，
非 loopback host、`localhost`、通配 Origin 和直接运行无凭据
`uvicorn app.main:create_app --factory` 都不能得到可用 API；中间件还会拒绝真实来源不是
数字 loopback 的 client peer。

每次启动会在第一行输出新的随机 token、授权 client/session、精确 Origin allowlist 和一小时
TTL；退出、过期或重启后旧 token 失效，已建立 WebSocket 也会在到期时关闭。默认 token
只有 `chat` scope；确需调试状态、feature、memory、export 或 delete 时必须显式增加
`--dev-admin`。不要把凭据行写入日志、
issue 或 shell 历史；疑似暴露时直接重启轮换。

HTTP 和 WebSocket 均要求以下六项，且 WebSocket 在 `accept` 前完成校验：

- `Authorization: Bearer <本次启动 token>`
- 精确匹配 allowlist 的 `Origin`
- 精确匹配当前 loopback listener authority 的 `Host`
- `X-Megumin-Protocol: 1`
- `X-Megumin-Client-ID: <本次授权 client>`
- `X-Megumin-Session-ID: <本次授权 session>`

WebSocket 客户端只可订阅该 client/session。连接后先发送 `session.resume`（首次连接的
`last_seq` 为 0），再发送业务命令；写入使用最终 protocol v1 envelope：

```json
{
  "protocol_version": 1,
  "command_id": "command-unique-id",
  "client_id": "<本次授权 client>",
  "type": "user.message",
  "session_id": "<本次授权 session>",
  "payload": {
    "session_id": "<本次授权 session>",
    "text": "你好"
  }
}
```

输出 event 带每 session 严格递增的 `seq` 和 `event_id`。服务保留 2,000 events/10 分钟；
窗口内按 `last_seq` replay，窗口外返回 `session.reset` 与不含正文的权威 snapshot。
同一 `(client_id, session_id, message_id)` 在终态保留窗口内返回原 turn；同键异文返回 409。
终态固定保留不超过 24 小时且每 session 最多 200 个，因此客户端不得把超出该窗口的重试当作
“保证不会重新执行”。

HTTP body 和 WebSocket frame 在 JSON 解析前限制为 64 KiB；metadata 深度/键数、ID、
客户端时间、请求速率、并发与 WebSocket 连接数也有硬上限。错误只返回稳定 code，
不回显正文、token 或路径。主要入口：

- `GET /health`
- `POST /api/chat`
- `POST /api/interrupt`
- `WS /ws/client`
- `GET /api/features` 与 `PATCH /api/features/{feature}`（`admin`）
- 记忆 list/search/update/delete/confirm/export/clear（`admin`）
- `POST /api/history/clear`（`admin`）

按 `Ctrl+C` 会取消活动轮次并等待对话、主动任务、VTS、记忆和 provider 资源完成清理。

## 可选真实适配器

所有真实能力都必须显式开启；测试不会安装外部服务、下载 Whisper 模型或使用付费密钥。

### OpenAI-compatible LLM

在 `config.yaml` 配置 provider、base URL、model，使用 `--config config.yaml` 显式加载。
开发模式可把专用且额度受限的密钥放入未跟踪的 `.env`，但必须同时显式传
`--env-file .env`：

```text
COMPANION_LLM_API_KEY=...
```

支持 `/v1/chat/completions` 的 streaming 与 complete。缺少 model/key、HTTP 错误、超时或协议错误都会明确失败，不回退到 Mock。

### GPT-SoVITS 与 VTube Studio

- GPT-SoVITS 需要用户自行启动兼容 `api_v2.py` 的 `/tts` 服务，并在 `tts.presets` 中配置参考音频等 preset。
- VTube Studio 默认连接 `ws://127.0.0.1:8001`；首次认证仍需用户在 VTS 内人工允许，并为真实模型配置 hotkey 映射。

仓库不包含参考音频、声音模型、Live2D 模型或角色资产。

### 视觉隐私

`app.perception` 提供平台无关的窗口/捕获协议和严格的 Guard → 捕获 → 变化检测 → RapidOCR → 内容 Guard → 全 OCR 框遮挡 → 可选云端分析 → 清理流水线。RapidOCR 只使用安装包内的本地 ONNX 模型；模型缺失时安全失败，不自动下载。

当前没有 macOS 或 Windows 前台窗口捕获实现，也没有把感知流水线接入主应用生命周期。真实屏幕权限、真实内容与云端视觉仍需后续人工验收。

### 主动发话

主动功能默认关闭；关闭时不创建 idle scheduler。通过 feature API 开启后才启动，关闭请求返回前会停止 scheduler 并等待活动主动轮次清理。生产组合目前只接入 idle trigger；Focus/DND、任务完成、视觉发布者及其他 trigger producer 尚未接线。

### 本地 whisper.cpp STT

STT 默认关闭。配置本机 `whisper-cli` 与模型路径后，可先运行只读检查：

```bash
uv run python tools/stt_smoke.py --mode check
uv run python tools/stt_smoke.py --mode file --audio /path/to/16khz-mono-pcm.wav
```

`--mode microphone` 会请求真实麦克风并需要人工操作；本次没有执行。工厂不会打开麦克风，直到调用显式 push-to-talk `start()`，也不会自动下载模型。当前尚无桌面按钮或全局热键接线。

## 目录

```text
app/clients/llm/          OpenAI-compatible 与 Mock LLM
app/clients/tts/          GPT-SoVITS 与 Mock TTS
app/clients/vts/          VTube Studio client、bridge、token store
app/emotion/              情绪状态、限幅、衰减与表现映射
app/prompts/              Prompt 与不可信外部上下文构建
app/memory/ + app/storage SQLite 历史、记忆、FTS5 与管理 API
app/perception/           视觉 Guard、OCR、脱敏与云端边界
app/proactive/            主动评分、抑制、调度与抢占
desktop_client/inputs/    whisper.cpp 与 push-to-talk 状态机
tests/                    单元、集成、属性与跨模块 E2E
tools/                    Gate A 与 STT 人工冒烟工具
docs/                     范围、架构、验收记录与实现报告
```

产品范围、隐私和资产边界以 [`docs/windows_development_plan.md`](docs/windows_development_plan.md) 第 1.3 节及 [`docs/decisions/w00_owner_decisions.md`](docs/decisions/w00_owner_decisions.md) 为准。任何真实设备、Windows 或人工体验验收都必须按对应 Gate 单独执行。
