# W07：端到端背压与统一预算

## 范围与非目标

本 PR 只实现 W07：集中硬上限、prompt 输入预算、LLM 输出预算、LLM→TTS→audio
背压、metadata 限制，以及取消后的有界清理。W08 的 provider 完成/EOF/TLS 语义和 W09 的
VTS 重连均不在本 PR 中改变。

## 契约与失败语义

- `limits` 是运行时硬上限的唯一集中配置。启动时拒绝互相矛盾的 queue、provider token、
  单文件音频和总在途音频配置；硬上限只允许调低，不能通过 provider 设置绕过。
- TTS job queue 由 LLM/segment producer 唯一关闭，容量默认 8；ready audio queue 由 TTS
  worker group 在最后一个 worker 退出时唯一关闭，容量默认 4。正常关闭使用有界 sentinel；
  异常/取消由 `TaskGroup` 取消 owner，finally drain 并清理，绝不等待向失去 consumer 的满 queue
  写 sentinel。
- ready-audio slot 在开始合成前取得，在播放/跳过后释放，因此 ordered-playback 的重排字典、
  queue 内结果和正在合成的结果共享同一容量，不形成隐藏无界容器。
- queue 只负责传输，不作为资源所有权账本。取得 ready-audio slot 后的每个结果还登记在同一容量
  约束的 outstanding registry；producer/consumer handoff 或 temp discard 恰逢取消时，最终 settle
  仍从 registry 释放 slot、音频 lease 和临时文件，然后才生成稳定终态报告。
- 音频字节 lease 在合成前按 provider 单文件硬上限预留，播放/跳过/取消后释放。无法取得 lease
  时 producer 等待，形成真实背压；单个结果违反 lease 或总音频时长预算时删除临时文件并产生
  `audio.degraded`，不静默保留或播放。
- LLM 文本以 UTF-8 bytes、segment 数和 provider output-token hard cap 分别限制。达到 byte 或
  segment 上限产生 `assistant.truncated` 与 outcome `terminal=truncated`；这不是 W08 的合法 EOF/
  `[DONE]` 判定，也不伪装成完整成功。
- prompt 始终保留 system policy，current user 始终是最后一条当前指令。各来源先受独立 token
  配额，再受 provider/model-aware 总预算。仍超总预算时固定按 screen → memory/profile → oldest
  history → current user 的顺序降级；system policy 不降级。截断 current user 会在 build report
  中明确记录，不修改持久化用户消息。
- metadata 独立限制为 8 KiB UTF-8 JSON、16 keys、深度 3；dev HTTP body/WS frame 仍在 JSON
  解析前受 64 KiB transport hard cap。超限统一返回 `metadata_limits_exceeded`。
- 每轮自动记录 queue capacity、最大观测深度、producer 阻塞次数/时长、最大音频 lease、清理耗时、
  终态和降级原因。记录只含计数/字节/毫秒/稳定错误码，不含正文、WAV 路径或用户路径。

## 共享面

W07 对 `settings`、`bootstrap`、dev API security/protocol 和 dialogue pipeline 做最小接线；不修改
根目录交接/总体计划状态，不改变 VTS reconnect、远端 TLS 或 provider 完成协议。

## W09 baseline 增量审计

- W07 分支已合并远端 `agent/windows-development-baseline@a8a4fdba2e64919b785393cdc3e0b2fd4ce4146e`。
  该 baseline 包含已受控合并的 W09 VTS generation/preflight；合并提交为
  `5c78b8f5363b3fe7363a96caf59093528fa42948`，未使用 force push。
- 共享 `settings.py` 与 `default_config.yaml` 自动合并：W07 `limits`、启动自检与 prompt/audio 配额
  保留，W09 `ref_audio_scope`、正数 reconnect 配置和非空 hotkey 校验同时保留。
- W07 未修改 W09 的 `begin_turn`/`cancel_turn`/generation-aware `enqueue_expression`、preflight、
  neutral reset、purge/drop 或 reconnect 状态机语义；VTS/TTS 文件变化全部来自已合并 baseline。
- 合并后的共享聚焦回归覆盖 W07、settings/bootstrap、GPT-SoVITS 与 W09 VTS fake-server/client/
  bridge/event-sink，共 `139 passed`。旧 head `7f894f8...` 的审计签字与 exact-head CI 只保留为
  历史证据，不能用于合并当前 head；当前 head 必须重新通过全量与双平台 exact-head CI。

## 回滚

可降低 worker 数、queue 容量、切换 silent playback 或完全串行 TTS；不得回滚为无界 queue、
字符数冒充 token/UTF-8 byte 预算，或取消后遗留 temp/lease。

## 自动证据（Windows，本地修复 revision）

- W07 聚焦 unit/property/integration/fault/WS flood：`24 passed`。覆盖中日英/emoji UTF-8
  property、provider/model estimator、固定 prompt 降级、快 LLM/慢 TTS、阻塞 playback、乱序 TTS、
  queue full、25 轮取消风暴、音频总时长、磁盘失败、metadata 和 WS flood。
- macOS exact-head CI 首次真实运行在 25 轮取消风暴中捕获一个 handoff/temp-discard 时序竞态：
  queue 已归零但报告仍有 9,644 bytes lease。修复后本地把同一测试重复 10 次（250 个取消回合），
  每回合 queue、lease 和 WAV temp 均归零。
- 合并 W09 baseline 后全仓：`803 passed, 2 skipped`；branch coverage `90.25%`。两个 skip 是既有 optional
  RapidOCR/Pillow 环境，不属于 W07。
- Ruff check 通过；Ruff format `180 files already formatted`；strict mypy `175 source files`
  通过。
- installed-wheel smoke 通过：109 wheel members，manifest SHA-256
  `d64982092ff3a9efc5983b5caeac940b62b834d4277dfea07043c627d8eb2282`；源码包隔离、任意
  CWD、CLI/desktop preflight、locked/authenticated ASGI health 和 idempotent chat 全部通过。
- 背压 stress 使用 TTS queue=2、ready audio=1、audio lease=256 KiB：queue 最大深度分别不超过
  2/1，LLM producer 阻塞计数大于 0，最大音频 lease 不超过 256 KiB；每轮结束两条 queue
  final depth=0、audio lease=0、WAV temp=0。默认生产硬上限仍为 8/4/64 MiB。
- 最终 runtime report 自动包含每条 queue capacity/max/final depth、producer block count/ms、
  reorder max depth、output UTF-8 bytes、audio/temp max/final bytes、cleanup ms 和稳定终态原因；
  没有正文或路径字段。

以上 wheel/hash 是本地开发证据，不冒充 GitHub runner 产物；双平台结论只在 Draft PR 的 exact
head checks 完成后记录。

## AI 反方审查

- 全仓 `asyncio.Queue` 只剩 VTS bridge 和 dialogue measured queue，两者构造均显式 `maxsize`。
- dialogue 的 `full_text_parts` 受 64 KiB 限制，segments/metrics list 受 128 限制，TTS jobs 受 8
  限制，audio queue/reorder/outstanding registry 共享 ready slot（默认 4），不存在把背压转移到
  `pending` 或 cleanup registry 的隐藏无界容器。
- 内部 TTS/audio 数据不做 drop-on-full；producer 等待并记录阻塞。W06 subscriber 的不可丢事件/
  snapshot reset 路径没有改动。
- diff 未修改 LLM provider 完成/EOF/TLS、GPT-SoVITS transport 或 VTS reconnect 文件；W08/W09
  仅在范围说明中作为明确非目标出现。

## 兼容性、迁移与残余风险

- GitHub 仓库当前由项目所有者决定公开。项目所有者声明已检查没有隐私数据上传；这只记录为
  owner attestation，不等同于本 PR 独立完成了全 Git 历史安全审计。当前 baseline 无 branch
  protection，仓库 ruleset 为空，因此公开化不提供合并保护；仍严格依赖 Draft PR、exact-head
  checks、expected-head guard 和合并后读回。
- PR 分支、Actions 日志和上传 artifact 可能公开可见。W07 只允许合成 sentinel、无路径 provenance
  和最小诊断证据进入这些表面；真实 secret/token、用户正文、数据库、日志、截图、WAV、模型/角色
  资产和用户路径不得进入 commit、PR、CI 日志或 artifact。

- `limits.version=1` 是新增可选配置段；旧用户设置缺少该段时使用内置默认值，不写回或重写用户数据。
- prompt 从字符池切换为保守 token 估算，长输入可能更早降级；current user 的持久化原文不修改，
  只截断本次 provider request，并通过 build report 标记。
- estimator 是离线、provider/model-aware 的保守 profile，不等同 provider 官方 tokenizer 的精确
  计费值；provider output 仍由请求 `max_tokens` 硬限制。
- 本地取消会 close/停止消费，但远端 provider 可能继续计算/计费；不作远端撤销承诺。
- fake blocked-playback 与 Windows 本地 pipeline 证明 asyncio 背压/取消；真实 PortAudio/native
  hard-kill 与设备级 hang 隔离属于 W17，不在 W07 冒充完成。
- 截断文案采用稳定 `assistant.truncated` + `reason=output_bytes|output_segments`；音频预算采用
  `audio.degraded`，文字继续。固定 prompt 降级顺序为 screen → memory/profile → oldest history →
  current user，system policy 永不降级。
