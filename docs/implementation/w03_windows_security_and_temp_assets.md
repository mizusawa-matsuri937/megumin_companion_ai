# W03 实现与待审计记录：Windows DACL、DPAPI 与临时资产 registry

> 实现日期：2026-07-17
>
> 状态：已合并；项目所有者在后续任务中明确确认 W03 已完成并授权从 W04 开始
>
> 风险：P0-03、P1-14
>
> ADR：[`../adr/ADR-W04-dacl-dpapi-uninstall.md`](../adr/ADR-W04-dacl-dpapi-uninstall.md)

## 已实现范围

- Windows 运行时通过原生安全 API 创建 `%LOCALAPPDATA%\MeguminCompanion`：DACL 受保护，只给当前用户和 `SYSTEM` 完全控制；root 与 config/state/secrets/logs/cache/temp/models 分类目录均显式落同一策略。`chmod` 只用于非 Windows 可移植测试，绝不作为 Windows 证据。
- LLM key 与 VTS token 使用 DPAPI current-user 密文文件。密文 envelope v1 绑定 algorithm、scope、format version、purpose 和稳定 key id；内部值另带校验摘要。
- secret 支持原子 replace、revoke、损坏后保留、显式 reset。生产 LLM 不读取环境变量；开发环境变量仍可作为显式输入，但不作为生产存储。
- 新增显式命令：`--import-llm-key-env`、`--import-vts-token`、`--revoke-secret`、`--reset-secret`。secret 值不作为命令行参数，也不进入成功/失败输出。
- VTS 明文 token 只读取用户明确指定的文件。源和密文目标不同则默认保留源，只有 `--delete-import-source` 才在密文写入并回读验证后删除；若源就是目标，不带该开关会拒绝原位覆盖，要求先保留人工确认用备份。
- 统一 temp registry 使用 root 内相对路径；先登记再创建，删除成功或目标已经不存在时才注销。Windows 占用失败会保留条目并按指数退避重试。
- 启动 scavenger 只处理私有 temp root 内、达到最小年龄且符合严格命名的登记项或残留 `.part`/WAV/录音与转写目录；目录中的临时 JSON/WAV 随所属严格命名目录一起清理。
- TTS part/final WAV、Mock WAV、PTT 录音目录和 whisper.cpp 输出目录均已接入 registry。删除前执行有界、无跟随的完整预检；junction/symlink/reparse point 会拒绝整个删除，不触碰 root 外目标。
- 原生 DACL/DPAPI FFI 不计入跨平台 aggregate coverage，因为 macOS 不可能执行这些 API；它们由 required Windows 测试和本节 VM Gate 单独验收。跨平台逻辑仍受 90% aggregate branch coverage 门约束。

## 安全契约

### DACL

当前模板为：

```text
D:P(A;OICI;FA;;;<current-user-SID>)(A;OICI;FA;;;SY)
```

- `P` 表示 DACL 受保护，不从可变父目录继承 ACE。
- 当前用户和 `SYSTEM` 拥有可继承的完全控制；不授予 `Everyone`、`Users` 或 `Authenticated Users`。
- 新 root 在创建时直接携带安全描述符；已有 root 和分类目录会重新应用受保护 DACL。
- 此边界用于阻止其他标准本地账户和意外宽松继承。管理员可取得所有权，同一用户上下文中的恶意软件也可读取，因此 DACL 不是针对管理员或已失陷账户的加密边界。

### DPAPI secret blob v1

外层只保存元数据和 DPAPI ciphertext：

```json
{
  "algorithm": "windows-dpapi",
  "ciphertext": "<base64>",
  "format_version": 1,
  "key_id": "llm-api-key | vts-token",
  "purpose": "llm.api-key | vts.authentication-token",
  "scope": "current_user"
}
```

实现只传 `CRYPTPROTECT_UI_FORBIDDEN`，不传 `CRYPTPROTECT_LOCAL_MACHINE`。purpose 与 key id 同时进入 optional entropy 和 DPAPI description；错用途、错 id、篡改或错误用户上下文均 fail closed。损坏文件不会静默删除，只有用户显式 replace/reset/revoke 才改变它。

必须准确向用户说明：

1. DPAPI 密文文件不是可移植的凭据备份。换 Windows 账户、丢失原用户配置/凭据、换机或恢复环境不完整时可能无法解密，产品必须始终提供重新输入路径。
2. 企业域/漫游配置可能改变具体恢复表现，因此不能把“复制后必然永远无法解密”作为通用承诺；安全承诺是 current-user scope、非 machine scope，以及不依赖密文备份恢复 secret。
3. revoke/reset/`--delete-import-source` 是逻辑和文件级删除，不保证 SSD、备份、父进程环境、shell 历史或旧 `.env` 中的物理擦除。
4. DPAPI 不防同一登录用户下的恶意代码、管理员、进程内存读取或已经取得 provider 请求权限的代码。

## 显式 secret 操作

LLM key 不得直接写在命令行；只从明确命名的进程环境变量导入：

```powershell
$env:COMPANION_LLM_API_KEY = "<在本机交互输入>"
uv run python -m app --import-llm-key-env COMPANION_LLM_API_KEY
Remove-Item Env:COMPANION_LLM_API_KEY -ErrorAction SilentlyContinue
```

子进程无法清除父 PowerShell 的环境变量；最后一行和 `.env` 的后续人工清理不可省略。

VTS 旧明文文件默认保留到认证验证完成：

```powershell
uv run python -m app --import-vts-token C:\explicit\legacy-vts-token.json
# 验证 VTS 后才显式重跑并授权文件级删除：
uv run python -m app --import-vts-token C:\explicit\legacy-vts-token.json --delete-import-source
```

撤销或损坏后重置：

```powershell
uv run python -m app --revoke-secret llm-api-key
uv run python -m app --reset-secret vts-token
```

## 自动证据

- `uv run pytest -q`：547 passed，1 skipped；skip 是既有的“无确定性测试字体”，与 W03 无关。
- W03 安全聚焦测试集：34 passed，无 skip。
- aggregate branch coverage：90.93%，高于 90% 门槛。
- `ruff check .`、`ruff format --check .`：通过。
- `mypy --strict`：151 个 source/test 文件通过。
- `uv build --wheel`：成功；仓库外隔离 venv 以 `--no-deps` 安装后可从 wheel 导入四个 W03 新模块，未回落源码树。
- 真实 Windows DPAPI round-trip、wrong purpose、ciphertext tamper 和 protected DACL：通过。
- 真实 Windows 独占句柄：首次删除保留 registry 条目并增加 retry；释放句柄后删除并注销：通过。
- NTFS junction：普通 symlink 权限不可用时改用 junction，验证父目录 alias、登记目标被替换和目录内部 reparse child；root 外哨兵均保持不变：通过。
- crash/restart、严格年龄/命名扫描、嵌套自定义 STT root、registry 损坏/越界、多实例并发写入、TTS/STT 注册与清理：通过。

最终提交后的精确 commit、CI URL 和 Draft PR URL 在发布后补入 PR 描述；PR 未通过下列人工 Gate 前保持 Draft。

## Windows VM 与人工安全审计方案

### 1. 环境和证据身份

使用干净 Windows 11、NTFS、两个不同的本地标准账户（下称 Owner 与 Other）和一个只用于准备 ACL/安装的管理员账户。记录：

- commit SHA、Windows build、Python/uv 版本；
- `whoami /user` 与两个账户均非 Administrators 的证据；
- VM snapshot 标识、测试盘为 NTFS；
- 审计人是否独立。项目所有者兼任 reviewer 时必须明确披露“非独立审计”。

所有输出中的用户名、绝对用户路径和测试 secret 在提交 PR 前脱敏。

### 2. 生产 LocalAppData DACL/SDDL

以 Owner 运行：

```powershell
uv run python -c "from app.paths import AppPaths; from app.runtime_storage import prepare_runtime_storage; prepare_runtime_storage(AppPaths.discover())"
$Root = Join-Path $env:LOCALAPPDATA "MeguminCompanion"
$Acl = Get-Acl -LiteralPath $Root
$Acl.Sddl
$Acl.AreAccessRulesProtected
$Acl.Access | Format-Table IdentityReference,FileSystemRights,AccessControlType,IsInherited
icacls $Root
```

逐项检查 root、`state`、`secrets`、`logs`、`temp`：

- SDDL 含 `D:P`；`AreAccessRulesProtected` 为 `True`；
- allow ACE 只有 Owner SID（Windows 可能正规化为等价 SDDL 账户别名，例如内置本地管理员为 `LA`）与 `SYSTEM`，权限为 FullControl，均非从可变父目录继承；
- 不含 `WD`/Everyone、`BU`/Users、`AU`/Authenticated Users allow ACE；
- `chmod`、POSIX mode 或仅看资源管理器“隐藏”均不得作为证据。

### 3. 隔离父目录继承与第二标准账户 effective access

为避免 Owner profile 自身 ACL 掩盖结果，由管理员在 `C:\Users\Public\Megumin-W03-Acl` 建测试父目录：Owner 对父目录 FullControl，Other 对父目录至少 Read/Traverse。然后以 Owner 调用 `WindowsDirectorySecurity`，在其下创建 `App\state\sentinel.txt`。

先在创建前给父目录 Other 读权限，创建后再把父目录 Other 权限改成 Modify；两次都重新抓取 `App` SDDL，必须保持同一受保护 DACL。以 Other 交互登录后直接执行：

```powershell
Get-ChildItem -LiteralPath C:\Users\Public\Megumin-W03-Acl\App -ErrorAction Stop
Get-Content -LiteralPath C:\Users\Public\Megumin-W03-Acl\App\state\sentinel.txt -ErrorAction Stop
Copy-Item -LiteralPath C:\Users\Public\Megumin-W03-Acl\App\state\sentinel.txt -Destination $env:TEMP -ErrorAction Stop
```

三项都必须 `Access denied`。再由 reviewer 在“高级安全设置 → 有效访问”选择 Other，保存无 List/Read/Copy 权限的截图或导出。若 Other 能读取，W03 直接不合格；不得用 Other profile 父目录拒绝来替代这个共享父目录测试。

### 4. DPAPI current-user、错误用户与备份/换机

以 Owner 导入仅用于审计的随机 sentinel，随后立刻从父 PowerShell 删除环境变量。检查 envelope 元数据为 v1/`windows-dpapi`/`current_user`/正确 purpose/key id，并以二进制和文本扫描确认文件、日志、CLI 输出没有 sentinel 明文。

以 Owner 调用 `llm_api_key_file(...).read_text()` 必须成功。由管理员把同一 ciphertext 复制到共享审计介质并只给 Other 读取；Other 将副本放入自己的 `%LOCALAPPDATA%\MeguminCompanion\secrets` 后读取，必须得到稳定的 `secret_decrypt_failed`，不得输出值、ciphertext 或路径。

再把同一密文副本放入第二台干净本地账户 VM；预期不能作为凭据备份恢复。若企业域/漫游配置使它可解密，记录环境并按上述限定文案审查，不能把结果误写成 machine scope。代码审查必须同时确认没有 `CRYPTPROTECT_LOCAL_MACHINE`。

最后验证：篡改/错 purpose 不删除原文件；只有显式 `--reset-secret`/replace/revoke 改变它。审计用 secret 完成后必须撤销。

### 5. 明文迁移与删除文案

- 不带 `--delete-import-source` 导入不同路径 VTS 文件：密文验证成功，明文源仍在。
- 带该开关重跑：验证成功后源文件消失，输出明确 `physical_erasure_guaranteed=false`。
- 源与密文目标相同时，不带开关必须拒绝覆盖；先保留人工确认用备份或显式授权原位替换。
- LLM import 后父 PowerShell 和旧 `.env` 仍需人工清理；不能声称子进程已清除父环境。
- 日志和错误只允许稳定 `llm-api-key`/`vts-token` 或 error code，不得出现 secret 值、ciphertext、认证 header 或完整用户路径。

### 6. temp、占用、崩溃和 junction

在 VM 运行：

```powershell
uv run pytest --no-cov tests/unit/test_temp_assets.py::test_real_windows_occupied_handle_keeps_registry_entry -q
uv run pytest --no-cov tests/unit/test_temp_asset_reparse.py tests/unit/test_secret_reparse.py -q
```

另做一次进程硬终止：在已登记 `.part`、WAV 和 `companion-stt-<32hex>` 目录存在时终止进程，等待超过 scavenger 最小年龄后重启。验收：

- 独占/杀软模拟占用期间条目保留、retry_count 指数增加；释放后才删除并注销；
- 启动只清理私有 root 内、严格命名且足龄的项目；相似但不合法文件保持不变；
- junction 指向 root 外时返回 rejected，外部哨兵不变；目录含 reparse child 时，本地普通文件也不得被部分删除；
- registry 只保存相对路径，不含用户名、正文、音频内容或 secret。

### 7. 只读安装目录

管理员把已构建 wheel/onedir 安装到标准用户只读目录，并记录运行前文件清单/hash。Owner 从仓库外中文/空格 CWD 运行 runtime storage、secret import 和 Mock TTS/STT smoke；运行后安装目录与 CWD hash/文件数不变，所有新文件只出现在 Owner LocalAppData 私有根。任何向安装目录、package resource 或 CWD 写入均不合格。

### 8. 审计签字条件

全部证据齐全后，reviewer 必须明确接受以下四项残余风险和文案，才能回复“审计合格”：

1. 管理员、同用户恶意代码和进程内存不在 DACL/DPAPI 防护边界内。
2. DPAPI 密文备份不是可移植 secret 备份，换账户/换机/丢失用户上下文时必须允许重新输入。
3. 企业域/漫游配置可能改变复制后的可恢复性，不能作绝对“永不可恢复”承诺。
4. revoke/reset/source deletion 不保证 SSD、备份、shell 历史或旧环境中的物理擦除。

若任一项失败，记录复现、预期/实际 SDDL 或稳定错误码，W03 保持 Draft；不得进入 W04。

## 阶段关闭记录

- 关闭日期：2026-07-17。
- 关闭依据：项目所有者在后续开发任务中明确说明“目前已经完成了 W03 的工作，接下来请从 W04 开始”，据此记录 W03 阶段完成与进入 W04 的授权。
- reviewer 独立性：项目所有者兼任 reviewer，属于**非独立审计**。
- 证据边界：本次 W04 工作没有伪造或补写未提供的 VM 截图/附件；W03 的代码与自动证据已经随 PR #13 合并，真实设备证据仍应由项目所有者保管并在需要时关联到发布记录。

## 回滚

- 可禁用真实 LLM、VTS、STT/TTS 并保留文字 Mock；不得回退到明文 token store、machine-scope DPAPI、宽松 DACL 或无 registry 临时文件。
- 审计确认前保留用户明确选择的旧 VTS 明文备份；确认后再显式删除，且只承诺文件级删除。
- 回滚应用版本不会自动删除 LocalAppData、密文、registry 或待重试临时项；用户数据处理必须另行显式执行。
