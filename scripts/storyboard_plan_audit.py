#!/usr/bin/env python3
"""Non-visual audit helpers for storyboard plans.

This module deliberately performs text/contract checks only. It does not call a
vision model and must not be used as an automated visual approval gate.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import storyboard_validator  # noqa: E402


HIGH_RISK_USE = re.compile(
    r"磁吸|吸附|卡扣|扣上|夹住|佩戴|戴上|插入|接入|连接|安装|固定|承重|支撑|"
    r"magnet|magnetic|attach|snap|clip|clamp|wear|plug|connect|mount|dock|stand|support",
    re.I,
)

PRODUCT_TERMS = re.compile(
    r"产品|product|earbud|earphone|speaker|charger|charging case|耳机|音箱|音响|充电|支架",
    re.I,
)

# ``产品介绍`` can describe software/services and ``连接`` can describe a
# flowchart.  Only concrete object language is allowed to turn the physical-use
# audit on when no actual product asset/contract is present.
PHYSICAL_PRODUCT_TERMS = re.compile(
    r"earbud|earphone|speaker|charger|charging case|phone case|wearable|"
    r"耳机|音箱|音响|充电器|充电盒|手机壳|支架|穿戴设备|实体产品|实物产品",
    re.I,
)
DIAGRAM_CONNECTION_CONTEXT = re.compile(
    r"图标|流程图|连接线|箭头|卡片|节点|版式|内容页|"
    r"icon|flowchart|diagram|arrow|card|node|layout",
    re.I,
)

RELATION_KEYS = (
    "use_relation", "physical_relation", "usage_relation",
    "interaction_relation", "product_use_relation",
)

DESIGN_BOARD_REQUIRED_SLOTS = {
    "ppt_page", "corner_pip", "title_area", "safe_zone", "highlight_card",
}
DESIGN_BOARD_OPTIONAL_SLOTS = {"chart", "arrow", "annotation", "chart_arrow_annotation"}
MOTION_PLAN_KEYS = (
    "personality", "primary", "secondary", "ambient", "density", "timing",
    "safe_zone", "a11y",
)
SLOT_LAYER_OWNERS = {
    "ppt_page": {"remotion"},
    "corner_pip": {"fuse", "remotion/fuse"},
    "title_area": {"hyperframes"},
    "safe_zone": {"composite", "remotion/fuse"},
    "highlight_card": {"hyperframes"},
    "chart": {"hyperframes"},
    "arrow": {"hyperframes"},
    "annotation": {"hyperframes"},
    "chart_arrow_annotation": {"hyperframes"},
}


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _shot_text(shot):
    fields = (
        "visual", "scene_prompt", "character_action", "action", "props",
        "prop_prompts", "video_prompt_notes", "director_notes",
    )
    parts = []
    for field in fields:
        value = shot.get(field)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    for panel in _list(shot.get("panel_plan")):
        if isinstance(panel, dict):
            parts.extend(str(panel.get(key) or "") for key in ("beat", "action", "event"))
        elif panel:
            parts.append(str(panel))
    return " ".join(parts)


def _has_product_reference(plan):
    refs = plan.get("asset_refs") or {}
    if any(refs.get(key) for key in (
            "product_images", "product_sku", "product_boards",
            "product_usage_images", "product_reference_images")):
        return True
    if plan.get("physical_product") or str(plan.get("product_kind") or "").lower() in {
            "physical", "physical_product", "实体产品", "实物产品"}:
        return True
    if PHYSICAL_PRODUCT_TERMS.search("%s %s" % (
            plan.get("product_type") or "", plan.get("product_name") or "")):
        return True
    for shot in plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        shot_refs = shot.get("asset_refs") or {}
        if (any(shot_refs.get(key) for key in (
                "product_images", "product_sku", "product_boards",
                "product_usage_images", "product_reference_images")) or
                shot.get("product_sku") or shot.get("product_refs") or
                _has_relation_contract(shot)):
            return True
        if PHYSICAL_PRODUCT_TERMS.search(_shot_text(shot)):
            return True
    return False


def _is_high_risk_physical_use(shot, *, has_product):
    if not has_product or not isinstance(shot, dict):
        return False
    if _has_relation_contract(shot):
        return True
    text = _shot_text(shot)
    match = HIGH_RISK_USE.search(text)
    if not match:
        return False
    term = str(match.group(0) or "").lower()
    if term in {"连接", "connect"} and DIAGRAM_CONNECTION_CONTEXT.search(text):
        return bool(PHYSICAL_PRODUCT_TERMS.search(text))
    return True


def _has_human(plan):
    refs = plan.get("asset_refs") or {}
    if plan.get("characters") or refs.get("digital_human_portraits"):
        return True
    for shot in plan.get("shots") or []:
        if isinstance(shot, dict) and (
                shot.get("characters") or shot.get("character_prompt") or
                shot.get("character_action") or shot.get("digital_human") or
                shot.get("actor")):
            return True
    return False


def _has_relation_contract(shot):
    return any(bool(shot.get(key)) for key in RELATION_KEYS)


def _slot_map(shot, motion_plan):
    raw = shot.get("asset_slots") or (motion_plan or {}).get("asset_slots") or []
    return {
        str(item.get("slot") or item.get("kind")): item
        for item in raw if isinstance(item, dict) and (item.get("slot") or item.get("kind"))
    }


def _has_document_material(plan, shot, slots):
    refs = plan.get("asset_refs") or {}
    plan_material = any(plan.get(key) for key in (
        "source_ppt", "source_doc", "source_pdf", "slide_images_dir", "ppt_slides_dir",
        "slides", "slide_images", "document_files", "ppt_files",
    ))
    shot_material = any(shot.get(key) for key in (
        "slide_image", "content_image", "source_slides", "source_pages", "slide_range",
        "source_refs", "content_design", "content_page_contract",
    ))
    slot_material = bool(
        (slots.get("ppt_page") or {}).get("source") or
        (slots.get("ppt_page") or {}).get("ref_tags") or
        (slots.get("ppt_page") or {}).get("enabled"))
    ref_material = bool(refs.get("document_images") or refs.get("slide_images") or
                       refs.get("ppt_pages"))
    return plan_material or shot_material or slot_material or ref_material


def _design_board_quality(plan, shot, motion_plan, slots, previous=None):
    """Return deterministic preflight findings for the render contract.

    Material bindings and safe-zone hierarchy are executable preconditions.
    Rhythm remains advisory, while a missing required asset or a collision must
    fail before an approval-grade preview is rendered.
    """
    materials = {"ok": True, "missing": [], "present": [], "unbound_slots": []}
    paired_assets = []
    if _has_document_material(plan, shot, slots):
        materials["present"].append("ppt_page")
    else:
        materials["ok"] = False
        materials["missing"].append("ppt_page/source_document")
    has_human = bool(shot.get("characters") or shot.get("character_prompt") or
                     (slots.get("corner_pip") or {}).get("enabled"))
    if has_human and not (slots.get("corner_pip") or {}).get("ref_tags") and not (
            plan.get("asset_refs") or {}).get("digital_human_portraits"):
        materials["ok"] = False
        materials["missing"].append("corner_pip/avatar_reference")
    for name, item in slots.items():
        binding = item.get("asset_binding") if isinstance(item.get("asset_binding"), dict) else {}
        if not binding:
            continue
        if (item.get("enabled", True) and binding.get("required") and
                binding.get("status") == "missing"):
            materials["ok"] = False
            materials["unbound_slots"].append(name)
            materials["missing"].append("%s/%s" % (name, binding.get("artifact") or "asset"))
        for requirement in _list(item.get("asset_requirements")):
            if not isinstance(requirement, dict) or not requirement.get("required"):
                continue
            status = str(requirement.get("status") or "needs_source")
            record = {
                "slot": name,
                "asset_id": requirement.get("asset_id") or "%s_asset" % name,
                "source": requirement.get("source") or "none",
                "status": status,
            }
            paired_assets.append(record)
            if status in {"needs_source", "needs_generation", "awaiting_confirmation"}:
                materials["ok"] = False
                materials["missing"].append("%s/%s" % (name, record["asset_id"]))
    hierarchy = {"ok": True, "missing": [], "invalid_layers": [], "duplicate_safe_zones": []}
    required = set(DESIGN_BOARD_REQUIRED_SLOTS)
    if has_human is False:
        required.discard("corner_pip")
    for slot in sorted(required):
        if slot not in slots:
            hierarchy["ok"] = False
            hierarchy["missing"].append(slot)
    for name, item in slots.items():
        expected = SLOT_LAYER_OWNERS.get(name)
        if expected and item.get("layer") not in expected:
            hierarchy["ok"] = False
            hierarchy["invalid_layers"].append(
                "%s=%s (expected %s)" % (name, item.get("layer"), "/".join(sorted(expected))))
    occupied = {}
    for name, item in slots.items():
        # Bullets/motion elements are children of their parent overlay and are
        # intentionally co-located; counting each child as an independent
        # collision made every valid list animation fail hierarchy QA.
        if name.startswith(("motion_overlay_bullet_", "motion_element_")):
            continue
        if item.get("enabled", True) and item.get("renderable", name not in ("ppt_page", "corner_pip", "safe_zone")):
            zone = str(item.get("safe_zone") or "")
            if zone:
                timing = str(item.get("timing") or "unspecified")
                occupied.setdefault((zone, timing), []).append(name)
    hierarchy["duplicate_safe_zones"] = [
        "%s [%s]: %s" % (zone, timing, ", ".join(names))
        for (zone, timing), names in occupied.items()
        if len(names) > 1 and zone not in ("center", "lower_third")
    ]
    if hierarchy["duplicate_safe_zones"]:
        hierarchy["ok"] = False

    rhythm = {"ok": True, "issues": [], "active_slots": 0, "timing_slots": 0,
              "motion_layers": [], "choreography": {}}
    active = [item for item in slots.values() if item.get("enabled", True) and
              item.get("renderable", item.get("slot") not in ("ppt_page", "corner_pip", "safe_zone"))]
    rhythm["active_slots"] = len(active)
    for item in active:
        if item.get("timing"):
            rhythm["timing_slots"] += 1
        else:
            rhythm["ok"] = False
            rhythm["issues"].append("%s 缺少 timing" % item.get("slot"))
    density = str(motion_plan.get("density") or "medium")
    limits = {"low": (0, 4), "medium": (1, 7), "high": (2, 12)}
    low, high = limits.get(density, limits["medium"])
    if len(active) > high:
        rhythm["ok"] = False
        rhythm["issues"].append("density=%s 但启用 %d 个动效槽位，超过 %d" % (density, len(active), high))
    if len(active) < low:
        rhythm["issues"].append("density=%s 但有效动效槽位偏少（%d）" % (density, len(active)))
    if previous:
        previous_preset = previous.get("preset") or previous.get("primary")
        current_preset = motion_plan.get("preset") or motion_plan.get("primary")
        if previous_preset and current_preset and previous_preset == current_preset:
            rhythm["issues"].append("相邻镜头主动效节奏相同，建议改变进入方式或景别")

    choreography = motion_plan.get("choreography") if isinstance(
        motion_plan.get("choreography"), dict) else {}
    identity = motion_plan.get("motion_identity") if isinstance(
        motion_plan.get("motion_identity"), dict) else {}
    rhythm["choreography"] = choreography
    required_layers = {"primary", "secondary", "ambient"}
    declared_layers = {
        value for key, value in choreography.items()
        if key in ("primary_layer", "secondary_layer", "ambient_layer") and value
    }
    rhythm["motion_layers"] = sorted(declared_layers)
    if choreography and declared_layers != required_layers:
        rhythm["ok"] = False
        rhythm["issues"].append("choreography 必须声明 primary/secondary/ambient 三层")
    active_limit = choreography.get("active_motion_limit", identity.get("active_motion_limit", 0.33))
    try:
        active_limit = float(active_limit)
    except (TypeError, ValueError):
        active_limit = 0.33
    # Timing strings are coarse in legacy plans. Count the fully overlapping
    # '全程' tracks as a conservative proxy for the one-third choreography rule.
    continuous = [item for item in active if str(item.get("timing") or "") in ("全程", "spoken")]
    if len(active) >= 3 and len(continuous) / float(len(active)) > active_limit:
        rhythm["ok"] = False
        rhythm["issues"].append(
            "同时持续运动槽位过多（%d/%d），超过 active_motion_limit=%.2f" %
            (len(continuous), len(active), active_limit))

    a11y = motion_plan.get("a11y") if isinstance(motion_plan.get("a11y"), dict) else {}
    accessibility = {
        "ok": bool(a11y.get("contrast") and a11y.get("max_lines") is not None),
        "contrast": a11y.get("contrast"), "max_lines": a11y.get("max_lines"),
        "subtitle_required": a11y.get("subtitle_required"),
    }
    return {
        "materials": materials, "paired_assets": paired_assets,
        "hierarchy": hierarchy, "rhythm": rhythm,
        "density": density, "a11y": accessibility,
        "ok": all(item.get("ok", True) for item in (materials, hierarchy, rhythm, accessibility)),
    }


def _checklist(plan):
    has_product = _has_product_reference(plan)
    has_human = _has_human(plan)
    usage_needed = has_product
    design_board = bool((plan or {}).get("storyboard_role") == "design_board" or
                        str((plan or {}).get("storyboard_contract", {}).get("kind") or "").lower() == "design_board")
    continuity_item = (
        "人物、产品、场景、光线和 BGM/声音氛围跨段保持一致"
        if has_product else
        "人物、PPT/页面、场景、光线和 BGM/声音氛围跨段保持一致"
    )
    checklist = {
        "product_board": [
            "产品外形、颜色、材质、按钮、接口、Logo/孔位与客户确认产品图一致",
            "九宫格没有把产品改成其它品类、扁平贴片或通用道具",
            "若源产品图变更，旧产品板确认必须失效",
        ] if has_product else [],
        "cast_board": [
            "六视图完整：全身正/后/侧 + 正脸正/后/侧",
            "正脸特写与全身脸是同一人，发型、服装、配饰一致",
            "没有多余人物、重复脸、裁脚或明显畸形",
        ] if has_human else [],
        "product_usage_image": [
            "人物真实使用产品，手部接触点、操作方向和产品关键细节可见",
            "第 3/4/7 格等关键接触格证明目标宽平面、贴合接触线、产品外露面",
            "若存在物理关系合同，必须由客户确认并写入 usage_geometry_review.json",
        ] if usage_needed else [],
        "storyboard": [
            "每个 shot 的格数来自剧本分镜，不为凑数拆分或合并",
            "相邻镜头有 30-50 度机位差、景别变化或构图重心变化",
            "底片画面无字幕、悬浮文字、Logo、水印；文字动效均归入 motion_elements",
            continuity_item,
        ],
    }
    if design_board:
        checklist["design_board"] = [
            "PPT 页、角落小窗、安全区、高亮卡片、箭头/标注位均已形成 asset_slots 合同",
            "motion_plan 包含 personality / primary / secondary / ambient / density / timing / safe_zone / a11y",
            "每个依赖视觉对象的动效槽位都有 asset_requirements，并在合成前绑定或生成配套素材",
            "LLM/Agent 负责动效创意与灵感；HyperFrames/Remotion 只负责确定性执行",
            "设计板仅作为渲染合同，不作为视频模型分镜输入",
            "文字与动画层由 HyperFrames / Remotion / fuse 叠加，不在底片里生成",
        ]
    return checklist


def audit_plan(plan, validation=None):
    validation = validation or storyboard_validator.validate_plan(plan)
    blockers = list(validation.get("errors") or [])
    warnings = list(validation.get("warnings") or [])
    notes = []
    shots = []
    design_board_quality = []
    previous_motion_plan = None
    refs = plan.get("asset_refs") or {}
    has_product = _has_product_reference(plan)
    has_human = _has_human(plan)
    contract = plan.get("storyboard_contract") if isinstance(plan.get("storyboard_contract"), dict) else {}
    design_board = bool(
        str(plan.get("storyboard_role") or "").strip().lower().replace("-", "_") == "design_board" or
        str(contract.get("kind") or contract.get("role") or contract.get("usage") or "").strip().lower().replace("-", "_") == "design_board" or
        str(contract.get("video_model_usage") or "").strip().lower() == "do_not_submit_as_storyboard_ref"
    )

    if has_product and not (refs.get("product_images") or refs.get("product_sku")):
        warnings.append("计划提到产品，但 plan.asset_refs 缺少 product_images/product_sku；请确认 brief hydration 已恢复真实产品图")
    if has_product and has_human:
        notes.append("数字人与产品同时存在：产品使用图必须单独生成、展示并确认后才能生成故事板")
    if design_board:
        notes.append("design_board 用于渲染合同，不作为视频分镜输入；视频只继承数字人/声音/着装/背景锚点")

    for index, shot in enumerate(plan.get("shots") or [], 1):
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("id") or index)
        text = _shot_text(shot)
        shot_warnings = []
        shot_blockers = []
        shot_notes = []
        if _is_high_risk_physical_use(shot, has_product=has_product):
            shot_notes.append("high_risk_physical_use")
            if not _has_relation_contract(shot):
                shot_warnings.append("高风险使用动作缺少结构化 use_relation/physical_relation 合同")
        refs_for_shot = _list(shot.get("ref_tags"))
        if has_product and not refs_for_shot and PRODUCT_TERMS.search(text):
            shot_warnings.append("产品相关镜头缺少 ref_tags，模型可能只按文字猜产品")
        if storyboard_validator.has_positive_text_request(text):
            shot_warnings.append("画面字段疑似包含文字/标签/字幕动效，应迁移到 motion_elements")
        motion_plan = shot.get("motion_plan") if isinstance(shot.get("motion_plan"), dict) else {}
        asset_slots = shot.get("asset_slots") or motion_plan.get("asset_slots") or []
        if design_board:
            if not asset_slots:
                shot_blockers.append("design_board 缺少 asset_slots 渲染合同")
            if not motion_plan:
                shot_blockers.append("design_board 缺少结构化 motion_plan")
            else:
                required_motion_keys = ["personality", "primary", "secondary", "ambient", "density", "timing", "safe_zone", "a11y"]
                missing_motion_keys = [key for key in required_motion_keys if key not in motion_plan]
                if missing_motion_keys:
                    shot_warnings.append("design_board motion_plan 缺少字段: %s" % ", ".join(missing_motion_keys))
                a11y = motion_plan.get("a11y") if isinstance(motion_plan.get("a11y"), dict) else {}
                if not a11y.get("subtitle_required") and motion_plan.get("density") not in ("low", "medium", "high"):
                    shot_warnings.append("design_board motion_plan 缺少可执行的可访问性/密度约束")
            slot_map = _slot_map(shot, motion_plan)
            quality = _design_board_quality(plan, shot, motion_plan, slot_map,
                                            previous=previous_motion_plan)
            design_board_quality.append({"id": sid, **quality})
            if not quality["materials"]["ok"]:
                shot_blockers.append("design_board 配套素材缺口: %s" %
                                     ", ".join(quality["materials"]["missing"]))
            if not quality["hierarchy"]["ok"]:
                shot_blockers.append("design_board 层级/安全区问题: %s" %
                                     "; ".join(quality["hierarchy"]["missing"] +
                                               quality["hierarchy"]["invalid_layers"] +
                                               quality["hierarchy"]["duplicate_safe_zones"]))
            if quality["rhythm"]["issues"]:
                shot_warnings.append("design_board 节奏/动效密度提示: %s" %
                                     "; ".join(quality["rhythm"]["issues"]))
            if not quality["a11y"]["ok"]:
                shot_blockers.append("design_board 缺少可访问性字段: contrast/max_lines")
            previous_motion_plan = motion_plan
        if shot_blockers:
            blockers.extend("shot %s: %s" % (sid, item) for item in shot_blockers)
        if shot_warnings:
            warnings.extend("shot %s: %s" % (sid, item) for item in shot_warnings)
        shots.append({
            "id": sid,
            "duration": shot.get("duration") or shot.get("duration_sec"),
            "ref_tags": refs_for_shot,
            "notes": shot_notes,
            "warnings": shot_warnings,
            "blockers": shot_blockers,
            "motion_plan": motion_plan if motion_plan else None,
            "asset_slots": asset_slots,
        })

    # generated_at must be deterministic: the audit JSON is a content-addressed
    # approval artifact, and a wall-clock timestamp makes the plan approval hash
    # drift on every re-run, blocking downstream board generation.
    _audit_identity = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()[:16]
    return {
        "ok": not blockers,
        "generated_at": "audit:" + _audit_identity,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
        "shot_count": len(plan.get("shots") or []),
        "has_product_reference": has_product,
        "has_human": has_human,
        "customer_checklist": _checklist(plan),
        "shots": shots,
        "design_board_quality": design_board_quality,
        "quality_summary": {
            "design_board": design_board,
            "material_checks": sum(1 for item in design_board_quality
                                    if item["materials"]["ok"]),
            "hierarchy_checks": sum(1 for item in design_board_quality
                                     if item["hierarchy"]["ok"]),
            "rhythm_checks": sum(1 for item in design_board_quality
                                  if item["rhythm"]["ok"]),
            "a11y_checks": sum(1 for item in design_board_quality
                                if item["a11y"]["ok"]),
            "all_checks_pass": all(item["ok"] for item in design_board_quality),
        },
    }


def _markdown(audit):
    lines = [
        "# Storyboard Plan Audit",
        "",
        "> 非视觉审计：只检查计划字段、引用合同和人工确认清单；不调用视觉模型，不替代客户确认。",
        "",
        "- 状态：%s" % ("通过" if audit.get("ok") else "阻断"),
        "- Shot 数量：%s" % audit.get("shot_count"),
        "- 包含产品：%s" % ("是" if audit.get("has_product_reference") else "否"),
        "- 包含人物：%s" % ("是" if audit.get("has_human") else "否"),
        "",
    ]
    if audit.get("blockers"):
        lines.extend(["## 阻断项", ""])
        lines.extend("- %s" % item for item in audit["blockers"])
        lines.append("")
    if audit.get("warnings"):
        lines.extend(["## 风险提示", ""])
        lines.extend("- %s" % item for item in audit["warnings"])
        lines.append("")
    if audit.get("notes"):
        lines.extend(["## 流程提示", ""])
        lines.extend("- %s" % item for item in audit["notes"])
        lines.append("")
    lines.extend(["## 客户确认清单", ""])
    for name, items in (audit.get("customer_checklist") or {}).items():
        if not items:
            continue
        lines.append("### %s" % name)
        lines.extend("- [ ] %s" % item for item in items)
        lines.append("")
    if audit.get("shots"):
        lines.extend(["## Shot 风险摘要", ""])
        for shot in audit["shots"]:
            bits = []
            if shot.get("ref_tags"):
                bits.append("refs=%s" % " ".join(map(str, shot["ref_tags"])))
            if shot.get("notes"):
                bits.append("notes=%s" % ", ".join(shot["notes"]))
            if shot.get("warnings"):
                bits.append("warnings=%s" % "；".join(shot["warnings"]))
            lines.append("- %s：%s" % (shot["id"], "；".join(bits) if bits else "未发现额外风险"))
    return "\n".join(lines).rstrip() + "\n"


def write_audit(plan, validation, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    audit = audit_plan(plan, validation=validation)
    json_path = os.path.abspath(os.path.join(out_dir, "storyboard_plan_audit.json"))
    md_path = os.path.abspath(os.path.join(out_dir, "storyboard_plan_audit.md"))
    payloads = {
        json_path: json.dumps(audit, ensure_ascii=False, indent=2),
        md_path: _markdown(audit),
    }
    for path, payload in payloads.items():
        try:
            unchanged = Path(path).read_text(encoding="utf-8") == payload
        except OSError:
            unchanged = False
        if not unchanged:
            Path(path).write_text(payload, encoding="utf-8")
    return audit, json_path, md_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit storyboard_plan.json without visual QC")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)
    with open(args.plan, encoding="utf-8") as handle:
        plan = json.load(handle)
    validation = storyboard_validator.validate_plan(plan)
    audit = audit_plan(plan, validation=validation)
    if args.out_dir:
        _, json_path, md_path = write_audit(plan, validation, args.out_dir)
        audit["audit_json"] = json_path
        audit["audit_md"] = md_path
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
