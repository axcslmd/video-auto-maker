#!/usr/bin/env python3
"""Cost ledger: derive cost metrics from the append-only generation ledger.

This module is a *pure read-side projection* over generation_ledger events.
It never writes to the ledger, never mutates manifest, and is safe to call
at any point in the pipeline for an at-cost snapshot.

Integration points (three, all additive):
  1. video_engine._persist_task — append cost_estimate on succeeded events
  2. pipeline._delivery_snapshot — include cost_summary in delivery manifest
  3. pipeline status CLI — print "estimated $X.XX, retry waste $Y.YY (Z%)"

Usage:
    from cost_ledger import estimate_run_cost
    summary = estimate_run_cost("output/<run_id>/generation_runs.jsonl")
    print(summary["total_est"], summary["retry_waste_ratio"])
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import generation_ledger

# ── Price table ─────────────────────────────────────────────────────────
# Versioned pricing in USD. Video models are per-second; image models are
# per-call. These are *internal estimates* for cost awareness, not customer
# billing rates. Update when the gateway publishes new pricing.
PRICE_TABLE_VERSION = "2026-08"

# USD per second of generated video (estimated)
VIDEO_PRICE_PER_SEC = {
    "seedance-2.0": 0.05,
    "kling-v3-omni-video": 0.09,
    "wan2.7-i2v": 0.03,
    "kling-v3-omni-image": 0.07,  # also used as video fallback
    "veo-3": 0.12,
}

# USD per image generation (flat per-call)
IMAGE_PRICE = {
    "gpt-image-2": 0.04,
    "seedream-5.0": 0.03,
    "nano-banana-pro": 0.025,
    "imagen-4-ultra": 0.04,
    "kling-v3-omni-image": 0.03,
}

# Fallback if model is unknown (conservative estimate)
UNKNOWN_VIDEO_PRICE_PER_SEC = 0.08
UNKNOWN_IMAGE_PRICE = 0.04

# Minimum billable duration for a video task (seconds)
MIN_BILLABLE_SEC = 5


def _estimate_video_cost(model, duration_sec):
    """Estimate cost for a single video generation call."""
    if duration_sec is None:
        duration_sec = MIN_BILLABLE_SEC
    try:
        duration_sec = max(float(duration_sec), MIN_BILLABLE_SEC)
    except (TypeError, ValueError):
        duration_sec = MIN_BILLABLE_SEC
    rate = VIDEO_PRICE_PER_SEC.get(model, UNKNOWN_VIDEO_PRICE_PER_SEC)
    return round(duration_sec * rate, 4)


def _estimate_image_cost(model):
    """Estimate cost for a single image generation call."""
    return IMAGE_PRICE.get(model, UNKNOWN_IMAGE_PRICE)


def _classify_event(event):
    """Classify a ledger event into a cost record or None.

    Returns dict with keys: model, cost_estimate, is_retry, stage, unit_id.
    """
    name = event.get("event", "")
    model = event.get("model")
    stage = event.get("stage", "video")

    # Only succeeded events represent actual spend
    if name != "task_succeeded":
        return None

    # Detect retry: attempt > 1 means this is a retry of a failed/rejected take
    attempt = event.get("attempt") or 1
    is_retry = attempt > 1

    # Video tasks have duration; image tasks (storyboard/cast_board) are per-call
    duration = event.get("duration_sec") or event.get("actual_duration")
    if stage in ("video",):
        cost = _estimate_video_cost(model, duration)
    elif stage in ("storyboard", "cast_board", "product_board", "product_usage"):
        cost = _estimate_image_cost(model)
    else:
        # Unknown stage: use video pricing as conservative default
        cost = _estimate_video_cost(model, duration)

    return {
        "model": model,
        "stage": stage,
        "unit_id": event.get("unit_id"),
        "cost_estimate": cost,
        "is_retry": is_retry,
        "attempt": attempt,
        "duration_sec": duration,
    }


def estimate_run_cost(ledger_path):
    """Replay ledger events and produce a cost summary.

    Returns:
        {
            "price_table_version": str,
            "total_est": float,          # total estimated cost in USD
            "by_model": {model: float},  # cost per model
            "by_stage": {stage: float},  # cost per pipeline stage
            "by_segment": {unit_id: float}, # cost per segment
            "retry_waste": float,        # cost of retried attempts
            "retry_waste_ratio": float,  # retry_waste / total_est (0-1)
            "task_count": int,           # total succeeded tasks
            "retry_count": int,          # tasks with attempt > 1
        }
    """
    events = generation_ledger.read_events(ledger_path) if isinstance(
        ledger_path, (str, bytes, os.PathLike)) else list(ledger_path)

    by_model = defaultdict(float)
    by_stage = defaultdict(float)
    by_segment = defaultdict(float)
    total_est = 0.0
    retry_waste = 0.0
    task_count = 0
    retry_count = 0

    for event in events:
        record = _classify_event(event)
        if record is None:
            continue

        cost = record["cost_estimate"]
        model = record["model"] or "unknown"
        stage = record["stage"]
        unit_id = record["unit_id"] or "unattributed"

        total_est += cost
        by_model[model] += cost
        by_stage[stage] += cost
        by_segment[unit_id] += cost
        task_count += 1

        if record["is_retry"]:
            retry_waste += cost
            retry_count += 1

    ratio = (retry_waste / total_est) if total_est > 0 else 0.0

    return {
        "price_table_version": PRICE_TABLE_VERSION,
        "total_est": round(total_est, 4),
        "by_model": dict(by_model),
        "by_stage": dict(by_stage),
        "by_segment": dict(by_segment),
        "retry_waste": round(retry_waste, 4),
        "retry_waste_ratio": round(ratio, 4),
        "task_count": task_count,
        "retry_count": retry_count,
    }


def format_cost_line(summary):
    """One-line human-readable cost summary for CLI output."""
    total = summary["total_est"]
    waste = summary["retry_waste"]
    ratio_pct = summary["retry_waste_ratio"] * 100
    return ("本 run 估算成本 $%.2f，其中重试浪费 $%.2f（%.1f%%），"
            "共 %d 段成功（%d 段重试）" % (
                total, waste, ratio_pct,
                summary["task_count"], summary["retry_count"]))


def cost_estimate_for_task(model, duration_sec, stage="video", attempt=1):
    """Convenience function for video_engine._persist_task to compute
    per-task cost estimate at write time.

    Call site (video_engine.py L1288 area):
        _persist_task(manifest, manifest_path, ledger_path, task, "succeeded",
                      video_url=url,
                      cost_estimate=cost_ledger.cost_estimate_for_task(
                          seg_model, actual_duration, "video", attempt))
    """
    cost = _estimate_video_cost(model, duration_sec) if stage == "video" \
        else _estimate_image_cost(model)
    return {
        "cost_estimate": cost,
        "is_retry": attempt > 1,
        "price_table_version": PRICE_TABLE_VERSION,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="成本账本：从 generation ledger 估算成本")
    parser.add_argument("ledger", help="generation_runs.jsonl 路径")
    args = parser.parse_args()
    if not os.path.isfile(args.ledger):
        print("ERROR: ledger file not found: %s" % args.ledger, file=sys.stderr)
        sys.exit(1)
    summary = estimate_run_cost(args.ledger)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n" + format_cost_line(summary), file=sys.stderr)
