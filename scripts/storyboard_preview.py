#!/usr/bin/env python3
"""Storyboard customer preview writers."""
import base64
import html as html_lib
import os
from pathlib import Path


def data_uri(path):
    ext = os.path.splitext(path)[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, encoded)


def _is_design_board(plan, results):
    contract = plan.get("storyboard_contract") if isinstance(plan.get("storyboard_contract"), dict) else {}
    return bool(
        str(results.get("storyboard_role") or plan.get("storyboard_role") or "").strip().lower().replace("-", "_") == "design_board" or
        str(contract.get("kind") or contract.get("role") or contract.get("usage") or "").strip().lower().replace("-", "_") == "design_board" or
        str(results.get("video_model_usage") or contract.get("video_model_usage") or "").strip().lower() == "do_not_submit_as_storyboard_ref"
    )


def _composite_shots(results):
    composite = results.get("composite_plan") if isinstance(results.get("composite_plan"), dict) else {}
    return {
        str(item.get("id") or ""): item
        for item in (composite.get("shots") or [])
        if isinstance(item, dict) and item.get("id")
    }


def _shot_results(results):
    """Return the generated shot list for both partial and final stage results."""
    shots = results.get("shots")
    return shots if isinstance(shots, list) else []


def _slot_lines(shot_contract):
    slots = []
    for slot in (shot_contract or {}).get("asset_slots") or []:
        if not isinstance(slot, dict):
            continue
        name = slot.get("slot") or slot.get("kind") or "slot"
        owner = slot.get("owner") or slot.get("layer") or "post"
        zone = slot.get("safe_zone") or slot.get("role") or ""
        desc = slot.get("description") or slot.get("role") or ""
        slots.append("`%s` · %s · %s：%s" % (name, owner, zone, desc))
    return slots


def _motion_summary(shot_contract):
    motion = (shot_contract or {}).get("motion_plan") if isinstance((shot_contract or {}).get("motion_plan"), dict) else {}
    if not motion:
        return []
    parts = []
    for key in ("personality", "primary", "secondary", "ambient", "density", "timing", "safe_zone"):
        if motion.get(key):
            parts.append("`%s=%s`" % (key, motion.get(key)))
    a11y = motion.get("a11y") if isinstance(motion.get("a11y"), dict) else {}
    if a11y:
        a11y_bits = ["%s=%s" % (k, v) for k, v in a11y.items() if v is not None]
        if a11y_bits:
            parts.append("`a11y:%s`" % ",".join(a11y_bits))
    return parts


def _html_escape(value):
    return html_lib.escape(str(value or ""), quote=True)


def _html_list(items):
    if not items:
        return "<p class='muted'>无</p>"
    return "<ul>" + "".join("<li>%s</li>" % _html_escape(item) for item in items) + "</ul>"


def _abs_result_path(record):
    path = (record or {}).get("abspath") or (record or {}).get("path")
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.abspath(path)


def _append_audit_markdown(results, target, *, embedded=False):
    audit = results.get("plan_audit") if isinstance(results.get("plan_audit"), dict) else {}
    audit_md = results.get("plan_audit_md")
    if not audit and not audit_md:
        return
    target.append("## 非视觉计划审计 / Non-visual plan audit")
    if audit_md:
        label = "storyboard_plan_audit.md"
        target.append("- 审计报告：[%s](<%s>)" % (label, audit_md))
    blockers = audit.get("blockers") or []
    warnings = audit.get("warnings") or []
    target.append("- 状态：%s；阻断项 %d 个；风险提示 %d 个" % (
        "通过" if audit.get("ok", True) else "阻断", len(blockers), len(warnings)))
    checklist = audit.get("customer_checklist") or {}
    if checklist:
        target.append("")
        target.append("### 客户确认清单 / Customer checklist")
        for section, items in checklist.items():
            if not items:
                continue
            target.append("**%s**" % section)
            for item in items:
                prefix = "- [ ]" if not embedded else "-"
                target.append("%s %s" % (prefix, item))
    target.append("")


def _write_markdown_index(plan, results, out_dir, design_board, composite_by_id, panel_summary):
    shot_results = _shot_results(results)
    lines = []
    if design_board:
        lines.append("%s %s — 16:9 客户确认设计板（单画面 + 后期合成合同）" %
                     ("#", plan.get("project_title", "Design Board")))
    else:
        lines.append("%s %s — 16:9 黑白铅笔预演故事板确认稿（格数按剧本分镜数量）" %
                     ("#", plan.get("project_title", "Storyboard")))
    lines.append("")
    if results.get("reference_registry"):
        lines.append("## 分镜引用映射 / Panel reference mapping")
        for item in results["reference_registry"]:
            lines.append("- %s：%s" % (item["tag"], item["source"]))
        lines.append("")
    if design_board:
        lines.append("> 这是 PPT/文档/服务讲解型执行预览：每张版式板都从最终 `IntegratedExplainer` 组件的同一时间轴抽帧，Animatic 也由同一组件渲染。它确认版式、内容层级、安全区和节奏；不承诺视频模型的人物动态/口型/纹理，也不作为视频模型 storyboard_ref。")
        animatic_path = (results.get("animatic") or {}).get("path")
        if animatic_path:
            lines.append("")
            lines.append("- [播放同源 Animatic](<%s>)" % animatic_path)
    else:
        lines.append("> 故事板为 16:9 黑白铅笔线稿预演，每个分段的格数由剧本设计的分镜/片段数量决定（本次各段格数：%s）；红/蓝/绿/橙/紫仅用于导演运动、机位、构图、灯光和声音情绪标注。请客户先确认人物、镜头、构图和产品表达，确认后才进入视频生成。" % panel_summary)
    lines.append("")
    if results.get("cast_board"):
        path = _abs_result_path(results["cast_board"])
        lines.append("## 人物板 / Cast board")
        lines.append("![人物板](<%s>)" % path)
        lines.append("")
    lines.append("## 本次生成内容 / Generation checklist")
    lines.append("- 人物板：%s" % ("已生成，待确认" if results.get("cast_board") else "未生成（计划没有出场人物）"))
    if design_board:
        lines.append("- 产品本体九宫格：不适用（服务/文档讲解，无实体产品素材闸门）")
        lines.append("- 产品使用九宫格：不适用（无实体产品操作关系）")
        lines.append("- 客户确认执行版式板：%d 张，均由正式 Remotion 组件抽帧，待与 Animatic 一起确认" %
                     len(shot_results))
        if results.get("animatic"):
            lines.append("- 同源 Animatic：[%s](<%s>)" % (
                "layout_animatic.mp4", (results.get("animatic") or {}).get("path")))
        if results.get("composite_plan_path"):
            lines.append("- 后期合成合同：[%s](<%s>)" % (
                "composite_plan.json", results.get("composite_plan_path")))
    else:
        lines.append("- 产品本体九宫格：%s" % ("已生成，待确认" if results.get("product_board") else "未生成（检测到产品图后必须生成）"))
        lines.append("- 产品使用九宫格：%s" % ("已生成，待确认" if results.get("product_usage_image") else "未生成（存在产品图时必须同步生成）"))
        lines.append("- 分段故事板：%d 张，均为 16:9，格数按剧本分镜数量（%s），待确认" %
                     (len(shot_results), panel_summary))
    lines.append("")
    _append_audit_markdown(results, lines)
    lines.append("## %s" % (
        "16:9 客户确认设计板 / 16:9 clean design-board frames"
        if design_board else
        "16:9 电影级分镜故事板 / 16:9 cinematic storyboard (panel count is script-driven)"))
    if results.get("product_board"):
        lines.append("## 产品本体九宫格 / Product-only 3x3 board")
        status = results["product_board"].get("status", "pending")
        lines.append("产品板状态：%s%s" % (
            status, "，必须先确认产品九宫格，再进入后续生成。" if status != "confirmed" else "。"))
        lines.append("![产品九宫格产品板](<%s>)" % _abs_result_path(results["product_board"]))
    if results.get("product_usage_image"):
        usage = results["product_usage_image"]
        lines.append("## 产品使用九宫格 / Product-in-use 3x3 board")
        lines.append("状态：%s。请确认九个使用镜面、数字人身份、手部接触和产品操作关系。" %
                     usage.get("status", "pending"))
        lines.append("![产品使用九宫格](<%s>)" % _abs_result_path(usage))
    for item in shot_results:
        shot = item.get("shot", {})
        contract = composite_by_id.get(str(shot.get("id") or item.get("id") or ""))
        path = _abs_result_path(item)
        lines.append("### %s · %ss" % (shot.get("id", "shot"), shot.get("duration", "?")))
        lines.append("- 台词/旁白：%s" % shot.get("dialogue", ""))
        lines.append("- 画面：%s" % shot.get("visual", ""))
        lines.append("- 镜头：%s" % shot.get("camera", ""))
        if shot.get("ref_tags"):
            lines.append("- 参考绑定：%s" % " ".join(shot["ref_tags"]))
        if design_board:
            qc = item.get("design_board_qc") or {}
            lines.append("- 执行帧用途：确认最终组件的版式、内容层级、安全区和当前时间点；身份由人物板单独确认。")
            lines.append("- 视频模型输入：另行使用干净人物/场景关键帧；不提交本执行帧作为 storyboard_ref。")
            if qc:
                lines.append("- 单画面质检：%s（%s）" % (
                    "通过" if qc.get("ok") else "未通过",
                    ", ".join(qc.get("failures") or ["无明显文字/多宫格问题"])))
            motion = _motion_summary(contract)
            if motion:
                lines.append("- 动效节奏合同：%s" % " ".join(motion))
            slots = _slot_lines(contract)
            if slots:
                lines.append("- 后期层位：")
                for slot in slots:
                    lines.append("  - %s" % slot)
        lines.append("![%s](<%s>)" % (shot.get("id", "shot"), path))
        lines.append("")
    index_path = os.path.join(out_dir, "storyboard_index.md")
    Path(index_path).write_text("\n".join(lines), encoding="utf-8")
    return os.path.abspath(index_path)


def _write_embedded_index(plan, results, out_dir, design_board, composite_by_id):
    shot_results = _shot_results(results)
    embedded = []
    embedded.append("%s %s — %s" %
                    ("#", plan.get("project_title", "Storyboard"),
                     "内嵌设计板预览" if design_board else "内嵌故事板预览"))
    embedded.append("")
    if results.get("reference_registry"):
        embedded.append("## 分镜引用映射 / Panel reference mapping")
        for item in results["reference_registry"]:
            embedded.append("- %s：%s" % (item["tag"], item["source"]))
        embedded.append("")
    embedded.append("> 如果当前 Agent 不能直接渲染本地文件路径图片，请打开这个版本。它把图片编码进 markdown，便于直接在聊天里预览。")
    embedded.append("")
    if design_board and (results.get("animatic") or {}).get("path"):
        embedded.append("- [播放同源 Animatic](<%s>)" % (results.get("animatic") or {}).get("path"))
        embedded.append("")
    _append_audit_markdown(results, embedded, embedded=True)
    if results.get("cast_board"):
        path = _abs_result_path(results["cast_board"])
        embedded.append("## 人物板 / Cast board")
        embedded.append("![人物板](%s)" % data_uri(path))
        embedded.append("")
    if results.get("product_board"):
        path = _abs_result_path(results["product_board"])
        status = results["product_board"].get("status", "pending")
        embedded.append("## 产品板 / Product consistency board")
        embedded.append("> 状态：%s%s" % (
            status, "；请确认产品九宫格后再进入后续生成。" if status != "confirmed" else "。"))
        embedded.append("![产品九宫格产品板](%s)" % data_uri(path))
        embedded.append("")
    if results.get("product_usage_image"):
        usage = results["product_usage_image"]
        path = _abs_result_path(usage)
        embedded.append("## 产品使用图 / Product-in-use reference")
        embedded.append("> 状态：%s。请确认人物实际使用动作、手部接触和产品细节。" %
                        usage.get("status", "pending"))
        embedded.append("![人物使用产品细节图](%s)" % data_uri(path))
        embedded.append("")
    embedded.append("## %s" % (
        "客户确认设计板 / Clean design-board frames"
        if design_board else "分镜故事板 / Shot storyboard"))
    for item in shot_results:
        shot = item.get("shot", {})
        contract = composite_by_id.get(str(shot.get("id") or item.get("id") or ""))
        path = _abs_result_path(item)
        embedded.append("### %s · %ss" % (shot.get("id", "shot"), shot.get("duration", "?")))
        if shot.get("ref_tags"):
            embedded.append("- 参考绑定：%s" % " ".join(shot["ref_tags"]))
        embedded.append("- 台词/旁白：%s" % shot.get("dialogue", ""))
        embedded.append("- 画面：%s" % shot.get("visual", ""))
        embedded.append("- 镜头：%s" % shot.get("camera", ""))
        if design_board:
            embedded.append("- 底片用途（执行帧）：确认同一 Remotion 时间轴上的版式、安全区和内容层级；不作为视频模型参考。")
            slots = _slot_lines(contract)
            if slots:
                embedded.append("- 后期层位：%s" % "；".join(slots))
        embedded.append("![%s](%s)" % (shot.get("id", "shot"), data_uri(path)))
        embedded.append("")
    embedded_path = os.path.join(out_dir, "storyboard_embedded.md")
    Path(embedded_path).write_text("\n".join(embedded), encoding="utf-8")
    return os.path.abspath(embedded_path)


def _write_html_index(plan, results, out_dir, design_board, composite_by_id):
    shot_results = _shot_results(results)
    html = []
    html.append("<!doctype html><html><head><meta charset='utf-8'>")
    html.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    html.append("<title>%s Preview</title>" % ("Design Board" if design_board else "Storyboard"))
    html.append("<style>body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:24px;background:#111;color:#eee} .card{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:18px;margin:18px 0}.shotgrid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);gap:18px;align-items:start}.panel{background:#141414;border:1px solid #2b2b2b;border-radius:8px;padding:14px}.pill{display:inline-block;border:1px solid #555;border-radius:999px;padding:2px 8px;margin:2px;color:#ddd}.ok{color:#8ee59f}.warn{color:#ffba75} img{max-width:100%;height:auto;border-radius:8px;border:1px solid #444} .meta,.muted{color:#bbb;line-height:1.7} li{margin:.35rem 0}@media(max-width:900px){.shotgrid{grid-template-columns:1fr}}</style>")
    html.append("</head><body>")
    html.append("<h1>%s — %s</h1>" % (
        _html_escape(plan.get("project_title", "Storyboard")),
        "客户确认设计板" if design_board else "故事板预览"))
    if design_board:
        html.append("<p class='meta'>这些执行版式板和 Animatic 均由最终 IntegratedExplainer 组件渲染，用于确认版式、内容层级、安全区与时间节奏。人物动态、口型和生成纹理由视频样片另验；本执行预览不作为视频模型 storyboard_ref。</p>")
        animatic_path = (results.get("animatic") or {}).get("path")
        if animatic_path:
            html.append("<div class='card'><h2>同源 Animatic</h2><video controls preload='metadata' style='width:100%;max-width:1100px;border-radius:8px' src='%s'></video></div>" % _html_escape(animatic_path))
    else:
        html.append("<p class='meta'>这些图由 gpt-image-2 生成。请确认人物、镜头、构图和产品表达；确认后才进入视频生成。</p>")
    if results.get("reference_registry"):
        html.append("<div class='card'><h2>分镜引用映射 / Panel reference mapping</h2><p class='meta'>" +
                    "<br>".join("%s: %s" % (item["tag"], item["source"])
                                for item in results["reference_registry"]) + "</p></div>")
    audit = results.get("plan_audit") if isinstance(results.get("plan_audit"), dict) else {}
    if audit or results.get("plan_audit_md"):
        checklist = audit.get("customer_checklist") or {}
        html.append("<div class='card'><h2>非视觉计划审计 / Non-visual plan audit</h2>")
        html.append("<p class='meta'>状态：%s；阻断项 %d 个；风险提示 %d 个。%s</p>" % (
            "通过" if audit.get("ok", True) else "阻断",
            len(audit.get("blockers") or []), len(audit.get("warnings") or []),
            ("审计报告：" + results.get("plan_audit_md")) if results.get("plan_audit_md") else ""))
        if checklist:
            html.append("<ul>")
            for section, items in checklist.items():
                for item in items or []:
                    html.append("<li><b>%s</b>：%s</li>" % (section, item))
            html.append("</ul>")
        html.append("</div>")
    if results.get("cast_board"):
        path = _abs_result_path(results["cast_board"])
        html.append("<div class='card'><h2>人物板 / Cast board</h2>")
        html.append("<img alt='人物板' src='%s'></div>" % data_uri(path))
    if results.get("product_board"):
        path = _abs_result_path(results["product_board"])
        status = results["product_board"].get("status", "pending")
        html.append("<div class='card'><h2>产品板 / Product consistency board</h2>")
        html.append("<p class='meta'>状态：%s%s</p>" % (
            status, "；请确认产品九宫格后再进入后续生成。" if status != "confirmed" else "。"))
        html.append("<img alt='产品九宫格产品板' src='%s'></div>" % data_uri(path))
    if results.get("product_usage_image"):
        usage = results["product_usage_image"]
        path = _abs_result_path(usage)
        html.append("<div class='card'><h2>产品使用图 / Product-in-use reference</h2>")
        html.append("<p class='meta'>状态：%s。请确认人物实际使用动作、手部接触和产品细节。</p>" %
                    usage.get("status", "pending"))
        html.append("<img alt='人物使用产品细节图' src='%s'></div>" % data_uri(path))
    for item in shot_results:
        shot = item.get("shot", {})
        contract = composite_by_id.get(str(shot.get("id") or item.get("id") or ""))
        path = _abs_result_path(item)
        html.append("<div class='card'>")
        html.append("<h2>%s · %ss</h2>" % (_html_escape(shot.get("id", "shot")), _html_escape(shot.get("duration", "?"))))
        if design_board:
            qc = item.get("design_board_qc") or {}
            qc_label = "通过" if qc.get("ok", True) else "未通过"
            qc_class = "ok" if qc.get("ok", True) else "warn"
            html.append("<div class='shotgrid'><div class='panel'><h3>干净底片画面</h3>")
            html.append("<img alt='%s' src='%s'>" % (_html_escape(shot.get("id", "shot")), data_uri(path)))
            html.append("</div><div class='panel'><h3>后期合成合同</h3>")
            html.append("<p class='meta'><b>台词/旁白：</b>%s<br><b>画面：</b>%s<br><b>镜头：</b>%s<br><b>参考绑定：</b>%s</p>" %
                        (_html_escape(shot.get("dialogue", "")), _html_escape(shot.get("visual", "")),
                         _html_escape(shot.get("camera", "")), _html_escape(" ".join(shot.get("ref_tags") or []))))
            html.append("<p class='%s'>单画面质检：%s</p>" % (qc_class, _html_escape(qc_label)))
            motion = _motion_summary(contract)
            if motion:
                html.append("<p>" + "".join("<span class='pill'>%s</span>" % _html_escape(item) for item in motion) + "</p>")
            html.append("<h4>层位</h4>%s" % _html_list(_slot_lines(contract)))
            html.append("<p class='muted'>确认重点：人物/背景/PPT区域构图；不确认最终文字层。视频模型只继承人物、服装、声音、背景锚点。</p>")
            html.append("</div></div>")
        else:
            html.append("<p class='meta'><b>台词/旁白：</b>%s<br><b>画面：</b>%s<br><b>镜头：</b>%s<br><b>参考绑定：</b>%s</p>" %
                        (_html_escape(shot.get("dialogue", "")), _html_escape(shot.get("visual", "")),
                         _html_escape(shot.get("camera", "")), _html_escape(" ".join(shot.get("ref_tags") or []))))
            html.append("<img alt='%s' src='%s'>" % (_html_escape(shot.get("id", "shot")), data_uri(path)))
        html.append("</div>")
    html.append("</body></html>")
    html_path = os.path.join(out_dir, "storyboard_preview.html")
    Path(html_path).write_text("\n".join(html), encoding="utf-8")
    return os.path.abspath(html_path)


def write_index(plan, results, out_dir, *, normalize_panel_plan_fn):
    shot_results = _shot_results(results)
    design_board = _is_design_board(plan, results)
    composite_by_id = _composite_shots(results)
    panel_counts = [len(normalize_panel_plan_fn(item.get("shot", {})))
                    for item in shot_results]
    panel_summary = "、".join(str(n) for n in panel_counts) if panel_counts else "N"
    index_path = _write_markdown_index(
        plan, results, out_dir, design_board, composite_by_id, panel_summary)
    embedded_path = _write_embedded_index(
        plan, results, out_dir, design_board, composite_by_id)
    html_path = _write_html_index(
        plan, results, out_dir, design_board, composite_by_id)
    return index_path, embedded_path, html_path
