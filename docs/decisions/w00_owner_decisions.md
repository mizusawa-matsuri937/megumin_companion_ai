# W00 项目所有者决策记录

> 回答日期：2026-07-17
> 适用范围：Gate W0 与后续 Windows 私人开发
> 回答者：项目所有者本人
> 审查安排：项目所有者兼任架构、安全/隐私和许可证 reviewer；当前没有独立人工审查人员

## 决策摘要

| ID | 决策 | 结论 | 影响 |
| --- | --- | --- | --- |
| OD-W00-01 | Qt 主线程 + BackendThread + Media/Perception worker | 批准 | 冻结 ADR-W01；修订早期单 OS 进程约束 |
| OD-W00-02 | 生产零 HTTP/WS；dev API 显式启用并认证 | 批准 | 冻结 ADR-W02；W04 不得保留无认证生产入口 |
| OD-W00-03 | package resource + LocalAppData 分类路径 | 批准 | 冻结 ADR-W03；不再依赖 CWD |
| OD-W00-04 | 旧 `data/` 显式 copy-verify-switch | 批准 | 禁止自动扫描、移动或删除旧数据 |
| OD-W00-05 | current-user DACL + DPAPI | 批准 | 冻结 ADR-W04；环境变量仅用于开发输入 |
| OD-W00-06 | 卸载数据与 secret 语义 | 批准建议 | 明确选择；默认保留非秘密数据，删除/撤销 secret |
| OD-W00-07 | 幂等键、seq、replay 和 snapshot reset | 批准 | 冻结 ADR-W05 |
| OD-W00-08 | 第 4.8 节初始资源硬上限 | 批准 | 冻结 ADR-W06；只允许基于证据调值，不允许移除上限 |
| OD-W00-09 | Job Object worker 和 hard kill | 批准 | 冻结 ADR-W07；soft cancel 失败可终止整个子树 |
| OD-W00-10 | wheel → PyInstaller onedir → per-user installer | 批准 | 冻结 ADR-W08；不做静默自动更新 |
| OD-W00-11 | Windows 支持矩阵 | 批准建议 | 首发只承诺 Windows 11 x64、标准用户、单交互会话 |
| OD-W00-12 | 指定窗口捕获失败时延期视觉 | 批准 | 禁止无提示全屏截图裁剪 fallback |
| OD-W00-13 | 云视觉 | 修订建议后批准 | 不永久关闭；本地隐私检测通过且用户显式启用时允许上传脱敏后的指定窗口图像 |
| OD-W00-14 | 安装器技术 | 暂定 | 先冻结 per-user/回滚/无静默更新；具体实现 W25 前 spike 后决定 |
| OD-W00-15 | 分发范围 | 私人使用 | 当前不进入公开分发的命名、资产、法律和商店流程 |
| OD-W00-16 | 代码签名 | 不需要 | 只适用于私人构建；公开 RC 因未签名保持阻塞 |
| OD-W00-17 | Windows/设备环境 | 基本具备 | 只是所有者声明；各 Gate 仍需逐项记录型号、拓扑和实测证据 |
| OD-W00-18 | 人工角色 | 本人兼任全部角色 | 没有独立审查；必须作为残余风险持续披露 |

## 云视觉的批准边界

OD-W00-13 只改变“是否允许设计云上传路径”，不表示当前代码已经获得上传权限或通过隐私验收。允许离开本机的图像必须同时满足：

1. vision/cloud vision 默认关闭，由用户显式启用并能随时停止。
2. 捕获对象是用户指定且在发送前重新验证的窗口；不得用全屏截图裁剪替代。
3. 本地窗口 Guard、内容 Guard、全 OCR bbox 遮挡和最终隐私检查全部成功。
4. 任一检查未知、超时、异常、权限变化、锁屏、窗口切换或 worker generation 变化时跳过上传。
5. 只上传脱敏后的图像和最小必要请求字段；不得上传原始 OCR 文本、窗口标题、聊天历史、secret 或本地路径。
6. UI 必须显示云端出口、provider 和当前启用状态；停止返回前取消在途任务并完成本地 buffer/temp 清理。
7. 真正启用由 W21/W22 的自动 sentinel、真实 Windows 隐私样本和出口审计证明；W0 本身不提供可用性证明。

## 私人使用和审查限制

- 当前允许项目所有者自审后继续私人开发，但所有文档必须写明 reviewer 不独立。
- 当前批准不覆盖公开二进制、商店发布、角色/声音/模型再分发或对外隐私承诺。
- 若改为公开分发，必须重新打开独立安全/隐私审查、许可证/命名审查、代码签名和发布 Gate。
- “设备基本具备”不能替代 W4～W6 的设备矩阵和人工实测记录。

## 变更规则

修改任何结论必须同时更新对应 ADR、[`../security/windows_threat_model.md`](../security/windows_threat_model.md)、[`../architecture/windows_data_flow_inventory.md`](../architecture/windows_data_flow_inventory.md) 和 [`../gates/gate_w0.md`](../gates/gate_w0.md)。涉及云视觉、公开发布、secret、截图或音频边界的放宽必须重新人工批准。
