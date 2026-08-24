---
name: story-video-orchestrator
description: 编排或恢复本项目从 brief、脚本、分阶段素材板、故事板、提示词审核、视频生成、字幕后期到交付的正式流程。用于跨阶段营销视频任务、既有 run 恢复、文档讲解与实物产品分支路由，以及检查下一入口或确认闸门时。
---

# 故事视频编排

让代码选择阶段，让本 Skill 只提供跨阶段不变量。

## 每次先取代码合同

已有 run 时执行：

```bash
python3 scripts/workflow_router.py status --manifest <manifest.json>
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/workflow_router.py chain --manifest <manifest.json>
```

完整读取 `contract.guidance_resources[]`。只调用
`contract.flow.entry_functions[]`，并通过 `contract.flow.confirm_function` 关闭阶段。
需要查看本 Skill 当前绑定的全部代码入口时执行：

```bash
python3 scripts/skill_contract.py contract --skill story-video-orchestrator
```

不要把本文中的示例顺序覆盖到路由器返回的 profile-specific chain 上。

## 编排不变量

1. 先完成环境、公共会话密钥和 CLIENT，再创建或恢复稳定 run。
2. 每个阶段必须具备当前输入产物、代码入口、输出产物和客户确认/机器质检证据。
3. 素材严格逐阶段推进：产品源图与产品板、人物板、人物产品使用图、故事板分别展示和确认；产品与人物同时存在时不得跳过 `product_usage`。
4. 使用 `gpt-image-2` 生成产品板、人物板、使用图和故事板；默认调用 `storyboard.py --stage next`，不得在正式流程使用 `--stage all`。
5. 区分故事板角色：
   - `video_storyboard`：TVC、实物产品、剧情和实景服务，逐 shot 作为视频分镜合同。
   - `design_board`：PPT/PDF/Word/方案讲解，只确认确定性版式和 Animatic，绝不提交给视频模型。
6. 正式视频提交前展示并确认编译后的完整提示词；确认后才批量提交。
7. 独立镜头一次性 batch 提交再统一轮询；同人物、场景和连续动作确需衔接时，先把 `chain_required/chain_contract` 与尾帧引用槽写入并确认 segments，再使用 `--chain`。不要在确认后临时加链，也不要用 seed 代替尾帧连续性。
8. 生成画面保持无字幕、无卖点文字和无后加 Logo；字幕、参数卡和动效文字交给 HyperFrames/Remotion。
9. OCR、take review、caption artifact、final QC 和 delivery evidence 任一不完整时失败关闭，只修复失效阶段。

## 文档讲解分支

当 chain profile 为 `document_explainer` 时，读取
[文档讲解合同](../../references/document-explainer-playbook.md)：

- 从原始文档生成内容结构、motion design 和派生 execution plan；不原地改写客户确认的 authored plan。
- 用正式 Remotion/HyperFrames 时间轴渲染 look frame、execution layout board 和 Animatic，并绑定审批哈希。
- 视频模型只生成干净人物/场景底片；PPT 页面、图表、卡片、字幕和 Logo 不进入视频参考图。
- 正式融合必须使用已批准 previs spec，并在交付前运行 commercial QC。

## 恢复

读取 manifest 和 router contract，而不是从目录中文件名猜进度。审批哈希、素材指纹或文件字节变化后，按路由返回的 recovery 重新生成并重新确认；切换 Agent 只记录交接历史，不创建新 run。
