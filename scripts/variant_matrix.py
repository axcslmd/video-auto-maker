#!/usr/bin/env python3
"""Variant matrix: expand a creative matrix into per-variant segment batches.

Takes a variant matrix JSON (hooks × actors × CTAs Cartesian product) and
produces a list of segment batches, each ready for `video_engine.py --batch`.

Each variant = [hook_segment] + base_segments + [cta_segment]
The hook/cta segments inherit the actor's cast_board_path as the reference image.

Cost discipline: before submission, the caller should call budget_check() to
show the client "N variants × $X/variant = $Y total" and enforce budget_limit_usd.

CLI:
  python3 variant_matrix.py expand matrix.json --out output/variants/
  python3 variant_matrix.py budget matrix.json
  python3 variant_matrix.py report variants/ results.json --ledger output/<run_id>/generation_runs.jsonl
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cost_ledger


def expand_variant_matrix(matrix_path):
    """Expand a variant matrix JSON into a list of per-variant segment batches.

    Returns: [
        {
            "variant_id": "v_pain_actor-01_soft",
            "hook_id": "pain", "actor_id": "actor-01", "cta_id": "soft",
            "segments": [hook_seg, *base_segs, cta_seg]
        }, ...
    ]
    """
    with open(matrix_path) as f:
        matrix = json.load(f)

    hooks = matrix["variant_axes"]["hooks"]
    actors = matrix["variant_axes"].get("actors", [{"id": "default"}])
    ctas = matrix["variant_axes"]["ctas"]
    base = matrix["base_segments"]

    variants = []
    for hook in hooks:
        for actor in actors:
            for cta in ctas:
                vid = "v_%s_%s_%s" % (hook["id"], actor["id"], cta["id"])
                cast_url = actor.get("cast_board_path")

                # Build hook segment
                hook_seg = dict(hook)
                hook_seg["id"] = "%s_hook" % vid
                if cast_url:
                    hook_seg["urls"] = [cast_url]

                # Build base segments (deep-copied with variant-prefixed IDs)
                base_segs = []
                for s in base:
                    seg = dict(s)
                    seg["id"] = "%s_%s" % (vid, s["id"])
                    if cast_url and "urls" not in seg:
                        seg["urls"] = [cast_url]
                    base_segs.append(seg)

                # Build CTA segment
                cta_seg = dict(cta)
                cta_seg["id"] = "%s_cta" % vid
                if cast_url:
                    cta_seg["urls"] = [cast_url]

                variants.append({
                    "variant_id": vid,
                    "hook_id": hook["id"],
                    "actor_id": actor["id"],
                    "cta_id": cta["id"],
                    "segments": [hook_seg] + base_segs + [cta_seg],
                })

    return variants


def budget_check(matrix_path):
    """Estimate total cost for a variant matrix and enforce budget_limit_usd.

    Returns: {"variant_count": N, "per_video_est": $X, "total_est": $Y,
              "budget_limit": $Z, "within_budget": bool}
    Raises ValueError if total exceeds budget_limit_usd.
    """
    with open(matrix_path) as f:
        matrix = json.load(f)

    variants = expand_variant_matrix(matrix_path)
    model = matrix.get("model", "seedance-2.0")

    # Estimate per-variant duration from segment durations
    total_duration = 0
    for v in variants:
        v_dur = sum(s.get("duration", 8) for s in v["segments"])
        total_duration += v_dur

    per_video_dur = total_duration / len(variants) if variants else 0
    per_video_est = cost_ledger.cost_estimate_for_task(
        model, per_video_dur, "video", 1).get("estimated_cost", 0)

    total_est = per_video_est * len(variants)
    budget_limit = matrix.get("budget_limit_usd")

    result = {
        "variant_count": len(variants),
        "per_video_duration_sec": round(per_video_dur, 1),
        "per_video_est": round(per_video_est, 4),
        "total_est": round(total_est, 2),
        "budget_limit": budget_limit,
        "within_budget": budget_limit is None or total_est <= budget_limit,
        "model": model,
    }

    if budget_limit is not None and total_est > budget_limit:
        raise ValueError(
            "BUDGET_EXCEEDED: %d 变体 × $%.2f = $%.2f > 上限 $%.2f"
            % (len(variants), per_video_est, total_est, budget_limit))

    return result


def generate_variant_report(variants, results, ledger_path):
    """Generate a variant comparison report as markdown.

    Args:
        variants: list from expand_variant_matrix()
        results: list of per-segment results from video_engine batch
        ledger_path: path to generation_runs.jsonl for cost data

    Returns: {"markdown": "...", "by_variant": {vid: {...}, ...}}
    """
    # Load cost data
    cost_summary = None
    try:
        cost_summary = cost_ledger.estimate_run_cost(ledger_path)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass

    by_segment = (cost_summary or {}).get("by_segment", {}) if cost_summary else {}

    # Map results to variants
    by_variant = {}
    for v in variants:
        vid = v["variant_id"]
        seg_ids = [s["id"] for s in v["segments"]]
        v_results = [r for r in (results or []) if r.get("segment_id") in seg_ids]
        v_cost = sum(by_segment.get(sid, 0) for sid in seg_ids)
        v_ok = all(r.get("ok") for r in v_results) if v_results else False

        by_variant[vid] = {
            "hook": v["hook_id"],
            "actor": v["actor_id"],
            "cta": v["cta_id"],
            "segment_count": len(seg_ids),
            "all_ok": v_ok,
            "cost": round(v_cost, 4),
            "results": v_results,
        }

    # Build markdown table
    lines = ["# 变体矩阵报告\n"]
    lines.append("| 变体 ID | 钩子 | 数字人 | CTA | 段数 | 状态 | 估算成本 |")
    lines.append("|---|---|---|---|---|---|---|")
    for vid, info in sorted(by_variant.items()):
        status = "✅ 通过" if info["all_ok"] else "❌ 失败"
        lines.append("| %s | %s | %s | %s | %d | %s | $%.2f |" % (
            vid, info["hook"], info["actor"], info["cta"],
            info["segment_count"], status, info["cost"]))

    if cost_summary:
        lines.append("\n## 成本汇总\n")
        lines.append("- 总估算成本: $%.2f" % cost_summary.get("total_est", 0))
        lines.append("- 重试浪费: $%.2f (%.1f%%)" % (
            cost_summary.get("retry_waste", 0),
            cost_summary.get("retry_waste_ratio", 0) * 100))
        lines.append("- 任务总数: %d (重试 %d)" % (
            cost_summary.get("task_count", 0),
            cost_summary.get("retry_count", 0)))

    lines.append("\n## 最佳变体（按 take 评分）\n")
    scored = []
    for vid, info in by_variant.items():
        for r in info.get("results", []):
            score = (r.get("take_score") or {}).get("overall_score", 0)
            if score:
                scored.append((vid, score, info))
    scored.sort(key=lambda x: -x[1])
    for vid, score, info in scored[:3]:
        lines.append("- **%s** — 评分 %.0f (钩子:%s 人物:%s CTA:%s)" % (
            vid, score, info["hook"], info["actor"], info["cta"]))

    return {"markdown": "\n".join(lines), "by_variant": by_variant}


def main():
    parser = argparse.ArgumentParser(description="Variant matrix expansion and reporting")
    sub = parser.add_subparsers(dest="cmd")

    exp = sub.add_parser("expand", help="Expand matrix to per-variant segment batches")
    exp.add_argument("matrix", help="Path to variant matrix JSON")
    exp.add_argument("--out-dir", default=".", help="Output directory for per-variant JSON files")

    bud = sub.add_parser("budget", help="Estimate cost and check budget")
    bud.add_argument("matrix", help="Path to variant matrix JSON")

    rep = sub.add_parser("report", help="Generate variant comparison report")
    rep.add_argument("variants_dir", help="Directory with per-variant results")
    rep.add_argument("results", help="Path to batch results JSON")
    rep.add_argument("--ledger", help="Path to generation_runs.jsonl")

    args = parser.parse_args()

    if args.cmd == "expand":
        variants = expand_variant_matrix(args.matrix)
        os.makedirs(args.out_dir, exist_ok=True)
        for v in variants:
            path = os.path.join(args.out_dir, "%s.json" % v["variant_id"])
            with open(path, "w") as f:
                json.dump({"variant_id": v["variant_id"],
                           "segments": v["segments"]}, f, indent=2, ensure_ascii=False)
        print("Expanded %d variants to %s/" % (len(variants), args.out_dir))

    elif args.cmd == "budget":
        result = budget_check(args.matrix)
        print("变体数: %d" % result["variant_count"])
        print("每条估算: $%.2f (%.1fs)" % (result["per_video_est"], result["per_video_duration_sec"]))
        print("总估算: $%.2f" % result["total_est"])
        if result["budget_limit"]:
            print("预算上限: $%.2f → %s" % (
                result["budget_limit"],
                "✅ 在预算内" if result["within_budget"] else "❌ 超预算"))

    elif args.cmd == "report":
        with open(args.results) as f:
            results = json.load(f)
        results_list = results.get("results", results) if isinstance(results, dict) else results

        # Try to find variants.json in the variants dir
        variants_path = os.path.join(args.variants_dir, "variants.json")
        if os.path.isfile(variants_path):
            with open(variants_path) as f:
                variants = json.load(f)
        else:
            # Reconstruct from individual files
            variants = []
            for fn in os.listdir(args.variants_dir):
                if fn.endswith(".json") and fn != "variants.json":
                    with open(os.path.join(args.variants_dir, fn)) as f:
                        data = json.load(f)
                        variants.append(data)

        report = generate_variant_report(variants, results_list, args.ledger or "")
        print(report["markdown"])

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
