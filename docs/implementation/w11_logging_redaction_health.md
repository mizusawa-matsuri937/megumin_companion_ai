# W11：有界日志、key-aware 脱敏、健康模型与诊断包

> 基线：`7eb82f14fe1be4395b8e73373c580dde334f8863`（W05 merge commit）
> 分支：`codex/w11-logging-redaction-health`
> 风险：P1-11、P1-12、P2-01、P2-13
> Gate：**OPEN；等待项目所有者明确回复“W11 审计合格”**

## 已实现范围

### 日志边界

- 逻辑日志路径仍由 `AppPaths.logs` 管理，实际文件固定在当前用户 LocalAppData 的
  `MeguminCompanion/logs` 子树；显式 override 离开该目录会失败。
- 每个进程拥有独立文件：`<stem>.<process-role>.<pid>.jsonl`。同一进程的 handler 在重新配置前先关闭，
  不允许多个进程抢同一个 `FileHandler`。
- 默认与可配置上限均为：每文件 `10 MiB`、每进程 lineage 共 `5` 个文件（active + 4 archive）、
  保留 `14` 天；配置只允许调低，不允许突破这三个硬上限。
- 轮转按 UTF-8 实际字节数判断，不按字符数估算。每条 JSONL 记录自身也有 64 KiB 上限，避免单条记录
  穿透文件上限。
- 体积和时间是两套同时执行的条件：启动/配置和 rollover 会做 lineage 清理；每次 emit 会检查当前 active
  文件的首条记录年龄；handler 自有 retention 线程最多每 60 秒巡检全部 lineage。`close()` 会通知线程退出并
  最多等待 1 秒。
- crash summary 独立使用 `crash.<role>.<pid>.<time>.json`，全局最多 5 份且最多 14 天。
- 创建日志或 crash 文件前再次执行当前平台私有目录安全策略；Windows 使用已批准的当前用户限定 DACL。
  多进程首次并发创建同一受管子目录时，loser 会重新 `lstat` 并验证 winner 是普通目录，不把
  `FileExistsError` 当作失败或跳过 reparse 检查。

### 脱敏与安全序列化

- `Redactor` 对 mapping 的 key 与 value 同时执行策略；credential、session、正文、OCR、路径、截图、WAV
  等敏感 key 本身统一替换为固定 `redacted_key`，不再只递归 value。
- value 继续覆盖 known secret、Bearer/OpenAI/GitHub/AWS/Google token、邮箱、手机号、验证码、身份证和卡号。
- 递归深度、节点数、字符串长度和最终记录字节数均有上限；cycle、bytes、NaN/Inf、异常 mapping/iterator、
  恶意 `__str__`/`__repr__` 都转成固定标记，不调用 `default=str`，不输出 traceback 或 exception message。
- 日志 structured fields 使用精确 allowlist：稳定 code、版本、boolean、已枚举的 queue/count/latency 与 fingerprint；
  `turn_id`、`message_id`、`session_id` 在输出前转为 SHA-256 截断 fingerprint。未知正文和用户路径直接丢弃；
  若检测到脱敏，只留下 `redaction_applied=[REDACTED]`。
- 非稳定自由文本 logger message 统一写成 `logging.unstructured_message`；格式化失败只写稳定
  `log_serialization_failed` error code。

### 健康模型

- 新端点：`/health/live`、`/health/ready`、`/health/capabilities`；旧 `/health` 仅作为 readiness 兼容别名。
- liveness 不调用任何外部 provider；readiness 只调用声明为 required 的 provider；capabilities 调用全部已注册
  provider。单 provider 有 250 ms 上限，timeout/exception/非法字段只映射为稳定 error code。
- response schema 严格 `extra=forbid`，component 只能返回 `status` 与 `error_code`；聚合层冻结 provider 名称及
  readiness 角色，运行时篡改不会进入响应。
- 当前只注册真实存在的 `core` provider。没有虚构 W10 database maintenance 或 W12 worker/device 状态；
  后续组件必须实现通用 `HealthProvider` protocol 后才会出现在 health 输出中。

### 显式诊断包

- 只在用户执行 `megumin-companion-api --export-diagnostics ZIP_FILE` 时导出；不会后台自动上传或导出。
- 先在私有 temp 中生成 manifest v1，再对 manifest、原始候选、sanitized entries 和最终 ZIP 的逐个 member
  执行 privacy scan。
  `manifest.json` 固定为 ZIP 第一个 member。
- 默认候选仅为当前配置的 process-owned JSONL lineage 与 allowlisted crash summaries。DB/WAL、截图、图片、
  WAV/PCM、cache、temp 资产和原文件名均不进入 ZIP。
- archive member 使用 `logs/log-NNNN.jsonl` / `crash/crash-NNNN.json`；manifest 只含 member 名、字节数、
  SHA-256、固定 exclusion/scan policy，不含用户 filesystem path。
- 目录枚举、文件数、单文件、总字节、单行均有上限；symlink/reparse、非 regular file、TOCTOU identity/size
  变化、非法 UTF-8/JSON/字段/路径、known secret、正文 sentinel 或强 token pattern 都 fail closed。
- ZIP 使用同目录随机 `.partial` 写入，复读验证并计算指纹后才 `os.replace`；任何替换前失败都会删除 partial
  与私有 staging，替换成功后不再执行可能失败的读取步骤。
- crash summary 只含 schema/timestamp/level/logger/event/error code，完全不检查异常 `repr`、正文或 traceback。

## 配置、兼容性与迁移

`logging` 新增：

```yaml
max_bytes: 10485760
file_count: 5
retention_days: 14
```

设置 schema 仍为 v1：旧 user settings 通过 package defaults 获得三项默认值，不做磁盘静默写回；现有
`file_path` 仍兼容，但实际文件名增加 role/PID。日志 schema 现在显式为 v1。诊断 manifest 独立为 v1。

## 与并行 W06/W10 的共享文件

2026-07-18 只读检查时没有远端 W06/W10 open PR 或分支；以下仍按用户要求视为潜在共享面：

| 文件 | W11 的局部语义 | 合并纪律 |
| --- | --- | --- |
| `app/api/routes.py` | 仅 health helper/四个 health route | 不改 chat/replay/debug/memory route 语义；W06 若改 router，逐 hunk 重审。 |
| `app/main.py` | health provider 注入、core 注册、content-free crash summary、handler close | 不改 turn/replay 生命周期；W10 若提供真实 health provider，增量重审。 |
| `app/config/settings.py` | 仅 `LoggingConfig` 三个上限 | 不替 W06/W10 决定其他 settings schema。 |
| `app/windows_security.py` | 仅修复受管子目录首次创建的多进程 race，并在 race 后重验类型/reparse | 不改变 W04 DACL/DPAPI/卸载策略。 |
| `app/resources/default_config.yaml`、`config.yaml` | 仅 logging 三项默认值 | 若其他分支改 config，保留各自独立 section。 |
| `tests/integration/test_api.py`、`tests/integration/test_dev_api_security.py` | 只适配 process-owned 日志文件名 | 不改既有 W04 security 断言。 |
| `tests/e2e/test_ai_backend_resilience.py` | 只适配 process-owned 日志文件名并显式关闭 handler | 不改 resilience/turn 语义。 |

这里没有擅自实现或解释 W06 的 idempotency/replay，也没有读取不存在的 W10/W12 私有状态。

## 自动证据

- 测试先行证据：新增测试在旧 baseline 上因缺少 `app.health` / `app.diagnostics` 明确 collection failure，
  实现后聚焦 property/fault/integration matrix 转绿。
- 聚焦覆盖：随机嵌套 key/value、Unicode、cycle、恶意 repr/exception、UTF-8 字节轮转、单文件模式、
  14 天 active/inactive 清理、并发 process writer、字段 allowlist、provider timeout/exception/非法输出、
  malicious diagnostic file/reparse/JSON/secret/body/path、ZIP/manifest/hash、失败清理和自动 crash report。
- 2026-07-18 本地 Windows 质量门：`ruff check .` 通过、`ruff format --check .` 通过（169 files）、
  strict `mypy` 通过（164 source files）、`pytest` 为 722 passed / 2 optional-dependency skipped，实际总覆盖率
  90.35%（门槛 90%）。Windows 首次建目录的双进程 race 在修复后额外连续运行 10 次，10/10 通过。
- 2026-07-18 installed-wheel smoke：从 `megumin_companion_ai-0.1.0-py3-none-any.whl` 创建隔离环境，临时隐藏
  source packages 后仍通过 API help/config、desktop preflight 和 authenticated/locked health；
  `source_tree_imported=false`。wheel SHA-256 为
  `6d32dd82f649cdc66ec69d9ab310e179c32dbe82061723f6a60c4f344d7324c9`，manifest SHA-256 为
  `0e985f9fdfa64d56b207fd37a5ba0b630f3974c5e0651d136815fd8e244d223e`。该 wheel 仅为本地验证产物，
  不上传为 release artifact。
- GitHub `quality` 与 `installed-wheel` 的 Windows/macOS 四项证据以 Draft PR 最终 head 的 check URLs 为准，
  避免为回填动态 URL 改写已测试 head；任一项未成功时不得提交人工 Gate。人工 Gate 在全部自动检查通过后仍保持
  OPEN。

## 人工审计方案（Gate W11）

所有检查只使用合成值；禁止真实 API key、真实对话、真实截图或真实音频进入审计目录。

建议合成 sentinel：

- secret：`sk-W11-AUDIT-SECRET-000000000000`
- 正文：`W11-AUDIT-BODY-爆裂魔法-0001`
- 路径：`C:\Users\W11-AUDIT-USER\Private Draft\body.txt`

### 1. 实际轮转文件与 14 天清理

1. 把 `LOCALAPPDATA` 临时重定向到一个全新、绝对、含空格/Unicode 的审计根；只使用 package default
   `10 MiB / 5 / 14 days`，连续写入超过 60 MiB 的 allowlisted synthetic numeric/code 日志。
2. 记录 `logs` 目录的文件名、PID、实际字节数、mtime 和 SHA-256；确认只有
   `app.main.<pid>.jsonl` 与 `.1`～`.4`，每个文件均不超过 10,485,760 bytes。
3. 复制一份合法 rotated file，使用 synthetic dead PID 命名并把 mtime 改成 15 天前；重新配置 logger，确认
   它被删除。再构造当前 PID、首条 timestamp 为 15 天前的 active file，触发一次 emit，确认旧记录消失。
4. 保持进程运行，确认存在且只有一个 `log-retention-<pid>` daemon thread；关闭 handler 后确认线程在 1 秒
   内退出。可把 active deadline 调到审计窗口内，观察无新 emit 时也执行清理。

预期证据：目录列表、逐文件 byte size/hash/mtime、清理前后 diff、线程列表和 close 时延。任一 lineage 超过
5 文件、任一文件超过 10 MiB、15 天旧内容仍可读或线程无法在 deadline 内结束，均拒绝。

### 2. 多进程归属

1. 同时启动两个 synthetic writer，使用同一 logical `app.jsonl`、相同 role、不同 PID，各写至少 1,000 条。
2. 确认得到两个不同 base filename；逐行解析 JSON，两个文件均完整、无交叉截断或共享 archive。
3. 使用 Process Explorer/handle 查询或等价 Windows 工具确认每个 PID 只持有自己的文件 handle。

预期证据：PID→filename→handle 映射、两份 JSON parse 结果、退出后 handle 释放。发现两个 PID 打开同一路径、
JSON 损坏或 archive 归属混淆即拒绝。

### 3. 健康字段 allowlist

1. 用已认证 loopback dev API 分别读取 `/health/live`、`/health/ready`、`/health/capabilities` 和兼容 `/health`。
2. 注入 synthetic required/optional providers，覆盖 ready/degraded/unavailable、timeout、exception 和恶意返回
   `{path, message, secret}`。
3. 确认 live 不调用 provider；required failure 令 ready 返回 503；optional degraded 不令 ready 失败；所有
   component 仅有 `name/status/error_code`。
4. 确认当前输出没有 database-maintenance、worker、device 等未实现状态；W10/W12 接口落地后必须增量重审。

预期证据：四个原始 JSON、HTTP status、provider call count。出现路径、正文、exception repr、额外字段、错误
readiness 传播或虚构 capability 即拒绝。

### 4. crash report 与诊断 ZIP 实物

1. 触发包含 secret/body/path sentinel 且 `__str__`/`__repr__` 抛异常的 synthetic startup crash。
2. 检查 crash JSON 仅有 `schema_version/timestamp/level/logger/event/error_code`；连续生成 7 份并放置一份
   15 天旧文件，确认最终只留 5 份且旧文件删除。
3. 在 app root 放置带 sentinel 的 DB/WAL、PNG、WAV、普通 temp/cache 文件；日志使用正常 W11 logger 生成。
4. 显式执行 `--export-diagnostics`，保存 ZIP 实物；确认 `manifest.json` 是第一个 member，逐 member 重算
   size/SHA-256，并确认 archive member 无绝对路径、`..`、原 PID filename 或用户路径。
5. 分别加入 matching-name 的非法 UTF-8、非法 JSON、reparse/non-regular file、known secret、正文 sentinel 和
   用户路径；每次导出必须失败，final ZIP、`.partial` 和私有 staging 都不得残留。
6. 对原始 logs/crash、解压后的 manifest/JSONL/JSON 和 ZIP member bytes 执行大小写敏感及不敏感扫描：
   secret sentinel、正文 sentinel、`W11-AUDIT-USER`、`C:\Users\`、`/Users/`、`/home/`、Bearer/token patterns
   均不得命中。

预期证据：crash JSON、ZIP 二进制 SHA-256、`unzip -l`/等价 member 清单、manifest hash 复算、全目录 sentinel
扫描输出、每个 fault case 的稳定 error code 和零残留截图。任何 sentinel/path/DB/image/WAV 泄漏、manifest
不是首项、hash 不符、恶意源被静默接受或失败残留均拒绝。

## 残余风险

- 日志 5-file 上限按**每个 process lineage**执行；总体上限还依赖 W12 最终进程数。W12 接入前不得声称已有
  worker 总量或 worker health 证据。
- retention 清理是 best effort；文件锁、权限变化或磁盘故障可能阻止删除。实现不会无限阻塞退出，也不会
  输出路径，但目前没有 W12/W25 的 cleanup-pending UI。
- provider protocol 能隔离异常和超时，但不等于真实 provider/VTS/device preflight；对应真实设备证据仍属于
  W09/W19/W17 及后续 Gate。
- 诊断导出不包含 DB，因此不能用于数据库内容恢复；它是隐私优先的运行摘要，不是完整 forensic dump。
- 项目所有者兼任 reviewer 仍属于非独立审计；若要求独立隐私签字，需另行安排 reviewer。

## 回滚

- 回滚代码时可恢复上一个 log schema reader 和 `/health` readiness 兼容行为，但**不得回滚 key-aware/value
  脱敏规则或重新允许明文 secret/正文/用户路径**。
- 新 process-owned JSONL 与旧 `app.jsonl` 可并存；回滚版本只读自己识别的旧文件，不合并或重写新日志。
- 诊断 manifest v1 没有 DB migration；停用 exporter 只需撤下显式 CLI action，已导出的 ZIP 由用户自行保留/
  删除。
- 若 W06/W10 合并导致 route/settings/health provider 接口变化，先回到最新 baseline 重放 W11，逐共享 hunk
  重审并重跑全部门禁；不得用冲突解决顺手改变另一分支语义。

## Gate 状态

自动验证通过不关闭 Gate。只有项目所有者明确回复 **“W11 审计合格”** 后，才可记录关闭结论；本分支不会
自行合并，也不会开始 W12。
