# 给 Pro 模型的提示词

## 第一轮主提示词：架构与修复计划

复制以下内容，并同时上传 `megumin_companion_ai_pro_handoff_d56cfbd.zip`：

```text
你是该项目的首席架构师、Python 异步系统审查者、Windows 桌面工程负责人和隐私安全负责人。

附件 `megumin_companion_ai_pro_handoff_d56cfbd.zip` 包含：
- 提交 d56cfbd 的完整 Git 跟踪源码、测试、配置、迁移、CI 与历史文档；
- docs/pro_handoff/00_read_me_first.md；
- docs/pro_handoff/01_project_overview.md；
- docs/pro_handoff/02_architecture_and_data_flow.md；
- docs/full_code_windows_risk_audit_2026-07-15.md；
- docs/pro_handoff/03_prompt_for_pro.md。

请严格按 00_read_me_first.md 的顺序阅读。当前代码与测试是最终事实；目标架构和旧计划可能包含尚未实现或已过时内容。

本轮目标不是直接重写代码，而是验证审计、完成关键架构决策，并生成可以逐 PR 执行的 Windows 修复计划。

工作要求：

1. 先确认你实际读取了哪些文件，并用不超过 15 条要点复述你理解的产品目标、当前运行拓扑、已实现能力、尚未接线能力、默认隐私边界和 Windows MVP 定义。

2. 逐项审查风险报告中的 4 个 P0、24 个 P1、13 个 P2。为每个问题标记：
   - Confirmed：同意报告因果链；
   - Needs correction：问题存在但根因、范围或严重度需要修正；
   - Rejected：代码证据足以反驳。
   每项必须引用具体文件、符号或测试。若无法确认，写出最小补充实验，不要猜测，也不要静默忽略任何 ID。

3. 优先形成并解释以下 Architecture Decision Records：
   ADR-01 Windows GUI、FastAPI/asyncio 和 native workers 的进程/线程/事件循环拓扑；
   ADR-02 desktop client 与 backend 的安全 IPC，包含认证、Origin、session authorization 和非 loopback 策略；
   ADR-03 package resources、Program Files、LocalAppData、Temp、日志、缓存、用户模型和旧 data/ 的路径/迁移模型；
   ADR-04 Windows DACL、DPAPI/Credential Manager、秘密轮换、卸载保留/删除策略；
   ADR-05 message idempotency、turn state、event sequence/ack/replay、重连与取消竞态；
   ADR-06 LLM→segment→TTS→audio→VTS 的有界背压、输出上限、deadline 和失败降级；
   ADR-07 OCR、PortAudio、whisper.cpp 等可能卡死的 native 工作的进程隔离、hard timeout、终止和敏感 buffer 清理；
   ADR-08 wheel/onedir/installer/升级/回滚/单实例/崩溃恢复与发布模型。

4. 每个 ADR 至少包含：当前问题、推荐方案、一个可行备选、选择理由、拒绝的方案、Windows 特有细节、兼容/迁移影响、安全与隐私影响、失败语义、验收方法。

5. 画出以下目标图：
   - 进程/线程/事件循环与 trust boundary；
   - 显式 text/voice turn 的 sequence；
   - WS 断线重连、幂等与 event replay；
   - app startup/shutdown 和 resource ownership；
   - package resource、用户数据、秘密、缓存、临时数据的目录与迁移流。

6. 对所有外部/原生边界明确回答：timeout 或 cancel 后谁仍拥有资源、工作是否真的停止、若不能停止如何隔离/终止、临时文件和内存怎样清理、用户看到什么状态、后台如何重试、日志记录哪些不敏感字段。

7. 生成问题依赖图和实施阶段。顺序必须优先解决不可逆基础：安全控制面、路径/秘密、app factory/安装产物、幂等与事件协议、有界资源，然后才是 GUI、真实 Windows adapters、安装器和体验扩展。

8. 将方案拆成小型 PR。每个 PR 输出：
   - PR 编号和目标；
   - 覆盖的问题 ID；
   - 前置依赖；
   - 主要影响文件/新接口；
   - schema/config/protocol/data migration；
   - 自动测试、Windows VM 测试、真实设备/人工 gate；
   - 回滚方式；
   - 完成定义；
   - 预计复杂度 S/M/L/XL。
   不要把安全 IPC、路径迁移、GUI 和安装器塞进同一个巨型 PR。

9. Windows 方案必须覆盖：
   - 标准用户与非管理员环境；
   - Program Files 只读安装；
   - LocalAppData/Temp 和用户限定 DACL；
   - 中文、空格、长路径、非系统盘；
   - x64/ARM64 取舍；
   - Windows Job Object、CREATE_NO_WINDOW、子进程树；
   - 麦克风/声卡热插拔、蓝牙、RDP、设备占用；
   - 文件被其他进程/杀毒软件短暂锁定；
   - 崩溃重启、SQLite WAL、升级/回滚/卸载；
   - 单实例、端口/IPC 冲突和防火墙；
   - PyInstaller onedir 资源、Qt/ONNX/PortAudio/whisper.cpp 原生依赖。

10. 保留并验证当前优点：默认离线静默、真实 provider 不伪装 Mock、每 turn 可取消、用户输入优先、音频按序、视觉 fail-closed、敏感 buffer wipe、长期记忆 opt-in、screen/proactive 是 untrusted context、严格 mypy/Ruff/branch coverage。若方案改变任何不变量，必须解释如何提供等价或更强保证。

11. 给出更新后的质量门：
   - source unit/integration/property；
   - installed wheel smoke；
   - frozen onedir/installer smoke；
   - auth/Origin/ACL/DPAPI/path/migration tests；
   - fault injection、slow consumer、queue/memory/disk bounds、soak；
   - dependency audit、SBOM、license、Action SHA pinning；
   - Windows 标准用户 VM；
   - 真实声卡/麦克风/VTS/GPT-SoVITS/whisper/OCR 人工 gate；
   - 隐私残留与网络出口审计。

12. 最终输出以下固定章节：
   A. Executive decision（是否可合并 PR #11、是否可称 Windows baseline）；
   B. 项目理解校验；
   C. 审计问题逐项判定表；
   D. ADR-01～ADR-08；
   E. 目标架构与数据流图；
   F. 分阶段 PR 计划；
   G. CI/Windows/人工验收矩阵；
   H. 数据与协议迁移计划；
   I. 风险登记表；
   J. 需要项目所有者回答的问题，最多 10 个并按阻塞度排序。

禁止：
- 不要只回答“加认证、加 timeout、加测试”；必须给出协议与资源所有权。
- 不要用 CORS 代替 WebSocket Origin/身份验证。
- 不要把 loopback 视为可信边界。
- 不要把 chmod 视为 Windows ACL。
- 不要声称 asyncio wait_for 可以停止卡死的 native thread。
- 不要建议记录原始聊天、截图、OCR、音频或密钥来改善诊断。
- 不要在第一轮输出大规模代码补丁。
- 不要忽略迁移、回滚、标准用户、文件锁和崩溃路径。

如果附件读取不完整，请先列出缺失文件并停止做精确代码结论；不要假装已经读取。
```

## 第二轮提示词：挑战与收敛方案

第一轮回答后，如需让 Pro 自我审查，可使用：

```text
请作为独立的反方架构评审者审查你刚才的方案。

重点寻找：
1. 是否存在 GUI/backend/native worker 的双重 owner 或无法等待的关闭路径；
2. IPC 是否仍允许恶意网页、本机进程、错误 session 或重放消息；
3. 幂等、事件 replay、取消、已播放音频和计费之间是否存在矛盾；
4. 路径迁移是否能同时覆盖源码、wheel、onedir、Program Files、LocalAppData、升级与回滚；
5. Windows ACL/DPAPI 是否有安装、轮换、备份、卸载和多账户语义；
6. native timeout 是否只是表面超时，worker 实际仍持有敏感数据或设备；
7. 是否有任何队列、响应、日志、缓存、状态表或重试没有硬上限；
8. privacy fail-closed 是否因可用性回退被削弱；
9. PR 依赖是否会导致半迁移版本不可启动；
10. 验收是否能在标准用户和真实 Windows 设备上证明，而不是只靠 mock。

输出：发现的问题、严重度、对原 ADR/PR 计划的具体修订，以及最终推荐版本。不要重复原答案。
```

## 第三轮提示词：实现单个 PR

确认总体方案后，每次只选择一个 PR：

```text
现在只实现已确认计划中的 PR <编号/名称>。

约束：
- 不扩张到其他 PR；发现前置问题先报告。
- 开始前重新读取该 PR 涉及的源码、测试和 ADR。
- 先列出将修改的文件、协议/schema/config 变化和迁移/回滚办法。
- 实现后运行与风险成比例的 unit/integration/property、Ruff、format、mypy 和安装/Windows 专项验证。
- 保留项目不变量和隐私边界。
- 最后给出 diff 摘要、验证证据、未验证的真实设备项和剩余风险。
```
