# Windows Gate W0 威胁模型

> 版本：2026-07-30
> 状态：项目所有者自审通过；没有独立人工安全/隐私 reviewer
> 范围：Windows 11 x64、标准用户、单交互会话、私人使用
> W30 注记：DeepSeek Flash 的固定文本出口、专用 DPAPI 密钥和提示注入边界已纳入本模型，且本地完整自动化质量门已通过；
> W30 的提交、Draft PR、最终 head CI、真实 Key 和远端服务行为仍须独立核验。

## 保护目标

- 用户明确发送的对话、近期历史和长期记忆。
- API key、VTS token、DPAPI 密文和 provider 身份。
- DeepSeek 专用 key-id/purpose、固定 endpoint/model/thinking 组合，以及不会把图像、multipart content 或 tool call 送出进程的约束。
- 麦克风 PCM/WAV、指定窗口截图、OCR 文本和脱敏前图像。
- SQLite/WAL、配置、日志、缓存、temp、迁移 backup。
- turn 幂等、计费、播放/VTS generation 和 feature 实际状态。
- Qt、BackendThread、worker、helper protocol、安装/升级边界的完整性和可终止性。

不保护用户主动提交给其所配置外部 provider 后的远端处理本身；本项目仍负责最小化出口、明确启用、TLS、失败状态和不透明重试禁令。

## 攻击者与假设

- 恶意网页、浏览器脚本或本机低权限进程。
- 同机另一 Windows 用户。
- 异常、恶意、过慢或返回超长内容的 LLM/TTS/VTS/vision provider。
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
| TM-W00-17 | 恶意/脆弱 GPT-SoVITS 服务或模型处理导致远端命令执行、数据泄露或错误响应；预检被误写成安全认证 | 应用不安装/打包/启动/升级服务，不调用有副作用的 `/set_refer_audio`；只连接用户显式配置且通过 W08 endpoint/TLS policy 的服务；预检只发送固定短语和已保存 preset/reference，响应受 deadline/bytes/WAV 校验且不播放并清理 | W19 fake API v2 route/synthesis、错误 body/path sentinel、deadline/cleanup 与设置/事件边界测试；真实服务仅做人类体验 Gate | [GHSL-2025-045～048](https://securitylab.github.com/advisories/GHSL-2025-045_GHSL-2025-048_RVC-Boss_GPT-SoVITS/) 披露命令注入，[GHSL-2025-049～053](https://securitylab.github.com/advisories/GHSL-2025-049_GHSL-2025-053_RVC-Boss_GPT-SoVITS/) 披露不安全反序列化/RCE；两组测试 `20250228v3`。服务及模型资产安全不由本应用证明，用户配置的 reference/prompt 会到达该服务 |
| TM-W00-18 | 多个 VTS writer、迟到 mouth progress 或错误红眼所有权导致旧 turn 重放、取消后重新张嘴、姿态残留或关闭人工状态；progress 泄漏 PCM/路径 | 单写者 AvatarRuntime；urgent bounded queue + latest frame；turn/playback/VTS/model generation；strict finite scalar `job.progress`；全部 terminal path 归零；release/Neutral/cancel 生命周期；`off/manual/system` Expression 所有权；状态不可验证即禁用自动层 | W28 fake VTS/event malformed、slow writer、fake clock/seed、20k frame、10k coalescing、progress flood/stale/terminal、PCM RMS 与 Avatar failure isolation；真实 VTS lifecycle/reconnect/manual-system 以及真实输出 silence/ramp/cancel/drain | VTS/driver/display latency 和主观自然度仍需真实体验；真实中文 GPT-SoVITS 当前未配置；私有配置 exact-byte guard 有等价 metadata 差异，只能声明 semantic 未改写 |
| TM-W00-19 | DeepSeek 路由/模型/thinking 漂移、通用密钥混用、图像/tool call/原始感知数据出站、候选写入误用 Flash、或将外部服务承诺为本地保证 | 专用固定 `DeepSeekFlashLLMProvider`；固定 HTTPS endpoint/Flash/disabled thinking；请求 image 或 non-string multipart content 与任一 response choice 的 tool call 本地拒绝；HTTP/SSE/JSON 的 `insufficient_system_resource` 受控为 retryable unavailable；专用 purpose-bound DPAPI 槽的 post-replace 回滚和 write-only UI；桌面不读环境 key；候选分析开启即拒绝配置/组合；视觉 prompt 只接收无 ID 的有限语义标签并拒绝自由文本/URI/path | MockTransport 覆盖固定 header/payload、SSE/JSON/finish/error；fake DPAPI configure/revoke/post-replace rollback 与 sentinel scan；原始 `PerceptionContext`/URL/OCR/title/path 自由文本、视觉 gate/budget/prompt-injection 测试；真实 Key 仅在用户明确触发的非敏感检查中使用 | 本地不能证明账号权限、服务可用性、费用、限流、远端停止、地域、保留或政策；任何用户允许的文本上下文仍会离开设备 |

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
7. **RR-W00-07 GPT-SoVITS 上游与服务边界：** 官方项目仍由用户在应用包外自行部署和管理。W19 的成功 preflight
   只证明一次 API/preset/reference 组合返回了合规 WAV，不证明服务版本没有已知/未知漏洞、模型可信、远端不会保留输入，
   也不提供独立许可证/安全审计。公开发布或改变服务管理边界前必须重新评估上游版本、advisory、依赖和资产许可。
8. **RR-W00-08 DeepSeek 远端文本处理与政策：** 启用 W30 后，当前用户文字及用户已打开的历史、长期记忆检索、
   合规视觉摘要文本可能到达 DeepSeek。固定模型、TLS、DPAPI 与本地拒绝图像并不能控制远端账号、地域、保留、
   模型改进、计费或政策变化；项目不承诺零保留或不用于训练。以
   [DeepSeek 隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)为准，私人使用由所有者
   显式知情选择；公开分发或改变发送字段前必须重新进行隐私、法律、许可与服务条款审查。
