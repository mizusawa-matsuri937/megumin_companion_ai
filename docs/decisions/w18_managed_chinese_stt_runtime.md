# W18：受管本地中文 STT 运行时选型

> 决策日期：2026-07-23（Asia/Shanghai）
> 范围：单机、单 Windows x64 用户、个人私用；不适用于 Windows ARM64、云端转写、多语言或自动更新。
> 状态：已由聚焦提交 [`224e06f`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/224e06f9cbb1d2ab0cc2260fb244b1f74cd7dfbc)
> 实现，并完成其 exact-head PR/push 双 OS `quality` / `installed-wheel` 核验；真实设备 Gate 与后续 head 核验仍未完成，不能表述为已发布或已修复上游安全问题。

## 已确认的选择

W18 沿用既有 `MediaWorker` / Job Object / `WhisperCppRunner` 调用链，不另起 STT 架构。受管配置固定为
CPU 离线 profile `whispercpp_base_q5_1`，只允许传入 `--language zh`。

| 项目 | 固定值与来源 | 完整性约束 |
| --- | --- | --- |
| runtime | [ggml-org/whisper.cpp v1.9.1](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.1) 的 `whisper-bin-x64.zip`；该 tag 页面显示提交 `f049fff` 为 GitHub verified signature | archive SHA-256 `7d8be46ecd31828e1eb7a2ecdd0d6b314feafd82163038ab6092594b0a063539`，下载上限 16 MiB；安装后 `whisper-cli.exe` SHA-256 `58245314fb73b30fbd0cf0542c5c172e23f02b6eb7cad7b51e792439cf5e1755` |
| model | [ggerganov/whisper.cpp 的不可变 revision `87cd18b`](https://huggingface.co/ggerganov/whisper.cpp/blob/87cd18b47b941d2f65d09981dad23bb7d0481c77/ggml-base-q5_1.bin) | SHA-256 `422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898`，下载上限 80 MiB |
| 许可证 | [whisper.cpp MIT](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE)；模型页标示为 MIT | 没有声称完成独立法律、许可证或安全审计 |
| 本地目录 | `%LOCALAPPDATA%\MeguminCompanion\models\stt\whispercpp\v1.9.1\` | `bin\whisper-cli.exe` 与 `model\ggml-base-q5_1.bin`；模型和原生资产不进入仓库、wheel、安装包或 CI artifact |

模型页标示的远端文件为 59.7 MB（约 57 MiB），这是文件体积而不是 Windows 峰值工作集。实际 30 秒中文 PTT 的
`whisper-cli` `PeakWorkingSetSize` 仍须真实设备 Gate 测量；通过上限是 **≤512 MiB**，超限必须阻止交付并重新选型。

## 运行与隐私边界

- 默认 `stt.enabled=false`；启动、设置刷新和 worker preflight 不下载模型，也不访问麦克风。
- 只有用户确认 UI 操作或 `--install-chinese-stt` 才会调用固定清单。服务不接受 URL、模型名或 hash 输入。
- 下载仅接受 HTTPS 链、有限重定向、限时和限大小；先校验 archive/model，再在受 current-user DACL 保护的 staging
  目录中验证 ZIP 路径、链接与重解析点，最后原子切换。取消或失败只清理 staging，不删除已验证资产或手工路径。
- 安装完成只写受管 `executable`、`model_path`、`provider` 与 `language=zh`；不会开启 STT、改变设备或线程数。
- 手工路径仍兼容，但状态明确为 `unmanaged`，且不会附加受管 hash。受管路径由每个 MediaWorker 首次使用时再完整
  SHA-256 校验，按文件状态缓存；不匹配时在启动 `whisper-cli` 前返回 `stt_runtime_integrity_failed`。
- 模型只由一次 PTT 的 `whisper-cli` 子进程加载，结束即退出；不加入 PyTorch、ONNX Runtime 或 GPU 依赖。

## 备选与取舍

- 没有采用 cloud STT：它增加了语音外发、凭据和网络可用性边界，且与离线/默认不联网目标冲突。
- 已调研 sherpa-onnx 等本地项目，但没有采用：它会替换当前已经隔离并测试的 `whisper.cpp` worker 路径，带来新的
  runtime/model格式和依赖面，而不是完成 W18 已缺失的受管供应。
- 没有采用更大的 Whisper 模型或 GPU 推理：本任务优先限制下载、常驻依赖和内存风险；准确率与实际内存仍以真实 Gate 为准。

## 已知安全状态与残余风险

NVD 对 [CVE-2026-10298](https://nvd.nist.gov/vuln/detail/CVE-2026-10298) 的描述列出影响范围“up to 1.8.2”，
并未把 v1.9.1 列为该记录的受影响版本；这不是 v1.9.1 已修复的证明。相关的
[上游 issue #3807](https://github.com/ggml-org/whisper.cpp/issues/3807) 在本次复核时仍为 open，并描述了恶意模型导致
进程 abort 的问题。因此本项目不声称问题已修复。

固定受管模型的不可变 URL、下载哈希和 worker 使用前全量哈希，能阻止受管路径在不匹配时把被替换的模型交给 CLI；
它不构成对已完全控制当前 Windows 用户或用户主动配置的非受管模型的安全保证。Job Object 继续把 native CLI 崩溃/卡死
限制在 MediaWorker 子树，但不替代上游修复或独立安全审计。

## 关联

- [W18 实现记录](../implementation/w18_push_to_talk_whisper.md)
- [ADR-W07：native worker 隔离](../adr/ADR-W07-native-worker-isolation.md)
- [ADR-W08：包与升级边界](../adr/ADR-W08-packaging-upgrade.md)
- [Windows 数据流与保留清单](../architecture/windows_data_flow_inventory.md)
- [Windows 威胁模型](../security/windows_threat_model.md)
