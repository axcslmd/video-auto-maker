#!/usr/bin/env python3
"""Compile-time ownership and geometry contract for composite preview layers.

The module prevents a renderer from painting text merely because a string is
available.  Remotion owns content copy, HyperFrames owns spoken captions and
explicitly exclusive overlays, and presenter/content regions are reserved
before either engine renders a frame.
"""
import re


CONTRACT_VERSION = 1
VISIBLE_KEYS = {
    "title", "subtitle", "kicker", "eyebrow", "label", "description", "value",
    "quote", "text", "cta", "tags", "items", "steps", "features", "nodes",
    "edges", "details", "headers", "rows", "cells", "badges", "highlights",
}
NON_COPY_KEYS = {
    "id", "icon", "image", "src", "path", "motion", "accent", "color",
    "status", "type", "from", "to", "x", "y", "w", "h", "width", "height",
}


def normalize_copy(value):
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def _collect_visible_strings(value, *, enabled=True):
    values = []
    if isinstance(value, str):
        if enabled and value.strip() and not value.startswith(("/", "file:", "http:")):
            values.append(value.strip())
        return values
    if isinstance(value, list):
        for item in value:
            values.extend(_collect_visible_strings(item, enabled=enabled))
        return values
    if not isinstance(value, dict):
        return values
    for key, item in value.items():
        key_name = str(key).lower()
        if key_name in NON_COPY_KEYS:
            continue
        values.extend(_collect_visible_strings(
            item, enabled=enabled and (key_name in VISIBLE_KEYS or not isinstance(item, str))))
    return values


def content_copy_by_shot(content_spec):
    result = {}
    for index, scene in enumerate((content_spec or {}).get("scenes") or [], 1):
        if not isinstance(scene, dict):
            continue
        shot_id = str(scene.get("source_shot_id") or scene.get("sceneId") or
                      scene.get("id") or "shot_%02d" % index)
        copy = _collect_visible_strings(scene.get("props") or {})
        result[shot_id] = {
            normalize_copy(item): item for item in copy if normalize_copy(item)
        }
    return result


def resolve_hyperframes_ownership(scenes, content_spec):
    """Keep only copy with an unambiguous HyperFrames ownership claim."""
    remotion_copy = content_copy_by_shot(content_spec)
    resolved = []
    suppressed = []
    for raw in scenes or []:
        if not isinstance(raw, dict):
            continue
        scene = dict(raw)
        shot_id = str(scene.get("source_shot_id") or "")
        slot = str(scene.get("slot") or "")
        text_key = normalize_copy(scene.get("text"))
        reason = None
        if scene.get("source_kind") != "audience_copy":
            reason = "non_audience_source"
        elif scene.get("owner") != "hyperframes":
            reason = "renderer_does_not_own_copy"
        elif slot != "subtitle" and text_key in remotion_copy.get(shot_id, {}):
            reason = "remotion_already_owns_visible_copy"
        elif slot != "subtitle" and not (
                scene.get("exclusive_owner") is True and
                scene.get("collision_policy") == "overlay_exclusive"):
            reason = "missing_exclusive_overlay_contract"
        if reason:
            suppressed.append({
                "source_shot_id": shot_id, "slot": slot,
                "semantic_id": scene.get("semantic_id"), "text": scene.get("text"),
                "reason": reason,
            })
            continue
        resolved.append(scene)
    return resolved, suppressed


def _layout_regions(shot):
    mode = str((shot or {}).get("visual_mode") or "")
    presenter_left = str((shot or {}).get("slot") or "").lower() == "left"
    if mode == "stacked_top_bottom":
        return {
            "content": {"x": .06, "y": .04, "w": .88, "h": .38},
            "subtitle": {"x": .20, "y": .44, "w": .60, "h": .12},
            "presenter": {"x": .31, "y": .59, "w": .38, "h": .37},
        }
    if mode == "split_left_right":
        return {
            "content": {"x": .48 if presenter_left else .04, "y": .06, "w": .48, "h": .72},
            "subtitle": {"x": .20, "y": .82, "w": .60, "h": .12},
            "presenter": {"x": .04 if presenter_left else .56, "y": .07, "w": .40, "h": .70},
        }
    return {
        "content": {"x": .06, "y": .06, "w": .88, "h": .72},
        "subtitle": {"x": .18, "y": .82, "w": .64, "h": .12},
        "presenter": {"x": .55, "y": .08, "w": .40, "h": .70},
    }


def _pixel_rect(scene, width, height):
    w = float(scene.get("width_px") or width * .5)
    h = float(scene.get("max_height_px") or max(72, float(scene.get("size") or 44) * 2.8))
    if scene.get("left_px") is not None:
        x = float(scene["left_px"])
    elif scene.get("right_px") is not None:
        x = width - float(scene["right_px"]) - w
    else:
        x = (width - w) / 2
    if scene.get("top_px") is not None:
        y = float(scene["top_px"])
    elif scene.get("bottom_px") is not None:
        y = height - float(scene["bottom_px"]) - h
    else:
        y = (height - h) / 2
    return {"x": x / width, "y": y / height, "w": w / width, "h": h / height}


def _overlap(a, b):
    return (min(a["x"] + a["w"], b["x"] + b["w"]) > max(a["x"], b["x"]) and
            min(a["y"] + a["h"], b["y"] + b["h"]) > max(a["y"], b["y"]))


def _time_overlap(a, b):
    return min(float(a.get("end") or 0), float(b.get("end") or 0)) > max(
        float(a.get("start") or 0), float(b.get("start") or 0))


def audit(hyperframes_spec, fusion, content_spec):
    """Fail-closed audit for a fully resolved pre-render layer contract."""
    scenes = [item for item in (hyperframes_spec or {}).get("scenes") or []
              if isinstance(item, dict)]
    width, height = (hyperframes_spec or {}).get("resolution") or [1920, 1080]
    shots = {
        str(item.get("id") or ""): item
        for item in ((fusion or {}).get("edl") or {}).get("shots") or []
        if isinstance(item, dict)
    }
    content_copy = content_copy_by_shot(content_spec)
    errors = []
    geometry = []
    for scene in scenes:
        shot_id = str(scene.get("source_shot_id") or "")
        slot = str(scene.get("slot") or "")
        rect = _pixel_rect(scene, float(width), float(height))
        geometry.append({"source_shot_id": shot_id, "slot": slot, "rect": rect})
        if scene.get("source_kind") != "audience_copy" or scene.get("owner") != "hyperframes":
            errors.append({"code": "VISIBLE_LAYER_SOURCE_OR_OWNER_INVALID", "shot_id": shot_id, "slot": slot})
        if slot != "subtitle" and normalize_copy(scene.get("text")) in content_copy.get(shot_id, {}):
            errors.append({"code": "VISIBLE_COPY_DUPLICATED_ACROSS_RENDERERS", "shot_id": shot_id, "slot": slot})
        regions = _layout_regions(shots.get(shot_id) or {})
        if slot == "subtitle":
            center = {"x": rect["x"] + rect["w"] / 2, "y": rect["y"] + rect["h"] / 2}
            subtitle = regions["subtitle"]
            if not (subtitle["x"] <= center["x"] <= subtitle["x"] + subtitle["w"] and
                    subtitle["y"] <= center["y"] <= subtitle["y"] + subtitle["h"]):
                errors.append({"code": "SUBTITLE_OUTSIDE_RESERVED_REGION", "shot_id": shot_id, "slot": slot})
        elif any(_overlap(rect, regions[name]) for name in ("content", "presenter")):
            errors.append({"code": "OVERLAY_INTERSECTS_RESERVED_REGION", "shot_id": shot_id, "slot": slot})
    for index, left in enumerate(scenes):
        if left.get("slot") == "subtitle":
            continue
        for right in scenes[index + 1:]:
            if (right.get("slot") == "subtitle" or
                    left.get("source_shot_id") != right.get("source_shot_id")):
                continue
            if _time_overlap(left, right) and _overlap(
                    _pixel_rect(left, float(width), float(height)),
                    _pixel_rect(right, float(width), float(height))):
                errors.append({
                    "code": "HYPERFRAMES_VISIBLE_GEOMETRY_COLLISION",
                    "shot_id": left.get("source_shot_id"),
                    "slots": [left.get("slot"), right.get("slot")],
                })
    return {
        "schema_version": CONTRACT_VERSION,
        "ok": not errors,
        "policy": {
            "remotion": "content_copy_and_layout",
            "hyperframes": "spoken_subtitle_or_explicit_exclusive_overlay",
            "presenter": "confirmed_cast_or_formal_avatar_plate",
        },
        "errors": errors,
        "geometry": geometry,
    }
