# 渲染前质量编译器

本项目把渲染质量从“出片后检测”前移为可执行合同。检测仍保留用于防止编码器、文件和人工操作异常，但不会再承担设计决策。

## 第一阶段：同源设计与母版

`styleframe.py` 生成 Design Token v3。颜色、字体、字号、行高、间距、圆角、表面材质、媒体裁切、动效节奏和母版策略由同一份 JSON 驱动 Styleframe、Content、IntegratedExplainer、Shots 与 Shotcraft。客户看到的风格帧由正式 Remotion 组件渲染，不是另一套静态 mockup。

```bash
python3 scripts/styleframe.py \
  --brand brand/<client>/brand.json \
  --direction premium_editorial \
  --energy medium \
  --out output/<client>/<run-id>/style_tokens.json \
  --preview-out output/<client>/<run-id>/styleframe.png
```

正式直出使用 PNG 帧、BT.709、H.264 CRF 16、slow preset、48 kHz/256 kbps AAC；商业母版使用 10-bit 4:2:2 ProRes HQ。透明中间层使用 ProRes 4444 或 VP9 alpha。最终角标进入同一 React 时间轴，`final_edit.py` 不再执行末端 H.264 二次“标准化”。

## 第二阶段：视觉、布局和音频编译

每个镜头先形成 `visual_intent.json`，明确唯一视觉命题、证明类型、主次主体和注意力顺序。`layout_contract.json` 再为每个目标画幅编译安全区、内容容量和素材变体；人物、产品及使用关系素材禁止未知裁切。`render_quality_spec.json` 最后冻结上述合同、音频节拍编舞、Remotion props、目标分辨率和编码档位。

```bash
python3 scripts/visual_intent.py \
  --plan output/storyboard_plan.json --strict \
  --out output/visual_intent.json

python3 scripts/layout_compiler.py \
  --plan output/storyboard_plan.json \
  --style-tokens output/style_tokens.json \
  --visual-intent output/visual_intent.json \
  --ratio 16:9 --ratio 9:16 \
  --out output/layout_contract.json

python3 scripts/render_spec_compiler.py \
  --plan output/storyboard_plan.json \
  --style-tokens output/style_tokens.json \
  --motion-design output/motion_design.json \
  --audio-contract output/audio_contract.json \
  --engine-props output/final_remotion_props.json \
  --profile direct_delivery --formal \
  --out output/render_quality_spec.json

python3 scripts/remotion_engine.py render-spec \
  --spec output/render_quality_spec.json \
  --out output/final.mp4
```

正式模式对以下情况 fail-closed：

- 文本超过组件容量；禁止靠缩小字号硬塞。
- 受保护人物、产品或使用关系素材没有目标画幅版本或已知源比例。
- 要求实测音频时只存在脚本估算节拍。
- 缺少最终引擎 props，或 props 画幅与目标合同不一致。
- Design Token、RenderSpec 或任何内嵌合同在审批后被修改。

`postproduction_motion_director.py` 会优先消费结构化 `visual_intent` 来选择 Shotcraft card。关键词推断只保留给旧计划兼容，不能覆盖结构化意图。

## 接入正式链路

- `video_use_bridge.py --formal`：在绑定已确认的预演规格和真实人物底片后编译 RenderSpec，再进入 IntegratedExplainer。
- `shotcraft_packaging.py`：为正式包装自动生成并登记 RenderSpec。
- `final_edit.py run`：正式成片要求 Design Token v3，自动生成 RenderSpec，并以单次交付编码渲染。

验证命令：

```bash
python3 -m pytest -q tests/test_render_quality_compiler.py
npm --prefix remotion_engine run typecheck
python3 scripts/architecture_audit.py validate
python3 scripts/release_audit.py run --out reports/RELEASE_VALIDATION.json
```
