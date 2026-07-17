# 全仓库代码、PR 与 Windows 后续开发风险审计

> 供 Pro 模型制定修复方案使用；本文只描述问题、证据、约束与验收目标，不包含实现补丁。

## 0. 审计元数据

| 项目 | 值 |
|---|---|
| 审计日期 | 2026-07-15 |
| 仓库 | `mizusawa-matsuri937/megumin_companion_ai` |
| 当前分支 | `agent/windows-development-baseline` |
| 当前提交 | `d56cfbd` (`Add Windows development baseline`) |
| 基线分支 | `main`，当前分支比 `origin/main` 多 1 个提交 |
| 当前 PR | #11 `Add Windows development baseline`，Draft，`MERGEABLE / CLEAN` |
| 审计范围 | 149 个受 Git 跟踪的文件；`app/`、`desktop_client/`、`tools/`、配置、迁移、CI、文档和测试；同时回看 PR #1–#11 |
| 代码规模 | 生产代码与工具约 10.9k 行（不含锁文件与生成物） |

本文中的结论分成三类：

- **已复现**：使用最小脚本、构建产物或 API 客户端实际触发。
- **静态确认**：控制流、数据流或配置组合能够直接证明，但没有真实外设/服务环境可复现。
- **能力缺口**：不是某个函数的局部 bug，但会直接阻挡 Windows MVP、安装包或长期运行。

严重度定义：

- **P0**：合并/发布/Windows 产品化前必须先解决；涉及控制面安全、安装包不可用、隐私边界或核心能力不存在。
- **P1**：进入真实用户长时间运行或真实设备联调前必须解决；很可能造成泄露、重复计费、卡死、资源耗尽或错误行为。
- **P2**：应排进近期工程化工作；主要影响可维护性、诊断、供应链和覆盖证据。

## 1. 执行结论

当前项目已经具备质量较好的 Python 核心：异步取消、隐私 fail-closed 思路、类型检查、属性测试、macOS/Windows 双平台 CI，以及相对清晰的模块边界。全量测试、Ruff、mypy 和源码构建均通过。

但它还不能被视为“可交付的 Windows 桌面应用”，也不建议仅凭当前绿灯把 PR #11 视为 Windows 基线已经闭环。最需要先处理的是：

1. **本地 HTTP/WebSocket 控制面没有认证、Origin 校验和会话隔离**。恶意网页或本机其他进程可能控制对话、读取/导出/删除记忆、修改隐私功能；WebSocket 还订阅所有 session 的事件。
2. **构建出的 wheel 实际不可独立导入或运行**。默认 `config.yaml` 没有进入 wheel，模块导入时又立即创建应用并读取这个不存在的文件；CI 的 `uv build` 绿灯不足以代表安装产物可用。
3. **Windows 用户数据、安全凭据、日志和临时音频没有统一的 OS 数据目录与 ACL/DPAPI 边界**。当前相对路径在仓库、当前工作目录和包安装目录之间漂移，安装到 `Program Files`、从开始菜单启动、标准用户运行时都会暴露问题。
4. **桌面产品闭环尚不存在**。STT 与感知模块主要停留在工厂/库层，真实 Windows 屏幕与前台窗口适配、桌面 UI/托盘/热键、设备切换、安装器和生命周期编排均未接入主应用。

因此建议把 Windows 路线拆成：**安全与路径基座 → 协议可靠性与有界资源 → 真实适配器/桌面壳 → 安装包与升级 → 长稳和设备矩阵**。不要先堆 UI，再回头改控制协议与数据目录；否则迁移成本会显著放大。

本报告共登记 **4 个 P0、24 个 P1、13 个 P2**。其中 wheel、WebSocket Origin、VTS 零退避和消息幂等 4 项已经用针对性实验复现；其余项目均标明是静态确认、静态风险还是能力缺口。

## 2. 实际验证与证据

### 2.1 通过的质量门槛

| 检查 | 结果 |
|---|---|
| `uv run pytest` | 460 项收集；459 passed，1 skipped；分支覆盖率 92.54% |
| 唯一跳过项 | 环境中没有可确定使用的测试字体，因此真实 OCR 字体路径没有得到执行证据 |
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 130 个文件均符合格式 |
| `uv run mypy` | 严格检查通过 |
| `uv build` | sdist 与 wheel 均成功生成 |
| `uv pip check` | 当前锁定环境依赖兼容 |
| Bandit | 无 high；一个 medium `B608` 经核对为常量 SQL 片段选择造成的误报；其余为低等级测试/断言提示 |

### 2.2 已复现的缺陷

#### R-01：wheel 成功构建但不可独立导入

构建产物只包含 `app`、`desktop_client` 与 metadata，没有 `config.yaml`，也没有 `[project.scripts]` 启动入口。将 wheel 放入隔离环境并在仓库外执行：

```text
uv run --isolated --no-project --with <built-wheel> python -c "import app.main"
```

得到：

```text
ConfigurationError: ...site-packages\config.yaml
```

直接原因见 `app/config/settings.py:22-24`、`app/main.py:213` 和 `pyproject.toml:42`。

#### R-02：任意 WebSocket Origin 被接受

对 `/ws/client` 发送 `Origin: https://attacker.example`，服务照常完成握手并处理消息。路由在 `app/api/routes.py:220-224` 先 `accept()`，没有 Origin、token、子协议或本机客户端身份检查。

#### R-03：VTS 零退避导致 CPU 热循环

配置允许 `reconnect_initial_seconds = reconnect_max_seconds = 0`（`app/config/settings.py:108-115`）。让连接工厂持续失败时，20 ms 内被调用 **1,921 次**。`app/clients/vts/bridge.py:153-194` 的重试循环与 `:281-284` 的零延时路径共同造成热循环。

#### R-04：相同 `message_id` 会创建两个新 turn

连续两次调用 `TurnService.accept()`，输入使用同一个 `UserMessage.message_id = "msg_same"`，得到两个不同 `turn_id`。当前不存在幂等表、重复请求响应缓存或 session 序列号。这会让 Windows UI 在断线重发、按钮双击或超时重试时重复调用 LLM/TTS 并重复播放。

### 2.3 依赖审计

使用锁文件导出依赖后执行 `pip-audit`：运行时依赖未报告已知漏洞；开发依赖中的 `pytest 8.4.2` 命中 `PYSEC-2026-1845`，修复版本为 `9.0.3`。项目当前声明 `pytest>=8,<9`，因此不能只刷新锁文件，需先评估并扩大上限、运行完整测试。此结果只代表 2026-07-15 当日漏洞库。

## 3. P0：合并/Windows 产品化阻塞项

### P0-01 本地控制面缺少身份认证、Origin 校验和会话隔离

**类型：已复现 + 静态确认**

证据：

- `app/api/routes.py:55-201` 暴露聊天、打断、功能开关、记忆查询/搜索/导出/更新/删除、历史清理和 debug state，均无认证/授权依赖。
- `app/api/routes.py:220-224` 对 WebSocket 无条件接受，并以 `service.subscribe("*")` 订阅全部会话。
- `app/config/settings.py:42-46` 允许配置监听地址；如果改成非 loopback，当前代码不会拒绝或要求 TLS/认证。
- `/debug/state` 在生产路径中始终可用，并能暴露累计的 turn/outcome 状态。

风险：

- 恶意网页存在跨站 WebSocket 劫持本地服务的路径；本机低权限进程也可直接调用 API。
- 任意客户端可读取或删除用户记忆、关闭隐私功能、发起带费用的模型请求或驱动语音/Live2D。
- 通配订阅会把其他 session 的内容推给当前连接，未来多窗口、多用户或插件化后形成明确的数据串流。

要求 Pro 模型给出一个明确架构决策：继续使用 loopback HTTP，还是 Windows Named Pipe/受限 IPC；若保留 HTTP，至少设计每次安装随机凭据、Windows 安全存储、HTTP 与 WebSocket 握手认证、严格 Origin allowlist、session 级订阅与授权、非 loopback/TLS 策略、速率限制与安全迁移方案。

验收目标：攻击者 Origin、无 token、错误 session、非 loopback 明文监听全部失败；正常桌面客户端重启后能安全恢复；记忆管理接口有单独的高权限边界。

### P0-02 Python 安装产物不可运行，资源与启动模型错误

**类型：已复现**

证据见 R-01。当前：

- `PROJECT_ROOT` 由源文件位置反推，默认配置与 `.env` 被假定在包根之外（`app/config/settings.py:22-24`）。
- `app/main.py:213` 在导入时立即执行 `create_app()`，把配置读取、路径解析与运行时构造都放进 import side effect。
- `pyproject.toml:42` 只声明 Python 包，没有打包默认配置，也没有 CLI/GUI entry point。

风险：CI 绿灯但用户安装后首启即失败；PyInstaller/Nuitka/MSIX 也会继承错误的资源定位假设；导入测试、ASGI factory 和嵌入式启动都变脆弱。

要求 Pro 设计统一的资源定位和用户数据定位层：只读默认配置作为 package resource；用户覆盖配置、数据库、日志和缓存放 OS 用户目录；显式 `create_app()` factory 与启动入口；提供旧路径迁移。需要同时决定 editable 开发、普通 wheel、冻结 exe 和 MSIX 的一致语义。

验收目标：从任意 CWD、含中文/空格路径、标准用户账户、只读安装目录安装 wheel/exe 后可首启；隔离环境中的 `import`、CLI `--help`、`/health` 均通过。

### P0-03 Windows 私有数据与凭据没有统一安全边界

**类型：静态确认**

目前数据库、日志、VTS token、STT 临时文件、TTS 临时/缓存音频依赖项目根目录、CWD 或配置中的相对路径。`app/main.py:90-95` 会把相对数据库路径放到 `PROJECT_ROOT`，而 `app/config/logging.py:109-115` 又把相对日志放到 `Path.cwd()`，形成两套不一致规则。PR #11 为 Windows 放宽了 POSIX mode 断言，但没有补上 Windows ACL 的等价保证。

风险：

- 安装到 `Program Files` 时无写权限；从开始菜单、终端或开发器启动会写到不同目录。
- SQLite/WAL、对话日志、VTS token、转写 WAV 和合成语音可能被同机其他账户读取。
- `chmod`/POSIX mode 不能证明 Windows DACL 安全；普通删除也不等于 SSD 上可物理擦除。

要求 Pro 设计 Windows 数据分类和目录表：Roaming/Local AppData、临时目录、可迁移配置、缓存、日志、模型、秘密分别去哪里；哪些用用户限定 DACL，哪些使用 DPAPI/Credential Manager，哪些需要开机清理、崩溃恢复和明确保留期。不要把“secure delete”承诺表述为无法在 SSD 上保证的物理擦除。

验收目标：标准用户、两个 Windows 账户、只读安装目录、升级/卸载、进程崩溃与 WAL 存在时均有自动化或安装验收；私有文件 ACL 被显式检查。

### P0-04 Windows 桌面产品闭环尚未接入

**类型：能力缺口**

证据：

- `app/perception/factory.py:24` 的 `build_perception_pipeline()` 未在 `app/main.py` 或生产入口组装；仓库也没有真实 Windows 屏幕捕获、前台窗口、焦点模式/DND 适配器。
- `desktop_client/inputs/factory.py` 能创建语音输入，但没有生产桌面 UI/托盘/热键去驱动 start/stop、显示隐私状态或处理权限/设备变化。
- `app/proactive/runtime.py` 接受 focus、DND、sensitive 参数，但当前主程序只建立闲置调度器，未提供真实 Windows 信号。
- 没有安装器、自动升级、开机启动、单实例、崩溃恢复或卸载数据策略。

这不是要求一次性完成全部 UI，而是要求 Pro 给出最小可运行垂直切片：**单实例桌面壳 → 安全 IPC → 文本对话 → 可选语音 → VTS → 隐私/退出**，并为每一阶段定义可回滚的接口和模拟器。否则现有模块会继续以“测试中的库”存在，无法验证真实生命周期。

## 4. P1：高风险代码问题

### P1-01 长运行状态与事件队列无界增长

**静态确认。** `TurnService` 永久保存 `_states`、`_outcomes`（`app/core/turns.py:90-97`），订阅队列由 `asyncio.Queue()` 无上限创建（`:477-478`）；`snapshot()` 会序列化全部历史（`:549-574`）。慢 WebSocket、断网、长会话和 debug 请求可导致内存、延迟和响应体持续增长。

Pro 需设计：turn 终态 TTL/LRU、持久化摘要与内存分层；事件队列 `maxsize`、丢弃/合并策略、慢消费者断开、序列号与有限 replay；为 assistant delta 使用背压或批处理。验收应包含数小时 soak、慢消费者和 10k turn 的内存上界。

### P1-02 缺少消息幂等、事件序列与断线恢复协议

**已复现。** 见 R-04。数据库 observer 的唯一性冲突不会阻止主 pipeline，因此不能把存储约束当作幂等机制。WebSocket 事件也没有递增序列、ack 或 replay cursor。

Pro 需定义 `(client_id, session_id, message_id)` 的唯一语义、处理中/已完成/已失败重复请求的响应、幂等保留时间、断线后快照+增量协议，以及取消与重试竞态。不得只在 UI 按钮上做 debounce。

### P1-03 对话内部 TTS/audio 队列无界，缺少端到端背压

**静态确认。** `app/pipelines/dialogue.py:134-135` 创建两个无界队列；LLM 可持续产生 segment，而较慢的 TTS/声卡在后面消费。兼容服务也可能忽略 `max_tokens`。长响应会积累文本、任务和音频文件。

Pro 需确定小容量队列、生产者背压、最大 turn 文本/segment/时长、取消时清队列与临时文件的原子性。验收应覆盖 LLM 快、TTS 慢、音频设备阻塞以及 provider 违规超长输出。

### P1-04 TTS 的配置超时没有传到实际 Job

**静态确认。** `TTSJob.timeout_ms` 固定默认 8,000 ms（`app/schemas/messages.py:81-90`）；`DialoguePipeline` 创建 Job 时没有传 timeout（`app/pipelines/dialogue.py:335-344`），随后严格按该值 `wait_for`（`:188-195`）。与此同时 GPT-SoVITS 配置允许另一套更长请求超时。真实服务冷启动或长句超过 8 秒时会被误报 `tts_timeout`。

Pro 需消除双重配置源，明确连接/首字节/总时长/取消超时，并把选择结果注入每个 Job。验收需用 7.9/8.1/30 秒边界和真实冷启动测试。

### P1-05 LLM 流在没有完成标记时也被当作成功

**静态确认。** `app/clients/llm/openai_compatible.py:80-92` 遇到 `[DONE]` 会返回，但远端在发出部分 delta 后直接 EOF 时循环也自然结束；没有检查 `[DONE]`、finish reason 或协议完成状态。pipeline 随后 flush、持久化并可能朗读截断文本。

Pro 需兼容不同 OpenAI-compatible 实现但仍能区分完整 EOF 与截断；给出完成判定、provider capability、最大输出、总 deadline、重试是否允许复用已播内容的策略。验收需模拟半包、缺 `[DONE]`、错误 JSON、连接重置和重复 delta。

### P1-06 VTS 零退避热循环与退避缺少抖动

**已复现。** 见 R-03。即使正数退避，多实例同时重连也没有 jitter。

最低要求是配置层 `gt=0` 或运行时强制 floor，并加入 capped exponential backoff + jitter；对认证失败、配置错误、连接失败分类，非重试错误不可热重连。增加“配置为 0”回归测试和重连速率指标。

### P1-07 VTS 旧 turn 的表情可能在取消/重连后重放

**静态确认。** `app/clients/vts/bridge.py:121-126` 只排队最新 action；action 虽带 `turn_id`，消费前没有与当前 turn generation 校验，取消与断线时也没有按 turn purge 或恢复 neutral。网络恢复后可能给已经被新回复覆盖的旧文本播放表情。

Pro 需把 turn generation/cancellation 传到 VTS，断线后只允许当前可见状态重放，并定义 neutral/reset 行为。验收覆盖旧轮取消、断线、第二轮开始、恢复连接的交错。

### P1-08 关闭“近期历史”后，旧数据停止执行保留期清理

**静态确认。** `HistoryService.cleanup()` 在 feature disabled 时直接返回 0（`app/memory/service.py:133-139`）；维护任务又只调用这个方法（`app/memory/runtime.py:459-465`）。用户关闭记录后，以前已经写入的数据可能无限超过 7 天保留期。

读取/新增开关与法定/产品保留期清理必须解耦。Pro 需定义“关闭”是停止新增、立即清空还是继续到期删除，并给出版本迁移与 UI 文案。验收需先写入、再关闭、推进时钟、确认仍被清理。

### P1-09 记忆维护任务遇到一次异常后永久退出

**静态确认。** `_maintenance_loop()` 只捕获 `TimeoutError`；任一 `prune_expired_confirmations` 或 `history.cleanup` 的数据库异常都会结束 task（`app/memory/runtime.py:459-465`）。`close()` 使用 `gather(..., return_exceptions=True)` 又会吞掉已发生的维护异常（`:477-480`）。

Pro 需增加每周期故障隔离、有限退避、健康状态/指标、隐私安全日志与退出时错误汇总；连续失败需要可见但不能泄露内容。验收覆盖数据库锁、磁盘满、损坏和恢复。

### P1-10 “删除已提交”与“安全清理失败”的 API 语义不一致

**静态确认。** 删除/清理先在 repository 提交，再调用 `database.secure_cleanup()` checkpoint/VACUUM。Windows 上 WAL、杀毒软件、索引器或另一个连接持锁时清理可能失败；此时 API 返回错误，但逻辑删除已经成功，客户端重试得到另一种结果。

Pro 需把逻辑删除结果与 best-effort 物理清理状态分开，提供后台重试和可观察状态，并解释 SQLite/SSD 上的保证边界。验收需注入 WAL busy 和 `PermissionError`。

### P1-11 日志路径不稳定且没有 rotation/retention

**静态确认。** `app/config/logging.py:109-115` 使用 CWD 解析路径和普通 `FileHandler`，没有文件大小、天数或总量限制。Windows 从 IDE、终端、开始菜单和计划任务启动会落到不同目录；长期运行会填满磁盘。

Pro 需将日志放到统一 LocalAppData，使用 bounded rotation/retention，避免多进程互抢，并定义隐私级别、用户导出诊断包和崩溃日志清理。

### P1-12 结构化日志脱敏不按敏感键处理

**静态确认。** `Redactor.redact()` 对 Mapping 只递归 value（`app/config/logging.py:58-67`）；若字段是 `{"api_key": "short-or-new-format"}`，且该值不在已知 secret 列表、也不匹配包含键名的文本正则，就不会被隐藏。未来 VTS、代理 token 或新 provider 字段很容易漏出。

Pro 需设计 key-aware 递归脱敏、值指纹/allowlist、异常字符串处理和属性测试；禁止记录原始 prompt、转写和屏幕内容作为普通诊断字段。

### P1-13 原生线程/音频操作可能让关闭和隐私屏障无限等待

**静态确认。** PortAudio 的 `write/stop/abort/close`、OCR 和图像处理使用 `asyncio.to_thread`；为确保敏感数据销毁，取消后代码会等待 worker drain。Python 无法真正停止卡死的原生线程，因此坏声卡驱动、OCR 死锁或网络盘文件操作可能让 feature disable、app close 甚至隐私 barrier 永远挂起。

这是隐私与可用性的真实冲突。Pro 需决定哪些 native 工作放独立进程，怎样设置 hard deadline、终止 worker、清理共享内存/临时文件，以及 UI 如何显示“正在关闭/已隔离”。不能只再套一层 `asyncio.wait_for`。

### P1-14 Windows 临时文件删除失败后可能失去追踪

**静态确认。** `app/clients/tts/gpt_sovits.py:550-553` 在 `unlink` 前先从 `_paths` 移除。Windows 常见的 `PermissionError`/杀软短暂锁文件会使删除失败，同时 `close()` 已失去重试该路径的记录。进程崩溃也会留下 TTS/STT `.part`、WAV 和临时目录。

Pro 需统一临时资产 registry：删除成功后才去追踪、指数退避、启动时 scavenger、年龄/目录边界验证、不可删除时的告警与下次启动重试。验收需模拟文件句柄占用、崩溃和重启。

### P1-15 whisper.cpp 子进程缺少 Windows 进程树与窗口控制

**静态确认。** `desktop_client/inputs/whisper_cpp.py:121-133` 直接 `asyncio.create_subprocess_exec`，没有 `CREATE_NO_WINDOW`、Job Object 或进程组；超时/取消只管理直接子进程。默认可执行路径为不带 `.exe` 的 `vendor/whisper.cpp/build/bin/whisper-cli`（`app/config/settings.py:168`）。包装器再拉起子进程时可能残留，GUI 模式还可能闪出控制台窗口。

Pro 需设计可执行发现与校验（架构、版本、哈希/签名）、CreateProcess flags、Job Object kill-on-close、stderr 诊断上限和升级策略。验收覆盖空格/中文路径、x64/ARM64、子进程再派生、超时和强制退出。

### P1-16 麦克风 callback 可无限向事件循环排队

**静态确认。** `desktop_client/inputs/voice_input.py:204` 对每个音频 frame 调用 `loop.call_soon_threadsafe` 并复制 bytes。事件循环被 LLM、磁盘或 UI 阻塞时，callback 队列和内存没有上界；PortAudio status 也没有形成可见的 overflow/underflow 事件。

Pro 需采用预分配/有界 ring buffer、明确 drop 策略和溢出错误，限制最大录音字节与时长，并把设备断开、采样率变化传给 UI。验收需故意冻结事件循环和模拟高速 callback。

### P1-17 API 的体积限制发生得太晚，schema 仍有非受控字段

**静态确认。** `UserMessage.text` 虽限制 20,000 字符（`app/schemas/messages.py:44`），ASGI 层没有请求体字节上限，服务要先把整个 JSON 读入内存；WS 消息也没有 frame 上限/速率限制。`created_at`、部分 id/metadata 的可信来源和时间范围没有形成服务端策略。STT 最终超长 transcript 会变成通用 Pydantic 错误，而非明确的 STT 错误。

Pro 需区分传输字节、UTF-8 字符、token、音频秒数和 metadata 深度/键数；服务端生成 received_at，客户端时间只作非可信元数据。验收覆盖大 body、压缩/多字节字符、未来时间、深层 JSON 和 WS flood。

### P1-18 Feature 开关持久化与运行时 transition 不是原子状态机

**静态确认。** `MemoryRuntime.set_feature()` 会持久化 enabled，再运行异步 transition handler。handler 失败时 API 可返回 500，但数据库已经记录新值，实际资源可能仍处于旧状态或半关闭。

Pro 需设计 `desired_state / actual_state / transitioning / failed`，或提供可证明的补偿事务；启动时必须 reconcile。尤其感知关闭是隐私屏障，不能仅靠一个布尔值代表完成。

### P1-19 主动行为限制是进程内易失状态，真实 Windows 抑制信号未接入

**静态确认 + 能力缺口。** `app/proactive/runtime.py:61` 的日计数、冷却和最后触发时间只在内存中，重启会重置；wall clock 跳变也影响冷却。focus mode、DND、sensitive 默认为 false（`:118-146`），没有生产适配器持续供给。

Pro 需明确哪些限制跨重启持久化、冷却使用 monotonic 还是 wall time，并接入 Windows Focus Assist/前台应用/锁屏/全屏/麦克风占用等信号；未知/权限拒绝应 fail closed。验收覆盖重启、时区/DST、手动改时钟、锁屏和 DND。

### P1-20 感知的 change cache 可能保留过期的“非敏感”上下文

**静态风险。** `app/perception/pipeline.py:164-187` 对变化低于阈值的帧复用上一结论；代码对“上一帧敏感”做了 sticky 保护，这是正确方向，但“上一帧非敏感、当前小区域变成密码/通知”仍可能因整体变化小而跳过 OCR。若该上下文用于主动发言，存在隐私误判。

Pro 需设计短 TTL、前台窗口/标题/敏感区域变化的强制重检，以及主动发送前的最后一次轻量敏感检查。验收用小面积密码框、通知弹窗、窗口标题变化和相同窗口局部更新。

### P1-21 感知取消不等于硬超时，真实适配器证据不足

**静态确认 + 测试缺口。** `app/perception/thread_jobs.py:26-34` 明确承认 `to_thread` 不能停止 worker，因此 operation timeout 后仍需 drain；真实 OCR 测试在当前环境被跳过。项目没有 Windows Capture/Window API 或云分析生产实现。

Pro 需把真实适配器纳入独立进程/超时设计，并给出隐私清零证明、模型版本固定、OCR 最大内存、GPU/CPU 回退和离线行为。不能把 mock/property tests 当作真实屏幕数据的充分证据。

### P1-22 长期记忆保存了整条原始来源，而非最小证据

**静态确认。** analyzer 要求 `evidence_quote` 是精确摘录，但 `app/memory/policy.py:131` 将整个 `proposal.source_text` 写入 `source_excerpt`。一条显式“记住”消息可能同时包含不应长期保存的上下文，扩大数据库和导出中的敏感面；旧 source 在 update/supersede 后仍可被导出。

Pro 需采用最小必要证据（quote + hash + provenance），设计 source 的保留/删除/更新级联、已有数据迁移和用户可解释展示。验收需验证删除/改写后旧内容不再通过 export/FTS/WAL 暴露。

### P1-23 非 loopback provider URL 允许明文 HTTP/WS

**静态风险。** 默认本机 GPT-SoVITS/VTS 使用 `http://`/`ws://` 合理，但配置模型没有约束“明文只允许 loopback”。用户把兼容 LLM/TTS/VTS 指向局域网或远程地址时，API key、文本、参考音频路径或 token 可能明文传输。

Pro 需做 URL validator：loopback 可显式允许明文；非 loopback 强制 HTTPS/WSS、证书验证和可选 pinning；明确代理与企业证书策略。不要静默降级 TLS。

### P1-24 Prompt/context 主要按字符预算，不是模型 token 预算

**静态风险。** 当前 context builder 对历史和记忆做了有界选择，这是优点，但兼容模型的 tokenizer、system prompt、当前用户文本和多语言字符会使字符预算与 token 上限偏离；部分路径还会先加载较多 profile 记录再选择。

Pro 需提供 provider/model-aware token estimator、硬的完整请求上限、各来源配额与降级顺序，并防止用户文本或 memory 中的提示注入越过 system policy。验收覆盖中日英混合、emoji、超长当前输入和不同 tokenizer。

## 5. P2：工程化、诊断与测试问题

### P2-01 `/health` 只表示进程存活，`/debug/state` 始终开放

`app/api/routes.py:55-57` 永远返回 ok，不检查数据库迁移、磁盘可写、provider/VTS/STT 状态；`:73-75` 的 debug endpoint 没有生产 gate。Pro 应区分 liveness/readiness/capability，并将 debug 置于受认证开发模式，同时确保健康信息不泄密。

### P2-02 `gate_a_review.py` 的 interrupt 人工验收路径会先报错

`tools/gate_a_review.py:117-120` 的 `MockLLMProvider.response_factory` 参数实际是 `ChatRequest`，却访问 `message.input_mode`；`ChatRequest` 没有该字段。`--mode interrupt/all` 无法到达预期的音频打断验证。该工具未被 CI 执行。需要修正并给工具本身增加无设备 dry-run 测试，真实音频仍保持人工确认。

### P2-03 Windows 音频缺少设备选择、热插拔与延迟回退

`app/pipelines/audio_player.py:99` 固定 `latency="low"`，没有设备 ID、共享/独占模式策略或失败后高延迟回退。真实 Windows 声卡、蓝牙耳机、RDP 和采样率切换都未覆盖。Pro 需设计设备枚举、选择持久化、热插拔状态机和可中断播放的真实矩阵。

### P2-04 VTS 默认 hotkey/ref audio 配置需要启动前验证

静态占位 ID 或错误 `ref_audio_path` 只会在运行时表现为“不动/不出声”。GPT-SoVITS 的参考音频路径对本机服务与远程服务的含义也不同，Windows 盘符路径不能直接假定服务端可见。Pro 需设计 capability discovery、配置向导与启动诊断，不把错误拖到第一次对话。

### P2-05 SQLite 缺少损坏恢复、备份与版本降级策略

现有迁移与事务测试较好，但数据库损坏、新版本 schema 被旧程序打开、磁盘满、杀软锁库时，桌面应用缺少安全模式、备份/恢复和用户可理解的修复流程。Pro 需设计原子备份、迁移前 checkpoint、失败回滚和不泄露内容的诊断包。

### P2-06 依赖元数据允许安装出未经锁文件验证的新组合

CI 使用 `uv.lock` 是优点，但 wheel 的依赖声明是宽范围；用户 `pip install` 会解析未来版本，不一定等同 CI 环境。Windows 冻结包还涉及 PortAudio、onnxruntime、RapidOCR、whisper.cpp 原生二进制和 VC runtime。Pro 需决定 lock/constraints、bundle manifest、SBOM、license、哈希和 x64/ARM64 策略，并处理 `pytest` 安全公告。

### P2-07 CI 没有验证真正的安装产物和 Windows 使用场景

当前 `.github/workflows/ci.yml:32-41` 只在源码环境 sync/test/lint/typecheck，没有：wheel 安装 smoke、CLI/ASGI 启动、打包、供应链扫描、标准用户、只读安装目录、含中文/空格/长路径、非系统盘、文件 ACL、真实/模拟设备和长稳测试。P0-02 正是因此漏过。

### P2-08 CI 触发范围和 Action 固定方式不完整

`.github/workflows/ci.yml:3-8` 的 push 仅匹配 `codex/**`，不覆盖 `main` 和当前使用的 `agent/**`；只能依赖 PR 触发。Actions 使用 `@v4`/`@v6` 标签而非 commit SHA。Pro 应给出分支保护、required checks、main push、定时依赖/长稳任务和 SHA 固定/更新机器人方案。

### P2-09 90% 汇总覆盖率掩盖关键边界缺口

92.54% 是良好基线，但 aggregate threshold 不能证明安全控制和打包路径。未覆盖或证据不足的关键点包括：wheel 安装、Origin/auth、ACL、setup 脚本、Gate A 工具、真实 OCR/声卡/麦克风/VTS/whisper、长时间慢消费者、崩溃恢复。应把这些定义为场景 gate，而不是继续单纯提高总百分比。

### P2-10 Windows setup 脚本对环境假设过强

`tools/setup_windows.ps1` 依赖 WinGet、uv 安装后的缓存路径和 `uv python update-shell` 对用户 shell 的持久修改；一次性安装全部 extras，未区分最小开发环境和感知/原生依赖。脚本没有标准用户、企业代理、无 WinGet、已有多版本 uv/Python、长路径和中文路径测试。Pro 应让脚本幂等、可诊断、少改用户环境，并提供离线/手工路径。

### P2-11 Windows 开发计划文档有一处已过时

`docs/windows_development_plan.md` 仍把远程 Windows CI 视为待确认，但 PR #11 的 Windows 与 macOS quality checks 已成功。需要更新为“源码质量门已通过，安装/设备/ACL/路径矩阵仍未验证”，防止把两类证据混为一谈。

### P2-12 配置覆盖与用户个性化路径不成熟

仓库忽略了 `config.local.yaml`，但当前加载器以 `config.yaml` + `.env` 为主；TTS preset、参考音频、VTS hotkey 和设备路径容易诱使开发者修改受跟踪配置。Pro 需定义 schema 版本、默认/机器/用户/环境覆盖优先级、配置迁移、秘密引用与 UI 写入规则。

### P2-13 可观测性在“隐私”与“可诊断”之间还没有契约

多个边界会吞掉底层异常或只返回稳定 error code，这对隐私有利，但真实 Windows 驱动、权限、路径、编解码问题会很难定位。Pro 需定义无内容诊断字段、correlation/turn id、错误类别、设备/版本信息、采样与用户授权导出；禁止为了排错恢复记录原文、屏幕 OCR 或音频。

## 6. 按板块审查覆盖表

| 板块 | 已审查内容 | 主要结论/问题 ID |
|---|---|---|
| 配置与启动 | `settings.py`、`bootstrap.py`、`main.py`、`config.yaml`、`.env` | 路径模型和 import side effect 阻挡安装；明文远端 URL；用户覆盖/迁移不清。P0-02、P0-03、P1-23、P2-12 |
| 日志与脱敏 | JSON formatter、known secrets、文件输出、redaction check | 无 rotation；CWD 漂移；结构化敏感键可能漏出。P1-11、P1-12、P2-13 |
| HTTP/WebSocket API | 全部路由、chat/cancel、feature、memory/history、debug、WS client/echo | 无认证/Origin/会话隔离/传输上限；健康与 debug 不分环境。P0-01、P1-01、P1-02、P1-17、P2-01 |
| Turn 核心与取消 | `turns.py`、cancellation、contracts、context | 取消/原子终态设计整体较扎实；但状态无界、无消息幂等和事件恢复。P1-01、P1-02 |
| Dialogue pipeline | 分句、LLM→TTS→audio 并发、清理、指标 | 队列无界；TTS timeout 脱节；长输出/背压不足。P1-03、P1-04 |
| LLM provider | OpenAI-compatible streaming/completion、错误映射、取消 | 取消测试较好；EOF 完成判定、总输出/远端明文和 token 预算仍有风险。P1-05、P1-23、P1-24 |
| TTS 与缓存 | mock、GPT-SoVITS、WAV 校验、临时/持久缓存 | 文件验证和取消有较多测试；Windows 锁文件、崩溃残留、路径/隐私/超时仍未闭环。P0-03、P1-04、P1-13、P1-14、P2-04 |
| 系统音频 | `SystemAudioPlayer`、PortAudio 流重用与打断 | 真实设备矩阵、热插拔、低延迟回退和 native hang 未处理。P1-13、P2-03 |
| VTube Studio | client、bridge、token store、expression mapper/event sink | 故障隔离方向正确；零退避热循环、旧 turn 动作、Windows secret 与配置发现不足。P0-03、P1-06、P1-07、P2-04 |
| Emotion | clock、engine、integration、mapper、models | 状态有界且测试充分；未发现独立 P0/P1。需要在协议幂等和重启恢复设计中明确情绪状态是否持久化。P1-02/P2 设计项 |
| Prompt/context | prompt builder、context builder、模型 | 对不可信上下文有明显防护；仍需模型 token 总预算与多语言测试。P1-24 |
| History/memory | analyzer、policy、privacy、service、runtime、repositories/FTS | 确认/凭据拒绝/property tests 较强；关闭后不清理、维护 task 死亡、source 过量保存、删除语义不清。P1-08–P1-10、P1-22 |
| SQLite/迁移 | database、v001、records、repositories | WAL/事务/FTS 基础良好；Windows busy、备份恢复、升级降级和统一目录不足。P0-03、P1-10、P2-05 |
| Perception | capture/OCR 接口、guard、change detection、sanitizer、cloud pipeline、thread jobs | fail-closed 思路和 wipe 测试值得保留；生产未组装，小变化 stale、native hard timeout、真实适配器证据缺失。P0-04、P1-13、P1-20、P1-21 |
| Proactive | policy、engine、runtime、lifecycle | 抑制规则完整；计数易失、wall clock、Windows focus/DND/sensitive 信号未接。P0-04、P1-19 |
| STT/麦克风 | contracts、factory、voice input、whisper.cpp、smoke tool | 取消/临时目录测试较强；callback backlog、进程树/窗口、可执行发现、设备 UI 和崩溃清理不足。P0-04、P1-14–P1-17 |
| Desktop/UI | `desktop_client` 当前仅 inputs | 没有托盘/热键/状态/权限/单实例/安全 IPC/升级卸载闭环。P0-04 |
| 工具 | Gate A、STT smoke、Windows setup | Gate A interrupt 路径 bug；setup 环境假设强；工具本身不在 CI 场景 gate。P2-02、P2-10 |
| 构建与发布 | `pyproject.toml`、`uv.lock`、wheel/sdist | wheel 已复现不可运行；无 entry point/安装器/SBOM/原生二进制策略。P0-02、P2-06、P2-07 |
| 测试与 CI | unit/integration/property、GitHub Actions | 459/460 通过且双 OS 质量门成功；缺安装、ACL、真实设备、长稳、安全和工具场景。P2-07–P2-09 |
| 文档 | README、架构/计划/验收资料、Windows plan | 架构意图清楚；Windows CI 状态一处过时，产品化验收需区分源码门与安装/设备门。P2-11 |

## 7. PR 审查结论

### 7.1 历史 PR

PR #1–#10 均已合并，依次引入 mock 对话、LLM、GPT-SoVITS、VTS、emotion/prompt、memory、perception、proactive、STT 和 Mac AI backend evidence。当前代码树已经包含这些改动，所以本文不是只审当前 diff，而是按最终组合后的跨模块数据流审查。GitHub 中没有可供复用的未解决 review thread；因此不能把“没有 review comment”理解为没有问题。

### 7.2 当前 PR #11

状态：Draft、mergeable、merge state clean；无评论、无 review。macOS 与 Windows 两个 quality job 均成功。变更 12 个文件，核心是 `.gitattributes`、双 OS CI、Windows 开发计划、`tzdata`、跨平台测试调整和 `tools/setup_windows.ps1`。

正面影响：

- 首次让同一套锁定依赖、pytest、Ruff 和 mypy 在 `windows-latest` 跑通。
- 对 Windows 子进程终止/删除竞态测试做了针对性调整。
- `tzdata` 解决无系统 IANA timezone 数据的 Windows 环境。
- 文档已识别真实 Windows 适配器、隐私边界和桌面壳是后续工作。

合并前建议至少处理或明确拆分：

1. 增加 **build wheel → 新隔离环境安装 → 仓库外 import/启动/health** 的 CI smoke；它会立即捕获 P0-02。
2. 明确 Windows token/数据库/日志/临时文件 ACL 的测试计划；不能只移除 POSIX mode 断言。
3. 修复 CI push filter 覆盖 `main` 与实际开发分支，或通过 branch protection 明确只允许 PR。
4. 更新 Windows plan 中“远程 Windows CI 待确认”的过时描述。
5. setup 脚本增加幂等、无 WinGet/企业代理/标准用户说明，避免静默持久修改 shell。
6. Actions 最终按组织供应链政策固定 SHA。

如果 PR #11 的目标仅定义为“源码层 Windows CI 基线”，可以在 P0-02 的安装 smoke 和文档措辞修正后合并；若目标被理解为“Windows 开发/产品基线”，则 P0-01、P0-03、P0-04 仍明显未完成，应继续保持 Draft 或在 PR 描述中严格缩窄承诺。

## 8. 建议 Pro 模型采用的修复顺序

### 阶段 A：先确定不可逆的边界

1. 决策桌面壳与 backend 的 IPC 方案、认证、session/事件协议。
2. 决策 package resource、用户数据目录、秘密存储、ACL、迁移与卸载。
3. 取消 `import app.main` 的运行时副作用，建立统一 app factory/entry point。
4. 在 CI 加入安装产物 smoke，防止继续在不可安装基础上开发。

### 阶段 B：让核心在长运行与断线条件下可靠

1. 消息幂等、事件 seq/replay、有限状态保留。
2. LLM/TTS/audio/VTS 全链路有界队列、超时、退避和 cancellation generation。
3. 修复 history retention、维护 task、删除/清理语义、日志 rotation/redaction。
4. 将可能永久阻塞的 native/OCR/STT 工作隔离到可终止边界。

### 阶段 C：实现一个最小 Windows 垂直切片

1. 托盘/窗口、单实例、安全 IPC、文本对话、退出。
2. 麦克风/whisper 的设备状态、Job Object、console suppression。
3. 音频播放与 VTS capability discovery。
4. 屏幕/窗口/idle/focus/DND 适配器；感知和 proactive 默认关闭，逐项 opt-in。

### 阶段 D：安装、升级与真实环境门禁

1. 标准用户、Program Files、LocalAppData、中文/空格/长路径、非系统盘。
2. 声卡/麦克风/蓝牙/RDP、VTS、whisper、OCR、GPU/CPU、断网与服务冷启动。
3. 崩溃、磁盘满、锁文件、数据库损坏、升级/回滚/卸载。
4. soak、内存上界、队列上界、日志/缓存上界和隐私残留扫描。

## 9. 交给 Pro 模型的完整任务说明

可将下方内容与本文一起发送：

```text
你是本项目的高级架构与 Windows 工程负责人。请基于这份审计报告，为每个 P0、P1、P2 问题提出可执行、相互兼容的解决方案。

要求：
1. 先验证报告中的因果链；若不同意某项，明确说明反证和需要补充的实验，不要静默忽略。
2. 优先给出全局架构决策，尤其是：Windows 桌面壳与 Python backend 的 IPC/认证、资源与用户数据路径、秘密存储/ACL、消息幂等与事件恢复、native worker 隔离、安装/升级模型。
3. 对每个问题输出：根因、推荐方案、备选方案与取舍、影响文件/接口、数据迁移、兼容性、安全/隐私影响、测试与验收条件。
4. 标出问题之间的依赖，生成分阶段实施顺序；不要让后续 UI/安装器建立在即将废弃的协议或路径上。
5. 对 P0/P1 给出最小补丁拆分建议（每个 PR 的范围、前置依赖、回滚方式），不要把全部修改塞入一个 PR。
6. Windows 方案必须覆盖：标准用户、Program Files 只读安装、LocalAppData、中文/空格/长路径、x64/ARM64、Windows ACL/DPAPI、Job Object、无控制台窗口、声卡/麦克风热插拔、文件被占用、杀毒软件干扰、崩溃重启、升级/卸载。
7. 保留现有优点：异步取消、隐私 fail-closed、敏感数据 wipe、mock 可测试性、严格 mypy/Ruff/branch coverage；若需要改变这些机制，解释为何以及如何保持等价保证。
8. 不要只建议“增加 try/except、加 timeout、写更多测试”。必须明确 timeout 后谁拥有资源、如何真正停止 native 工作、如何保证队列/磁盘/内存上界、如何向用户报告部分失败。
9. 给出更新后的 CI 矩阵和发布 gate，包括已安装 wheel/exe smoke、ACL/路径测试、依赖审计、真实设备人工 gate、长稳测试。
10. 最后输出一个风险登记表：问题 ID、修复 PR、负责人角色、预计复杂度、剩余风险、验收证据。

请先产出设计与实施计划，不要直接生成大规模代码补丁；待架构决策确认后再逐 PR 实现。
```

## 10. 审计边界与未获得的证据

- 本次没有真实连接 GPT-SoVITS、VTube Studio、whisper.cpp 模型、Windows 麦克风/声卡、真实 OCR 字体、屏幕捕获或云视觉服务，因此这些部分的“可用性”不能由 mock 测试替代。
- 没有现成 Windows 安装器/exe/MSIX 可测试；wheel 隔离 smoke 已失败。
- GitHub PR #11 没有人工 review 或评论；两个 CI job 成功只证明源码环境的自动门槛。
- 本次没有修改业务代码，只生成审计文档；所有修复建议都应在独立分支/PR 中重新验证。
