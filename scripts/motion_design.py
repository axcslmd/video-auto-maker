#!/usr/bin/env python3
"""导演级动效设计规划器 — 在故事板阶段就规划好字幕/动效/安全区。

核心理念（资深导演视角）：
  字幕动效不是后期补救，而是前期预演的一部分。导演在写剧本时就应该决定：
  「这里画面上要叠什么字、什么时候出现、怎么动、放在画面哪个位置」。
  这个决策必须同时影响：
    ① 视频生成 prompt（告诉模型在安全区不要放关键动作/主体）
    ② 故事板生成 prompt（在构图中预留文字叠加空间）
    ③ 后期字幕渲染（直接使用预规划的文本/时机/样式，而非逆向猜测）

工作流：
  1. design_from_plan(storyboard_plan) → motion_design.json
     读取定稿 storyboard_plan.json，用 LLM 生成逐镜动效设计规范。
  2. inject_safe_zones(prompt_text, shot_design) → prompt_text
     把安全区指令注入视频生成 prompt。
  3. storyboard_safe_zone_prompt(shot_design) → str
     生成故事板构图的安全区提示。

CLI:
  design --plan output/storyboard_plan.json --out output/motion_design.json
  inject --plan output/storyboard_plan.json --design output/motion_design.json \
    --out output/<client>/<run-id>/storyboard_execution_plan.json \
    --manifest output/<client>/<run-id>/run_manifest.json
"""
import os
import re
import sys
import json
import argparse
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import br_client  # noqa: E402
import key_setup  # noqa: E402
import run_manifest as run_manifest_store  # noqa: E402
import text_provider  # noqa: E402
import dialogue_timing  # noqa: E402
from project_utils import atomic_write_json  # noqa: E402
from aspect_ratio import output_ratio  # noqa: E402

# ── 安全区定义 ──────────────────────────────────────────────────────
# 每个安全区对应画面的一块区域，视频生成时模型应避免在此区域放置关键动作/主体。
# 后期字幕/动效会精确叠加在这些区域。

MOTION_PLAN_SCHEMA_VERSION = 2
# The semantic MotionPlan remains v2 for compatibility. This contract version
# adds render-time asset bindings without forcing old storyboard files to be
# regenerated.
MOTION_CONTRACT_VERSION = 2
EXECUTION_PLAN_VERSION = 1
EXECUTION_PROPERTIES = {
    "opacity", "x", "y", "scale", "scaleX", "scaleY", "rotation", "skewX", "skewY",
    "width", "height", "borderRadius", "blur", "brightness", "saturate", "maskOpacity",
}

# These are composition slots, not video-model prompt content.  The first
# five define the base explainer layout; chart/arrow/annotation are separate
# semantic anchors so Remotion and HyperFrames can address them independently.
DESIGN_BOARD_ASSET_SLOTS = (
    "ppt_page", "corner_pip", "title_area", "safe_zone", "highlight_card",
    "chart", "arrow", "annotation", "chart_arrow_annotation",
)

SAFE_ZONES = {
    "lower_third": {
        "description": "画面下方 25% 区域",
        "prompt_hint": "Keep the lower 25% of the frame clear of key action, faces, and product details; this space is reserved for subtitle overlay.",
        "storyboard_hint": "构图时下方留 25% 空白区，不放置关键主体、人脸或产品细节",
        "video_region": {"bottom_pct": 25},
    },
    "upper_third": {
        "description": "画面上方 20% 区域",
        "prompt_hint": "Keep the upper 20% of the frame clear of key action and faces; this space is reserved for title overlay.",
        "storyboard_hint": "构图时上方留 20% 空白区，不放置关键主体或人脸",
        "video_region": {"top_pct": 20},
    },
    "center": {
        "description": "画面中央区域",
        "prompt_hint": "Keep the center of the frame clear for a title reveal; position key subjects slightly off-center.",
        "storyboard_hint": "构图时中央留白，主体偏左或偏右",
        "video_region": {"center": True},
    },
    "corner": {
        "description": "画面右上角区域",
        "prompt_hint": "Keep the upper-right corner clear; a small data card will appear there.",
        "storyboard_hint": "构图时右上角留空，不放置关键元素",
        "video_region": {"corner": "upper_right"},
    },
    "left": {
        "description": "画面左侧 30% 区域",
        "prompt_hint": "Keep the left 30% of the frame clear; text content will appear on the left side.",
        "storyboard_hint": "构图时左侧留 30% 空白",
        "video_region": {"left_pct": 30},
    },
    "right": {
        "description": "画面右侧 30% 区域",
        "prompt_hint": "Keep the right 30% of the frame clear; text content will appear on the right side.",
        "storyboard_hint": "构图时右侧留 30% 空白",
        "video_region": {"right_pct": 30},
    },
}

# ── 动效类型与安全区映射 ─────────────────────────────────────────────
# 每种动效类型默认使用哪个安全区。
STYLE_DEFAULT_ZONE = {
    "title_reveal": "center",
    "bullet_list": "left",
    "metric_pop": "corner",
    "data_card": "corner",
    "lower_third": "lower_third",
    "keyword_flash": "center",
}

MOTION_PERSONALITY_PRESETS = {
    "calm": {
        "primary": "low",
        "secondary": "low",
        "ambient": "low",
        "density": "low",
        "timing": "slow",
    },
    "balanced": {
        "primary": "medium",
        "secondary": "medium",
        "ambient": "low",
        "density": "medium",
        "timing": "steady",
    },
    "energetic": {
        "primary": "high",
        "secondary": "medium",
        "ambient": "medium",
        "density": "high",
        "timing": "quick",
    },
}

# The LLM/Agent chooses one project-level motion identity. The renderer only
# executes this authored result; it must never invent the creative direction.
MOTION_IDENTITY_PRESETS = {
    "calm": {
        "archetype": "premium",
        "primary_lens": "restraint",
        "secondary_lens": "production_polish",
        "selective_lens": "creative_experimentation",
        "signature_easing": "cubic-bezier(0.4,0,0.2,1)",
        "duration_palette_ms": {"quick": 180, "standard": 420, "slow": 720},
        "entrance_pattern": "soft_rise",
        "stagger_budget_ms": 420,
        "active_motion_limit": 0.33,
    },
    "balanced": {
        "archetype": "corporate",
        "primary_lens": "production_polish",
        "secondary_lens": "restraint",
        "selective_lens": "creative_experimentation",
        "signature_easing": "cubic-bezier(0.2,0,0,1)",
        "duration_palette_ms": {"quick": 160, "standard": 320, "slow": 560},
        "entrance_pattern": "directional_fade",
        "stagger_budget_ms": 360,
        "active_motion_limit": 0.33,
    },
    "energetic": {
        "archetype": "energetic",
        "primary_lens": "creative_experimentation",
        "secondary_lens": "production_polish",
        "selective_lens": "restraint",
        "signature_easing": "expo.out",
        "duration_palette_ms": {"quick": 120, "standard": 220, "slow": 420},
        "entrance_pattern": "purposeful_pop",
        "stagger_budget_ms": 300,
        "active_motion_limit": 0.33,
    },
}


def _motion_identity(personality):
    return dict(MOTION_IDENTITY_PRESETS.get(
        str(personality or "balanced"), MOTION_IDENTITY_PRESETS["balanced"]))


def _slot_asset_binding(slot, *, shot=None, plan_summary=None):
    """Describe the artifact a composition slot must receive at render time.

    A design board is a composition contract, not a video-model storyboard.
    This binding makes that distinction executable: source material is consumed
    by Remotion, the presenter plate is generated by the video model, and
    authored graphics are rendered deterministically by HyperFrames.
    """
    slot = slot if isinstance(slot, dict) else {}
    name = str(slot.get("slot") or slot.get("kind") or "motion_element")
    owner = str(slot.get("owner") or slot.get("layer") or "hyperframes")
    renderable = bool(slot.get("renderable", name not in (
        "ppt_page", "corner_pip", "safe_zone")))
    if name == "ppt_page":
        return {
            "kind": "source_material",
            "artifact": "confirmed_slide_or_document_surface",
            "owner": "remotion",
            "required": bool(slot.get("required", True)),
            "generation_mode": "source_or_pre_rendered_content_spec",
            "source": slot.get("source") or slot.get("ref_tags") or None,
            "status": "bound" if slot.get("source") or slot.get("ref_tags") else "missing",
        }
    if name == "corner_pip":
        tags = _compact_list(_coerce_list(slot.get("ref_tags")))
        return {
            "kind": "generated_plate",
            "artifact": "presenter_video_plate",
            "owner": "video_model",
            "required": bool(slot.get("required", False)),
            "generation_mode": "avatar_plate_only",
            "ref_tags": tags,
            "source": slot.get("source") or tags or None,
            "status": "bound" if tags else ("optional" if not slot.get("required") else "missing"),
        }
    if owner in {"hyperframes", "remotion", "remotion/fuse"} and renderable:
        return {
            "kind": "deterministic_overlay",
            "artifact": "hyperframes_motion_layer",
            "owner": "hyperframes" if owner == "hyperframes" else owner,
            "required": bool(slot.get("required", False)),
            "generation_mode": "deterministic_composite",
            "paired_with": _compact_list(_coerce_list(
                slot.get("paired_with") or ["ppt_page"] if name in (
                    "title_area", "highlight_card", "chart", "arrow", "annotation",
                    "motion_overlay") else [])),
            "source": slot.get("description") or slot.get("text") or None,
            "status": "bound" if slot.get("description") or slot.get("text") else "missing",
        }
    return {
        "kind": "layout_anchor",
        "artifact": "composition_geometry",
        "owner": owner,
        "required": bool(slot.get("required", False)),
        "generation_mode": "deterministic_layout",
        "source": slot.get("source") or slot.get("description") or None,
        "status": "bound",
    }


def _normalize_asset_requirements(slot):
    """Normalize LLM-authored paired-material requirements for downstream gates."""
    raw = slot.get("asset_requirements") if isinstance(slot, dict) else None
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    requirements = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "%s_asset_%d" % (
            slot.get("slot") or "motion", index)).strip()
        source = str(item.get("source") or "none").strip().lower()
        approval = str(item.get("approval") or "required").strip().lower()
        if source not in {"confirmed_upload", "generated_img2img", "remotion_content", "none"}:
            source = "none"
        if approval not in {"required", "inherited", "not_required"}:
            approval = "required"
        requirements.append({
            "asset_id": asset_id,
            "role": str(item.get("role") or "paired_visual"),
            "source": source,
            "required": bool(item.get("required", True)),
            "reference_tags": _compact_list(_coerce_list(item.get("reference_tags"))),
            "generation_prompt": str(item.get("generation_prompt") or "").strip(),
            "approval": approval,
            "status": "needs_generation" if source == "generated_img2img" else
                      "needs_source" if source == "none" else
                      "awaiting_confirmation" if approval == "required" else "bound",
        })
    return requirements


def _collect_asset_requirements(asset_slots):
    requirements = []
    for slot in asset_slots or []:
        if not isinstance(slot, dict):
            continue
        for item in _normalize_asset_requirements(slot):
            item["slot"] = slot.get("slot") or slot.get("kind") or "motion_element"
            requirements.append(item)
    return requirements


def _execution_track_for_slot(execution_plan, slot_name):
    if not isinstance(execution_plan, dict):
        return None
    tracks = execution_plan.get("tracks") or []
    aliases = {
        "ppt_page": "content_stage", "chart": "content_stage", "arrow": "foreground_graphics",
        "annotation": "foreground_graphics", "title_area": "foreground_graphics",
        "highlight_card": "foreground_graphics", "subtitle": "foreground_graphics",
        "motion_overlay": "foreground_graphics", "corner_pip": "presenter_plate",
    }
    target = aliases.get(str(slot_name), str(slot_name))
    for track in tracks:
        if isinstance(track, dict) and str(track.get("target")) == target:
            return track
    return None


def _validate_execution_track(track, label, shot_duration=None):
    """Validate one concrete renderer track authored by the LLM."""
    if not isinstance(track, dict):
        raise ValueError("MOTION_EXECUTION_INVALID: %s 必须是对象" % label)
    executor = str(track.get("executor") or "").strip().lower()
    if executor not in {"hyperframes", "remotion"}:
        raise ValueError("MOTION_EXECUTION_INVALID: %s.executor 必须是 hyperframes 或 remotion" % label)
    if not str(track.get("target") or "").strip():
        raise ValueError("MOTION_EXECUTION_INVALID: %s.target 不能为空" % label)
    if not str(track.get("fallback") or "").strip():
        raise ValueError("MOTION_EXECUTION_INVALID: %s.fallback 不能为空" % label)
    parameters = track.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("MOTION_EXECUTION_INVALID: %s.parameters 不能为空" % label)
    timeline = track.get("timeline")
    if not isinstance(timeline, dict):
        raise ValueError("MOTION_EXECUTION_INVALID: %s.timeline 必须是对象" % label)
    try:
        start = float(timeline.get("start"))
        end = float(timeline.get("end"))
    except (TypeError, ValueError):
        raise ValueError("MOTION_EXECUTION_INVALID: %s.timeline.start/end 必须是秒数" % label)
    if start < 0 or end <= start or (shot_duration is not None and end > float(shot_duration) + 0.001):
        raise ValueError("MOTION_EXECUTION_INVALID: %s.timeline 时间范围无效" % label)
    keyframes = track.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes 不能为空" % label)
    previous_at = -1.0
    for index, keyframe in enumerate(keyframes):
        if not isinstance(keyframe, dict):
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d] 必须是对象" % (label, index))
        try:
            at = float(keyframe.get("at"))
        except (TypeError, ValueError):
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].at 必须是秒数" % (label, index))
        if at < 0 or at < previous_at or at > end - start:
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].at 顺序或范围无效" % (label, index))
        previous_at = at
        props = keyframe.get("props")
        if not isinstance(props, dict) or not props:
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].props 不能为空" % (label, index))
        unknown = set(props) - EXECUTION_PROPERTIES
        if unknown:
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d] 属性不受支持: %s" %
                             (label, index, ", ".join(sorted(unknown))))
        for prop, value in props.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].%s 必须是数字" %
                                 (label, index, prop))
        if not str(keyframe.get("easing") or "").strip():
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].easing 不能为空" % (label, index))
        duration = keyframe.get("duration_ms")
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
                                     duration < 0 or duration > 5000):
            raise ValueError("MOTION_EXECUTION_INVALID: %s.keyframes[%d].duration_ms 无效" % (label, index))
    return track


def _validate_execution_plan(execution, label, shot_duration=None):
    """Validate concrete renderer instructions authored by the LLM."""
    if not isinstance(execution, dict):
        raise ValueError("MOTION_EXECUTION_INVALID: %s 必须是对象" % label)
    tracks = execution.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("MOTION_EXECUTION_INVALID: %s.tracks 不能为空" % label)
    for index, track in enumerate(tracks):
        _validate_execution_track(track, "%s.tracks[%d]" % (label, index), shot_duration)
    targets = [str(track.get("target")) for track in tracks]
    if len(set(targets)) != len(targets):
        raise ValueError("MOTION_EXECUTION_INVALID: %s.tracks.target 不能重复" % label)
    render_order = execution.get("render_order")
    if not isinstance(render_order, list) or not render_order:
        raise ValueError("MOTION_EXECUTION_INVALID: %s.render_order 不能为空" % label)
    if set(map(str, render_order)) != set(targets):
        raise ValueError("MOTION_EXECUTION_INVALID: %s.render_order 必须覆盖全部 tracks.target" % label)
    return execution


def _base_plate_for_shot(shot, plan_summary=None):
    """Return the authored/measured plate contract for this shot, if present."""
    shot = shot if isinstance(shot, dict) else {}
    plan_summary = plan_summary if isinstance(plan_summary, dict) else {}
    direct = shot.get("base_plate") or shot.get("base_plate_analysis")
    if isinstance(direct, dict):
        return direct
    analysis = plan_summary.get("base_plate_analysis")
    if isinstance(analysis, dict):
        sid = str(shot.get("id") or shot.get("shot_id") or "")
        for item in analysis.get("shots") or []:
            if isinstance(item, dict) and str(item.get("shot_id") or item.get("id")) == sid:
                return item
    return None


def _attach_base_plate_execution_contract(execution, shot, plan_summary=None):
    """Bind every renderer track to the plate it is designing around.

    This is metadata and geometry input for the executors, not a creative
    fallback.  Unresolved observations remain unresolved so a production gate
    can require vision/agent review before rendering.
    """
    if not isinstance(execution, dict):
        return execution
    plate = _base_plate_for_shot(shot, plan_summary)
    def plate_source(value):
        if isinstance(value, dict):
            return value.get("path") or value.get("ref")
        return value
    for track in execution.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        parameters = track.setdefault("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}
            track["parameters"] = parameters
        source = plate.get("source") if isinstance(plate, dict) else None
        parameters.setdefault("base_plate_ref", plate_source(source))
        parameters.setdefault("base_plate_aspect_ratio", source.get("aspect_ratio")
                              if isinstance(source, dict) else plate.get("measured_aspect_ratio")
                              if isinstance(plate, dict) else None)
        parameters.setdefault("base_plate_duration_seconds", plate.get("measured_duration_seconds")
                              if isinstance(plate, dict) else None)
        parameters.setdefault("dialogue", plate.get("dialogue") if isinstance(plate, dict) else
                              shot.get("dialogue") or shot.get("voiceover") or "")
        parameters.setdefault("character_action", plate.get("action_observation")
                              if isinstance(plate, dict) else shot.get("character_action"))
        parameters.setdefault("occupied_region", plate.get("occupied_region") if isinstance(plate, dict) else None)
        parameters.setdefault("avoid_region", plate.get("avoid_region") if isinstance(plate, dict) else None)
        parameters.setdefault("content_region", plate.get("content_region") if isinstance(plate, dict) else None)
        parameters.setdefault("plate_review_status", plate.get("region_status")
                              if isinstance(plate, dict) else "missing_base_plate_analysis")
    if plate is not None:
        execution.setdefault("base_plate_contract", {
            "timing_master": True,
            "ref": plate_source(plate.get("source")) or plate.get("path"),
            "shot_id": plate.get("shot_id"),
            "duration_seconds": plate.get("measured_duration_seconds"),
            "aspect_ratio": (plate.get("source", {}).get("aspect_ratio")
                             if isinstance(plate.get("source"), dict)
                             else plate.get("measured_aspect_ratio") or plate.get("aspect_ratio")),
            "speech_beats_seconds": plate.get("speech_beats_seconds") or [],
            "action_status": plate.get("action_status"),
            "region_status": plate.get("region_status"),
        })
    return execution


def _safe_zone_hint(zone):
    return SAFE_ZONES.get(zone, {}).get("storyboard_hint") or "构图需预留安全区"


def _compact_list(values):
    return [item for item in (values or []) if item not in (None, "")]


def _coerce_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]
    return [value]


def _detect_motion_personality(shot, plan_summary=None):
    text = " ".join(str(shot.get(key) or "") for key in (
        "visual", "scene_prompt", "character_action", "motion_elements",
        "director_note", "motion_overlay", "audio", "dialogue", "voiceover"))
    text = text.lower()
    if any(term in text for term in ("calm", "稳", "沉", "克制", "minimal", "clean", "document", "explainer")):
        return "calm"
    if any(term in text for term in ("high", "快", "强", "bold", "promo", "energy", "active", "dynamic", "flash")):
        return "energetic"
    if plan_summary and str(plan_summary.get("title") or "").lower().find("doc") >= 0:
        return "calm"
    return "balanced"


def _infer_density(motion_elements, subtitle, overlay):
    item_count = len(_coerce_list(motion_elements))
    if overlay and isinstance(overlay, dict):
        if overlay.get("style") in {"data_card", "bullet_list", "title_reveal", "keyword_flash"}:
            item_count += 1
    if subtitle and subtitle.get("text"):
        item_count += 1
    if item_count >= 3:
        return "high"
    if item_count == 2:
        return "medium"
    return "low"


def _clean_motion_elements(shot):
    return _compact_list(_coerce_list(shot.get("motion_elements")))


def _is_design_board_contract(shot=None, plan_summary=None):
    shot = shot if isinstance(shot, dict) else {}
    plan_summary = plan_summary if isinstance(plan_summary, dict) else {}
    values = [
        shot.get("storyboard_role"), shot.get("storyboard_usage"),
        shot.get("storyboard_contract"), plan_summary.get("storyboard_role"),
        plan_summary.get("storyboard_usage"), plan_summary.get("scene_type"),
        plan_summary.get("source_ppt"), plan_summary.get("source_doc"),
    ]
    blob = " ".join(
        json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value or "")
        for value in values
    ).lower().replace("-", "_")
    explicit = any(token in blob for token in (
        "design_board", "motion_design_board", "layout_board", "customer_preview",
        "document_explainer", "ppt_explainer",
    ))
    document_source = any(plan_summary.get(key) or shot.get(key)
                          for key in ("source_ppt", "source_doc", "source_pdf", "slide_images_dir"))
    document_words = any(token in blob for token in (
        "ppt", "powerpoint", "document", "文档", "方案", "内容页", "讲解",
    ))
    return explicit or (document_source and document_words)


def _shot_ref_tags(shot, prefixes=()):
    tags = shot.get("ref_tags") or shot.get("video_ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(tag) for tag in tags if tag]
    if not prefixes:
        return list(dict.fromkeys(tags))
    return list(dict.fromkeys(
        tag for tag in tags if any(tag.lower().startswith(prefix) for prefix in prefixes)
    ))


def _design_board_asset_slots(shot, motion_plan, plan_summary=None):
    """Build the render-time layout contract for a document explainer.

    Slots deliberately carry no generated text requirement.  ``renderable``
    tells HyperFrames whether a slot is an actual overlay track; PPT, PIP and
    safe-zone slots are consumed by Remotion/fuse as layout anchors instead.
    """
    shot = shot if isinstance(shot, dict) else {}
    plan_summary = plan_summary if isinstance(plan_summary, dict) else {}
    motion_plan = motion_plan if isinstance(motion_plan, dict) else {}
    refs = _shot_ref_tags(shot)
    ppt_refs = [tag for tag in refs if tag.lower().startswith(("@ppt", "@slide", "@doc"))]
    if not ppt_refs:
        ppt_refs = [str(item) for item in _coerce_list(
            shot.get("source_refs") or shot.get("slide_range") or shot.get("source_slides")) if item]
    avatar_refs = [tag for tag in refs if any(token in tag.lower() for token in ("host", "avatar", "presenter", "character"))]
    slide_image = shot.get("slide_image") or shot.get("content_image")
    source_document = (shot.get("source_ppt") or shot.get("source_doc") or
                       plan_summary.get("source_ppt") or plan_summary.get("source_doc"))
    has_human = bool(shot.get("characters") or shot.get("character_prompt") or avatar_refs)
    explicit_title = shot.get("title") or shot.get("heading") or shot.get("content_title")
    explicit_highlight = shot.get("highlight") or shot.get("highlights") or shot.get("animation_hint")
    motion_text = " ".join(map(str, _clean_motion_elements(shot))).lower()
    annotation_hint = (shot.get("annotation") or shot.get("chart") or
                       shot.get("arrow") or any(token in motion_text for token in (
                           "chart", "arrow", "annotat", "图表", "箭头", "标注", "流程")))
    safe_zone = motion_plan.get("safe_zone") or "lower_third"
    timing = motion_plan.get("timing") or "steady"

    slots = [
        {
            "slot": "ppt_page", "kind": "content_surface", "layer": "remotion",
            "owner": "remotion", "role": "main_visual", "safe_zone": "center",
            "ref_tags": ppt_refs, "source": slide_image or source_document,
            "required": True, "enabled": bool(slide_image or source_document or ppt_refs),
            "renderable": False, "timing": "全程",
            "description": "PPT/content page surface bound to the approved source material",
        },
        {
            "slot": "corner_pip", "kind": "avatar_plate", "layer": "fuse",
            "owner": "remotion/fuse", "role": "presenter_window", "safe_zone": "corner",
            "ref_tags": avatar_refs, "source": "video_model.avatar_plate",
            "required": has_human, "enabled": has_human, "renderable": False,
            "timing": "全程",
            "description": "digital human presenter plate; lock face, voice, wardrobe and background",
        },
        {
            "slot": "title_area", "kind": "title_zone", "layer": "hyperframes",
            "owner": "hyperframes", "role": "hierarchy_anchor",
            "safe_zone": shot.get("title_safe_zone") or "upper_third",
            "required": True, "enabled": bool(explicit_title), "renderable": bool(explicit_title),
            "timing": "进场0.5s后", "description": explicit_title or "reserved title/chapter heading area",
        },
        {
            "slot": "safe_zone", "kind": "layout_guard", "layer": "composite",
            "owner": "remotion/fuse", "role": "do_not_cover_content", "safe_zone": safe_zone,
            "required": True, "enabled": True, "renderable": False, "timing": "全程",
            "geometry": (SAFE_ZONES.get(safe_zone) or {}).get("video_region") or {},
            "description": "reserved readable zones for captions, cards and presenter window",
        },
        {
            "slot": "highlight_card", "kind": "callout_card", "layer": "hyperframes",
            "owner": "hyperframes", "role": "emphasis",
            "safe_zone": shot.get("highlight_safe_zone") or "right",
            "required": True, "enabled": bool(explicit_highlight),
            "renderable": bool(explicit_highlight), "timing": timing,
            "description": explicit_highlight or "reserved key highlight card/emphasis block",
        },
    ]
    for slot_name, label in (("chart", "chart position"), ("arrow", "arrow path"), ("annotation", "annotation label position")):
        value = shot.get(slot_name)
        slots.append({
            "slot": slot_name, "kind": slot_name, "layer": "hyperframes",
            "owner": "hyperframes", "role": label, "safe_zone": shot.get("annotation_safe_zone") or "left",
            # A motion-direction hint such as “流程线” reserves geometry but is
            # not audience copy.  Rendering the fallback labels (“chart
            # position”, “arrow path”...) leaked production notes into the
            # customer Animatic.  Only an explicitly authored slot value may
            # become a visible HyperFrames element.
            "required": True, "enabled": bool(value), "renderable": bool(value),
            "timing": timing, "description": str(value or shot.get("content_design") or label),
        })
    # Backward-compatible grouped address used by older composite previews.
    slots.append({
        "slot": "chart_arrow_annotation", "kind": "annotation_group", "layer": "hyperframes",
        "owner": "hyperframes", "role": "chart_arrow_label_position",
        "safe_zone": shot.get("annotation_safe_zone") or "left", "required": True,
        "enabled": bool(annotation_hint), "renderable": False, "timing": timing,
        "description": str(shot.get("annotation") or shot.get("content_design") or
                            "chart, arrow, marker and annotation positions"),
        "children": ["chart", "arrow", "annotation"],
    })
    return slots


def _merge_asset_slots(base_slots, extra_slots):
    by_slot = {}
    for item in list(base_slots or []) + list(extra_slots or []):
        if not isinstance(item, dict) or not item.get("slot"):
            continue
        slot = str(item["slot"])
        by_slot[slot] = dict(by_slot.get(slot, {}), **item)
    return list(by_slot.values())


def _synchronize_subtitle_slot(slots, subtitle):
    """Make generated subtitle slots mirror the one authored subtitle source.

    ``asset_slots`` and ``motion_plan.asset_slots`` are execution derivatives,
    not independent copy-editing surfaces. Preserve extension metadata on an
    existing slot, while overwriting every compiler-owned text/typography field
    so an older injected snapshot cannot win during a later merge.
    """
    slots = [dict(item) if isinstance(item, dict) else item for item in (slots or [])]
    if not isinstance(subtitle, dict) or not str(subtitle.get("text") or "").strip():
        return slots
    text = str(subtitle.get("text") or "").strip()
    typography = dict(subtitle.get("typography") or {})
    target = next((item for item in slots
                   if isinstance(item, dict) and item.get("slot") == "subtitle"), None)
    if target is None:
        target = {"slot": "subtitle", "kind": "subtitle", "layer": "hyperframes"}
        slots.append(target)
    target.update({
        "kind": "subtitle",
        "layer": "hyperframes",
        "safe_zone": subtitle.get("safe_zone", "lower_third"),
        "timing": "spoken",
        "description": text,
        "text": text,
        "typography": typography,
        "size_px": typography.get("size_px"),
        "max_width_px": typography.get("max_width_px"),
        "max_lines": typography.get("max_lines"),
        "preset": typography.get("preset"),
        "emphasis": typography.get("emphasis") or [],
    })
    binding = target.get("asset_binding")
    if isinstance(binding, dict):
        binding = dict(binding)
        binding["source"] = text
        target["asset_binding"] = binding
    return slots


def _explicit_motion_overlay(shot):
    """Return viewer-facing overlay copy only when it was explicitly authored.

    ``motion_elements`` are production directions.  Older plans inferred a
    title and bullets from those directions, which made notes such as camera or
    layout instructions visible in the customer render.  This adapter accepts
    only the dedicated overlay fields and deliberately has no legacy fallback.
    """
    shot = shot if isinstance(shot, dict) else {}
    embedded = shot.get("motion_design") if isinstance(
        shot.get("motion_design"), dict) else {}
    candidates = [embedded.get("motion_overlay"), shot.get("motion_overlay")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        overlay = dict(candidate)
        title = str(overlay.get("title") or "").strip()
        bullets = _compact_list(overlay.get("bullets"))
        metric = overlay.get("metric") if isinstance(overlay.get("metric"), dict) else None
        has_copy = bool(title or bullets or (metric and (
            metric.get("value") or metric.get("label"))))
        if str(overlay.get("style") or "").lower() == "none" or not has_copy:
            return {"style": "none", "safe_zone": None, "reason": overlay.get("reason") or ""}
        if not overlay.get("style"):
            overlay["style"] = "data_card" if metric else (
                "bullet_list" if bullets else "title_reveal")
        overlay["title"] = title
        overlay["bullets"] = bullets
        overlay["source_kind"] = "audience_copy"
        overlay["owner"] = "hyperframes"
        return overlay
    return {"style": "none", "safe_zone": None, "reason": "no_explicit_audience_copy"}


def _copy_sha256(value):
    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()


def _annotate_asset_slot(slot, shot_id=None):
    """Attach source/owner metadata used by the render-time visibility gate."""
    slot = dict(slot) if isinstance(slot, dict) else {}
    name = str(slot.get("slot") or "")
    text = str(slot.get("text") or slot.get("description") or "").strip()
    if name.startswith("motion_element_"):
        source_kind = "director_instruction"
        slot.update({"owner": "none", "renderable": False})
    elif name in {"safe_zone", "ppt_page", "corner_pip", "chart_arrow_annotation"}:
        source_kind = "layout_reservation"
        slot["renderable"] = False
    elif name in {"subtitle", "motion_overlay", "motion_overlay_metric"} or name.startswith(
            "motion_overlay_bullet_"):
        source_kind = "audience_copy"
        slot.setdefault("owner", "hyperframes")
    elif name in {"title_area", "highlight_card", "chart", "arrow", "annotation"}:
        source_kind = "audience_copy" if slot.get("renderable") and text else "layout_reservation"
        slot.setdefault("owner", "hyperframes")
    else:
        source_kind = str(slot.get("source_kind") or "layout_reservation")
    slot["source_kind"] = source_kind
    slot.setdefault("semantic_id", "%s.%s" % (shot_id or "shot", name or "slot"))
    if source_kind == "audience_copy" and text:
        slot["copy_sha256"] = _copy_sha256(text)
    slot.setdefault("collision_policy", "reserved_regions")
    return slot


def _canonicalize_asset_slots(slots, canonical_slots, shot_id=None):
    """Remove stale compiled copy and normalize every remaining slot."""
    canonical_names = {
        str(item.get("slot")) for item in (canonical_slots or [])
        if isinstance(item, dict) and item.get("slot")
    }
    visible_prefixes = ("motion_overlay_bullet_",)
    visible_names = {"motion_overlay", "motion_overlay_metric"}
    normalized = []
    for item in slots or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("slot") or "")
        is_overlay_slot = name in visible_names or name.startswith(visible_prefixes)
        if is_overlay_slot and name not in canonical_names:
            continue
        normalized.append(_annotate_asset_slot(item, shot_id=shot_id))
    return normalized


def compact_subtitle_text(value, max_chars=38):
    """Create an explicit preview caption without cutting an ASCII word.

    The deterministic design board shows a compact spoken-caption sample, not
    the full final subtitle track.  Legacy code used ``[:20]`` and could turn
    ``basicrouter`` into ``basicrout``.  This helper keeps the preview compact,
    prefers a semantic word boundary, and marks every truncation with an
    ellipsis so QC can distinguish an intentional excerpt from lost copy.
    """
    text = re.sub(r"[。！？!?；;，,、\s]+", " ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or len(text) <= max_chars:
        return text
    max_chars = max(8, int(max_chars))
    prefix = text[:max_chars].rstrip()
    next_char = text[len(prefix):len(prefix) + 1]
    # Never split an ASCII identifier/brand word. Drop the partial token when
    # a safe boundary exists; Chinese text may still use a character boundary.
    if (prefix and next_char and prefix[-1].isascii() and prefix[-1].isalnum() and
            next_char.isascii() and next_char.isalnum()):
        boundary = prefix.rfind(" ")
        if boundary >= max(4, int(max_chars * 0.45)):
            prefix = prefix[:boundary].rstrip()
    else:
        boundary = prefix.rfind(" ")
        if boundary >= int(max_chars * 0.62):
            prefix = prefix[:boundary].rstrip()
    return (prefix or text[:max_chars - 1].rstrip()) + "…"


def _build_asset_slots(subtitle, overlay, motion_elements, motion_plan,
                       shot=None, plan_summary=None):
    slots = []
    if _is_design_board_contract(shot, plan_summary):
        slots.extend(_design_board_asset_slots(shot, motion_plan, plan_summary))
    if subtitle:
        typography = dict(subtitle.get("typography") or {})
        slots.append({
            "slot": "subtitle",
            "kind": "subtitle",
            "layer": "hyperframes",
            "owner": "hyperframes",
            "source_kind": "audience_copy",
            "renderable": True,
            "safe_zone": subtitle.get("safe_zone", "lower_third"),
            "timing": "spoken",
            "description": subtitle.get("text", ""),
            "text": subtitle.get("text", ""),
            "typography": typography,
            "size_px": typography.get("size_px"),
            "max_width_px": typography.get("max_width_px"),
            "max_lines": typography.get("max_lines"),
            "preset": typography.get("preset"),
            "emphasis": typography.get("emphasis") or [],
        })
    if overlay and overlay.get("style") != "none":
        slots.append({
            "slot": "motion_overlay",
            "kind": overlay.get("style"),
            "layer": "hyperframes",
            "owner": "hyperframes",
            "source_kind": "audience_copy",
            "renderable": True,
            "exclusive_owner": bool(overlay.get("exclusive_owner", False)),
            "collision_policy": overlay.get("collision_policy") or "reserved_regions",
            "safe_zone": overlay.get("safe_zone"),
            "timing": overlay.get("timing") or motion_plan.get("timing") or "全程",
            "description": overlay.get("title") or overlay.get("reason") or "",
        })
        for index, bullet in enumerate(_compact_list(overlay.get("bullets"))[:4], 1):
            slots.append({
                "slot": "motion_overlay_bullet_%d" % index,
                "kind": "bullet",
                "layer": "hyperframes",
                "owner": "hyperframes",
                "source_kind": "audience_copy",
                "renderable": True,
                "exclusive_owner": bool(overlay.get("exclusive_owner", False)),
                "collision_policy": overlay.get("collision_policy") or "reserved_regions",
                "safe_zone": overlay.get("safe_zone"),
                "timing": overlay.get("timing") or motion_plan.get("timing") or "全程",
                "description": bullet,
            })
        metric = overlay.get("metric") if isinstance(overlay.get("metric"), dict) else None
        if metric and (metric.get("value") or metric.get("label")):
            slots.append({
                "slot": "motion_overlay_metric",
                "kind": "metric",
                "layer": "hyperframes",
                "owner": "hyperframes",
                "source_kind": "audience_copy",
                "renderable": True,
                "exclusive_owner": bool(overlay.get("exclusive_owner", False)),
                "collision_policy": overlay.get("collision_policy") or "reserved_regions",
                "safe_zone": overlay.get("safe_zone"),
                "timing": overlay.get("timing") or motion_plan.get("timing") or "全程",
                "description": "%s %s" % (metric.get("value", ""), metric.get("label", "")),
            })
    for index, element in enumerate(_compact_list(motion_elements), 1):
        slots.append({
            "slot": "motion_element_%d" % index,
            "kind": "motion_element",
            "layer": "hyperframes",
            # ``motion_elements`` are choreography/director instructions.  They
            # stay in the semantic grammar for deterministic motion planning,
            # but must never be painted as viewer-facing cards.  Audience copy
            # is carried by the explicit motion_overlay/subtitle slots above.
            "owner": "none",
            "source_kind": "director_instruction",
            "renderable": False,
            "safe_zone": motion_plan.get("safe_zone") or (subtitle or {}).get("safe_zone") or (overlay or {}).get("safe_zone"),
            "timing": motion_plan.get("timing") or "全程",
            "description": str(element),
        })
    return [_annotate_asset_slot(item, shot_id=(shot or {}).get("id"))
            for item in _merge_asset_slots([], slots)]


def _motion_plan_from_legacy(shot, plan_summary=None):
    subtitle_text = ""
    dialogue = str(shot.get("dialogue") or shot.get("voiceover") or "").strip()
    if dialogue:
        subtitle_text = compact_subtitle_text(dialogue)
    subtitle = None
    if subtitle_text:
        subtitle = {
            "text": subtitle_text,
            "position": "lower_third",
            "style": "subtitle",
            "safe_zone": "lower_third",
            "typography": {
                "role": "spoken",
                "size_px": 44,
                "max_width_px": 960,
                "max_lines": 2,
                "emphasis": [],
                "preset": "fade_up",
            },
        }
    motion_elements = _clean_motion_elements(shot)
    # Audience copy must come from the dedicated authored field.  Production
    # directions in ``motion_elements`` still drive choreography below, but
    # are never promoted into a title, bullet, metric or card.
    overlay = _explicit_motion_overlay(shot)
    motion_style = str(overlay.get("style") or "none")
    motion_title = str(overlay.get("title") or "").strip()
    personality = _detect_motion_personality(shot, plan_summary)
    density = _infer_density(motion_elements, subtitle, overlay)
    preset = (subtitle.get("typography", {}) or {}).get("preset") if subtitle else None
    if not preset:
        preset = "fade_up" if personality != "energetic" else "pop"
    safe_zones = _compact_list([subtitle.get("safe_zone") if subtitle else None, overlay.get("safe_zone") if overlay.get("style") != "none" else None])
    if not safe_zones:
        safe_zones = ["center"]
    motion_plan = {
        "schema_version": MOTION_PLAN_SCHEMA_VERSION,
        "motion_contract_version": MOTION_CONTRACT_VERSION,
        "personality": personality,
        "primary": MOTION_PERSONALITY_PRESETS.get(personality, MOTION_PERSONALITY_PRESETS["balanced"])["primary"],
        "secondary": MOTION_PERSONALITY_PRESETS.get(personality, MOTION_PERSONALITY_PRESETS["balanced"])["secondary"],
        "ambient": MOTION_PERSONALITY_PRESETS.get(personality, MOTION_PERSONALITY_PRESETS["balanced"])["ambient"],
        "density": density,
        "timing": MOTION_PERSONALITY_PRESETS.get(personality, MOTION_PERSONALITY_PRESETS["balanced"])["timing"],
        "safe_zone": safe_zones[0],
        "a11y": {
            "subtitle_required": bool(subtitle),
            "max_lines": (subtitle or {}).get("typography", {}).get("max_lines", 2),
            "contrast": "high",
        },
        "asset_slots": [],
        "motion_elements": motion_elements,
        "preset": preset,
    }
    identity = _motion_identity(personality)
    motion_plan["motion_identity"] = identity
    motion_plan["choreography"] = {
        "hero_slot": "title_area" if motion_title else "motion_element_1",
        "primary_layer": "primary",
        "secondary_layer": "secondary",
        "ambient_layer": "ambient",
        "stagger_budget_ms": identity["stagger_budget_ms"],
        "active_motion_limit": identity["active_motion_limit"],
        "narrative": "setup_action_resolution",
    }
    motion_plan["asset_slots"] = _build_asset_slots(
        subtitle, overlay, motion_elements, motion_plan,
        shot=shot, plan_summary=plan_summary)
    for slot in motion_plan["asset_slots"]:
        slot["asset_binding"] = _slot_asset_binding(slot, shot=shot, plan_summary=plan_summary)
        requirements = _normalize_asset_requirements(slot)
        if requirements:
            slot["asset_requirements"] = requirements
    motion_plan["asset_requirements"] = _collect_asset_requirements(
        motion_plan.get("asset_slots"))
    motion_plan["execution_plan"] = _offline_execution_plan(
        shot, motion_plan, shot.get("duration") or shot.get("duration_sec") or 5)
    _attach_base_plate_execution_contract(motion_plan["execution_plan"], shot, plan_summary)
    _validate_execution_plan(
        motion_plan["execution_plan"], "shot[%s].execution_plan" % (shot.get("id") or "unknown"),
        shot_duration=shot.get("duration") or shot.get("duration_sec") or 5)
    shot_design = {
        "shot_id": shot.get("id"),
        "subtitle": subtitle,
        "motion_overlay": overlay if motion_style != "none" else {
            "style": "none", "safe_zone": None,
            "reason": overlay.get("reason") or "no_explicit_audience_copy"},
        "video_safe_zones": safe_zones,
        "director_note": shot.get("director_note") or shot.get("visual") or "",
        "motion_plan": motion_plan,
        "asset_slots": motion_plan["asset_slots"],
        "motion_elements": motion_elements,
    }
    return shot_design


def _offline_execution_plan(shot, motion_plan, duration):
    """Compile a deterministic execution contract for offline validation.

    This is intentionally a conservative fallback: it preserves the authored
    storyboard and gives both renderers concrete tracks, without pretending to
    be new LLM creative direction.
    """
    duration = max(0.1, float(duration or 1))
    personality = str((motion_plan or {}).get("personality") or "balanced")
    identity = _motion_identity(personality)
    style = str(((motion_plan or {}).get("motion_overlay") or {}).get("style") or
                (motion_plan or {}).get("preset") or "fade_up")
    if style in {"pop", "keyword_flash"}:
        content_from = {"x": 28, "y": 0, "scale": 0.97}
        content_to = {"x": 0, "y": 0, "scale": 1}
    elif personality == "calm":
        content_from = {"x": 0, "y": 12, "scale": 0.985}
        content_to = {"x": 0, "y": 0, "scale": 1}
    else:
        content_from = {"x": -36, "y": 0, "scale": 0.97}
        content_to = {"x": 0, "y": 0, "scale": 1}
    lead = min(0.48, max(0.22, duration * 0.08))
    presenter_lead = min(0.42, max(0.2, duration * 0.07))
    graphics_lead = min(0.36, max(0.18, duration * 0.06))
    signature = identity.get("signature_easing") or "cubic-bezier(0.2,0,0,1)"
    return {
        "version": EXECUTION_PLAN_VERSION,
        "source": "offline_fallback_from_confirmed_storyboard",
        "render_order": ["content_stage", "presenter_plate", "foreground_graphics"],
        "tracks": [
            {
                "target": "content_stage", "executor": "remotion",
                "purpose": "按设计板节奏编排确认的 PPT 内容页，并执行入场位移与轻微缩放",
                "timeline": {"start": 0.0, "end": duration},
                "parameters": {
                    "visual_mode": "content_full_bleed",
                    "source_role": "confirmed_slide",
                    "anchor": "center", "fit": "contain", "mask": "none",
                    "blend_mode": "normal", "transition": "fade",
                },
                "keyframes": [
                    {"at": 0.0, "props": dict(opacity=0, **content_from), "easing": signature},
                    {"at": lead, "props": dict(opacity=1, **content_to), "easing": signature},
                ],
                "fallback": "PPT 内容缺失时保留人物视频并使用品牌纯色背景",
            },
            {
                "target": "presenter_plate", "executor": "remotion",
                "purpose": "执行已确认数字人片段的画中画位置、入场和连续身份锚定",
                "timeline": {"start": 0.0, "end": duration},
                "parameters": {
                    "layout": "corner_pip", "source_role": "video_plate",
                    "position": {"x": 0.78, "y": 0.76}, "scale": 0.42,
                    "opacity": 1, "mask": "none",
                },
                "keyframes": [
                    {"at": 0.0, "props": {"opacity": 0, "x": 24, "y": 18, "scale": 0.96}, "easing": "power3.out"},
                    {"at": presenter_lead, "props": {"opacity": 1, "x": 0, "y": 0, "scale": 1}, "easing": "power3.out"},
                ],
                "fallback": "隐藏人物层并保持内容页完整",
            },
            {
                "target": "foreground_graphics", "executor": "hyperframes",
                "purpose": "在透明 alpha 层执行字幕、标题和后期图形的分层出现",
                "timeline": {"start": 0.0, "end": duration},
                "parameters": {
                    "layer": "transparent_alpha",
                    "safe_zone": (motion_plan or {}).get("safe_zone") or "lower_third",
                    "style": style, "font_size_px": 44, "max_width_px": 960,
                    "paired_slots": ["subtitle", "motion_overlay", "motion_element_1"],
                },
                "keyframes": [
                    {"at": 0.0, "props": {"opacity": 0, "y": 18, "scale": 0.98}, "easing": "power3.out"},
                    {"at": graphics_lead, "props": {"opacity": 1, "y": 0, "scale": 1}, "easing": "power3.out"},
                ],
                "fallback": "不渲染透明图形层，严禁把文字写入底片或视频模型 prompt",
            },
        ],
    }


def ensure_motion_plan_contract(shot, plan_summary=None):
    """Return a normalized structured MotionPlan for one render shot.

    This public adapter lets older document plans enter the same composite
    contract without asking the customer to regenerate a design board solely
    because the JSON predates structured asset slots.
    """
    shot = shot if isinstance(shot, dict) else {}
    plan_summary = plan_summary if isinstance(plan_summary, dict) else {}
    fallback = _motion_plan_from_legacy(shot, plan_summary)
    authored = shot.get("motion_plan") if isinstance(shot.get("motion_plan"), dict) else {}
    motion_plan = dict(fallback.get("motion_plan") or {}, **authored)
    authored_slots = shot.get("asset_slots") if isinstance(shot.get("asset_slots"), list) else []
    embedded_design = shot.get("motion_design") if isinstance(
        shot.get("motion_design"), dict) else {}
    canonical_subtitle = embedded_design.get("subtitle") if isinstance(
        embedded_design.get("subtitle"), dict) else fallback.get("subtitle")
    canonical_overlay = _explicit_motion_overlay(shot)
    canonical_slots = _build_asset_slots(
        canonical_subtitle, canonical_overlay, _clean_motion_elements(shot), motion_plan,
        shot=shot, plan_summary=plan_summary)
    motion_plan["asset_slots"] = _merge_asset_slots(
        canonical_slots, authored_slots or authored.get("asset_slots") or [])
    motion_plan["asset_slots"] = _canonicalize_asset_slots(
        motion_plan["asset_slots"], canonical_slots, shot_id=shot.get("id"))
    motion_plan["asset_slots"] = _synchronize_subtitle_slot(
        motion_plan["asset_slots"], canonical_subtitle)
    motion_plan["schema_version"] = MOTION_PLAN_SCHEMA_VERSION
    for key in ("personality", "primary", "secondary", "ambient", "density", "timing", "safe_zone"):
        if not motion_plan.get(key):
            motion_plan[key] = fallback["motion_plan"].get(key)
    motion_plan["a11y"] = dict(fallback["motion_plan"].get("a11y") or {}, **(
        authored.get("a11y") if isinstance(authored.get("a11y"), dict) else {}))
    personality = str(motion_plan.get("personality") or "balanced")
    motion_plan["motion_contract_version"] = MOTION_CONTRACT_VERSION
    motion_plan.setdefault("motion_identity", _motion_identity(personality))
    identity = motion_plan["motion_identity"]
    if not isinstance(identity, dict):
        identity = _motion_identity(personality)
        motion_plan["motion_identity"] = identity
    choreography = motion_plan.get("choreography")
    if not isinstance(choreography, dict):
        choreography = {}
    choreography.setdefault("hero_slot", "title_area")
    choreography.setdefault("primary_layer", "primary")
    choreography.setdefault("secondary_layer", "secondary")
    choreography.setdefault("ambient_layer", "ambient")
    choreography.setdefault("stagger_budget_ms", identity.get("stagger_budget_ms", 360))
    choreography.setdefault("active_motion_limit", identity.get("active_motion_limit", 0.33))
    choreography.setdefault("narrative", "setup_action_resolution")
    motion_plan["choreography"] = choreography
    if not isinstance(motion_plan.get("execution_plan"), dict):
        motion_plan["execution_plan"] = _offline_execution_plan(
            shot, motion_plan, shot.get("duration") or shot.get("duration_sec") or 5)
    _attach_base_plate_execution_contract(motion_plan["execution_plan"], shot, plan_summary)
    _validate_execution_plan(
        motion_plan["execution_plan"], "shot[%s].execution_plan" % (shot.get("id") or "unknown"),
        shot_duration=shot.get("duration") or shot.get("duration_sec") or 5)
    for slot in motion_plan.get("asset_slots") or []:
        if isinstance(slot, dict):
            normalized_slot = _annotate_asset_slot(slot, shot_id=shot.get("id"))
            slot.clear()
            slot.update(normalized_slot)
            slot.setdefault("asset_binding", _slot_asset_binding(slot, shot=shot, plan_summary=plan_summary))
    return motion_plan


# ── LLM 动效设计提示词 ───────────────────────────────────────────────

DESIGN_PROMPT = """你是一位资深广告导演、动态图形设计师和剪辑师。请根据以下分镜脚本，为每个镜头创作「可执行但不模板化的字幕与动效设计方案」。

你负责设计和灵感，HyperFrames、Remotion、ffmpeg 只是执行层：不要让执行层的既有 preset 限制你的创意。你的方案必须先回答观众在这一拍应该感受到什么，再决定运动、层级、节奏和素材需求。

【设计参考原则，吸收方法不要照抄模板】
1. 运动人格：为整条片选择一个主人格（premium / corporate / energetic / playful），并定义 signature easing、quick/standard/slow 三档时长、统一入场语法。
2. 三层动效：每个需要动效的镜头分别考虑 primary（观众主视线）、secondary（跟随/信息支撑）、ambient（环境生命感）；不需要的层明确关闭。
3. 动效叙事：把每个镜头写成 setup → action → resolution；使用 anticipation、staging、follow-through、secondary action 等原则，让动效有因果，不是随机飞入。
4. 编排克制：hero 先行；同一时刻不要让所有元素一起动；3 个以上元素遵守约三分之一同时运动规则；总 stagger 不超过镜头可感知节奏。
5. 三种审美透镜要按场景加权：高频信息界面优先 restraint & speed，商业成片优先 production polish，品牌/年轻化表达才选择性加入 creative experimentation。
6. 反模板化：禁止每镜头统一 fade-up、无意义 pulse、所有卡片同时 stagger、所有元素 hover/scale 式装饰；每个动作必须有叙事理由。
7. 物理/材质语义：纸张、玻璃、金属、液体、数据流可以有不同的重量、惯性和缓动，但只在能帮助理解时使用。

【设计板与配套素材的边界】
- design_board 是客户确认的合成设计合同，不是视频模型 storyboard_ref。
- 设计板上出现的 PPT 页、产品局部、人物小窗、界面截图、图表底图等，必须在 asset_requirements 中声明对应素材来源、生成/导入方式、身份锚点和确认状态；不能只写“后期叠加”。
- 文字、图表、箭头、标签、光效和动效形状由 HyperFrames/Remotion 确定性合成；底片生成模型只生成干净的人物/场景/产品画面。
- 如果动效依赖某个视觉对象（例如产品局部被高亮、人物手部引导箭头、PPT 某块区域被扫描），必须同时设计该对象的配套素材，而不是让设计板凭空占位。
- video_storyboard 才是视频模型的分镜参考；design_board 仅继承已确认的人物、产品、场景和声音身份锚。

【核心原则】
1. 字幕和动效必须与画面内容融为一体，不是后期贴上去的
2. 每个镜头的构图必须预留文字叠加的安全区
3. 字幕文本要精炼有力，不是台词的简单重复
4. 动效要服务叙事节奏，不是越多越好

【输入】分镜脚本（JSON）：
{plan_json}

【输出格式】严格输出 JSON（不要其他文字）：
{{
  "version": 1,
  "global_style": {{
    "font_family": "品牌字体或默认",
    "primary_color": "#RRGGBB",
    "subtitle_position": "lower_third",
    "motion_density": "low|medium|high"
  }},
  "shots": [
    {{
      "shot_id": "s1",
      "subtitle": {{
        "text": "精炼字幕文本（≤38字，无标点，优先保留完整语义）",
        "position": "lower_third",
        "style": "subtitle",
        "safe_zone": "lower_third",
        "typography": {{
          "role": "spoken|hook|proof|cta",
          "size_px": 52,
          "max_width_px": 1220,
          "max_lines": 2,
          "emphasis": ["需要强调的关键词"],
          "preset": "fade_up|pop|slide_left|slide_right|none"
        }}
      }},
      "motion_overlay": {{
        "style": "title_reveal|bullet_list|metric_pop|data_card|lower_third|keyword_flash|none",
        "title": "动效标题（如有）",
        "bullets": ["要点1", "要点2"],
        "metric": {{"value": "数值", "label": "标签"}},
        "position": "center|top|bottom|lower_third|left|right|corner",
        "safe_zone": "center|upper_third|lower_third|corner|left|right",
        "timing": "进场0.5s后|全程|结尾强调",
        "preset": "pop|slide_left|slide_right|fade_up|none",
        "size_px": 42,
        "width_px": 560,
        "reason": "为什么这里需要这个动效（一句话）"
      }},
      "motion_plan": {{
        "schema_version": 2,
        "personality": "calm|balanced|energetic",
        "primary": "low|medium|high",
        "secondary": "low|medium|high",
        "ambient": "low|medium|high",
        "density": "low|medium|high",
        "timing": "slow|steady|quick",
        "safe_zone": "lower_third|upper_third|center|corner|left|right",
        "a11y": {{"subtitle_required": true, "max_lines": 2, "contrast": "high"}},
        "motion_identity": {{
          "archetype": "premium|corporate|energetic|playful",
          "primary_lens": "restraint|production_polish|creative_experimentation",
          "secondary_lens": "restraint|production_polish|creative_experimentation",
          "selective_lens": "restraint|production_polish|creative_experimentation",
          "signature_easing": "具体 easing 曲线或 spring 参数",
          "duration_palette_ms": {{"quick": 180, "standard": 420, "slow": 720}},
          "entrance_pattern": "整片统一的入场语法"
        }},
        "choreography": {{
          "hero_slot": "主视线元素",
          "primary_layer": "primary",
          "secondary_layer": "secondary",
          "ambient_layer": "ambient",
          "stagger_budget_ms": 360,
          "active_motion_limit": 0.33,
          "narrative": "setup_action_resolution"
        }}
      }},
      "asset_slots": [
        {{"slot":"ppt_page|corner_pip|title_area|safe_zone|highlight_card|chart|arrow|annotation",
         "layer":"remotion|fuse|hyperframes|composite", "owner":"remotion|fuse|hyperframes|remotion/fuse",
         "safe_zone":"center|corner|upper_third|lower_third|left|right", "enabled": true,
         "renderable": false, "timing":"全程", "description":"该槽位的版式/内容意图",
         "asset_requirements": [{{"asset_id":"唯一标识", "role":"source|identity_anchor|paired_visual",
           "source":"confirmed_upload|generated_img2img|remotion_content|none", "required":true,
           "reference_tags":["@tag"], "generation_prompt":"若需生成，给出配套素材提示词，不含字幕文字",
           "approval":"required|inherited|not_required"}}]}}
      ],
      "execution_plan": {{
        "version": 1,
        "render_order": ["content_stage", "presenter_plate", "foreground_graphics"],
        "tracks": [
          {{
            "target": "content_stage",
            "executor": "remotion",
            "purpose": "内容页/媒体/内容动效的具体执行目的",
            "timeline": {{"start": 0.0, "end": 3.2}},
            "parameters": {{
              "visual_mode": "content_full_bleed|split_left_right|stacked_top_bottom|corner_pip",
              "source_role": "confirmed_slide|content_spec|video_plate",
              "anchor": "left|right|top|bottom|center",
              "fit": "cover|contain",
              "mask": "具体遮罩或 none",
              "blend_mode": "normal|screen|multiply",
              "camera": {{"from": {{"x": 0, "y": 0, "scale": 1}}, "to": {{"x": 0, "y": 0, "scale": 1}}}},
              "transition": "cut|fade|slide|match_cut"
            }},
            "keyframes": [
              {{"at": 0.0, "props": {{"opacity": 0, "x": -48, "y": 0, "scale": 0.96}}, "easing": "cubic-bezier(0.2,0,0,1)"}},
              {{"at": 0.42, "props": {{"opacity": 1, "x": 0, "y": 0, "scale": 1}}, "easing": "cubic-bezier(0.2,0,0,1)"}}
            ],
            "fallback": "若内容层缺失，使用已确认视频/纯色背景并保留构图安全区"
          }},
          {{
            "target": "presenter_plate",
            "executor": "remotion",
            "purpose": "数字人视频层的画中画/全屏/遮罩/聚焦执行",
            "timeline": {{"start": 0.0, "end": 3.2}},
            "parameters": {{"layout": "corner_pip|full_bleed|cutout", "position": {{"x": 0.78, "y": 0.76}}, "scale": 0.42, "opacity": 1, "mask": "none|ellipse|gradient"}},
            "keyframes": [
              {{"at": 0.0, "props": {{"opacity": 0, "x": 24, "y": 18, "scale": 0.96}}, "easing": "power3.out"}},
              {{"at": 0.36, "props": {{"opacity": 1, "x": 0, "y": 0, "scale": 1}}, "easing": "power3.out"}}
            ],
            "fallback": "隐藏 presenter_plate，保持内容层完整"
          }},
          {{
            "target": "foreground_graphics",
            "executor": "hyperframes",
            "purpose": "字幕/标题/图表/箭头/高亮等透明后期层",
            "timeline": {{"start": 0.0, "end": 3.2}},
            "parameters": {{"layer": "transparent_alpha", "safe_zone": "upper_third|lower_third|left|right|center", "style": "具体视觉处理", "font_size_px": 52, "max_width_px": 900}},
            "keyframes": [
              {{"at": 0.0, "props": {{"opacity": 0, "y": 18, "scale": 0.98}}, "easing": "power3.out"}},
              {{"at": 0.32, "props": {{"opacity": 1, "y": 0, "scale": 1}}, "easing": "power3.out"}}
            ],
            "fallback": "不渲染该图形层，不能把文字写入底片生成 prompt"
          }}
        ]
      }},
      "video_safe_zones": ["lower_third"],
      "director_note": "导演备注：这个镜头的动效如何服务叙事"
    }}
  ]
}}

【硬性要求】
- subtitle.text 不超过 38 字，去掉语气词和标点，优先保留完整语义
- 每个镜头最多 1 个字幕 + 1 个主动效，复杂信息拆为 asset_slots，不在底片里生成
- 主字幕不是每镜头固定同一个字号：hook/cta 可放大，长台词必须缩小或分两行；字号必须在 38–72px（基于1920x1080）
- 主字幕 max_width_px 必须在 680–1320px，禁止横贯全屏；卖点卡 width_px 必须在 320–620px
- 相邻镜头不要连续使用同一个 preset；字幕与卖点卡应使用不同 track_index，避免互相遮挡
- preset、字号、宽度和强调词必须能直接用于后期渲染，不能只写导演描述
- 不需要动效的镜头 style 填 "none"
- 口播镜头必须有 subtitle，非口播镜头可以没有
- 数据类镜头（有 metric）优先用 data_card
- 安全区必须与动效位置匹配
- 每个有动效的镜头必须提供 execution_plan；不能只写 director_note 或 preset
- execution_plan 必须同时覆盖 Remotion 的内容/人物/布局执行与 HyperFrames 的透明图形执行
- 每条 track 必须有 target、executor、purpose、timeline、parameters、至少2个带 easing 的 keyframes、fallback
- keyframes 的 at 使用相对镜头秒数；必须明确初始状态、落地状态、必要时的跟随/退出状态
- Remotion parameters 必须说明 visual_mode、素材角色、位置/缩放/遮罩/转场；HyperFrames parameters 必须说明透明层、安全区、字体/尺寸/视觉处理
- 不要把执行步骤写成自然语言长段落；参数必须能被编译器逐项读取"""


def _validate_design(design, plan_shots, require_execution=False):
    """Validate the LLM-generated motion design against the storyboard plan."""
    if not isinstance(design, dict):
        raise ValueError("MOTION_DESIGN_INVALID: 设计必须是对象")
    shots = design.get("shots") or []
    plan_ids = {str(s.get("id")) for s in plan_shots}
    design_ids = {str(s.get("shot_id")) for s in shots}
    missing = plan_ids - design_ids
    if missing:
        raise ValueError("MOTION_DESIGN_INCOMPLETE: 缺少镜头的动效设计: %s" % ", ".join(sorted(missing)))
    for shot in shots:
        sid = shot.get("shot_id")
        sub = shot.get("subtitle") or {}
        overlay = shot.get("motion_overlay") or {}
        typography = sub.get("typography") or {}
        if typography:
            size = typography.get("size_px", 52)
            width = typography.get("max_width_px", 1100)
            if not isinstance(size, (int, float)) or not 38 <= size <= 72:
                raise ValueError("MOTION_DESIGN_INVALID: subtitle.typography.size_px 必须在38-72px")
            if not isinstance(width, (int, float)) or not 680 <= width <= 1320:
                raise ValueError("MOTION_DESIGN_INVALID: subtitle.typography.max_width_px 必须在680-1320px")
            typography["size_px"] = int(size)
            typography["max_width_px"] = int(width)
            typography["max_lines"] = max(1, min(2, int(typography.get("max_lines", 2))))
            if typography.get("preset") not in (None, "fade_up", "pop", "slide_left", "slide_right", "none"):
                raise ValueError("MOTION_DESIGN_INVALID: subtitle.typography.preset 不受支持")
        if overlay.get("size_px") is not None:
            size = overlay.get("size_px")
            if not isinstance(size, (int, float)) or not 30 <= size <= 56:
                raise ValueError("MOTION_DESIGN_INVALID: motion_overlay.size_px 必须在30-56px")
            overlay["size_px"] = int(size)
        if overlay.get("width_px") is not None:
            width = overlay.get("width_px")
            if not isinstance(width, (int, float)) or not 320 <= width <= 620:
                raise ValueError("MOTION_DESIGN_INVALID: motion_overlay.width_px 必须在320-620px")
            overlay["width_px"] = int(width)
        if overlay.get("preset") not in (None, "pop", "slide_left", "slide_right", "fade_up", "none"):
            raise ValueError("MOTION_DESIGN_INVALID: motion_overlay.preset 不受支持")
        # Validate subtitle
        if sub.get("text") and len(sub["text"]) > 38:
            sub["text"] = compact_subtitle_text(sub["text"], max_chars=38)
        # Validate safe zones
        for zone in shot.get("video_safe_zones") or []:
            if zone not in SAFE_ZONES:
                shot["video_safe_zones"] = [z for z in shot["video_safe_zones"] if z in SAFE_ZONES]
                break
        # Validate overlay style
        style = overlay.get("style", "none")
        if style not in ("title_reveal", "bullet_list", "metric_pop", "data_card",
                         "lower_third", "keyword_flash", "none"):
            overlay["style"] = "none"
        motion_plan = shot.get("motion_plan")
        if motion_plan is not None and not isinstance(motion_plan, dict):
            raise ValueError("MOTION_DESIGN_INVALID: motion_plan 必须是对象")
        if isinstance(motion_plan, dict):
            for key in ("personality", "primary", "secondary", "ambient", "density",
                        "timing", "safe_zone", "a11y"):
                if key not in motion_plan:
                    raise ValueError("MOTION_DESIGN_INVALID: motion_plan 缺少字段 %s" % key)
            if motion_plan.get("personality") not in MOTION_PERSONALITY_PRESETS:
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.personality 不受支持")
            if motion_plan.get("density") not in ("low", "medium", "high"):
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.density 不受支持")
            if motion_plan.get("safe_zone") not in SAFE_ZONES:
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.safe_zone 不受支持")
            if not isinstance(motion_plan.get("a11y"), dict):
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.a11y 必须是对象")
            slots = motion_plan.get("asset_slots")
            if slots is not None and not isinstance(slots, list):
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.asset_slots 必须是数组")
            identity = motion_plan.get("motion_identity")
            if identity is not None and not isinstance(identity, dict):
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.motion_identity 必须是对象")
            choreography = motion_plan.get("choreography")
            if choreography is not None and not isinstance(choreography, dict):
                raise ValueError("MOTION_DESIGN_INVALID: motion_plan.choreography 必须是对象")
        for slot in shot.get("asset_slots") or []:
            if not isinstance(slot, dict):
                continue
            requirements = slot.get("asset_requirements")
            if requirements is not None and not isinstance(requirements, (list, dict)):
                raise ValueError("MOTION_DESIGN_INVALID: asset_slots.asset_requirements 必须是数组或对象")
        execution = shot.get("execution_plan")
        if execution is not None:
            _validate_execution_plan(
                execution, "shot[%s].execution_plan",
                shot_duration=next((s.get("duration") or s.get("duration_sec")
                                    for s in plan_shots if str(s.get("id")) == str(sid)), None))
        elif require_execution:
            raise ValueError("MOTION_EXECUTION_MISSING: shot[%s] 缺少 execution_plan" % sid)
    return design



def _normalize_design_contract(design, plan_shots, plan_summary=None):
    """Ensure every shot carries a v2 MotionPlan and asset_slots contract."""
    by_id = {str(shot.get("id")): shot for shot in plan_shots}
    for shot_design in design.get("shots") or []:
        sid = str(shot_design.get("shot_id") or "")
        source_shot = by_id.get(sid) or {}
        fallback = _motion_plan_from_legacy(source_shot, plan_summary)
        fallback_subtitle = fallback.get("subtitle")
        if isinstance(fallback_subtitle, dict):
            # The compact spoken-caption sample is derived from the current
            # approved dialogue. Preserve authored visual treatment, but never
            # let an older LLM-authored text snapshot override current copy.
            existing_subtitle = (shot_design.get("subtitle")
                                 if isinstance(shot_design.get("subtitle"), dict)
                                 else {})
            current_subtitle = dict(fallback_subtitle, **existing_subtitle)
            current_subtitle["text"] = fallback_subtitle.get("text")
            shot_design["subtitle"] = current_subtitle
        overlay = shot_design.get("motion_overlay")
        if not isinstance(overlay, dict):
            overlay = fallback.get("motion_overlay") or {"style": "none", "safe_zone": None, "reason": ""}
            shot_design["motion_overlay"] = overlay
        motion_elements = _compact_list(
            _coerce_list(shot_design.get("motion_elements")) or
            _coerce_list(source_shot.get("motion_elements")) or
            _coerce_list(fallback.get("motion_elements"))
        )
        safe_zones = _compact_list(
            _coerce_list(shot_design.get("video_safe_zones")) or
            _coerce_list(fallback.get("video_safe_zones"))
        )
        if not safe_zones:
            safe_zones = ["center"]
        motion_plan = shot_design.get("motion_plan") if isinstance(shot_design.get("motion_plan"), dict) else {}
        personality = motion_plan.get("personality") or _detect_motion_personality(source_shot, plan_summary)
        preset = MOTION_PERSONALITY_PRESETS.get(personality, MOTION_PERSONALITY_PRESETS["balanced"])
        motion_plan = dict(fallback.get("motion_plan") or {}, **motion_plan)
        motion_plan["schema_version"] = MOTION_PLAN_SCHEMA_VERSION
        motion_plan.setdefault("personality", personality)
        motion_plan.setdefault("primary", preset["primary"])
        motion_plan.setdefault("secondary", preset["secondary"])
        motion_plan.setdefault("ambient", preset["ambient"])
        motion_plan.setdefault("density", _infer_density(motion_elements, shot_design.get("subtitle"), overlay))
        motion_plan.setdefault("timing", preset["timing"])
        motion_plan.setdefault("safe_zone", safe_zones[0])
        a11y = motion_plan.get("a11y") if isinstance(motion_plan.get("a11y"), dict) else {}
        a11y.setdefault("subtitle_required", bool((shot_design.get("subtitle") or {}).get("text")))
        a11y.setdefault("max_lines", ((shot_design.get("subtitle") or {}).get("typography") or {}).get("max_lines", 2))
        a11y.setdefault("contrast", "high")
        motion_plan["a11y"] = a11y
        motion_plan["motion_elements"] = motion_elements
        execution_plan = shot_design.get("execution_plan")
        if execution_plan is not None:
            _validate_execution_plan(
                execution_plan, "shot[%s].execution_plan" % sid,
                shot_duration=source_shot.get("duration") or source_shot.get("duration_sec"))
            _attach_base_plate_execution_contract(execution_plan, source_shot, plan_summary)
            motion_plan["execution_plan"] = execution_plan
        elif isinstance(motion_plan.get("execution_plan"), dict):
            execution_plan = motion_plan.get("execution_plan")
        authored_slots = shot_design.get("asset_slots")
        if not isinstance(authored_slots, list):
            authored_slots = []
        canonical_slots = _build_asset_slots(
            shot_design.get("subtitle"), overlay, motion_elements, motion_plan,
            shot=source_shot, plan_summary=plan_summary)
        motion_plan["asset_slots"] = _merge_asset_slots(canonical_slots, authored_slots)
        motion_plan["asset_slots"] = _canonicalize_asset_slots(
            motion_plan["asset_slots"], canonical_slots, shot_id=sid)
        # Older saved motion-design JSON may have compiled director notes and
        # inferred chart/arrow placeholders as visible text.  Re-normalizing an
        # approved design must remove those stale renderer flags as well; the
        # authored overlay copy remains untouched.
        for slot in motion_plan["asset_slots"]:
            if not isinstance(slot, dict):
                continue
            slot_name = str(slot.get("slot") or "")
            if slot_name.startswith("motion_element_"):
                slot["renderable"] = False
            elif slot_name in {"chart", "arrow", "annotation"} and not source_shot.get(slot_name):
                slot["enabled"] = False
                slot["renderable"] = False
        motion_plan["asset_slots"] = _synchronize_subtitle_slot(
            motion_plan["asset_slots"], shot_design.get("subtitle"))
        personality = str(motion_plan.get("personality") or "balanced")
        motion_plan["motion_contract_version"] = MOTION_CONTRACT_VERSION
        motion_plan.setdefault("motion_identity", _motion_identity(personality))
        identity = motion_plan.get("motion_identity")
        if not isinstance(identity, dict):
            identity = _motion_identity(personality)
            motion_plan["motion_identity"] = identity
        choreography = motion_plan.get("choreography")
        if not isinstance(choreography, dict):
            choreography = {}
        choreography.setdefault("hero_slot", "title_area")
        choreography.setdefault("primary_layer", "primary")
        choreography.setdefault("secondary_layer", "secondary")
        choreography.setdefault("ambient_layer", "ambient")
        choreography.setdefault("stagger_budget_ms", identity.get("stagger_budget_ms", 360))
        choreography.setdefault("active_motion_limit", identity.get("active_motion_limit", 0.33))
        choreography.setdefault("narrative", "setup_action_resolution")
        motion_plan["choreography"] = choreography
        for slot in motion_plan.get("asset_slots") or []:
            if isinstance(slot, dict):
                normalized_slot = _annotate_asset_slot(slot, shot_id=sid)
                slot.clear()
                slot.update(normalized_slot)
                slot.setdefault("asset_binding", _slot_asset_binding(
                    slot, shot=source_shot, plan_summary=plan_summary))
                requirements = _normalize_asset_requirements(slot)
                if requirements:
                    slot["asset_requirements"] = requirements
        motion_plan["asset_requirements"] = _collect_asset_requirements(
            motion_plan.get("asset_slots"))
        shot_design["motion_plan"] = motion_plan
        if execution_plan is not None:
            shot_design["execution_plan"] = execution_plan
        shot_design["asset_slots"] = motion_plan["asset_slots"]
        shot_design["motion_elements"] = motion_elements
        shot_design["video_safe_zones"] = safe_zones
    design["schema_version"] = MOTION_PLAN_SCHEMA_VERSION
    return design

def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as handle:
        return _sha256_bytes(handle.read())


def build_dialogue_binding(plan, director_plan, guide=None, *, plan_sha256=None,
                           director_plan_sha256=None, guide_sha256=None):
    """Bind approved dialogue without mutating the authored storyboard plan."""
    shots = plan.get("shots") or []
    segments = director_plan.get("segments") or []
    by_id = {str(item.get("id") or ""): item for item in segments}
    shot_ids = [str(item.get("id") or "") for item in shots]
    if not shot_ids or any(not item for item in shot_ids):
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_SHOT_ID_INVALID")
    if set(shot_ids) != set(by_id) or len(by_id) != len(segments):
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_ID_MISMATCH")
    rows = (guide or {}).get("rows") or []
    if guide is not None and len(rows) != len(shots):
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_GUIDE_COUNT_MISMATCH")
    timing_sources = {}
    for shot in shots:
        shot_id = str(shot.get("id") or "")
        segment = by_id[shot_id]
        authored_duration = shot.get("duration") or shot.get("seconds")
        director_duration = (segment.get("duration_seconds") or
                             segment.get("duration") or segment.get("seconds"))
        if authored_duration is None:
            raise ValueError(
                "MOTION_DESIGN_DIALOGUE_DURATION_REQUIRED: %s" % shot_id)
        if director_duration is not None and abs(
                float(director_duration) - float(authored_duration)) > 0.001:
            raise ValueError(
                "MOTION_DESIGN_DIALOGUE_DURATION_MISMATCH: %s" % shot_id)
        timing_sources[shot_id] = {
            "dialogue": segment.get("dialogue") or segment.get("voiceover"),
            "duration_seconds": authored_duration,
            "audio_contract": segment.get("audio_contract") or {},
            "voice_type": segment.get("voice_type"),
        }
    plan_voice_type = ((director_plan.get("presenter") or {}).get(
        "voice_type"))
    timing_contract = dialogue_timing.build_contract(
        shots, timing_sources,
        plan_voice_type=plan_voice_type)
    proposal = dialogue_timing.propose_reallocation(
        shots, timing_sources,
        plan_voice_type=plan_voice_type)
    dialogue_timing.assert_fits(
        timing_contract,
        error_code="MOTION_DESIGN_DIALOGUE_DURATION_OVERFLOW",
        proposal=proposal)
    timing_by_id = {
        item["shot_id"]: item for item in timing_contract["items"]}
    items = []
    for index, shot_id in enumerate(shot_ids):
        dialogue = str((by_id.get(shot_id) or {}).get("dialogue") or "").strip()
        if not dialogue:
            raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_MISSING: %s" % shot_id)
        item = {
            "shot_id": shot_id, "dialogue": dialogue,
            "dialogue_sha256": _sha256_bytes(dialogue.encode("utf-8")),
            "director_segment_id": shot_id,
            "duration_seconds": timing_by_id[shot_id]["duration_seconds"],
            "dialogue_timing": timing_by_id[shot_id],
        }
        if guide is not None:
            row = rows[index] or {}
            guide_talk = str(row.get("talk") or "").strip()
            expected_row_id = "r%d" % (index + 1)
            if str(row.get("id") or "") != expected_row_id or guide_talk != dialogue:
                raise ValueError(
                    "MOTION_DESIGN_DIALOGUE_BINDING_GUIDE_MISMATCH: %s" % shot_id)
            item.update({
                "guide_row_id": expected_row_id,
                "guide_talk_sha256": _sha256_bytes(guide_talk.encode("utf-8")),
            })
        items.append(item)
    binding = {
        "version": 1, "kind": "approved_dialogue_motion_execution_binding",
        "authored_plan_sha256": plan_sha256,
        "director_plan_sha256": director_plan_sha256,
        "guide_sha256": guide_sha256,
        "plan_voice_type": plan_voice_type,
        "timing_contract": timing_contract, "items": items,
    }
    binding["binding_sha256"] = _sha256_bytes(json.dumps(
        binding, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))
    return binding


def build_authored_dialogue_binding(plan, *, plan_sha256=None):
    """Bind dialogue already present in the approved authored plan."""
    director_plan = {"segments": [{
        "id": shot.get("id"),
        "dialogue": shot.get("dialogue") or shot.get("voiceover"),
        "duration": shot.get("duration") or shot.get("seconds"),
    } for shot in plan.get("shots") or []]}
    return build_dialogue_binding(
        plan, director_plan, plan_sha256=plan_sha256,
        director_plan_sha256=plan_sha256)


def validate_dialogue_binding(plan, binding, *, plan_sha256=None):
    if not isinstance(binding, dict) or binding.get("kind") != "approved_dialogue_motion_execution_binding":
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_REQUIRED")
    source = {key: value for key, value in binding.items()
              if key != "binding_sha256"}
    expected = _sha256_bytes(json.dumps(
        source, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))
    if binding.get("binding_sha256") != expected:
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_AGGREGATE_HASH_MISMATCH")
    if plan_sha256 and binding.get("authored_plan_sha256") != plan_sha256:
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_PLAN_SHA_MISMATCH")
    items = binding.get("items") or []
    by_id = {str(item.get("shot_id") or ""): item for item in items
             if isinstance(item, dict)}
    plan_ids = [str(item.get("id") or "") for item in (plan.get("shots") or [])]
    if set(plan_ids) != set(by_id) or len(by_id) != len(items):
        raise ValueError("MOTION_DESIGN_DIALOGUE_BINDING_ID_MISMATCH")
    result = {}
    for shot_id in plan_ids:
        item = by_id[shot_id]
        dialogue = str(item.get("dialogue") or "").strip()
        if (not dialogue or item.get("dialogue_sha256") !=
                _sha256_bytes(dialogue.encode("utf-8"))):
            raise ValueError(
                "MOTION_DESIGN_DIALOGUE_BINDING_HASH_MISMATCH: %s" % shot_id)
        shot = next(candidate for candidate in plan.get("shots") or []
                    if str(candidate.get("id") or "") == shot_id)
        duration = shot.get("duration") or shot.get("seconds")
        if (duration is None or
                abs(float(item.get("duration_seconds") or 0) -
                    float(duration)) > 0.001):
            raise ValueError(
                "MOTION_DESIGN_DIALOGUE_BINDING_DURATION_MISMATCH: %s" %
                shot_id)
        result[shot_id] = item
    timing_sources = {
        shot_id: {
            "dialogue": item["dialogue"],
            "duration_seconds": item["duration_seconds"],
        } for shot_id, item in result.items()}
    timing = dialogue_timing.validate_contract(
        binding.get("timing_contract"), plan.get("shots") or [],
        timing_sources, plan_voice_type=binding.get("plan_voice_type"))
    dialogue_timing.assert_fits(
        timing, error_code="MOTION_DESIGN_DIALOGUE_DURATION_OVERFLOW")
    for shot_id, item in result.items():
        expected = next(row for row in timing["items"]
                        if row["shot_id"] == shot_id)
        if item.get("dialogue_timing") != expected:
            raise ValueError(
                "MOTION_DESIGN_DIALOGUE_BINDING_TIMING_MISMATCH: %s" %
                shot_id)
    return result


def design_from_plan(plan, api_key=None, model=None, require_llm=False, offline_fallback=False,
                     material_interpretation=None, base_plate_analysis=None,
                     dialogue_binding=None, plan_sha256=None):
    """从定稿 storyboard_plan.json 生成 motion_design.json。

    使用 BasicRouter LLM 分析每镜内容，生成统一的动效设计规范。
    返回 design dict（含 global_style + shots[]）。
    """
    key_setup.ensure_session_id()
    if not api_key:
        api_key = key_setup.load_key()
    shots = plan.get("shots") or []
    if not shots:
        raise ValueError("MOTION_DESIGN_NO_SHOTS: storyboard_plan 无 shots")
    missing_authored_dialogue = [
        str(shot.get("id") or "unknown") for shot in shots
        if not str(shot.get("dialogue") or shot.get("voiceover") or "").strip()
    ]
    if missing_authored_dialogue and dialogue_binding is None:
        raise ValueError(
            "MOTION_DESIGN_DIALOGUE_BINDING_REQUIRED: authored plan 缺台词 %s" %
            ",".join(missing_authored_dialogue))
    if dialogue_binding is None:
        dialogue_binding = build_authored_dialogue_binding(
            plan, plan_sha256=plan_sha256)
    dialogue_by_id = validate_dialogue_binding(
        plan, dialogue_binding, plan_sha256=plan_sha256)

    # Build a compact plan summary for the LLM
    material_interpretation = material_interpretation or plan.get("material_interpretation") or {}
    base_plate_analysis = base_plate_analysis or plan.get("base_plate_analysis") or {}
    plan_summary = {
        "title": plan.get("title") or plan.get("product_name") or "",
        "aspect_ratio": output_ratio(plan),
        "brand": plan.get("brand") or plan.get("brand_kit") or plan.get("render_profile") or {},
        "creative_direction": plan.get("creative_direction") or plan.get("visual_style") or
                              plan.get("style") or plan.get("director_note") or "",
        "storyboard_role": plan.get("storyboard_role"),
        "storyboard_usage": plan.get("storyboard_usage"),
        "storyboard_contract": plan.get("storyboard_contract") or {},
        "scene_type": plan.get("scene_type") or plan.get("type"),
        "source_ppt": plan.get("source_ppt") or plan.get("source_doc"),
        "asset_refs": plan.get("asset_refs") or {},
        "material_interpretation": material_interpretation,
        "base_plate_analysis": base_plate_analysis,
        "shots": [{
            "id": s.get("id"),
            "duration": s.get("duration") or s.get("seconds"),
            "dialogue": str((dialogue_by_id.get(str(s.get("id"))) or {}).get(
                "dialogue") or s.get("dialogue") or s.get("voiceover") or "")[:240],
            "dialogue_sha256": (dialogue_by_id.get(str(s.get("id"))) or {}).get(
                "dialogue_sha256"),
            "visual": (s.get("visual") or s.get("scene_prompt") or "")[:80],
            "camera": s.get("camera_movement") or s.get("camera") or "",
            "characters": s.get("characters") or [],
            "has_product": bool(s.get("product_sku") or s.get("product_refs") or
                                 (s.get("asset_refs") or {}).get("product_images")),
            "motion_elements": s.get("motion_elements") or [],
            "asset_slots": s.get("asset_slots") or [],
            "base_plate": s.get("base_plate") or s.get("base_plate_analysis"),
        } for s in shots],
    }

    # Try LLM-based design
    llm_error = None
    if api_key and not offline_fallback:
        try:
            design, provider_run = _llm_design(plan_summary, api_key, model=model)
            # The model owns creative intent. Renderer tracks and asset bindings
            # are deterministic compiler output, so validate the bounded
            # creative proposal first, then require the compiled execution plan.
            validated = _validate_design(design, shots, require_execution=False)
            validated = _normalize_design_contract(validated, shots, plan_summary)
            validated = _validate_design(validated, shots, require_execution=True)
            validated["design_engine"] = {
                "mode": "llm",
                "model": provider_run["model"],
                "structured_output_mode": provider_run["mode"],
                "attempts": provider_run["attempts"],
                "strict_schema_compatible": provider_run["strict_schema_compatible"],
                "selection_basis": "declared_capabilities_plus_business_validation",
                "execution_owner": "deterministic_motion_compiler",
            }
            validated["dialogue_contract"] = dialogue_binding
            return validated
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            llm_error = str(exc)
            if require_llm:
                raise ValueError("MOTION_DESIGN_LLM_FAILED: %s" % exc) from exc
    elif require_llm:
        raise ValueError("MOTION_DESIGN_LLM_REQUIRED: 缺少 BasicRouter key")

    # Offline compatibility fallback. Production workflow passes require_llm so
    # creative direction cannot silently degrade to renderer-shaped defaults.
    fallback = _rule_based_design(plan_summary, shots)
    fallback = _normalize_design_contract(fallback, shots, plan_summary)
    fallback["design_engine"] = {
        "mode": "rule_based",
        "reason": ("explicit_offline_fallback" if offline_fallback else
                   (llm_error or "api_key_unavailable")),
        "creative_source": "confirmed_storyboard_legacy_fields",
        "execution_contract": "offline_deterministic_template",
    }
    fallback["dialogue_contract"] = dialogue_binding
    return fallback


def _llm_design(plan_summary, api_key, model=None):
    """Call a capability-selected BasicRouter LLM with bounded validation."""
    prompt = """你是广告动效导演。根据已确认分镜，为每个镜头只做创意决策：观众感受、字幕精简、图形层级、节奏、动效性格和安全区。不要输出 Remotion/HyperFrames 执行参数、asset_slots、execution_plan、客户确认或业务状态；这些由确定性编译器生成。字幕不超过38字；文字只属于后期透明层，不得要求视频模型把字画进底片。每个输入镜头必须恰好返回一项并保持 shot_id。\n\n已确认分镜摘要：\n%s""" % json.dumps(
        plan_summary, ensure_ascii=False, indent=2)
    models = br_client.list_models(category="text")
    if not models:
        raise ValueError("在线文本模型目录为空")
    messages = [
        {"role": "system", "content": "输出可由 Remotion 与 HyperFrames 逐项执行的字幕和动效设计；不得修改客户事实或审批状态。"},
        {"role": "user", "content": prompt},
    ]
    response_schema = {
        "type": "object",
        "required": ["global_style", "shots"],
        "additionalProperties": False,
        "properties": {
            "global_style": {
                "type": "object", "additionalProperties": False,
                "required": ["font_family", "primary_color", "subtitle_position", "motion_density"],
                "properties": {
                    "font_family": {"type": "string", "description": "字体家族或品牌字体角色"},
                    "primary_color": {"type": "string", "description": "已确认品牌主色或用途描述"},
                    "subtitle_position": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]},
                    "motion_density": {"enum": ["low", "medium", "high"]}
                }
            },
            "shots": {
                "type": "array",
                "minItems": len(plan_summary.get("shots") or []),
                "maxItems": len(plan_summary.get("shots") or []),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shot_id", "subtitle", "motion_overlay", "motion_plan", "motion_elements", "video_safe_zones", "director_note"],
                    "properties": {
                        "shot_id": {"type": "string", "description": "输入镜头 ID 的字符串形式"},
                        "subtitle": {
                            "type": "object", "additionalProperties": False,
                            "required": ["text", "position", "safe_zone", "typography"],
                            "properties": {
                                "text": {"type": "string", "maxLength": 38},
                                "position": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]},
                                "safe_zone": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]},
                                "typography": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["size_px", "max_width_px", "max_lines", "preset", "emphasis"],
                                    "properties": {
                                        "size_px": {"type": "integer", "minimum": 38, "maximum": 72},
                                        "max_width_px": {"type": "integer", "minimum": 680, "maximum": 1320},
                                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 2},
                                        "preset": {"enum": ["fade_up", "pop", "slide_left", "slide_right", "none"]},
                                        "emphasis": {"type": "array", "maxItems": 5, "items": {"type": "string"}}
                                    }
                                }
                            }
                        },
                        "motion_overlay": {
                            "type": "object", "additionalProperties": False,
                            "required": ["style", "safe_zone", "title", "bullets", "metric", "preset", "size_px", "width_px", "reason"],
                            "properties": {
                                "style": {"enum": ["title_reveal", "bullet_list", "metric_pop", "data_card", "lower_third", "keyword_flash", "none"]},
                                "safe_zone": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]},
                                "title": {"type": "string", "maxLength": 60},
                                "bullets": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 60}},
                                "metric": {"type": "string", "maxLength": 40},
                                "preset": {"enum": ["pop", "slide_left", "slide_right", "fade_up", "none"]},
                                "size_px": {"type": "integer", "minimum": 30, "maximum": 56},
                                "width_px": {"type": "integer", "minimum": 320, "maximum": 620},
                                "reason": {"type": "string", "maxLength": 200}
                            }
                        },
                        "motion_plan": {
                            "type": "object", "additionalProperties": False,
                            "required": ["schema_version", "personality", "primary", "secondary", "ambient", "density", "timing", "safe_zone", "a11y"],
                            "properties": {
                                "schema_version": {"const": 2},
                                "personality": {"enum": ["calm", "balanced", "energetic"]},
                                "primary": {"enum": ["low", "medium", "high"]},
                                "secondary": {"enum": ["low", "medium", "high"]},
                                "ambient": {"enum": ["low", "medium", "high"]},
                                "density": {"enum": ["low", "medium", "high"]},
                                "timing": {"enum": ["slow", "steady", "quick"]},
                                "safe_zone": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]},
                                "a11y": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["subtitle_required", "max_lines", "contrast"],
                                    "properties": {
                                        "subtitle_required": {"type": "boolean"},
                                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 2},
                                        "contrast": {"enum": ["high"]}
                                    }
                                }
                            }
                        },
                        "motion_elements": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 200}},
                        "video_safe_zones": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"enum": ["lower_third", "upper_third", "center", "left", "right", "corner"]}},
                        "director_note": {"type": "string", "minLength": 1, "maxLength": 300}
                    }
                }
            }
        }
    }
    design, provider_run = text_provider.chat_json(
        api_key, messages, response_schema, model=model,
        catalog=models, task="motion_design",
        timeout=180, max_repairs=1)
    return design, provider_run


def _rule_based_design(plan_summary, shots):
    """Rule-based motion design fallback (no LLM needed).

    The fallback must emit the same structured contract as LLM mode. This keeps
    design_board projects executable even when the text model is unavailable.
    """
    design_shots = []
    for shot in shots:
        enriched = dict(shot)
        design_shots.append(_motion_plan_from_legacy(enriched, plan_summary))

    brand = plan_summary.get("brand") if isinstance(plan_summary, dict) else {}
    brand = brand if isinstance(brand, dict) else {}
    colors = brand.get("colors") if isinstance(brand.get("colors"), dict) else {}
    primary_color = (
        brand.get("primary_color") or brand.get("brand_primary") or
        colors.get("primary") or colors.get("brand") or "#64748B")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(primary_color)):
        primary_color = "#64748B"
    return {
        "version": 1,
        "schema_version": MOTION_PLAN_SCHEMA_VERSION,
        "global_style": {
            "font_family": "default",
            # Preserve the approved brand kit. Neutral slate is only used
            # when the plan carries no valid colour; never invent brand red.
            "primary_color": primary_color,
            "subtitle_position": "lower_third",
            "motion_density": "medium",
        },
        "shots": design_shots,
    }


# ── 安全区注入 ──────────────────────────────────────────────────────

def inject_safe_zones(prompt_text, shot_design):
    """把安全区指令注入视频生成 prompt。

    告诉模型在哪些区域不要放置关键动作/主体，为后期字幕/动效留出空间。
    """
    if not shot_design:
        return prompt_text
    zones = shot_design.get("video_safe_zones") or []
    if not zones:
        return prompt_text

    hints = []
    for zone in zones:
        zone_def = SAFE_ZONES.get(zone)
        if zone_def:
            hints.append(zone_def["prompt_hint"])
    if not hints:
        return prompt_text

    safe_zone_block = "\n【画面构图安全区】%s" % " ".join(hints)
    return prompt_text + safe_zone_block


def storyboard_safe_zone_prompt(shot_design):
    """生成故事板构图的安全区提示。"""
    if not shot_design:
        return ""
    zones = shot_design.get("video_safe_zones") or []
    if not zones:
        return ""
    hints = []
    for zone in zones:
        zone_def = SAFE_ZONES.get(zone)
        if zone_def:
            hints.append(zone_def["storyboard_hint"])
    if not hints:
        return ""
    return "；".join(hints)


def get_shot_design(design, shot_id):
    """从 motion_design 中获取指定镜头的动效设计。"""
    if not design:
        return None
    for shot in (design.get("shots") or []):
        if str(shot.get("shot_id")) == str(shot_id):
            return shot
    return None


def design_to_motion_overlay(shot_design):
    """把 shot 级动效设计转成 final_edit._build_motion_overlay 兼容的格式。"""
    if not shot_design:
        return None
    overlay = shot_design.get("motion_overlay") or {}
    if overlay.get("style") == "none":
        return None
    result = {
        "style": overlay.get("style"),
        "position": overlay.get("position") or STYLE_DEFAULT_ZONE.get(overlay.get("style"), "center"),
        "timing": overlay.get("timing") or "",
    }
    if overlay.get("title"):
        result["title"] = overlay["title"]
    if overlay.get("bullets"):
        result["bullets"] = overlay["bullets"]
    if overlay.get("metric"):
        result["metric"] = overlay["metric"]
    return result


def design_to_subtitle(shot_design, dialogue=""):
    """把 shot 级动效设计转成字幕行。"""
    if not shot_design:
        return None
    sub = shot_design.get("subtitle")
    if not sub or not sub.get("text"):
        # Fallback to dialogue
        if dialogue:
            text = compact_subtitle_text(dialogue)
            return {"text": text, "position": "lower_third"} if text else None
        return None
    typography = sub.get("typography") or {}
    result = {"text": sub["text"], "position": sub.get("position", "lower_third")}
    for source, target in (("size_px", "size"), ("max_width_px", "width_px"),
                           ("preset", "preset"), ("max_lines", "max_height_lines")):
        if typography.get(source) is not None:
            result[target] = typography[source]
    if typography.get("emphasis"):
        result["emphasis"] = typography["emphasis"]
    return result


# ── CLI ─────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="导演级动效设计规划器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dp = sub.add_parser("design", help="从 storyboard_plan 生成 motion_design")
    dp.add_argument("--plan", required=True)
    dp.add_argument("--out", required=True)
    dp.add_argument("--model", help="LLM model for design generation")
    dp.add_argument("--require-llm", action="store_true",
                    help="模型不可用或返回无效时阻断，不允许静默退回规则模板")
    dp.add_argument("--offline-fallback", action="store_true",
                    help="明确使用离线执行模板；仅用于已确认素材实测，不代表新的 LLM 创意")
    dp.add_argument("--dialogue-source",
                    help="已审批 director_plan.json；为缺台词 authored plan 提供只读绑定")
    dp.add_argument("--guide-source",
                    help="可选 guide.json；与 director plan 逐镜台词交叉核验")
    dp.add_argument("--dialogue-binding-out",
                    help="写出经哈希校验的台词绑定；不会改写 authored plan")

    ip = sub.add_parser("inject", help="把动效设计注入 storyboard_plan")
    ip.add_argument("--plan", required=True)
    ip.add_argument("--design", required=True)
    ip.add_argument("--out", help="输出路径（默认原地修改）")
    ip.add_argument(
        "--manifest",
        help="design_board 正式执行计划必填；绑定 authored plan 与 motion_design 阶段")

    args = ap.parse_args(argv)

    if args.cmd == "design":
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
        plan_sha256 = _sha256_file(args.plan)
        dialogue_binding = None
        if args.dialogue_source:
            with open(args.dialogue_source, encoding="utf-8") as f:
                director_plan = json.load(f)
            guide = None
            if args.guide_source:
                with open(args.guide_source, encoding="utf-8") as f:
                    guide = json.load(f)
            dialogue_binding = build_dialogue_binding(
                plan, director_plan, guide, plan_sha256=plan_sha256,
                director_plan_sha256=_sha256_file(args.dialogue_source),
                guide_sha256=(_sha256_file(args.guide_source)
                              if args.guide_source else None))
        elif all(str(shot.get("dialogue") or shot.get("voiceover") or "").strip()
                 for shot in plan.get("shots") or []):
            dialogue_binding = build_authored_dialogue_binding(
                plan, plan_sha256=plan_sha256)
        if dialogue_binding and args.dialogue_binding_out:
            os.makedirs(os.path.dirname(os.path.abspath(
                args.dialogue_binding_out)) or ".", exist_ok=True)
            atomic_write_json(args.dialogue_binding_out, dialogue_binding)
        key_setup.ensure_session_id()
        api_key = key_setup.load_key()
        design = design_from_plan(plan, api_key=api_key, model=args.model,
                                  require_llm=args.require_llm,
                                  offline_fallback=args.offline_fallback,
                                  dialogue_binding=dialogue_binding,
                                  plan_sha256=plan_sha256)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        atomic_write_json(args.out, design)
        print(json.dumps({"ok": True, "out": args.out,
                          "shots": len(design.get("shots", [])),
                          "design_engine": design.get("design_engine")}, ensure_ascii=False))
        return 0

    if args.cmd == "inject":
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
        with open(args.design, encoding="utf-8") as f:
            design = json.load(f)
        plan_shots = plan.get("shots") or []
        design = _validate_design(design, plan_shots, require_execution=False)
        design = _normalize_design_contract(design, plan_shots, {
            "storyboard_role": plan.get("storyboard_role"),
            "storyboard_usage": plan.get("storyboard_usage"),
            "scene_type": plan.get("scene_type") or plan.get("type"),
            "source_ppt": plan.get("source_ppt"),
            "source_doc": plan.get("source_doc"),
        })
        contract = plan.get("storyboard_contract") or {}
        role = str(
            plan.get("storyboard_role") or contract.get("kind") or
            contract.get("role") or "").strip().lower().replace("-", "_")
        design_board = role == "design_board"
        if design_board and not args.out:
            raise ValueError(
                "DESIGN_BOARD_EXECUTION_PLAN_OUT_REQUIRED: 禁止原地修改客户已确认的 "
                "storyboard_plan；请用 --out 写入独立 execution plan。")
        if design_board and not args.manifest:
            raise ValueError("DESIGN_BOARD_EXECUTION_PLAN_MANIFEST_REQUIRED")
        manifest = None
        if args.manifest:
            manifest = run_manifest_store.load_manifest(args.manifest)
            run_manifest_store.identity_gate(
                manifest, client=plan.get("client") or manifest.get("client"))
            if (plan.get("run_id") and
                    str(plan.get("run_id")) != str(manifest.get("run_id"))):
                raise ValueError("EXECUTION_PLAN_RUN_IDENTITY_MISMATCH")
            source_record = run_manifest_store.file_record(args.plan)
            if (source_record != (manifest.get("storyboard_plan") or {}) or
                    not run_manifest_store.file_record_is_current(source_record)):
                raise ValueError("EXECUTION_PLAN_SOURCE_PLAN_STALE")
            run_manifest_store.generation_gate(manifest, "motion_design")
        # Inject motion design into each shot
        for shot in plan.get("shots") or []:
            sid = str(shot.get("id"))
            shot_design = get_shot_design(design, sid)
            if shot_design:
                shot["motion_design"] = shot_design
                shot["video_safe_zones"] = shot_design.get("video_safe_zones") or []
                shot["motion_plan"] = shot_design.get("motion_plan") or {}
                shot["asset_slots"] = shot_design.get("asset_slots") or []
                if shot_design.get("motion_elements"):
                    shot["motion_elements"] = shot_design.get("motion_elements")
        out_path = args.out or args.plan
        if manifest is not None:
            plan["_execution_plan"] = {
                "kind": "motion_enriched_storyboard_plan",
                "version": 1,
                "client": manifest.get("client"),
                "run_id": manifest.get("run_id"),
                "source_plan": run_manifest_store.file_record(args.plan),
                "motion_design": run_manifest_store.file_record(args.design),
            }
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        atomic_write_json(out_path, plan)
        if manifest is not None:
            # The execution plan is a deterministic derivative. Bind its exact
            # bytes without mutating the already-approved creative artifact set.
            manifest.setdefault("generation", {}).setdefault(
                "motion_design", {})["execution_plan"] = (
                    run_manifest_store.file_record(out_path))
            run_manifest_store.save_manifest(manifest, args.manifest)
        print(json.dumps({"ok": True, "out": out_path}, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
