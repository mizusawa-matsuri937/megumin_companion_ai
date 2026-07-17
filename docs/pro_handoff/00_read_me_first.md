# Pro 模型交接包：先读这里

## 1. 交付目标

这套材料用于让 Pro 模型先理解完整项目，再对全仓库审计中登记的 **4 个 P0、24 个 P1、13 个 P2** 给出统一、可实施的 Windows 修复方案。

不要直接要求 Pro 一次生成所有代码。第一轮应先完成架构决策、问题验证、依赖排序、PR 拆分和验收设计；确认方向后再逐 PR 实现。

## 2. 推荐上传方式

### 方式 A：推荐

上传：

1. `megumin_companion_ai_pro_handoff_d56cfbd.zip`
2. 将 `03_prompt_for_pro.md` 中的“第一轮主提示词”复制到对话框

ZIP 包含提交 `d56cfbd` 的全部 Git 跟踪源码、测试、配置和已有文档，以及本轮新增的概览、架构说明、风险审计和提示词。ZIP 不包含 `.git`、本地 `.env`、`data/` 运行数据、缓存、虚拟环境、构建产物或其他未跟踪文件。

### 方式 B：Pro 无法可靠读取 ZIP 时

向 Pro 提供仓库访问权限或源码后，再单独上传以下四份 Markdown：

1. `01_project_overview.md`
2. `02_architecture_and_data_flow.md`
3. `docs/full_code_windows_risk_audit_2026-07-15.md`
4. `03_prompt_for_pro.md`

如果不能提供源码，Pro 仍可做架构方案，但不得让它声称已经验证具体代码或给出精确补丁。

## 3. Pro 的阅读顺序

1. 本文件：理解任务和材料权威级别。
2. `01_project_overview.md`：建立产品、范围和成熟度心智模型。
3. `02_architecture_and_data_flow.md`：理解当前运行时、数据流、所有权和未接线模块。
4. `docs/full_code_windows_risk_audit_2026-07-15.md`：逐项读取 P0/P1/P2、复现证据和验收要求。
5. 当前源码与测试：验证审计因果链并确定准确影响面。
6. `docs/windows_development_plan.md` 第 1.3 节及 `docs/decisions/w00_owner_decisions.md`：核对当前隐私、资产、产品边界和人工决定。
7. 其他历史/计划文档：用于背景参考，不得覆盖当前代码事实。

## 4. 信息权威顺序

材料冲突时按以下顺序判断：

1. 提交 `d56cfbd` 的当前代码、schema、迁移和测试。
2. 已复现命令及结果。
3. `full_code_windows_risk_audit_2026-07-15.md`。
4. 本轮项目概览与架构数据流说明。
5. `windows_development_plan.md` 第 1.3 节、`decisions/w00_owner_decisions.md` 与 `gates/gate_w0.md` 中的产品/隐私/资产边界和 Gate 状态。
6. 旧的实现报告、每日计划和目标架构文档。

旧文档中可能同时存在理想设计、历史状态和已过时进度。Pro 必须明确区分“当前实现”“计划实现”和“建议实现”。

## 5. 第一轮期望输出

Pro 第一轮应交付：

- 对每个 P0/P1/P2 的同意、修正或反证。
- 一张全局目标架构图和关键 ADR（Architecture Decision Records）。
- 安全 IPC、Windows 路径/ACL/DPAPI、幂等/事件恢复、native worker 隔离、打包升级五个核心设计。
- 问题依赖图和分阶段实施顺序。
- 小而可审查的 PR 列表，每个 PR 有范围、依赖、迁移、回滚和验收。
- 更新后的自动 CI、Windows VM/设备、人工隐私/体验 gate。
- 风险登记表和仍需项目所有者决定的问题。

## 6. 不接受的回答模式

- 只按文件罗列零散修改，没有全局协议、路径和生命周期设计。
- 用“加 try/except”“加 timeout”“增加测试”代替资源所有权和失败语义。
- 建议先完成 UI，再处理认证、幂等和用户数据目录。
- 将 loopback 等同于无需认证，或用 CORS 代替 WebSocket Origin/身份验证。
- 把 POSIX `chmod` 当成 Windows ACL 证据。
- 把 `asyncio.wait_for` 当成可以停止卡死 native thread 的方案。
- 建议把 screenshot、OCR、聊天、API key、声音模型或受保护资产加入日志、fixture 或发布包。
- 因为自动化测试全绿就宣称 Windows MVP 已完成。
- 一次输出不可审查的大规模重写，缺少迁移和回滚。

## 7. 隐私提醒

只上传生成的交接 ZIP 或 Git 跟踪文件。不要额外上传本机 `.env`、`data/`、SQLite/WAL、日志、截图、录音、TTS 缓存、VTS token、模型、参考音频、Live2D 资产或真实用户对话。
