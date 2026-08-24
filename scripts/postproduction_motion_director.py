#!/usr/bin/env python3
"""Assign deterministic postproduction motion intent and Shotcraft cards.

The director consumes the approved storyboard, authored motion design, and
brand tokens as three independent contracts. It emits renderer instructions
only; spoken or visible copy remains owned by the video/audio and HyperFrames
layers respectively.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import shotcraft_compile  # noqa: E402
import visual_intent  # noqa: E402


DEFAULT_INTENT_CARD = {
    "hero": "spotlight-hero-card",
    "product": "segmented-thumb-hero",
    "comparison": "before-after-slider-scrub",
    "proof": "document-typewriter-reveal",
    "metric": "odometer-digit-roll",
    "data": "data-viz-landscape-open",
    "timeline": "timeline-travel",
    "process": "integration-hub-map",
    "transition": "line-carry-transition",
    "brand": "brand-frame-snap",
    "cta": "logo-sting-button",
}

KEYWORD_RULES = (
    ("comparison", re.compile(r"\b(before|after|compare|vs|versus|old|new)\b|对比|前后|之前|之后|新旧|方案A|方案B", re.I)),
    ("metric", re.compile(r"\b(metric|kpi|percent|percentage|ratio|revenue|conversion)\b|指标|转化率|百分比|营收|客单|倍数|\d+(?:\.\d+)?\s*[%倍]", re.I)),
    ("data", re.compile(r"\b(number|growth|rate|data|chart|graph)\b|数据|增长|效率|图表|趋势", re.I)),
    ("timeline", re.compile(r"\b(timeline|roadmap|milestone|history|journey)\b|时间线|路线图|里程碑|阶段|历程", re.I)),
    ("proof", re.compile(r"\b(document|paper|report|case|proof|evidence|deck|slide)\b|文档|报告|证明|案例|方案|PPT|幻灯", re.I)),
    ("process", re.compile(r"\b(flow|workflow|pipeline|integration|hub|system)\b|流程|链路|集成|系统|平台|连接", re.I)),
    ("product", re.compile(r"\b(product|sku|feature|detail|unbox)\b|产品|卖点|细节|开箱|功能|特写", re.I)),
    ("cta", re.compile(r"\b(cta|signup|buy|download|try|contact)\b|行动|购买|预约|下载|扫码|咨询|联系", re.I)),
    ("brand", re.compile(r"\b(brand|logo|identity|outro)\b|品牌|标识|片尾|收尾|logo", re.I)),
    ("transition", re.compile(r"\b(transition|bridge|interlude)\b|转场|过渡|承接", re.I)),
)

ENERGY_CARD_OVERRIDES = {
    "high": {
        "hero": "slam-entrance-moves",
        "transition": "crash-zoom-punch",
        "data": "odometer-digit-roll",
        "brand": "brand-ink-open",
    },
    "low": {
        "hero": "spotlight-hero-card",
        "transition": "shared-element-morph",
        "data": "data-viz-landscape-open",
        "brand": "brand-frame-snap",
    },
}

STRUCTURED_COMPONENT_INTENTS = {
    "hero": "hero", "metric": "metric", "comparison": "comparison",
    "process": "process", "timeline": "timeline", "proof": "proof",
    "product": "product", "brand": "brand", "cta": "cta",
}


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _canonical_sha256(value):
    payload = json.dumps(value or {}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text_blob(value):
    if isinstance(value, dict):
        return " ".join(_text_blob(v) for key, v in value.items()
                        if not str(key).startswith("_") and str(key).lower() not in {
                            "subtitle", "caption", "text"})
    if isinstance(value, list):
        return " ".join(_text_blob(v) for v in value)
    return "" if value is None else str(value)


def _ratio(width, height):
    if width == height:
        return "1:1"
    return "16:9" if width > height else "9:16"


def _energy(tokens, motion_design=None):
    motion = tokens.get("motion") if isinstance(tokens, dict) else {}
    value = str((motion or {}).get("energy") or tokens.get("energy") or "").lower()
    if value not in {"low", "medium", "high"} and isinstance(motion_design, dict):
        style = motion_design.get("global_style") or {}
        value = str(style.get("motion_density") or "medium").lower()
    return value if value in {"low", "medium", "high"} else "medium"


def _motion_shots(motion_design):
    by_id = {}
    ordered = []
    for item in (motion_design or {}).get("shots") or []:
        if not isinstance(item, dict):
            continue
        ordered.append(item)
        sid = str(item.get("shot_id") or item.get("id") or item.get("seg_id") or "")
        if sid:
            by_id[sid] = item
    return by_id, ordered


def _motion_for_shot(shot, index, by_id, ordered):
    sid = str(shot.get("id") or shot.get("shot_id") or shot.get("seg_id") or "")
    if sid and sid in by_id:
        return by_id[sid]
    return ordered[index] if index < len(ordered) else {}


def infer_intent(shot, motion_shot=None):
    motion_shot = motion_shot or {}
    for container in (shot.get("visual_intent"), motion_shot.get("visual_intent")):
        if not isinstance(container, dict):
            continue
        component = str(container.get("component_family") or "").strip().lower()
        if component in STRUCTURED_COMPONENT_INTENTS:
            return STRUCTURED_COMPONENT_INTENTS[component]
        semantic = str(container.get("semantic_kind") or "").strip().lower()
        semantic_map = {
            "narrative": "hero", "quantitative_proof": "metric",
            "comparison": "comparison", "process": "process", "timeline": "timeline",
            "documentary_proof": "proof", "product_showcase": "product",
            "physical_demonstration": "product", "brand_resolve": "brand", "cta": "cta",
        }
        if semantic in semantic_map:
            return semantic_map[semantic]
    for container in (shot, motion_shot):
        component = str(container.get("content_scene_kind") or container.get("component") or "").strip().lower()
        if component in STRUCTURED_COMPONENT_INTENTS:
            return STRUCTURED_COMPONENT_INTENTS[component]
    explicit = str(
        motion_shot.get("motion_intent") or motion_shot.get("intent") or
        shot.get("motion_intent") or shot.get("intent") or
        shot.get("marketing_intent") or "").strip().lower()
    if explicit:
        for intent in DEFAULT_INTENT_CARD:
            if intent in explicit:
                return intent
    blob = _text_blob({"storyboard": shot, "motion": motion_shot})
    for intent, pattern in KEYWORD_RULES:
        if pattern.search(blob):
            return intent
    return "hero"


def _assets_for_intent(shot, motion_shot, intent):
    post = shot.get("postproduction") if isinstance(shot.get("postproduction"), dict) else {}
    if isinstance(post.get("assets"), list) and post["assets"]:
        return post["assets"]
    refs = []
    for source in (shot, motion_shot or {}):
        for value in source.get("ref_tags") or []:
            if isinstance(value, str) and value.strip() and value.strip() not in refs:
                refs.append(value.strip())
        for key in ("image", "video", "expanded_image", "source"):
            value = source.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in refs:
                refs.append(value.strip())
    if intent == "comparison" and len(refs) == 1:
        refs.append(refs[0])
    return refs[:8]


def _preferred_transition_card(motion_shot, default):
    blob = _text_blob(motion_shot).lower()
    if "iris" in blob or "circle" in blob or "圆形" in blob or "光圈" in blob:
        return "circle-match-iris"
    if "shared" in blob or "morph" in blob or "共享元素" in blob:
        return "shared-element-morph"
    return default


def choose_card(shot, motion_shot, cards, *, width, height, tokens, motion_design=None):
    intent = infer_intent(shot, motion_shot)
    energy = _energy(tokens, motion_design)
    preferred = ENERGY_CARD_OVERRIDES.get(energy, {}).get(intent) or DEFAULT_INTENT_CARD[intent]
    if intent == "transition":
        preferred = _preferred_transition_card(motion_shot, preferred)
    explicit = str(motion_shot.get("shotcraft_card_id") or motion_shot.get("card_id") or
                   shot.get("shotcraft_card_id") or "").strip()
    if explicit:
        preferred = explicit
    ratio = _ratio(width, height)
    if preferred in cards and ratio in cards[preferred].get("ratios", []):
        return preferred, intent
    fallback = DEFAULT_INTENT_CARD[intent]
    if fallback in cards and ratio in cards[fallback].get("ratios", []):
        return fallback, intent
    for card_id, card in cards.items():
        if ratio in card.get("ratios", []):
            return card_id, intent
    raise ValueError("SHOTCRAFT_NO_RATIO_CARD: %s" % ratio)


def _fallback_card_without_required_assets(cards, *, intent, ratio):
    for card_id, card in cards.items():
        if ratio in card.get("ratios", []) and not card.get("required_assets") and \
                intent in (card.get("intents") or []):
            return card_id
    for card_id, card in cards.items():
        if ratio in card.get("ratios", []) and not card.get("required_assets"):
            return card_id
    return None


def _beat_seconds(motion_shot):
    values = []
    plan = motion_shot.get("motion_plan") if isinstance(motion_shot.get("motion_plan"), dict) else {}
    execution = motion_shot.get("execution_plan") or plan.get("execution_plan") or {}
    plate = execution.get("base_plate_contract") if isinstance(execution, dict) else {}
    for value in (plate or {}).get("speech_beats_seconds") or []:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            pass
    for track in (execution or {}).get("tracks") or []:
        timeline = track.get("timeline") if isinstance(track, dict) else {}
        for value in (timeline or {}).values():
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                pass
    return sorted({value for value in values if value >= 0})


def _safe_zone_contract(shot, motion_shot):
    zones = motion_shot.get("video_safe_zones") or shot.get("video_safe_zones") or []
    if not zones:
        return [], {"source": "not_required", "status": "verified", "analysis_ref": None}
    plan = motion_shot.get("motion_plan") if isinstance(motion_shot.get("motion_plan"), dict) else {}
    execution = motion_shot.get("execution_plan") or plan.get("execution_plan") or {}
    plate = execution.get("base_plate_contract") if isinstance(execution, dict) else {}
    candidates = [
        motion_shot.get("visual_analysis"), motion_shot.get("base_plate_analysis"),
        motion_shot.get("base_plate"), shot.get("visual_analysis"),
        shot.get("base_plate_analysis"), shot.get("base_plate"), plate,
    ]
    analysis = next((item for item in candidates if isinstance(item, dict) and item), None)
    region_status = str((analysis or {}).get("region_status") or
                        (analysis or {}).get("status") or "").lower()
    verified = bool(analysis) and region_status not in {
        "", "missing", "requires_llm_visual_review", "pending", "unreviewed"}
    ref = ((analysis or {}).get("sha256") or (analysis or {}).get("ref") or
           (analysis or {}).get("path") or (analysis or {}).get("shot_id"))
    return zones, {
        "source": "visual_analysis" if analysis else "motion_design_contract",
        "status": "verified" if verified else "pending_visual_analysis",
        "analysis_ref": ref,
    }


def _timing_contract(card, shot, motion_shot, tokens, *, fps, duration_frames,
                     energy, is_last):
    motion = tokens.get("motion") if isinstance(tokens.get("motion"), dict) else {}
    hold_min = max(int(motion.get("hold_min_frames") or 15),
                   int(card.get("hold_min_frames") or 0))
    hold_frames = min(max(1, duration_frames - 1), hold_min)
    intro_seconds = 0.38 if energy == "high" else 0.52
    intro_frames = max(1, min(duration_frames - hold_frames,
                              int(round(intro_seconds * fps))))
    transition_frames = min(max(4, int(motion.get("transition_frames") or round(fps * 0.4))),
                            max(4, duration_frames // 3))
    beats = [int(round(value * fps)) for value in _beat_seconds(motion_shot)
             if 0 < int(round(value * fps)) < duration_frames]
    default_beat = duration_frames - transition_frames
    usable_beats = [value for value in beats
                    if 4 <= duration_frames - value <= max(4, duration_frames // 3)]
    beat_frame = min(usable_beats, key=lambda value: abs(value - default_beat)) \
        if usable_beats else default_beat
    transition_frames = duration_frames - beat_frame
    transition = {
        "style": "none" if is_last else str(card.get("transition_style") or
                                             motion.get("transition_style") or "fade"),
        "duration_frames": 0 if is_last else transition_frames,
        "beat_frame": None if is_last else beat_frame,
        "beat_source": "motion_design" if usable_beats else "authored_timing_contract",
    }
    fast = str(card.get("motion_speed") or "medium") == "fast"
    return {
        "intro_frames": intro_frames,
        "intro_seconds": round(intro_frames / float(fps), 4),
        "hold_frames": hold_frames,
        "hold_seconds": round(hold_frames / float(fps), 4),
        "transition": transition,
        "motion_blur": {
            "enabled": bool(card.get("requires_motion_blur") or fast),
            "shutter_angle": 180 if fast else 120,
            "samples": 10 if fast else 6,
        },
    }


def annotate_plan(plan, motion_design=None, brand_tokens=None, *,
                  registry_path=shotcraft_compile.REGISTRY, override_existing=False,
                  max_cards=None):
    """Return a storyboard copy annotated from all three approved inputs."""
    # Compatibility with the original annotate_plan(plan, style_tokens) API.
    if brand_tokens is None and isinstance(motion_design, dict) and \
            not motion_design.get("shots") and any(
                key in motion_design for key in ("brand", "motion", "typography", "energy")):
        brand_tokens = motion_design
        motion_design = {}
    motion_design = motion_design or {}
    tokens = brand_tokens or {}
    cards = shotcraft_compile._cards(registry_path)
    width, height = int(plan.get("width", 1080)), int(plan.get("height", 1920))
    fps = int(plan.get("fps", 30))
    result = copy.deepcopy(plan)
    by_id, ordered = _motion_shots(motion_design)
    annotated = 0
    shots = result.get("shots") or []
    intent_contract = visual_intent.compile_plan(plan)
    structured_intents = {item["shot_id"]: item for item in intent_contract["shots"]}
    energy = _energy(tokens, motion_design)
    token_sha = _canonical_sha256(tokens)
    ratio = _ratio(width, height)
    for index, shot in enumerate(shots):
        post = shot.get("postproduction")
        if isinstance(post, dict) and post.get("engine") == "shotcraft" and not override_existing:
            continue
        if max_cards is not None and annotated >= max_cards:
            continue
        motion_shot = _motion_for_shot(shot, index, by_id, ordered)
        shot_id = str(shot.get("id") or shot.get("shot_id") or "shot_%02d" % (index + 1))
        if shot_id in structured_intents:
            shot["visual_intent"] = copy.deepcopy(structured_intents[shot_id])
        card_id, intent = choose_card(
            shot, motion_shot, cards, width=width, height=height, tokens=tokens,
            motion_design=motion_design)
        card = cards[card_id]
        assets = _assets_for_intent(shot, motion_shot, intent)
        if card.get("required_assets") and not assets:
            fallback = _fallback_card_without_required_assets(cards, intent=intent, ratio=ratio)
            if fallback:
                card_id = fallback
                card = cards[card_id]
        duration = float(shot.get("duration") or shot.get("seconds") or 5)
        duration_frames = max(1, int(round(duration * fps)))
        timing = _timing_contract(
            card, shot, motion_shot, tokens, fps=fps, duration_frames=duration_frames,
            energy=energy, is_last=index == len(shots) - 1)
        safe_zones, safe_zone_provenance = _safe_zone_contract(shot, motion_shot)
        shot["motion_intent"] = intent
        shot["shotcraft_card_id"] = card_id
        shot["postproduction"] = {
            "engine": "shotcraft",
            "card_id": card_id,
            "shotcraft_card_id": card_id,
            "card_version": str(card.get("version") or "1"),
            "intent": intent,
            "motion_intent": intent,
            "renderer": card.get("renderer"),
            "primary_effect": card.get("primary_effect"),
            "assets": assets,
            "timing": {
                "intro_frames": timing["intro_frames"],
                "intro_seconds": timing["intro_seconds"],
                "hold_frames": timing["hold_frames"],
                "hold_seconds": timing["hold_seconds"],
            },
            "transition": timing["transition"],
            "motion_blur": timing["motion_blur"],
            "safe_zones": safe_zones,
            "safe_zone_provenance": safe_zone_provenance,
            "brand_token_sha256": token_sha,
            "text_ownership": "hyperframes",
            "selection_source": (shot.get("visual_intent") or {}).get("selection_source") or "legacy_fallback",
            "director_reason": "selected from structured visual intent and authored motion design",
        }
        annotated += 1
    result["_postproduction_motion_director"] = {
        "engine": "postproduction_motion_director",
        "schema_version": 2,
        "annotated_shots": annotated,
        "registry": os.path.abspath(registry_path),
        "energy": energy,
        "input_sha256": {
            "storyboard_plan": _canonical_sha256(plan),
            "motion_design": _canonical_sha256(motion_design),
            "brand_tokens": token_sha,
            "visual_intent": intent_contract["contract_sha256"],
        },
    }
    result["_postproduction_director"] = copy.deepcopy(
        result["_postproduction_motion_director"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assign Shotcraft cards from storyboard, motion design, and brand tokens")
    parser.add_argument("--storyboard-plan", "--plan", dest="plan", required=True)
    parser.add_argument("--motion-design")
    parser.add_argument("--brand-tokens", "--style-tokens", dest="brand_tokens")
    parser.add_argument("--registry", default=shotcraft_compile.REGISTRY)
    parser.add_argument("--out", required=True)
    parser.add_argument("--override-existing", action="store_true")
    parser.add_argument("--max-cards", type=int)
    args = parser.parse_args(argv)
    motion = _load(args.motion_design) if args.motion_design else {}
    tokens = _load(args.brand_tokens) if args.brand_tokens else {}
    result = annotate_plan(
        _load(args.plan), motion, tokens, registry_path=args.registry,
        override_existing=args.override_existing, max_cards=args.max_cards)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({
        "ok": True,
        "out": os.path.abspath(args.out),
        "annotated_shots": result["_postproduction_motion_director"]["annotated_shots"],
        "input_sha256": result["_postproduction_motion_director"]["input_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
