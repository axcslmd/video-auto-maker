# 内容引导子模板 · 图片类 →「展示+种草」（LLM 参考，非用户命令）

**触发**：客户给的是商品照 / 包装图 / 细节特写 / 场地 / 工厂 / 门店 / 服务现场图。
**视频职责**：视觉吸引、激发行动。**引导目标**：帮客户**排出视觉叙事顺序**，
台词短、节奏快。配运镜特写（Remotion Ken Burns/推拉）+ 参数快闪（HyperFrames）+ CTA。

## 引导逻辑：先分图角色，再排叙事节奏

图片素材本身就是分镜原料。你的价值是帮客户给每张图**定角色**、**排顺序**。分两类走。

---

### A. 商品图 → 商品推荐/带货

**第 1 轮 · 给图定角色**（ingest 时打 tag）：
- `hero`（第一眼视觉钩子，最出彩那张）
- `detail`（卖点特写，质感/工艺/接口）
- `scene`（使用场景，帮观众代入）
- `pack`（包装/规格）

**第 2 轮 · 排卖点递进**：问客户「这款最想让人记住的 1 个卖点是什么？其次呢？」按「钩子→主卖点→次卖点→信任」排。

**产出：带货视觉叙事分镜表**

| 镜位 | 用图 | 运镜 | 叠加(HyperFrames) | 台词(短!) | 时长 |
|---|---|---|---|---|---|
| 钩子 | hero | 快推近 push_in | 悬念大字 | 一句抓眼球 | 2-3s |
| 主卖点 | detail | Ken Burns | 参数标签快闪 | 一句点题 | 3-4s |
| 次卖点 | detail/scene | 横摇 pan | 数字高亮 | 一句补充 | 3-4s |
| 场景代入 | scene | 拉远 pull_out | 场景标签 | 一句共鸣 | 2-3s |
| 种草CTA | hero/pack | 定格 still | CTA+价格/二维码 | 一句促单 | 2-3s |

- 总时长建议 8-20s（Reels/Shorts 节奏）；走 `/brand-tvc` 的快剪/纯产品展示子类型，有主持人带货则走 `/oral-broadcast`。
- 可选数字人：画外音解说走独立解说片 + `fuse.py corner` 角窗（不抠像）。

---

### B. 场地/工厂/门店图 → 实景介绍

**第 1 轮 · 给图定角色**：
- `establish`（全景/门头，建立"这是哪"）
- `scale`（规模：车间/货架/工位，证实力）
- `proof`（资质/证书/荣誉墙/设备特写，建信任）
- `service`（服务动作/人员，讲体验）

**第 2 轮 · 排可信背书漏斗**：环境实拍→规模/资质→服务讲解。让观众「看到→信任→行动」。

**产出：实景介绍分镜表**

| 镜位 | 用图 | 运镜 | 叠加 | 讲解点 | 时长 |
|---|---|---|---|---|---|
| 建立场景 | establish | 缓推 push_in | 地点/品牌名 | 我们是谁 | 3-4s |
| 展示规模 | scale | 横摇 pan | 数据(面积/产能/年限) | 实力背书 | 4-6s |
| 资质信任 | proof | Ken Burns | 认证标签快闪 | 为什么信我们 | 4-6s |
| 服务讲解 | service | 拉远 pull_out | 服务要点 bullets | 提供什么 | 6-10s |
| CTA | establish | 定格 | 联系方式+二维码 | 怎么找我们 | 3-4s |

- 总时长建议 20-40s；走 `/explainer-video` 的实景服务子类型。
- 数字人讲师走路线A：场地图作参考图，在已确认 segment 中设 `video_type:4/5`，人自然站在实景里讲解。

## 可执行产物（免誊抄，直接接引擎）

分镜表不用手抄——`guide_scaffold.py` 生成骨架、填完编译。商品图用 `--kind product`，场地/工厂图用 `--kind venue`：
```
# 1. 生成空分镜表（商品带货用 product，实景背书用 venue）
python3 scripts/guide_scaffold.py scaffold --client "$CLIENT" --kind product --segments 5 --out assets/$CLIENT/guide.json
# 2. 你（LLM）填每行：image(用图相对路径)/talk(短台词)/content/overlay；image_role 已预置(hero/detail…)
# 3a. 运镜特写背景（Remotion Ken Burns/推拉）
python3 scripts/guide_scaffold.py compile-shots --file assets/$CLIENT/guide.json --out output/shots.json
python3 scripts/remotion_engine.py render --shotlist output/shots.json --out output/bg.mp4
# 3b. 数字人/画外音草稿 segments（只做本地结构检查，不得直接付费出片）
python3 scripts/guide_scaffold.py compile-segments --file assets/$CLIENT/guide.json \
  --urls-map '{"r1":"<hero图URL>","r2":"<detail图URL>"}' --out output/segments.json
```
urls-map 给每行配参考图 URL（人景同框路线A）；行内 image 字段也会被 compile-shots 用作运镜素材。
正式出片前必须把已确认的 guide 行迁移到 `storyboard_plan.json`，完成故事板展示与客户确认，再用
`script_splitter.py split --client <client> --manifest <run_manifest.json>` 登记 handoff；随后捕获并确认
视频提示词，最后使用带 `--client --manifest --results-out --prompt-review` 的正式 batch 命令。

## 铁律
- **图定角色再排序**：每张图先问「这张证明什么」，没角色的图不用。
- **台词让位画面**：图片类是画面主导，台词是点睛不是主体，一镜一句。
- **节奏比信息量重要**：宁可少讲一个卖点，也别让节奏拖沓。
- 参数/数据用 HyperFrames 快闪叠加（中文不乱码），不塞进台词念。
