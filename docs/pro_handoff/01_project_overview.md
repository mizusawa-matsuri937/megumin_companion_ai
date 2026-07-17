# Megumin Desktop Companion AI：Pro 模型项目概览

> 本文用于快速建立项目心智模型。提交 `d56cfbd` 的代码是最终事实；现有设计文档同时包含已实现内容和未来目标，不应仅凭文档标题判断功能已完成。

## 1. 一句话定位

这是一个面向单个用户、个人私用的 **Windows 常驻桌面陪伴 AI 原型**。目标不是自动控制电脑，也不是直播型 AI VTuber，而是将文字/按键说话、角色化 LLM 回复、本地语音合成、Live2D 表情、短期历史、长期记忆、隐私受控的屏幕感知与低打扰主动发话组合成桌面陪伴体验。

当前仓库已经实现了较完整的 Python 后端核心和平台无关模块，但还没有可交付的 Windows 桌面壳、真实 Windows 感知适配器、安装器或完整设备闭环。

## 2. 产品目标、边界与非目标

### 2.1 MVP 目标

- 同一会话内支持文字输入与 push-to-talk 语音输入。
- LLM 流式输出，按短句分段，并发调用 TTS，按原顺序播放。
- 回复字幕、声音与 VTube Studio 表情属于同一 turn，可被新输入快速打断。
- 近期对话默认本地保存 7 天；长期记忆需用户显式开启，候选需可解释、可确认、可编辑、可删除、可导出。
- 视觉和主动发话默认关闭；开启后必须经过敏感窗口/内容 Guard，并可随时关闭。
- Windows 桌面壳最终需要窗口、托盘、连接状态、输入、打断、隐私开关、设备状态和统一退出。

### 2.2 明确非目标

- 第一版不自动点击、输入、控制其他应用或执行高风险电脑操作。
- 不做持续监听、唤醒词或后台全时麦克风采集。
- 不做复杂插件平台、向量数据库、微服务化或多用户服务。
- 不把 Mock 成功伪装成真实 provider 成功。
- 不在仓库或发布包中分发 Live2D 模型、角色图片、受保护台词库、声音模型、参考音频、Whisper 模型、真实聊天/截图/记忆或 API key。

### 2.3 资产与合规边界

项目以私人桌宠为目标，源码只提供框架和用户自行导入资产的机制。若公开分享或发布，需要重新审查产品命名、角色形象、声音、Live2D、参考音频和第三方模型许可。Pro 的技术方案不能默认这些资产可以随安装包分发。

## 3. 当前成熟度

### 3.1 已实现并有自动化证据

| 能力 | 当前实现 |
|---|---|
| 对话核心 | `TurnService`、turn 状态、按 session 抢占、取消 token、事件发布、observer/event sink |
| LLM | Mock 与 OpenAI-compatible streaming/complete provider；真实失败不会回退为 Mock |
| 对话流水线 | LLM delta → 分句 → 多 TTS worker → 有序音频播放 → 指标与清理 |
| TTS | Mock WAV 与 GPT-SoVITS `/tts`；WAV 校验、临时文件、可选持久缓存 |
| VTS | WebSocket client、认证 token store、重连 bridge、表达映射、turn event sink |
| Emotion/Prompt | 有界情绪变化、衰减、冷却、segment 装饰、不可信上下文标记 |
| 历史与记忆 | SQLite、WAL、迁移、FTS5、近期历史、长期记忆策略、确认与管理 API |
| Perception 核心 | 平台无关 Guard、变化检测、RapidOCR、内容过滤、遮挡、可选云端边界、buffer wipe |
| Proactive 核心 | 评分、安静时段、冷却、每日上限、敏感/DND/focus 抑制、idle runtime |
| STT 核心 | push-to-talk 状态机、sounddevice 输入适配、whisper.cpp 子进程、临时 WAV 清理 |
| 工程质量 | Python 3.11、uv lock、mypy strict、Ruff、pytest unit/integration/property、macOS/Windows CI |

### 3.2 已实现但尚未接入生产主生命周期

- `app.perception` 有完整工厂和协议，但 `app/main.py` 没有创建真实 capture/window/OCR/cloud adapters，也没有启动观察循环。
- `desktop_client.inputs` 能创建 recorder/STT，但没有桌面按钮、热键或生产 `InputController` 调用它。
- `ProactiveRuntime` 已接入主程序，但生产组合目前主要有 idle trigger；真实 focus/DND、前台窗口、视觉 publisher 等信号未接入。
- `SystemAudioPlayer`、真实 GPT-SoVITS、VTS 和真实 LLM 都需要用户环境与人工配置，当前没有真实设备验收证据。

### 3.3 尚未实现

- PySide6 或其他 Windows GUI、托盘、全局热键、中文输入法体验、重连 UI。
- Windows 前台窗口与受限窗口截图、多显示器/DPI/全屏/锁屏处理。
- Windows 用户数据/ACL/DPAPI 的统一实现。
- 单实例、开机启动、崩溃恢复、安装/升级/回滚/卸载。
- PyInstaller/Nuitka/MSIX 可运行产物；当前 wheel 也无法独立启动。
- 干净 Windows 标准用户、真实设备、真实外部服务和长时间稳定性门禁。

## 4. 当前技术栈与进程假设

### 4.1 主要技术栈

- Python `>=3.11,<3.12`
- FastAPI + Uvicorn：本机 HTTP/WebSocket gateway
- asyncio：turn、provider、TTS、播放、VTS、维护任务的并发与取消
- Pydantic v2：跨模块契约与配置
- httpx：LLM 与 GPT-SoVITS HTTP
- websockets：VTube Studio
- sounddevice/PortAudio：录音和系统音频
- SQLite/FTS5：近期历史、长期记忆、profile、feature flags
- RapidOCR + ONNX Runtime + Pillow：可选本地视觉依赖
- uv + hatchling：依赖锁定和 Python 构建
- pytest/Hypothesis/Ruff/mypy：质量门

### 4.2 当前与目标进程模型

目标仍是一个模块化 Python 单体，而不是一开始拆成微服务：

```text
[VTube Studio 独立进程]
[GPT-SoVITS 独立进程]
[用户选择的 LLM 服务：本机/局域网/云端]
             ↑
[一个 Python App：Backend + Desktop Client 模块]
```

当前实际可运行入口只有 FastAPI backend；`desktop_client` 只包含输入模块，没有 GUI。后续是否让 GUI 与 backend 处于同一个 OS 进程、同一事件循环，仍需要结合 P0 安全问题和 Qt/asyncio 生命周期做明确决策。

## 5. 默认运行行为

`config.yaml` 的默认值刻意保持低风险：

- LLM：`mock`
- TTS：`mock`
- 播放：`silent`
- VTS：关闭
- storage：开启
- recent history：数据库初始默认开启，保留 7 天
- long-term memory、vision、cloud vision、proactive：数据库初始默认关闭
- 记忆候选 LLM：关闭
- STT：关闭
- server：`127.0.0.1:8765`

因此默认启动不会联网、不会发声、不会请求麦克风或屏幕权限，但会创建本地数据库并保存显式对话历史。这里的“默认安全”不等于控制面已经安全；现有 HTTP/WS 无认证与 Origin 限制，详见风险审计 P0-01。

## 6. 关键领域模型

### 6.1 输入与 turn

所有显式输入最终应统一为 `UserMessage`：

- `message_id`
- `session_id`
- `user_id`
- `text`
- `input_mode = text | voice`
- `interruption_policy = stop_now | finish_sentence | ignore`
- `screen_context_allowed`
- `metadata`

`TurnState` 在 `accepted → streaming → speaking → completed` 之间推进，也可能进入 `cancelled` 或 `failed`。新输入一般可以抢占活动 turn；每轮拥有独立 cancellation token。

### 6.2 AI 请求与上下文

内部使用 provider-neutral `ChatRequest`。上下文来源显式标记：

- `recent_dialogue`
- `long_term_memory`
- `user_profile`
- `screen`
- `proactive`

信任级别区分用户陈述、已存事实、不可信观察和内部 intent。屏幕/主动上下文不能被当成用户指令，也不应自动进入长期记忆。

### 6.3 音频与表现

LLM delta 被 `DialogueSegmenter` 切成短句。每个 `DialogueSegment` 带 emotion、TTS style/speed、Live2D expression 和 interruptible。segment 转成 `TTSJob`，得到 `AudioResult`，最后按 segment index 有序播放。VTS 通过非阻塞 event sink 获取 expression 事件，避免网络 I/O 卡住主 turn。

### 6.4 历史与长期记忆

SQLite v1 schema 包含：

- `conversation_messages`
- `memories`
- `memory_sources`
- `user_profiles`
- `feature_flags`
- `memories_fts`
- `schema_migrations`

长期记忆支持 `user_profile / fact / event / emotion / relationship`，并记录 importance、confidence、sensitivity、来源和 active/superseded 状态。显式记忆候选需经过凭据拒绝、来源验证、policy 和确认流程。

### 6.5 Perception

平台无关设计为：

```text
active window metadata
→ window guard
→ capture
→ frame change detection
→ local OCR
→ content guard
→ redact all OCR regions
→ optional cloud analysis
→ sanitized PerceptionContext
→ wipe raw image/OCR data
```

公开给其他模块的只有短小、带 generation/time/sensitive 标记的 `PerceptionContext`。窗口标题、OCR spans 和 image bytes 应停留在 perception 内部边界。

### 6.6 Proactive

主动触发类型包括 idle、task complete、emotion shift、visual change、scheduled。引擎会考虑 feature flag、用户 turn、focus/DND、敏感状态、安静时段、冷却、每日上限、idle 时长和置信度。生成的 `ProactiveIntent` 会把调用者文本替换成固定目标，减少外部上下文把任意指令注入 LLM 的风险。

## 7. 必须保留的设计不变量

Pro 在设计修复时应尽量保留以下已经被测试的性质：

1. 真实 provider 失败必须显式失败，不能静默退回 Mock。
2. 每个 turn 可取消；新用户输入能够优先于主动行为。
3. LLM 生成、TTS 和播放可以流水并发，但声音必须按原 segment 顺序。
4. 取消、失败和退出后，临时音频及在途资源必须由明确 owner 清理。
5. 视觉未知/异常默认阻断，不允许为了可用性绕过 privacy guard。
6. 原始截图、OCR 全文和 PCM/WAV 不进入普通日志、历史或长期记忆。
7. 屏幕与 proactive 内容是 untrusted context，不是用户命令。
8. 长期记忆默认关闭，凭据永不保存，记忆可管理、可禁用。
9. 未发送的 UI 草稿只在客户端内存，不进入 backend、日志或数据库。
10. 文字与语音共享同一 `UserMessage → TurnService` 业务链，不能复制两套逻辑。
11. feature 关闭和 app 退出应形成资源清理屏障，而不是只翻转布尔值。
12. 默认启动不联网、不发声、不采集屏幕或麦克风。

## 8. 当前质量证据

截至提交 `d56cfbd`：

- 460 项收集，459 passed，1 skipped。
- 分支覆盖率 92.54%。
- Ruff lint、Ruff format、mypy strict 通过。
- macOS 与 Windows GitHub Actions quality jobs 通过。
- sdist/wheel 能生成，但 wheel 隔离导入已确认失败。
- 运行时锁定依赖未报告已知漏洞；开发依赖 `pytest 8.4.2` 有一项需升级到 9.x 评估的安全公告。

绿灯只证明源码环境，不代表 Windows GUI、安装包、ACL、真实设备、真实服务或长期运行已经通过。

## 9. 文档阅读注意事项

- `docs/windows_development_plan.md` 第 1.3 节、`docs/decisions/w00_owner_decisions.md` 与 `docs/gates/gate_w0.md`：当前产品、隐私、资产边界、人工决定和 Gate 状态，优先级高。
- `docs/project_architecture.md`：完整目标架构，部分目录与组件是规划而非当前代码。
- `docs/ai_backend_mac_implementation_report.md`：后端功能和延期项证据。
- `docs/windows_development_plan.md`：2026-07-17 起的唯一 Windows 分阶段执行计划；旧的固定 16 日排期已失效。
- `docs/full_code_windows_risk_audit_2026-07-15.md`：本轮全仓库复审后的最新风险基线；发生冲突时优先以代码和该审计的实证为准。

## 10. Pro 最需要先做的决策

1. Windows GUI 与 backend 同进程、同事件循环还是受限子进程；如果跨边界，使用什么安全 IPC。
2. loopback HTTP/WS 是否继续作为桌面控制面；如何认证、限制 Origin 和隔离 session。
3. package resource、`%LOCALAPPDATA%`、临时文件、日志、缓存、模型、秘密和迁移的统一路径模型。
4. Windows DACL、DPAPI/Credential Manager 与卸载/保留数据的明确契约。
5. 消息幂等、事件 seq/replay、断线恢复和 turn 状态保留协议。
6. 原生 OCR、sounddevice、whisper.cpp 的硬隔离、超时和终止方式。
7. 首个可安装垂直切片与 CI/release gate，而不是继续只扩展源码模块。
