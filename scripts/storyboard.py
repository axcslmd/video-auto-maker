#!/usr/bin/env python3
"""Storyboard / cast-board generator using BasicRouter image API (gpt-image-2).

Workflow position:
  finalized script JSON -> storyboard_plan.json -> gpt-image-2 storyboards/cast board
  -> customer confirmation -> video_engine.py renders final video.

The local host agent is responsible for parsing the approved script into a
structured plan. This script only turns that plan into preview images and an
index markdown file so the customer can SEE and confirm before Credits are spent
on video.

Input schema (JSON):
{
  "project_title": "Client Brand 15s promo",
  "client": "acme",
  "output_ratio": "9:16",
  "storyboard_aspect_ratio": "16:9",
  "visual_style": "clean premium commercial, brand primary-color accents",
  "continuity": {
    "background": "same bright studio / same store scene across stitched segments",
    "character_identity": "same face, hairstyle, wardrobe and posture language",
    "wardrobe": "same outfit and accessories",
    "voice": "same speaker / voice tone / language",
    "bgm": "same music cue, tempo and mood",
    "lighting": "same key-light direction and color temperature"
  },
  "characters": [
    {"id":"host", "name":"粤语女主持", "role":"host", "appearance":"...",
     "costume":"...", "personality":"...", "voice":"..."}
  ],
  "shots": [
     {"id":"s1", "duration":3, "dialogue":"...", "visual":"...",
     "camera":"left-front 45° medium shot, slow push-in", "shot_size":"medium close-up",
     "camera_movement":"slow push-in", "angle_offset":"left-front 45 degrees",
     "composition":"rule of thirds, product on lower-right intersection",
     "lighting":"soft key light from camera-left, warm rim light",
     "character_action":"host picks up product and turns it toward camera",
     "micro_expression":"warm confident smile grows as benefit is revealed",
     "scene_prompt":"premium studio, warm neutral background, clean display table",
     "character_prompt":"same face, same hair, same white blazer, moderate gestures, direct-to-lens trust-building",
     "prop_prompts":["product: stable real shape, color, logo position and material"],
      "panel_plan":["establish","product/action detail","reaction or proof","resolution"],
     "audio":{"voice":"same calm Cantonese host voice", "bgm":"subtle upbeat premium BGM", "sfx":"soft whoosh on transition"},
     "characters":["host"], "props":"client product"}
  ]
}

Outputs:
  output/storyboard/<run-id>/cast_board.jpg              # character reference sheet: six required views per character
   output/storyboard/<run-id>/shot_01_s1.jpg ...          # one 16:9 cinematic storyboard per segment (script-driven panel count and automatic grid)
  output/storyboard/<run-id>/storyboard_index.md         # markdown gallery with absolute paths
  output/storyboard/<run-id>/storyboard_embedded.md      # markdown gallery with embedded image data
  output/storyboard/<run-id>/storyboard_preview.html     # self-contained preview page
  output/storyboard/<run-id>/storyboard_result.json      # machine-readable result
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from project_utils import atomic_write_json, require_contained_path  # noqa: E402
import br_client as _br  # noqa: E402
import br_client  # noqa: E402
import asset_prep  # noqa: E402
import media_tools  # noqa: E402
import artifact_contract  # noqa: E402
import key_setup  # noqa: E402
import prompt_review_contract  # noqa: E402
import schema_validate  # noqa: E402
import storyboard_preview  # noqa: E402
import storyboard_plan_audit  # noqa: E402
import storyboard_runs  # noqa: E402
import storyboard_semantics  # noqa: E402
import storyboard_usage_review  # noqa: E402
import run_manifest as run_manifest_store  # noqa: E402
import workflow_contract  # noqa: E402
import ux  # noqa: E402
import cast_reference_pack  # noqa: E402
from aspect_ratio import STORYBOARD_RATIO, output_ratio as normalized_output_ratio  # noqa: E402
from image_utils import image_type  # noqa: E402
from video_segmentation import partition_shots, SEEDANCE_MAX_SECONDS  # noqa: E402
from board_confirm import _source_refs_fingerprint  # noqa: E402 — v4 shim


DEFAULT_MODEL = "gpt-image-2"
IMAGE_MAX_WAIT = 900
IMAGE_RETRIES = 2
MAX_DESIGN_BOARD_QC_ATTEMPTS = 2
MAX_IMAGE_REFERENCES_PER_REQUEST = 4
TAG_PATTERN = re.compile(r"^@[a-z][a-z0-9_]*$")
SHOT_REFERENCE_POLICY_VERSION = "shot-reference-policy-v3-no-character-lock"
PRODUCT_USAGE_POLICY_VERSION = "product-usage-policy-v5-physical-relation-contract"
CAST_SOURCE_POLICY_VERSION = "cast-source-policy-v2-prompt-and-identity-contract"
EMPTY_SOURCE_FINGERPRINT = hashlib.sha256(b"").hexdigest()
_PROCESS_GENERATION_OWNER = "storyboard-%s-%s" % (os.getpid(), uuid.uuid4().hex)
DEFAULT_DESIGN_BOARD_PANEL_PLAN = [
    "one same-timeline execution frame confirming PPT/content hierarchy, presenter window, safe zones, and deterministic Remotion/HyperFrames compositing",
]

USAGE_GEOMETRY_REVIEW_REQUIRED_CHECKS = (
    storyboard_usage_review.USAGE_GEOMETRY_REVIEW_REQUIRED_CHECKS)
USAGE_GEOMETRY_REVIEW_PANEL_CHECKS = (
    storyboard_usage_review.USAGE_GEOMETRY_REVIEW_PANEL_CHECKS)
USAGE_GEOMETRY_REVIEW_CRITICAL_PANELS = (
    storyboard_usage_review.USAGE_GEOMETRY_REVIEW_CRITICAL_PANELS)


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or ""))
    return s.strip("_") or "shot"


def _listify(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def ratio_to_image_ratio(video_ratio):
    """BasicRouter image ratio uses the same style strings; keep common values."""
    if video_ratio in ("9:16", "16:9", "1:1", "4:3", "3:4"):
        return video_ratio
    return "9:16"


def normalize_panel_plan(shot):
    """Return the shot's panel plan as-is (script-driven panel count).

    2026-08-05 revision: panel count is no longer forced to 12. The script
    co-creation stage decides how many panels/beats a shot needs. Missing panel
    plans remain missing so validation can fail closed instead of silently
    changing the customer-approved shot structure.
    """
    raw = shot.get("panel_plan") or shot.get("twelve_panel_plan")
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        return []
    return list(raw)


def _generation_owner_token():
    """Distinguish concurrent invocations inside one host Python process."""
    return "%s-thread-%s" % (_PROCESS_GENERATION_OWNER, threading.get_ident())


def _grid_for_panel_count(panel_count, portrait):
    """Pick a (cols, rows) grid that fits panel_count panels with minimal empty cells.

    Landscape (16:9) storyboards favour more columns than rows; portrait
    (9:16) storyboards favour more rows than columns. 12 remains a valid
    value (falls through to 4x3/3x4 as before) but is no longer the only one.
    """
    common_landscape = {
        1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2),
        7: (4, 2), 8: (4, 2), 9: (3, 3), 10: (4, 3), 11: (4, 3), 12: (4, 3),
    }
    cols, rows = common_landscape.get(panel_count, (4, 3))
    if panel_count > 12:
        cols = math.ceil(math.sqrt(panel_count * 4 / 3))
        rows = math.ceil(panel_count / cols)
    if portrait:
        cols, rows = rows, cols
    return cols, rows


# Category -> stable base @tag. Mirrors script_splitter._REFERENCE_META so the
# same asset gets the same @tag both in the storyboard preview prompt and in
# the later per-shot video submission prompt (script_splitter.py).
_ASSET_BASE_TAG = {
    "product_images": "@product",
    "product_usage_images": "@usage",
    "scene_images": "@scene",
    "digital_human_portraits": "@host",
}


def _assign_asset_tags(product_imgs, product_usage_imgs, scene_imgs, portrait_imgs):
    """Return [(tag, url, category_key), ...] preserving category order.

    Same-category duplicates get a numeric suffix (@host, @host2, @host3...),
    matching script_splitter._next_tag numbering so tags stay stable when the
    plan later flows into script_splitter.py for video submission.
    """
    buckets = [
        ("product_images", product_imgs),
        ("product_usage_images", product_usage_imgs),
        ("scene_images", scene_imgs),
        ("digital_human_portraits", portrait_imgs),
    ]
    tagged = []
    for category, urls in buckets:
        base = _ASSET_BASE_TAG[category]
        for i, url in enumerate(urls or []):
            tag = base if i == 0 else "%s%d" % (base, i + 1)
            tagged.append((tag, url, category))
    return tagged


def client_slug(plan):
    """Stable short slug for run directories; keeps clients/sessions separated."""
    for key in ("client", "client_name", "brand", "brand_name", "project_title"):
        v = plan.get(key)
        if v:
            return safe_name(v).lower()[:48]
    return "client"


def run_output_dir(base_out_dir, plan, run_id=None, flat=False):
    """Return the actual output directory.

    Best practice for any host agent is one immutable directory per generated
    storyboard session, so reruns never overwrite earlier customer-approved
    preview boards. Passing --flat preserves the legacy behavior for debugging.
    """
    if flat:
        return base_out_dir
    if run_id:
        rid = safe_name(run_id)
    else:
        # A timestamp creates a new empty run on every retry, defeating the
        # progress file. Derive the default from the approved plan instead so
        # an interrupted invocation automatically resumes the same run. A plan
        # edit intentionally produces a new directory.
        fingerprint = plan_fingerprint(plan)
        rid = "%s_%s" % (client_slug(plan), fingerprint)
    return os.path.join(base_out_dir, rid)


def plan_fingerprint(plan, allow_unconfirmed_product_images=False):
    """Stable identity for a canonical storyboard plan.

    Callers may provide the authored, normalized, hydrated, or duration-
    partitioned representation. Canonicalization is idempotent, so every stage
    binds approval to the same semantic plan identity.

    Runtime fields injected by prompt review are excluded: they are generation
    inputs, not authored storyboard_plan.json identity. Otherwise producers and
    consumers can disagree about the current revision for the same logical
    run_id.
    """
    plan = canonical_storyboard_plan(
        plan,
        allow_unconfirmed_product_images=allow_unconfirmed_product_images)
    if allow_unconfirmed_product_images:
        # A draft that deliberately retains unconfirmed product anchors must
        # never share an identity with an approvable/formal storyboard plan.
        plan["_product_source_policy"] = "draft_allow_unconfirmed"
    plan.pop("_asset_composition_briefs", None)
    for shot in (plan.get("shots") or []):
        shot.pop("approved_prompt_zh", None)
        shot.pop("approved_submission_prompt_zh", None)
        shot.pop("references", None)
    return hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def _digest_plan_for_prompt_review(plan):
    """Use the same full-plan identity as prompt_review without importing it.

    prompt_review.polish stores storyboard.plan_fingerprint(plan) after
    calling expand_product_sku_refs(canonical_storyboard_plan(plan)).
    Mirror that exactly so the confirmation gate matches.
    """
    canonical = expand_product_sku_refs(canonical_storyboard_plan(plan))
    return plan_fingerprint(canonical)


def visual_plan_fingerprint(plan):
    """Fingerprint of the plan's visual fields, excluding dialogue/voiceover/audio.

    Storyboard image generation (shot_prompt) contains no dialogue — spoken
    lines only appear in the customer-facing index/preview text. A dialogue-only
    edit therefore cannot change the storyboard image, so the storyboard
    prompt-review gate keys off this visual fingerprint instead of the full
    plan_fingerprint. The full plan_fingerprint still gates run directory
    identity and video handoff.

    Canonicalization happens exactly once here (expand + canonical). Callers
    must NOT pre-canonicalize the plan, because partition_shots is not
    idempotent once dialogue has been split — a second canonical pass would
    re-split the already-split dialogue and make the fingerprint drift on
    dialogue-only edits, defeating the whole purpose of this gate.
    """
    plan = expand_product_sku_refs(canonical_storyboard_plan(plan))
    plan.pop("_asset_composition_briefs", None)
    for shot in (plan.get("shots") or []):
        shot.pop("approved_prompt_zh", None)
        shot.pop("approved_submission_prompt_zh", None)
        shot.pop("references", None)
        shot.pop("dialogue", None)
        shot.pop("voiceover", None)
        shot.pop("audio", None)
    return hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def shot_fingerprint(shot):
    """Return the identity of one storyboard panel's visual contract.

    Dialogue and audio are intentionally excluded: neither changes the approved
    composition image.  This lets downstream handoff invalidate only the panel
    whose visual direction changed instead of treating an unrelated line edit as
    a reason to discard every confirmed storyboard panel.
    """
    def normalize(value):
        if isinstance(value, dict):
            ignored = {
                "dialogue", "voiceover", "audio", "approved_prompt_zh",
                "approved_submission_prompt_zh", "references",
                "motion_elements",
            }
            return {key: normalize(item) for key, item in value.items()
                    if key not in ignored}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            text = value.replace("字幕", "")
            for char in (" ", "\t", "\n", "。", ".", "，", ",", "；", ";", "、", "：", ":"):
                text = text.replace(char, "")
            return text
        return value
    visual = normalize(json.loads(json.dumps(shot or {}, ensure_ascii=False)))
    return hashlib.sha256(
        json.dumps(visual, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def reference_fingerprint(registry):
    def ref_identity(item):
        url = str(item.get("url") or "")
        identity = {"tag": item.get("tag"), "url": url, "type": item.get("type")}
        if url and not re.match(r"^(https?|data):", url) and os.path.isfile(url):
            identity["sha256"] = _file_sha256(url)
        return identity

    payload = {
        "policy": SHOT_REFERENCE_POLICY_VERSION,
        "refs": [ref_identity(item) for item in (registry or [])],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]


def _digest_visual_plan_for_review(plan):
    """Visual-only fingerprint mirroring prompt_review's stored value."""
    return visual_plan_fingerprint(plan)


def _load_prompt_review_for_shots(path, plan):
    if not path:
        raise br_client.BRError(
            "PROMPT_REVIEW_REQUIRED: 正式生图前必须先生成并确认中文提示词审核文件；"
            "请将 --prompt-review 指向审核文件，或把 storyboard_prompt_review.json 放在计划文件同目录。")
    if not os.path.isfile(path):
        raise br_client.BRError(
            "PROMPT_REVIEW_REQUIRED: 中文提示词审核文件不存在：%s；"
            "请先运行 prompt_review.py polish 并 confirm。" % path)
    with open(path, encoding="utf-8") as handle:
        review = json.load(handle)
    if review.get("status") != "confirmed":
        raise br_client.BRError(
            "PROMPT_REVIEW_REQUIRED: 提示词审核文件未确认（status=%s）。"
            "请运行 prompt_review.py confirm --customer-confirmation '<客户确认原话>' "
            "确认后再生成。" % review.get("status"))
    if review.get("stage") != "storyboard":
        raise br_client.BRError(
            "PROMPT_REVIEW_REQUIRED: 提示词审核文件阶段不匹配（stage=%s，期望 storyboard）。"
            "请重新运行 prompt_review.py polish --stage storyboard。" % review.get("stage"))
    try:
        prompt_review_contract.assert_current(review, path=path)
    except ValueError as error:
        raise br_client.BRError(str(error)) from error
    expected_visual_fp = _digest_visual_plan_for_review(plan)
    review_visual_fp = review.get("visual_plan_fingerprint")
    if review_visual_fp is not None:
        # New-style review carries a visual fingerprint; only visual changes
        # (visual/camera/action/panel_plan/...) force re-polish. Dialogue-only
        # edits keep the approved review valid because shot_prompt contains no
        # dialogue — the storyboard image is unchanged.
        if review_visual_fp != expected_visual_fp:
            raise br_client.BRError(
                "PROMPT_REVIEW_REQUIRED: 提示词审核的画面指纹与当前计划不匹配\n"
                "  审核画面指纹: %s\n  当前画面指纹: %s\n"
                "  原因：计划的画面相关字段（visual/camera/动作/分镜等）在审核后被修改过。"
                "请重新运行 prompt_review.py polish + confirm。"
                % (review_visual_fp, expected_visual_fp))
    else:
        # Legacy review file (pre visual_plan_fingerprint): fall back to the
        # strict full-plan fingerprint to preserve existing behavior.
        expected_fp = _digest_plan_for_prompt_review(plan)
        if review.get("plan_fingerprint") != expected_fp:
            raise br_client.BRError(
                "PROMPT_REVIEW_REQUIRED: 提示词审核的指纹与当前计划不匹配\n"
                "  审核文件指纹: %s\n  当前计划指纹: %s\n"
                "  原因：计划在审核确认后被修改过。请重新运行 prompt_review.py polish + confirm。"
                % (review.get("plan_fingerprint"), expected_fp))
    prompts = {str(item.get("shot_id")): item
               for item in review.get("prompts") or []}
    expected_assets = {
        item.get("asset_id"): item
        for item in asset_prompt_review_items(plan)
        if item.get("asset_id")
    }
    reviewed_assets = {
        item.get("asset_id"): item
        for item in (review.get("asset_prompts") or [])
        if isinstance(item, dict) and item.get("asset_id")
    }
    for asset_id, expected in expected_assets.items():
        reviewed = reviewed_assets.get(asset_id) or {}
        if not reviewed.get("submission_prompt_zh"):
            raise br_client.BRError(
                "PROMPT_REVIEW_REQUIRED: 缺少资产级提示词 %s。"
                "请重新运行 prompt_review.py capture-storyboard + confirm。"
                % asset_id)
        if reviewed.get("prompt_fingerprint") != expected.get("prompt_fingerprint"):
            raise br_client.BRError(
                "PROMPT_REVIEW_REQUIRED: 资产级提示词 %s 已过期，产品身份/使用关系合同"
                "或生成策略已变化。请重新运行 prompt_review.py capture-storyboard + confirm。"
                % asset_id)
    composition_briefs = {
        asset_id: item.get("composition_brief")
        for asset_id, item in reviewed_assets.items()
        if isinstance(item, dict) and item.get("composition_brief")
    }
    if composition_briefs:
        plan["_asset_composition_briefs"] = composition_briefs
    global_refs = [ref for ref in (plan.get("references") or []) if isinstance(ref, dict)]
    for shot in plan.get("shots") or []:
        item = prompts.get(str(shot.get("id"))) or {}
        if not (item.get("prompt_zh") or item.get("submission_prompt_zh")):
            raise br_client.BRError(
                "PROMPT_REVIEW_REQUIRED: 缺少镜头 %s 的确认提示词。"
                "请重新运行 prompt_review.py polish + confirm。" % shot.get("id"))
        if item.get("prompt_zh"):
            shot["approved_prompt_zh"] = item["prompt_zh"]
        if item.get("submission_prompt_zh"):
            shot["approved_submission_prompt_zh"] = item["submission_prompt_zh"]
        if global_refs and not shot.get("references"):
            wanted = shot.get("ref_tags") or []
            if isinstance(wanted, str):
                wanted = [wanted]
            wanted = set(wanted)
            selected = [ref for ref in global_refs
                        if not wanted or ref.get("tag") in wanted]
            if selected:
                shot["references"] = json.loads(
                    json.dumps(selected, ensure_ascii=False))


def canonical_storyboard_plan(plan, allow_unconfirmed_product_images=False):
    """Return the exact normalized plan used by storyboard and video handoff."""
    from storyboard_validator import normalize_plan_motion_elements
    canonical = json.loads(json.dumps(plan, ensure_ascii=False))
    storyboard_semantics.reconcile(canonical, root=ROOT)
    canonical["output_ratio"] = normalized_output_ratio(canonical)
    canonical["storyboard_aspect_ratio"] = STORYBOARD_RATIO
    canonical.pop("aspect_ratio", None)
    canonical, _ = normalize_plan_motion_elements(canonical)
    canonical = _hydrate_plan_asset_refs(
        canonical,
        allow_unconfirmed_product_images=allow_unconfirmed_product_images)
    continuity = canonical.get("continuity_contract") or {}
    if continuity:
        for shot in canonical.get("shots") or []:
            shot.setdefault("scene_id", continuity.get("scene_id"))
            lock = " CONTINUITY LOCK: %s. %s. %s. %s." % (
                continuity.get("background", "same background"),
                continuity.get("lighting", "same lighting"),
                continuity.get("host_position", "same host position"),
                continuity.get("product_state", "same product state"))
            shot["scene_prompt"] = (str(shot.get("scene_prompt") or "") + lock).strip()
            shot["continuity_in"] = shot.get("continuity_in") or continuity.get("transition")
    shots = canonical.get("shots") or []
    preserve = str(canonical.get("scene_type") or "").lower() in {
        "oral-broadcast", "oralbroadcast", "broadcast", "口播", "普通口播"}
    # Storyboard contact sheets are approved by the customer at the script-shot
    # level. Do not merge adjacent short shots here; video segmentation can
    # still aggregate later when preparing model submissions. Only split when a
    # single shot exceeds the model duration limit.
    if not preserve:
        preserve = all(float(shot.get("duration") or shot.get("seconds") or 1)
                       <= SEEDANCE_MAX_SECONDS for shot in shots)
    canonical["shots"] = partition_shots(
        shots, max_seconds=SEEDANCE_MAX_SECONDS, preserve_shots=preserve)
    # A shot is a storyboard panel and a video generation unit. Persist its
    # position in the client-approved contact sheet so later img2img expansion
    # can identify it without relying on filename order or a separate timeline.
    for index, shot in enumerate(canonical["shots"], 1):
        shot["panel_index"] = index
    _enrich_storyboard_contract(canonical)
    canonicalize_usage_ref_tags(canonical)
    _sync_usage_reference_contract(canonical)
    return canonical


def _enrich_storyboard_contract(plan):
    """Fill deterministic director intent and design-composition contracts.

    These are execution contracts, not creative facts: fallbacks describe each
    shot's structural job without inventing claims, products, or scene content.
    """
    shots = plan.get("shots") or []
    last = len(shots) - 1
    design_board = _looks_like_design_board(plan)
    for index, shot in enumerate(shots):
        if index == 0:
            arc = "opening"
            narrative = "establish the audience problem, context, or opening hook"
            felt = "recognition and curiosity"
        elif index == last:
            arc = "resolution"
            narrative = "resolve the explanation and land the approved conclusion or next step"
            felt = "clarity and confidence"
        else:
            arc = "development"
            narrative = "advance the approved explanation with one clear visual beat"
            felt = "growing understanding and trust"
        shot.setdefault("arc_position", arc)
        shot.setdefault("narrative_function", narrative)
        shot.setdefault("felt_intent", felt)
        shot.setdefault(
            "director_voice",
            ("restrained enterprise explainer; prioritize content hierarchy and compositing safe zones"
             if design_board else
             "continuity-first cinematic direction; prioritize concrete visible action and shot contrast"))
        shot.setdefault("shot_size", shot.get("camera") or "medium shot")
        shot.setdefault("camera_movement", "locked or gently motivated camera")
        shot.setdefault("composition", "clear foreground, subject, and negative-space hierarchy")
        shot.setdefault("lighting", "consistent with the authored scene and continuity contract")
        shot.setdefault("character_action", shot.get("action") or "preserve the authored action")
        shot.setdefault("micro_expression", "preserve the authored emotional state")
        shot.setdefault("scene_prompt", shot.get("visual") or "preserve the authored scene")
        shot.setdefault("prop_prompts", [])
        shot.setdefault("audio", {})
        if design_board:
            shot.setdefault("panel_plan", list(DEFAULT_DESIGN_BOARD_PANEL_PLAN))
            motion_plan = _motion_plan_for_design_board_shot(shot)
            shot["motion_plan"] = motion_plan
            shot["asset_slots"] = motion_plan.get("asset_slots") or []
    return plan


def _read_result(path):
    return storyboard_runs.read_result(path)


def _legacy_plan_matches(plan, previous):
    return storyboard_runs.legacy_plan_matches(plan, previous)


def resolve_run_output_dir(base_out_dir, plan, run_id=None, flat=False):
    return storyboard_runs.resolve_run_output_dir(
        base_out_dir, plan, run_id=run_id, flat=flat,
        run_output_dir_fn=run_output_dir,
        plan_fingerprint_fn=plan_fingerprint,
        safe_name_fn=safe_name)


def _run_pointer_path(base_out_dir, run_id):
    return storyboard_runs.run_pointer_path(
        base_out_dir, run_id, safe_name_fn=safe_name)


def _write_run_pointer(base_out_dir, run_id, plan, out_dir, result_path, *, stage=None,
                       plan_fingerprint_value=None,
                       visual_plan_fingerprint_value=None):
    return storyboard_runs.write_run_pointer(
        base_out_dir, run_id, plan, out_dir, result_path, stage=stage,
        plan_fingerprint_value=plan_fingerprint_value,
        visual_plan_fingerprint_value=visual_plan_fingerprint_value,
        safe_name_fn=safe_name,
        client_slug_fn=client_slug,
        plan_fingerprint_fn=plan_fingerprint,
        visual_plan_fingerprint_fn=visual_plan_fingerprint)


def _refresh_workflow_canvas(results, result_path, *, reason=None):
    """Best-effort UI sync through the observer's process boundary.

    Storyboard is a business-stage owner; workflow_canvas is an optional view.
    Running its refresh entry point out-of-process prevents the observer from
    importing the pipeline back into the generation transaction.
    """
    try:
        command = [
            sys.executable,
            os.path.join(HERE, "workflow_canvas.py"),
            "refresh",
            "--storyboard-result", os.path.abspath(result_path),
        ]
        client = (results or {}).get("client")
        run_id = (results or {}).get("run_id")
        if client:
            command.extend(["--client", str(client)])
        if run_id:
            command.extend(["--run-id", str(run_id)])
        if reason:
            command.extend(["--reason", str(reason)])
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise RuntimeError(detail[-500:])
        return completed.stdout.strip() or None
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # Canvas rendering is observability, never the owner of the generation
        # transaction.  Keep the media checkpoint valid and surface a concise
        # diagnostic instead of rolling back an already completed image.
        print("[storyboard] workflow canvas refresh skipped: %s" % exc, flush=True)
        return None


_RESULT_STAGE_TO_MANIFEST_STAGE = {
    "product_board": "product_board",
    "cast_board": "cast_board",
    "product_usage": "product_usage",
    "product_usage_image": "product_usage",
    "storyboard": "storyboard",
}


def _manifest_stage_name(stage):
    return _RESULT_STAGE_TO_MANIFEST_STAGE.get(str(stage or "").strip())


def _discover_run_manifest(plan, client, run_id):
    """Resolve an explicit or conventional manifest without inventing one."""
    explicit = (plan or {}).get("manifest_path") or (plan or {}).get("run_manifest")
    if explicit:
        candidate = explicit if os.path.isabs(str(explicit)) else os.path.join(ROOT, str(explicit))
        candidate = os.path.abspath(candidate)
        if not os.path.isfile(candidate):
            raise br_client.BRError("STORYBOARD_RUN_MANIFEST_MISSING: %s" % candidate)
        return candidate
    if not client or not run_id:
        return None
    candidate = os.path.abspath(os.path.join(
        ROOT, "output", str(client), safe_name(run_id), "run_manifest.json"))
    return candidate if os.path.isfile(candidate) else None


def _validate_manifest_identity(results, manifest):
    run_manifest_store.identity_gate(
        manifest, client=(results or {}).get("client"))
    if str(manifest.get("run_id") or "") != str((results or {}).get("run_id") or ""):
        raise br_client.BRError("STORYBOARD_MANIFEST_IDENTITY_MISMATCH")
    return True


def _manifest_approval_state(results, stage):
    """Return None for a legacy unbound run, otherwise the authoritative state."""
    manifest_path = (results or {}).get("manifest_path")
    if not manifest_path:
        return None
    manifest = run_manifest_store.load_manifest(os.path.abspath(manifest_path))
    _validate_manifest_identity(results, manifest)
    return bool(run_manifest_store.approval_is_current(manifest, stage))


def _effective_board_approval(results, stage, local_approved):
    manifest_state = _manifest_approval_state(results, stage)
    return bool(local_approved if manifest_state is None else manifest_state)


def _begin_manifest_generation(results, stage, *, force=False):
    """Pass the dependency gate before any paid submission or imported finish."""
    manifest_path = (results or {}).get("manifest_path")
    if not manifest_path:
        return None
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest_store.load_manifest(manifest_path)
    _validate_manifest_identity(results, manifest)
    generation = (manifest.get("generation") or {}).get(stage) or {}
    status = generation.get("status")
    if not force and status == "pending_approval":
        return manifest
    if (not force and status == "approved" and
            run_manifest_store.approval_is_current(manifest, stage)):
        return manifest
    return run_manifest_store.acquire_generation_lease(
        manifest_path, stage, _generation_owner_token(), force=force,
        ttl_seconds=300)


def _result_stage_artifact_paths(results, result_path, stage):
    # Product/cast/usage approvals must bind only immutable, customer-viewed
    # media.  storyboard_result/index/preview/audit are shared run checkpoints
    # and are deliberately rewritten by later stages; tracking them here made
    # a valid board confirmation become stale as soon as storyboard generation
    # refreshed the audit or preview.
    stable_board_stage = stage in {
        "product_board", "cast_board", "product_usage",
    }
    paths = [] if stable_board_stage else [os.path.abspath(result_path)]

    def add(value):
        if isinstance(value, dict):
            value = value.get("abspath") or value.get("path")
        if value:
            value = os.path.abspath(str(value))
            if os.path.isfile(value) and value not in paths:
                paths.append(value)

    board_key = {
        "product_board": "product_board",
        "cast_board": "cast_board",
        "product_usage": "product_usage_image",
    }.get(stage)
    if board_key:
        board = (results or {}).get(board_key) or {}
        add(board)
        for value in board.get("panel_paths") or []:
            add(value)
    if not stable_board_stage:
        for key in (
                "index_md", "embedded_md", "preview_html", "composite_plan_path",
                "plan_audit_json", "plan_audit_md", "execution_preview_result"):
            add((results or {}).get(key))
    if stage == "storyboard":
        for item in (results or {}).get("shots") or []:
            add(item)
            qc_path = ((item.get("design_board_qc") or {}).get("commercial_qc")
                       if isinstance(item, dict) else None)
            add(qc_path)
        for item in (results or {}).get("layout_boards") or []:
            add(item)
    return paths


def _register_storyboard_result_artifacts(results, result_path):
    """Bind the current board/storyboard checkpoint to its authoritative stage."""
    manifest_path = (results or {}).get("manifest_path")
    if not manifest_path:
        return None
    stage = _manifest_stage_name((results or {}).get("stage"))
    if not stage:
        raise br_client.BRError("STORYBOARD_MANIFEST_STAGE_UNKNOWN")
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest_store.load_manifest(manifest_path)
    _validate_manifest_identity(results, manifest)
    generation = (manifest.get("generation") or {}).get(stage) or {}
    status = generation.get("status")
    if status == "approved":
        if not run_manifest_store.approval_is_current(manifest, stage):
            raise br_client.BRError(
                "STORYBOARD_MANIFEST_APPROVAL_STALE: %s" % stage)
        return manifest
    paths = _result_stage_artifact_paths(results, result_path, stage)
    expected_revision = int(manifest.get("revision") or 0)
    if status == "pending_approval":
        run_manifest_store.add_stage_artifacts(manifest, stage, paths)
    else:
        run_manifest_store.generation_gate(manifest, stage)
        run_manifest_store.mark_generation_finished(manifest, stage, paths)
    run_manifest_store.save_manifest(
        manifest, manifest_path, expected_revision=expected_revision)
    return manifest


def _preflight_manifest_approval(results, stage):
    manifest_path = (results or {}).get("manifest_path")
    if not manifest_path:
        return None
    manifest = run_manifest_store.load_manifest(os.path.abspath(manifest_path))
    _validate_manifest_identity(results, manifest)
    run_manifest_store.generation_gate(manifest, stage)
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if generation.get("status") != "pending_approval":
        raise br_client.BRError(
            "APPROVAL_BLOCKED: %s 尚未处于 pending_approval" % stage)
    return manifest


def _approve_manifest_stage(results, result_path, stage, approval_path, record):
    manifest_path = (results or {}).get("manifest_path")
    if not manifest_path:
        return None
    manifest_path = os.path.abspath(manifest_path)
    manifest = _preflight_manifest_approval(results, stage)
    expected_revision = int(manifest.get("revision") or 0)
    paths = _result_stage_artifact_paths(results, result_path, stage)
    paths.append(os.path.abspath(approval_path))
    run_manifest_store.add_stage_artifacts(manifest, stage, paths)
    run_manifest_store.bind_customer_approval_evidence(manifest, stage, {
        "confirmed_by_customer": True,
        "customer_confirmation": record.get("customer_confirmation"),
        "confirmation_source": record.get("confirmation_source"),
        "confirmed_at": record.get("confirmed_at"),
        "approval_path": os.path.abspath(approval_path),
        "artifact_sha256": (record.get("approval_artifact_sha256") or
                            record.get("board_sha256") or
                            record.get("storyboard_sha256") or
                            record.get("plan_fingerprint")),
    })
    run_manifest_store.approve(manifest, stage, strict=True)
    run_manifest_store.save_manifest(
        manifest, manifest_path, expected_revision=expected_revision)
    return manifest


def _atomic_json_replace(path, payload):
    atomic_write_json(path, payload)


def _restore_file_bytes(path, original):
    if original is None:
        if os.path.exists(path):
            os.remove(path)
        return
    tmp = path + ".rollback"
    Path(tmp).write_bytes(original)
    os.replace(tmp, path)


def resolve_current_storyboard_dir(base_out_dir, run_id, plan=None,
                                   allow_unconfirmed_product_images=False):
    fingerprint_fn = lambda value: plan_fingerprint(
        value,
        allow_unconfirmed_product_images=allow_unconfirmed_product_images)
    canonical_fn = lambda value: canonical_storyboard_plan(
        value,
        allow_unconfirmed_product_images=allow_unconfirmed_product_images)
    return storyboard_runs.resolve_current_storyboard_dir(
        base_out_dir, run_id, plan,
        safe_name_fn=safe_name,
        plan_fingerprint_fn=fingerprint_fn,
        canonical_storyboard_plan_fn=canonical_fn,
        expand_product_sku_refs_fn=expand_product_sku_refs)


def download_first_image(api_key, prompt, out_path, *, model=DEFAULT_MODEL,
                         ratio="9:16", image_urls=None, on_progress=None,
                         resume_task_id=None, sync_img2img=False, force=False):
    """生成一张图并下载到 out_path。

    image_urls: 可选参考图列表（本地 data URL 或 https URL）。
    传入时走 img2img，不传则纯文生图。sync_img2img 保留为旧调用方兼容参数；
    正式流程统一走 BasicRouter 文档的异步 /v1/image-generations + retrieve
    轮询，避免同步 /ai/createImage 长连接在大图生成时断开。
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    from image_utils import image_type
    if (not force and os.path.isfile(out_path) and os.path.getsize(out_path) > 0 and
            image_type(out_path) in {"png", "jpeg", "webp"}):
        return {"url": "", "path": out_path, "abspath": os.path.abspath(out_path),
                "skipped": True}
    last_error = None
    result_url = None
    task_id = resume_task_id
    request_payload = {
        "stage": "storyboard_image", "prompt": prompt, "model": model,
        "ratio": ratio, "resolution": "2k", "image_urls": image_urls or [],
        "out_name": os.path.basename(out_path),
    }
    request_id = "image-" + artifact_contract.sha256_json(request_payload)
    for attempt in range(IMAGE_RETRIES + 1):
        try:
            task_id = task_id or br_client.create_image_generation(
                api_key, prompt, model=model, count=1, resolution="2k",
                ratio=ratio, image_urls=image_urls or [], request_id=request_id)
            if on_progress:
                on_progress({"status": "submitted", "task_id": task_id, "attempt": attempt + 1})

            def tick(status, waited):
                if on_progress and (waited == 0 or waited % 30 == 0):
                    on_progress({"status": status, "waited": waited, "task_id": task_id})

            urls = br_client.wait_image_generation(
                api_key, task_id, interval=5, max_wait=IMAGE_MAX_WAIT, on_tick=tick)
            if not urls:
                raise br_client.BRError("image task succeeded but returned no image")
            result_url = urls[0]
            # Result URLs come from BasicRouter's image-generation retrieve
            # endpoint. Some customer machines terminate HTTPS through a
            # local loopback proxy even when the result hostname resolves
            # publicly, so allow a non-public socket peer only for this
            # provider-returned media download path. User-supplied remote
            # source downloads keep the default stricter SSRF check.
            br_client.download(result_url, out_path, allow_nonpublic_peer=True)
            if image_type(out_path) not in {"png", "jpeg", "webp"}:
                raise br_client.BRError("image task downloaded an invalid image")
            return {"url": result_url, "path": out_path,
                    "abspath": os.path.abspath(out_path), "task_id": task_id,
                    "request_id": request_id, "sha256": _file_sha256(out_path)}
        except (OSError, ValueError, TypeError, RuntimeError, br_client.BRError) as exc:
            last_error = exc
            if br_client.is_insufficient_credit(exc):
                # Billing exhaustion is terminal for this invocation. Retrying
                # it nine times only delays the actionable recharge message.
                raise
            if result_url:
                # The paid task has already succeeded. Never submit another task
                # merely because the local download exhausted its own retries.
                raise
            # Once a task ID exists, timeout or polling transport failure is
            # resumable. Never submit another paid task unless the provider
            # explicitly reported a terminal task failure.
            if task_id and "task failed" not in str(exc).lower():
                raise
            if attempt < IMAGE_RETRIES:
                delay = 5 * (attempt + 1)
                print("[gpt-image-2] image task failed, retrying in %ss: %s" %
                      (delay, exc), flush=True)
                time.sleep(delay)
    raise last_error


def _design_board_grid_report(image_path):
    report = {"status": "unavailable", "grid_detected": False,
              "vertical_lines": [], "horizontal_lines": [], "error": None}
    try:
        from PIL import Image
    except ImportError as exc:
        report["error"] = "Pillow unavailable: %s" % exc
        return report
    try:
        with Image.open(image_path) as image:
            gray = image.convert("L")
            width, height = gray.size
            if width <= 0 or height <= 0:
                raise ValueError("invalid image dimensions")
            max_side = max(width, height)
            if max_side > 900:
                scale = 900.0 / max_side
                gray = gray.resize((max(1, int(width * scale)),
                                    max(1, int(height * scale))))
                width, height = gray.size
            pixels = gray.load()
            fractions = (1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 4)

            def line_metric(axis, position):
                axis_size = width if axis == "vertical" else height
                cross_size = height if axis == "vertical" else width
                # Separators can be only 1-2 pixels after thumbnailing. A wide
                # averaging stripe hides those lines inside nearby sketch
                # texture, so keep the center probe narrow.
                stripe = max(1, int(axis_size * 0.0015))
                side_gap = max(4, stripe * 4)
                contrasts = []
                for cross in range(max(1, int(cross_size * 0.02)),
                                   min(cross_size, int(cross_size * 0.98))):
                    center_values = []
                    side_values = []
                    for offset in range(-stripe, stripe + 1):
                        point = max(0, min(axis_size - 1, position + offset))
                        center_values.append(
                            pixels[point, cross] if axis == "vertical" else pixels[cross, point])
                    for anchor in (position - side_gap, position + side_gap):
                        for offset in range(-stripe, stripe + 1):
                            point = max(0, min(axis_size - 1, anchor + offset))
                            side_values.append(
                                pixels[point, cross] if axis == "vertical" else pixels[cross, point])
                    contrasts.append(
                        (sum(side_values) / float(len(side_values))) -
                        (sum(center_values) / float(len(center_values))))
                positive = [value for value in contrasts if value >= 14.0]
                return {
                    "contrast": round(sum(contrasts) / float(len(contrasts) or 1), 2),
                    "coverage": round(len(positive) / float(len(contrasts) or 1), 4),
                }

            def candidate(axis, fraction):
                axis_size = width if axis == "vertical" else height
                target = int(axis_size * fraction)
                # Generated contact sheets often use deliberately unequal
                # panel widths, so separators can drift several percent away
                # from the mathematical quarter/third. Search a bounded 5.5%
                # neighborhood and still require near-full-height/width
                # straight-line contrast to avoid matching ordinary objects.
                search = max(2, int(axis_size * 0.055))
                choices = []
                for position in range(max(1, target - search),
                                      min(axis_size - 1, target + search + 1)):
                    metric = line_metric(axis, position)
                    metric["position"] = position
                    choices.append(metric)
                best = max(choices, key=lambda item: (item["coverage"], item["contrast"]))
                best["fraction"] = round(fraction, 4)
                best["strong"] = bool(
                    (best["coverage"] >= 0.62 and best["contrast"] >= 18.0) or
                    (best["coverage"] >= 0.80 and best["contrast"] >= 10.0))
                best["x" if axis == "vertical" else "y"] = best.pop("position")
                return best

            vertical = [candidate("vertical", frac) for frac in fractions]
            horizontal = [candidate("horizontal", frac) for frac in fractions]
            strong_vertical = [item for item in vertical if item["strong"]]
            strong_horizontal = [item for item in horizontal if item["strong"]]
            report.update({
                "status": "checked",
                "width": width,
                "height": height,
                "vertical_lines": vertical,
                "horizontal_lines": horizontal,
                "grid_detected": bool(strong_vertical and strong_horizontal),
                "detection_method": "relative_separator_contrast_v2",
            })
    except (OSError, ValueError, TypeError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    return report


def _design_board_ocr_report(image_path, confidence_threshold=0.55):
    report = {"status": "unavailable", "text_detected": False,
              "detections": [], "error": None}
    if sys.platform != "darwin":
        report["error"] = "macOS Vision OCR only supports macOS"
        return report
    try:
        import ocr_check
        hits = ocr_check._vision_ocr_file(image_path)
        confident = []
        for hit in hits:
            text = str(hit.get("text") or "").strip()
            if len(text) < 2:
                continue
            if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text):
                continue
            if float(hit.get("confidence") or 0.0) >= confidence_threshold:
                confident.append({
                    "text": text,
                    "confidence": float(hit.get("confidence") or 0.0),
                })
        report.update({
            "status": "checked",
            "text_detected": bool(confident),
            "detections": confident,
        })
    except ImportError as exc:
        report["error"] = "Vision OCR unavailable: %s" % exc
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    return report


def design_board_image_qc(image_path):
    """Reject obvious design-board failures before customer confirmation."""
    grid = _design_board_grid_report(image_path)
    ocr = _design_board_ocr_report(image_path)
    failures = []
    if grid.get("grid_detected"):
        failures.append("grid_or_multi_panel_detected")
    if ocr.get("text_detected"):
        failures.append("readable_text_detected")
    return {
        "ok": not failures,
        "image": os.path.abspath(image_path),
        "checks": {"grid": grid, "ocr": ocr},
        "failures": failures,
        "policy": "design-board-single-clean-frame-v2-relative-separators",
    }


def _usage_panel_goals(relation=None):
    """Return explicit, single-frame proof goals for a usage board.

    The model is asked for one frame at a time. Keeping these goals structured
    prevents a contact-sheet model from borrowing the receiver edge or another
    panel's action as the apparent contact surface.
    """
    relation = relation or {}
    product_surface = relation.get("product_contact_surface") or "specified product contact surface"
    receiver_surface = relation.get("receiver_contact_surface") or "specified receiver contact surface"
    receiver = relation.get("receiver_object") or "receiver object"
    outward = relation.get("outward_surface") or "specified outward product surface"
    return [
        "Show the product and %s separated, with the %s and the %s broad receiving plane both visible." %
        (receiver, product_surface, receiver_surface),
        "Show a hand aligning the product's %s directly toward the %s; show the receiver plane, never only its edge." %
        (product_surface, receiver_surface),
        "Show a three-quarter view immediately before contact: the broad %s faces the product contact surface; the receiver edge is secondary and cannot be the target." % receiver_surface,
        "Show the attached result from the side and rear three-quarter view: the product contact surface is flush against the broad %s, with a visible contact line and the product protruding outward." % receiver_surface,
        "Show the real use result after attachment: the receiver is in the specified usable orientation and the product remains a complete unchanged object, with both contact surfaces inferable.",
        "Show a macro crop of the actual contact line between %s and %s; do not show a decorative seam or a receiver edge as a substitute." % (product_surface, receiver_surface),
        "Show the product's %s facing outward toward camera while the receiver's %s is visibly behind it; never attach to the receiver display/front, side edge or top edge." % (outward, receiver_surface),
        "Show a stable final action result from a different angle, preserving the same product volume, contact plane and receiver plane; no standing-on or resting-on interpretation.",
        "Show a wider context of the same final attachment/use relation, without changing the receiver orientation or turning the product into a support object.",
    ]


def product_usage_panel_prompt(plan, shot, panel_index, include_human=True):
    """Build the prompt for exactly one product-use proof frame."""
    relation = product_usage_physical_relation(plan, shot)
    facts = _plan_product_facts(plan, shot)
    product_name = facts.get("product_name") or facts.get("product_type") or "the exact confirmed product"
    goal = _usage_panel_goals(relation)[int(panel_index) - 1]
    identity = _product_identity_lock(facts)
    relation_lock = _physical_relation_lock(plan, shot)
    surface_lock = _surface_attachment_lock(relation)
    human = (
        "Use the confirmed digital-human board only for cropped hands; do not invent a different face, outfit or person. "
        if not include_human else
        "If a person is needed, use the confirmed digital-human identity; hands must interact naturally and never cover the contact proof. "
    )
    return (
        "Create ONE clean 16:9 commercial product-use frame, not a contact sheet, not a collage, not a split screen, and no borders or panel labels. "
        "This is panel %d of 9, but render only this panel's single full-frame proof. "
        "One frame means one continuous camera view, one composition, one smartphone and one speaker; do not show a sequence, contact sheet, storyboard, split view, duplicate object or multiple thumbnails. "
        "Attached references are single-view identity anchors only; never reproduce any reference-board grid or multi-view layout. "
        "The active product is %s. %s "
        "%s "
        "Required proof for this frame: %s "
        "Do not invent extra products, packaging, accessories, text, captions, logos or watermarks. "
        "Preserve the exact uploaded product geometry, complete 3D volume, thickness, edge profile, curvature, seams, controls, openings, markings and material. "
        "Occlusion or contact must not flatten, cut, stretch, round off or redesign the product. "
        "If the required contact plane is not clearly visible, the frame is invalid; do not hide it behind the phone edge or screen. "
        "Use the same clean bright commercial lighting and neutral lifestyle setting across all panels."
        % (int(panel_index), product_name, identity, human, goal)
        + relation_lock + surface_lock
    )


def _ffmpeg_for_storyboard():
    return media_tools.resolve_ffmpeg_pair()[0]


def _compose_usage_panels(panel_paths, out_path, width=960, height=540):
    """Compose nine independently generated frames locally with ffmpeg."""
    if len(panel_paths) != 9:
        raise br_client.BRError("USAGE_PANEL_COUNT_INVALID: expected 9, got %s" % len(panel_paths))
    ffmpeg = _ffmpeg_for_storyboard()
    if not ffmpeg:
        raise br_client.BRError("USAGE_PANEL_COMPOSE_FFMPEG_MISSING: 请先准备 ffmpeg。")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in panel_paths:
        cmd.extend(["-i", os.path.abspath(path)])
    filters = []
    for index in range(9):
        filters.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1[v%d]" %
            (index, width, height, width, height, index))
    layout = "|".join(
        "%d_%d" % ((index % 3) * width, (index // 3) * height)
        for index in range(9))
    filters.append("".join("[v%d]" % index for index in range(9)) +
                   "xstack=inputs=9:layout=%s:fill=white[out]" % layout)
    cmd.extend(["-filter_complex", ";".join(filters), "-map", "[out]",
                "-frames:v", "1", "-q:v", "2", os.path.abspath(out_path)])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise br_client.BRError("USAGE_PANEL_COMPOSE_FAILED: %s" %
                                proc.stderr.decode("utf-8", "replace")[-1200:])
    return out_path


def generate_usage_panels(api_key, plan, shot, out_path, image_urls, *, model=DEFAULT_MODEL,
                          include_human=True, force=False, resume_tasks=None,
                          on_progress=None):
    """Generate nine independent usage frames and compose a review board."""
    panel_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "usage_panels")
    os.makedirs(panel_dir, exist_ok=True)
    panel_paths = [os.path.join(panel_dir, "panel_%02d.jpg" % index)
                   for index in range(1, 10)]

    def generate(index):
        prompt = product_usage_panel_prompt(plan, shot, index, include_human=include_human)
        if isinstance(image_urls, dict):
            panel_urls = image_urls.get(str(index)) or image_urls.get(index) or []
        else:
            panel_urls = image_urls
        result = download_first_image(
            api_key, prompt, panel_paths[index - 1], model=model, ratio="16:9",
            image_urls=panel_urls,
            resume_task_id=(resume_tasks or {}).get(str(index)), force=force,
            on_progress=(lambda event: on_progress(index, event)
                         if on_progress else None))
        result["panel_index"] = index
        result["prompt"] = prompt
        return result

    results = [None] * 9
    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {executor.submit(generate, index): index for index in range(1, 10)}
        for future in as_completed(futures):
            index = futures[future]
            results[index - 1] = future.result()
    _compose_usage_panels(panel_paths, out_path)
    panel_urls = [item.get("url") for item in results if item.get("url")]
    return {
        "path": out_path,
        "abspath": os.path.abspath(out_path),
        "sha256": _file_sha256(out_path),
        "panel_paths": [os.path.abspath(path) for path in panel_paths],
        "panel_urls": panel_urls,
        "imageUrls": panel_urls,
        "representative_panel_index": 5,
        "url": panel_urls[4] if len(panel_urls) >= 5 else (panel_urls[0] if panel_urls else ""),
        "generation_mode": "panelized_local_composite",
        "panels": results,
    }


def expand_storyboard_panel(api_key, segment, *, out_dir):
    """Create the high-resolution keyframe reference for one approved shot.

    The source script-driven storyboard sheet is never pixel-cropped. gpt-image-2 receives
    the confirmed shot sheet and only the assets selected by the segment's
    ``ref_tags``. The output is a single representative 16:9 keyframe for the
    current segment, not "panel N" copied from a contact sheet.
    """
    sheet = segment.get("storyboard_path")
    panel_index = int(segment.get("storyboard_panel_index") or 1)
    if not sheet or not os.path.isfile(sheet):
        raise br_client.BRError("STORYBOARD_PANEL_SOURCE_MISSING: %s" % sheet)
    refs = [ref for ref in (segment.get("references") or []) if isinstance(ref, dict)]
    wanted = set(segment.get("ref_tags") or [])
    selected = [ref for ref in refs if ref.get("tag") in wanted]
    if not selected:
        raise br_client.BRError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 未绑定 ref_tags" %
                                segment.get("id"))
    recipe = artifact_contract.build_storyboard_panel_recipe(segment)
    recipe["panel_index"] = panel_index
    if not recipe.get("storyboard_sha256"):
        raise br_client.BRError("STORYBOARD_PANEL_SOURCE_MISSING: %s" % sheet)
    if set(recipe.get("ref_tags") or []) != wanted or len(recipe.get("references") or []) != len(wanted):
        raise br_client.BRError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 的标签与参考契约不一致" %
                                segment.get("id"))
    budget = artifact_contract.storyboard_image_input_tags(segment)
    input_tags = budget["selected"]
    omitted_tags = budget["omitted"]
    recipe_sha = artifact_contract.sha256_json(recipe)
    panel_dir = os.path.join(out_dir, "expanded_panels")
    out = os.path.join(panel_dir, "shot_%s_keyframe.jpg" %
                       safe_name(segment.get("id")))
    meta = out + ".json"
    previous = _read_result(meta)
    if (previous and previous.get("recipe_sha256") == recipe_sha and
            os.path.isfile(out) and os.path.getsize(out) > 0):
        return {"path": out, "abspath": os.path.abspath(out), "sha256": _file_sha256(out),
                "recipe_sha256": recipe_sha, "skipped": True}
    os.makedirs(panel_dir, exist_ok=True)
    source_urls = [_br.to_image_ref(sheet, api_key=api_key, prefer_hosted=False)]
    selected_by_tag = {ref.get("tag"): ref for ref in selected}
    for tag in input_tags:
        ref = selected_by_tag[tag]
        source_urls.append(_br.to_image_ref(ref["url"], api_key=api_key, prefer_hosted=False))
    source_urls = list(dict.fromkeys(source_urls))
    prompt = (
        "Create ONE high-resolution 16:9 photorealistic live-action keyframe for video generation. "
        "Use the approved script-driven storyboard sheet as the storyboard for THIS CURRENT SHOT only. "
        "Synthesize one representative full-frame keyframe that best matches the current shot's director text, "
        "product/character references and intended action. Do not copy or crop any grid cell literally. "
        "Do not show any grid, border, annotation, sketch, text, subtitle, "
        "watermark or logo. Reconstruct this single panel as a clean full-frame commercial plate. "
        "Preserve the same character identity, wardrobe, product geometry, scene, props, lighting and action. "
        "This shot uses %s. Segment id: %s. Source shot ids: %s. Shot instruction: %s"
        % (" ".join(input_tags), segment.get("id"),
           ",".join(str(x) for x in (segment.get("source_shot_ids") or [])),
           segment.get("text") or "the approved shot action"))
    if omitted_tags:
        prompt += (" Lower-priority reference tags were intentionally not uploaded due to the provider "
                   "4-image limit: %s. Preserve their semantic role from the shot instruction and storyboard, "
                   "but treat the uploaded tags as the visual anchors." % " ".join(omitted_tags))
    result = download_first_image(api_key, prompt, out, model=DEFAULT_MODEL, ratio="16:9",
                                  image_urls=source_urls, sync_img2img=True)
    # Keep the handoff shape stable even for alternate image backends/tests that
    # return only ``path``.
    result["path"] = out
    result["abspath"] = os.path.abspath(out)
    result["recipe_sha256"] = recipe_sha
    result["panel_index"] = panel_index
    atomic_write_json(meta, {
        "recipe_sha256": recipe_sha,
        "recipe": recipe,
        "path": os.path.abspath(out),
    })
    return result


def _as_feature_dict(val):
    """把 facial_features / body_features 归一成 dict。

    容错：LLM 生成的 plan 里这些字段可能是 dict，也可能被写成一段纯文本 string
    （真机实测遇到过）。string 时包成 {"_raw": <描述>}，让后续 .get(key) 不崩，
    并能通过 _feat() 把整段描述当兜底文本用。None/其它类型归一成空 dict。
    """
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        return {"_raw": val.strip()}
    return {}


def _feat(d, key, char, char_key=None):
    """取特征字段：优先 features dict 的具体键，其次 character 顶层同名键，
    最后回落到 string 形态被包进来的整段描述 (_raw)。全空返回 ''。"""
    v = d.get(key)
    if v:
        return v
    if char_key is None:
        char_key = key
    v = char.get(char_key)
    if v:
        return v
    return d.get("_raw", "")


def cast_prompt(plan):
    import board_prompts
    return board_prompts.cast_prompt(plan)


def cast_source_fingerprint(plan, prompt=None):
    """Bind cast reuse to reference bytes and the authored identity contract."""
    plan = plan or {}
    prompt = prompt if prompt is not None else cast_prompt(plan)
    refs = list((plan.get("asset_refs") or {}).get("digital_human_portraits") or [])
    identity_contract = json.dumps({
        "policy": CAST_SOURCE_POLICY_VERSION,
        "characters": plan.get("characters") or [],
        "immutable_features": plan.get("immutable_features") or [],
        "cast_prompt": prompt or "",
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _source_refs_fingerprint(
        refs + [CAST_SOURCE_POLICY_VERSION, identity_contract])










def _plan_has_physical_product_subject(plan):
    import board_prompts
    return board_prompts._plan_has_physical_product_subject(plan)


def _looks_like_design_board(plan):
    import board_prompts
    return board_prompts._looks_like_design_board(plan)




def design_board_prompt(plan, shot, idx, bw=True, strict_bw=False,
                        reference_registry=None):
    import board_prompts
    return board_prompts.design_board_prompt(
        plan, shot, idx, bw=bw, strict_bw=strict_bw,
        reference_registry=reference_registry)






def shot_prompt(plan, shot, idx, bw=True, strict_bw=False):
    import board_prompts
    return board_prompts.shot_prompt(
        plan, shot, idx, bw=bw, strict_bw=strict_bw)




def _design_board_asset_slots_for_shot(shot):
    slots = []
    content_page_contract = (
        shot.get("content_page_contract")
        if isinstance(shot.get("content_page_contract"), dict) else {})
    panel_plan = normalize_panel_plan(dict(shot, panel_plan=shot.get("panel_plan") or DEFAULT_DESIGN_BOARD_PANEL_PLAN))
    visual_mode = (shot.get("visual_mode") or shot.get("layout_mode") or
                   shot.get("presentation_mode") or "corner_pip")
    slide_refs = []
    for tag in _shot_ref_tags(shot):
        if tag.startswith("@ppt") or tag.startswith("@slide") or tag.startswith("@doc"):
            slide_refs.append(tag)
    if not slide_refs and (shot.get("source_refs") or shot.get("slide_range")):
        slide_refs = [
            str(item) for item in _listify(
                shot.get("source_refs") or shot.get("slide_range"))
        ]
    slots.append({
        "slot": "ppt_page",
        "kind": "content_surface",
        "layer": "remotion",
        "role": "main_visual",
        "safe_zone": "center",
        "enabled": True,
        "renderable": False,
        "timing": "steady",
        "ref_tags": slide_refs,
        "description": (shot.get("content_design") or
                        content_page_contract.get("title") or
                        shot.get("visual") or "PPT/content page surface"),
    })
    slots.append({
        "slot": "corner_pip",
        "kind": "avatar_plate",
        "layer": "fuse",
        "role": "presenter_window",
        "safe_zone": "corner",
        "enabled": bool(shot.get("characters") or shot.get("character_prompt")),
        "renderable": False,
        "timing": "steady",
        "ref_tags": list(dict.fromkeys([tag for tag in _shot_ref_tags(shot) if tag.startswith("@host") or "host" in tag or "avatar" in tag])),
        "description": "digital human presenter plate; keep voice, face, wardrobe and background locked",
    })
    slots.append({
        "slot": "title_area",
        "kind": "title_zone",
        "layer": "hyperframes",
        "role": "hierarchy_anchor",
        "safe_zone": shot.get("title_safe_zone") or "upper_third",
        "enabled": True,
        "renderable": True,
        "timing": "steady",
        "description": shot.get("title") or shot.get("heading") or "title / chapter heading area",
    })
    slots.append({
        "slot": "safe_zone",
        "kind": "layout_guard",
        "layer": "composite",
        "role": "do_not_cover_content",
        "safe_zone": shot.get("safe_zone") or "lower_third",
        "enabled": True,
        "renderable": False,
        "timing": "steady",
        "description": "reserved readable zones for captions, cards and presenter window",
    })
    slots.append({
        "slot": "highlight_card",
        "kind": "callout_card",
        "layer": "hyperframes",
        "role": "emphasis",
        "safe_zone": shot.get("highlight_safe_zone") or "right",
        "enabled": bool(shot.get("highlight") or shot.get("motion_elements")),
        "renderable": True,
        "timing": "steady",
        "description": shot.get("highlight") or shot.get("animation_hint") or "key highlight card / emphasis block",
    })
    slots.append({
        "slot": "chart_arrow_annotation",
        "kind": "annotation",
        "layer": "hyperframes",
        "role": "chart_arrow_label_position",
        "safe_zone": shot.get("annotation_safe_zone") or "left",
        "enabled": False,
        "renderable": True,
        "timing": "steady",
        "description": shot.get("annotation") or shot.get("content_design") or "chart, arrow, marker and annotation positions",
    })
    authored_overlay_text = " ".join(str(shot.get(key) or "") for key in (
        "chart", "arrow", "annotation", "content_design", "visual", "props"))
    for slot_name, role in (("chart", "chart position"), ("arrow", "arrow path"),
                            ("annotation", "annotation label position")):
        tokens = {
            "chart": ("chart", "graph", "图表", "数据图"),
            "arrow": ("arrow", "flow", "箭头", "流程", "连接线"),
            "annotation": ("annotation", "callout", "标注", "注释"),
        }[slot_name]
        slots.append({
            "slot": slot_name,
            "kind": slot_name,
            "layer": "hyperframes",
            "owner": "hyperframes",
            "role": role,
            "safe_zone": shot.get("annotation_safe_zone") or "left",
            "required": True,
            "enabled": any(token.lower() in authored_overlay_text.lower()
                           for token in tokens),
            "renderable": True,
            "timing": "steady",
            "description": shot.get(slot_name) or shot.get("annotation") or
                           shot.get("content_design") or role,
        })
    return slots


def _motion_plan_for_design_board_shot(shot):
    try:
        import motion_design
        motion_plan = motion_design.ensure_motion_plan_contract(
            dict(shot, storyboard_role="design_board"),
            {"storyboard_role": "design_board", "source_ppt": shot.get("source_ppt")})
    except (OSError, ValueError, TypeError, RuntimeError):
        motion_plan = {
            "schema_version": 2,
            "personality": "balanced",
            "primary": "medium",
            "secondary": "medium",
            "ambient": "low",
            "density": "medium",
            "timing": "steady",
            "safe_zone": "center",
            "a11y": {"contrast": "high", "max_lines": 2, "subtitle_required": bool(shot.get("dialogue"))},
            "motion_elements": _listify(shot.get("motion_elements")),
            "asset_slots": [],
        }
    authored_slots = shot.get("asset_slots") if isinstance(shot.get("asset_slots"), list) else []
    required_slots = _design_board_asset_slots_for_shot(shot)
    by_slot = {item.get("slot"): item for item in required_slots if isinstance(item, dict)}
    for item in authored_slots:
        if isinstance(item, dict) and item.get("slot"):
            by_slot[item["slot"]] = dict(by_slot.get(item["slot"], {}), **item)
    motion_slots = motion_plan.get("asset_slots") if isinstance(motion_plan.get("asset_slots"), list) else []
    for item in motion_slots:
        if isinstance(item, dict) and item.get("slot") and item.get("slot") not in by_slot:
            by_slot[item["slot"]] = item
    motion_plan["asset_slots"] = list(by_slot.values())
    return motion_plan


def _build_design_board_composite_plan(plan, shot_records):
    shots = []
    for index, record in enumerate(shot_records or [], 1):
        shot = record.get("shot") or {}
        motion_plan = shot.get("motion_plan") if isinstance(shot.get("motion_plan"), dict) else _motion_plan_for_design_board_shot(shot)
        # The normalized MotionPlan is the source of truth.  It already merges
        # legacy/authored slots with the complete design-board slot catalog.
        asset_slots = motion_plan.get("asset_slots") or []
        shots.append({
            "id": shot.get("id") or record.get("id") or "shot_%02d" % index,
            "duration": shot.get("duration") or shot.get("seconds"),
            "design_board_path": record.get("abspath") or record.get("path"),
            "design_board_url": record.get("url"),
            "visual_mode": shot.get("visual_mode") or shot.get("layout_mode") or shot.get("presentation_mode") or "corner_pip",
            "asset_slots": asset_slots,
            "motion_plan": motion_plan,
            "composition_contract": {
                "owner": "design_board",
                "usage": "render_contract_only",
                "video_model": "avatar_plate_only",
                "final_composition": "remotion",
                "composition_components": ["remotion", "hyperframes", "ffmpeg"],
            },
        })
    return {
        "schema_version": 2,
        "kind": "design_board_composite_plan",
        "contract": {
            "design_board_usage": "render_contract_only",
            "video_model_usage": "avatar_plate_only",
            "final_composition_owner": "remotion",
            "composition_components": ["remotion", "hyperframes", "ffmpeg"],
            "storyboard_ref_for_video_model": False,
            "pipeline": ["design_board", "video_model.avatar_plate_only", "remotion", "hyperframes", "fuse"],
            "video_model_allowed_locks": ["person", "voice", "wardrobe", "background"],
            "video_model_forbidden_inputs": [
                "ppt_page", "title_area", "safe_zone", "highlight_card",
                "chart", "arrow", "annotation", "motion_grammar", "design_board",
            ],
        },
        "composition_contract": {
            "owner": "design_board",
            "usage": "render_contract_only",
            "asset_slots_are_required": True,
            "motion_plan_is_structured": True,
            "content_owner": "remotion",
            "motion_owner": "hyperframes",
            "assembly_owner": "fuse",
            "video_model_role": "avatar_plate_only",
        },
        "client": plan.get("client") or client_slug(plan),
        "output_ratio": normalized_output_ratio(plan),
        "storyboard_aspect_ratio": STORYBOARD_RATIO,
        "asset_slot_kinds": [
            "ppt_page", "corner_pip", "title_area", "safe_zone", "highlight_card",
            "chart", "arrow", "annotation", "chart_arrow_annotation",
        ],
        "shots": shots,
    }

def contact_sheet_prompt(plan, bw=True, reference_registry=None):
    import board_prompts
    return board_prompts.contact_sheet_prompt(
        plan, bw=bw, reference_registry=reference_registry)



def _data_uri(path):
    return storyboard_preview.data_uri(path)


def write_index(plan, results, out_dir):
    return storyboard_preview.write_index(
        plan, results, out_dir, normalize_panel_plan_fn=normalize_panel_plan)


def _collect_image_urls(refs_list, api_key, *, fail_on_invalid=False, label="参考图"):
    """把 asset_refs 里的本地路径/URL 转成 API 可接受的 URL 列表。

    本地路径 → base64 data URL（图生图 createImage 支持）。
    已是 http(s)/data URL → 直接透传。
    生产链路传 ``fail_on_invalid=True``：已声明的素材损坏时必须阻断，禁止
    静默跳过后退化为纯文字生成。默认保留宽松模式供诊断工具兼容。
    最多取前 4 张（API 通常支持 1-4 张参考图，防止超限）。
    """
    result = []
    for r in refs_list or []:
        try:
            url = _br.to_image_ref(r, api_key=api_key, prefer_hosted=False)
            result.append(url)
        except (OSError, ValueError, TypeError, RuntimeError, br_client.BRError) as e:
            if fail_on_invalid:
                raise br_client.BRError(
                    "%s不可用：%s（%s）。请替换为真实有效的 PNG/JPEG/WebP 图片；"
                    "为避免退化成纯文字生成，本次故事板已停止。" % (label, r, e)
                ) from e
            print(f"  [asset_ref] 跳过参考图 {r}：{e}", flush=True)
    return result[:4]


def _merge_reference_urls(portraits=None, products=None, scenes=None, limit=4):
    """Merge reference URLs without crowding product anchors out.

    A character plus several scene refs used to consume all four API slots and
    silently drop the product image. For product shots, identity and product
    geometry are the hard constraints; scene refs are secondary context.
    """
    groups = [list(portraits or []), list(products or []), list(scenes or [])]
    result = []
    seen = set()

    def add(value):
        if value and value not in seen and len(result) < limit:
            result.append(value)
            seen.add(value)

    # Keep one character anchor, then reserve the next slots for product views.
    for value in groups[0][:1]:
        add(value)
    for value in groups[1]:
        add(value)
    for value in groups[0][1:]:
        add(value)
    for value in groups[2]:
        add(value)
    return result


def _usage_reference_urls(product_refs=None, cast_refs=None, pose_refs=None, limit=3):
    """Build product-in-use image references with product identity first."""
    return _merge_reference_urls(product_refs, cast_refs, pose_refs, limit=limit)


def _single_view_reference(source_path, out_dir, label, *, grid=None, cell=None):
    """Derive one visual identity anchor from a confirmed reference board.

    Contact sheets are excellent review artifacts but are a poor img2img
    reference for a single-frame request: image models may copy the sheet
    layout. This local, deterministic derivative preserves the approved pixels
    while removing the grid from the image sent to the model. The original
    board remains the source of truth and is recorded by the caller.
    """
    if not source_path or not os.path.isfile(source_path):
        return source_path
    if not grid or not cell:
        return os.path.abspath(source_path)
    try:
        from PIL import Image
    except ImportError as exc:
        raise br_client.BRError(
            "产品使用图参考素材处理失败：缺少 Pillow，无法从确认板提取单视图身份锚点。"
        ) from exc
    cols, rows = grid
    col, row = cell
    if cols <= 0 or rows <= 0 or not (0 <= col < cols and 0 <= row < rows):
        raise br_client.BRError("USAGE_REFERENCE_GRID_INVALID: %s %s %s" %
                                (source_path, grid, cell))
    source_path = os.path.abspath(source_path)
    try:
        image_context = Image.open(source_path)
    except (OSError, ValueError):
        # Unit/diagnostic callers may provide placeholder bytes while mocking
        # the remote generation layer. Let the normal reference validation
        # report those bytes instead of failing in the optional crop step.
        return source_path
    with image_context as image:
        width, height = image.size
        x0 = round(width * col / cols)
        x1 = round(width * (col + 1) / cols)
        y0 = round(height * row / rows)
        y1 = round(height * (row + 1) / rows)
        # Leave the cell border behind. A one-pixel inset also prevents a
        # board divider from becoming a false object edge in the new prompt.
        inset = max(1, min(x1 - x0, y1 - y0) // 200)
        crop = image.crop((x0 + inset, y0 + inset, x1 - inset, y1 - inset))
        source_sha = _file_sha256(source_path)[:12]
        name = "%s_%s_%d_%d_%s.png" % (
            safe_name(label), source_sha, col, row,
            hashlib.sha256((str(grid) + str(cell)).encode("ascii")).hexdigest()[:8])
        ref_dir = os.path.join(os.path.abspath(out_dir), "usage_reference_anchors")
        os.makedirs(ref_dir, exist_ok=True)
        out_path = os.path.join(ref_dir, name)
        if not os.path.isfile(out_path):
            crop.convert("RGB").save(out_path, format="PNG")
    return os.path.abspath(out_path)


def _prepare_usage_panel_reference_paths(product_board_path, product_image_paths,
                                          cast_board_path, out_dir, *,
                                          include_human=True):
    """Build single-view refs for the nine product-use image requests.

    The product and cast boards remain authoritative. The API receives only a
    single product view plus, when needed, a single Mina view per request so a
    model cannot reproduce either board's 3x3/3x2 layout inside one panel.
    """
    product_image_paths = [p for p in (product_image_paths or [])
                           if p and os.path.isfile(p)]
    hero = product_image_paths[-1] if product_image_paths else None
    product_front = _single_view_reference(
        product_board_path, out_dir, "product_front", grid=(3, 3), cell=(0, 0))
    product_bottom = _single_view_reference(
        product_board_path, out_dir, "product_bottom", grid=(3, 3), cell=(2, 2))
    product_anchor = hero or product_front
    if not product_anchor:
        raise br_client.BRError("USAGE_REFERENCE_PRODUCT_MISSING: 缺少已确认产品身份图。")

    human_anchor = None
    if include_human and cast_board_path:
        # Full-body front view keeps Mina's clothing and proportions stable;
        # the model can use it as an identity anchor for medium/close shots.
        cast_pack = cast_reference_pack.derive(
            cast_board_path, os.path.join(out_dir, "usage_reference_anchors", "cast"))
        human_anchor = ((cast_pack.get("views") or {}).get(
            "full_body_front") or {}).get("path")

    refs = {}
    # Mechanical proof panels benefit from the approved bottom view. Other
    # panels use the clean hero view to keep the speaker silhouette stable.
    bottom_panels = {2, 3, 4, 6}
    for index in range(1, 10):
        product_ref = product_bottom if index in bottom_panels else product_anchor
        values = [product_ref]
        if human_anchor:
            values.append(human_anchor)
        refs[str(index)] = values
    return refs


def _reference_url_abs(url):
    if not isinstance(url, str) or not url:
        return url
    if re.match(r"^(https?|data):", url):
        return url
    return os.path.abspath(url if os.path.isabs(url) else os.path.join(ROOT, url))


def build_reference_registry(plan, limit=None):
    """Build the image reference registry for storyboard generation.

    This is a whole-plan index, not a single BasicRouter request payload. A PPT
    explainer may legitimately bind different slide refs to different shots, so
    this registry must not silently truncate at the model's per-request image
    limit. The per-shot limit is enforced after `shot_reference_registry()`
    filters this registry down to the exact refs for one generated image.
    """
    registry = []
    seen_tags = set()

    def add(tag, url, ref_type, source=None):
        if not tag or not url or tag in seen_tags:
            return
        if limit is not None and len(registry) >= limit:
            return
        registry.append({
            "tag": str(tag),
            "url": _reference_url_abs(url),
            "type": ref_type or "generic_visual",
            "source": source or _reference_url_abs(url),
        })
        seen_tags.add(str(tag))

    asset_refs = plan.get("asset_refs") or {}
    for index, url in enumerate(asset_refs.get("product_usage_images") or []):
        add("@usage" if index == 0 else "@usage%d" % (index + 1),
            url, "product_usage_identity", url)

    wanted_tags = []
    ref_by_tag = {}
    for shot in plan.get("shots") or []:
        tags = shot.get("ref_tags") or []
        if isinstance(tags, str):
            tags = [tags]
        for tag in tags:
            if tag not in wanted_tags:
                wanted_tags.append(tag)
        for ref in (shot.get("references") or []):
            if isinstance(ref, dict) and ref.get("tag") and ref.get("url"):
                ref_by_tag.setdefault(ref["tag"], ref)
    for ref in (plan.get("references") or []):
        if isinstance(ref, dict) and ref.get("tag") and ref.get("url"):
            ref_by_tag.setdefault(ref["tag"], ref)

    # Product identity next, then character identity, then any remaining tags.
    def tag_priority(tag):
        ref_type = str((ref_by_tag.get(tag) or {}).get("type") or "")
        if "product" in ref_type:
            return 0
        if "character" in ref_type:
            return 1
        return 2

    for tag in sorted(wanted_tags, key=tag_priority):
        ref = ref_by_tag.get(tag) or {}
        add(tag, ref.get("url"), ref.get("type"), ref.get("label") or ref.get("url"))

    for index, url in enumerate(asset_refs.get("product_boards") or []):
        add("@product_board" if index == 0 else "@product_board%d" % (index + 1),
            url, "product_board", url)
    for index, url in enumerate(asset_refs.get("cast_boards") or []):
        add("@host_board" if index == 0 else "@host_board%d" % (index + 1),
            url, "character_board", url)
    return registry


def _validate_shot_reference_registry(shot, registry, *, limit=MAX_IMAGE_REFERENCES_PER_REQUEST):
    """Fail before paid image calls when one shot exceeds model image refs."""
    if len(registry or []) <= int(limit):
        return
    sid = (shot or {}).get("id") or "unknown"
    tags = [str(item.get("tag")) for item in (registry or []) if item.get("tag")]
    raise br_client.BRError(
        "SHOT_REFERENCE_REGISTRY_TOO_LARGE: shot %s 绑定了 %d 张参考图，超过 gpt-image-2 "
        "单次 img2img 的 %d 张上限：%s。请减少该镜头 ref_tags 或先合并为确认板；"
        "不得静默丢弃某一镜头绑定素材。"
        % (sid, len(registry or []), int(limit), ", ".join(tags)))


def _validate_reference_registry(plan, registry):
    seen = set()
    for item in registry:
        tag = item.get("tag")
        url = item.get("url")
        if not tag or not TAG_PATTERN.match(str(tag)):
            raise br_client.BRError("REFERENCE_REGISTRY_INVALID_TAG: %s" % tag)
        if tag in seen:
            raise br_client.BRError("REFERENCE_REGISTRY_DUPLICATE_TAG: %s" % tag)
        seen.add(tag)
        if not url:
            raise br_client.BRError("REFERENCE_REGISTRY_MISSING_URL: %s" % tag)
        if not re.match(r"^(https?|data):", str(url)) and not os.path.isfile(url):
            raise br_client.BRError("REFERENCE_REGISTRY_MISSING_FILE: %s %s" % (tag, url))
    wanted = set()
    for shot in plan.get("shots") or []:
        tags = shot.get("ref_tags") or []
        if isinstance(tags, str):
            tags = [tags]
        wanted.update(tags)
    missing = sorted(tag for tag in wanted if tag not in seen)
    if missing:
        raise br_client.BRError(
            "REFERENCE_REGISTRY_MISSING_TAGS: %s" % ", ".join(missing))


def _shot_ref_tags(shot):
    tags = (shot or {}).get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return [str(tag) for tag in tags if tag]


def _physical_use_trigger_text(shot):
    """Text fields that describe the current shot's physical action.

    Keep identity/wardrobe/product-category text out of this extraction.  A
    product being magnetic, wearable or held by a presenter is not enough to
    attach the product-in-use board; the shot must describe a real physical
    operation, contact, attachment, wearing, mounting or control action.
    """
    if not isinstance(shot, dict):
        return ""
    parts = []
    for key in (
            "visual", "character_action", "action", "scene_prompt",
            "video_prompt_notes", "director_notes"):
        value = shot.get(key)
        if value:
            parts.append(str(value))
    panels = shot.get("panel_plan") or []
    if isinstance(panels, str):
        # Free-form panel labels often contain camera grammar such as
        # "long-lens compression"; matching action substrings inside those
        # labels can falsely classify product-only shots as physical-use shots.
        pass
    elif isinstance(panels, list):
        for panel in panels:
            if isinstance(panel, dict):
                for key in ("beat", "action", "event", "continuity"):
                    if panel.get(key):
                        parts.append(str(panel[key]))
    return " ".join(parts).lower()


def _has_structured_use_relation(source):
    if not isinstance(source, dict):
        return False
    for key in ("use_relation", "physical_relation", "usage_relation",
                "interaction_relation", "product_use_relation"):
        value = source.get(key)
        if isinstance(value, dict) and _as_relation_dict(value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _has_physical_use_action(shot):
    if not shot:
        return False
    if _has_structured_use_relation(shot):
        return True
    text = _physical_use_trigger_text(shot)
    if not text:
        return False
    strong_terms = (
        "贴合", "贴到", "吸附", "扣上", "夹住", "夹到", "佩戴", "戴上",
        "插入", "接入", "安装", "固定", "支撑", "展开支撑",
        "按下", "长按", "点击", "旋转", "滑动", "操作产品", "正确使用",
        "真实使用", "使用产品",
        "snap", "attach", "mount", "install", "dock", "plug",
        "clip", "clamp", "wear", "put on", "press", "tap", "operate",
        "switch on", "turn on", "use the product", "contact point",
    )
    if any(term in text for term in strong_terms):
        return True
    # ``连接/connect`` is common in diagrams, content cards and software-flow
    # descriptions.  Treat it as physical only when the same action clause also
    # names a tangible receiver/contact surface.  This prevents a PPT phrase
    # such as "底座图标连接四张卡片" from creating a phantom @usage dependency.
    connection_terms = ("连接", "connect", "connected", "connection")
    physical_objects = (
        "产品", "设备", "手机", "平板", "耳机", "音箱", "音响", "充电器",
        "接口", "插头", "插座", "卡扣", "佩戴部位", "手腕", "耳朵", "背面",
        "接触面", "承载面", "product", "device", "phone", "tablet", "earbud",
        "speaker", "charger", "port", "socket", "wrist", "ear", "back plane",
        "contact surface", "receiver surface",
    )
    diagram_context = (
        "图标", "流程图", "连接线", "箭头", "卡片", "节点", "版式", "内容页",
        "icon", "flowchart", "diagram", "arrow", "card", "node", "layout",
    )
    if any(term in text for term in connection_terms):
        if any(term in text for term in diagram_context) and not any(
                term in text for term in physical_objects):
            return False
        return any(term in text for term in physical_objects)
    magnetic_terms = ("磁吸", "magnetic", "magnet", "magsafe")
    receiver_terms = (
        "手机背", "手机的背", "手机背面", "背面", "平板背", "tablet back",
        "phone back", "smartphone back", "receiver", "target surface",
        "contact surface", "接触面", "目标面", "承载面",
    )
    return any(term in text for term in magnetic_terms) and any(
        term in text for term in receiver_terms)


def _shot_needs_usage_reference(shot):
    """Whether product-in-use board should be attached to this shot."""
    return _has_physical_use_action(shot)


def _force_usage_reference(shot):
    value = (shot or {}).get("force_usage_reference")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "force", "forced"}
    return False


def _explicit_usage_contract(shot):
    if not isinstance(shot, dict) or "requires_usage" not in shot:
        return None
    return bool(shot.get("requires_usage"))


def _usage_reference_url(plan):
    """Return a concrete confirmed usage reference, if one is declared.

    ``requires_usage`` is a semantic requirement; ``@usage`` is an actual
    media binding.  Keeping those two facts separate is what lets the product
    usage stage exist without making the pre-usage plan fail its own reference
    registry validation.
    """
    asset_refs = (plan or {}).get("asset_refs") or {}
    for value in asset_refs.get("product_usage_images") or []:
        if value:
            return value
    references = list((plan or {}).get("references") or [])
    for shot in (plan or {}).get("shots") or []:
        if isinstance(shot, dict):
            references.extend(shot.get("references") or [])
    for ref in references:
        if isinstance(ref, dict) and ref.get("tag") == "@usage" and ref.get("url"):
            return ref["url"]
    return None


def canonicalize_usage_ref_tags(plan):
    """Persist product-use reference decisions on each shot.

    The usage board is a storyboard contract input, not a video handoff patch.
    Shots with physical operation/use semantics carry ``requires_usage: true``
    and receive an explicit ``@usage`` tag only after that confirmed media
    binding exists. Pure product or presenter shots carry ``requires_usage:
    false`` and do not inherit usage geometry.
    """
    has_physical_product = bool(
        _plan_has_physical_product_subject(plan) or
        any(isinstance(shot, dict) and
            (_force_usage_reference(shot) or _shot_needs_usage_reference(shot))
            for shot in (plan or {}).get("shots") or []))
    for shot in (plan or {}).get("shots") or []:
        if not isinstance(shot, dict):
            continue
        contract_value = _explicit_usage_contract(shot)
        requires_usage = (
            contract_value if contract_value is not None
            else (_force_usage_reference(shot) or _shot_needs_usage_reference(shot))
        )
        requires_usage = bool(has_physical_product and requires_usage)
        tags = _shot_ref_tags(shot)
        if requires_usage:
            tags = ["@usage"] + [tag for tag in tags if tag != "@usage"]
        else:
            tags = [tag for tag in tags if tag != "@usage"]
        shot["requires_usage"] = bool(requires_usage)
        shot["ref_tags"] = list(dict.fromkeys(tags))
    return plan


def _sync_usage_reference_contract(plan):
    """Keep asset_refs, global references and shot references in agreement."""
    usage_url = _usage_reference_url(plan)
    canonicalize_usage_ref_tags(plan)
    if not usage_url:
        plan["references"] = [
            ref for ref in (plan.get("references") or [])
            if not (isinstance(ref, dict) and ref.get("tag") == "@usage")
        ]
        for shot in plan.get("shots") or []:
            if isinstance(shot, dict):
                shot["references"] = [
                    ref for ref in (shot.get("references") or [])
                    if not (isinstance(ref, dict) and ref.get("tag") == "@usage")
                ]
        return plan
    usage_ref = {
        "id": "ref_product_usage_confirmed",
        "url": usage_url,
        "type": "product_usage_identity",
        "scope": "global",
        "label": "Confirmed product usage board",
        "role": "physical product-use and contact-surface anchor",
        "intent": "Lock the approved product-use relationship, contact surfaces and operating orientation.",
        "tag": "@usage",
    }
    global_refs = [ref for ref in (plan.get("references") or [])
                   if isinstance(ref, dict)]
    global_usage = next((ref for ref in global_refs if ref.get("tag") == "@usage"), None)
    if global_usage is None:
        global_refs.append(dict(usage_ref))
    else:
        global_usage.update(dict(usage_ref))
    plan["references"] = global_refs

    for shot in plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        if not shot.get("requires_usage"):
            shot["ref_tags"] = [
                tag for tag in _shot_ref_tags(shot) if tag != "@usage"
            ]
            shot["references"] = [
                ref for ref in (shot.get("references") or [])
                if not (isinstance(ref, dict) and ref.get("tag") == "@usage")
            ]
            continue
        shot["ref_tags"] = list(dict.fromkeys(["@usage"] + _shot_ref_tags(shot)))
        shot_refs = [ref for ref in (shot.get("references") or [])
                     if isinstance(ref, dict) and ref.get("tag") != "@usage"]
        shot_refs.append(dict(usage_ref))
        shot["references"] = shot_refs
    return plan


def shot_reference_registry(reference_registry, shot):
    """Filter the global registry to the exact images this shot may receive."""
    registry = reference_registry or []
    by_tag = {item.get("tag"): item for item in registry}
    allowed = []
    seen = set()

    def add(tag):
        item = by_tag.get(tag)
        if item and tag not in seen:
            allowed.append(item)
            seen.add(tag)

    requires_usage = _explicit_usage_contract(shot)
    if requires_usage is None:
        requires_usage = _force_usage_reference(shot) or _shot_needs_usage_reference(shot)
    if requires_usage:
        add("@usage")
    for tag in _shot_ref_tags(shot):
        add(tag)
    return allowed


def _confirmed_product_identity_paths(plan=None, results=None, client=None, limit=2):
    """Return product-board plus confirmed product images for strict usage identity."""
    paths = []
    results = results or {}
    board_record = results.get("product_board") or {}
    board = board_record.get("path") or board_record.get("abspath")
    source_fp = board_record.get("source_fingerprint")
    out_dir = os.path.abspath(results.get("out_dir") or "") if results.get("out_dir") else None
    local_current = bool(
        out_dir and source_fp and _approval_current(out_dir, "product", source_fp))
    registry_current = bool(
        client and source_fp and _registered_board(client, "product", source_fp))
    if board and os.path.isfile(board) and (local_current or registry_current):
        paths.append(os.path.abspath(board))
    refs = ((plan or {}).get("asset_refs") or {}).get("product_images") or []
    for ref in refs:
        candidate = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
        if not os.path.isfile(candidate):
            continue
        if client and not asset_prep.is_product_asset_ready(client, candidate):
            continue
        abs_path = os.path.abspath(candidate)
        if abs_path not in paths:
            paths.append(abs_path)
        if len(paths) >= limit:
            break
    return paths[:limit]


def _hydrate_plan_asset_refs(plan, allow_unconfirmed_product_images=False):
    """Use the client brief as the source of truth for storyboard references.

    The plan is authored by an LLM and may describe a product correctly while
    omitting ``asset_refs``. Never let that omission turn a real uploaded hero
    image into text-only product prompting: recover local brief images before
    validation and product-board generation.
    """
    refs = dict(plan.get("asset_refs") or {})
    # Older plans stored the confirmed usage board under the singular
    # ``product_usage_board`` key. The reference contract and downstream video
    # handoff use the plural ``product_usage_images`` bucket, so migrate the
    # legacy pointer before canonicalize_usage_ref_tags adds @usage. Without
    # this normalization the plan can contain @usage with no registry entry.
    if not refs.get("product_usage_images") and refs.get("product_usage_board"):
        legacy_usage = refs.get("product_usage_board")
        if isinstance(legacy_usage, str):
            candidate = (legacy_usage if os.path.isabs(legacy_usage)
                         else os.path.join(ROOT, legacy_usage))
            if (os.path.isfile(candidate) or
                    legacy_usage.startswith(("http://", "https://", "data:"))):
                refs["product_usage_images"] = [legacy_usage]
    # Filter stale local paths even when no brief exists yet. Otherwise an
    # LLM-authored path can survive hydration and fail only after generation
    # credits have been requested.
    existing = []
    for ref in refs.get("product_images") or []:
        if not isinstance(ref, str):
            continue
        absolute = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
        if os.path.isfile(absolute):
            existing.append(os.path.abspath(absolute))
        elif ref.startswith(("http://", "https://", "data:")):
            existing.append(ref)
    if existing:
        refs["product_images"] = existing[:4]
    elif "product_images" in refs:
        refs.pop("product_images")
    plan["asset_refs"] = refs

    client = plan.get("client")
    if not client:
        return plan
    try:
        import asset_prep
        brief = asset_prep._load_brief(client)
    except (OSError, ValueError, TypeError):
        brief = {}
    # Recover briefs from older runs only when the canonical client brief has
    # no material data. This keeps legacy product facts available without
    # overriding the canonical assets/<client>/brief.json source of truth.
    if not brief.get("images") and not brief.get("product_name"):
        legacy_path = os.path.join(ROOT, "output", client, "assets", "brief.json")
        try:
            if os.path.isfile(legacy_path):
                with open(legacy_path, encoding="utf-8") as handle:
                    legacy_brief = json.load(handle)
                if isinstance(legacy_brief, dict):
                    brief = legacy_brief
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    # A file copied into assets/<client>/images by a host Agent is still an
    # unregistered upload. Do not silently skip it: surface an actionable
    # ingestion error instead of treating the product as absent.
    images_dir = os.path.join(ROOT, "assets", client, "images")
    if os.path.isdir(images_dir):
        registered_paths = {
            os.path.realpath(
                p if os.path.isabs(p) else os.path.join(ROOT, p))
            for item in (brief.get("images") or []) if isinstance(item, dict)
            for p in [item.get("path")] if isinstance(p, str)
        }
        unregistered = []
        for name in sorted(os.listdir(images_dir)):
            path = os.path.join(images_dir, name)
            if (os.path.isfile(path) and not os.path.islink(path) and
                    image_type(path) in {"png", "jpeg", "gif", "webp", "bmp", "tiff"} and
                    os.path.realpath(path) not in registered_paths):
                unregistered.append(path)
        if unregistered:
            raise br_client.BRError(
                "UNREGISTERED_PRODUCT_IMAGE: 检测到客户目录中的产品图尚未登记：%s；"
                "请先执行 `python3 scripts/asset_prep.py ingest-image --client %s --file <图片>`，"
                "再用 confirm-image 确认。" % (", ".join(unregistered), client))

    # Keep only usable local references. A stale path in an LLM-authored plan
    # must not mask the real upload recorded in brief.json. When confirmed
    # product anchors exist, authored product refs must pass the stricter
    # product-ready contract too, so canonical preflight and render_storyboard
    # see the same trusted material set.
    authored_product_refs = list(refs.get("product_images") or [])

    def _draft_ref_exists(path):
        if not isinstance(path, str) or not path.strip():
            return False
        if path.startswith(("https://", "http://")):
            return True
        absolute = path if os.path.isabs(path) else os.path.join(ROOT, path)
        return os.path.isfile(absolute) and not os.path.islink(absolute)

    if allow_unconfirmed_product_images:
        existing = [path for path in authored_product_refs
                    if _draft_ref_exists(path)]
        rejected_product_refs = [path for path in authored_product_refs
                                 if not _draft_ref_exists(path)]
    else:
        existing = [
            path for path in authored_product_refs
            if isinstance(path, str) and asset_prep.is_product_asset_ready(client, path)
        ]
        rejected_product_refs = [
            path for path in authored_product_refs
            if not isinstance(path, str) or not asset_prep.is_product_asset_ready(client, path)
        ]
    tracked = {
        os.path.abspath(p if os.path.isabs(p) else os.path.join(ROOT, p))
        for p in existing if isinstance(p, str) and
        os.path.isfile(p if os.path.isabs(p) else os.path.join(ROOT, p))
    }
    ranked_entries = sorted(
        [entry for entry in (brief.get("images") or []) if isinstance(entry, dict)],
        key=lambda entry: {
            "hero": 0, "front": 1, "product": 2, "detail": 3,
            "side": 4, "pack": 5,
        }.get(str(entry.get("tag") or "").lower(), 99))
    for entry in ranked_entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        tag = str(entry.get("tag") or "").lower() if isinstance(entry, dict) else ""
        if not path or (tag not in {"hero", "product", "pack", "detail", "front", "side"}
                        and not tag.startswith("product-")):
            continue
        # Every product identity source must pass the canonical cleaned-image,
        # current-hash, customer-evidence contract. Legacy/no-status entries and
        # raw uploads are not product-ready merely because the bytes exist.
        if (not allow_unconfirmed_product_images and
                not asset_prep.is_product_asset_ready(client, path)):
            rejected_product_refs.append(path)
            continue
        absolute = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if (os.path.isfile(absolute) and not os.path.islink(absolute) and
                os.path.abspath(absolute) not in tracked):
            existing.append(absolute)
            tracked.add(os.path.abspath(absolute))
    if existing:
        refs["product_images"] = existing[:4]
    elif rejected_product_refs:
        refs.pop("product_images", None)
        raise br_client.BRError(
            "UNCONFIRMED_PRODUCT_IMAGE: 产品源图尚未通过清洗、哈希与客户选版确认：%s；"
            "请先执行 asset_prep.py ingest-image/gen-image，再用 confirm-image "
            "--customer-confirmation 确认。" %
            ", ".join(str(path) for path in rejected_product_refs))
    else:
        refs.pop("product_images", None)
    if brief.get("product_type") and not plan.get("product_type"):
        plan["product_type"] = brief["product_type"]
    # Send verified brief facts together with the product images. Text is not a
    # substitute for the image, but it makes non-negotiable product details
    # explicit to the image model.
    product_facts = {
        key: brief.get(key) for key in
        ("product_name", "product_type", "product_color", "color", "price",
         "features", "usps", "specs", "selling_points", "key_messages")
        if brief.get(key)
    }
    if product_facts:
        plan["product_facts"] = product_facts
    plan["asset_refs"] = refs
    return plan


def expand_product_sku_refs(plan):
    """Resolve a product SKU before fingerprinting so every stage sees one plan."""
    asset_refs = dict(plan.get("asset_refs") or {})
    product_sku = asset_refs.get("product_sku")
    if not product_sku:
        return plan
    try:
        import product_library as _pl
        client = plan.get("client") or asset_refs.get("product_client") or ""
        if not client:
            return plan
        product = _pl.resolve(client, product_sku)
        expanded = []
        if product.get("hero") and os.path.isfile(product["hero"]):
            expanded.append(product["hero"])
        for path in product.get("refs") or []:
            if os.path.isfile(path) and path not in expanded:
                expanded.append(path)
            if len(expanded) >= 4:
                break
        for path in asset_refs.get("product_images") or []:
            if path not in expanded:
                expanded.append(path)
        if expanded:
            asset_refs["product_images"] = expanded[:4]
            plan["asset_refs"] = asset_refs
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("  [product_sku] 展开失败，降级用 product_images 列表: %s" % exc, flush=True)
    return plan


def needs_product_board(plan):
    """Return whether user-provided or planned product references require a product board."""
    refs = plan.get("asset_refs") or {}
    if refs.get("product_sku") or refs.get("product_images"):
        return True
    for shot in plan.get("shots") or []:
        shot_refs = shot.get("asset_refs") or {}
        if shot_refs.get("product_sku") or shot_refs.get("product_images"):
            return True
        if shot.get("product_sku") or shot.get("product_refs"):
            return True
        # A finalized TVC may describe a product in props without the LLM
        # emitting asset_refs. The brief hydration step supplies the image;
        # this fallback makes the missing reference impossible to overlook.
        product_text = " ".join(str(shot.get(k, "")) for k in
                                 ("props", "scene", "visual", "prop_prompts"))
        if re.search(r"产品|product|earbud|earphone|耳机|charging case|充电盒", product_text, re.I):
            return True
    return False


def needs_product_usage_image(plan):
    """Return whether the plan needs an explicit human-product interaction anchor."""
    return bool(workflow_contract.plan_requirements(plan)["requirements"][
        "requires_product_usage"])


def _plan_product_facts(plan, shot=None):
    """Merge product facts from old and new storyboard plan shapes."""
    shot = shot or {}
    facts = {}
    for source in (plan.get("product_facts"), shot.get("product_facts")):
        if isinstance(source, dict):
            facts.update(source)
    brief = plan.get("client_brief") if isinstance(plan.get("client_brief"), dict) else {}
    for key in ("product_type", "features", "usps", "specs"):
        if brief.get(key) and key not in facts:
            facts[key] = brief.get(key)
    for key in ("product_name", "product_type", "product_model", "model",
                "product_color", "color", "features", "specs", "usps"):
        value = shot.get(key)
        if value is None:
            value = plan.get(key)
        if value is not None and key not in facts:
            facts[key] = value
    return facts


def _product_identity_lock(facts):
    product_name = facts.get("product_name") or facts.get("product_type") or "the confirmed product"
    details = []
    for key in ("product_type", "product_model", "model", "product_color",
                "color", "features", "usps", "specs"):
        value = facts.get(key)
        if value:
            details.append("%s=%s" % (key, json.dumps(value, ensure_ascii=False)))
    detail_text = "; ".join(details)
    return (
        "Product identity lock: every panel must copy the exact same product from the confirmed product board. "
        "Product name: %s. %s "
        "Preserve its true silhouette, volume, proportions, material, color, seams, buttons, ports, logos, grille/opening pattern, "
        "surface curvature and all visible functional parts. Do not simplify, flatten, round off, change category, add/remove modules, "
        "or replace it with a generic object. 产品身份锁定：每一格都必须严格继承已确认产品板中的同一件产品，"
        "不得因使用姿势而改变造型、比例、颜色、按钮、接口、Logo、孔位或品类。 "
        "Occlusion, attachment, support, folding, wearing or contact with another object must never change the product's 3D volume or silhouette. "
        "Keep the same thickness, edge profile, curvature, seams, control areas and functional surfaces even when part of the product is hidden. "
        "Never redraw the product as a flat patch, half-disc, hemisphere, wedge, stand base, generic cylinder, sticker or replacement housing. "
        "遮挡、磁吸、承重、折叠、佩戴或与其他物品接触时，产品仍是完整同一实体；禁止被压扁、切掉、拉长、变薄或改造成支撑件。 "
        % (product_name, detail_text)
    )


def _usage_action_shot(plan, shot=None):
    """Pick the shot that best represents the required product-use action."""
    shot = dict(shot or {})
    if shot and (_has_structured_use_relation(shot) or _has_physical_use_action(shot)):
        return shot
    candidates = []
    for index, candidate in enumerate(plan.get("shots") or []):
        if not isinstance(candidate, dict):
            continue
        score = 0
        if _has_structured_use_relation(candidate):
            score += 100
        text = _physical_use_trigger_text(candidate)
        if _has_physical_use_action(candidate):
            score += 20
        if any(term in text for term in ("磁吸", "吸附", "magnetic", "magnet", "magsafe")):
            score += 20
        if any(term in text for term in (
                "底部", "底座", "bottom", "base",
                "背面", "手机背", "phone back", "smartphone back")):
            score += 20
        if any(term in text for term in (
                "贴合", "贴向", "贴到", "接触", "contact", "flush", "attach", "snap")):
            score += 10
        if any(term in text for term in ("支撑", "支架", "stand", "support")):
            score += 5
        if score:
            candidates.append((score, -index, candidate))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][2]
    if shot.get("character_action") or shot.get("action") or shot.get("visual"):
        return shot
    return shot


def _usage_contract_text(plan, shot=None):
    facts = _plan_product_facts(plan, shot)
    parts = [json.dumps(facts, ensure_ascii=False)]
    for source in (shot or {}, plan):
        if isinstance(source, dict):
            parts.extend(str(source.get(key, "")) for key in (
                "product_name", "product_type", "visual", "character_action",
                "action", "props", "prop_prompts", "features", "specs"))
    return " ".join(parts).lower()


def product_usage_outcome_context(plan, primary_shot=None):
    """Collect downstream use-result beats for the usage board.

    The physical relation contract answers "what touches what"; this context
    answers "what practical selling-point result must the image prove".
    Keep it generic so any product category can contribute outcomes through
    shot visual/action/props fields.
    """
    lines = []
    facts = _plan_product_facts(plan, primary_shot)
    for key, label in (("features", "feature"), ("usps", "usp"),
                       ("selling_points", "selling_point"),
                       ("key_messages", "key_message")):
        value = facts.get(key) if key in facts else plan.get(key)
        if isinstance(value, dict):
            for name, detail in value.items():
                lines.append("%s:%s=%s" % (label, name, detail))
        elif isinstance(value, (list, tuple)):
            for item in value:
                lines.append("%s:%s" % (label, item))
        elif value:
            lines.append("%s:%s" % (label, value))
    specs = facts.get("specs") or plan.get("specs") or {}
    if isinstance(specs, dict):
        for name, detail in specs.items():
            lines.append("spec:%s=%s" % (name, detail))
    elif specs:
        lines.append("spec:%s" % specs)
    primary_id = (primary_shot or {}).get("id")
    for candidate in (plan.get("shots") or []):
        if primary_id and candidate.get("id") == primary_id:
            include = True
        else:
            include = _has_physical_use_action(candidate)
        if not include:
            continue
        bits = []
        for key in ("id", "visual", "character_action", "action", "props", "scene_prompt"):
            value = candidate.get(key)
            if value:
                bits.append("%s=%s" % (key, value))
        if bits:
            lines.append("; ".join(bits))
    deduped = []
    seen = set()
    for line in lines:
        if line not in seen:
            deduped.append(line)
            seen.add(line)
    return deduped[:10]


def product_usage_geometry_contract(plan, shot=None):
    """Return a named physical geometry contract for fragile use relations."""
    selected = _usage_action_shot(plan, shot)
    text = _usage_contract_text(plan, selected)
    magnetic = any(term in text for term in ("磁吸", "吸附", "magnetic", "magnet"))
    receiver_back = any(term in text for term in (
        "背面", "back plane", "phone back", "smartphone back", "手机背", "手机的背", "手机背面"))
    product_bottom = any(term in text for term in (
        "底部", "底座", "bottom", "base"))
    if magnetic and receiver_back and product_bottom:
        return "bottom_surface_magnetic_attach_to_receiver_back"
    return None


def _as_relation_dict(value):
    if not isinstance(value, dict):
        return {}
    aliases = {
        "relation_type": ("relation_type", "type", "verb", "action"),
        "active_object": ("active_object", "product", "object", "moving_object"),
        "receiver_object": ("receiver_object", "target", "carrier", "receiving_object", "body_part"),
        "product_contact_surface": (
            "product_contact_surface", "contact_surface", "object_contact_surface",
            "product_surface", "moving_object_surface"),
        "receiver_contact_surface": (
            "receiver_contact_surface", "target_contact_surface", "carrier_contact_surface",
            "receiving_surface", "body_contact_surface"),
        "outward_surface": (
            "outward_surface", "visible_surface", "control_surface", "non_contact_surface"),
        "final_state": ("final_state", "result", "visible_result"),
        "forbidden": ("forbidden", "negative", "forbidden_orientations", "must_not"),
    }
    result = {}
    for key, names in aliases.items():
        for name in names:
            if value.get(name):
                result[key] = value.get(name)
                break
    return result


def product_usage_physical_relation(plan, shot=None):
    """Normalize product-use spatial mechanics before any image generation.

    Preferred input is a structured ``use_relation``/``physical_relation`` dict
    on the shot or plan. Older plans fall back to conservative extraction for
    known high-risk verbs such as magnetic attach, clip, wear, plug and dock.
    """
    selected = _usage_action_shot(plan, shot)
    for source in (selected, plan):
        if isinstance(source, dict):
            for key in ("use_relation", "physical_relation", "usage_relation",
                        "interaction_relation", "product_use_relation"):
                relation = _as_relation_dict(source.get(key))
                if relation:
                    relation.setdefault("source", key)
                    return relation
    text = _usage_contract_text(plan, selected)
    action_text = _physical_use_trigger_text(selected)
    contract = product_usage_geometry_contract(plan, selected)
    facts = _plan_product_facts(plan, selected)
    product_name = facts.get("product_name") or facts.get("product_type") or "confirmed product"
    if contract == "bottom_surface_magnetic_attach_to_receiver_back":
        return {
            "source": "auto:magnetic-bottom-surface-to-receiver-back",
            "relation_type": "magnetic_attach",
            "active_object": product_name,
            "receiver_object": "smartphone" if any(term in text for term in ("手机", "phone", "smartphone")) else "receiver object",
            "product_contact_surface": "bottom/base magnetic surface",
            "receiver_contact_surface": "receiver back plane",
            "outward_surface": "non-contact visible/operable product surface",
            "final_state": "product bottom/base sits flush on the receiver back, product body protrudes outward",
            "forbidden": [
                "front/back/side/control/display/decorative face attached when bottom/base is specified",
                "product redesigned as a flat patch, case, badge, puck, different category or decorative sticker",
                "contact plane hidden or swapped",
            ],
        }
    high_risk = any(term in action_text for term in (
        "attach", "magnetic", "magnet", "磁吸", "吸附", "贴合", "贴到", "佩戴",
        "wear", "clip", "夹", "clamp", "dock", "支架", "stand", "plug", "插入",
        "connect", "连接", "mount", "安装"))
    if not high_risk:
        return {}
    receiver = ""
    if any(term in action_text for term in ("手机", "phone", "smartphone")):
        receiver = "smartphone"
    elif any(term in action_text for term in ("耳", "ear")):
        receiver = "outer ear / ear area"
    elif any(term in action_text for term in ("桌", "desktop", "table")):
        receiver = "desktop/table surface"
    product_surface = "the named or visibly functional product contact surface, never an arbitrary face"
    receiver_surface = "the named receiving surface/body area in the script"
    if any(term in action_text for term in ("底部", "底座", "bottom", "base")):
        product_surface = "bottom/base contact surface"
    if any(term in action_text for term in ("背面", "phone back", "smartphone back", "手机背")):
        receiver_surface = "smartphone back plane"
    return {
        "source": "auto:generic-high-risk-use",
        "relation_type": "physical_use_or_attachment",
        "active_object": product_name,
        "receiver_object": receiver or "the target/carrier object or body part named in the shot",
        "product_contact_surface": product_surface,
        "receiver_contact_surface": receiver_surface,
        "outward_surface": "the product surface that must remain visible/operable after contact",
        "final_state": "the final frame must visibly prove the intended use relation without swapping contact faces",
        "forbidden": [
            "swapping the product contact surface with the visible/control surface",
            "attaching a random product side when a specific surface is named",
            "turning the product into another category or a flat decorative patch",
        ],
    }


def _physical_relation_lock(plan, shot=None):
    relation = product_usage_physical_relation(plan, shot)
    if not relation:
        return ""
    forbidden = relation.get("forbidden") or []
    if isinstance(forbidden, str):
        forbidden = [forbidden]
    return (
        "[PRODUCT-USE PHYSICAL RELATION CONTRACT / 产品使用物理关系合同]\n"
        "Resolve the use action as a rigid mechanical relation before drawing. "
        "This contract overrides generic posing and generic product-on-target imagery.\n"
        "- Relation type: %s\n"
        "- Active product/object: %s\n"
        "- Receiving object/body/scene part: %s\n"
        "- Product contact surface: %s\n"
        "- Receiver contact surface: %s\n"
        "- Product outward/visible/operable surface after contact: %s\n"
        "- Required final state: %s\n"
        "- Required proof panels: include separated-before-contact, alignment-before-contact, side/edge contact-line proof, post-contact result, and real-use result. The viewer must be able to name which two surfaces touch.\n"
        "- Rejection rule: if the contact surfaces are swapped, hidden, inferred from the wrong face, or the product category/shape changes, the image is invalid.\n"
        "- Forbidden: %s\n"
        "中文：先把动作解析成机械装配关系，再生成画面。必须明确“哪个产品面”接触“哪个目标面”，并保留产品外露/可操作面；禁止把接触面、展示面、控制面互换。\n"
        % (
            relation.get("relation_type") or "physical_use",
            relation.get("active_object") or "confirmed product",
            relation.get("receiver_object") or "target object/body part",
            relation.get("product_contact_surface") or "specified product contact surface",
            relation.get("receiver_contact_surface") or "specified receiver contact surface",
            relation.get("outward_surface") or "visible/control surface",
            relation.get("final_state") or "correct visible use result",
            "; ".join(str(item) for item in forbidden) or "wrong contact face or impossible use relation",
        )
    )


def _surface_attachment_lock(relation):
    relation = relation or {}
    forbidden = relation.get("forbidden") or []
    if isinstance(forbidden, str):
        forbidden = [forbidden]
    return (
        "[MANDATORY SURFACE ATTACHMENT GEOMETRY / 表面连接几何硬合同]\n"
        "This is a product-agnostic attachment contract, not a category-specific rule. "
        "The product contact surface is the ONLY surface allowed to touch the receiver contact surface. "
        "Product contact surface: %s. Receiver contact surface: %s. "
        "After contact, the required outward/visible/operable surface is: %s. "
        "Required final state: %s. "
        "Show side/edge contact-line proof in multiple panels so the viewer can identify exactly which two surfaces touch. "
        "Forbidden mistakes: %s. Do not swap the contact surface with the display/control/decorative/visible surface; do not hide the contact plane; do not flatten or redesign the product into another category. "
        "The receiver is a receiving plane, not a platform, tray, shelf, top edge, base or desktop. The active product must protrude outward from that receiving plane after its contact surface is flush against the receiver contact surface. "
        "Never show the active product standing, sitting, resting, balanced, stacked, perched, leaning or placed on top of the receiver, on its edge, or on its front/display side. "
        "中文硬约束：这是通用表面连接合同，不是某个物品的专用规则。只有“产品接触面”可以接触“目标接触面”；产品外露/可操作面必须保持朝外可见。禁止把接触面、展示面、控制面、装饰面互换，禁止把产品画成其它品类或扁平贴片。\n"
        "中文补充：目标物是承载平面，不是托盘/桌面/上沿/底座；产品必须由接触面贴合该平面并向外突出，禁止画成站在、立在、放在、搁在、靠在目标物上。\n"
        "PANEL-SPECIFIC SURFACE PROOF: Panel 3 must show the receiver's broad specified contact plane facing the camera, with the product aligned toward that plane; never use only the receiver edge, rim, top edge or side rail as the proof. "
        "Panel 4 must show the same broad receiver contact plane behind the product plus a visible flush contact line after attachment; the product must protrude outward from that plane. "
        "Panel 7 must show the product's outward/operable surface facing the viewer while the receiver's specified contact plane remains visibly behind it; never attach the product to the receiver display/front face, side edge or top edge. "
        "逐格硬证明：第3格必须看见目标接触面的宽平面，不能只看手机边框；第4格必须同时看见贴合后的目标平面与接触线；第7格展示产品外露面时，目标接触面必须位于产品后方，禁止吸在屏幕、侧边或上沿。\n"
        % (
            relation.get("product_contact_surface") or "specified product contact surface",
            relation.get("receiver_contact_surface") or "specified receiver contact surface",
            relation.get("outward_surface") or "specified visible/operable surface",
            relation.get("final_state") or "correct visible attachment result",
            "; ".join(str(item) for item in forbidden) or "wrong contact face or impossible attachment",
        )
    )


def _asset_composition_brief_text(plan, asset_id):
    briefs = (plan or {}).get("_asset_composition_briefs") or {}
    brief = briefs.get(asset_id) if isinstance(briefs, dict) else None
    if not isinstance(brief, dict):
        return ""
    panel_plan = brief.get("panel_plan") or []
    lines = [
        "[MODEL-GENERATED COMPOSITION BRIEF / 模型构图提示词 skill 输出]",
        "Use this only as composition execution guidance. It cannot override product identity, the physical relation contract, contact surfaces, receiver object, character identity, or no-text rules.",
    ]
    for key, label in (
            ("composition_strategy", "Composition strategy"),
            ("primary_subject_scope", "Primary subject scope"),
            ("camera_scope", "Camera scope"),
            ("range_limits", "Range limits")):
        if brief.get(key):
            lines.append("- %s: %s" % (label, brief[key]))
    if panel_plan:
        lines.append("- Nine-panel composition plan:")
        for index, panel in enumerate(panel_plan, 1):
            if isinstance(panel, dict):
                bits = []
                for key in ("shot_size", "composition", "must_show", "proof_goal", "forbidden"):
                    if panel.get(key):
                        bits.append("%s=%s" % (key, panel[key]))
                lines.append("  %d. %s" % (index, "; ".join(bits) or json.dumps(panel, ensure_ascii=False)))
            elif panel:
                lines.append("  %d. %s" % (index, panel))
    for key, label in (("must_include", "Must include"),
                       ("must_exclude", "Must exclude")):
        values = brief.get(key) or []
        if isinstance(values, str):
            values = [values]
        if values:
            lines.append("- %s: %s" % (label, "; ".join(str(v) for v in values)))
    return "\n".join(lines) + "\n"


def product_usage_prompt(plan, shot=None, include_human=True):
    """Build the physical interaction lock for product-in-use reference imagery."""
    # When called without a specific shot (board-level usage image), fall back
    # to the most action-specific shot in the plan so the model focuses on the
    # core interaction (e.g. magnetic snap-on) instead of generic posing.
    shot = _usage_action_shot(plan, shot)
    facts = _plan_product_facts(plan, shot)
    product_name = facts.get("product_name") or facts.get("product_type") or "the exact uploaded product"
    action = (shot.get("character_action") or shot.get("action") or shot.get("visual") or
              "the person actively and correctly uses the product for its intended purpose")
    subject = (
        "Every panel must show the same confirmed digital human actively using the same confirmed product. "
        if include_human else
        "No digital human is configured. Show the same confirmed product in nine physically credible usage contexts, using only anonymous hands or operator POV when needed; never invent a recognizable person. "
    )
    reference_priority = (
        "The confirmed digital-human board and confirmed product board are the primary subjects of every panel. "
        "The confirmed product board is the highest-priority visual identity anchor; every panel must preserve "
        "the exact product from that board. Any supplied wearing-position reference is only a pose and fit guide; "
        "do not replace the confirmed person or confirmed product with the reference image's person or product. "
    )
    action_line = (
        "Show the person ACTIVELY AND CORRECTLY USING %s, not merely posing beside it. "
        % product_name
        if include_human else
        "Show the exact product performing or being operated for its intended use; do not add a recognizable person. "
    )
    required_action = "Required action: %s. " % action
    shape_lock = _product_identity_lock(facts)
    geometry_contract = product_usage_geometry_contract(plan, shot)
    geometry_lock = ""
    panel_order = (
        "Panel order: establish, front interaction, left interaction, right interaction, "
        "over-shoulder operation, hand/contact close-up, control detail, action result, wider context. "
    )
    relation = product_usage_physical_relation(plan, shot)
    if geometry_contract == "bottom_surface_magnetic_attach_to_receiver_back":
        geometry_lock = _surface_attachment_lock(relation)
        receiver_name = relation.get("receiver_object") or "receiver object"
        receiver_surface = relation.get("receiver_contact_surface") or "receiver contact surface"
        product_surface = relation.get("product_contact_surface") or "product contact surface"
        outward_surface = relation.get("outward_surface") or "outward visible/operable product surface"
        subject = (
            "This is a close-up physical attachment operation board, not a presenter portrait or generic lifestyle usage board. "
            "Every panel must prioritize the confirmed product, the receiver object, the two named contact surfaces, and cropped operator hands only when needed. "
            "A full digital-human face/body may appear in at most one wider context panel; do not center the presenter. "
        )
        reference_priority = (
            "The confirmed product board is the highest-priority visual identity anchor. The confirmed digital-human board is only a secondary hand/operator identity reference. "
            "For this attachment board, the primary subject in every proof panel is the mechanical relation between the same product and the receiver object. "
            "Do not turn the board into presenter portraits, handheld beauty shots, tabletop solo product shots, or app-control phone screen shots. "
        )
        panel_order = (
            "Panel order for this surface attachment: 1 product and receiver separated, clearly showing the product contact surface; "
            "2 hand aligns the product contact surface toward the receiver contact surface; 3 side/edge view just before contact; "
            "4 side/edge view after attachment, contact surfaces flush and product volume protruding outward if applicable; "
            "5 real use with product attached to the receiver; 6 macro contact line; "
            "7 outward visible/operable product surface; 8 stability/action result; "
            "9 wider context proving the product remains the same object from the confirmed product board. "
        )
        action_line = (
            "This board is NOT a generic lifestyle usage board. At least seven of nine panels must visibly prove the same surface-attachment relation: "
            "the active product's specified contact surface touches the receiver object's specified contact surface, while the product's outward/visible/operable surface stays facing outward. "
            "At least seven panels must include BOTH the product and the receiver object in the same frame, with the receiver contact surface visible or inferable from a side/edge contact-line view. "
        )
        required_action = (
            "Required attachment action: %s. Product contact surface=%s. Receiver object=%s. Receiver contact surface=%s. Outward/visible product surface=%s. "
            "Do not replace this with handheld posing, button pressing, tabletop product beauty shots, a receiver front-screen/control-app scene, or a standalone presenter lifestyle scene. "
            % (action, product_surface, receiver_name, receiver_surface, outward_surface)
        )
    relation_lock = _physical_relation_lock(plan, shot)
    composition_brief = _asset_composition_brief_text(plan, "product_usage_image")
    outcome_context = product_usage_outcome_context(plan, shot)
    outcome_block = ""
    if outcome_context:
        outcome_block = (
            "[USAGE OUTCOME / 使用结果与卖点证明]\n"
            "The board must prove not only the physical contact relation, but also the practical use result/selling point that follows from it. "
            "Use these approved script beats as outcome constraints; do not invent unrelated uses:\n"
            + "\n".join("  - %s" % item for item in outcome_context)
            + "\n"
        )
    return "".join((
        "[PRODUCT-IN-USE NINE-PANEL BOARD]\n",
        "Create one 16:9 landscape 3x3 board with exactly nine distinct panels. ",
        subject,
        reference_priority,
        panel_order,
        action_line, required_action,
        outcome_block,
        composition_brief,
        shape_lock,
        relation_lock,
        geometry_lock,
        "Preserve the exact uploaded product geometry, scale, color, orientation and functional parts. "
        "Show anatomically plausible finger placement, all real contact points, grip pressure and occlusion; "
        "the product must remain on the correct side and in the correct operating position. Hands must have exactly five fingers each: "
        "no extra fingers, fused fingers, missing fingers, duplicated hands, floating product, hand-product intersection, "
        "skin passing through the product, or impossible contact. The visible result of the action must match the intended use. "
        "No captions, subtitles, non-product text overlays, watermarks, extra logos or extra product. "
        "Preserve genuine logos, labels, buttons and markings that are physically printed on the confirmed product."
    ))


def asset_prompt_review_items(plan):
    """Return non-shot image prompts that must be reviewed before paid generation."""
    items = []
    if needs_product_board(plan):
        import product_board as _product_board
        facts = _plan_product_facts(plan)
        facts.setdefault("product_type",
                         plan.get("product_type") or
                         ((plan.get("asset_refs") or {}).get("product_type")) or
                         "the exact product")
        if plan.get("product_color"):
            facts.setdefault("product_color", plan.get("product_color"))
        facts.setdefault("style_hint",
                         plan.get("visual_style") or
                         "commercial product reference photography")
        prompt = _product_board.product_board_prompt(facts)
        items.append({
            "asset_id": "product_board",
            "kind": "product_board",
            "stage": "storyboard_asset",
            "submission_prompt_zh": prompt,
            "negative_prompt_zh": "错误品类、错误材质、错误颜色、错误按钮/接口/Logo、错误尺寸比例、人物或使用场景",
            "policy_version": _product_board.PRODUCT_BOARD_POLICY_VERSION,
        })
    if needs_product_usage_image(plan):
        has_human = bool(plan.get("characters") or any(
            shot.get("characters") or shot.get("character_prompt") or
            shot.get("character_action") or shot.get("digital_human") or
            shot.get("actor") for shot in plan.get("shots") or []))
        usage_shot = _usage_action_shot(plan)
        prompt = product_usage_prompt(plan, usage_shot, include_human=has_human)
        item = {
            "asset_id": "product_usage_image",
            "kind": "product_usage_image",
            "stage": "storyboard_asset",
            "submission_prompt_zh": prompt,
            "generation_mode": "panelized_local_composite",
            "negative_prompt_zh": "错误接触面、错误朝向、错误外露面、产品形变、手部畸形、人物或产品替换",
            "policy_version": PRODUCT_USAGE_POLICY_VERSION,
            "physical_relation_contract": product_usage_physical_relation(plan, usage_shot),
            "usage_outcome_context": product_usage_outcome_context(plan, usage_shot),
        }
        geometry = product_usage_geometry_contract(plan, usage_shot)
        if geometry:
            item["geometry_contract"] = geometry
        item["panel_prompts"] = [
            product_usage_panel_prompt(plan, usage_shot, index,
                                       include_human=has_human)
            for index in range(1, 10)
        ]
        item["submission_prompt_zh"] = "\n\n".join(
            "[PANEL %d]\n%s" % (index, panel_prompt)
            for index, panel_prompt in enumerate(item["panel_prompts"], 1))
        items.append(item)
    for item in items:
        item["prompt_fingerprint"] = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]
    return items


def load_plan_json(plan_path):
    """读取并解析 storyboard_plan.json，语法错误时给出人话定位（铁律#14：错误说人话）。

    裸 json.load() 抛出的 JSONDecodeError 只有字符偏移量，Agent/客户都难以
    直接定位到剧本文件里具体哪一行漏了引号/逗号。这里补上：出错行号、出错列号、
    该行原文 + 列指示箭头，让排查一次到位，而不是让 agent 去猜「第几行」。
    """
    if not os.path.exists(plan_path):
        raise br_client.BRError(
            "剧本文件不存在：%s。请先把定稿剧本解析成 storyboard_plan.json 再运行。" % plan_path
        )
    with open(plan_path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw.split("\n")
        bad_line = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        pointer = " " * max(e.colno - 1, 0) + "^"
        # 附带出错行的前一行，多数「漏引号/漏逗号」的真实报错点在上一行的行尾
        prev_line = lines[e.lineno - 2] if e.lineno >= 2 else ""
        context = ""
        if prev_line:
            context = "第 %d 行（上一行，供比对）: %s\n" % (e.lineno - 1, prev_line)
        raise br_client.BRError(
            "storyboard_plan.json 第 %d 行第 %d 列有 JSON 语法错误：%s\n"
            "%s第 %d 行: %s\n      %s\n"
            "常见原因：字符串少了收尾引号、对象/数组里多了或少了逗号、中文引号混入了英文 JSON。"
            "修好这一行后重新运行 storyboard.py。"
            % (e.lineno, e.colno, e.msg, context, e.lineno, bad_line, pointer)
        ) from e




def _approval_path(out_dir, kind):
    return os.path.join(out_dir, ".%s_confirmed.json" % kind)


def _asset_registry_path(client):
    """Return the per-client registry without accepting a path as a client id."""
    return os.path.join(ROOT, "assets", safe_name(client).lower(), "asset_registry.json")


def _asset_registry_key(kind, source_fingerprint):
    return "%s:%s" % (kind, source_fingerprint)


def _load_asset_registry(client):
    try:
        with open(_asset_registry_path(client), encoding="utf-8") as handle:
            registry = json.load(handle)
        if isinstance(registry, dict) and isinstance(registry.get("assets"), dict):
            return registry
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "assets": {}}


def _save_asset_registry(client, registry):
    path = _asset_registry_path(client)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, registry)


def _customer_confirmation_evidence_current(record):
    return bool(
        isinstance(record, dict) and
        record.get("confirmed_by_customer") is True and
        str(record.get("customer_confirmation") or "").strip() and
        record.get("confirmation_source") == "customer_dialogue")


def _registered_board(client, kind, source_fingerprint):
    """Return a still-valid confirmed board from another run, if available."""
    entry = _load_asset_registry(client).get("assets", {}).get(
        _asset_registry_key(kind, source_fingerprint))
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
    if (not source_fingerprint or source_fingerprint == EMPTY_SOURCE_FINGERPRINT or
            not _customer_confirmation_evidence_current(entry) or
            entry.get("kind") != kind or
            entry.get("source_fingerprint") != source_fingerprint or
            not path or not os.path.isfile(path) or
            entry.get("board_sha256") != _file_sha256(path)):
        return None
    return dict(entry, path=os.path.abspath(path), abspath=os.path.abspath(path),
                status="confirmed", reused_from_registry=True, skipped=True)


def _approval_record(out_dir, kind):
    try:
        with open(_approval_path(out_dir, kind), encoding="utf-8") as handle:
            record = json.load(handle)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _remote_media_url(item):
    if not isinstance(item, dict):
        return None
    for key in ("url", "result_url", "remote_url", "download_url", "imageUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    values = item.get("imageUrls")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return None


def _backup_existing_board(path, out_dir, kind):
    if not path or not os.path.isfile(path):
        return None
    backup_dir = os.path.join(out_dir, ".force_regen_backups")
    os.makedirs(backup_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    backup = os.path.join(
        backup_dir, "%s_%s%s" %
        (stem, datetime.now().strftime("%Y%m%d%H%M%S"), ext or ".jpg"))
    shutil.move(path, backup)
    approval = _approval_path(out_dir, kind)
    if os.path.exists(approval):
        os.remove(approval)
    return os.path.abspath(backup)


def _backup_existing_storyboard_shot(path, out_dir):
    if not path or not os.path.isfile(path):
        return None
    backup_dir = os.path.join(out_dir, ".force_regen_backups")
    os.makedirs(backup_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    backup = os.path.join(
        backup_dir, "%s_%s%s" %
        (stem, datetime.now().strftime("%Y%m%d%H%M%S"), ext or ".jpg"))
    shutil.move(path, backup)
    approval = _approval_path(out_dir, "storyboard")
    if os.path.exists(approval):
        os.remove(approval)
    return os.path.abspath(backup)


def _clear_generated_media_fields(record):
    for field in ("url", "result_url", "remote_url", "download_url", "imageUrl",
                  "imageUrls", "task_id", "taskId", "request_id", "sha256",
                  "board_sha256", "confirmed_source_fingerprint"):
        record.pop(field, None)


def _mark_board_regeneration_pending(results, key, *, kind, path,
                                     source_fingerprint, backup_path=None,
                                     reason="force_regeneration"):
    """Replace stale completed board metadata before submitting a new task."""
    previous = results.get(key) if isinstance(results.get(key), dict) else {}
    superseded = {
        "status": previous.get("status"),
        "path": os.path.abspath(previous.get("path") or previous.get("abspath") or path),
        "source_fingerprint": previous.get("source_fingerprint"),
        "cleared_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
    }
    for field in ("url", "result_url", "remote_url", "download_url", "imageUrl",
                  "task_id", "taskId", "request_id", "sha256", "board_sha256"):
        if previous.get(field):
            superseded[field] = previous[field]
    if backup_path:
        superseded["previous_backup"] = os.path.abspath(backup_path)
    pending = dict(previous)
    _clear_generated_media_fields(pending)
    pending.update({
        "path": os.path.abspath(path),
        "abspath": os.path.abspath(path),
        "source_fingerprint": source_fingerprint,
        "status": "pending_regeneration",
        "superseded": superseded,
        "regeneration_started_at": datetime.now().isoformat(timespec="seconds"),
    })
    results[key] = pending
    results["stage"] = key
    results["needs_confirmation"] = True
    results.pop("in_progress", None)
    approval = _approval_path(os.path.dirname(os.path.abspath(path)), kind)
    if os.path.exists(approval):
        os.remove(approval)
    return pending


def _register_confirmed_board(client, kind, record):
    """Persist a confirmed board only after its content-bound approval succeeds."""
    if kind not in ("product", "cast") or not client:
        return
    entry = {
        "kind": kind,
        "source_fingerprint": record["source_fingerprint"],
        "path": record["path"],
        "board_sha256": record["board_sha256"],
        "model": record.get("model"),
        "confirmed_at": record["confirmed_at"],
        "confirmed_by_customer": True,
        "customer_confirmation": record.get("customer_confirmation"),
        "confirmation_source": record.get("confirmation_source"),
    }
    for key in ("url", "result_url", "task_id", "request_id"):
        if record.get(key):
            entry[key] = record[key]
    registry = _load_asset_registry(client)
    registry["assets"][_asset_registry_key(kind, record["source_fingerprint"])] = entry
    _save_asset_registry(client, registry)


def _approval_current(out_dir, kind, source_fingerprint):
    try:
        with open(_approval_path(out_dir, kind), encoding="utf-8") as handle:
            record = json.load(handle)
        if (not source_fingerprint or source_fingerprint == EMPTY_SOURCE_FINGERPRINT or
                not _customer_confirmation_evidence_current(record) or
                record.get("status") != "confirmed" or
                record.get("source_fingerprint") != source_fingerprint):
            return False
        board_path = record.get("path")
        board_sha256 = record.get("board_sha256")
        if board_sha256:
            if not bool(board_path and os.path.isfile(board_path) and
                        _file_sha256(board_path) == board_sha256):
                return False
            if kind == "usage":
                refs = record.get("identity_reference_paths") or []
                if ((record.get("geometry_contract") or
                     record.get("physical_relation_contract")) and
                        not _usage_geometry_review_record_current(record, out_dir)):
                    return False
                if record.get("generation_mode") == "panelized_local_composite":
                    panel_paths = record.get("panel_paths") or []
                    if len(panel_paths) != 9:
                        return False
                    for panel_path in panel_paths:
                        require_contained_path(
                            out_dir, panel_path, label="usage_panel",
                            must_exist=True)
                    panel_urls = record.get("panel_urls") or record.get("imageUrls") or []
                    if len(panel_urls) != 9:
                        return False
                return bool(refs) and all(os.path.isfile(path) for path in refs)
            return True
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _usage_identity_reference_paths(board):
    return storyboard_usage_review.usage_identity_reference_paths(board)


def _validate_usage_board_identity_references(board):
    return storyboard_usage_review.validate_usage_board_identity_references(board)


def _usage_panel_sha256s(board):
    return storyboard_usage_review.usage_panel_sha256s(board)


def _review_bool(value):
    return storyboard_usage_review.review_bool(value)


def _usage_geometry_review_fingerprint(review):
    return storyboard_usage_review.usage_geometry_review_fingerprint(review)


def _load_usage_geometry_review(path, out_dir):
    return storyboard_usage_review.load_usage_geometry_review(path, out_dir)


def _validate_usage_geometry_review(board, review, *, board_sha256,
                                    source_fingerprint):
    return storyboard_usage_review.validate_usage_geometry_review(
        board, review, board_sha256=board_sha256, source_fingerprint=source_fingerprint)


def _usage_geometry_review_record_current(record, out_dir):
    return storyboard_usage_review.usage_geometry_review_record_current(record, out_dir)


def confirm_board(result_json, kind, geometry_reviewed=False, geometry_review_path=None,
                  customer_confirmation=None):
    """Confirm one generated board and bind approval to its source fingerprint."""
    with open(result_json, encoding="utf-8") as handle:
        results = json.load(handle)
    keys = {
        "product": ("product_board", "产品板"),
        "cast": ("cast_board", "人物板"),
        "usage": ("product_usage_image", "产品使用图"),
    }
    if kind not in keys:
        raise br_client.BRError("不支持的确认类型: %s" % kind)
    key, label = keys[kind]
    board = results.get(key)
    if not board:
        raise br_client.BRError("没有可确认的%s。" % label)
    path = board.get("path") or board.get("abspath")
    if not path or not os.path.isfile(path):
        raise br_client.BRError("待确认板文件不存在: %s" % path)
    fingerprint = board.get("source_fingerprint")
    if not fingerprint or fingerprint == EMPTY_SOURCE_FINGERPRINT:
        raise br_client.BRError("待确认板缺少源素材指纹，请重新生成后再确认。")
    out_dir = os.path.dirname(os.path.abspath(result_json))
    try:
        path = require_contained_path(
            out_dir, path, label="storyboard_board", must_exist=True)
    except ValueError as exc:
        raise br_client.BRError(str(exc)) from exc
    identity_refs = []
    geometry_review = None
    geometry_review_file = None
    board_sha256 = _file_sha256(path)
    if kind == "usage":
        if board.get("generation_mode") == "panelized_local_composite":
            panel_paths = []
            for panel_path in board.get("panel_paths") or []:
                try:
                    panel_paths.append(require_contained_path(
                        out_dir, panel_path, label="usage_panel",
                        must_exist=True))
                except ValueError as exc:
                    raise br_client.BRError(str(exc)) from exc
            board["panel_paths"] = panel_paths
        identity_refs = _validate_usage_board_identity_references(board)
        if board.get("geometry_contract") or board.get("physical_relation_contract"):
            review = _load_usage_geometry_review(geometry_review_path, out_dir)
            geometry_review_file = review.get("_path")
            geometry_review = _validate_usage_geometry_review(
                board, review, board_sha256=board_sha256,
                source_fingerprint=fingerprint)
    remote_url = _remote_media_url(board)
    if not remote_url:
        raise br_client.BRError(
            "%s缺少 BasicRouter 图片 retrieve URL，不能确认进入视频链路；"
            "人物板/产品使用图请用 --force-board 重新生成；故事板单镜头请用 --force-shot。"
            % label)
    confirmation_text = str(customer_confirmation or "").strip()
    if not confirmation_text and geometry_review:
        confirmation_text = str(geometry_review.get("customer_confirmation") or "").strip()
    try:
        confirmation_text = run_manifest_store.require_explicit_customer_confirmation(
            confirmation_text, kind)
    except ValueError as exc:
        raise br_client.BRError(str(exc)) from exc
    record = {"status": "confirmed", "kind": kind,
               "source_fingerprint": fingerprint,
              "path": os.path.abspath(path),
              "board_sha256": _file_sha256(path),
              "client": results.get("client"),
              "run_id": results.get("run_id"),
              "plan_fingerprint": results.get("plan_fingerprint"),
              "model": results.get("model"),
              "confirmed_at": datetime.now().isoformat(timespec="seconds"),
              "confirmed_by_customer": True,
              "customer_confirmation": confirmation_text,
              "confirmation_source": "customer_dialogue",
              "url": remote_url}
    for field in ("url", "result_url", "task_id", "request_id"):
        if board.get(field):
            record[field] = board[field]
    if identity_refs:
        record["identity_reference_paths"] = identity_refs
    if board.get("geometry_contract"):
        record["geometry_contract"] = board["geometry_contract"]
        record["geometry_reviewed"] = bool(geometry_review)
    if board.get("physical_relation_contract"):
        record["physical_relation_contract"] = board["physical_relation_contract"]
        record["geometry_reviewed"] = bool(geometry_review)
    if geometry_review:
        record["geometry_review"] = geometry_review
        record["geometry_review_path"] = geometry_review_file
        record["geometry_review_fingerprint"] = geometry_review["review_fingerprint"]
    if board.get("usage_policy_version"):
        record["usage_policy_version"] = board["usage_policy_version"]
    if kind == "usage":
        for field in ("board_type", "generation_mode"):
            if board.get(field):
                record[field] = board[field]
        if board.get("panel_paths"):
            record["panel_paths"] = [os.path.abspath(path)
                                     for path in board.get("panel_paths")]
        if board.get("panel_urls"):
            record["panel_urls"] = list(board.get("panel_urls") or [])
    manifest_stage = {
        "product": "product_board",
        "cast": "cast_board",
        "usage": "product_usage",
    }[kind]
    _preflight_manifest_approval(results, manifest_stage)
    approval_path = _approval_path(out_dir, kind)
    tracked_paths = [
        approval_path,
        os.path.abspath(result_json),
        os.path.join(out_dir, "storyboard_index.md"),
        os.path.join(out_dir, "storyboard_embedded.md"),
        os.path.join(out_dir, "storyboard_preview.html"),
    ]
    originals = {
        tracked: Path(tracked).read_bytes() if os.path.isfile(tracked) else None
        for tracked in tracked_paths
    }
    board["status"] = "confirmed"
    board["confirmed_source_fingerprint"] = fingerprint
    board["board_sha256"] = board_sha256
    board["sha256"] = board_sha256
    pending_boards = []
    for board_key in ("product_board", "cast_board", "product_usage_image"):
        candidate = results.get(board_key)
        if isinstance(candidate, dict) and str(candidate.get("status") or "").lower() in {
                "pending", "pending_confirmation", "needs_confirmation"}:
            pending_boards.append(board_key)
    results["needs_confirmation"] = bool(pending_boards)
    results["pending_confirmation_boards"] = pending_boards
    committed = False
    try:
        plan_path = results.get("plan_source")
        if plan_path and os.path.isfile(plan_path):
            plan = load_plan_json(plan_path)
            index_md, embedded_md, preview_html = write_index(plan, results, out_dir)
            results["index_md"] = index_md
            results["embedded_md"] = embedded_md
            results["preview_html"] = preview_html
        _atomic_json_replace(approval_path, record)
        _atomic_json_replace(result_json, results)
        _approve_manifest_stage(
            results, result_json, manifest_stage, approval_path, record)
        committed = True
    finally:
        if not committed:
            for tracked, original in originals.items():
                _restore_file_bytes(tracked, original)
    try:
        _register_confirmed_board(results.get("client"), kind, record)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print("[storyboard] confirmed board registry refresh skipped: %s" % exc,
              flush=True)
    _refresh_workflow_canvas(
        results, result_json, reason="board_confirmed:%s" % kind)
    return record


def refine_board(result_json, kind, edit_prompt, *, feedback_refs=None, model=None):
    """Regenerate a pending board from customer feedback without advancing gates.

    Storyboard stage boards are not normal asset_prep candidates: downstream
    code expects stable filenames such as ``product_usage_board.jpg``. Refining
    a board therefore backs up the currently displayed file, replaces the same
    fixed path with the new candidate, and removes the old approval marker so
    the customer must review the revised image before the next stage can run.
    """
    keys = {
        "product": ("product_board", "产品板"),
        "cast": ("cast_board", "人物板"),
        "usage": ("product_usage_image", "产品使用图"),
    }
    if kind not in keys:
        raise br_client.BRError("不支持的精修类型: %s" % kind)
    if not edit_prompt or not str(edit_prompt).strip():
        raise br_client.BRError("精修必须提供 --edit 修改说明。")
    with open(result_json, encoding="utf-8") as handle:
        results = json.load(handle)
    key, label = keys[kind]
    board = results.get(key)
    if not board:
        raise br_client.BRError("没有可精修的%s。" % label)
    out_dir = os.path.dirname(os.path.abspath(result_json))
    board_path = board.get("path") or board.get("abspath")
    try:
        board_path = require_contained_path(
            out_dir, board_path, label="storyboard_board", must_exist=True)
    except ValueError as exc:
        raise br_client.BRError(str(exc)) from exc

    manifest_stage = {
        "product": "product_board",
        "cast": "cast_board",
        "usage": "product_usage",
    }[kind]
    if kind == "usage" and not results.get("manifest_path"):
        missing_confirmed = []
        for result_key, approval_kind in (
                ("product_board", "product"), ("cast_board", "cast")):
            candidate = results.get(result_key) or {}
            source_fp = candidate.get("source_fingerprint")
            current = bool(
                source_fp and (
                    _approval_current(out_dir, approval_kind, source_fp) or
                    _registered_board(
                        results.get("client"), approval_kind, source_fp)))
            if not current:
                missing_confirmed.append(approval_kind)
        if missing_confirmed:
            raise br_client.BRError(
                "GENERATION_BLOCKED: 产品使用图精修前必须确认当前 %s 板件。" %
                ", ".join(missing_confirmed))

    feedback_paths = []
    for ref in ([feedback_refs] if isinstance(feedback_refs, str)
                else list(feedback_refs or [])):
        candidate = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
        if not os.path.isfile(candidate):
            raise br_client.BRError("feedback image not found: %s" % ref)
        feedback_paths.append(os.path.abspath(candidate))

    # This is a paid regeneration entry. Bind/revoke the authoritative stage
    # before loading the key or submitting the request.
    _begin_manifest_generation(results, manifest_stage, force=True)
    key_setup.ensure_session_id()
    api_key = key_setup.load_key()
    if not api_key:
        raise br_client.BRError("no API key; run key onboarding first")

    # BasicRouter image refs are limited. For product-in-use refinements the
    # confirmed product board must be the strongest identity anchor; otherwise
    # a previously drifted usage board can teach the model the wrong shape.
    reference_paths = []
    identity_reference_paths = []
    if kind == "usage":
        plan_for_refs = {}
        plan_path = results.get("plan_source")
        if plan_path and os.path.isfile(plan_path):
            try:
                plan_for_refs = load_plan_json(plan_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                plan_for_refs = {}
        identity_reference_paths = _confirmed_product_identity_paths(
            plan_for_refs, results, client=results.get("client"), limit=2)
        reference_paths.extend(identity_reference_paths)
        reference_paths.append(board_path)
        reference_paths.extend(feedback_paths)
        cast = (results.get("cast_board") or {}).get("path") or (results.get("cast_board") or {}).get("abspath")
        if cast and os.path.isfile(cast) and os.path.abspath(cast) not in {
                os.path.abspath(p) for p in reference_paths}:
            reference_paths.append(cast)
    else:
        reference_paths = [board_path]
        reference_paths.extend(feedback_paths)
    reference_paths = reference_paths[:4]
    image_refs = _collect_image_urls(
        reference_paths, api_key, fail_on_invalid=True, label="%s精修参考图" % label)

    product_identity_lock = ""
    if kind == "usage":
        facts = {}
        plan_path = results.get("plan_source")
        if plan_path and os.path.isfile(plan_path):
            try:
                facts = _plan_product_facts(load_plan_json(plan_path))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                facts = {}
        product_identity_lock = (
            "The first product reference images are the confirmed product identity anchors and have the highest priority: "
            + _product_identity_lock(facts) +
            "The previous usage/pose board is only for human pose and phone attachment relation, "
            "but do not inherit any product-shape drift from it. "
            "Do not simplify the product into a flat puck, disk, badge, phone grip, charger, case, or generic object. "
        )
    prompt = (
        "Refine the displayed %s according to the customer's feedback. "
        "Keep the same board format, same confirmed product identity, same confirmed digital human identity, "
        "same clean commercial photography style, and the same general composition unless the feedback explicitly asks to change it. "
        "%s"
        "Only change the requested issue: %s. "
        "If feedback reference images are provided, use them only to understand the correction target; "
        "do not copy unrelated people, text, UI, borders, watermarks, logos, or backgrounds from feedback images. "
        "No subtitles, no added graphic text, no watermark."
        % (label, product_identity_lock, str(edit_prompt).strip())
    )

    backup_dir = os.path.join(out_dir, ".refine_backups")
    os.makedirs(backup_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(board_path))
    backup_path = os.path.join(
        backup_dir, "%s_%s%s" % (stem, datetime.now().strftime("%Y%m%d%H%M%S"), ext or ".jpg"))
    os.replace(board_path, backup_path)
    tmp_out = board_path + ".refine.jpg"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)

    def refine_progress(event):
        if event.get("status") in ("submitted", "success", "failed"):
            print("[storyboard] refine %s: %s" % (kind, event.get("status")), flush=True)
        elif event.get("waited", 0) and event["waited"] % 30 == 0:
            print("[storyboard] refine %s still processing (%ss)" %
                  (kind, event["waited"]), flush=True)

    refined_committed = False
    try:
        refined = download_first_image(
            api_key, prompt, tmp_out, model=model or results.get("model") or DEFAULT_MODEL,
            ratio="16:9", image_urls=image_refs, on_progress=refine_progress)
        os.replace(tmp_out, board_path)
        refined_committed = True
    finally:
        if not refined_committed:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            if os.path.exists(backup_path) and not os.path.exists(board_path):
                os.replace(backup_path, board_path)

    approval_path = _approval_path(out_dir, kind)
    if os.path.exists(approval_path):
        os.remove(approval_path)
    previous_sha = board.get("sha256") or board.get("board_sha256")
    board.update(refined)
    board["path"] = os.path.abspath(board_path)
    board["abspath"] = os.path.abspath(board_path)
    board["sha256"] = _file_sha256(board_path)
    board["status"] = "pending"
    board.pop("confirmed_source_fingerprint", None)
    board["refinement"] = {
        "edit_prompt": str(edit_prompt).strip(),
        "feedback_refs": feedback_paths,
        "generation_reference_paths": [os.path.abspath(p) for p in reference_paths],
        "identity_reference_paths": [os.path.abspath(p) for p in identity_reference_paths],
        "previous_backup": os.path.abspath(backup_path),
        "previous_sha256": previous_sha,
        "refined_at": datetime.now().isoformat(timespec="seconds"),
    }
    results[key] = board
    results["stage"] = key
    results["needs_confirmation"] = True
    plan_path = results.get("plan_source")
    if plan_path and os.path.isfile(plan_path):
        try:
            plan = load_plan(plan_path)
            index_md, embedded_md, preview_html = write_index(plan, results, out_dir)
            results["index_md"] = index_md
            results["embedded_md"] = embedded_md
            results["preview_html"] = preview_html
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    atomic_write_json(result_json, results)
    _register_storyboard_result_artifacts(results, result_json)
    _refresh_workflow_canvas(
        results, result_json, reason="board_refined:%s" % kind)
    return {"status": "pending", "kind": kind, "path": os.path.abspath(board_path),
            "sha256": board["sha256"], "backup_path": os.path.abspath(backup_path),
            "feedback_refs": feedback_paths, "needs_confirmation": True}


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_shot_matches_plan(existing, shot, plan_fingerprint_value, out_path,
                                expected_reference_fingerprint=None):
    """Reuse a shot only when checkpoint metadata and bytes prove provenance."""
    if not isinstance(existing, dict) or not os.path.isfile(out_path):
        return False
    # New checkpoints are panel-scoped. Legacy checkpoints retain the stricter
    # whole-plan fallback because they cannot prove which visual fields matched.
    expected_shot_fp = shot_fingerprint(shot)
    recorded_shot_fp = existing.get("shot_fingerprint")
    if recorded_shot_fp:
        if recorded_shot_fp != expected_shot_fp:
            return False
        if (expected_reference_fingerprint and
                existing.get("reference_fingerprint") != expected_reference_fingerprint):
            return False
    elif existing.get("plan_fingerprint") != plan_fingerprint_value:
        return False
    if not recorded_shot_fp and (existing.get("shot") or {}) != shot:
        return False
    recorded_sha = existing.get("sha256")
    return bool(recorded_sha and recorded_sha == _file_sha256(out_path))


def _storyboard_approval_context(results, result_json):
    result_json = os.path.abspath(result_json)
    out_dir = os.path.abspath(results.get("out_dir") or os.path.dirname(result_json))
    if os.path.dirname(result_json) != out_dir:
        raise br_client.BRError("故事板结果的 out_dir 与 result_json 所在目录不一致。")
    plan_fp = results.get("plan_fingerprint")
    client = results.get("client")
    run_id = results.get("run_id")
    if not plan_fp or not client or not run_id:
        raise br_client.BRError("故事板结果缺少 plan fingerprint、client 或 run id，请重新生成。")
    design_board = bool(
        str(results.get("storyboard_role") or results.get("storyboard_usage") or "").strip().lower().replace("-", "_") == "design_board" or
        str(results.get("video_model_usage") or "").strip().lower() == "do_not_submit_as_storyboard_ref")
    deterministic_design_board = bool(
        design_board and results.get("design_board_renderer") == "remotion.IntegratedExplainer")
    manifest = None
    manifest_root = out_dir
    if deterministic_design_board:
        manifest_path = results.get("manifest_path")
        binding = results.get("manifest_binding") or {}
        if not manifest_path or not os.path.isfile(manifest_path):
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_BINDING_REQUIRED: 正式执行预演未绑定 run_manifest.json。")
        manifest_path = os.path.abspath(manifest_path)
        if not (binding.get("status") == "bound" and
                binding.get("approval_eligible") is True and
                os.path.abspath(binding.get("manifest_path") or "") == manifest_path):
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_BINDING_INVALID: 执行预演只是诊断稿，不能确认。")
        manifest = run_manifest_store.load_manifest(manifest_path)
        if (manifest.get("client") != client or manifest.get("run_id") != run_id):
            raise br_client.BRError("DESIGN_BOARD_MANIFEST_IDENTITY_MISMATCH")
        minimum_revision = int(
            binding.get("expected_manifest_revision_after_registration") or 0)
        if int(manifest.get("revision") or 0) < minimum_revision:
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_REGISTRATION_INCOMPLETE: 预演产物尚未完成登记。")
        storyboard_status = ((manifest.get("generation") or {}).get(
            "storyboard") or {}).get("status")
        if storyboard_status not in {"pending_approval", "approved"}:
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_STAGE_INVALID: storyboard 未处于 pending_approval/approved。")
        if (storyboard_status == "approved" and
                not run_manifest_store.approval_is_current(manifest, "storyboard")):
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_APPROVAL_STALE: storyboard 审批证据已失效。")
        manifest_root = os.path.dirname(manifest_path)
    return {
        "result_json": result_json, "out_dir": out_dir, "plan_fingerprint": plan_fp,
        "client": client, "run_id": run_id, "design_board": design_board,
        "deterministic_design_board": deterministic_design_board,
        "manifest": manifest, "manifest_root": manifest_root, "binding": binding,
    }


def _storyboard_approval_shots(results, context):
    deterministic_design_board = context["deterministic_design_board"]
    manifest_root = context["manifest_root"]
    out_dir = context["out_dir"]
    shots = []
    seen = set()
    for item in results.get("shots") or []:
        shot_id = str((item.get("shot") or {}).get("id") or "").strip()
        path = item.get("abspath") or item.get("path")
        path = os.path.abspath(path) if path else None
        if not shot_id or shot_id in seen:
            raise br_client.BRError("故事板 shot id 缺失或重复，不能整体确认。")
        if not path or not os.path.isfile(path):
            raise br_client.BRError("待确认故事板文件不存在: %s" % path)
        remote_url = _remote_media_url(item)
        if not remote_url and not deterministic_design_board:
            raise br_client.BRError(
                "故事板 shot %s 缺少 BasicRouter 图片 retrieve URL，不能确认进入视频链路；"
                "请用 --stage storyboard --force-shot %s 重新生成该分镜，"
                "不要手动删除图片或全量重跑。" % (shot_id, shot_id))
        if deterministic_design_board and (
                item.get("deterministic_render") is not True or
                item.get("renderer") != "remotion.IntegratedExplainer"):
            raise br_client.BRError(
                "DESIGN_BOARD_RENDERER_MISMATCH: 镜头 %s 不是由正式 IntegratedExplainer 组件渲染，"
                "不能作为执行版式合同确认。" % shot_id)
        try:
            path = require_contained_path(
                manifest_root if deterministic_design_board else out_dir,
                path, label="storyboard_shot", must_exist=True)
        except ValueError as exc:
            raise br_client.BRError(str(exc)) from exc
        seen.add(shot_id)
        actual_shot_sha = _file_sha256(path)
        if deterministic_design_board and item.get("sha256") != actual_shot_sha:
            raise br_client.BRError(
                "DESIGN_BOARD_STILL_STALE: 镜头 %s 的执行版式静帧内容已变化，"
                "请重新渲染并展示后再确认。" % shot_id)
        shot_fp = item.get("shot_fingerprint") or shot_fingerprint(item.get("shot") or {})
        shot_record = {"id": shot_id, "path": path, "sha256": actual_shot_sha,
                       "shot_fingerprint": shot_fp}
        if remote_url:
            shot_record["url"] = remote_url
        if deterministic_design_board:
            shot_record.update({
                "renderer": "remotion.IntegratedExplainer",
                "frame": item.get("frame"),
                "deterministic_render": True,
            })
        for field in ("task_id", "request_id"):
            if item.get(field):
                shot_record[field] = item[field]
        shots.append(shot_record)
    if not shots:
        raise br_client.BRError("没有可确认的分段故事板。")
    expected_ids = [str(value) for value in (results.get("expected_shot_ids") or [])]
    if expected_ids and (len(expected_ids) != len(set(expected_ids)) or set(expected_ids) != seen):
        missing = sorted(set(expected_ids) - seen)
        extra = sorted(seen - set(expected_ids))
        raise br_client.BRError(
            "故事板不完整，不能整体确认。缺失=%s，多出=%s" % (missing, extra))
    return shots


def _storyboard_execution_artifacts(results, context):
    deterministic_design_board = context["deterministic_design_board"]
    manifest_root = context["manifest_root"]
    out_dir = context["out_dir"]
    binding = context["binding"]
    manifest = context["manifest"]
    deterministic_artifacts = {}
    if deterministic_design_board:
        animatic = results.get("animatic") if isinstance(results.get("animatic"), dict) else {}
        animatic_path = animatic.get("path")
        if not animatic_path or not os.path.isfile(animatic_path):
            raise br_client.BRError(
                "DESIGN_BOARD_ANIMATIC_REQUIRED: 执行版式板必须连同同一时间轴的 Animatic 一起确认。")
        animatic_path = require_contained_path(
            manifest_root, animatic_path, label="design_board_animatic", must_exist=True)
        actual_animatic_sha = _file_sha256(animatic_path)
        if animatic.get("sha256") != actual_animatic_sha:
            raise br_client.BRError(
                "DESIGN_BOARD_ANIMATIC_STALE: Animatic 内容已变化，请重新渲染并展示后再确认。")
        keyframe_contract = results.get("generation_keyframe_contract") or {}
        if (keyframe_contract.get("role") != "avatar_plate_only" or
                keyframe_contract.get("storyboard_ref") is not False or
                keyframe_contract.get("clean_plate") is not True):
            raise br_client.BRError(
                "GENERATION_KEYFRAME_CONTRACT_INVALID: 视频模型输入必须是干净 avatar plate，"
                "且不得把设计板作为 storyboard_ref。")
        bound_files = {}
        artifact_candidates = {
            "execution_preview_result": (
                results.get("execution_preview_result"), manifest_root),
            "composite_plan": (results.get("composite_plan_path"), out_dir),
            "integrated_previs_spec": (
                (results.get("integrated_previs_spec") or {}).get("path"), manifest_root),
        }
        for label, (value, allowed_root) in artifact_candidates.items():
            if not value or not os.path.isfile(value):
                raise br_client.BRError(
                    "DESIGN_BOARD_ARTIFACT_REQUIRED: 缺少 %s，不能确认执行版式合同。" % label)
            contained = require_contained_path(
                allowed_root, value, label="design_board_%s" % label, must_exist=True)
            bound_files[label] = {"path": contained, "sha256": _file_sha256(contained)}
        execution_record = _read_result(bound_files["execution_preview_result"]["path"])
        if (execution_record or {}).get("manifest_binding") != binding:
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_BINDING_DIVERGED: 结果文件与执行预演绑定不一致。")
        execution_artifact_path = bound_files["execution_preview_result"]["path"]
        execution_artifact_sha = _file_sha256(execution_artifact_path)
        registered = {
            (item.get("path"), item.get("sha256"))
            for item in (((manifest.get("generation") or {}).get("storyboard") or {}).get(
                "artifacts") or []) if isinstance(item, dict)
        }
        if (execution_artifact_path, execution_artifact_sha) not in registered:
            raise br_client.BRError(
                "DESIGN_BOARD_MANIFEST_ARTIFACT_MISSING: execution_preview_result 未登记或已变化。")
        if int((execution_record or {}).get("schema_version") or 0) < 2:
            raise br_client.BRError(
                "DESIGN_BOARD_COMMERCIAL_CONTRACT_V2_REQUIRED: 请重渲新版执行预演。")
        commercial_contract = {}
        if int((execution_record or {}).get("schema_version") or 1) >= 2:
            approved_render = execution_record.get("approved_render_contract") or {}
            integrated_record = execution_record.get("integrated_spec") or {}
            if (approved_render.get("formal_render_must_bind_this_spec") is not True or
                    approved_render.get("spec_sha256") != integrated_record.get("sha256") or
                    not approved_render.get("frozen_fingerprint")):
                raise br_client.BRError(
                    "DESIGN_BOARD_FREEZE_CONTRACT_INVALID: 执行预演没有可冻结的正式合成规格。")
            extra_artifacts = {
                "content_contract_spec": execution_record.get("content_contract_spec") or {},
                "content_previs_spec": execution_record.get("content_previs_spec") or {},
                "content_layer": execution_record.get("content_layer") or {},
                "hyperframes_spec": execution_record.get("hyperframes_spec") or {},
                "motion_layer": execution_record.get("motion_layer") or {},
            }
            for label, artifact in extra_artifacts.items():
                path = artifact.get("path") if isinstance(artifact, dict) else None
                if label == "motion_layer" and not path and artifact.get("required") is False:
                    continue
                if not path or not os.path.isfile(path):
                    raise br_client.BRError(
                        "DESIGN_BOARD_ARTIFACT_REQUIRED: 缺少 %s，不能确认商业级执行合同。" % label)
                contained = require_contained_path(
                    manifest_root, path, label="design_board_%s" % label, must_exist=True)
                actual_sha = _file_sha256(contained)
                if artifact.get("sha256") != actual_sha:
                    raise br_client.BRError(
                        "DESIGN_BOARD_ARTIFACT_STALE: %s 已变化，请重新渲染确认。" % label)
                bound_files[label] = {"path": contained, "sha256": actual_sha}
                if label == "motion_layer" and artifact.get("master_path"):
                    master = require_contained_path(
                        manifest_root, artifact["master_path"],
                        label="design_board_motion_master", must_exist=True)
                    master_sha = _file_sha256(master)
                    if artifact.get("master_sha256") != master_sha:
                        raise br_client.BRError(
                            "DESIGN_BOARD_ARTIFACT_STALE: motion_master 已变化，请重新渲染确认。")
                    bound_files["motion_master"] = {"path": master, "sha256": master_sha}
            commercial_contract = {
                "schema_version": 2,
                "approved_render_contract": approved_render,
                "audio_timing_contract": execution_record.get("audio_timing_contract") or {},
                "hyperframes_required": bool((execution_record.get("hyperframes_spec") or {}).get(
                    "renderable_scenes")),
                "formal_render_must_bind_approved_spec": True,
            }
        deterministic_artifacts = {
            "renderer_source_of_truth": "remotion.IntegratedExplainer",
            "animatic": {"path": animatic_path, "sha256": actual_animatic_sha},
            **bound_files,
            "generation_keyframe_contract": keyframe_contract,
        }
        if commercial_contract:
            deterministic_artifacts["commercial_render_contract"] = commercial_contract
    return deterministic_artifacts


def _storyboard_approval_record(results, result_json):
    """Build a content-bound approval record for the complete shot storyboard."""
    context = _storyboard_approval_context(results, result_json)
    shots = _storyboard_approval_shots(results, context)
    deterministic_artifacts = _storyboard_execution_artifacts(results, context)
    out_dir = context["out_dir"]
    plan_fp = context["plan_fingerprint"]
    client = context["client"]
    run_id = context["run_id"]
    design_board = context["design_board"]
    record = {
        "status": "confirmed",
        "kind": "design_board" if design_board else "storyboard",
        "approval_scope": (
            "composition_design_contract" if design_board else
            "video_storyboard_reference"),
        "storyboard_usage": "design_board" if design_board else "video_storyboard",
        "video_model_usage": (
            "do_not_submit_as_storyboard_ref" if design_board else
            "submit_per_shot"),
        "final_composition_owner": (
            "remotion" if design_board else "storyboard"),
        "composition_components": (
            ["remotion", "hyperframes", "ffmpeg"] if design_board else
            ["video_model", "storyboard_ref"]),
        "client": client,
        "run_id": run_id,
        "out_dir": out_dir,
        "plan_fingerprint": plan_fp,
        "shots": shots,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if deterministic_artifacts:
        record["execution_artifacts"] = deterministic_artifacts
    return record


def _storyboard_approval_artifact_sha256(record):
    payload = {
        key: value for key, value in record.items()
        if key not in {"approval_artifact_sha256", "artifact_short_id"}
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def confirm_storyboard(result_json, customer_confirmation=None):
    """Persist confirmation of the complete storyboard and all shot bytes."""
    result_json = os.path.abspath(result_json)
    results = _read_result(result_json)
    if not results:
        raise br_client.BRError("无法读取待确认的 storyboard_result.json。")
    record = _storyboard_approval_record(results, result_json)
    try:
        confirmation_text = run_manifest_store.require_explicit_customer_confirmation(
            customer_confirmation, "storyboard")
    except ValueError as exc:
        raise br_client.BRError(str(exc)) from exc
    record["confirmed_by_customer"] = True
    record["customer_confirmation"] = confirmation_text
    record["confirmation_source"] = "customer_dialogue"
    record["approval_artifact_sha256"] = _storyboard_approval_artifact_sha256(record)
    record["artifact_short_id"] = record["approval_artifact_sha256"][:12]
    approval_path = _approval_path(record["out_dir"], "storyboard")
    _preflight_manifest_approval(results, "storyboard")
    tracked_paths = [
        approval_path,
        result_json,
        os.path.join(record["out_dir"], "storyboard_index.md"),
        os.path.join(record["out_dir"], "storyboard_embedded.md"),
        os.path.join(record["out_dir"], "storyboard_preview.html"),
    ]
    originals = {
        tracked: Path(tracked).read_bytes() if os.path.isfile(tracked) else None
        for tracked in tracked_paths
    }
    results["storyboard_approval"] = record
    results["needs_confirmation"] = False
    results["pending_confirmation_boards"] = []
    committed = False
    try:
        plan_path = results.get("plan_source")
        if plan_path and os.path.isfile(plan_path):
            plan = load_plan_json(plan_path)
            index_md, embedded_md, preview_html = write_index(
                plan, results, record["out_dir"])
            results["index_md"] = index_md
            results["embedded_md"] = embedded_md
            results["preview_html"] = preview_html
        _atomic_json_replace(approval_path, record)
        _atomic_json_replace(result_json, results)
        _approve_manifest_stage(
            results, result_json, "storyboard", approval_path, record)
        committed = True
    finally:
        if not committed:
            for tracked, original in originals.items():
                _restore_file_bytes(tracked, original)
    _refresh_workflow_canvas(
        results, result_json, reason="storyboard_confirmed")
    return record


def storyboard_approval_is_current(result_json, *, client=None, run_id=None,
                                   out_dir=None, plan_fingerprint_value=None):
    """Re-read approval and shot files for downstream identity/generation gates."""
    result_json = os.path.abspath(result_json)
    results = _read_result(result_json)
    if not results:
        return False
    actual_out_dir = os.path.abspath(results.get("out_dir") or os.path.dirname(result_json))
    record = _read_result(_approval_path(actual_out_dir, "storyboard"))
    if (not record or record.get("status") != "confirmed" or
            not _customer_confirmation_evidence_current(record)):
        return False
    approval_identity = record.get("approval_artifact_sha256")
    if (not isinstance(approval_identity, str) or len(approval_identity) != 64 or
            record.get("artifact_short_id") != approval_identity[:12] or
            _storyboard_approval_artifact_sha256(record) != approval_identity):
        # Storyboard approval covers the whole decision record, not merely a
        # 12-character display id or the current panel bytes.  Any mutation to
        # semantics, execution bindings, customer wording or timestamps must
        # require a fresh approval.
        return False
    expected = {
        "client": client if client is not None else results.get("client"),
        "run_id": run_id if run_id is not None else results.get("run_id"),
        "out_dir": os.path.abspath(out_dir) if out_dir is not None else actual_out_dir,
        "plan_fingerprint": (plan_fingerprint_value if plan_fingerprint_value is not None
                             else results.get("plan_fingerprint")),
    }
    # The aggregate plan identity may change for dialogue/audio-only edits. In
    # that case panel approvals remain sound when every approved image is bound
    # to the current panel visual fingerprint and unchanged bytes.
    identity_keys = ("client", "run_id", "out_dir")
    if any(record.get(key) != expected[key] for key in identity_keys):
        return False
    try:
        current = _storyboard_approval_record(results, result_json)
    except (OSError, br_client.BRError):
        return False
    if record.get("shots") != current.get("shots"):
        return False
    if (record.get("kind") == "design_board" and
            record.get("execution_artifacts") != current.get("execution_artifacts")):
        return False
    expected_plan = expected["plan_fingerprint"]
    if record.get("plan_fingerprint") == expected_plan:
        return True
    return bool(record.get("shots")) and all(
        item.get("shot_fingerprint") for item in record.get("shots")
    )


class _StoryboardCheckpoint:
    """Own resumable storyboard state and its observer notifications."""

    def __init__(self, results, result_path, *, requested_base_out_dir,
                 run_id, plan, out_dir, plan_fingerprint_value,
                 visual_plan_fingerprint_value):
        self.results = results
        self.result_path = result_path
        self.requested_base_out_dir = requested_base_out_dir
        self.run_id = run_id
        self.plan = plan
        self.out_dir = out_dir
        self.plan_fingerprint = plan_fingerprint_value
        self.visual_plan_fingerprint = visual_plan_fingerprint_value
        self._lock = threading.Lock()

    def save(self):
        with self._lock:
            atomic_write_json(self.result_path, self.results)

    def _write_pointer(self, stage):
        pointer = _write_run_pointer(
            self.requested_base_out_dir, self.run_id, self.plan,
            self.out_dir, self.result_path, stage=stage,
            plan_fingerprint_value=self.plan_fingerprint,
            visual_plan_fingerprint_value=self.visual_plan_fingerprint)
        if pointer:
            self.results["current_run_pointer"] = os.path.abspath(pointer)

    def finish(self, stage_name=None, needs_confirmation=False):
        self.results["stage"] = stage_name or "storyboard"
        self.results["needs_confirmation"] = bool(needs_confirmation)
        self.results["result_json"] = os.path.abspath(self.result_path)
        manifest_stage = _manifest_stage_name(self.results["stage"])
        if manifest_stage:
            _begin_manifest_generation(self.results, manifest_stage, force=False)
        self._write_pointer(self.results["stage"])
        index_md, embedded_md, preview_html = write_index(
            self.plan, self.results, self.out_dir)
        self.results["index_md"] = index_md
        self.results["embedded_md"] = embedded_md
        self.results["preview_html"] = preview_html
        self.save()
        _register_storyboard_result_artifacts(self.results, self.result_path)
        _refresh_workflow_canvas(
            self.results, self.result_path,
            reason="storyboard:%s" % self.results["stage"])
        return self.results

    def progress_callback(self, kind, asset_id, out_path):
        def update(event):
            current = {
                "kind": kind,
                "id": asset_id,
                "out_path": os.path.abspath(out_path),
            }
            current.update(event)
            self.results["in_progress"] = current
            self.results["stage"] = {
                "product_board": "product_board",
                "cast_board": "cast_board",
                "product_usage_image": "product_usage_image",
                "storyboard": "storyboard",
            }.get(kind, self.results.get("stage") or "storyboard")
            if (kind == "product_usage_image" and
                    str(asset_id).startswith("usage_panel_")):
                panels = self.results.setdefault("in_progress_panels", {})
                panels[str(asset_id)] = current
            self.save()
            manifest_path = self.results.get("manifest_path")
            manifest_stage = {
                "product_board": "product_board",
                "cast_board": "cast_board",
                "product_usage_image": "product_usage",
                "storyboard": "storyboard",
            }.get(kind)
            if manifest_path and manifest_stage:
                run_manifest_store.heartbeat_generation_lease(
                    os.path.abspath(manifest_path), manifest_stage,
                    _generation_owner_token(), ttl_seconds=300)
            self._write_pointer(self.results["stage"])
            if event.get("status") in ("submitted", "success", "failed"):
                print("[storyboard] %s %s: %s" %
                      (kind, asset_id, event.get("status")), flush=True)
            elif event.get("waited", 0) and event["waited"] % 30 == 0:
                print("[storyboard] %s %s still processing (%ss)" %
                      (kind, asset_id, event["waited"]), flush=True)
        return update

    def previous_task(self, kind, asset_id):
        state = self.results.get("in_progress") or {}
        if state.get("kind") == kind and state.get("id") == asset_id:
            return state.get("task_id")
        return None


def render_storyboard(plan_path, out_dir, model=DEFAULT_MODEL, run_id=None, flat=False, bw=True,
                      stage="next", debug_allow_all=False, prompt_review=None,
                      only_shot_ids=None, force_boards=None, force_shot_ids=None):
    """Compatibility façade for the isolated storyboard stage orchestrator."""
    import storyboard_orchestrator
    return storyboard_orchestrator.render_storyboard(
        plan_path, out_dir, model=model, run_id=run_id, flat=flat, bw=bw,
        stage=stage, debug_allow_all=debug_allow_all,
        prompt_review=prompt_review, only_shot_ids=only_shot_ids,
        force_boards=force_boards, force_shot_ids=force_shot_ids)


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate storyboard/cast-board images from a finalized script plan using gpt-image-2")
    p.add_argument("--plan", help="storyboard_plan.json")
    p.add_argument("--out-dir", default="output/storyboard", help="base output directory; by default each run creates a timestamped subdirectory here")
    p.add_argument("--run-id", default=None, help="optional stable session id, e.g. acme-v1 or 20260723-acme-approved-script")
    p.add_argument("--flat", action="store_true", help="write directly into --out-dir; legacy/debug mode, may overwrite previous previews")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--color", action="store_true",
                   help="生成彩色故事板；默认生成黑白故事板（可被 plan/shot 的 color_mode 覆盖）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--prompt-review", help="已确认的中文故事板提示词审核文件")
    p.add_argument("--only-shot", action="append", default=None,
                   help="只重生指定 shot id；仅可配合 --stage storyboard，用于修复单个过期分镜，避免重生全部故事板。")
    p.add_argument("--force-shot", action="append", default=None,
                   help="强制重新提交指定 shot id 并保留其它 shot；用于修复缺 BasicRouter retrieve URL 或客户只改某一格。")
    p.add_argument("--force-board", action="append", choices=["cast", "usage"], default=None,
                   help="显式重生已存在的人物板/产品使用图，用于旧图缺少 BasicRouter retrieve URL 的修复；会备份旧图并清除旧确认。")
    p.add_argument("--stage", choices=["next", "product", "cast", "usage", "storyboard", "all"], default="next",
                    help="默认 next：一次只推进一个待确认阶段；all 为旧参数且始终被安全阻止")
    p.add_argument("--debug-allow-all", action="store_true",
                   help="已弃用的兼容参数；不会绕过逐阶段确认闸门")
    p.add_argument("--confirm-board", choices=["product", "cast", "usage"],
                    help="确认已展示的产品板/人物板/产品使用图，必须同时传 --result-json")
    p.add_argument("--geometry-reviewed", action="store_true",
                   help="旧参数，仅保留兼容；有物理关系合同的产品使用图必须改用 --geometry-review。")
    p.add_argument("--geometry-review",
                   help="配合 --confirm-board usage 使用：客户确认后的 usage_geometry_review.json，需绑定当前图哈希、源指纹和关键接触格复核。")
    p.add_argument("--refine-board", choices=["product", "cast", "usage"],
                    help="按客户反馈精修已展示的产品板/人物板/产品使用图，结果仍为 pending")
    p.add_argument("--edit", help="配合 --refine-board 使用：客户要求修改的具体内容")
    p.add_argument("--feedback-ref", nargs="+", default=None,
                   help="配合 --refine-board 使用：客户上传的反馈参考图，可多张")
    p.add_argument("--result-json", help="要确认的 storyboard_result.json")
    p.add_argument("--confirm", action="store_true",
                   help="整体确认全部分段故事板，必须同时传 --result-json")
    p.add_argument("--customer-confirmation",
                   help="客户明确确认原话；--confirm 或 --confirm-board 必填，写入审批证据。")
    args = p.parse_args(argv)
    try:
        if args.confirm:
            if not args.result_json:
                raise br_client.BRError("--confirm 必须配合 --result-json")
            res = confirm_storyboard(
                args.result_json, customer_confirmation=args.customer_confirmation)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.confirm_board:
            if not args.result_json:
                raise br_client.BRError("--confirm-board 必须配合 --result-json")
            res = confirm_board(args.result_json, args.confirm_board,
                                geometry_reviewed=args.geometry_reviewed,
                                geometry_review_path=args.geometry_review,
                                customer_confirmation=args.customer_confirmation)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.refine_board:
            if not args.result_json:
                raise br_client.BRError("--refine-board 必须配合 --result-json")
            res = refine_board(args.result_json, args.refine_board, args.edit,
                               feedback_refs=args.feedback_ref, model=args.model)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if not args.plan:
            raise br_client.BRError("生成故事板必须提供 --plan")
        res = render_storyboard(args.plan, args.out_dir, model=args.model, run_id=args.run_id,
                                flat=args.flat, bw=not args.color, stage=args.stage,
                                debug_allow_all=args.debug_allow_all,
                                prompt_review=args.prompt_review,
                                only_shot_ids=args.only_shot,
                                force_boards=args.force_board,
                                force_shot_ids=args.force_shot)
    except (OSError, ValueError, TypeError, RuntimeError, KeyError,
            json.JSONDecodeError, br_client.BRError) as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e),
                              "user_message": ux.friendly_error(e)}, ensure_ascii=False))
        else:
            print("ERROR: %s\n下一步：%s" % (e, ux.friendly_error(e)))
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("[done] storyboard index: %s" % res["index_md"])
        print("[done] result json: %s" % res["result_json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
