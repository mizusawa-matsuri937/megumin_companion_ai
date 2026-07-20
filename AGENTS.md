# 项目 Agent 工作入口

> 本文件是仓库级的**强约束和文档导航入口**，不是完整设计文档的副本。完整资料按类型保存在
> [`docs/`](docs/README.md)。开始工作前先读本文件；随后按下方“启动与路由”读取最小且足够的资料集。

## 首要强制约束

### 上下文压缩与状态恢复

- **发生上下文压缩后的恢复优先级最高。** 若本轮历史已被压缩、只剩摘要，或明显从先前状态恢复，回复、推理、改文件或调用工具前，必须先完整读取 [`.agents/CONTEXT_MEMORY.md`](.agents/CONTEXT_MEMORY.md)；若不存在，先依据工作区、Git 与保留摘要重建它，再继续。
- 预计即将压缩时，必须先更新该文件。它记录的是本次会话的可恢复工作状态；它不是需求、设计、验收结果或当前产品目标的权威来源。
- 可提交、可长期检索的当前产品目标只放在 [`docs/current/CURRENT_GOAL.md`](docs/current/CURRENT_GOAL.md)。本地 `.agents/CONTEXT_MEMORY.md` 可能包含短期工作细节，默认不入 Git。

### AI 优先审计

- **先由 AI 完成所有能够可靠自动化、观察或复现的审计。** 人工只承担 AI 无法忠实验证的复杂视觉/交互体验、真实硬件或操作系统行为、真实 IME 候选交互、屏幕阅读器听感、账号/法律/所有者审批等事项。
- 不得因为旧清单写着“人工”就跳过机器可验证部分；必须拆分客观断言和不可自动化断言，先记录 AI 已完成的证据。
- Mock、headless、模拟输入、强制缩放或模拟器只证明对应模拟条件，不能冒充真实设备、真实多屏、真实 IME 或实际无障碍体验。
- 发现客观失败时，必须明确记录失败和证据；不得把同一断言转交人工“确认”来掩盖失败。

### 事实、范围与隐私边界

- 先检查用户前提、逻辑跳跃和缺失信息。事实、合理推测、个人建议和未验证信息必须明确区分；不能核实就直接说明。
- 最新用户指令、当前工作区和已验证证据优先于历史文档。不要为了顺从而沿用过时结论。
- 不得把受保护角色资产、声音数据、图片、模型、真实对话、截图、录音、令牌、密钥或本机私密数据加入仓库、fixture、日志或文档。
- 用户只要求计划、架构、审计或文档时，不擅自写产品代码。工作树已有未关联改动时，必须保留并隔离它们。

## 启动与文档路由

### 每个新任务的最低阅读集

1. 完整阅读本文件；先执行上方的压缩恢复规则（如适用）。
2. 检查 `git status --short --branch`，识别未关联改动、分支和基线；不要把它们混入当前提交。
3. 阅读 [`docs/README.md`](docs/README.md)、[`docs/current/CURRENT_GOAL.md`](docs/current/CURRENT_GOAL.md) 和 [`docs/standards/AGENT_OPERATING_CONSTRAINTS.md`](docs/standards/AGENT_OPERATING_CONSTRAINTS.md)。
4. 依任务类型读取下表指定资料，再读取相关代码、测试、配置和最近 Git/CI 证据。不要把历史报告误写成当前事实。

| 任务类型 | 必读资料 |
| --- | --- |
| Wxx 实现、修复或发布 | [`docs/plans/README.md`](docs/plans/README.md)、对应 Wxx 段落、相关 ADR、已有实现记录和测试 |
| 架构、协议、数据流或隐私设计 | [`docs/architecture/README.md`](docs/architecture/README.md)、[`docs/adr/README.md`](docs/adr/README.md)、[`docs/decisions/`](docs/decisions/)、[`docs/security/`](docs/security/) |
| 审计、验收、风险或发布判断 | [`docs/standards/`](docs/standards/)、[`docs/gates/`](docs/gates/)、相关实现记录、目标提交上的自动化证据 |
| 排障或复盘 | [`docs/experience/`](docs/experience/)、相关实现/审计记录、当前代码和测试 |
| 新会话的历史背景 | [`docs/pro_handoff/00_read_me_first.md`](docs/pro_handoff/00_read_me_first.md)，但先按本表建立当前状态 |

文档索引中的“权威级别”和“已过时/历史”标记是强制语义。根目录 [`HANDOFF.md`](HANDOFF.md) 仅是历史交接材料，不能覆盖当前目标快照。

## 文档体系与维护规则

- [`docs/README.md`](docs/README.md) 是文档目录；新文档必须放到它规定的分类中，并在索引中登记。
- 当前目标、当前阶段、PR/Gate 状态或下一步发生实质变化时，更新 `docs/current/CURRENT_GOAL.md`，并标明核验日期、证据和未验证项。
- 架构或边界决策必须更新对应 ADR、决策记录、威胁模型/数据流（若受影响）和执行计划；不能只更新一份说明。
- 每个 Wxx 结束时，补充/更新对应 `docs/implementation/wxx_*.md` 的范围、验证、残余风险、人工 Gate 与发布状态。跨任务可复用的教训沉淀到 `docs/experience/`，不要只留在聊天记录或临时 handoff。
- 文档中的“完成”“通过”“已合并”必须附可复核的提交、命令、CI 或人工批准证据；计划和猜测不得写成既成事实。

## Superpowers Skill Usage

本项目不默认使用 `superpowers` 技能。只有用户明确要求“使用/调用 superpowers”或指定某个
`superpowers` 技能时才使用；普通项目问答、文档、目录、规划和实现工作直接使用项目上下文。

## W 任务的发布与合并

### W Task Pull Request Delivery

- 每个 `Wxx` 任务都把发布视为完成定义的一部分。
- 通过所需检查后，只暂存该任务的预期改动，创建聚焦提交，推送分支，并创建或更新 Draft PR，才能报告交付。
- PR 标题使用 `Wxx` 标识；当前 Windows 开发计划优先于旧计划或旧 PR 描述。
- 完成报告必须包含 PR URL、验证证据、剩余人工 Gate 和任何发布阻塞。
- 未获明确授权，不得将无关用户改动放入 Wxx PR。
- TTL、保留、replay、幂等 fixture 不得使用会随现实日期老化的固定时间。代码读取 wall time 时注入共享可控时钟；故意使用系统时钟的集成面则在运行前即时生成 UTC fixture。

### Guarded Merge Without Platform Enforcement

- 所有者接受缺失 branch protection/ruleset Gate，不等于 PR 已可安全合并。
- 先记录被接受的残余风险并推送关闭记录；等待最终 head 的全部必需检查，再重读 PR base/head、diff、review、conversation、mergeability 和 draft 状态。
- 仅用 `--match-head-commit` 等 expected-head guard 合并；head/base/diff 或检查发生变化即中止并重新审计。
- 合并后必须从远端确认 PR 状态和 merge commit；然后清理本地基线、`fetch`/`--ff-only` 同步、同步锁定依赖，并在最终 HEAD 跑完整测试。
- 合并后若旧绿测失败，先比较 merge commit 与审计 head 的 tree ID，并在两个 worktree 重跑同一聚焦测试，区分真实合并差异和时间/依赖/权限环境漂移。

## 上下文压缩记忆协议（强制）

统一记忆文件为 [`.agents/CONTEXT_MEMORY.md`](.agents/CONTEXT_MEMORY.md)。它只负责恢复进行中的
会话状态；正式需求、设计、验收结果和当前产品目标仍以 `docs/`、代码和用户最新指令为准。

### 压缩前：先写记忆，再继续工作

当系统或应用提示即将压缩上下文，或代理判断上下文已经很长、下一步很可能触发压缩时，必须暂停其他工作，
立即更新 `.agents/CONTEXT_MEMORY.md`，不得只依赖聊天历史或自动生成的压缩摘要。

更新时先读取已有记忆，再将其整理为简洁、无冲突的“当前状态快照”。至少记录：

- 最后更新时间与时区；
- 当前唯一主要目标及验收标准；
- 用户已确认的要求、限制和禁止事项；
- 已完成工作、完成度与可核验的证据；
- 正在进行的工作和准确停点；
- 未完成事项及按顺序排列的下一步；
- 关键决定、理由和被否决方案；
- 已修改或新增文件；
- 已运行检查、测试或命令及结果；
- 阻塞项、风险、待确认问题和任何不能验证的信息。

记忆必须区分已确认事实、合理推测和未验证信息；不得把计划写成已完成，也不得记录密码、令牌或其他秘密。

### 压缩后：先读记忆，再做任何事

一旦发现本轮历史已经被压缩、收到的是压缩摘要，或上下文显然从先前状态恢复，必须在回复用户、继续推理、
修改文件或调用其他工具之前完整读取 `.agents/CONTEXT_MEMORY.md`。

读取后必须将记忆与压缩摘要、当前工作区、相关文档及 Git 状态交叉核对。工作区实际内容和用户最新指令
优先于可能过时的记忆；从准确停点继续，不重做已经完成的工作。

如果压缩前没有机会写入，压缩后的第一步仍是读取现有记忆；随后依据保留摘要和工作区证据立即补全它。
若该文件缺失或无法读取，先根据可用证据重建，并明确标注无法验证的部分。

### 日常维护

- 每次主要目标、完成状态、阻塞项或下一步发生实质变化时同步更新记忆。
- 每次压缩循环都应满足“压缩前写入、压缩后读取”；普通进度回复不能替代文件落盘。
- `.agents/CONTEXT_MEMORY.md` 是本地运行态，不代替正式文档；正式的可共享状态同步到 `docs/current/` 和相关记录。
