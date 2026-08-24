# /asset-prep — 产品素材导入 + 产品 Brief

你负责把客户上传的**产品图 + PPT + 文字资料**整理成一份结构化「产品 Brief」。这份 brief 是所有脚本的**接地上下文**——有了它，脚本才言之有物、对齐真实产品，而不是空泛套模板。文本解析全在本地（零 API）。

## 开场先做

**密钥准入闸门（铁律，先做）**：`python3 scripts/key_setup.py gate`。`BLOCKED`/`QUARANTINED` 时立即停止；客户仅通过宿主安全凭证输入录入 key，Agent 用不回显的标准输入执行 `python3 scripts/key_setup.py save --stdin --session-id <公共会话ID>`；严禁普通聊天收集，隔离状态须先轮换并更换指纹，`STORED` 才继续。

## 什么时候用

- 客户发来产品图 / .pptx / 产品参数文档时，先跑这个整理成 brief。
- 任何场景 skill（口播/访谈/服务/四条营销线）在写脚本前，都应先读 `assets/<client>/brief.json`。

## 素材分诊 → 视频主题（最高优先，先做）

**客户提供的素材类型直接决定视频主题和模板——这一步优先级最高，必须在选场景/写脚本之前完成。** 先判断客户给的是「文档文案」还是「图片」，据此锁定主题走向：

| 客户给的 | 素材性质 | 视频主题 | 内容引导方式 | 推荐路由 |
|---|---|---|---|---|
| 产品介绍书 / 营销方案 / 课程大纲 / 服务说明 / PPT / 长文案 | **文档/文案** | 内容讲解型（把一件事讲清楚） | 拆信息层级：核心论点→支撑要点→行动号召；数字人讲师逐点展开 | `/explainer-video`；如只要营销策略文档则属独立拆单 |
| 商品照 / 包装图 / 细节特写 | **商品图** | 商品推荐/带货 | 排卖点：第一眼视觉钩子→核心卖点特写→参数快闪→种草 CTA | 纯产品展示 `/brand-tvc`；主持人带货 `/oral-broadcast` |
| 车间 / 门店 / 办公 / 服务现场 / 场地图 | **场地/工厂图** | 实景介绍（工厂/场景/服务） | 实景漫游+可信背书：环境实拍→规模/资质→服务讲解漏斗 | `/explainer-video` 实景服务子类型 |
| 人物照（老板/员工/代言人） | **人物图** | 先建数字人形象 | 定人设（身份/性格/口头禅）再进任意口播 | `/digital-human` → 口播 |
| 网页截图（电商详情页/落地页截屏） | **截图，需标准化** | 视觉素材不干净，需先清洗再定主题 | 先跑 `standardize`（见下）把截图转成干净参考图，再按图片实际内容（商品/场地/人物）回到上面对应行分诊 | 先 `standardize`，再按内容路由 |
| 视频模板（客户发的参考短视频/竞品视频/往期成片） | **视频截帧，需标准化** | 客户想「套用这个模板」的构图/风格 | 先跑 `standardize`（自动抽中间帧）把它转成一张标准化参考图，再按风格/构图套进选定场景 | 先 `standardize`，再按风格套用创作 skill |
| 文档 + 图混合 | **混合** | 以客户核心目的为准 | 先问「这条视频最想让人记住/去做什么」，据此定主题、图作素材佐证 | 按主目的路由 |

**两类素材的引导逻辑根本不同，别混用：**
- **文档类 = 讲清楚**：视频在「传递信息、建立理解」。引导重点是帮客户把冗长资料**提炼成有层级的讲解脚本**（一个核心信息 + 3-5 个支撑点 + 一个 CTA），配数字人讲师 + 内容页。别堆参数。
- **图片类 = 展示+种草**：视频在「视觉吸引、激发行动」。引导重点是帮客户**排出视觉叙事顺序**（先抓眼球拍什么、卖点怎么递进、结尾怎么导流），配运镜特写 + 参数快闪 + CTA。台词短、节奏快。

分诊完成后，把判定的主题/素材类型写进 brief（`set-profile` 的 product-type 或 style-hints），并把客户导流到上表对应的创作 skill，再进 `script-cocreation.md` 细化。

## 网页截图 / 视频模板 → standardize（先清洗再分诊）

客户上传的素材有时不是「干净的产品图」，而是：
- **网页截图**：电商详情页/落地页截屏，画面里混着导航栏、按钮、价格角标、其它无关内容。
- **视频模板**：客户发来一个参考短视频/竞品视频/往期成片，想「套用它的构图/风格」，本身不是静态图。

这两类**不能直接 `ingest-image` 入库当锚定图用**（截图有干扰元素、视频不是图片格式）。先跑 `standardize` 清洗成一张干净的标准化参考图，再回到上面的分诊表按图片实际内容（商品/场地/人物）继续走：

```
python3 scripts/asset_prep.py standardize --client <client> \
  --source <截图路径 或 视频模板路径> \
  --prompt "<你想要的标准化效果，如：只保留商品本身，去掉网页UI元素，白底产品图>" \
  --tag hero
```

- `--source` 为图片（截图）时直接作参考图；为视频（`.mp4/.mov/.webm` 等）时**本地 ffmpeg 自动抽中间帧**再作参考图，零 API、不占 Credit。
- `--prompt` 是当前本地 Agent 根据客户诉求写的标准化需求描述，例如「去掉网页背景和按钮，只留产品主体，居中白底」或「保留这个视频里的构图和运镜风格，换成客户自己的产品」。
- 走的是异步 `/v1/image-generations` 接口（参考图+文字需求），与 `gen-image` 走的同步接口是两条独立路径，但**确认闸门完全一致**：产出同样 `status:pending`，必须渲染 `abspath` 给客户看、客户选定后 `confirm-image` 才能进出片，不能因为走了新接口就绕过确认铁律。
- 客户确认后的标准化图，再按其实际内容（商品/场地/人物）回到上面分诊表对应行，继续 `script-cocreation.md` 的引导流程。

## 素材完整性诊断 + 缺口补齐（铁律：图生视频靠素材图锚定）

**成片方法论：每个镜位 = 素材图 + 文字台词 → 图生视频。** 素材图是成片锚点，保证产品/场景/人物真实可控。所以分诊后、出片前，必须确认**每个要出镜的镜位都有一张锚定图**，缺了就补，绝不用纯文生视频糊弄。

1. **诊断缺口**：镜位骨架定了之后（引导表 rows 或需求 tag 清单），跑
   `python3 scripts/asset_prep.py assess --client <c> --segments-file <引导表.json>`
   （或 `--need-tags hero detail pack scene`）。看 `missing` 里缺哪些镜位的图、`coverage` 覆盖率。
2. **缺口补齐（主动引导客户，二选一）**：
   - **优先真实图**：引导客户上传对应镜位的真实产品/场景图 → `ingest-image --tag <镜位>`。真实素材永远比生成的可信。
   - **客户没有 / 需要衍生场景**：用 `gen-image` 生成锚定图（**默认带两遍清洗**）：
     `python3 scripts/asset_prep.py gen-image --client <c> --prompt "<画面描述>" --tag <镜位> [--ref <现有产品图>]`
     带 `--ref` 走图生图，以真实产品为基准衍生（换背景/换场景/补角度），保持产品一致性；不带 `--ref` 走文生图。
3. **文生图两遍清洗 + 确认闸门（铁律，生图必走）**：`gen-image` 默认产出**两版候选**——
   pass1(v1) 首版 + pass2(v2) 图生图精修版（提清晰度/修瑕疵，主体不变），都标 `status:pending`。
   - **必须把两版图片真正显示给客户看**：gen-image 返回的每版里有 `abspath`（绝对路径），
     用 markdown 图片语法把图渲染出来（客户 0 基础，只给路径文字他看不到）：
     ```
     这是「主图 hero」的两个版本，你看看更喜欢哪个：

     v1 首版：
     ![hero-v1](<pass1.abspath>)
     v2 精修版（更清晰、细节更干净）：
     ![hero-v2](<pass2.abspath>)
     ```
     然后问：「用 v1 还是 v2？或者哪里想调整（比如背景、角度、颜色）？」路径含中文/空格时用尖括号包住。
    - 客户提修改项 → `refine-image --client <c> --file <选定图path> --edit "<修改描述>"`，
      如果客户同时上传了问题截图或补充素材，把它们作为后续参考图传入（可传一张或多张）：
      `refine-image --client <c> --file <选定图path> --feedback-ref <反馈图1> <反馈图2> --edit "<修改描述>"`。
      第一张图用于锁定人物/产品身份，反馈图片用于理解客户要求和需要调整的细节；反馈图片不会直接成为成片素材。
      再把返回的 `abspath` 用同样方式渲染给客户看，直到满意。
   - 客户满意 → `confirm-image --client <c> --file <最终选定图path> --customer-confirmation '<客户确认原话>'`（自动丢弃同镜位其它候选）。**只有带当前客户证据的 confirmed 图才进出片。**
   - 赶时间/省 Credit 时才用 `--no-refine` 只出一版，但仍要渲染给客户确认。
   - 若客户上传的是**真实产品图**（`ingest-image`），无需两遍清洗与确认，直接可用。
4. **补齐 + 全部确认后重新诊断** `assess` 直到 `complete:true`，再进出片。`assess` 结果里：
   - `missing` = 完全缺图的镜位（回 ② 补齐）；
   - `pending_confirmation` = 已生成但**客户还没确认**的候选（回 ③ 走 confirm-image）；
   - **只要 `missing` 或 `pending_confirmation` 非空，`complete` 就是 false，不能出片。**
    **绝不因为缺图就退回文生视频（type1）；也绝不拿未确认的 pending 图出片。**

### 客户补充反馈必须回到当前素材确认闸门

客户在确认候选图后补充「人物佩戴有问题」「产品位置不对」「比例不协调」等反馈，
视为当前素材未通过确认，不能回复能力范围外，也不能继续脚本、故事板或视频流程：

1. 明确复述问题，例如「耳夹没有正确夹在耳廓外侧，疑似进入耳道/方向反了」，或「客户上传的新参考图要求改用这个佩戴角度」。
2. 保留原候选为 `pending`，不要执行 `confirm-image`。
3. 只有文字反馈时，把文字完整整理为 `--edit` 修改要求；客户上传图片时，把上传图片作为 `--feedback-ref` 重新提交给图像模型，可同时保留文字要求。
4. 把新候选的绝对路径用 Markdown 图片展示，重新询问是否 OK。
5. 只有客户明确确认新版本后，才执行 `confirm-image`，再进入脚本逐段定稿。

反馈图片的处理边界：它们是本次重生成的参考输入，不自动写入客户正式 `brief.json`，也不自动替代已确认素材；只有重新生成的候选图经过客户确认后，才替代原镜位素材。

**判定后按素材类别打开对应的内容引导子模板（逐字段引导，别凭感觉聊）：**
- 文档/文案类 → 读 `guide-document-class.md`：三轮引导（抽核心信息→选 3-5 支撑点→定 CTA）+ **信息层级拆解表**，产出讲解脚本骨架。
- 图片类 → 读 `guide-image-class.md`：给每张图定角色（hero/detail/scene 或 establish/scale/proof/service）+ **视觉叙事分镜表**，产出分镜脚本。商品图走带货表，场地/工厂图走实景背书表。

## 执行

### 用户上传图片的强制落盘顺序

用户在对话中上传产品图后，**不能只把聊天附件路径写进剧本或
`storyboard_plan.json`**。必须先把附件复制进客户素材库，再把命令返回的
`path` 写入 brief/plan：

1. 先执行 `ingest-image --client <client> --file <用户上传文件> --tag hero`
   （细节图、包装图、场景图分别使用 `detail`、`pack`、`scene`）。
2. 检查返回 JSON 中的 `path`，并执行 `brief --client <client>` 确认该图片已经出现在
   `assets/<client>/brief.json.images[]`。
3. 再用该入库后的相对路径做 `analyze-image`，并把同一路径作为
   `storyboard_plan.json.asset_refs.product_images`；不要使用聊天临时附件路径、失效旧路径或
   图片描述代替产品图。
4. 只有看到 brief 中存在真实图片路径后，才允许运行 `storyboard.py`。如果计划中已有
   `asset_refs.product_images`，但文件已不存在，先回到第 1 步或重新生成计划，不能继续调用
   BasicRouter。

真实上传图无需走 `gen-image` 的 pending 确认流程，但仍必须通过有效 PNG/JPEG/WebP 文件校验。

- 解析 PPT（抽 slogan/USPs/规格/文字/内嵌图数量）：
  `python3 scripts/asset_prep.py parse-ppt --client <client> --file <客户.pptx路径>`
- 导入产品图（入库，标 tag 便于引用，如 hero/detail/scene）：
  `python3 scripts/asset_prep.py ingest-image --client <client> --file <图片路径> --tag hero`
- 查看合并后的 brief：
  `python3 scripts/asset_prep.py brief --client <client>`

## 解析后你要做的（本地模型，不调 API）

parse-doc 会机械抽取 `specs`（key:value）、`texts`、`slogans`。你要在此基础上**智能提炼**：
- 从 texts/specs 里归纳出 3-5 条真正的 **USPs**（卖点），去重去噪。
- 识别品牌 slogan、目标人群线索。

## 判断产品类型 → 渲染/动画风格（关键）

**动画风格必须根据产品类型由你判断，不套固定模板。** 参考 `render-style-guide.md`：
1. 从产品资料判断品类（数码3C/美妆/食品/母婴/服饰/家居/金融/教育/健康…）。
2. 按品类映射出 render_profile（视觉风格/动效手法/色调节奏/字体感/video_style_prompt）。
3. 拿不准时引导对话跟客户确认风格方向（给 2 选项+推荐）。
4. 写回 brief：
   ```
   python3 scripts/asset_prep.py set-profile --client <client> \
     --product-type "数码3C-充电宝" \
     --render-profile '{"visual":"科技快闪","motion":"粒子/电流光效快切","palette_hint":"深色+品牌红","pace":"快","font_feel":"无衬线粗体","video_style_prompt":"商業廣告動態,深色科技背景,粒子電流光效,緊湊快切,kinetic typography 高級質感"}'
   ```
后续所有出图/出片/动态文字都会带上这个 `video_style_prompt` 风格前缀，保证风格与品类匹配且跨产出统一。

## 产品图的用途（图生视频主路径）

打了 tag 的素材图是**每个镜位的成片锚点**，出片时作参考图传给 `video_engine.py`——这是成片主路径，不是可选项：
- `video_type:4` 参考图人景同框（数字人 + 产品/场景，最常用）
- `video_type:2` 首帧图生视频，让产品图动起来（实景/快促）
- `video_type:5` 产品图 + 数字人多图参考融合
- 需要去背/换场景时：`asset_prep.py cutout`（去背）或 `gen-image --ref <图>`（图生图换场景），产出新锚定图再出片。

出片一律「图 + 文字台词 → 图生视频」。只有确无产品/场景要展示的纯数字人口播，才允许 segment 使用 `video_type:1`；需要数字人像锚时应使用 `video_type:4`。这些值在提示词审批前写入 segments，不使用 CLI 事后覆写。

## 融合创建高质量脚本（关键）

写任何脚本前，先读 brief，让脚本**融合真实产品信息**：
- 卖点用 brief 里的真实 USPs（如「65W 快充」「僅 320g」），不要编参数。
- 口播/TVC 的钩子和 CTA 围绕 brief 的目标人群和 slogan。
- 服务介绍的讲解点对齐 brief 的真实规格，不夸大。
- 若 brief 缺关键信息（如没有卖点），回到引导式对话向客户补齐，别硬编。

## 完成标准

- `brief.json` 含真实 specs/USPs/slogan/产品图，客户资料已结构化入库。
- 后续脚本引用的产品信息均来自 brief，可追溯，无编造参数。
