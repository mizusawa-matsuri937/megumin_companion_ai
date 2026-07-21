# ADR-W01：Windows 运行拓扑

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任架构/安全 reviewer）
- **风险：** P0-04、P1-13～P1-16、P1-20、P1-21
- **后续 PR：** W12、W13、W17、W21

## 背景

Qt 要求 UI 主线程所有权，当前后端以 asyncio/FastAPI lifespan 为 owner，PortAudio、OCR 和外部 STT 又可能永久阻塞。把 UI、asyncio 和 native 调用放入同一事件循环会产生双重 owner、UI 冻结和不可完成的隐私关闭屏障。

## 决策

```text
MeguminCompanion.exe
├─ Qt 主线程：Widgets、窗口、托盘、IME、热键状态和用户交互
├─ BackendThread：独立 asyncio loop、TurnService、memory、providers、VTS
├─ ApplicationBridge：typed command/event，有界 64/512，Qt signal 只传脱敏数据
├─ MediaWorker：麦克风、播放、whisper.cpp 子树，受 Job Object 监管
└─ PerceptionWorker：窗口复核、捕获、OCR、遮挡和 wipe，受 Job Object 监管
```

- Qt 主线程不得执行阻塞网络、数据库、音频、OCR 或 STT 工作。
- BackendThread 是业务异步生命周期的唯一 owner；UI 不直接访问 repository/provider。
- worker 是同一安装包内的受监管实现细节，不是网络服务。
- FastAPI 不作为生产桌面与后端之间的通信层。

## W17 实施校准（2026-07-22）

- 系统播放已按本决策落在专用 `MediaWorker`：父侧 `app.media`、bootstrap 和 BackendThread 不导入或调用
  PortAudio / `sounddevice`，native binding 只位于 helper-side `app/media/worker.py`。
- 父侧经既有匿名 pipe / typed JSON 提交 `media.devices`、`media.play`、`media.release`。播放请求只携带
  W12 批准根下的 symbolic `ResourceReference`，不携带任意路径、WAV body、PCM 或 PortAudio index。
- Qt 设置界面的设备枚举是显式用户命令，临时 worker 完成枚举即关闭；普通设置快照不会隐式接触声卡。
- 这是当前工作树和自动化 fake 的实现状态，不代表真实内置、USB 或蓝牙设备体验已验收；这些仍属于 W17
  人工设备 Gate。

## 备选与取舍

- **可行备选：** backend 也放入独立 OS 进程并通过带 DACL 的 pipe 通信。隔离更强，但增加状态恢复、部署和调试复杂度，首版不采用。
- **降级备选：** 永久关闭真实音频和视觉，只发布文字模式。保留为 worker 方案失败时的安全回滚。
- **拒绝：** Qt 主线程直接承载全部 asyncio/native 工作；以 `PySide6.QtAsyncio` technical preview 作为唯一发布架构；用 `asyncio.wait_for` 声称停止卡死线程。

## 所有权与失败语义

- UI 关闭请求先阻止新命令，再让 BackendThread 取消 turn、停止维护任务并关闭 DB。
- worker 先 soft cancel；超过 deadline 后由 supervisor 终止整个 Job Object。
- BackendThread crash 必须变成 UI 可见的 degraded/failed 状态，不允许 UI 假装仍在线。
- worker crash 可以按 crash budget 重启；超过预算后 quarantine 对应 feature，文字功能继续。

## 迁移、隐私和兼容性

当前 FastAPI 单 owner 组装将逐步迁移到 app factory 和 BackendThread host。worker 只返回 transcript 或脱敏摘要；原始 PCM、截图和 OCR buffer 不返回主进程。首发只承诺 Windows 11 x64 标准用户。

## 验收

- 100 次启动/关闭和 backend crash/restart，Qt 主线程无阻塞。
- hanging worker 能在 hard deadline 内终止，Job 子树归零。
- 10k delta 合并后 queue、内存和关闭时间仍有上界。
- 人工复核 owner graph、shutdown 顺序和中文 IME/DPI 行为。

## 回滚

W13 UI spike 可整体移除并退回 CLI/dev API；native feature 可强制 disabled，保留文字模式。不得回滚到生产无认证 HTTP/WS 或卡死 thread 模型。
