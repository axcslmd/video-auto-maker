# 客户交付包说明

本包是客户无关的 AI 营销视频工作室运行包，不包含当前测试项目的 PPT、人物底片、故事板、视频和其他生成素材。客户使用自己的素材目录：

```text
assets/<client>/
brand/<client>/
actors/<client>/
```

## 安装

默认社区交付物是**跨平台源码包**，不包含 Python wheels、`node_modules`、HyperFrames 缓存、FFmpeg、Chromium 或 Chrome Headless Shell 等第三方二进制。目标机器需准备 Python 3.9+、Node/npm，并单独安装 ffmpeg/ffprobe。

macOS Apple Silicon：

```bash
./deploy.sh
```

部署脚本会按依赖清单安装或检查：

- Python 依赖：`requirements.lock.txt`
- Node / npm 与 Remotion 工程依赖：`remotion_engine/package-lock.json`
- 用户单独安装的 ffmpeg/ffprobe：从 PATH 或 `BASICROUTER_FFMPEG_DIR` 读取

如需制作真正的离线二进制包，须对准确版本的 wheels、npm 包、浏览器和 FFmpeg 构建参数另行审计，收集许可证全文、署名、源码提供义务并生成 SBOM；默认社区构建会拒绝 `--include-local-runtime`。

安装完成后，从兼容 Agent 的 `/video-auto-maker` 入口开始。API Key 不随包提供，也不会写入项目文件；由客户在自己的会话中配置。

## 交付包内容

保留内容包括：业务脚本、项目自有 Remotion 源码、Agent 入口协议、工作流契约、schemas、skills、运行所需 references、依赖锁文件、社区许可证、Required Notice、商标政策、CLA 和第三方声明。

已排除内容包括：`output/` 渲染结果、PPT/图片/视频素材、客户数据、内部调研/问题报告、可重建缓存、Python 虚拟环境、Agent 会话记忆、本机配置、`offline-assets/`、`node_modules/` 及第三方二进制。

正式交付包保留 `tests/` 与 `evals/`。打包前由 `license_compliance.py` 和 `release_audit.py` 运行闸门并把结果绑定到源码指纹；打包后同时验证目录形态、文件清单和 ZIP 内部成员。Python/Node 依赖仍以锁文件为准。

具体文件及 SHA-256 指纹见 `DELIVERY_MANIFEST.json`。
