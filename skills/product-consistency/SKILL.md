---
name: product-consistency
description: 建立并确认营销视频中的产品、人物和人物产品使用关系锚点。用于存在真实 SKU、包装、手持、佩戴、磁吸、卡扣、插接、承重或其他人物与产品接触关系的产品板、人物板、使用图、故事板和视频阶段。
---

# 人物与产品一致性

## 先读取当前合同

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill product-consistency
```

只使用当前 stage contract 返回的 `entry_functions` 和 `confirm_function`。正式流程统一由 `scripts/storyboard.py` 管理产品板、人物板和产品使用图；不要改走旧的独立产品板确认链路。

## 分阶段顺序

```text
确认客户真实产品图
→ 产品九宫格板
→ 客户确认产品板
→ 人物六视图板（有人物时）
→ 客户确认人物板
→ 人物产品使用图（人物真实使用产品时）
→ 客户确认使用图
→ 分镜故事板
→ 客户确认故事板
→ 视频
```

每次只执行：

```bash
python3 scripts/storyboard.py --plan <storyboard_plan.json> \
  --out-dir output/storyboard --run-id <run-id> \
  --model gpt-image-2 --stage next --json
```

展示脚本返回的绝对路径后，使用 router contract 中当前阶段的确认命令。源产品图指纹变化时，旧产品板、使用图、故事板及其确认全部失效。

## 产品使用图

当人物与产品存在真实接触时必须进入 `product_usage`：

- 同时引用已确认人物板和产品板。
- 高风险物理关系按 panel 独立生成，再由本地工具拼成确认板；不得让图像模型一次生成整张 3×3。
- 保存每格 `panel_index`、本地路径、BasicRouter retrieve URL 和 SHA-256。
- 对关键接触格验证目标接触面、贴合线、手部接触点、产品外露面和操作方向。
- 客户指出偏差时执行 `--refine-board usage`；不得由 Agent 自评后直接确认。
- `usage_geometry_review.json` 必须绑定当前 board、源指纹、关键 panel 哈希和客户确认原话；`--geometry-reviewed` 单独使用无效。

## 视频参考

- 人物：已确认人物板的正脸特写和全身图作为身份锚。
- 产品：已确认源产品图、产品板和必要细节图作为外观锚。
- 使用关系：已确认使用图作为动作和几何合同。
- 场景：已确认场景图作为环境锚。
- 在每个 shot 的 `ref_tags[]` 与提交文本中明确引用角色；不要写模糊的“参考图片”。
- 引用在 `script_splitter` 正式 handoff 时冻结；handoff 后不得直接修改 `segments.json` 或用 CLI 临时注入引用。
- 连续段先在已确认 segments 中声明 `chain_required/chain_contract` 并校验尾帧加入后不超参考图上限，再使用 `--chain`；不要依赖 seed 锁定人物或产品一致性。
