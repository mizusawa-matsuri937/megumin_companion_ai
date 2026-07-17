# ADR-W04：Windows DACL、DPAPI、秘密轮换和卸载

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任安全/隐私 reviewer）
- **风险：** P0-03、P1-06、P1-07、P1-11、P1-12、P1-14
- **后续 PR：** W03、W11、W25

## 背景

VTS token、API key、数据库/WAL、日志和临时文件缺少 Windows 等价安全边界。POSIX mode 或 `chmod` 不能证明 Windows effective access。

## 决策

- app 私有根在写入任何敏感数据前创建当前用户限定 DACL，并验证 effective access。
- LLM/VTS secret 使用 DPAPI current-user scope 的独立密文文件，禁止 machine scope。
- secret blob 包含 format version、purpose、key id 和完整性校验，支持 replace、revoke、reset；purpose 不匹配或篡改必须失败。
- 环境变量只作为开发输入，不作为生产持久存储；日志和诊断包不得输出 secret。
- 普通卸载显示保留/删除选择：默认保留非秘密用户数据，删除并撤销 API key、VTS token 等 secret。
- 选择删除全部时执行 best-effort 逻辑/物理清理并报告 pending 项，不承诺 SSD 上可证明的物理擦除。

## 备选与取舍

- **可行备选：** Windows Credential Locker 保存少量凭据。DPAPI 文件更适合版本化、purpose 隔离和本地备份语义，首版采用 DPAPI。
- **拒绝：** 明文 `.env`/YAML/JSON；DPAPI machine scope；只设置 POSIX mode；卸载静默删除全部数据；宣称 secure delete 等于物理擦除。

## 失败与恢复语义

- 换机、换用户、篡改、purpose 错误或 DPAPI 解密失败时要求用户重新输入，不回退明文或 Mock。
- secret 轮换采用 write-new、验证、原子 replace、撤销旧值；崩溃后最多保留不可用旧 blob，不能保留两个活跃明文值。
- 删除被文件锁阻止时登记 cleanup pending，后续启动/卸载重试并向用户显示结果。

## 迁移、备份和隐私

旧明文 secret 只允许显式一次性导入，成功 DPAPI 重包并验证后撤销旧值。普通 DB/config backup 不包含可移植明文 secret；current-user DPAPI 数据换机不可恢复，UI 必须提前说明。

## 验收

- 两个 Windows 账户验证 effective access；非当前用户、tamper 和错误 purpose 解密失败。
- 安装、轮换、崩溃恢复、普通卸载、删除全部和文件锁场景测试。
- 日志、fixture、artifact 和诊断包执行 secret sentinel 扫描。

## 回滚

安全失败时禁用对应真实 provider/VTS，文字 Mock 或其他明确配置继续；绝不回退明文存储。卸载失败保留数据并报告，不进行不受控递归删除。
