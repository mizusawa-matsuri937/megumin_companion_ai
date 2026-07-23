# ADR-W08：wheel、onedir、安装、升级和发布

- **状态：** 2026-07-17 已批准；具体安装器实现暂定
- **决策者：** 项目所有者（兼任许可证/发布 reviewer）
- **风险：** P0-02、P0-03、P0-04、P2-05～P2-10
- **后续 PR：** W01、W05、W24～W27

> W05 Gate W1 于 2026-07-17 经项目所有者非独立人工审计后走显式例外路径关闭：所有者明确接受
> RR-W05-01/02。仓库仍为 private Free，branch protection/rulesets 未配置，Actions 仍允许全部来源且
> 仓库级 SHA enforcement 仍关闭；不得把例外描述为平台保护已启用。完整记录见
> [`../implementation/w05_installed_artifact_ci.md`](../implementation/w05_installed_artifact_ci.md)。

## 背景

wheel 当前能构建但不能在仓库外独立导入；没有 GUI entry、冻结包、安装器、单实例、升级/回滚或崩溃恢复。源码质量绿灯不是可安装产品证据。

## 决策

1. 先建立无 import side effect 的 app factory、console/desktop entry 和完整 package resources。
2. wheel 必须在仓库外的新环境完成 import、`--help`、最小启动/health smoke。
3. 使用 PyInstaller `onedir + windowed` 形成可审计目录；不使用 onefile 自解压。
4. 首发采用 per-user、标准用户友好、无静默自动更新的安装模型。
5. 具体安装器实现保持暂定；W25 前对候选实现做 spike 后由所有者冻结。
6. 升级前 DB checkpoint + backup；迁移失败回滚应用并保留原数据。
7. 项目目前仅私人使用，不要求代码签名；任何公开分发重新打开签名、许可证、命名和资产 Gate。

## 备选与取舍

- **可行备选：** Nuitka 或 MSIX。可以在后续评估，但会改变 native 依赖、商店/签名和资源模型，不能绕过 wheel/resource 基线。
- **拒绝：** 直接 onefile；安装器把用户数据写进安装目录；静默自动更新；无备份迁移；把源码环境 smoke 当安装产物 smoke。

## 资产和许可证边界

安装包不得包含角色图片、Live2D、声音模型、参考音频、whisper 模型、真实用户数据或密钥。Qt、ONNX、RapidOCR、PortAudio、whisper.cpp 和安装器依赖必须记录版本、来源、hash、许可证和目标架构。私人范围变化为公开分发时必须引入独立许可证/法律审查。

### W18 受管 STT 的后安装资产边界（2026-07-23，待新 head 核验）

`whisper.cpp` v1.9.1 runtime 和 `ggml-base-q5_1.bin` 不是 wheel、onedir 或安装器输入。它们只可由当前 Windows
用户在安装包外显式确认下载到 LocalAppData；固定 URL、大小、SHA-256 和 MIT 来源记录在
[W18 runtime 决策](../decisions/w18_managed_chinese_stt_runtime.md)。不提供静默下载、自动更新或用户 URL/hash 覆盖。

wheel 检查把 `.bin`、`.ggml`、`.gguf`、`.exe`、`.dll` 和 `.zip` STT 资产列为禁止成员。未来 W24/W25 的 SBOM/installer
工作仍必须单独列出 runtime 选择、来源、许可证与 hash；本节不把运行时下载实现误写成已完成安装器或公开分发审计。

## 失败和恢复语义

- 安装/升级事务失败时回滚应用文件，保留 DB/config backup 和清晰错误码。
- 新 schema 不允许旧应用直接写入；兼容矩阵决定只读安全模式或回滚。
- 文件锁、杀毒软件和磁盘满使用有界重试，失败不删除唯一数据副本。
- 崩溃重启先检查 migration/crash marker，再决定安全模式，不能半迁移继续。

## 验收

- clean standard-user VM 的 fresh、upgrade、migration failure、rollback、uninstall。
- Program Files/只读安装、LocalAppData、中文/空格/长路径、非系统盘和无控制台窗口。
- artifact allowlist、bundle manifest、SBOM、dependency/license 清单与隐私残留扫描。
- 私人未签名包不得被描述为公开发布候选。

## W05 Gate W1 例外与补偿控制

- **RR-W05-01：** private Free 仓库不能启用本项目要求的 branch protection/rulesets；owner 仍可直接
  push 或绕过失败检查。项目所有者已明确接受该风险，补偿控制是所有 W 任务继续只经 Draft PR、最终
  head 四项双 OS checks、人工 diff/review 和锁定 head 的受控合并。
- **RR-W05-02：** 仓库当前 `allowed_actions=all` 且 `sha_pinning_required=false`。项目所有者已明确接受
  未来 workflow 误配缺少仓库级强制防护的风险；当前 workflow 仍通过测试要求三个 Action 使用审核过的
  40 位 SHA，并保持只读 token、无 PR secrets、无 checkout 凭据持久化。
- 例外只关闭 Gate W1 的流程阻塞，不消除风险，也不批准把 private 仓库改 public。若以后具备 GitHub Pro
  或等价平台能力，应配置并做阻塞验证，然后以新证据关闭两项例外。

## 回滚

发布上一个已验证 onedir artifact，恢复迁移前 DB/config backup；失败版本不覆盖旧 artifact。安装器 spike 可替换，但 per-user、备份、回滚和资产边界不变。
