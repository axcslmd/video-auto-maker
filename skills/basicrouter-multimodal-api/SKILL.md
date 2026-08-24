---
name: basicrouter-multimodal-api
description: 使用项目现有 BasicRouter 客户端完成模型发现、文本/视觉请求、异步图像或视频提交、轮询、限流恢复和托管引用。用于维护 scripts/br_client.py、scripts/video_models.py 或生成执行器，诊断网关能力，或在正式工作流代码入口内部接入 BasicRouter 时。
---

# BasicRouter 多模态执行参考

优先复用项目代码，不在 Agent 或 Skill 中重新实现 HTTP、重试、轮询、密钥和模型回退。

## 使用边界

- 正式营销视频流程先读取当前阶段合同：

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill basicrouter-multimodal-api
```

- 图像阶段调用 `storyboard.py` 或 `asset_prep.py`；视频阶段调用 `video_engine.py`。只有维护、测试和诊断底层客户端时才直接调用 `br_client.py`。
- 本地 Agent 负责共创和提示词语言；BasicRouter 只负责需要专业模型的渲染或视觉分析。

## 代码真源

- `scripts/br_client.py`：请求封装、响应归一化、异步图像/视频、托管引用、重试和错误类型。
- `scripts/video_models.py`：实时目录归一化、`allowVideoType` 判断、模型选择与离线兜底。
- `scripts/key_setup.py`：公共会话密钥准入和标准输入保存。
- `scripts/video_engine.py`：正式视频 gate、批量任务、恢复、OCR、候选选择和结果登记。
- [字段级参考](references/api-reference.md)：只记录稳定协议字段；模型在线状态必须实时查询。
- `scripts/probe_models.py`：诊断当前图像/视频模型目录。

## 稳定协议

- 网关基址由 `br_client.py` 决定，不在 Skill 中另写环境分支。
- 图像和视频生成均使用异步 submit → taskId → retrieve/poll。
- `videoType` 语义为 1 文生、2 首帧、3 首尾帧、4 单参考身份、5 多参考；实际支持范围以实时 `allowVideoType` 为准。
- 图像参考使用 `imageUrls`，正式产品板必须绑定源产品图指纹。
- 多模态聊天使用 `br_client` 已实现的输入块和响应文本归一化，不手写不兼容结构。
- 重推理视觉请求优先使用客户端的流式保活路径。

## 安全与恢复

- 每个新会话建立稳定 `BASICROUTER_SESSION_ID`；密钥只通过 `key_setup.py save --stdin` 保存到会话缓存。
- 不把密钥写入命令参数、日志、项目、Skill、manifest 或交付包。
- 429、5xx 和瞬时网络错误由客户端按既有退避策略处理；业务错误、余额不足和无权限不得伪装成成功。
- 已提交任务优先凭 taskId 恢复，不因超时、宿主切换或轮询中断重复付费提交。
- 不在 Skill 中维护“当前在线模型”清单；执行时实时查询，离线时使用代码中的受控兜底。
