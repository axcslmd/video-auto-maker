#!/usr/bin/env python3
"""Render a static dependency graph for storyboard and video run artifacts.

The graph is intentionally dependency-free so it can be opened from a run
directory without a server or a frontend build step.
"""
import argparse
import html
import json
import os
from pathlib import Path

import run_manifest
from project_utils import atomic_write_bytes


STATE_COLORS = {
    "confirmed": "#18864b",
    "completed": "#1769aa",
    "succeeded": "#1769aa",
    "approved": "#18864b",
    "stale": "#bd2c2c",
    "pending": "#c77d00",
    "pending_approval": "#c77d00",
    "submitted": "#1769aa",
    "running": "#1769aa",
    "in_progress": "#1769aa",
    "failed": "#bd2c2c",
    "error": "#bd2c2c",
    "missing": "#6b7280",
    "unknown": "#6b7280",
}


def _read_json(path):
    """Read an optional JSON artifact, rejecting malformed documents clearly."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取 JSON 文件 %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象: %s" % path)
    return value


def _normal_state(value):
    state = str(value or "unknown").strip().lower()
    return state if state in STATE_COLORS else "unknown"


def _stage_state(manifest, stage):
    generation = (manifest.get("generation") or {}).get(stage) or {}
    approved = bool((manifest.get("approvals") or {}).get(stage))
    if approved:
        try:
            return ("confirmed" if run_manifest.approval_is_current(manifest, stage)
                    else "stale")
        except (OSError, ValueError, TypeError, KeyError):
            return "stale"
    if generation.get("status"):
        state = _normal_state(generation["status"])
        return "stale" if state == "approved" else state
    return "unknown"


def _node(node_id, label, kind, state="unknown", detail=""):
    return {"id": node_id, "label": label, "kind": kind,
            "state": _normal_state(state), "detail": detail}


def _norm_tag(value):
    return str(value or "").strip().lower().lstrip("@").replace("-", "_")


def _shot_ref_tags(shot):
    tags = (shot or {}).get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return {_norm_tag(tag) for tag in tags if str(tag or "").strip()}


def _board_matches_shot(board_key, shot):
    tags = _shot_ref_tags(shot)
    if not tags:
        return False
    if board_key == "cast_board":
        return bool(tags & {"mina", "host", "cast", "character", "actor"})
    if board_key == "product_board":
        return any(tag.startswith("product") or tag in {"hero", "angle"} for tag in tags)
    if board_key == "product_usage_image":
        return any(tag in {"usage", "product_usage", "product_use"} for tag in tags)
    return False


def _confirmed_board_fallback(board, manifest, stage):
    """Only a current manifest approval may fill missing authored ref_tags."""
    if _normal_state((board or {}).get("status")) != "confirmed":
        return False
    try:
        return run_manifest.approval_is_current(manifest, stage)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _board_state(board, manifest, stage):
    state = _normal_state((board or {}).get("status"))
    if state in {"confirmed", "approved"}:
        return "confirmed" if _stage_state(manifest, stage) == "confirmed" else "stale"
    return state


def build_graph(manifest=None, storyboard_result=None, segments_spec=None):
    """Build serializable nodes and edges from available production artifacts."""
    manifest = manifest or {}
    storyboard_result = storyboard_result or {}
    segments_spec = segments_spec or {}
    nodes, edges = [], []
    node_ids = set()

    def add(node):
        if node["id"] not in node_ids:
            nodes.append(node)
            node_ids.add(node["id"])

    boards = (
        ("cast_board", "Role board"),
        ("product_board", "Product board"),
        ("product_usage_image", "Product-use board"),
    )
    board_ids = {}
    board_records = {}
    for key, label in boards:
        board = storyboard_result.get(key) or {}
        if board:
            stage = "product_usage" if key == "product_usage_image" else key
            board_id = "board:%s" % key
            add(_node(board_id, label, "board", _board_state(board, manifest, stage),
                      board.get("abspath") or board.get("path") or ""))
            board_ids[key] = board_id
            board_records[key] = board
        elif key in (manifest.get("generation") or {}) or key in (manifest.get("approvals") or {}):
            board_id = "board:%s" % key
            add(_node(board_id, label, "board", _stage_state(manifest, key)))
            board_ids[key] = board_id
            board_records[key] = {"status": _stage_state(manifest, key)}

    shot_ids = []
    for index, item in enumerate(storyboard_result.get("shots") or [], 1):
        shot = item.get("shot") or {}
        shot_key = str(shot.get("id") or index)
        node_id = "shot:%s" % shot_key
        state = item.get("status") or _stage_state(manifest, "storyboard")
        add(_node(node_id, "Storyboard shot %s" % shot_key, "shot", state,
                  item.get("abspath") or item.get("path") or ""))
        shot_ids.append(shot_key)
        for board_key, board_id in board_ids.items():
            if (_board_matches_shot(board_key, shot) or
                    (not _shot_ref_tags(shot) and
                     _confirmed_board_fallback(
                         board_records.get(board_key), manifest, {
                             "cast_board": "cast_board",
                             "product_board": "product_board",
                             "product_usage_image": "product_usage",
                         }[board_key]))):
                edges.append((board_id, node_id))

    segments = segments_spec.get("segments") or []
    task_states = {str(task.get("unit_id")): task.get("status")
                   for task in (manifest.get("tasks") or [])
                   if task.get("stage") == "video" and task.get("unit_id") is not None}
    segment_ids = []
    for index, segment in enumerate(segments, 1):
        segment_key = str(segment.get("id") or index)
        node_id = "segment:%s" % segment_key
        state = task_states.get(segment_key) or _stage_state(manifest, "video")
        add(_node(node_id, "Video segment %s" % segment_key, "segment", state,
                  segment.get("out_path") or ""))
        segment_ids.append(node_id)
        for ref in segment.get("references") or []:
            if not isinstance(ref, dict):
                continue
            ref_type = _norm_tag(ref.get("type"))
            if ref_type == "product_usage_identity" and board_ids.get("product_usage_image"):
                edges.append((board_ids["product_usage_image"], node_id))
        source_ids = segment.get("source_shot_ids") or [segment.get("source_shot_id") or segment_key]
        for source_id in source_ids:
            shot_id = "shot:%s" % source_id
            if shot_id in node_ids:
                edges.append((shot_id, node_id))

    final = ((manifest.get("generation") or {}).get("final") or {})
    final_outputs = final.get("outputs") or manifest.get("outputs") or []
    if final_outputs or "final" in (manifest.get("generation") or {}):
        output_state = _stage_state(manifest, "final")
        add(_node("output:assembled", "Assembled output", "output", output_state,
                  "\n".join(str(path) for path in final_outputs)))
        for segment_id in segment_ids:
            edges.append((segment_id, "output:assembled"))

    return {"nodes": nodes, "edges": [{"from": left, "to": right} for left, right in edges]}


def render_html(graph, title="Asset dependency graph"):
    """Render one self-contained HTML document using CSS Grid dependency columns."""
    groups = {"board": [], "shot": [], "segment": [], "output": []}
    for node in graph["nodes"]:
        groups.setdefault(node["kind"], []).append(node)

    def cards(kind):
        result = []
        for node in groups.get(kind, []):
            detail = ("<div class=\"detail\">%s</div>" % html.escape(node["detail"])
                      if node["detail"] else "")
            result.append("<article class=\"node %s\"><b>%s</b><span>%s</span>%s</article>" % (
                node["state"], html.escape(node["label"]), html.escape(node["state"]), detail))
        return "".join(result) or "<p class=\"empty\">No artifact found</p>"

    edge_list = "".join("<li>%s &rarr; %s</li>" % (
        html.escape(edge["from"]), html.escape(edge["to"])) for edge in graph["edges"])
    return """<!doctype html>
<html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{title}</title><style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f7f8fa;color:#18212b}}h1{{margin-top:0}}.graph{{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:1rem}}section{{min-width:0}}h2{{font-size:1rem}}.node{{border-left:6px solid #6b7280;background:#fff;padding:.7rem;margin:.6rem 0;box-shadow:0 1px 3px #0002;overflow-wrap:anywhere}}.node span{{display:block;font-size:.8rem;color:#4b5563;margin-top:.25rem}}.detail{{font-size:.75rem;color:#4b5563;margin-top:.45rem}}.confirmed,.approved{{border-color:#18864b}}.pending,.pending_approval{{border-color:#c77d00}}.completed,.succeeded,.submitted,.running,.in_progress{{border-color:#1769aa}}.stale,.failed,.error{{border-color:#bd2c2c}}.empty{{color:#6b7280}}details{{margin-top:2rem}}@media(max-width:800px){{.graph{{grid-template-columns:1fr 1fr}}}}@media(max-width:480px){{.graph{{grid-template-columns:1fr}}}}</style>
<body><h1>{title}</h1><div class=\"graph\"><section><h2>Role / product boards</h2>{boards}</section><section><h2>Storyboard shots</h2>{shots}</section><section><h2>Video segments</h2>{segments}</section><section><h2>Assembled output</h2>{outputs}</section></div><details><summary>Dependencies ({edge_count})</summary><ul>{edges}</ul></details></body></html>""".format(
        title=html.escape(title), boards=cards("board"), shots=cards("shot"),
        segments=cards("segment"), outputs=cards("output"),
        edge_count=len(graph["edges"]), edges=edge_list)


def generate_graph(*, out_path, manifest_path=None, storyboard_result_path=None, segments_path=None):
    """Read artifacts and atomically write a static graph page."""
    graph = build_graph(_read_json(manifest_path), _read_json(storyboard_result_path),
                        _read_json(segments_path))
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    atomic_write_bytes(
        out_path, render_html(graph).encode("utf-8"), label="asset_graph_html")
    return graph


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a static asset dependency graph")
    parser.add_argument("--out", required=True, help="Output HTML path")
    parser.add_argument("--manifest", help="Optional run manifest JSON")
    parser.add_argument("--storyboard-result", help="Optional storyboard_result.json")
    parser.add_argument("--segments", help="Optional segments.json")
    args = parser.parse_args(argv)
    graph = generate_graph(out_path=args.out, manifest_path=args.manifest,
                           storyboard_result_path=args.storyboard_result,
                           segments_path=args.segments)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
