---
name: horizontal-kinetic-talk-video
description: 为 16:9 真人或数字人口播底片编排校准字幕、语义章节、动态卡片、PIP 和 Remotion 包装。用于已确认底片进入 captions 或 final 阶段，并需要横版 kinetic typography 或内容讲解增强时。
---

# 横版口播增强

保持底片与确定性排版分离：人物底片负责真实说话，Remotion/HyperFrames 负责章节、卡片、PIP、进度和字幕。

## 先确认正式阶段

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill horizontal-kinetic-talk-video
```

只有已批准视频底片和当前 caption artifact 可以进入正式包装。草稿预览不得作为交付成片。

## 编排

1. 必要时统一底片规格：

```bash
python3 scripts/remotion_engine.py normalize-input \
  --input <approved_basecut.mp4> --out <normalized.mp4>
```

2. 使用 `script_splitter.py derive-captions` 从实际各段结果和已批准 basecut 生成连续字幕时间轴，再让客户确认 caption artifact。
3. 使用 `kinetic_talk.py` 把视频、时长、已确认字幕和可选场景编译为 Remotion props。
4. 使用 `remotion_engine.py render-kinetic` 渲染包装预览；正式交付仍遵守 router 的 final gate 和记录函数。

## 质检

- 输出保持 16:9，分辨率和 fps 以项目渲染合同为准。
- 字幕起止以实际语音时间轴为准，最后一句完整可见。
- PIP 不裁脸，卡片不遮挡眼睛、嘴部、字幕安全区和产品关键区域。
- 章节不重叠；动态层不替代底片中的真实产品动作。
- 字幕、卡片、Logo 和参数全部由确定性后期生成，不交给视频模型绘制。
- 修改 props、caption artifact 或底片后重新渲染并重新确认，不复用旧 final approval。
