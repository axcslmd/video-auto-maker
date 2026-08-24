#!/usr/bin/env python3
"""Product-shot library: multi-angle product views + resolve to video refs.

对标 digital_human.py 的产品素材版本。
核心理念：每件产品需要从 front/back/side/detail/scene/pack 等多方位准备素材图，
最终 resolve() 返回 portrait(主图) + refs(其它方位图) 列表，
供最终 segments.json 的 video_type=5 多图锚定，再由 video_engine.render_batch()/--batch
统一提交，确保视频里产品外观保持一致。

目录结构:
  assets/<client>/product/<sku>/
    hero.png          主图（正面/主推镜位）
    meta.json         {sku, client, product_type, style_hint, default_views}
    views/
      front.png       正面
      back.png        背面
      side.png        侧面
      detail.png      细节特写
      scene.png       场景/使用环境
      pack.png        包装图

标准方位:
  front   正面产品图（默认主图来源）
  back    背面
  side    侧45°
  detail  细节/纹理/特写
  scene   使用场景/生活方式
  pack    外包装/礼盒

CLI:
  python3 product_library.py list [--client <client>]
  python3 product_library.py create --client <client> --sku <sku> \\
      --from-file /path/hero.png [--product-type 智能手机 --style-hint 科技感]
  python3 product_library.py add-view --client <client> --sku <sku> \\
      --view front --file /path/front.png
  python3 product_library.py gen-view --client <client> --sku <sku> \\
      --view side [--prompt "产品侧面45度，纯白背景"] [--ref views/front.png]
  python3 product_library.py gen-all-views --client <client> --sku <sku> \\
      --ref views/front.png   # 以主图为参考，批量生成全方位图
  python3 product_library.py resolve --client <client> --sku <sku> \\
      [--views front side detail]
"""
import os
import sys
import json
import shutil
import argparse
import hashlib
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS_DIR = os.path.join(ROOT, "assets")
sys.path.insert(0, HERE)
import br_client    # noqa: E402
import key_setup    # noqa: E402
import ux           # noqa: E402
from project_utils import (validate_client, validate_component, atomic_copy_file,
                           atomic_write_json)  # noqa: E402

# 标准方位定义：view_name -> (中文名, 生成 prompt 模板)
_VIEW_DEFS = {
    "front":  ("正面",   "产品正面，纯白/纯色背景，居中构图，商业产品图，高清写实"),
    "back":   ("背面",   "产品背面，纯白背景，居中，商业产品图，高清写实"),
    "side":   ("侧面45°","产品侧面45度角，纯白背景，斜构图，商业产品图，高清写实"),
    "detail": ("细节特写","产品局部特写/细节/纹理，浅色背景，微距，商业级质感"),
    "scene":  ("场景图", "产品自然使用场景，生活方式摄影，柔和自然光，空气感"),
    "pack":   ("包装图", "产品外包装/礼盒，俯视或斜45度，商业产品图，高清写实"),
}
# resolve 默认选用的方位顺序（取前 N 张作 refs）
_DEFAULT_VIEW_ORDER = ["front", "side", "detail", "back", "scene", "pack"]


def _sku_dir(client, sku):
    validate_client(client)
    validate_component(sku, "sku")
    path = os.path.join(ASSETS_DIR, client, "product", sku)
    root = os.path.abspath(ASSETS_DIR)
    current = os.path.abspath(path)
    while os.path.commonpath((root, current)) == root:
        if os.path.lexists(current) and os.path.islink(current):
            raise ValueError("PRODUCT_DIRECTORY_SYMLINK_BLOCKED: %s" % current)
        if current == root:
            break
        current = os.path.dirname(current)
    return path


def _meta_path(client, sku):
    return os.path.join(_sku_dir(client, sku), "meta.json")


def _load_meta(client, sku):
    p = _meta_path(client, sku)
    if os.path.isfile(p):
        with open(p) as f:
            return json.load(f)
    return {"sku": sku, "client": client, "product_type": "",
            "style_hint": "", "default_views": _DEFAULT_VIEW_ORDER[:]}


def _save_meta(client, sku, meta):
    atomic_write_json(_meta_path(client, sku), meta)


def _view_path(client, sku, view):
    if view not in _VIEW_DEFS:
        raise ValueError("VIEW_INVALID: %s" % view)
    return os.path.join(_sku_dir(client, sku), "views", view + ".png")


def _view_confirmation_path(client, sku, view):
    return os.path.join(_sku_dir(client, sku), "views", view + ".confirmed.json")


def _hero_confirmation_path(client, sku):
    return os.path.join(_sku_dir(client, sku), "hero.confirmed.json")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _anchor_confirmation_current(path, marker_path, *, view=None):
    """Validate a formal product anchor against its immutable evidence marker."""
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    marker = _load_json(marker_path)
    if marker.get("status") != "confirmed":
        return False
    if view is not None and marker.get("view") != view:
        return False
    if marker.get("path") != os.path.abspath(path):
        return False
    if marker.get("image_sha256") != file_sha256(path):
        return False
    source_kind = marker.get("source_kind")
    if source_kind == "trusted_upload":
        return marker.get("confirmation_source") == "customer_upload"
    return (
        source_kind == "model_generated"
        and marker.get("confirmed_by_customer") is True
        and bool(str(marker.get("customer_confirmation") or "").strip())
        and marker.get("confirmation_source") == "customer_dialogue"
        and bool(marker.get("confirmed_at"))
    )


def _record_trusted_upload(path, marker_path, *, client, sku, kind, view=None):
    payload = {
        "status": "confirmed",
        "kind": kind,
        "client": client,
        "sku": sku,
        "view": view,
        "path": os.path.abspath(path),
        "image_sha256": file_sha256(path),
        "source_kind": "trusted_upload",
        "confirmation_source": "customer_upload",
        "ingested_at": _now_iso(),
    }
    atomic_write_json(marker_path, payload)
    return payload


def source_manifest(client, sku):
    """Return only evidence-current hero/formal-view hashes in stable order."""
    directory = _sku_dir(client, sku)
    hero = os.path.join(directory, "hero.png")
    records = []
    if _anchor_confirmation_current(hero, _hero_confirmation_path(client, sku)):
        records.append({"role": "hero", "sha256": file_sha256(hero)})
    for view in _DEFAULT_VIEW_ORDER:
        path = _view_path(client, sku, view)
        if _anchor_confirmation_current(
                path, _view_confirmation_path(client, sku, view), view=view):
            records.append({"role": "view:%s" % view,
                            "sha256": file_sha256(path)})
    return records


def source_fingerprint(client, sku):
    payload = json.dumps(source_manifest(client, sku), sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _product_board_status(client, sku):
    directory = _sku_dir(client, sku)
    board = os.path.join(directory, "product_board.png")
    state_path = os.path.join(directory, "product_board_state.json")
    record_path = os.path.join(directory, "product_board.json")
    state = _load_json(state_path)
    record = _load_json(record_path)
    current_source = source_fingerprint(client, sku)
    evidence_current = (
        state.get("confirmed_by_customer") is True
        and bool(str(state.get("customer_confirmation") or "").strip())
        and state.get("confirmation_source") == "customer_dialogue"
        and bool(state.get("confirmed_at"))
        and record.get("confirmed_by_customer") is True
        and record.get("customer_confirmation") == state.get("customer_confirmation")
        and record.get("confirmation_source") == "customer_dialogue"
        and record.get("confirmed_at") == state.get("confirmed_at")
    )
    valid = (
        state.get("status") == "confirmed"
        and record.get("status") == "confirmed"
        and os.path.isfile(board)
        and state.get("source_fingerprint") == current_source
        and record.get("source_fingerprint") == current_source
        and state.get("board_sha256") == file_sha256(board)
        and record.get("board_sha256") == state.get("board_sha256")
        and evidence_current
    )
    if state.get("status") == "confirmed" and not valid:
        state["status"] = "stale"
        state["current_source_fingerprint"] = current_source
        atomic_write_json(state_path, state)
    return board if valid else None, state


def list_products(client=None):
    """列出已登记的产品 SKU 及其方位图。"""
    out = []
    for cl in ([client] if client else sorted(os.listdir(ASSETS_DIR))):
        pdir = os.path.join(ASSETS_DIR, cl, "product")
        if not os.path.isdir(pdir):
            continue
        for sku in sorted(os.listdir(pdir)):
            mp = os.path.join(pdir, sku, "meta.json")
            if not os.path.isfile(mp):
                continue
            with open(mp) as f:
                meta = json.load(f)
            vdir = os.path.join(pdir, sku, "views")
            views = {}
            if os.path.isdir(vdir):
                for f in sorted(os.listdir(vdir)):
                    if f.endswith(".png") or f.endswith(".jpg"):
                        name = os.path.splitext(f)[0]
                        views[name] = os.path.join(vdir, f)
            meta["views"] = views
            hero = os.path.join(pdir, sku, "hero.png")
            meta["hero"] = hero if os.path.isfile(hero) else None
            out.append(meta)
    return out


def create_product(client, sku, from_file=None, product_type="", style_hint=""):
    """建立 SKU 条目，可选从文件导入主图 hero.png。"""
    adir = _sku_dir(client, sku)
    os.makedirs(os.path.join(adir, "views"), exist_ok=True)
    hero = os.path.join(adir, "hero.png")
    source = "empty"
    if from_file:
        if not os.path.isfile(from_file):
            raise SystemExit("file not found: %s" % from_file)
        try:
            atomic_copy_file(from_file, hero)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        _record_trusted_upload(
            hero, _hero_confirmation_path(client, sku), client=client, sku=sku,
            kind="product_hero")
        source = "photo:" + os.path.basename(from_file)
    meta = _load_meta(client, sku)
    meta.update({"sku": sku, "client": client,
                 "product_type": product_type or meta.get("product_type", ""),
                 "style_hint": style_hint or meta.get("style_hint", ""),
                 "source": source})
    _save_meta(client, sku, meta)
    return {"hero": hero if os.path.isfile(hero) else None, "meta": meta}


def add_view(client, sku, view, file_path):
    """把已有图片加入指定方位 views/<view>.png。"""
    if not os.path.isfile(file_path):
        raise SystemExit("file not found: %s" % file_path)
    dest = _view_path(client, sku, view)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        atomic_copy_file(file_path, dest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _record_trusted_upload(
        dest, _view_confirmation_path(client, sku, view), client=client,
        sku=sku, kind="product_view", view=view)
    # 如果还没 hero，就把 front 或第一张设为 hero
    hero = os.path.join(_sku_dir(client, sku), "hero.png")
    if not os.path.isfile(hero):
        atomic_copy_file(dest, hero)
        _record_trusted_upload(
            hero, _hero_confirmation_path(client, sku), client=client, sku=sku,
            kind="product_hero")
    return {"view": view, "path": dest}


def _compose_view_prompt(sku, product_type, style_hint, view, extra_prompt=""):
    """组装方位生成 prompt：品类风格 + 方位模板 + 额外描述。"""
    base_label, base_tpl = _VIEW_DEFS.get(view, ("方位图", "产品图，纯色背景，商业级"))
    parts = []
    if product_type:
        parts.append(product_type)
    parts.append(base_tpl)
    if style_hint:
        parts.append(style_hint)
    if extra_prompt:
        parts.append(extra_prompt)
    return "，".join(p for p in parts if p)


def gen_view(client, sku, view, prompt=None, ref=None,
             ratio="1:1", resolution="2k", model=None, refine=True):
    """AI 生成指定方位图（可选以现有图为参考保持产品一致性）。

    - ref=None：纯文生图（适合产品外观图、场景图）
    - ref=<path>：图生图，以参考图为基础衍生，产品外观保持一致
    - refine=True：同 asset_prep.gen_image，产出 A/B 两个独立候选（v1/v2 平行、非精修）待确认
    生成结果保存到 views/<view>_v1.png / _v2.png（pending），
    confirm_view() 后才晋升为正式方位图 views/<view>.png。
    """
    k = key_setup.load_key()
    if not k:
        raise SystemExit(ux.friendly_error("No API key. Run key onboarding first."))
    meta = _load_meta(client, sku)
    full_prompt = prompt or _compose_view_prompt(
        sku, meta.get("product_type", ""), meta.get("style_hint", ""), view)

    image_urls = []
    if ref:
        src = ref if os.path.isfile(ref) else os.path.join(ROOT, ref)
        if not os.path.isfile(src):
            raise SystemExit("ref image not found: %s" % ref)
        image_urls = [br_client.to_image_ref(src)]

    kw = {"count": 1, "resolution": resolution, "ratio": ratio,
          "image_urls": image_urls}
    if model:
        kw["model"] = model
    task1 = br_client.create_image_generation(k, full_prompt, **kw)
    print("[product-library] view %s task submitted: %s" % (view, task1), flush=True)
    urls1 = br_client.wait_image_generation(k, task1, interval=5, max_wait=900)
    if not urls1:
        raise SystemExit("gen_view: API returned no image")

    vdir = os.path.join(_sku_dir(client, sku), "views")
    os.makedirs(vdir, exist_ok=True)
    dest1 = os.path.join(vdir, "%s_v1.png" % view)
    br_client.download(urls1[0], dest1, allow_nonpublic_peer=True)

    if not refine:
        return {"view": view, "pass1": dest1, "pass2": None,
                "needs_confirmation": True, "prompt": full_prompt}

    # 版本 B：用同一 prompt/同参考图独立再生成一版（平行候选，非 v1 的精修）
    dest2 = None
    try:
        task2 = br_client.create_image_generation(k, full_prompt, **kw)
        print("[product-library] view %s variant task submitted: %s" %
              (view, task2), flush=True)
        urls2 = br_client.wait_image_generation(k, task2, interval=5, max_wait=900)
        if urls2:
            dest2 = os.path.join(vdir, "%s_v2.png" % view)
            br_client.download(urls2[0], dest2, allow_nonpublic_peer=True)
    except (br_client.BRError, OSError, ValueError, RuntimeError):
        dest2 = None

    return {"view": view, "pass1": dest1, "pass2": dest2,
            "needs_confirmation": True, "prompt": full_prompt}


def confirm_view(client, sku, view, use_v2=True, customer_confirmation=None):
    """确认选用哪版方位图（use_v2=True 选版本B/v2，否则版本A/v1），
    晋升为 views/<view>.png 正式锚点，清除另一版候选文件。"""
    customer_confirmation = str(customer_confirmation or "").strip()
    if not customer_confirmation:
        raise SystemExit("CUSTOMER_CONFIRMATION_REQUIRED")
    vdir = os.path.join(_sku_dir(client, sku), "views")
    v1 = os.path.join(vdir, "%s_v1.png" % view)
    v2 = os.path.join(vdir, "%s_v2.png" % view)
    chosen = v2 if (use_v2 and os.path.isfile(v2)) else v1
    if not os.path.isfile(chosen):
        raise SystemExit("no pending view found: %s/%s" % (sku, view))
    dest = _view_path(client, sku, view)
    if os.path.islink(chosen) or os.path.islink(dest):
        raise SystemExit("PRODUCT_VIEW_SYMLINK_BLOCKED")
    marker_path = _view_confirmation_path(client, sku, view)
    marker_existed = os.path.isfile(marker_path)
    old_marker = _load_json(marker_path)
    backup = None
    if os.path.isfile(dest):
        fd, backup = tempfile.mkstemp(prefix=".%s.previous-" % view,
                                      suffix=".png", dir=vdir)
        os.close(fd)
        os.unlink(backup)
        os.replace(dest, backup)
    hero = os.path.join(_sku_dir(client, sku), "hero.png")
    create_hero = view == "front" and not os.path.isfile(hero)
    hero_marker_path = _hero_confirmation_path(client, sku)
    hero_marker_existed = os.path.isfile(hero_marker_path)
    old_hero_marker = _load_json(hero_marker_path)
    confirmed_at = _now_iso()
    committed = False
    try:
        os.replace(chosen, dest)
        atomic_write_json(marker_path, {
            "status": "confirmed",
            "kind": "product_view",
            "client": client,
            "sku": sku,
            "view": view,
            "path": os.path.abspath(dest),
            "image_sha256": file_sha256(dest),
            "source_kind": "model_generated",
            "confirmed_by_customer": True,
            "customer_confirmation": customer_confirmation,
            "confirmation_source": "customer_dialogue",
            "confirmed_at": confirmed_at,
        })
        if create_hero:
            atomic_copy_file(dest, hero)
            atomic_write_json(hero_marker_path, {
                "status": "confirmed",
                "kind": "product_hero",
                "client": client,
                "sku": sku,
                "view": view,
                "path": os.path.abspath(hero),
                "image_sha256": file_sha256(hero),
                "source_kind": "model_generated",
                "confirmed_by_customer": True,
                "customer_confirmation": customer_confirmation,
                "confirmation_source": "customer_dialogue",
                "confirmed_at": confirmed_at,
            })
        committed = True
    finally:
        if not committed:
            if create_hero and os.path.isfile(hero):
                os.unlink(hero)
            if hero_marker_existed:
                atomic_write_json(hero_marker_path, old_hero_marker)
            elif os.path.isfile(hero_marker_path):
                os.unlink(hero_marker_path)
            if os.path.isfile(dest):
                os.replace(dest, chosen)
            if backup and os.path.isfile(backup):
                os.replace(backup, dest)
            if marker_existed:
                atomic_write_json(marker_path, old_marker)
            elif os.path.isfile(marker_path):
                os.unlink(marker_path)
    if backup and os.path.isfile(backup):
        os.unlink(backup)
    # 清理另一版候选
    for leftover in [v1, v2]:
        if leftover != dest and os.path.isfile(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass
    return {"confirmed": dest, "view": view}


def gen_all_views(client, sku, ref=None, views=None,
                  ratio="1:1", resolution="2k", model=None, refine=True):
    """批量生成所有标准方位图。

    以 ref 图（通常是 front 或 hero）为参考保持产品一致性，
    依次生成 views 列表里的每个方位，所有结果均为 pending 状态，
    需要逐一 confirm_view() 确认。
    返回 {view -> gen_view_result} 字典，方便 agent 逐项展示给客户确认。
    """
    views = views or _DEFAULT_VIEW_ORDER
    results = {}
    for v in views:
        # 如果该方位已经有正式图，跳过（不覆盖已确认的）
        formal_path = _view_path(client, sku, v)
        if _anchor_confirmation_current(
                formal_path, _view_confirmation_path(client, sku, v), view=v):
            results[v] = {"view": v, "skipped": True,
                          "reason": "正式方位图已存在，跳过生成"}
            continue
        try:
            r = gen_view(client, sku, v, ref=ref,
                         ratio=ratio, resolution=resolution,
                         model=model, refine=refine)
            results[v] = r
        except (br_client.BRError, OSError, ValueError, TypeError, RuntimeError) as e:
            results[v] = {"view": v, "error": str(e)}
    return results


def resolve(client, sku, views=None, max_refs=4):
    """返回 hero(主图) + refs(多方位图列表) 供 video_engine videoType=5 使用。

    对标 digital_human.resolve()。
    - hero:  主图，优先 views/front.png，fallback hero.png
    - refs:  其余方位图路径列表（不含 hero，最多 max_refs 张）
    - video_type: refs>=1 时返回 5，否则返回 2（图生视频）
    - views 参数可指定优先方位顺序，默认 _DEFAULT_VIEW_ORDER
    """
    adir = _sku_dir(client, sku)
    mp = os.path.join(adir, "meta.json")
    if not os.path.isdir(adir):
        raise SystemExit("product not found: %s/%s" % (client, sku))
    meta = _load_meta(client, sku)

    order = views or meta.get("default_views") or _DEFAULT_VIEW_ORDER

    # 按顺序收集所有已有方位图
    found = []
    for v in order:
        p = _view_path(client, sku, v)
        if _anchor_confirmation_current(
                p, _view_confirmation_path(client, sku, v), view=v):
            found.append({"view": v, "path": p})

    hero_path = None
    if found:
        hero_path = found[0]["path"]
        refs = [f["path"] for f in found[1:max_refs + 1]]
    else:
        # fallback：没有方位图，找 hero.png
        h = os.path.join(adir, "hero.png")
        hero_path = h if _anchor_confirmation_current(
            h, _hero_confirmation_path(client, sku)) else None
        refs = []

    product_board, board_state = _product_board_status(client, sku)
    return {
        "client": client, "sku": sku,
        "product_type": meta.get("product_type", ""),
        "style_hint": meta.get("style_hint", ""),
        "hero": hero_path,
        "refs": refs,
        "views_available": [f["view"] for f in found],
        "product_board": product_board,
        "product_board_confirmed": bool(product_board),
        "product_board_status": board_state.get("status", "unknown"),
        "video_type": 5 if refs else 2,
    }


def main(argv):
    p = argparse.ArgumentParser(description="product-shot library: multi-angle views")
    sub = p.add_subparsers(dest="cmd")

    pl = sub.add_parser("list", help="列出所有产品 SKU 及方位图")
    pl.add_argument("--client", default=None)

    pc = sub.add_parser("create", help="建立产品 SKU（可选导入主图）")
    pc.add_argument("--client", required=True)
    pc.add_argument("--sku", required=True)
    pc.add_argument("--from-file", dest="from_file", default=None,
                    help="导入主图 hero.png")
    pc.add_argument("--product-type", dest="product_type", default="",
                    help="产品类型，如 智能手表/护肤品/运动鞋")
    pc.add_argument("--style-hint", dest="style_hint", default="",
                    help="风格提示，如 科技感/简约极简/活力")

    pav = sub.add_parser("add-view", help="添加已有方位图文件")
    pav.add_argument("--client", required=True)
    pav.add_argument("--sku", required=True)
    pav.add_argument("--view", required=True,
                     choices=list(_VIEW_DEFS.keys()),
                     help="方位: front/back/side/detail/scene/pack")
    pav.add_argument("--file", required=True, dest="file_path")

    pgv = sub.add_parser("gen-view", help="AI 生成单个方位图（产出 A/B 两版供选）")
    pgv.add_argument("--client", required=True)
    pgv.add_argument("--sku", required=True)
    pgv.add_argument("--view", required=True, choices=list(_VIEW_DEFS.keys()))
    pgv.add_argument("--prompt", default=None, help="自定义生成 prompt（留空自动组装）")
    pgv.add_argument("--ref", default=None, help="参考图路径（图生图，保持产品一致性）")
    pgv.add_argument("--ratio", default="1:1")
    pgv.add_argument("--resolution", default="2k")
    pgv.add_argument("--model", default=None)
    pgv.add_argument("--no-refine", dest="no_refine", action="store_true",
                     help="只出一版（不出 A/B 两版供选，快速/省 Credit）")

    pga = sub.add_parser("gen-all-views", help="批量生成所有标准方位图")
    pga.add_argument("--client", required=True)
    pga.add_argument("--sku", required=True)
    pga.add_argument("--ref", default=None,
                     help="参考图路径，通常用 front 或 hero 保持产品一致性")
    pga.add_argument("--views", nargs="*", default=None,
                     help="指定要生成的方位列表（留空=全部标准方位）")
    pga.add_argument("--ratio", default="1:1")
    pga.add_argument("--resolution", default="2k")
    pga.add_argument("--model", default=None)
    pga.add_argument("--no-refine", dest="no_refine", action="store_true")

    pcv = sub.add_parser("confirm-view", help="确认方位图候选，晋升为正式锚点")
    pcv.add_argument("--client", required=True)
    pcv.add_argument("--sku", required=True)
    pcv.add_argument("--view", required=True)
    pcv.add_argument("--use-v1", dest="use_v1", action="store_true",
                     help="使用版本A(v1)（默认使用版本B(v2)）")
    pcv.add_argument("--customer-confirmation", required=True,
                     help="客户在对话中确认选版的原话")

    prv = sub.add_parser("resolve", help="输出 hero + refs 供 video_engine 使用")
    prv.add_argument("--client", required=True)
    prv.add_argument("--sku", required=True)
    prv.add_argument("--views", nargs="*", default=None,
                     help="指定方位优先顺序（默认 front/side/detail/back/scene/pack）")
    prv.add_argument("--max-refs", dest="max_refs", type=int, default=4)

    args = p.parse_args(argv)
    if args.cmd == "list":
        print(json.dumps(list_products(args.client), ensure_ascii=False, indent=2))
    elif args.cmd == "create":
        print(json.dumps(create_product(args.client, args.sku,
                                        from_file=args.from_file,
                                        product_type=args.product_type,
                                        style_hint=args.style_hint),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "add-view":
        print(json.dumps(add_view(args.client, args.sku, args.view, args.file_path),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "gen-view":
        print(json.dumps(gen_view(args.client, args.sku, args.view,
                                  prompt=args.prompt, ref=args.ref,
                                  ratio=args.ratio, resolution=args.resolution,
                                  model=args.model, refine=not args.no_refine),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "gen-all-views":
        print(json.dumps(gen_all_views(args.client, args.sku,
                                       ref=args.ref, views=args.views,
                                       ratio=args.ratio, resolution=args.resolution,
                                       model=args.model, refine=not args.no_refine),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "confirm-view":
        print(json.dumps(confirm_view(args.client, args.sku, args.view,
                                      use_v2=not args.use_v1,
                                      customer_confirmation=args.customer_confirmation),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "resolve":
        print(json.dumps(resolve(args.client, args.sku,
                                 views=args.views, max_refs=args.max_refs),
                         ensure_ascii=False, indent=2))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
