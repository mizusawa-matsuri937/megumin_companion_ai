# W02 实现与待审计记录：路径、配置和显式旧数据迁移

> 实现日期：2026-07-17
> 状态：自动验收与项目所有者人工审计均已通过；等待 W02 提交/合并，合并前不得进入 W03
> 风险：P0-03、P2-12
> ADR：[`../adr/ADR-W03-paths-resources-migration.md`](../adr/ADR-W03-paths-resources-migration.md)

## 已实现范围

- 新增无副作用 `AppPaths`，统一 package resource、config、state、secrets、logs、cache、temp、models；构造和 `--check-config` 均不创建目录。
- DB、日志、VTS token、TTS 临时/缓存、Mock 音频、STT 临时/模型/可执行文件全部改由分类路径解析；受管路径拒绝绝对路径和 `..` 越界。
- 配置顺序固定为 package defaults → LocalAppData 用户设置 → 显式开发 `--config`/`--env-file`/`MEGUMIN_*`；不再自动发现 CWD 或配置目录中的 `.env`。
- 设置 schema 当前为 v1。旧无版本设置只在内存中升级；`--upgrade-settings` 才执行原子写入，并保留 `settings.yaml.bak`。未来 schema 直接拒绝。
- 用户设置写入前拒绝明文 secret 字段；生产用户设置拒绝开发配置、dotenv 和环境覆盖。DPAPI secret store 仍严格留给 W03。
- `--migrate-from OLD_DATA` 只读取显式路径，不扫描 CWD。迁移先做只读预检，再在 LocalAppData 同卷 sibling staging 中 copy/checkpoint/backup/verify，最后用目录替换原子启用；旧源永不删除。
- 迁移白名单只有 `private/companion.sqlite3` 与 `models/`。日志、缓存、STT/temp、`.env` 和 VTS token 不迁移；secret 必须重新输入。
- 若目标 LocalAppData 根已存在，迁移拒绝覆盖或合并。成功后 CLI 明确要求先验证，再由用户手动删除旧 `data/`。

## 配置与路径迁移

旧 v0 默认相对路径只在匹配已知旧默认值时迁移：

| 旧值 | v1 分类结果 |
| --- | --- |
| `data/logs/app.jsonl` | `logs/app.jsonl` |
| `data/private/companion.sqlite3` | `state/companion.sqlite3` |
| `data/private/vts-token.json` | `secrets/vts-token.json`，W03 再加 DPAPI |
| `data/cache/audio/...` | TTS persistent → `cache/audio`；ephemeral/Mock → `temp/audio` |
| `data/private/stt` | `temp/stt` |
| `data/models/whisper/...` | `models/whisper/...` |

自定义绝对模型/可执行文件路径继续视为用户选择的外部资产；DB、日志、secret、cache 和 temp 不允许离开应用受管目录。

## 旧数据覆盖与合并规则

| 类别 | 行为 | 理由 |
| --- | --- | --- |
| SQLite | checkpoint 后通过 SQLite backup API 复制；目标与迁移前 backup 均做 `quick_check` | WAL 一致性、可回滚 |
| models | 逐文件复制并比较 SHA-256；限制 10,000 文件/32 GiB | 避免静默损坏和无界扫描 |
| logs/cache/temp | 跳过 | 非必要派生数据或高泄露风险 |
| `.env`/VTS token | 跳过并提示重新输入 | 不把明文 secret 带入新目录 |
| 未知文件 | 不进入白名单 | fail closed |
| 已存在目标 | 整体拒绝，不覆盖、不合并 | 防止旧/新状态静默混合 |

## 自动证据

- `uv run pytest -q`：**506 passed, 1 skipped**；唯一 skip 为缺少确定性测试字体。
- branch coverage：**92.01%**，高于 90% 门槛。
- `ruff check`、`ruff format --check`：通过；143 files already formatted。
- `mypy --strict`：140 source files 通过。
- `git diff --check`：通过。
- W02 受影响单元/集成测试：84 passed；copy/verify/switch 故障注入、部分复制失败、未来 schema、原子写失败、明文 secret 和目标冲突均有断言。
- 真实 D: 盘 pytest 临时根：路径与迁移用例 **16 passed**；全量测试及隔离 wheel 的 LocalAppData 证据位于 C: 临时根。
- 隔离 wheel：仓库外中文/空格 CWD、中文/空格 venv、重定向 LocalAppData、只读 package default 下通过；`uv pip check` 25 packages compatible；导入来自隔离 `site-packages`；CWD item count 为 0；DB/日志只在 LocalAppData 目标出现。

## 人工审计签字

- 审计日期：2026-07-17
- 审计人：项目所有者（兼任架构/隐私 reviewer；非独立审计）
- 结论：**审计合格**
- 签字依据：项目所有者在任务中明确回复“审计合格”，接受本节全部检查项及“未关闭风险与回滚”所列残余风险。

已签字检查项：

1. 确认白名单只迁移 DB/models，跳过日志、缓存、临时录音和 secret 符合产品预期。
2. 确认“目标已存在即拒绝、完全不合并”可接受；若需要合并，必须先补字段级冲突策略和新的故障注入。
3. 确认成功提示不会诱导提前删除旧数据；用户应先启动、检查历史/模型，再手动删除。
4. 确认 copy、verify、switch 和部分复制失败后，旧源保留、目标未激活、当前 staging 清理的证据充分。
5. 确认 v0 → v1 只改写已知旧默认路径，自定义值不被猜测性重写；未来 schema fail closed。
6. 检查错误、manifest 和 CLI 输出不含完整用户路径、secret、日志正文或模型内容。
7. 明确认可下列残余风险仍由 W03 关闭，而不是误判为 W02 已解决。

## 未关闭风险与回滚

- W02 只依赖用户 LocalAppData 的继承 ACL；明确 current-user DACL/effective-access 证据属于 W03。
- VTS token/LLM secret 的 DPAPI、用途绑定、轮换和撤销属于 W03；本阶段只完成路径和“不迁移明文 secret”。
- junction/reparse-point 的 Windows 对抗性边界与跨账户验证属于 W03；W02 已拒绝普通 symlink 和路径越界，但不宣称完成该安全 Gate。
- 进程被硬杀可能留下未激活的 sibling staging；不会切换目标或删除旧源，但再次迁移前需人工清理。当前异常/复制/校验/切换失败会自动清理当前 staging。
- 删除本 PR 的代码并恢复 W01 路径兼容规则可以回滚应用，但会重新打开 P0-03/P2-12；已经写出的 LocalAppData 用户数据和 backup 不得自动删除。
