#!/usr/bin/env python3
"""Host-neutral, pure workflow requirements and profile contract.

This module is intentionally free of manifest, router and renderer imports.  It
is the single place where an authored storyboard plan is classified into
business facts, a chain profile and optional stage requirements.  Orchestration
modules consume this result; they must not re-infer the same facts independently.
"""
from __future__ import annotations

from copy import deepcopy


CONTRACT_VERSION = "2026-08-20.8"

ALL_STAGES = (
    "idea", "brief", "script", "content_scaffold", "motion_design",
    "styleframe", "audio", "product_board", "cast_board",
    "product_usage", "storyboard", "render_plan", "test_segment", "video",
    "shotcraft_packaging", "captions", "final", "derive", "delivery",
)

OPTIONAL_STAGE_FLAGS = {
    "content_scaffold": "requires_content_scaffold",
    "motion_design": "requires_motion_design",
    "styleframe": "requires_styleframe",
    "audio": "requires_audio",
    "product_board": "requires_product_board",
    "cast_board": "requires_cast_board",
    "product_usage": "requires_product_usage",
    "test_segment": "requires_test_segment",
    "shotcraft_packaging": "requires_shotcraft_packaging",
}

PROFILE_STAGE_ORDERS = {
    "oral_broadcast": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "cast_board", "storyboard", "render_plan",
        "test_segment", "video", "shotcraft_packaging", "captions", "final",
        "derive", "delivery",
    ),
    "document_explainer": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "cast_board", "storyboard", "render_plan",
        "test_segment", "video", "shotcraft_packaging", "captions", "final",
        "derive", "delivery",
    ),
    "product_showcase": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "product_board", "storyboard", "render_plan",
        "test_segment", "video", "shotcraft_packaging", "captions", "final",
        "derive", "delivery",
    ),
    "product_with_human": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "product_board", "cast_board", "product_usage",
        "storyboard", "render_plan", "test_segment", "video",
        "shotcraft_packaging", "captions", "final", "derive", "delivery",
    ),
    "venue_service": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "cast_board", "storyboard", "render_plan",
        "test_segment", "video", "shotcraft_packaging", "captions", "final",
        "derive", "delivery",
    ),
    "brand_tvc": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "product_board", "cast_board", "product_usage",
        "storyboard", "render_plan", "test_segment", "video",
        "shotcraft_packaging", "captions", "final", "derive", "delivery",
    ),
    "generic_video": (
        "idea", "brief", "script", "content_scaffold", "motion_design",
        "styleframe", "audio", "product_board", "cast_board", "product_usage",
        "storyboard", "render_plan", "test_segment", "video",
        "shotcraft_packaging", "captions", "final", "derive", "delivery",
    ),
}

PROFILE_PACKAGE_IDS = {
    "oral_broadcast": "oral-broadcast",
    "document_explainer": "explainer",
    "product_showcase": "brand-tvc",
    "product_with_human": "oral-broadcast",
    "venue_service": "explainer",
    "brand_tvc": "brand-tvc",
    "generic_video": None,
}

# ``PROFILE_PACKAGE_IDS`` is the default authoring choice retained for backward
# compatibility.  Authorization is a capability set: the same product+human
# production chain can legitimately serve an oral product explanation or a TVC.
# Keeping those concepts separate prevents a chain inference from silently
# changing what the customer purchased.
KNOWN_PACKAGE_IDS = frozenset(("oral-broadcast", "explainer", "brand-tvc"))
PROFILE_ALLOWED_PACKAGE_IDS = {
    "oral_broadcast": ("oral-broadcast",),
    "document_explainer": ("explainer",),
    "product_showcase": ("brand-tvc",),
    "product_with_human": ("oral-broadcast", "brand-tvc"),
    "venue_service": ("oral-broadcast", "explainer"),
    "brand_tvc": ("brand-tvc",),
    "generic_video": (),
}


def _flatten(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _flatten(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _flatten(nested)
    else:
        yield value


def text_blob(*values):
    return " ".join(
        str(value).strip().lower()
        for source in values
        for value in _flatten(source)
        if isinstance(value, str) and value.strip()
    )


def normalized_storyboard_role(plan):
    plan = plan if isinstance(plan, dict) else {}
    contract = plan.get("storyboard_contract") or {}
    return str(plan.get("storyboard_role") or contract.get("kind") or "").strip().lower().replace("-", "_")


def _shot_has_human(shot):
    if not isinstance(shot, dict):
        return False
    refs = shot.get("asset_refs") or {}
    if (shot.get("characters") or shot.get("human") or shot.get("presenter") or
            shot.get("digital_human") or shot.get("actor") or
            shot.get("character_prompt") or shot.get("character_action") or
            refs.get("digital_human_portraits")):
        return True
    return any(token in text_blob(shot.get("ref_tags"), shot.get("visual")) for token in (
        "@host", "@presenter", "@speaker", "@human", "talking head",
        "presenter", "host", "speaker", "digital human", "数字人", "主持人",
    ))


def _shot_has_product(shot):
    if not isinstance(shot, dict):
        return False
    refs = shot.get("asset_refs") or {}
    if (refs.get("product_images") or refs.get("product_sku") or
            shot.get("product_sku") or shot.get("product_refs") or
            shot.get("product_type") or shot.get("product")):
        return True
    return any(token in text_blob(shot.get("ref_tags")) for token in (
        "@product", "@usage",
    ))


def plan_facts(plan):
    """Return normalized business facts without mutating the authored plan."""
    plan = plan if isinstance(plan, dict) else {}
    refs = plan.get("asset_refs") or {}
    shots = [shot for shot in (plan.get("shots") or []) if isinstance(shot, dict)]
    role = normalized_storyboard_role(plan)
    # Profile routing must use authored metadata, not dialogue/visual prose.  A
    # product shot saying "open the PDF" or "explain the solution" is content,
    # not authority to switch the whole run to the document-explainer chain.
    routing_blob = text_blob(
        plan.get("scene_type"), plan.get("workflow"), plan.get("workflow_kind"),
        plan.get("skill"), plan.get("command"), plan.get("guide_kind"),
        plan.get("chain_profile"), plan.get("kind"), plan.get("content_type"),
    )
    explicit_product = any(token in routing_blob for token in (
        "product-demo", "product_demo", "product showcase", "product-showcase",
        "product_showcase", "product_with_human", "产品演示", "产品展示", "带货",
    ))
    explicit_oral = any(token in routing_blob for token in (
        "oral-broadcast", "oral_broadcast", "oral broadcast", "oral-interview",
        "oral_interview", "口播", "访谈",
    ))
    explicit_venue = any(token in routing_blob for token in (
        "oral-scene-service", "oral_scene_service", "scene service",
        "venue_service", "实景服务",
    ))
    explicit_document = any(token in routing_blob for token in (
        "explainer-video", "explainer_video", "explainer video",
        "document_explainer",
    ))
    has_product = bool(
        plan.get("product_type") or plan.get("product_sku") or
        refs.get("product_images") or refs.get("product_sku") or
        refs.get("product_boards") or refs.get("product_usage_images") or
        explicit_product or any(_shot_has_product(shot) for shot in shots)
    )
    has_human = bool(
        plan.get("characters") or plan.get("has_digital_human") or
        refs.get("digital_human_portraits") or explicit_oral or explicit_venue or
        "product_with_human" in routing_blob or
        any(_shot_has_human(shot) for shot in shots)
    )
    has_document = bool(
        role in {"design_board", "motion_design_board", "layout_board"} or
        plan.get("source_ppt") or plan.get("source_pdf") or plan.get("source_doc") or
        plan.get("source_document") or plan.get("content_spec") or
        plan.get("content_shotlist") or explicit_document or
        any(token in routing_blob for token in (
            "explainer", "document", "ppt", "pdf", "word", "solution",
            "course", "方案", "课程", "讲解",
        ))
    )
    has_venue = bool(
        refs.get("scene_images") or refs.get("background_images") or
        refs.get("environment_images") or explicit_venue or
        any(token in routing_blob for token in (
            "venue", "factory", "store", "shop", "scene service", "场地", "门店", "工厂",
        ))
    )
    is_tvc = any(token in routing_blob for token in (
        "brand-tvc", "brand_tvc", "brand tvc", "tvc", "品牌广告片",
    ))
    has_dialogue = bool(
        plan.get("audio") or plan.get("audio_contract") or plan.get("voice") or
        plan.get("voice_type") or any(
            shot.get("dialogue") or shot.get("voiceover") or shot.get("audio") or
            shot.get("audio_contract") for shot in shots
        )
    )
    return {
        "storyboard_role": role,
        "has_product": has_product,
        "has_human": has_human,
        "has_document": has_document,
        "has_venue": has_venue,
        "is_tvc": is_tvc,
        "has_dialogue": has_dialogue,
        "shot_count": len(shots),
        "route_hint": (
            "product" if explicit_product else
            "venue" if explicit_venue else
            "oral" if explicit_oral else
            "document" if explicit_document else ""),
    }


def infer_profile(plan):
    facts = plan_facts(plan)
    if facts["is_tvc"]:
        return {"profile": "brand_tvc", "reason": "plan_mentions_tvc"}
    # design_board is the authored semantic fork for deterministic document
    # explainers. Product screenshots/logos inside a proposal do not turn that
    # work into a physical product-use workflow.
    if facts["has_document"] and facts["storyboard_role"] == "design_board":
        return {"profile": "document_explainer", "reason": "design_board_document_contract"}
    if facts["has_product"] and facts["has_human"]:
        return {"profile": "product_with_human", "reason": "plan_has_product_and_human"}
    if facts["has_document"]:
        return {"profile": "document_explainer", "reason": "plan_has_document_source"}
    if facts["has_product"]:
        return {"profile": "product_showcase", "reason": "plan_has_product_without_human"}
    if facts["has_venue"] and facts["has_human"]:
        return {"profile": "venue_service", "reason": "plan_has_venue_and_human"}
    if facts["has_human"]:
        return {"profile": "oral_broadcast", "reason": "plan_has_human_without_product"}
    return {"profile": "generic_video", "reason": "plan_generic_fallback"}


def plan_requirements(plan):
    """Return the only authoritative optional-stage decisions for a plan."""
    plan = plan if isinstance(plan, dict) else {}
    facts = plan_facts(plan)
    profile = infer_profile(plan)["profile"]
    authored_package = str(
        plan.get("package") or plan.get("package_id") or "").strip().lower()
    explicit_motion = bool(
        plan.get("motion_design") or plan.get("motion_design_path") or
        plan.get("motion_elements") or any(
            isinstance(shot, dict) and shot.get("motion_elements")
            for shot in (plan.get("shots") or [])
        )
    )
    explicit_style = bool(
        plan.get("styleframe") or plan.get("style_tokens") or plan.get("brand_primary")
    )
    explicit_test = bool(
        plan.get("requires_test_segment") is True or
        str(plan.get("test_segment_mode") or "").strip().lower() == "required"
    )
    explicit_packaging = bool(
        plan.get("requires_shotcraft_packaging") is True or
        str(plan.get("shotcraft_packaging_mode") or "").strip().lower() == "required"
    )
    requires_content = facts["has_document"]
    physical_product_workflow = bool(
        facts["has_product"] and not (
            profile == "document_explainer" and
            facts["storyboard_role"] == "design_board"))
    requires_audio = facts["has_dialogue"] or profile in {
        "oral_broadcast", "document_explainer", "product_with_human",
        "venue_service", "brand_tvc",
    }
    # Multi-segment audio work must prove the exact provider route with one
    # real take before parallel paid submission. The accepted test task uses
    # the same segment/handoff/model identity and is reusable by the full batch.
    requires_audio_canary = bool(requires_audio and facts["shot_count"] > 1)
    requirements = {
        "requires_product_board": physical_product_workflow,
        "requires_cast_board": facts["has_human"],
        # Product use is a human-product identity/geometry bridge. Product-only
        # showcase work must never be forced to invent a missing person.
        "requires_product_usage": physical_product_workflow and facts["has_human"],
        "requires_content_scaffold": requires_content,
        "requires_motion_design": requires_content or explicit_motion,
        "requires_styleframe": explicit_style or profile != "generic_video",
        "requires_audio": requires_audio,
        "requires_test_segment": explicit_test or requires_audio_canary,
        # Packaging remains opt-in. The audio canary above is a paid-batch
        # safety gate and reuses the same task rather than creating a throwaway.
        "requires_shotcraft_packaging": explicit_packaging,
    }
    allowed_packages = list(PROFILE_ALLOWED_PACKAGE_IDS.get(profile) or ())
    # An explicit package entry is customer intent, not just descriptive prose.
    # Narrow ambiguous execution chains to that entry's sellable boundary.
    route_hint = facts.get("route_hint")
    if route_hint == "oral" and "oral-broadcast" in allowed_packages:
        allowed_packages = ["oral-broadcast"]
    elif route_hint == "document" and "explainer" in allowed_packages:
        allowed_packages = ["explainer"]
    return {
        "contract_version": CONTRACT_VERSION,
        "profile": profile,
        # Keep authored selection separate from the inferred expectation. A
        # formal run may not treat inference as customer/package authorization.
        "authored_package": authored_package or None,
        "package": authored_package or PROFILE_PACKAGE_IDS.get(profile),
        "expected_package": PROFILE_PACKAGE_IDS.get(profile),
        "allowed_packages": allowed_packages,
        "facts": facts,
        "requirements": requirements,
    }


def validate_package_binding(plan, *, require_authored=False):
    """Validate the sellable-package boundary for one authored plan.

    ``expected_package`` is a backward-compatible default. ``allowed_packages``
    is the authorization contract and ``authored_package`` is the customer's
    explicit selection. Formal binding requires an allowed explicit selection.
    """
    contract = plan_requirements(plan)
    actual = contract.get("authored_package")
    expected = contract.get("expected_package")
    allowed = contract.get("allowed_packages") or []
    errors = []
    if require_authored and not allowed:
        errors.append(
            "PACKAGE_SELECTION_REQUIRED: generic_video 不属于可交付的三大售卖包，"
            "请先选择 oral-broadcast / explainer / brand-tvc")
    elif require_authored and not actual:
        errors.append(
            "PACKAGE_SELECTION_REQUIRED: 正式 storyboard_plan 必须显式写入 package，"
            "可选范围=%s" % ", ".join(allowed))
    elif actual and actual not in KNOWN_PACKAGE_IDS:
        errors.append("PACKAGE_UNKNOWN: 未知售卖包 %s" % actual)
    elif actual and actual not in allowed:
        errors.append(
            "PACKAGE_PROFILE_MISMATCH: profile %s 允许 package [%s]，实际为 %s" %
            (contract["profile"], ", ".join(allowed), actual))
    elif actual and not allowed:
        errors.append(
            "PACKAGE_PROFILE_MISMATCH: generic_video 不能绑定正式 package %s" % actual)
    return {
        "ok": not errors,
        "errors": errors,
        "profile": contract["profile"],
        "authored_package": actual,
        "expected_package": expected,
        "allowed_packages": allowed,
        "contract": contract,
    }


def stage_required(stage, requirements):
    flag = OPTIONAL_STAGE_FLAGS.get(stage)
    return True if flag is None else bool((requirements or {}).get(flag))


def required_stage_order(profile, requirements, *, include_boundaries=False):
    profile = profile if profile in PROFILE_STAGE_ORDERS else "generic_video"
    stages = [
        stage for stage in PROFILE_STAGE_ORDERS[profile]
        if stage_required(stage, requirements)
    ]
    if not include_boundaries:
        stages = [stage for stage in stages if stage not in {"idea", "delivery"}]
    return stages


def validate_contract():
    errors = []
    valid = set(ALL_STAGES)
    for profile, stages in PROFILE_STAGE_ORDERS.items():
        if profile not in PROFILE_PACKAGE_IDS or profile not in PROFILE_ALLOWED_PACKAGE_IDS:
            errors.append("%s missing package binding" % profile)
        default_package = PROFILE_PACKAGE_IDS.get(profile)
        allowed_packages = PROFILE_ALLOWED_PACKAGE_IDS.get(profile) or ()
        if default_package and default_package not in allowed_packages:
            errors.append("%s default package is not allowed" % profile)
        unknown_packages = set(allowed_packages) - KNOWN_PACKAGE_IDS
        if unknown_packages:
            errors.append("%s contains unknown packages: %s" % (
                profile, ",".join(sorted(unknown_packages))))
        if not stages or stages[0] != "idea" or stages[-1] != "delivery":
            errors.append("%s must start at idea and end at delivery" % profile)
        if len(stages) != len(set(stages)):
            errors.append("%s contains duplicate stages" % profile)
        unknown = [stage for stage in stages if stage not in valid]
        if unknown:
            errors.append("%s contains unknown stages: %s" % (profile, ",".join(unknown)))
        if "storyboard" not in stages or "video" not in stages or "final" not in stages:
            errors.append("%s omits a mandatory production stage" % profile)
    return errors


def contract_snapshot():
    return {
        "contract_version": CONTRACT_VERSION,
        "optional_stage_flags": deepcopy(OPTIONAL_STAGE_FLAGS),
        "profile_stage_orders": {
            key: list(value) for key, value in PROFILE_STAGE_ORDERS.items()
        },
        "profile_package_ids": deepcopy(PROFILE_PACKAGE_IDS),
        "profile_allowed_package_ids": {
            key: list(value) for key, value in PROFILE_ALLOWED_PACKAGE_IDS.items()
        },
    }
