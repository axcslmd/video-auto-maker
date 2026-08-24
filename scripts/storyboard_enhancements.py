#!/usr/bin/env python3
"""Continuity graph, binding report, and stale-artifact propagation utilities."""
import html
import json
import os
from datetime import datetime

from artifact_contract import file_sha256, sha256_json


def build_continuity_graph(shots):
    nodes, edges, errors = [], [], []
    previous = None
    for index, shot in enumerate(shots or [], 1):
        node = {"id": str(shot.get("id") or index), "panel_index": shot.get("panel_index") or index,
                "scene_id": shot.get("scene_id") or "default",
                "entry": shot.get("planned_start_state") or (shot.get("sequence_state") or {}).get("entry") or {},
                "exit": shot.get("planned_end_state") or (shot.get("sequence_state") or {}).get("exit") or {},
                "screen_direction": shot.get("screen_direction"), "lighting": shot.get("lighting"),
                "product_state": shot.get("product_state"), "camera": shot.get("camera_movement") or shot.get("camera"),
                "shot_size": shot.get("shot_size")}
        nodes.append(node)
        if previous and previous["scene_id"] == node["scene_id"]:
            changes = [key for key in ("camera", "shot_size") if previous.get(key) != node.get(key)]
            if not changes:
                errors.append("CONTINUITY_COVERAGE_REQUIRED: %s -> %s 缺少机位或景别差异" %
                              (previous["id"], node["id"]))
            if (previous.get("screen_direction") and node.get("screen_direction") and
                    previous["screen_direction"] != node["screen_direction"] and
                    not shot.get("axis_crossing_reason")):
                errors.append("CONTINUITY_AXIS_CROSSING: %s -> %s 屏幕方向反转" %
                              (previous["id"], node["id"]))
            edges.append({"from": previous["id"], "to": node["id"], "changes": changes,
                          "entry_matches_previous_exit": previous.get("exit") == node.get("entry")
                          if previous.get("exit") and node.get("entry") else None})
        previous = node
    return {"version": 1, "nodes": nodes, "edges": edges, "errors": errors,
            "ok": not errors, "fingerprint": sha256_json({"nodes": nodes, "edges": edges})}


def inject_continuity(segments, graph):
    if not graph.get("ok"):
        raise ValueError("CONTINUITY_GRAPH_INVALID: %s" % "; ".join(graph.get("errors") or []))
    by_id = {node["id"]: node for node in graph.get("nodes") or []}
    output = []
    for segment in segments:
        node = by_id.get(str(segment.get("id")))
        item = dict(segment)
        if node:
            item["continuity_graph"] = {"fingerprint": graph["fingerprint"], "node": node}
            item["continuity_in"] = item.get("continuity_in") or json.dumps(node.get("entry"), ensure_ascii=False)
            item["continuity_out"] = item.get("continuity_out") or json.dumps(node.get("exit"), ensure_ascii=False)
        output.append(item)
    return output


def stale_artifacts(segments):
    stale = []
    for segment in segments or []:
        panel = segment.get("storyboard_panel") or {}
        if panel.get("path") and file_sha256(panel["path"]) != panel.get("sha256"):
            stale.append({"segment_id": segment.get("id"), "reason": "STORYBOARD_PANEL_BYTES_CHANGED",
                          "invalidate": ["video_attempts", "take_reviews", "captions", "final"]})
        expected = panel.get("recipe_sha256")
        if expected and expected != sha256_json(__import__("artifact_contract").build_storyboard_panel_recipe(segment)):
            stale.append({"segment_id": segment.get("id"), "reason": "STORYBOARD_PANEL_RECIPE_CHANGED",
                          "invalidate": ["panel", "video_attempts", "take_reviews", "captions", "final"]})
    return {"generated_at": datetime.now().isoformat(timespec="seconds"), "stale": stale,
            "ok": not stale}


def write_binding_report(segments, results, out_path):
    results = {str(item.get("segment_id")): item for item in (results or []) if isinstance(item, dict)}
    rows = []
    for segment in segments or []:
        sid = str(segment.get("id"))
        panel = segment.get("storyboard_panel") or {}
        result = results.get(sid) or {}
        image = panel.get("path")
        image_html = '<img src="file://%s">' % html.escape(os.path.abspath(image)) if image else ""
        rows.append("<section><h2>%s</h2>%s<p>Tags: %s</p><pre>%s</pre><pre>%s</pre></section>" % (
            html.escape(sid), image_html, html.escape(" ".join(segment.get("ref_tags") or [])),
            html.escape(json.dumps({"panel": panel, "budget": segment.get("reference_budget_plan"),
                                    "risk": segment.get("risk_profile"), "quality": segment.get("panel_quality")},
                                   ensure_ascii=False, indent=2)),
            html.escape(json.dumps({"task": result.get("taskId"), "model": result.get("model"),
                                    "ocr": result.get("ocr_status"), "qc": result.get("media_qc")},
                                   ensure_ascii=False, indent=2))))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("<html><head><meta charset='utf-8'><style>body{background:#101114;color:#eee;font-family:sans-serif;padding:24px}section{border:1px solid #444;margin:16px 0;padding:16px}img{max-width:100%}pre{white-space:pre-wrap}</style></head><body><h1>Video binding report</h1>%s</body></html>" % "\n".join(rows))
    return os.path.abspath(out_path)
