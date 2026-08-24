# BasicRouter 稳定字段参考

本文件只记录跨版本相对稳定的协议。请求路径、兼容模式、响应差异、重试和模型别名以 `scripts/br_client.py` 与 `scripts/video_models.py` 为准。

## 认证与错误

- 使用 Bearer 密钥；正式 Agent 会话由 `scripts/key_setup.py` 管理。
- 公共模型目录不能证明密钥有效；密钥准入使用客户端的低成本认证检查。
- HTTP 错误与 `{code,message,data}` 业务错误分别处理。
- 余额不足、无权限、参数错误和内容策略错误不自动重放付费请求。

## 模型发现

- 文本、图像和视频模型必须从实时目录读取。
- 视频能力读取每个模型公布的 `allowVideoType`；目录不可用时才使用 `scripts/video_models.py` 的离线能力表。
- 提交名称、目录 ID 和别名可能不同，使用客户端的目录归一化与别名解析。
- 不把某次查询得到的在线模型列表复制进 Skill。

## 图像生成

- 提交：`POST /v1/image-generations`
- 主要字段：`model`、`text`、`count`、`resolution`、`ratio`、`imageUrls`
- 返回 taskId 后，通过对应 retrieve 端点轮询。
- 参考图使用 `imageUrls`；本地素材由客户端转换为受支持的引用格式。
- 成功响应中的图片字段由客户端统一解析为 URL 列表。

正式故事板、人物板、产品板和产品使用图固定经 `scripts/storyboard.py` 调用 `gpt-image-2`，不要从通用图像 fallback 推断该阶段模型。

## 视频生成

- 提交：`POST /v1/video-generations`
- 主要字段：`model`、`text`、`videoType`、`imageUrls`、`resolution`、`ratio`、`duration`
- `videoType`：1 文生；2 首帧；3 首尾帧；4 单参考身份；5 多参考。
- 非文生模式需要参考图片；精确数量和类型限制读取实时模型能力。
- 返回 taskId 后轮询对应 retrieve 端点；成功结果可能包含 `videoUrl` 与 `lastFrameUrl`。

正式调用经 `scripts/video_engine.py`，确保 manifest、预算、prompt review、任务恢复、OCR 和结果文件保持一致。

## 多模态聊天

使用 `br_client` 的视觉分析函数构造输入并调用 `_extract_chat_text` 归一化输出。不要在 Skill 中复制某一模型或某一网关版本的 content block/response shape；长请求使用现有流式保活与受控传输错误回退。
