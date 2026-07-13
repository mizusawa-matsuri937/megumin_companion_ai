# Megumin Desktop Companion AI

面向单用户、个人私用的桌面陪伴 AI 原型。当前仓库已完成 Gate A，以及 Day 8～27 中可在 macOS 自动验证的 AI 后端：OpenAI-compatible LLM、GPT-SoVITS、VTube Studio、情绪与 Prompt、SQLite 历史/记忆、视觉隐私、主动发话和本地 whisper.cpp STT。

完整实现、测试证据、九个堆叠 Draft PR 与延期项见 [`docs/ai_backend_mac_implementation_report.md`](docs/ai_backend_mac_implementation_report.md)。这次交付不包含 Windows UI、前台窗口捕获、全局热键、打包或真实设备体验，也不宣称 Gate B～G 已通过。

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

不要把真实 API key、聊天、截图、记忆、声音、模型或 Live2D 资产提交到仓库。运行数据默认位于已被 Git 忽略的 `data/`。

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

pytest 对 `app` 与 `desktop_client` 统计分支覆盖，并设置 90% 综合门槛。CI 在 GitHub `macos-latest` 执行同一组命令。

## 启动后端

```bash
uv run python app/main.py
```

默认监听 `127.0.0.1:8765`。主要入口：

- `GET /health`
- `POST /api/chat`
- `POST /api/interrupt`
- `WS /ws/client`
- `GET /api/features` 与 `PATCH /api/features/{feature}`
- 记忆 list/search/update/delete/confirm/export/clear
- `POST /api/history/clear`

按 `Ctrl+C` 会取消活动轮次并等待对话、主动任务、VTS、记忆和 provider 资源完成清理。

## 可选真实适配器

所有真实能力都必须显式开启；测试不会安装外部服务、下载 Whisper 模型或使用付费密钥。

### OpenAI-compatible LLM

在 `config.yaml` 配置 provider、base URL、model，并把专用且额度受限的密钥放入未跟踪的 `.env`：

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

产品范围、隐私和资产边界以 [`docs/day1_scope_freeze.md`](docs/day1_scope_freeze.md) 为准。任何真实设备、Windows 或人工体验验收都应按最终报告中的清单单独执行。
