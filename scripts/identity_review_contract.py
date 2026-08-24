#!/usr/bin/env python3
"""Deterministic media and visual-review contract for identity plates."""
import json


REQUIRED_VISUAL_CHECKS = (
    "single_person",
    "face_clear",
    "cast_identity_consistent",
    "hair_outfit_accessories_consistent",
    "background_clean",
    "no_ppt_text_logo_or_watermark",
    "avatar_plate_composition_usable",
)


def _ratio_value(value):
    text = str(value or "").strip()
    if ":" not in text:
        raise ValueError("IDENTITY_MEDIA_QC_RATIO_INVALID")
    left, right = text.split(":", 1)
    try:
        numerator, denominator = float(left), float(right)
    except (TypeError, ValueError) as exc:
        raise ValueError("IDENTITY_MEDIA_QC_RATIO_INVALID") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("IDENTITY_MEDIA_QC_RATIO_INVALID")
    return numerator / denominator


def inspect_image(path, *, expected_ratio="16:9", minimum_width=1280,
                  minimum_height=720, ratio_tolerance=0.02):
    """Return deterministic decode/dimension/aspect evidence for one image."""
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format, mode = image.format, image.mode
    except (OSError, ValueError, SyntaxError) as exc:
        return {
            "passed": False, "error": str(exc),
            "checks": {"decodable": False, "minimum_dimensions": False,
                       "expected_aspect_ratio": False},
        }
    expected = _ratio_value(expected_ratio)
    actual = (float(width) / float(height)) if height else 0.0
    relative_error = abs(actual - expected) / expected if actual else 1.0
    checks = {
        "decodable": True,
        "minimum_dimensions": (
            int(width) >= int(minimum_width) and
            int(height) >= int(minimum_height)),
        "expected_aspect_ratio": relative_error <= float(ratio_tolerance),
    }
    return {
        "passed": all(checks.values()), "checks": checks,
        "width": int(width), "height": int(height),
        "format": image_format, "mode": mode,
        "expected_ratio": expected_ratio, "actual_ratio": actual,
        "aspect_ratio_relative_error": relative_error,
        "minimum_dimensions": [int(minimum_width), int(minimum_height)],
    }


def validate_visual_review_evidence(review, *, candidate, generation_result,
                                    media_qc, identity_references):
    """Validate pass/reject inspection evidence bound to exact bytes."""
    if not isinstance(review, dict):
        raise ValueError("IDENTITY_VISUAL_REVIEW_OBJECT_REQUIRED")
    checks = review.get("checks") or {}
    observations = review.get("observations") or {}
    if (review.get("kind") != "identity_plate_visual_review" or
            review.get("reviewer_type") != "agent_visual_inspection" or
            review.get("artifact") != candidate or
            review.get("generation_result") != generation_result or
            review.get("identity_references") != identity_references or
            review.get("deterministic_media_qc") != media_qc or
            not media_qc.get("passed") or
            set(checks) != set(REQUIRED_VISUAL_CHECKS) or
            not all(isinstance(checks.get(name), bool)
                    for name in REQUIRED_VISUAL_CHECKS)):
        raise ValueError("IDENTITY_VISUAL_REVIEW_INVALID")
    allowed = set(REQUIRED_VISUAL_CHECKS)
    unknown = sorted(set(checks) - allowed)
    if unknown:
        raise ValueError(
            "IDENTITY_VISUAL_REVIEW_UNKNOWN_CHECKS: %s" % ",".join(unknown))
    if set(observations) != allowed or any(
            len(str(observations.get(name) or "").strip()) < 4
            for name in REQUIRED_VISUAL_CHECKS):
        raise ValueError("IDENTITY_VISUAL_REVIEW_OBSERVATIONS_REQUIRED")
    passed = all(checks.get(name) is True for name in REQUIRED_VISUAL_CHECKS)
    if (review.get("passed") is not passed or
            review.get("status") != ("passed" if passed else "rejected")):
        raise ValueError("IDENTITY_VISUAL_REVIEW_STATUS_MISMATCH")
    # Round-trip canonical JSON now so non-serializable/NaN-like review data
    # cannot enter an approval marker later.
    json.dumps(review, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return True


def validate_visual_review(review, *, candidate, generation_result, media_qc,
                           identity_references):
    """Validate a passing inspection before customer approval."""
    validate_visual_review_evidence(
        review, candidate=candidate, generation_result=generation_result,
        media_qc=media_qc, identity_references=identity_references)
    if review.get("passed") is not True:
        raise ValueError("IDENTITY_VISUAL_REVIEW_NOT_PASSED")
    return True
