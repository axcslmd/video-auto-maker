#!/usr/bin/env python3
"""Reconcile storyboard role with the upstream director contract.

The director plan is the semantic owner of whether an image is a customer-facing
layout/design board or a video-model storyboard.  A later plan must not silently
promote a design board into a video storyboard merely because it was copied
through another JSON producer.
"""
import json
import os


DESIGN_BOARD_ROLE = "design_board"
VIDEO_STORYBOARD_ROLE = "video_storyboard"


def _normalized_role(value):
    value = str(value or "").strip().lower().replace("-", "_")
    if value in {"design_board", "motion_design_board", "layout_board",
                 "customer_preview"}:
        return DESIGN_BOARD_ROLE
    if value in {"video_storyboard", "storyboard_ref", "narrative_storyboard",
                 "剧情分镜"}:
        return VIDEO_STORYBOARD_ROLE
    return None


def _role_from_plan(plan):
    contract = (plan or {}).get("storyboard_contract") or {}
    if not isinstance(contract, dict):
        contract = {}
    return _normalized_role(
        (plan or {}).get("storyboard_role") or contract.get("kind") or
        contract.get("role") or contract.get("usage"))


def _has_physical_product(plan):
    """Use concrete media/identity evidence, never generic marketing words."""
    if not isinstance(plan, dict):
        return False
    asset_keys = {
        "product_images", "product_boards", "product_usage_images",
        "usage_reference_images", "product_reference_images",
    }
    asset_refs = plan.get("asset_refs") or {}
    if any(asset_refs.get(key) for key in asset_keys):
        return True
    if any(plan.get(key) for key in (
            "product_sku", "product_refs", "product_assets", "physical_product",
            "product_image", "product_board", "usage_geometry_review")):
        return True
    if str(plan.get("product_kind") or "").strip().lower() in {
            "physical", "physical_product", "实体产品", "实物产品"}:
        return True
    reference_types = {"product_identity", "product_board", "product_usage_identity"}
    for ref in plan.get("references") or []:
        if isinstance(ref, dict) and ref.get("type") in reference_types:
            return True
    for shot in plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        if any(shot.get(key) for key in (
                "product_sku", "product_refs", "physical_product")):
            return True
        shot_refs = shot.get("asset_refs") or {}
        if any(shot_refs.get(key) for key in asset_keys):
            return True
        for ref in shot.get("references") or []:
            if isinstance(ref, dict) and ref.get("type") in reference_types:
                return True
    return False


def _candidate_director_paths(plan, *, root=None, plan_path=None):
    paths = []
    for key in ("director_plan_path", "source_director_plan", "director_plan"):
        value = (plan or {}).get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    if plan_path:
        paths.append(os.path.join(os.path.dirname(os.path.abspath(plan_path)),
                                  "director_plan.json"))
    client = str((plan or {}).get("client") or "").strip()
    run_id = str((plan or {}).get("run_id") or "").strip()
    if root and client and run_id:
        paths.append(os.path.join(root, "output", client, run_id, "director_plan.json"))
    resolved = []
    for value in paths:
        path = value if os.path.isabs(value) else os.path.join(root or os.getcwd(), value)
        path = os.path.abspath(path)
        if path not in resolved:
            resolved.append(path)
    return resolved


def _matching_director_plan(plan, *, root=None, plan_path=None):
    client = str((plan or {}).get("client") or "").strip()
    run_id = str((plan or {}).get("run_id") or "").strip()
    for path in _candidate_director_paths(plan, root=root, plan_path=plan_path):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                director = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(director, dict):
            continue
        if client and director.get("client") and str(director["client"]) != client:
            continue
        if run_id and director.get("run_id") and str(director["run_id"]) != run_id:
            continue
        return director, path
    return None, None


def _merge_director_content_contracts(plan, director):
    by_id = {
        str(item.get("id")): item for item in (director or {}).get("segments") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    for shot in plan.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        source = by_id.get(str(shot.get("id")))
        if not source:
            continue
        content_page = source.get("content_page")
        if isinstance(content_page, dict):
            shot.setdefault("content_page_contract", json.loads(json.dumps(
                content_page, ensure_ascii=False)))
            if content_page.get("visual_mode"):
                shot.setdefault("visual_mode", content_page["visual_mode"])


def reconcile(plan, *, root=None, plan_path=None):
    """Mutate and return ``plan`` with one authoritative storyboard role.

    Physical-product evidence always selects ``video_storyboard``.  Otherwise a
    matching director plan is authoritative.  This makes role drift observable
    before prompt review, image generation, split, or video submission.
    """
    if not isinstance(plan, dict):
        return {"role": None, "changed": False, "source": None}
    original = _role_from_plan(plan)
    director, director_path = _matching_director_plan(
        plan, root=root, plan_path=plan_path)
    director_role = _role_from_plan(director)
    physical = _has_physical_product(plan) or _has_physical_product(director)
    # Legacy plans predate the explicit semantic fork. Their safest compatible
    # default is the historical video storyboard; document/design workflows
    # become design boards only through an explicit plan or director contract.
    resolved = (VIDEO_STORYBOARD_ROLE if physical else
                director_role or original or VIDEO_STORYBOARD_ROLE)

    if resolved == DESIGN_BOARD_ROLE:
        plan["storyboard_role"] = DESIGN_BOARD_ROLE
        plan["storyboard_usage"] = DESIGN_BOARD_ROLE
        plan["video_model_usage"] = "do_not_submit_as_storyboard_ref"
        contract = dict(plan.get("storyboard_contract") or {})
        contract.update({
            "kind": DESIGN_BOARD_ROLE,
            "purpose": "customer_layout_and_motion_confirmation",
            "composition_owner": "remotion",
            "composition_components": ["remotion", "hyperframes", "ffmpeg"],
            "video_model_usage": "do_not_submit_as_storyboard_ref",
        })
        plan["storyboard_contract"] = contract
        _merge_director_content_contracts(plan, director)
    else:
        plan["storyboard_role"] = VIDEO_STORYBOARD_ROLE
        plan["storyboard_usage"] = VIDEO_STORYBOARD_ROLE
        plan["video_model_usage"] = "submit_per_shot"
        contract = dict(plan.get("storyboard_contract") or {})
        contract.update({
            "kind": VIDEO_STORYBOARD_ROLE,
            "purpose": "video_generation_story_continuity",
            "composition_owner": "storyboard",
            "video_model_usage": "submit_per_shot",
        })
        plan["storyboard_contract"] = contract
    return {
        "role": resolved,
        "previous_role": original,
        "changed": bool(original and original != resolved),
        "source": director_path,
        "physical_product": physical,
    }
