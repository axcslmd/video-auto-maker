#!/usr/bin/env python3
"""Derive auditable single-view identity anchors from a confirmed cast board.

The 3x2 contact sheet remains the customer approval artifact.  Renderers and
single-frame image requests consume the deterministic crops in this pack so a
grid can never be mistaken for a six-person scene.
"""
import hashlib
import json
import os
import tempfile

from project_utils import atomic_write_json
import schema_validate


PACK_VERSION = 1
VIEW_CELLS = {
    "full_body_front": (0, 0),
    "full_body_back": (1, 0),
    "full_body_profile": (2, 0),
    "face_front": (0, 1),
    "face_back": (1, 1),
    "face_profile": (2, 1),
}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(path):
    return {
        "path": os.path.abspath(path),
        "sha256": _sha256(path),
        "size": os.path.getsize(path),
    }


def _pack_is_current(pack, board_path, board_sha):
    if not isinstance(pack, dict) or pack.get("schema_version") != PACK_VERSION:
        return False
    source = pack.get("source_board") or {}
    if source.get("path") != board_path or source.get("sha256") != board_sha:
        return False
    views = pack.get("views") or {}
    for role in VIEW_CELLS:
        item = views.get(role) or {}
        path = item.get("path")
        if not path or not os.path.isfile(path) or item.get("sha256") != _sha256(path):
            return False
    return True


def _save_png_atomic(image, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(path), suffix=".png",
        dir=os.path.dirname(path))
    os.close(fd)
    try:
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def derive(board_path, out_dir):
    """Create/reuse a 3x2 single-view pack bound to the board's exact bytes."""
    board_path = os.path.abspath(board_path)
    out_dir = os.path.abspath(out_dir)
    if not os.path.isfile(board_path):
        raise ValueError("CAST_REFERENCE_BOARD_MISSING: %s" % board_path)
    board_sha = _sha256(board_path)
    manifest_path = os.path.join(out_dir, "cast_reference_pack.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                existing = json.load(handle)
            if _pack_is_current(existing, board_path, board_sha):
                schema_validate.enforce(
                    existing, "cast-reference-pack",
                    context="cast_reference_pack.derive:cached")
                return existing
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("CAST_REFERENCE_PACK_PILLOW_REQUIRED") from exc
    os.makedirs(out_dir, exist_ok=True)
    try:
        image_context = Image.open(board_path)
    except (OSError, ValueError) as exc:
        raise ValueError("CAST_REFERENCE_BOARD_INVALID: %s" % board_path) from exc
    views = {}
    with image_context as image:
        width, height = image.size
        if width < 600 or height < 400:
            raise ValueError("CAST_REFERENCE_BOARD_TOO_SMALL")
        if abs((width / float(height)) - 1.5) > 0.18:
            raise ValueError("CAST_REFERENCE_BOARD_GRID_RATIO_INVALID")
        cell_width, cell_height = width / 3.0, height / 2.0
        inset_x = max(2, int(round(cell_width * .018)))
        inset_y = max(2, int(round(cell_height * .018)))
        for role, (column, row) in VIEW_CELLS.items():
            x0 = int(round(column * cell_width)) + inset_x
            y0 = int(round(row * cell_height)) + inset_y
            x1 = int(round((column + 1) * cell_width)) - inset_x
            y1 = int(round((row + 1) * cell_height)) - inset_y
            crop = image.crop((x0, y0, x1, y1)).convert("RGB")
            path = os.path.join(out_dir, "%s.png" % role)
            _save_png_atomic(crop, path)
            views[role] = dict(
                _record(path), cell={"column": column, "row": row},
                crop_box=[x0, y0, x1, y1],
                remote_video_input_allowed=False)
    pack = {
        "schema_version": PACK_VERSION,
        "kind": "cast_single_view_reference_pack",
        "source_board": dict(
            _record(board_path), board_type="digital_human_6_view",
            review_artifact_only=True, direct_presenter_input_allowed=False,
            direct_video_model_input_allowed=False),
        "grid": {"columns": 3, "rows": 2},
        "primary_identity_anchor": "face_front",
        "full_body_identity_anchor": "full_body_front",
        "views": views,
        "usage_contract": {
            "customer_review": "source_board",
            "local_previs": "full_body_front",
            "image_generation_identity": ["face_front", "full_body_front"],
            "video_generation": "separately_confirmed_single_person_remote_identity_plate",
        },
    }
    schema_validate.enforce(
        pack, "cast-reference-pack", context="cast_reference_pack.derive")
    atomic_write_json(manifest_path, pack)
    return pack
