# /setup — 首次初始化（0 基础客户第一步）

你是安装向导。客户机器可能什么依赖都没装。这个流程把环境从零带到「可出片」；客户在普通对话里做选择，在宿主安全凭证框或 Agent 拉起的本机系统密码弹窗中录入 key，不碰命令行细节。**按顺序执行，每步给客户看得懂的反馈。**

> 说明：客户**无需先在终端跑 deploy**——直接在当前兼容 Agent 里运行 `/video-auto-maker`（总入口，会自动自举环境）或 `/setup`（本命令）即可；不支持 slash command 的宿主使用同名工作流。终端跑 `./deploy.sh` 只是可选的独立部署方式。本 `/setup` 在当前 Agent 内完成完整自举（依赖 + Node/引擎）+ 配 Key。

## 第一步 · 检测并安装运行依赖 + 引擎

先检测：
```
python3 scripts/setup_env.py full-check
```
- 若显示「全部就绪 READY」→ 跳到第二步。
- 若缺依赖 → 告诉客户「检测到缺少组件，我来自动安装（无需管理员权限，约一两分钟）」，然后跑**一键自举**（inline 模式：装 pip 依赖 + Node + Remotion/HyperFrames 引擎，不建 venv、不安装或切换宿主 Agent）：
  macOS/Linux 执行 `AGENT_INLINE_BOOTSTRAP=1 bash deploy.sh`；Windows PowerShell 执行
  `$env:AGENT_INLINE_BOOTSTRAP=1; .\deploy.ps1`。跑完 `python3 scripts/setup_env.py full-check` 复验。
  - 只想快速补齐 **pip 核心依赖**（不装 Node/引擎）时，也可用轻量方式：`python3 scripts/setup_env.py install`。
- 若仍缺**核心依赖**（pptx/docx/fitz/openpyxl/ffmpeg，网络受限等）→ 如实告诉客户失败原因，建议检查网络或手动 `pip3 install -r requirements.txt`，**不要假装成功**。
- 只缺 **Node/Remotion/HyperFrames**（非核心）→ 可继续，出片正常，动态文字暂走 libass 兜底，联网后自动补齐。

完成标准：核心依赖就绪（`setup_env.py check` 核心项全 OK）。

## 第二步 · 配置三类 API 中转地址（首次初始化弹窗）

执行：
```
python3 scripts/api_endpoint_setup.py init
```
缺少配置时会弹出一个统一窗口，要求客户一次填写三个**不同的中转基础地址**：文本 API、图片 API、视频 API。三个字段都必填，填写基础地址即可，不要包含 `/v1/chat/completions`、`/v1/image-generations`、`/v1/video-generations` 等具体接口路径。配置保存在用户配置目录，不写入项目文件；以后初始化会自动复用。取消或地址格式错误时保持阻断，不进入密钥校验或创作流程。

## 第三步 · 配置 API Key（首次对话输入）

```
python3 scripts/key_setup.py init --host-session-id <宿主稳定会话ID>
python3 scripts/key_setup.py gate --session-id <返回的公共会话ID>
```
- `STORED` → 已配置，跳过。
- `MISSING` / `BLOCKED` → 优先请客户使用宿主安全凭证输入录入 BasicRouter 密钥；严禁要求客户把 key 发到普通聊天。Agent 从不回显的标准输入读取后执行：
  ```
  python3 scripts/key_setup.py save --stdin --session-id <公共会话ID>
  ```
  宿主没有安全凭证组件时，不要让客户找不存在的输入框，直接由 Agent 执行 `python3 scripts/key_setup.py save --secure-prompt --session-id <公共会话ID>` 拉起本机系统密码弹窗。校验通过会显示 `SAVED (VALIDATED)`；失败时如实告知并通过同一安全通道重试。两种安全通道都不可用时保持阻断，不开始引导或外部调用。`QUARANTINED` 时还必须先在 BasicRouter 侧撤销/轮换旧 key，并通过安全通道提交不同指纹的新 key。

## 第四步 · 就绪确认 + 引导下一步

上述步骤都通过后，告诉客户：
「初始化完成 ✅ 现在可以开始创作了。你可以：
 · 先上传产品图/PPT/PDF 让我了解产品 → 说『导入产品资料』（走 /asset-prep）
 · 设置品牌 Logo 和风格 → 说『配置品牌』（走 /brand-kit）
 · 直接做视频 → 说『做条口播/访谈/服务介绍』」

建议客户首次先走 asset-prep + brand-kit，后续所有脚本都会更贴合他的产品和品牌。

## 常见问题（照实处理，别编）

- pip 装不上：多为网络问题。建议客户确认能访问 PyPI，或换镜像源
  `pip3 install -i https://pypi.org/simple -r requirements.txt`。
- Python 版本过低（<3.7）：提示升级 Python。
- key 校验失败：区分「key 错」和「余额不足」，分别引导。

## 完成标准

- setup_env 报 READY；api_endpoint_setup 报 API_ENDPOINTS_READY；key_setup 报 STORED；已告知客户下一步入口。
- 任何一步失败都如实反馈原因，不跳过、不伪装。
