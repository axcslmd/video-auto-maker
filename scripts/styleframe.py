#!/usr/bin/env python3
"""Compile approval-bound Design Token v3 and a same-renderer look frame.

The previous styleframe contract mostly changed a brand color while claiming to
approve typography hierarchy and surface language.  This compiler freezes the
actual values consumed by Remotion and HyperFrames: type scale, spacing, layout,
surface, media treatment, motion identity and mastering policy.
"""
import argparse
import copy
import json
import os
import re
import tempfile

import artifact_contract
from project_utils import atomic_write_json
import schema_validate


TOKEN_VERSION = 3
SUPPORTED_DIRECTIONS = (
    "premium_editorial", "clean_corporate", "technical_system",
    "energetic_social", "custom",
)


_DIRECTION_PRESETS = {
    "premium_editorial": {
        "palette": {
            "background": "#090D16", "surface": "#151B28",
            "text": "#F7F9FC", "text_secondary": "#AFB8C9",
        },
        "typography": {
            "display_weight": 780, "body_weight": 480,
            "letter_spacing": -0.02, "line_height": 1.42,
        },
        "surface": {
            "material": "editorial_solid", "radius": 18,
            "border_width": 1, "shadow": "soft", "blur": 8,
        },
        "motion": {
            "personality": "premium", "signature_easing": "cubic-bezier(0.2,0,0,1)",
            "active_motion_limit": 0.28,
        },
    },
    "clean_corporate": {
        "palette": {
            "background": "#F4F7FB", "surface": "#FFFFFF",
            "text": "#152033", "text_secondary": "#5E6B80",
        },
        "typography": {
            "display_weight": 740, "body_weight": 480,
            "letter_spacing": -0.012, "line_height": 1.46,
        },
        "surface": {
            "material": "clean_solid", "radius": 14,
            "border_width": 1, "shadow": "subtle", "blur": 0,
        },
        "motion": {
            "personality": "balanced", "signature_easing": "cubic-bezier(0.4,0,0.2,1)",
            "active_motion_limit": 0.25,
        },
    },
    "technical_system": {
        "palette": {
            "background": "#07121C", "surface": "#102433",
            "text": "#EFFBFF", "text_secondary": "#9BB6C5",
        },
        "typography": {
            "display_weight": 720, "body_weight": 500,
            "letter_spacing": -0.006, "line_height": 1.44,
        },
        "surface": {
            "material": "technical_grid", "radius": 10,
            "border_width": 1, "shadow": "precise", "blur": 0,
        },
        "motion": {
            "personality": "precise", "signature_easing": "cubic-bezier(0.4,0,0.2,1)",
            "active_motion_limit": 0.30,
        },
    },
    "energetic_social": {
        "palette": {
            "background": "#100B22", "surface": "#24173F",
            "text": "#FFFFFF", "text_secondary": "#D1C8E8",
        },
        "typography": {
            "display_weight": 900, "body_weight": 560,
            "letter_spacing": -0.025, "line_height": 1.34,
        },
        "surface": {
            "material": "bold_blocks", "radius": 24,
            "border_width": 2, "shadow": "punchy", "blur": 4,
        },
        "motion": {
            "personality": "energetic", "signature_easing": "cubic-bezier(0.16,1,0.3,1)",
            "active_motion_limit": 0.36,
        },
    },
}


def _normalize_hex(value, fallback):
    raw = str(value or fallback).strip()
    hex_value = raw[1:] if raw.startswith("#") else raw
    if re.fullmatch(r"[0-9a-fA-F]{3}", hex_value):
        return "#" + "".join(char * 2 for char in hex_value).upper()
    if re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
        return "#" + hex_value.upper()
    raise ValueError("DESIGN_TOKEN_COLOR_INVALID: %s" % raw)


def _brand_palette(brand):
    palette = brand.get("colors") or brand.get("palette") or {}
    return {
        "primary": _normalize_hex(palette.get("primary"), "#0C66E4"),
        "accent": _normalize_hex(palette.get("accent"), "#36B37E"),
        "background": palette.get("background"),
        "surface": palette.get("surface"),
        "text": palette.get("text"),
        "text_secondary": palette.get("text_secondary"),
    }


def _direction_preset(direction, brand=None):
    if direction == "custom" and (brand or {}).get("surface_language") == "dark_glass":
        return {
            "palette": {
                "background": "#091627", "surface": "#10243A",
                "text": "#F7F9FC", "text_secondary": "#AFC2D4",
            },
            "typography": {
                "display_weight": 740, "body_weight": 500,
                "letter_spacing": -0.012, "line_height": 1.44,
            },
            "surface": {
                "material": "dark_glass", "radius": 20,
                "border_width": 1, "shadow": "luminous", "blur": 18,
            },
            "motion": {
                "personality": "balanced",
                "signature_easing": "cubic-bezier(0.2,0,0,1)",
                "active_motion_limit": 0.30,
            },
        }
    selected = "premium_editorial" if direction == "custom" else direction
    if selected not in _DIRECTION_PRESETS:
        raise ValueError("DESIGN_DIRECTION_UNSUPPORTED: %s" % direction)
    return copy.deepcopy(_DIRECTION_PRESETS[selected])


def _type_scale(energy):
    multiplier = {"low": 0.96, "medium": 1.0, "high": 1.06}[energy]
    landscape = {
        "hero": 88, "h1": 68, "h2": 52, "h3": 38,
        "body": 28, "caption": 20, "metric": 112,
    }
    portrait = {
        "hero": 80, "h1": 60, "h2": 44, "h3": 32,
        "body": 26, "caption": 19, "metric": 104,
    }
    return {
        "landscape": {key: int(round(value * multiplier)) for key, value in landscape.items()},
        "portrait": {key: int(round(value * multiplier)) for key, value in portrait.items()},
    }


def _font_contract(brand):
    font = str(brand.get("font") or brand.get("font_family") or "sans-serif").strip()
    font_file = brand.get("font_file")
    font_sha = None
    if font_file:
        if not os.path.isfile(font_file):
            raise ValueError("DESIGN_TOKEN_FONT_FILE_MISSING: %s" % font_file)
        font_file = os.path.abspath(font_file)
        font_sha = artifact_contract.file_sha256(font_file)
    return font, font_file, font_sha


def build_tokens(brand=None, *, direction="custom", energy="medium"):
    """Return a validated, backwards-compatible Design Token v3 contract."""
    if energy not in {"low", "medium", "high"}:
        raise ValueError("DESIGN_ENERGY_UNSUPPORTED: %s" % energy)
    brand = brand or {}
    preset = _direction_preset(direction, brand=brand)
    palette = _brand_palette(brand)
    font, font_file, font_sha = _font_contract(brand)
    color = dict(preset["palette"])
    color.update({key: value for key, value in palette.items() if value})
    color = {key: _normalize_hex(value, value) for key, value in color.items()}
    typography = dict(preset["typography"])
    typography.update({
        "display_family": font,
        "body_family": font,
        "mono_family": str(brand.get("mono_font") or "monospace"),
        "font_file": font_file,
        "font_file_sha256": font_sha,
        "scale": _type_scale(energy),
    })
    motion = dict(preset["motion"])
    motion.update({
        "energy": energy,
        "entry_duration_frames": {"low": 18, "medium": 16, "high": 12}[energy],
        "settle_duration_frames": 12,
        "stagger_frames": {"low": 6, "medium": 4, "high": 3}[energy],
        "hold_min_frames": 18,
        "transition_style": "shared_element",
        "transition_frames": {"low": 16, "medium": 12, "high": 9}[energy],
    })
    tokens = {
        "schema_version": TOKEN_VERSION,
        "approved_direction": direction,
        "brand": {
            "primary": color["primary"], "accent": color["accent"],
            "background": color["background"], "text": color["text"],
        },
        "color": color,
        "typography": {
            **typography,
            # Legacy consumers still read these names.
            "title_font": font, "body_font": font,
        },
        "spacing": {
            "base": 8,
            "landscape": {"page_padding": 72, "section_gap": 48, "component_gap": 24},
            "portrait": {"page_padding": 60, "section_gap": 40, "component_gap": 20},
        },
        "layout": {
            "grid_columns": {"landscape": 12, "portrait": 6},
            "safe_margin_ratio": 0.06,
            "subtitle_reserved_ratio": 0.20,
            "max_primary_subjects": 1,
            "max_secondary_subjects": 2,
        },
        "surface": preset["surface"],
        "media": {
            "image_radius": preset["surface"]["radius"],
            "image_treatment": (
                "high_contrast_clean" if direction != "clean_corporate" else "neutral_clean"),
            "overlay_strength": 0.18,
            "upscale_limit": 1.25,
            "protected_subject_crop": "forbidden",
        },
        "motion": motion,
        "mastering": {
            "working_profile": "commercial_master",
            "delivery_profile": "direct_delivery",
            "frame_format": "png",
            "color_space": "bt709",
            "audio_sample_rate": 48000,
            "single_lossy_encode": True,
        },
        "artifact_contract": {
            "role": "look_frame",
            "approves": [
                "palette", "typography_hierarchy", "spacing_system",
                "surface_language", "media_treatment", "motion_identity",
            ],
            "does_not_approve": [
                "shot_layout", "avatar_identity", "generated_video_fidelity"],
            "renderer_source_of_truth": "remotion.Styleframe",
        },
    }
    tokens["contract_sha256"] = artifact_contract.sha256_json(tokens)
    schema_validate.enforce(tokens, "design-tokens", context="styleframe.build_tokens")
    return tokens


def render_preview(tokens, out_path, *, title=None, subtitle=None,
                   width=1920, height=1080):
    """Render the look frame using the exact approved token values."""
    import remotion_engine

    props = {
        "width": int(width), "height": int(height),
        "brandPrimary": tokens["brand"]["primary"],
        "title": title or "视觉方向确认",
        "subtitle": subtitle or "同一设计系统将用于版式板、Animatic 与最终 Remotion 合成",
        "direction": tokens.get("approved_direction") or "custom",
        "energy": (tokens.get("motion") or {}).get("energy") or "medium",
        "designTokens": tokens,
    }
    fd, props_path = tempfile.mkstemp(prefix=".styleframe-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(props, handle, ensure_ascii=False)
        return remotion_engine.render_still(
            props_path, out_path,
            composition=remotion_engine.STYLEFRAME_COMPOSITION, frame=0)
    finally:
        try:
            os.unlink(props_path)
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build Design Token v3 and same-renderer look frame")
    parser.add_argument("--brand")
    parser.add_argument("--direction", choices=SUPPORTED_DIRECTIONS, default="custom")
    parser.add_argument("--energy", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--out", required=True)
    parser.add_argument("--preview-out", help="用正式 Remotion 设计系统渲染 look frame PNG")
    parser.add_argument("--title")
    parser.add_argument("--subtitle")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args(argv)
    brand = {}
    if args.brand:
        with open(args.brand, encoding="utf-8") as handle:
            brand = json.load(handle)
    tokens = build_tokens(brand, direction=args.direction, energy=args.energy)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    atomic_write_json(args.out, tokens)
    result = {"ok": True, "out": os.path.abspath(args.out),
              "contract_sha256": tokens["contract_sha256"]}
    if args.preview_out:
        preview = render_preview(
            tokens, args.preview_out, title=args.title, subtitle=args.subtitle,
            width=args.width, height=args.height)
        result["preview"] = os.path.abspath(preview)
        result["renderer"] = "remotion.Styleframe"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
