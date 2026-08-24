# 通用客户接入说明

本包不是单一品牌专用。每个客户用一个 `client` 代号隔离素材、品牌和数字人。

## 目录约定

```text
assets/<client>/brief.json        # 产品/服务 brief、render_profile、render_plan
assets/<client>/images/           # 产品图、场景图、故事板锚定图
brand/<client>/brand.json         # Logo、主色、字体、视觉风格
actors/<client>/<actor>/          # 数字人形象库
output/                           # 生成结果
```

## 新客户启动

1. 让客户说品牌名，转成英文小写代号，例如 `acme`。
2. 导入产品/服务资料：`/asset-prep`。
3. 配置品牌 Logo/主色：`/brand-kit`。
4. 创建或选择数字人：`/digital-human`。
5. 视频需求统一从 `/video-auto-maker` 进入。

`/video-auto-maker` 首次运行会检查全部本地组件：Python 核心依赖、ffmpeg、Node/HyperFrames、Remotion、Chrome Headless Shell 和平台可用的 OCR；任一缺失都会自动执行 inline 部署并复验。

交付包中的 Node 依赖仅适用于打包机器的 macOS ARM 架构；Windows、Linux 或 Intel Mac 会自动忽略这份依赖并执行 `npm ci` 安装对应平台版本。Chrome Headless Shell 不跨平台复用，而是首次部署时按操作系统和 CPU 架构下载匹配版本。

## 出片前的硬性闸门

- 剧本 + 逐段完整分镜确认。
- 出场人物/数字人/音色确认。
- `gpt-image-2` 生成的人物板 + 故事板确认。
- 相邻分镜必须有 30°–50° 机位偏移，或远/中/近/特写景别跨度。
- 故事板确认后才允许调用 `video_engine.py` 生成视频。

## Demo 说明

包内 `assets/momax/`、`brand/momax/`、`actors/momax/` 仅是演示客户资料，用来做参考或跑样例。正式客户不要默认使用 demo 数据。
