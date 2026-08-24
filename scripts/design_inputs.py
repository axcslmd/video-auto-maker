#!/usr/bin/env python3
"""Generic inputs for document-derived motion design.

This module is deliberately brand and shot-count agnostic.  It measures the
source plate and records material provenance; it does not invent a layout or
creative direction.  Editorial observations may be supplied by the agent or
by a vision LLM and are kept separate from measured facts.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import br_client


HERE = Path(__file__).resolve().parent


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


def _ratio(width, height):
    if not width or not height:
        return None
    divisor = math.gcd(int(width), int(height))
    return "%d:%d" % (int(width) // divisor, int(height) // divisor)


def _resolve_path(value, base_dir=None):
    if not value or not isinstance(value, (str, os.PathLike)):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir:
        path = Path(base_dir) / path
    return str(path.resolve()) if path.exists() else None


def _probe_media(path):
    """Measure video stream facts without making assumptions about the shot."""
    try:
        import fuse
        fuse.ensure_ffmpeg_on_path()
    except ImportError:
        pass
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to measure a base plate")
    result = subprocess.check_output([
        ffprobe, "-v", "error", "-show_entries",
        "stream=width,height,r_frame_rate,codec_name:format=duration",
        "-select_streams", "v:0", "-of", "json", path,
    ], stderr=subprocess.STDOUT).decode("utf-8")
    data = json.loads(result)
    stream = (data.get("streams") or [{}])[0]
    rate = str(stream.get("r_frame_rate") or "0/1").split("/")
    fps = 0.0
    if len(rate) == 2 and float(rate[1] or 0):
        fps = round(float(rate[0]) / float(rate[1]), 3)
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    return {
        "path": str(Path(path).resolve()),
        "dimensions": [width, height],
        "aspect_ratio": _ratio(width, height),
        "fps": fps,
        "codec": stream.get("codec_name"),
        "duration_seconds": round(float((data.get("format") or {}).get("duration") or 0), 3),
    }


def _shot_source(plan, shot, index, base_dir=None):
    value = _first(shot, "human_video", "plate_path", "video_path", "video",
                   "human_path", "out_path", "source_video")
    if not value and isinstance(shot.get("base_plate"), dict):
        value = _first(shot["base_plate"], "path", "video", "human_video", "source")
        if isinstance(value, dict):
            value = _first(value, "path", "ref")
    if not value and isinstance(shot.get("base_plate"), str):
        value = shot.get("base_plate")
    if not value:
        segments = plan.get("human_segments") or plan.get("segments") or []
        if isinstance(segments, dict):
            value = (segments.get(str(shot.get("id"))) or
                     segments.get(str(index)) or
                     segments.get("shot_%02d" % index))
        else:
            for item in segments:
                if isinstance(item, dict) and str(item.get("id") or item.get("shot_id")) == str(
                        shot.get("id") or ""):
                    value = _first(item, "out_path", "path", "video", "human_video")
                    break
    return _resolve_path(value, base_dir or plan.get("base_dir"))


def _speech_beats(dialogue, duration):
    """Return explicitly estimated beats, never present them as audio facts."""
    if not dialogue or not duration:
        return []
    chunks = [part.strip() for part in re.split(r"[。！？!?；;，,]", str(dialogue)) if part.strip()]
    if len(chunks) <= 1:
        return [0.0]
    weights = [max(1, len(re.findall(r"\w", chunk))) for chunk in chunks]
    total = float(sum(weights))
    beats, elapsed = [0.0], 0.0
    for weight in weights[:-1]:
        elapsed += float(duration) * weight / total
        beats.append(round(elapsed, 3))
    return beats


def analyze_base_plate(plan, *, observations=None, base_dir=None):
    """Create a generic base-plate contract from plan fields and real media.

    ``observations`` is intentionally an input, not a default.  It may contain
    LLM/agent observations keyed by shot id: action, occupied_region,
    content_region, avoid_region, and speech_beats_seconds.
    """
    plan = plan if isinstance(plan, dict) else {}
    observations = observations if isinstance(observations, dict) else {}
    shots = plan.get("shots") if isinstance(plan.get("shots"), list) else []
    target_ratio = plan.get("composition_aspect_ratio") or plan.get("output_ratio") or plan.get("aspect_ratio")
    items = []
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("id") or shot.get("shot_id") or "shot_%02d" % index)
        source = _shot_source(plan, shot, index, base_dir=base_dir)
        measured = {}
        media_status = "missing_source"
        if source:
            try:
                measured = _probe_media(source)
                media_status = "measured"
            except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                media_status = "measurement_failed"
                measured = {"path": source, "measurement_error": str(exc)}
        duration = float(shot.get("duration") or shot.get("duration_sec") or shot.get("seconds") or 0)
        actual_duration = float(measured.get("duration_seconds") or 0)
        observation = observations.get(sid) if isinstance(observations.get(sid), dict) else {}
        observation = dict(shot.get("base_plate_observation") or {}, **observation)
        dialogue = str(_first(shot, "dialogue", "voiceover", "talk", "text") or "")
        beats = observation.get("speech_beats_seconds")
        if not isinstance(beats, list):
            beats = _speech_beats(dialogue, duration or actual_duration)
            beat_status = "estimated_from_script" if beats else "missing"
        else:
            beat_status = "explicit_observation"
        action = _first(observation, "action", "action_observation", "character_action")
        regions = {
            key: observation.get(key)
            for key in ("occupied_region", "content_region", "avoid_region")
            if observation.get(key) is not None
        }
        shot_item = {
            "shot_id": sid,
            "source": measured,
            "path": measured.get("path"),
            "aspect_ratio": measured.get("aspect_ratio"),
            "duration_seconds": measured.get("duration_seconds"),
            "media_status": media_status,
            "target_duration_seconds": round(duration, 3),
            "measured_duration_seconds": round(actual_duration, 3),
            "duration_delta_seconds": round(actual_duration - duration, 3) if actual_duration and duration else None,
            "duration_status": ("aligned" if actual_duration and duration and abs(actual_duration - duration) <= 0.12
                                 else "needs_timeline_review" if actual_duration and duration else "unknown"),
            "dialogue": dialogue,
            "dialogue_status": "provided" if dialogue else "missing",
            "speech_beats_seconds": beats,
            "speech_beats_status": beat_status,
            "action_observation": action,
            "action_status": "explicit_observation" if action else "requires_llm_visual_review",
            "occupied_region": regions.get("occupied_region"),
            "content_region": regions.get("content_region"),
            "avoid_region": regions.get("avoid_region"),
            "region_status": "explicit_observation" if regions else "requires_llm_visual_review",
            "base_plate_ref_required": True,
        }
        items.append(shot_item)
    known_ratios = {item["source"].get("aspect_ratio") for item in items if item["source"].get("aspect_ratio")}
    source_ratio = sorted(known_ratios)[0] if len(known_ratios) == 1 else None
    return {
        "schema_version": 1,
        "type": "base_plate_analysis",
        "analysis_owner": "agent_design_layer",
        "analysis_status": "measured_plus_explicit_observation" if any(
            item["action_status"] == "explicit_observation" for item in items) else "measured_only_or_pending_visual_review",
        "composition_contract": {
            "base_plate_is_timing_master": True,
            "content_must_adapt_to_base_plate": True,
            "human_video_is_not_a_generic_background": True,
            "source_aspect_ratio": source_ratio,
            "composition_aspect_ratio": target_ratio,
            "content_safe_area_policy": "derive from occupied_region/avoid_region; do not place authored content over unresolved regions",
        },
        "shots": items,
    }


def _material_paths(plan):
    refs = plan.get("asset_refs") if isinstance(plan.get("asset_refs"), dict) else {}
    values = []
    for key in ("images", "image_paths", "document_images", "ppt_pages", "source_images", "visual_refs"):
        values.extend(_as_list(refs.get(key)))
    values.extend(_as_list(plan.get("material_images")))
    base_dir = plan.get("base_dir")
    result = []
    for value in values:
        path = value.get("path") if isinstance(value, dict) else value
        resolved = _resolve_path(path, base_dir)
        if resolved and resolved not in result:
            result.append(resolved)
    return result


def interpret_materials(plan, *, vision_analyzer=None, max_images=8):
    """Extract source facts and optionally ask a vision LLM to interpret images.

    The returned status is ``llm_interpreted`` only when the injected analyzer
    actually returns a result.  Without it, callers must keep the run in a
    review/pending state rather than inventing creative content.
    """
    plan = plan if isinstance(plan, dict) else {}
    source_document = _first(plan, "source_ppt", "source_doc", "source_pdf", "source_document")
    result = {
        "schema_version": 1,
        "type": "material_interpretation",
        "interpretation_owner": "agent_design_layer",
        "source_document": str(Path(source_document).resolve()) if source_document and Path(source_document).exists() else source_document,
        "source_refs": [],
        "text_blocks": [],
        "visual_assets": [],
        "visual_interpretations": [],
        "interpretation_status": "pending_llm_or_agent_review",
        "unresolved": [],
    }
    if source_document and os.path.isfile(source_document):
        try:
            import doc_extract
            result["text_blocks"] = doc_extract.extract(source_document)
            result["source_refs"].append({"path": result["source_document"], "kind": "source_document"})
            result["embedded_image_count"] = doc_extract.count_images(source_document)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            result["unresolved"].append({"kind": "document_extraction", "reason": str(exc)})
    image_paths = _material_paths(plan)[:max(1, int(max_images))]
    for path in image_paths:
        result["visual_assets"].append({"path": path, "status": "available_for_review"})
    if vision_analyzer:
        for path in image_paths:
            try:
                analysis = vision_analyzer(path, plan)
                if analysis:
                    result["visual_interpretations"].append({"path": path, "analysis": analysis})
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                result["unresolved"].append({"path": path, "kind": "vision_analysis", "reason": str(exc)})
    if result["visual_interpretations"] or result["text_blocks"]:
        result["interpretation_status"] = (
            "llm_interpreted" if result["visual_interpretations"] else "text_extracted_visual_review_pending")
    if not result["text_blocks"] and not result["visual_interpretations"]:
        result["interpretation_status"] = "pending_llm_or_agent_review"
    return result


def basicrouter_vision_analyzer(api_key, *, model=None):
    """Return an injected analyzer using the public BasicRouter vision API."""
    if not api_key:
        return None

    def analyze(path, plan):
        question = (
            "分析这张来源素材图，严格输出 JSON：{facts:[{text,confidence}], "
            "visual_structure:[...], objects:[...], layout_regions:[...], "
            "motion_opportunities:[...], unresolved:[...]}. "
            "只描述看见的事实，不生成字幕，不补写不存在的品牌信息。项目背景：%s" %
            json.dumps({"title": plan.get("title"), "source_document": plan.get("source_document")}, ensure_ascii=False))
        return br_client.analyze_image(api_key, path, question, model=model)

    return analyze
