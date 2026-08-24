#!/usr/bin/env python3
"""video-use style bridge for explainer editing.

This adapter brings the useful editing contract from browser-use/video-use into
this project without making the public business flow depend on one host Agent:

  - keep a per-run edit memory (`project.md`)
  - pack the approved director script into a compact transcript view
    (`takes_packed.md`)
  - compile a lightweight EDL (`edl.json`)
  - fuse the two explainer branches:
      HyperFrames/Remotion document animation + digital-human presentation

It does not run a local ML model and does not replace storyboard/video
confirmation gates. It is a postproduction bridge for the explainer type.
Pure oral-broadcast runs belong on the horizontal kinetic talk path, not here.

CLI:
  python3 scripts/video_use_bridge.py plan --director-plan output/storyboard_plan.json
  python3 scripts/video_use_bridge.py render --draft --plan output/acme/run/edit/fusion_plan.json \
      --animation output/doc_anim.mp4 --human output/host.mp4 --out output/explainer.mp4
"""
import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fuse  # noqa: E402
from aspect_ratio import output_ratio  # noqa: E402
import hf_engine  # noqa: E402
import design_inputs  # noqa: E402
import design_token_contract  # noqa: E402
import render_spec_compiler  # noqa: E402
import content_scaffold  # noqa: E402
import media_qc  # noqa: E402
import run_manifest as run_manifest_store  # noqa: E402
from agent_runtime import detect_agent_runtime  # noqa: E402
from project_utils import atomic_write_json  # noqa: E402

CONTENT_SPEC_COMPILER_VERSION = "video-use-bridge-content-contract-v3"


LAYOUT_ALIASES = {
    "split": "side_by_side",
    "side-by-side": "side_by_side",
    "side_by_side": "side_by_side",
    "horizontal": "side_by_side",
    "left_right": "side_by_side",
    "stacked": "stacked",
    "vertical": "stacked",
    "top_bottom": "stacked",
    "overlay": "overlay",
    "pip": "overlay",
    "picture_in_picture": "overlay",
}

VISUAL_MODE_ALIASES = {
    # Director plans use these canonical names directly. Keep them in the
    # alias table so normalization does not silently fall back to split view.
    "host_full_bleed_text": "host_full_bleed_text",
    "content_full_bleed": "content_full_bleed",
    "split_left_right": "split_left_right",
    "stacked_top_bottom": "stacked_top_bottom",
    "corner_pip": "corner_pip",
    "cutout_presenter": "cutout_presenter",
    "hero": "host_full_bleed_text",
    "title": "host_full_bleed_text",
    "title_reveal": "host_full_bleed_text",
    "background_human": "host_full_bleed_text",
    "host_full_bleed": "host_full_bleed_text",
    "full_bleed_host": "host_full_bleed_text",
    "full_bleed_text": "host_full_bleed_text",
    "full_bleed": "content_full_bleed",
    "content_full_bleed": "content_full_bleed",
    "split": "split_left_right",
    "side_by_side": "split_left_right",
    "side-by-side": "split_left_right",
    "left_right": "split_left_right",
    "horizontal": "split_left_right",
    "左右分栏": "split_left_right",
    "stacked": "stacked_top_bottom",
    "top_bottom": "stacked_top_bottom",
    "vertical": "stacked_top_bottom",
    "上下分栏": "stacked_top_bottom",
    "overlay": "corner_pip",
    "pip": "corner_pip",
    "corner": "corner_pip",
    "picture_in_picture": "corner_pip",
    "cutout": "cutout_presenter",
    "foreground": "cutout_presenter",
    "presenter_cutout": "cutout_presenter",
    "人物抠图": "cutout_presenter",
}


def _output_dimensions(plan, *, default=(1920, 1080)):
    """Resolve final render dimensions; storyboard/design-board stays separate."""
    width = plan.get("width")
    height = plan.get("height")
    resolution = plan.get("resolution")
    if ((width is None or height is None) and
            isinstance(resolution, (list, tuple)) and len(resolution) == 2):
        width = resolution[0] if width is None else width
        height = resolution[1] if height is None else height
    try:
        explicit = (int(width), int(height))
        if explicit[0] > 0 and explicit[1] > 0:
            return explicit
    except (TypeError, ValueError):
        pass
    ratio = str(plan.get("output_ratio") or plan.get("video_ratio") or
                plan.get("aspect_ratio") or "16:9").lower().replace(" ", "")
    if ratio in {"9:16", "portrait", "vertical"}:
        return 1080, 1920
    if ratio in {"1:1", "square"}:
        return 1080, 1080
    return default


def _display_aspect_ratio(width, height):
    """Return a human-readable reduced ratio for media metadata."""
    if not width or not height:
        return None
    divisor = math.gcd(int(width), int(height))
    return "%d:%d" % (int(width) // divisor, int(height) // divisor)


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_json(path, value):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, value)
    return path


def _sha256_file(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


APPROVED_SPEC_MUTABLE_MEDIA_FIELDS = frozenset({
    "humanVideoPath", "humanImagePath", "contentVideoPath", "contentHasAlpha",
    "motionVideoPath", "motionHasAlpha", "previewAudioPath",
    "humanSourceDimensions", "humanSourceAspectRatio",
})


def _approved_spec_fingerprint(spec):
    """Fingerprint every approved render decision except replaceable media slots."""
    if not isinstance(spec, dict):
        raise ValueError("APPROVED_PREVIS_SPEC_INVALID")
    frozen = {
        key: value for key, value in spec.items()
        if key not in APPROVED_SPEC_MUTABLE_MEDIA_FIELDS
    }
    import hashlib
    payload = json.dumps(
        frozen, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_approved_previs_spec(value):
    """Load either an approved integrated spec or its execution/approval record."""
    if isinstance(value, dict):
        record = value
        source_path = None
    else:
        source_path = os.path.abspath(_text(value))
        if not source_path or not os.path.isfile(source_path):
            raise SystemExit("APPROVED_PREVIS_SPEC_MISSING")
        record = _load_json(source_path)
    spec_record = record.get("integrated_spec") if isinstance(record, dict) else None
    if not isinstance(spec_record, dict):
        execution = record.get("execution_artifacts") if isinstance(record, dict) else None
        spec_record = execution.get("integrated_previs_spec") if isinstance(execution, dict) else None
    if isinstance(spec_record, dict) and spec_record.get("path"):
        spec_path = os.path.abspath(spec_record["path"])
        expected_sha = spec_record.get("sha256")
        if not os.path.isfile(spec_path):
            raise SystemExit("APPROVED_PREVIS_SPEC_MISSING")
        if expected_sha and _sha256_file(spec_path) != expected_sha:
            raise SystemExit("APPROVED_PREVIS_SPEC_STALE")
        spec = _load_json(spec_path)
        return spec, spec_path, record
    # A raw IntegratedExplainer props file is also accepted, but only if it
    # carries the execution-preview artifact contract written by this project.
    if isinstance(record, dict) and isinstance(record.get("scenes"), list):
        contract = record.get("_artifact_contract") or {}
        if contract.get("renderer_source_of_truth") != "remotion.IntegratedExplainer":
            raise SystemExit("APPROVED_PREVIS_SPEC_UNTRUSTED")
        return record, source_path, None
    raise SystemExit("APPROVED_PREVIS_SPEC_INVALID")


def _require_customer_approved_previs(value):
    """Fail before any local render if a formal approval record is absent."""
    if isinstance(value, dict):
        raise SystemExit(
            "CUSTOMER_APPROVED_PREVIS_FILE_REQUIRED: 正式合成必须传入当前审批 JSON 文件，"
            "不能用内存 status 代替客户证据。")
    else:
        approval_path = os.path.abspath(_text(value))
        if not approval_path or not os.path.isfile(approval_path):
            raise SystemExit("APPROVED_PREVIS_SPEC_MISSING")
        record = _load_json(approval_path)
    artifacts = record.get("execution_artifacts") if isinstance(record, dict) else {}
    commercial = (artifacts or {}).get("commercial_render_contract") or {}
    if not (isinstance(record, dict) and record.get("status") == "confirmed" and
            record.get("kind") == "design_board" and
            record.get("confirmed_by_customer") is True and
            str(record.get("customer_confirmation") or "").strip() and
            record.get("confirmation_source") == "customer_dialogue" and
            record.get("confirmed_at") and
            commercial.get("formal_render_must_bind_approved_spec") is True):
        raise SystemExit(
            "CUSTOMER_APPROVED_PREVIS_REQUIRED: 正式合成只能绑定客户已确认的 design_board "
            "v2 审批记录；execution preview 本身不代表客户同意。")
    # Only after authorization is proven do we resolve and hash the bound
    # spec. This keeps fail-fast authorization separate from artifact health.
    _load_approved_previs_spec(value)
    return record


def bind_approved_previs_spec(approved, *, content_video, human_video,
                              motion_video=None, content_has_alpha=True,
                              motion_has_alpha=None):
    """Reuse the approved spec verbatim and replace only formal media slots."""
    source, source_path, record = _load_approved_previs_spec(approved)
    bound = json.loads(json.dumps(source, ensure_ascii=False))
    before = _approved_spec_fingerprint(source)
    human_width, human_height = _media_dimensions(human_video)
    bound.update({
        "humanVideoPath": os.path.abspath(human_video),
        "humanImagePath": None,
        "contentVideoPath": os.path.abspath(content_video),
        "contentHasAlpha": bool(content_has_alpha),
        "motionVideoPath": os.path.abspath(motion_video) if motion_video else None,
        "motionHasAlpha": bool(motion_has_alpha) if motion_video else None,
        "previewAudioPath": None,
        "humanSourceDimensions": [human_width, human_height],
        "humanSourceAspectRatio": _display_aspect_ratio(human_width, human_height),
    })
    after = _approved_spec_fingerprint(bound)
    if before != after:
        raise SystemExit("APPROVED_PREVIS_SPEC_MUTATED")
    commercial = ((record.get("execution_artifacts") or {}).get(
        "commercial_render_contract") if isinstance(record, dict) else {}) or {}
    execution_artifacts = (record.get("execution_artifacts")
                           if isinstance(record, dict) else {}) or {}
    approval_record_path = (None if isinstance(approved, dict)
                            else os.path.abspath(_text(approved)))
    approval_record_sha256 = (
        _sha256_file(approval_record_path)
        if approval_record_path and os.path.isfile(approval_record_path) else None)
    customer_approved = bool(
        isinstance(record, dict) and record.get("status") == "confirmed" and
        record.get("kind") == "design_board" and
        record.get("confirmed_by_customer") is True and
        str(record.get("customer_confirmation") or "").strip() and
        record.get("confirmation_source") == "customer_dialogue" and
        record.get("confirmed_at") and approval_record_sha256 and
        commercial.get("formal_render_must_bind_approved_spec") is True)
    if customer_approved:
        approved_motion = execution_artifacts.get("motion_layer") or {}
        hyperframes_required = bool(commercial.get("hyperframes_required"))
        if hyperframes_required and not motion_video:
            raise SystemExit("APPROVED_MOTION_LAYER_REQUIRED")
        if motion_video and approved_motion.get("sha256"):
            if _sha256_file(motion_video) != approved_motion["sha256"]:
                raise SystemExit("APPROVED_MOTION_LAYER_MISMATCH")
    return bound, {
        "approved_spec_path": source_path,
        "approved_spec_sha256": _sha256_file(source_path) if source_path else None,
        "approved_frozen_fingerprint": before,
        "bound_frozen_fingerprint": after,
        "mutable_media_fields": sorted(APPROVED_SPEC_MUTABLE_MEDIA_FIELDS),
        "approval_record_type": record.get("type") if isinstance(record, dict) else None,
        "approval_record_path": approval_record_path,
        "approval_record_sha256": approval_record_sha256,
        "customer_approved": customer_approved,
        "confirmed_by_customer": (record.get("confirmed_by_customer")
                                  if isinstance(record, dict) else None),
        "customer_confirmation": (record.get("customer_confirmation")
                                  if isinstance(record, dict) else None),
        "confirmation_source": (record.get("confirmation_source")
                                if isinstance(record, dict) else None),
        "confirmed_at": record.get("confirmed_at") if isinstance(record, dict) else None,
        "approval_status": record.get("status") if isinstance(record, dict) else None,
        "approval_kind": record.get("kind") if isinstance(record, dict) else None,
        "approved_content_contract_spec": execution_artifacts.get("content_contract_spec"),
        "approved_motion_layer": execution_artifacts.get("motion_layer"),
    }


def _validate_approved_content_contract(fusion_plan, approval_binding):
    approved = (approval_binding or {}).get("approved_content_contract_spec") or {}
    expected_sha = approved.get("sha256")
    if not expected_sha:
        raise SystemExit("APPROVED_CONTENT_CONTRACT_REQUIRED")
    files = fusion_plan.get("files") if isinstance(fusion_plan.get("files"), dict) else {}
    candidates = [
        files.get("content_spec"), fusion_plan.get("content_spec_path"),
        fusion_plan.get("content_spec_source_path"),
    ]
    path = next((os.path.abspath(value) for value in candidates
                 if isinstance(value, str) and os.path.isfile(value)), None)
    if not path:
        raise SystemExit("FINAL_CONTENT_CONTRACT_MISSING")
    if _sha256_file(path) != expected_sha:
        raise SystemExit("FINAL_CONTENT_CONTRACT_DIVERGED")
    return path


def _existing_content_spec_path(plan, source_plan=None):
    candidates = []
    explicit = plan.get("content_spec_path") or plan.get("content_spec")
    if isinstance(explicit, str):
        candidates.append(explicit)
    if source_plan:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(source_plan)), "content_spec.json"))
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            data = _load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if (isinstance(data, dict) and isinstance(data.get("scenes"), list)
                and data["scenes"]):
            return os.path.abspath(path)
    return None


def _write_text(path, text, append=False):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "a" if append and os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
    return path


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _text(value, limit=None):
    if value is None:
        return ""
    result = str(value).strip()
    if limit and len(result) > limit:
        return result[:limit].rstrip() + "..."
    return result


def _ppt_slide_image(plan, raw):
    """Resolve the confirmed PPT page for a design-board shot.

    PPT pages are composition assets. They are intentionally resolved here,
    after the video-model reference split, so a page can never become an
    accidental avatar-plate background reference.
    """
    direct = raw.get("slide_image") or raw.get("content_image")
    candidates = [direct] if direct else []
    numbers = []
    for value in _as_list(raw.get("source_slides") or raw.get("source_pages") or raw.get("slide_range")):
        match = re.search(r"\d+", str(value))
        if match:
            numbers.append(int(match.group(0)))
    for tag in _as_list(raw.get("ref_tags")):
        match = re.search(r"(?:ppt_)?slide[_-]?(\d+)", str(tag), re.I)
        if match:
            numbers.append(int(match.group(1)))
    slide_dir = plan.get("slide_images_dir") or plan.get("ppt_slides_dir")
    page_assets = _as_list((plan.get("asset_refs") or {}).get("ppt_pages"))
    for asset in page_assets:
        match = re.search(r"slide[_-]?(\d+)\.(?:png|jpg|jpeg)$", str(asset), re.I)
        if match and int(match.group(1)) in numbers:
            candidates.append(asset)
    if not slide_dir and page_assets:
        slide_dir = os.path.dirname(os.path.abspath(str(page_assets[0])))
    if slide_dir:
        for number in list(dict.fromkeys(numbers)):
            candidates.extend([
                os.path.join(slide_dir, "slide-%d.png" % number),
                os.path.join(slide_dir, "slide_%d.png" % number),
                os.path.join(slide_dir, "%d.png" % number),
            ])
    for value in candidates:
        if not value:
            continue
        path = os.path.abspath(str(value))
        if os.path.isfile(path):
            return path
    return None


def _content_scene_kind(raw, slide_image):
    kind = _text(
        raw.get("content_component") or raw.get("component") or
        raw.get("content_scene") or raw.get("scene_kind"))
    allowed = {"hero", "section", "list", "features", "metrics", "table", "typewriter",
               "quote", "process", "evolution", "comparison", "causal", "product"}
    if slide_image and kind in allowed:
        return "slide_with_overlay"
    return kind if kind in allowed else ("slide" if slide_image else "section")


def _content_props(raw, slide_image, index):
    authored = raw.get("content_props")
    if isinstance(authored, dict):
        props = dict(authored)
    else:
        props = {}
    if slide_image:
        props["image"] = slide_image
        props.setdefault("motion", "ken_burns_focus")
        props.setdefault("accent", raw.get("accent") or raw.get("brand_accent"))
        props.setdefault("title", raw.get("content_title") or raw.get("title") or "")
        props.setdefault("highlights", _as_list(raw.get("highlights") or raw.get("highlight_regions")))
    elif not props:
        props = {"title": raw.get("content_title") or raw.get("title") or "内容页 %d" % index}
    return props


def _is_document_explainer_plan(plan):
    """Detect document-derived explainers without forcing a screenshot layout."""
    if not isinstance(plan, dict):
        return False
    if _is_oral_broadcast_plan(plan):
        return False
    explicit_role = str(
        plan.get("storyboard_role") or
        (plan.get("storyboard_contract") or {}).get("kind") or
        (plan.get("storyboard_contract") or {}).get("role") or
        (plan.get("storyboard_contract") or {}).get("usage") or
        (plan.get("composition_contract") or {}).get("design_board") or
        ""
    ).strip().lower().replace("-", "_")
    explicit_design_board = explicit_role == "design_board"
    markers = []
    for key in (
            "scene_type", "type", "kind", "input_kind", "source_type",
            "source_doc_type", "content_strategy", "title", "project_title"):
        markers.append(_text(plan.get(key), limit=1000))
    asset_refs = plan.get("asset_refs") if isinstance(plan.get("asset_refs"), dict) else {}
    if (plan.get("ppt_files") or plan.get("document_files") or plan.get("slides") or
            asset_refs.get("ppt_pages") or asset_refs.get("document_images")):
        markers.append("ppt document slides")
    if plan.get("_source_ppt") or plan.get("source_ppt") or plan.get("source_doc"):
        markers.append("ppt document")
    blob = " ".join(item for item in markers if item).lower()
    is_explainer = any(token in blob for token in (
        "explainer", "讲解", "内容讲解", "课程", "digital human presenter"))
    has_document = any(token in blob for token in (
        "ppt", "powerpoint", "deck", "slide", "slides", "pdf", "doc",
        "document", "文档", "内容页", "营销方案", "服务说明"))
    # An explicit design-board contract is authoritative.  This keeps the
    # document pipeline stable even when a generated plan has a terse title or
    # omits the older keyword-based scene markers.
    return explicit_design_board or (is_explainer and has_document)


def _is_dense_slide_preservation_plan(plan):
    """Detect PPT/page-preserving explainers where content must stay full frame."""
    if not _is_document_explainer_plan(plan):
        return False
    markers = []
    for key in (
            "content_strategy", "render_strategy", "visual_strategy",
            "source_strategy", "layout_note", "title", "project_title"):
        markers.append(_text(plan.get(key), limit=1000))
    if plan.get("slides") or plan.get("slide_images") or plan.get("ppt_files"):
        markers.append("slides")
    blob = " ".join(item for item in markers if item).lower()
    preserve_tokens = (
        "原页", "幻灯页截图", "截图", "保留原版", "主视觉",
        "dense ppt", "slide screenshot", "source slides", "page-preserving",
        "preserve slides", "raw slide", "original slide",
    )
    redesign_tokens = (
        "提炼为内容动效", "不逐页播放", "重新导演", "重构为",
        "redesign", "motion graphics", "not raw", "not slideshow",
    )
    return any(token in blob for token in preserve_tokens) and not any(
        token in blob for token in redesign_tokens)


def _is_oral_broadcast_plan(plan):
    if not isinstance(plan, dict):
        return False
    role = str(plan.get("storyboard_role") or "").strip().lower().replace("-", "_")
    # A document design board is the more specific route. Legacy runs may
    # retain scene_type=oral-broadcast from the entry skill, but PPT-backed
    # design_board plans must use the Remotion/HyperFrames explainer branch.
    if role == "design_board":
        return False
    explicit_markers = []
    for key in (
            "scene_type", "sceneType", "type", "kind", "input_kind",
            "source_type", "workflow", "skill", "command"):
        explicit_markers.append(_text(plan.get(key), limit=1000))
    explicit_blob = " ".join(item for item in explicit_markers if item).lower()
    oral_tokens = ("oral-broadcast", "oralbroadcast", "broadcast", "口播", "普通口播")
    if any(token in explicit_blob for token in oral_tokens):
        return True
    fallback_blob = " ".join(
        _text(plan.get(key), limit=1000) for key in ("content_strategy", "title", "project_title")
    ).lower()
    return not explicit_blob and any(token in fallback_blob for token in oral_tokens)


def _normalize_visual_mode(value):
    if not value:
        return None
    key = str(value).strip().lower().replace("-", "_")
    return VISUAL_MODE_ALIASES.get(key) or VISUAL_MODE_ALIASES.get(str(value).strip())


def _valid_hex_color(value):
    value = _text(value)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.upper()
    return None


def _brand_primary_from_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        value = _load_json(path)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    candidates = [value.get("brandPrimary"), value.get("brand_primary"), value.get("primary")]
    for container_key in ("brand", "colors", "render_profile", "renderProfile"):
        container = value.get(container_key)
        if isinstance(container, dict):
            candidates.extend([
                container.get("brandPrimary"), container.get("brand_primary"),
                container.get("primary"),
            ])
    for candidate in candidates:
        color = _valid_hex_color(candidate)
        if color:
            return color
    return None


def _brand_primary_for_plan(plan, source_plan=None):
    """Resolve the customer color without imposing a global template color."""
    tokens = design_token_contract.resolve(plan, source_plan=source_plan)
    token_primary = _valid_hex_color(design_token_contract.primary(tokens))
    if token_primary:
        return token_primary
    fusion = plan.get("fusion") if isinstance(plan.get("fusion"), dict) else {}
    explicit = [
        plan.get("brandPrimary"), plan.get("brand_primary"),
        fusion.get("brandPrimary"), fusion.get("brand_primary"),
    ]
    render_profile = plan.get("render_profile") if isinstance(plan.get("render_profile"), dict) else {}
    palette_hint = _text(render_profile.get("palette_hint"))
    explicit.extend([render_profile.get("brandPrimary"), render_profile.get("brand_primary")])
    palette_match = re.search(r"#[0-9a-fA-F]{6}", palette_hint)
    if palette_match:
        explicit.append(palette_match.group(0))
    for candidate in explicit:
        color = _valid_hex_color(candidate)
        if color:
            return color

    if source_plan:
        source_dir = os.path.dirname(os.path.abspath(source_plan))
        for filename in ("content_spec.json", "brand.json", "brief.json"):
            color = _brand_primary_from_file(os.path.join(source_dir, filename))
            if color:
                return color
    return "#64748B"


def _infer_visual_mode(index, raw, *, document_explainer=False, is_last=False):
    explicit = (
        raw.get("visual_mode") or raw.get("layout_mode") or raw.get("shot_layout") or
        raw.get("presentation_mode") or raw.get("fusion_mode") or raw.get("human_layout")
    )
    normalized = _normalize_visual_mode(explicit)
    if normalized:
        return normalized

    hint = " ".join(_text(raw.get(key), limit=400) for key in (
        "animation_hint", "content_design", "content", "motion_overlay", "visual",
        "scene_prompt", "title", "dialogue", "text") if raw.get(key)).lower()
    if index == 1 or any(token in hint for token in (
            "hero", "开场", "标题", "title", "hook", "背景主体", "透明动效")):
        return "host_full_bleed_text"
    if any(token in hint for token in ("对比", "比较", "vs", "contrast")):
        return "split_left_right"
    if any(token in hint for token in ("打字机", "定位", "关键词流动", "标题流动", "字幕流动")):
        return "content_full_bleed"
    if any(token in hint for token in ("流程", "process", "四步", "步骤", "timeline")):
        return "stacked_top_bottom"
    if any(token in hint for token in ("架构", "结构", "体系", "graph", "causal", "flow")):
        return "content_full_bleed" if document_explainer else "split_left_right"
    if any(token in hint for token in ("亮点", "结果", "获得", "proof", "cta", "收束")):
        return "cutout_presenter"
    if document_explainer:
        if is_last and index > 1:
            return "cutout_presenter"
        if any(token in hint for token in (
                "卡片", "清单", "列表", "矩阵", "能力", "模块", "服务", "客户",
                "页", "slide", "card", "matrix", "module", "service")):
            return "content_full_bleed"
        # Document explainers should feel like a designed presentation system,
        # not a default two-video split. Keep split for explicit contrast only.
        return ("content_full_bleed", "corner_pip", "stacked_top_bottom", "content_full_bleed")[
            (index - 2) % 4]
    return ("split_left_right", "stacked_top_bottom", "corner_pip")[(index - 1) % 3]


def _shot_layout_hint(visual_mode):
    hints = {
        "host_full_bleed_text": {
            "kind": "host_background",
            "note": "数字人或场景作主体背景，HyperFrames 叠透明文字/关键词动效。",
        },
        "content_full_bleed": {
            "kind": "content_background",
            "note": "内容动效铺满主画面，讲师只作为短时点缀或画外音。",
        },
        "split_left_right": {
            "kind": "split",
            "layout": "side_by_side",
            "note": "左右分栏：内容承担解释，数字人承担可信讲解。",
        },
        "stacked_top_bottom": {
            "kind": "split",
            "layout": "stacked",
            "note": "上下分栏：适合流程、时间线或横向架构拆解。",
        },
        "corner_pip": {
            "kind": "overlay",
            "slot": "corner",
            "note": "角落画中画：只用于信息密集或需要完整展示内容图谱的段落。",
        },
        "cutout_presenter": {
            "kind": "foreground_presenter",
            "note": "人物前景式呈现；抠图/人景融合必须走外部模型或已确认透明素材，本地不跑抠像模型。",
        },
    }
    return hints.get(visual_mode, hints["split_left_right"])


def _integrated_layout_regions(visual_mode, human_slot=None):
    """Executable normalized regions shared by Content and Integrated renderers."""
    if visual_mode == "stacked_top_bottom":
        return {
            "content": {"x": 0.06, "y": 0.04, "w": 0.88, "h": 0.38},
            "subtitle": {"x": 0.20, "y": 0.44, "w": 0.60, "h": 0.12},
            "presenter": {"x": 0.31, "y": 0.59, "w": 0.38, "h": 0.37},
        }
    if visual_mode == "split_left_right":
        presenter_left = str(human_slot or "").lower() == "left"
        return {
            "content": {"x": 0.48 if presenter_left else 0.04,
                        "y": 0.06, "w": 0.48, "h": 0.72},
            "subtitle": {"x": 0.20, "y": 0.82, "w": 0.60, "h": 0.12},
            "presenter": {"x": 0.04 if presenter_left else 0.56,
                          "y": 0.07, "w": 0.40, "h": 0.70},
        }
    return {
        "content": {"x": 0.06, "y": 0.06, "w": 0.88, "h": 0.72},
        "subtitle": {"x": 0.18, "y": 0.82, "w": 0.64, "h": 0.12},
        "presenter": {"x": 0.55, "y": 0.08, "w": 0.40, "h": 0.70},
    }


def _ratio_from_plan(plan):
    raw = _text(
        plan.get("output_ratio") or plan.get("ratio") or
        plan.get("video_aspect_ratio") or plan.get("target_ratio") or
        plan.get("aspect_ratio"))
    if raw:
        try:
            return output_ratio({"output_ratio": raw})
        except (ValueError, TypeError):
            return raw
    width = plan.get("width") or ((plan.get("resolution") or [None, None])[0]
                                  if isinstance(plan.get("resolution"), list) else None)
    height = plan.get("height") or ((plan.get("resolution") or [None, None])[1]
                                    if isinstance(plan.get("resolution"), list) else None)
    try:
        return "16:9" if float(width) >= float(height) else "9:16"
    except (TypeError, ValueError):
        return "16:9"


def _default_edit_dir(plan):
    client = _text(plan.get("client") or "unknown")
    run_id = _text(plan.get("run_id") or plan.get("id") or "explainer")
    client = re.sub(r"[^0-9A-Za-z_-]+", "_", client).strip("_") or "unknown"
    run_id = re.sub(r"[^0-9A-Za-z_-]+", "_", run_id).strip("_") or "explainer"
    return os.path.join(ROOT, "output", client, run_id, "edit")


def _normalized_layout(plan, override=None, split_ratio=None, gap=None, swap=None):
    fusion = plan.get("fusion") if isinstance(plan.get("fusion"), dict) else {}
    raw = override or fusion.get("layout") or plan.get("layout")
    dense_slide_preservation = _is_dense_slide_preservation_plan(plan)
    if raw:
        key = LAYOUT_ALIASES.get(str(raw).strip().lower().replace("-", "_"))
        if not key:
            raise ValueError("VIDEO_USE_LAYOUT_UNSUPPORTED: %s" % raw)
    else:
        if dense_slide_preservation:
            key = "overlay"
        else:
            # Redesigned document explainers are motion-design-first. Do not
            # auto-lock them to a tiny corner PIP unless the director plan says
            # the original slide/page must remain the main visual.
            ratio = _ratio_from_plan(plan)
            key = "stacked" if ratio.startswith("9:16") else "side_by_side"

    mix_audio = fusion.get("mix_audio", True)
    if key == "overlay":
        default_scale = 0.5 if dense_slide_preservation else 0.9
        return {
            "kind": "overlay",
            "slot": fusion.get("slot") or "corner",
            "scale": float(fusion.get("scale", default_scale)),
            "mix_audio": bool(mix_audio),
            "swap": bool(swap) if swap is not None else bool(fusion.get("swap", False)),
        }

    inferred_ratio = 0.62 if key == "stacked" else 0.58
    value = split_ratio if split_ratio is not None else fusion.get("ratio", inferred_ratio)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("VIDEO_USE_SPLIT_RATIO_INVALID")
    if not 0.2 <= value <= 0.8:
        raise ValueError("VIDEO_USE_SPLIT_RATIO_INVALID: must be 0.2-0.8")
    return {
        "kind": "split",
        "layout": key,
        "ratio": value,
        "gap": int(gap if gap is not None else fusion.get("gap", 24)),
        "mix_audio": bool(mix_audio),
        "swap": bool(swap) if swap is not None else bool(fusion.get("swap", False)),
    }


def _ffmpeg():
    fuse.ensure_ffmpeg_on_path()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("VIDEO_USE_FFMPEG_MISSING")
    return ffmpeg


def _ffprobe():
    fuse.ensure_ffmpeg_on_path()
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SystemExit("VIDEO_USE_FFPROBE_MISSING")
    return ffprobe


def _run_ffmpeg(cmd, label):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise SystemExit("%s 失败:\n%s" % (
            label, result.stdout.decode("utf-8", "replace")[-1800:]))
    return result


def _atomic_video_path(out_path):
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(out_path),
        suffix=os.path.splitext(out_path)[1] or ".mp4",
        dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(temporary)
    return out_path, temporary


def _finish_video(temporary, out_path, label):
    if not fuse._valid_video(temporary):
        if os.path.exists(temporary):
            os.remove(temporary)
        raise SystemExit("%s 未生成有效视频: %s" % (label, out_path))
    os.replace(temporary, out_path)
    return out_path


def _media_dimensions(video):
    fp = _ffprobe()
    result = subprocess.check_output([
        fp, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", video,
    ]).decode("utf-8")
    stream = json.loads(result)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _media_duration(video):
    # The bridge can be called directly from a clean shell. Ensure the same
    # bundled ffmpeg/ffprobe runtime used by fuse is available before probing.
    fuse.ensure_ffmpeg_on_path()
    return fuse._probe_duration(video)


def _validate_human_timeline(fusion_plan, human_video):
    """Reject a single short take being used as a multi-shot presenter timeline."""
    edl = fusion_plan.get("edl") if isinstance(fusion_plan, dict) else {}
    shots = edl.get("shots") if isinstance(edl, dict) else []
    if not isinstance(shots, list) or len(shots) <= 1:
        return
    expected = float(edl.get("total_duration") or 0)
    if expected <= 0 or not os.path.isfile(human_video):
        return
    actual = float(_media_duration(human_video))
    # Allow a small encoder/container rounding difference, but never let a
    # 5-7 second segment be stretched/cut as the presenter track for a 30-60s edit.
    if actual + 0.6 < expected:
        raise SystemExit(
            "VIDEO_USE_HUMAN_TIMELINE_REQUIRED: 数字人素材只有 %.2fs，但 EDL 需要 %.2fs；"
            "请先按全部 segments 生成并拼接完整数字人时间轴，不能重复使用第一段素材。"
            % (actual, expected))


def _cut_video(video, start_sec, duration, out_path):
    ff = _ffmpeg()
    out_path, temporary = _atomic_video_path(out_path)
    cmd = [
        ff, "-hide_banner", "-y",
        "-ss", "%.3f" % float(start_sec),
        "-i", video,
        "-t", "%.3f" % float(duration),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]
    if fuse._has_audio(video):
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
    else:
        cmd.append("-an")
    cmd.append(temporary)
    _run_ffmpeg(cmd, "ffmpeg cut")
    return _finish_video(temporary, out_path, "ffmpeg cut")


def _prepare_human_focus(human_video, out_path, focus="center_speaker"):
    focus = (focus or "none").strip().lower().replace("-", "_")
    if focus in ("none", "off", "false", "0"):
        return human_video
    width, height = _media_dimensions(human_video)
    if focus in ("center", "center_speaker", "single", "single_speaker"):
        crop_w = min(width, int(round(height * 0.80)))
        # Bias slightly to the right: generated presenter takes sometimes open
        # with two faces side by side, and the speaking identity is usually the
        # right/center-right figure. On normal centered takes this remains a
        # clean medium close-up.
        crop_x = int(round((width - crop_w) * 0.68))
    elif focus in ("right", "right_speaker"):
        crop_w = min(width, int(round(height * 0.95)))
        crop_x = width - crop_w
    elif focus in ("left", "left_speaker"):
        crop_w = min(width, int(round(height * 0.95)))
        crop_x = 0
    else:
        raise ValueError("VIDEO_USE_HUMAN_FOCUS_UNSUPPORTED: %s" % focus)
    crop_x = max(0, min(crop_x, width - crop_w))
    ff = _ffmpeg()
    out_path, temporary = _atomic_video_path(out_path)
    vf = "crop=%d:%d:%d:0,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1" % (
        crop_w, height, crop_x, width, height, width, height)
    cmd = [
        ff, "-hide_banner", "-y", "-i", human_video,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]
    if fuse._has_audio(human_video):
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
    else:
        cmd.append("-an")
    cmd.append(temporary)
    _run_ffmpeg(cmd, "ffmpeg human focus")
    return _finish_video(temporary, out_path, "ffmpeg human focus")


def _shot_render_recipe(shot, index):
    mode = _normalize_visual_mode(shot.get("visual_mode")) or shot.get("visual_mode")
    mode = mode or "split_left_right"
    if mode == "host_full_bleed_text":
        return {
            "kind": "host_full_bleed_text",
            "description": "数字人作主体背景，内容动效半透明叠加",
        }
    if mode == "content_full_bleed":
        return {
            "kind": "overlay",
            "slot": "corner",
            "scale": 0.62,
            "description": "内容动效铺满，数字人角窗点题",
        }
    if mode == "stacked_top_bottom":
        return {
            "kind": "split",
            "layout": "stacked",
            "ratio": 0.66,
            "gap": 18,
            "swap": False,
            "description": "上下分栏，内容在上、讲师在下",
        }
    if mode == "corner_pip":
        return {
            "kind": "overlay",
            "slot": "corner",
            "scale": 0.72,
            "description": "内容全屏，数字人角落画中画",
        }
    if mode == "cutout_presenter":
        return {
            "kind": "overlay",
            "slot": "right",
            "scale": 0.88,
            "description": "内容全屏，数字人以前景大窗进入；真抠图留给外部模型",
        }
    return {
        "kind": "split",
        "layout": "side_by_side",
        "ratio": 0.64 if index % 2 == 0 else 0.58,
        "gap": 18,
        "swap": bool(index % 4 == 0),
        "description": "左右分栏并交替主体位置",
    }


def _audio_mix_filter(first, second, duration_mode="shortest"):
    first_audio = fuse._has_audio(first)
    second_audio = fuse._has_audio(second)
    if first_audio and second_audio:
        return {
            "filter": ";[0:a][1:a]amix=inputs=2:duration=%s:dropout_transition=2[a]" % duration_mode,
            "map": ["-map", "[v]", "-map", "[a]"],
        }
    if second_audio:
        return {"filter": "", "map": ["-map", "[v]", "-map", "1:a"]}
    if first_audio:
        return {"filter": "", "map": ["-map", "[v]", "-map", "0:a"]}
    return {"filter": "", "map": ["-map", "[v]", "-an"]}


def _compose_host_full_bleed_text(content_video, human_video, out_path):
    ff = _ffmpeg()
    width, height = _media_dimensions(content_video)
    panel_w = int(width * 0.55)
    panel_h = int(round(panel_w * 9 / 16))
    panel_x = 0
    panel_y = int((height - panel_h) / 2)
    cover_w = int(width * 0.14)
    out_path, temporary = _atomic_video_path(out_path)
    video_filter = (
        "[1:v]scale=%d:%d:force_original_aspect_ratio=increase,"
        "crop=%d:%d,eq=brightness=-0.13:saturation=0.9,"
        "drawbox=x=0:y=0:w=%d:h=%d:color=0x050B13@1:t=fill[host];"
        "[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=0x050B13,"
        "format=rgba,colorchannelmixer=aa=1.0[content];"
        "[host][content]overlay=%d:%d:format=auto:eof_action=pass:shortest=0[v]"
        % (width, height, width, height,
           cover_w, height,
           panel_w, panel_h, panel_w, panel_h,
           panel_x, panel_y)
    )
    audio = _audio_mix_filter(content_video, human_video, duration_mode="shortest")
    cmd = [
        ff, "-hide_banner", "-y", "-i", content_video, "-i", human_video,
        "-filter_complex", video_filter + audio["filter"],
    ] + audio["map"] + [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", temporary,
    ]
    _run_ffmpeg(cmd, "ffmpeg host_full_bleed_text")
    return _finish_video(temporary, out_path, "ffmpeg host_full_bleed_text")


def _concat_segments(segment_paths, out_path):
    ff = _ffmpeg()
    out_path, temporary = _atomic_video_path(out_path)
    if not segment_paths:
        raise SystemExit("VIDEO_USE_CONCAT_EMPTY")
    cmd = [ff, "-hide_banner", "-y"]
    for path in segment_paths:
        cmd.extend(["-i", path])
    filters = []
    inputs = []
    for index, path in enumerate(segment_paths):
        filters.append(
            "[%d:v]fps=30,scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,setsar=1,setpts=PTS-STARTPTS[v%d]" % (index, index))
        if fuse._has_audio(path):
            filters.append("[%d:a]aresample=48000,asetpts=PTS-STARTPTS[a%d]" % (index, index))
        else:
            duration = max(0.1, _media_duration(path))
            filters.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                "atrim=0:%.3f,asetpts=PTS-STARTPTS[a%d]" % (duration, index))
        inputs.append("[v%d][a%d]" % (index, index))
    filters.append("%sconcat=n=%d:v=1:a=1[v][a]" % ("".join(inputs), len(segment_paths)))
    cmd = [
        *cmd,
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]",
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", temporary,
    ]
    _run_ffmpeg(cmd, "ffmpeg concat")
    return _finish_video(temporary, out_path, "ffmpeg concat")


def _render_shot(recipe, content_cut, human_cut, out_path):
    if recipe["kind"] == "host_full_bleed_text":
        return _compose_host_full_bleed_text(content_cut, human_cut, out_path)
    if recipe["kind"] == "split":
        return fuse.split(
            content_cut, human_cut, out_path,
            layout=recipe.get("layout") or "side_by_side",
            ratio=float(recipe.get("ratio", 0.6)),
            gap=int(recipe.get("gap", 18)),
            swap=bool(recipe.get("swap", False)),
            mix_audio=True)
    if recipe["kind"] == "overlay":
        return fuse.overlay(
            content_cut, human_cut, out_path,
            slot=recipe.get("slot") or "corner",
            scale=float(recipe.get("scale", 0.72)),
            mix_audio=True)
    raise ValueError("VIDEO_USE_RECIPE_UNSUPPORTED: %s" % recipe.get("kind"))


def _integrated_scene_spec(fusion_plan, animation_video, human_video=None, *, human_image=None,
                           content_has_alpha=None, animation_dimensions=None,
                           motion_video=None, motion_has_alpha=None,
                           preview_audio=None):
    """Build one Remotion timeline where the presenter is a scene plate.

    The presenter video stays continuous (and keeps its original audio). The
    document animation is a muted visual layer whose mask and transform change
    per director shot. This is the key distinction from split/overlay: there
    is one shared canvas and no opaque presenter rectangle.
    """
    edl = fusion_plan.get("edl") or {}
    shots = edl.get("shots") or []
    if not shots:
        raise SystemExit("VIDEO_USE_NO_SHOTS")
    width, height = _output_dimensions(fusion_plan)
    human_media = human_video or human_image
    human_width, human_height = _media_dimensions(human_media) if human_media else (0, 0)
    human_aspect = (float(human_width) / float(human_height)) if human_height else 0
    canvas_aspect = (float(width) / float(height)) if height else 0
    # A portrait presenter plate must remain intact on a landscape design
    # board.  The executor may only use cover when the source and canvas match.
    human_fit = "contain" if human_aspect and canvas_aspect and abs(human_aspect - canvas_aspect) > 0.01 else "cover"
    fps = int(fusion_plan.get("fps") or 30)
    if content_has_alpha is None:
        content_has_alpha = _media_has_alpha(animation_video)
    if motion_video and motion_has_alpha is None:
        motion_has_alpha = _media_has_alpha(motion_video)
    design_dimensions = animation_dimensions or _media_dimensions(animation_video)
    scenes = []
    cursor = 0
    for index, shot in enumerate(shots, 1):
        duration = max(0.1, float(shot.get("duration") or 0))
        frames = max(1, int(round(duration * fps)))
        mode = _normalize_visual_mode(shot.get("visual_mode")) or "split_left_right"
        # Integrated mode pre-focuses the human source, so scene-level focus can
        # stay close to center and only nudge content-led shots slightly.
        focus_x = 50
        scale = 1.0 if human_fit == "contain" else 1.18
        if human_fit != "contain":
            if mode == "host_full_bleed_text":
                scale = 1.12
            elif mode == "content_full_bleed":
                focus_x = 52
                scale = 1.24
            elif mode == "split_left_right":
                focus_x = 58
                scale = 1.08
            elif mode == "cutout_presenter":
                scale = 1.18
            elif mode == "corner_pip":
                scale = 1.10
        shot_layout = shot.get("shot_layout_hint") if isinstance(
            shot.get("shot_layout_hint"), dict) else {}
        scenes.append({
            "id": shot.get("id") or "shot_%02d" % index,
            "fromFrame": cursor,
            "durationInFrames": frames,
            "visualMode": mode,
            "layoutFamily": (
                "native_stacked_three_band" if mode == "stacked_top_bottom"
                else "integrated_scene"),
            "layoutRegions": _integrated_layout_regions(
                mode, shot.get("slot")),
            "component": shot.get("component") or shot.get("content_scene_kind") or "section",
            "humanSlot": shot.get("slot") or (
                "none" if mode == "content_full_bleed" else
                "right" if mode == "split_left_right" else "full"),
            "cameraMovement": shot.get("camera") or "still",
            "humanFocusX": focus_x,
            "humanScale": scale,
            "contentShiftX": (
                -int(width * 0.22) if mode in {
                    "host_full_bleed_text", "cutout_presenter"} else None
            ),
            "contentScale": (
                0.62 if mode in {
                    "host_full_bleed_text", "cutout_presenter"} else None
            ),
            "dialogue": shot.get("dialogue") or "",
            "animationHint": shot.get("animation_hint") or "",
            "contentDesign": shot.get("content_design") or "",
            "shotLayoutNote": shot_layout.get("note") or "",
            "motionElements": _as_list(shot.get("motion_elements")),
            "motionPlan": shot.get("motion_plan") or {},
            "executionPlan": shot.get("execution_plan") or
                (shot.get("motion_plan") or {}).get("execution_plan") or {},
            "assetSlots": shot.get("asset_slots") or [],
            "compositionContract": {
                "owner": "design_board",
                "usage": "render_contract_only",
                "videoModel": "avatar_plate_only",
                "contentOwner": "remotion",
                "motionOwner": "hyperframes",
                "assemblyOwner": "fuse",
                "design_board": "composite_plan",
                "video_model": "avatar_plate_only",
                "content_owner": "remotion",
                "motion_owner": "hyperframes",
                "assembly_owner": "fuse",
                "asset_slots_are_render_contract": True,
            },
            "motionTextOwner": (
                shot.get("motion_text_owner") or
                shot.get("motionTextOwner") or
                ("content" if animation_video else "foreground")
            ),
            "basePlate": shot.get("base_plate") or shot.get("base_plate_analysis") or {},
        })
        cursor += frames
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "brandPrimary": fusion_plan.get("brandPrimary") or "#64748B",
        "designTokens": fusion_plan.get("designTokens"),
        "humanVideoPath": os.path.abspath(human_video) if human_video else None,
        "humanImagePath": os.path.abspath(human_image) if human_image else None,
        "contentVideoPath": os.path.abspath(animation_video),
        "contentHasAlpha": content_has_alpha,
        "motionVideoPath": os.path.abspath(motion_video) if motion_video else None,
        "motionHasAlpha": bool(motion_has_alpha) if motion_video else None,
        "previewAudioPath": os.path.abspath(preview_audio) if preview_audio else None,
        "designBoardDimensions": list(design_dimensions),
        "designBoardAspectRatio": "16:9",
        "outputDimensions": [width, height],
        "outputAspectRatio": _display_aspect_ratio(width, height),
        "humanSourceDimensions": [human_width, human_height],
        "humanSourceAspectRatio": _display_aspect_ratio(human_width, human_height)
        if human_width and human_height else None,
        "humanFit": human_fit,
        "scenes": scenes,
        "_composition_contract": {
            "type": "scene_embedded",
            "human_role": (
                "continuous_scene_plate_with_audio" if human_video else
                ("approved_identity_still_placeholder" if human_image else "neutral_layout_placeholder")
            ),
            "animation_role": "muted_masked_motion_layer",
            "transparent_base_background": True,
            "edge_shadow_span_ratio": 0.15,
            "split_layout": {
                "content_fraction": 0.56,
                "presenter_fraction": 0.40,
                "gap_fraction": 0.04,
                "content_clip_enabled": True,
                "presenter_style": "editorial_card",
            },
            "hero_layout": {
                "content_fraction": 0.48,
                "presenter_fraction": 0.43,
                "gap_fraction": 0.04,
                "content_clip_enabled": True,
                "presenter_style": "editorial_card",
            },
            "opaque_pip": False,
            "local_ml_matting": False,
            "pipeline": ["design_board", "video_model.avatar_plate_only", "remotion", "hyperframes", "fuse"],
            "asset_slots_are_render_contract": True,
        },
    }


def build_previs_integrated_spec(fusion_plan, animation_video, *, human_image=None,
                                 motion_video=None, preview_audio=None):
    """Build the approval preview from the final IntegratedExplainer composition.

    A still or neutral silhouette stands in for the yet-to-be-generated talking
    plate. It approves geometry and timing only; avatar identity remains bound
    to the separate cast-board gate.
    """
    spec = _integrated_scene_spec(
        fusion_plan, animation_video, human_video=None, human_image=human_image,
        content_has_alpha=True, animation_dimensions=_output_dimensions(fusion_plan),
        motion_video=motion_video, motion_has_alpha=True if motion_video else None,
        preview_audio=preview_audio)
    spec["_artifact_contract"] = {
        "role": "execution_layout_and_animatic",
        "renderer_source_of_truth": "remotion.IntegratedExplainer",
        "approves": ["layout", "content_hierarchy", "safe_zones", "timing", "transition_rhythm"],
        "does_not_approve": ["avatar_motion_quality", "lip_sync", "video_model_texture"],
        "presenter_media": "approved_identity_still" if human_image else "neutral_placeholder",
        "video_model_receives_this_artifact": False,
        "hyperframes_layer": "actual_transparent_timeline" if motion_video else "not_required",
        "timing_source": "preview_audio" if preview_audio else "approved_edl",
    }
    return spec


def _media_has_alpha(path):
    fuse.ensure_ffmpeg_on_path()
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not os.path.isfile(path):
        return False
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=pix_fmt:stream_tags=alpha_mode", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return False
    try:
        stream = (json.loads(result.stdout).get("streams") or [{}])[0]
    except (ValueError, TypeError, IndexError):
        return False
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    alpha_mode = str((stream.get("tags") or {}).get("alpha_mode") or "")
    return alpha_mode == "1" or any(marker in pix_fmt for marker in (
        "yuva", "rgba", "argb", "abgr", "bgra", "gbrap"))


def _content_spec_for_fusion(fusion_plan, animation_video):
    candidates = []
    explicit = _text(fusion_plan.get("content_spec_path"))
    if explicit:
        candidates.append(os.path.abspath(explicit))
    source_plan = _text(fusion_plan.get("source_plan"))
    if source_plan:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(source_plan)),
                                       "content_spec.json"))
    animation_dir = os.path.dirname(os.path.abspath(animation_video))
    candidates.extend([
        os.path.join(animation_dir, "content_spec.json"),
        os.path.join(os.path.dirname(animation_dir), "content_spec.json"),
    ])
    return next((path for path in candidates if os.path.isfile(path)), None)


def _prepare_transparent_content_layer(fusion_plan, animation_video, work_dir):
    if _media_has_alpha(animation_video):
        return os.path.abspath(animation_video)
    scene_composition = fusion_plan.get("scene_composition")
    wants_transparent = (
        isinstance(scene_composition, dict) and
        scene_composition.get("transparent_base_background") is True
    )
    if not wants_transparent:
        return os.path.abspath(animation_video)
    content_spec_path = _content_spec_for_fusion(fusion_plan, animation_video)
    if not content_spec_path:
        if fusion_plan.get("content_render_required"):
            raise SystemExit(
                "VIDEO_USE_CONTENT_SPEC_REQUIRED: 文档讲解必须先生成 content_spec.json，"
                "再由 Remotion 渲染 PPT 动效层；不能退回普通静态幻灯片。")
        print(
            "[render-integrated] 未找到 content_spec.json，使用 screen 混合的低暗度兜底。",
            flush=True)
        return os.path.abspath(animation_video)

    content_spec = _load_json(content_spec_path)
    content_spec["transparentBackground"] = True
    transparent_spec_path = os.path.join(work_dir, "content_transparent_spec.json")
    transparent_video = os.path.join(work_dir, "content_transparent.webm")
    # Alpha intermediates are expensive to encode. Reuse the exact prior
    # render only when both its authored spec and probed alpha channel still
    # match the current contract.
    if os.path.isfile(transparent_spec_path) and os.path.isfile(transparent_video):
        try:
            cached_spec = _load_json(transparent_spec_path)
        except (OSError, ValueError, TypeError):
            cached_spec = None
        if cached_spec == content_spec and _media_has_alpha(transparent_video):
            print("[render-integrated] 复用已校验的 VP9 透明内容层。", flush=True)
            return transparent_video
    transparent_spec = _atomic_json(transparent_spec_path, content_spec)
    print("[render-integrated] 渲染浏览器可解码的 VP9 透明内容层。", flush=True)
    import remotion_engine
    remotion_engine.render(
        transparent_spec,
        transparent_video,
        quality="high",
        composition=remotion_engine.CONTENT_COMPOSITION,
        alpha=True,
    )
    if not _media_has_alpha(transparent_video):
        raise SystemExit("VIDEO_USE_TRANSPARENT_CONTENT_ALPHA_MISSING")
    return transparent_video


def _ensure_content_animation(fusion_plan, animation_video):
    """Render the PPT content spec before fusion, and invalidate stale still slides."""
    if not fusion_plan.get("content_render_required"):
        return os.path.abspath(animation_video)
    spec_path = _content_spec_for_fusion(fusion_plan, animation_video)
    if not spec_path:
        raise SystemExit("VIDEO_USE_CONTENT_SPEC_REQUIRED: 缺少 PPT Remotion content_spec.json")
    animation_video = os.path.abspath(animation_video)
    meta_path = animation_video + ".content-render.json"
    spec_sha = _sha256_file(spec_path)
    valid = False
    if os.path.isfile(animation_video) and os.path.isfile(meta_path):
        try:
            meta = _load_json(meta_path)
            valid = meta.get("spec_sha256") == spec_sha and meta.get("output") == animation_video
        except (OSError, ValueError, TypeError):
            valid = False
    if not valid:
        print("[render-content] 使用 Remotion 从 PPT content_spec 重新渲染动效层。", flush=True)
        import remotion_engine
        remotion_engine.render(
            spec_path, animation_video, quality="high",
            composition=remotion_engine.CONTENT_COMPOSITION)
        _atomic_json(meta_path, {
            "schema_version": 1, "output": animation_video,
            "spec": os.path.abspath(spec_path), "spec_sha256": spec_sha,
            "renderer": "remotion.Content",
        })
    return animation_video


def render_integrated_fusion(fusion_plan, animation_video, human_video, out_path,
                             work_dir=None, emit_json=True, *, motion_video=None,
                             approved_previs_spec=None, formal=False):
    """Render a shared-canvas Remotion explainer composition."""
    out_path = os.path.abspath(out_path)
    work_dir = os.path.abspath(work_dir or os.path.join(
        os.path.dirname(out_path), "integrated_work"))
    os.makedirs(work_dir, exist_ok=True)
    if formal and not approved_previs_spec:
        raise SystemExit(
            "APPROVED_PREVIS_SPEC_REQUIRED: 正式文档讲解合成必须绑定客户确认过的执行预演规格。")
    if formal:
        _require_customer_approved_previs(approved_previs_spec)
    animation_video = _ensure_content_animation(fusion_plan, animation_video)
    if not os.path.isfile(animation_video) or not os.path.isfile(human_video):
        raise SystemExit("VIDEO_USE_SCENE_PLATE_MISSING")
    _validate_human_timeline(fusion_plan, human_video)
    scene_composition = fusion_plan.get("scene_composition")
    human_focus = "center_speaker"
    if isinstance(scene_composition, dict):
        human_focus = _text(scene_composition.get("human_focus") or human_focus)
    human_source = human_video
    human_width, human_height = _media_dimensions(human_video)
    animation_width, animation_height = _media_dimensions(animation_video)
    human_ratio = float(human_width) / float(human_height) if human_height else 0
    animation_ratio = float(animation_width) / float(animation_height) if animation_height else 0
    aspect_mismatch = human_ratio and animation_ratio and abs(human_ratio - animation_ratio) > 0.01
    if aspect_mismatch:
        # Never crop a portrait plate into a landscape plate merely because the
        # legacy focus default is enabled. The original plate is the identity,
        # action, and timing source; IntegratedExplainer will contain it.
        print("[render-integrated] 保留底片原始画幅 %dx%d，跳过跨比例人物裁切。" %
              (human_width, human_height), flush=True)
        human_focus = "none"
    if human_focus and human_focus.lower() not in ("none", "off", "false", "0"):
        human_source = _prepare_human_focus(
            human_video, os.path.join(work_dir, "human_focused.mp4"), focus=human_focus)
    animation_source = _prepare_transparent_content_layer(
        fusion_plan, animation_video, work_dir)
    motion_has_alpha = None
    if motion_video:
        motion_video = os.path.abspath(motion_video)
        if not os.path.isfile(motion_video):
            raise SystemExit("VIDEO_USE_MOTION_LAYER_MISSING")
        motion_has_alpha = _media_has_alpha(motion_video)
        if formal and not motion_has_alpha:
            raise SystemExit("VIDEO_USE_MOTION_LAYER_ALPHA_REQUIRED")
    approval_binding = None
    if approved_previs_spec:
        spec, approval_binding = bind_approved_previs_spec(
            approved_previs_spec,
            content_video=animation_source,
            human_video=human_source,
            motion_video=motion_video,
            content_has_alpha=_media_has_alpha(animation_source),
            motion_has_alpha=motion_has_alpha)
        if formal and approval_binding.get("customer_approved") is not True:
            raise SystemExit(
                "CUSTOMER_APPROVED_PREVIS_REQUIRED: 正式合成只能绑定客户已确认的 design_board "
                "v2 审批记录；execution preview 本身不代表客户同意。")
        if formal:
            _validate_approved_content_contract(fusion_plan, approval_binding)
        approved_duration = max(
            (int(scene.get("fromFrame") or 0) + int(scene.get("durationInFrames") or 0)
             for scene in spec.get("scenes") or []), default=0) / float(spec.get("fps") or 30)
        actual_duration = float(_media_duration(human_video))
        tolerance = max(0.6, approved_duration * 0.02)
        if approved_duration and abs(actual_duration - approved_duration) > tolerance:
            raise SystemExit(
                "APPROVED_TIMELINE_INVALIDATED: 人物底片 %.2fs 与已确认预演 %.2fs 相差超过 %.2fs；"
                "必须按真实底片时长重渲 Animatic 并重新确认，不能静默拉伸或截断。"
                % (actual_duration, approved_duration, tolerance))
    else:
        spec = _integrated_scene_spec(
            fusion_plan, animation_source, human_source,
            motion_video=motion_video, motion_has_alpha=motion_has_alpha)
    spec_path = _atomic_json(os.path.join(work_dir, "integrated_spec.json"), spec)
    print("[render-integrated] shared canvas: human scene plate + masked motion layer", flush=True)
    import remotion_engine
    quality_spec_path = None
    if formal:
        width = int(spec.get("width") or animation_width or 1920)
        height = int(spec.get("height") or animation_height or 1080)
        fps = int(spec.get("fps") or 30)
        shots = list(spec.get("scenes") or [])
        tokens = design_token_contract.resolve(fusion_plan, required=True)
        compiler_plan = {
            **fusion_plan,
            "width": width, "height": height, "fps": fps,
            "output_ratio": fusion_plan.get("output_ratio") or fusion_plan.get("ratio"),
            "shots": shots,
        }
        quality_spec = render_spec_compiler.compile_spec(
            compiler_plan, tokens, motion_design=fusion_plan.get("motion_design"),
            audio_contract=fusion_plan.get("audio_contract"), engine_props=spec,
            composition="IntegratedExplainer", render_profile="direct_delivery",
            formal=True)
        quality_spec_path = _atomic_json(
            os.path.join(work_dir, "render_quality_spec.json"), quality_spec)
        remotion_engine.render_quality_spec(quality_spec_path, out_path)
    else:
        remotion_engine.render(
            spec_path, out_path, quality="high",
            composition=remotion_engine.INTEGRATED_COMPOSITION,
            profile="direct_delivery")
    duration = round(_media_duration(out_path), 3)
    manifest = {
        "schema_version": 2,
        "type": "video_use_scene_embedded_render",
        "animation_video": os.path.abspath(animation_video),
        "animation_source_used": os.path.abspath(animation_source),
        "animation_has_alpha": _media_has_alpha(animation_source),
        "human_video": os.path.abspath(human_video),
        "human_source_used": os.path.abspath(human_source),
        "human_focus": human_focus,
        "motion_video": motion_video,
        "motion_has_alpha": motion_has_alpha,
        "integrated_spec": spec_path,
        "render_quality_spec": quality_spec_path,
        "approval_binding": approval_binding,
        "formal": bool(formal),
        "out": out_path,
        "duration": duration,
        "shots": spec["scenes"],
        "contract": spec["_composition_contract"],
    }
    manifest_path = _atomic_json(os.path.splitext(out_path)[0] + ".integrated.json", manifest)
    if emit_json:
        print(json.dumps({
            "ok": True,
            "out": out_path,
            "manifest": manifest_path,
            "shots": len(spec["scenes"]),
            "duration": duration,
            "composition": "IntegratedExplainer",
        }, ensure_ascii=False, indent=2))
    return out_path


def render_segmented_fusion(fusion_plan, animation_video, human_video, out_path,
                            work_dir=None, human_focus="center_speaker", emit_json=True):
    edl = fusion_plan.get("edl") or {}
    shots = edl.get("shots") or []
    if not shots:
        raise SystemExit("VIDEO_USE_NO_SHOTS")
    _validate_human_timeline(fusion_plan, human_video)
    out_path = os.path.abspath(out_path)
    work_dir = os.path.abspath(work_dir or os.path.join(
        os.path.dirname(out_path), "segmented_work"))
    os.makedirs(work_dir, exist_ok=True)
    human_source = _prepare_human_focus(
        human_video, os.path.join(work_dir, "human_focused.mp4"), focus=human_focus)
    rendered = []
    manifest = {
        "schema_version": 1,
        "type": "video_use_segmented_render",
        "animation_video": os.path.abspath(animation_video),
        "human_video": os.path.abspath(human_video),
        "human_source_used": os.path.abspath(human_source),
        "human_focus": human_focus,
        "out": out_path,
        "shots": [],
    }
    for index, shot in enumerate(shots, 1):
        start = float(shot.get("start_sec", 0))
        duration = float(shot.get("duration") or (float(shot.get("end_sec", start)) - start))
        sid = re.sub(r"[^0-9A-Za-z_-]+", "_", _text(shot.get("id") or "shot_%02d" % index))
        content_cut = os.path.join(work_dir, "%02d_%s_content.mp4" % (index, sid))
        human_cut = os.path.join(work_dir, "%02d_%s_human.mp4" % (index, sid))
        shot_out = os.path.join(work_dir, "%02d_%s_fused.mp4" % (index, sid))
        print("[render-segmented] %s %.2fs-%.2fs" % (sid, start, start + duration), flush=True)
        _cut_video(animation_video, start, duration, content_cut)
        _cut_video(human_source, start, duration, human_cut)
        recipe = _shot_render_recipe(shot, index)
        _render_shot(recipe, content_cut, human_cut, shot_out)
        rendered.append(shot_out)
        manifest["shots"].append({
            "id": sid,
            "start_sec": round(start, 3),
            "duration": round(duration, 3),
            "visual_mode": shot.get("visual_mode"),
            "recipe": recipe,
            "content_cut": os.path.abspath(content_cut),
            "human_cut": os.path.abspath(human_cut),
            "fused": os.path.abspath(shot_out),
        })
    _concat_segments(rendered, out_path)
    manifest["out"] = out_path
    manifest["duration"] = round(_media_duration(out_path), 3)
    manifest_path = _atomic_json(os.path.splitext(out_path)[0] + ".segmented.json", manifest)
    if emit_json:
        print(json.dumps({
            "ok": True,
            "out": out_path,
            "manifest": manifest_path,
            "shots": len(rendered),
            "duration": manifest["duration"],
        }, ensure_ascii=False, indent=2))
    return out_path


def _extract_units(plan):
    units = []
    source = plan.get("shots") if isinstance(plan.get("shots"), list) else None
    if source is None and isinstance(plan.get("rows"), list):
        source = plan["rows"]
    source = source or []
    document_explainer = _is_document_explainer_plan(plan)
    base_analysis = plan.get("base_plate_analysis") if isinstance(plan.get("base_plate_analysis"), dict) else {}
    base_by_id = {
        str(item.get("shot_id") or item.get("id")): item
        for item in base_analysis.get("shots") or [] if isinstance(item, dict)
    }
    total = len(source)
    for index, raw in enumerate(source, 1):
        if not isinstance(raw, dict):
            continue
        sid = _text(raw.get("id") or raw.get("shot_id") or raw.get("scene_id") or "s%d" % index)
        duration = raw.get("duration") or raw.get("duration_sec") or raw.get("seconds") or 5
        try:
            duration = max(0.1, float(duration))
        except (TypeError, ValueError):
            duration = 5.0
        dialogue = _text(
            raw.get("dialogue") or raw.get("talk") or raw.get("voiceover") or raw.get("text"),
            limit=500)
        animation_hint = _text(
            raw.get("animation_hint") or raw.get("content_design") or raw.get("content") or
            raw.get("motion_overlay") or raw.get("visual") or raw.get("scene_prompt") or
            raw.get("overlay"),
            limit=500)
        content_design = _text(
            raw.get("content_design") or raw.get("design_intent") or raw.get("scene_kind") or
            raw.get("component") or raw.get("content_scene"),
            limit=500)
        slide_image = _ppt_slide_image(plan, raw)
        source_slides = _as_list(
            raw.get("source_slides") or raw.get("slide_range") or raw.get("source_pages"))
        if not source_slides:
            source_slides = [int(match.group(1)) for tag in _as_list(raw.get("ref_tags"))
                             for match in [re.search(r"(?:ppt_)?slide[_-]?(\d+)", str(tag), re.I)]
                             if match]
        content_kind = _content_scene_kind(raw, slide_image)
        human_hint = _text(
            raw.get("character_action") or raw.get("action") or dialogue,
            limit=500)
        visual_mode = _infer_visual_mode(
            index, raw,
            document_explainer=document_explainer,
            is_last=index == total)
        motion_plan = raw.get("motion_plan") if isinstance(raw.get("motion_plan"), dict) else {}
        base_plate = raw.get("base_plate") or raw.get("base_plate_analysis") or base_by_id.get(sid) or {}
        asset_slots = _as_list(raw.get("asset_slots"))
        # Document explainers are render-contract driven.  Normalize legacy
        # shots here so Remotion never receives a text-only motion placeholder.
        if document_explainer:
            try:
                import motion_design
                motion_plan = motion_design.ensure_motion_plan_contract(
                    dict(raw, storyboard_role="design_board"),
                    {"storyboard_role": "design_board",
                     "source_ppt": plan.get("source_ppt") or plan.get("source_doc"),
                     "scene_type": plan.get("scene_type")})
                asset_slots = motion_plan.get("asset_slots") or asset_slots
            except (TypeError, ValueError):
                pass
        units.append({
            "id": sid,
            "duration": duration,
            "dialogue": dialogue,
            "animation_hint": animation_hint,
            "content_design": content_design,
            "human_hint": human_hint,
            "ref_tags": _as_list(raw.get("ref_tags")),
            "source_refs": _as_list(
                raw.get("source_refs") or raw.get("slide_range") or raw.get("source_pages")),
            "source_slides": source_slides,
            "slide_image": slide_image,
            "content_scene_kind": content_kind,
            "component": _text(raw.get("component") or content_kind),
            "slot": _text(raw.get("slot") or raw.get("human_slot") or raw.get("presenter_slot")),
            "camera": _text(raw.get("camera") or raw.get("camera_movement") or "still"),
            "content_props": _content_props(raw, slide_image, index),
            "motion_elements": _as_list(raw.get("motion_elements")),
            "motion_plan": motion_plan,
            "execution_plan": raw.get("execution_plan") or motion_plan.get("execution_plan") or {},
            "asset_slots": asset_slots,
            "visual_mode": visual_mode,
            "shot_layout_hint": _shot_layout_hint(visual_mode),
            "base_plate": base_plate,
        })
    return units


def build_edl(plan, layout):
    """Compile shot/row durations into a lightweight explainer EDL."""
    timeline = []
    cursor = 0.0
    for unit in _extract_units(plan):
        end = cursor + unit["duration"]
        timeline.append({
            "id": unit["id"],
            "start_sec": round(cursor, 3),
            "end_sec": round(end, 3),
            "duration": round(unit["duration"], 3),
            "dialogue": unit["dialogue"],
            "animation_hint": unit["animation_hint"],
            "content_design": unit["content_design"],
            "human_hint": unit["human_hint"],
            "motion_elements": unit["motion_elements"],
            "motion_plan": unit.get("motion_plan") or {},
            "execution_plan": unit.get("execution_plan") or
                (unit.get("motion_plan") or {}).get("execution_plan") or {},
            "asset_slots": unit.get("asset_slots") or [],
            "ref_tags": unit["ref_tags"],
            "source_refs": unit["source_refs"],
            "source_slides": unit.get("source_slides") or [],
            "slide_image": unit.get("slide_image"),
            "content_scene_kind": unit.get("content_scene_kind") or "section",
            "component": unit.get("component") or unit.get("content_scene_kind") or "section",
            "slot": unit.get("slot") or None,
            "camera": unit.get("camera") or "still",
            "content_props": unit.get("content_props") or {},
            "visual_mode": unit["visual_mode"],
            "shot_layout_hint": unit["shot_layout_hint"],
            "base_plate": unit.get("base_plate") or {},
            "fusion_layout": layout,
        })
        cursor = end
    return {
        "schema_version": 1,
        "type": "explainer_edl",
        "total_duration": round(cursor, 3),
        "shots": timeline,
    }


def build_content_spec(plan, edl):
    """Compile the PPT-derived content into a real Remotion scene spec."""
    scenes = []
    fps = int(plan.get("fps") or 30)
    output_width, output_height = _output_dimensions(plan)
    for shot in edl.get("shots") or []:
        props = dict(shot.get("content_props") or {})
        props.setdefault("dialogue", shot.get("dialogue") or "")
        props.setdefault("motion_elements", shot.get("motion_elements") or [])
        props.setdefault("motion_plan", shot.get("motion_plan") or {})
        props.setdefault("execution_plan", shot.get("execution_plan") or
                         (shot.get("motion_plan") or {}).get("execution_plan") or {})
        props.setdefault("asset_slots", shot.get("asset_slots") or [])
        props.setdefault("base_plate", shot.get("base_plate") or {})
        props.setdefault("base_plate_analysis", plan.get("base_plate_analysis") or {})
        props.setdefault("design_board_role", "composite_plan")
        props.setdefault("motion_engine", "hyperframes.semantic_motion_compiler")
        if props.get("motion_plan"):
            try:
                props.setdefault("motion_grammar", hf_engine.compile_motion_grammar(
                    props["motion_plan"], end=float(shot.get("duration") or 1)))
            except (TypeError, ValueError):
                # The storyboard audit remains the authoritative preflight;
                # keep content spec generation backward compatible for legacy
                # plans and let formal render validation report the issue.
                props.setdefault("motion_grammar", {})
        scenes.append({
            "kind": shot.get("content_scene_kind") or "section",
            "durationInFrames": max(1, int(round(float(shot.get("duration") or 1) * fps))),
            "transition": "lightsweep" if shot.get("motion_plan") else "fade",
            "props": props,
            "source_shot_id": shot.get("id"),
            "visualMode": shot.get("visual_mode"),
            "layoutRegions": _integrated_layout_regions(
                shot.get("visual_mode"), shot.get("slot")),
            "component": shot.get("component") or shot.get("content_scene_kind") or "section",
            "slot": shot.get("slot"),
            "camera": shot.get("camera") or "still",
            "source_slides": shot.get("source_slides") or [],
            "source_slide_image": shot.get("slide_image"),
            "base_plate": shot.get("base_plate") or {},
        })
    design_tokens = design_token_contract.resolve(plan)
    return {
        "schema_version": 2,
        "type": "ppt_explainer_content_spec",
        "width": output_width,
        "height": output_height,
        "fps": fps,
        "orientation": "portrait" if output_height > output_width else "landscape",
        "storyboard_aspect_ratio": plan.get("storyboard_aspect_ratio") or "16:9",
        "output_ratio": plan.get("output_ratio") or "%d:%d" % (output_width, output_height),
        "brandPrimary": _brand_primary_for_plan(plan),
        "designTokens": design_tokens,
        "transparentBackground": False,
        "source_document": plan.get("source_ppt") or plan.get("source_doc") or plan.get("source_pdf"),
        "material_interpretation": plan.get("material_interpretation") or {},
        "base_plate_analysis": plan.get("base_plate_analysis") or {},
        "composition_contract": {
            "owner": "remotion",
            "ppt_pages_are_bound_assets": True,
            "motion_owner": "remotion_and_hyperframes",
            "video_model_receives_ppt_pages": False,
            "design_board": "composite_plan",
            "video_model": "avatar_plate_only",
            "assembly_owner": "fuse",
            "pipeline": ["design_board", "video_model.avatar_plate_only", "remotion", "hyperframes", "fuse"],
            "required_asset_slots": [
                "ppt_page", "corner_pip", "title_area", "safe_zone", "highlight_card",
                "chart", "arrow", "annotation",
            ],
        },
        "scenes": scenes,
    }


def build_takes_packed_md(plan, edl):
    title = _text(plan.get("title") or plan.get("theme") or plan.get("product_name") or "Explainer edit")
    lines = [
        "# takes_packed",
        "",
        "Source: approved director script",
        "Mode: explainer, audio/script first, visuals support the explanation",
        "Title: %s" % title,
        "",
    ]
    for item in edl.get("shots") or []:
        lines.append("## %s  %.2fs-%.2fs" % (
            item["id"], item["start_sec"], item["end_sec"]))
        if item.get("dialogue"):
            lines.append("- spoken: %s" % item["dialogue"])
        if item.get("animation_hint"):
            lines.append("- animation: %s" % item["animation_hint"])
        if item.get("visual_mode"):
            lines.append("- visual mode: %s" % item["visual_mode"])
        if item.get("content_design"):
            lines.append("- content design: %s" % item["content_design"])
        if item.get("human_hint") and item.get("human_hint") != item.get("dialogue"):
            lines.append("- presenter: %s" % item["human_hint"])
        if item.get("motion_elements"):
            lines.append("- overlays: %s" % " | ".join(map(str, item["motion_elements"])))
        if item.get("motion_plan"):
            mp = item.get("motion_plan") or {}
            lines.append("- motion plan: personality=%s density=%s timing=%s safe_zone=%s" % (
                mp.get("personality"), mp.get("density"), mp.get("timing"), mp.get("safe_zone")))
        if item.get("asset_slots"):
            lines.append("- asset slots: %s" % " | ".join(
                str(slot.get("slot") if isinstance(slot, dict) else slot)
                for slot in item.get("asset_slots") or []))
        if item.get("ref_tags"):
            lines.append("- refs: %s" % " ".join(map(str, item["ref_tags"])))
        if item.get("source_refs"):
            lines.append("- source refs: %s" % " ".join(map(str, item["source_refs"])))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _session_count(project_md_path):
    try:
        with open(project_md_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return 0
    return len(re.findall(r"^## Session\b", text, flags=re.M))


def build_project_section(plan, fusion_plan, edl, session_index=1):
    title = _text(plan.get("title") or plan.get("theme") or plan.get("product_name") or "Explainer edit")
    layout = fusion_plan["fusion"]
    if layout["kind"] == "overlay":
        layout_line = "picture-in-picture / slot=%s / scale=%.2f" % (
            layout.get("slot"), layout.get("scale", 0.9))
    else:
        layout_line = "%s / ratio=%.2f / gap=%spx" % (
            layout.get("layout"), layout.get("ratio", 0.58), layout.get("gap", 24))
    visual_modes = [item.get("visual_mode") for item in edl.get("shots") or []
                    if item.get("visual_mode")]
    rhythm = " -> ".join(visual_modes[:8]) if visual_modes else "not specified"
    lines = [
        "## Session %d - %s" % (session_index, _dt.datetime.now().isoformat(timespec="seconds")),
        "",
        "**Project:** %s" % title,
        "",
        "**Strategy:** This is a document-derived explainer edit. Treat PPT/PDF/Word as source material, "
        "extract the meaning, then redesign it into motion graphics. The approved narration is the spine; "
        "HyperFrames/Remotion carry content animation, while the digital-human branch adds presenter presence.",
        "",
        "**Decisions:**",
        "- Layout: %s" % layout_line,
        "- Visual rhythm: %s" % rhythm,
        "- Animation branch: HyperFrames/Remotion document or content animation",
        "- Human branch: BasicRouter digital-human presentation video",
        "- Audio policy: %s" % ("mix animation + presenter audio" if layout.get("mix_audio") else "keep primary branch audio"),
        "- Editing memory: use takes_packed.md + edl.json before inspecting full video frames",
        "",
        "**Timeline:** %d units, %.2fs total" % (
            len(edl.get("shots") or []), edl.get("total_duration") or 0),
        "",
        "**Next checks:**",
        "- Confirm source documents are not used as raw slideshow playback unless a specific chart/image must be preserved.",
        "- Confirm each shot uses a distinct visual mode instead of a single picture-in-picture treatment.",
        "- Confirm the digital human branch matches the approved voice, look, and script.",
        "- Confirm the fused cut preserves readability and does not double-render spoken CTA text.",
        "",
    ]
    return "\n".join(lines)


def build_fusion_plan(plan, *, source_plan=None, animation_video=None, human_video=None,
                      layout=None, split_ratio=None, gap=None, swap=None,
                      composition_mode=None):
    if _is_oral_broadcast_plan(plan):
        raise ValueError(
            "VIDEO_USE_ORAL_BROADCAST_UNSUPPORTED: 口播型请走 horizontal-kinetic-talk-video，"
            "不要用 video_use_bridge 的 explainer 分支")
    layout_spec = _normalized_layout(
        plan, override=layout, split_ratio=split_ratio, gap=gap, swap=swap)
    edl = build_edl(plan, layout_spec)
    document_explainer = _is_document_explainer_plan(plan)
    dense_slide_preservation = _is_dense_slide_preservation_plan(plan)
    fusion_input = plan.get("fusion") if isinstance(plan.get("fusion"), dict) else {}
    mode = _text(
        composition_mode or fusion_input.get("composition_mode") or
        plan.get("composition_mode")).lower().replace("-", "_")
    if not mode:
        # Redesigned document explainers use a shared canvas. Dense slide/page
        # preservation keeps the original page full-frame with a presenter PIP.
        mode = (
            "legacy_layout"
            if dense_slide_preservation
            else ("scene_embedded" if document_explainer else "legacy_layout")
        )
    if mode not in ("scene_embedded", "legacy_layout"):
        raise ValueError("VIDEO_USE_COMPOSITION_MODE_UNSUPPORTED: %s" % mode)
    render_strategy = "segmented" if len(edl.get("shots") or []) > 1 else "static"
    content_source = _existing_content_spec_path(plan, source_plan)
    design_tokens = design_token_contract.resolve(plan, source_plan=source_plan)
    return {
        "schema_version": 1,
        "type": "video_use_explainer_fusion",
        "source": "browser-use/video-use style editing bridge",
        "source_plan": os.path.abspath(source_plan) if source_plan else None,
        "ratio": _ratio_from_plan(plan),
        "output_ratio": _ratio_from_plan(plan),
        "client": plan.get("client"),
        "run_id": plan.get("run_id"),
        "brandPrimary": _brand_primary_for_plan(plan, source_plan),
        "designTokens": design_tokens,
        "fps": int(plan.get("fps") or 30),
        "fusion": layout_spec,
        "composition_mode": mode,
        "render_strategy": render_strategy,
        "pipeline_contract": {
            "design_board": "composite_plan",
            "video_model": "avatar_plate_only",
            "video_model_allowed_locks": ["person", "voice", "wardrobe", "background"],
            "content_owner": "remotion",
            "motion_owner": "hyperframes",
            "assembly_owner": "fuse",
            "storyboard_ref_for_video_model": False,
            "base_plate_is_timing_master": bool(plan.get("base_plate_analysis")),
            "content_adapts_to_base_plate": bool(plan.get("base_plate_analysis")),
            "base_plate_analysis_required": bool(document_explainer and plan.get("human_segments")),
        },
        "material_interpretation": plan.get("material_interpretation") or {},
        "base_plate_analysis": plan.get("base_plate_analysis") or {},
        "branches": {
            "animation": {
                "engine": "hyperframes_or_remotion",
                "video": os.path.abspath(animation_video) if animation_video else None,
                "role": ("masked_motion_graphics_layer" if mode == "scene_embedded"
                         else "document_animation"),
            },
            "human": {
                "engine": "video_engine",
                "video": os.path.abspath(human_video) if human_video else None,
                "role": ("digital_human_scene_plate" if mode == "scene_embedded"
                         else "digital_human_presenter"),
            },
        },
        "scene_composition": {
            "engine": "remotion",
            "composition": "IntegratedExplainer",
            "enabled": mode == "scene_embedded",
            "opaque_pip": False,
            "local_ml_matting": False,
            "human_focus": "center_speaker",
            "transparent_base_background": True,
            "edge_shadow_span_ratio": 0.15,
            "note": ("数字人是同一时间轴的场景底片，内容动效以蒙版和运动层进入同一画面；"
                     "底色保持通透，左右阴影只保留边缘 15% 内，避免整屏压暗。"),
        },
        "content_render_required": bool(document_explainer),
        "content_spec_source_path": content_source,
        "edl": edl,
        "editorial_principles": {
            "audio_primary": True,
            "document_animation_branch": True,
            "source_documents_are_material_not_frames": document_explainer,
            "source_documents_are_preserved_frames": dense_slide_preservation,
            "vary_visual_mode_by_shot": True,
            "digital_human_branch": True,
            "digital_human_embedded_in_animation_scene": mode == "scene_embedded",
            "transparent_base_background": True,
            "edge_shadow_span_ratio": 0.15,
            "local_ml_models": False,
            "confirmation_gates_remain_required": True,
        },
    }


def write_plan_files(plan, fusion_plan, out_dir):
    out_dir = os.path.abspath(out_dir or _default_edit_dir(plan))
    os.makedirs(out_dir, exist_ok=True)
    source_spec = fusion_plan.get("content_spec_source_path")
    if source_spec and os.path.isfile(source_spec):
        content = _load_json(source_spec)
        source_fps = int(content.get("fps") or 30)
        target_fps = int(fusion_plan.get("fps") or source_fps)
        if source_fps != target_fps:
            for scene in content.get("scenes") or []:
                if isinstance(scene, dict) and scene.get("durationInFrames") is not None:
                    scene["durationInFrames"] = max(
                        1, int(round(float(scene["durationInFrames"]) *
                                     target_fps / float(source_fps))))
        content["fps"] = target_fps
        content["brandPrimary"] = fusion_plan.get("brandPrimary") or content.get("brandPrimary")
        content["designTokens"] = fusion_plan.get("designTokens") or content.get("designTokens")
        content.setdefault("composition_contract", {})
        content["composition_contract"].update({
            "owner": "remotion",
            "ppt_pages_are_bound_assets": True,
            "motion_owner": "remotion_and_hyperframes",
            "video_model_receives_ppt_pages": False,
            "design_board": "composite_plan",
            "video_model": "avatar_plate_only",
            "assembly_owner": "fuse",
            "pipeline": ["design_board", "video_model.avatar_plate_only", "remotion", "hyperframes", "fuse"],
        })
    else:
        content = build_content_spec(plan, fusion_plan["edl"])
    source_plan = fusion_plan.get("source_plan")
    source_plan_path = (os.path.abspath(source_plan)
                        if source_plan and os.path.isfile(source_plan) else None)
    source_spec_path = (os.path.abspath(source_spec)
                        if source_spec and os.path.isfile(source_spec) else None)
    provenance = {
        "compiler": "video_use_bridge.write_plan_files",
        "compiler_version": CONTENT_SPEC_COMPILER_VERSION,
        "authorship": ("validated_source_content_spec" if source_spec_path
                       else "compiled_from_director_plan"),
        "source_plan_path": source_plan_path,
        "source_plan_sha256": _sha256_file(source_plan_path) if source_plan_path else None,
        "source_content_spec_path": source_spec_path,
        "source_content_spec_sha256": _sha256_file(source_spec_path) if source_spec_path else None,
        "agent_runtime": detect_agent_runtime(),
        "customer_confirmation_required": True,
        "approval_status": "unconfirmed",
    }
    content["provenance"] = provenance
    validation = content_scaffold.validate(content)
    if not validation.get("ok"):
        raise ValueError(
            "VIDEO_USE_CONTENT_SPEC_INVALID: %s" %
            " | ".join(str(item) for item in validation.get("errors") or []))
    content_path = _atomic_json(os.path.join(out_dir, "content_spec.json"), content)
    fusion_plan["content_spec_path"] = content_path
    fusion_plan["content_contract"] = {
        "spec_path": content_path,
        "spec_sha256": _sha256_file(content_path),
        "render_required": bool(fusion_plan.get("content_render_required")),
        "validation": validation,
        "provenance": provenance,
    }
    fusion_path = _atomic_json(os.path.join(out_dir, "fusion_plan.json"), fusion_plan)
    edl_path = _atomic_json(os.path.join(out_dir, "edl.json"), fusion_plan["edl"])
    takes_path = _write_text(
        os.path.join(out_dir, "takes_packed.md"),
        build_takes_packed_md(plan, fusion_plan["edl"]))
    project_path = os.path.join(out_dir, "project.md")
    section = build_project_section(
        plan, fusion_plan, fusion_plan["edl"], session_index=_session_count(project_path) + 1)
    prefix = "" if not os.path.exists(project_path) else "\n"
    project_path = _write_text(project_path, prefix + section, append=True)
    fusion_plan["files"] = {
        "fusion_plan": fusion_path,
        "edl": edl_path,
        "takes_packed": takes_path,
        "project_md": project_path,
        "content_spec": content_path,
    }
    _atomic_json(fusion_path, fusion_plan)
    return fusion_plan


def _should_use_segmented_render(fusion_plan):
    fusion_spec = fusion_plan.get("fusion") if isinstance(fusion_plan.get("fusion"), dict) else {}
    strategy = _text(
        fusion_plan.get("render_strategy") or fusion_spec.get("render_strategy")).lower()
    if strategy in ("static", "flat", "single"):
        return False
    edl = fusion_plan.get("edl") if isinstance(fusion_plan.get("edl"), dict) else {}
    shots = edl.get("shots") if isinstance(edl.get("shots"), list) else []
    if len(shots) <= 1:
        return False
    if strategy in ("segmented", "shot", "shots", "bridge", "auto"):
        return True
    editorial = fusion_plan.get("editorial_principles")
    if isinstance(editorial, dict) and editorial.get("vary_visual_mode_by_shot"):
        return True
    modes = {shot.get("visual_mode") for shot in shots if isinstance(shot, dict) and shot.get("visual_mode")}
    return len(modes) > 1


def _should_use_integrated_render(fusion_plan):
    mode = _text(fusion_plan.get("composition_mode")).lower().replace("-", "_")
    scene_spec = fusion_plan.get("scene_composition")
    return mode == "scene_embedded" or (
        isinstance(scene_spec, dict) and scene_spec.get("enabled") is True)


def render_fusion(fusion_plan, animation_video, human_video, out_path, *,
                  motion_video=None, approved_previs_spec=None, formal=False):
    fusion_spec = fusion_plan.get("fusion") or {}
    if formal and not approved_previs_spec:
        raise SystemExit(
            "APPROVED_PREVIS_SPEC_REQUIRED: 正式文档讲解合成必须绑定客户确认过的执行预演规格。")
    if formal:
        _require_customer_approved_previs(approved_previs_spec)
    _validate_human_timeline(fusion_plan, human_video)
    animation_video = _ensure_content_animation(fusion_plan, animation_video)
    if _should_use_integrated_render(fusion_plan):
        return render_integrated_fusion(
            fusion_plan, animation_video, human_video, out_path, emit_json=False,
            motion_video=motion_video, approved_previs_spec=approved_previs_spec,
            formal=formal)
    if _should_use_segmented_render(fusion_plan):
        return render_segmented_fusion(
            fusion_plan, animation_video, human_video, out_path, emit_json=False)
    if fusion_spec.get("kind") == "overlay":
        slot = fusion_spec.get("slot") or "corner"
        scale = float(fusion_spec.get("scale", 0.9))
        if fusion_spec.get("swap"):
            animation_video, human_video = human_video, animation_video
        return fuse.overlay(
            animation_video, human_video, out_path,
            slot=slot, scale=scale, mix_audio=bool(fusion_spec.get("mix_audio", True)))
    layout = fusion_spec.get("layout") or "side_by_side"
    return fuse.split(
        animation_video, human_video, out_path,
        layout=layout, ratio=float(fusion_spec.get("ratio", 0.58)),
        gap=int(fusion_spec.get("gap", 24)),
        swap=bool(fusion_spec.get("swap", False)),
        mix_audio=bool(fusion_spec.get("mix_audio", True)))


def _formal_fusion_transaction_current(manifest, *, plan_path, fusion_plan,
                                       animation_video, human_video, motion_video,
                                       approved_previs_spec, out_path):
    transaction = manifest.get("video_fusion") or {}
    expected = {
        "plan": run_manifest_store.file_record(plan_path),
        "human": run_manifest_store.file_record(human_video),
        "motion": (run_manifest_store.file_record(motion_video)
                   if motion_video else None),
        "approval_record": run_manifest_store.file_record(approved_previs_spec),
    }
    if (transaction.get("status") != "running" or
            transaction.get("plan") != expected["plan"] or
            transaction.get("human") != expected["human"] or
            transaction.get("motion") != expected["motion"] or
            transaction.get("approval_record") != expected["approval_record"] or
            transaction.get("planned_output") != os.path.abspath(out_path) or
            transaction.get("planned_render_manifest") !=
            os.path.splitext(os.path.abspath(out_path))[0] + ".integrated.json" or
            any(not run_manifest_store.file_record_is_current(record)
                for record in expected.values() if record)):
        raise ValueError("VIDEO_FUSION_RUNNING_TRANSACTION_REQUIRED")
    animation_record = run_manifest_store.file_record(animation_video)
    content_spec = transaction.get("content_spec")
    if content_spec:
        if not run_manifest_store.file_record_is_current(content_spec):
            raise ValueError("VIDEO_FUSION_CONTENT_SPEC_STALE")
        meta_path = os.path.abspath(animation_video) + ".content-render.json"
        if not os.path.isfile(meta_path):
            raise ValueError("VIDEO_FUSION_CONTENT_RENDER_BINDING_REQUIRED")
        meta = _load_json(meta_path)
        if (meta.get("output") != os.path.abspath(animation_video) or
                meta.get("spec") != content_spec.get("path") or
                meta.get("spec_sha256") != content_spec.get("sha256") or
                not animation_record or not animation_record.get("exists")):
            raise ValueError("VIDEO_FUSION_CONTENT_RENDER_BINDING_REQUIRED")
    elif transaction.get("animation") != animation_record:
        raise ValueError("VIDEO_FUSION_RUNNING_TRANSACTION_REQUIRED")
    if (str(fusion_plan.get("client") or "") != str(manifest.get("client") or "") or
            str(fusion_plan.get("run_id") or "") != str(manifest.get("run_id") or "")):
        raise ValueError("VIDEO_FUSION_RUN_IDENTITY_MISMATCH")
    return transaction, animation_record


def render_formal_fusion_and_record(manifest, manifest_path, *, client, plan_path,
                                    fusion_plan, animation_video, human_video,
                                    out_path, motion_video, approved_previs_spec):
    """Bind approved design-board composition to the pending formal video cut."""
    run_manifest_store.identity_gate(manifest, client=client)
    if (str(fusion_plan.get("client") or "") != str(client) or
            str(fusion_plan.get("run_id") or "") != str(manifest.get("run_id") or "")):
        raise ValueError("VIDEO_FUSION_RUN_IDENTITY_MISMATCH")
    if not _should_use_integrated_render(fusion_plan):
        raise ValueError("FORMAL_VIDEO_FUSION_INTEGRATED_SPEC_REQUIRED")
    if (not run_manifest_store.approval_is_current(manifest, "storyboard") or
            not run_manifest_store.customer_approval_evidence_is_current(
                manifest, "storyboard")):
        raise ValueError("VIDEO_FUSION_STORYBOARD_APPROVAL_REQUIRED")
    approval_path = os.path.abspath(approved_previs_spec)
    evidence = (manifest.get("approval_evidence") or {}).get("storyboard") or {}
    if os.path.abspath(evidence.get("approval_path") or "") != approval_path:
        raise ValueError("VIDEO_FUSION_STORYBOARD_APPROVAL_MISMATCH")
    approval = _require_customer_approved_previs(approval_path)
    if (str(approval.get("client") or "") != str(client) or
            str(approval.get("run_id") or "") != str(manifest.get("run_id") or "")):
        raise ValueError("VIDEO_FUSION_STORYBOARD_APPROVAL_MISMATCH")
    video_generation = (manifest.get("generation") or {}).get("video") or {}
    if (video_generation.get("status") != "pending_approval" or
            manifest.get("approvals", {}).get("video")):
        raise ValueError("VIDEO_FUSION_REQUIRES_PENDING_VIDEO")
    run_manifest_store.validate_video_closure(manifest)
    video_artifact = manifest.get("video_artifact") or {}
    human_record = run_manifest_store.file_record(human_video)
    if human_record != video_artifact.get("basecut"):
        raise ValueError("VIDEO_FUSION_HUMAN_MUST_MATCH_VIDEO_BASECUT")
    if os.path.abspath(human_video) == os.path.abspath(out_path):
        raise ValueError("VIDEO_FUSION_OUTPUT_MUST_NOT_OVERWRITE_SOURCE")
    plan_record = run_manifest_store.file_record(plan_path)
    animation_record = run_manifest_store.file_record(animation_video)
    motion_record = (run_manifest_store.file_record(motion_video)
                     if motion_video else None)
    approval_record = run_manifest_store.file_record(approval_path)
    if (not plan_record or not plan_record.get("exists") or
            not animation_record or not animation_record.get("exists") or
            any(not record or not record.get("exists")
                for record in (motion_record, approval_record) if record is not None)):
        raise ValueError("VIDEO_FUSION_INPUT_MISSING")
    content_spec_path = (_content_spec_for_fusion(fusion_plan, animation_video)
                         if fusion_plan.get("content_render_required") else None)
    content_spec = (run_manifest_store.file_record(content_spec_path)
                    if content_spec_path else None)
    if fusion_plan.get("content_render_required") and (
            not content_spec or not content_spec.get("exists")):
        raise ValueError("VIDEO_FUSION_CONTENT_SPEC_REQUIRED")
    planned = run_manifest_store.require_run_output(
        manifest, out_path, label="video_fusion_output")
    render_manifest_path = os.path.splitext(planned)[0] + ".integrated.json"
    qc_path = planned + ".qc.json"
    run_manifest_store.require_run_output(
        manifest, render_manifest_path, label="video_fusion_manifest")
    run_manifest_store.require_run_output(
        manifest, qc_path, label="video_fusion_qc")
    run_manifest_store.invalidate_from(manifest, "video")
    manifest.setdefault("generation", {}).setdefault("video", {}).update({
        "status": "running",
        "operation": "fusion",
        "planned_output": planned,
    })
    manifest["status"] = "generating_video"
    manifest["video_fusion"] = {
        "status": "running",
        "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "plan": plan_record,
        "animation": animation_record,
        "content_spec": content_spec,
        "human": human_record,
        "motion": motion_record,
        "approval_record": approval_record,
        "planned_output": planned,
        "planned_render_manifest": render_manifest_path,
        "planned_qc": qc_path,
        "render_quality": "high",
    }
    run_manifest_store.save_manifest(manifest, manifest_path)
    render_fusion(
        fusion_plan, animation_video, human_video, planned,
        motion_video=motion_video, approved_previs_spec=approval_path, formal=True)
    transaction, final_animation_record = _formal_fusion_transaction_current(
        manifest, plan_path=plan_path, fusion_plan=fusion_plan,
        animation_video=animation_video, human_video=human_video,
        motion_video=motion_video, approved_previs_spec=approval_path,
        out_path=planned)
    render_manifest_record = run_manifest_store.file_record(render_manifest_path)
    if not render_manifest_record or not render_manifest_record.get("exists"):
        raise ValueError("VIDEO_FUSION_RENDER_MANIFEST_REQUIRED")
    render_record = _load_json(render_manifest_path)
    binding = render_record.get("approval_binding") or {}
    if (render_record.get("formal") is not True or
            render_record.get("out") != planned or
            binding.get("customer_approved") is not True or
            binding.get("approval_record_path") != approval_path or
            binding.get("approval_record_sha256") != approval_record.get("sha256")):
        raise ValueError("VIDEO_FUSION_RENDER_BINDING_INVALID")
    qc = media_qc.check(
        planned, profile="formal", expected_ratio=fusion_plan.get("output_ratio"),
        audio_required=True, report_path=qc_path)
    media_qc.require_pass(qc)
    _formal_fusion_transaction_current(
        manifest, plan_path=plan_path, fusion_plan=fusion_plan,
        animation_video=animation_video, human_video=human_video,
        motion_video=motion_video, approved_previs_spec=approval_path,
        out_path=planned)
    output_record = run_manifest_store.file_record(planned)
    if ((qc.get("file") or {}).get("path") != output_record.get("path") or
            (qc.get("file") or {}).get("sha256") != output_record.get("sha256")):
        raise ValueError("VIDEO_FUSION_QC_IDENTITY_MISMATCH")
    source_basecut = video_artifact["basecut"]
    artifacts = [video_artifact[name]["path"]
                 for name in ("segments", "results", "reviews")]
    artifacts.extend([source_basecut["path"], planned, render_manifest_path, qc_path])
    started_at = video_generation.get("started_at")
    run_manifest_store.mark_video_generation_finished(manifest, artifacts)
    if started_at:
        manifest["generation"]["video"]["started_at"] = started_at
    manifest["video_artifact"] = dict(
        video_artifact, basecut=output_record, source_basecut=source_basecut,
        source_assembly_qc=video_artifact.get("assembly_qc"),
        source_assembly_qc_file=video_artifact.get("assembly_qc_file"),
        assembly_qc=qc, assembly_qc_file=run_manifest_store.file_record(qc_path),
        fusion_manifest=render_manifest_record, fusion_qc=qc)
    manifest["video_fusion"] = dict(
        transaction, status="pending_video_approval",
        finished_at=_dt.datetime.now().isoformat(timespec="seconds"),
        animation=final_animation_record, output=output_record,
        render_manifest=render_manifest_record, media_qc=qc)
    run_manifest_store.save_manifest(manifest, manifest_path)
    return manifest["video_fusion"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="video-use 风格讲解型剪辑桥接器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="导演计划 → project.md/takes_packed/EDL/fusion_plan")
    p.add_argument("--director-plan", required=True)
    p.add_argument("--out-dir")
    p.add_argument("--layout", choices=sorted(LAYOUT_ALIASES.keys()))
    p.add_argument("--split-ratio", type=float, dest="split_ratio")
    p.add_argument("--gap", type=int)
    p.add_argument("--swap", action="store_true")
    p.add_argument("--animation-video")
    p.add_argument("--human-video")
    p.add_argument(
        "--composition-mode",
        choices=["scene_embedded", "legacy_layout"],
        help="人物与动画的合成模式；文档讲解默认 scene_embedded")

    r = sub.add_parser("render", help="用 fusion_plan 融合已渲染的动画视频与数字人视频")
    r.add_argument("--plan", required=True, help="fusion_plan.json")
    r.add_argument("--animation", required=True, help="HyperFrames/Remotion 动画视频")
    r.add_argument("--human", required=True, help="数字人讲解视频")
    r.add_argument("--out", required=True)
    r.add_argument("--motion", help="HyperFrames 透明 MOV/WebM 动效层")
    r.add_argument("--approved-previs-spec",
                   help="审批记录；--formal 时必须是客户已确认的 design_board v2 approval JSON")
    r.add_argument("--formal", action="store_true",
                   help="正式合成：强制绑定审批规格并校验动效 alpha 与时长")
    r.add_argument("--draft", action="store_true",
                   help="仅内部诊断；不绑定 manifest、不可进入正式交付")
    r.add_argument("--client", help="正式合成所属客户")
    r.add_argument("--manifest", help="正式合成所属 run_manifest.json")

    rs = sub.add_parser(
        "render-segmented",
        help="按 EDL 逐镜头切换版式融合动画视频与数字人视频")
    rs.add_argument("--plan", required=True, help="fusion_plan.json 或 director_plan.json")
    rs.add_argument("--animation", required=True, help="HyperFrames/Remotion 内容动效视频")
    rs.add_argument("--human", required=True, help="数字人讲解视频")
    rs.add_argument("--out", required=True)
    rs.add_argument("--work-dir", help="分段临时素材目录")
    rs.add_argument(
        "--human-focus",
        default="center_speaker",
        choices=["none", "center_speaker", "right_speaker", "left_speaker"],
        help="数字人素材聚焦裁切；center_speaker 可缓解生成视频开头双人脸")
    rs.add_argument("--draft", action="store_true",
                    help="分段低层渲染仅限诊断；正式文档讲解使用 render --formal")

    args = parser.parse_args(argv)
    if args.cmd == "plan":
        plan = _load_json(args.director_plan)
        fusion_plan = build_fusion_plan(
            plan, source_plan=args.director_plan,
            animation_video=args.animation_video, human_video=args.human_video,
            layout=args.layout, split_ratio=args.split_ratio, gap=args.gap,
            swap=True if args.swap else None,
            composition_mode=args.composition_mode)
        written = write_plan_files(plan, fusion_plan, args.out_dir)
        print(json.dumps({
            "ok": True,
            "edit_dir": os.path.dirname(written["files"]["fusion_plan"]),
            "files": written["files"],
            "fusion": written["fusion"],
            "shots": len((written.get("edl") or {}).get("shots") or []),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "render":
        if args.formal == args.draft:
            parser.error("render 必须二选一：正式使用 --formal，内部诊断使用 --draft")
        if args.formal and not (args.client and args.manifest and args.approved_previs_spec):
            parser.error("正式 render 必须提供 --client/--manifest/--approved-previs-spec")
        raw_plan = _load_json(args.plan)
        fusion_plan = (raw_plan if isinstance(raw_plan, dict)
                       and raw_plan.get("fusion") and raw_plan.get("edl")
                       else build_fusion_plan(raw_plan, source_plan=args.plan))
        if args.formal:
            manifest = run_manifest_store.load_manifest(args.manifest)
            result = render_formal_fusion_and_record(
                manifest, args.manifest, client=args.client, plan_path=args.plan,
                fusion_plan=fusion_plan, animation_video=args.animation,
                human_video=args.human, out_path=args.out,
                motion_video=args.motion,
                approved_previs_spec=args.approved_previs_spec)
            out = result["output"]["path"]
        else:
            out = render_fusion(
                fusion_plan, args.animation, args.human, args.out,
                motion_video=args.motion,
                approved_previs_spec=args.approved_previs_spec,
                formal=False)
        print(json.dumps({"ok": True, "out": os.path.abspath(out)}, ensure_ascii=False))
        return 0
    if args.cmd == "render-segmented":
        if not args.draft:
            parser.error("render-segmented 是低层诊断入口，必须显式 --draft；正式合成使用 render --formal")
        raw_plan = _load_json(args.plan)
        fusion_plan = (raw_plan if isinstance(raw_plan, dict)
                       and raw_plan.get("fusion") and raw_plan.get("edl")
                       else build_fusion_plan(raw_plan, source_plan=args.plan))
        render_segmented_fusion(
            fusion_plan, args.animation, args.human, args.out,
            work_dir=args.work_dir, human_focus=args.human_focus)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
