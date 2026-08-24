#!/usr/bin/env python3
"""Compile one visual thesis and a semantic component family per shot.

This is the structured replacement for keyword-driven renderer selection.
Text remains useful as authored copy, but layout and Shotcraft selection derive
from explicit semantic fields and data shapes rather than substring matches.
"""
import argparse
import json
import os

import artifact_contract
from project_utils import atomic_write_json
import schema_validate


KIND_TO_COMPONENT = {
    "narrative": "hero",
    "quantitative_proof": "metric",
    "comparison": "comparison",
    "process": "process",
    "timeline": "timeline",
    "documentary_proof": "proof",
    "product_showcase": "product",
    "physical_demonstration": "product",
    "brand_resolve": "brand",
    "cta": "cta",
}

SEMANTIC_ALIASES = {
    "hero": "narrative", "section": "narrative", "quote": "narrative",
    "metrics": "quantitative_proof", "metric": "quantitative_proof",
    "data": "quantitative_proof", "chart": "quantitative_proof",
    "comparison": "comparison", "compare": "comparison",
    "process": "process", "flow": "process", "causal": "process",
    "evolution": "timeline", "timeline": "timeline",
    "proof": "documentary_proof", "document": "documentary_proof",
    "slide": "documentary_proof", "slide_with_overlay": "documentary_proof",
    "product": "product_showcase", "product_showcase": "product_showcase",
    "physical": "physical_demonstration", "product_usage": "physical_demonstration",
    "brand": "brand_resolve", "outro": "brand_resolve", "cta": "cta",
}

DEFAULT_GOALS = {
    "narrative": "建立一个清晰的观看焦点",
    "quantitative_proof": "用一个可读数字证明核心结论",
    "comparison": "让差异在同一视觉基准上可见",
    "process": "解释动作或系统如何逐步发生",
    "timeline": "展示阶段推进与先后关系",
    "documentary_proof": "展示可追溯的事实或原始材料",
    "product_showcase": "突出产品身份与关键卖点",
    "physical_demonstration": "证明人物、产品与接触面的物理关系",
    "brand_resolve": "收束品牌记忆",
    "cta": "形成单一明确的下一步行动",
}


def _text(value, limit=120):
    if value is None:
        return ""
    value = str(value).strip()
    return value[:limit]


def _first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _semantic_kind(shot):
    authored = shot.get("visual_intent") if isinstance(shot.get("visual_intent"), dict) else {}
    explicit = _first(
        authored, "semantic_kind", "proof_type", "kind") or _first(
            shot, "semantic_kind", "proof_type", "content_scene_kind", "component")
    key = str(explicit or "").strip().lower().replace("-", "_")
    if key in KIND_TO_COMPONENT:
        return key, "authored_visual_intent" if authored else "structured_semantics"
    if key in SEMANTIC_ALIASES:
        return SEMANTIC_ALIASES[key], "authored_visual_intent" if authored else "structured_semantics"

    props = shot.get("content_props") if isinstance(shot.get("content_props"), dict) else {}
    if any(_first(container, "metrics", "metric", "kpis", "chart_data")
           for container in (shot, props)):
        return "quantitative_proof", "data_shape"
    if any(_first(container, "before", "after", "left", "right", "comparison_items")
           for container in (shot, props)):
        return "comparison", "data_shape"
    if any(_first(container, "steps", "process_steps", "nodes", "edges")
           for container in (shot, props)):
        return "process", "data_shape"
    if any(_first(container, "milestones", "timeline", "evolution")
           for container in (shot, props)):
        return "timeline", "data_shape"
    if _first(shot, "physical_relation_contract", "usage_geometry", "contact_surface"):
        return "physical_demonstration", "data_shape"
    if _first(shot, "product", "sku", "product_ref", "product_board"):
        return "product_showcase", "data_shape"
    if shot.get("is_cta") is True:
        return "cta", "structured_semantics"
    return "narrative", "safe_default"


def _subjects(shot, kind):
    authored = shot.get("visual_intent") if isinstance(shot.get("visual_intent"), dict) else {}
    props = shot.get("content_props") if isinstance(shot.get("content_props"), dict) else {}
    primary = _text(_first(
        authored, "primary_subject") or _first(
            shot, "primary_subject", "product_name", "title", "headline") or
        _first(props, "title", "headline") or DEFAULT_GOALS[kind])
    secondary = _first(authored, "secondary_subjects") or _first(
        shot, "secondary_subjects", "supporting_subjects") or []
    if not isinstance(secondary, list):
        secondary = [secondary]
    secondary = [_text(item, 80) for item in secondary if _text(item, 80) and _text(item, 80) != primary]
    if not secondary:
        tags = shot.get("ref_tags") or []
        if isinstance(tags, str):
            tags = [tags]
        secondary = [_text(tag, 80) for tag in tags if _text(tag, 80) != primary]
    return primary, list(dict.fromkeys(secondary))[:2]


def compile_shot(shot, index=1):
    shot = shot if isinstance(shot, dict) else {}
    sid = _text(_first(shot, "id", "shot_id", "seg_id") or "shot_%02d" % index, 80)
    authored = shot.get("visual_intent") if isinstance(shot.get("visual_intent"), dict) else {}
    kind, source = _semantic_kind(shot)
    primary, secondary = _subjects(shot, kind)
    communication_goal = _text(
        _first(authored, "communication_goal") or _first(
            shot, "communication_goal", "felt_intent", "narrative_function",
            "marketing_intent") or DEFAULT_GOALS[kind])
    thesis = _text(
        _first(authored, "single_visual_thesis", "visual_thesis") or _first(
            shot, "single_visual_thesis", "visual_thesis", "headline", "title") or
        _first(shot.get("content_props") or {}, "headline", "title") or
        _first(shot, "dialogue", "text", "visual") or communication_goal)
    unresolved = []
    if not thesis:
        unresolved.append("single_visual_thesis_missing")
    if not primary:
        unresolved.append("primary_subject_missing")
    return {
        "shot_id": sid,
        "semantic_kind": kind,
        "component_family": KIND_TO_COMPONENT[kind],
        "communication_goal": communication_goal or DEFAULT_GOALS[kind],
        "single_visual_thesis": thesis or DEFAULT_GOALS[kind],
        "primary_subject": primary or DEFAULT_GOALS[kind],
        "secondary_subjects": secondary,
        "attention_order": [primary or DEFAULT_GOALS[kind]] + secondary,
        "selection_source": source,
        "unresolved": unresolved,
    }


def compile_plan(plan, *, strict=False):
    plan = plan if isinstance(plan, dict) else {}
    shots = [compile_shot(shot, index) for index, shot in enumerate(plan.get("shots") or [], 1)]
    if not shots:
        raise ValueError("VISUAL_INTENT_SHOTS_REQUIRED")
    unresolved = ["%s:%s" % (shot["shot_id"], issue)
                  for shot in shots for issue in shot["unresolved"]]
    if strict and unresolved:
        raise ValueError("VISUAL_INTENT_UNRESOLVED: %s" % "; ".join(unresolved))
    result = {"schema_version": 1, "type": "visual_intent_contract", "shots": shots}
    result["contract_sha256"] = artifact_contract.sha256_json(result)
    schema_validate.enforce(result, "visual-intent", context="visual_intent.compile_plan")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile structured per-shot visual intent")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    with open(args.plan, encoding="utf-8") as handle:
        result = compile_plan(json.load(handle), strict=args.strict)
    atomic_write_json(args.out, result)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "contract_sha256": result["contract_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
