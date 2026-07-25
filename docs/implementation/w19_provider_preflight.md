# W19：真实 VTS/GPT-SoVITS 配置向导与联动

> 状态：实现与完整本地质量门已通过；功能提交
> [`d641c29`](https://github.com/mizusawa-matsuri937/megumin_companion_ai/commit/d641c29b744bf400d46fe4453aefec0ed69044ee)
> 已推送，Draft PR [#32](https://github.com/mizusawa-matsuri937/megumin_companion_ai/pull/32) 已创建；exact-head CI 待核验。最后核验：
> 2026-07-25（Asia/Shanghai）。工作分支 `codex/w19-provider-preflight`，基线为已合并 W18 的
> `90e758d55a87e330a45260aaccd8c794069e3f7a`。
>
> 本文只记录当前工作树和可复现证据。fake HTTP/WebSocket、headless Qt、合成 WAV 和静音回归不能证明真实
> GPT-SoVITS 声音/延迟、真实 VTube Studio Allow/模型表情或服务重启体验。

## 范围与已实现契约

W19 在 W16 设置对话框增加可保存的 GPT-SoVITS 默认 preset/reference 字段和独立“服务预检”页，复用 W08
`GPTSoVITSProvider`、W09 `VTSClient`/`VTSBridge` 与 W16 BackendThread 管理面，不引入新的 provider client、网络
协议、依赖或 worker。

- `ProviderPreflightCommand` 只在用户确认后运行，并且只读取已经保存、重新校验过的设置；未保存的 Qt 表单必须先保存。
- 完整 snapshot 固定为七项：TTS service/preset/reference 和 VTS API/auth/model/hotkeys。事件只含 enum、稳定
  reason code 和缺失 hotkey 数量，不含 token、模型/hotkey ID、reference 路径、prompt、provider body 或 WAV 路径。
- GPT-SoVITS 先执行既有、显式 deadline 的 API v2 route probe。端到端 preset/reference 检查只发送固定短语
  `连接测试`；成功 WAV 不进入播放队列、不启用持久 cache，并立即调用 provider 的清理所有权路径。
- `/tts` 只提供组合式 preset/reference 检查，因而合成失败时不能可靠断定两者中哪一项单独失败；UI 如实将二者都标记失败，
  而不伪造更精细结论。
- VTS 继续使用官方 API 1.0 授权流。首次或失效 token 的 Allow 只能由用户在 VTube Studio 窗口内完成；应用不模拟点击、
  不接受替代 token 回显。bridge snapshot 现在显式记录 API、认证和模型三个已观察阶段，失败页不再只靠最终错误码反推。
- TTS 和 VTS 检查并发运行，但各自失败只更新自身状态。运行结束后 provider/bridge 均有有界 close；VTS 断线显示
  reconnecting，认证/配置类失败显示 disabled 分阶段结果。
- 默认 `tts.provider=mock`、`vts.enabled=false`、`pipeline.playback_mode=silent` 不变；设置保存继续标记
  “重启后生效”。W19 不把预检结果写成运行时启用或真实体验通过。

## 失败、恢复和旧 turn

- TTS preflight 失败不修改 TurnService、历史、字幕或 playback 状态；正常对话的既有 W07/W08 降级仍保留文字并发出
  无内容状态。
- VTS preflight/运行失败只影响 expression sink。W09 的 generation、cancel/new-turn/disconnect purge 和 neutral
  recovery 不变；恢复后不能消费旧 generation 的 action。
- W19 没有透明重试整轮、没有自动播放测试音频，也没有向 provider 发送历史/草稿。远端在本地取消后是否已执行计算仍属于
  W08 记录的外部服务残余风险。
- 回滚开关为 `tts.provider=mock`、`pipeline.playback_mode=silent` 和 `vts.enabled=false`；文字输入/输出不依赖
  W19 预检成功。

## 开源调研与复用决定

2026-07-25 对直接相关上游做了实施前复核：

| 上游 | 许可证/维护 | 采用决定与边界 |
| --- | --- | --- |
| [VTube Studio API](https://github.com/DenchiSoft/VTubeStudio) | 官方仓库，MIT；API 1.0 文档仍维护 | 继续复用 W09 client/bridge 和官方 request types，不复制上游代码、不引入第三方 wrapper |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) | 官方仓库，MIT；仍有发布活动 | 继续复用 W08 API v2 adapter；不安装、打包、启动、升级或管理该服务 |
| [GitHub Security Lab GHSL-2025-045～048](https://securitylab.github.com/advisories/GHSL-2025-045_GHSL-2025-048_RVC-Boss_GPT-SoVITS/) 与 [GHSL-2025-049～053](https://securitylab.github.com/advisories/GHSL-2025-049_GHSL-2025-053_RVC-Boss_GPT-SoVITS/) | 分别公开披露命令注入和不安全反序列化/RCE，测试版本均为 `20250228v3` | 不把“服务可达”写成“服务安全”；应用只连接用户明确配置的 endpoint，并保持服务进程/资产在包外 |

GPT-SoVITS API v2 没有独立、无副作用的 reference 可见性接口；`/set_refer_audio` 会改变服务状态。因此 W19 不调用该
route，而采用用户明确触发的一次固定文本 `/tts` 合成来验证 service/preset/reference 组合。该选择避免擅自改变远端默认
reference，但仍会触发一次远端推理，UI 在运行前明确说明。

## 自动化证据

当前工作树的扩展聚焦矩阵：

```text
uv run pytest --no-cov \
  tests/unit/ui/test_w19_provider_preflight.py \
  tests/unit/test_vts_bridge.py \
  tests/integration/test_vts_fake_server.py \
  tests/integration/test_w07_bounded_pipeline.py \
  tests/integration/test_w08_provider_semantics.py \
  tests/unit/ui/test_w16_management.py \
  tests/unit/ui/test_w17_media_settings.py
```

结果为 **60 passed in 9.70s**。该矩阵覆盖：

- fake GPT-SoVITS API v2 route 与合成响应、固定测试文本、WAV discard、错误 preset/reference、上游 body/path/prompt
  sentinel 不进入 bridge event；
- VTS cold start、Allow/action-required 状态、auth revoke、model/hotkey 缺失、disconnect/reconnecting 与 close；
- W09 fake WebSocket capability server、generation/cancel/disconnect purge、恢复后旧 turn 不重放；
- GPT-SoVITS/VTS 失败下的文字/音频降级、设置 YAML 持久化、重启生效标记和 headless Qt 操作门槛。

此前首次测试在沙箱内因 pytest 临时目录 ACL 拒绝而终止；同一测试集合在获准的沙箱外运行后通过。该现象是执行环境
权限限制，不是产品断言失败。随后完整 `uv run pytest` 为 **1252 passed, 3 skipped in 178.67s**，raw branch
coverage **90.13%**；skip 是可选 RapidOCR、可选 Pillow 和当前账户不能创建 directory symlink，不是 W19 断言失败。
`uv run ruff check .`、`uv run ruff format --check .`（244 files）、`uv run mypy`（237 source）、
`uv lock --check`、`git diff --check` 与 6 份本轮文档的相对 Markdown link 检查均通过。
临时 wheel 的 W05 隔离安装 smoke 也通过：`source_tree_imported=false`、138 members、manifest SHA-256
`54d3d2e8bcfe13b418b792c8cca0ca4e06d4dca181bfc9a2a777f7ce63b2cae3`；wheel/evidence 已删除，没有作为
release artifact 保留。最终 diff、敏感内容扫描和完成前上游复核已通过；发布仍待完成，因此本页尚不把 W19
写成已交付。

## 隐私、安全与资产边界

- 测试只使用合成 WAV、合成 sentinel 和 fake provider；没有真实 reference、角色声音、模型、token、截图、录音或对话进入
  仓库、fixture、日志、event 或 artifact。
- 用户配置的 reference 路径和 prompt 会保存到 current-user settings YAML，并在显式预检/正常合成时发给用户配置的
  GPT-SoVITS endpoint；它们不是 secret，仍属于不应进入日志/诊断/PR 的私密配置。
- `service_resource` 由远端/容器服务解释，本应用不能用本机 `Path.exists()` 证明可见；只有端到端 `/tts` 成功能证明该次
  组合可用。`local_file` 仍只允许 loopback endpoint，并由既有 provider 在构造时检查本机普通文件。
- W19 没有独立审计 GPT-SoVITS 或 VTube Studio 的全部上游代码/依赖。MIT 许可证、维护活动和公开 advisory 的复核不等同于
  法律或安全认证。
- 最终复核时 GPT-SoVITS 主分支 changelog 仍有 2026-04 的维护记录，release 页的最新正式 release 仍为
  `20250606v2pro`；上述 CVE/NVD affected 记录指向 `20250228v3` 及以前。版本范围信息不能反向证明更新版本安全，
  所以 W19 不选择、下载或批准任何 GPT-SoVITS 版本。

## 未验证项与人工 Gate

以下只能在 exact PR head、使用所有者批准的非敏感测试资产和低额度/本地服务时执行：

1. 在真实 VTube Studio 内完成首次 Allow，确认撤销后重新 Allow、实际模型/hotkey 表情语义，以及断线/服务重启后的视觉状态。
2. 在真实 GPT-SoVITS 上确认声音、首音延迟、取消和服务重启；测试 reference/文本不得含受保护角色资产或私人录音。
3. Gate W4 / Beta B 的内置、USB、蓝牙三类设备组合仍未执行；fake/headless/silent 结果不能替代。

RDP、快速切用户、跨 session 和第二 Windows 用户属于当前单机、单用户、私人使用范围外，不写作已通过，也不列为 W19
人工 Gate。

## 发布与回滚状态

W19 功能提交 `d641c29b744bf400d46fe4453aefec0ed69044ee` 已推送至远端分支，Draft PR #32 的 base 为
`agent/windows-development-baseline`、head 为 `codex/w19-provider-preflight`。本状态记录提交推送后，仍须核验其
最终 exact head 的 macOS/Windows `quality` 与 `installed-wheel` 检查。用户现有 `AGENTS.md` 无关改动保持未暂存，
没有进入 W19 PR。

回滚不删除用户资产或 secret：将 TTS 改回 mock、播放改回 silent、VTS 关闭即可恢复文字-only；如撤回 UI/command，
保留既有 W08/W09 provider 与 generation 安全语义。
