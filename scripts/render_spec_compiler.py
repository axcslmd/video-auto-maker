#!/usr/bin/env python3
"""Quality-by-construction compiler for the final renderer invocation.

The result is immutable: approved tokens, visual intent, ratio geometry,
audio choreography, engine props and encoder policy share one fingerprint.
Formal rendering is blocked while any prerequisite remains unresolved.
"""
import argparse
import copy
import json
import os
import re

import artifact_contract
import audio_contract as audio_contract_module
import design_token_contract
import layout_compiler
from project_utils import atomic_write_json
import schema_validate
import visual_intent


COMPOSITIONS = {
    "shots": "Shots", "content": "Content", "horizontal_kinetic": "HorizontalKinetic",
    "integrated_explainer": "IntegratedExplainer", "shotcraft": "Shotcraft",
}


def _duration(shot):
    try:
        start, end = float(shot.get("start_sec")), float(shot.get("end_sec"))
        if end > start:
            return end - start
    except (TypeError, ValueError):
        pass
    for key in ("duration", "seconds", "duration_sec"):
        try:
            value = float(shot.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    frames = shot.get("durationInFrames")
    fps = shot.get("fps") or shot.get("timeline_fps")
    if frames and fps:
        return float(frames) / float(fps)
    return 0.0


def _script_beats(dialogue, duration):
    chunks = [item.strip() for item in re.split(r"[。！？!?；;，,]", str(dialogue or "")) if item.strip()]
    if not chunks or duration <= 0:
        return []
    weights = [max(1, len(item)) for item in chunks]
    total, elapsed, beats = float(sum(weights)), 0.0, [0.0]
    for weight in weights[:-1]:
        elapsed += duration * weight / total
        beats.append(round(elapsed, 3))
    return beats


def _shot_maps(value):
    by_id, ordered = {}, []
    for item in (value or {}).get("shots") or []:
        if not isinstance(item, dict):
            continue
        ordered.append(item)
        sid = str(item.get("shot_id") or item.get("id") or item.get("seg_id") or "")
        if sid:
            by_id[sid] = item
    return by_id, ordered


def _plate_for_shot(plan, motion_design, shot, index):
    sid = str(shot.get("id") or shot.get("shot_id") or "shot_%02d" % index)
    candidates = []
    for container in (shot, (motion_design or {}).get("shots", [{}])[index - 1]
                      if index <= len((motion_design or {}).get("shots") or []) else {}):
        if not isinstance(container, dict):
            continue
        candidates.extend([
            container.get("base_plate"), container.get("base_plate_analysis"),
            ((container.get("execution_plan") or {}).get("base_plate_contract")
             if isinstance(container.get("execution_plan"), dict) else None),
        ])
    top_by_id, top_ordered = _shot_maps(plan.get("base_plate_analysis") or {})
    candidates.append(top_by_id.get(sid) or (top_ordered[index - 1] if index <= len(top_ordered) else None))
    return next((item for item in candidates if isinstance(item, dict) and item), {})


def compile_audio_choreography(plan, tokens, *, motion_design=None, audio_contract=None,
                               require_measured_audio=False):
    motion = tokens.get("motion") if isinstance(tokens.get("motion"), dict) else {}
    entry_default = max(1, int(motion.get("entry_duration_frames") or 16))
    settle_default = max(1, int(motion.get("settle_duration_frames") or 12))
    fps = int(plan.get("fps") or 30)
    unresolved, timeline, cursor = [], [], 0
    for index, shot in enumerate(plan.get("shots") or [], 1):
        sid = str(shot.get("id") or shot.get("shot_id") or "shot_%02d" % index)
        duration = _duration(shot)
        if duration <= 0 and shot.get("durationInFrames"):
            duration = float(shot["durationInFrames"]) / float(fps)
        if duration <= 0:
            unresolved.append("%s:duration_missing" % sid)
            duration = 1.0
        frames = max(1, int(round(duration * fps)))
        plate = _plate_for_shot(plan, motion_design, shot, index)
        beats = []
        for value in plate.get("speech_beats_seconds") or []:
            try:
                value = float(value)
                if 0 <= value < duration:
                    beats.append(value)
            except (TypeError, ValueError):
                pass
        beat_status = str(plate.get("speech_beats_status") or "")
        if beats and beat_status in ("explicit_observation", "measured", "audio_aligned"):
            beat_source = "measured_or_observed_audio"
        elif beats:
            beat_source = "authored_or_estimated_plate"
        else:
            beats = _script_beats(
                shot.get("dialogue") or (shot.get("audio_contract") or {}).get("dialogue") or shot.get("text"),
                duration)
            beat_source = "estimated_from_script" if beats else "duration_only"
        if require_measured_audio and beat_source != "measured_or_observed_audio":
            unresolved.append("%s:measured_audio_beats_required" % sid)
        beat_frames = sorted({max(0, min(frames - 1, int(round(value * fps)))) for value in beats})
        entry = min(entry_default, max(1, frames // 4))
        exit_frames = min(int(motion.get("transition_frames") or 12), max(1, frames // 4))
        settle = min(settle_default, max(1, frames - entry - exit_frames))
        proof_start = beat_frames[-1] if beat_frames else max(entry, frames - exit_frames - settle)
        timeline.append({
            "shot_id": sid,
            "from_frame": cursor,
            "duration_frames": frames,
            "beat_source": beat_source,
            "beat_frames": beat_frames,
            "phases": {
                "enter": [0, entry],
                "establish": [entry, max(entry, proof_start)],
                "proof_or_emphasis": [proof_start, max(proof_start, frames - exit_frames)],
                "exit": [max(0, frames - exit_frames), frames],
            },
            "focus_budget": {
                "primary": {"max_active": 1, "window": [entry, max(entry, frames - exit_frames)]},
                "secondary": {"max_active": 1, "window": [proof_start, max(proof_start, frames - exit_frames)]},
                "max_simultaneous": 2,
            },
        })
        cursor += frames
    return {
        "schema_version": 1,
        "timeline_owner": "audio_first_motion_choreography",
        "fps": fps,
        "total_frames": cursor,
        "active_motion_limit": float(motion.get("active_motion_limit") or 0.33),
        "shots": timeline,
        "unresolved": unresolved,
        "audio_contract_sha256": artifact_contract.sha256_json(audio_contract or {}),
    }


def _composition(props, explicit=None):
    if explicit:
        normalized = str(explicit).strip().lower().replace("-", "_")
        value = COMPOSITIONS.get(normalized) or explicit
        if value not in COMPOSITIONS.values():
            raise ValueError("RENDER_SPEC_COMPOSITION_INVALID: %s" % explicit)
        return value
    if isinstance(props, dict):
        scenes = props.get("scenes") or []
        if scenes and any(isinstance(item, dict) and "visualMode" in item for item in scenes):
            return "IntegratedExplainer"
        if scenes:
            return "Content"
        shots = props.get("shots") or []
        if shots and any(isinstance(item, dict) and (item.get("card_id") or item.get("shotcraft_card_id")) for item in shots):
            return "Shotcraft"
        if shots and any(isinstance(item, dict) and item.get("durationInFrames") for item in shots):
            return "Shots"
    return "Content"


def _target(plan, props, ratio):
    standard = layout_compiler.RATIO_DIMENSIONS[ratio]
    width = int((props or {}).get("width") or plan.get("width") or standard[0])
    height = int((props or {}).get("height") or plan.get("height") or standard[1])
    return {"ratio": ratio, "width": width, "height": height,
            "fps": int((props or {}).get("fps") or plan.get("fps") or 30)}


def _inject_executable_layout(props, layout, ratio):
    """Copy compiled geometry onto the exact scenes the renderer consumes."""
    if not isinstance(props, dict):
        return
    compiled = {
        str(item.get("shot_id")): (item.get("layouts") or {}).get(ratio)
        for item in layout.get("shots") or [] if isinstance(item, dict)
    }
    for index, scene in enumerate(props.get("scenes") or [], 1):
        if not isinstance(scene, dict):
            continue
        sid = str(scene.get("id") or scene.get("source_shot_id") or
                  scene.get("shot_id") or "shot_%02d" % index)
        selected = compiled.get(sid)
        if not selected:
            continue
        scene["layoutFamily"] = selected.get("layout_family")
        scene["layoutRegions"] = copy.deepcopy(selected.get("regions") or {})
        scene["contentCapacity"] = copy.deepcopy(
            selected.get("content_capacity") or {})


def compile_spec(plan, tokens, *, motion_design=None, audio_contract=None, engine_props=None,
                 ratios=None, composition=None, render_profile="direct_delivery",
                 formal=False, require_measured_audio=False, source_contracts=None):
    plan = plan if isinstance(plan, dict) else {}
    tokens = design_token_contract.validate(copy.deepcopy(tokens or {}))
    unresolved = []
    if int(tokens.get("schema_version") or 0) != 3:
        unresolved.append("design_tokens_v3_required")
    intent = visual_intent.compile_plan(plan, strict=formal)
    layout = layout_compiler.compile_layouts(
        plan, intent, tokens, ratios=ratios, strict=False)
    unresolved.extend(layout.get("unresolved") or [])
    if audio_contract:
        audio_contract_module.validate(audio_contract)
    choreography = compile_audio_choreography(
        plan, tokens, motion_design=motion_design, audio_contract=audio_contract,
        require_measured_audio=require_measured_audio)
    unresolved.extend(choreography.get("unresolved") or [])
    props = copy.deepcopy(engine_props) if isinstance(engine_props, dict) else None
    selected_ratio = layout["ratios"][0]
    target = _target(plan, props, selected_ratio)
    try:
        actual_ratio = layout_compiler.normalize_ratio(None, target["width"], target["height"])
        if actual_ratio != selected_ratio:
            unresolved.append("engine_props_ratio_mismatch:%s!=%s" % (actual_ratio, selected_ratio))
    except ValueError:
        unresolved.append("engine_props_ratio_unsupported")
    if props is None:
        unresolved.append("engine_props_required")
    else:
        props["width"], props["height"], props["fps"] = (
            target["width"], target["height"], target["fps"])
        props["brandPrimary"] = design_token_contract.primary(tokens, props.get("brandPrimary") or "#64748B")
        props["designTokens"] = tokens
        props["visualIntentContract"] = intent
        props["layoutContract"] = layout
        props["audioChoreography"] = choreography
        _inject_executable_layout(props, layout, selected_ratio)
    if render_profile not in ("preview", "direct_delivery", "commercial_master"):
        raise ValueError("RENDER_SPEC_PROFILE_INVALID: %s" % render_profile)
    unresolved = list(dict.fromkeys(unresolved))
    result = {
        "schema_version": 1,
        "type": "render_quality_spec",
        "mode": "formal" if formal else "draft",
        "target": target,
        "design_tokens": tokens,
        "visual_intent": intent,
        "layout_contract": layout,
        "audio_choreography": choreography,
        "renderer": {
            "engine": "remotion",
            "composition": _composition(props, composition),
            "render_profile": render_profile,
            "frame_format": "png" if render_profile != "preview" else "jpeg",
            "color_space": "bt709",
            "audio_sample_rate": 48000,
            "single_lossy_encode": True,
        },
        "source_contracts": {
            "plan_sha256": artifact_contract.sha256_json(plan),
            "design_tokens_sha256": artifact_contract.sha256_json(tokens),
            "motion_design_sha256": artifact_contract.sha256_json(motion_design or {}),
            "audio_contract_sha256": artifact_contract.sha256_json(audio_contract or {}),
            "engine_props_sha256": artifact_contract.sha256_json(engine_props or {}),
            **(source_contracts or {}),
        },
        "engine_props": props,
        "unresolved": unresolved,
    }
    result["contract_sha256"] = artifact_contract.sha256_json(result)
    schema_validate.enforce(result, "render-quality-spec", context="render_spec_compiler.compile_spec")
    if formal and unresolved:
        raise ValueError("RENDER_SPEC_UNRESOLVED: %s" % "; ".join(unresolved))
    return result


def _load(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile an immutable pre-render quality spec")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--style-tokens", required=True)
    parser.add_argument("--motion-design")
    parser.add_argument("--audio-contract")
    parser.add_argument("--engine-props")
    parser.add_argument("--composition")
    parser.add_argument("--ratio", action="append", dest="ratios")
    parser.add_argument("--profile", choices=["preview", "direct_delivery", "commercial_master"],
                        default="direct_delivery")
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--require-measured-audio", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = compile_spec(
        _load(args.plan), _load(args.style_tokens),
        motion_design=_load(args.motion_design), audio_contract=_load(args.audio_contract),
        engine_props=_load(args.engine_props), ratios=args.ratios,
        composition=args.composition, render_profile=args.profile, formal=args.formal,
        require_measured_audio=args.require_measured_audio,
        source_contracts={
            "plan_path": os.path.abspath(args.plan),
            "design_tokens_path": os.path.abspath(args.style_tokens),
            "engine_props_path": os.path.abspath(args.engine_props) if args.engine_props else None,
        })
    atomic_write_json(args.out, result)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "mode": result["mode"], "unresolved": result["unresolved"],
                      "contract_sha256": result["contract_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
