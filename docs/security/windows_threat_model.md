# Windows Gate W0 威胁模型

> 版本：2026-08-01
> 状态：项目所有者自审通过；没有独立人工安全/隐私 reviewer
> 范围：Windows 11 x64、标准用户、单交互会话、私人使用
> W30 注记：DeepSeek Flash 的固定文本出口、专用 DPAPI 密钥和提示注入边界已纳入本模型。W19～W30 已按
> PR #34、#35、#33、#32 合入 `agent/windows-development-baseline@83f52e228d8df6df9abf08ce492192752ff113c2`；
> 既有本地 GPT-SoVITS Gateway 仍是独立 loopback/bearer 边界，不能借此扩大 DeepSeek 出站数据。所有者接受
> 已记录的残余风险并授权合并，不等于真实 Key、账号、价格、保留、地域或远端服务行为已获验证。

## 保护目标

- 用户明确发送的对话、近期历史和长期记忆。
- API key、VTS token、DPAPI 密文和 provider 身份。
- DeepSeek 专用 key-id/purpose、固定 endpoint/model/thinking 组合，以及不会把图像、multipart content 或 tool call 送出进程的约束。
- W29 TTS gateway Bearer、私有 manifest、声音权重、参考音频、日语提示和生成 WAV。
- 麦克风 PCM/WAV、指定窗口截图、OCR 文本和脱敏前图像。
- SQLite/WAL、配置、日志、缓存、temp、迁移 backup。
- turn 幂等、计费、播放/VTS generation 和 feature 实际状态。
- Qt、BackendThread、worker、helper protocol、安装/升级边界的完整性和可终止性。

不保护用户主动提交给其所配置外部 provider 后的远端处理本身；本项目仍负责最小化出口、明确启用、TLS、失败状态和不透明重试禁令。

## 攻击者与假设

- 恶意网页、浏览器脚本或本机低权限进程。
- 同机另一 Windows 用户。
- 异常、恶意、过慢或返回超长内容的 LLM/TTS/VTS/vision provider。
- 恶意或被替换的声音 ZIP、权重、公共模型、源码树，以及伪造的 loopback TTS 请求。
- 被提示注入控制的屏幕内容或云视觉返回。
- 卡死/崩溃的 OCR、PortAudio、whisper.cpp 和子进程树。
- 文件锁、磁盘满、杀毒软件、崩溃、半迁移和旧版本程序。
- 误操作的当前用户，包括错误卸载、错误窗口选择或忘记云端已启用。

首版不抵御已完全控制当前用户账户或内核的攻击者，也不承诺 SSD 物理擦除。

## 威胁与控制

| ID | 威胁/失败 | 主要控制 | 验证 | 残余风险 |
| --- | --- | --- | --- | --- |
| TM-W00-01 | 恶意网页调用 loopback API | 生产零端口；数字 loopback listener/真实 peer、dev token、Origin、Host、session/scope、frame/rate limits | W04 TestClient 攻击矩阵、真实 Uvicorn socket smoke、源码 desktop PID 端口扫描；最终 onedir/exe 在 W24/W25 重验 | dev 模式仍扩大攻击面，必须显式启用；同用户进程/内存不在 token 保密边界内 |
| TM-W00-02 | 本机进程伪造 helper/bridge 消息 | 进程内 bridge；继承匿名 pipe；typed/versioned JSON；无 pickle | malformed/replay/flood tests | 当前用户已被控制时不能完全阻止 |
| TM-W00-03 | 另一账户读取 DB/token/temp | current-user DACL + DPAPI current-user | 两账户 effective access 与解密测试 | 管理员/内核攻击不在保证内 |
| TM-W00-04 | 消息重放导致重复计费/播放 | 三元幂等键、有限状态、seq/replay、generation | 并发重复与断线 property tests | 远端在本地取消后可能继续计费 |
| TM-W00-05 | 慢/恶意 provider 耗尽内存磁盘 | byte/token/segment/deadline、queue/temp/log 硬上限、背压 | slow consumer/fault/soak | 合理上限可能截断合法长回复 |
| TM-W00-06 | native thread 卡死且继续持有敏感数据 | worker 进程、Job Object、hard deadline、kill、scavenger；W17 播放 wire 仅允许批准根相对引用；W18 PTT PCM/WAV/JSON 仅在 MediaWorker，有界 ring 不逐帧排入 parent loop，whisper timeout 走 terminate→grace→kill | W12 hanging child/tree tests；W17 fake descriptor/device/cancel/deadline tests；W18 ring/timeout/cancel/cleanup、Windows synthetic whisper child-tree tests；真实驱动体验另列设备 Gate | 强杀不是物理内存/磁盘擦除证明；原生 driver abort 和真实录音指示受设备差异影响 |
| TM-W00-07 | 截图捕获错误窗口或包含敏感内容 | 指定窗口 recheck、Guard、锁屏/权限 fail closed、禁止全屏 fallback | 多显示器/DPI/UWP/管理员/RDP 人工 Gate | OCR/Guard false negative 不可能证明为零 |
| TM-W00-08 | 云端收到未经脱敏截图 | cloud 默认关闭；显式启用；全 OCR bbox 遮挡；最终本地检查；未知即跳过 | sentinel、出口审计、真实敏感窗口 Gate | 本地检测 false negative 和 provider 保留策略 |
| TM-W00-09 | 屏幕/云返回或 W30 脱敏视觉摘要中的提示注入 | 标记 `untrusted`；不当用户命令；不产生长期记忆候选；W30 只在视觉开关、单次同意、新鲜、非敏感同时成立时注入摘要 | prompt injection、gate/freshness/sensitive、candidate-analysis-reject tests | 模型仍可能受内容影响，需要最小权限和 UI 解释 |
| TM-W00-10 | secret 泄露到日志/fixture/artifact | DPAPI、key-aware redaction、allowlist 诊断、sentinel scan；W30 DeepSeek key 使用独立 purpose-bound 槽，不写 YAML/通用槽 | artifact/log/export scan、专用密钥 configure/revoke 与异常/snapshot sentinel tests | 新字段可能绕过脱敏，需 schema review |
| TM-W00-11 | 半迁移、旧版本写新 DB、backup 丢失 | checkpoint/backup、staging verify、原子 switch、兼容矩阵 | 每阶段 fault injection | 文件锁/杀毒软件会延迟恢复 |
| TM-W00-12 | 卸载删除过多或保留 secret | 明确选择；默认保留非秘密数据、删除/revoke secret；pending 报告 | fresh/upgrade/uninstall VM | 用户可能误解保留内容，需清晰文案 |
| TM-W00-13 | 崩溃重启造成 crash loop/后台录音捕获 | crash marker、budget/quarantine、feature actual state reconcile；W18 只有显式 PTT start 才打开输入设备，默认不持续监听 | repeated crash/restart tests；W18 start/cancel/watchdog/worker cleanup tests | 设备/驱动特定问题、真实锁屏和系统级 hotkey 仍需真实环境/后续 adapter |
| TM-W00-14 | 安装包夹带资产、密钥或用户数据 | artifact allowlist、manifest、SBOM、sentinel 和许可证清单 | clean build + artifact scan | 当前无独立许可证 reviewer |
| TM-W00-15 | 未签名私人包被误作公开发布 | 文档标记私人/未签名；公开发布重新 Gate | release checklist | 用户手工转发仍可能产生信任警告 |
| TM-W00-16 | 受管 STT 下载被篡改、ZIP 越界或恶意模型导致 CLI 崩溃 | 固定 HTTPS URL/版本/大小/hash；有限重定向、staging、拒绝 Zip Slip/link/reparse point、原子切换；MediaWorker 首次使用前全量 CLI/model SHA-256，失败不启动 CLI；Job Object 约束子树 | 合成 archive/hash/timeout/cancel/repair、受管/手工路径、worker integrity-fail-before-CLI、wheel asset denylist；真实模型/内存另列设备 Gate | 当前用户完全受控、手工非受管模型和 upstream parser 缺陷不在此控制的保证内；不得宣称上游 issue 已修复 |
| TM-W00-17 | 恶意/脆弱 GPT-SoVITS 服务、声音 ZIP、权重或源码处理导致命令执行、数据泄露、混合权重、路径越界或不可清理进程；预检/hash 被误写成安全认证 | W19 legacy 路径仍不安装或管理用户服务，也不调用有副作用的 `/set_refer_audio`。W29 只导入用户明确确认可信的五包：安全 ZIP、固定扩展/数量、SHA-256、current-user ACL、固定官方提交/公共模型树；推理隔离在仓库外单 worker gateway。HTTP 仅认证 loopback health/TTS，拒绝 Origin/异常 Host/peer/body/JSON/path；单锁事务切换完整 GPT/SoVITS pair，失败回滚，回滚失败 quarantine；启动器 Job Object 收束子树 | W19 fake API v2 route/synthesis、错误 body/path sentinel、deadline/cleanup；W29 ZIP traversal/link/encryption/duplicate、manifest/hash/ACL/source-tree、auth/Host/Origin/body/admission、事务切模/取消/回滚/quarantine、launcher cleanup；真实五槽中文 WAV、20 次切换和 Job cleanup | [GHSL-2025-045～048](https://securitylab.github.com/advisories/GHSL-2025-045_GHSL-2025-048_RVC-Boss_GPT-SoVITS/) 披露命令注入，[GHSL-2025-049～053](https://securitylab.github.com/advisories/GHSL-2025-049_GHSL-2025-053_RVC-Boss_GPT-SoVITS/) 披露不安全反序列化/RCE。用户确认、hash、ACL 和进程隔离不能证明权重语义安全、上游无未知漏洞或当前用户账户受控时 token 仍保密 |
| TM-W00-18 | 多个 VTS writer、迟到 mouth progress 或错误红眼所有权导致旧 turn 重放、取消后重新张嘴、姿态残留或关闭人工状态；progress 泄漏 PCM/路径 | 单写者 AvatarRuntime；urgent bounded queue + latest frame；turn/playback/VTS/model generation；strict finite scalar `job.progress`；全部 terminal path 归零；release/Neutral/cancel 生命周期；`off/manual/system` Expression 所有权；状态不可验证即禁用自动层 | W28 fake VTS/event malformed、slow writer、fake clock/seed、20k frame、10k coalescing、progress flood/stale/terminal、PCM RMS 与 Avatar failure isolation；W29 focused 变体、有序红眼和取消迟到矩阵；真实 VTS lifecycle/reconnect/manual-system、真实输出和 W29 中文播放/cancel | VTS/driver/display latency 和主观自然度仍需真实体验；私有配置 exact-byte guard 有等价 metadata 差异，只能声明 semantic 未改写 |
| TM-W00-19 | 提示注入、截断/重复/超限 JSON 或并行合成乱序使控制字段泄漏，或让 LLM 越权选择声音/动作/路径；取消后迟到段重新播放/触发红眼 | 严格增量 JSON parser 只释放完整 schema 段；重复键、未知字段、非法枚举、空/超限/截断/尾随输入 fail closed；EmotionEngine 最终裁决，本地派生声音/速率/动作，LLM 只建议受限情绪/变体/红眼；红眼绑定有序实际播放，generation 失效拒绝迟到结果；每轮红眼硬上限 | 任意 chunk 边界、malformed/duplicate/unknown/truncated/oversize/control-field non-disclosure、deterministic resplit、TTS out-of-order、取消和已播放/未播放边界；真实播放确认动作/红眼晚于首个 progress | LLM 仍可能在合法正文中产生不合适内容；本地情绪裁决是有界策略而非语义正确性证明，随机动作与台词协调仍需所有者体验 |
| TM-W00-20 | DeepSeek 路由/模型/thinking 漂移、通用密钥混用、图像/tool call/原始感知数据出站、候选写入误用 Flash、或将外部服务承诺为本地保证 | 专用固定 `DeepSeekFlashLLMProvider`；固定 HTTPS endpoint/Flash/disabled thinking；请求 image 或 non-string multipart content 与任一 response choice 的 tool call 本地拒绝；HTTP/SSE/JSON 的 `insufficient_system_resource` 受控为 retryable unavailable；专用 purpose-bound DPAPI 槽的 post-replace 回滚和 write-only UI；桌面不读环境 key；候选分析开启即拒绝配置/组合；视觉 prompt 只接收无 ID 的有限语义标签并拒绝自由文本/URI/path | MockTransport 覆盖固定 header/payload、SSE/JSON/finish/error；fake DPAPI configure/revoke/post-replace rollback 与 sentinel scan；原始 `PerceptionContext`/URL/OCR/title/path 自由文本、视觉 gate/budget/prompt-injection 测试；真实 Key 仅在用户明确触发的非敏感检查中使用 | 本地不能证明账号权限、服务可用性、费用、限流、远端停止、地域、保留或政策；任何用户允许的文本上下文仍会离开设备 |
| TM-W00-21 | 恶意或被抢占的本机 loopback 进程接收 Gateway bearer、伪造健康/WAV 或滥用待合成文本 | 固定数值 `127.0.0.1:9880`、`trust_env=false`、无重定向/代理/自定义 CA/缓存；专用 current-user DPAPI token；请求只含文本、有限 voice slot 与 speed，响应通过协议头和有界 health/WAV 校验 | MockTransport 覆盖固定 URL/Bearer/协议头、health、WAV、超时、取消、错误和 token-slot 隔离 | loopback HTTP 不提供进程身份、TLS 或对同用户恶意代码的机密性保证 |

## 反方审查清单

| 检查 | W0 结论 |
| --- | --- |
| GUI/backend/native 是否存在双重 owner | ADR-W01 指定唯一 owner 和关闭顺序；代码实现待 W12/W13 证明 |
| IPC 是否允许网页、本机进程、错误 session 或重放 | W04 已完成 dev API token/Origin/Host/session/scope 与 frame/rate 攻击套件；幂等/replay 和 helper/bridge 仍待 W06 |
| 幂等、计费、已播放音频是否矛盾 | 重复请求零副作用；已播放内容不回滚/补播；远端计费残余风险显式化 |
| 路径迁移是否覆盖 wheel/onedir/Program Files/LocalAppData | ADR-W03/W08 已覆盖；产物和 VM 证据待 W01/W02/W24/W25 |
| DACL/DPAPI 是否覆盖轮换、换机、卸载 | ADR-W04 覆盖；两账户和卸载证据待 W03/W25 |
| native timeout 是否只是表面 timeout | ADR-W07 采用 Job hard kill，不以 thread timeout 声称停止 |
| 是否仍有无界资源 | 首轮统一硬上限已批准；实现审计待 W06/W07/W11/W12 |
| privacy fallback 是否削弱 fail closed | 禁止全屏 fallback；云端任一未知/错误均跳过 |
| 半迁移版本能否启动 | 只允许 staging verify + atomic switch，失败保留旧版 |
| 验收是否只依赖 Mock | 每个后续 Gate 保留 Windows VM、设备和人工证据栏 |

## 接受的残余风险

1. **RR-W00-01 非独立自审：** 所有人工角色由项目所有者本人兼任。私人开发可以继续，但不能表述为独立安全/隐私或许可证审查；公开发布前必须重新 Gate。
2. **RR-W00-02 云视觉 false negative：** 项目所有者允许本地检查后上传脱敏图像，但本地检测不可能证明零漏检。实际启用必须经过 W21/W22 sentinel、真实敏感窗口和出口审计；失败则保持 cloud disabled。
3. **RR-W00-03 安装器未冻结：** 只批准 per-user、无静默更新、备份回滚约束；具体技术在 W25 前决定。
4. **RR-W00-04 设备声明未具象化：** “基本具备”不是验收证据；W4～W6 前必须登记具体内置/USB/蓝牙声卡、麦克风和多显示器结果。RDP、快速切用户、跨 session 与第二账户有效访问属于当前单机单用户私人范围的范围外项，不能写成通过或待本地人工 Gate。
5. **RR-W00-05 私人未签名：** 当前不需要签名，只能用于私人构建；任何公开分发保持阻塞。
6. **RR-W00-06 whisper.cpp 上游模型解析风险：** [CVE-2026-10298](https://nvd.nist.gov/vuln/detail/CVE-2026-10298)
   的 NVD 记录列出范围至 1.8.2，未把受管的 v1.9.1 列为受影响版本；但
   [issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 在本次复核时仍为 open，不能据此声称 v1.9.1
   已修复。固定 hash 的受管模型减少意外/替换输入，不能替代上游修复、独立审计或对非受管模型的防护。
7. **RR-W00-07 GPT-SoVITS legacy 服务边界：** W19 兼容路径的外部服务仍由用户在应用包外自行部署和
   管理。成功 preflight 只证明一次 API/preset/reference 组合返回了合规 WAV，不证明服务版本没有已知/未知
   漏洞、模型可信、远端不会保留输入，也不提供独立许可证/安全审计。
8. **RR-W00-08 W29 私有模型与固定上游：** W29 改变的是本机受管边界，不是上游安全证明。只有用户明确
   确认可信、私有 manifest 中 SHA-256/ACL/源码树均匹配的五槽可加载；这仍无法排除恶意权重在允许的
   `torch.load(..., weights_only=False)` 中执行代码、未知上游漏洞或受控当前用户修改运行时。任何公开发布、
   新模型、上游提交或依赖升级都必须重新审查来源、许可、advisory、兼容性和资产权利。
9. **RR-W00-09 DeepSeek 远端文本处理与政策：** 启用 W30 后，当前用户文字及已打开的历史、长期记忆检索、合规视觉摘要文本可能到达 DeepSeek。固定模型、TLS、DPAPI 与本地拒绝图像不能控制远端账号、地域、保留、模型改进、计费或政策变化；以 [DeepSeek 隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)为准。
10. **RR-W00-10 本地 Gateway 进程身份与下游：** 固定 loopback、专用 DPAPI token 和响应校验限制了误配置，但 HTTP loopback 不能证明监听端就是预期 Gateway，也不能阻止同用户恶意进程先绑定端口。该风险仅在当前单机、单用户私人范围内被所有者接受；扩大范围前必须重新设计进程所有权并重新 Gate。
