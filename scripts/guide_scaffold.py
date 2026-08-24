#!/usr/bin/env python3
"""Guide scaffold: 素材分诊结果 → 可执行引导表 JSON → 下游引擎产物.

下扎最后一层：把 guide-document-class / guide-image-class 两张「引导表」变成
机器可读产物，客户/LLM 填完即可编译成 remotion_engine 的 shotlist 和用于结构检查的
草稿 segments，无需人工誊抄。草稿 segments 不登记正式 video handoff，不能直接用于交付；
正式出片必须把已确认 guide 内容写入 storyboard_plan，再由 script_splitter --manifest 编译。

三个子命令：
  scaffold  --client X --kind <document|product|venue> --brand <确认主色hex> --segments N [--out FILE]
      按分诊类型生成一份「空引导表」JSON（预置层级/镜位骨架，待填内容）。
  compile-shots  --file guide.json --out shots.json
      把填好的引导表编译成 remotion_engine 的 shotlist（内容页+运镜背景）。
  compile-segments --file guide.json --urls-map '{"row_id":"url"}' --out segments.json
      把填好的引导表编译成仅供本地结构检查的草稿 segments；不得直接正式出片。

引导表 JSON schema（kind 决定 rows 骨架）：
  {"client","kind","theme","brand_primary","fps":30,"resolution":[1080,1920],
   "rows":[{"id","role","content","talk","move","overlay","seconds","image","bullets"}]}

fps/resolution 默认竖屏 1080x1920@30。seconds→durationInFrames=seconds*fps。
"""
import os
import sys
import json
import argparse
import re

from artifact_contract import build_video_handoff
import interaction_contract
import model_behavior
from script_splitter import _pick_video_type

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")

# 每类分诊 → 预置镜位骨架（role/move/overlay 建议值，content/talk 待填）
SKELETONS = {
    # 文档/文案类（内容讲解型） → 核心信息 + N 支撑点 + CTA
    "document": {
        "theme": "内容讲解型（讲清楚）",
        "fixed_head": {"role": "开场·核心信息", "move": "push_in",
                       "overlay": "大标题快闪", "seconds": 4},
        "repeat":    {"role": "支撑点", "move": "ken_burns",
                      "overlay": "标题+bullets", "seconds": 10},
        "fixed_tail": {"role": "结尾·CTA", "move": "still",
                       "overlay": "CTA页+二维码位", "seconds": 4},
    },
    # 商品图 → 带货：钩子→主卖点→次卖点→场景→CTA
    "product": {
        "theme": "商品推荐/带货（展示+种草）",
        "fixed_head": {"role": "钩子", "move": "push_in",
                       "overlay": "悬念大字", "seconds": 3, "image_role": "hero"},
        "repeat":    {"role": "卖点", "move": "ken_burns",
                      "overlay": "参数标签快闪", "seconds": 4, "image_role": "detail"},
        "fixed_tail": {"role": "种草CTA", "move": "still",
                       "overlay": "CTA+价格/二维码", "seconds": 3, "image_role": "pack"},
    },
    # 场地/工厂图 → 实景介绍：建立→规模→资质→服务→CTA
    "venue": {
        "theme": "实景介绍（工厂/场景/服务）",
        "fixed_head": {"role": "建立场景", "move": "push_in",
                       "overlay": "地点/品牌名", "seconds": 4, "image_role": "establish"},
        "repeat":    {"role": "背书", "move": "ken_burns",
                      "overlay": "数据/资质标签", "seconds": 6, "image_role": "scale"},
        "fixed_tail": {"role": "CTA", "move": "still",
                       "overlay": "联系方式+二维码", "seconds": 4, "image_role": "establish"},
    },
}


def _row(idx, spec, extra=None):
    r = {
        "id": "r%d" % idx,
        "role": spec.get("role", ""),
        "content": "",          # 内容页/讲解要点（待填）
        "talk": "",             # 数字人台词（待填，短句）
        "move": spec.get("move", "still"),
        "overlay": spec.get("overlay", ""),
        "seconds": spec.get("seconds", 5),
        "image": "",            # 用图相对路径（图片类填，文档类可空）
        "image_role": spec.get("image_role", ""),
        "bullets": [],          # 内容页 bullets（文档类支撑点用）
    }
    if extra:
        r.update(extra)
    return r


def _normalize_brand_primary(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    hex_value = raw[1:] if raw.startswith("#") else raw
    if re.fullmatch(r"[0-9a-fA-F]{3}", hex_value):
        return "#" + "".join(ch * 2 for ch in hex_value).upper()
    if re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
        return "#" + hex_value.upper()
    raise ValueError("BRAND_PRIMARY_INVALID: 请提供 hex 颜色，例如 #E60012")


def scaffold(client, kind, segments, brand_primary=None):
    if kind not in SKELETONS:
        raise SystemExit("kind 必须是 document/product/venue，收到: %s" % kind)
    sk = SKELETONS[kind]
    rows = []
    idx = 1
    rows.append(_row(idx, sk["fixed_head"])); idx += 1
    body_n = max(1, segments - 2)  # 去掉头尾，中间重复段
    for _ in range(body_n):
        rows.append(_row(idx, sk["repeat"])); idx += 1
    rows.append(_row(idx, sk["fixed_tail"])); idx += 1
    return {
        "client": client,
        "kind": kind,
        "theme": sk["theme"],
        "brand_primary": _normalize_brand_primary(brand_primary),
        "fps": 30,
        "resolution": [1080, 1920],
        "rows": rows,
        "authoring_contract": {
            "mode": "restricted_patch",
            "question_id": "guide.rows",
            "editable_row_fields": [
                "content", "talk", "bullets", "motion_elements", "image",
                "characters", "references", "asset_refs"],
            "formal_video_compiler": "script_splitter --manifest",
        },
    }


def guide_patch_contract(guide, model_profile=None, artifact_path=None):
    """Return a strict patch contract instead of inviting full-file rewrites."""
    profile = model_profile or model_behavior.conservative_profile()
    contract = interaction_contract.build_contract(
        "script", {"flow": {}, "customer_preview": [artifact_path] if artifact_path else []},
        {"brief": {"client": guide.get("client")}}, profile)
    fields = set(((guide.get("authoring_contract") or {}).get("editable_row_fields") or []))
    editable = []
    for index, _row_item in enumerate(guide.get("rows") or []):
        editable.extend("/rows/%d/%s" % (index, field) for field in sorted(fields))
    contract["question_id"] = "guide.rows"
    contract["objective"] = "只修改当前引导表中的创意字段，不覆盖客户、镜位 ID、时长和渲染合同"
    contract["editable_paths"] = editable or ["/rows/0/content"]
    contract["success_criteria"] = ["每次修改不超过模型能力上限", "台词和卖点有客户或事实证据", "不修改受保护业务字段"]
    from schema_validate import enforce
    enforce(contract, "interaction-contract", context="guide_patch_contract")
    return contract


def compile_shots(guide):
    """引导表 → remotion_engine shotlist（内容页+运镜背景）。

    输出 width/height 顶层字段（Root.tsx calculateMetadata 所需）；
    brand_primary 是必填品牌锚点，缺失则直接报错，避免 ShotSequence 回退到默认色。
    has_digital_human=false 时不留 humanSlot，防止右侧 42% 区域被白占。
    """
    fps = guide.get("fps", 30)
    res = guide.get("resolution", [1080, 1920])
    width, height = (int(res[0]), int(res[1])) if isinstance(res, (list, tuple)) and len(res) == 2 else (1080, 1920)
    brand_primary = _normalize_brand_primary(guide.get("brand_primary") or guide.get("brandPrimary"))
    if not brand_primary:
        raise ValueError("BRAND_PRIMARY_REQUIRED: 请先在引导阶段确认企业主色 hex（例如 #E60012）")
    has_human = guide.get("has_digital_human", True)  # 默认保留槽以兼容旧行为
    shots = []
    for r in guide["rows"]:
        shot = {
            "durationInFrames": int(round(r.get("seconds", 5) * fps)),
            "move": r.get("move", "still"),
        }
        if has_human:
            shot["humanSlot"] = r.get("human_slot", "right")
        if r.get("content") or r.get("role"):
            shot["title"] = r.get("content") or r.get("role")
        if r.get("bullets"):
            shot["bullets"] = r["bullets"]
        if r.get("image"):
            shot["image"] = r["image"]
        shots.append(shot)
    out = {
        "width": width, "height": height,
        "fps": fps, "shots": shots,
    }
    out["brandPrimary"] = brand_primary
    if guide.get("font_family"):
        out["fontFamily"] = guide["font_family"]
    return out


def compile_segments(guide, urls_map=None, ratio="9:16", allow_text2video=False):
    """引导表 → 草稿 batch segments（图+文字结构检查，不登记正式 handoff）。

    成片方法论（铁律）：每段都是「图 + 文字 → 图生视频」。参考图是成片锚点，
    给每段做人景同框(type4)/首帧(type2)/多图(type5)。台词取 row.talk（空则用 content）。

    urls_map: {row_id: 参考图URL/路径}，覆盖 row.image。
    缺图处理（关键）：**默认不静默退回文生视频**。无图的段落会被标记
    needs_image=true 并跳过提交——调用方（asset_prep.assess / agent）应先补齐
    素材图（上传或 gen-image）再重新编译。只有显式 allow_text2video=True
    （纯数字人口播、确无产品图的场景）才允许 type1 文生。

    返回 {"segments": [...], "needs_image": [row_id...]}。
    """
    urls_map = urls_map or {}
    client = guide.get("client")
    if not client:
        raise ValueError("CLIENT_REQUIRED: guide 缺少 client")
    run_id = str(guide.get("run_id") or "guide")
    storyboard_approval = dict(guide.get("storyboard_approval") or {
        "client": client, "run_id": run_id, "status": "not_applicable",
        "plan_fingerprint": None, "result_json": None, "out_dir": None,
    })
    segs = []
    needs_image = []
    for r in guide["rows"]:
        text = (r.get("talk") or r.get("content") or "").strip()
        if not text:
            continue  # 台词为空的行跳过（可能只是内容页）
        mapped = urls_map.get(r["id"])
        raw_urls = mapped if mapped is not None else r.get("urls") or r.get("image")
        urls = list(raw_urls) if isinstance(raw_urls, (list, tuple)) else ([raw_urls] if raw_urls else [])
        if not urls and not allow_text2video:
            # 缺图 → 不降级为文生，标记待补图
            needs_image.append(r["id"])
            continue
        has_human = bool(r.get("characters") or guide.get("has_digital_human"))
        has_environment = bool(r.get("image_role") or guide.get("kind") in ("product", "venue"))
        seg = {
            "id": r["id"],
            "client": client,
            "run_id": run_id,
            "storyboard_approval": storyboard_approval,
            "text": text,
            "dialogue": (r.get("talk") or "").strip(),
            "video_type": _pick_video_type(len(urls), has_human, has_environment),
            "urls": urls,
            "references": list(r.get("references") or []),
            "duration": max(3, int(round(r.get("seconds", 5)))),
            "ratio": ratio,
            "resolution": guide.get("video_resolution") or "1080p",
            "out_path": os.path.join("output", client, run_id, "seg_%s.mp4" % r["id"]),
            "motion_elements": list(r.get("motion_elements") or []),
            "characters": list(r.get("characters") or []),
            "asset_refs": dict(r.get("asset_refs") or {}),
            "source_shot_ids": list(r.get("source_shot_ids") or [r["id"]]),
        }
        if r.get("image_role"):
            seg["image_role"] = r["image_role"]
        seg["video_handoff_fingerprint"] = build_video_handoff(seg)["fingerprint"]
        segs.append(seg)
    return {
        "schema_version": 2, "contract_version": 1,
        "client": client, "run_id": run_id,
        "storyboard_approval": storyboard_approval,
        "kind": guide.get("kind"), "theme": guide.get("theme"),
        "ratio": ratio, "resolution": guide.get("video_resolution") or "1080p",
        "segments": segs, "needs_image": needs_image,
        "total_seconds": sum(segment["duration"] for segment in segs),
        "video_handoff_fingerprints": {
            segment["id"]: segment["video_handoff_fingerprint"] for segment in segs},
        "guide_metadata": {key: value for key, value in guide.items() if key != "rows"},
    }


def main(argv):
    p = argparse.ArgumentParser(description="guide scaffold + compile to engines")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("scaffold", help="按分诊类型生成空引导表")
    ps.add_argument("--client", required=True)
    ps.add_argument("--kind", required=True, help="document/product/venue")
    ps.add_argument("--segments", type=int, default=5, help="总镜位数(含头尾)")
    ps.add_argument("--brand", required=True, help="品牌主色 hex（必填，例如 #E60012）")
    ps.add_argument("--out")

    pc = sub.add_parser("compile-shots", help="引导表→remotion shotlist")
    pc.add_argument("--file", required=True)
    pc.add_argument("--out")

    pg = sub.add_parser("compile-segments", help="引导表→草稿 segments（仅结构检查，不登记正式 handoff）")
    pg.add_argument("--file", required=True)
    pg.add_argument("--urls-map", dest="urls_map", help='JSON {"r1":"url",...}')
    pg.add_argument("--ratio", default="9:16")
    pg.add_argument("--allow-text2video", dest="allow_text2video", action="store_true",
                    help="允许无图段落退回文生视频 type1（默认关闭：缺图会标记待补图）")
    pg.add_argument("--out")

    pct = sub.add_parser("patch-contract", help="输出当前引导表的受限 Patch 合同")
    pct.add_argument("--file", required=True)
    pct.add_argument("--model-profile")
    pct.add_argument("--out")

    pap = sub.add_parser("apply-patch", help="校验并原子应用 LLM 创意 Patch")
    pap.add_argument("--file", required=True)
    pap.add_argument("--proposal", required=True)
    pap.add_argument("--model-profile")

    a = p.parse_args(argv)
    if a.cmd == "scaffold":
        g = scaffold(a.client, a.kind, a.segments, brand_primary=a.brand)
        out = json.dumps(g, ensure_ascii=False, indent=2)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(out)
            print("引导表已生成: %s（%d 镜位待填）" % (a.out, len(g["rows"])))
        else:
            print(out)
    elif a.cmd == "compile-shots":
        with open(a.file, encoding="utf-8") as f:
            g = json.load(f)
        out = json.dumps(compile_shots(g), ensure_ascii=False, indent=2)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(out)
            print("shotlist 已生成: %s" % a.out)
        else:
            print(out)
    elif a.cmd == "compile-segments":
        with open(a.file, encoding="utf-8") as f:
            g = json.load(f)
        um = json.loads(a.urls_map) if a.urls_map else None
        result = compile_segments(g, urls_map=um, ratio=a.ratio,
                                  allow_text2video=a.allow_text2video)
        segs = result["segments"]
        needs = result["needs_image"]
        out = json.dumps(result, ensure_ascii=False, indent=2)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(out)
            print("segments 已生成: %s（%d 段可出片）" % (a.out, len(segs)))
        else:
            print(out)
        if needs:
            print("\n⚠ %d 个镜位缺锚定图，未纳入出片：%s" % (len(needs), ", ".join(needs)))
            print("  → 图生视频铁律：这些段需先补图（上传或 "
                  "`asset_prep.py gen-image`）再重编译；")
            print("  → 若确为纯数字人口播无需产品图，加 --allow-text2video 显式放行。")
    elif a.cmd in ("patch-contract", "apply-patch"):
        with open(a.file, encoding="utf-8") as f:
            g = json.load(f)
        profile = (model_behavior.load_profile(a.model_profile)
                   if a.model_profile else model_behavior.conservative_profile())
        contract = guide_patch_contract(g, profile, artifact_path=os.path.abspath(a.file))
        if a.cmd == "patch-contract":
            if a.out:
                os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
                from project_utils import atomic_write_json
                atomic_write_json(a.out, contract)
            print(json.dumps(contract, ensure_ascii=False, indent=2))
        else:
            with open(a.proposal, encoding="utf-8") as f:
                proposal = json.load(f)
            result = interaction_contract.apply_proposal(
                contract, proposal, a.file, target_schema=None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
