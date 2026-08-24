#!/usr/bin/env python3
"""Performance feedback: load historical投放 data to inform creative decisions.

Reads assets/<client>/performance.json (validated against
schemas/performance.schema.json) and returns structured learnings that the
Agent can cite during the co-creation conversation.

This is the "data flywheel" — the second time a client creates a video,
the Agent can reference what worked last time (completion rate, CTR,
which hook style performed better, etc.).

Never calls any external API. The client fills in performance.json manually
after publishing, using the metrics from their distribution platform
(Douyin, YouTube, etc.).

CLI:
  python3 performance_feedback.py load --client acme
  python3 performance_feedback.py validate --client acme
  python3 performance_feedback.py reminder --client acme --run-id 20260803-120611
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _performance_path(client):
    """Return the canonical path for a client's performance.json."""
    return os.path.join(os.path.dirname(HERE), "assets", client or "default", "performance.json")


def validate_performance_json(path):
    """Validate a performance.json file against the schema.

    Returns (is_valid, errors_list).
    """
    if not os.path.isfile(path):
        return False, ["File not found: %s" % path]

    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, ["Invalid JSON: %s" % e]

    errors = []

    # Schema version check
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1, got %s" % data.get("schema_version"))

    # Client check
    if not data.get("client"):
        errors.append("'client' field is required")

    # Videos array
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        errors.append("'videos' must be an array")
        videos = []

    for i, v in enumerate(videos):
        if not v.get("run_id"):
            errors.append("videos[%d].run_id is required" % i)
        if not v.get("delivery_sha256"):
            errors.append("videos[%d].delivery_sha256 is required" % i)
        if not v.get("channel"):
            errors.append("videos[%d].channel is required" % i)
        metrics = v.get("metrics", {})
        if not isinstance(metrics, dict):
            errors.append("videos[%d].metrics must be an object" % i)
        elif "views" not in metrics:
            errors.append("videos[%d].metrics.views is required" % i)

    # Learnings array
    learnings = data.get("learnings", [])
    if not isinstance(learnings, list):
        errors.append("'learnings' must be an array")

    return len(errors) == 0, errors


def load_performance_context(client):
    """Load a client's performance history and extract actionable learnings.

    Returns a list of formatted strings for the Agent to cite in co-creation,
    or None if no performance data exists.

    The returned strings are designed to be directly inserted into the
    co-creation conversation:
        "📊 历史数据参考：15s 痛点钩子结构完播率比 30s 反差钩子高 40%（来源 run: X, Y）"
    """
    path = _performance_path(client)
    if not os.path.isfile(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    learnings = data.get("learnings", [])
    if not learnings:
        return None

    # Format learnings for Agent citation
    formatted = []
    for l in learnings:
        insight = l.get("insight", "")
        evidence = l.get("evidence_run_ids", [])
        stage = l.get("applicable_stage", "")
        if insight:
            ref = ""
            if evidence:
                ref = "（来源 run: %s）" % ", ".join(evidence[:3])
            formatted.append("📊 历史数据参考：%s%s" % (insight, ref))

    # Also extract top-performing variant patterns
    videos = data.get("videos", [])
    if videos:
        # Sort by completion_rate descending
        sorted_videos = sorted(
            videos,
            key=lambda v: (v.get("metrics", {}).get("completion_rate", 0)),
            reverse=True)

        top = sorted_videos[0]
        va = top.get("variant_axis", {})
        if va:
            formatted.append(
                "🏆 最佳变体：钩子=%s 人物=%s CTA=%s，完播率 %.0f%%，CTR %.1f%%" % (
                    va.get("hook_id", "?"), va.get("actor_id", "?"),
                    va.get("cta_id", "?"),
                    top.get("metrics", {}).get("completion_rate", 0) * 100,
                    top.get("metrics", {}).get("ctr", 0) * 100))

        # Worst performer for contrast
        if len(sorted_videos) > 1:
            worst = sorted_videos[-1]
            wa = worst.get("variant_axis", {})
            if wa:
                formatted.append(
                    "⚠️ 最低变体：钩子=%s 人物=%s CTA=%s，完播率仅 %.0f%%" % (
                        wa.get("hook_id", "?"), wa.get("actor_id", "?"),
                        wa.get("cta_id", "?"),
                        worst.get("metrics", {}).get("completion_rate", 0) * 100))

    return formatted if formatted else None


def generate_delivery_reminder(client, run_id):
    """Generate the post-delivery reminder text for the Agent to output.

    This is shown to the client after a successful delivery, prompting them
    to fill in performance data.
    """
    path = _performance_path(client)
    has_existing = os.path.isfile(path)

    if has_existing:
        return (
            "✅ 交付完成。发布后请回填效果数据到 assets/%s/performance.json\n"
            "   建议字段：channel, published_at, metrics.completion_rate, metrics.ctr\n"
            "   下次共创时我会自动引用这些数据来优化推荐。\n"
            "   当前已有 %d 条历史记录。" % (
                client, len(json.load(open(path)).get("videos", []))))
    else:
        return (
            "✅ 交付完成。发布后请创建 assets/%s/performance.json 并回填效果数据。\n"
            "   最小字段：{\"schema_version\":1,\"client\":\"%s\",\"videos\":[{"
            "\"run_id\":\"%s\",\"delivery_sha256\":\"...\",\"channel\":\"douyin\","
            "\"metrics\":{\"views\":0,\"completion_rate\":0,\"ctr\":0}}]}\n"
            "   下次共创时我会自动引用这些数据来优化推荐。" % (
                client, client, run_id))


def main():
    parser = argparse.ArgumentParser(description="Performance feedback data flywheel")
    sub = parser.add_subparsers(dest="cmd")

    ld = sub.add_parser("load", help="Load and display performance learnings")
    ld.add_argument("--client", required=True)

    val = sub.add_parser("validate", help="Validate performance.json")
    val.add_argument("--client", required=True)

    rem = sub.add_parser("reminder", help="Generate post-delivery reminder")
    rem.add_argument("--client", required=True)
    rem.add_argument("--run-id", required=True)

    args = parser.parse_args()

    if args.cmd == "load":
        learnings = load_performance_context(args.client)
        if learnings:
            for l in learnings:
                print(l)
        else:
            print("（无历史投放数据）")

    elif args.cmd == "validate":
        path = _performance_path(args.client)
        ok, errors = validate_performance_json(path)
        if ok:
            print("✅ %s 有效" % path)
        else:
            print("❌ %s 无效：" % path)
            for e in errors:
                print("  - %s" % e)

    elif args.cmd == "reminder":
        print(generate_delivery_reminder(args.client, args.run_id))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
