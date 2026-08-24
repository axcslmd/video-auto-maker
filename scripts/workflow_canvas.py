#!/usr/bin/env python3
"""Workflow canvas for idea -> assets -> boards -> storyboard -> video -> edit.

This module aggregates the existing manifest, brief, storyboard and segment
artifacts into one customer-readable canvas that makes the current step,
material references, feedback loops and revision requests visible at a glance.

It is intentionally static and dependency-free so it can render in any browser
without a frontend build step.
"""
import argparse
import base64
import mimetypes
import html
import json
import os
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import asset_graph  # noqa: E402
import artifact_registry  # noqa: E402
import run_manifest as rm  # noqa: E402
from project_utils import atomic_write_bytes, atomic_write_json  # noqa: E402
try:
    import obs_log  # noqa: E402
except ImportError:  # pragma: no cover - optional for very old worktrees
    obs_log = None
try:
    import workflow_router  # noqa: E402
except ImportError:  # pragma: no cover - router is expected, but canvas can fall back
    workflow_router = None


STEP_ORDER = [
    ("idea", "Idea / Brief"),
    ("assets", "Assets"),
    ("boards", "Boards"),
    ("storyboard", "Storyboard"),
    ("video", "Video"),
    ("qc", "QC / Review"),
    ("edit", "Post Edit"),
    ("delivery", "Delivery"),
]

MILESTONE_ORDER = [
    ("plan", "方案定稿"),
    ("previs", "素材与预演"),
    ("generation_review", "生成与验片"),
    ("finish_delivery", "后期与交付"),
]

STATUS_LABELS = {
    "confirmed": "confirmed",
    "approved": "approved",
    "trusted_upload": "trusted_upload",
    "completed": "completed",
    "succeeded": "succeeded",
    "verified": "completed",
    "running": "running",
    "in_progress": "running",
    "submitted": "submitted",
    "pending": "pending",
    "pending_approval": "pending",
    "pending_confirmation": "pending",
    "stale": "stale",
    "failed": "failed",
    "invalidated": "invalidated",
    "missing": "failed",
    "unknown": "unknown",
}

ACTION_LABELS = {
    "review_ocr_warning": "查看 OCR 提醒，确认接受原生品牌文字或重出对应视频段",
    "review_generated_videos": "查看 5 段视频，确认是否进入后期剪辑",
    "review_storyboard_or_generate_video": "确认故事板，确认后进入视频生成",
    "prepare_boards": "生成或确认产品板、人物板和使用细节图",
}


def _load_json(path, default=None):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return default
    return value


def _json_lines(path):
    if not path or not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except ValueError:
                continue
    return rows


def _append_json_line(path, record):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_json_object(path):
    value = _load_json(path, {})
    return value if isinstance(value, dict) else {}


def _json_identity(value):
    if not isinstance(value, dict):
        return ("", "")
    return (str(value.get("client") or "").strip(),
            str(value.get("run_id") or value.get("runId") or "").strip())


def _candidate_key(client, run_id):
    return (str(client or "").strip(), str(run_id or "").strip())


def _update_candidate(candidates, *, client, run_id, kind, path, extra=None):
    client = str(client or "").strip()
    run_id = str(run_id or "").strip()
    if not client or not run_id or not path:
        return
    key = _candidate_key(client, run_id)
    candidate = candidates.setdefault(key, {
        "client": client,
        "run_id": run_id,
        "paths": {},
        "score": 0,
        "mtime": 0.0,
    })
    current_path = candidate["paths"].get(kind)
    if (not current_path or _file_mtime(path) >= _file_mtime(current_path)):
        candidate["paths"][kind] = os.path.abspath(path)
    candidate["score"] += 1
    candidate["mtime"] = max(candidate["mtime"], _file_mtime(path))
    if extra:
        for key_name, value in extra.items():
            if value and not candidate.get(key_name):
                candidate[key_name] = value


def _load_run_log_identity(path):
    if not path or not os.path.isfile(path):
        return None
    last = None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get("client") and record.get("run_id"):
                last = record
    return last


def _discover_segments_path(run_dir, client, run_id):
    if not run_dir or not os.path.isdir(run_dir):
        return ""
    preferred = []
    for name in (
        "segments.json",
        "batch_results.json",
        "%s_video_segments.json" % client if client else "",
        "%s_video_segments.recovered.json" % client if client else "",
        "%s_video_segments.fast_url_recovered.json" % client if client else "",
    ):
        if name:
            preferred.append(os.path.join(run_dir, name))
    preferred.extend(sorted(
        str(path) for path in Path(run_dir).glob("*video_segments*.json")
        if path.is_file()))
    preferred.extend(sorted(
        str(path) for path in Path(run_dir).glob("*segments*.json")
        if path.is_file()))
    for path in preferred:
        if not os.path.isfile(path):
            continue
        data = _load_json(path, {})
        if isinstance(data, dict) and data.get("segments"):
            return os.path.abspath(path)
        if os.path.basename(path) == "segments.json":
            return os.path.abspath(path)
    if run_id:
        candidate = os.path.join(run_dir, "%s_video_segments.json" % run_id)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ""


def _discover_first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def _discover_canvas_candidates(*, root, client, run_id, manifest_path,
                                brief_path, storyboard_result_path,
                                segments_path, events_path):
    root = os.path.abspath(root or ROOT)
    candidates = {}

    def maybe_manifest(path):
        record = _load_json_object(path)
        cl, rid = _json_identity(record)
        if cl and rid:
            kind = ("run_manifest_path" if os.path.basename(path) == "run_manifest.json"
                    else "manifest_path")
            _update_candidate(candidates, client=cl, run_id=rid, kind=kind, path=path,
                              extra={"manifest_data": record})

    def maybe_storyboard(path):
        record = _load_json_object(path)
        cl, rid = _json_identity(record)
        if cl and rid:
            extra = {"storyboard_data": record}
            out_dir = record.get("out_dir")
            if out_dir:
                extra["storyboard_dir"] = os.path.abspath(out_dir)
            _update_candidate(candidates, client=cl, run_id=rid, kind="storyboard_result_path",
                              path=path, extra=extra)

    def maybe_run_log(path):
        record = _load_run_log_identity(path)
        if not record:
            return
        _update_candidate(candidates, client=record.get("client"), run_id=record.get("run_id"),
                          kind="events_path", path=path, extra={"events_data": record})

    def maybe_segments(path):
        record = _load_json_object(path)
        cl, rid = _json_identity(record)
        if cl and rid:
            _update_candidate(candidates, client=cl, run_id=rid, kind="segments_path", path=path,
                              extra={"segments_data": record})

    def maybe_jsonl_events(path):
        record = _load_run_log_identity(path)
        if record:
            _update_candidate(candidates, client=record.get("client"), run_id=record.get("run_id"),
                              kind="events_path", path=path, extra={"events_data": record})

    def maybe_generic_json(path, kind, extra=None):
        record = _load_json_object(path)
        cl, rid = _json_identity(record)
        if cl and rid:
            _update_candidate(candidates, client=cl, run_id=rid, kind=kind, path=path, extra=extra)

    def maybe_stage_file(path, stage_name, inferred_client=None, inferred_run_id=None):
        if not path or not os.path.isfile(path):
            return
        try:
            record = _load_json_object(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        cl, rid = _json_identity(record)
        cl = cl or inferred_client or ""
        rid = rid or inferred_run_id or ""
        if cl and rid:
            _update_candidate(candidates, client=cl, run_id=rid, kind=stage_name,
                              path=path, extra={stage_name + "_data": record})

    def maybe_brief(path, inferred_client=None):
        if not path or not os.path.isfile(path):
            return
        record = _load_json_object(path)
        cl = str(record.get("client") or inferred_client or "").strip()
        if not cl:
            return
        key = None
        if str(record.get("run_id") or "").strip():
            key = _candidate_key(cl, str(record.get("run_id")))
        else:
            for existing_key, candidate in candidates.items():
                if candidate.get("client") == cl:
                    key = existing_key
                    break
        if key is None:
            return
        candidate = candidates.setdefault(key, {
            "client": cl,
            "run_id": str(record.get("run_id") or "").strip(),
            "paths": {},
            "score": 0,
            "mtime": 0.0,
        })
        current_path = candidate["paths"].get("brief_path")
        if (not current_path or _file_mtime(path) >= _file_mtime(current_path)):
            candidate["paths"]["brief_path"] = os.path.abspath(path)
        candidate["score"] += 1
        candidate["mtime"] = max(candidate["mtime"], _file_mtime(path))

    # Explicit paths are authoritative inputs and must participate in discovery.
    # Previously the function accepted them but silently ignored them.
    maybe_manifest(manifest_path)
    maybe_storyboard(storyboard_result_path)
    maybe_segments(segments_path)
    maybe_run_log(events_path)
    maybe_brief(brief_path, inferred_client=client)

    if client and run_id:
        run_dir = os.path.join(root, "output", client, run_id)
        run_manifest_guess = os.path.join(run_dir, "run_manifest.json")
        legacy_manifest_guess = os.path.join(run_dir, "manifest.json")
        storyboard_guess = os.path.join(root, "output", "storyboard", run_id, "storyboard_result.json")
        final_guess = os.path.join(run_dir, "final.mp4")
        caption_guess = os.path.join(run_dir, "caption_manifest.json")
        delivery_guess = os.path.join(run_dir, "delivery.json")
        script_guess = os.path.join(root, "assets", client, "brief.json")
        maybe_manifest(run_manifest_guess)
        maybe_manifest(legacy_manifest_guess)
        maybe_storyboard(storyboard_guess)
        maybe_run_log(os.path.join(run_dir, "run.log"))
        maybe_segments(_discover_segments_path(run_dir, client, run_id))
        maybe_brief(os.path.join(root, "assets", client, "brief.json"), inferred_client=client)
        maybe_brief(os.path.join(root, "output", client, "assets", "brief.json"), inferred_client=client)
        maybe_generic_json(caption_guess, "caption_manifest")
        maybe_generic_json(delivery_guess, "delivery")
        maybe_stage_file(script_guess, "brief", inferred_client=client, inferred_run_id=run_id)

    search_roots = [
        os.path.join(root, "output"),
        os.path.join(root, "assets"),
    ]
    for search_root in search_roots:
        if not os.path.isdir(search_root):
            continue
        for path in Path(search_root).rglob("manifest.json"):
            maybe_manifest(str(path))
        for path in Path(search_root).rglob("run_manifest.json"):
            maybe_manifest(str(path))
        for path in Path(search_root).rglob("caption_manifest.json"):
            maybe_generic_json(str(path), "caption_manifest")
        for path in Path(search_root).rglob("delivery.json"):
            maybe_generic_json(str(path), "delivery")
        for path in Path(search_root).rglob("final.mp4"):
            maybe_stage_file(str(path), "final")
            candidate = os.path.dirname(str(path))
            maybe_jsonl_events(os.path.join(candidate, "run.log"))
        for path in Path(search_root).rglob("storyboard_result.json"):
            maybe_storyboard(str(path))
        for path in Path(search_root).rglob("run.log"):
            maybe_run_log(str(path))
        for path in Path(search_root).rglob("*video_segments*.json"):
            maybe_segments(str(path))
        for path in Path(search_root).glob("*/brief.json"):
            maybe_brief(str(path), inferred_client=path.parent.name)
        for path in Path(search_root).rglob("brief.json"):
            if path.parent.name == "assets":
                maybe_brief(str(path), inferred_client=path.parent.parent.name)
    return candidates


def discover_canvas_inputs(*, root=None, client=None, run_id=None,
                           manifest_path=None, brief_path=None,
                           storyboard_result_path=None, segments_path=None,
                           events_path=None):
    """Discover the most complete current workflow run without user re-entry."""
    root = os.path.abspath(root or ROOT)
    candidates = _discover_canvas_candidates(
        root=root, client=client, run_id=run_id,
        manifest_path=manifest_path, brief_path=brief_path,
        storyboard_result_path=storyboard_result_path,
        segments_path=segments_path, events_path=events_path)
    if not candidates:
        return {}

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item.get("score", 0),
            item.get("mtime", 0.0),
            item.get("client", ""),
            item.get("run_id", ""),
        ),
        reverse=True,
    )
    selected = ordered[0]
    client = selected.get("client") or client or ""
    run_id = selected.get("run_id") or run_id or ""
    selected_manifest = (selected.get("paths", {}).get("run_manifest_path") or
                         selected.get("paths", {}).get("manifest_path"))
    run_dir = selected_manifest and os.path.dirname(selected_manifest)
    if not run_dir:
        run_dir = selected.get("paths", {}).get("events_path") and os.path.dirname(selected["paths"]["events_path"])
    if not run_dir:
        run_dir = selected.get("paths", {}).get("segments_path") and os.path.dirname(selected["paths"]["segments_path"])
    if not run_dir:
        run_dir = os.path.join(root, "output", client, run_id) if client and run_id else ""
    storyboard_dir = selected.get("paths", {}).get("storyboard_dir") or (
        os.path.join(root, "output", "storyboard", run_id) if run_id else ""
    )
    manifest_path = selected_manifest or _discover_first_existing([
        os.path.join(run_dir, "run_manifest.json") if run_dir else "",
        os.path.join(run_dir, "manifest.json") if run_dir else "",
    ])
    brief_candidates = [
        selected.get("paths", {}).get("brief_path"),
        os.path.join(root, "assets", client, "brief.json") if client else "",
        os.path.join(root, "output", client, "assets", "brief.json") if client else "",
    ]
    brief_path = next((os.path.abspath(path) for path in brief_candidates if path and os.path.isfile(path)), "")
    storyboard_candidates = [
        selected.get("paths", {}).get("storyboard_result_path"),
        os.path.join(storyboard_dir, "storyboard_result.json") if storyboard_dir else "",
    ]
    storyboard_result_path = next((os.path.abspath(path) for path in storyboard_candidates
                                   if path and os.path.isfile(path)), "")
    caption_candidates = [
        os.path.join(run_dir, "caption_manifest.json") if run_dir else "",
        selected.get("paths", {}).get("caption_manifest_path"),
    ]
    caption_manifest_path = next((os.path.abspath(path) for path in caption_candidates
                                  if path and os.path.isfile(path)), "")
    delivery_candidates = [
        os.path.join(run_dir, "delivery.json") if run_dir else "",
        selected.get("paths", {}).get("delivery_path"),
    ]
    delivery_path = next((os.path.abspath(path) for path in delivery_candidates
                          if path and os.path.isfile(path)), "")
    events_candidates = [
        selected.get("paths", {}).get("events_path"),
        os.path.join(run_dir, "run.log") if run_dir else "",
    ]
    events_path = next((os.path.abspath(path) for path in events_candidates if path and os.path.isfile(path)), "")
    segments_candidates = [
        selected.get("paths", {}).get("segments_path"),
        _discover_segments_path(run_dir, client, run_id),
    ]
    segments_path = next((os.path.abspath(path) for path in segments_candidates if path and os.path.isfile(path)), "")
    if not manifest_path and not brief_path and not storyboard_result_path and not segments_path and not events_path:
        return {}
    return {
        "client": client,
        "run_id": run_id,
        "run_dir": os.path.abspath(run_dir) if run_dir else "",
        "storyboard_dir": os.path.abspath(storyboard_dir) if storyboard_dir else "",
        "manifest_path": os.path.abspath(manifest_path) if manifest_path else "",
        "brief_path": brief_path,
        "storyboard_result_path": storyboard_result_path,
        "caption_manifest_path": caption_manifest_path,
        "delivery_path": delivery_path,
        "segments_path": segments_path,
        "events_path": events_path,
        "final_path": _discover_first_existing([
            os.path.join(run_dir, "final.mp4") if run_dir else "",
            selected.get("paths", {}).get("final_path"),
        ]),
        "discovery_source": {
            "score": selected.get("score", 0),
            "mtime": selected.get("mtime", 0.0),
            "paths": dict(selected.get("paths") or {}),
        },
    }


def _resolve_canvas_args(*, root=None, client=None, run_id=None,
                         manifest_path=None, brief_path=None,
                         storyboard_result_path=None, segments_path=None,
                         events_path=None):
    discovered = discover_canvas_inputs(
        root=root, client=client, run_id=run_id, manifest_path=manifest_path,
        brief_path=brief_path, storyboard_result_path=storyboard_result_path,
        segments_path=segments_path, events_path=events_path)
    if not discovered:
        return {
            "client": client or "",
            "run_id": run_id or "",
            "manifest_path": manifest_path or "",
        "brief_path": brief_path or "",
        "storyboard_result_path": storyboard_result_path or "",
        "caption_manifest_path": "",
        "delivery_path": "",
        "final_path": "",
        "segments_path": segments_path or "",
        "events_path": events_path or "",
        "out_path": "",
        "discovery_source": {},
        }
    out_root = discovered.get("run_dir") or discovered.get("storyboard_dir") or os.path.join(
        os.path.abspath(root or ROOT), "output", discovered.get("client") or "",
        discovered.get("run_id") or "")
    return {
        "client": discovered.get("client") or client or "",
        "run_id": discovered.get("run_id") or run_id or "",
        "manifest_path": discovered.get("manifest_path") or manifest_path or "",
        "brief_path": discovered.get("brief_path") or brief_path or "",
        "storyboard_result_path": discovered.get("storyboard_result_path") or storyboard_result_path or "",
        "caption_manifest_path": discovered.get("caption_manifest_path") or "",
        "delivery_path": discovered.get("delivery_path") or "",
        "final_path": discovered.get("final_path") or "",
        "segments_path": discovered.get("segments_path") or segments_path or "",
        "events_path": discovered.get("events_path") or events_path or "",
        "out_path": os.path.join(out_root, "workflow_canvas.html") if out_root else "",
        "discovery_source": discovered.get("discovery_source") or {},
    }


def _snapshot_fingerprint(snapshot):
    core = dict(snapshot or {})
    core.pop("history", None)
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, default=str,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _history_path(out_dir):
    return os.path.join(os.path.abspath(out_dir), "workflow_canvas_history.jsonl")


def _interaction_path(out_dir):
    return os.path.join(os.path.abspath(out_dir), "workflow_canvas_interactions.jsonl")


def _compact_steps(steps):
    return [{"id": item.get("id"), "status": item.get("status"),
             "current": bool(item.get("is_current"))}
            for item in steps or []]


def _compact_assets(assets):
    return [{"tag": item.get("tag"), "status": item.get("status"),
             "path": item.get("path"), "edited_from": item.get("edited_from"),
             "feedback_refs": list(item.get("feedback_refs") or [])}
            for item in assets or []]


def _compact_loops(loops):
    return [{"kind": item.get("kind"), "status": item.get("status"),
             "path": item.get("path"), "refs": list(item.get("refs") or [])}
            for item in loops or []]


def _compact_refs(refs):
    return [{"kind": item.get("kind"), "tag": item.get("tag"),
             "scope": item.get("scope"), "segment_id": item.get("segment_id"),
             "source": item.get("source")}
            for item in refs or []]


def _history_delta(previous, current):
    if not previous:
        return {
            "stage_changed": None,
            "step_changes": [],
            "asset_changes": [],
            "loop_changes": [],
            "reference_count_changed": None,
        }
    prev_steps = {item["id"]: item for item in previous.get("steps") or [] if item.get("id")}
    step_changes = []
    for item in current.get("steps") or []:
        old = prev_steps.get(item.get("id"))
        if not old:
            step_changes.append({"id": item.get("id"), "from": None, "to": item.get("status")})
        elif old.get("status") != item.get("status") or old.get("current") != bool(item.get("is_current")):
            step_changes.append({"id": item.get("id"), "from": old.get("status"), "to": item.get("status")})

    prev_assets = {(item.get("tag"), item.get("path")): item for item in previous.get("assets") or []}
    asset_changes = []
    for item in current.get("assets") or []:
        key = (item.get("tag"), item.get("path"))
        old = prev_assets.get(key)
        if not old:
            asset_changes.append({"tag": item.get("tag"), "from": None, "to": item.get("status")})
        elif old.get("status") != item.get("status") or old.get("edited_from") != item.get("edited_from") \
                or old.get("feedback_refs") != item.get("feedback_refs"):
            asset_changes.append({"tag": item.get("tag"), "from": old.get("status"), "to": item.get("status")})

    prev_loops = {(item.get("kind"), item.get("path")): item for item in previous.get("feedback_loops") or []}
    loop_changes = []
    for item in current.get("feedback_loops") or []:
        key = (item.get("kind"), item.get("path"))
        old = prev_loops.get(key)
        if not old:
            loop_changes.append({"kind": item.get("kind"), "path": item.get("path"), "change": "added"})
        elif old.get("status") != item.get("status") or old.get("refs") != item.get("refs"):
            loop_changes.append({"kind": item.get("kind"), "path": item.get("path"), "change": "updated"})

    return {
        "stage_changed": [previous.get("current_stage"), current.get("current_stage")]
        if previous.get("current_stage") != current.get("current_stage") else None,
        "step_changes": step_changes,
        "asset_changes": asset_changes,
        "loop_changes": loop_changes,
        "reference_count_changed": [len(previous.get("references") or []), len(current.get("references") or [])]
        if len(previous.get("references") or []) != len(current.get("references") or []) else None,
    }


def _history_entry(snapshot, previous=None, *, source="poll", reason=None, extra=None):
    entry = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "fingerprint": _snapshot_fingerprint(snapshot),
        "source": source,
        "reason": reason or "",
        "current_stage": snapshot.get("current_stage") or "idea",
        "next_action": snapshot.get("next_action") or "",
        "customer_message": snapshot.get("customer_message") or "",
        "steps": _compact_steps(snapshot.get("steps") or []),
        "assets": _compact_assets(snapshot.get("assets") or []),
        "feedback_loops": _compact_loops(snapshot.get("feedback_loops") or []),
        "references": _compact_refs(snapshot.get("references") or []),
        "event_count": len(snapshot.get("events") or []),
        "delta": _history_delta(previous or {}, snapshot),
    }
    if extra:
        entry.update(extra)
    return entry


def _load_history(out_dir, limit=25):
    path = _history_path(out_dir)
    entries = _json_lines(path)
    return entries[-limit:] if limit else entries


def _record_history(out_dir, snapshot, *, source="poll", reason=None, extra=None, force=False):
    path = _history_path(out_dir)
    current_fp = _snapshot_fingerprint(snapshot)
    previous = None
    entries = _json_lines(path)
    if entries:
        previous = entries[-1]
        if not force and previous.get("fingerprint") == current_fp:
            return previous
    entry = _history_entry(snapshot, previous, source=source, reason=reason, extra=extra)
    _append_json_line(path, entry)
    return entry


def _record_interaction(out_dir, kind, payload):
    entry = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        **payload,
    }
    _append_json_line(_interaction_path(out_dir), entry)
    if obs_log is not None:
        try:
            obs_log.log_event("workflow_interaction", **entry)
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    return entry


def _load_events(path):
    if not path:
        return []
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
    return events


def _abs(path):
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.abspath(path)


def _is_image_path(path):
    if not path or not os.path.isfile(path):
        return False
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith("image/"))


def _is_video_path(path):
    path = _abs(path)
    if not path or not os.path.isfile(path):
        return False
    mime, _ = mimetypes.guess_type(path)
    return bool((mime and mime.startswith("video/")) or os.path.splitext(path)[1].lower() in {".mp4", ".mov", ".webm", ".m4v"})


def _file_url(path):
    path = _abs(path)
    if not path:
        return ""
    return "file://" + path


def _preview_src(path, *, limit_bytes=20_000_000):
    path = _abs(path)
    if not path or not os.path.isfile(path):
        return ""
    if _is_image_path(path):
        try:
            size = os.path.getsize(path)
            if size <= limit_bytes:
                with open(path, "rb") as handle:
                    raw = handle.read()
                mime, _ = mimetypes.guess_type(path)
                mime = mime or "application/octet-stream"
                encoded = base64.b64encode(raw).decode("ascii")
                return "data:%s;base64,%s" % (mime, encoded)
        except OSError:
            pass
    return _file_url(path)


def _preview_img_html(path, *, label="preview"):
    src = _preview_src(path)
    if not src:
        return ""
    return (
        "<figure class='thumb'><img src='%s' alt='%s' loading='lazy' />"
        "<figcaption>%s</figcaption></figure>"
        % (html.escape(src, quote=True), html.escape(label), html.escape(os.path.basename(_abs(path)))))


def _preview_video_html(path, *, label="video"):
    path = _abs(path)
    if not path or not os.path.isfile(path):
        return ""
    src = _file_url(path)
    return (
        "<figure class='video-preview'><video controls preload='metadata' playsinline src='%s'></video>"
        "<figcaption>%s</figcaption></figure>"
        % (html.escape(src, quote=True), html.escape(os.path.basename(path))))


def _preview_media_html(path, *, label="preview"):
    if _is_video_path(path):
        return _preview_video_html(path, label=label)
    return _preview_img_html(path, label=label)


def _status_bucket(value):
    status = str(value or "unknown").strip().lower()
    return STATUS_LABELS.get(status, "unknown")


def _action_label(value):
    value = str(value or "").strip()
    return ACTION_LABELS.get(value, value)


def _pick_stage(manifest, stage_name):
    if not manifest:
        return "unknown"
    generation = (manifest.get("generation") or {}).get(stage_name) or {}
    if (manifest.get("approvals") or {}).get(stage_name):
        try:
            return "confirmed" if rm.approval_is_current(manifest, stage_name) else "stale"
        except (OSError, ValueError, TypeError, KeyError):
            return "stale"
    status = _status_bucket(generation.get("status"))
    # A cached approved/confirmed generation status is not authorization. Only
    # the content-bound manifest approval above may be rendered as confirmed.
    if status in {"confirmed", "approved"}:
        return "stale"
    if generation.get("status"):
        return status
    return "unknown"


def _aggregate_status(statuses, *, empty="unknown", unverified_confirmed=False):
    statuses = [_status_bucket(item) for item in statuses if item is not None]
    if not statuses:
        return empty
    if unverified_confirmed and any(item in {"confirmed", "approved"} for item in statuses):
        return "pending"
    if any(item in {"failed", "invalidated"} for item in statuses):
        return "failed"
    if any(item == "stale" for item in statuses):
        return "stale"
    if any(item == "running" for item in statuses):
        return "running"
    if any(item in {"pending", "submitted"} for item in statuses):
        return "pending"
    if all(item in {"confirmed", "approved"} for item in statuses):
        return "confirmed"
    if all(item in {"confirmed", "approved", "trusted_upload", "completed", "succeeded"}
           for item in statuses):
        return "completed"
    if any(item != "unknown" for item in statuses):
        return "pending"
    return empty


def _board_step_status(manifest, storyboard_result):
    board_keys = (
        ("product_board", "product_board"),
        ("cast_board", "cast_board"),
        ("product_usage_image", "product_usage"),
    )
    if manifest:
        try:
            dependencies = set(rm.generation_dependencies(manifest, "storyboard"))
        except (OSError, ValueError, TypeError, KeyError):
            dependencies = {
                stage for _, stage in board_keys
                if manifest.get({
                    "product_board": "requires_product_board",
                    "cast_board": "requires_cast_board",
                    "product_usage": "requires_product_usage",
                }[stage])
            }
        stages = [stage for _, stage in board_keys if stage in dependencies]
        if not stages:
            return "completed"
        return _aggregate_status([_pick_stage(manifest, stage) for stage in stages])

    board_states = []
    for key, _ in board_keys:
        board = storyboard_result.get(key) or {}
        if isinstance(board, dict) and board:
            board_states.append(board.get("status") or "pending")
    return _aggregate_status(
        board_states, unverified_confirmed=True) if board_states else "unknown"


def _delivery_step_status(manifest, pipeline):
    action = str(pipeline.get("next_action") or "")
    if action == "complete":
        return "completed"
    if action == "repair_delivery_evidence":
        return "stale"
    if action == "create_delivery":
        return "pending"
    delivery = (manifest or {}).get("delivery") or {}
    if delivery.get("path"):
        return ("completed" if _delivery_is_current(manifest, delivery["path"])
                else "stale")
    if pipeline.get("current_stage") == "delivery":
        return "pending"
    return "unknown"


def _replace_step_status(snapshot, step_id, status):
    for step in snapshot.get("steps") or []:
        if step.get("id") == step_id:
            step["status"] = _status_bucket(status)
            return


def _pipeline_status(manifest):
    if workflow_router and hasattr(workflow_router, "route_workflow"):
        try:
            return workflow_router.route_workflow(manifest=manifest)
        except (OSError, ValueError, TypeError, RuntimeError, KeyError):
            pass
    return workflow_router.route_workflow(manifest=manifest) if workflow_router else {}


def _delivery_is_current(manifest, delivery_path):
    """Verify a delivery only when the canvas actually displays one.

    Delivery validation pulls in the full post-production stack.  Keeping that
    import outside module initialization lets the canvas remain an observer and
    prevents it from becoming a reverse dependency of storyboard generation.
    """
    try:
        import pipeline
        pipeline.verify_delivery(manifest, delivery_path)
        return True
    except (ImportError, OSError, ValueError, TypeError, KeyError,
            json.JSONDecodeError):
        return False


def _brief_assets(brief):
    assets = []
    for item in brief.get("images") or []:
        if not isinstance(item, dict):
            continue
        assets.append({
            "tag": item.get("tag") or "",
            "path": _abs(item.get("path")),
            "status": _status_bucket(item.get("status")),
            "source": item.get("source") or item.get("source_kind") or "",
            "via": item.get("via") or "",
            "edited_from": _abs(item.get("edited_from")),
            "feedback_refs": [_abs(p) for p in item.get("feedback_refs") or []],
            "variant": item.get("variant_label") or item.get("variant") or "",
            "prompt": item.get("prompt") or "",
            "extra": {
                key: value for key, value in item.items()
                if key in ("source", "source_kind", "processing_kind", "model",
                           "source_fingerprint", "board_sha256", "variant_label")
            },
        })
    document_fields = (
        ("ppt_files", "ppt"), ("pdf_files", "pdf"),
        ("document_files", "document"), ("word_files", "word"),
        ("source_docs", "document"), ("documents", "document"),
        ("source_ppt", "ppt"), ("source_pdf", "pdf"),
        ("source_document", "document"),
    )
    seen_paths = {item["path"] for item in assets if item.get("path")}
    for field, kind in document_fields:
        raw_items = brief.get(field)
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        for index, raw in enumerate(raw_items, 1):
            item = raw if isinstance(raw, dict) else {"path": raw}
            path = _abs(item.get("path") or item.get("source") or item.get("file"))
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            assets.append({
                "tag": item.get("tag") or "@%s_%d" % (kind, index),
                "path": path,
                "status": _status_bucket(item.get("status") or (
                    "trusted_upload" if os.path.isfile(path) else "missing")),
                "source": item.get("source_kind") or kind,
                "via": item.get("via") or "brief.%s" % field,
                "edited_from": _abs(item.get("edited_from")),
                "feedback_refs": [_abs(p) for p in item.get("feedback_refs") or []],
                "variant": item.get("variant_label") or item.get("variant") or "",
                "prompt": item.get("prompt") or "",
                "extra": {"document_kind": kind, "brief_field": field},
            })
    return assets


def _board_feedback(storyboard_result):
    feedback = []
    if not isinstance(storyboard_result, dict):
        return feedback
    for key in ("cast_board", "product_board", "product_usage_image"):
        item = storyboard_result.get(key) or {}
        if not isinstance(item, dict):
            continue
        status = _status_bucket(item.get("status"))
        revision = item.get("refinement") or {}
        feedback_refs = [p for p in revision.get("feedback_refs") or [] if p]
        if status in {"pending", "stale", "failed", "invalidated"} or feedback_refs:
            feedback.append({
                "kind": key,
                "status": status,
                "path": _abs(item.get("abspath") or item.get("path")),
                "edit_prompt": revision.get("edit_prompt") or "",
                "feedback_refs": [_abs(p) for p in feedback_refs],
                "source_refs": [_abs(p) for p in revision.get("generation_reference_paths") or []],
                "identity_refs": [_abs(p) for p in revision.get("identity_reference_paths") or []],
            })
    return feedback


def _shot_feedback(storyboard_result):
    items = []
    if not isinstance(storyboard_result, dict):
        return items
    for shot_item in storyboard_result.get("shots") or []:
        if not isinstance(shot_item, dict):
            continue
        shot = shot_item.get("shot") or {}
        items.append({
            "shot_id": str(shot.get("id") or ""),
            "path": _abs(shot_item.get("abspath") or shot_item.get("path")),
            "status": _status_bucket(shot_item.get("status") or storyboard_result.get("status")),
            "ref_tags": [str(tag) for tag in shot.get("ref_tags") or []],
            "dialogue": shot.get("dialogue") or "",
            "visual": shot.get("visual") or "",
        })
    return items


def _feedback_loop_items(brief_assets, board_feedback, storyboard_result, manifest):
    loops = []
    for asset in brief_assets:
        if asset["edited_from"] or asset["feedback_refs"]:
            loops.append({
                "kind": "asset_revision",
                "title": asset["tag"] or "revision",
                "status": asset["status"],
                "reason": "用户建议已回写为精修版本",
                "path": asset["path"],
                "refs": asset["feedback_refs"] or ([asset["edited_from"]] if asset["edited_from"] else []),
                "note": asset["prompt"] or "",
            })
        elif asset["status"] in {"pending", "stale", "failed", "invalidated"}:
            loops.append({
                "kind": "asset",
                "title": asset["tag"] or "unnamed asset",
                "status": asset["status"],
                "reason": "素材待确认或需要重做",
                "path": asset["path"],
                "refs": asset["feedback_refs"] or ([asset["edited_from"]] if asset["edited_from"] else []),
                "note": asset["prompt"] or "",
            })

    for item in board_feedback:
        loops.append({
            "kind": "board_revision",
            "title": item["kind"],
            "status": item["status"],
            "reason": "板件待确认或已根据反馈重做",
            "path": item["path"],
            "refs": item["feedback_refs"] or item["identity_refs"] or item["source_refs"],
            "note": item["edit_prompt"],
        })

    story_gate = storyboard_result.get("needs_confirmation") if isinstance(storyboard_result, dict) else None
    if story_gate:
        loops.append({
            "kind": "storyboard_confirmation",
            "title": "storyboard",
            "status": "pending",
            "reason": "故事板待客户确认，用户仍可提出修改建议",
            "path": _abs(storyboard_result.get("result_json") or storyboard_result.get("path")),
            "refs": [item["path"] for item in board_feedback if item["path"]],
            "note": "确认故事板前可继续修改镜头、构图、素材引用和人物关系。",
        })

    pipeline = _pipeline_status(manifest) if manifest else {}
    if pipeline.get("current_stage") in ("delivery", "final"):
        delivery_complete = pipeline.get("next_action") == "complete"
        loops.append({
            "kind": "delivery_check",
            "title": pipeline.get("current_stage", "delivery"),
            "status": ("completed" if delivery_complete else
                       ("pending" if pipeline.get("current_stage") == "delivery" else "unknown")),
            "reason": pipeline.get("customer_message") or "",
            "path": "",
            "refs": pipeline.get("customer_preview") or [],
            "note": pipeline.get("cost_line") or "",
        })
    return loops


def _event_counts(events):
    counts = {}
    for event in events or []:
        name = str(event.get("event") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _infer_runtime_status(manifest, brief, storyboard_result, segments, events):
    if manifest:
        return _pipeline_status(manifest)
    route = workflow_router.route_workflow(
        brief=brief, storyboard_result=storyboard_result, segments=segments, events=events)
    if route:
        return route
    return workflow_router.route_workflow()


def _build_steps(manifest, brief, storyboard_result, segments=None, events=None):
    steps = []
    pipeline = _infer_runtime_status(manifest, brief, storyboard_result, segments or [], events or [])
    current_stage = pipeline.get("current_stage") or "idea"
    approval_status = str(
        (storyboard_result.get("storyboard_approval") or {}).get("status") or ""
    ).strip().lower()
    storyboard_invalidated = bool(
        storyboard_result.get("invalidated") or
        approval_status in {"invalidated", "stale", "failed", "rejected"})
    has_storyboard = bool(storyboard_result.get("shots"))
    storyboard_approval_current = workflow_router.storyboard_result_approval_is_current(
        storyboard_result)
    storyboard_status = (
        "pending" if storyboard_invalidated or storyboard_result.get("needs_confirmation") else
        ("confirmed" if has_storyboard and storyboard_approval_current else
         "pending" if has_storyboard else "unknown"))
    video_done = any(_is_video_path((node.get("detail") or "")) for node in asset_graph.build_graph({}, storyboard_result, {"segments": segments or []}).get("nodes") or [])
    has_ocr_warning = bool(_event_counts(events or []).get("ocr_warning"))
    brief_assets = _brief_assets(brief)
    has_brief = bool(brief.get("texts") or brief.get("specs") or brief_assets)
    idea_status = _pick_stage(manifest, "brief") if manifest else (
        "completed" if has_brief else "unknown")
    if manifest and idea_status == "unknown" and has_brief:
        idea_status = "pending"
    assets_status = _aggregate_status(
        [item.get("status") for item in brief_assets], empty="unknown")
    step_status = {
        "idea": idea_status,
        "assets": assets_status,
        "boards": _board_step_status(manifest, storyboard_result),
        "storyboard": _pick_stage(manifest, "storyboard") if manifest else storyboard_status,
        "video": _pick_stage(manifest, "video") if manifest else ("completed" if video_done else "unknown"),
        "qc": _pick_stage(manifest, "final") if manifest else ("pending" if has_ocr_warning else ("pending" if video_done else "unknown")),
        "edit": _pick_stage(manifest, "captions") if manifest else "unknown",
        "delivery": _delivery_step_status(manifest, pipeline),
    }
    step_detail = {
        "idea": "brief / script / render plan",
        "assets": "%d asset(s) in brief" % len(brief_assets),
        "boards": "boards and preview gallery",
        "storyboard": "storyboard_result / approvals",
        "video": "segments / handoffs / takes",
        "qc": "review / OCR / formal QC",
        "edit": "captions / basecut / final_edit",
        "delivery": "delivery evidence / manifest",
    }
    for step_id, title in STEP_ORDER:
        steps.append({
            "id": step_id,
            "title": title,
            "status": _status_bucket(step_status.get(step_id)),
            "is_current": step_id == current_stage,
            "detail": step_detail.get(step_id, ""),
        })
    return steps


def _build_milestone_steps(manifest, technical_stage, segments=None):
    """Expose four customer milestones while preserving technical stages below."""
    try:
        registry = artifact_registry.compile_registry(manifest or {}, segments or [])
    except (KeyError, TypeError, ValueError):
        registry = {"nodes": [], "edges": [], "ready_actions": [], "milestones": []}
    current = artifact_registry.milestone_for_stage(technical_stage)
    status_map = {
        "completed": "completed", "in_progress": "running", "ready": "pending",
        "blocked": "unknown", "stale": "stale", "failed": "failed",
    }
    by_id = {item.get("id"): item for item in registry.get("milestones") or []}
    steps = []
    for milestone_id, title in MILESTONE_ORDER:
        item = by_id.get(milestone_id) or {}
        completed = int(item.get("completed_units") or 0)
        total = int(item.get("total_units") or 0)
        steps.append({
            "id": milestone_id,
            "title": title,
            "status": status_map.get(item.get("state"), "unknown"),
            "is_current": milestone_id == current,
            "detail": "%d/%d 个必需产物已完成" % (completed, total),
        })
    return steps, registry


def _reference_rows(storyboard_result, segments):
    rows = []
    if isinstance(storyboard_result, dict):
        for item in storyboard_result.get("reference_registry") or []:
            rows.append({
                "source": item.get("url") or item.get("source") or "",
                "label": item.get("source") or "",
                "tag": item.get("tag") or "",
                "scope": item.get("scope") or "",
                "kind": "storyboard",
                "type": item.get("type") or "",
            })
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        refs = seg.get("references") or []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            rows.append({
                "source": ref.get("url") or ref.get("source") or "",
                "tag": ref.get("tag") or "",
                "scope": ref.get("scope") or "",
                "kind": "segment",
                "segment_id": seg.get("id") or "",
                "type": ref.get("type") or "",
            })
    return rows


def build_snapshot(*, manifest=None, brief=None, storyboard_result=None, segments=None, events=None):
    manifest = manifest or {}
    brief = brief or {}
    storyboard_result = storyboard_result or {}
    segments = segments or []
    events = events or []
    pipeline = _infer_runtime_status(manifest, brief, storyboard_result, segments, events)
    technical_steps = _build_steps(
        manifest, brief, storyboard_result, segments, events)
    technical_stage = pipeline.get("current_stage") or "idea"
    steps, registry = _build_milestone_steps(
        manifest, technical_stage, segments)
    brief_assets = _brief_assets(brief)
    board_feedback = _board_feedback(storyboard_result)
    shots = _shot_feedback(storyboard_result)
    loops = _feedback_loop_items(brief_assets, board_feedback, storyboard_result, manifest)
    refs = _reference_rows(storyboard_result, segments)
    graph = asset_graph.build_graph(manifest, storyboard_result, {"segments": segments}) if manifest or storyboard_result or segments else {"nodes": [], "edges": []}
    return {
        "title": manifest.get("project_title") or brief.get("client") or "Workflow Canvas",
        "client": manifest.get("client") or brief.get("client") or "",
        "run_id": manifest.get("run_id") or storyboard_result.get("run_id") or "",
        "current_stage": technical_stage,
        "current_milestone": artifact_registry.milestone_for_stage(technical_stage),
        "next_action": pipeline.get("next_action") or "",
        "customer_message": pipeline.get("customer_message") or "",
        "customer_preview": pipeline.get("customer_preview") or [],
        "cost_line": pipeline.get("cost_line") or "",
        "chain_profile": pipeline.get("chain_profile") or {},
        "chain_stage": pipeline.get("chain_stage") or {},
        "route_variant": pipeline.get("route_variant") or {},
        "steps": steps,
        "technical_steps": technical_steps,
        "artifact_registry": registry,
        "assets": brief_assets,
        "board_feedback": board_feedback,
        "storyboard_shots": shots,
        "feedback_loops": loops,
        "references": refs,
        "graph": graph,
        "events": events,
    }


def _norm_token(value):
    return str(value or "").strip().lower().lstrip("@").replace("-", "_")

def _matches_ref(tag, ref):
    tag = _norm_token(tag)
    ref = _norm_token(ref)
    if not tag or not ref:
        return False
    if tag == ref:
        return True
    return tag in ref or ref in tag

def _same_path(left, right):
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left or not right:
        return False
    try:
        return os.path.abspath(left) == os.path.abspath(right)
    except (OSError, TypeError):
        return left == right

def _collect_refs(*parts):
    refs_out = []
    seen = set()
    for part in parts:
        for ref in part or []:
            token = _norm_token(ref)
            if token and token not in seen:
                refs_out.append(token)
                seen.add(token)
    return refs_out

def _node_title(node):
    return node.get("label") or node.get("title") or node.get("id") or ""

def _node_detail(node):
    return node.get("detail") or node.get("path") or ""

def _is_image_node(node):
    detail = _node_detail(node)
    return _is_image_path(detail)

def _shape_node(node, *, kind, x, y, width, height, tone="", ref_tags=None, note="", image_label="preview"):
    detail = _node_detail(node)
    thumb = _preview_media_html(detail, label=image_label) if (_is_image_path(detail) or _is_video_path(detail)) else ""
    ref_bits = "".join(
        "<span class='tag'>@%s</span>" % html.escape(tag)
        for tag in (ref_tags or [])
    )
    note_html = "<small>%s</small>" % html.escape(note) if note else ""
    image_html = "<div class='node-image'>%s</div>" % thumb if thumb else ""
    return {
        "id": node.get("id") or "",
        "kind": kind,
        "tone": tone or node.get("state") or "unknown",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "html": (
            "<article class='canvas-node %s %s' style='left:%spx;top:%spx;width:%spx;min-height:%spx'>"
            "<div class='node-head'><b>%s</b><span>%s</span></div>"
            "%s%s<div class='node-detail' title='%s'>%s</div>"
            "</article>"
            % (
                html.escape(kind),
                html.escape(tone or node.get("state") or "unknown"),
                x, y, width, height,
                html.escape(_node_title(node)),
                html.escape(node.get("state") or "unknown"),
                image_html,
                ("<div class='node-tags'>%s</div>" % ref_bits) if ref_bits else "",
                html.escape(note or detail or "", quote=True),
                html.escape(note or detail or ""),
            )
        )
    }

def _edge_svg(edge_list, positioned):
    lines = []
    seen_edges = set()
    for left, right, tone in edge_list:
        edge_key = (left, right, tone or "flow")
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        a = positioned.get(left)
        b = positioned.get(right)
        if not a or not b:
            continue
        x1 = a["x"] + a["width"]
        y1 = a["y"] + a["height"] * 0.5
        x2 = b["x"]
        y2 = b["y"] + b["height"] * 0.5
        dx = max(80, min(220, abs(x2 - x1) * 0.35))
        stroke = {
            "current": "#60a5fa",
            "ref": "#7dd3fc",
            "flow": "#4b5563",
            "warning": "#f59e0b",
        }.get(tone or "flow", "#4b5563")
        opacity = "0.82" if tone == "current" else "0.5"
        lines.append(
            "<path d='M %s %s C %s %s, %s %s, %s %s' fill='none' stroke='%s' stroke-width='1.5' stroke-opacity='%s' />"
            % (x1, y1, x1 + dx, y1, x2 - dx, y2, x2, y2, stroke, opacity)
        )
    return "".join(lines)

def _board_refs(node):
    detail = _node_detail(node)
    if not detail:
        return []
    refs_out = []
    if detail.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        refs_out.append(os.path.basename(detail))
    return refs_out


def _workflow_canvas_render_context(snapshot):
    steps = snapshot.get("steps") or []
    assets = snapshot.get("assets") or []
    board_feedback = snapshot.get("board_feedback") or []
    shots = snapshot.get("storyboard_shots") or []
    loops = snapshot.get("feedback_loops") or []
    refs = snapshot.get("references") or []
    events = snapshot.get("events") or []
    graph = snapshot.get("graph") or {"nodes": [], "edges": []}
    current_stage = snapshot.get("current_stage") or ""
    current_step = next((step for step in steps if step.get("is_current")), {})
    graph_nodes = graph.get("nodes") or []
    graph_edges = graph.get("edges") or []
    graph_node_map = {node.get("id"): node for node in graph_nodes if node.get("id")}
    video_nodes = [node for node in graph_nodes if node.get("kind") == "segment" and _is_video_path(node.get("detail"))]
    ocr_events = [event for event in events if event.get("event") == "ocr_warning"]
    current_summary = [
        ("素材", "%d 张" % len(assets)),
        ("分镜", "%d 镜" % len(shots)),
        ("视频", "%d 段" % len(video_nodes)),
        ("OCR", "%d 条提醒" % len(ocr_events)),
    ]
    next_action_text = _action_label(snapshot.get("next_action")) or "等待查看与确认"
    current_detail_text = (
        current_step.get("detail") or snapshot.get("customer_message") or
        "当前流程阶段"
    )
    current_detail_lines = []
    if snapshot.get("customer_message"):
        current_detail_lines.append(snapshot.get("customer_message"))
    current_detail_lines.append(current_detail_text)
    if ocr_events:
        first_ocr = ocr_events[-1]
        texts = ", ".join(str(item) for item in (first_ocr.get("ocr_texts") or [])[:5])
        if texts:
            current_detail_lines.append("最近 OCR: %s" % texts)
    current_detail_html = (
        "<p>%s</p><div class='stage-stats'>%s</div><p class='stage-next'>下一步：%s</p>"
        % (
            html.escape(" / ".join(dict.fromkeys(line for line in current_detail_lines if line))),
            "".join("<span><b>%s</b>%s</span>" % (html.escape(k), html.escape(v)) for k, v in current_summary),
            html.escape(next_action_text),
        )
    )
    return dict(locals())


def _workflow_canvas_nodes(context):
    snapshot = context["snapshot"]
    assets = context["assets"]
    board_feedback = context["board_feedback"]
    shots = context["shots"]
    graph_nodes = context["graph_nodes"]
    current_stage = context["current_stage"]
    current_step = context["current_step"]
    asset_nodes = []
    for index, asset in enumerate(assets):
        node = {
            "id": "asset:%s" % (asset.get("tag") or index),
            "label": asset.get("tag") or "asset",
            "state": asset.get("status") or "unknown",
            "detail": asset.get("path") or "",
        }
        asset_nodes.append(_shape_node(
            node, kind="asset", x=70, y=260 + index * 250, width=320, height=210,
            tone=asset.get("status") or "unknown",
            note=asset.get("variant") or asset.get("source") or asset.get("prompt") or "",
            image_label=asset.get("tag") or "asset"))

    board_nodes = []
    for index, item in enumerate(board_feedback):
        node = {
            "id": "board:%s" % (item.get("kind") or index),
            "label": item.get("kind") or "board",
            "state": item.get("status") or "unknown",
            "detail": item.get("path") or "",
        }
        board_nodes.append(_shape_node(
            node, kind="board", x=560, y=180 + index * 330, width=360, height=260,
            tone=item.get("status") or "unknown",
            ref_tags=_collect_refs(item.get("feedback_refs"), item.get("identity_refs"), item.get("source_refs")),
            note=item.get("edit_prompt") or "",
            image_label=item.get("kind") or "board"))

    if not board_nodes and graph_nodes:
        for index, node in enumerate(node for node in graph_nodes if node.get("kind") == "board"):
            board_nodes.append(_shape_node(
                node, kind="board", x=560, y=180 + index * 330, width=360, height=260,
                tone=node.get("state") or "unknown",
                note=_node_detail(node),
                image_label=_node_title(node) or "board"))

    shot_nodes = []
    shot_meta = {}
    for index, item in enumerate(shots):
        node = {
            "id": "shot:%s" % (item.get("shot_id") or index),
            "label": item.get("shot_id") or "shot",
            "state": item.get("status") or "unknown",
            "detail": item.get("path") or "",
        }
        shot_meta[node["id"]] = item
        shot_nodes.append(_shape_node(
            node, kind="shot", x=1010, y=160 + index * 220, width=320, height=220,
            tone=item.get("status") or "unknown",
            ref_tags=item.get("ref_tags") or [],
            note=item.get("visual") or item.get("dialogue") or "",
            image_label=item.get("shot_id") or "shot"))

    segment_nodes = []
    segment_meta = {}
    for index, node in enumerate(node for node in graph_nodes if node.get("kind") == "segment"):
        segment_meta[node.get("id") or ""] = node
        segment_nodes.append(_shape_node(
            node, kind="segment", x=1490, y=210 + index * 210, width=300, height=190,
            tone=node.get("state") or "unknown",
            note=node.get("detail") or "",
            image_label=node.get("label") or "segment"))

    output_nodes = []
    for index, node in enumerate(node for node in graph_nodes if node.get("kind") == "output"):
        output_nodes.append(_shape_node(
            node, kind="output", x=1900, y=520 + index * 50, width=320, height=210,
            tone=node.get("state") or "unknown",
            note=node.get("detail") or "",
            image_label=node.get("label") or "output"))

    stage_node = _shape_node(
        {
            "id": "stage:%s" % (current_stage or "idea"),
            "label": "Current Stage",
            "state": current_step.get("status") or "current",
            "detail": current_step.get("detail") or snapshot.get("customer_message") or "",
        },
        kind="stage", x=70, y=40, width=520, height=150, tone="current",
        note=snapshot.get("next_action") or "", image_label=current_stage or "stage")
    positioned_nodes = {stage_node["id"]: stage_node}
    for item in asset_nodes + board_nodes + shot_nodes + segment_nodes + output_nodes:
        positioned_nodes[item["id"]] = item
    return {
        "asset_nodes": asset_nodes, "board_nodes": board_nodes,
        "shot_nodes": shot_nodes, "segment_nodes": segment_nodes,
        "output_nodes": output_nodes, "stage_node": stage_node,
        "positioned_nodes": positioned_nodes,
    }


def _workflow_canvas_edges(context, node_context):
    current_stage = context["current_stage"]
    refs = context["refs"]
    assets = context["assets"]
    shots = context["shots"]
    graph_edges = context["graph_edges"]
    positioned_nodes = node_context["positioned_nodes"]
    asset_nodes = node_context["asset_nodes"]
    segment_nodes = node_context["segment_nodes"]

    canvas_edges = [("stage:%s" % (current_stage or "idea"), node["id"], "current") for node in asset_nodes]
    registry_by_tag = {}
    for ref in refs:
        tag = _norm_token(ref.get("tag"))
        if tag and ref.get("kind") == "storyboard":
            registry_by_tag[tag] = ref

    for asset in assets:
        asset_id = "asset:%s" % (asset.get("tag") or "")
        if not asset_id.strip():
            continue
        for registry_ref in registry_by_tag.values():
            if not _same_path(registry_ref.get("source"), asset.get("path")):
                continue
            ref_type = _norm_token(registry_ref.get("type"))
            if ref_type == "product_identity" and "board:product_board" in positioned_nodes:
                canvas_edges.append((asset_id, "board:product_board", "ref"))
            elif ref_type == "product_usage_identity" and "board:product_usage_image" in positioned_nodes:
                canvas_edges.append((asset_id, "board:product_usage_image", "ref"))
            else:
                for shot in shots:
                    shot_id = "shot:%s" % (shot.get("shot_id") or "")
                    shot_tags = {_norm_token(tag) for tag in (shot.get("ref_tags") or [])}
                    if _norm_token(registry_ref.get("tag")) in shot_tags:
                        canvas_edges.append((asset_id, shot_id, "ref"))

    graph_edge_map = []
    for edge in graph_edges:
        left = edge.get("from")
        right = edge.get("to")
        if left and right and left in positioned_nodes and right in positioned_nodes:
            graph_edge_map.append((left, right, "flow"))

    canvas_edges.extend(graph_edge_map)
    canvas_edges.extend([
        (node["id"], "output:assembled", "flow")
        for node in segment_nodes if "output:assembled" in positioned_nodes
    ])
    canvas_svg = _edge_svg(canvas_edges, positioned_nodes)
    return canvas_svg


def _workflow_canvas_cards(context):
    steps = context["steps"]
    assets = context["assets"]
    board_feedback = context["board_feedback"]
    shots = context["shots"]
    loops = context["loops"]
    refs = context["refs"]
    events = context["events"]
    def _join_nodes(nodes):
        return "".join(node["html"] for node in nodes) or "<div class='canvas-empty'>No artifact found</div>"

    def card_status(value):
        return _status_bucket(value)

    def stage_card(step):
        return (
            "<article class='step %s %s'><b>%s</b><span>%s</span><p>%s</p></article>" % (
                html.escape(step.get("status") or "unknown"),
                "current" if step.get("is_current") else "",
                html.escape(step.get("title") or ""),
                html.escape(step.get("id") or ""),
                html.escape(step.get("detail") or ""),
            )
        )

    step_cards = [stage_card(step) for step in steps]

    def media_card(kind, title, path, status, body="", refs_text=None):
        thumb = _preview_media_html(path, label=title)
        ref_html = ""
        if refs_text:
            ref_html = "<div class='refs-inline'>%s</div>" % html.escape(refs_text)
        return (
            "<article class='media-card %s'><div class='media-top'><div>"
            "<b>%s</b><span>%s</span></div><em>%s</em></div>%s<p>%s</p>%s</article>"
            % (
                html.escape(status or "unknown"),
                html.escape(title or kind),
                html.escape(kind),
                html.escape(status or "unknown"),
                thumb,
                html.escape(body or path or ""),
                ref_html,
            )
        )

    asset_cards = []
    for asset in assets:
        refs_text = ", ".join(
            [item for item in ([asset.get("edited_from")] if asset.get("edited_from") else [])
             + list(asset.get("feedback_refs") or []) if item]
        )
        asset_cards.append(media_card(
            "asset", asset.get("tag") or "asset", asset.get("path"), asset.get("status"),
            body=asset.get("variant") or asset.get("source") or asset.get("prompt") or "",
            refs_text=refs_text))

    board_cards = []
    for item in board_feedback:
        refs_text = ", ".join(
            [p for p in (item.get("feedback_refs") or []) + (item.get("identity_refs") or []) + (item.get("source_refs") or []) if p]
        )
        board_cards.append(media_card(
            "board", item.get("kind") or "board", item.get("path"), item.get("status"),
            body=item.get("edit_prompt") or "", refs_text=refs_text))

    shot_cards = []
    for item in shots:
        refs_text = ", ".join(item.get("ref_tags") or [])
        shot_cards.append(media_card(
            "shot", item.get("shot_id") or "shot", item.get("path"), item.get("status"),
            body=(item.get("visual") or item.get("dialogue") or ""), refs_text=refs_text))

    loop_cards = []
    for item in loops:
        loop_cards.append(
            "<article class='loop %s'><b>%s</b><span>%s</span><p>%s</p>%s</article>" % (
                html.escape(card_status(item.get("status"))),
                html.escape(item.get("title") or item.get("kind") or "feedback"),
                html.escape(item.get("reason") or ""),
                html.escape(item.get("note") or ""),
                ("<div class='refs-inline'>%s</div>" % html.escape(", ".join(str(ref) for ref in item.get("refs") or [])))
                if item.get("refs") else "",
            )
        )

    ref_rows = []
    for ref in refs:
        ref_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(ref.get("kind") or ""),
                html.escape(ref.get("tag") or ""),
                html.escape(ref.get("segment_id") or ref.get("scope") or ""),
                html.escape(ref.get("source") or ""),
                _preview_img_html(ref.get("source") or "", label=ref.get("tag") or "ref"),
            )
        )
    event_rows = []
    for event in events:
        event_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(str(event.get("ts") or event.get("timestamp") or "")),
                html.escape(str(event.get("event") or "")),
                html.escape(json.dumps(event, ensure_ascii=False, default=str)),
            )
        )
    return {
        "step_cards": step_cards, "asset_cards": asset_cards,
        "board_cards": board_cards, "shot_cards": shot_cards,
        "loop_cards": loop_cards, "ref_rows": ref_rows,
        "event_rows": event_rows,
    }


WORKFLOW_CANVAS_HTML_TEMPLATE = """<!doctype html>
    <html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title}</title>
    <style>
    body{{margin:0;font-family:system-ui,-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif;background:#090d16;color:#e9eef5}}
    header{{padding:20px 24px;border-bottom:1px solid #1f2a3a;background:linear-gradient(180deg,#0f172a 0%,#0b1220 100%);position:sticky;top:0;z-index:10;box-shadow:0 8px 26px rgba(0,0,0,.25)}}
    header h1{{margin:0 0 8px;font-size:20px}}
    header p{{margin:0;color:#9ca3af;line-height:1.5}}
    main{{padding:20px;display:grid;gap:18px}}
    .band{{background:#111827;border:1px solid #223149;border-radius:16px;padding:16px;box-shadow:0 20px 40px rgba(0,0,0,.16)}}
    .band h2{{margin:0 0 14px;font-size:14px;color:#cbd5e1;text-transform:uppercase;letter-spacing:.04em}}
    .steps{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}
    .step,.asset,.loop,.media-card,.canvas-node{{background:#0b1324;border:1px solid #243041;border-radius:16px;padding:12px;min-width:0;position:relative;overflow:hidden}}
    .step::before,.asset::before,.loop::before,.media-card::before,.canvas-node::before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:rgba(148,163,184,.35)}}
    .step b,.asset b,.loop b,.media-card b,.canvas-node b{{display:block;font-size:15px;margin-bottom:4px;overflow-wrap:anywhere}}
    .step span,.asset span,.loop span,.media-card span,.canvas-node span{{display:block;color:#94a3b8;font-size:12px;margin-bottom:6px;overflow-wrap:anywhere}}
    .step p,.asset p,.loop p,.media-card p,.canvas-node .node-detail{{margin:0;color:#d1d5db;line-height:1.5;overflow-wrap:anywhere}}
    .current{{box-shadow:0 0 0 2px #60a5fa inset, 0 0 0 1px rgba(96,165,250,.25);transform:translateY(-1px)}}
    .confirmed,.approved{{border-color:#10b981}}
    .trusted_upload,.completed,.succeeded,.submitted,.running{{border-color:#3b82f6}}
    .pending,.pending_approval,.pending_confirmation{{border-color:#f59e0b}}
    .stale,.failed,.invalidated{{border-color:#ef4444}}
    .active-stage{{background:linear-gradient(135deg,#13213a 0%,#0b1220 100%);border:1px solid #3b82f6;box-shadow:0 0 0 1px rgba(59,130,246,.35), 0 20px 50px rgba(59,130,246,.18);padding:18px;border-radius:18px;position:relative;overflow:hidden}}
    .active-stage::after{{content:"";position:absolute;inset:-1px;background:radial-gradient(circle at 12% 20%,rgba(96,165,250,.26),transparent 38%),radial-gradient(circle at 86% 18%,rgba(34,197,94,.14),transparent 32%);pointer-events:none}}
    .active-stage .label{{color:#93c5fd;font-size:12px;text-transform:uppercase;letter-spacing:.06em}}
    .active-stage h3{{margin:6px 0 8px;font-size:24px}}
    .active-stage p{{margin:0;color:#dbeafe;line-height:1.6}}
    .layout{{display:grid;grid-template-columns:1.7fr .8fr;gap:18px;align-items:start}}
    .canvas-shell{{overflow:auto;border-radius:18px;border:1px solid #243041;background:#060911;padding:14px}}
    .canvas-stage{{position:relative;min-width:2360px;min-height:1500px;background:
        radial-gradient(circle at 10% 12%, rgba(59,130,246,.08), transparent 26%),
        radial-gradient(circle at 85% 18%, rgba(16,185,129,.07), transparent 24%),
        linear-gradient(transparent 0, transparent 63px, rgba(148,163,184,.08) 64px),
        linear-gradient(90deg, transparent 0, transparent 63px, rgba(148,163,184,.06) 64px),
        #060911;
      background-size:auto,auto,64px 64px,64px 64px,auto;overflow:hidden;border-radius:14px}}
    .canvas-stage .stage-card{{position:absolute;z-index:3}}
    .canvas-node{{position:absolute;z-index:2;box-shadow:0 16px 30px rgba(0,0,0,.22)}}
    .canvas-node.current{{border-color:#60a5fa;box-shadow:0 0 0 2px rgba(96,165,250,.35),0 22px 34px rgba(96,165,250,.18)}}
    .canvas-node .node-head{{display:flex;justify-content:space-between;gap:10px;align-items:baseline}}
    .canvas-node .node-head span{{color:#93c5fd;font-size:12px;white-space:nowrap}}
    .canvas-node .node-image{{margin-top:10px}}
    .canvas-node .node-image .thumb{{margin:0}}
    .canvas-node .node-image img{{display:block;width:100%;max-height:118px;object-fit:cover;border-radius:10px;border:1px solid #31415d;background:#09111d}}
    .canvas-node .node-image figcaption{{display:none}}
    .canvas-node .node-image video{{display:block;width:100%;height:118px;object-fit:cover;border-radius:10px;border:1px solid #31415d;background:#000}}
    .canvas-node .node-tags{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
    .tag{{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;background:#122033;border:1px solid #24395a;color:#9cc0ff;font-size:11px;white-space:nowrap}}
    .stage-node{{border-color:#60a5fa;background:linear-gradient(135deg,#13213a 0%,#0d1526 100%)}}
    .stage-node .node-detail{{color:#dbeafe;font-size:14px}}
    .stage-node p{{margin:0 0 10px;line-height:1.45;color:#dbeafe}}
    .stage-stats{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0}}
    .stage-stats span{{display:block;border:1px solid #2a4266;background:#0b1728;border-radius:12px;padding:8px;color:#dbeafe;font-size:12px}}
    .stage-stats b{{display:block;font-size:11px;color:#93c5fd;margin-bottom:2px}}
    .stage-next{{color:#bfdbfe;font-size:13px}}
    .canvas-empty{{color:#8ea2bf;font-size:12px;padding:12px}}
    .table{{overflow:auto}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:10px 8px;border-bottom:1px solid #243041;text-align:left;vertical-align:top}}
    th{{color:#94a3b8;font-weight:600}}
    .thumb{{margin:10px 0 0}}
    .thumb img{{display:block;width:100%;max-height:180px;object-fit:cover;border-radius:10px;border:1px solid #31415d;background:#0b1020}}
    .thumb figcaption{{margin-top:6px;color:#8ea2bf;font-size:11px;overflow-wrap:anywhere}}
    .video-preview{{margin:10px 0 0}}
    .video-preview video{{display:block;width:100%;height:180px;object-fit:cover;border-radius:10px;border:1px solid #31415d;background:#000}}
    .video-preview figcaption{{margin-top:6px;color:#8ea2bf;font-size:11px;overflow-wrap:anywhere}}
    .media-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}}
    .media-card .media-top{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:6px}}
    .media-card em{{font-style:normal;color:#60a5fa;font-size:12px;white-space:nowrap}}
    .refs-inline{{margin-top:8px;padding:8px 10px;border-radius:10px;background:#0b1324;border:1px solid #223149;color:#9fb6d5;font-size:12px;overflow-wrap:anywhere}}
    .meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
    .meta div{{background:#0f172a;border:1px solid #243041;border-radius:12px;padding:12px}}
    .meta strong{{display:block;color:#94a3b8;font-size:12px;margin-bottom:4px}}
    small{{display:block;color:#94a3b8;line-height:1.4}}
    .panel-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
    .canvas-node .node-detail{{font-size:12px;line-height:1.45;max-height:54px;overflow:auto;color:#cbd5e1}}
    .canvas-node.stage-node .node-detail{{max-height:none;overflow:visible}}
    .canvas-legend{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
    .legend-chip{{padding:6px 10px;border-radius:999px;background:#10192a;border:1px solid #243041;color:#dbeafe;font-size:12px}}
    @media(max-width:1120px){{.layout{{grid-template-columns:1fr}} .panel-grid{{grid-template-columns:1fr}}}}
    @media(max-width:640px){{.media-grid{{grid-template-columns:1fr}} .steps{{grid-template-columns:1fr}}}}
    </style></head><body>
    <header>
      <h1>{title}</h1>
      <p>{message}</p>
      <p>{next_action}</p>
    </header>
    <main>
      <section class="band active-stage">
        <div class="label">Current Stage</div>
        <h3>{current_stage}</h3>
        <p>{current_detail}</p>
      </section>
      <section class="band layout">
        <div>
          <div class="canvas-legend">
            <span class="legend-chip">流程从左到右</span>
            <span class="legend-chip">当前步骤高亮</span>
            <span class="legend-chip">图片直接缩略图预览</span>
            <span class="legend-chip">引用线显示素材关系</span>
            <span class="legend-chip">链路类型可见</span>
          </div>
          <div class="canvas-shell">
            <div class="canvas-stage">
              <svg class="edge-layer" width="2360" height="1500" viewBox="0 0 2360 1500" style="position:absolute;inset:0;pointer-events:none;z-index:1">
                {canvas_svg}
              </svg>
              <section class="canvas-node stage-node current stage-card" style="left:70px;top:40px;width:520px;min-height:150px">
                <div class="node-head"><b>Current Stage</b><span>{current_stage}</span></div>
                <div class="node-detail">{current_detail_html}</div>
              </section>
              {canvas_nodes}
            </div>
          </div>
        </div>
        <div>
          <h2>Current Metadata</h2>
          <div class="meta">
            <div><strong>Client</strong>{client}</div>
            <div><strong>Run ID</strong>{run_id}</div>
            <div><strong>Current Step</strong>{current_stage}</div>
            <div><strong>Cost</strong>{cost_line}</div>
            <div><strong>Chain Profile</strong>{chain_profile}</div>
            <div><strong>Route Variant</strong>{route_variant}</div>
            <div><strong>Chain Stage</strong>{chain_stage}</div>
          </div>
          <h2 style="margin-top:16px;">Workflow Steps</h2>
          <div class="steps">{steps}</div>
          <h2 style="margin-top:16px;">Revision Loop</h2>
          <div class="loops">{loops}</div>
          <h2 style="margin-top:16px;">Reference Map</h2>
          <div class="table">
            <table><thead><tr><th>Kind</th><th>Tag</th><th>Scope / Segment</th><th>Source</th></tr></thead>
            <tbody>{refs}</tbody></table>
          </div>
        </div>
      </section>
      <section class="band panel-grid">
        <div>
          <h2>Material Inspector</h2>
          <div class="media-grid">{assets}</div>
        </div>
        <div>
          <h2>Storyboard / Boards</h2>
          <div class="media-grid">{boards}</div>
          <h2 style="margin-top:16px;">Storyboard Shots</h2>
          <div class="media-grid">{shots}</div>
        </div>
      </section>
      <section class="band">
        <h2>Dependency Graph</h2>
        <div class="table">
          <table><thead><tr><th>Kind</th><th>Label</th><th>State</th><th>Detail</th></tr></thead>
          <tbody>{graph_rows}</tbody></table>
        </div>
      </section>
      <section class="band">
        <h2>Event Log</h2>
        <div class="table">
          <table><thead><tr><th>Time</th><th>Event</th><th>Payload</th></tr></thead><tbody>{events}</tbody></table>
        </div>
      </section>
    </main></body></html>"""


def _workflow_canvas_document(snapshot, context, node_context, card_context,
                              canvas_svg):
    current_stage = context["current_stage"]
    current_step = context["current_step"]
    current_detail_html = context["current_detail_html"]
    graph_nodes = context["graph_nodes"]
    step_cards = card_context["step_cards"]
    loop_cards = card_context["loop_cards"]
    asset_cards = card_context["asset_cards"]
    board_cards = card_context["board_cards"]
    shot_cards = card_context["shot_cards"]
    ref_rows = card_context["ref_rows"]
    event_rows = card_context["event_rows"]
    asset_nodes = node_context["asset_nodes"]
    board_nodes = node_context["board_nodes"]
    shot_nodes = node_context["shot_nodes"]
    segment_nodes = node_context["segment_nodes"]
    output_nodes = node_context["output_nodes"]
    return WORKFLOW_CANVAS_HTML_TEMPLATE.format(
            title=html.escape(snapshot.get("title") or "Workflow Canvas"),
            message=html.escape(snapshot.get("customer_message") or "Shows the current step and where feedback or revisions are waiting."),
            next_action=html.escape(_action_label(snapshot.get("next_action")) or ""),
            current_stage=html.escape(current_stage or "idea"),
            current_detail=html.escape((snapshot.get("customer_message") or current_step.get("detail") or "Current workflow stage") if current_step else (snapshot.get("customer_message") or "Current workflow stage")),
            current_detail_html=current_detail_html,
            steps="".join(step_cards) or "<div class='step unknown'><b>No steps</b><span></span><p></p></div>",
            loops="".join(loop_cards) or "<div class='loop unknown'><b>No revision items</b><span></span><p></p></div>",
            assets="".join(asset_cards) or "<div class='asset unknown'><b>No assets</b><span></span><p></p></div>",
            boards="".join(board_cards) or "<div class='media-card unknown'><b>No boards</b><span></span><p></p></div>",
            shots="".join(shot_cards) or "<div class='media-card unknown'><b>No shots</b><span></span><p></p></div>",
            refs="".join(ref_rows) or "<tr><td colspan='5'>No references</td></tr>",
            client=html.escape(snapshot.get("client") or ""),
            run_id=html.escape(snapshot.get("run_id") or ""),
            cost_line=html.escape(snapshot.get("cost_line") or ""),
            chain_profile=html.escape(json.dumps(snapshot.get("chain_profile") or {}, ensure_ascii=False)),
            route_variant=html.escape(json.dumps(snapshot.get("route_variant") or {}, ensure_ascii=False)),
            chain_stage=html.escape(json.dumps(snapshot.get("chain_stage") or {}, ensure_ascii=False)),
            canvas_svg=canvas_svg,
            canvas_nodes="".join(node["html"] for node in (asset_nodes + board_nodes + shot_nodes + segment_nodes + output_nodes)),
            graph_rows="".join(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    html.escape(node.get("kind") or ""),
                    html.escape(node.get("label") or ""),
                    html.escape(node.get("state") or ""),
                    html.escape(node.get("detail") or ""),
                )
                for node in graph_nodes
            ) or "<tr><td colspan='4'>No dependency graph</td></tr>",
            events="".join(event_rows) or "<tr><td colspan='3'>No events</td></tr>",
        )


def render_html(snapshot):
    context = _workflow_canvas_render_context(snapshot)
    node_context = _workflow_canvas_nodes(context)
    canvas_svg = _workflow_canvas_edges(context, node_context)
    card_context = _workflow_canvas_cards(context)
    return _workflow_canvas_document(
        snapshot, context, node_context, card_context, canvas_svg)



def _html_json(value):
    return json.dumps(value, ensure_ascii=False, default=str).replace("</", "<\\/")


def render_live_shell(snapshot):
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif;background:#0b1020;color:#e5eefc}}
header{{padding:16px 20px;border-bottom:1px solid #233046;background:#0f172a;display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}}
header h1{{margin:0 0 6px;font-size:20px}}
header p{{margin:0;color:#93a4be;line-height:1.5}}
.grid{{display:grid;grid-template-columns:1.6fr .9fr;gap:14px;padding:14px;min-height:calc(100vh - 72px)}}
.panel{{background:#121a2b;border:1px solid #223149;border-radius:12px;overflow:hidden}}
.panel h2{{margin:0;padding:12px 14px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;border-bottom:1px solid #223149;color:#c8d5ea}}
.panel .body{{padding:14px}}
.frame{{width:100%;height:78vh;border:0;background:#fff}}
.chips{{display:flex;flex-wrap:wrap;gap:8px}}
.chip{{padding:6px 10px;border-radius:999px;background:#16233a;border:1px solid #29415e;font-size:12px;color:#d9e7ff}}
.history{{display:grid;gap:10px;max-height:30vh;overflow:auto}}
.history-item{{border:1px solid #24354f;background:#0f172a;border-radius:10px;padding:10px}}
.history-item b{{display:block;margin-bottom:4px}}
.history-item small{{display:block;color:#8ea2bf;line-height:1.4}}
.comments textarea{{width:100%;min-height:96px;resize:vertical;border-radius:10px;border:1px solid #2b405f;background:#0b1324;color:#e7efff;padding:10px;box-sizing:border-box}}
.comments select,.comments button{{margin-top:8px;border-radius:8px;border:1px solid #2b405f;background:#17233a;color:#e7efff;padding:8px 10px}}
.comments button{{cursor:pointer}}
.meta{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}
.meta div{{background:#0b1324;border:1px solid #24354f;border-radius:10px;padding:10px}}
.meta strong{{display:block;color:#8ea2bf;font-size:12px;margin-bottom:4px}}
.status{{color:#8ab4ff}}
@media(max-width:1100px){{.grid{{grid-template-columns:1fr}} .frame{{height:66vh}}}}
</style></head><body>
<header>
  <div>
    <h1>{title}</h1>
    <p id="message">{message}</p>
    <p class="status" id="nextAction">{next_action}</p>
  </div>
    <div class="chips">
      <div class="chip" id="currentStage">{current_stage}</div>
      <div class="chip" id="eventCount">{event_count}</div>
      <div class="chip" id="historyCount">{history_count}</div>
      <div class="chip" id="chainProfile">{chain_profile}</div>
      <div class="chip" id="routeVariant">{route_variant}</div>
    </div>
</header>
<div class="grid">
  <section class="panel">
    <h2>Live Preview</h2>
    <iframe id="previewFrame" class="frame" src="/preview" title="workflow preview"></iframe>
  </section>
  <aside style="display:grid;gap:14px;align-content:start">
    <section class="panel">
      <h2>Current State</h2>
      <div class="body meta">
        <div><strong>Client</strong><span id="client">{client}</span></div>
        <div><strong>Run ID</strong><span id="runId">{run_id}</span></div>
        <div><strong>Current Step</strong><span id="currentStep">{current_stage}</span></div>
        <div><strong>Next Action</strong><span id="nextActionShort">{next_action}</span></div>
        <div><strong>Chain Profile</strong><span id="chainProfileText">{chain_profile}</span></div>
        <div><strong>Route Variant</strong><span id="routeVariantText">{route_variant}</span></div>
      </div>
    </section>
    <section class="panel comments">
      <h2>Interaction</h2>
      <div class="body">
        <form id="commentForm">
          <textarea id="commentText" placeholder="用户建议、素材修改意见、补充说明"></textarea>
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <select id="commentKind">
              <option value="suggestion">Suggestion</option>
              <option value="asset_fix">Asset Fix</option>
              <option value="storyboard_fix">Storyboard Fix</option>
              <option value="approval">Approval</option>
              <option value="question">Question</option>
            </select>
            <button type="submit">记录并刷新</button>
          </div>
        </form>
      </div>
    </section>
    <section class="panel">
      <h2>History</h2>
      <div class="body history" id="historyList"></div>
    </section>
  </aside>
</div>
<script>
window.__INITIAL_SNAPSHOT__ = {snapshot_json};
let currentFingerprint = window.__INITIAL_SNAPSHOT__._fingerprint || "";
const refreshMs = 2000;

function esc(value) {{
  return String(value ?? "").replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[ch]));
}}

function renderHistory(items) {{
  const root = document.getElementById("historyList");
  root.innerHTML = (items || []).slice().reverse().map(item => `
    <div class="history-item">
      <b>${{esc(item.ts || "")}} · ${{esc(item.source || "poll")}}</b>
      <small>${{esc(item.reason || "")}}</small>
      <small>stage: ${{esc(item.current_stage || "")}} · steps: ${{(item.steps || []).length}} · assets: ${{(item.assets || []).length}} · refs: ${{(item.references || []).length}}</small>
      <small>${{esc(JSON.stringify(item.delta || {{}}))}}</small>
    </div>`).join("") || "<div class='history-item'><b>No history yet</b></div>";
}}

function updateMeta(snapshot) {{
  document.getElementById("message").textContent = snapshot.customer_message || "";
  document.getElementById("nextAction").textContent = snapshot.next_action || "";
  document.getElementById("nextActionShort").textContent = snapshot.next_action || "";
  document.getElementById("currentStage").textContent = snapshot.current_stage || "";
  document.getElementById("currentStep").textContent = snapshot.current_stage || "";
  document.getElementById("eventCount").textContent = "events: " + ((snapshot.events || []).length);
  document.getElementById("historyCount").textContent = "history: " + ((snapshot.history || []).length);
  document.getElementById("chainProfile").textContent = JSON.stringify(snapshot.chain_profile || {{}});
  document.getElementById("routeVariant").textContent = JSON.stringify(snapshot.route_variant || {{}});
  document.getElementById("chainProfileText").textContent = JSON.stringify(snapshot.chain_profile || {{}});
  document.getElementById("routeVariantText").textContent = JSON.stringify(snapshot.route_variant || {{}});
  document.getElementById("client").textContent = snapshot.client || "";
  document.getElementById("runId").textContent = snapshot.run_id || "";
  renderHistory(snapshot.history || []);
}}

async function fetchJson(url, options) {{
  const res = await fetch(url, Object.assign({{cache: "no-store"}}, options || {{}}));
  if (!res.ok) {{
    throw new Error(await res.text());
  }}
  return await res.json();
}}

async function refreshSnapshot() {{
  try {{
    const snapshot = await fetchJson("/api/snapshot?ts=" + Date.now());
    updateMeta(snapshot);
    if ((snapshot._fingerprint || "") !== currentFingerprint) {{
      currentFingerprint = snapshot._fingerprint || "";
      document.getElementById("previewFrame").src = "/preview?ts=" + Date.now();
    }}
  }} catch (error) {{
    console.error(error);
  }}
}}

document.getElementById("commentForm").addEventListener("submit", async function(event) {{
  event.preventDefault();
  const text = document.getElementById("commentText").value.trim();
  if (!text) return;
  const kind = document.getElementById("commentKind").value;
  await fetchJson("/api/comment", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{kind, text}})
  }});
  document.getElementById("commentText").value = "";
  await refreshSnapshot();
}});

setInterval(refreshSnapshot, refreshMs);
refreshSnapshot();
</script>
</body></html>""".format(
        title=html.escape(snapshot.get("title") or "Workflow Canvas"),
        message=html.escape(snapshot.get("customer_message") or ""),
        next_action=html.escape(_action_label(snapshot.get("next_action")) or ""),
        current_stage=html.escape(snapshot.get("current_stage") or ""),
        event_count=html.escape("events: %d" % len(snapshot.get("events") or [])),
        history_count=html.escape("history: %d" % len(snapshot.get("history") or [])),
        client=html.escape(snapshot.get("client") or ""),
        run_id=html.escape(snapshot.get("run_id") or ""),
        chain_profile=html.escape(json.dumps(snapshot.get("chain_profile") or {}, ensure_ascii=False)),
        route_variant=html.escape(json.dumps(snapshot.get("route_variant") or {}, ensure_ascii=False)),
        chain_stage=html.escape(json.dumps(snapshot.get("chain_stage") or {}, ensure_ascii=False)),
        snapshot_json=_html_json(snapshot),
    )


class CanvasRuntime:
    def __init__(self, *, out_path, manifest_path=None, brief_path=None,
                 storyboard_result_path=None, caption_manifest_path=None,
                 delivery_path=None, final_path=None, segments_path=None, events_path=None,
                 history_limit=25):
        self.out_path = os.path.abspath(out_path)
        self.out_dir = os.path.dirname(self.out_path) or "."
        self.manifest_path = manifest_path
        self.brief_path = brief_path
        self.storyboard_result_path = storyboard_result_path
        self.caption_manifest_path = caption_manifest_path
        self.delivery_path = delivery_path
        self.final_path = final_path
        self.segments_path = segments_path
        self.events_path = events_path
        self.history_limit = history_limit
        self._lock = threading.Lock()
        self._last_snapshot = None

    def snapshot(self, *, source="poll", reason=None):
        with self._lock:
            snapshot = generate_canvas(
                out_path=self.out_path, manifest_path=self.manifest_path,
                brief_path=self.brief_path, storyboard_result_path=self.storyboard_result_path,
                caption_manifest_path=self.caption_manifest_path,
                delivery_path=self.delivery_path, final_path=self.final_path,
                segments_path=self.segments_path, events_path=self.events_path,
                history_limit=self.history_limit)
            snapshot["_fingerprint"] = _snapshot_fingerprint(snapshot)
            self._last_snapshot = snapshot
            return snapshot

    def record_comment(self, kind, text, *, user_agent="", remote_addr=""):
        payload = {
            "text": text,
            "user_agent": user_agent,
            "remote_addr": remote_addr,
        }
        entry = _record_interaction(self.out_dir, kind, payload)
        snapshot = self.snapshot(source="interaction", reason=text)
        _record_history(
            self.out_dir, snapshot, source="interaction", reason=text,
            extra={"interaction": entry}, force=True)
        return entry

    def history(self, limit=None):
        return _load_history(self.out_dir, limit=limit or self.history_limit)


class _CanvasHandler(BaseHTTPRequestHandler):
    runtime = None

    def _send(self, body, *, content_type="text/plain; charset=utf-8", status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, value, status=200):
        self._send(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                   content_type="application/json; charset=utf-8", status=status)

    def _parse_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype == "application/json":
            return json.loads(raw or "{{}}")
        if ctype == "application/x-www-form-urlencoded":
            parsed = parse_qs(raw, keep_blank_values=True)
            return {key: values[-1] if values else "" for key, values in parsed.items()}
        return {"raw": raw}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            snapshot = self.runtime.snapshot(source="poll", reason="page load")
            self._send(render_live_shell(snapshot), content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/preview":
            snapshot = self.runtime.snapshot(source="poll", reason="preview refresh")
            self._send(render_html(snapshot), content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/api/snapshot":
            snapshot = self.runtime.snapshot(source="poll", reason="api snapshot")
            self._send_json(snapshot)
            return
        if parsed.path == "/api/history":
            self._send_json({"items": self.runtime.history()})
            return
        if parsed.path == "/api/events":
            self._send_json({"items": self.runtime.history()})
            return
        self._send("Not found", status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/comment":
            self._send("Not found", status=404)
            return
        payload = self._parse_body()
        text = str(payload.get("text") or "").strip()
        kind = str(payload.get("kind") or "comment").strip() or "comment"
        if not text:
            self._send_json({"ok": False, "error": "EMPTY_COMMENT"}, status=400)
            return
        entry = self.runtime.record_comment(
            kind, text, user_agent=self.headers.get("User-Agent", ""),
            remote_addr=self.client_address[0] if self.client_address else "")
        self._send_json({"ok": True, "entry": entry})

    def log_message(self, fmt, *args):
        if obs_log is not None:
            try:
                obs_log.log_event("workflow_canvas_http", message=fmt % args)
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
        super().log_message(fmt, *args)


def generate_canvas(*, out_path, manifest_path=None, brief_path=None,
                    storyboard_result_path=None, caption_manifest_path=None,
                    delivery_path=None, final_path=None, segments_path=None, events_path=None,
                    history_limit=25):
    if not out_path:
        raise ValueError("WORKFLOW_CANVAS_OUT_REQUIRED")
    manifest = _load_json(manifest_path, {}) or {}
    brief = _load_json(brief_path, {}) or {}
    storyboard_result = _load_json(storyboard_result_path, {}) or {}
    caption_manifest = _load_json(caption_manifest_path, {}) or {}
    delivery = _load_json(delivery_path, {}) or {}
    if final_path and not os.path.isfile(final_path):
        final_path = ""
    segments_doc = _load_json(segments_path, {}) or {}
    segments = segments_doc.get("segments") if isinstance(segments_doc, dict) else segments_doc
    events = _load_events(events_path)
    snapshot = build_snapshot(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, events=events)
    if caption_manifest:
        snapshot.setdefault("caption_artifact", caption_manifest)
    if delivery:
        snapshot.setdefault("delivery", delivery)
        delivery_status = "stale"
        if manifest and delivery_path and _delivery_is_current(manifest, delivery_path):
            delivery_status = "completed"
        _replace_step_status(snapshot, "delivery", delivery_status)
    if final_path:
        snapshot.setdefault("final_path", os.path.abspath(final_path))
    snapshot["_fingerprint"] = _snapshot_fingerprint(snapshot)
    out_path = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_path) or "."
    _record_history(out_dir, snapshot, source="render", reason="canvas refresh")
    history = _load_history(out_dir, limit=history_limit)
    final_snapshot = dict(snapshot, history=history)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    atomic_write_bytes(
        out_path, render_html(final_snapshot).encode("utf-8"), mode=0o600,
        label="canvas_html")
    json_path = os.path.splitext(out_path)[0] + ".json"
    atomic_write_json(json_path, final_snapshot)
    return final_snapshot


def refresh_run_canvas(*, root=None, client=None, run_id=None,
                       storyboard_result_path=None, existing_only=True,
                       reason=None):
    """Refresh the discovered canvas after an execution-stage checkpoint.

    The canvas used to advance only when a separate operator reran its CLI,
    which let storyboard generation reach confirmation while the JSON snapshot
    still claimed ``idea``.  Storyboard/video owners can call this lightweight
    adapter after an atomic checkpoint.  ``existing_only`` prevents library
    calls and unit tests from creating surprise UI artifacts for runs that did
    not opt into a canvas.
    """
    args = _resolve_canvas_args(
        root=root, client=client, run_id=run_id,
        storyboard_result_path=storyboard_result_path)
    out_path = args.get("out_path")
    if not out_path:
        return None
    json_path = os.path.splitext(out_path)[0] + ".json"
    if existing_only and not (
            os.path.isfile(out_path) or os.path.isfile(json_path) or
            args.get("manifest_path")):
        return None
    snapshot = generate_canvas(
        out_path=out_path,
        manifest_path=args.get("manifest_path"),
        brief_path=args.get("brief_path"),
        storyboard_result_path=(args.get("storyboard_result_path") or
                                storyboard_result_path),
        caption_manifest_path=args.get("caption_manifest_path"),
        delivery_path=args.get("delivery_path"),
        final_path=args.get("final_path"),
        segments_path=args.get("segments_path"),
        events_path=args.get("events_path"))
    if reason:
        snapshot["refresh_reason"] = str(reason)
    return snapshot


def serve_canvas(*, out_path, manifest_path=None, brief_path=None,
                 storyboard_result_path=None, caption_manifest_path=None,
                 delivery_path=None, final_path=None, segments_path=None, events_path=None,
                 host="127.0.0.1", port=8765, history_limit=25):
    if not out_path:
        raise ValueError("WORKFLOW_CANVAS_OUT_REQUIRED")
    runtime = CanvasRuntime(
        out_path=out_path, manifest_path=manifest_path, brief_path=brief_path,
        storyboard_result_path=storyboard_result_path, caption_manifest_path=caption_manifest_path,
        delivery_path=delivery_path, final_path=final_path, segments_path=segments_path,
        events_path=events_path, history_limit=history_limit)
    if obs_log is not None:
        try:
            bootstrap = runtime.snapshot(source="serve", reason="bootstrap")
            obs_log.configure(
                client=(bootstrap.get("client") or None),
                run_id=(bootstrap.get("run_id") or None),
                run_dir=runtime.out_dir)
            obs_log.tee_stdout(True)
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    _CanvasHandler.runtime = runtime
    server = ThreadingHTTPServer((host, int(port)), _CanvasHandler)
    print(json.dumps({
        "ok": True,
        "mode": "serve",
        "url": "http://%s:%s/" % (host, int(port)),
        "preview": "http://%s:%s/preview" % (host, int(port)),
        "snapshot": "http://%s:%s/api/snapshot" % (host, int(port)),
        "history": "http://%s:%s/api/history" % (host, int(port)),
        "interactions": "http://%s:%s/api/comment" % (host, int(port)),
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a workflow canvas from current run artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate a static workflow canvas snapshot")
    gen.add_argument("--out", help="Output HTML path")
    gen.add_argument("--manifest", help="run manifest JSON")
    gen.add_argument("--brief", help="assets/<client>/brief.json")
    gen.add_argument("--storyboard-result", help="storyboard_result.json")
    gen.add_argument("--segments", help="segments.json")
    gen.add_argument("--events", help="run.log JSONL (optional; if omitted the canvas shows none)")
    gen.add_argument("--client", help="client slug override for discovery")
    gen.add_argument("--run-id", help="run id override for discovery")

    srv = sub.add_parser("serve", help="Serve a live canvas with polling and comments")
    srv.add_argument("--out", help="Output HTML path")
    srv.add_argument("--manifest", help="run manifest JSON")
    srv.add_argument("--brief", help="assets/<client>/brief.json")
    srv.add_argument("--storyboard-result", help="storyboard_result.json")
    srv.add_argument("--segments", help="segments.json")
    srv.add_argument("--events", help="run.log JSONL")
    srv.add_argument("--client", help="client slug override for discovery")
    srv.add_argument("--run-id", help="run id override for discovery")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--history-limit", type=int, default=25)

    refresh = sub.add_parser(
        "refresh", help="Refresh an existing run canvas without creating a new one")
    refresh.add_argument("--storyboard-result", required=True)
    refresh.add_argument("--client")
    refresh.add_argument("--run-id")
    refresh.add_argument("--reason")

    args = parser.parse_args(argv)
    resolved = _resolve_canvas_args(
        client=args.client if hasattr(args, "client") else None,
        run_id=args.run_id if hasattr(args, "run_id") else None,
        manifest_path=args.manifest if hasattr(args, "manifest") else None,
        brief_path=args.brief if hasattr(args, "brief") else None,
        storyboard_result_path=args.storyboard_result if hasattr(args, "storyboard_result") else None,
        segments_path=args.segments if hasattr(args, "segments") else None,
        events_path=args.events if hasattr(args, "events") else None)
    out_path = getattr(args, "out", None) or resolved.get("out_path")
    if args.command != "refresh" and not out_path:
        parser.error("无法自动发现当前 run，请显式提供 --manifest / --storyboard-result / --segments / --events 或 --client / --run-id")
    if args.command == "refresh":
        snapshot = refresh_run_canvas(
            client=args.client, run_id=args.run_id,
            storyboard_result_path=args.storyboard_result,
            existing_only=True, reason=args.reason)
        print(json.dumps({
            "ok": True,
            "refreshed": snapshot is not None,
            "client": args.client or "",
            "run_id": args.run_id or "",
        }, ensure_ascii=False))
        return 0
    if args.command == "generate":
        snapshot = generate_canvas(
            out_path=out_path, manifest_path=resolved["manifest_path"],
            brief_path=resolved["brief_path"],
            storyboard_result_path=resolved["storyboard_result_path"],
            caption_manifest_path=resolved.get("caption_manifest_path"),
            delivery_path=resolved.get("delivery_path"),
            final_path=resolved.get("final_path"),
            segments_path=resolved["segments_path"], events_path=resolved["events_path"])
        print(json.dumps({"ok": True, "out": os.path.abspath(out_path),
                          "json": os.path.splitext(os.path.abspath(out_path))[0] + ".json",
                          "steps": len(snapshot.get("steps") or []),
                          "assets": len(snapshot.get("assets") or []),
                          "feedback": len(snapshot.get("feedback_loops") or []),
                          "client": resolved.get("client") or "",
                          "run_id": resolved.get("run_id") or ""},
                         ensure_ascii=False))
        return 0
    serve_canvas(
        out_path=out_path, manifest_path=resolved["manifest_path"],
        brief_path=resolved["brief_path"], storyboard_result_path=resolved["storyboard_result_path"],
        caption_manifest_path=resolved.get("caption_manifest_path"),
        delivery_path=resolved.get("delivery_path"),
        final_path=resolved.get("final_path"),
        segments_path=resolved["segments_path"], events_path=resolved["events_path"],
        host=args.host, port=args.port,
        history_limit=args.history_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
