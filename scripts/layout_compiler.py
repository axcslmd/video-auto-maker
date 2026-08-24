#!/usr/bin/env python3
"""Compile ratio-aware geometry, content capacity and asset variant contracts."""
import argparse
import json
import math
import os
import re

import artifact_contract
from project_utils import atomic_write_json
import schema_validate
import visual_intent


RATIO_DIMENSIONS = {
    "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080],
    "4:3": [1440, 1080], "3:4": [1080, 1440],
}

BASE_CAPACITY = {
    "hero": 74, "metric": 54, "comparison": 108, "process": 126,
    "timeline": 112, "proof": 112, "product": 78, "brand": 44, "cta": 44,
}


def normalize_ratio(value, width=None, height=None):
    raw = str(value or "").strip().lower().replace("×", ":").replace("x", ":")
    aliases = {"landscape": "16:9", "portrait": "9:16", "square": "1:1"}
    raw = aliases.get(raw, raw)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)\s*", raw)
    if match:
        left, right = float(match.group(1)), float(match.group(2))
        candidate = min(RATIO_DIMENSIONS, key=lambda item: abs(
            (RATIO_DIMENSIONS[item][0] / RATIO_DIMENSIONS[item][1]) - left / right))
        if abs((RATIO_DIMENSIONS[candidate][0] / RATIO_DIMENSIONS[candidate][1]) - left / right) < 0.03:
            return candidate
    if width and height:
        divisor = math.gcd(int(width), int(height))
        return normalize_ratio("%d:%d" % (int(width) // divisor, int(height) // divisor))
    raise ValueError("LAYOUT_RATIO_UNSUPPORTED: %s" % value)


def requested_ratios(plan, overrides=None):
    values = list(overrides or [])
    values.append(plan.get("output_ratio") or plan.get("ratio") or plan.get("aspect_ratio"))
    for key in ("derive_ratios", "target_ratios", "delivery_ratios"):
        value = plan.get(key) or []
        values.extend(value if isinstance(value, list) else [value])
    result = []
    for value in values:
        if not value:
            continue
        normalized = normalize_ratio(value, plan.get("width"), plan.get("height"))
        if normalized not in result:
            result.append(normalized)
    if not result:
        result.append(normalize_ratio(None, plan.get("width") or 1920, plan.get("height") or 1080))
    return result


def _copy_units(value):
    text = str(value or "")
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    other = len(re.findall(r"[A-Za-z0-9]", text))
    return round(cjk + other * 0.55, 1)


def _visible_copy(shot, intent):
    values = [intent.get("single_visual_thesis")]
    for key in ("title", "headline", "kicker", "subtitle", "motion_elements"):
        value = shot.get(key)
        values.extend(value if isinstance(value, list) else [value])
    props = shot.get("content_props") if isinstance(shot.get("content_props"), dict) else {}
    for key in ("title", "headline", "subtitle", "items", "bullets", "steps", "metrics"):
        value = props.get(key)
        if isinstance(value, dict):
            values.extend(value.values())
        elif isinstance(value, list):
            for item in value:
                values.extend(item.values() if isinstance(item, dict) else [item])
        else:
            values.append(value)
    return [str(value).strip() for value in values if value not in (None, "", [], {})]


def _regions(family, ratio, has_presenter, visual_mode=None):
    portrait = ratio in ("9:16", "3:4")
    subtitle = {"x": 0.07, "y": 0.80, "w": 0.86, "h": 0.14}
    if visual_mode == "stacked_top_bottom":
        # Three mutually exclusive editorial bands.  The content component is
        # rendered natively inside ``primary``; it is never globally shrunk or
        # clipped after rendering.
        primary = {"x": 0.06, "y": 0.04, "w": 0.88, "h": 0.38}
        subtitle = {"x": 0.20, "y": 0.44, "w": 0.60, "h": 0.12}
        secondary = {"x": 0.31, "y": 0.59, "w": 0.38, "h": 0.37}
        name = "native_stacked_three_band"
    elif family == "comparison":
        primary = {"x": 0.06, "y": 0.13, "w": 0.41, "h": 0.60}
        secondary = {"x": 0.53, "y": 0.13, "w": 0.41, "h": 0.60}
        name = "dual_compare"
    elif family in ("process", "timeline", "proof"):
        primary = {"x": 0.07, "y": 0.14, "w": 0.86, "h": 0.56}
        secondary = {"x": 0.07, "y": 0.06, "w": 0.86, "h": 0.07}
        name = "structured_flow"
    elif family in ("metric", "product"):
        primary = {"x": 0.08, "y": 0.13, "w": 0.84 if portrait else 0.55, "h": 0.59}
        secondary = {"x": 0.08 if portrait else 0.68, "y": 0.08 if portrait else 0.18,
                     "w": 0.84 if portrait else 0.25, "h": 0.12 if portrait else 0.46}
        name = "metric_focus" if family == "metric" else "product_detail"
    elif has_presenter:
        primary = {"x": 0.07, "y": 0.11, "w": 0.86 if portrait else 0.47, "h": 0.61}
        secondary = {"x": 0.57 if not portrait else 0.54, "y": 0.10 if not portrait else 0.50,
                     "w": 0.36 if not portrait else 0.39, "h": 0.66 if not portrait else 0.28}
        name = "presenter_stage"
    else:
        primary = {"x": 0.08, "y": 0.12, "w": 0.84, "h": 0.62}
        secondary = {"x": 0.08, "y": 0.06, "w": 0.84, "h": 0.06}
        name = "editorial_focus"
    return name, {"primary": primary, "secondary": secondary, "subtitle_reserved": subtitle}


def _asset_records(shot):
    records = []
    for item in shot.get("references") or []:
        if isinstance(item, dict):
            records.append(dict(item))
    tags = shot.get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    known = {item.get("tag") for item in records}
    records.extend({"tag": tag} for tag in tags if tag not in known)
    for key in ("image", "video", "product_ref", "cast_board", "product_board"):
        if shot.get(key):
            records.append({"tag": key, "source": shot[key], "type": key})
    return records


def _asset_variant(record, ratio):
    role = str(record.get("role") or record.get("type") or record.get("tag") or "scene").lower()
    protected = any(token in role for token in ("product", "cast", "character", "actor", "usage"))
    variants = record.get("variants") if isinstance(record.get("variants"), dict) else {}
    source_ratio = record.get("aspect_ratio") or record.get("ratio")
    try:
        source_ratio = normalize_ratio(source_ratio) if source_ratio else None
    except ValueError:
        source_ratio = None
    if ratio in variants:
        status, selected = "variant_bound", variants[ratio]
    elif source_ratio == ratio:
        status, selected = "source_exact", record.get("source") or record.get("url")
    elif protected and source_ratio:
        status, selected = "regeneration_required", None
    elif protected:
        status, selected = "source_ratio_required", None
    else:
        status, selected = "responsive_crop", record.get("source") or record.get("url")
    return {
        "tag": record.get("tag") or role,
        "role": role,
        "protected_subject": protected,
        "source_ratio": source_ratio,
        "target_ratio": ratio,
        "crop_policy": "forbidden" if protected else "safe_area_cover",
        "status": status,
        "selected_source": selected,
    }


def compile_layouts(plan, intent_contract=None, tokens=None, *, ratios=None, strict=False):
    plan = plan if isinstance(plan, dict) else {}
    tokens = tokens if isinstance(tokens, dict) else {}
    intents = intent_contract or visual_intent.compile_plan(plan)
    by_id = {item["shot_id"]: item for item in intents["shots"]}
    selected_ratios = requested_ratios(plan, overrides=ratios)
    unresolved, compiled = [], []
    for index, shot in enumerate(plan.get("shots") or [], 1):
        sid = str(shot.get("id") or shot.get("shot_id") or "shot_%02d" % index)
        intent = by_id.get(sid) or visual_intent.compile_shot(shot, index)
        family = intent["component_family"]
        copy_units = sum(_copy_units(value) for value in _visible_copy(shot, intent))
        has_presenter = bool(shot.get("human") or shot.get("character") or
                             shot.get("human_slot") or shot.get("humanSlot"))
        visual_mode = str(
            shot.get("visual_mode") or shot.get("layout_mode") or "").strip()
        layouts = {}
        for ratio in selected_ratios:
            factor = 0.72 if ratio in ("9:16", "3:4") else 0.86 if ratio == "1:1" else 1.0
            if visual_mode == "stacked_top_bottom":
                factor *= 0.62
            capacity = round(BASE_CAPACITY[family] * factor, 1)
            capacity_status = "fits" if copy_units <= capacity else "recompose_required"
            if capacity_status != "fits":
                unresolved.append("%s:%s:content_capacity_exceeded(%.1f>%.1f)" % (
                    sid, ratio, copy_units, capacity))
            layout_family, regions = _regions(
                family, ratio, has_presenter, visual_mode=visual_mode)
            variants = [_asset_variant(record, ratio) for record in _asset_records(shot)]
            for variant in variants:
                if variant["status"] in ("regeneration_required", "source_ratio_required"):
                    unresolved.append("%s:%s:%s" % (sid, ratio, variant["status"]))
            margin = float((tokens.get("layout") or {}).get("safe_margin_ratio") or 0.06)
            scale = ((tokens.get("typography") or {}).get("scale") or {}).get(
                "portrait" if ratio in ("9:16", "3:4") else "landscape") or {}
            layouts[ratio] = {
                "dimensions": RATIO_DIMENSIONS[ratio],
                "layout_family": layout_family,
                "regions": regions,
                "content_capacity": {
                    "used_units": copy_units, "max_units": capacity,
                    "status": capacity_status,
                    "min_body_px": int(scale.get("body") or (26 if ratio == "9:16" else 28)),
                    "fit_policy": "recompose_or_split_never_auto_shrink",
                },
                "asset_variants": variants,
                "safe_zone": {"margin_ratio": margin, "subtitle_reserved": regions["subtitle_reserved"]},
            }
        compiled.append({"shot_id": sid, "component_family": family, "layouts": layouts})
    unresolved = list(dict.fromkeys(unresolved))
    if strict and unresolved:
        raise ValueError("LAYOUT_CONTRACT_UNRESOLVED: %s" % "; ".join(unresolved))
    result = {"schema_version": 1, "type": "layout_contract",
              "ratios": selected_ratios, "shots": compiled, "unresolved": unresolved}
    result["contract_sha256"] = artifact_contract.sha256_json(result)
    schema_validate.enforce(result, "layout-contract", context="layout_compiler.compile_layouts")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile multi-ratio layout and asset contracts")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--style-tokens")
    parser.add_argument("--visual-intent")
    parser.add_argument("--ratio", action="append", dest="ratios")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.plan, encoding="utf-8") as handle:
        plan = json.load(handle)
    def load(path):
        if not path:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    result = compile_layouts(
        plan, load(args.visual_intent), load(args.style_tokens),
        ratios=args.ratios, strict=args.strict)
    atomic_write_json(args.out, result)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "contract_sha256": result["contract_sha256"],
                      "unresolved": result["unresolved"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
