---
name: ugc-unboxing
description: 把生活化开箱体验配方应用到本项目的脚本、产品板、人物板、产品使用图和故事板。用于新品开箱、拆封、配件展示、第一次上手或手部产品特写时；本 Skill 不替代正式分阶段生成、几何复核和客户确认。
---

# UGC 开箱配方

```bash
python3 scripts/workflow_router.py contract --manifest <manifest.json>
python3 scripts/skill_contract.py contract --skill ugc-unboxing
```

按 router 返回的 `product_with_human` 链路执行，不另建一套 UGC 状态机。

## 脚本结构

```text
自然钩子 → 拆封与配件 → 第一印象 → 一个核心功能首次使用 → 克制收尾
```

- 使用第一人称分享语气，允许自然语气词。
- 至少包含一个超出预期的细节和一个小遗憾。
- 总时长按镜头动作拆为多个符合实时模型时长能力的 segment；不要把 30、45 或 60 秒总片长提交成单段。
- 价格、促销和 CTA 动效留给确定性后期。

## 产品和使用关系

- 产品板覆盖包装、盒内结构、配件、产品正反面、材质、功能区和组合全景；具体格数服从当前 storyboard plan，而不是为配方强制固定旧模板。
- 人物存在时先确认人物板，再生成 `product_usage`。
- 使用图必须展示拆封、取出、持握比例和至少一个真实操作；磁吸、卡扣、插接、佩戴、承重等关系按独立 panel 生成并完成几何复核。
- 客户指出手部或物理关系错误时执行 usage refine，重新展示并确认。

## 故事板和视频

- 手部和产品细节镜头约占三分之一，同时保留环境建立镜头和首次使用结果镜头。
- 使用 shot 的 `ref_tags[]` 同时绑定产品、人物和必要场景；视频类型由 `script_splitter` 和实时 `allowVideoType` 决定。
- 不固定 type 5：单首帧、首尾帧、单身份或多参考应按该 shot 的实际素材选择。
- 保持生活化光线和自然桌面，但不凭空改变包装、配件数量、产品比例或功能状态。
- 检查产品身份、手部接触、包装原生文字、意外字幕、产品可见性和 take review。
