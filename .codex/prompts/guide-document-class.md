# 内容引导子模板 · 文档/文案类 →「讲清楚」（LLM 参考，非用户命令）

**触发**：客户给的是产品介绍书 / 营销方案 / 课程大纲 / 服务说明 / PPT / 长文案。
**视频职责**：传递信息、建立理解。**引导目标**：把冗长资料**提炼成有层级的讲解脚本**，
不是照读、不是堆参数。配数字人讲师 + 内容页（Remotion 排版）。

## 引导逻辑：先塌缩成金字塔，再展开成脚本

一份文档往往有 20+ 个信息点，客户容易想全讲。你的价值是帮他做**减法+排序**。分三轮引导：

### 第 1 轮 · 抽核心信息（One Core Message）
先读 brief（`asset_prep.py brief`），然后问客户：
> 「这条视频如果观众只记住一句话，你希望是哪句？」
给 2-3 个候选（从文档 slogan/结论里提炼）+ 推荐 + 理由，让客户选/改。
→ 落成 **1 句核心信息**（视频开场 3 秒要抛出的钩子）。

### 第 2 轮 · 选支撑点（3-5 个，不多）
从文档里列出所有论据/卖点/模块，帮客户**砍到 3-5 个**最有力的：
> 「这些点里，支撑上面那句话最有力的是哪 3 个？其余的这条视频先不讲，留给下一条。」
每个支撑点配一句「怎么讲」的建议（数据/案例/对比/演示）。

### 第 3 轮 · 定 CTA（看完去做什么）
> 「观众看完，你希望他下一步做什么？」（咨询/扫码/报名/加微信/进店）
→ 落成 **1 个明确行动号召**，放结尾。

## 产出：信息层级拆解表（填完即脚本骨架）

| 层级 | 内容 | 讲解方式 | 内容页(Remotion) | 时长 |
|---|---|---|---|---|
| 开场·核心信息 | 〈那句话〉 | 数字人正面抛钩子 | 大标题快闪 | 3-5s |
| 支撑点 1 | 〈论据/卖点〉 | 数据/案例/对比 | 标题+bullets | 8-12s |
| 支撑点 2 | 〈…〉 | 〈…〉 | 图文页 | 8-12s |
| 支撑点 3 | 〈…〉 | 〈…〉 | 图表/演示页 | 8-12s |
| 结尾·CTA | 〈行动号召〉 | 数字人直视引导 | CTA页+二维码位 | 3-5s |

- 总时长 = 各段之和，讲解型建议 30s-90s；超过 5 个支撑点就拆成系列多条。
- 每个支撑点对应一个 Remotion 内容页（`shots.json` 一个 shot：title+bullets+运镜）。
- 数字人讲师走路线A（在已确认 segment 中设 `video_type:4/5` 参考图人景同框）或画中画角窗（`fuse.py corner`）。

## 可执行产物（免誊抄，直接接引擎）

引导表不用手抄——用 `guide_scaffold.py` 生成空表、填完编译成引擎产物：
```
# 1. 生成空引导表（segments=总镜位数，含开场+CTA）
python3 scripts/guide_scaffold.py scaffold --client "$CLIENT" --kind document --segments 5 --out assets/$CLIENT/guide.json
# 2. 你（LLM）把每行的 content(内容页标题)/talk(数字人台词)/bullets 填进 guide.json
# 3a. 编译成 Remotion 内容页 shotlist
python3 scripts/guide_scaffold.py compile-shots --file assets/$CLIENT/guide.json --out output/shots.json
python3 scripts/remotion_engine.py render --shotlist output/shots.json --out output/bg.mp4
# 3b. 编译成草稿 segments（只做本地结构检查，不得直接付费出片）
python3 scripts/guide_scaffold.py compile-segments --file assets/$CLIENT/guide.json --out output/segments.json
```
seconds→durationInFrames 自动按 fps 换算；talk 为空的行编译 segments 时跳过（纯内容页）。
正式出片前必须把已确认 guide 写入 `storyboard_plan.json`，完成故事板确认，再由
`script_splitter.py split --client <client> --manifest <run_manifest.json>` 登记 handoff；捕获并确认
视频提示词后，才可使用带 `--client --manifest --results-out --prompt-review` 的正式 batch 命令。

## 铁律
- **提炼不照读**：文档 300 字的段落 → 视频一句话讲点。别让数字人念说明书。
- **一条一个核心**：贪多必崩。多主题拆成系列。
- **数据要口语化**：「转化率提升 37%」→「差不多每 3 个人就多 1 个下单」。
- 支撑点顺序按「最能打动目标人群」排，不按文档原顺序。
