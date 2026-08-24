---
name: bw-storyboard-color-anchor
description: 用 gpt-image-2 黑白故事板确认构图，同时用已确认彩色人物、产品和场景素材控制最终视频色彩与身份。用于 cast_board、storyboard、render_plan 或 video 阶段，客户允许黑白分镜但要求彩色品牌成片时。
---

# 黑白故事板与彩色素材锚

黑白故事板负责镜头合同，彩色确认素材负责身份、产品和色彩合同。不要让黑白预演图成为最终配色来源。

## 当前代码合同

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill bw-storyboard-color-anchor
```

## 流程

1. 使用当前 storyboard stage 入口和 `gpt-image-2`，默认生成黑白故事板；客户明确要求彩色时才使用 `--color` 或 plan/shot 的 `color_mode:"color"`。
2. 展示 `storyboard.py` 返回的 `preview_html`、`embedded_md` 或 `index_md`，取得客户确认。
3. 在 authored plan 的 `asset_refs` 和各 shot 的 `ref_tags[]` 中绑定已确认的彩色人物板、产品图/产品板和场景图。
4. 运行正式 `script_splitter.py split`。默认黑白模式下，彩色素材用于身份与色彩锚定，故事板用于构图和时序；彩色故事板才使用 `--color-storyboard`。
5. 展示并确认视频提示词审核文件，再按 router video contract 批量生成。

## 检查

- 人物板正脸特写与全身图必须是同一身份；不一致时先精修人物板。
- 产品与人物同时出现并发生接触时，必须先确认 `product_usage`。
- 缺少彩色素材时返回资产阶段补齐，不让视频模型从黑白图猜颜色。
- 引用集合在 video handoff 后冻结；修改素材或故事板时重新 split、重新审核。
- 连续段先在已确认 segments 中声明 `chain_required/chain_contract` 并预留尾帧引用槽，再用 `--chain`；不要用 seed 锁颜色或身份。
- 生成画面不包含字幕、卖点文字、后加 Logo 或水印。
