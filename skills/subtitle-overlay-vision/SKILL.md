---
name: subtitle-overlay-vision
description: 为已批准视频底片生成、确认并合成安全区字幕层。用于 captions 或 final 阶段，需要视觉分析避开人脸和产品、HyperFrames 透明 alpha 字幕、中文或粤语字体、字幕位置修正或正式 caption artifact 登记时。
---

# 字幕安全区与透明叠加

## 正式入口

先读取代码合同：

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill subtitle-overlay-vision
```

正式字幕使用 router 返回的 captions 入口。典型执行由以下命令完成，但参数必须来自当前合同和 manifest：

```bash
python3 scripts/subtitle_overlay.py run \
  --video <approved_basecut.mp4> \
  --lines <confirmed_lines.json> \
  --client <client> --manifest <run_manifest.json> \
  --caption-manifest <caption_manifest.json> \
  --out <captioned.mp4>
```

只有内部诊断预览可以显式使用 `--draft`；草稿不可交付。

## 流程不变量

1. `lines.json` 从已批准底片的实际段时长派生，不按台词字数估时。
2. 视觉模型只负责推荐字幕安全区；实时模型选择由代码完成，Skill 不维护固定模型名单。
3. HyperFrames 渲染透明 MOV/WebM alpha 层；正式透明层不得输出 MP4。
4. ffmpeg 把 alpha 层叠回原底片并保留原音轨。
5. 渲染后验证 alpha、画面尺寸、可读性、首尾字幕、字体和输出媒体，再登记 caption render。
6. 展示成片绝对路径，取得客户字幕时间轴确认后再进入 final。

## 安全检查

- 字幕不遮挡人脸、嘴部、手部接触点、产品 UI、包装关键文字和 CTA 安全区。
- 中文字幕使用浏览器真实字体渲染；只有 Node 不可用时才走受控兜底。
- 透明层缺 alpha 时立即失败，避免不透明黑底覆盖底片。
- 输入底片、字幕行或 caption manifest 任一变化时，旧渲染与确认失效。
- 生成画面中的意外字幕仍属于视频阶段 OCR 问题，不能靠后期字幕层掩盖。

需要调试时可分别运行 `subtitle_overlay.py analyze`、`build-scenes`、`compose`，但正式交付必须回到 `run` 路径完成 gate 和登记。
