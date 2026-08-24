# AI 营销视频工作室

[![CI](https://github.com/axcslmd/video-auto-maker/actions/workflows/ci.yml/badge.svg)](https://github.com/axcslmd/video-auto-maker/actions/workflows/ci.yml)

> **你出创意，我出成片。** 从一句话需求到可发布的营销视频，全程引导式完成。

## 文档导航

| 你是谁 | 看这个 |
|--------|--------|
| **我是客户，想做视频** | [客户指南](docs/customer/CUSTOMER_GUIDE.md) — 客户使用指南 |
| **我是交付人员** | [交付清单](docs/operations/DELIVERY_CHECKLIST.md) — 交付检查清单 |
| **我是技术负责人** | [项目概览](docs/architecture/PROJECT_OVERVIEW.md) — 项目概览 |
| **我是开发者** | 继续往下读本文档 |

安全漏洞请通过 GitHub 私密报告入口或按 [SECURITY.md](SECURITY.md) 联系项目作者；贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CLA.md](CLA.md)。

---

## 开发者文档

> **使用前先解压：** 不要直接把 zip 当作工作目录。请先解压，再用任意兼容 Agent 打开 `marketing-video-agent/` 文件夹。客户只需要记住通用入口 `/video-auto-maker`；宿主专用配置只是内部薄适配，不作为客户入口。
>
> **分镜图片说明：** 分镜图片只有在实际运行 `python3 scripts/storyboard.py --plan output/storyboard_plan.json --out-dir output/storyboard --model gpt-image-2 --json` 后才会生成。成功日志必须看到 `[gpt-image-2] rendering ...` 和 `"ok": true`。默认会写入 `output/storyboard/<run-id>/`，每次会话独立保存，避免覆盖旧的人物板/故事板。如果当前 Agent 没有这些日志，说明还没有调用 gpt-image-2。若宿主界面不显示本地路径图片，请改看返回 JSON 里的 `preview_html` 或 `embedded_md`。

### 目录形态

```text
project/
├── AGENTS.md + AGENT_ENTRY_PROTOCOL.md    # 人类/Agent 唯一公共协议
├── runtime-manifest.json + pyproject.toml # 运行时与工程元数据
├── .codex/ .kilo/ .workbuddy/             # 宿主自动发现薄入口
├── adapters/                               # 其他宿主入口说明
├── scripts/                                # Agent 无关业务内核与稳定 CLI
├── skills/ prompts/ references/            # 行为、共创与专业知识
├── schemas/ policies/                      # 机器可校验合同与闸门
├── evals/ tests/                           # 业务评测与回归测试
├── packages/                               # 三个售卖模块的边界定义
├── assets/ brand/ actors/                  # 按 client 隔离的业务素材
├── remotion_engine/                        # 确定性视频编排引擎
├── offline-assets/                         # 可选的本机私有运行时（忽略且不进入社区发布包）
└── docs/                                   # 客户、交付、架构文档
```

根目录只保留 Agent 自动发现、统一协议、运行时清单和部署必需文件。历史说明文档不再平铺；渲染临时 `_props*.json`、输出、缓存和本地会话状态不会进入交付包。`scripts/` 路径作为现有业务 CLI 的稳定 ABI 保留，避免为了“看起来像 src/”而破坏 100 多个正式命令和恢复合同。

这是一套**客户无关的 AI 营销数字员工通用包**。它可以服务任意品牌/客户：先把客户一句话需求，通过**引导式顾问对话**共创成高质量剧本和逐段分镜；剧本确认后，用 **gpt-image-2** 生成**人物六视图人物板 + 电影级 16:9、格数由剧本分镜数量决定的故事板**让客户看图确认；最后再生成**真人数字人短视频 / 动态文字视频 / 产品展示视频**。

### 多 Agent / 多 LLM 运行合同

Kilo、Codex、Hermes、WorkBuddy 和未知兼容 Agent 都只是薄入口。业务状态、审批、素材指纹、付费任务和恢复由同一 Python 内核负责；宿主 LLM 只提交受限创意 Patch。恢复已有 run 时使用：

```bash
python3 scripts/workflow_api.py next-v2 --manifest <manifest.json>
```

支持 MCP 的薄入口也可直接以 stdio 启动：

```bash
python3 scripts/workflow_mcp_server.py
```

该入口只负责能力协商、参数校验、路径隔离、幂等和协议回包；阶段判断、审批和
生成依赖仍全部委托给同一个 `workflow_api.py` / `run_manifest.py` 业务层。

返回的 Experience Packet V2 只展示 4 个客户里程碑、1 个主操作、进度、当前审批物和变更阻断原因；19 个技术阶段仍保留在 manifest 中作为恢复与审计合同。客户确认前必须先执行 `review-present`，将实际展示的文件字节、上游审批和 run 身份绑定到回执；再使用该 `presentation_id` 执行 `confirm-v2`。未知模型默认一轮只处理一个 Patch；结构化能力和批量上限来自实测画像，不按模型名字猜测。详细合同见 `references/llm-agnostic-runtime.md`。

三轮外部最佳实践对照、发现的问题和逐项修复证据见
`references/optimization-cycles.md`。正式发布前先生成内容绑定的验证报告，再打包：

```bash
python3 scripts/release_audit.py run --out reports/RELEASE_VALIDATION.json
python3 scripts/package_delivery.py --out-dir <新目录> \
  --validation-report reports/RELEASE_VALIDATION.json
python3 scripts/package_delivery.py --verify-archive <新目录>.zip
```

脚本与分镜的正式新建路径是 `source_facts.json → creative_brief.json → script_beats.json → shot patches → storyboard_plan_compiler.py`。最终计划由代码分配 ID、格位、比例、素材引用和所有权；LLM 不能直接写审批或 manifest。

讲解型视频额外走一条双分支剪辑：HyperFrames/Remotion 先把文档或内容页做成动画视频，BasicRouter 生成数字人讲解视频，再由 `scripts/video_use_bridge.py` 和 `scripts/fuse.py split/overlay` 按导演设计融合成一条成片。

> **故事板转视频说明：** 客户确认 `shot_*.jpg` 后，在最终 `segments.json` 中写入 `"storyboard_ref": true`、格号与已确认参考图绑定，再运行 `video_engine.py --batch <segments> --preflight --preflight-out <video_preflight.json>`，并把同一文件交给 `capture-video --preflight-json`。Seedance 原生路径提交已确认的 contact sheet 与当前镜头素材；preflight 实际选中 Kling/非原生路径时，必须先用 `panel_expansion.py` 生成单格展开图，完成 QA、展示和客户原话确认，然后对最终 segments 重新 preflight/capture/confirm。系统不会在未展示展开图时自动提交 Kling 付费任务；design-board/身份底片也不会误入单格展开。所有 shot 仍逐镜头生成，并保持角色、产品、场景、光线、声音人设、BGM 与故事顺序一致。

> **专业分镜补强说明：** 本包保留自己的 0 基础顾问式引导，不会改成复杂填表；但在脚本定稿后，会参考 `references/professional-storyboard-enrichment.md` 补齐电影级运镜、构图、角色/场景/道具资产提示词、微表情和 BGM/SFX 连续性，再生成 16:9 动态格数故事板。

> 包内自带 `assets/<client>/`、`brand/<client>/`、`actors/<client>/` 只是演示样例，不是业务绑定。正式客户使用时，当前 Agent 会使用 `--client <客户英文代号>` 在 `assets/<client>/`、`brand/<client>/`、`actors/<client>/` 下生成独立资料。

---

## 一、开始使用

**你不需要先在终端跑安装脚本。** 直接：

1. 解压本包。
2. 用你选择的任意兼容 Agent 打开解压后的文件夹。公共流程只认 `AGENT_ENTRY_PROTOCOL.md`、项目级 `AGENTS.md`、`prompts/video-auto-maker.md` 和 `/video-auto-maker`。Tencent WorkBuddy 会从 `.workbuddy/skills/video-auto-maker/SKILL.md` 发现同一入口，宿主入口说明统一位于 `adapters/`。
3. 在对话框输入 **`/video-auto-maker`**；若宿主不支持 slash command，则让它执行同名入口协议。这是唯一要记的总入口。

它会自动完成首次准备：
- 检测 Python、Node/npm、项目依赖，以及用户单独安装的 ffmpeg/ffprobe；缺少 Python/npm 包时按锁文件安装。源码版首次安装通常需要联网。
- 本地私有发行版若另带经过授权和审计的运行时，可使用自己的安装流程；这些二进制不属于社区源码许可证，也不会进入默认发布包。
- 弹出一个统一窗口，一次填写文本 API、图片 API、视频 API 三个不同的中转基础地址；配置保存在当前用户配置目录，不需要编辑项目文件。
- 引导你通过宿主的**安全凭证输入**录入一次 API Key（`sk-` 开头）；宿主没有对应组件时，会由 Agent 拉起本机系统密码弹窗完成同一步骤，不需要把 Key 发到普通聊天，也不需要编辑配置文件。真实 Key 不随交付包提供、不写入项目目录；在线校验通过后仅保存到当前机器的会话级缓存，全程只填这一次。两种安全通道都不可用时才保持阻断。
- 询问或识别客户/品牌代号，例如 `acme`、`hotelhk`、`<client>`，之后所有资料都按该 client 隔离保存。
- 用一张菜单帮你选想做的视频类型，然后一步步带你共创脚本、分镜、故事板和成片。

不知道敲什么时，永远先敲 `/video-auto-maker`，它会带你往下。

---

## 二、标准视频生成流程

```text
/video-auto-maker
  → 三类 API 中转地址 / 环境 / API Key 检查
  → 确定 client 代号
  → 导入产品/品牌/人物素材
  → 读取 assets/<client>/brief.json，脚本接地
  → 顾问式共创剧本
  → 每段生成完整分镜：角度、景别、构图、动作、镜头运动、锚定图
  → 确认剧本 + 逐段分镜
  → 确认出场人物/数字人/音色
  → gpt-image-2 生成人物六视图人物板 + 故事板
  → 客户确认故事板
  → 确认渲染方案与同源 Design Token v3 风格帧
  → visual_intent + layout_contract + audio choreography
  → 编译不可变 formal render_quality_spec（未解析项直接阻断）
  → video_engine.py 生成底片
  → 每段 formal take review + accept-take/retry
  → OCR clear 或精确 waiver
  → assemble 并审批 video
  → postproduction_motion_director 读取 storyboard_plan + motion_design + brand tokens
  → Shotcraft 静态审美 QC → Remotion 后期动效 → 抽帧 QC
  → 若是讲解型：HyperFrames/Remotion 文档动画 + 数字人视频 → video_use_bridge.py / fuse.py 融合
  → derive-captions + 客户确认 caption artifact
  → HyperFrames/subtitle overlay 生成 captioned basecut
  → final_edit.py 单次交付编码 + formal media QC + 客户审批 final
  → pipeline.py delivery create/verify
```

关键原则：

- **脚本没定稿，不出片。**
- **故事板没确认，不出片。**
- **每段分镜必须有剪辑跨度和专业画面设计**：相邻镜头要有 30°–50° 机位偏移，或远景/中景/近景/特写变化，避免连续正面半身口播；每段 `storyboard_plan.json` 尽量补齐景别、运镜、构图、光线、微表情、角色动作、场景/道具资产提示词和音频连续性。
- **素材图 + 文字台词 → 图生视频** 是默认路径，避免纯文生视频胡编产品。
- 正式流程缺 reviews、accepted take、OCR 证据、caption artifact 或 formal QC 时不会交付。
- `--draft` 仅用于内部预览，不是正式客户成片。
- 正式 Remotion 渲染统一消费不可变 `render_quality_spec.json`；详细设计与命令见 [渲染前质量编译器](docs/architecture/RENDER_QUALITY_COMPILER.md)。

### Shotcraft 后期动效导演

确认后的后期动效先编译结构化视觉意图，再由故事板、动效设计和品牌 token 共同决定，不再只按故事板关键词选卡：

```bash
python3 scripts/visual_intent.py --plan output/storyboard_plan.json \
  --strict --out output/visual_intent.json
python3 scripts/postproduction_motion_director.py \
  --storyboard-plan output/storyboard_plan.json \
  --motion-design output/motion_design.json \
  --brand-tokens output/style_tokens.json \
  --out output/shotcraft_plan.json
python3 scripts/shotcraft_compile.py --plan output/shotcraft_plan.json \
  --style-tokens output/style_tokens.json --out output/shotcraft_spec.json
python3 scripts/shotcraft_qc.py --spec output/shotcraft_spec.json
```

每个镜头会显式得到 `motion_intent` 和 `shotcraft_card_id`。注册表提供 30 张高频卡，并为每张卡声明独立 renderer、主效、运动速度、hold、motion blur 与 transition 合同。渲染前 QC 会阻断多主效、hold 不足、转场未卡拍、品牌 token 漂移、快速运动缺 blur、字幕安全区缺视觉分析依据及后期层夹带文字；渲染后会按 `qa_frames` 抽帧，记录 SHA-256、亮度和空白/冻结检查结果。

Remotion 核心及 `@remotion/transitions`、`@remotion/motion-blur`、`@remotion/lottie`、`@remotion/rive`、`@remotion/three` 使用同一 `4.0.509` 版本。文字、字幕和参数标签仍由 HyperFrames 负责，不进入视频模型。

---

## 三、你会用到的几个说法

| 你想做的事 | 对当前 Agent 说 |
|---|---|
| 开始 / 不知道敲什么 | `/video-auto-maker` |
| 首次初始化 / 配置 API 地址与密钥 | `/setup` |
| 导入产品图/PPT/PDF/Word | `导入产品资料` |
| 设置品牌 Logo 和风格 | `配置品牌` |
| 生成数字人形象 | `做一个数字人形象` |
| 普通口播（粤语/普通话） | `做一条口播` |
| 访谈形式 | `做一条访谈` |
| 实景 + 服务介绍 | `做一条服务介绍` |
| 动态文字/字幕动效 | `做一条动态文字视频` |
| 整合营销方案（含排期预算） | `做一份营销方案` |
| 社交快剪（5-10秒） | `做一条社交快剪` |
| 产品实拍演示（30秒-1分钟） | `做一条产品演示` |
| 品牌广告片 TVC（15-20秒） | `做一条品牌TVC` |

## 四、模块售卖边界

本项目按三个主模块售卖，所有模块共用环境自检、素材、品牌、数字人、故事板、视频生成、QC 和交付底座。机器可读定义在 `packages/`。

| 售卖包 | 主入口 | 包含 | 不包含 |
|---|---|---|---|
| 口播包 | `/oral-broadcast` | 数字人单人对镜口播、普通话/粤语脚本、口播字幕/动效增强 | 复杂 PPT 讲解、品牌 TVC 大片 |
| 讲解包 | `/explainer-video` | PPT/PDF/Word/课程/方案讲解、内容页动效、数字人讲师双分支融合 | 单纯带货口播、品牌形象大片 |
| TVC 包 | `/brand-tvc` | 15-20s 品牌广告片、创意方向、电影级多镜头分镜、品牌收尾 | 长文档讲解、批量口播 |

查看或校验模块定义：

```bash
python3 scripts/module_packages.py list
python3 scripts/module_packages.py validate
```

运行宿主/模型无关合同和 36 个业务评测夹具校验：

```bash
python3 scripts/workflow_router.py validate
python3 scripts/skill_contract.py validate
python3 scripts/llm_workflow_eval.py validate-fixtures
python3 -m pytest -q
```

---

## 五、可选：独立终端部署

如果你更习惯先在终端一次性装好环境，也可以在包目录跑：

macOS / Linux：

```bash
./deploy.sh
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

但这一步不是必须的；支持 inline 自举的 Agent 可直接运行 `/video-auto-maker` 自动准备环境。

---

## 六、常见问题

- **这个包是否只适用于 客户品牌？** 不是。客户品牌 只是内置演示客户。正式客户会使用自己的 `--client <客户代号>`、品牌包、演员库和素材 brief。
- **成片在哪？** 生成的视频都在 `output/` 文件夹，当前 Agent 也会把可播放的绝对路径发给你。
- **装依赖失败？** 源码版要求 Python 3.9+、Node/npm，以及用户单独安装的 ffmpeg/ffprobe。项目不会捆绑或自动下载 FFmpeg；可放入 PATH，或用 `BASICROUTER_FFMPEG_DIR` 指向同一目录下的两个可执行文件。Python/npm 依赖按锁文件安装。
- **没有某个特定 Agent / 没有 Node？** 本项目不要求指定 Agent；任选兼容宿主即可。deploy 脚本会处理业务运行依赖，但不会擅自安装或切换宿主 Agent。
- **视频生成失败？** 可能是 API Key 无效、余额不足、限流或网络超时。当前 Agent 应用客户能理解的语言说明原因和下一步。
- **想看流程画布？** 可用 `python3 scripts/workflow_canvas.py generate ...` 一次性导出；或用 `python3 scripts/workflow_canvas.py serve ...` 起一个会自动刷新、能记录评论和历史轨迹的本地工作台。它会自动从当前 workspace 里反推 `client / run-id`，把 `manifest / brief / storyboard_result / segments / run.log` 汇成一页画布，显示当前步骤、素材引用、修改回路和事件流。

生成的成片、口型、Logo 稳定性以实际结果为准；建议人工过目一遍再发布。

---

## 七、许可与商业使用

本项目采用双重许可模式：

- 社区使用：根目录 `LICENSE`（PolyForm Noncommercial 1.0.0），允许符合条款的非商业学习、研究、测试、修改与分发。
- 商业使用：企业内部生产、收费客户交付、SaaS、托管服务、转售或商业产品集成，须事先取得单独书面商业许可，联系 `359971043@qq.com`。
- 项目名称与标识：不随代码授权；修改版必须使用清晰可区分的新名称，见 `TRADEMARKS.md`。
- 外部贡献：合并前须接受 `CLA.md`，以便项目继续提供社区许可和商业许可。

这是“源码可见/社区许可”项目，不属于 OSI 定义的开源软件，因为社区许可证限制商业用途。第三方组件继续适用各自许可证，详见 `THIRD_PARTY_NOTICES.md`。

发布源码包前运行：

```bash
python3 scripts/license_compliance.py check
```

默认发布包排除 `offline-assets/`、`node_modules/`、FFmpeg、Chromium、Python wheels 和其他未经单独审计的第三方二进制。
