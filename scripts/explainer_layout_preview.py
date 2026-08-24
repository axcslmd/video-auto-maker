#!/usr/bin/env python3
"""Generate a deterministic visual-layout preview for explainer videos.

The six explainer visual modes already drive `video_use_bridge.py` and the
Remotion IntegratedExplainer composition. This script turns that contract into
a portable preview artifact so Agents do not depend on host-specific widgets.
"""
import argparse
import copy
import datetime as _dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import video_use_bridge as bridge  # noqa: E402
import hf_engine  # noqa: E402
import motion_design  # noqa: E402
import run_manifest as run_manifest_store  # noqa: E402
import visible_layer_contract  # noqa: E402
import cast_reference_pack  # noqa: E402
from project_utils import atomic_write_bytes, atomic_write_json  # noqa: E402


MODE_ORDER = (
    "host_full_bleed_text",
    "split_left_right",
    "content_full_bleed",
    "stacked_top_bottom",
    "cutout_presenter",
    "corner_pip",
)


MODE_CATALOG = {
    "host_full_bleed_text": {
        "name": "讲师主讲",
        "short_name": "讲师主讲",
        "use_when": "开场、定义问题、情绪转折",
        "layout_contract": "讲师或场景占满主画面，标题和关键词由 HyperFrames 后期叠加。",
        "pip_like": False,
        "needs_external_fusion": False,
    },
    "split_left_right": {
        "name": "左右分屏",
        "short_name": "左右分屏",
        "use_when": "架构、对比、方案定位",
        "layout_contract": "内容一侧、讲师一侧，二者都保持可读可看。",
        "pip_like": False,
        "needs_external_fusion": False,
    },
    "content_full_bleed": {
        "name": "全屏内容",
        "short_name": "全屏内容",
        "use_when": "复杂图谱、定位句、指标冲击段",
        "layout_contract": "内容动效铺满画面，讲师可短时退场或作为旁白承接。",
        "pip_like": False,
        "needs_external_fusion": False,
    },
    "stacked_top_bottom": {
        "name": "上下分屏",
        "short_name": "上下分屏",
        "use_when": "流程、时间线、步骤演进",
        "layout_contract": "上方内容、下方讲师或反过来；这是方正分区，不是角落圆形小窗。",
        "pip_like": False,
        "needs_external_fusion": False,
    },
    "cutout_presenter": {
        "name": "前景讲师收束",
        "short_name": "前景讲师",
        "use_when": "结尾、核心亮点、CTA",
        "layout_contract": "人物以前景压上内容动效；抠图或人景融合必须走外部模型或透明素材。",
        "pip_like": False,
        "needs_external_fusion": True,
    },
    "corner_pip": {
        "name": "角落画中画",
        "short_name": "角落小窗",
        "use_when": "PPT/图表很密，必须完整展示原页",
        "layout_contract": "主视觉完整保留，讲师只在角落小窗；不作为全片默认版式。",
        "pip_like": True,
        "needs_external_fusion": False,
    },
}


COLORS = {
    "ink": "#1F2937",
    "muted": "#64748B",
    "line": "#CBD5E1",
    "surface": "#FFFFFF",
    "canvas": "#F8FAFC",
    "host": "#DCEBFF",
    "host_dark": "#2563EB",
    "content": "#EEF2FF",
    "content_dark": "#4F46E5",
    "process": "#EAF7E7",
    "process_dark": "#2F7D32",
    "accent": "#FFF3D6",
    "accent_dark": "#B45309",
    "warn": "#FDE9E2",
    "warn_dark": "#C2410C",
}


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_text(path, text):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes(path, text.encode("utf-8"), label="preview_text")
    return path


def _write_json(path, value):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, value)
    return path


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _nearest_existing_directory(path):
    candidate = os.path.abspath(path)
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return os.path.abspath(os.sep)
        candidate = parent
    return candidate if os.path.isdir(candidate) else os.path.dirname(candidate)


def _preview_resource_plan(plan, out_dir, quality):
    """Estimate wall time and disk before any formal renderer is started."""
    width, height = bridge._output_dimensions(plan)
    def shot_duration(shot):
        raw = (shot.get("duration") or shot.get("duration_sec") or
               shot.get("seconds") or 5)
        try:
            return max(0.1, float(raw))
        except (TypeError, ValueError):
            raise ValueError(
                "EXECUTION_PREVIEW_DURATION_INVALID: shot %s duration=%r" %
                (shot.get("id") or "unknown", raw))
    duration = sum(shot_duration(shot) for shot in _plan_shots(plan))
    pixel_factor = max(0.25, (width * height) / float(1920 * 1080))
    quality_factor = {"draft": 0.45, "standard": 0.72, "high": 1.0}[quality]
    # ProRes alpha + VP9 interchange + content/Animatic/cache.  The estimate is
    # intentionally conservative because evidence must not be deleted before
    # approval merely to rescue a nearly full disk.
    estimated_bytes = int(max(
        1024 ** 3,
        duration * 72 * 1024 ** 2 * pixel_factor * quality_factor))
    required_free = int(estimated_bytes * 1.25)
    probe_dir = _nearest_existing_directory(out_dir)
    free_bytes = shutil.disk_usage(probe_dir).free
    if free_bytes < required_free:
        raise ValueError(
            "EXECUTION_PREVIEW_DISK_SPACE_REQUIRED: 预计需要 %.1f GB，当前可用 %.1f GB；"
            "请先释放可重建缓存空间，审批证据不得删除。" %
            (required_free / 1024 ** 3, free_bytes / 1024 ** 3))
    minimum_minutes = max(2, int(round(duration * 0.18)))
    maximum_minutes = max(minimum_minutes + 2, int(round(duration * 0.34)))
    return {
        "duration_seconds": round(duration, 3),
        "estimated_intermediate_bytes": estimated_bytes,
        "required_free_bytes": required_free,
        "free_bytes_at_start": free_bytes,
        "estimated_wall_time_minutes": [minimum_minutes, maximum_minutes],
        "retention_policy": {
            "preserve_until_customer_decision": [
                "layout_animatic", "layout_boards", "final_frame_visual_qa",
                "commercial_qc", "approved_render_contract"],
            "recomputable_after_approval": [
                "motion_master", "browser_alpha_interchange", "render_cache"],
        },
    }


def _local_file_dependencies(value):
    """Return content records for local media referenced by a render spec."""
    found = {}

    def visit(item):
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            candidates = [item]
            if not os.path.isabs(item):
                candidates.extend([
                    os.path.join(os.getcwd(), item),
                    os.path.join(os.path.dirname(HERE), item),
                ])
            for candidate in candidates:
                path = os.path.abspath(candidate)
                if os.path.isfile(path):
                    found[path] = {
                        "path": path,
                        "sha256": _sha256_file(path),
                        "size": os.path.getsize(path),
                    }
                    break

    visit(value)
    return [found[path] for path in sorted(found)]


def _source_tree_fingerprint(paths):
    digest = hashlib.sha256()
    files = []
    for raw_path in paths:
        if not raw_path:
            continue
        path = os.path.abspath(raw_path)
        if os.path.isfile(path):
            files.append(path)
        elif os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(name for name in dirs if name not in {
                    "node_modules", "__pycache__", ".git"})
                for name in sorted(names):
                    if name.endswith((".py", ".ts", ".tsx", ".js", ".json")):
                        files.append(os.path.join(root, name))
    for path in sorted(set(files)):
        digest.update(path.encode("utf-8"))
        digest.update(_sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def _execution_preview_code_sha():
    """Bind a formal preview to the exact orchestration + renderer sources."""
    project_root = os.path.dirname(HERE)
    return _source_tree_fingerprint([
        __file__,
        os.path.join(HERE, "video_use_bridge.py"),
        os.path.join(HERE, "hf_engine.py"),
        os.path.join(HERE, "motion_design.py"),
        os.path.join(HERE, "visible_layer_contract.py"),
        os.path.join(HERE, "cast_reference_pack.py"),
        os.path.join(HERE, "explainer_commercial_qc.py"),
        os.path.join(project_root, "remotion_engine", "src"),
        os.path.join(project_root, "remotion_engine", "package-lock.json"),
    ])


def _render_fingerprint(spec, *, quality, renderer, composition,
                        renderer_source_sha, extra=None):
    payload = {
        "schema_version": 1,
        "quality": quality,
        "renderer": renderer,
        "composition": composition,
        "renderer_source_sha256": renderer_source_sha,
        "spec": spec,
        "local_dependencies": _local_file_dependencies(spec),
        "extra": extra or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_cache_is_current(output_path, fingerprint):
    cache_path = output_path + ".render-cache.json"
    if not os.path.isfile(output_path) or not os.path.isfile(cache_path):
        return False
    try:
        cache = _load_json(cache_path)
        return bool(
            cache.get("fingerprint") == fingerprint and
            cache.get("output") == {
                "path": os.path.abspath(output_path),
                "sha256": _sha256_file(output_path),
                "size": os.path.getsize(output_path),
            })
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _run_cached_render(label, output_path, fingerprint, callback):
    output_path = os.path.abspath(output_path)
    if _render_cache_is_current(output_path, fingerprint):
        print("[execution-preview] %s：缓存命中，跳过重复渲染" % label, flush=True)
        return True
    print("[execution-preview] %s：开始渲染" % label, flush=True)
    started = time.monotonic()
    callback()
    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise ValueError("EXECUTION_PREVIEW_RENDER_OUTPUT_MISSING: %s" % output_path)
    _write_json(output_path + ".render-cache.json", {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "output": {
            "path": output_path,
            "sha256": _sha256_file(output_path),
            "size": os.path.getsize(output_path),
        },
    })
    print("[execution-preview] %s：完成（%.1f 秒）" % (
        label, time.monotonic() - started), flush=True)
    return False


def _confirmed_cast_board_from_manifest(manifest):
    """Resolve the exact customer-confirmed cast board, never a loose file."""
    if not isinstance(manifest, dict):
        return None
    if not run_manifest_store.stage_required(manifest, "cast_board"):
        return None
    if not run_manifest_store.approval_is_current(manifest, "cast_board"):
        raise ValueError("EXECUTION_PREVIEW_CONFIRMED_CAST_STALE")
    evidence = ((manifest.get("approval_evidence") or {}).get("cast_board") or {})
    marker_path = evidence.get("approval_path")
    if not marker_path or not os.path.isfile(os.path.abspath(str(marker_path))):
        raise ValueError("EXECUTION_PREVIEW_CAST_APPROVAL_MARKER_MISSING")
    marker = _load_json(os.path.abspath(str(marker_path)))
    board_path = marker.get("path") if isinstance(marker, dict) else None
    if not board_path or not os.path.isfile(os.path.abspath(str(board_path))):
        raise ValueError("EXECUTION_PREVIEW_CONFIRMED_CAST_MISSING")
    board_path = os.path.abspath(str(board_path))
    actual_sha = _sha256_file(board_path)
    approved_sha = str(evidence.get("artifact_sha256") or marker.get("board_sha256") or "")
    if (not approved_sha or actual_sha != approved_sha or
            marker.get("confirmed_by_customer") is not True):
        raise ValueError("EXECUTION_PREVIEW_CONFIRMED_CAST_HASH_MISMATCH")
    return board_path


def _prepare_human_preview_image(human_image, out_dir):
    """Return a single-view presenter still and an auditable source contract.

    A confirmed six-view cast board is an identity reference sheet, never a
    presenter plate.  For deterministic previsualisation only, crop its first
    (front full-body) cell locally.  Formal generation still requires an
    external type4/5 avatar plate and cannot reuse this preview derivative.
    """
    if not human_image:
        return None, {
            "kind": "neutral_placeholder",
            "approved_identity_media": False,
            "formal_video_input_allowed": False,
        }
    source_path = os.path.abspath(human_image)
    if not os.path.isfile(source_path):
        raise ValueError("EXECUTION_PREVIEW_HUMAN_IMAGE_MISSING: %s" % source_path)
    source_record = {
        "source_path": source_path,
        "source_sha256": _sha256_file(source_path),
        "formal_video_input_allowed": False,
    }
    basename = os.path.basename(source_path).lower()
    is_cast_board = any(token in basename for token in (
        "cast_board", "character_board", "six_view", "6_view"))
    if not is_cast_board:
        source_record.update({
            "kind": "single_view_identity_still",
            "derived": False,
            "approved_identity_media": True,
            "preview_path": source_path,
            "preview_sha256": source_record["source_sha256"],
        })
        return source_path, source_record

    try:
        from PIL import Image
        reference_pack = cast_reference_pack.derive(
            source_path, os.path.join(out_dir, "cast_reference_pack"))
        front_view = (reference_pack.get("views") or {}).get("full_body_front") or {}
        derived_path = os.path.abspath(str(front_view.get("path") or ""))
        crop_box = front_view.get("crop_box") or []
        if not os.path.isfile(derived_path):
            raise ValueError("full_body_front derivative missing")
        with Image.open(derived_path) as derived:
            derived = derived.convert("RGB")
            preview_width, preview_height = derived.size
            upper_body_crop_box = (
                int(round(preview_width * 0.20)),
                int(round(preview_height * 0.02)),
                int(round(preview_width * 0.80)),
                int(round(preview_height * 0.70)),
            )
            stacked_derived = derived.crop(upper_body_crop_box)
            stacked_path = os.path.join(
                out_dir, "human_preview_stacked_upper_body.jpg")
            stacked_derived.save(
                stacked_path, format="JPEG", quality=95, optimize=True)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "EXECUTION_PREVIEW_CAST_BOARD_CROP_FAILED: %s" % exc) from exc

    source_record.update({
        "kind": "cast_board_front_cell_derivative",
        "board_type": "digital_human_6_view",
        "derived": True,
        "approved_identity_media": True,
        "grid": {"columns": 3, "rows": 2, "selected_panel": 1},
        "crop_box": list(crop_box),
        "preview_path": os.path.abspath(derived_path),
        "preview_sha256": _sha256_file(derived_path),
        "reference_pack": {
            "manifest_path": os.path.join(
                out_dir, "cast_reference_pack", "cast_reference_pack.json"),
            "primary_identity_anchor": reference_pack.get("primary_identity_anchor"),
            "full_body_identity_anchor": reference_pack.get("full_body_identity_anchor"),
            "source_board_sha256": (reference_pack.get("source_board") or {}).get("sha256"),
        },
        "stacked_upper_body_derivative": {
            "path": os.path.abspath(stacked_path),
            "sha256": _sha256_file(stacked_path),
            "crop_box_relative_to_front_cell": list(upper_body_crop_box),
            "usage": "deterministic_preview_only_stacked_top_bottom",
            "formal_video_input_allowed": False,
        },
        "usage": "deterministic_preview_only",
    })
    return os.path.abspath(derived_path), source_record


def _text(value, limit=None):
    if value is None:
        return ""
    result = str(value).strip()
    if limit and len(result) > limit:
        return result[:limit].rstrip() + "..."
    return result


def _safe(value):
    return html.escape(_text(value), quote=True)


def build_layout_catalog():
    return [
        {"id": mode, **MODE_CATALOG[mode], "hint": bridge._shot_layout_hint(mode)}
        for mode in MODE_ORDER
    ]


def _plan_shots(plan):
    if not isinstance(plan, dict):
        return []
    for key in ("shots", "segments", "scenes"):
        value = plan.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _shot_title(shot, index):
    for key in ("title", "name", "heading", "content_title", "id"):
        if shot.get(key):
            return _text(shot.get(key), 28)
    return "S%d" % index


def _shot_summary(shot):
    for key in ("visual", "content_design", "animation_hint", "scene_prompt", "dialogue", "text"):
        if shot.get(key):
            return _text(shot.get(key), 58)
    return ""


def _fusion_content_spec(fusion):
    candidates = [
        (fusion or {}).get("content_spec_path"),
        ((fusion or {}).get("files") or {}).get("content_spec"),
    ]
    for value in candidates:
        if value and os.path.isfile(os.path.abspath(str(value))):
            loaded = _load_json(os.path.abspath(str(value)))
            if isinstance(loaded, dict):
                return loaded
    embedded = (fusion or {}).get("content_spec")
    return embedded if isinstance(embedded, dict) else {}


def build_hyperframes_timeline_spec(fusion):
    """Compile every shot's structured motion plan onto the approved EDL."""
    edl = fusion.get("edl") if isinstance(fusion, dict) else {}
    width, height = bridge._output_dimensions(fusion)
    fps = int(fusion.get("fps") or 30)
    scenes = []
    grammars = []
    deduplicated = []
    for index, shot in enumerate((edl or {}).get("shots") or [], 1):
        motion_plan = shot.get("motion_plan") if isinstance(shot, dict) else None
        if not isinstance(motion_plan, dict) or not motion_plan:
            continue
        # ``motion_elements`` are choreography/director instructions, never
        # audience copy.  Legacy adapters used to infer bullet-list overlays
        # from those strings, so a complete modern plan could still inherit
        # production notes such as “保持留白” as visible cards.  Bind the
        # compiler to the semantic contract here as a final safety boundary.
        production_note_copy = {
            re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)
            for value in (shot.get("motion_elements") or [])
            if str(value or "").strip()
        }
        start = float(shot.get("start_sec") or 0)
        end = float(shot.get("end_sec") or (start + float(shot.get("duration") or 0)))
        compiled = hf_engine.compile_motion_plan_to_spec(
            motion_plan,
            resolution=[width, height], fps=fps,
            brand={"primary": fusion.get("brandPrimary") or "#64748B"},
            background={"type": "transparent"},
            start=start, end=end)
        grammar = dict(compiled.get("motion_grammar") or {})
        grammar["source_shot_id"] = shot.get("id") or "shot_%02d" % index
        grammars.append(grammar)
        seen_copy = {}
        lower_lane = 0
        shot_scenes = []
        for scene in compiled.get("scenes") or []:
            item = dict(scene)
            item["source_shot_id"] = shot.get("id") or "shot_%02d" % index
            normalized_copy = re.sub(
                r"[\W_]+", "", str(item.get("text") or "").lower(), flags=re.UNICODE)
            if normalized_copy and normalized_copy in production_note_copy:
                deduplicated.append({
                    "source_shot_id": item["source_shot_id"],
                    "slot": item.get("slot"), "text": item.get("text"),
                    "reason": "production_direction_is_not_audience_copy",
                })
                continue
            if normalized_copy:
                previous_index = seen_copy.get(normalized_copy)
                previous = shot_scenes[previous_index] if previous_index is not None else None
                # Spoken captions are an accessibility requirement.  If a
                # title/card repeats the same copy, retain the subtitle and
                # drop the decorative duplicate rather than silently removing
                # the only caption for that spoken shot.
                if previous is not None:
                    if item.get("slot") == "subtitle" and previous.get("slot") != "subtitle":
                        deduplicated.append({
                            "source_shot_id": item["source_shot_id"],
                            "slot": previous.get("slot"), "text": previous.get("text"),
                            "reason": "decorative_duplicate_replaced_by_required_subtitle",
                        })
                        shot_scenes[previous_index] = None
                    else:
                        deduplicated.append({
                            "source_shot_id": item["source_shot_id"],
                            "slot": item.get("slot"), "text": item.get("text"),
                            "reason": "duplicate_visible_copy_in_same_shot",
                        })
                        continue
                seen_copy[normalized_copy] = len(shot_scenes)
            # HyperFrames lint treats many elements on one track as a dense
            # timeline even when separate shots never overlap. Namespace
            # authored lanes per shot while preserving within-shot ordering.
            item["track_index"] = int(item.get("track_index") or 1) + (index - 1) * 20
            # Track index prevents HyperFrames lint overlap, but CSS positions
            # still collide unless each visible lane has distinct geometry.
            # Spoken subtitles own the bottom safe zone; any other lower-third
            # graphic is raised above it in deterministic 96px lanes.
            if item.get("slot") == "subtitle":
                stacked = shot.get("visual_mode") == "stacked_top_bottom"
                width_ratio = 0.60 if stacked else 0.64
                caption_width = min(
                    max(680, int(item.get("width_px") or width * width_ratio)),
                    max(680, width - 144))
                caption_left = max(0, int(round((width - caption_width) / 2.0)))
                item.update({
                    "bottom_px": (int(height * 0.44) if stacked else
                                  max(48, int(height * 0.06))),
                    "width_px": caption_width,
                    "left_px": caption_left,
                    "right_px": width - caption_left - caption_width,
                    "max_height_px": (max(96, int(height * 0.12)) if stacked
                                      else max(120, int(height * 0.18))),
                    "layout_region": "subtitle_reserved",
                })
            elif item.get("slot") == "title_area":
                title_width = min(820, max(520, int(item.get("width_px") or 760)))
                title_width = min(title_width, max(520, int(width * 0.48)))
                visual_mode = str(shot.get("visual_mode") or "")
                human_slot = str(shot.get("slot") or "")
                if visual_mode == "split_left_right":
                    title_left = (
                        width - max(72, int(width * 0.05)) - title_width
                        if human_slot == "left" else max(72, int(width * 0.05)))
                elif visual_mode in {"host_full_bleed_text", "cutout_presenter"}:
                    title_left = max(72, int(width * 0.08))
                else:
                    title_left = max(0, int(round((width - title_width) / 2.0)))
                item.update({
                    "variant": "title",
                    "size": min(44, int(item.get("size") or 44)),
                    "top_px": max(52, int(height * 0.055)),
                    "width_px": title_width,
                    "left_px": title_left,
                    "right_px": width - title_left - title_width,
                    "max_height_px": max(96, int(height * 0.12)),
                })
            elif item.get("safe_zone") == "upper_third":
                # Upper-third cards must remain a compact header lane.  The
                # legacy full-width default crossed the presenter head and
                # obscured content cards in host-led 16:9 layouts.
                item.pop("right_px", None)
                item.update({
                    "variant": "feature",
                    "size": min(44, int(item.get("size") or 44)),
                    "top_px": max(52, int(height * 0.055)),
                    "left_px": max(72, int(width * 0.045)),
                    "width_px": min(760, max(520, int(width * 0.40))),
                    "max_height_px": max(96, int(height * 0.12)),
                    "layout_region": "upper_left_graphics",
                })
            elif item.get("safe_zone") == "right":
                # Presenter plates occupy the centre/right body region.  Keep
                # right-side audience cards above that plate instead of at
                # torso/face height; this also preserves the content surface.
                item.pop("left_px", None)
                item.update({
                    "variant": "feature",
                    "size": min(44, int(item.get("size") or 44)),
                    "top_px": max(52, int(height * 0.055)),
                    "right_px": max(72, int(width * 0.045)),
                    "width_px": min(520, max(420, int(width * 0.25))),
                    "max_height_px": max(96, int(height * 0.12)),
                    "layout_region": "upper_right_graphics",
                })
            elif item.get("pos") == "lower":
                stacked = shot.get("visual_mode") == "stacked_top_bottom"
                item.update({
                    "bottom_px": (
                        int(height * 0.60) + lower_lane * 82 if stacked else
                        max(190, int(height * 0.22)) + lower_lane * 96),
                    "left_px": max(84, int(width * 0.10)),
                    "right_px": max(84, int(width * 0.10)),
                    "layout_region": "content" if stacked else "lower_graphics",
                })
                lower_lane += 1
            shot_scenes.append(item)
        scenes.extend(item for item in shot_scenes if isinstance(item, dict))
    content_spec = _fusion_content_spec(fusion)
    scenes, ownership_suppressed = visible_layer_contract.resolve_hyperframes_ownership(
        scenes, content_spec)
    deduplicated.extend(ownership_suppressed)
    spec = {
        "schema_version": 1,
        "type": "document_explainer_hyperframes_timeline",
        "resolution": [width, height],
        "fps": fps,
        "duration": round(float((edl or {}).get("total_duration") or 0), 3),
        "background": {"type": "transparent"},
        "brand": {"primary": fusion.get("brandPrimary") or "#64748B"},
        "semantic_compiler": "hf_engine.semantic_motion_compiler",
        "motion_grammars": grammars,
        "deduplicated_scenes": deduplicated,
        "scenes": scenes,
    }
    visibility_audit = visible_layer_contract.audit(spec, fusion, content_spec)
    spec["visible_layer_contract"] = visibility_audit
    if not visibility_audit.get("ok"):
        codes = sorted({str(item.get("code")) for item in visibility_audit.get("errors") or []})
        raise ValueError("VISIBLE_LAYER_PREFLIGHT_FAILED: %s" % ",".join(codes))
    return spec


def transcode_browser_alpha(master_mov, browser_webm):
    """Convert verified ProRes alpha into Chromium-decodable VP9 alpha."""
    browser_webm = os.path.abspath(browser_webm)
    temporary = browser_webm + ".tmp.webm"
    if os.path.exists(temporary):
        os.remove(temporary)
    command = [
        bridge._ffmpeg(), "-hide_banner", "-y", "-i", os.path.abspath(master_mov),
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "18",
        "-pix_fmt", "yuva420p", "-row-mt", "1", "-an", temporary,
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0 or not os.path.isfile(temporary):
        raise RuntimeError("BROWSER_ALPHA_TRANSCODE_FAILED: %s" % result.stdout[-1200:])
    if not bridge._media_has_alpha(temporary):
        os.remove(temporary)
        raise RuntimeError("BROWSER_ALPHA_VERIFY_FAILED")
    os.replace(temporary, browser_webm)
    return browser_webm


def build_sequence(plan):
    shots = _plan_shots(plan)
    document_explainer = bridge._is_document_explainer_plan(plan or {})
    sequence = []
    for index, shot in enumerate(shots, start=1):
        explicit = (
            shot.get("visual_mode") or shot.get("layout_mode") or shot.get("shot_layout") or
            shot.get("presentation_mode") or shot.get("fusion_mode") or shot.get("human_layout")
        )
        mode = bridge._normalize_visual_mode(explicit)
        inferred = False
        if not mode:
            mode = bridge._infer_visual_mode(
                index, shot,
                document_explainer=document_explainer,
                is_last=index == len(shots))
            inferred = True
        catalog = MODE_CATALOG.get(mode, MODE_CATALOG["split_left_right"])
        sequence.append({
            "index": index,
            "shot_id": _text(shot.get("id") or shot.get("shot_id") or "s%02d" % index),
            "title": _shot_title(shot, index),
            "summary": _shot_summary(shot),
            "visual_mode": mode,
            "visual_mode_name": catalog["name"],
            "inferred": inferred,
            "duration": shot.get("duration") or shot.get("duration_sec") or shot.get("seconds"),
            "layout_contract": catalog["layout_contract"],
            "hint": bridge._shot_layout_hint(mode),
        })
    return sequence


def _sample_sequence():
    return [
        {
            "index": index,
            "shot_id": "sample_%02d" % index,
            "title": MODE_CATALOG[mode]["short_name"],
            "summary": MODE_CATALOG[mode]["use_when"],
            "visual_mode": mode,
            "visual_mode_name": MODE_CATALOG[mode]["name"],
            "inferred": False,
            "duration": None,
            "layout_contract": MODE_CATALOG[mode]["layout_contract"],
            "hint": bridge._shot_layout_hint(mode),
        }
        for index, mode in enumerate(MODE_ORDER, start=1)
    ]


def build_preview_payload(plan=None, *, client=None, run_id=None, title=None):
    plan = plan if isinstance(plan, dict) else {}
    sequence = build_sequence(plan) or _sample_sequence()
    return {
        "ok": True,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "client": _text(client or plan.get("client") or "unknown"),
        "run_id": _text(run_id or plan.get("run_id") or plan.get("id") or "layout-preview"),
        "title": _text(title or plan.get("title") or plan.get("project_title") or "讲解视频视觉版式预览"),
        "mode_catalog": build_layout_catalog(),
        "sequence": sequence,
        "acceptance": {
            "stacked_top_bottom_is_not_corner_pip": True,
            "corner_pip_is_fallback_not_default": True,
            "text_and_labels_owner": "HyperFrames/content layer, not generated video background",
            "local_matte_model_allowed": False,
        },
    }


def _panel_svg(mode, x, y, w, h):
    c = COLORS
    screen_x = x + 22
    screen_y = y + 72
    screen_w = w - 44
    screen_h = 130
    title = MODE_CATALOG[mode]["short_name"]
    subtitle = mode
    parts = [
        '<g>',
        '<rect x="%d" y="%d" width="%d" height="%d" rx="12" fill="%s" stroke="%s"/>' %
        (x, y, w, h, c["surface"], c["line"]),
        '<text x="%d" y="%d" fill="%s" font-size="18" font-weight="600">%s</text>' %
        (x + 22, y + 32, c["ink"], _safe(title)),
        '<text x="%d" y="%d" fill="%s" font-size="12">%s</text>' %
        (x + 22, y + 52, c["muted"], _safe(subtitle)),
        '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s" stroke="%s"/>' %
        (screen_x, screen_y, screen_w, screen_h, c["canvas"], c["line"]),
    ]
    if mode == "host_full_bleed_text":
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, screen_w - 16, screen_h - 16, c["host"]),
            '<circle cx="%d" cy="%d" r="24" fill="%s"/>' %
            (screen_x + screen_w // 2, screen_y + 48, c["host_dark"]),
            '<rect x="%d" y="%d" width="86" height="42" rx="20" fill="%s"/>' %
            (screen_x + screen_w // 2 - 43, screen_y + 76, c["host_dark"]),
            '<rect x="%d" y="%d" width="%d" height="22" rx="6" fill="%s"/>' %
            (screen_x + 18, screen_y + screen_h - 36, screen_w - 36, c["content"]),
        ])
    elif mode == "split_left_right":
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, (screen_w - 24) // 2, screen_h - 16, c["content"]),
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + screen_w // 2 + 4, screen_y + 8, (screen_w - 24) // 2, screen_h - 16, c["host"]),
            '<rect x="%d" y="%d" width="%d" height="12" rx="5" fill="%s"/>' %
            (screen_x + 24, screen_y + 38, (screen_w - 78) // 2, c["content_dark"]),
            '<circle cx="%d" cy="%d" r="20" fill="%s"/>' %
            (screen_x + screen_w - 70, screen_y + 50, c["host_dark"]),
            '<rect x="%d" y="%d" width="68" height="40" rx="18" fill="%s"/>' %
            (screen_x + screen_w - 104, screen_y + 78, c["host_dark"]),
        ])
    elif mode == "content_full_bleed":
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, screen_w - 16, screen_h - 16, c["accent"]),
            '<rect x="%d" y="%d" width="%d" height="18" rx="6" fill="%s"/>' %
            (screen_x + 34, screen_y + 34, screen_w - 68, c["accent_dark"]),
            '<rect x="%d" y="%d" width="%d" height="14" rx="5" fill="%s" opacity="0.65"/>' %
            (screen_x + 48, screen_y + 66, screen_w - 96, c["accent_dark"]),
            '<rect x="%d" y="%d" width="%d" height="14" rx="5" fill="%s" opacity="0.38"/>' %
            (screen_x + 64, screen_y + 94, screen_w - 128, c["accent_dark"]),
        ])
    elif mode == "stacked_top_bottom":
        top_h = (screen_h - 24) // 2
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, screen_w - 16, top_h, c["process"]),
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + top_h + 16, screen_w - 16, screen_h - top_h - 24, c["host"]),
            '<circle cx="%d" cy="%d" r="13" fill="%s"/>' %
            (screen_x + 48, screen_y + 40, c["process_dark"]),
            '<circle cx="%d" cy="%d" r="13" fill="%s" opacity="0.72"/>' %
            (screen_x + 100, screen_y + 40, c["process_dark"]),
            '<circle cx="%d" cy="%d" r="13" fill="%s" opacity="0.45"/>' %
            (screen_x + 152, screen_y + 40, c["process_dark"]),
            '<circle cx="%d" cy="%d" r="20" fill="%s"/>' %
            (screen_x + screen_w // 2, screen_y + top_h + 47, c["host_dark"]),
            '<rect x="%d" y="%d" width="78" height="28" rx="14" fill="%s"/>' %
            (screen_x + screen_w // 2 - 39, screen_y + top_h + 68, c["host_dark"]),
        ])
    elif mode == "cutout_presenter":
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, screen_w - 16, screen_h - 16, c["content"]),
            '<rect x="%d" y="%d" width="%d" height="16" rx="5" fill="%s" opacity="0.55"/>' %
            (screen_x + 34, screen_y + 34, screen_w - 68, c["content_dark"]),
            '<circle cx="%d" cy="%d" r="34" fill="%s"/>' %
            (screen_x + screen_w // 2, screen_y + 74, c["host_dark"]),
            '<rect x="%d" y="%d" width="96" height="44" rx="22" fill="%s"/>' %
            (screen_x + screen_w // 2 - 48, screen_y + 96, c["host_dark"]),
        ])
    elif mode == "corner_pip":
        parts.extend([
            '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' %
            (screen_x + 8, screen_y + 8, screen_w - 16, screen_h - 16, c["canvas"]),
            '<rect x="%d" y="%d" width="%d" height="16" rx="5" fill="%s"/>' %
            (screen_x + 30, screen_y + 32, screen_w - 60, c["muted"]),
            '<rect x="%d" y="%d" width="%d" height="14" rx="5" fill="%s" opacity="0.46"/>' %
            (screen_x + 30, screen_y + 64, screen_w - 82, c["muted"]),
            '<circle cx="%d" cy="%d" r="27" fill="%s"/>' %
            (screen_x + screen_w - 48, screen_y + screen_h - 44, c["host_dark"]),
        ])
    parts.extend([
        '<text x="%d" y="%d" fill="%s" font-size="12">%s</text>' %
        (x + 22, y + h - 28, c["muted"], _safe(MODE_CATALOG[mode]["use_when"])),
        '</g>',
    ])
    return "\n".join(parts)


def render_svg(payload):
    w, h = 1200, 760
    panel_w, panel_h = 360, 300
    gap_x, gap_y = 36, 34
    start_x, start_y = 30, 112
    panels = []
    for idx, mode in enumerate(MODE_ORDER):
        col = idx % 3
        row = idx // 3
        panels.append(_panel_svg(
            mode,
            start_x + col * (panel_w + gap_x),
            start_y + row * (panel_h + gap_y),
            panel_w,
            panel_h,
        ))
    title = _safe(payload.get("title"))
    return """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">
<title>{title}</title>
<desc>讲解型视频六种视觉版式的确定性预览图，包含讲师主讲、左右分屏、全屏内容、上下分屏、前景讲师和角落画中画。</desc>
<rect width="{w}" height="{h}" fill="{canvas}"/>
<text x="30" y="46" fill="{ink}" font-size="28" font-weight="700">{title}</text>
<text x="30" y="78" fill="{muted}" font-size="15">stacked_top_bottom 是上下方正分区；corner_pip 才是角落小窗。</text>
{panels}
</svg>""".format(
        w=w,
        h=h,
        title=title,
        canvas=COLORS["canvas"],
        ink=COLORS["ink"],
        muted=COLORS["muted"],
        panels="\n".join(panels),
    )


def _sequence_rows(sequence):
    rows = []
    for item in sequence:
        rows.append(
            "<tr>"
            "<td>S{index}</td>"
            "<td><code>{mode}</code><br><span>{mode_name}</span></td>"
            "<td>{title}</td>"
            "<td>{summary}</td>"
            "<td>{contract}</td>"
            "</tr>".format(
                index=item["index"],
                mode=_safe(item["visual_mode"]),
                mode_name=_safe(item["visual_mode_name"]),
                title=_safe(item["title"]),
                summary=_safe(item["summary"]),
                contract=_safe(item["layout_contract"]),
            )
        )
    return "\n".join(rows)


def render_html(payload, svg_filename):
    title = _safe(payload.get("title"))
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1F2937; background: #F8FAFC; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 44px; }}
    h1 {{ font-size: 26px; margin: 0 0 8px; }}
    p {{ line-height: 1.55; color: #475569; }}
    img {{ width: 100%; height: auto; border: 1px solid #CBD5E1; border-radius: 12px; background: #fff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 24px; background: #fff; border: 1px solid #CBD5E1; border-radius: 10px; overflow: hidden; }}
    th, td {{ text-align: left; vertical-align: top; padding: 12px 14px; border-bottom: 1px solid #E2E8F0; font-size: 14px; }}
    th {{ color: #334155; background: #F1F5F9; }}
    td span {{ color: #64748B; font-size: 13px; }}
    code {{ color: #1D4ED8; }}
    .notice {{ background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 12px 14px; margin: 18px 0 22px; }}
  </style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <p>这份预览由项目脚本确定性生成，不调用图片或视频模型。它用于客户确认讲解型视频每段的视觉版式节奏。</p>
  <div class="notice">上下分屏是方正分区，不是圆形小弹窗；角落画中画只在密集 PPT 或信息图必须完整展示时使用。</div>
  <img src="{svg_filename}" alt="讲解视频六种视觉版式示意图">
  <table>
    <thead><tr><th>段落</th><th>版式</th><th>标题</th><th>画面摘要</th><th>合同说明</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</main>
</body>
</html>""".format(
        title=title,
        svg_filename=_safe(svg_filename),
        rows=_sequence_rows(payload.get("sequence") or []),
    )


def write_preview(out_dir, *, plan=None, client=None, run_id=None, title=None):
    payload = build_preview_payload(plan, client=client, run_id=run_id, title=title)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    svg_path = os.path.join(out_dir, "explainer_layout_preview.svg")
    html_path = os.path.join(out_dir, "explainer_layout_preview.html")
    json_path = os.path.join(out_dir, "explainer_layout_preview.json")
    _write_text(svg_path, render_svg(payload))
    _write_text(html_path, render_html(payload, os.path.basename(svg_path)))
    payload["files"] = {
        "html": html_path,
        "svg": svg_path,
        "json": json_path,
    }
    _write_json(json_path, payload)
    return payload


def _validate_embedded_subtitle_contract(plan):
    """Reject a stale injected motion-design snapshot before rendering.

    The enriched plan is the execution source of truth.  A neighbouring
    motion_design.json is intentionally not read here because silently mixing
    two revisions would invalidate approval provenance.  When a plan already
    contains an injected subtitle, it must match the deterministic excerpt for
    the current dialogue; callers should rerun ``motion_design.py inject`` to
    produce a new enriched revision.
    """
    stale = []
    for shot in _plan_shots(plan):
        dialogue = str(shot.get("dialogue") or shot.get("voiceover") or "").strip()
        embedded = shot.get("motion_design") if isinstance(
            shot.get("motion_design"), dict) else {}
        subtitle = embedded.get("subtitle") if isinstance(
            embedded.get("subtitle"), dict) else {}
        expected = motion_design.compact_subtitle_text(dialogue) if dialogue else ""
        copies = []
        if subtitle.get("text"):
            copies.append(("motion_design.subtitle", str(subtitle.get("text") or "").strip()))
        for owner, slots in (
                ("motion_plan.asset_slots", (shot.get("motion_plan") or {}).get("asset_slots")
                 if isinstance(shot.get("motion_plan"), dict) else []),
                ("shot.asset_slots", shot.get("asset_slots"))):
            for slot in slots or []:
                if isinstance(slot, dict) and slot.get("slot") == "subtitle" and slot.get("text"):
                    copies.append((owner, str(slot.get("text") or "").strip()))
        if not dialogue or not copies:
            continue
        for owner, actual in copies:
            if actual != expected:
                stale.append("%s.%s:%r!=%r" % (
                    shot.get("id") or shot.get("shot_id") or "unknown",
                    owner, actual, expected))
    if stale:
        raise ValueError(
            "EXECUTION_PREVIEW_SUBTITLE_SNAPSHOT_STALE: %s; "
            "请先运行 motion_design.py inject 生成新的 enriched plan" %
            "; ".join(stale))
    return True


def _execution_plan_fingerprint(plan):
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _formal_preview_revision(plan, publish_root, *, manifest_path, source_plan,
                             human_image, preview_audio, quality):
    """Choose an immutable render directory before any output is written."""
    if not manifest_path:
        return os.path.abspath(publish_root), human_image, None
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest_store.load_manifest(manifest_path)
    approved_cast = _confirmed_cast_board_from_manifest(manifest)
    if approved_cast:
        if human_image:
            supplied = os.path.abspath(human_image)
            if not os.path.isfile(supplied) or _sha256_file(supplied) != _sha256_file(approved_cast):
                raise ValueError("EXECUTION_PREVIEW_HUMAN_NOT_CONFIRMED_CAST")
        human_image = approved_cast
    inputs = {
        "schema_version": 1,
        "plan_fingerprint": _execution_plan_fingerprint(plan),
        "preview_code_sha256": _execution_preview_code_sha(),
        "quality": quality,
        "source_plan": (run_manifest_store.file_record(source_plan)
                        if source_plan and os.path.isfile(source_plan) else None),
        "human_image": (run_manifest_store.file_record(human_image)
                        if human_image and os.path.isfile(human_image) else None),
        "preview_audio": (run_manifest_store.file_record(preview_audio)
                          if preview_audio and os.path.isfile(preview_audio) else None),
    }
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    revision_id = hashlib.sha256(encoded).hexdigest()[:20]
    revision_dir = os.path.join(os.path.abspath(publish_root), "revisions", revision_id)
    return revision_dir, human_image, revision_id


def _validate_formal_execution_plan(plan):
    missing = []
    metadata = plan.get("_execution_plan") or {}
    if metadata.get("kind") != "motion_enriched_storyboard_plan":
        missing.append("_execution_plan")
    for shot in _plan_shots(plan):
        sid = shot.get("id") or shot.get("shot_id") or "unknown"
        if not isinstance(shot.get("motion_design"), dict):
            missing.append("%s.motion_design" % sid)
        if not isinstance(shot.get("motion_plan"), dict) or not shot.get("motion_plan"):
            missing.append("%s.motion_plan" % sid)
        if not isinstance(shot.get("asset_slots"), list) or not shot.get("asset_slots"):
            missing.append("%s.asset_slots" % sid)
    if missing:
        raise ValueError(
            "FORMAL_EXECUTION_PLAN_INCOMPLETE: %s；请先完成 motion_design design + "
            "inject，使用独立 storyboard_execution_plan.json。" % ", ".join(missing))
    return True


def _require_execution_preview_transaction(manifest, *, source_plan, plan,
                                           out_dir, quality, lease_owner):
    transaction = (manifest.get("generation") or {}).get("storyboard") or {}
    source_record = run_manifest_store.file_record(source_plan)
    result_path = os.path.join(os.path.abspath(out_dir), "execution_preview_result.json")
    animatic_path = os.path.join(os.path.abspath(out_dir), "layout_animatic.mp4")
    if (transaction.get("status") != "running" or quality != "high" or
            transaction.get("render_quality") != "high" or
            transaction.get("source_plan") != source_record or
            not run_manifest_store.file_record_is_current(
                transaction.get("source_plan")) or
            transaction.get("plan_fingerprint") != _execution_plan_fingerprint(plan) or
            transaction.get("preview_code_sha256") != _execution_preview_code_sha() or
            transaction.get("planned_output_dir") != os.path.abspath(out_dir) or
            transaction.get("planned_result") != result_path or
            transaction.get("planned_animatic") != animatic_path or
            (transaction.get("lease") or {}).get("owner_token") != lease_owner):
        raise ValueError("EXECUTION_PREVIEW_RUNNING_TRANSACTION_REQUIRED")
    return transaction


def _prepare_execution_preview(context):
    plan = context["plan"]
    out_dir = context["out_dir"]
    source_plan = context["source_plan"]
    human_image = context["human_image"]
    quality = context["quality"]
    remotion_module = context["remotion_module"]
    hf_module = context["hf_module"]
    alpha_transcoder = context["alpha_transcoder"]
    manifest_path = context["manifest_path"]
    lease_owner = context["lease_owner"]
    require_manifest = context["require_manifest"]
    if not isinstance(plan, dict) or not bridge._is_document_explainer_plan(plan):
        raise ValueError("EXECUTION_PREVIEW_REQUIRES_DOCUMENT_EXPLAINER_PLAN")
    _validate_embedded_subtitle_contract(plan)
    out_dir = os.path.abspath(out_dir)
    resource_plan = _preview_resource_plan(plan, out_dir, quality)
    eta = resource_plan["estimated_wall_time_minutes"]
    print("[execution-preview] 预计 %d–%d 分钟，预留磁盘 %.1f GB；完成后仍需逐帧视觉复核。" % (
        eta[0], eta[1], resource_plan["required_free_bytes"] / 1024 ** 3))
    manifest = None
    manifest_path = os.path.abspath(manifest_path) if manifest_path else None
    if require_manifest and not manifest_path:
        raise ValueError(
            "EXECUTION_PREVIEW_MANIFEST_REQUIRED: 正式可确认执行预演必须绑定 run_manifest.json")
    if manifest_path:
        if quality != "high":
            raise ValueError(
                "FORMAL_EXECUTION_PREVIEW_HIGH_QUALITY_REQUIRED")
        if not source_plan or not os.path.isfile(source_plan):
            raise ValueError("EXECUTION_PREVIEW_SOURCE_PLAN_REQUIRED")
        source_plan = os.path.abspath(source_plan)
        if not os.path.isfile(manifest_path):
            raise ValueError("EXECUTION_PREVIEW_MANIFEST_MISSING: %s" % manifest_path)
        manifest = run_manifest_store.load_manifest(manifest_path)
        if (str(manifest.get("client") or "") != str(plan.get("client") or "") or
                str(manifest.get("run_id") or "") != str(plan.get("run_id") or "")):
            raise ValueError("EXECUTION_PREVIEW_MANIFEST_IDENTITY_MISMATCH")
        run_manifest_store.require_run_output(
            manifest, out_dir, label="execution_preview_out", must_exist=False)
        _validate_formal_execution_plan(plan)
        run_manifest_store.validate_execution_plan_binding(
            manifest, source_plan)
        if not human_image:
            human_image = _confirmed_cast_board_from_manifest(manifest)
        if (run_manifest_store.stage_required(manifest, "cast_board") and
                not human_image):
            raise ValueError("EXECUTION_PREVIEW_CONFIRMED_CAST_REQUIRED")
        manifest = run_manifest_store.acquire_generation_lease(
            manifest_path, "storyboard", lease_owner, ttl_seconds=1800)
        # Revalidate against the lease-acquired snapshot: another process may
        # have changed an upstream approval between the initial read and CAS.
        run_manifest_store.validate_execution_plan_binding(
            manifest, source_plan)
        if not human_image:
            human_image = _confirmed_cast_board_from_manifest(manifest)
        start_revision = int(manifest.get("revision") or 0)
        manifest["generation"]["storyboard"].update({
            "source_plan": run_manifest_store.file_record(source_plan),
            "plan_fingerprint": _execution_plan_fingerprint(plan),
            "planned_output_dir": out_dir,
            "planned_result": os.path.join(out_dir, "execution_preview_result.json"),
            "planned_animatic": os.path.join(out_dir, "layout_animatic.mp4"),
            "render_quality": quality,
            "preview_code_sha256": _execution_preview_code_sha(),
        })
        run_manifest_store.save_manifest(
            manifest, manifest_path, expected_revision=start_revision)
    os.makedirs(out_dir, exist_ok=True)
    human_image, human_preview_source = _prepare_human_preview_image(
        human_image, out_dir)
    remotion = remotion_module
    if remotion is None:
        import remotion_engine as remotion
    hyperframes = hf_module or hf_engine
    alpha_transcoder = alpha_transcoder or transcode_browser_alpha
    project_root = os.path.dirname(HERE)
    remotion_source_sha = _source_tree_fingerprint([
        getattr(remotion, "__file__", ""),
        os.path.join(project_root, "remotion_engine", "src"),
        os.path.join(project_root, "remotion_engine", "package-lock.json"),
    ])
    hyperframes_source_sha = _source_tree_fingerprint([
        getattr(hyperframes, "__file__", ""),
        getattr(hyperframes, "ENGINE", ""),
        os.path.join(HERE, "hf_engine.py"),
    ])
    render_scale = (remotion.quality_scale(quality)
                    if hasattr(remotion, "quality_scale") else
                    {"draft": 0.5, "standard": 0.75, "high": 1.0}[quality])

    authored_fps = int(plan.get("fps") or 30)
    preview_fps = (
        15 if manifest is None and quality == "draft" else
        24 if manifest is None and quality == "standard" else
        authored_fps)
    render_plan = copy.deepcopy(plan)
    render_plan["fps"] = preview_fps
    fusion = bridge.build_fusion_plan(
        render_plan, source_plan=source_plan, composition_mode="scene_embedded")
    fusion = bridge.write_plan_files(plan, fusion, os.path.join(out_dir, "contracts"))
    content_spec = _load_json(fusion["files"]["content_spec"])
    content_spec["transparentBackground"] = True
    content_spec_path = _write_json(
        os.path.join(out_dir, "content_previs_spec.json"), content_spec)
    content_layer = os.path.join(out_dir, "content_previs.webm")
    content_fingerprint = _render_fingerprint(
        content_spec, quality=quality, renderer="remotion",
        composition=remotion.CONTENT_COMPOSITION,
        renderer_source_sha=remotion_source_sha,
        extra={"alpha": True, "scale": render_scale})
    _run_cached_render(
        "内容层", content_layer, content_fingerprint,
        lambda: remotion.render(
            content_spec_path, content_layer, quality=quality,
            composition=remotion.CONTENT_COMPOSITION, alpha=True))
    context.update({
        "out_dir": out_dir, "manifest": manifest,
        "manifest_path": manifest_path, "source_plan": source_plan,
        "human_image": human_image,
        "human_preview_source": human_preview_source,
        "remotion": remotion, "hyperframes": hyperframes,
        "alpha_transcoder": alpha_transcoder,
        "remotion_source_sha": remotion_source_sha,
        "hyperframes_source_sha": hyperframes_source_sha,
        "render_scale": render_scale, "authored_fps": authored_fps,
        "preview_fps": preview_fps, "fusion": fusion,
        "content_spec_path": content_spec_path,
        "content_layer": content_layer,
        "resource_plan": resource_plan,
    })


def _render_execution_motion(context):
    fusion = context["fusion"]
    render_scale = context["render_scale"]
    out_dir = context["out_dir"]
    quality = context["quality"]
    hyperframes = context["hyperframes"]
    hyperframes_source_sha = context["hyperframes_source_sha"]
    alpha_transcoder = context["alpha_transcoder"]
    motion_fusion = dict(fusion)
    source_width, source_height = bridge._output_dimensions(fusion)
    # Current motion templates use a 680px minimum caption lane.  Keep their
    # internal canvas at least half-HD while the final diagnostic Animatic can
    # still be encoded at quarter scale.
    motion_scale = max(0.5, render_scale)
    motion_fusion.update({
        "width": max(2, int(round(source_width * motion_scale / 2.0)) * 2),
        "height": max(2, int(round(source_height * motion_scale / 2.0)) * 2),
    })
    motion_spec = build_hyperframes_timeline_spec(motion_fusion)
    motion_spec_path = _write_json(
        os.path.join(out_dir, "hyperframes_previs_spec.json"), motion_spec)
    motion_layer = None
    motion_master = None
    if motion_spec.get("scenes"):
        motion_master = os.path.join(out_dir, "motion_master.mov")
        motion_layer = os.path.join(out_dir, "motion_previs.webm")
        # Chromium cannot decode ProRes 4444 inside Remotion's <Video>, even
        # though the verified MOV is the preferred editing master. Render that
        # master first, then explicitly transcode it to VP9 WebM alpha for the
        # browser timeline. Direct HyperFrames WebM export has lost alpha on
        # some runtime versions and therefore is not trusted here.
        motion_master_fingerprint = _render_fingerprint(
            motion_spec, quality=quality, renderer="hyperframes",
            composition="motion_master",
            renderer_source_sha=hyperframes_source_sha,
            extra={"format": "mov", "scale": motion_scale})
        _run_cached_render(
            "HyperFrames 动效母版", motion_master,
            motion_master_fingerprint,
            lambda: hyperframes.render(
                motion_spec, motion_master, fmt="mov", quality=quality))
        motion_layer_spec = {
            "motion_master": {
                "path": os.path.abspath(motion_master),
                "sha256": _sha256_file(motion_master),
                "size": os.path.getsize(motion_master),
            },
            "target": "vp9_webm_alpha",
        }
        motion_layer_fingerprint = _render_fingerprint(
            motion_layer_spec, quality=quality, renderer="ffmpeg",
            composition="alpha_interchange",
            renderer_source_sha=hyperframes_source_sha)
        _run_cached_render(
            "浏览器透明动效层", motion_layer,
            motion_layer_fingerprint,
            lambda: alpha_transcoder(motion_master, motion_layer))
    context.update({
        "motion_scale": motion_scale, "motion_spec": motion_spec,
        "motion_spec_path": motion_spec_path, "motion_layer": motion_layer,
        "motion_master": motion_master,
    })


def _execution_audio_contract(context):
    preview_audio = context["preview_audio"]
    fusion = context["fusion"]
    if preview_audio:
        preview_audio = os.path.abspath(preview_audio)
        if not os.path.isfile(preview_audio):
            raise ValueError("EXECUTION_PREVIEW_AUDIO_MISSING")
        expected = float((fusion.get("edl") or {}).get("total_duration") or 0)
        actual = float(bridge._media_duration(preview_audio))
        tolerance = max(0.6, expected * 0.02)
        if expected and abs(actual - expected) > tolerance:
            raise ValueError(
                "EXECUTION_PREVIEW_AUDIO_TIMELINE_MISMATCH: %.2fs != %.2fs (tolerance %.2fs)"
                % (actual, expected, tolerance))
        audio_timing = {
            "source": "preview_audio", "path": preview_audio,
            "sha256": _sha256_file(preview_audio), "duration": round(actual, 3),
            "expected_duration": round(expected, 3), "tolerance": round(tolerance, 3),
            "matched": True,
        }
    else:
        audio_timing = {
            "source": "approved_edl",
            "expected_duration": round(float((fusion.get("edl") or {}).get("total_duration") or 0), 3),
            "matched": None,
            "invalidation_rule": "actual_avatar_plate_duration_delta_gt_max_0.6s_or_2pct",
        }
    context.update({"preview_audio": preview_audio, "audio_timing": audio_timing})


def _render_integrated_execution_preview(context):
    plan = context["plan"]
    fusion = context["fusion"]
    content_layer = context["content_layer"]
    human_image = context["human_image"]
    motion_layer = context["motion_layer"]
    preview_audio = context["preview_audio"]
    human_preview_source = context["human_preview_source"]
    out_dir = context["out_dir"]
    quality = context["quality"]
    remotion = context["remotion"]
    remotion_source_sha = context["remotion_source_sha"]
    render_scale = context["render_scale"]
    integrated = bridge.build_previs_integrated_spec(
        fusion, content_layer, human_image=human_image,
        motion_video=motion_layer, preview_audio=preview_audio)
    integrated["humanImageContract"] = human_preview_source
    subtitle_geometry = {}
    for item in context["motion_spec"].get("scenes") or []:
        if item.get("slot") != "subtitle" or not item.get("source_shot_id"):
            continue
        subtitle_geometry[str(item["source_shot_id"])] = {
            key: int(item[key]) for key in (
                "left_px", "right_px", "bottom_px", "max_height_px")
            if item.get(key) is not None
        }
    stacked_derivative = ((human_preview_source.get(
        "stacked_upper_body_derivative") or {}).get("path")
        if isinstance(human_preview_source, dict) else None)
    for scene in integrated.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        geometry = subtitle_geometry.get(str(scene.get("id") or ""))
        if geometry:
            scene["motionSubtitleCrop"] = geometry
        if scene.get("visualMode") == "stacked_top_bottom" and stacked_derivative:
            scene["stackedHumanImagePath"] = stacked_derivative
    integrated_path = _write_json(
        os.path.join(out_dir, "integrated_previs_spec.json"), integrated)
    animatic_path = os.path.join(out_dir, "layout_animatic.mp4")
    animatic_fingerprint = _render_fingerprint(
        integrated, quality=quality, renderer="remotion",
        composition=remotion.INTEGRATED_COMPOSITION,
        renderer_source_sha=remotion_source_sha,
        extra={"alpha": False, "scale": render_scale})
    _run_cached_render(
        "版式 Animatic", animatic_path, animatic_fingerprint,
        lambda: remotion.render(
            integrated_path, animatic_path, quality=quality,
            composition=remotion.INTEGRATED_COMPOSITION))

    layout_dir = os.path.join(out_dir, "layout_boards")
    os.makedirs(layout_dir, exist_ok=True)
    context.update({
        "integrated": integrated, "integrated_path": integrated_path,
        "animatic_path": animatic_path,
        "rendered_shots": [], "plan_shots": _plan_shots(plan),
        "layout_dir": layout_dir,
    })


def _render_execution_layout_boards(context):
    integrated = context["integrated"]
    quality = context["quality"]
    remotion = context["remotion"]
    remotion_source_sha = context["remotion_source_sha"]
    preview_fps = context["preview_fps"]
    animatic_path = context["animatic_path"]
    layout_dir = context["layout_dir"]
    plan_shots = context["plan_shots"]
    rendered_shots = context["rendered_shots"]
    animatic_sha = _sha256_file(animatic_path)
    for index, scene in enumerate(integrated.get("scenes") or [], start=1):
        duration_frames = max(1, int(scene.get("durationInFrames") or 1))
        # Capture after entrance motion has settled while remaining inside the
        # exact scene interval. This frame also exists in the Animatic.
        settle_floor = min(duration_frames - 1, 20)
        local_frame = min(
            duration_frames - 1,
            max(settle_floor, int(round(duration_frames * 0.50))))
        frame = int(scene.get("fromFrame") or 0) + local_frame
        shot_id = str(scene.get("id") or "s%02d" % index)
        out_path = os.path.join(layout_dir, "%02d_%s.png" % (index, shot_id))
        command = _animatic_frame_extract_command(animatic_path, frame, out_path)
        still_fingerprint = _render_fingerprint(
            {"animatic_sha256": animatic_sha, "frame": frame},
            quality=quality, renderer="ffmpeg_animatic_frame_extract",
            composition=remotion.INTEGRATED_COMPOSITION,
            renderer_source_sha=remotion_source_sha,
            extra={"extraction_command": command})
        _run_cached_render(
            "版式静帧 %d/%d" % (index, len(integrated.get("scenes") or [])),
            out_path, still_fingerprint,
            lambda command=command: subprocess.run(command, check=True))
        shot = plan_shots[index - 1] if index - 1 < len(plan_shots) else {"id": shot_id}
        rendered_shots.append({
            "id": shot_id,
            "path": os.path.abspath(out_path),
            "abspath": os.path.abspath(out_path),
            "sha256": _sha256_file(out_path),
            "frame": frame,
            "scene_from_frame": int(scene.get("fromFrame") or 0),
            "scene_duration_frames": duration_frames,
            "visual_mode": scene.get("visualMode"),
            "shot": shot,
            "renderer": "remotion.IntegratedExplainer",
            "board_renderer": "ffmpeg_animatic_frame_extract",
            "animatic_sha256": animatic_sha,
            "timestamp_seconds": round(frame / float(preview_fps), 6),
            "extraction_command": command,
            "representative_frame_contract": {
                "selection": "mid_scene_after_entrance_settle",
                "local_frame": local_frame,
                "entrance_settle_floor": settle_floor,
            },
            "deterministic_render": True,
            "remote_url_required": False,
        })


def _build_execution_preview_artifacts(context):
    quality = context["quality"]
    render_scale = context["render_scale"]
    motion_scale = context["motion_scale"]
    preview_fps = context["preview_fps"]
    authored_fps = context["authored_fps"]
    manifest = context["manifest"]
    human_preview_source = context["human_preview_source"]
    source_plan = context["source_plan"]
    rendered_shots = context["rendered_shots"]
    animatic_path = context["animatic_path"]
    content_layer = context["content_layer"]
    fusion = context["fusion"]
    content_spec_path = context["content_spec_path"]
    motion_layer = context["motion_layer"]
    motion_master = context["motion_master"]
    motion_spec_path = context["motion_spec_path"]
    motion_spec = context["motion_spec"]
    audio_timing = context["audio_timing"]
    integrated_path = context["integrated_path"]
    integrated = context["integrated"]
    manifest_path = context["manifest_path"]
    resource_plan = context["resource_plan"]
    artifacts = {
        "schema_version": 2,
        "type": "document_explainer_execution_preview",
        "render_profile": {
            "quality": quality,
            "scale": render_scale,
            "motion_layer_scale": motion_scale,
            "fps": preview_fps,
            "authored_fps": authored_fps,
            "approval_eligible": manifest is not None and quality == "high",
            "formal_high_resolution_unchanged": True,
        },
        "renderer_source_of_truth": "remotion.IntegratedExplainer",
        "content_renderer": "remotion.Content",
        "renderer_provenance": {
            "preview_code_sha256": _execution_preview_code_sha(),
            "remotion_source_sha256": context["remotion_source_sha"],
            "hyperframes_source_sha256": context["hyperframes_source_sha"],
            "hotfix_requires_new_transaction": True,
        },
        "resource_plan": resource_plan,
        "preview_revision": ({
            "id": context.get("revision_id"),
            "directory": os.path.abspath(context["out_dir"]),
            "publish_root": os.path.abspath(context["publish_root"]),
            "immutable": True,
        } if context.get("revision_id") else {
            "id": None, "directory": os.path.abspath(context["out_dir"]),
            "publish_root": os.path.abspath(context["publish_root"]),
            "immutable": False,
        }),
        "human_preview_source": human_preview_source,
        "source_plan": os.path.abspath(source_plan) if source_plan else None,
        "shots": rendered_shots,
        "layout_boards": rendered_shots,
        "animatic": {
            "path": os.path.abspath(animatic_path),
            "sha256": _sha256_file(animatic_path),
            "renderer": "remotion.IntegratedExplainer",
        },
        "content_layer": {
            "path": os.path.abspath(content_layer),
            "sha256": _sha256_file(content_layer),
            "has_alpha": True,
        },
        "content_contract_spec": {
            "path": os.path.abspath(fusion["files"]["content_spec"]),
            "sha256": _sha256_file(fusion["files"]["content_spec"]),
        },
        "content_previs_spec": {
            "path": os.path.abspath(content_spec_path),
            "sha256": _sha256_file(content_spec_path),
        },
        "motion_layer": ({
            "path": os.path.abspath(motion_layer),
            "sha256": _sha256_file(motion_layer),
            "has_alpha": True,
            "alpha_verified_by": "hf_engine.render_prores_then_ffmpeg_vp9_alpha",
            "renderer": "hyperframes+ffmpeg_browser_interchange",
            "master_path": os.path.abspath(motion_master),
            "master_sha256": _sha256_file(motion_master),
            "master_format": "prores_4444_alpha",
            "browser_format": "vp9_webm_alpha",
        } if motion_layer else {
            "path": None, "required": False, "reason": "no_renderable_motion_scenes",
        }),
        "hyperframes_spec": {
            "path": os.path.abspath(motion_spec_path),
            "sha256": _sha256_file(motion_spec_path),
            "renderable_scenes": len(motion_spec.get("scenes") or []),
            "visible_layer_contract": motion_spec.get("visible_layer_contract") or {},
        },
        "audio_timing_contract": audio_timing,
        "integrated_spec": {
            "path": os.path.abspath(integrated_path),
            "sha256": _sha256_file(integrated_path),
            "frozen_fingerprint": bridge._approved_spec_fingerprint(integrated),
        },
        "approved_render_contract": {
            "renderer": "remotion.IntegratedExplainer",
            "spec_path": os.path.abspath(integrated_path),
            "spec_sha256": _sha256_file(integrated_path),
            "frozen_fingerprint": bridge._approved_spec_fingerprint(integrated),
            "mutable_media_fields": sorted(bridge.APPROVED_SPEC_MUTABLE_MEDIA_FIELDS),
            "formal_render_must_bind_this_spec": True,
            "timing_change_requires_reapproval": True,
        },
        "artifact_contract": integrated.get("_artifact_contract") or {},
        "generation_keyframe_contract": {
            "role": "avatar_plate_only",
            "clean_plate": True,
            "allowed": ["approved_person", "wardrobe", "background", "lighting"],
            "forbidden": ["ppt_page", "design_board", "subtitle", "graphic_card", "logo_overlay"],
            "storyboard_ref": False,
        },
        "manifest_binding": ({
            "status": "bound",
            "approval_eligible": False,
            "approval_blocked_by": "final_frame_visual_qa_required",
            "manifest_path": manifest_path,
            "stage": "storyboard",
            "manifest_revision_started": int(manifest.get("revision") or 0),
            "expected_manifest_revision_after_registration": int(
                manifest.get("revision") or 0) + 1,
        } if manifest is not None else {
            "status": "unbound_diagnostic",
            "approval_eligible": False,
            "stage": "storyboard",
            "reason": "no_run_manifest",
        }),
    }
    context["artifacts"] = artifacts


def _verify_execution_artifact_bundle(artifacts):
    pairs = []

    def visit(value):
        if isinstance(value, dict):
            for path_key, sha_key in (
                    ("path", "sha256"), ("master_path", "master_sha256"),
                    ("source_path", "source_sha256"),
                    ("preview_path", "preview_sha256")):
                if value.get(path_key) and value.get(sha_key):
                    pairs.append((str(value[path_key]), str(value[sha_key])))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(artifacts)
    stale = []
    for path, expected in pairs:
        absolute = os.path.abspath(path)
        if not os.path.isfile(absolute) or _sha256_file(absolute) != expected:
            stale.append(absolute)
    if stale:
        raise ValueError("EXECUTION_PREVIEW_ARTIFACT_BUNDLE_STALE: %s" %
                         ", ".join(sorted(set(stale))))
    return True


def _persist_execution_preview(context):
    out_dir = context["out_dir"]
    artifacts = context["artifacts"]
    manifest = context["manifest"]
    source_plan = context["source_plan"]
    plan = context["plan"]
    quality = context["quality"]
    manifest_path = context["manifest_path"]
    artifact_path = os.path.join(out_dir, "execution_preview_result.json")
    artifacts["result_json"] = os.path.abspath(artifact_path)
    if manifest is not None:
        manifest = run_manifest_store.load_manifest(manifest_path)
        _require_execution_preview_transaction(
            manifest, source_plan=source_plan, plan=plan,
            out_dir=out_dir, quality=quality,
            lease_owner=context["lease_owner"])
    _verify_execution_artifact_bundle(artifacts)
    _write_json(artifact_path, artifacts)
    if manifest is not None:
        manifest = run_manifest_store.load_manifest(manifest_path)
        _require_execution_preview_transaction(
            manifest, source_plan=source_plan, plan=plan,
            out_dir=out_dir, quality=quality,
            lease_owner=context["lease_owner"])
        # Rendering alone cannot attest readability, face visibility or
        # cross-layer occlusion.  The running transaction is closed only by
        # submit_execution_visual_review after exact-frame QA and commercial QC.
    if context.get("revision_id"):
        result_record = {
            "path": os.path.abspath(artifact_path),
            "sha256": _sha256_file(artifact_path),
            "size": os.path.getsize(artifact_path),
        }
        _write_json(os.path.join(context["publish_root"], "current.json"), {
            "schema_version": 1,
            "revision_id": context["revision_id"],
            "revision_dir": os.path.abspath(out_dir),
            "result": result_record,
            "publish_rule": "pointer_written_only_after_bundle_hash_verification",
        })
    return artifacts


def render_execution_preview(plan, out_dir, *, source_plan=None, human_image=None,
                             preview_audio=None, quality="draft", remotion_module=None,
                             hf_module=None, alpha_transcoder=None,
                             manifest_path=None, require_manifest=False):
    """Render real layout boards and an Animatic from final Remotion components."""
    publish_root = os.path.abspath(out_dir)
    out_dir, human_image, revision_id = _formal_preview_revision(
        plan, publish_root, manifest_path=manifest_path, source_plan=source_plan,
        human_image=human_image, preview_audio=preview_audio, quality=quality)
    lease_owner = (
        "execution-preview:%d:%s" % (os.getpid(), os.urandom(12).hex())
        if manifest_path else None)
    context = dict(locals())
    try:
        _prepare_execution_preview(context)
        _render_execution_motion(context)
        _execution_audio_contract(context)
        _render_integrated_execution_preview(context)
        _render_execution_layout_boards(context)
        _build_execution_preview_artifacts(context)
        return _persist_execution_preview(context)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        if manifest_path and os.path.isfile(manifest_path):
            current = run_manifest_store.load_manifest(manifest_path)
            generation = (current.get("generation") or {}).get("storyboard") or {}
            if generation.get("status") == "running":
                run_manifest_store.record_generation_failure_if_running(
                    manifest_path, "storyboard", exc,
                    error_code="EXECUTION_PREVIEW_FAILED",
                    owner_token=lease_owner)
        raise


def _animatic_frame_extract_command(animatic_path, frame, output_path):
    return [
        bridge._ffmpeg(), "-hide_banner", "-y", "-i", animatic_path,
        "-vf", "select=eq(n\\,%d)" % int(frame), "-vsync", "0",
        "-frames:v", "1", output_path,
    ]


def register_animatic_extracted_boards(result_path):
    """Atomically replace every approval board with exact Animatic pixels."""
    payload = _load_json(result_path)
    animatic = payload.get("animatic") or {}
    animatic_path = os.path.abspath(str(animatic.get("path") or ""))
    boards = payload.get("layout_boards") or []
    if not os.path.isfile(animatic_path) or not boards:
        raise ValueError("ANIMATIC_BOARD_EXTRACTION_INPUT_MISSING")
    animatic_sha = _sha256_file(animatic_path)
    expected_dimensions = tuple(bridge._media_dimensions(animatic_path))
    fps = int((payload.get("render_profile") or {}).get("fps") or 30)
    prepared = []
    try:
        for board in boards:
            path = os.path.abspath(str(board.get("path") or ""))
            frame = int(board.get("frame"))
            if not path or frame < 0:
                raise ValueError("ANIMATIC_BOARD_EXTRACTION_INVALID_BOARD")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".%s.frame-%d." % (os.path.basename(path), frame),
                suffix=".png", dir=os.path.dirname(path) or ".")
            os.close(fd)
            command = _animatic_frame_extract_command(
                animatic_path, frame, temporary)
            try:
                subprocess.run(command, check=True)
                actual_dimensions = tuple(bridge._media_dimensions(temporary))
                if (not os.path.isfile(temporary) or os.path.getsize(temporary) <= 0 or
                        actual_dimensions != expected_dimensions):
                    raise ValueError("ANIMATIC_BOARD_EXTRACTION_INVALID_OUTPUT")
                prepared.append({
                    "path": path, "temporary": temporary, "frame": frame,
                    "command": command, "sha256": _sha256_file(temporary),
                    "dimensions": list(actual_dimensions),
                })
            except (OSError, ValueError, subprocess.CalledProcessError):
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
        for item in prepared:
            os.replace(item["temporary"], item["path"])
            if _sha256_file(item["path"]) != item["sha256"]:
                raise ValueError("ANIMATIC_BOARD_EXTRACTION_COMMIT_HASH_MISMATCH")
        refreshed = copy.deepcopy(payload)
        for board, item in zip(refreshed.get("layout_boards") or [], prepared):
            board.update({
                "path": item["path"], "abspath": item["path"],
                "sha256": item["sha256"],
                "renderer": "remotion.IntegratedExplainer",
                "board_renderer": "ffmpeg_animatic_frame_extract",
                "animatic_sha256": animatic_sha,
                "timestamp_seconds": round(item["frame"] / float(fps), 6),
                "extraction_command": item["command"],
                "extraction_verified": {
                    "method": "ffmpeg_extract_to_temp_then_os_replace",
                    "source_frame": item["frame"],
                    "source_dimensions": list(expected_dimensions),
                    "board_dimensions": item["dimensions"],
                    "committed_sha256": item["sha256"],
                },
            })
        refreshed["shots"] = refreshed["layout_boards"]
        refreshed.setdefault("manifest_binding", {}).update({
            "status": "bound", "approval_eligible": False,
            "approval_blocked_by": "final_frame_visual_qa_required",
        })
        _write_json(result_path, refreshed)
        return refreshed
    finally:
        for item in prepared:
            if os.path.exists(item["temporary"]):
                os.unlink(item["temporary"])


def _normalized_subtitle_text(text):
    return re.sub(r"\s+", "", str(text or ""))


def _visual_review_passes(shot_review, *, subtitle_required, presenter_required):
    required = [
        shot_review.get("content_complete") is True,
        shot_review.get("cross_layer_occlusion") is False,
        shot_review.get("safe_zone") is True,
        shot_review.get("artifact_detected") is False,
    ]
    if subtitle_required:
        required.append(shot_review.get("subtitle_text_complete") is True)
    elif shot_review.get("subtitle_text_complete") not in (None, "not_applicable"):
        raise ValueError("FINAL_FRAME_VISUAL_QA_SUBTITLE_NA_REQUIRED")
    if presenter_required:
        required.append(shot_review.get("presenter_visible") is True)
        presenter_visibility = "visible"
    else:
        if shot_review.get("presenter_visible") not in (None, "not_applicable"):
            raise ValueError("FINAL_FRAME_VISUAL_QA_PRESENTER_NA_REQUIRED")
        presenter_visibility = "not_applicable"
    return all(required), presenter_visibility


def write_final_frame_visual_qa(result_path, manual_visual_review, *, out_path=None):
    """Bind truthful manual review to current Animatic, specs and board bytes."""
    execution = _load_json(result_path)
    review = dict(manual_visual_review or {})
    reviewer = str(review.get("checker") or "").strip()
    method = str(review.get("method") or "").strip()
    reviewer_type = str(review.get("reviewer_type") or "").strip().lower()
    if not reviewer or reviewer_type not in {"agent", "human"}:
        raise ValueError("FINAL_FRAME_VISUAL_QA_MANUAL_REVIEWER_REQUIRED")
    if reviewer_type == "agent" and method != "manual_frame_by_frame_agent_visual_review":
        raise ValueError("FINAL_FRAME_VISUAL_QA_AGENT_METHOD_REQUIRED")
    review_by_shot = review.get("shots") or {}
    if not isinstance(review_by_shot, dict):
        raise ValueError("FINAL_FRAME_VISUAL_QA_SHOT_REVIEW_REQUIRED")

    hyperframes_record = execution.get("hyperframes_spec") or {}
    integrated_record = execution.get("integrated_spec") or {}
    hyperframes_path = os.path.abspath(str(hyperframes_record.get("path") or ""))
    integrated_path = os.path.abspath(str(integrated_record.get("path") or ""))
    for path, record, label in (
            (hyperframes_path, hyperframes_record, "HYPERFRAMES"),
            (integrated_path, integrated_record, "INTEGRATED")):
        if (not os.path.isfile(path) or
                record.get("sha256") not in (None, _sha256_file(path))):
            raise ValueError("FINAL_FRAME_VISUAL_QA_%s_SPEC_STALE" % label)
    subtitles = {
        str(scene.get("source_shot_id")): scene
        for scene in (_load_json(hyperframes_path).get("scenes") or [])
        if isinstance(scene, dict) and scene.get("slot") == "subtitle" and
        scene.get("source_shot_id") and str(scene.get("text") or "").strip()
    }
    integrated_by_id = {
        str(scene.get("id")): scene
        for scene in (_load_json(integrated_path).get("scenes") or [])
        if isinstance(scene, dict) and scene.get("id")
    }
    animatic = execution.get("animatic") or {}
    animatic_path = os.path.abspath(str(animatic.get("path") or ""))
    if not os.path.isfile(animatic_path):
        raise ValueError("FINAL_FRAME_VISUAL_QA_ANIMATIC_MISSING")
    animatic_sha = _sha256_file(animatic_path)
    if animatic.get("sha256") not in (None, animatic_sha):
        raise ValueError("FINAL_FRAME_VISUAL_QA_ANIMATIC_STALE")

    qa_shots = []
    for board in execution.get("layout_boards") or []:
        shot_id = str(board.get("id") or "")
        scene = integrated_by_id.get(shot_id)
        shot_review = review_by_shot.get(shot_id)
        if not scene or not isinstance(shot_review, dict):
            raise ValueError("FINAL_FRAME_VISUAL_QA_SHOT_CONTRACT_MISSING:%s" % shot_id)
        board_path = os.path.abspath(str(board.get("path") or ""))
        board_sha = _sha256_file(board_path) if os.path.isfile(board_path) else None
        if (not board_sha or board.get("sha256") != board_sha or
                board.get("animatic_sha256") != animatic_sha):
            raise ValueError("FINAL_FRAME_VISUAL_QA_BOARD_STALE:%s" % shot_id)
        subtitle = subtitles.get(shot_id)
        dialogue = str((board.get("shot") or {}).get("dialogue") or "").strip()
        if dialogue and not subtitle:
            raise ValueError("FINAL_FRAME_VISUAL_QA_SUBTITLE_MISSING:%s" % shot_id)
        expected_text = str((subtitle or {}).get("text") or "")
        presenter_required = str(scene.get("humanSlot") or "").lower() != "none"
        passed, presenter_visibility = _visual_review_passes(
            shot_review, subtitle_required=bool(subtitle),
            presenter_required=presenter_required)
        normalized = _normalized_subtitle_text(expected_text)
        qa_shots.append({
            "id": shot_id, "path": board_path, "sha256": board_sha,
            "animatic_sha256": animatic_sha, "frame": int(board.get("frame")),
            "timestamp_seconds": board.get("timestamp_seconds"),
            "board_renderer": board.get("board_renderer"),
            "subtitle_required": bool(subtitle),
            "expected_subtitle_text": expected_text,
            "expected_subtitle_normalized_text": normalized,
            "subtitle_first_character": normalized[0] if normalized else None,
            "subtitle_last_character": normalized[-1] if normalized else None,
            "presenter_required": presenter_required,
            "presenter_visibility": presenter_visibility,
            "manual_visual_review": copy.deepcopy(shot_review),
            "passed": bool(passed),
        })
    qa = {
        "schema_version": 2,
        "type": "document_explainer_final_frame_visual_qa",
        "method": method, "checker": reviewer, "reviewer_type": reviewer_type,
        "checked_at": review.get("checked_at") or _dt.datetime.now(
            _dt.timezone.utc).isoformat(),
        "automated_visual_claim": False,
        "review_scope": "manual_visual_review_of_animatic_extracted_boards",
        "customer_visual_confirmation_required": True,
        "customer_confirmed": False,
        "execution_preview": {
            "path": os.path.abspath(result_path),
            "animatic_sha256": animatic_sha,
            "integrated_spec_sha256": _sha256_file(integrated_path),
        },
        "shots": qa_shots,
        "passed": bool(qa_shots) and all(item["passed"] for item in qa_shots),
        "approval_eligible": False,
        "approval_blocked_by": "commercial_qc_refresh_required",
    }
    destination = out_path or os.path.join(
        os.path.dirname(os.path.abspath(result_path)),
        "final_frame_visual_qa.json")
    _write_json(destination, qa)
    return qa


def bind_passed_final_frame_visual_qa(result_path, qa_path):
    payload = _load_json(result_path)
    qa_path = os.path.abspath(qa_path)
    qa = _load_json(qa_path)
    if (not qa.get("passed") or qa.get("automated_visual_claim") is not False or
            qa.get("customer_visual_confirmation_required") is not True or
            qa.get("customer_confirmed") is not False):
        raise ValueError("FINAL_FRAME_VISUAL_QA_NOT_ELIGIBLE")
    boards = {str(item.get("id")): item for item in payload.get("layout_boards") or []}
    qa_boards = {str(item.get("id")): item for item in qa.get("shots") or []}
    if not boards or set(boards) != set(qa_boards):
        raise ValueError("FINAL_FRAME_VISUAL_QA_SHOT_SET_MISMATCH")
    for shot_id, board in boards.items():
        checked = qa_boards[shot_id]
        if (not checked.get("passed") or checked.get("sha256") != board.get("sha256") or
                checked.get("animatic_sha256") != board.get("animatic_sha256")):
            raise ValueError("FINAL_FRAME_VISUAL_QA_BINDING_STALE:%s" % shot_id)
    payload.setdefault("manifest_binding", {}).update({
        "status": "bound", "approval_eligible": True,
        "approval_blocked_by": None,
        "final_frame_visual_qa": {
            "path": qa_path, "sha256": _sha256_file(qa_path)},
    })
    _write_json(result_path, payload)
    return payload


def finalize_final_frame_visual_qa(qa_path, commercial_qc_path):
    qa = _load_json(qa_path)
    commercial_qc = _load_json(commercial_qc_path)
    if not qa.get("passed") or not commercial_qc.get("passed"):
        raise ValueError("FINAL_FRAME_VISUAL_QA_COMMERCIAL_QC_NOT_PASSED")
    qa.update({
        "commercial_qc_path": os.path.abspath(commercial_qc_path),
        "commercial_qc_passed": True,
        "approval_eligible": True, "approval_blocked_by": None,
    })
    _write_json(qa_path, qa)
    return qa


def _formal_execution_preview_artifact_paths(execution, result_path, qa_path,
                                             qc_path):
    paths = [os.path.abspath(path) for path in (result_path, qa_path, qc_path)]
    def add(record):
        if isinstance(record, dict) and record.get("path"):
            path = os.path.abspath(str(record["path"]))
            if os.path.isfile(path) and path not in paths:
                paths.append(path)
    for key in ("animatic", "content_layer", "content_contract_spec",
                "content_previs_spec", "hyperframes_spec", "integrated_spec"):
        add(execution.get(key))
    motion = execution.get("motion_layer") or {}
    add(motion)
    add({"path": motion.get("master_path")} if motion.get("master_path") else None)
    for board in execution.get("layout_boards") or []:
        add(board)
    return paths


def _write_failed_visual_review(result_path, code, qc):
    binding = _load_json(result_path)
    binding.setdefault("manifest_binding", {}).update({
        "approval_eligible": False, "approval_blocked_by": code})
    _write_json(result_path, binding)
    failure_path = os.path.join(
        os.path.dirname(result_path), "commercial_qc_visual_review_failed.json")
    _write_json(failure_path, {
        "type": "document_explainer_visual_review_failure",
        "code": code,
        "attempted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "commercial_qc": qc,
        "manifest_transition": "not_applied",
    })
    return failure_path


def submit_execution_visual_review(result_path, review_path, *, manifest_path=None):
    """Close rendering only after exact-frame manual QA and current-code QC."""
    result_path = os.path.abspath(result_path)
    review_path = os.path.abspath(review_path)
    if not os.path.isfile(result_path) or not os.path.isfile(review_path):
        raise ValueError("EXECUTION_VISUAL_REVIEW_INPUT_MISSING")
    initial = _load_json(result_path)
    bound_manifest = (initial.get("manifest_binding") or {}).get("manifest_path")
    manifest_path = os.path.abspath(manifest_path or bound_manifest or "")
    if (not manifest_path or not os.path.isfile(manifest_path) or
            (bound_manifest and os.path.abspath(bound_manifest) != manifest_path)):
        raise ValueError("EXECUTION_VISUAL_REVIEW_MANIFEST_MISMATCH")
    manifest = run_manifest_store.load_manifest(manifest_path)
    transaction = (manifest.get("generation") or {}).get("storyboard") or {}
    if (transaction.get("status") != "running" or
            os.path.abspath(transaction.get("planned_result") or "") != result_path):
        raise ValueError("EXECUTION_VISUAL_REVIEW_RUNNING_TRANSACTION_REQUIRED")
    provenance = initial.get("renderer_provenance") or {}
    current_code_sha = _execution_preview_code_sha()
    if (transaction.get("preview_code_sha256") != current_code_sha or
            provenance.get("preview_code_sha256") != current_code_sha):
        raise ValueError(
            "EXECUTION_VISUAL_REVIEW_CODE_CHANGED: 渲染开始后代码已变化；"
            "必须关闭当前交易并用新 run/attempt 重渲，禁止在生产 run 内热修后继续审批。")

    execution = register_animatic_extracted_boards(result_path)
    qa_path = os.path.join(os.path.dirname(result_path),
                           "final_frame_visual_qa.json")
    write_final_frame_visual_qa(
        result_path, _load_json(review_path), out_path=qa_path)
    execution = bind_passed_final_frame_visual_qa(result_path, qa_path)
    import explainer_commercial_qc as commercial_qc
    qc_path = os.path.join(os.path.dirname(result_path), "commercial_qc.json")
    first_qc = commercial_qc.audit(
        execution, stage="preview", probe_alpha=True,
        require_manifest_binding=True)
    if not first_qc.get("passed"):
        _write_failed_visual_review(
            result_path, "commercial_qc_failed", first_qc)
        raise ValueError("EXECUTION_VISUAL_REVIEW_COMMERCIAL_QC_FAILED")
    _write_json(qc_path, first_qc)
    finalize_final_frame_visual_qa(qa_path, qc_path)
    execution = bind_passed_final_frame_visual_qa(result_path, qa_path)
    final_qc = commercial_qc.audit(
        execution, stage="preview", probe_alpha=True,
        require_manifest_binding=True)
    if not final_qc.get("passed"):
        _write_failed_visual_review(
            result_path, "final_commercial_qc_failed", final_qc)
        raise ValueError("EXECUTION_VISUAL_REVIEW_FINAL_QC_FAILED")
    _write_json(qc_path, final_qc)

    expected_revision = int(manifest.get("revision") or 0)
    outputs = _formal_execution_preview_artifact_paths(
        execution, result_path, qa_path, qc_path)
    run_manifest_store.mark_generation_finished(manifest, "storyboard", outputs)
    run_manifest_store.save_manifest(
        manifest, manifest_path, expected_revision=expected_revision)
    return {
        "execution_preview": execution,
        "final_frame_visual_qa": _load_json(qa_path),
        "commercial_qc": final_qc,
        "manifest_path": manifest_path,
        "manifest_status": manifest.get("status"),
        "storyboard_status": manifest["generation"]["storyboard"]["status"],
    }


def revalidate_execution_visual_qa(result_path, *, manifest_path=None):
    """Refresh pending evidence without overwriting it on a failed audit."""
    result_path = os.path.abspath(result_path)
    execution = _load_json(result_path)
    binding = execution.get("manifest_binding") or {}
    bound_manifest = binding.get("manifest_path")
    manifest_path = os.path.abspath(manifest_path or bound_manifest or "")
    if (not manifest_path or not os.path.isfile(manifest_path) or
            (bound_manifest and os.path.abspath(bound_manifest) != manifest_path)):
        raise ValueError("EXECUTION_VISUAL_REVALIDATE_MANIFEST_MISMATCH")
    manifest = run_manifest_store.load_manifest(manifest_path)
    transaction = (manifest.get("generation") or {}).get("storyboard") or {}
    if (transaction.get("status") != "pending_approval" or
            manifest.get("approvals", {}).get("storyboard") is True):
        raise ValueError("EXECUTION_VISUAL_REVALIDATE_PENDING_APPROVAL_REQUIRED")
    import explainer_commercial_qc as commercial_qc
    qc = commercial_qc.audit(
        execution, stage="preview", probe_alpha=True,
        require_manifest_binding=True)
    qc_path = os.path.join(os.path.dirname(result_path), "commercial_qc.json")
    if not qc.get("passed"):
        failed_path = os.path.join(
            os.path.dirname(result_path),
            "commercial_qc_revalidation_failed.json")
        _write_json(failed_path, {
            "type": "document_explainer_commercial_qc_revalidation_failure",
            "attempted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "commercial_qc": qc,
            "preserved_formal_commercial_qc": qc_path,
            "manifest_transition": "not_applied",
        })
        raise ValueError("EXECUTION_VISUAL_REVALIDATE_COMMERCIAL_QC_FAILED")
    _write_json(qc_path, qc)
    qa_path = (binding.get("final_frame_visual_qa") or {}).get("path")
    if not qa_path:
        raise ValueError("EXECUTION_VISUAL_REVALIDATE_QA_BINDING_REQUIRED")
    expected_revision = int(manifest.get("revision") or 0)
    run_manifest_store.add_stage_artifacts(
        manifest, "storyboard", _formal_execution_preview_artifact_paths(
            execution, result_path, qa_path, qc_path))
    run_manifest_store.save_manifest(
        manifest, manifest_path, expected_revision=expected_revision)
    return {
        "execution_preview": execution, "commercial_qc": qc,
        "manifest_path": manifest_path,
        "manifest_status": manifest.get("status"),
        "storyboard_status": manifest["generation"]["storyboard"]["status"],
    }



def _default_out_dir(plan, explicit=None):
    if explicit:
        return explicit
    client = _text((plan or {}).get("client") or "unknown")
    run_id = _text((plan or {}).get("run_id") or (plan or {}).get("id") or "layout-preview")
    return os.path.join("output", client, run_id, "layout_preview")


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成讲解型视频六种视觉版式预览")
    parser.add_argument(
        "--plan",
        help="正式模式使用 motion_design inject 生成的 storyboard_execution_plan.json；"
             "未绑定诊断模式可读取 director/storyboard plan")
    parser.add_argument("--out-dir", help="输出目录，默认 output/<client>/<run-id>/layout_preview")
    parser.add_argument("--client")
    parser.add_argument("--run-id")
    parser.add_argument("--title")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印输出文件路径")
    parser.add_argument("--render-execution", action="store_true",
                        help="用实际 Remotion Content + IntegratedExplainer 渲染版式板和 Animatic")
    parser.add_argument("--human-image", help="可选：已确认人物身份静帧；缺省时使用明确标记的中性占位")
    parser.add_argument("--audio", help="可选：已确认口播预听轨；时长必须与 EDL 一致")
    parser.add_argument("--quality", choices=["draft", "standard", "high"], default="draft")
    parser.add_argument("--manifest", help="权威 run_manifest.json；--render-execution 必填")
    parser.add_argument("--diagnostic-unbound", action="store_true",
                        help="只生成不可确认的本地诊断稿；不得与 --manifest 同用")
    parser.add_argument("--submit-visual-review",
                        help="提交 agent/human 手工逐帧复核 JSON；不代表客户确认")
    parser.add_argument("--execution-result",
                        help="视觉复核对应的 execution_preview_result.json")
    parser.add_argument("--revalidate-visual-qa", action="store_true",
                        help="重跑当前审计器并原子刷新 pending 证据")
    args = parser.parse_args(argv)

    if args.revalidate_visual_qa:
        if (args.render_execution or args.submit_visual_review or
                not args.execution_result or not args.manifest):
            parser.error("--revalidate-visual-qa 需配合 --execution-result/--manifest")
        payload = revalidate_execution_visual_qa(
            args.execution_result, manifest_path=args.manifest)
        payload["files"] = {
            "json": os.path.abspath(args.execution_result),
            "commercial_qc": os.path.join(
                os.path.dirname(os.path.abspath(args.execution_result)),
                "commercial_qc.json"),
        }
        payload["sequence"] = (payload.get("execution_preview") or {}).get(
            "layout_boards") or []
    elif args.submit_visual_review:
        if args.render_execution or not args.execution_result or not args.manifest:
            parser.error("--submit-visual-review 需配合 --execution-result/--manifest")
        payload = submit_execution_visual_review(
            args.execution_result, args.submit_visual_review,
            manifest_path=args.manifest)
        result_dir = os.path.dirname(os.path.abspath(args.execution_result))
        payload["files"] = {
            "json": os.path.abspath(args.execution_result),
            "final_frame_visual_qa": os.path.join(
                result_dir, "final_frame_visual_qa.json"),
            "commercial_qc": os.path.join(result_dir, "commercial_qc.json"),
        }
        payload["sequence"] = (payload.get("execution_preview") or {}).get(
            "layout_boards") or []
    else:
        plan = _load_json(args.plan) if args.plan else {}
    if not args.submit_visual_review and not args.revalidate_visual_qa and args.render_execution:
        if not args.plan:
            parser.error("--render-execution 必须配合 --plan")
        if args.manifest and args.diagnostic_unbound:
            parser.error("--manifest 与 --diagnostic-unbound 不能同时使用")
        if not args.manifest and not args.diagnostic_unbound:
            parser.error(
                "--render-execution 必须配合 --manifest；仅本地排错可显式传 --diagnostic-unbound")
        if args.manifest and args.quality != "high":
            parser.error(
                "绑定 manifest 的正式执行预演只接受 --quality high；低质量仅限 --diagnostic-unbound")
        payload = render_execution_preview(
            plan, _default_out_dir(plan, args.out_dir), source_plan=args.plan,
            human_image=args.human_image, preview_audio=args.audio, quality=args.quality,
            manifest_path=args.manifest, require_manifest=not args.diagnostic_unbound)
        files = {
            "json": payload["result_json"],
            "animatic": payload["animatic"]["path"],
        }
        payload = dict(payload, files=files, sequence=payload.get("shots") or [])
    elif not args.submit_visual_review and not args.revalidate_visual_qa:
        payload = write_preview(
            _default_out_dir(plan, args.out_dir),
            plan=plan,
            client=args.client,
            run_id=args.run_id,
            title=args.title,
        )
    if args.json:
        print(json.dumps({
            "ok": True,
            "html": payload["files"].get("html"),
            "svg": payload["files"].get("svg"),
            "json": payload["files"]["json"],
            "animatic": payload["files"].get("animatic"),
            "final_frame_visual_qa": payload["files"].get("final_frame_visual_qa"),
            "commercial_qc": payload["files"].get("commercial_qc"),
            "manifest_status": payload.get("manifest_status"),
            "shots": len(payload.get("sequence") or []),
            "modes": [item["visual_mode"] for item in payload.get("sequence") or []],
        }, ensure_ascii=False, indent=2))
    else:
        print(payload["files"].get("html") or payload["files"]["json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
