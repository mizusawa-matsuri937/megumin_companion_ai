# ADR-W07：native worker 隔离、hard timeout 和敏感清理

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任安全/隐私 reviewer）
- **风险：** P0-04、P1-13～P1-16、P1-20、P1-21
- **后续 PR：** W12、W17、W18、W21、W22

## 背景

`asyncio.to_thread` 和 `wait_for` 无法停止卡死的 OCR/PortAudio native 调用；whisper 还可能创建子进程树。等待 drain 可以保护正常清理，却可能让退出或 vision disable 永久阻塞并继续持有截图、PCM 或设备。

## 决策

- `WorkerSupervisor` 使用 `CREATE_NO_WINDOW` 和 Job Object `KILL_ON_JOB_CLOSE` 启动 MediaWorker/PerceptionWorker。
- worker 必须有版本化 handshake、heartbeat、soft cancel、hard deadline、crash budget 和 quarantine。
- soft cancel 失败后允许终止整个 Job Object；终止后运行 temp scavenger 并验证子树归零。
- 原始 PCM、WAV、截图、OCR/raw buffer 只存在于 worker；主进程只接收 transcript 或脱敏摘要。
- helper protocol 不接受任意路径、reparse-point escape、Python object 或未批准 metadata；文件通过批准根或继承 handle 访问。
- worker stderr 使用 1 MiB 无内容诊断 ring；禁止记录 transcript、OCR、窗口标题、图像或用户路径。

## 云视觉边界

项目所有者允许云视觉，但仅能在指定窗口捕获、本地 Guard、全 OCR bbox 遮挡和最终隐私检查全部成功后，由用户显式启用的路径上传脱敏图像。任一检查未知、错误、超时或 worker 重启都必须跳过上传；不得回退到全屏截图。实际启用仍受 W21/W22 隐私 Gate 约束。

## 备选与取舍

- **可行备选：** 永久关闭真实音频/OCR，只保留文字模式。安全性高但不满足完整产品目标，作为 quarantine/回滚路径。
- **拒绝：** 用 thread timeout 宣称 native 已停止；强杀前没有 owner/temp registry；worker 返回原始截图/PCM 给主进程；无提示捕获其他窗口。

## 失败、恢复和隐私

- crash budget 内可以重启；超过预算后 feature actual state 为 failed/quarantined，用户手动恢复。
- vision 的 disabled 只有在 worker 终止、队列清空、buffer/temp 清理和上传任务取消后成立。
- 杀进程不能证明内存或 SSD 物理擦除；只承诺生命周期隔离、best-effort wipe 和残留扫描。

## 验收

- 注入永久卡死、子进程孙树、文件锁和 crash loop，证明 hard deadline 和 Job 子树归零。
- 敏感 sentinel 图像/文字/PCM 不出现在主进程事件、日志、temp 残留或诊断包。
- 多显示器、DPI、锁屏、RDP、管理员窗口和指定窗口范围由真实 Windows Gate 验证。

## 回滚

强制 `vision=false`、`stt=false`、`playback=silent` 并移除 worker artifact；文字模式继续。不得回滚到同进程卡死线程或未脱敏云上传。
