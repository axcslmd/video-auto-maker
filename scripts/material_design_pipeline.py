#!/usr/bin/env python3
"""Compile an arbitrary material/design-board plan into executable artifacts.

The input is authored by the Agent/LLM.  This module measures and binds source
assets, asks the existing motion designer for concrete tracks, then emits the
JSON contracts consumed by Remotion and HyperFrames.  It contains no brand,
slide-count, component, or visual-position assumptions.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import design_inputs
import hf_engine
import motion_design
import video_use_bridge
import key_setup
from project_utils import atomic_write_json


def _write(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), value)
    return str(path)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("MATERIAL_PLAN_INVALID: director plan must be an object")
    return value


def _merge_design(plan, design, material, base):
    shots = []
    designs = {str(item.get("shot_id")): item for item in design.get("shots") or []
               if isinstance(item, dict)}
    plates = {str(item.get("shot_id")): item for item in base.get("shots") or []
              if isinstance(item, dict)}
    for raw in plan.get("shots") or []:
        if not isinstance(raw, dict):
            continue
        shot = dict(raw)
        sid = str(shot.get("id") or shot.get("shot_id"))
        authored = designs.get(sid) or {}
        plate = shot.get("base_plate") or plates.get(sid) or {}
        if plate:
            shot["base_plate"] = plate
        if authored.get("motion_plan"):
            shot["motion_plan"] = authored["motion_plan"]
        if authored.get("execution_plan"):
            shot["execution_plan"] = authored["execution_plan"]
        for key in ("asset_slots", "motion_elements", "video_safe_zones", "subtitle", "motion_overlay"):
            if key in authored:
                shot[key] = authored[key]
        # The material interpretation is provenance for components and overlays;
        # it is not passed as a prompt to a video model.
        shot["material_interpretation_ref"] = material.get("source_refs") or []
        shots.append(shot)
    merged = dict(plan)
    merged["shots"] = shots
    merged["material_interpretation"] = material
    merged["base_plate_analysis"] = base
    return merged


def _hyperframes_spec(plan, out_dir):
    fps = int(plan.get("fps") or 30)
    output_ratio = str(plan.get("output_ratio") or plan.get("aspect_ratio") or "16:9").lower()
    resolution = [1080, 1920] if output_ratio in {"9:16", "portrait", "vertical"} else \
        [1080, 1080] if output_ratio in {"1:1", "square"} else [1920, 1080]
    cursor = 0.0
    scenes = []
    for shot_index, shot in enumerate(plan.get("shots") or []):
        duration = float(shot.get("duration") or shot.get("duration_sec") or shot.get("seconds") or 1)
        motion = shot.get("motion_plan") if isinstance(shot.get("motion_plan"), dict) else {}
        compiled = hf_engine.compile_motion_plan_to_spec(
            dict(motion, duration=duration), resolution=resolution, fps=fps,
            brand=plan.get("brand") or plan.get("brand_kit") or {},
            background={"type": "transparent"}, start=cursor, end=cursor + duration)
        for local_index, scene in enumerate(compiled.get("scenes") or []):
            if scene.get("slot") != "subtitle":
                scene["track_index"] = 2 + shot_index * 100 + local_index
        scenes.extend(compiled.get("scenes") or [])
        cursor += duration
    return {
        "schema_version": 1,
        "type": "agent_authored_hyperframes_execution_spec",
        "resolution": resolution,
        "fps": fps,
        "duration": round(cursor, 3),
        "background": {"type": "transparent"},
        "brand": plan.get("brand") or plan.get("brand_kit") or {},
        "design_board": "agent_or_llm_authored",
        "source_material_is_not_playback": True,
        "base_plate_analysis": plan.get("base_plate_analysis") or {},
        "scenes": scenes,
    }


def compile_execution_bundle(plan, *, api_key=None, model=None, require_llm=False,
                             offline_fallback=False, observations=None,
                             vision_analyzer=None, material_interpretation=None,
                             base_plate_analysis=None, out_dir=None):
    """Return and optionally write the complete renderer bundle."""
    plan = dict(plan)
    material = material_interpretation or plan.get("material_interpretation")
    if not isinstance(material, dict):
        material = design_inputs.interpret_materials(plan, vision_analyzer=vision_analyzer)
    base = base_plate_analysis or plan.get("base_plate_analysis")
    if not isinstance(base, dict):
        base = design_inputs.analyze_base_plate(plan, observations=observations)
    if require_llm:
        if material.get("visual_assets") and not material.get("visual_interpretations"):
            raise ValueError(
                "MATERIAL_VISION_LLM_REQUIRED: 素材包含图片，但尚未完成视觉 LLM 解读")
        unresolved_plate = [item for item in base.get("shots") or []
                            if item.get("action_status") == "requires_llm_visual_review" or
                            item.get("region_status") == "requires_llm_visual_review"]
        if unresolved_plate:
            raise ValueError(
                "BASE_PLATE_VISION_REVIEW_REQUIRED: 底片动作/占位区域尚未完成视觉复核")
    design = motion_design.design_from_plan(
        plan, api_key=api_key, model=model, require_llm=require_llm,
        offline_fallback=offline_fallback, material_interpretation=material,
        base_plate_analysis=base)
    merged = _merge_design(plan, design, material, base)
    fusion = video_use_bridge.build_fusion_plan(merged, composition_mode=merged.get("composition_mode"))
    content = video_use_bridge.build_content_spec(merged, fusion["edl"])
    hyperframes = _hyperframes_spec(merged, out_dir)
    bundle = {
        "schema_version": 1,
        "type": "agent_material_motion_execution_bundle",
        "design_engine": design.get("design_engine") or {},
        "material_interpretation": material,
        "base_plate_analysis": base,
        "motion_design": design,
        "execution_contract": {
            "remotion": "content_spec.json",
            "hyperframes": "hyperframes_spec.json",
            "human_plate_is_timing_master": True,
            "content_must_adapt_to_plate": True,
            "video_model_receives_clean_plate_only": True,
        },
        "plan": merged,
        "fusion_plan": fusion,
        "content_spec": content,
        "hyperframes_spec": hyperframes,
    }
    if out_dir:
        root = Path(out_dir).resolve()
        _write(root / "material_interpretation.json", material)
        _write(root / "base_plate_analysis.json", base)
        _write(root / "motion_design.json", design)
        _write(root / "content_spec.json", content)
        _write(root / "hyperframes_spec.json", hyperframes)
        _write(root / "fusion_plan.json", fusion)
        _write(root / "edl.json", fusion["edl"])
        _write(root / "execution_bundle.json", bundle)
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generic material + base plate -> Remotion/HyperFrames execution bundle")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--model")
    parser.add_argument("--require-llm", action="store_true")
    parser.add_argument("--offline-fallback", action="store_true")
    args = parser.parse_args(argv)
    key_setup.ensure_session_id()
    api_key = key_setup.load_key()
    vision_analyzer = design_inputs.basicrouter_vision_analyzer(api_key)
    bundle = compile_execution_bundle(
        _read(args.plan), api_key=api_key, model=args.model,
        require_llm=args.require_llm, offline_fallback=args.offline_fallback,
        vision_analyzer=vision_analyzer, out_dir=args.out_dir)
    print(json.dumps({
        "ok": True,
        "out_dir": str(Path(args.out_dir).resolve()),
        "design_mode": bundle.get("design_engine", {}).get("mode"),
        "material_status": bundle["material_interpretation"].get("interpretation_status"),
        "base_plate_status": bundle["base_plate_analysis"].get("analysis_status"),
        "shots": len(bundle["plan"].get("shots") or []),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
