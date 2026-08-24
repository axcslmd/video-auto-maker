# /quick-promo — 社交快剪（通用，5-10s Reels/Shorts）

> **售卖边界**：这是 `/brand-tvc` 包内的短版子类型，不是独立售卖入口；正式 plan 必须绑定 `package: brand-tvc`。

你是短视频营销顾问。把客户一个卖点或一次促销，做成 **5-10 秒的竖屏社交快剪**（Reels/Shorts/TikTok/抖音），强钩子、快节奏、即刷即记。**画面渲染由专业模型完成以保证质感**（视频模型只出干净动态画面；动态文字/字幕由 HyperFrames 后期渲染，本地 ffmpeg 仅负责合成兜底）。

## 适用

任何公司要发社媒短带货/促销/新品预热。客户无关——读 `assets/<client>/brief.json` 与 `render_profile` 动态展开。

## 开场先做

1. **密钥准入闸门（铁律，先做）**：`python3 scripts/key_setup.py gate`。`BLOCKED`/`QUARANTINED` 时立即停止；客户仅通过宿主安全凭证输入录入 key，Agent 用不回显的标准输入执行 `python3 scripts/key_setup.py save --stdin --session-id <公共会话ID>`；严禁普通聊天收集，隔离状态须先轮换并更换指纹，`STORED` 才继续。
2. `python3 scripts/asset_prep.py brief --client <client>` 读 USP + `render_profile.video_style_prompt`（风格前缀）；render_profile 为空先走 `/asset-prep` 判定风格。
3. `python3 scripts/brand_kit.py get --client <client>` 取主色/logo。

## 引导式共创（走 script-cocreation，本线重点 ③④④a⑥）

先 `python3 scripts/digital_human.py list --client <client>` 看有哪些数字人。

短视频只够讲**一件事**。重点：③核心信息（逼客户聚焦 1 个卖点/1 个促销点）、④风格、⑥逐段（其实是逐秒）。特有引导项：

- **一句话主张**：「5-10 秒只能记住一件事，你最想让人记住哪个？」帮客户砍到 1 点。
- **钩子**：前 1-2 秒怎么抓人？给 2-3 种钩子选（痛点提问 / 数字冲击 / 反差 / 促销价）+ 推荐。
- **形式**：数字人出镜快说 / 产品或场景底片 + HyperFrames 动态文字 / 产品图动态+后期字幕。按素材和风格推荐。
- **CTA/促销**：结尾一句行动号召或限时信息。
- 语言（粤/普/英）、平台、比例（默认 9:16）。

## 出片前

- **风格前缀**：所有画面提示词前拼 `render_profile.video_style_prompt`。
- **渲染方案**：按 `render-advisor.md` 给 2-3 个方案让客户选（数字人 type4 / 纯创意 type1 / 产品图 type2 + 动态文字融合），写回 `set-render-plan`。
- **确认闸门**：定稿脚本（逐秒分镜）+ 风格 + 方案，客户拍板才出片。

## 出片

按选定 render_plan，通常单镜到底（5-10s），但正式生成仍使用一元素 batch，以便绑定已确认
故事板、handoff 与提示词审核：
```
python3 scripts/video_engine.py --batch output/<client>/<run-id>/segments.json \
  --client <client> --manifest output/<client>/<run-id>/run_manifest.json \
  --results-out output/<client>/<run-id>/batch_results.json \
  --prompt-review output/<client>/<run-id>/video_prompt_review.json --json
```
如需叠品牌 logo：`python3 scripts/brand_kit.py stamp --client <client> --in <mp4> --out <branded.mp4>`。
发客户：使用命令 JSON 返回的绝对 `absPath`，例如 `[社交快剪成片](</绝对路径/output/quickpromo-<日期>.mp4>)`。



## gpt-image-2 故事板/人物板确认（所有视频出片前必做）

完整剧本/分镜定稿后，**不要直接调用 `video_engine.py`**。先由你把剧本解析成 `output/storyboard_plan.json`：
- `characters[]`：所有出场人物/数字人/主持/嘉宾，写清 id、name、role、appearance、costume、personality、voice。
- `shots[]`：每段分镜，写清 id、duration、dialogue、visual、camera、characters、props/scene。

然后运行：
```bash
python3 scripts/storyboard.py --plan output/storyboard_plan.json --out-dir output/storyboard --model gpt-image-2 --json
```
把人物板 `cast_board.jpg` 和每段故事板 `shot_*.jpg` 用绝对路径 Markdown 展示给客户确认。**剪辑友好镜头差异**：检查相邻故事板是否有 30°–50° 机位偏移，或远/中/近/特写景别跨度；如果连续同角度同景别，先改 `storyboard_plan.json` 重出故事板，不要直接出视频。客户确认人物数量/形象、镜头构图、产品表达、镜头顺序后，才进入后面的 `video_engine.py` 出片。

## 完成标准

- 聚焦单一主张；前 2 秒有钩子；风格与品类匹配（带 video_style_prompt）。
- 客户定稿后出片，5-10s 竖版在 `output/` 可播放。失败如实告知，不伪造。
