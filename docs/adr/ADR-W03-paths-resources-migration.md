# ADR-W03：package resource、Windows 路径和旧数据迁移

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任架构/隐私 reviewer）
- **风险：** P0-02、P0-03、P1-10、P1-14、P2-05、P2-12
- **后续 PR：** W01、W02、W05、W24、W25

## 背景

当前代码混用仓库根、CWD 和包位置；wheel 缺少必要资源，且 Program Files、开始菜单、中文/空格/长路径和非系统盘场景不可靠。

## 决策

| 类别 | 位置/机制 |
| --- | --- |
| 只读默认配置、Prompt | package/frozen resource，通过 `importlib.resources` 读取 |
| 用户设置 | `%LOCALAPPDATA%\MeguminCompanion\config\settings.yaml` |
| SQLite/WAL | `%LOCALAPPDATA%\MeguminCompanion\state\companion.sqlite3` |
| 日志 | `%LOCALAPPDATA%\MeguminCompanion\logs` |
| 可选 TTS cache | `%LOCALAPPDATA%\MeguminCompanion\cache\audio`，默认关闭 |
| 临时资产 | `%LOCALAPPDATA%\MeguminCompanion\temp\<run-id>`，registry 跟踪 |
| 用户模型/角色/参考音频 | 用户选择的外部绝对路径，只保存路径与 fingerprint |

- 所有设置带 `schema_version`；生产设置不保存明文 secret。
- 旧 `data/` 只由显式 migration tool 执行 read-only preflight、copy、checkpoint/backup、verify、dry-run 和原子 switch。
- 不扫描 CWD，不静默移动或删除旧数据；失败删除 staging，保留旧应用和旧数据。

## 备选与取舍

- **可行备选：** 非敏感设置放 RoamingAppData。首版是单机私人应用，避免跨机同步路径和隐私混淆，采用 LocalAppData。
- **拒绝：** 写入 Program Files/CWD；从 `__file__` 向上推断仓库；启动时自动发现并迁移任意 `data/`；把用户模型复制进安装包。

## 失败、隐私与兼容性

- 目录、迁移或配置验证失败时进入可解释的 safe mode，不半迁移启动。
- DB 迁移前 checkpoint + backup；磁盘满、文件锁和杀毒软件干扰使用有界重试。
- 诊断错误不得输出完整用户路径；可以输出路径类别和不可逆 fingerprint。
- current-user DACL 必须在复制敏感数据前建立。

## 验收

- wheel/onedir、标准用户、只读安装目录、中文/空格/长路径、非系统盘 smoke。
- 在 copy、verify、switch 每一步故障注入，原数据和上个版本仍可用。
- package 资源清单与构建 artifact allowlist 一致。

## 回滚

删除未激活 staging，继续使用原路径/原版本；激活后保留迁移前 backup 到人工确认。不得通过重新扫描 CWD“自动修复”。
