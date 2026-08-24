---
name: seedance-video
description: 将已确认的逐镜头计划、故事板和引用素材编译为项目视频引擎可执行的原生故事板提示词。用于 render_plan、test_segment 或 video 阶段，需要 Seedance 原生故事板、Kling 单格展开兜底、逐镜头素材角色、批量生成或尾帧连续性时。
---

# Seedance 视频编排

## 代码合同优先

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill seedance-video
```

模型与 `videoType` 能力从实时网关目录和 `scripts/video_models.py` 选择，不在 Skill 中维护在线模型快照。异步提交、回退、轮询、恢复、OCR、成本和结果登记全部交给 `scripts/video_engine.py`。

## 编译规则

- 每个 segment 只执行一个 shot，并明确 `segment_id`、`panel_index`、持续时间和一个主导连续运镜。
- 使用时间窗描述动作，例如 `0–2秒`、`2–5秒`；复杂动作或机位拆成不同 shot。
- 在提交文本中逐个写明 `@tag` 的角色：人物身份、产品外观、场景、首帧、尾帧或构图参考。
- `video_storyboard` 优先设置 `storyboard_ref_mode:"native_storyboard"`，让 Seedance 同时看到已确认整张故事板与当前 shot 对应素材。
- 先用 `video_engine.py --batch <segments> --preflight --preflight-out <video_preflight.json>` 解析当前模型，并让 `capture-video --preflight-json` 消费同一份能力合同。只有实际选择 Kling/非原生故事板模型时，才由 `gpt-image-2` 基于整张故事板和 `panel_index` 生成高分辨率单格展开图；不得像素裁切。展开图必须 QA、展示并由客户确认，再对最终 segments 重新 preflight/capture/确认。若 Seedance 提交后才触发隐私回落，必须先停下完成这套确认，不能未展示就自动提交 Kling；design-board/身份底片不进入单格展开。
- `design_board` 永远不作为视频参考；只允许干净的人物/场景 plate 进入视频模型。
- 字幕、Logo、slogan、参数卡和图表动效留给 HyperFrames/Remotion。

## 引用与连续性

在正式 handoff 前修改 authored plan 或素材映射，然后重新运行 `script_splitter.py split` 和提示词审核。handoff 后不要直接改 `segments.json`。

独立镜头使用并行 batch；连续人物、背景和动作确需无缝衔接时，先把 `chain_required/chain_contract` 和尾帧引用槽纳入已确认 segments，再使用 `--chain`。`--seed` 只作为模型可选参数透传，不能替代尾帧串联。

正式生成必须带当前 `client`、`manifest`、`results-out` 和已确认的 `prompt-review`，并持续向客户报告进度。
