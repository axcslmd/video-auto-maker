# 第三方软件与许可声明

本项目自身代码适用根目录 `LICENSE`。下列第三方组件不因此改变许可证，使用者和分发者须分别遵守其原始条款。本清单是当前直接依赖与已知特殊许可项的合规摘要，不替代上游许可证全文。

| 组件 | 使用方式 | 许可证/注意事项 |
|---|---|---|
| video-shotcraft skill | 仓库内 `skills/video-shotcraft/` | Apache License 2.0；保留该目录内 LICENSE 和原作者 Wei Yihao 的声明 |
| pypdf | 外部 Python 依赖 | BSD 3-Clause；用于 PDF 读取，替代原 PyMuPDF 依赖 |
| python-pptx、python-docx、openpyxl、Pillow、requests 等 | 外部 Python 依赖 | 以各包发布版本中的许可证和声明为准；打包或再分发 wheel 前须生成完整 SBOM/NOTICE |
| Remotion 及 `@remotion/*` | 外部 npm 依赖 | Remotion License；其免费/付费条件按组织规模和使用方式适用，不由本项目社区许可证覆盖 |
| React、Three.js、Lottie 等 npm 包 | 外部 npm 依赖 | 主要为 MIT 等宽松许可证，以 `package-lock.json` 对应版本为准 |
| Mediabunny | Remotion 的传递依赖 | Mozilla Public License 2.0；若分发其源码或修改文件，须履行 MPL 的文件级义务 |
| caniuse-lite 数据 | npm 传递依赖 | CC BY 4.0；保留署名和许可证信息 |
| `@remotion/transitions` | 外部 npm 依赖 | 锁文件元数据可能显示 `UNLICENSED`；不得将其解释为可自由再分发，须依据 Remotion 官方许可使用 |
| FFmpeg / ffprobe | 用户单独安装的外部工具 | FFmpeg 根据实际构建选项适用 LGPL 2.1+ 或 GPL 2+；本社区源码包不捆绑 FFmpeg 二进制 |
| Chromium / Chrome Headless Shell | 用户或工具链单独安装 | 由 Chromium 及其第三方组件各自许可证约束；本社区源码包不捆绑浏览器二进制 |
| HyperFrames / GSAP | 外部工具链 | 仅按各自发布方条款安装和使用；如分发其 CLI、运行时或缓存，必须先单独完成许可证审计 |

## 分发规则

- 默认社区发布物只包含项目源码与依赖清单，不包含 `node_modules/`、Python wheels、`offline-assets/`、FFmpeg、Chromium 或本地缓存。
- 任何包含第三方二进制的“离线完整包”都不是默认社区发布物；发布前必须针对准确版本和构建参数完成单独审计、收集许可证全文/源码提供义务并生成 SBOM。
- 发现遗漏或许可信息不一致，请联系 359971043@qq.com，在确认前暂停分发相关制品。
