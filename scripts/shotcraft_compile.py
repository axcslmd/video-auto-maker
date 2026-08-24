#!/usr/bin/env python3
"""Compile approved storyboard postproduction plans into a deterministic Shotcraft spec."""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schema_validate

REGISTRY = os.path.join(ROOT, "remotion_engine", "shotcraft", "registry.json")


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _ratio(width, height):
    if width == height:
        return "1:1"
    return "16:9" if width > height else "9:16"


def _cards(path=REGISTRY):
    data = _load(path)
    return {card["id"]: card for card in data.get("cards") or []}


def _canonical_sha256(value):
    payload = json.dumps(value or {}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compile_spec(plan, design_tokens, *, registry_path=REGISTRY):
    """Compile only declared Shotcraft cards; no implicit decorative animation."""
    shots = plan.get("shots") or []
    if not shots:
        raise ValueError("SHOTCRAFT_PLAN_NO_SHOTS")
    width, height = plan.get("width", 1080), plan.get("height", 1920)
    fps = int(plan.get("fps", 30))
    ratio = _ratio(width, height)
    cards = _cards(registry_path)
    token_sha = _canonical_sha256(design_tokens)
    result = []
    for index, shot in enumerate(shots, 1):
        post = shot.get("postproduction") or {}
        if post.get("engine") != "shotcraft":
            continue
        card_id = post.get("card_id")
        card = cards.get(card_id)
        if not card:
            raise ValueError("SHOTCRAFT_CARD_UNKNOWN: %s" % card_id)
        if ratio not in card.get("ratios", []):
            raise ValueError("SHOTCRAFT_CARD_RATIO_UNSUPPORTED: %s/%s" % (card_id, ratio))
        assets = post.get("assets") or []
        if not isinstance(assets, list):
            raise ValueError("SHOTCRAFT_ASSETS_INVALID: %s" % card_id)
        duration = shot.get("duration") or shot.get("seconds") or 5
        frames = max(1, int(round(float(duration) * fps)))
        primary_effect = post.get("primary_effect") or card.get("primary_effect")
        renderer = post.get("renderer") or card.get("renderer")
        timing = post.get("timing") if isinstance(post.get("timing"), dict) else {}
        hold_frames = int(timing.get("hold_frames") or max(
            int(card.get("hold_min_frames") or 0),
            int((design_tokens.get("motion") or {}).get("hold_min_frames") or 15)))
        transition = post.get("transition") if isinstance(post.get("transition"), dict) else {
            "style": "none" if index == len(shots) else card.get("transition_style", "fade"),
            "duration_frames": 0 if index == len(shots) else max(4, int(round(fps * 0.4))),
            "beat_frame": None if index == len(shots) else
                          frames - max(4, int(round(fps * 0.4))),
            "beat_source": "compiler_default",
        }
        motion_blur = post.get("motion_blur") if isinstance(post.get("motion_blur"), dict) else {
            "enabled": bool(card.get("requires_motion_blur")),
            "shutter_angle": 180,
            "samples": 10,
        }
        post_token_sha = post.get("brand_token_sha256")
        if post_token_sha and post_token_sha != token_sha:
            raise ValueError("SHOTCRAFT_BRAND_TOKEN_MISMATCH: %s" %
                             (shot.get("id") or "shot_%02d" % index))
        result.append({
            "id": str(shot.get("id") or "shot_%02d" % index),
            "card_id": card_id,
            "card_version": post.get("card_version", "1"),
            "durationInFrames": frames,
            "source": shot.get("expanded_image") or shot.get("video") or shot.get("image"),
            "postproduction": post,
            "assets": assets,
            "motion_intent": post.get("motion_intent") or post.get("intent") or
                             shot.get("motion_intent") or "hero",
            "shotcraft_card_id": card_id,
            "renderer": renderer,
            "primary_effect": primary_effect,
            "hold_frames": hold_frames,
            "transition": transition,
            "motion_blur": motion_blur,
            "motion_speed": card.get("motion_speed", "medium"),
            "brand_token_sha256": token_sha,
            "safe_zone_provenance": post.get("safe_zone_provenance") or {
                "source": "not_required", "status": "verified", "analysis_ref": None},
            "text_ownership": post.get("text_ownership") or "hyperframes",
            "safe_zones": post.get("safe_zones") or shot.get("video_safe_zones") or [],
            "qa_frames": post.get("qa_frames") or [max(0, int(frames * x)) for x in card.get("qa_offsets", [])],
        })
    if not result:
        raise ValueError("SHOTCRAFT_NO_DECLARED_SHOTS")
    spec = {"schema_version": 1, "width": width, "height": height, "fps": fps,
            "theme": design_tokens, "brand_token_sha256": token_sha, "shots": result}
    schema_validate.enforce(spec, "shotcraft-spec", context="shotcraft_compile.compile_spec")
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compile approved Shotcraft postproduction specs")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--style-tokens", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--registry", default=REGISTRY)
    args = parser.parse_args(argv)
    spec = compile_spec(_load(args.plan), _load(args.style_tokens), registry_path=args.registry)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out), "shots": len(spec["shots"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
