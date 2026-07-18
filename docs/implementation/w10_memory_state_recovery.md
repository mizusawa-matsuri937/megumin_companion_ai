# W10：记忆状态、删除与恢复

> 基线：`2f9df7e30a5927d797d32c1236a152358e24310c`（W06/W11 已合并）
>
> 分支：`codex/w10-memory-state-recovery`
>
> 数据库基线：W06 schema v2；W10 仅追加 schema v3，未改写或复制 v1/v2 migration
>
> Gate：OPEN；Draft PR 不得自行合并，等待“W10 审计合格”

## 已实现范围

- `recent_history` disabled 后停止业务读写，但 7 天到期清理继续运行。
- maintenance 将 confirmation、history retention 和 physical cleanup 分成独立周期；单项异常不会终止后续项目。失败使用 60–900 秒有界指数退避，并输出稳定健康码。
- logical delete 与 physical cleanup 分离：删除事务同时移除业务可见数据并建立不含正文的 cleanup job；API 返回 `logical_deleted`、`cleanup_id`、`cleanup_pending`，客户端可查询 retry/completed 状态。
- 记忆来源收敛为最多 512 字符的 evidence quote、完整显式来源的 SHA-256 和枚举 provenance。旧 source 在 v3 迁移时被替换为可验证的最小证据或固定 redaction 标记。
- FTS 只索引 active memory 的 `content`/`canonical_key`；supersede/delete 同步删除索引。export 仅返回最小 evidence 字段。
- feature 使用 `desired_state`、`actual_state`、`generation`、`reason_code` 单一状态机；generation CAS 拒绝陈旧完成，启动时把中断 transition 显式恢复为 failed。
- migration 前执行 WAL checkpoint 和可校验 sibling backup；future schema 零写入进入 safe read-only，业务 API fail closed，并只暴露 content-free 状态与显式 retry/verified restore 选项。
- restore 先复制并校验 backup，再隔离原 DB/WAL/SHM；恢复成功要求重启。迁移失败可保留原 v2 数据并重试。
- W11 共享面仅新增通用 `HealthProvider` 接线，memory 作为 optional capability；未改变 readiness 契约。
- 未实现 W22 vision 强屏障。

## 自动证据

所有数据库、WAL、backup、export、temp 和正文都只使用 synthetic 数据及显式 sentinel；测试未访问真实用户数据库。

### Migration、恢复与状态机

- `test_v2_to_v3_redacts_old_source_from_live_query_fts_export_and_manifest` 验证 v2→v3 后，旧 source 无法从 active DB dump、查询、FTS、export、migration audit 或 backup manifest 恢复。
- migration fault injection 覆盖 `after_checkpoint`、`after_backup`、`before_migration`、v3 statement 前后和 commit 前；每个失败点都保持 schema v2 及原数据不变。
- 真实 WAL writer lock 使 checkpoint fail closed；释放锁后迁移恢复成功。
- future schema 测试逐字节及 mtime 比较 DB/WAL/SHM 前后状态，确认启动 probe 零写入，并确认 transaction 拒绝写入；另由仍存活的 writer 保持非空 WAL，验证 probe 不会忽略 WAL 中已提交的 future schema。
- verified restore 覆盖非法名称、错误 hash、损坏 backup、DB/WAL/SHM quarantine 和恢复后 v2 内容。
- Hypothesis 运行 30 组、每组最多 40 次 feature desired 序列，验证 generation 单调、stable 幂等和陈旧 CAS 拒绝；另验证中断状态恢复为 `failed/interrupted_transition`。

### 删除、maintenance 与健康状态

- disabled history 推进 fake clock 8 天后仍完成 retention cleanup。
- secure cleanup 模拟 file lock 时 logical delete 立即成功，查询、FTS、export 立即不可见；job 进入 retrying，时钟推进后恢复 completed。
- maintenance 覆盖 DB busy、full、corrupt、file lock、单周期异常隔离、有界退避和恢复归零。
- 健康状态覆盖 safe-mode unavailable、maintenance degraded、cleanup pending、repository corrupt/full 和恢复 ready。
- API integration 覆盖 cleanup pending/status/missing、future schema status、health capability、retry 拒绝、错误 restore 和 verified restore。

### Sentinel 与敏感字段扫描

显式正文 sentinel 为 `old-source-sentinel::full dialogue that must not survive active v3 surfaces`。迁移测试扫描生成目录内所有文件，并验证：

- active DB、WAL、SHM、export、temp：0 命中；
- FTS、query、audit、manifest：0 命中；
- migration rollback backup：唯一预期命中；
- migration temp：0 残留；
- backup manifest 只含文件名和 SHA-256，不含正文。

这一区分很重要：migration 前 backup 的目的就是保留可回滚的旧库，因此它可能包含旧 source。测试没有把“active surface 已清除”错误表述成“所有备份均物理擦除”。

## 本地质量门禁（2026-07-18）

- `uv run pytest`：802 passed，2 skipped；两个 skip 均为未安装的可选 RapidOCR/Pillow 依赖。branch coverage 90.07%，门槛 90%。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：通过。
- strict `uv run mypy`：通过，173 source files。
- wheel 构建和隔离安装 smoke：通过；`source_tree_imported=false`，API/config/desktop/idempotency smoke 全通过。
- 本地临时 wheel SHA-256：`66c0e8de73273d3c102508e1292de581d37c1c4ca194a88ef6fc50d96bc797ce`；manifest SHA-256：`c79f1a973adf4c8a433f4cd77bf542e127ed3926c9403975cd5e4b03b362051d`。该 wheel 不是 release artifact。
- exact-head GitHub CI：创建 Draft PR 后以远端 check URL 为准，本节不预先声称通过。

## 反方审查与残余风险

- “删除成功”只表示 logical visibility barrier 已提交，不等于底层介质安全擦除。SQLite page、filesystem snapshot、系统备份和 W10 migration backup 仍可能保留旧字节。
- cleanup 是 best effort；磁盘满、锁、损坏或权限变化可能长期维持 `cleanup_pending`。实现会退避并暴露稳定状态，不承诺固定时间完成。
- backup 包含敏感数据的风险没有被 hash manifest 消除；hash 只验证完整性。backup 生命周期和最终用户承诺仍需按已批准隐私文档执行。
- restore 会保留原 DB/WAL/SHM quarantine，便于失败回退，但也延长敏感字节的本地留存；当前没有把 quarantine 当成已擦除数据。
- future/corrupt safe mode 是 fail-closed 的内容隔离，不是通用 SQLite 修复器；verified restore 后必须重启，避免旧 runtime 状态与磁盘 schema 混用。
- 本地 branch coverage 为 90.07%，仅略高于 90% 门槛；exact-head CI 是最终自动判据。

## Public repository 与证据边界

- 2026-07-18 已通过 GitHub API 确认仓库 visibility 为 `public`。项目所有者声明其已检查没有隐私数据上传；本记录是 **owner attestation**，不是独立的全历史安全审计，也不替代 secret scanning 或历史重写审计。
- 旧文档中“仓库必须保持 private”的前提已被项目所有者的新决定覆盖；W10 不再以仓库公开为阻塞条件。
- 公开化没有自动启用保护：`agent/windows-development-baseline` 当前返回 `Branch not protected`，仓库 ruleset 列表为空。因此仍强制使用 Draft PR、exact-head checks、expected-head guard；未来只有收到审计授权后才可合并，并须执行合并后读回。不得声称公开仓库已受保护。
- PR 分支、Actions 日志和上传 artifact 可能被公开读取。W10 commit、PR 和 CI 只允许 synthetic sentinel、稳定 error code、hash/count 和无路径 provenance；禁止真实 secret/token、用户正文、数据库、日志、截图、WAV、模型/角色资产或用户路径。
- W10 自动检查只证明本 PR 的合成 fixture、生成物和规定扫描面符合上述边界；没有把 owner attestation 扩张为对仓库全部历史、其他 PR 或第三方缓存的独立结论。

## 回滚

- 应优先停止写入并保留 safe mode，再由显式 recovery API 使用 migration 前 verified backup 恢复 v2；恢复后重启。
- 不得用代码回滚直接让 v2 程序写入已经成功迁移的 v3 数据库，也不得删除唯一 migration backup 后再尝试降级。
- 若 v3 migration 在 commit 前失败，事务回滚保持 v2；修复环境问题后可选择 retry migration。
- 若 restore staging 失败，实现尝试恢复原 DB 及 WAL/SHM；quarantine 和 backup 在人工核验前不得当作垃圾自动删除。

## 人工 Gate

无新增隐私决策，仅需核对 AI 证据并签字。

唯一不可替代人工确认：隐私 reviewer/项目所有者核对本页证据与既有承诺一致后，明确回复“W10 审计合格”。收到该确认前保持 Draft，不记录签字、不请求合并、不自行合并。
