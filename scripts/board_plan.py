#!/usr/bin/env python3
"""Compatibility wrappers for canonical storyboard plan functions.

Production semantics live in ``storyboard.py``. This module remains for older
imports and deliberately carries no independent panel, hydration or fingerprint
policy.
"""
import os
import sys
import json
import re

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import br_client
from video_segmentation import SEEDANCE_MAX_SECONDS, partition_shots

ROOT = os.path.dirname(HERE)

def safe_name(value):
    """Sanitize a string for use as a directory or file name."""
    s = re.sub(r"[^A-Za-z0-9_\-.]", "_", str(value or ""))
    return s.strip("_") or "shot"


def client_slug(plan):
    """Stable short slug for run directories."""
    for key in ("client", "client_name", "brand", "brand_name", "project_title"):
        v = plan.get(key)
        if v:
            return safe_name(v).lower()[:48]
    return "client"


def run_output_dir(base_out_dir, plan, run_id=None, flat=False):
    """Return the actual output directory for a storyboard run."""
    if flat:
        return base_out_dir
    if run_id:
        rid = safe_name(run_id)
    else:
        fingerprint = plan_fingerprint(plan)
        rid = "%s_%s" % (client_slug(plan), fingerprint)
    return os.path.join(base_out_dir, rid)


def normalize_panel_plan(shot):
    from storyboard import normalize_panel_plan as canonical
    return canonical(shot)


def canonical_storyboard_plan(plan):
    from storyboard import canonical_storyboard_plan as canonical
    return canonical(plan)


def plan_fingerprint(plan):
    from storyboard import plan_fingerprint as canonical
    return canonical(plan)


def load_plan_json(plan_path):
    """Read and parse storyboard_plan.json with human-readable error location."""
    if not os.path.exists(plan_path):
        raise br_client.BRError(
            "剧本文件不存在：%s。请先把定稿剧本解析成 storyboard_plan.json 再运行。" % plan_path
        )
    with open(plan_path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw.split("\n")
        bad_line = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        pointer = " " * max(e.colno - 1, 0) + "^"
        prev_line = lines[e.lineno - 2] if e.lineno >= 2 else ""
        context = ""
        if prev_line:
            context = "第 %d 行（上一行，供比对）: %s\n" % (e.lineno - 1, prev_line)
        raise br_client.BRError(
            "storyboard_plan.json 第 %d 行第 %d 列有 JSON 语法错误：%s\n"
            "%s第 %d 行: %s\n      %s\n"
            "常见原因：字符串少了收尾引号、对象/数组里多了或少了逗号、中文引号混入了英文 JSON。"
            "修好这一行后重新运行 storyboard.py。"
            % (e.lineno, e.colno, e.msg, context, e.lineno, bad_line, pointer)
        ) from e
