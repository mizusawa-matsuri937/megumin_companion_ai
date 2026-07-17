# W05 实现与审计记录：安装产物与源码双轨 CI

> 实现日期：2026-07-17
>
> 状态：实现与本地自动验收通过；GitHub 双平台 checks 和人工供应链/仓库设置审计待完成，保持 Draft，不得进入 W06
>
> 风险：P0-02、P2-07、P2-08
>
> ADR：[`../adr/ADR-W08-packaging-upgrade.md`](../adr/ADR-W08-packaging-upgrade.md)

## 已实现范围

### 双轨、双平台质量门

`.github/workflows/ci.yml` 保留原有源码 `quality` job，并新增独立的
`installed-wheel` job；两个 job 都在 `windows-latest` 和 `macos-latest` 运行。
最终应出现四个互不混淆的 check 名：

1. `quality (windows-latest)`；
2. `quality (macos-latest)`；
3. `installed-wheel (windows-latest)`；
4. `installed-wheel (macos-latest)`。

`pull_request` 继续覆盖所有 PR；`push` 明确覆盖 `main`、实际开发分支
`agent/windows-development-baseline` 和工作分支 `codex/**`。源码 job 继续执行 frozen
全量依赖同步、pytest + branch coverage、Ruff lint/format 和 strict mypy。

installed-wheel job 不同步项目源码环境，按以下顺序执行：

1. 从 checkout 构建唯一 wheel；
2. 在安装前读取 ZIP/metadata，执行 package allowlist 和敏感内容拒绝策略；
3. 在系统临时根创建全新 Python 3.11 venv 和空 uv cache；
4. 只从 wheel 安装项目及解析出的运行依赖，并执行 `uv pip check`；
5. 在同盘 `dist/` quarantine 中临时隐藏 checkout 的 `app/` 与 `desktop_client/`，`finally` 原样恢复；
6. 把 smoke 脚本复制到仓库外含中文、空格的任意 CWD，以 `python -I -B` 启动；
7. 运行真实 `megumin-companion-api` 的 `--help`、`--version`、`--check-config`，
   运行真实 `megumin-companion-desktop` 版本/preflight，再运行锁闭和显式认证 ASGI health；
8. 对 CWD 做运行前后递归 hash 快照，任何文件新增、删除或修改均失败；
9. 写出不含绝对路径的 provenance JSON，只上传该 JSON，绝不上传 wheel 或 cache。

### wheel 内容契约

`tools/w05_ci_smoke.py` 对 wheel fail-closed：

- 只允许 `app/`、`desktop_client/` 和唯一项目 `dist-info/` 顶层；
- product package 当前只允许 Python 源文件及唯一
  `app/resources/default_config.yaml`，新增非代码资源必须显式修改策略并接受 review；
- 拒绝绝对路径、`..`、反斜杠、空 path component、大小写碰撞、重复成员和 symlink；
- 拒绝 `.env`、用户配置、data/log/model/audio/image/database、密钥和常见受保护资产格式；
- 要求 app/desktop 关键模块、METADATA/WHEEL/RECORD、两个 entry point 及精确目标存在；
- 要求项目名和 Python `>=3.11,<3.12` 约束不变，并限制 wheel 压缩前后大小；
- 记录 wheel SHA-256、逐成员内容 manifest SHA-256、`uv.lock` SHA-256、Python/uv
  版本和无本地路径的依赖版本清单。

`tools/installed_wheel_smoke.py` 在设置隔离 LocalAppData 后才导入 product package；同时检查
`app` 和 `desktop_client` 都不来自源码树、`app.main` 没有 import-time 全局 ASGI app、默认资源和
真实 entry point 可用。synthetic dev token 只在子进程内使用，不写 evidence、日志或 artifact。

### Actions 供应链配置

workflow 顶层权限为 `contents: read`；checkout 设置 `persist-credentials: false`，不引用
repository/environment secrets，不使用 `pull_request_target`、`workflow_run`、self-hosted runner 或
可变 action tag。当前 action 来源与固定值如下：

| Action | 审核版本 | 固定 commit | 上游 |
| --- | --- | --- | --- |
| `actions/checkout` | `v7.0.0` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` | [release](https://github.com/actions/checkout/releases/tag/v7.0.0) / [commit](https://github.com/actions/checkout/commit/9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0) |
| `astral-sh/setup-uv` | `v8.3.2` | `11f9893b081a58869d3b5fccaea48c9e9e46f990` | [release](https://github.com/astral-sh/setup-uv/releases/tag/v8.3.2) / [commit](https://github.com/astral-sh/setup-uv/commit/11f9893b081a58869d3b5fccaea48c9e9e46f990) |
| `actions/upload-artifact` | `v7.0.1` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` | [release](https://github.com/actions/upload-artifact/releases/tag/v7.0.1) / [commit](https://github.com/actions/upload-artifact/commit/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a) |

uv 本身固定为 [`0.11.28`](https://github.com/astral-sh/uv/releases/tag/0.11.28)；Python 固定 minor
`3.11`，实际 patch 写入 provenance。GitHub 官方
[secure use reference](https://docs.github.com/en/actions/reference/security/secure-use) 将完整 commit SHA
列为 action 不可变引用方式，本实现同时在同一行保留 release 注释，使 Dependabot 可更新 SHA 和版本注释。

`.github/dependabot.yml` 仅配置 `github-actions` 每周一 05:00 UTC 的版本更新，目标为实际开发
分支；不配置 pip，因此 pytest/Python 包安全公告必须走独立依赖 PR、独立 lock diff 和完整质量门，
不得混入 action pin PR。每个 action 更新 PR 必须核对上游 release、tag→commit 身份和变更说明；
不能只因机器人创建 PR 就自动合并。

GitHub 要求 `dependabot.yml` 位于默认分支才会生效；当前默认分支是 `main`，而 W05 PR 以开发
基线为 base。因此在该配置进入 `main` 前，机器人尚未激活，维护者必须每周人工核对上述三个 action。
`target-branch` 只使 version update 检查/PR 指向开发分支，security update 仍以默认分支为准；固定
SHA 的 action advisory 也不能只依赖告警，仍需人工检查 GitHub Advisory Database/upstream release。

## 自动证据

### 本地 Windows 证据

- 全仓 pytest：`652 passed, 1 skipped`；唯一 skip 是既有“无确定性测试字体”。
- 最终 aggregate branch coverage：`90.85%`，高于 90% 门槛；Hypothesis 路径会令成功运行的
  小数位轻微波动。
- W05 聚焦矩阵：`35 passed`，覆盖敏感成员、ZIP 路径/碰撞/symlink/大小上限、metadata/entry point、
  dependency inventory、环境清理、CWD 快照、workflow 和 Dependabot policy。
- Ruff：`check .` 通过；`format --check .` 为 `163 files already formatted`。
- strict mypy：`158 source files` 通过，包含两个安装产物工具。
- 构建 wheel：`megumin_companion_ai-0.1.0-py3-none-any.whl`，102 个成员，184,778 bytes，
  解压后 626,066 bytes。
- wheel SHA-256：`0607b53ce602dc2479f6bb62f22d06ab06249c32b1d862ef54c37f4b9b160f26`。
- wheel manifest SHA-256：`650c750316ac8edf5f914725a98d1bd57d87be46eea5b683968d4871834da941`。
- `uv.lock` SHA-256：`cc69c6a7574d663781856f4a69a1d8bd0f5112cbb217ac0579628daf0770ae2c`。
- 与 workflow 完全相同的 `uv run --no-project --python 3.11 ...` 命令通过：在空 cache 的新 venv
  安装 25 个依赖，build/installed Python 均为 CPython 3.11.15，uv 0.11.28。
- installed smoke：运行期间 checkout product packages 已隐藏、源码树导入 `false`、CWD unchanged
  `true`、API help/check-config/desktop preflight 均通过、无凭据 health `503`、显式认证 health
  `200 ready`。
- `git diff --check` 在发布前再次执行；最终 commit/PR 和 GitHub 双 OS 结果由 Draft PR 记录。

以上 wheel/hash 是本地未提交 revision 的开发证据，不冒充 GitHub runner 或发布产物。GitHub
provenance 必须带 40 位 `source_revision`，本地 evidence 的 `local-unrecorded` 不得上传为远端证明。

### GitHub 证据（发布后核对）

Draft PR 创建后必须等四个最终 check 全部成功；两个 installed-wheel job 各上传一份 7 天保留的
`provenance.json`。人工 reviewer 下载并核对：

- `artifact_kind == "w05-ci-provenance-only"`、`release_artifact == false`；
- `source_revision` 等于 PR head 的 40 位 SHA；
- `source_checkout.product_packages_hidden_during_smoke == true`；
- 两平台 `uv.lock` SHA 一致，Python 为 CPython 3.11.x；
- wheel/package policy 和 installed smoke 全部为通过状态；
- JSON 不含 runner home、cache/checkout 绝对路径、用户名、token、聊天/记忆或环境变量值；
- upload 中只有 provenance JSON，没有 wheel、`.venv`、uv cache 或用户数据。

纯 Python wheel 在 `.gitattributes` LF 约束下预期两平台 wheel/manifest hash 一致；若不一致必须先解释
具体成员差异。未解释的不一致不得把任一 wheel 当作发布候选；W05 本来也不上传或发布 wheel。

## 迁移、兼容性和回滚

- 无 schema、protocol、用户数据或 runtime 配置迁移。
- 保留现有 `quality (OS)` check 名，降低既有 required check 失效风险；只新增
  `installed-wheel (OS)`。
- Action major 版本均使用 Node 24，只在 GitHub-hosted runner 执行；不在 W05 声明 GHES 或旧
  self-hosted runner 兼容。
- 回滚 workflow 可移除新增 job/Dependabot 配置，但会重新打开 P2-07/P2-08；即使回滚，也不得把
  源码测试冒充 installed artifact 证据，不得恢复可变 action tag。

## 当前仓库设置只读快照

2026-07-17 在未修改外部设置的前提下读取 GitHub API：

| 项目 | 当前值 | 判定 |
| --- | --- | --- |
| 可见性 / 默认分支 | private / `main` | 符合隐私边界；Dependabot 配置尚未在默认分支，暂不激活 |
| Actions | enabled；`allowed_actions=all` | 可运行，但来源策略过宽，待人工审计/收紧 |
| repo-level SHA policy | `sha_pinning_required=false` | workflow 自身已固定；仓库未强制，待人工审计/收紧 |
| 默认 workflow token | `read` | 通过 |
| Actions 可批准 PR | `false` | 通过 |
| `main` branch protection | API 403：private repo 需 GitHub Pro 或 public | **人工 Gate blocker** |
| 开发分支 protection | 同上 | **人工 Gate blocker** |
| repository rulesets | 同上 | **人工 Gate blocker** |

不得为取得免费 branch protection 把私人仓库改为 public。推荐方案是升级 GitHub Pro 后配置 ruleset；
若所有者不升级，只能把“无平台强制保护、依赖人工纪律”作为明确 Gate 例外写入本记录和 ADR，不能描述
成 branch protection 已通过。

## 人工供应链与仓库设置审计方案

### 1. 证据身份和 reviewer

记录最终 PR URL、head SHA、base `agent/windows-development-baseline`、四个 check URL、执行时间、
GitHub runner image 和 reviewer。项目所有者兼任 reviewer 时写“非独立审计”；不得写成独立安全审查。
截图/附件不得包含用户名路径、token、环境变量、runner debug dump 或 private artifact 下载 URL。

### 2. workflow diff 与触发面

在 PR Files changed 中逐行核对：

- 只有 `pull_request` 和限定的 `push`；没有 `pull_request_target`、`workflow_run`、schedule release、
  deployment environment 或 self-hosted runner；
- `push` 精确包含 `main`、`agent/windows-development-baseline`、`codex/**`；
- 原 quality 五个命令仍存在且都是双 OS；installed job 没有 editable install、`uv sync` 或源码
  `PYTHONPATH` fallback；
- 四个 check 名唯一，不能与其他 workflow 重名；最终 head 上没有 pending/cancelled/skipped/failure。

### 3. Action 来源、代码与固定值

对表中每个 action：

1. 从 release 页面进入 tag，再确认 tag 指向表中的 40 位 commit，仓库 owner 不是 fork/拼写变体；
2. 抽查 `action.yml`、运行时版本、输入项、post step 和网络/上传行为；
3. 确认 workflow 的 `uses:` 全部是完整小写 40 位 SHA，同一行版本注释与 release 匹配；
4. 确认只有 GitHub 官方 `checkout`/`upload-artifact` 和 Astral 官方 `setup-uv`，没有隐含 reusable
   workflow、Docker action 或本地 action；
5. 对任何未来更新重复本节，不允许仅更新注释、不更新 SHA，或只更新 SHA、不确认 release 来源。

### 4. token、secret 和允许来源策略

在 `Settings → Actions → General`：

1. 确认 Workflow permissions 为 **Read repository contents**，且 **Allow GitHub Actions to create and
   approve pull requests** 未勾选；这两项当前 API 已通过。
2. 推荐把 “Allow all actions” 改成选择性策略：允许 GitHub-created actions，并只额外允许审核过的
   `astral-sh/setup-uv`；打开可用的 **Require actions to be pinned to a full-length commit SHA**。
3. 查看 PR workflow 使用的 secrets/environment：必须为零；job permissions 不得出现 write、OIDC、
   packages、deployments、issues 或 pull-requests 权限。
4. checkout 的 `persist-credentials` 必须为 false；runner git config 不应留下可用于后续 push 的 token。

若所有者保留 `allowed_actions=all` 或不启用 repo-level SHA policy，必须把 RR-W05-02 作为明确接受的
残余风险；这不改变当前 workflow 的完整 SHA 代码约束，但未来 workflow 的仓库级防误改能力较弱。

### 5. cache、provenance 与 artifact retention

1. quality job 可缓存 uv 下载；cache key 受 `uv.lock` 影响，但 cache 永远不是可信发布输入。
2. installed job 的 setup-uv `enable-cache` 必须为 false；夹具另建空 `UV_CACHE_DIR`，证明新 venv
   安装，不得指向 quality cache 或 checkout 内 `.venv`。
3. Actions 页面下载两个 provenance artifact；各自只含一个 JSON，retention 为 7 天。
4. 按“GitHub 证据”逐字段核对，并在本地计算下载文件 SHA；不得上传 wheel/cache 来替代 hash 记录。
5. Actions repository retention 可长于 7 天，但该 step 的 7 天显式值不得删除或改成 0/default；
   private artifact 下载权限仍需人工确认。

### 6. branch protection / ruleset 人工 Gate

推荐升级 GitHub Pro 后，在 `Settings → Rules → Rulesets` 为 `main` 和
`agent/windows-development-baseline` 建立 Active ruleset：

- Require a pull request before merging；
- Require status checks to pass，精确加入本节开头四个 check，并选择 GitHub Actions 作为预期来源；
- Require branches to be up to date before merging（strict）；
- Require conversation resolution；
- Block force pushes 和 deletion；
- 不给 repository admin/owner 静默 bypass，或把任何必需 bypass 事件记录到审计证据。

配置后用只读 API/界面重新核对两分支，再做一次受控验证：创建无害测试 PR，确认缺任一 installed-wheel
check 时 Merge 按钮被阻塞，四项全绿且 branch up-to-date 后才放行。不要在 W05 审计中实际合并该测试
PR；关闭即可。

当前 private Free 计划返回 403，故在升级/配置前本项客观上不合格。若不升级，所有者必须明确选择
**Gate exception**：保持 private、禁止 public，接受平台不能阻止 owner 直接 push/绕过失败 check，
并承诺 W06 及后续仍只经 Draft PR、人工核对四项 check 后合并。该例外需作为 RR-W05-01 写入 W05
关闭记录和 ADR-W08；不能用普通“已配置 branch protection”措辞掩盖。

### 7. Dependabot 更新流程

1. 当前 W05 PR 中先验证 YAML 结构和同一行 tag 注释；不要声称机器人已经运行。
2. 因 config 必须位于默认分支，W05 审计后由所有者决定通过单独、最小的基础设施 PR 把同一配置送入
   `main`；不得顺带把 W06 runtime 改动送入 main。
3. 激活后确认下一次 weekly run 能读取开发分支 workflow，并对 action 新 release 开独立 PR。
4. 在激活前、或机器人对固定 SHA/advisory 无法告警时，每周人工查看三个上游 release/advisory。
5. action pin、uv tool、pytest/Python dependency 分成不同 PR；Python dependency PR 必须更新并审计
   `uv.lock`，重跑全量测试和 installed wheel，不准只改 pytest 版本绕过公告。

### 8. Gate W1 汇总

确认前置记录：W01 wheel/resource、W02 路径/迁移、W03 DACL/DPAPI/temp 与真实 Windows 边界、W04
生产零监听/dev API 均已按顺序完成相应自动验收和项目所有者非独立人工审计。W05 GitHub-hosted runner
不替代 W02/W03 的真实 Windows 标准账户证据，也不把当前 desktop preflight 冒充 W13 GUI。

Gate W1 只在以下两种情形之一成立：

1. **推荐路径：** 四项 CI 全绿，Action/权限/artifact 审计通过，Pro ruleset/required checks 实际配置并
   做阻塞验证；或
2. **显式例外路径：** 其余项目全通过，所有者明确接受 RR-W05-01 和 RR-W05-02，并授权把例外写入
   ADR；不得声称 branch protection 已配置。

在 Gate W1 有书面结论前，PR 保持 Draft，W06 不得开始。

## 审计通过、拒绝与回复格式

### 通过条件

- 最终 head 上四项双 OS check 全部 success；
- wheel 内容 policy、CLI/desktop/ASGI/CWD smoke 与 path-free provenance 均符合；
- 三个 action 的来源、40 位 SHA、权限和上传行为人工核对无误；
- workflow token 保持 read-only，secrets 为零；
- branch protection 走推荐路径，或所有者明确批准 Gate exception；
- reviewer 接受下节所有残余风险、非独立性和回滚边界。

### 直接拒绝

任一情况出现即拒绝：可变 `@vN` tag、未知/ fork action、write token、PR secrets、
`pull_request_target` 执行不可信代码、wheel/cache 被上传为发布产物、包含私密/受保护资源、源码树回落、
CWD 写入、任一 required check 未绿，或把 private repo 改 public 以规避付费限制。

### 回复格式

- 若已配置并验证 ruleset：回复“审计合格”，并附设置页/只读 API 的无敏感证据摘要。
- 若选择 private Free 例外：必须明确回复
  “审计合格，并接受 RR-W05-01/02 Gate 例外”，不能只说 branch protection 已通过。
- 若未完成：列出失败项、预期/实际值和证据位置，回复“审计不合格”；修复后重新执行受影响部分及四项
  CI，不得直接进入 W06。

## 未关闭的残余风险

1. **RR-W05-01：** private Free 仓库当前不能启用 branch protection/rulesets；若走例外路径，owner
   仍可直接 push 或绕过失败 check，只有流程纪律而无平台强制。
2. **RR-W05-02：** 当前仓库允许所有 Actions 且未强制 SHA；当前 workflow 已由测试固定完整 SHA，
   但未来新增 workflow 的仓库级防误配取决于管理员是否收紧设置。
3. `dependabot.yml` 在进入默认分支前不生效；固定 SHA action 的安全告警不能代替人工 upstream
   advisory/release 巡检。
4. wheel 直接安装按 `pyproject.toml` 范围解析运行依赖，不是发布级 lock/constraints、SBOM 或 license
   证明；W24/W26 继续负责。
5. provenance artifact 只是 7 天 CI 证据，不是签名、attestation 或 release artifact；W24～W27
   继续负责 onedir/installer/signing/release。
6. GitHub hosted Windows 不是两个本地标准账户、Program Files 只读安装或杀软/企业策略 VM；不能覆盖
   W03/W25 的设备与 ACL 重验。
7. 当前 desktop entry point 仍是 W13 前诚实 preflight；installed smoke 不证明 GUI、托盘、IME 或
   长稳运行。

## 回滚

回滚到上一个 workflow 会恢复仅源码 `quality`，同时重新打开 P2-07/P2-08 和 Gate W1。紧急情况下可
暂时停用整个 workflow，但必须冻结 W06 及合并；不得删掉 installed smoke 后仍把源码绿灯描述为 wheel
可交付。Action 更新失败时回退到本表最后一个已审核完整 SHA，不回退可变 tag。
