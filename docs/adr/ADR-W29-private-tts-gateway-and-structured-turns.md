# ADR-W29：私有五槽 TTS 网关与结构化 Avatar 回合

> 状态：已批准并在 W29 工作树实现；发布 CI 与所有者主观试听 Gate 待关闭
> 决策日期：2026-07-30
> 范围：单机、单 Windows 用户、私人使用

## 背景

W28 已建立单写者 Avatar Runtime、实际播放音量口型、主体动作释放和红眼所有权，但当时仍有两个明确缺口：

1. 真实回复只有纯文本流，没有受限的整轮主情绪、focused 视觉变体和分段红眼协议。
2. 用户的五套 GPT-SoVITS v2ProPlus 声音尚未通过可审计的本机运行时接入；旧通用 provider 会把
   reference 和路径发送给原始 API，也无法约束任意切模与控制端点。

[上游固定 README](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/README.md)
说明跨语言推理，并列出 Python 3.11、PyTorch 2.5.1、CUDA 12.4 的测试组合；固定提交采用
[MIT License](https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/LICENSE)。
与此同时，上游权重加载仍显式使用不安全反序列化，
[GitHub Security Lab 公告](https://securitylab.github.com/advisories/GHSL-2025-049_GHSL-2025-053_RVC-Boss_GPT-SoVITS/)
也确认这类入口可导致远程代码执行。因此“能运行”不能替代权重来源、哈希、ACL 和入口面的信任控制。

## 决策

### 1. 流式结构化回合

真实 LLM provider 声明 `avatar_json` 流，顶层只允许 `plan` 和 `segments`：

```json
{
  "plan": {
    "emotion": "focused",
    "focused_variant": "chuunibyou"
  },
  "segments": [
    {"text": "中文正文", "red_eye": true}
  ]
}
```

- 增量 parser 只在完整对象通过严格 schema 后释放 segment；半成品 JSON、字段名和控制值永不显示或朗读。
- 未知字段、重复键、非法枚举、非有限值、空正文、截断、尾随内容和超限输入均以稳定协议错误终止。
- LLM 的 emotion 只是建议；本地 `EmotionEngine` 仍拥有最短持续期、冷却和最终主情绪裁决权。
- `focused_variant` 只允许 `default` 与 `chuunibyou`，且非 focused 必须为 `default`。
- 旧 mock/纯文本 provider 保持兼容；声音槽、速率和动作语义始终由本地映射推导。

### 2. 有序播放与 Avatar 生命周期

- 主体动作只在首个音频段实际开始播放时触发；TTS 失败时才在首个安全文字段显示时走视觉 fallback，
  嘴巴保持闭合。
- 红眼只跟随有序的实际播放段，不跟随 LLM 到达或并行合成完成顺序；每轮最多八个显式红眼段。
- 爆裂状态由本地保证首段红眼；中二病 focused 至少一个红眼段。其他状态只有结构化段显式标记才触发。
- 主情绪或 focused 变体改变时执行 `release → 新动作`；二者均未改变时延续当前姿态。
- 取消立即停止口型和主体动作并淘汰 generation；保留当前主情绪，迟到音频、动作和红眼均被拒绝。
- `TurnCancelledError` 在 TaskGroup 内转换为普通内部信号以取消 sibling，离开边界时再恢复领域取消异常；
  外部 `task.cancel()` 继续保持原生取消语义。

### 3. 五槽声音映射

声音槽为 `neutral`、`gentle`、`tsundere`、`focused`、`excited_explosion`。本地映射冻结以下速率：

| 主情绪 | 声音槽 | 速率 | 过渡 |
| --- | --- | ---: | --- |
| neutral | neutral | 1.00 | normal |
| happy | gentle | 1.05 | normal |
| shy | tsundere | 0.95 | sudden |
| proud | focused | 1.03 | normal |
| angry_cute | tsundere | 1.08 | normal |
| worried | gentle | 0.92 | gentle |
| bored | neutral | 0.93 | gentle |
| excited | excited_explosion | **1.00** | sudden |
| explosion_mode | excited_explosion | 1.12 | sudden |
| sleepy | neutral | 0.85 | gentle |
| focused | focused | 0.95 | normal |

动作候选只以公共语义键存在代码中。真实模型名称、候选入口、统一 release、红眼入口及特定外观的第二兴奋
候选只存于仓库外用户配置，并按当前模型规范化名称唯一解析；不把任何私有 ID 或入口名称编译进产品。

### 4. 私有、安全、path-free 的 TTS 网关

- 官方源码固定为提交 `d523079fc05d9a8028d6085bffe4a2757c32abb6`，只作为仓库外 inference runtime；
  不暴露 WebUI、原始 API、上传、控制、任意路径、reference 切换或权重切换端点。
- 网关只监听 `127.0.0.1:9880`，单 worker；HTTP 面只有认证后的 `GET /v1/health` 与 `POST /v1/tts`。
- 请求只包含正文、本地声音槽和速率。Host、Origin、peer、Bearer、JSON、body 和 admission 均严格受限；
  最多一个活动推理和两个等待请求。
- 256 位 Bearer token 使用 CurrentUser DPAPI；当前用户账户已完全受控时，token 不是隔离边界。
- 私有 manifest 固定五槽 GPT/SoVITS 权重、参考 WAV、日语提示、公共模型与源码树 SHA-256；启动时对
  manifest、所有文件/目录、源码提交和 Windows ACL 做 fail-closed 审计。
- 启动加载 Neutral，一次只保留一个槽位。GPT 与 SoVITS 切换使用同一独占锁并核对成对指针；
  部分失败回滚完整 pair，回滚失败进入 quarantine。
- 参考音频和提示语言固定为日语，输出正文语言固定为中文：`prompt_lang=ja`、`text_lang=zh`。
- 推理参数固定为用户已验收的 v2ProPlus 参数。为同时保留分桶与情绪速率，官方推理阶段保持 1.0，
  再使用上游同一音频变速实现处理最终波形。
- 网关不由主程序自动启动。当前用户桌面启动器创建进程并放入 kill-on-close Windows Job Object；
  启动器关闭时清理整个子进程树。

### 5. 安全 ZIP 导入

只接受用户明确确认可信的声音包。导入器拒绝路径穿越、链接、加密项、重复/NFC-casefold 冲突、意外扩展和
大小/数量超限；每包必须恰好包含一份 `.ckpt`、`.pth` 与 `.wav`。macOS 元数据被忽略，错误缺失 UTF-8
标志的文件名只做可逆 UTF-8/NFC 恢复；日语句子本身不改写。源 ZIP 保持不变。

## 被拒绝的方案

- **直接暴露上游 `api_v2.py`：** 它包含控制、reference 和任意路径切模入口，扩大攻击面且破坏本地事务 owner。
- **让 LLM 输出声音槽、路径或 VTS 入口：** 提示注入可越过本地策略和私有配置边界。
- **按整轮 JSON 完成后才合成：** 不必要地增加首段延迟；严格增量 parser 已能只释放完整、验证过的段。
- **在父进程加载权重：** 扩大不安全反序列化和 CUDA 故障的影响范围。
- **降低 coverage 阈值或使用 pragma：** W29 通过增加真实失败/清理分支测试恢复跨平台余量。

## 隐私与残余风险

- 权重、参考音频、日语提示、生成 WAV、token、真实路径、VTS 模型/入口名称和 ID 不进入 Git、wheel、
  fixture、日志、诊断包或 PR。
- SHA-256 与 ACL 只证明本机固定输入和访问边界，不证明模型语义安全或上游代码不存在未知漏洞。
- 已自动验证中文非静音 WAV、切模、真实播放、口型、动作、红眼、取消和清理；五种音色差异、中文自然度
  与整体观感仍只能由所有者试听判断。

## 回滚

停止桌面启动器并保持网关不运行；将应用 TTS provider 切回 Mock 或 silent。Avatar 与文字继续按 W28
降级语义工作，口型保持闭合。回滚不删除用户的声音包、私有运行时、VTS 配置或备份。
