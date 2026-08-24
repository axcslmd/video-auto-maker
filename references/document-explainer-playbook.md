# 文档 / PPT 讲解型实测 Playbook

适用：PPT、PDF、Word、营销方案、课程大纲、服务说明。核心原则是：
**文档只是素材和事实边界，不是成片画面本身**。要先抽取内容层级，再重新导演成
「内容动效 + 数字人讲师」的讲解视频。

## 核心口径

1. 不把 PPT/PDF/Word 做成截图轮播，也不只抽文字贴成小卡片。先保留原意，再重构为
   hook、问题、方案、架构、流程、结果和 CTA。
2. 内容分支默认走 `content_spec.json` + `remotion_engine.py render-content`，用
   hero、comparison、process、causal、metrics、quote 等组件做动画表达。
3. 如果客户明确要求 PPT 原页/图表/截图保真，或单页信息密集到重排会损失可信度，就切到
   `content_shotlist.json` 原页保真模式：幻灯页作为主视觉，Remotion 只做轻运镜，数字人默认
   `overlay/corner`，窗口建议 `scale=0.45-0.55`。
4. 字幕、关键词、参数标签、透明文字、标题扫光等走 HyperFrames 后期层；不要让视频模型在
   背景画面里生成讲解文字。
5. 可重导演模式下，数字人不固定为角落画中画。每段都要选一种视觉版式，形成节奏变化。
6. 如果需要“人物抠图/前景人物”效果，本地仍不跑抠像模型：要用外部模型人景融合，或使用
   已确认的透明素材；`fuse.py` 只做机械叠加和分栏。
7. 融合页底色尽量保持通透，左右阴影只收在边缘 15% 内，不要把暗部铺到画面中段；否则会显得太沉。
   内容动效进入融合时优先输出 VP9 alpha WebM（交付中间层可保留 ProRes 4444），禁止把透明 CSS 层直接编码成普通 MP4。
8. 同一镜头只能有一个文字/动效拥有者。内容动效层已经包含标题、卡片、流程、关键词时，融合层只做光效、遮罩和空间关系，不再生成第二套标题/卡片；需要前景动效时必须显式声明 `motion_text_owner: foreground` 并给出独立安全区。

## 标准文件

每个讲解型 run 至少沉淀这些文件：

- `director_plan.json`：总导演脚本、分段台词、每段 `visual_mode`、融合方式。
- `source_outline.json`：从文档抽出的事实、页码/段落来源、禁止改写的硬信息。
- `content_spec.json`：可重导演内容动效场景，不是截图播放。
- `content_shotlist.json`：原页保真模式使用，引用已渲染的 PPT/文档页面截图并做轻运镜。
- `human_segments.json`：数字人讲师分段口播，台词必须和确认稿一致。
- `layout_preview/explainer_layout_preview.html`：六种视觉版式和本 run 分段版式节奏的确认预览。
- `edit/fusion_plan.json`：由 `video_use_bridge.py plan` 生成的剪辑记忆与 EDL。

推荐目录：

```text
output/<client>/<run-id>/
  director_plan.json
  source_outline.json
  content_spec.json
  content_shotlist.json
  human_segments.json
  layout_preview/explainer_layout_preview.html
  layout_preview/explainer_layout_preview.svg
  layout_preview/explainer_layout_preview.json
  content/content.mp4
  human/human.mp4
  edit/project.md
  edit/takes_packed.md
  edit/edl.json
  edit/fusion_plan.json
  final/<run-id>.mp4
```

## 内容重设计步骤

1. 用 `doc_extract.py` / `asset_prep.py parse-ppt` 抽取文字和页码，先做事实清单：
   原文说了什么、哪些数字/产品名/流程名不能改。
2. 把内容压成 4–6 个讲解单元，45 秒视频通常不要超过 6 段。
3. 给每段选择一个 `content_spec` 组件：
   `hero` 开场、`comparison` 痛点对比、`typewriter` 定位、`causal` 架构、
   `process` 流程、`metrics/quote` 结果和 CTA。
4. 给每段选择一个 `visual_mode`：
   `host_full_bleed_text`、`split_left_right`、`stacked_top_bottom`、`corner_pip`、
   `cutout_presenter`、`content_full_bleed`。
5. 用确定性脚本生成版式预览并给客户确认，避免依赖宿主 Agent 的临时 widget：
   ```bash
   python3 scripts/explainer_layout_preview.py --plan output/<client>/<run-id>/director_plan.json \
       --out-dir output/<client>/<run-id>/layout_preview --json
   ```
6. 生成内容动画后抽帧检查：主信息是否占据画面、黑场是否过多、文字是否可读、转场是否遮挡内容。

## 原页保真模式

当客户要求“就讲这份 PPT”、页面包含复杂图表/架构/截图，或法务/品牌要求保留原页样式时，使用原页保真模式：

```json
{
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "brandPrimary": "#246BFF",
  "shots": [
    {
      "durationInFrames": 150,
      "move": "ken_burns",
      "image": "/absolute/path/slide-1.png",
      "transition": "cut"
    }
  ]
}
```

运镜建议循环使用：`ken_burns`、`pan_right`、`pull_out`、`pan_left`、`push_in`。不要连续多页
`still`，否则质检容易判定冻结，观感也会像静态翻页。

`director_plan.json` 需明确 `scene_type:"explainer"`，并在 `content_strategy` 写清
“PPT 原页/截图作为主视觉”。`video_use_bridge.py plan` 会默认生成 `overlay/corner` 融合计划；
如客户确认页面不密集，才改成 split 或 scene embedded。

## 视觉版式建议

- `host_full_bleed_text`：开场或情绪转折。数字人/场景作背景主体，透明标题和关键词动效浮现。
- `split_left_right`：讲架构、对比、方案定位。内容一侧，数字人一侧，利于信息和可信度同时存在。
- `stacked_top_bottom`：讲流程、时间线、步骤。上方内容演进，下方讲师或反过来。
- `corner_pip`：只在信息图很密、必须完整展示内容时使用，不作为全片默认。
- `cutout_presenter`：结尾、核心亮点或 CTA。人物前景压上内容动效，但抠图/融合必须走外部模型或透明素材。
- `content_full_bleed`：复杂图谱、流程、指标冲击段。数字人可以短暂退场，用旁白承接。
- 所有融合页都要避开“大面积暗底 + 中段阴影”的默认模板，宁可让底色更通透一点，也不要把左右边缘压黑。
- 版式解释或确认一律使用 `explainer_layout_preview.py` 落盘的 HTML/SVG/JSON；不要只在对话中临时画图，因为换 Agent 后无法复现或交付。

如果全片需要多种版式，不能把 45 秒整条视频一次性用单个 `overlay/corner` 融合。应按 EDL
切分内容分支和数字人分支，对每段分别 `fuse.py split/overlay` 或外部人景融合，再用
`compose.py concat` 拼接。

## Basicware 45s 推荐结构

- 0–7s：`host_full_bleed_text`，开场定义“企业缺的不是工具，而是能执行的数字员工”。
- 7–17s：`split_left_right`，左侧痛点对比卡，右侧数字人讲师。
- 17–25s：`content_full_bleed`，定位句和场景扩展做打字机/关键词流动。
- 25–35s：`split_left_right` 或 `stacked_top_bottom`，架构图谱逐层点亮。
- 35–45s：`cutout_presenter` 或 `stacked_top_bottom`，流程四步和交付结果收束。

## 验收口径

- 可重导演模式不能像“人在讲 PPT 图片”；观众应看到信息被重新设计和调度。
- 可重导演模式不允许全片只有一种画中画形态；至少 3 种视觉版式或内容组件。
- 原页保真模式必须让 PPT/图表/架构成为清晰主视觉，讲师窗口不得遮挡标题、关键流程箭头、架构层级和页码核心内容。
- 每段都能追溯到源文档，不改事实、不凭空添加客户没提供的能力承诺。
- 尾段语言保持普通话中文，不突然切英文。
- 抽帧里主信息占比足够，黑场和无信息留白不能压过内容。
