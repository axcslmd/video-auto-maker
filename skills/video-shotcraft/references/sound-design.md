# sound-design — 声音设计

来源：模板片（`template/`）声音设计实战。准则编号 S1–S4 见 `references/aesthetic-rules.md`。

模板片的声音全部集中在一个文件里管理（`template/src/aifl/Main.tsx`：`SFX[]` 钉帧表），场景组件不含任何音频代码——声音是时间线级资产，不是镜头级资产。

---

## 1. 方法

**顺序：画面结构基本锁定 → 先铺 BGM 定能量骨架 → 逐拍钉 SFX。**

1. **BGM 先行，定能量骨架。** 一条 BGM 全片铺底，音量包络用 `interpolate` 做首尾淡入淡出（模板片 `[0, 30, TOTAL-50, TOTAL] → [0, 0.34, 0.34, 0]`，即 1s 淡入、1.7s 淡出）。BGM 音量压在 0.34 左右给 SFX 留 headroom。曲子的能量曲线要和分镜表的能量曲线对得上（低开 → 中段推进 → outro 峰值），候选曲必须垫进成片试听——单听曲子无法判断气质（S1）。
2. **词汇表按"片种"选，不按"事件"选（S1）。** 产品宣传片的 SFX 词汇 = whoosh(运镜) / impact(落地) / riser(铺垫) / sparkle(光效) / transition(转场)，禁用游戏 UI 音包（click/pluck/glass 类 tap 音）。模板片第一版按 UI 事件语义选音（click/drop/confirmation），用户一耳朵判死刑"太像游戏了"。
3. **SFX 逐拍钉帧、声明式表集中管理（S2）。** SFX 是 `{ from, src, volume }[]` 数组，逐条注释对应的画面动作（"hero card: whoosh up on the pop"），渲染时每条包一个 `<Sequence from={s.from}>`。杜绝凭感觉铺音效。
4. **通用音之外留"贴画面定制"槽位。** 词汇表定稿后，有辨识度的画面动作仍会要专属拟音（打字揭示配键盘声、逐周落入配 pop）——泛用 swoosh 盖不住这些动作（S4）。

时机教训：模板片的音频工作直到画面改了约 30 轮之后才开始（收尾最后几轮）；有一次把换 BGM 和画面重做混在同一轮改动里，画面随后继续改，SFX 全表报废重钉。**声音永远排在画面锁定之后**（S3），细节见第 4 节。

---

## 2. BGM 选型

### 演进：三易其稿（34 分钟）

| 稿 | 曲目 | 来源/授权 | 被换原因 |
|---|---|---|---|
| v1 | Kevin MacLeod – *Inspired*（暖色 ambient 钢琴底）；同批还下了备选 *Deliberate Thought*，从未引用 | incompetech，CC-BY | 用户："BGM换个更有节奏感，更激情欢快的"——钢琴 ambient 不够激情 |
| v2 | Kevin MacLeod – *Life of Riley*（欢快 folk-pop） | incompetech，CC-BY | 用户："这个配乐和音效都太像游戏了。你帮我找那种鼓点强的，节奏感强的"——欢快≠宣传片气质 |
| v3（定稿） | Mixkit tech-house 鼓底（`bgm-tech-house.mp3`），音量升至 0.34 | Mixkit license | 未再被换，沿用至成片 |

v1→v2 只隔 22 分钟就又被否——说明选曲时根本没在成片语境里听。**候选曲必须垫进成片试听后再定**（S1 自检项）。

### 选型判据（从用户原话反推）

- **鼓点强、节奏感强**，优先电子底（tech-house 类）；"好听/欢快"不是判据。
- 气质基准是"典型的产品宣传视频"——闭眼只听音轨，应像产品发布会预告，不像手机游戏（S1）。
- 能量曲线能托住全片结构：平稳鼓底适合叠 SFX 层次，旋律太抢的曲子会和拟音打架。

### 免费授权来源清单

| 来源 | 授权 | 适用 | 备注 |
|---|---|---|---|
| [Mixkit](https://mixkit.co/) | Mixkit License（免费商用、免署名） | BGM + 电影系 SFX（whoosh/impact/riser/sparkle） | 模板片定稿的 BGM 和 SFX 主力来源；批量下载后 metadata 常被抹掉，**下载时就记录曲名/URL**，否则事后无法反查（模板片 `bgm-tech-house.mp3` 已无法逐曲对回 Mixkit 曲库，商用前需复核） |
| [incompetech](https://incompetech.com/)（Kevin MacLeod） | CC-BY 4.0（需署名） | BGM，曲库大、按情绪筛选 | 模板片 v1/v2 出处；注意 CC-BY 的署名义务 |
| [Kenney](https://kenney.nl/assets)（音包） | CC0 | 游戏类项目 | **产品宣传片禁用**（S1）——模板片引入后被整批删除，仅列此供授权档案参考 |

---

## 3. SFX 词汇表

以下 14 个文件是 `assets/audio/` 的基础层（模板片实际使用的一批）；2026-07 扩充的 27 个新 SFX 与 `bgm/` 备选见 `assets/audio/ATTRIBUTION.md`。时长来自 ffprobe；"典型钉帧位置"引用模板片 `template/src/aifl/Main.tsx` SFX 表的定稿帧号（30fps、全片 1085f），复用时取其相对语义而非绝对数值。

| 文件 | 时长 | 用途场景 | 典型钉帧位置（模板片） | 来源与授权 |
|---|---|---|---|---|
| `bgm-tech-house.mp3` | 288.7s | 整片鼓底 BGM，tech-house 电子 | 全片铺底，音量包络 0→0.34→0.34→0 | Mixkit（无法逐曲反查，商用前复核） |
| `transition-soft.mp3` | 1.27s | 柔转场：品牌落定、场景切入 | f12 / f277 / f475 / f623 / f779（每次进新场景一发） | Mixkit |
| `whoosh-fast.mp3` | 1.76s | 快速运镜、批量元素飞走 | f78 brand→dashboard、f340/356 发牌加速、f435 筛选网格飞走 | Mixkit |
| `whoosh-big.mp3` | 2.32s | 大幅度运镜：弹起、拉远、回摆 | f127 hero 卡弹起、f308 orbit 拉远、f388 swoosh 回搜索栏 | Mixkit |
| `sparkle.mp3` | 4.55s | 光效 reveal：扫描光束、收尾闪光 | f141 hero 卡光束、f1005 结尾 rule 闪光 | Mixkit |
| `transition-snap.mp3` | 0.57s | 短促落定/贴回原位的 snap | f204 hero 卡 impact reseat | Mixkit |
| `swoosh-quick.mp3` | 0.78s | 字卡出场统一音、轻推镜 | f220/565/725/885 四张 title card、f455 点击后 push-in | Mixkit |
| `keyboard.mp3` | 19.6s | 真实键盘打字拟音（长样本，按段落裁剪用） | f401 搜索框输入（截 24f）、f781 周报页"自己写出来"（截 44f） | Mixkit |
| `click-camera.mp3` | 0.35s | 点击确认/快门感（全片最响 vol 0.6） | f451 点击卡片进详情、f648 papers 计数落定 | Mixkit |
| `riser-cine.mp3` | 4.81s | 电影系上升铺垫，进 finale | f945 outro 合影组装段起 | Mixkit |
| `impact-cine.mp3` | 4.06s | 电影系重音钉点（vol 0.55 全片 SFX 峰值） | f980 字标 stamp 落地 | Mixkit |
| `pop.mp3` | 0.48s | 列表条目逐个落入的短促 pop | f840–865 周报周列表 6 连发，每 5f 一发、音量 0.40→0.25 阶梯递减 | **来源待考** |
| `impact-transition.mp3` | 4.87s | **死资产：全片未被引用**，与定稿 SFX 同批下载的备用 impact | 无 | Mixkit（同批），未接线 |
| `typewriter.mp3` | 0.22s | **死资产：全片未被引用**。文档页揭示实际用的是 `keyboard.mp3` 截 44f，此文件下了没接线 | 无 | **来源待考** |

小结：12/14 在片中实际发声；2 个死资产（`impact-transition.mp3`、`typewriter.mp3`）如实保留并标注；2 个来源待考（`pop.mp3`、`typewriter.mp3`）。"Mixkit" 的判定依据是模板片 Main.tsx 的批量声明，无逐文件 URL——**商用前应逐个回查授权**。

---

## 4. 对齐技巧

### 4.1 钉帧方法

- **声明式中央注册表**：`SFX: { from, src, volume }[]`，每条注释对应的画面动作；渲染层遍历数组，每条包 `<Sequence from={s.from}>`。帧号表与分镜表（`AIFL_SHOTS`）放同一文件对照（S2）。
- **长样本靠 Sequence 截断，不剪音频文件**：`keyboard.mp3`（19.6s 原素材）按语境给 `durationInFrames` 24f 或 44f；其余统一 90f 让 ≤3s 素材自然播完。音频时长与画面动作严格等长（S4）。
- **音量分层**：BGM 0.34 打底，SFX 0.2–0.6 分层——点击确认 0.6 最响、pop 连发尾音 0.25 最轻，用响度表达"这一拍多重要"（曾出现的 0.14 出自已删除的 v2 pluck 连发串，不属于定稿区间）。

### 4.2 变调与防机枪感

**本仓库实证无 playbackRate 变调**（模板片全仓 `grep playbackRate` 零命中）。同类元素连发防机枪感靠三招组合（S2），不靠变调：

1. **双样本交替**：连发用两个近似样本轮流（模板片 deck-deal 8 连发 pluck_002/pluck_001 交替）。
2. **音量阶梯递减**：沿序列线性衰减做"距离感"——定稿 pop 6 连发 0.40/0.37/0.34/0.31/0.28/0.25。
3. **间隔跟随动画曲线加速**：连发间隔贴画面加速节拍收缩（8f→3f）；密到糊成一片时**主动让声音淡出成一条 swoosh**，不逐个配音——耳朵和眼睛一样，分辨不出糊掉的个体。

目标听感：连发段落是"数得出来的节奏"，不是机械复读（S2 自检项）。

### 4.3 riser→impact：镜头收束句式

大镜头（尤其 outro）的固定三拍：

```
riser-cine（组装/铺垫段起） → 约 35f 后 impact-cine（主体 stamp 落地，全片响度峰值） → 25f 后 sparkle（余韵光效）
```

模板片 f945→f980→f1005，是定稿声音方向确立后唯一从未改动的段落句式——能量铺垫、钉点、余韵三件套一次成型。其它可复用的小句式：场景切换 = `transition-soft` 一发；字卡出场 = `swoosh-quick` 统一音；点击确认 = `click-camera`（给全片最高 SFX 响度）。

### 4.4 返工教训：三次重钉的顺序账

模板片 SFX 全表重钉了三次，其中**两次纯属画面返工的连带成本**（S3）：

| 次 | 起因 | 性质 |
|---|---|---|
| 1 | 音色方向错误（游戏音包→电影系词汇） | 声音自身的返工，难免 |
| 2 | 画面时间线变更（总长 1020→1085），全表 from 值平移 | **画面没锁就钉音的连带成本** |
| 3 | 周报段画面加动画，该段 SFX 重写 | 同上（局部） |

规则化：**声音在画面锁定后做；任何改变镜头时长/顺序的修改，收尾动作固定包含"全表 SFX 帧号重对"**（S3）。反面案例：有一次把换 BGM 和 deck-deal 画面重做塞进同一 commit，画面随后又改，音效白钉。

### 4.5 钉帧一律相对 shot 起点，而非绝对帧（硬规则）

模板片的 SFX 表钉**绝对帧号**，单文件数组便于整表平移（第 2 次重钉一个 diff 完成），但绝对帧意味着零结构复用——任何一个镜头改时长，其后所有条目全部失效。

做法：钉帧写成 `SHOTS.<shot>.from + offset`（相对该镜头起点的偏移；卡点片写 `beatF(n)`，见 music-beat-sync.md）。这样镜头内部节拍不变时，改前面镜头的时长只需分镜表更新一处，SFX 自动跟随——绝大部分重钉可免除。新项目起手即按相对钉帧写，绝不写裸数字帧号。
