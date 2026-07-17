# 新会话交接：Windows 开发计划 W05 → W06

> 交接日期：2026-07-17
>
> 权威计划：[`docs/windows_development_plan.md`](docs/windows_development_plan.md)
>
> 当前会话边界：只关闭并受控合并 W05；**没有开始 W06**。新会话不得假设合并成功，第一步必须从
> GitHub 只读核验 PR #15 的最终状态和 merge commit。

## 我们在做什么

项目正在严格按 Windows 开发计划的 W 编号顺序，把当前源码级陪伴 AI 原型推进为可安装、可审计、
有界且可恢复的 Windows 私人桌面应用。每个 W 任务都必须独立实现、充分测试、发布 Draft PR，并在计划
指定的人工 Gate 停下；只有项目所有者明确审计合格后才能进入下一阶段。

本次会话从 W04 审计通过开始，完成并发布了 W05“安装产物与源码双轨 CI smoke”。项目所有者已明确
接受 W05 的两个 GitHub 平台残余风险，授权在确认不会误合并时合并 PR #15，并要求合并后结束会话。

## 已经完成了什么

### W01～W04

- W01～W03 已在此前按计划完成并合并；对应实现/审计记录在 `docs/implementation/`。
- W04 的生产零监听与安全 dev API 已完成自动测试和项目所有者非独立安全审计。
- W04 PR #14 已合并到 `agent/windows-development-baseline`，merge commit：
  `0e4e7ed2c72fcdafce59a0228b3269bd56905d90`。

### W05 实现

- 分支：`codex/w05-installed-artifact-ci`。
- Draft PR：[PR #15](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/15)，base 为
  `agent/windows-development-baseline`。
- 保留 Windows/macOS 源码 `quality` job，并新增双平台 `installed-wheel` job。
- wheel 在安装前执行 fail-closed 内容策略；在空 cache、新 venv、仓库外 Unicode/空格 CWD 中安装。
- smoke 期间原子隐藏 checkout 的 `app/` 与 `desktop_client/`，证明没有源码树 fallback，并在 `finally`
  恢复。
- 验证真实 API CLI、desktop preflight、locked/authenticated ASGI health 和 CWD 不变性。
- 三个 Actions 均固定审核过的 40 位 commit SHA；workflow token 只读，checkout 不持久化凭据。
- build backend 固定并验证为 `hatchling==1.31.0`。
- 只上传 7 天保留、无路径的 provenance JSON；不上传 wheel、cache、venv 或用户数据。
- `.github/dependabot.yml` 只更新 GitHub Actions；注意它进入默认分支 `main` 前不会生效。

### 自动证据

- 本地最终全仓：`657 passed, 1 skipped`；branch coverage `90.81%`。
- W05 聚焦矩阵：`40 passed`。
- Ruff lint/format 和 strict mypy（158 source files）通过。
- 实现 head `d77a3eb427cbed7daef12c957c2ed1fa85bb1b3a` 的 PR/push 双平台矩阵全部通过；
  随后的文档 head `4d9632affb65e72c7c54fec9871f72e7685893e8` 也全部通过。
- 两平台 wheel 都有 102 个相同成员，内容 manifest SHA-256 均为
  `650c750316ac8edf5f914725a98d1bd57d87be46eea5b683968d4871834da941`。
- Windows/macOS raw wheel SHA 不同不是 payload 漂移：ZIP creator-system 分别为 `0`/`3`；成员名、长度和
  内容 hash 全部一致。
- `uv.lock` SHA-256：`cc69c6a7574d663781856f4a69a1d8bd0f5112cbb217ac0579628daf0770ae2c`。
- 完整证据与审计步骤：
  [`docs/implementation/w05_installed_artifact_ci.md`](docs/implementation/w05_installed_artifact_ci.md)。

### W05 人工 Gate

- 项目所有者原文：**“审计合格，并接受 RR-W05-01/02 Gate 例外”**。
- 审计是项目所有者兼任 reviewer 的**非独立审计**。
- RR-W05-01：private Free 无 branch protection/rulesets，owner 仍能直接 push/绕过检查。
- RR-W05-02：`allowed_actions=all` 且 `sha_pinning_required=false`，未来 workflow 缺少仓库级防误配。
- 两项风险被接受但没有消失；仓库仍必须保持 private，禁止改 public 来规避 GitHub 付费限制。
- 例外与补偿控制已记录到
  [`docs/adr/ADR-W08-packaging-upgrade.md`](docs/adr/ADR-W08-packaging-upgrade.md)。

## 当前卡在哪里

没有代码或测试 blocker。`HANDOFF.md` 必须作为 W05 最终关闭提交的一部分进入 PR #15，所以它无法在
提交时自证“包含自己的 PR 已合并”。当前会话获准执行一次受控合并，并将在合并后结束。

新会话的第一条检查必须是：

```powershell
gh pr view 15 --repo mizusawa-matsuri937/megumin_companion_ai `
  --json state,mergedAt,mergeCommit,baseRefName,headRefName,headRefOid,url
```

- 若 `state=MERGED`，再 fetch `agent/windows-development-baseline`，确认 merge commit 包含本文件和 W05
  全部提交，然后才开始 W06。
- 若因本会话中断而未合并，不要直接重试：先确认 PR 仍是预期 base/head、所有最终 checks 成功、无新增
  commit/review blocker，再使用 expected-head guard 合并。
- 若 head、base 或 diff 与本记录不同，立即停止并重新审计变化；不要把旧审计套到新 head。

## 下一步计划

确认 W05 已合并后，严格执行：

1. 更新本地 `agent/windows-development-baseline` 到 PR #15 的已验证 merge commit，确认工作区干净。
2. 从该基线创建 `codex/w06-idempotency-replay`（或同义 `codex/` 分支）；不要从旧 W05 head 或 `main`
   开始。
3. 先完整重读：
   - `docs/windows_development_plan.md` 的 PR W06；
   - `docs/adr/ADR-W05-idempotency-replay.md`；
   - `app/core/turns.py`、API protocol/routes、现有 turn/event tests。
4. W06 只实现 message idempotency、event seq/replay/snapshot reset、有限 TTL/LRU 状态和有界 subscriber；
   不要顺手开始 W07 的全流水线背压。
5. 先写契约和 state/property/fault tests；必须覆盖 duplicate/cancel/disconnect/replay/new-turn preemption、
   10k turn、慢消费者、跨 session 泄漏和 provider/TTS/VTS/observer 单副作用。
6. 跑聚焦测试、全量 pytest+coverage、Ruff、strict mypy 和受影响的 installed-wheel smoke；发布独立 W06
   Draft PR。
7. 到 W06 人工审计点再次停下，给出幂等保留时间、重复计费/播放、replay 跨 session 泄漏的详细审计
   方案。没有项目所有者“审计合格”不得进入 W07。

## 踩过的坑：绝对不要再踩

1. **不要把 PR head 和受测提交混为一谈。** `pull_request` 默认 checkout 临时 merge ref；provenance 必须
   分别记录 `change_revision` 和 `source_revision`/kind。
2. **不要用跨 OS raw wheel hash 判断 payload。** ZIP creator-system 会令容器字节不同；跨平台内容一致性
   看逐成员 manifest，raw hash 只标识单次构建。发布级可复现性仍留 W24～W27。
3. **不要因为 owner 接受 Gate 例外就盲合并。** 关闭记录必须先提交，最终 head checks 必须全绿，再核对
   base/head/diff/review/mergeability，并用 `--match-head-commit` 锁 head；合并后还要读回远端状态。
4. **绝对不要把 private 仓库改 public 来获得免费 branch protection。** 这是审计直接拒绝项。
5. **不要声称 Dependabot 已运行。** 配置尚未进入默认分支 `main`，激活前需要人工巡检 Action release/
   advisory。
6. **聚焦 pytest 要加 `--no-cov`。** 仓库全局 coverage source 是 `app`/`desktop_client`；只跑 W05 工具
   测试会出现“测试全过但 coverage 0%”的预期门失败。真正覆盖率结论必须来自随后全仓 `uv run pytest`。
7. **不要依赖沙箱外 uv cache 的默认写权限。** 若 uv 因缓存访问被沙箱拒绝，应按权限流程重跑，不要用
   不可审计的临时绕过；installed smoke 自身仍必须使用空 cache。
8. **不要破坏 source quarantine。** `app/` 和 `desktop_client/` 只能同盘原子 rename，并必须在 `finally`
   恢复；不能递归删除、跨盘搬运或在失败后留下 quarantine。
9. **不要上传 wheel/cache 来“方便审计”。** W05 artifact 只允许 path-free provenance；wheel 不是 release
   candidate。
10. **不要在本会话开始 W06。** 用户明确要求 W05 合并后结束会话；这是对先前“合并后继续 W06”计划的
    后续覆盖。

## 本次协作复盘

本次可识别的用户纠正只有一项；W04/W05 的“审计合格”及 RR 风险接受属于计划要求的人工输入，不是对
错误输出的纠正，因此不虚构为问题。

| 我修改的内容 | 归因 | 下次的开头指令建议 |
| --- | --- | --- |
| 助手原计划“W05 合并确认后进入 W06”；用户改为“W05 合并后结束本会话，先写完整交接，不得启动 W06”。 | **前置信息不够、后续新增会话边界。** 最初总任务要求按 W 顺序持续开发，所以继续 W06 的推断有依据；收到新约束后必须立即覆盖旧计划。 | 开头增加：“本会话只处理到 W05 合并；合并后立即结束，不要开始 W06。结束前把状态、验证、阻塞、下一步、踩坑和纠错复盘写入根目录 `HANDOFF.md`。” |

## 错题本/系统记忆处理

- 检查结果：Codex 专用记忆数据库当前没有条目，`memories/` 为空，且 `generate_memories`/
  `use_memories` 均为 false；没有可更新的同类旧条目。
- 没有直接修改未公开契约的内部 SQLite，也没有擅自启用全局记忆功能。
- “新用户指令覆盖旧计划”已由系统提示词明确规定，按用户要求不重复录入。
- 唯一新增的有效方法是“无平台强制保护时的 expected-head 受控合并流程”，已去重后写入根目录
  `agent.md` 的 `Guarded Merge Without Platform Enforcement`，新会话会作为仓库级持久指令读取。
