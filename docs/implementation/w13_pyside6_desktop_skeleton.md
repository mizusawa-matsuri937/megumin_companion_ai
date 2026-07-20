# W13：PySide6 技术 spike 与桌面骨架

## 状态与边界

- 基线：`agent/windows-development-baseline` 的 `4fe8d27d864716ad786b21f9ac0948b302ae9b9c`。
- 依赖：W12 PR #21 已合并，当前仓库没有 open PR。
- 风险：P0-04；ADR-W01、ADR-W02 已批准的 Qt 主线程、BackendThread 和进程内 bridge 拓扑。
- 本 PR 只交付桌面技术骨架。真实 `TurnService` 组装、session replay、聊天 streaming、重试和恢复仍属于 W14。

W13 不启动 Uvicorn、不监听 TCP，不接入真实 provider、数据库、音频、STT、VTS 或感知。默认窗口中的发送/停止控件存在，但 `SkeletonBackendRuntime` 明确上报 `text_chat=false`，因此不会伪装成已经完成的聊天产品。

## 依赖与许可决策

- 锁定 `pyside6-essentials==6.11.1` 与传递依赖 `shiboken6==6.11.1`；W13 只使用 QtCore、QtGui 和 QtWidgets。
- 选择 Essentials 而不是完整 Addons 元包，减少未使用模块、下载面和后续 bundle/SBOM 面积。官方说明 PySide6 wheels 自带 Qt binaries，Essentials 是 PySide6 的基础分发之一：<https://doc.qt.io/qtforpython-6/package_details.html>。
- 官方 Qt for Python 页面将社区版描述为 LGPLv3/GPLv3，另有 commercial license：<https://doc.qt.io/qtforpython-6/>。第三方许可证清单见 <https://doc.qt.io/qtforpython-6/licenses.html>。
- `PySide6.QtAsyncio` 仍由官方标为 technical preview，因此没有作为 W13 的 event-loop owner：<https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html>。

以上只证明技术选型和上游声明，不是法律意见或公开分发许可签字。当前私人 spike 可继续；W24/W26 在生成 onedir、SBOM 和公开发布候选前，仍须人工复核 LGPL 合规方式、notice/license 文件、实际 bundle 模块和第三方清单。

## 所有权与协议

```text
Qt main thread
├─ MainWindow / DesktopViewModel（仅内存中的草稿与可见消息）
├─ ApplicationBridge command deque：最大 64
├─ ApplicationBridge event deque：最大 512
└─ BackendThreadHost：唯一 thread generation owner
   └─ QThread
      └─ asyncio.run(...)
         └─ BackendRuntime（W13 默认 SkeletonBackendRuntime）
```

- Qt 主线程只创建/更新 Widgets，并通过 `submit_command()` 做非阻塞 `put`；queue 满时返回 `False` 和稳定码 `command_queue_full`。
- BackendThread 在自己的 OS thread 内创建和销毁 asyncio loop；Qt 主线程不运行 socket、数据库或 provider coroutine。
- bridge 复用 v1 `UserMessage`、`TurnInterruptRequest` 和 `PipelineEvent` 业务 payload；不复用 dev API token、Origin、client 身份或网络 envelope。
- backend 到 Qt 的 `events_available` signal 不带正文参数，只通知主线程 drain 有界 queue；拒绝 signal 只携带稳定 reason code。
- 相邻同 turn 的 `assistant.delta` 在入队时合并。单 delta 上限 64 KiB；10,000 个小 delta 不会变成 10,000 个 Qt queued signal。
- event queue 溢出时清空非权威增量并插入 `event_snapshot_required` marker；终态事件与 marker 同时保留。W14 必须用 authoritative snapshot 完成恢复，不能拼接缺口后的旧文本。

## 生命周期与失败语义

1. Host 启动 generation，QThread 内创建 asyncio loop。
2. runtime 发布 `starting`/`ready`；异常正文不跨 bridge，只发布 `backend_crashed`。
3. crash 最多自动重启 1 次；配置硬上限为 3，不能形成无界 crash loop。
4. 窗口关闭先拒绝 close、禁用新命令并请求 backend stop；thread 完成后才清空 editor/model/bridge 并真正关闭窗口。
5. 已完成 QThread 由 host 暂存且最多保留 4 个 wrapper，避免 Qt deferred-delete teardown abort，同时保持重复生命周期有界。
6. `aboutToQuit` 也会请求停止；event loop 返回后只允许在 UI 已退出阶段做最终 bounded wait。

BackendThread 只承载可取消 asyncio 业务；不可取消 native 工作仍必须走 W12 WorkerSupervisor/Job Object。Python 无法安全强杀卡死 thread，W13 不把等待 timeout 冒充 hard kill。

## UI 与隐私

- 最小窗口只有消息区、内存编辑器、发送、停止、后端状态和 feature 状态。
- `QPlainTextEdit` 保留原生中文/日文 IME、复制粘贴和多行编辑；`Ctrl+Enter` 是显式发送快捷键，普通 Enter 不被劫持。
- 控件设置 accessible name 和确定的 tab 顺序；Qt 6 原生 high-DPI 行为不通过固定像素布局覆盖。
- transcript 使用 plain text，不把用户/assistant 内容当 HTML 渲染。
- 草稿、可见消息和 command queue 不写文件、日志、诊断或数据库；最终 close 显式清空 editor、model 与 bridge queue。
- UI 只显示经过格式验证的稳定 reason code；backend exception body 不跨线程，也不进入状态标签。

## 自动验证

W13 focused suite 覆盖：

- 完整 shell owner graph 100 次启动/关闭；另对同一 host 做 100 次 BackendThread generation 启停；
- backend crash、单次自动 restart、意外提前返回和 restart 配置上限；
- 10,000 delta 合并、64/512 queue 上限、超大 delta、overflow/snapshot-required 与终态保留；
- fake event timeline 的 empty/error/loading、中文/日文 plain-text 渲染、停止命令；
- fake backend 人为停顿 200 ms 时，send click 在 100 ms 门槛内返回且 Qt `QTimer` 继续触发，证明测试路径没有把该异步工作放在 UI thread；
- installed entry point 的 offscreen 启动/关闭（沿用 provenance 的 `desktop_preflight` 字段）和 production desktop PID 零 TCP listener。

当前本地验证：

- W13 UI + desktop CLI focused：`45 passed`。
- 单独统计 `desktop_client.ui`：20 tests passed，raw branch coverage `91%`。
- 全仓：`1,095 passed, 2 skipped`，raw branch coverage `90.46%`；两个 skip 分别是缺少确定性测试字体和当前用户不能创建目录 symlink。
- Ruff lint、Ruff format（214 files）、strict mypy（207 source files）和 `git diff --check`：通过。
- Windows production desktop PID 探针：`desktop_return=0`、`uvicorn_loaded=false`、TCP socket count `0`。
- 本地 wheel/source-quarantine smoke：`status=ok`、`source_tree_imported=false`、installed desktop headless smoke true；PySide6 Essentials/Shiboken 均为 6.11.1。
- `uv lock --check`：65 packages 已同步。

最终交付以最终 commit 的安装产物 provenance 和 push/PR 的 Windows/macOS exact-head CI 为准；不得用 pre-commit local evidence 替代 CI。

## 尚未关闭的人工 Gate

- Windows 11 标准账户：中文/日文 IME composition、复制粘贴、Ctrl+Enter 与键盘导航。
- 125%、150%、200% DPI 和多显示器移动。
- Narrator/屏幕阅读器的名称、焦点顺序和状态朗读基础体验。
- Qt/PySide6 6.11.1 的最终私人使用许可接受；若未来公开分发，重新执行 bundle/SBOM/notice 法务复核。

## 回滚

移除 `desktop_client/ui/`、PySide6 Essentials 依赖和 W13 entry-point/smoke 变更，即可退回 W01 的 CLI/dev API；不得恢复生产无认证 HTTP/WS，也不得把 asyncio/native 工作迁回 Qt 主线程。
