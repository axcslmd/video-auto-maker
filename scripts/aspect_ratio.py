#!/usr/bin/env python3
"""Single source of truth for video output and storyboard preview ratios."""

OUTPUT_RATIOS = {"9:16", "16:9", "1:1", "4:3", "3:4"}
STORYBOARD_RATIO = "16:9"


def output_ratio(plan, default="9:16"):
    """Read the canonical plan field, accepting legacy aliases on import."""
    value = ((plan or {}).get("output_ratio") or (plan or {}).get("ratio") or
             (plan or {}).get("video_aspect_ratio") or default)
    if value not in OUTPUT_RATIOS:
        raise ValueError("OUTPUT_RATIO_INVALID: %s" % value)
    return value


def normalize_plan(plan):
    """Normalize legacy plans without changing their storyboard preview policy."""
    plan = dict(plan or {})
    plan["output_ratio"] = output_ratio(plan)
    plan["storyboard_aspect_ratio"] = STORYBOARD_RATIO
    plan.pop("aspect_ratio", None)
    return plan
