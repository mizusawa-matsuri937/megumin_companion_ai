# ADR-W06：有界 LLM→TTS→audio→VTS 流水线

- **状态：** 2026-07-17 已批准
- **决策者：** 项目所有者（兼任架构/产品 reviewer）
- **风险：** P1-03、P1-04、P1-05、P1-24、P2-03、P2-04
- **后续 PR：** W07～W09、W17

## 背景

TTS/audio queue、文本、provider 输出和临时资产缺少端到端硬上限；timeout 有多个来源，LLM 截断 EOF 可能被当作成功。慢 TTS 或阻塞音频会把内存和磁盘增长转移到上游。

## 决策

- 采用 Windows 计划第 4.8 节的首轮硬上限；只有基准和人工体验证据可以调整数值，不能移除上限。
- TTS job queue 最大 8、ready audio queue 最大 4、在途音频最大 64 MiB；producer 必须对慢消费者背压。
- LLM 完整输出最大 64 KiB UTF-8、128 segments，并受 provider token 预算约束。
- 连接、首字节、总时长和取消 deadline 分层且各自只有一个配置源。
- LLM 明确区分 finish、`[DONE]`、合法 EOF、截断和协议错误；达到限制是受控终止，不伪装成功。
- VTS action 带 turn generation；取消或重连 purge 旧 generation。
- 达到上限时 UI 显示 busy、truncated、audio degraded 或 failed，不静默丢数据。

## 备选与取舍

- **可行备选：** 完全串行 TTS。资源模型更简单但延迟更高，可作为低资源/故障降级模式。
- **拒绝：** 无界 queue；只限制字符而无 token/byte/segment 预算；多个 timeout 配置源；把截断 EOF 当成功；已播放部分后透明重试整轮。

## 所有权、取消和清理

- 每个 queue 有唯一 producer/consumer owner 和 close/drain 语义。
- 新用户输入提高 cancellation generation；旧字幕、音频、VTS 动作和临时文件不得恢复或补播。
- 未消费 TTS 文件在取消时原子脱链并进入 temp registry 清理。
- 远端 provider 可能在本地取消后继续计费；UI 和诊断明确本地已停止但不承诺远端撤销。

## 迁移和兼容性

新增统一 limits/timeout config schema。长回复的截断行为可能改变，必须在 UI 和 provider 错误映射中可解释；不改变默认离线、Mock、silent 行为。

## 验收

- 快 LLM/慢 TTS、阻塞 audio、半流 EOF、超长输出、emoji/中日英混合、取消风暴和磁盘受限故障注入。
- 证明 queue、内存、temp、日志和关闭时间上界。
- 人工审查截断文案、已播放语义和真实低额度 provider 的计费风险。

## 回滚

可以降低并发或切换串行 TTS、silent playback、禁用 VTS；不得恢复无界 queue、双重 timeout 或透明整轮重试。
