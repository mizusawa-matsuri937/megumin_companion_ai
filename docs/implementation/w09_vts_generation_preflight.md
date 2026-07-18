# W09：VTS 重连、turn generation 与 capability preflight

> 分支：`codex/w09-vts-generation-preflight`
>
> 权威范围：`docs/windows_development_plan.md` 的 W09、4.8、4.9 与统一 DoD
>
> 风险：P1-06、P1-07、P2-04

## 契约与失败语义

- `vts.reconnect_initial_seconds` 与 `reconnect_max_seconds` 都严格大于零，且 max 不小于 initial。
- 只有连接/timeout 类故障进入重连；重连使用 capped exponential base 与 equal jitter，实际 delay 在
  `0.5 * base..base` 内且不超过 max。sleep 与 random 都可注入，测试不依赖真实时间。
- 认证拒绝/撤销、secret store、协议、API 配置、未加载模型、缺少 neutral/配置 hotkey 都进入稳定
  `disabled`，不热重试。`disabled` 只拒绝 VTS expression sink；turn/provider/文字事件继续。
- capability preflight 顺序为 API availability → authentication → current model → current-model hotkeys。
  snapshot 只保留布尔值、计数和稳定 error code，不返回 token、模型 ID/name/path 或 hotkey ID/name。
- event sink 在 `turn.accepted`/`proactive.accepted` 推进可信 generation。action 同时携带 `turn_id` 和
  generation，并在入队和消费前各校验一次。
- cancel/failed、新 turn、断线都会提高 generation 并 purge queue；neutral reset 是独立高优先级状态，
  在 preflight 成功后先于普通 expression 执行。断线与 action 同时 ready 时，断线优先，action 不执行。
- queue owner 是 `VTSBridge`，容量沿用 `vts.queue_capacity`（1..256）。overflow 淘汰最旧普通 action；
  neutral 不进入普通 queue，因此不会被 overflow 淘汰。close 负责停止、purge、socket close 与 task join。

## VTube Studio 协议依据

实现只使用官方 Public API 1.0 的 `APIStateRequest`、`AuthenticationRequest`、`CurrentModelRequest`、
`HotkeysInCurrentModelRequest` 与 `HotkeyTriggerRequest`：

- <https://github.com/DenchiSoft/VTubeStudio>

fake server 会校验 `apiName`、`apiVersion`、request correlation、认证、model/hotkey preflight、断线和重连。

## GPT-SoVITS reference 边界

`GPTSoVITSPresetConfig.ref_audio_scope`/`GPTSoVITSPreset.ref_audio_scope` 明确区分：

- `service_resource`（默认）：字符串由 GPT-SoVITS 服务解释，可代表远端主机或容器资源；应用不调用
  本机 `Path.exists()`，不假定 Windows 盘符对服务可见。
- `local_file`：只允许 loopback GPT-SoVITS endpoint，并在 provider 组装时检查绝对本机 regular file；失败只给
  content-free error，不回显路径。应用只验证可见性，不复制或提交 reference 音频。

该字段为有默认值的加法变更，不提高 settings schema version；旧 preset 保持 `service_resource` 语义。

## 隐私与日志

- VTS client 给 `websockets` 注入 WARNING 级专用 transport logger，防止 DEBUG frame 把认证 token 写入
  应用日志。
- VTS snapshot/preflight 不持有 capability 明细；action 不含正文；event sink 只读取 expression metadata。
- fake-server 隔离测试向 token、正文、模型与用户路径注入 sentinel，并扫描应用 LogRecord、snapshot 和
  VTS request，任何命中都会失败。

## 共享面、迁移与回滚

共享文件的最小修改：

- `app/config/settings.py`：零退避拒绝；增加默认化的 `ref_audio_scope`。
- `app/bootstrap.py`：已复核且无需改动；继续只负责把 settings 注入 bridge/provider。
- `app/resources/default_config.yaml`：说明 reference scope；默认 VTS 仍禁用。

无数据库、secret envelope 或用户文件迁移。回滚可设置 `vts.enabled=false`，文字/TTS 主流程继续；不得回滚
为零/无界重试或允许旧 generation 重放。`local_file` 可回滚为显式 `service_resource`，但远端服务资源的
可见性仍由服务所有者负责。

## 自动证据与人工边界

本地最终证据（2026-07-18）：

- W09 聚焦矩阵：`95 passed`，覆盖 fake clock/random、backoff/jitter/cap、重连风暴、零退避、断线交错、
  generation、cancel/new-turn/disconnect purge、queue overflow、neutral、auth 撤销/超时、model/hotkey、
  protocol、日志 sentinel 和 close race。
- 全仓：`787 passed, 2 skipped`；branch coverage `90.16%`。
- Ruff lint、Ruff format、strict mypy（171 source files）通过。
- 最终 Windows installed-wheel source-quarantine smoke 通过：107 members，manifest SHA-256
  `f4a6e8edd58de20654f648054abc3b3fa32977f9282837cf9a04b76a90134cc1`；未把 wheel/evidence 纳入 PR。
- GitHub Windows/macOS `quality` 与 `installed-wheel` 矩阵须在 Draft PR final head 上另行验证；当前本地记录
  不把本机 Windows 结果冒充双平台证据。

真实 VTube Studio Allow/token UI、真实模型 hotkey 的视觉语义和最终 reviewer 签字保持人工 Gate，不以 fake
server 冒充。
