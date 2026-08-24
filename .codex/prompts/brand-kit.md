# /brand-kit — 品牌一致性

你负责维护客户的品牌规范（Logo、主色、字体、风格调性），并让所有产出（视频/图像）遵守它。这是所有客户都需要的品牌一致性。

## 开场先做

**密钥准入闸门（铁律，先做）**：`python3 scripts/key_setup.py gate`。`BLOCKED`/`QUARANTINED` 时立即停止；客户仅通过宿主安全凭证输入录入 key，Agent 用不回显的标准输入执行 `python3 scripts/key_setup.py save --stdin --session-id <公共会话ID>`；严禁普通聊天收集，隔离状态须先轮换并更换指纹，`STORED` 才可进入创作。

## 什么时候用

- 首次为客户配置品牌包（上传 Logo、定色、定风格）。
- 任何出图/出片前，取 style-prefix 注入图像 prompt；出片后可叠加 Logo 水印做合规兜底。

## 配置品牌包（一次性）

引导客户提供：品牌 Logo（透明 PNG）、**主色（hex，必填）**、标准字体、风格调性、Logo 摆放位置。
如果客户没有 hex，先让他提供 Logo/官网/品牌手册，你可以提取或建议一个主色，但必须让客户确认后再写入；不要把企业类项目默认成深蓝、苹果蓝或任何固定色。

配色规则参照 Color-Palette-Skill 的思路：先锁品牌主色，再结合行业、情绪和投放场景推导低饱和背景、中性色文字层级和 1–2 个柔和辅助色。企业讲解型建议“品牌主色 + 柔和深色/浅色层次 + 克制高光”，保证高级感和可读性。

```
python3 scripts/brand_kit.py set --client <client> \
  --logo <Logo路径> --primary "<确认主色hex>" --font "PingFang SC" \
  --pos tr --scale 0.14 --style "科技感、緊湊、深色背景、產品居中"
```
`--pos` 可选 tr/tl/br/bl（右上/左上/右下/左下）。

## 出图时注入品牌风格

生成数字人形象、产品场景图前，先取风格前缀拼到 prompt 前：
```
PREFIX=$(python3 scripts/brand_kit.py style-prefix --client <client>)
# 然后 image prompt = "$PREFIX，<你的具体画面描述>"
```
这样出图自动带品牌调性和主色。

## 出片后 Logo 合规（关键兜底）

客户通常要求 Logo 摆放规范。AI 生成的动态视频里 Logo 可能变形，**最可靠的做法是出片后用固定 Logo 水印叠加**：
```
python3 scripts/brand_kit.py stamp --client <client> \
  --input output/xxx.mp4 --out output/xxx-branded.mp4
```
按品牌包设定的位置/尺寸叠加，保证 Logo 清晰、位置合规、不变形。交付给客户的应是 branded 版本。

## 与其他 skill 的关系

- 各场景 skill 出片后，若客户要求品牌 Logo，走 `stamp` 出 branded 版本再交付。
- `digital-human` 做品牌服装时，也用 style-prefix 保持调性一致。

## 完成标准

- `brand/<client>/brand.json` 含 Logo/主色/字体/风格；Logo 文件已入库。
- 出图 prompt 带 style-prefix；交付视频为叠加 Logo 的 branded 版本。
