#!/usr/bin/env python3
"""Compile fact-bound creative IR into a deterministic storyboard plan.

The host LLM authors small creative proposals.  This compiler owns identifiers,
panel order, ratios, reference binding, renderer ownership, provenance and the
production-strict validation gate.
"""
import argparse
import copy
import hashlib
import json
import os
import re

from project_utils import atomic_write_json
from schema_validate import enforce
from storyboard_validator import validate_plan
import workflow_contract


COMPILER_VERSION = "1.0"
ALLOWED_SHOT_FIELDS = {
    "shot_size", "camera_movement", "angle_offset", "composition", "lighting",
    "character_action", "micro_expression", "scene_prompt", "prop_prompts",
    "audio", "narrative_function", "felt_intent", "director_voice", "arc_position",
    "motion_elements", "ref_tags", "references", "characters", "asset_slots",
    "motion_plan", "panel_plan", "scene_id", "product_sku", "fact_refs", "visual",
}
PROTECTED_SHOT_FIELDS = {"id", "panel_index", "duration", "dialogue"}

_PURPOSE_DIRECTION = {
    "hook": ("建立注意力", "立即产生好奇", "直接、有控制力"),
    "problem": ("呈现真实痛点", "感到问题与自己有关", "克制、不夸张"),
    "proof": ("提供可信证据", "相信表达有依据", "清晰、客观"),
    "benefit": ("展示用户收益", "看见改善后的状态", "温暖、具体"),
    "demonstration": ("演示产品或服务", "理解动作与结果", "准确、可执行"),
    "transition": ("连接前后信息", "自然跟随叙事", "简洁、流畅"),
    "payoff": ("兑现核心承诺", "获得情绪回报", "坚定、有余韵"),
    "cta": ("给出下一步行动", "知道现在该做什么", "明确、不施压"),
}


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _arc(index, total):
    if index == 0:
        return "opening"
    if index == total - 1:
        return "resolution"
    return "development"


def _default_panel_plan(scene, action):
    scene = str(scene or "干净场景")
    action = str(action or "主体完成本镜动作")
    return [
        "建立镜头：%s，交代主体和空间关系" % scene,
        "动作镜头：%s，清楚展示动作起点" % action,
        "细节镜头：聚焦动作接触点、产品细节或表情反应",
        "结果镜头：展示动作完成后的可见结果并为下一镜留出连续性",
    ]


def _default_shot(beat, index, total, role):
    purpose = beat["purpose"]
    narrative, felt, voice = _PURPOSE_DIRECTION[purpose]
    scene = beat["message"]
    action = "主体以一个明确、可见的动作表达：%s" % beat["message"]
    sizes = ("wide", "medium", "close_up", "detail")
    moves = ("slow_push_in", "lateral_track", "controlled_static", "slow_pull_out")
    compositions = ("主体居中建立空间", "主体位于左侧三分线", "关键物位于右侧三分线", "细节占据画面中心")
    angles = (0, 40, -35, 45)
    panel_plan = (["同一 Remotion/HyperFrames 时间轴的代表性执行帧：%s" % scene]
                  if role == "design_board" else _default_panel_plan(scene, action))
    shot = {
        "id": "s%02d" % (index + 1),
        "panel_index": index + 1,
        "scene_id": "scene_%02d" % (index + 1),
        "duration": beat["duration"],
        "dialogue": beat["talk"],
        "visual": scene,
        "shot_size": sizes[index % len(sizes)],
        "camera_movement": moves[index % len(moves)],
        "angle_offset": angles[index % len(angles)],
        "composition": compositions[index % len(compositions)],
        "lighting": "统一品牌色温的真实商业布光，主体与背景有明确层次",
        "character_action": action,
        "micro_expression": "自然、克制，与本镜叙事意图一致",
        "scene_prompt": "%s；真实材质和光线；画面干净；无字幕、无悬浮文字、无水印" % scene,
        "prop_prompts": [],
        "audio": {"voice": beat["talk"], "bgm": "延续全片统一氛围", "sfx": "只强化可见动作"},
        "narrative_function": narrative,
        "felt_intent": felt,
        "director_voice": voice,
        "arc_position": _arc(index, total),
        "motion_elements": list(beat.get("motion_elements") or []),
        "fact_refs": list(beat.get("fact_refs") or []),
        "references": [],
        "ref_tags": [],
        "characters": [],
        "panel_plan": panel_plan,
    }
    if role == "design_board":
        slot_id = "content_%02d" % (index + 1)
        shot["asset_slots"] = [{"id": slot_id, "type": "content", "required": True}]
        shot["motion_plan"] = {
            "schema_version": 2,
            "motion_elements": list(beat.get("motion_elements") or []),
            "asset_slots": copy.deepcopy(shot["asset_slots"]),
        }
    return shot


def _proposal_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("proposals"), list):
        return value["proposals"]
    if isinstance(value, dict):
        return [value]
    raise ValueError("SHOT_PATCHES_INVALID: 必须是 proposal 或 proposals 数组")


def _apply_shot_proposals(shots, proposals):
    touched = {shot["id"]: set() for shot in shots}
    for proposal in proposals:
        enforce(proposal, "shot-patch", context="storyboard_plan_compiler")
        for patch in proposal["patches"]:
            match = re.match(r"^/shots/(\d+)/([a-zA-Z0-9_]+)$", patch["path"])
            if not match:
                raise ValueError("SHOT_PATCH_PATH_INVALID: %s" % patch["path"])
            index, field = int(match.group(1)), match.group(2)
            if index < 0 or index >= len(shots):
                raise ValueError("SHOT_PATCH_INDEX_INVALID: %s" % patch["path"])
            if field in PROTECTED_SHOT_FIELDS or field not in ALLOWED_SHOT_FIELDS:
                raise ValueError("SHOT_PATCH_FIELD_FORBIDDEN: %s" % field)
            shots[index][field] = copy.deepcopy(patch.get("value"))
            touched[shots[index]["id"]].add(field)
    return touched


def _bind_assets(shots, asset_contract):
    asset_contract = asset_contract if isinstance(asset_contract, dict) else {}
    references = [item for item in asset_contract.get("references") or [] if isinstance(item, dict)]
    by_tag = {str(item.get("tag")): item for item in references if item.get("tag")}
    shot_tags = asset_contract.get("shot_ref_tags") if isinstance(asset_contract.get("shot_ref_tags"), dict) else {}
    for shot in shots:
        tags = list(shot.get("ref_tags") or shot_tags.get(shot["id"]) or
                    shot_tags.get("beat_%02d" % shot["panel_index"]) or [])
        if tags:
            unknown = [tag for tag in tags if tag not in by_tag]
            if unknown:
                raise ValueError("ASSET_TAG_UNKNOWN[%s]: %s" % (shot["id"], ",".join(unknown)))
            shot["ref_tags"] = tags
            shot["references"] = [copy.deepcopy(by_tag[tag]) for tag in tags]
    return copy.deepcopy(asset_contract.get("characters") or [])


def compile_plan(source_facts, creative_brief, script_beats, shot_patches=None,
                 asset_contract=None, draft=False, source_hashes=None):
    enforce(source_facts, "source-facts", context="compile_plan.source_facts")
    enforce(creative_brief, "creative-brief", context="compile_plan.creative_brief")
    enforce(script_beats, "script-beats", context="compile_plan.script_beats")
    clients = {source_facts.get("client"), creative_brief.get("client"), script_beats.get("client")}
    if len(clients) != 1:
        raise ValueError("IR_CLIENT_MISMATCH")
    run_ids = {value for value in (source_facts.get("run_id"), creative_brief.get("run_id"),
                                   script_beats.get("run_id")) if value}
    if len(run_ids) > 1:
        raise ValueError("IR_RUN_ID_MISMATCH")
    known_fact_ids = {item["id"] for item in source_facts["facts"]}
    used_fact_ids = set(creative_brief.get("fact_refs") or [])
    for beat in script_beats["beats"]:
        used_fact_ids.update(beat.get("fact_refs") or [])
    unknown_facts = sorted(used_fact_ids - known_fact_ids)
    if unknown_facts:
        raise ValueError("IR_FACT_REF_UNKNOWN: %s" % ", ".join(unknown_facts))
    profile = creative_brief["profile"]
    role = creative_brief.get("storyboard_role") or (
        "design_board" if profile == "document_explainer" else "video_storyboard")
    if profile == "document_explainer" and role != "design_board":
        raise ValueError("DOCUMENT_EXPLAINER_ROLE_INVALID")
    beats = script_beats["beats"]
    shots = [_default_shot(beat, index, len(beats), role)
             for index, beat in enumerate(beats)]
    touched = _apply_shot_proposals(shots, _proposal_list(shot_patches))
    characters = _bind_assets(shots, asset_contract)
    digital_human = creative_brief.get("digital_human") or {}
    if not characters and isinstance(digital_human, dict) and digital_human.get("id"):
        characters = [{"id": digital_human["id"],
                       "appearance": digital_human.get("appearance") or "客户确认的数字人形象"}]
    if characters:
        character_ids = [item.get("id") for item in characters if isinstance(item, dict) and item.get("id")]
        for shot in shots:
            if not shot.get("characters") and character_ids:
                shot["characters"] = character_ids[:1]
    if not draft:
        creative_required = {"scene_prompt", "character_action", "composition", "lighting"}
        missing = {shot_id: sorted(creative_required - fields)
                   for shot_id, fields in touched.items()
                   if creative_required - fields}
        if missing:
            raise ValueError("FORMAL_SHOT_PATCHES_REQUIRED: %s" % json.dumps(
                missing, ensure_ascii=False, sort_keys=True))
    if profile in {"product_showcase", "product_with_human"}:
        missing_product = [shot["id"] for shot in shots
                           if not any((ref.get("type") in {"product_identity", "product_board", "product_usage_identity"})
                                      for ref in shot.get("references") or [])]
        if missing_product and not draft:
            raise ValueError("PRODUCT_REFERENCE_REQUIRED: %s" % ",".join(missing_product))
    if profile == "product_with_human" and not characters and not draft:
        raise ValueError("CHARACTER_REFERENCE_REQUIRED")
    contract = ({
        "kind": "design_board", "purpose": "render_contract_only",
        "composition_owner": "remotion", "video_model_usage": "do_not_submit_as_storyboard_ref",
        "content_owner": "remotion", "motion_owner": "hyperframes", "assembly_owner": "fuse",
    } if role == "design_board" else {
        "kind": "video_storyboard", "purpose": "confirmed_video_model_shot_contract",
        "composition_owner": "storyboard", "video_model_usage": "submit_per_shot",
        "content_owner": "external_video_model", "motion_owner": "hyperframes",
        "assembly_owner": "ffmpeg",
    })
    plan = {
        "contract_strictness": "draft" if draft else "production",
        "compiler": {"name": "storyboard_plan_compiler", "version": COMPILER_VERSION,
                     "source_hashes": source_hashes or {}},
        "client": creative_brief["client"],
        "run_id": creative_brief.get("run_id") or script_beats.get("run_id") or source_facts.get("run_id") or "draft",
        "project_title": creative_brief["objective"],
        "chain_profile": profile,
        "package": (creative_brief.get("package") or
                    workflow_contract.PROFILE_PACKAGE_IDS.get(profile)),
        "storyboard_role": role,
        "storyboard_aspect_ratio": "16:9",
        "output_ratio": creative_brief["output_ratio"],
        "storyboard_contract": contract,
        "facts_contract": {"fact_ids": sorted(known_fact_ids),
                           "used_fact_ids": sorted(used_fact_ids),
                           "forbidden_inferences": list(source_facts["forbidden_inferences"])},
        "creative_brief": {key: copy.deepcopy(value) for key, value in creative_brief.items()
                           if key not in {"schema_version", "client", "run_id"}},
        "characters": characters,
        "asset_refs": copy.deepcopy((asset_contract or {}).get("asset_refs") or {}),
        "shots": shots,
    }
    validation = validate_plan(plan)
    if not validation.get("ok"):
        raise ValueError("COMPILED_PLAN_INVALID: %s" % "；".join(validation.get("errors") or []))
    return plan, validation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-facts", required=True)
    parser.add_argument("--creative-brief", required=True)
    parser.add_argument("--script-beats", required=True)
    parser.add_argument("--shot-patches")
    parser.add_argument("--asset-contract")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--out", required=True)
    parser.add_argument("--validation-out")
    args = parser.parse_args(argv)
    paths = [args.source_facts, args.creative_brief, args.script_beats]
    if args.shot_patches:
        paths.append(args.shot_patches)
    if args.asset_contract:
        paths.append(args.asset_contract)
    source_hashes = {os.path.basename(path): _sha(path) for path in paths}
    plan, validation = compile_plan(
        _read(args.source_facts), _read(args.creative_brief), _read(args.script_beats),
        shot_patches=_read(args.shot_patches) if args.shot_patches else None,
        asset_contract=_read(args.asset_contract) if args.asset_contract else None,
        draft=args.draft, source_hashes=source_hashes)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    atomic_write_json(args.out, plan)
    validation_path = args.validation_out or args.out + ".validation.json"
    atomic_write_json(validation_path, validation)
    print(json.dumps({"ok": True, "plan": os.path.abspath(args.out),
                      "validation": os.path.abspath(validation_path),
                      "shot_count": len(plan["shots"]),
                      "contract_strictness": plan["contract_strictness"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
