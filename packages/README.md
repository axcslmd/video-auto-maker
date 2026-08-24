# 模块售卖包边界

本目录定义可独立售卖的三个功能包。`shared_core` 是所有包共用的运行底座，不作为第四个售卖包；客户购买任一包时都随包交付。

## 三个售卖包

| 包 | 入口命令 | 适合客户 | 核心交付 |
|---|---|---|---|
| 口播包 | `/oral-broadcast` | 要真人数字人对镜介绍、带货、种草、服务说明的客户 | 数字人口播成片、脚本、人物板、故事板、字幕/动效增强 |
| 讲解包 | `/explainer-video` | 有 PPT/PDF/Word/课程/方案，需要把复杂内容讲清楚的客户 | 数字人讲师 + 内容页/文档动画融合成片、讲解脚本、内容分支、融合计划 |
| TVC 包 | `/brand-tvc` | 要品牌形象片、新品发布、节点营销大片的客户 | 15-20s 品牌广告片、创意脚本、电影级故事板、品牌收尾 |

## 共享底座

任一售卖包都包含这些基础能力：环境自检、BasicRouter 密钥管理、客户/品牌隔离、素材导入、品牌包、数字人形象库、故事板确认、视频生成、OCR/QC、字幕/动效、本地合成和交付清单。

共享入口包括：`/video-auto-maker`、`/setup`、`/asset-prep`、`/brand-kit`、`/digital-human`、`/text-anim`。

## 边界原则

1. 一个客户订单只能绑定一个主包，跨包需求必须明确升级或追加模块。
2. 共享底座可以被任一包使用，但不能把其他包的主交付混入当前包。
3. 口播包不承诺复杂 PPT 重导演；讲解包不承诺品牌大片创意；TVC 包不承诺长课件讲解。
4. 如果客户素材触发其他包能力，Agent 必须先说明边界，再建议升级或拆单。

## 机器可读定义

每个包都有 `package.json`，可用下面命令检查：

```bash
python3 scripts/module_packages.py list
python3 scripts/module_packages.py show oral-broadcast
python3 scripts/module_packages.py validate
```
