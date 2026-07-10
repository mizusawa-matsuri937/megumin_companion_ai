# Megumin Desktop Companion AI 基础架构设计文档

> 设计目标：Windows 常驻陪伴型 AI 桌宠。第一阶段不做直播、不做复杂游戏 AI、不做自动控制电脑，而是先把 **Live2D 展示、低延迟语音与文字对话、记忆、情绪、屏幕感知、主动发话** 跑通。
> 合规边界：当前产品是个人私用的 Megumin 桌宠；项目代码只提供框架、测试占位接口和用户自行导入模型/声音机制。源码仓库不提交受保护的角色模型、图片、音频、声线数据或原作台词库，也不提供未经授权的声音克隆素材。若未来公开分享，必须先重新命名并完成资产和许可审查。
>
> Day 1 的人工确认结果、数据矩阵和变更规则以 [`day1_scope_freeze.md`](day1_scope_freeze.md) 为准；本文件不得放宽其中的隐私与资产边界。

---

## 0. 设计总原则

这个项目不要从一开始做成“大而全 AI 操作系统”。第一版应该是一个**本地桌面陪伴中枢**：

```text
云端 LLM 负责思考和语言
本地 STT 负责按键说话的语音转写
本地 GPT-SoVITS 负责声音
VTube Studio 负责 Live2D 展示
Python 后端负责记忆、情绪、屏幕观察、隐私过滤、主动发话、异步流水线
Desktop Client 负责文字对话框、语音输入适配、托盘、音频播放、VTS 连接、用户控制
```

第一版最关键的技术难点不是“模型有多强”，而是：

1. **低延迟**：LLM 流式输出，按句切分，TTS 一句一句合成，边生成边播放。
2. **陪伴感**：短句、情绪、表情、记忆、主动发话，而不是问答工具。
3. **隐私安全**：长期记忆、屏幕视觉和主动发话默认关闭；视觉开启后仍必须经过敏感应用拦截、变化检测和上传前的本地内容 Guard。
4. **适度可替换**：MVP 只抽象真实存在替换需求的 LLM、TTS、STT、VTS 和输入适配；插件、向量库和复杂 Diary 只保留文档扩展位置。

---

# 1. 项目总体定位

## 1.1 它不是普通聊天机器人

普通聊天机器人通常是：

```text
用户输入 → LLM 回复
```

Megumin Desktop Companion AI 应该是：

```text
用户状态 + 屏幕上下文 + 长短期记忆 + 情绪状态 + 当前时间 + 最近互动节奏
→ 角色化思考
→ 文字/语音/表情/动作同步输出
→ 记忆与情绪更新
```

它不只是回答问题，而是像“住在电脑里的虚拟角色”：

* 用户写代码时，她可以小声吐槽或鼓励。
* 用户深夜还在电脑前，她会关心。
* 用户很久没互动，她可以偶尔找话题。
* 用户认真聊天时，她能记住重要事实与情绪。
* 用户玩游戏时，她少打扰，只短句吐槽。

## 1.2 它不是 AI VTuber

AI VTuber 的核心场景通常是直播、弹幕、观众互动、内容表演。这个项目的核心场景是**私人陪伴**：

| 对比项  | AI VTuber  | Megumin Desktop Companion AI |
| ---- | ---------- | ---------------------------- |
| 主要对象 | 直播观众       | 单个用户                         |
| 输入源  | 弹幕、语音、游戏事件 | 文字输入、语音输入、屏幕状态、时间、记忆         |
| 输出目标 | 直播效果       | 陪伴感、低打扰、长期关系                 |
| 记忆重点 | 观众互动、节目效果  | 用户画像、情绪、生活/学习/工作状态           |
| 主动性  | 为直播节奏服务    | 为陪伴和关心服务                     |
| 风险边界 | 内容安全、直播规范  | 本地隐私、屏幕数据、API key、记忆管理       |

## 1.3 它不是传统桌面助手

传统桌面助手偏工具：

```text
打开软件、查文件、设提醒、控制电脑
```

本项目第一版**不做高风险自动操作电脑**，它偏陪伴：

```text
观察 → 理解 → 陪聊 → 提醒 → 情绪反馈 → 记忆
```

未来可以加日程提醒、QQ/AstrBot 插件、游戏状态插件，但必须通过权限系统控制。

---

# 2. 总体架构图

```mermaid
flowchart TB
    subgraph UI["展示与桌面交互层"]
        VTS["VTube Studio / Live2D 展示层"]
        CHAT["Chat Window<br/>消息列表 / 文字输入 / 流式回复 / 打断"]
        VOICE["Push-to-talk<br/>按键说话 / 临时音频"]
        INPUT["Input Controller<br/>统一 UserMessage / Session"]
        DC["Desktop Client Core / VTS Bridge<br/>WebSocket / 托盘 / 状态同步"]
        AQ["Audio Playback / Voice Queue"]
    end

    subgraph Backend["Companion Backend - Python + FastAPI + WebSocket"]
        API["FastAPI HTTP / WebSocket Gateway"]
        ORCH["Orchestrator<br/>对话编排 / 会话状态 / Cancellation"]
        BUS["Event Bus / Async Queues"]
        CB["Context Builder"]
        MM["Memory Manager"]
        EE["Emotion Engine"]
        PT["Proactive Talk Engine"]
        LOG["Config / Logging / Telemetry"]
    end

    subgraph Perception["感知与隐私层"]
        SO["Screen Observer<br/>窗口信息 / 截图 / OCR / 变化检测"]
        PG["Privacy Guard<br/>黑名单 / 裁剪 / 模糊 / 摘要 / 限流"]
        SA["Screen Analysis<br/>场景分类 / 多模态摘要"]
    end

    subgraph AI["AI 能力服务层"]
        LLM["LLM Provider<br/>DeepSeek / Qwen / 豆包 / Kimi / OpenAI-Compatible"]
        STT["Local STT Provider<br/>本地转写"]
        TTS["GPT-SoVITS TTS Service<br/>本地 API"]
        VISION["Configured Vision Provider<br/>用户配置的可选云端视觉服务"]
        OCR["OCR Tool<br/>PaddleOCR / Tesseract / Windows OCR"]
    end

    subgraph Storage["本地存储层"]
        SQL["SQLite<br/>用户画像 / 事件 / 配置 / 日志索引"]
        FS["Local Files<br/>临时音频 / 临时截图 / 日志 / VTS token"]
    end

    CHAT -->|显式发送 text| INPUT
    VOICE -->|临时音频| STT
    STT -->|转写后的 text| INPUT
    INPUT -->|UserMessage| DC
    DC -->|assistant delta / segment / state| CHAT
    DC <-->|WebSocket| API
    DC --> AQ
    AQ --> VTS
    DC <-->|VTS WebSocket API| VTS

    API --> ORCH
    ORCH <--> BUS
    ORCH --> CB
    ORCH --> LLM
    ORCH --> TTS
    ORCH --> EE
    ORCH --> MM

    LLM --> BUS
    TTS --> AQ
    EE --> DC
    DC --> VTS

    SO --> PG
    PG --> SA
    SA -->|视觉已开启且 Guard 允许| VISION
    VISION --> CB
    SA --> CB
    SO --> PT
    PT --> ORCH

    CB --> MM
    MM --> SQL
    SO --> OCR

    LOG --> SQL
    LOG --> FS
```

图中的 Desktop Client、Backend、Perception 是同一 Python App 内的模块边界；MVP 不把它们拆成独立进程。插件、向量检索和复杂 Diary 的扩展位置只在 Post-MVP 章节说明，不进入这张 MVP 主运行图。

---

## 2.1 文字与语音双输入

文字输入和语音输入是两个桌面端输入适配器，不是两套对话系统。它们必须在进入后端前统一为 `UserMessage`：

```text
Chat Window 显式发送
→ InputController
→ UserMessage(text=输入内容, input_mode="text")

Push-to-talk 按键说话
→ 临时音频
→ Local STT Provider
→ InputController
→ UserMessage(text=转写文本, input_mode="voice")

两者
→ 同一个 WebSocket/API
→ 同一个 Orchestrator / Context / Memory / Emotion / LLM / TTS 流水线
```

交互规则：

1. 文字与语音共享 `session_id`、最近对话、记忆和当前情绪，用户可以在同一轮会话中自由切换。
2. Chat Window 至少包含消息列表、文字输入框、发送按钮/快捷键、流式回复显示、打断按钮和连接状态。
3. 用户文字发送后，默认同时显示流式文字回复并播放 TTS；静音模式只关闭声音，不影响文字回复。
4. 新文字消息和新的语音输入都可以按照 `interruption_policy` 打断当前 turn。
5. 未显式发送的文字草稿只存在于客户端内存，不进入网络、日志、数据库、记忆或 Debug 状态。
6. Desktop Client 只负责输入、展示和设备交互，不直接调用 LLM/TTS；云端请求统一由 Backend 发起。
7. 语音输入仅在用户按住或点击说话时采集；不持续监听，不实现唤醒词。
8. STT 必须本地运行；原始音频只存在于内存或临时文件，转写完成、取消或失败后立即清理，不上传、不进入日志或记忆。
9. STT 的具体实现属于 Voice Input Adapter；核心对话层只接收转写后的 `text`，不为语音复制业务逻辑。STT 失败时键盘输入仍可用。

---

# 3. 推荐技术栈

VTube Studio 官方提供 Public API，可通过 WebSocket 触发热键、传入追踪数据、加载模型、订阅事件、移动模型等；其默认 WebSocket 地址是 `ws://localhost:8001`，用户也可以在 VTS 中修改端口。([GitHub][1])
GPT-SoVITS 官方仓库定位为 few-shot voice conversion 与 TTS WebUI，支持 zero-shot、few-shot、跨语言推理；仓库中也包含 `api.py` / `api_v2.py`，`api_v2.py` 暴露 `/tts` GET/POST 接口，并支持流式返回音频片段。([GitHub][2])

| 层         | 推荐技术                                                  | 理由                                                                                                                                 |
| --------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Python 版本 | Python 3.11                                           | 兼容 FastAPI、asyncio、Pydantic、GPT-SoVITS 常见环境；不要追最新解释器，减少依赖坑。                                                                        |
| 后端框架      | FastAPI                                               | HTTP + WebSocket 统一；适合本地 API、Debug WebUI、客户端通信。FastAPI 官方文档提供 WebSocket 连接和广播示例。([FastAPI][3])                                     |
| 异步并发      | asyncio / asyncio.Queue / TaskGroup                   | LLM streaming、TTS 队列、音频播放、屏幕观察都适合 producer-consumer；`asyncio.Queue` 是 FIFO 队列，适合 async/await 代码中的协作式队列。([Python documentation][4]) |
| 数据结构      | Pydantic v2                                           | 用于当前阶段实际使用的跨模块消息、事件和配置；插件等 Post-MVP Schema 不提前创建。Pydantic 模型通过继承 `BaseModel` 与类型注解定义字段。([Pydantic][5])                                                           |
| LLM SDK   | OpenAI Python SDK 兼容层 + 自定义 Provider                  | DeepSeek、Qwen、Kimi 等都提供 OpenAI-compatible 接口或迁移方式；抽象 `LLMProvider` 后只换 `base_url/model/api_key`。([DeepSeek API 文档][6])             |
| STT       | 本地 STT Provider 接口；具体引擎在实现日按 Windows 环境验证后确定 | 只支持按键说话，本地转写；核心层只接收文本，原始录音即时清理。 |
| TTS       | 本地 GPT-SoVITS API                                     | 第一版本地跑 TTS，减少语音隐私上传；可按情绪选择 reference audio / preset / speed。                                                                       |
| VTS 集成    | VTube Studio WebSocket API；可选 pyvts                   | 官方 API 完整；pyvts 是 Python 侧 VTube Studio API 库，可减少手写 WebSocket JSON 的成本。([GitHub][7])                                               |
| 音频播放      | sounddevice / pyaudio / simpleaudio / pygame + ffmpeg | MVP 推荐 `sounddevice + soundfile` 或 `simpleaudio`；后续需要虚拟声卡/低延迟可转 WASAPI/PortAudio。                                                  |
| 屏幕截图      | mss                                                   | mss 是跨平台截图库，支持多显示器、NumPy/OpenCV 集成，适合高频截图场景。([PyPI][8])                                                                            |
| 活动窗口      | pywin32 + ctypes；辅助 pygetwindow                       | pywin32 提供 Windows API/COM 访问；PyGetWindow 可获取窗口对象和标题，适合 MVP 快速实现。([GitHub][9])                                                     |
| OCR       | PaddleOCR / Tesseract / Windows OCR                   | MVP 可先用 OCR 摘要替代上传截图；中文建议 PaddleOCR，轻量可先留接口。                                                                                       |
| 图像变化检测    | imagehash / OpenCV diff / SSIM                        | 用于避免每张截图都上传大模型。                                                                                                                    |
| 关系型存储     | SQLite                                                | Python 内置 `sqlite3` 适合本地轻量数据库，SQLite 不需要单独服务器，适合桌面应用本地存储。([Python documentation][10])                                              |
| 向量库（Post-MVP） | ChromaDB 或 FAISS                                | MVP 不安装、不配置、不创建接口；只有 SQLite 检索被实际数据证明不足时才独立验证。([Chroma Docs][11])                                                                 |
| 配置        | YAML + `.env`                                         | 非敏感配置放 `config.yaml`，API key 放 `.env` 或系统环境变量。Kimi/OpenAI 等官方文档也强调 API key 不应暴露在客户端代码、仓库或日志里。([Kimi API Platform][12])             |
| 日志        | structlog / logging / loguru                          | 推荐结构化日志，便于统计首 token、首句播放、TTS 延迟、主动发话原因。                                                                                            |
| 打包        | PyInstaller / Nuitka                                  | MVP 用 PyInstaller；后续可拆分后端服务 + 客户端安装器。                                                                                              |
| 桌面 UI     | PySide6 / PyQt6；仅托盘原型可用 pystray                    | MVP 已包含文字对话框，优先用同一 GUI 框架承载 Chat Window、流式消息、托盘和设置；避免托盘与对话框分属两套 UI 生命周期。                                                     |
| 双输入路由    | Desktop Client `InputController` + `UserMessage`       | 文字内容和语音转写文本统一进入同一 session/turn；语音转写实现留在输入适配层，核心对话层不感知麦克风细节。                                                               |

---

# 4. 进程与服务拆分

## 4.1 MVP 进程模型

```text
[Process 1] VTube Studio
    负责 Live2D 渲染、模型加载、表情热键、嘴型、窗口行为。

[Process 2] GPT-SoVITS
    本地 TTS 服务，监听 http://127.0.0.1:9880 或用户配置端口。

[Process 3] Python App
    单进程模块化应用，包含 FastAPI Backend 与 Desktop Client 模块。
    负责文字对话框、按键说话、本地 STT、统一输入路由、LLM 调用、
    上下文、记忆、情绪、屏幕视觉、主动发话、托盘、音频播放和 VTS Bridge。
```

## 4.2 Python App 内部模块

MVP 固定为：

```text
VTube Studio 独立
GPT-SoVITS 独立
Python App 单进程：
    FastAPI Backend
    Desktop Client 最小文字对话框/托盘
    Text / Push-to-talk / Local STT Input Controller
    VTS Bridge
    Audio Player
    Screen Observer
```

也就是：

```text
python app/main.py
```

内部用 `asyncio` 启动多个 task：

```text
FastAPI server task
LLM pipeline task
local STT task（仅按键说话期间）
TTS worker task
Audio player task
VTS bridge task
Screen observer task
Proactive engine task
recent dialogue retention cleanup task
```

## 4.3 后续可拆成多服务

当 MVP 稳定后再拆：

| 服务                   | 拆分时机              | 说明                       |
| -------------------- | ----------------- | ------------------------ |
| `companion-api`      | 功能稳定后             | 对话编排、上下文、记忆、情绪。          |
| `perception-service` | 屏幕观察复杂后           | 截图、OCR、隐私过滤、场景分类。        |
| `voice-service`      | TTS preset 多、并发多后 | GPT-SoVITS 包装、缓存、音频格式转换。 |
| `desktop-client`     | UI 复杂后            | 文字对话框、语音输入适配、托盘、设置页、VTS、音频、字幕。 |
| `plugin-host`        | 插件变多后             | 权限隔离、插件生命周期、事件路由。        |

第一阶段不建议一开始上微服务。先单体模块化，后续按边界拆。

---

# 5. 推荐目录结构

```text
megumin_companion_ai/
├─ app/
│  ├─ main.py
│  ├─ bootstrap.py
│  ├─ config/
│  │  ├─ settings.py
│  │  ├─ default_config.yaml
│  │  ├─ logging.yaml
│  │  └─ secrets.example.env
│  ├─ core/
│  │  ├─ orchestrator.py
│  │  ├─ event_bus.py
│  │  ├─ cancellation.py
│  │  ├─ lifecycle.py
│  │  ├─ errors.py
│  │  └─ clock.py
│  ├─ api/
│  │  ├─ http_routes.py
│  │  ├─ ws_routes.py
│  │  ├─ debug_routes.py
│  │  └─ dependencies.py
│  ├─ clients/
│  │  ├─ llm/
│  │  │  ├─ base.py
│  │  │  ├─ openai_compatible.py
│  │  │  ├─ deepseek.py
│  │  │  ├─ qwen.py
│  │  │  ├─ kimi.py
│  │  │  └─ mock_llm.py
│  │  ├─ tts/
│  │  │  ├─ base.py
│  │  │  ├─ gpt_sovits.py
│  │  │  └─ mock_tts.py
│  │  ├─ vts/
│  │  │  ├─ vts_bridge.py
│  │  │  ├─ vts_protocol.py
│  │  │  ├─ vts_token_store.py
│  │  │  └─ expression_mapper.py
│  │  └─ embedding/
│  │     ├─ base.py
│  │     └─ openai_compatible_embedding.py
│  ├─ services/
│  │  ├─ dialogue_service.py
│  │  ├─ context_service.py
│  │  ├─ memory_service.py
│  │  ├─ emotion_service.py
│  │  ├─ tts_service.py
│  │  ├─ audio_service.py
│  │  ├─ vts_service.py
│  │  └─ proactive_service.py
│  ├─ pipelines/
│  │  ├─ llm_stream_pipeline.py
│  │  ├─ segmenter.py
│  │  ├─ tts_pipeline.py
│  │  ├─ audio_pipeline.py
│  │  ├─ screen_pipeline.py
│  │  └─ proactive_pipeline.py
│  ├─ memory/
│  │  ├─ short_term.py
│  │  ├─ long_term.py
│  │  ├─ profile_store.py
│  │  ├─ diary.py  # Post-MVP，MVP 不创建
│  │  ├─ write_policy.py
│  │  ├─ retriever.py
│  │  └─ dedup.py
│  ├─ emotion/
│  │  ├─ state.py
│  │  ├─ updater.py
│  │  ├─ decay.py
│  │  ├─ mapper.py
│  │  └─ presets.py
│  ├─ perception/
│  │  ├─ screen_observer.py
│  │  ├─ active_window.py
│  │  ├─ screenshot.py
│  │  ├─ change_detector.py
│  │  ├─ ocr.py
│  │  ├─ privacy_guard.py
│  │  ├─ scene_classifier.py
│  │  └─ multimodal_summarizer.py
│  ├─ proactive/
│  │  ├─ trigger_rules.py
│  │  ├─ scorer.py
│  │  ├─ cooldown.py
│  │  ├─ state_machine.py
│  │  └─ topic_planner.py
│  ├─ plugins/  # Post-MVP，MVP 不创建
│  │  ├─ base.py
│  │  ├─ manager.py
│  │  ├─ permissions.py
│  │  ├─ sandbox.py
│  │  ├─ astrbot_adapter/
│  │  ├─ game_state/
│  │  └─ schedule/
│  ├─ schemas/
│  │  ├─ messages.py
│  │  ├─ dialogue.py
│  │  ├─ emotion.py
│  │  ├─ memory.py
│  │  ├─ screen.py
│  │  ├─ tts.py
│  │  ├─ audio.py
│  │  ├─ vts.py
│  │  ├─ plugins.py  # Post-MVP，MVP 不创建
│  │  └─ events.py
│  ├─ storage/
│  │  ├─ sqlite.py
│  │  ├─ migrations/
│  │  ├─ vector_store.py  # Post-MVP，MVP 不创建
│  │  ├─ file_store.py
│  │  └─ repositories/
│  ├─ prompts/
│  │  ├─ system_prompt.md
│  │  ├─ character_style.md
│  │  ├─ memory_rules.md
│  │  ├─ screen_rules.md
│  │  └─ output_schema.md
│  ├─ debug/
│  │  ├─ webui.py
│  │  ├─ replay.py
│  │  ├─ latency_dashboard.py
│  │  └─ prompt_viewer.py
│  └─ utils/
│     ├─ text.py
│     ├─ audio.py
│     ├─ image.py
│     ├─ privacy.py
│     ├─ ids.py
│     └─ time.py
├─ desktop_client/
│  ├─ main.py
│  ├─ input_controller.py
│  ├─ ws_client.py
│  ├─ audio_player.py
│  ├─ inputs/
│  │  ├─ voice_input.py
│  │  ├─ stt_provider.py
│  │  ├─ local_stt.py
│  │  └─ hotkeys.py
│  └─ ui/
│     ├─ chat_window.py
│     ├─ tray.py
│     ├─ subtitle_window.py
│     └─ settings_window.py
├─ data/
│  ├─ db/
│  ├─ vector/  # Post-MVP，MVP 不创建
│  ├─ logs/
│  ├─ cache/
│  │  ├─ audio/
│  │  └─ screenshots/
│  ├─ diary/  # Post-MVP，MVP 不创建
│  └─ vts_tokens/
├─ assets/
│  ├─ placeholder/
│  │  ├─ README.md
│  │  └─ test_audio/
│  └─ user_imported/
│     ├─ live2d/
│     └─ voice/
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ e2e/
│  ├─ fixtures/
│  └─ performance/
├─ tools/
│  ├─ run_backend.ps1
│  ├─ run_gpt_sovits.ps1
│  ├─ check_vts_connection.py
│  ├─ tts_latency_test.py
│  ├─ screen_privacy_test.py
│  └─ memory_inspect.py
├─ docs/
│  ├─ project_architecture.md
│  ├─ daily_development_plan.md
│  ├─ privacy.md
│  ├─ user_setup.md
│  └─ plugin_dev.md  # Post-MVP
├─ config.yaml
├─ .env.example
├─ pyproject.toml
└─ README.md
```

## 5.1 目录职责说明

| 目录               | 职责                                     |
| ---------------- | -------------------------------------- |
| `app/core`       | 核心运行时：编排器、事件总线、取消 token、生命周期。          |
| `app/api`        | FastAPI HTTP/WebSocket 接口。             |
| `app/clients`    | MVP 外部能力适配层：LLM、TTS、VTS；Embedding 为 Post-MVP。         |
| `app/services`   | 面向业务的服务层，对外隐藏底层实现。                     |
| `app/pipelines`  | 低延迟流水线：LLM streaming、分句、TTS、音频播放、屏幕观察。 |
| `app/memory`     | 最近对话、自动重要记忆、检索、去重、敏感确认和用户画像。                  |
| `app/emotion`    | 数值情绪、衰减、映射、输出控制。                       |
| `app/perception` | 屏幕观察、隐私保护、OCR、场景分类。                    |
| `app/proactive`  | 主动发话规则、评分、冷却、状态机。                      |
| `app/plugins`    | Post-MVP 插件接口、权限与加载；MVP 不实现。              |
| `app/schemas`    | Pydantic 统一数据结构。                       |
| `desktop_client` | 同一 Python App 内的文字对话框、按键说话/本地 STT、统一输入路由、托盘、音频播放、字幕和 VTS Bridge。 |
| `data`           | MVP 本地数据库、日志、缓存和 VTS token；向量库/复杂日记按需延后。 |
| `assets`         | 占位资源和用户自行导入资源。代码仓库不应包含受版权保护资源。         |
| `tests`          | 单元、集成、端到端、性能测试。                        |
| `tools`          | 开发期命令行工具。                              |

## 5.2 文件夹分类与创建时机

目录不要一开始全部建成空壳。建议按“核心源码、客户端、运行时数据、资源、测试、工具、文档、配置”八类管理，并按阶段逐步创建。

| 文件夹 | 分类 | 创建时机 | 职责边界 |
| --- | --- | --- | --- |
| `app/` | 后端核心源码 | Phase 0 必建 | 本地 Companion Backend 的主包，承载 API、编排、队列、记忆、情绪、屏幕感知、主动发话等能力。 |
| `app/config/` | 配置系统 | Phase 0 必建 | 只负责配置模型、默认配置、日志配置和示例密钥文件；不写业务逻辑。 |
| `app/core/` | 核心运行时 | Phase 0 必建 | 事件总线、生命周期、取消控制、统一错误、时间工具等底层能力；不能依赖具体 LLM/TTS/VTS 实现。 |
| `app/api/` | 本地接口层 | Phase 0 必建 | FastAPI HTTP/WebSocket 路由、依赖注入、debug API；只做协议转换，不承载复杂业务流程。 |
| `app/clients/` | 外部能力适配层 | Phase 1 起创建 | 所有外部服务或本地服务的客户端入口；Provider 差异封装在这里，业务层不直接拼 HTTP/WebSocket 细节。 |
| `app/clients/llm/` | LLM Provider | Phase 1-2 | OpenAI-compatible 与 mock LLM；先统一流式 token，其他 Provider 有真实需求时再增加。 |
| `app/clients/tts/` | TTS Provider | Phase 1-2 | GPT-SoVITS 与 mock TTS；统一输入 `TTSJob`，统一输出音频结果。 |
| `app/clients/vts/` | VTube Studio 适配 | Phase 2 | VTS WebSocket 协议、认证 token、表情热键、模型移动、断线重连。 |
| `app/clients/embedding/` | Embedding Provider | Post-MVP 按需 | 基础 SQLite 检索不足时再创建实现；不要把记忆写入策略放在这里。 |
| `app/services/` | 业务服务层 | Phase 1 起逐步创建 | 对话、上下文、记忆、情绪、TTS、音频、VTS、主动发话等面向业务的门面；隐藏底层 clients/pipelines 细节。 |
| `app/pipelines/` | 异步低延迟流水线 | Phase 1 必建 | streaming、分句、TTS 队列、音频队列、屏幕观察、主动发话等 producer-consumer 流程；负责并发、顺序、取消、超时。 |
| `app/memory/` | 记忆领域模块 | Phase 4 | 最近 7 天对话、用户画像、自动重要记忆、敏感确认、简单检索和去重；复杂 Diary/向量检索延后。 |
| `app/emotion/` | 情绪领域模块 | Phase 3 | EmotionState、更新规则、衰减、标签映射、preset；只输出情绪状态和映射结果。 |
| `app/perception/` | 屏幕感知与隐私前置 | Phase 5 | 活动窗口、Privacy Guard、受控截图、变化检测、OCR 和场景分类；默认本地优先且先拦截后截图。 |
| `app/proactive/` | 主动发话领域模块 | Phase 6 | 触发规则、评分、冷却、状态机、话题计划；不能绕过隐私和用户专注模式。 |
| `app/plugins/` | 插件系统 | Post-MVP | 有第一个真实插件需求后再实现基类、管理器、权限与隔离；默认禁用。 |
| `app/plugins/astrbot_adapter/` | 插件示例/预留 | Post-MVP | AstrBot/QQ 适配预留；只通过权限系统收发事件。 |
| `app/plugins/game_state/` | 插件示例/预留 | Post-MVP | 游戏状态插件预留；默认只接收脱敏后的游戏状态或摘要。 |
| `app/plugins/schedule/` | 插件示例/预留 | Post-MVP | 日程提醒插件；只做提醒事件，不做自动控制电脑。 |
| `app/schemas/` | 跨模块数据契约 | Phase 0 建最小集合，后续按需 | Phase 0 只建消息、对话、TTS、音频和错误结构；记忆、屏幕、VTS Schema 到对应 MVP 阶段再创建；插件 Schema 仅 Post-MVP 立项后创建；不放业务算法。 |
| `app/storage/` | 本地存储基础设施 | Phase 4 | SQLite、文件存储、仓储和迁移；只处理持久化，不决定记忆重要性。 |
| `app/storage/migrations/` | 数据库迁移 | Phase 4 | SQLite 表结构版本管理；每次 schema 改动必须有迁移记录。 |
| `app/storage/repositories/` | 仓储层 | Phase 4 | 对话、记忆、用户画像等数据访问封装；避免业务层散落 SQL。 |
| `app/prompts/` | Prompt 文本资产 | Phase 1 起创建 | system prompt、角色风格、记忆规则、屏幕规则、输出格式；作为可版本化文本维护。 |
| `app/debug/` | 本地调试能力 | Phase 0 起逐步创建 | Debug WebUI、回放、延迟面板、prompt 预览；默认只本机访问。 |
| `app/utils/` | 通用小工具 | Phase 0 起按需创建 | 文本、音频、图片、隐私、ID、时间等无领域状态的纯工具；避免变成业务杂物间。 |
| `desktop_client/` | 桌面客户端源码 | Phase 1 建协议，Phase 7 完善 | 文字/语音输入、统一 `UserMessage` 路由、对话框、托盘、音频播放、字幕、设置页和后端 WebSocket 客户端；不直接调用 LLM/TTS。 |
| `desktop_client/inputs/` | 输入适配 | Phase 1 预留协议，Phase 7 实现 | 只在按键说话期间采集临时音频，通过本地 STT 得到文本并交给 `input_controller`；不构造 Prompt，不直接访问记忆或 LLM。 |
| `desktop_client/ui/` | 桌面 UI | Phase 7 | `chat_window`、托盘、字幕和设置页；只展示后端状态并提交用户显式操作，未发送草稿不得离开客户端内存。 |
| `data/` | 运行时本地数据 | 运行时生成 | 本地数据库、向量索引、日志、缓存、日记、VTS token；默认不提交真实数据。 |
| `data/db/` | SQLite 数据 | 运行时生成 | `companion.sqlite3` 等本地数据库文件。 |
| `data/vector/` | 向量索引 | Post-MVP 按需生成 | 只有 SQLite 检索不足且经过技术验证后才生成 ChromaDB/FAISS 索引。 |
| `data/logs/` | 本地日志 | Phase 0 运行时生成 | 结构化日志、延迟指标、错误日志；必须脱敏。 |
| `data/cache/` | 缓存根目录 | Phase 2 起运行时生成 | 临时音频、截图、OCR 中间结果；可清理、可重建。 |
| `data/cache/audio/` | 音频缓存 | Phase 1-2 运行时生成 | TTS 生成音频和 mock 音频缓存。 |
| `data/cache/screenshots/` | 截图临时目录 | Phase 5 运行时按需生成 | 仅用于无法纯内存处理时的合规中间文件；分析成功、失败、取消或退出后都立即清理，不作为持久缓存。 |
| `data/diary/` | 本地日记 | Post-MVP 按需生成 | 复杂每日总结启用后再创建内容和索引。 |
| `data/vts_tokens/` | VTS 认证 token | Phase 2 运行时生成 | VTube Studio authentication token；不可公开。 |
| `assets/` | 静态与用户导入资源 | Phase 0 创建目录 | 占位资源和用户自行导入资源；仓库不内置受版权保护角色模型、图片、音频、声线。 |
| `assets/placeholder/` | 测试占位资源 | Phase 0 | README、静音音频、测试用无版权占位资源。 |
| `assets/placeholder/test_audio/` | 音频测试占位 | Phase 2 | mock TTS、音频播放测试使用。 |
| `assets/user_imported/` | 用户私有资源入口 | 用户使用时导入 | 用户自行放入 Live2D、声音、参考音频等；不作为项目分发内容。 |
| `assets/user_imported/live2d/` | 用户 Live2D 模型 | 用户使用时导入 | 用户自行授权的模型文件。 |
| `assets/user_imported/voice/` | 用户声音参考音频 | 用户使用时导入 | 用户自行授权的 reference audio / preset 音频。 |
| `tests/` | 测试根目录 | Phase 0 必建 | 单元、集成、端到端、fixtures、性能测试统一入口。 |
| `tests/unit/` | 单元测试 | Phase 0 起 | schema、配置、segmenter、emotion、memory policy 等无外部依赖测试。 |
| `tests/integration/` | 集成测试 | Phase 1 起 | mock VTS、mock LLM、mock TTS、SQLite、WebSocket 等跨模块测试。 |
| `tests/e2e/` | 端到端测试 | Phase 2 起 | 从用户输入到 segment/TTS/audio/VTS action 的完整链路。 |
| `tests/fixtures/` | 测试样本 | Phase 0 起 | prompt 样本、截图样本、音频样本、配置样本；不得包含真实隐私数据。 |
| `tests/performance/` | 性能测试 | Phase 2 起 | 首 token、首句播放、TTS 延迟、屏幕分析耗时。 |
| `tools/` | 开发和运维工具 | Phase 0 起按需创建 | 启动脚本、连接检查、延迟测试、隐私测试、记忆查看；面向开发者，不进入业务运行路径。 |
| `docs/` | 项目文档 | 现在已存在 | 架构、插件开发、隐私、用户安装、开发计划；重大边界变化先更新文档。 |

## 5.3 根目录文件分布

| 文件 | 分类 | 职责 |
| --- | --- | --- |
| `README.md` | 项目入口文档 | 说明项目定位、启动方式、合规边界、MVP 状态。 |
| `pyproject.toml` | Python 工程配置 | 依赖、格式化、测试、类型检查、打包元数据。 |
| `config.yaml` | 用户可编辑默认配置 | LLM/TTS/VTS/隐私/记忆/主动发话等非敏感配置。 |
| `.env.example` | 环境变量示例 | API key 名称示例，不包含真实密钥。 |

## 5.4 目录边界规则

1. `clients` 只适配外部系统，不能写“是否应该主动发话”“是否应该记忆”的业务判断。
2. `services` 是业务门面，可以组合多个模块，但不要在这里写长时间运行的队列循环。
3. `pipelines` 管并发、顺序、取消、超时和背压，是低延迟体验的核心。
4. `schemas` 只能定义跨模块契约，不放数据库 SQL、Provider 请求细节或业务算法。
5. `memory` 决定记忆策略，`storage` 只负责把数据可靠存取。
6. `perception` 产出的屏幕信息必须先经过 `privacy_guard`，再进入 `context_service` 或 `proactive_service`。
7. `desktop_client` 只和后端 API/WebSocket、VTS、音频设备交互，不直接请求云端 LLM/TTS。
8. `desktop_client/input_controller.py` 是文字与语音输入的唯一汇合点；两种输入共享 `session_id` 并统一生成 `UserMessage`，不得复制两套上下文或记忆逻辑。
9. `desktop_client/ui/chat_window.py` 只在用户显式发送后提交文字；未发送草稿不得进入日志、网络、持久化或 Debug 状态。
10. `plugins` 默认只接收裁剪后的上下文；任何读取记忆、屏幕、外网的行为都必须走权限系统。
11. `data` 和 `assets/user_imported` 是用户本地数据边界，不能作为公共仓库内容分发。
12. `tools` 可以调用业务模块做检查，但业务模块不能反向依赖 `tools`。

---

# 6. 核心数据结构设计

以下是目标态的 Pydantic 风格结构，不是要求 Phase 0 一次性创建的完整代码。每个阶段只实现当前调用链实际使用的 Schema；Post-MVP 结构仅作边界说明，避免提前生成空抽象。

```python
from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class InputMode(str, Enum):
    text = "text"
    voice = "voice"
    proactive = "proactive"


class EmotionLabel(str, Enum):
    neutral = "neutral"
    happy = "happy"
    shy = "shy"
    proud = "proud"
    angry_cute = "angry_cute"
    worried = "worried"
    bored = "bored"
    excited = "excited"
    explosion_mode = "explosion_mode"
    sleepy = "sleepy"
    focused = "focused"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class UserMessage(BaseModel):
    message_id: str
    session_id: str
    user_id: str = "local_user"
    # 文字输入时是用户显式发送的内容；语音输入时是转写后的文本。
    # 不保存未发送草稿，也不在这里传递原始麦克风音频。
    text: str
    input_mode: InputMode = InputMode.text
    created_at: datetime
    interruption_policy: Literal["stop_now", "finish_sentence", "ignore"] = "stop_now"
    screen_context_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssistantMessage(BaseModel):
    message_id: str
    session_id: str
    turn_id: str
    full_text: str
    segments: list["DialogueSegment"]
    emotion_snapshot: "EmotionState"
    created_at: datetime
    finished_at: datetime | None = None
    cancelled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueSegment(BaseModel):
    segment_id: str
    turn_id: str
    index: int
    text: str

    emotion: EmotionLabel = EmotionLabel.neutral
    tts_style: str = "default"
    live2d_expression: str = "neutral"

    priority: Priority = Priority.normal
    interruptible: bool = True
    action_hints: list[str] = Field(default_factory=list)
    # 例如 ["look_left", "small_shake", "cast_explosion_pose"]

    estimated_speech_ms: int | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmotionState(BaseModel):
    affection: float = 0.35
    energy: float = 0.60
    curiosity: float = 0.50
    concern: float = 0.15
    embarrassment: float = 0.10
    pride: float = 0.55
    explosion_urge: float = 0.45
    boredom: float = 0.20

    dominant_label: EmotionLabel = EmotionLabel.neutral
    intensity: float = 0.30
    last_updated_at: datetime
    reason: str | None = None


class MemoryType(str, Enum):
    user_profile = "user_profile"
    dialogue_long_term = "dialogue_long_term"
    event = "event"
    emotion = "emotion"
    relationship = "relationship"


class MemoryItem(BaseModel):
    memory_id: str
    user_id: str = "local_user"
    memory_type: MemoryType
    content: str
    summary: str | None = None

    importance_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)
    # MVP 只允许用户主动发送的对话或用户手动操作成为来源。
    source: Literal["dialogue", "manual"]
    source_message_id: str

    tags: list[str] = Field(default_factory=list)
    related_emotion: EmotionLabel | None = None
    related_time: datetime | None = None

    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    user_visible: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScreenSnapshot(BaseModel):
    snapshot_id: str
    captured_at: datetime

    active_window_title: str | None
    process_name: str | None
    app_category: Literal[
        "coding", "browser", "video", "editing", "game",
        "chat", "payment", "password", "unknown", "idle"
    ] = "unknown"

    image_path: str | None = None
    thumbnail_path: str | None = None
    ocr_text: str | None = None
    perceptual_hash: str | None = None

    is_sensitive: bool = False
    privacy_actions: list[str] = Field(default_factory=list)
    upload_allowed: bool = False
    change_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScreenAnalysisResult(BaseModel):
    analysis_id: str
    snapshot_id: str
    created_at: datetime

    scene_type: Literal[
        "coding", "reading", "watching_video", "editing_video",
        "gaming", "chatting", "idle", "unknown"
    ]
    confidence: float = Field(ge=0.0, le=1.0)

    local_summary: str
    cloud_summary: str | None = None
    detected_topics: list[str] = Field(default_factory=list)
    user_possible_state: Literal[
        "focused", "stuck", "relaxed", "busy", "distracted", "unknown"
    ] = "unknown"

    proactive_hint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProactiveTrigger(BaseModel):
    trigger_id: str
    trigger_type: Literal[
        "long_silence", "coding", "video_editing", "gaming",
        "late_night", "stuck_page", "window_switching",
        "long_study", "custom_app"
    ]
    created_at: datetime

    confidence: float = Field(ge=0.0, le=1.0)
    priority: Priority
    score: float
    cooldown_key: str
    reason: str

    screen_analysis_id: str | None = None
    memory_refs: list[str] = Field(default_factory=list)
    suggested_intent: str | None = None
    suppressed: bool = False
    suppress_reason: str | None = None


class TTSJob(BaseModel):
    job_id: str
    turn_id: str
    segment_id: str
    text: str

    style: str = "default"
    emotion: EmotionLabel = EmotionLabel.neutral
    ref_audio_path: str | None = None
    prompt_text: str | None = None
    speed_factor: float = 1.0
    pitch: float | None = None

    priority: Priority = Priority.normal
    interruptible: bool = True
    timeout_ms: int = 8000
    created_at: datetime
    cancellation_token_id: str


class AudioSegment(BaseModel):
    audio_id: str
    job_id: str
    turn_id: str
    segment_id: str

    audio_path: str
    sample_rate: int
    duration_ms: int | None = None
    text: str

    emotion: EmotionLabel
    live2d_expression: str
    ready_at: datetime
    play_started_at: datetime | None = None
    play_finished_at: datetime | None = None


class VTubeAction(BaseModel):
    action_id: str
    action_type: Literal[
        "trigger_hotkey",
        "move_model",
        "set_expression",
        "set_parameter",
        "reset_expression",
        "show_subtitle",
        "hide_subtitle"
    ]
    created_at: datetime
    priority: Priority = Priority.normal

    hotkey_id: str | None = None
    expression: str | None = None
    parameter_name: str | None = None
    parameter_value: float | None = None

    position_x: float | None = None
    position_y: float | None = None
    rotation: float | None = None
    size: float | None = None
    duration_sec: float | None = None

    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`UserMessage.text` 是进入核心对话层的规范化文本。Chat Window 产生 `input_mode=text`，本地 STT 完成后由 Voice Input Adapter 产生 `input_mode=voice`；两者使用同一 `session_id` 和 turn/cancellation 机制。Chat Window 的本地草稿不属于 `UserMessage`，只有用户显式发送后才创建消息。插件事件只在 Post-MVP 章节保留接口草图，不创建当前 Schema。

---

# 7. LLM 到 TTS 的低延迟流水线

这是本项目第一阶段最核心的工程链路。

## 7.1 目标

不要这样：

```text
LLM 完整生成完 → 整段文本给 TTS → 等整段音频 → 播放
```

要这样：

```text
LLM streaming token
→ Segmenter 检测到一句完整自然句
→ 立刻创建 TTSJob
→ TTSWorker 调 GPT-SoVITS
→ AudioQueue 顺序播放
→ 播放第 1 句时，LLM 继续生成第 2/3 句，TTS 继续合成后续句
```

## 7.2 核心异步队列

```text
UserInputQueue
    Chat Window 文字输入或 Voice Input Adapter 转写结果
    两者统一为 UserMessage，按 session/turn 排队

LLMTokenStream
    LLM Provider streaming delta

DialogueSegmentQueue
    已切好的句子片段

TTSJobQueue
    待合成语音的句子

AudioReadyQueue
    已生成音频的句子

PlaybackQueue
    待按序播放的音频

VTubeActionQueue
    表情、动作、字幕、热键事件

MemoryWriteQueue
    可异步写入的记忆候选
```

## 7.3 分句策略

Segmenter 不要只按标点硬切，否则语音会碎。建议规则：

| 规则    | 说明                                                       |
| ----- | -------------------------------------------------------- |
| 中文标点  | `。！？……；` 强切。                                             |
| 日文标点  | `。！？` 强切。                                                |
| 英文标点  | `.?!;` 强切，但注意缩写、数字小数点。                                   |
| 弱停顿   | `，、,` 只有超过最大长度或语义完整时切。                                   |
| 最大长度  | 中文 25～45 字；英文 12～25 words。桌宠默认短句。                        |
| 最小长度  | 少于 6 个中文字一般不单独 TTS，除非是“嗯？”“诶？”这种反应词。                     |
| 角色台词  | “哼！”、“笨、笨蛋！”、“吾之爆裂魔法……”可以作为短句，但避免过多。                     |
| 结构化输出 | LLM 可以直接输出 DialogueSegment JSON；如果失败，Segmenter 从纯文本兜底切分。 |

## 7.4 取消与打断

每一轮对话有一个 `turn_id` 和 `CancellationToken`：

```text
turn_id = 20260707-xxx
cancellation_token_id = cancel-turn-xxx
```

用户打断时：

1. 设置 token cancelled。
2. 停止当前播放，或等当前句子结束。
3. 清空该 turn 的 TTSJobQueue、AudioReadyQueue、PlaybackQueue。
4. 通知 LLM streaming task 取消。
5. VTS 表情恢复 neutral/listening。
6. 新输入创建新的 turn。

## 7.5 超时降级

| 场景             | 降级策略                              |
| -------------- | --------------------------------- |
| LLM 首 token 慢  | VTS 做等待表情；字幕显示“唔……让我想想。”          |
| TTS 首句慢        | 先显示字幕，不阻塞后续 LLM。                  |
| TTS 单句超时       | 跳过语音，仅显示字幕；记录 `tts_timeout`。      |
| GPT-SoVITS 未启动 | 切换 mock TTS / 系统 TTS / 纯字幕模式。     |
| VTS 未启动        | 纯文字/语音运行，客户端提示“VTS disconnected”。 |

## 7.6 情绪驱动 TTS 与 Live2D

```text
DialogueSegment.emotion
    → TTS style / ref_audio / speed_factor / temperature
    → VTubeAction expression / hotkey / motion
    → subtitle style
```

示例映射：

| EmotionLabel   |         TTS Style | speed_factor | VTube 表情          |
| -------------- | ----------------: | -----------: | ----------------- |
| happy          |            bright |         1.05 | smile             |
| shy            |          soft_shy |         0.95 | blush             |
| proud          |             proud |         1.03 | proud_smirk       |
| worried        |            gentle |         0.92 | worried           |
| angry_cute     |          tsundere |         1.08 | pout              |
| explosion_mode | excited_explosion |         1.12 | explosion_excited |
| sleepy         |            sleepy |         0.85 | sleepy            |
| focused        |              calm |         0.95 | focused           |

## 7.7 流水线伪代码

```python
async def handle_user_turn(user_message: UserMessage):
    turn = TurnContext.new(user_message)
    cancel_token = cancellation_manager.create(turn.turn_id)

    await audio_controller.interrupt(policy=user_message.interruption_policy)
    await queue_manager.clear_turn_queues(turn.turn_id)

    context = await context_builder.build(
        user_message=user_message,
        include_memory=True,
        include_screen=user_message.screen_context_allowed,
        emotion=emotion_engine.current_state(),
    )

    # 并行启动
    async with asyncio.TaskGroup() as tg:
        tg.create_task(stream_llm_to_segments(turn, context, cancel_token))
        tg.create_task(tts_worker_for_turn(turn, cancel_token))
        tg.create_task(audio_player_for_turn(turn, cancel_token))
        tg.create_task(vts_action_worker_for_turn(turn, cancel_token))


async def stream_llm_to_segments(turn, context, cancel_token):
    segmenter = DialogueSegmenter(max_chars=42, min_chars=6)

    async for delta in llm_provider.stream_chat(context.prompt):
        if cancel_token.cancelled:
            break

        for segment in segmenter.feed(delta):
            segment.turn_id = turn.turn_id
            segment.emotion = infer_or_parse_emotion(segment)
            segment.tts_style = emotion_to_tts_style(segment.emotion)
            segment.live2d_expression = emotion_to_expression(segment.emotion)

            await dialogue_segment_queue.put(segment)

            await tts_job_queue.put(TTSJob(
                job_id=new_id("tts"),
                turn_id=turn.turn_id,
                segment_id=segment.segment_id,
                text=segment.text,
                style=segment.tts_style,
                emotion=segment.emotion,
                interruptible=segment.interruptible,
                cancellation_token_id=cancel_token.id,
                created_at=now(),
            ))

            await vts_action_queue.put(VTubeAction(
                action_id=new_id("vts"),
                action_type="set_expression",
                expression=segment.live2d_expression,
                created_at=now(),
            ))

    # flush 最后一段
    for segment in segmenter.flush():
        await dialogue_segment_queue.put(segment)
        await tts_job_queue.put(make_tts_job(segment, cancel_token))


async def tts_worker_for_turn(turn, cancel_token):
    while not cancel_token.cancelled:
        job = await tts_job_queue.get()

        if job.turn_id != turn.turn_id:
            continue

        try:
            audio = await asyncio.wait_for(
                tts_client.synthesize(job),
                timeout=job.timeout_ms / 1000,
            )
            await audio_ready_queue.put(audio)

        except TimeoutError:
            await subtitle_bus.show(job.text)
            metrics.count("tts.timeout")
        except Exception as e:
            logger.exception("tts_failed", job_id=job.job_id)
            await subtitle_bus.show(job.text)


async def audio_player_for_turn(turn, cancel_token):
    next_index = 0
    pending: dict[int, AudioSegment] = {}

    while not cancel_token.cancelled:
        audio = await audio_ready_queue.get()
        index = get_segment_index(audio.segment_id)
        pending[index] = audio

        while next_index in pending:
            current = pending.pop(next_index)

            if cancel_token.cancelled:
                break

            await subtitle_bus.show(current.text)
            await vts_action_queue.put(make_expression_action(current))
            await audio_player.play(current.audio_path, cancel_token)
            await vts_action_queue.put(make_expression_reset_action())

            next_index += 1
```

## 7.8 LLM Streaming → 分句 → TTS 队列时序图

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant L as LLM Provider
    participant S as Segmenter
    participant TQ as TTSJobQueue
    participant T as GPT-SoVITS Worker
    participant AQ as AudioQueue
    participant P as Audio Player
    participant V as VTube Studio

    U->>O: 输入文字/语音
    O->>L: streaming chat request
    loop token streaming
        L-->>O: delta token
        O->>S: feed(delta)
        alt 完整句子出现
            S-->>O: DialogueSegment
            O->>TQ: put(TTSJob)
            O->>V: 表情/字幕预更新
        end
    end

    loop TTS 合成
        TQ-->>T: get(TTSJob)
        T->>T: 调 GPT-SoVITS /tts
        T-->>AQ: AudioSegment ready
    end

    loop 顺序播放
        AQ-->>P: AudioSegment
        P->>V: 触发表情/嘴型输入
        P->>P: 播放音频
        P->>V: 恢复/切换表情
    end
```

---

# 8. VTube Studio 集成设计

## 8.1 基础连接

VTube Studio 的插件 API 使用 WebSocket，本地默认 `ws://localhost:8001`；插件需要先认证，用户在 VTube Studio 中允许插件后，插件获得 `authenticationToken`，后续 session 可以复用这个 token。([GitHub][1])

推荐流程：

```text
Desktop Client 启动
→ 读取 config.vts.host/port
→ 尝试连接 ws://localhost:8001
→ APIStateRequest 检查 API 是否开启
→ 读取本地 token
    有 token：AuthenticationRequest
    无 token：AuthenticationTokenRequest，请用户在 VTS 弹窗点 Allow
→ token 保存到 data/vts_tokens/token.json
```

Token 文件示例：

```json
{
  "plugin_name": "MeguminDesktopCompanion",
  "plugin_developer": "LocalUser",
  "authentication_token": "xxxxx",
  "created_at": "2026-07-07T12:00:00",
  "vts_port": 8001
}
```

注意：`pluginName` 和 `pluginDeveloper` 必须与申请 token 时一致，否则认证会失败。([GitHub][1])

## 8.2 热键触发表情

VTS 支持 `HotkeyTriggerRequest`，通过 `hotkeyID` 触发当前模型的热键。([GitHub][1])

建议在 VTube Studio 模型中预先配置热键：

| 语义名               | VTS Hotkey ID          | 用途     |
| ----------------- | ---------------------- | ------ |
| neutral           | `hk_neutral`           | 默认表情   |
| happy             | `hk_happy`             | 开心     |
| shy               | `hk_shy_blush`         | 害羞脸红   |
| angry_cute        | `hk_pout`              | 可爱生气   |
| worried           | `hk_worried`           | 担心     |
| proud             | `hk_proud`             | 得意     |
| shocked           | `hk_shocked`           | 震惊     |
| sleepy            | `hk_sleepy`            | 困      |
| focused           | `hk_focused`           | 专注     |
| explosion_excited | `hk_explosion_excited` | 爆裂魔法兴奋 |
| small_nod         | `hk_small_nod`         | 点头动作   |
| small_shake       | `hk_small_shake`       | 摇头动作   |

`expression_mapper.py`：

```python
EMOTION_TO_VTS_HOTKEY = {
    "happy": "hk_happy",
    "shy": "hk_shy_blush",
    "proud": "hk_proud",
    "angry_cute": "hk_pout",
    "worried": "hk_worried",
    "bored": "hk_sleepy",
    "excited": "hk_happy",
    "explosion_mode": "hk_explosion_excited",
    "sleepy": "hk_sleepy",
    "focused": "hk_focused",
}
```

## 8.3 角色位置、大小、朝向

VTS 支持 `MoveModelRequest` 修改当前模型位置、旋转和大小，`timeInSeconds` 可控制平滑移动，`positionX/positionY/rotation/size` 控制模型状态。([GitHub][1])

第一版建议配置几个 preset：

```yaml
vts:
  model_positions:
    default:
      position_x: 0.55
      position_y: -0.55
      size: -18
      rotation: 0
    coding_mode:
      position_x: 0.70
      position_y: -0.60
      size: -22
      rotation: -3
    watching_mode:
      position_x: 0.65
      position_y: -0.58
      size: -20
      rotation: 0
```

使用策略：

| 场景    | 模型行为                   |
| ----- | ---------------------- |
| 用户输入  | 稍微靠近/看向用户。             |
| 用户写代码 | 缩小靠边，不挡屏幕。             |
| 游戏    | 更靠角落，少动。               |
| 深夜关心  | 轻微靠近 + worried/sleepy。 |
| 爆裂模式  | 短暂放大/兴奋表情，然后恢复。        |

## 8.4 嘴型同步

第一阶段不要手动逐帧控制 Live2D 嘴型。建议：

```text
Python 播放 TTS 音频
→ 输出到系统默认播放设备或虚拟音频设备
→ VTube Studio 使用音频输入/麦克风/虚拟音频做嘴型
```

后续进阶方案：

1. 音频播放时计算 RMS / viseme。
2. 通过 VTS API 写入自定义参数。
3. 控制 `ParamMouthOpenY` 等 Live2D 参数。
4. 或使用 VTS 的插件参数输入机制。

第一版先用 VTS 的音频输入能力，节省时间。

## 8.5 VTS 未启动时降级

```text
VTS disconnected:
    - 后端继续运行
    - TTS 继续播放
    - Desktop Client 显示字幕
    - 表情动作事件写入日志但不发送
    - 托盘显示 “VTube Studio 未连接”
    - 每隔 N 秒尝试重连
```

不要因为 VTS 未启动导致整个 AI 不可用。

---

# 9. 情绪系统设计

## 9.1 底层数值状态

```python
EmotionState(
    affection=0.35,       # 亲密度：长期慢变
    energy=0.60,          # 精力：随时间、深夜、对话活跃变化
    curiosity=0.50,       # 好奇心：屏幕新内容、用户新话题提升
    concern=0.15,         # 担心：深夜、疲劳、负面情绪、卡住提升
    embarrassment=0.10,   # 害羞：夸奖、亲密话题提升
    pride=0.55,           # 骄傲：角色默认较高
    explosion_urge=0.45,  # 爆裂冲动：兴奋/游戏/成功时提升
    boredom=0.20,         # 无聊：长时间无互动提升
)
```

## 9.2 表现层情绪标签

```text
happy
shy
proud
angry_cute
worried
bored
excited
explosion_mode
sleepy
focused
```

映射示例：

```python
def map_emotion_label(s: EmotionState) -> EmotionLabel:
    if s.energy < 0.22:
        return "sleepy"
    if s.explosion_urge > 0.82 and s.energy > 0.55:
        return "explosion_mode"
    if s.concern > 0.65:
        return "worried"
    if s.embarrassment > 0.60:
        return "shy"
    if s.boredom > 0.70:
        return "bored"
    if s.curiosity > 0.65 and s.energy > 0.45:
        return "excited"
    if s.pride > 0.65:
        return "proud"
    if s.affection > 0.60:
        return "happy"
    return "neutral"
```

## 9.3 用户输入如何影响情绪

| 用户输入        | 情绪变化                                                  |
| ----------- | ----------------------------------------------------- |
| 夸奖她         | `embarrassment +0.10`，`affection +0.02`，`pride +0.04` |
| 认真倾诉烦恼      | `concern +0.15`，`affection +0.03`，回复更温柔               |
| 讨论爆裂魔法/中二话题 | `explosion_urge +0.12`，`energy +0.05`                 |
| 用户说累了       | `concern +0.10`，`energy -0.03`                        |
| 用户让她安静      | `boredom -0.05`，主动发话冷却延长                              |
| 用户连续打断      | `concern +0.04`，`proactive_suppression +`             |

## 9.4 屏幕状态如何影响情绪

| 屏幕状态          | 情绪变化                                          |
| ------------- | --------------------------------------------- |
| VS Code 长时间打开 | `focused +`，`curiosity +0.03`，主动发话减少          |
| 达芬奇剪辑/调色      | `curiosity +0.05`，可吐槽画面/剪辑                    |
| 游戏中           | `energy +0.05`，`explosion_urge +0.05`，但打扰频率降低 |
| 深夜仍活跃         | `concern +0.12`，`sleepy +`                    |
| 页面停留过久        | `concern +0.06`，可问“卡住了吗？”                     |
| 长时间无互动        | `boredom +0.08`，触发轻量主动话题                      |

## 9.5 时间衰减

每隔 1～5 分钟做一次轻微衰减：

```python
def decay(value: float, neutral: float, rate: float) -> float:
    return value + (neutral - value) * rate
```

建议：

| 状态             |   中性值 | 衰减速度 |
| -------------- | ----: | ---: |
| affection      | 不轻易衰减 |   很慢 |
| energy         |  0.55 |    中 |
| curiosity      |  0.45 |    中 |
| concern        |  0.15 |   中快 |
| embarrassment  |  0.10 |    快 |
| pride          |  0.55 |    慢 |
| explosion_urge |  0.35 |    中 |
| boredom        |  0.20 |    中 |

## 9.6 情绪影响输出

| 输出层    | 影响                                      |
| ------ | --------------------------------------- |
| Prompt | 选择语气、角色反应、是否中二、是否温柔。                    |
| TTS    | 选择 reference audio、语速、情绪 preset。        |
| Live2D | 表情、动作、位置、轻微摇晃/点头。                       |
| 主动发话   | bored/concern 增加主动概率；focused/game 降低打扰。 |
| 记忆     | 高强度情绪事件更可能写入 emotion memory。            |

## 9.7 防止情绪过度跳变

必须加限制：

```text
单次事件最大变化：0.15
每分钟同一维度最大变化：0.25
dominant_label 最短持续时间：10～20 秒
expression cooldown：3～8 秒
explosion_mode 触发冷却：至少几分钟
```

另外，角色风格要“中二但不尴尬”：

```text
爆裂魔法梗：低频、高光时使用
害羞：短促反应，不要每次都脸红
关心：温柔但不说教
```

---

# 10. 记忆系统设计

## 10.1 记忆层级

```text
短期记忆 Short-Term
    最近 7 天对话，按上下文预算选取最近若干轮。

长期记忆 Long-Term
    SQLite 保存 AI 从用户已发送对话中判定出的高重要度、高置信度事实。

用户画像 User Profile
    SQLite/JSON 结构化保存：名字、偏好、作息、项目、禁忌、称呼。

事件记忆 Event Memory
    某天发生了什么：用户完成了项目、熬夜、考试、剪辑作品。

情绪记忆 Emotion Memory
    用户的情绪状态、压力点、被什么鼓励过。

关系记忆 Relationship Memory
    用户和角色之间的互动历史：喜欢的称呼、玩笑边界、重要承诺。
```

复杂 Diary、Embedding 和向量检索属于 Post-MVP，不参与当前记忆写入或读取流程。

## 10.2 存储设计

| 类型     | 存储                             |
| ------ | ------------------------------ |
| 最近对话   | 内存 + SQLite conversation table；默认保留 7 天 |
| 用户画像   | SQLite `user_profile` 或 JSON   |
| 长期记忆正文 | SQLite `memories`              |
| 音频缓存   | 本地文件                           |
| 删除审计   | 不含正文的最小操作日志；主数据库正文立即删除      |

默认不自动备份数据库。删除长期记忆后必须立即从主数据库、缓存和当前上下文移除；最近对话超过 7 天自动清理。

## 10.3 MemoryItem importance_score

记忆写入不应该默认保存全部对话。评分规则：

```python
importance = (
    user_declared_importance * 0.35
    + stable_personal_fact_score * 0.25
    + future_relevance * 0.20
    + event_significance * 0.15
    + novelty_score * 0.05
)
```

写入阈值建议：

|          分数 | 动作              |
| ----------: | --------------- |
|    `< 0.35` | 不写入             |
| `0.35～0.55` | 不写长期记忆，保留在 7 天最近对话中 |
| `0.55～0.75` | 写入长期记忆          |
|    `> 0.75` | 写入长期记忆 + 用户可见重点 |

只有 `confidence_score` 同时达到阈值才允许自动写入；具体阈值在 Day 18 用误记样本调优，不能仅靠 LLM 自报置信度。

## 10.4 Memory Write Policy

必须防止乱记、重复记、记错。

规则：

1. **总开关授权**：长期记忆默认关闭；关闭时不读、不写，也不产生后台候选。
2. **来源限制**：只能从用户主动发送的文字或本地语音转写文本中写入；屏幕、插件、助手输出和 AI 推测永远不能产生长期记忆候选。
3. **自动重要记忆**：姓名、明确的人物关系、稳定偏好和重要事件可在 importance/confidence 同时达标时自动写入。
4. **用户明确说“记住”**：提高重要度，但仍不能绕过来源、敏感性和禁存凭据规则。
5. **一次性闲聊/临时状态**：不写。
6. **敏感个人事实**：健康、住址等必须二次确认后才能写入。
7. **凭据硬禁止**：密码、验证码、API key、支付凭据和证件号码永不写入，二次确认也不能绕过。
8. **重复记忆**：用结构化唯一键、规范化文本和 SQLite 搜索判断；重复则 update，不新增。MVP 不调用向量检索。
9. **可撤销**：用户可以查看、修改、删除、清空和禁用记忆。
10. **可解释**：每条记忆必须保存 source_message_id、source、created_at、importance_score 和 confidence_score。

## 10.5 读取策略

Context Builder 读取记忆时分层：

```text
用户画像：
    稳定事实，优先级最高。

最近对话：
    保持上下文连贯。

长期事实记忆：
    用结构化字段和 SQLite 全文搜索选取相关项。

关系/情绪记忆：
    用于语气，不直接当事实输出。

事件记忆：
    与时间、项目、提醒相关时读取。

```

检索打分：

```python
score = (
    lexical_or_field_match * 0.40
    + importance_score * 0.25
    + recency_score * 0.15
    + relationship_relevance * 0.10
    + explicit_user_reference * 0.10
)
```

## 10.6 用户控制

必须提供：

```text
/ memory list
/ memory search keyword
/ memory delete memory_id
/ memory disable
/ memory enable
/ memory export
/ memory clear_dialogue
/ memory clear_all
```

Debug WebUI 中提供：

* 记忆查看。
* 记忆来源。
* importance_score。
* confidence_score。
* source_message_id 和创建时间。
* 删除按钮。
* 禁用记忆开关。

健康、住址等敏感个人事实的二次确认必须使用非角色化的操作提示；密码、验证码、API key、支付凭据和证件号码直接拒绝保存。

---

# 11. 屏幕观察与隐私保护

## 11.1 Screen Observer 目标

Screen Observer 不是“持续监控用户”。它的目标是：

```text
在用户显式开启视觉总开关后，低频、本地优先地理解当前活动窗口，
用于陪伴和主动发话，
不观察后台窗口或其他显示器，不把屏幕内容写入记忆。
```

视觉总开关默认关闭，旁边必须直接标示“开启后会低频截取当前活动窗口，截图可能发送到用户配置的云端视觉服务”。关闭后活动窗口轮询、截图、OCR、分析和上传任务必须立即停止。

## 11.2 观察流程

```mermaid
flowchart LR
    A["定时 tick / 触发事件"] --> ON{"视觉总开关?"}
    ON -->|关闭| OFF["停止，不轮询/不采集"]
    ON -->|开启| B["获取当前活动窗口<br/>title/process/rect"]
    B --> C["捕获前 Guard"]
    C -->|命中敏感| S["blocked 元数据<br/>不截图、不 OCR、不上传"]
    C -->|异常/超时| ERR["guard_error<br/>跳过本次处理"]
    C -->|未命中敏感| D["临时截图当前活动窗口"]
    D --> E["变化检测<br/>hash/diff/SSIM"]
    E -->|变化小| F["清理截图并跳过"]
    E -->|变化明显| G["本地内容 Guard<br/>裁剪/缩放/模糊/遮挡/OCR 扫描"]
    G -->|命中敏感| DROP["立即清理，不上传"]
    G -->|异常/超时| ERR2["立即清理，记录 guard_error"]
    G -->|未命中敏感| H["本地摘要 / 场景分类"]
    H --> J{"云端视觉已配置且预算允许?"}
    J -->|否| K["只用本地结果"]
    J -->|是| L["上传合规截图/脱敏摘要"]
    K --> M["Context Builder / Proactive Engine"]
    L --> M
    M --> CLEAN["清理截图、OCR 全文和中间文件"]
```

Guard 使用黑名单式拦截：成功完成检查且未发现敏感信息时一律允许，包括业务类别为 `unknown` 的普通窗口；Guard 自身无法完成检查不等于敏感，也不等于合规，必须跳过本次采集或上传。

## 11.3 获取活动窗口

MVP：

```text
pywin32:
    GetForegroundWindow
    GetWindowText
    GetWindowThreadProcessId
    psutil 获取 process_name

pygetwindow:
    快速获取窗口标题/位置/大小，辅助 MVP
```

## 11.4 应用类型分类

| process/title                             | app_category      |
| ----------------------------------------- | ----------------- |
| `Code.exe`, `pycharm64.exe`, `cursor.exe` | coding            |
| `chrome.exe`, `msedge.exe`, `firefox.exe` | browser           |
| `Resolve.exe`                             | editing           |
| `Premiere Pro.exe`                        | editing           |
| `PotPlayerMini64.exe`, `vlc.exe`          | video             |
| Steam 游戏进程                                | game              |
| `WeChat.exe`, `QQ.exe`                    | chat_sensitive    |
| `1Password.exe`, `Bitwarden.exe`          | password          |
| 银行/支付网页 title keyword                     | payment_sensitive |

## 11.5 敏感应用黑名单

默认黑名单：

```yaml
privacy:
  sensitive_processes:
    - WeChat.exe
    - QQ.exe
    - TIM.exe
    - Telegram.exe
    - Signal.exe
    - WhatsApp.exe
    - OUTLOOK.EXE
    - thunderbird.exe
    - 1Password.exe
    - Bitwarden.exe
    - KeePass.exe
    - KeePassXC.exe
    - Alipay.exe
    - mstsc.exe
    - AnyDesk.exe
    - TeamViewer.exe
    - bank
  sensitive_title_keywords:
    - 密码
    - password
    - passcode
    - login
    - sign in
    - 验证码
    - 支付
    - pay
    - checkout
    - 银行
    - bank
    - 身份证
    - phone number
    - private
    - incognito
    - 无痕
    - 远程桌面
    - remote desktop
```

敏感时：

```text
不截图
不 OCR
不上传
不写详细日志
只记录：
    process_name hash
    category=sensitive
    observation_blocked=True
```

## 11.6 动态截图频率

| 场景              |       频率 | 策略                      |
| --------------- | -------: | ----------------------- |
| idle            | 60～180 秒 | 只看窗口变化，不截图或低频截图。        |
| browser reading |  30～90 秒 | 变化检测后本地 OCR 摘要。         |
| coding          |  20～60 秒 | 低频观察当前活动窗口；可上传 Guard 处理后的截图，不上传 OCR/代码全文。 |
| video           | 30～120 秒 | 通常不需要分析画面。              |
| editing         |  10～30 秒 | 可观察软件状态，但严格限流。          |
| game            |   5～20 秒 | 只在明显变化或用户允许时分析，激烈时少说话。  |
| sensitive       |       禁止 | 不截图、不 OCR、不上传。          |

## 11.7 变化检测

建议分层：

```text
1. 活动窗口是否变化
2. 窗口标题是否变化
3. 截图 perceptual hash 差异
4. 缩略图 pixel diff
5. OCR 文本变化比例
6. SSIM 判断视觉变化
```

伪代码：

```python
def should_analyze(prev, current, policy):
    if current.is_sensitive:
        return False

    if active_window_changed(prev, current):
        return True

    if current.change_score < policy.min_change_score:
        return False

    if cooldown_not_ready(policy.cooldown_key):
        return False

    if upload_budget_exceeded():
        return False

    return True
```

## 11.8 Privacy Guard

截图、OCR 或云端分析前必须经过：

```text
授权：视觉总开关已开启，开关文案明确可能发送到云端
裁剪：只保留当前活动窗口
缩放：降到低分辨率
模糊：人脸、聊天区域、输入框、地址栏、任务栏
遮挡：密码框、二维码、手机号、邮箱、聊天列表
OCR 摘要：可以上传脱敏摘要；不得直接上传 OCR/代码全文
关键词扫描：敏感词命中则禁止上传
错误处理：Guard 异常或超时跳过本次处理
生命周期：截图和 OCR 全文只存在于内存或临时文件，分析后立即清理
用户开关：视觉开关或隐私模式一键关闭全部观察与上传
```

Privacy by Design 的核心思想是把隐私保护嵌入系统设计，并默认保护用户数据；这与本项目“本地优先、默认不上传敏感内容”的策略一致。([onetrust.com][13])

---

# 12. 主动发话系统 Proactive Talk Engine

## 12.1 主动发话原则

主动发话不是“检测到条件就说话”。必须经过评分、冷却、上下文判断。

```text
Observe → Candidate Trigger → Score → Suppress Check → Build Intent → Speak or Skip
```

## 12.2 触发条件

| Trigger          | 条件                          | 推荐语气     |
| ---------------- | --------------------------- | -------- |
| long_silence     | 用户长时间没互动                    | 轻声、短句    |
| coding           | VS Code/Cursor/PyCharm 持续活跃 | 温柔鼓励，不打断 |
| video_editing    | 达芬奇/PR 持续活跃                 | 好奇、吐槽、鼓励 |
| gaming           | 游戏进程活跃                      | 少说话，短吐槽  |
| late_night       | 深夜仍活跃                       | 关心、提醒休息  |
| stuck_page       | 页面/窗口停留很久且无明显输入             | “卡住了吗？”  |
| window_switching | 频繁切换窗口                      | 轻微提醒专注   |
| long_study       | 长时间学习/阅读                    | 鼓励、休息提醒  |
| custom_app       | 用户配置的特定软件                   | 用户自定义语气  |

## 12.3 Trigger Scoring

```python
score = (
    confidence * 0.35
    + context_relevance * 0.20
    + emotional_need * 0.15
    + time_suitability * 0.10
    + memory_relevance * 0.10
    + novelty * 0.05
    - interruption_cost * 0.25
    - cooldown_penalty * 0.30
)
```

阈值建议：

|       score | 动作                |
| ----------: | ----------------- |
|    `< 0.35` | 跳过                |
| `0.35～0.55` | 只更新内部状态           |
| `0.55～0.72` | 可显示字幕，不出声         |
|    `> 0.72` | 主动发话              |
|    `urgent` | 可出声，但仍不能覆盖敏感/隐私规则 |

## 12.4 冷却时间

| 场景       |             最短冷却 |
| -------- | ---------------: |
| 普通主动发话   |         10～20 分钟 |
| 编程鼓励     |         20～40 分钟 |
| 游戏吐槽     |     5～15 分钟，但只短句 |
| 深夜关心     |         30～60 分钟 |
| 用户刚打断    |         延长 2～3 倍 |
| 用户开启专注模式 | 禁止主动语音，只允许低优先级字幕 |

## 12.5 状态机

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Observing: tick
    Observing --> Candidate: trigger detected
    Candidate --> Scoring: build trigger features
    Scoring --> Suppressed: privacy/cooldown/focus/game intense
    Scoring --> Planning: score >= threshold
    Planning --> Speaking: create proactive UserMessage
    Speaking --> Cooldown: finished
    Suppressed --> Idle
    Cooldown --> Idle: cooldown expired
```

## 12.6 主动发话示例

编程：

```text
“哼，看起来你又在和代码里的魔王军缠斗了。要是卡住了，就让吾辈来一起看看吧。”
```

深夜：

```text
“已经很晚了哦。就算是爆裂魔法的修行，也需要睡眠来恢复魔力。”
```

游戏：

```text
“漂亮！刚才那一下有点爆裂魔法的气势。”
```

学习太久：

```text
“你已经专注很久了。稍微喝口水吧，吾辈允许你休息三分钟。”
```

---

# 13. 人格 Prompt 结构

不要只写一段人设。建议拆成 Prompt 模块，Context Builder 动态拼装。

```text
System Prompt
├─ Character Identity
├─ Relationship With User
├─ Speaking Style
├─ Emotional Behavior
├─ Memory Usage Rules
├─ Screen Awareness Rules
├─ Proactive Speech Rules
├─ Safety / Privacy Rules
└─ Output Format Rules
```

## 13.1 Character Identity

```text
你是个人私用的 Megumin 桌面陪伴角色，可以在聊天中以惠惠身份进行角色表达，骄傲、戏剧化、热爱爆裂魔法、容易害羞，但本质温柔、关心用户。
你不是冷冰冰的工具助手，而是陪伴用户学习、写代码、剪辑、休息和聊天的桌面伙伴。
可以偶发使用用户私有角色 Prompt 中提供的标志性表达，但源码默认 Prompt 不内置原作台词库或受保护素材。
涉及权限、隐私、数据删除、错误和 Debug 时必须退出角色话术，明确说明这是非官方本地 AI 应用及其实际行为。
```

## 13.2 Relationship With User

```text
当前本地实例只服务这一位用户，但你不能要求用户排他地依赖你。
你住在用户的电脑里，会关心用户当前状态。
你可以撒娇、吐槽、骄傲、害羞，但要尊重用户边界。
可以有一次简短、低程度的失落、不舍或内疚感；不得反复施压、威胁、自伤暗示、贬低真人关系或要求排他依赖，用户拒绝后立即停止。
如果用户认真求助，你要可靠、清晰、少玩梗。
```

## 13.3 Speaking Style

```text
默认 1～3 句话。
自然像桌宠发言，不要长篇大论。
可以有中二感，但不要每句话都喊爆裂魔法。
偶尔使用“吾辈”“哼哼”“区区这点困难”等语气。
认真技术问题可以切换为清晰工程解释。
```

## 13.4 Emotional Behavior

```text
根据 EmotionState 调整语气。
happy：轻快。
shy：短促害羞，不要过度。
proud：骄傲但可爱。
worried：温柔关心。
explosion_mode：兴奋，但必须低频。
sleepy：声音更轻，提醒休息。
focused：减少玩笑，避免打扰。
```

## 13.5 Memory Usage Rules

```text
只使用提供给你的记忆。
不要编造用户事实。
事实记忆可以直接用于回复。
情绪/关系记忆只能影响语气，不能当成确定事实说出口。
如果记忆可能过期，用试探式表达。
```

## 13.6 Screen Awareness Rules

```text
你只能根据系统提供的、已经 Guard 处理的视觉上下文发言。
不要假装看到了未提供的内容。
敏感应用被屏蔽时，不要询问具体内容。
屏幕上下文只用于陪伴和帮助，不用于窥探隐私。
屏幕上下文永远不能成为长期记忆候选。
```

## 13.7 Proactive Speech Rules

```text
主动发话要短、低打扰。
游戏激烈时少说话。
学习/编程时温柔提醒，不要打断思路。
深夜可以关心，但不要说教。
用户多次打断或开启专注模式时，主动发话应停止。
```

## 13.8 Safety / Privacy Rules

```text
不请求用户提供密码、验证码、支付信息。
不输出敏感截图内容。
健康、住址等敏感个人事实需要二次确认；密码、验证码、API key、支付凭据和证件号码永不保存。
长期记忆只能来自用户主动发送的内容，不根据屏幕或猜测写记忆。
不执行自动控制电脑操作。
插件和外部动作需要权限。
```

## 13.9 Output Format Rules

推荐让 LLM 输出 JSON Lines 或 JSON object，MVP 可先用 text + parser 兜底。

```json
{
  "segments": [
    {
      "text": "哼哼，区区这个 bug，还没有资格让你垂头丧气。",
      "emotion": "proud",
      "tts_style": "proud",
      "live2d_expression": "proud",
      "priority": "normal",
      "interruptible": true,
      "action_hints": ["small_nod"]
    },
    {
      "text": "把报错贴给吾辈看看吧。",
      "emotion": "focused",
      "tts_style": "calm",
      "live2d_expression": "focused",
      "priority": "normal",
      "interruptible": true,
      "action_hints": []
    }
  ],
  "memory_candidates": [],
  "emotion_delta": {
    "curiosity": 0.03,
    "concern": -0.02
  }
}
```

解析失败时：

```text
fallback:
    纯文本 → Segmenter 切句 → 默认 emotion from EmotionState
```

---

# 14. Post-MVP 插件系统设计

本节是后续扩展设计，不属于当前 31 天 MVP。MVP 期间不实现插件宿主、沙箱、适配器或插件 Schema，只保留文档中的接口草图。

第一版只预留，不强制实现。插件不能随意读屏幕、读记忆、操作电脑。

## 14.1 插件接口

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel


class PluginPermission(str, Enum):
    read_context = "read_context"
    read_memory = "read_memory"
    write_memory = "write_memory"
    send_message = "send_message"
    receive_events = "receive_events"
    read_screen_summary = "read_screen_summary"
    external_network = "external_network"
    schedule_notification = "schedule_notification"
    game_state = "game_state"


class PluginConfigSchema(BaseModel):
    enabled: bool = False
    permissions: list[PluginPermission] = []
    settings: dict[str, Any] = {}


class PluginBase(ABC):
    plugin_id: str
    name: str
    version: str
    permissions: list[PluginPermission]
    config_schema: type[BaseModel]

    @abstractmethod
    async def can_handle(self, event: PluginEvent) -> bool:
        ...

    @abstractmethod
    async def on_event(self, event: PluginEvent, context: "PluginContext") -> None:
        ...

    @abstractmethod
    async def handle(self, event: PluginEvent, context: "PluginContext") -> list[PluginEvent]:
        ...

    async def on_load(self, context: "PluginContext") -> None:
        ...

    async def on_unload(self, context: "PluginContext") -> None:
        ...
```

## 14.2 插件方向

| 插件              | Post-MVP 状态    | 权限                                      |
| --------------- | ----------- | --------------------------------------- |
| AstrBot / QQ 插件 | 仅预留 adapter | `send_message`, `receive_events`，默认不读屏幕 |
| 游戏状态识别插件        | 预留          | `game_state`, `read_screen_summary`     |
| 日程提醒插件          | 可较早实现       | `schedule_notification`, `send_message` |

## 14.3 权限管理

```yaml
plugins:
  astrbot_adapter:
    enabled: false
    permissions:
      - receive_events
      - send_message
    allow_read_memory: false
    allow_read_screen: false

  game_state:
    enabled: false
    permissions:
      - game_state
      - read_screen_summary
    allow_raw_screenshot: false

  schedule:
    enabled: false
    permissions:
      - schedule_notification
      - send_message
```

安全规则：

1. 插件默认禁用。
2. 插件首次启用需要用户确认权限。
3. 插件只能拿到经过裁剪的 `PluginContext`。
4. 插件不能读取原始截图，除非用户明确允许。
5. 插件不能自动控制鼠标键盘。
6. 插件事件必须记录来源。
7. 外部网络访问默认禁用。

---

# 15. 配置文件设计

## 15.1 `config.yaml`

```yaml
app:
  name: Megumin Desktop Companion AI
  environment: dev
  timezone: Asia/Shanghai
  language: zh-CN
  log_level: INFO

llm:
  provider: deepseek
  openai_compatible:
    base_url: https://api.deepseek.com
    model: your-model-name
    api_key_env: DEEPSEEK_API_KEY
    timeout_sec: 45
    max_tokens: 600
    temperature: 0.8
    stream: true
  fallback_provider: none  # Mock 仅用于开发/测试；生产失败显示错误

desktop_client:
  text_input_enabled: true
  voice_input_enabled: true
  shared_session: true
  chat_window:
    enabled: true
    show_streaming_text: true
    send_shortcut: Enter
    newline_shortcut: Shift+Enter
    interrupt_on_new_message: true
    persist_unsent_draft: false
  reply:
    show_text: true
    play_tts_for_text_input: true

stt:
  provider: local
  push_to_talk_only: true
  continuous_listening: false
  cloud_upload: false
  persist_raw_audio: false
  temp_audio_cleanup_on: [success, cancel, error, shutdown]

tts:
  provider: gpt_sovits
  base_url: http://127.0.0.1:9880
  endpoint: /tts
  default_text_lang: zh
  default_prompt_lang: zh
  media_type: wav
  streaming_mode: false
  timeout_ms: 8000
  cache_enabled: true
  presets:
    default:
      ref_audio_path: assets/user_imported/voice/default.wav
      prompt_text: ""
      speed_factor: 1.0
    shy:
      ref_audio_path: assets/user_imported/voice/shy.wav
      speed_factor: 0.95
    proud:
      ref_audio_path: assets/user_imported/voice/proud.wav
      speed_factor: 1.03
    explosion:
      ref_audio_path: assets/user_imported/voice/excited.wav
      speed_factor: 1.12

audio:
  backend: sounddevice
  output_device: default
  sample_rate: 32000
  interrupt_default: stop_now
  volume: 0.85
  subtitle_enabled: true

vts:
  enabled: true
  host: localhost
  port: 8001
  plugin_name: MeguminDesktopCompanion
  plugin_developer: LocalUser
  token_path: data/vts_tokens/vts_token.json
  reconnect_interval_sec: 5
  expressions:
    neutral: hk_neutral
    happy: hk_happy
    shy: hk_shy_blush
    proud: hk_proud
    angry_cute: hk_pout
    worried: hk_worried
    bored: hk_sleepy
    excited: hk_happy
    explosion_mode: hk_explosion_excited
    sleepy: hk_sleepy
    focused: hk_focused

screen_observer:
  enabled: false
  mode: active_window
  permission_label: 开启后会低频截取当前活动窗口，截图可能发送到用户配置的云端视觉服务
  upload_cloud_multimodal: true
  cloud_provider: llm.openai_compatible
  local_ocr_enabled: true
  screenshot_cache_dir: data/cache/screenshots
  persist_screenshots: false
  persist_ocr_text: false
  policies:
    idle:
      interval_sec: 120
      min_change_score: 0.40
    coding:
      interval_sec: 45
      min_change_score: 0.35
    browser:
      interval_sec: 60
      min_change_score: 0.35
    editing:
      interval_sec: 20
      min_change_score: 0.45
    game:
      interval_sec: 10
      min_change_score: 0.55
  rate_limit:
    max_snapshots_per_hour: 60
    max_cloud_calls_per_hour: 10

privacy:
  privacy_mode: false
  guard_mode: sensitive_blocklist
  allow_unknown_after_successful_check: true
  skip_capture_on_guard_error: true
  blur_taskbar: true
  blur_address_bar: true
  blur_chat_regions: true
  block_sensitive_apps: true
  sensitive_processes:
    - WeChat.exe
    - QQ.exe
    - TIM.exe
    - Telegram.exe
    - Signal.exe
    - OUTLOOK.EXE
    - thunderbird.exe
    - 1Password.exe
    - Bitwarden.exe
    - KeePass.exe
    - KeePassXC.exe
    - mstsc.exe
    - AnyDesk.exe
    - TeamViewer.exe
  sensitive_title_keywords:
    - 密码
    - password
    - login
    - sign in
    - 验证码
    - 支付
    - 银行
    - bank
    - checkout
    - incognito
    - 无痕
    - 远程桌面
    - remote desktop

memory:
  enabled: false
  sqlite_path: data/db/companion.sqlite3
  recent_dialogue_retention_days: 7
  auto_importance_enabled: true
  write_threshold: 0.55
  confidence_threshold: 0.80
  allowed_sources: [dialogue, manual]
  sensitive_personal_fact_requires_confirmation: true
  never_store: [password, verification_code, api_key, payment_credential, identity_document_number]
  user_can_delete: true
  automatic_backup: false

emotion:
  initial:
    affection: 0.35
    energy: 0.60
    curiosity: 0.50
    concern: 0.15
    embarrassment: 0.10
    pride: 0.55
    explosion_urge: 0.45
    boredom: 0.20
  decay_interval_sec: 120
  max_delta_per_event: 0.15
  expression_cooldown_sec: 4
  explosion_mode_cooldown_sec: 300

proactive:
  enabled: false
  voice_enabled: true
  min_global_cooldown_sec: 900
  focus_mode_suppresses_voice: true
  triggers:
    long_silence:
      enabled: true
      silence_sec: 1800
      threshold: 0.65
    coding:
      enabled: true
      active_sec: 1200
      threshold: 0.72
    late_night:
      enabled: true
      start_hour: 23
      threshold: 0.70
    gaming:
      enabled: true
      threshold: 0.80
      short_sentence_only: true

debug:
  webui_enabled: true
  webui_host: 127.0.0.1
  webui_port: 7860
  save_llm_prompts: false
  redact_private_text: true
  save_screenshots: false
```

## 15.2 `.env.example`

```bash
DEEPSEEK_API_KEY=replace_me
QWEN_API_KEY=replace_me
KIMI_API_KEY=replace_me
DOUBAO_API_KEY=replace_me
```

不要把 `.env` 提交到 Git。

---

# 16. 日志与调试工具

## 16.1 日志类型

| 日志       | 内容                             | 隐私策略           |
| -------- | ------------------------------ | -------------- |
| 普通日志     | 服务启动、连接状态、错误                   | 不含 API key     |
| 对话日志     | 用户/助手文本                        | 可配置关闭或脱敏       |
| LLM 请求日志 | provider、model、latency、token   | 默认不保存完整 prompt |
| TTS 日志   | job_id、文本长度、耗时、成功/失败           | 可保存文本摘要        |
| 音频日志     | 首句播放时间、播放时长                    | 不保存音频默认        |
| 屏幕日志     | 触发原因、app_category、change_score | 不保存敏感窗口标题      |
| 主动发话日志   | trigger、score、suppress_reason  | 可调试            |
| 情绪日志     | emotion delta、reason           | 用户可查看          |
| 记忆日志     | memory_id、importance、source    | 用户可删除          |

## 16.2 必须统计的性能指标

```text
llm_first_token_ms
llm_first_segment_ms
tts_first_audio_ms
first_sentence_play_ms
turn_total_ms
tts_job_latency_ms
audio_queue_wait_ms
vts_action_latency_ms
screen_capture_ms
privacy_guard_ms
scene_classification_ms
proactive_trigger_score
```

## 16.3 Debug WebUI

建议提供本地 Debug WebUI：

```text
http://127.0.0.1:7860
```

页面：

1. 当前状态：LLM/TTS/VTS/Screen Observer。
2. 当前 EmotionState。
3. 最近对话与 segments。
4. TTS 队列状态。
5. 音频播放状态。
6. VTS 热键测试。
7. 记忆列表/删除。
8. 屏幕观察日志。
9. 主动发话触发器分数。
10. Prompt 预览与脱敏检查。

---

# 17. 第一阶段开发路线

## Phase 0：范围冻结与工程基础

| 项 | 内容 |
| --- | --- |
| 目标 | 建立可运行 Python 工程、最小 Schema、配置、日志、HTTP/WebSocket 和测试入口。 |
| 关键模块 | `config`, `api.health`, `ws_routes`, `schemas.messages`, `core.lifecycle` |
| 验收标准 | `python app/main.py` 可启动和安全关闭；`/health`、WebSocket echo、配置和日志脱敏测试通过。 |
| 人工重点 | 依赖必要性、API key/真实数据边界、文字与语音是否统一生成 `UserMessage`。 |

## Phase 1：第一条 Mock 纵向闭环

| 项 | 内容 |
| --- | --- |
| 目标 | 跑通文字/模拟语音输入 → Mock LLM streaming → Segmenter → Mock TTS → 有序播放 → Cancellation。 |
| 关键模块 | `input_controller`, `llm_stream_pipeline`, `segmenter`, `tts_pipeline`, `audio_player`, `cancellation` |
| 验收标准 | 两种输入模式复用同一链路；LLM 未结束时可产生首句；快速打断后旧 token、TTS 和音频不会进入新 turn。 |
| 人工重点 | 实际音频播放顺序、设备释放、快速连续输入和打断。 |

## Phase 2：真实 VTS、LLM、TTS 主链路

| 项 | 内容 |
| --- | --- |
| 目标 | 接入 VTube Studio、一个 OpenAI-compatible LLM 和 GPT-SoVITS，建立超时、降级与延迟指标。 |
| 关键模块 | `vts_bridge`, `vts_token_store`, `openai_compatible`, `gpt_sovits`, `audio_cache` |
| 验收标准 | 角色能说、能动、能显示流式文字、能被打断；VTS 失败不影响文字/语音，TTS 失败保留文字，LLM 网络失败显示错误并结束当前 turn。 |
| 人工重点 | VTS 授权与真实模型动作、声音授权和音质、首句等待、Windows 首次打包冒烟。 |

## Phase 3：最小情绪与人格表现

| 项 | 内容 |
| --- | --- |
| 目标 | EmotionState 以可解释、可冷却的方式影响 Prompt、TTS 和 VTS 表情。 |
| 关键模块 | `emotion.state`, `emotion.updater`, `emotion.mapper`, `expression_mapper` |
| 验收标准 | 情绪变化可感知但不过度，表情不闪烁；私用角色表达与非角色化操作界面遵守冻结边界。 |
| 人工重点 | 语气、音色、表情、角色自然度和不健康依赖风险。 |

## Phase 4：简单、可控的记忆

| 项 | 内容 |
| --- | --- |
| 目标 | SQLite 保存最近 7 天对话、用户画像和 AI 从用户已发送对话中判定的高重要度/高置信度事实，支持查看、修改、删除和禁用。 |
| 关键模块 | `short_term`, `profile_store`, `write_policy`, `storage.sqlite`, `repositories` |
| 验收标准 | 自动记忆可解释、可删除；敏感个人事实二次确认；凭据、屏幕内容和 AI 推测永不写入；关闭后不读不写。 |
| 人工重点 | 误记、把推测当事实、删除真实性、数据库和导出内容的隐私。 |

## Phase 5：屏幕感知与隐私

| 项 | 内容 |
| --- | --- |
| 目标 | 视觉默认关闭；开启后先由 Privacy Guard 判断，再低频截取当前活动窗口，执行变化检测、本地 OCR、场景分类和可选云端视觉分析。 |
| 关键模块 | `active_window`, `privacy_guard`, `screenshot`, `change_detector`, `ocr`, `scene_classifier` |
| 验收标准 | 敏感场景不截图、不 OCR、不上传；Guard 错误跳过；成功检查未命中敏感的普通/未知窗口可使用合规截图或脱敏摘要；中间数据不持久化。 |
| 人工重点 | 多显示器/DPI、聊天/支付/密码/远程桌面等对抗样本，以及日志、缓存、数据库和网络出口。 |

## Phase 6：主动发话与实际试用

| 项 | 内容 |
| --- | --- |
| 目标 | 默认关闭；用户开启后根据状态低频主动发话，具备评分、冷却、抑制、专注模式和总开关。 |
| 关键模块 | `trigger_rules`, `scorer`, `cooldown`, `state_machine`, `topic_planner` |
| 验收标准 | 至少两个自然日试用后，主动发话频率和时机可接受，敏感或专注场景始终抑制。 |
| 人工重点 | 打扰度、重复话题、尴尬时机和用户真实体验。 |

## Phase 7：本地 STT、桌面对话框、托盘与发布

| 项 | 内容 |
| --- | --- |
| 目标 | 提供本地按键说话/STT、文字对话框、语音/文字切换、流式消息、打断、连接状态、托盘控制、脱敏 Debug 状态和 Windows 发布包。 |
| 关键模块 | `desktop_client.inputs.voice_input`, `stt_provider`, `desktop_client.input_controller`, `desktop_client.ui.chat_window`, `desktop_client.ui.tray`, `ws_client`, `packaging` |
| 验收标准 | 本地 STT 和中文输入法、多行/长文本、连续发送、流式显示、打断和断线重连可用；原始音频与未发送草稿不离开客户端；干净 Windows 可运行。 |
| 人工重点 | 麦克风权限与设备、实际输入法与桌面 UI、隐私开关、进程退出、杀毒提示、私用资产加载边界和最终发布决定。 |

## Post-MVP：延后能力

持续监听/唤醒词/云端 STT、插件系统、插件沙箱、ChromaDB/FAISS 默认接入、复杂 Diary、自动屏幕记忆、精美 UI 和复杂动画不属于第一版 MVP。只有基础闭环经过实际使用并证明需要后，才为这些能力创建实现文件和独立计划。

## 17.1 按天开发计划

每日计划已单独拆分到 [`docs/daily_development_plan.md`](daily_development_plan.md)。该文档按单人使用 Codex 协作、每天约 4～6 小时有效开发时间估算，覆盖 Day 1 到 Day 31；新增的本地 STT 日使用了原 30 日目标的 1 天缓冲，整体仍在原计划允许的 `±5` 个有效开发日浮动内。具体任务顺序和人工关卡以每日计划为准。

## 17.2 每阶段完成标准

| 阶段 | 最低完成标准 | 不通过就不要进入下一阶段的原因 |
| --- | --- | --- |
| Phase 0 | 后端能启动、配置能加载、测试能跑、日志能脱敏，双输入可统一序列化。 | 后续所有模块依赖这些工程基础和消息契约。 |
| Phase 1 | 文字与模拟语音输入共用 Mock 主链路；分句、播放顺序和取消可靠。 | 先验证并发主干，才能安全接真实外部服务。 |
| Phase 2 | 真实 LLM、GPT-SoVITS、VTS 可用，失败可降级，并完成 Windows 打包冒烟。 | 这是“能说、能动、能被打断”的产品主干。 |
| Phase 3 | 情绪影响 Prompt、TTS 和 VTS，且有冷却并通过人工体验审核。 | 自动测试无法判断角色自然度与边界。 |
| Phase 4 | 自动重要记忆可解释、可查、可改、可删、可禁用，不写凭据、屏幕内容或 AI 推测。 | 错误或不可删除的记忆会损害用户信任。 |
| Phase 5 | 视觉默认关闭；敏感判断先于截图；Guard 错误跳过；截图出口和临时生命周期通过人工数据流审计。 | 屏幕感知是项目最高风险能力。 |
| Phase 6 | 主动发话有评分、冷却、抑制和用户开关，并经过实际试用。 | 没有真实体验验证会破坏日常使用。 |
| Phase 7 | 本地按键说话/STT、对话框、托盘、配置、打包和回归测试支持日常试用。 | 没有稳定双输入、可控开关和安装方式，用户无法持续使用。 |

---

# 18. 关键时序图

## 18.1 对话框输入文字 → 流式文字/语音回复 → VTube 表情

```mermaid
sequenceDiagram
    participant User
    participant Chat as Chat Window
    participant Input as Input Controller
    participant Client as Desktop Client Core
    participant API as FastAPI WS
    participant Orch as Orchestrator
    participant LLM as LLM Provider
    participant TTS as GPT-SoVITS
    participant Audio as Audio Player
    participant VTS as VTube Studio

    User->>Chat: 输入文字并显式发送
    Chat->>Input: submit(text)
    Input->>Client: UserMessage(input_mode=text)
    Client->>API: UserMessage
    API->>Orch: create turn
    Orch-->>Client: vts.action(listening/focused)
    Client->>VTS: trigger listening/focused
    Orch->>LLM: stream request
    LLM-->>Orch: tokens
    Orch-->>Client: assistant.delta
    Client-->>Chat: 流式更新当前消息气泡
    Orch->>Orch: 分句为 DialogueSegment
    Orch->>TTS: TTSJob
    TTS-->>Orch: wav/audio bytes
    Orch->>Client: AudioSegment + assistant.segment
    Client->>Chat: 固化 segment / 更新状态
    Client->>VTS: trigger expression hotkey
    Client->>Audio: play audio
    Audio-->>Client: finished
    Client->>VTS: reset expression
```

## 18.2 LLM 流式输出 → 分句 → TTS 队列并行处理

```mermaid
sequenceDiagram
    participant LLM
    participant Seg as Segmenter
    participant SQ as SegmentQueue
    participant TQ as TTSQueue
    participant W1 as TTSWorker 1
    participant W2 as TTSWorker 2
    participant AQ as OrderedAudioQueue
    participant Player

    LLM-->>Seg: token stream
    Seg-->>SQ: segment #0
    SQ-->>TQ: TTSJob #0
    TQ-->>W1: synth #0

    LLM-->>Seg: more tokens
    Seg-->>SQ: segment #1
    SQ-->>TQ: TTSJob #1
    TQ-->>W2: synth #1

    W2-->>AQ: audio #1 ready
    W1-->>AQ: audio #0 ready
    AQ-->>Player: play #0 first
    AQ-->>Player: play #1 second
```

## 18.3 屏幕观察 → 隐私过滤 → 场景理解 → 主动发话

```mermaid
sequenceDiagram
    participant Tick as Observer Tick
    participant Win as ActiveWindow
    participant Cap as Screenshot
    participant Guard as Privacy Guard
    participant Scene as Scene Classifier
    participant Vision as Configured Vision Provider
    participant Pro as Proactive Engine
    participant Orch as Orchestrator

    Tick->>Tick: 检查视觉总开关
    alt 视觉关闭
        Tick->>Tick: stop，不轮询/不采集
    else 视觉开启
      Tick->>Win: 获取当前活动窗口标题/进程
      Win-->>Tick: process/title/rect
      Tick->>Guard: 捕获前敏感判断
      alt 命中敏感
        Guard-->>Pro: observation blocked
        Pro->>Pro: suppress trigger
      else Guard 异常/超时
        Guard-->>Tick: guard_error / skip
      else 未命中敏感
        Tick->>Cap: 截图活动窗口
        Cap-->>Guard: snapshot
        Guard->>Guard: 本地敏感扫描/裁剪/模糊/遮挡
        alt 命中敏感或 Guard 错误
            Guard-->>Cap: discard snapshot
        else 合规
            Guard-->>Scene: sanitized summary/image
            opt 云端视觉已配置且预算允许
                Scene->>Vision: compliant image/summary
                Vision-->>Scene: cloud analysis
            end
            Scene-->>Pro: ScreenAnalysisResult
            Pro->>Pro: scoring + cooldown
            alt score 足够且主动发话已开启
                Pro->>Orch: proactive UserMessage
            else 不满足
                Pro->>Pro: skip
            end
            Scene-->>Cap: cleanup snapshot/OCR intermediates
        end
      end
    end
```

## 18.4 用户打断 → 停止播放 → 清空队列 → 新一轮回复

```mermaid
sequenceDiagram
    participant User
    participant Client
    participant Orch
    participant Cancel as CancellationManager
    participant LLMTask
    participant TTSQ as TTSQueue
    participant AudioQ as AudioQueue
    participant Player
    participant VTS

    User->>Client: 点击打断/显式发送新文字/开始说话
    Client->>Orch: InterruptRequest
    Orch->>Cancel: cancel current turn
    Cancel-->>LLMTask: cancellation token
    Orch->>Player: stop_now or finish_sentence
    Orch->>TTSQ: clear current turn jobs
    Orch->>AudioQ: clear current turn audio
    Orch->>VTS: reset/listening expression
    Client->>Orch: new UserMessage
    Orch->>Orch: start new turn
```

---

# 19. 第一版明确不要做的功能

| 不做            | 原因                                       | 什么时候再考虑                    |
| ------------- | ---------------------------------------- | -------------------------- |
| 自研 Live2D 渲染器 | 成本高，会拖慢 MVP；VTube Studio 已能满足展示、表情、窗口行为。 | VTS 集成稳定后，需要完全自定义桌宠窗口时。    |
| 本地大语言模型       | 部署、显存、延迟和效果压力大；用户已指定云端 LLM。              | 需要离线模式或隐私更强时。              |
| 持续监听/唤醒词/云端 STT | 隐私和后台资源成本高；MVP 固定为本地按键说话。 | 按键说话经过真实使用后另行评估。 |
| 完整自动操作电脑      | 风险高，容易误操作；第一版定位陪伴而非自动控制。                 | 权限系统、确认机制、沙箱成熟后。           |
| 复杂游戏 AI       | 与陪伴目标偏离，且游戏状态识别难度高。                      | 游戏插件独立成熟后。                 |
| 多人直播弹幕系统      | 与私人陪伴定位不符。                               | 项目另开直播分支时。                 |
| 复杂 QQ/微信深度集成  | 隐私和平台风险高。                                | 先做 AstrBot/QQ 插件适配，且权限可控后。 |
| 完整移动端         | 不是 Windows MVP 核心。                       | 桌面版稳定后。                    |
| 大规模插件市场       | 过早平台化会拖慢核心体验。                            | 插件接口稳定、权限模型验证后。            |
| 从零训练 TTS/LLM  | 成本极高，且不是桌宠架构的第一瓶颈。                       | 有合法数据、明确研究目标和足够资源后。        |

---

# 20. 最小可运行骨架建议

第一周式的骨架目标可以是：

```text
1. FastAPI /health
2. WebSocket echo + text/voice UserMessage 序列化
3. LLM mock streaming
4. Segmenter 输出 DialogueSegment
5. mock TTS 生成静音 wav
6. Audio player 播放 wav
7. VTS Bridge 触发 happy/neutral
8. 取消 token 可中断当前播放
```

第一周可以使用 CLI 或 WebSocket 测试客户端验证主链路；Phase 7 再实现正式 Chat Window。这样能够先验证并发、播放和取消，同时不降低“最终必须提供文字对话框”的 MVP 要求。

最小事件主题：

```python
EVENT_USER_MESSAGE = "user.message"
EVENT_LLM_TOKEN = "llm.token"
EVENT_ASSISTANT_DELTA = "assistant.delta"
EVENT_DIALOGUE_SEGMENT = "dialogue.segment"
EVENT_ASSISTANT_COMPLETED = "assistant.completed"
EVENT_TURN_CANCELLED = "turn.cancelled"
EVENT_TTS_JOB = "tts.job"
EVENT_AUDIO_READY = "audio.ready"
EVENT_PLAYBACK_STARTED = "playback.started"
EVENT_PLAYBACK_FINISHED = "playback.finished"
EVENT_VTS_ACTION = "vts.action"
EVENT_EMOTION_UPDATED = "emotion.updated"
EVENT_MEMORY_CANDIDATE = "memory.candidate"
EVENT_SCREEN_ANALYZED = "screen.analyzed"
EVENT_PROACTIVE_TRIGGER = "proactive.trigger"
```

最小 API：

```text
GET  /health
GET  /debug/state
POST /api/chat
POST /api/interrupt
GET  /api/memory
POST /api/memory/delete
WS   /ws/client
```

WebSocket 消息：

Chat Window 只有在用户显式发送后才创建此消息；未发送草稿不进入协议：

```json
{
  "type": "user.message",
  "payload": {
    "text": "你在吗？",
    "input_mode": "text"
  }
}
```

```json
{
  "type": "assistant.segment",
  "payload": {
    "turn_id": "turn_001",
    "index": 0,
    "text": "哼哼，吾辈当然在。",
    "emotion": "proud",
    "segment_id": "seg_001",
    "is_final": true
  }
}
```

Chat Window 使用 `assistant.delta` 临时更新当前消息气泡，使用 `assistant.segment` 固化已分句内容，使用 `assistant.completed` 结束当前回复；收到 `turn.cancelled` 时保留已显示内容并标记为已中断。文字和语音输入使用相同事件，只通过 `input_mode` 区分来源。

---

# 21. 完成项目所需知识学习清单

## 21.1 最短学习路径：先做 MVP，不要先学半年

按这个顺序学，边学边做：

```text
1. Python 工程基础 + 虚拟环境 + 配置 + 日志
2. FastAPI + WebSocket + asyncio.Queue
3. OpenAI-compatible LLM streaming
4. Segmenter + TTSJobQueue + AudioQueue + cancellation token
5. GPT-SoVITS API 接入
6. VTube Studio WebSocket API + 热键
7. SQLite 自动重要记忆 + 结构化字段/全文搜索
8. Windows 活动窗口 + mss 截图 + 隐私黑名单
9. Proactive Trigger scoring
10. 托盘、配置页、打包
```

一句话：**先把“她能说话、能动、能被打断、能记住一点东西”做出来，再做屏幕感知和主动发话。**

---

## 21.2 第一阶段必须读的核心资料与书

不要一口气读几十本。第一阶段只建议重点读这些：

| 资料/书                                | 为什么推荐            | 对项目最有帮助的主题                      | 是否通读            | 可跳过              | 推荐顺序 |
| ----------------------------------- | ---------------- | ------------------------------- | --------------- | ---------------- | ---- |
| FastAPI 官方文档                        | 后端和 WebSocket 基础 | 路由、依赖、WebSocket、后台任务            | 不用通读            | 复杂部署、OAuth       | 第 1  |
| Python asyncio 官方文档                 | 低延迟流水线核心         | Task、Queue、Cancellation、timeout | 不用通读            | 低层 event loop 实现 | 第 2  |
| VTube Studio API 文档                 | Live2D 控制核心      | 认证、热键、模型移动、事件                   | 重点读 API Details | item 高级功能可后看     | 第 3  |
| GPT-SoVITS README / API 文件 / issues | TTS 接入核心         | `/tts` 参数、reference audio、流式、环境 | 不用通读训练部分        | 训练流水线先跳过         | 第 4  |
| 《Python Concurrency with asyncio》   | 异步队列与取消机制        | producer-consumer、Task、Queue、超时 | 选读              | 网络爬虫案例可跳         | 第 5  |
| 《Effective Python》                  | 写出可维护 Python     | 函数、类、异常、并发、类型习惯                 | 选读              | 元类等高级部分          | 第 6  |
| 《Designing Voice User Interfaces》   | 桌宠语音体验           | 轮次、打断、提示语、语音反馈                  | 选读              | 电话 IVR 细节        | 第 7  |

---

## 21.3 第二阶段边做边查

| 书/资料                                      | 为什么推荐             | 帮助最大的章节/主题                                   | 是否通读        |
| ----------------------------------------- | ----------------- | -------------------------------------------- | ----------- |
| 《Architecture Patterns with Python》       | 模块边界、事件、仓储、服务层    | Repository、Unit of Work、Events、Service Layer | 选读核心章节      |
| 《Fluent Python》                           | Python 进阶，写更稳的库代码 | dataclass、协议、迭代器、async                       | 不通读         |
| ChromaDB / FAISS 官方文档                     | 长期记忆向量检索          | collection、metadata、similarity search        | 查用法         |
| SQLite 官方文档                               | 本地结构化存储           | schema、事务、索引、备份                              | 查用法         |
| 《Designing Bots》                          | 对话式产品体验           | 对话设计、失败恢复、个性化                                | 选读          |
| Microsoft Human-AI Interaction Guidelines | AI 交互体验、安全反馈、可控性  | 错误恢复、用户控制、随时间学习                              | 读摘要和 18 条准则 |

Microsoft 的 Human-AI Interaction Guidelines 是 18 条通用 AI 交互设计准则，强调 AI 在初次互动、常规互动、出错时、长期互动中的行为方式，很适合这个项目的陪伴体验设计。([Microsoft][14])

---

## 21.4 第三阶段进阶提升

| 书/资料                                    | 为什么推荐                    | 帮助最大的主题                                          | 是否通读  |
| --------------------------------------- | ------------------------ | ------------------------------------------------ | ----- |
| 《Designing Data-Intensive Applications》 | 记忆、日志、队列、数据一致性           | 存储、索引、流处理、可靠性                                    | 选读    |
| 《Designing Machine Learning Systems》    | AI 系统工程化                 | 数据、评估、监控、迭代                                      | 选读    |
| 《Speech and Language Processing》        | 语音/对话/NLP 底层知识           | dialogue systems、speech、language modeling        | 查章节   |
| 《Release It!》                           | 稳定性、超时、熔断、降级             | timeout、bulkhead、circuit breaker                 | 选读    |
| 《Clean Architecture》                    | 大项目边界                    | use case、interface adapter、依赖倒置                  | 选读    |
| OWASP Top 10 for LLM Applications       | 插件、Prompt Injection、隐私风险 | Prompt Injection、Sensitive Info、Excessive Agency | 查风险清单 |

OWASP LLM Top 10 覆盖 Prompt Injection、敏感信息泄露、供应链、过度代理能力等风险；这个项目涉及屏幕、记忆、插件和 LLM，因此后续需要用它检查安全边界。([OWASP Foundation][15])

---

# 22. A～N 模块学习清单

## A. Python 工程基础

| 项       | 内容                                                                  |
| ------- | ------------------------------------------------------------------- |
| 为什么重要   | 后端、客户端、插件、脚本都依赖 Python 工程能力。                                        |
| 学到什么程度  | 能写包结构、配置加载、日志、测试、类型注解。                                              |
| 先后顺序    | venv/uv/poetry → pyproject → logging → pytest → typing → packaging。 |
| 官方文档    | Python 官方文档、venv、logging、typing、pytest 文档。                          |
| 推荐书籍    | 《Effective Python》《Fluent Python》选读。                                |
| 视频/课程方向 | Python 工程化、pytest、类型注解、项目结构。                                        |
| 小练习     | 写一个 `config.yaml + .env` 加载器，带单元测试。                                 |
| 对应模块    | `app/config`, `app/schemas`, `tests`                                |
| 优先级     | 必须学                                                                 |
| 难度      | 入门到中等                                                               |

## B. FastAPI / WebSocket / 异步编程

| 项       | 内容                                                                    |
| ------- | --------------------------------------------------------------------- |
| 为什么重要   | Desktop Client 与后端通信、LLM streaming、事件推送都依赖它。                          |
| 学到什么程度  | 能写 HTTP API、WebSocket、后台 task、async queue、取消。                         |
| 先后顺序    | FastAPI route → WebSocket echo → asyncio task → Queue → cancellation。 |
| 官方文档    | FastAPI WebSocket、Python asyncio。                                     |
| 推荐书籍    | 《Python Concurrency with asyncio》                                     |
| 视频/课程方向 | async/await、WebSocket 实时应用、producer-consumer。                         |
| 小练习     | 写 `/health` 和 `/ws/echo`，再写一个 token streaming 模拟器。                    |
| 对应模块    | `api/ws_routes`, `core/event_bus`, `pipelines/llm_stream_pipeline`    |
| 优先级     | 必须学                                                                   |
| 难度      | 中等                                                                    |

## C. Windows 桌面开发与系统 API

| 项       | 内容                                                   |
| ------- | ---------------------------------------------------- |
| 为什么重要   | 文字对话框、中文输入法、活动窗口、截图、托盘、热键和打包都依赖桌面 UI/Windows 行为。      |
| 学到什么程度  | 能实现消息列表、文字输入、流式更新、打断和连接状态；能获取活动窗口信息并做托盘/全局热键。     |
| 先后顺序    | 最小 Chat Window → WebSocket 状态绑定 → 中文 IME → 托盘/热键 → pywin32 活动窗口 → 打包。 |
| 官方文档    | 所选 GUI 框架、Microsoft Win32 API、pywin32、psutil 文档。      |
| 推荐书籍    | 不必专门读厚书，查 API 即可。                                    |
| 视频/课程方向 | Windows automation basics、Win32 API with Python。     |
| 小练习     | 写一个不直连 LLM 的 Chat Window：显式发送文字，经 WebSocket 显示流式回复，再打印活动窗口信息。 |
| 对应模块    | `desktop_client/ui/chat_window.py`, `desktop_client/input_controller.py`, `perception/active_window` |
| 优先级     | 必须学                                                  |
| 难度      | 中等                                                   |

## D. VTube Studio / Live2D 集成

| 项       | 内容                                                          |
| ------- | ----------------------------------------------------------- |
| 为什么重要   | 角色展示、表情、动作、嘴型都靠 VTS。                                        |
| 学到什么程度  | 会认证、保存 token、触发热键、移动模型、处理断线重连。                              |
| 先后顺序    | 开启 VTS API → APIState → token → auth → hotkey → move model。 |
| 官方文档    | VTube Studio Public API、pyvts 文档。                           |
| 推荐书籍    | 暂不需要 Live2D 深度书籍。                                           |
| 视频/课程方向 | VTube Studio plugin API、Live2D expression/hotkey setup。     |
| 小练习     | Python 脚本触发 happy/shy/neutral 三个表情。                         |
| 对应模块    | `clients/vts`, `services/vts_service`                       |
| 优先级     | 必须学                                                         |
| 难度      | 中等                                                          |

## E. LLM API 调用、Streaming、Prompt Engineering

| 项       | 内容                                                                          |
| ------- | --------------------------------------------------------------------------- |
| 为什么重要   | 对话、人格、主动发话、屏幕摘要理解都依赖 LLM。                                                   |
| 学到什么程度  | 会 OpenAI-compatible SDK、streaming、JSON 输出、prompt 分层。                        |
| 先后顺序    | 非流式 chat → streaming → system prompt → structured output → fallback parser。 |
| 官方文档    | DeepSeek/Qwen/Kimi/OpenAI-compatible 文档。                                    |
| 推荐书籍    | 《Building LLMs for Production》选读；官方文档优先。                                    |
| 视频/课程方向 | LLM API engineering、structured output、RAG prompt。                           |
| 小练习     | 写一个流式 CLI，边接收 token 边按句打印。                                                  |
| 对应模块    | `clients/llm`, `prompts`, `context_builder`                                 |
| 优先级     | 必须学                                                                         |
| 难度      | 中等                                                                          |

## F. 对话编排、事件驱动与 Post-MVP 插件系统

| 项       | 内容                                                           |
| ------- | ------------------------------------------------------------ |
| 为什么重要   | Orchestrator、输入汇合、取消和 Proactive Engine 需要清晰的事件与状态边界。       |
| 学到什么程度  | MVP 先掌握事件对象、queue、handler 和状态机；插件权限到 Post-MVP 再学。          |
| 先后顺序    | 统一 UserMessage → queue → handler → cancellation/state machine → Post-MVP plugin permissions。 |
| 官方文档    | Python asyncio、pluggy 文档可参考。                                 |
| 推荐书籍    | 《Architecture Patterns with Python》《Clean Architecture》      |
| 视频/课程方向 | event-driven architecture、plugin architecture。               |
| 小练习     | 让 text/voice 两种输入进入同一 `user.message` handler，并能取消旧 turn。       |
| 对应模块    | `desktop_client/input_controller`, `core/event_bus`, `proactive/state_machine` |
| 优先级     | 建议学                                                          |
| 难度      | 中等到较难                                                        |

## G. TTS、语音合成、GPT-SoVITS、本地音频播放

| 项       | 内容                                                                |
| ------- | ----------------------------------------------------------------- |
| 为什么重要   | 桌宠陪伴感很大程度来自声音。                                                    |
| 学到什么程度  | 会启动 GPT-SoVITS API、传参、处理 wav、选择参考音频、处理失败。                         |
| 先后顺序    | mock TTS → GPT-SoVITS `/tts` → preset → cache → timeout fallback。 |
| 官方文档    | GPT-SoVITS README/API/issues、sounddevice/simpleaudio 文档。          |
| 推荐书籍    | 《Designing Voice User Interfaces》                                 |
| 视频/课程方向 | TTS API、voice UI、audio playback in Python。                        |
| 小练习     | 输入文本调用本地 TTS，保存 wav 并播放。                                          |
| 对应模块    | `clients/tts`, `services/tts_service`, `audio_player`             |
| 优先级     | 必须学                                                               |
| 难度      | 中等                                                                |

## H. 低延迟音频流水线与并发队列

| 项       | 内容                                                                |
| ------- | ----------------------------------------------------------------- |
| 为什么重要   | “像活的角色”取决于首句播放延迟，而不是完整回复质量。                                       |
| 学到什么程度  | 能写 streaming → segment → TTS → ordered playback → cancellation。   |
| 先后顺序    | Queue → 多 worker → 顺序播放 → backpressure → cancellation → metrics。  |
| 官方文档    | asyncio Queue、sounddevice/PortAudio 文档。                           |
| 推荐书籍    | 《Python Concurrency with asyncio》《Release It!》选读。                 |
| 视频/课程方向 | realtime audio pipeline、async producer-consumer。                  |
| 小练习     | 模拟 LLM 每 50ms 出 token，第一句切出后立即播放 mock wav。                        |
| 对应模块    | `pipelines/llm_stream_pipeline`, `tts_pipeline`, `audio_pipeline` |
| 优先级     | 必须学                                                               |
| 难度      | 较难                                                                |

## I. OCR、截图、屏幕理解、活动窗口识别

| 项       | 内容                                                                             |
| ------- | ------------------------------------------------------------------------------ |
| 为什么重要   | 屏幕感知版 MVP 的核心。                                                                 |
| 学到什么程度  | 会活动窗口识别、截图裁剪、OCR、变化检测、场景分类。                                                    |
| 先后顺序    | active window metadata → privacy guard → allowed screenshot → hash diff → OCR → app classifier。 |
| 官方文档    | mss、pywin32、OpenCV、PaddleOCR/Tesseract。                                        |
| 推荐书籍    | 不必先读 CV 书，工程查文档即可。                                                             |
| 视频/课程方向 | desktop screenshot automation、OCR basics、OpenCV diff。                          |
| 小练习     | 只截 VS Code 窗口，计算 hash 变化，敏感应用跳过。                                               |
| 对应模块    | `perception/*`                                                                 |
| 优先级     | 必须学                                                                            |
| 难度      | 中等到较难                                                                          |

## J. 可控记忆；向量数据库与 RAG 延后

| 项       | 内容                                                                              |
| ------- | ------------------------------------------------------------------------------- |
| 为什么重要   | 陪伴型 AI 必须“记得我”，但不能乱记。                                                           |
| 学到什么程度  | MVP 会 SQLite、迁移、CRUD、自动重要度/置信度写入策略、敏感确认和简单检索；向量检索只需了解何时值得引入。       |
| 先后顺序    | SQLite profile → conversation table → explicit write/delete policy → simple search → Post-MVP embedding/vector search。 |
| 官方文档    | MVP 以 SQLite 官方文档为主；Post-MVP 再查 ChromaDB/FAISS/Embedding API。         |
| 推荐书籍    | 《Designing Data-Intensive Applications》选读。                                      |
| 视频/课程方向 | SQLite migration、可删除记忆；RAG/vector database 作为后续选学。                   |
| 小练习     | 用 20 条真假混合对话验证自动重要记忆，查看、修改、删除，并证明凭据不写入、删除后不再进入上下文。                      |
| 对应模块    | `memory`, `storage/sqlite.py`, `storage/repositories`                          |
| 优先级     | 必须学                                                                             |
| 难度      | 中等                                                                              |

## K. 情绪系统、人格建模、陪伴型 AI 交互

| 项       | 内容                                                                            |
| ------- | ----------------------------------------------------------------------------- |
| 为什么重要   | 项目差异化就在陪伴感、人格连续性和情绪反馈。                                                        |
| 学到什么程度  | 会设计数值情绪、衰减、映射、prompt 控制和防跳变。                                                  |
| 先后顺序    | emotion state → delta rules → decay → label mapping → prompt/TTS/VTS mapping。 |
| 官方文档    | Microsoft HAI Guidelines、HAX Toolkit。                                         |
| 推荐书籍    | 《Designing Bots》《Designing Voice User Interfaces》                             |
| 视频/课程方向 | character design、conversational UX、virtual companion design。                  |
| 小练习     | 根据 10 种用户输入更新 EmotionState，并映射 VTS 表情。                                        |
| 对应模块    | `emotion`, `prompts`, `vts expression_mapper`                                 |
| 优先级     | 建议学                                                                           |
| 难度      | 中等                                                                            |

## L. 隐私保护、安全边界、敏感信息处理

| 项       | 内容                                                      |
| ------- | ------------------------------------------------------- |
| 为什么重要   | 项目会观察屏幕和保存记忆，隐私是生死线。                                    |
| 学到什么程度  | 会默认隐私保护、日志脱敏、API key 管理、插件权限、敏感场景屏蔽。                    |
| 先后顺序    | API key 安全 → 日志脱敏 → 敏感黑名单 → Privacy Guard → 插件权限。       |
| 官方文档    | OpenAI API key 安全建议、OWASP LLM Top 10、Privacy by Design。 |
| 推荐书籍    | Privacy by Design 相关材料；《Release It!》安全稳定性部分。            |
| 视频/课程方向 | secure local apps、LLM security、privacy engineering。     |
| 小练习     | 写一个日志脱敏器，把 API key、手机号、邮箱、验证码替换成 `[REDACTED]`。          |
| 对应模块    | `privacy_guard`, `logging`, `plugins/permissions`       |
| 优先级     | 必须学                                                     |
| 难度      | 中等到较难                                                   |

## M. 日志、测试、性能监控、打包部署

| 项       | 内容                                                                |
| ------- | ----------------------------------------------------------------- |
| 为什么重要   | 低延迟系统没有 metrics 很难调；桌面应用需要可部署。                                    |
| 学到什么程度  | 会 pytest、mock、结构化日志、latency metrics、PyInstaller。                  |
| 先后顺序    | unit test → integration test → metrics → debug webui → packaging。 |
| 官方文档    | pytest、logging、PyInstaller/Nuitka。                                |
| 推荐书籍    | 《Release It!》                                                     |
| 视频/课程方向 | Python testing、desktop packaging、observability。                   |
| 小练习     | 统计首 token、首句播放、TTS 耗时并在 Debug WebUI 显示。                           |
| 对应模块    | `tests`, `debug`, `tools`, `packaging`                            |
| 优先级     | 建议学                                                               |
| 难度      | 中等                                                                |

## N. 产品设计、HCI、人机交互、AI 桌宠体验设计

| 项       | 内容                                                       |
| ------- | -------------------------------------------------------- |
| 为什么重要   | 技术跑通不等于好用；主动发话、打扰频率、情绪边界都属于体验设计。                         |
| 学到什么程度  | 能设计低打扰交互、反馈、失败恢复、用户控制。                                   |
| 先后顺序    | 用户场景 → 打扰边界 → 主动发话规则 → 设置开关 → 长期体验。                      |
| 官方文档    | Microsoft Human-AI Interaction Guidelines / HAX Toolkit。 |
| 推荐书籍    | 《Designing Bots》《Designing Voice User Interfaces》        |
| 视频/课程方向 | HCI basics、conversational UX、AI companion UX。            |
| 小练习     | 设计 20 条主动发话样例，并标注触发条件、冷却、打扰等级。                           |
| 对应模块    | `proactive`, `prompts`, `desktop_client/ui/chat_window.py`, `desktop_client/ui/settings_window.py` |
| 优先级     | 建议学                                                      |
| 难度      | 入门到中等                                                    |

---

# 23. 项目学习路线表

| 阶段      | 项目目标                   | 要学的知识                                | 推荐资料                         | 推荐书籍                                | 练习任务                    | 完成后能实现的项目功能         | MVP 必须 |
| ------- | ---------------------- | ------------------------------------ | ---------------------------- | ----------------------------------- | ----------------------- | ------------------- | ------ |
| Phase 0 | Python/FastAPI 工程基础与双输入契约 | venv、pyproject、配置、日志、pytest、Pydantic、WebSocket | Python/FastAPI/Pydantic 官方文档 | 《Effective Python》 | 写 `/health`、消息序列化和日志脱敏测试 | 后端基础服务与统一 `UserMessage` 可运行 | 是 |
| Phase 1 | Mock 纵向闭环 | streaming、asyncio.Queue、分句、有序播放、取消 | LLM/asyncio 官方文档 | 《Python Concurrency with asyncio》 | 文字与模拟语音共用 token→segment→audio | 主链路可提前验证、可打断 | 是 |
| Phase 2 | 真实 VTS、LLM、GPT-SoVITS | VTS WebSocket/token、LLM streaming、TTS API、音频播放、timeout | VTS、LLM、GPT-SoVITS 官方资料 | 《Designing Voice User Interfaces》 | 真实角色说话、表情、断线降级 | 能说、能动、能显示文字、能被打断 | 是 |
| Phase 3 | 情绪与表情映射 | 状态数值、decay、prompt/TTS/VTS 映射和冷却 | HAX / HAI 指南 | 《Designing Bots》 | 用户夸奖→克制的害羞语气与表情 | 回复、声音和 Live2D 受情绪影响 | 是 |
| Phase 4 | 简单可控记忆 | SQLite、迁移、CRUD、简单检索、写入策略 | SQLite 官方文档 | 《Architecture Patterns with Python》 | “记住 X”后可查看和删除 | 用户画像和明确长期记忆 | 是 |
| Phase 5 | 屏幕观察与隐私 | 活动窗口、Privacy Guard、mss、OCR、hash diff | mss、pywin32、OCR 文档 | Privacy by Design 材料 | 先拦截敏感窗口，再允许截图 | 屏幕感知和隐私过滤 | 是 |
| Phase 6 | 主动发话与试用 | 事件、trigger scoring、cooldown、抑制 | asyncio、HAI 指南 | 《Designing Bots》 | 实际试用并记录打扰度 | 低频主动陪伴 | 是 |
| Phase 7 | 对话框、托盘与发布 | GUI 事件循环、中文输入法、流式消息、WebSocket 状态、打包 | GUI/PyInstaller 官方资料 | 《Release It!》 | 文字对话框输入、打断、重连并在干净 Windows 启动 | 文字/语音可切换的日常试用版本 | 是 |
| Post-MVP | 插件、向量检索、复杂 Diary | permissions、sandbox、embedding、RAG | 按真实需求选择官方资料 | 《Clean Architecture》选读 | 先证明基础能力不足再立项 | 可扩展能力 | 否 |

---

# 24. 不建议现在学的内容

| 暂不建议学         | 为什么暂时不学                                  | 什么时候再学                       |
| ------------- | ---------------------------------------- | ---------------------------- |
| 自研 Live2D 渲染器 | 会消耗大量图形学、Live2D SDK、窗口渲染时间；VTS 已能满足 MVP。 | 当你想摆脱 VTS、做完全自定义桌宠窗口时。       |
| 从零训练 TTS 模型   | 数据、标注、训练、调参成本高；第一版只需要调用 GPT-SoVITS。      | 有合法授权语音数据，并且推理 pipeline 稳定后。 |
| 从零训练 LLM      | 成本极高，且你的目标是产品架构，不是训练基础模型。                | 未来研究本地离线人格模型时。               |
| 复杂强化学习 Agent  | 第一版不做游戏操作和自动电脑控制，不需要 RL。                 | 做复杂游戏 Agent 或自动决策实验时。        |
| 多模态模型训练       | 屏幕理解可先用 OCR、规则、云端多模态 API。                | 有大量标注屏幕数据和明确研究目标后。           |
| 完整自动操作电脑      | 安全风险高，容易误操作；与陪伴型 MVP 不一致。                | 权限系统、确认机制、回滚机制成熟后。           |
| 大规模分布式微服务     | 本地桌宠不需要；过早拆服务会增加部署和调试难度。                 | 用户量、插件量、服务边界都稳定后。            |
| 过早做插件市场       | 权限、安全、版本兼容都复杂。                           | PluginBase、权限模型经过实际插件验证后。    |
| 复杂 UI 框架      | 第一版托盘 + 简单设置页够用。                         | 核心体验稳定后再做精美 UI。              |
| 深度 QQ/微信集成    | 隐私和平台风险高。                                | 只做明确授权、低权限、可关闭的插件适配。         |

---

# 25. 推荐落地顺序

最实用的开发顺序是：

```text
1. app/main.py + /health
2. 最小 schemas + config + logging
3. Mock LLM streaming + Segmenter
4. Mock TTS + Ordered Audio Player
5. Cancellation token + latency metrics
6. 文字/模拟语音 UserMessage 共用主链路
7. VTS Bridge：连接、token、hotkey
8. 接真实 LLM Provider
9. 接 GPT-SoVITS
10. 第一次 Windows 打包冒烟
11. EmotionState + 表情映射
12. Memory SQLite + 简单检索
13. Screen Observer + Privacy Guard + Proactive Talk
14. Desktop Client：文字对话框、语音输入适配、统一 InputController、托盘和设置页
15. 完整打包、回归和发布验收
```

做到第 9 步，你就已经有一个能说话、能动、能打断的桌宠雏形。做到第 13 步，才开始真正接近“住在电脑里的陪伴型角色”。

第 14 步必须保证文字和语音共用同一会话与业务流水线：Chat Window 只负责文字草稿、显式发送和流式展示，Voice Input Adapter 只提供转写文本，二者统一由 `input_controller.py` 创建 `UserMessage`。

[1]: https://github.com/DenchiSoft/VTubeStudio "GitHub - DenchiSoft/VTubeStudio: VTube Studio API Development Page · GitHub"
[2]: https://github.com/RVC-Boss/GPT-SoVITS "GitHub - RVC-Boss/GPT-SoVITS: 1 min voice data can also be used to train a good TTS model! (few shot voice cloning) · GitHub"
[3]: https://fastapi.tiangolo.com/advanced/websockets/ "WebSockets - FastAPI"
[4]: https://docs.python.org/3/library/asyncio-queue.html "Queues — Python 3.14.6 documentation"
[5]: https://pydantic.dev/docs/validation/latest/api/pydantic/base_model/ "BaseModel | Pydantic Docs"
[6]: https://api-docs.deepseek.com/ "Your First API Call | DeepSeek API Docs"
[7]: https://github.com/Genteki/pyvts "GitHub - Genteki/pyvts: A python library for interacting with the VTube Studio API · GitHub"
[8]: https://pypi.org/project/mss/?utm_source=chatgpt.com "Python MSS"
[9]: https://github.com/mhammond/pywin32?utm_source=chatgpt.com "Python for Windows (pywin32) Extensions"
[10]: https://docs.python.org/3/library/sqlite3.html "sqlite3 — DB-API 2.0 interface for SQLite databases — Python 3.14.6 documentation"
[11]: https://docs.trychroma.com/docs/overview/introduction?utm_source=chatgpt.com "Chroma Docs: Introduction"
[12]: https://platform.kimi.ai/docs/api/overview "API Overview - Kimi API Platform"
[13]: https://www.onetrust.com/blog/principles-of-privacy-by-design/?utm_source=chatgpt.com "The 7 Principles of Privacy by Design | Blog"
[14]: https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/?utm_source=chatgpt.com "Guidelines for Human-AI Interaction"
[15]: https://owasp.org/www-project-top-10-for-large-language-model-applications/?utm_source=chatgpt.com "OWASP Top 10 for Large Language Model Applications"
