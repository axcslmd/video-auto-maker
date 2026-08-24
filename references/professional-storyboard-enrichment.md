# 专业分镜补强参考（保留本项目顾问式引导）

本文件用于把专业分镜能力吸收到当前营销视频 Agent。**只吸收专业能力，不迁移对方工作流**：本项目仍以 `AGENTS.md` 定义的 Agent 无关顾问式引导和确认闸门为主，各宿主仅提供薄入口适配。

参考来源包括：

- `cinematic-camera-movements.md`：电影级运镜库与适用场景。
- `cinematic-layouts.md`：景别、构图、画面层次。
- `character-prompts.md`：角色基础提示词 + 分镜角色状态提示词。
- `scene-prompts.md`：场景提示词结构。
- `prop-prompts.md`：核心道具/产品提示词结构。
- `micro-expression-control.md`：自然微表情与情绪递进。
- `audio-bgm.md`：环境音、转场音、强调音、BGM 节奏和音量。

---

## 1. 保留现有项目的引导方式

不要照搬外部 skill 的长表单。面对 0 基础客户仍按本项目规则：

1. **一次只问一组关键问题**，不要把角色、场景、景别、运镜、BGM 一次性全甩给客户。
2. **每个问题给选项 + 推荐 + 理由**，例如“这条更适合产品种草，因为需要先建立信任，再放参数特写”。
3. 客户回答后，同一条回复里完成：**确认 → 落盘 → 下一阶段推荐 + 提问**。
4. 昂贵生成前必须过确认闸门：**脚本/逐段分镜 → 人物/声音 → gpt-image-2 人物板 + 16:9、格数由 shots[] 决定的故事板 → 视频**。
5. 专业字段由当前本地 Agent 在脚本定稿后补齐，不要求客户填写专业术语。

一句话：**客户体验是顾问式，内部产物是导演级。**

---

## 2. `storyboard_plan.json` 推荐结构

每个 `shot` 除了原有 `id/duration/dialogue/visual/camera/characters/props`，建议补齐以下字段。字段可以由当前本地 Agent 根据已确认剧本、品牌资料、素材图、数字人人设推导，不要凭空编造真实产品外观。

```json
{
  "project_title": "Client Brand 20s product promo",
  "client": "acme",
  "aspect_ratio": "9:16",
  "visual_style": "premium commercial, clean warm studio, brand accent color",
  "continuity": {
    "background": "same premium studio with warm neutral wall and clean display table",
    "character_identity": "same host face, hairstyle, posture language and product handling",
    "wardrobe": "same white blazer and minimal accessories",
    "voice": "same calm confident Cantonese female host voice",
    "bgm": "same subtle upbeat premium lifestyle BGM, 30-40% volume",
    "lighting": "same soft key from camera-left, warm rim light, stable color temperature",
    "scene_relationship": "product always remains on the same table/right-hand interaction zone"
  },
  "characters": [
    {
      "id": "host",
      "name": "粤语女主持",
      "role": "host",
      "age": "young adult",
      "gender": "female",
      "ethnicity": "Asian / HK commercial presenter",
      "appearance": "young adult woman, oval face, tidy shoulder-length dark hair, warm eyes",
      "facial_features": {
        "face_shape": "oval face with soft jawline",
        "eyebrows": "natural dark arched eyebrows",
        "eyes": "warm almond-shaped dark brown eyes",
        "nose": "straight medium-height nose bridge",
        "lips": "natural soft pink lips",
        "ears": "small ears, simple earrings",
        "skin": "clear warm fair skin",
        "special_marks": "none, keep face clean and consistent"
      },
      "hair": "tidy shoulder-length dark hair, same parting in all views",
      "makeup": "natural commercial makeup, light lipstick",
      "costume": "white blazer, simple earrings, natural makeup",
      "shoes": "neutral business shoes if visible",
      "accessories": "simple earrings, no distracting jewelry",
      "body_features": {
        "height": "average height",
        "build": "slim professional build",
        "proportions": "natural 7.5-head commercial presenter proportion"
      },
      "personality": "professional, friendly, trustworthy",
      "default_expression": "calm, gentle, trustworthy half-smile",
      "voice": "calm Cantonese, confident premium commercial tone",
      "body_language": "moderate gestures, direct-to-lens engagement, product held carefully",
      "immutable_features": ["face shape", "eye shape/color", "nose bridge", "lip shape", "hair length/parting", "skin tone", "body proportion", "white blazer", "earrings"]
    }
  ],
  "shots": [
    {
      "id": "s1",
      "duration": 8,
      "dialogue": "本段最终台词",
      "visual": "主持人在展示台前拿起产品，先建立痛点，再展示关键卖点",
      "characters": ["host"],
      "scene": "scene_01_premium_studio",
      "props": "client product on clean display table",
      "shot_size": "wide establishing → medium close-up → product detail close-up",
      "camera_movement": "slow push-in, then subtle arc shot around the product",
      "angle_offset": "panel sequence alternates front, left-front 45°, right-front 35°",
      "composition": "rule of thirds; host eyes on upper third; product on lower-right intersection; negative space on top for later CTA overlay",
      "lighting": "soft key light from camera-left, warm rim light, glossy but controlled product highlights",
      "character_action": "host picks up product, turns it toward camera, points to the key feature, then returns eye contact",
      "micro_expression": "neutral attentive face → small reassuring smile → brighter smile when benefit is revealed; eyebrows relax, eyes stay focused",
      "scene_prompt": "室内高端品牌展示台，上午或柔和棚拍光，中性暖色背景，干净桌面，品牌色小面积点缀，空间关系稳定",
      "character_prompt": "主持人保持同一脸型、发型、白色西装、自然妆容；动作从拿起产品到面向镜头介绍，气质专业可信",
      "prop_prompts": [
        "道具-产品: 真实品牌产品外观，颜色、形状、Logo位置、材质反光和接口/包装关系保持稳定；第5/6格作为特写视觉焦点",
        "道具-展示台: 干净浅色展示台，产品固定放在右前方，角色手部与产品互动关系一致"
      ],
      "panel_plan": [
        "1 建立空间：展示台和主持人全景",
        "2 主体入场：主持人拿起产品",
        "3 中景介绍：视线回到镜头",
        "4 表情反应：痛点/信任感",
        "5 产品动作特写：核心卖点",
        "6 材质/功能细节：接口/纹理/包装",
        "7 运镜推进：横移或弧形绕产品",
        "8 价值兑现：人物、产品、场景合一",
         "9 材质/光线释放：稳定展示产品质感",
         "10 运镜收束：回到人物与产品关系",
         "11 情绪兑现：人物确认式微笑",
         "12 结尾停留：留白给后期文字"
      ],
      "audio": {
        "voice": "same calm confident Cantonese host voice, clear pace",
        "bgm": "light electronic/premium lifestyle BGM, 30-40% volume, consistent tempo",
        "sfx": "soft whoosh on transition, gentle product handling sound only on close-up"
      },
      "video_prompt_notes": "Use final 16:9 4x3 storyboard as primary visual reference; follow panels 1 to 12 in order; do not animate the grid as a flat image.",
      "motion_elements": [
        "如该镜头需要 kinetic typography / 数据标签快闪 / slogan 逐字浮现 / 图标动效等，在此列出，供 HyperFrames 后期渲染叠加层，不要求 seedream/视频模型在画面里生成这些文字/图形（AGENTS.md 铁律#15）"
      ]
    }
  ]
}
```

---

## 3. 人物六视图参考板标准（吸收 `jubenrenwugoujian-7.0.0`）

人物板不是普通出场人物拼图，而是后续数字人/故事板/图生视频的**身份锁定参考图**。生成 `cast_board.jpg` 时，每个出场人物必须按剧本角色原型拆解出不可变身份特征，并在一张白底参考板里呈现六种视图。

### 3.1 角色信息提取维度

从已确认剧本、品牌资料和数字人人设中提取，能填则填，不能凭空编造真实特征：

| 维度 | 必填优先级 | 用途 |
|---|---|---|
| 基础身份 | 年龄、性别、职业/角色、族裔/地域气质 | 决定商业可信度和目标市场本地化 |
| 面部核心 | 脸型、眉形/眉色、眼形/眼色、鼻形、唇形、耳形、肤色、特殊标记 | 跨视图身份一致性锁 |
| 发型妆容 | 发长、分路、刘海、发色、妆容浓淡、唇色 | 防止正面/背面/侧面换人 |
| 体型比例 | 身高感、胖瘦、肩宽、头身比、姿态习惯 | 防止全身视图比例漂移 |
| 服饰配饰 | 上装、下装、鞋、首饰、眼镜、手表、Logo/胸牌位置 | 防止故事板和视频换装 |
| 默认神态 | 中性表情、商业微笑、眼神气质 | 供正脸特写和侧脸特写统一 |
| 不可变特征 | 以上所有必须锁定的字段列表 | 明确告诉模型哪些不能变 |

### 3.2 六视图布局

一张 `cast_board.jpg` 内，每个角色都要有清晰 2x3 或 3x2 网格：

| 格 | 视图 | 必须看到 |
|---|---|---|
| 1 | 全身正视图 | 从头到脚、双脚完整、正面站姿、全套服装和鞋 |
| 2 | 全身后视图 | 背对镜头、后背服装、发型背面、轮廓剪影 |
| 3 | 全身侧视图 | 完整侧面轮廓、鼻/下巴/profile、服装侧缝和体型比例 |
| 4 | 正脸正视图 | 85mm 级别正面脸部特写、五官清晰、中性/温和表情 |
| 5 | 正脸后视图/后脑勺视图 | 后脑勺、发际线、发型后部、耳朵/颈部细节 |
| 6 | 正脸侧视图 | 侧脸特写，颧骨、眉眼、鼻梁、嘴唇、下颌线清晰 |

### 3.3 白底摄影棚标准

- 背景：`pure white seamless background #FFFFFF`，不要室外、暗背景、花纹背景。
- 灯光：高调柔光、三点布光、5500K 中性色温，面部不要过曝。
- 镜头语言：脸部特写可写 `85mm portrait, f/5.6`；全身图要求 `full body in frame, feet visible`。
- 标签：只允许极小视图标签；不要长字幕、不要大段文字、不要水印。
- 多角色：每个角色单独一个六视图 reference block，不要混脸、不要共享服装。

### 3.4 人物板负面约束

统一追加：

```text
different face, inconsistent identity, changed hairstyle, changed outfit, different body proportions, wrong back view, duplicate panels, missing feet, cropped full body, extra limbs, bad hands, asymmetrical face, blurry, low quality, text blocks, watermark, dark background, patterned background
```

### 3.5 检查清单

生成后给客户确认时，至少检查：

- 全身正/后/侧三张是否都完整，不裁脚；
- 正脸正/后/侧三张是否都存在，不重复同一角度；
- 脸型、眼形、鼻梁、唇形、肤色、发型、体型比例是否一致；
- 服装、鞋、配饰、Logo/胸牌位置是否一致；
- 背面图是否真的是背面，不是 3/4 侧面或又一张正面；
- 是否出现多余文字、水印、背景图案、换装、换人。

---

## 4. 运镜补强规则

优先使用 AI 视频模型容易理解的清晰描述，不要堆砌术语。`storyboard_plan.json` 的相邻 shot 应有可剪辑的镜头跨度；故事板格数由已确认分镜数量决定，不为凑固定 12 格而拆分或合并。

| 中文 | 英文提示 | 适合场景 | 使用注意 |
|---|---|---|---|
| 缓推 | slow push-in / dolly in | 引出卖点、情绪递进、信任建立 | 适合开场或核心卖点，不要每格都推 |
| 缓拉 | slow pull-out / reveal pull-out | 从细节回到场景、结尾 CTA | 适合第8/9格收束 |
| 横移 | truck left/right / side slide | 产品展示、场景层次 | 保持空间方向一致，避免左右关系跳变 |
| 侧跟 | profile tracking | 人物移动、服务流程、门店介绍 | 用于实景服务或产品使用流程 |
| 弧形环绕 | arc shot / orbital move | 产品高级感、人物+产品关系 | 建议 30°–90°，不要 360°，防止模型漂移 |
| 俯拍降镜 | crane down / high-to-eye-level | 从环境进入主体 | 适合场地/工厂/展台建立镜头 |
| 轻摇 | subtle pan / tilt | 展示空间或参数内容页 | 幅度小，保持主体清晰 |
| 甩镜 | whip pan | 快剪转场 | 谨慎使用，容易糊；只用于明确转场 |
| 静稳镜 | locked-off stable shot | 信任、专业、CTA | 结尾、专业讲解、确认信息适合 |

### 相邻镜头差异铁律

相邻格/相邻段至少满足其一：

1. **机位/主体朝向偏移 30°–50°**：正面 → 左前 45° → 右前 35°。
2. **景别变化**：远景/全景 → 中景 → 近景 → 产品/手部/材质特写。
3. **构图重心变化**：人物居中 → 产品三分线 → 手部细节 → 留白 CTA。

如果 3x3 九格看起来像同一个正面中景的复制，必须回到 `storyboard_plan.json` 修改后重生。

---

## 5. 电影级 16:9 动态故事板布局标准

每条视频 = 一张 16:9 动态格数故事板。格数来自 `shots[]`：N≤4 通常用 2x2，N=5/6 用接近 3x2 的规整布局，N=9 用 3x3，N=12 才使用 4x3。不要为了凑 12 格拆分或合并已确认分镜。

| 格 | 作用 | 推荐景别/构图 | 常用运镜/动作 |
|---|---|---|---|
| 1 | 建立空间 | 全景/远景；场景关系清晰 | 静稳或轻微降镜 |
| 2 | 主体/产品入场 | 中景；主体进入注意力中心 | 轻横移/主体动作 |
| 3 | 核心表达 | 中近景；人物眼神和产品同框 | 缓推 |
| 4 | 情绪反应 | 近景；脸/手势/痛点反应 | 稳镜或小推 |
| 5 | 产品/动作特写 | 产品、手部、功能点 | 微距/细节特写 |
| 6 | 材质/信息细节 | 材质、接口、包装、使用状态 | 静稳细节镜 |
| 7 | 运动推进 | 人物/产品/场景关系变化 | 横移/弧形环绕 |
| 8 | 价值兑现 | 人物、产品、结果同框 | 缓拉或弧形结束 |
| 9 | 收束/CTA | 稳定构图；留白给后期字幕/Logo | 静稳镜 |

可用构图词：三分法、对称构图、前景框架、引导线、负空间留白、中心构图、产品三分线特写、低角度高级感、高角度建立空间。正式提示词里要写清楚：**主体在哪里、产品在哪里、视线/手势指向哪里、留白在哪里**。

---

## 6. 角色 / 场景 / 道具资产提示词

### 6.1 角色提示词

角色提示词分两层：

1. **基础角色提示词**：锁定不会变的身份。
   - 年龄、性别、脸型、发型、服饰、配饰、体态、视线习惯、整体气质。
   - 不要把“微笑/惊讶/奔跑”这类临时状态写进基础提示词。
2. **分镜角色提示词**：描述该段的状态。
   - 外观状态、服饰是否变化、表情神态、动作姿势、场景定位。
   - 多段拼接时服饰和核心外貌原则上不变，只变动作和镜头。

推荐结构：

```text
角色-主持人: 年轻女性，椭圆脸，肩长深色整齐发型，白色西装，自然妆容，简洁耳饰，站姿挺拔，眼神直视镜头，专业可信，电影级柔光，高质量写实商业广告风格
主持人-分镜s1: 同一外貌和服饰，站在展示台左侧，右手拿起产品，先自然专注再露出小幅安心微笑，视线在产品和镜头之间切换
```

### 6.2 场景提示词

场景提示词用于锁定空间，不要每段换一个世界。结构：

```text
场景-[名称]: 环境类型 + 时间/天气 + 光线方向 + 色调 + 关键元素 + 空间关系 + 与品牌/产品的关系
```

示例：

```text
场景-高端展示台: 室内品牌展示空间，上午柔和棚拍光，暖中性色调，浅色展示台、干净背景墙、少量品牌红色点缀，产品固定在桌面右前方，主持人站在左侧，背景和光线在所有段落保持一致
```

### 6.3 产品/道具提示词

只抓重要道具：推动剧情、角色互动、特写出现、品牌识别相关。不要给纯装饰物写一堆细节。

结构：

```text
道具-[名称]: 外观描述 + 材质质感 + 状态特征 + 位置/场景关联 + 角色互动 + 一致性要求
```

示例：

```text
道具-产品: 客户真实产品，主色和 Logo 位置严格保持，表面材质反光稳定，包装/接口/按钮关系不漂移；出现在第2-9格，第5/6格为特写，主持人右手托举并轻转面向镜头
```

---

## 7. 微表情与表演控制

微表情要用自然语言写“情绪为什么变化、如何渐变”，不要机械写强度等级。营销视频常用节奏：

- **痛点开场**：眉头轻微收紧、眼神专注、嘴角中性。
- **提出解决方案**：眉眼放松、轻微点头、嘴角出现小幅安心笑。
- **展示卖点**：眼神更亮、动作更坚定、语速略有能量。
- **价值兑现**：微笑变自然开放、身体朝向产品或镜头更稳定。
- **CTA**：亲和但不夸张，稳定直视镜头。

写法示例：

```text
micro_expression: starts with a focused neutral expression when describing the pain point, then eyebrows relax and a small reassuring smile appears as the solution is shown; eyes brighten on the key benefit, ending with calm confident eye contact.
```

中文也可以：

```text
micro_expression: 开场是专注中性的表情；说到解决方案时眉眼放松、轻微点头；展示卖点时眼神更亮、笑容更自然；结尾稳定看镜头，传递可信任感。
```

---

## 8. 音效和 BGM 建议

视频模型是音画一体，但脚本仍要显式描述音频方向，尤其是多段拼接的一致性。

### 8.1 环境音

- 办公室/会议室：键盘、轻微空调、纸张或杯子声。
- 门店/商场：轻背景音乐、人流、购物车或脚步声。
- 户外：风声、车流、人群环境音。
- 产品操作：轻点击、插拔、开合、滑动、轻放置声。

### 8.2 转场音

- 科技/3C：clean digital swoosh、data flow whoosh。
- 高端生活方式：soft whoosh、gentle chime。
- 快剪带货：pop、swish、snappy click。
- 严肃专业：clean subtle transition，不要夸张。

### 8.3 BGM

| 视频类型 | BGM | 音量建议 |
|---|---|---|
| 讲解/教程 | 轻电子、钢琴、氛围音乐 | 20–40%，不能压旁白 |
| 产品介绍 | 轻快流行、活力电子、高端 lifestyle | 30–50% |
| 品牌 TVC | 弦乐/电子氛围/电影感鼓点 | 30–50%，高潮可上扬 |
| 数据报告/专业服务 | 低音量科技氛围 | 20–30% |
| CTA 收尾 | 稳定、渐弱、有完成感 | 30–40%，尾部 1–2 秒淡出 |

### 8.4 字段写法

```json
"audio": {
  "voice": "same Cantonese female host, calm confident, medium pace, clear articulation",
  "bgm": "premium lifestyle electronic BGM, same tempo across all stitched segments, 35% volume, 1s fade out at ending",
  "sfx": "soft whoosh between panel 3 and 4, gentle product click on close-up; no excessive cartoon sounds"
}
```

---

## 9. 生成 gpt-image-2 故事板前的检查清单

当前本地 Agent 在运行 `scripts/storyboard.py` 前必须自查：

- [ ] 每条视频都有一张 16:9 动态格数故事板，不是单帧；格数等于已确认 `shots[]` 分镜数量。
- [ ] shots[] 体现连续剧情：建立 → 入场/操作 → 核心表达 → 产品/人物反应 → 价值兑现 → 收束；不为了凑 12 格而拆分或合并。
- [ ] 相邻 shot 满足 30°–50° 角度偏移、景别变化或构图重心变化。
- [ ] 两段/多段拼接时，顶层 `continuity` 至少有 `background / character_identity / wardrobe / voice / bgm / lighting`。
- [ ] 角色外貌、服装、体态语言稳定；只改变动作/表情/机位。
- [ ] 产品/道具的形状、颜色、Logo、位置、互动关系没有自相矛盾。
- [ ] 场景不是每段重写成不同空间；变化发生在机位和构图，而不是物理世界。
- [ ] 不要求 gpt-image-2 生成字幕、水印、长文字、kinetic typography、数据标签快闪；这类需求写进该 shot 的 `motion_elements`，CTA/字幕/动效交给 HyperFrames 后期确定性叠加（AGENTS.md 铁律#15）。
- [ ] 输出会写到 `output/storyboard/<run-id>/`，并返回 `preview_html / embedded_md / index_md` 供客户真正看图确认。

---

## 10. 映射到当前项目执行链路

1. 顾问式共创得到客户确认的脚本和逐段分镜。
2. 当前本地 Agent 根据本文件补齐专业字段，写入 `output/storyboard_plan.json`。
3. 运行：

```bash
python3 scripts/storyboard.py \
  --plan output/storyboard_plan.json \
  --out-dir output/storyboard \
  --model gpt-image-2 \
  --json
```

4. 把返回 JSON 的 `preview_html` 优先展示给客户；若当前宿主渲染异常，再展示 `storyboard_embedded.md` 或 `storyboard_index.md`。
5. 客户确认后，先对最终 segments 运行 `video_engine.py --preflight --preflight-out <video_preflight.json>`，再把同一文件传给 `capture-video --preflight-json`。Seedance 路径使用确认故事板/contact sheet + 当前 shot 的 ref_tags 素材；实际需要 Kling 时，先用 `panel_expansion.py` 生成当前 shot 的单格展开图，完成 QA、展示和客户确认，并对改变后的最终 segments 重新 preflight/capture/确认。晚发隐私回落同样先停下补确认，不能自动提交未展示的展开图；design-board/身份底片不做单格展开。之后才调用 `video_engine.py`；两条路径都逐 shot 生成，并保持画面、声音人设、BGM 和 SFX 连续。
