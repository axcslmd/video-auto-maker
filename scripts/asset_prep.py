#!/usr/bin/env python3
"""Asset prep: ingest product images + parse PPT/text into a structured brief.

The brief is the GROUNDING context every scene skill reads before writing a
script — so scripts reflect the real product (USPs, specs, slogan) instead of
generic filler. PPT/text parsing is LOCAL (no API). Image cutout / scene fusion
is optional and goes through BasicRouter image models.

Layout:
  assets/<client>/
    images/          copied/normalized product shots
    ppt/             source .pptx files
    brief.json       merged structured brief (USPs, specs, slogan, images, style)

CLI:
  python3 asset_prep.py ingest-image --client <client> --file /path/shot.png [--tag hero]
      # ingest-image always runs gpt-image-2 cleanup and stores only a pending candidate
  python3 asset_prep.py parse-ppt   --client <client> --file /path/deck.pptx
  python3 asset_prep.py brief       --client <client>           # print merged brief
  python3 asset_prep.py cutout      --client <client> --file images/shot.png  # bg removal (API)
"""
import os
import sys
import json
import shutil
import argparse
import tempfile
import hashlib
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
sys.path.insert(0, HERE)
import br_client  # noqa: E402
import run_manifest as run_manifest_store  # noqa: E402
import media_tools  # noqa: E402
try:
    from . import ux
except ImportError:
    import ux
from project_utils import (validate_client, require_contained_path, FileLock,
                           atomic_copy_file, atomic_write_json)
from image_utils import image_type


def _customer_confirmation_evidence_current(record):
    return bool(
        isinstance(record, dict) and
        record.get("confirmed_by_customer") is True and
        str(record.get("customer_confirmation") or "").strip() and
        record.get("confirmation_source") == "customer_dialogue" and
        record.get("confirmed_at"))


def is_confirmed(client, url_or_path, *, allow_remote=False,
                 allow_untracked=False, allowed_statuses=None):
    """Return whether a reference has an explicit trusted state.

    The default is fail-closed: only ``confirmed`` and ``trusted_upload`` pass.
    Callers implementing an explicit preview/draft path may opt into remote,
    untracked, or additional statuses. Missing/unknown/rejected/failed/quarantine
    states never become trusted merely because a file exists.
    """
    trusted = {"confirmed", "trusted_upload"}
    trusted.update(allowed_statuses or ())
    if not url_or_path or not isinstance(url_or_path, str):
        return False
    if url_or_path.startswith("http://") or url_or_path.startswith("https://") \
            or url_or_path.startswith("data:"):
        return allow_remote
    try:
        rel = (os.path.relpath(url_or_path, ROOT) if os.path.isabs(url_or_path)
               else url_or_path)
    except ValueError:
        return allow_untracked
    brief = _load_brief(client)
    if brief.get("client") != client:
        return False
    images_root = os.path.join(_client_dir(client), "images")
    candidate_abs = (url_or_path if os.path.isabs(url_or_path)
                     else os.path.join(ROOT, url_or_path))
    for entry in brief.get("images", []):
        entry_path = entry.get("path")
        entry_abs = (entry_path if isinstance(entry_path, str) and os.path.isabs(entry_path)
                     else os.path.join(ROOT, entry_path) if isinstance(entry_path, str) else None)
        if (entry.get("path") == rel or entry.get("path") == url_or_path or
                (entry_abs and os.path.realpath(entry_abs) == os.path.realpath(candidate_abs))):
            try:
                absolute = entry_abs
                require_contained_path(images_root, absolute, label="asset_image", must_exist=True)
            except (OSError, ValueError, TypeError):
                continue
            expected_sha = entry.get("sha256")
            if entry.get("status") in {"confirmed", "trusted_upload"} and not expected_sha:
                continue
            if expected_sha:
                with open(absolute, "rb") as handle:
                    actual_sha = hashlib.sha256(handle.read()).hexdigest()
                if actual_sha != expected_sha:
                    continue
            if (entry.get("status") == "confirmed" and
                    not _customer_confirmation_evidence_current(entry)):
                continue
            if entry.get("status") in trusted:
                return True
    return allow_untracked


def confirmed_asset_record(client, url_or_path):
    """Return immutable customer-confirmation evidence for a local asset."""
    if not is_confirmed(client, url_or_path, allowed_statuses={"confirmed"}):
        raise ValueError("ASSET_CUSTOMER_CONFIRMATION_REQUIRED")
    target = (url_or_path if os.path.isabs(str(url_or_path)) else
              os.path.join(ROOT, str(url_or_path)))
    target = os.path.abspath(target)
    brief = _load_brief(client)
    for item in brief.get("images") or []:
        raw = item.get("path")
        if not isinstance(raw, str):
            continue
        candidate = raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
        if os.path.realpath(candidate) != os.path.realpath(target):
            continue
        return {
            "path": target, "sha256": item.get("sha256"),
            "status": "confirmed", "confirmed_by_customer": True,
            "customer_confirmation": item.get("customer_confirmation"),
            "confirmation_source": item.get("confirmation_source"),
            "confirmed_at": item.get("confirmed_at"),
            "source_fingerprint": item.get("source_fingerprint"),
            "model": item.get("model"), "tag": item.get("tag"),
        }
    raise ValueError("ASSET_CONFIRMATION_RECORD_MISSING")


def is_product_asset_ready(client, url_or_path, *, allow_pending=False):
    """Validate the canonical product-material contract.

    Product identity references are stricter than portraits or customer scene
    photos: they must be produced by the gpt-image-2 cleanup stage and then
    explicitly confirmed. This prevents a downloaded webpage image or a raw
    trusted upload from becoming a product-board/video anchor.
    """
    if not url_or_path or not isinstance(url_or_path, str):
        return False
    if url_or_path.startswith(("http://", "https://", "data:")):
        return False
    rel = os.path.relpath(url_or_path, ROOT) if os.path.isabs(url_or_path) else url_or_path
    brief = _load_brief(client)
    for entry in brief.get("images", []):
        if entry.get("path") != rel and entry.get("path") != url_or_path:
            continue
        status = entry.get("status")
        cleaned = (entry.get("via") in {"standardize", "clean_image"} and
                   entry.get("model", "gpt-image-2") == "gpt-image-2")
        if not cleaned:
            continue
        if status == "confirmed":
            return is_confirmed(client, url_or_path, allowed_statuses={"confirmed"})
        if allow_pending and status == "pending":
            return True
    return False


def _client_dir(client):
    validate_client(client)
    d = os.path.join(ASSETS, client)
    for path in (ASSETS, d, os.path.join(d, "images"), os.path.join(d, "ppt")):
        if os.path.lexists(path) and os.path.islink(path):
            raise ValueError("ASSET_DIRECTORY_SYMLINK_BLOCKED: %s" % path)
    os.makedirs(os.path.join(d, "images"), exist_ok=True)
    os.makedirs(os.path.join(d, "ppt"), exist_ok=True)
    return d


def safe_remove_client_dir(client):
    """Remove one client directory only; never allow the shared assets root."""
    directory = os.path.abspath(_client_dir(client))
    assets_root = os.path.abspath(ASSETS)
    if directory == assets_root or os.path.dirname(directory) != assets_root:
        raise ValueError("ASSET_CLIENT_DELETE_SCOPE_INVALID: %s" % directory)
    shutil.rmtree(directory)


def _session_digest():
    """Return a non-secret identifier for diagnostics and resume metadata."""
    sid = os.environ.get("BASICROUTER_SESSION_ID", "").strip()
    return hashlib.sha256(sid.encode("utf-8")).hexdigest() if sid else None


def _context_path(client):
    return os.path.join(_client_dir(client), ".asset_context.json")


def _write_context(client):
    """Persist ownership metadata without storing the API key or raw session ID."""
    path = _context_path(client)
    previous = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
    current_session = _session_digest()
    history = list(previous.get("session_id_sha256_history", []))
    for digest in (previous.get("session_id_sha256"), current_session):
        if digest and digest not in history:
            history.append(digest)
    context = {
        "client": client,
        "asset_dir": os.path.abspath(_client_dir(client)),
        "session_id_sha256": previous.get("session_id_sha256") or current_session,
        "session_id_sha256_history": history,
    }
    atomic_write_json(path, context)


def _recovered_image_entries(client):
    """Recover the image index when a stale process removed only brief.json."""
    directory = os.path.join(_client_dir(client), "images")
    entries = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or image_type(path) not in {
                "png", "jpeg", "gif", "webp", "bmp", "tiff"}:
            continue
        entries.append({"path": os.path.relpath(path, ROOT), "tag": "recovered",
                        "status": "quarantine", "recovered": True})
    return entries


def _brief_path(client):
    return os.path.join(_client_dir(client), "brief.json")


def _default_brief(client):
    return {"client": client, "revision": 0, "product_type": None, "usps": [], "specs": {},
            "slogans": [], "texts": [], "images": [], "style_hints": [],
            "render_profile": None, "render_plan": None, "ppt_files": []}


def _load_brief(client):
    p = _brief_path(client)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            brief = json.load(f)
        if brief.get("client") not in (None, client):
            raise ValueError("BRIEF_CLIENT_MISMATCH")
        normalized = _default_brief(client)
        normalized.update(brief)
        normalized["client"] = client
        normalized.setdefault("revision", 0)
        return normalized
    recovered = _recovered_image_entries(client)
    brief = _default_brief(client)
    if recovered:
        brief["images"] = recovered
        _save_brief(client, brief, replace=True)
    return brief


def _save_brief(client, brief, replace=False):
    """Persist with revision CAS; revision-less callers use additive patch semantics."""
    path = _brief_path(client)
    lock_path = path + ".lock"
    with FileLock(lock_path, timeout=30.0, stale_after=300.0):
        current = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as existing:
                    current = json.load(existing)
            except (OSError, ValueError, json.JSONDecodeError):
                current = {"images": _recovered_image_entries(client)}
        elif not current:
            current = {"images": _recovered_image_entries(client)}
        current_revision = int(current.get("revision", 0))
        expected_revision = brief.get("revision")
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise RuntimeError("BRIEF_CONFLICT: expected revision %s, found %s" %
                               (expected_revision, current_revision))
        if replace or not current:
            merged = dict(brief)
        else:
            merged = dict(current)
            for key, value in brief.items():
                if key == "images":
                    continue
                # Do not let a stale worker erase a field populated by another.
                if value not in (None, [], {}, "") or current.get(key) in (None, [], {}, ""):
                    merged[key] = value
            by_path = {item.get("path"): item for item in current.get("images", [])
                       if isinstance(item, dict) and item.get("path")}
            for item in brief.get("images", []):
                if isinstance(item, dict) and item.get("path"):
                    by_path[item["path"]] = item
            merged["images"] = list(by_path.values())
        merged["client"] = client
        merged["revision"] = current_revision + 1
        brief["revision"] = merged["revision"]
        atomic_write_json(path, merged)
        _write_context(client)
    return path


def _patch_brief(client, mutator):
    """Apply one mutation to the latest on-disk brief under the brief lock."""
    path = _brief_path(client)
    with FileLock(path + ".lock", timeout=30.0, stale_after=300.0):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle)
        else:
            current = {"client": client, "revision": 0, "images": []}
        revision = int(current.get("revision", 0))
        result = mutator(current)
        current["client"] = client
        current["revision"] = revision + 1
        atomic_write_json(path, current)
        _write_context(client)
        return result, current


def ingest_image(client, file_path, tag=""):
    """Clean an incoming image with gpt-image-2 before registering it.

    Raw files are never promoted to a usable/trusted asset by this entry point.
    The returned generated candidate remains pending until confirm-image.
    """
    if not os.path.isfile(file_path):
        raise SystemExit("file not found: %s" % file_path)
    # Do not register placeholders or arbitrary text files as product images.
    # The upload path is the first trust boundary for every later image-to-image step.
    detected_type = image_type(file_path)
    if detected_type not in {"png", "jpeg", "gif", "webp", "bmp", "tiff"}:
        raise SystemExit(
            "invalid image file: %s (请上传有效的 PNG/JPEG/WebP 图片)" % file_path
        )
    prompt = (
        "Clean and standardize this uploaded product image with gpt-image-2. "
        "Preserve the exact product identity, geometry, colors, materials, ports, controls, "
        "logos printed on the product and all distinctive details. Remove webpage UI, browser "
        "chrome, unrelated objects, compression artifacts and distracting background clutter. "
        "Create a clean commercial reference image of the same physical product; do not redesign, "
        "beautify into a different product, invent features, or add generated text."
    )
    return clean_image(client, file_path, prompt, tag=tag or "product")


_DEFAULT_ANALYZE_QUESTION = (
    "这是营销视频项目的产品素材图，请描述：产品类型、外观颜色材质、拍摄角度/构图、"
    "背景环境、画面里出现的关键卖点/参数文字（如有）、以及这张图适合作为哪种镜位"
    "（hero 主图/细节特写/场景图/包装图）的锚定素材。用简洁要点列出，不要虚构图中不存在的内容。"
)


def _session_api_key():
    """Resolve the host-neutral public session before reading its scoped key."""
    import key_setup
    key_setup.ensure_session_id()
    return key_setup.load_key()


def analyze_image(client, file_path_or_url, question=None, model=None):
    """用 BasicRouter 在线视觉模型分析一张素材图（客户自己的 key，走 br_client.analyze_image）。

    铁律对齐：本地宿主 Agent 不跑视觉模型，图像理解也不依赖宿主平台的内置视觉工具
    （这类工具通常需要宿主侧另配视觉供应商，客户机未必具备）。这里统一使用客户已经
    配好的 BasicRouter key，走 br_client.analyze_image()（与 video_reverse.py 的
    逆向分析同一套 /v1/chat/completions 多模态协议 + 在线视觉模型选型）。

    file_path_or_url: 本地文件路径（会先上传拿 hosted URL）或已可访问的图片 URL。
    返回: {"ok": True, "model": ..., "analysis": "<模型文字分析>"} 或 {"ok": False, "error": ...}。
    """
    api_key = _session_api_key()
    if not api_key:
        return {"ok": False, "error": "没有 BasicRouter key，先跑密钥准入闸门（key_setup.py gate）。"}
    q = question or _DEFAULT_ANALYZE_QUESTION
    try:
        text = br_client.analyze_image(api_key, file_path_or_url, q, model=model)
    except (br_client.BRError, OSError, ValueError, RuntimeError) as e:
        return {"ok": False, "error": "图片分析失败: %s" % e}
    return {"ok": True, "model": model or br_client.pick_vision_model(), "analysis": text}


def parse_doc(client, file_path):
    """Extract text + spec-like key:value pairs + image count from any supported doc.

    Supports .pptx .pdf .docx .doc .rtf .txt .md .xlsx .csv (see doc_extract).
    """
    import doc_extract
    if not os.path.isfile(file_path):
        raise SystemExit("file not found: %s" % file_path)
    d = _client_dir(client)
    dest = os.path.join(d, "ppt", os.path.basename(file_path))  # ppt/ holds all source docs
    if os.path.abspath(file_path) != os.path.abspath(dest):
        try:
            atomic_copy_file(file_path, dest)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    blocks = doc_extract.extract(dest)
    images = doc_extract.count_images(dest)
    texts, specs = [], {}
    for b in blocks:
        line = b["text"]
        texts.append({"slide": b.get("page", 1), "text": line})
        for sep in (":", "："):
            if sep in line and len(line) < 60:
                k, v = line.split(sep, 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    specs[k] = v
                break
    brief = _load_brief(client)
    brief["texts"] = brief.get("texts", []) + texts
    brief["specs"].update(specs)
    rel = os.path.relpath(dest, ROOT)
    if rel not in brief["ppt_files"]:
        brief["ppt_files"].append(rel)
    _save_brief(client, brief)
    return {"file": os.path.basename(dest), "text_lines": len(texts),
            "specs_found": len(specs), "embedded_images": images,
            "brief": _brief_path(client)}


# backward-compat alias
def parse_ppt(client, file_path):
    return parse_doc(client, file_path)


def set_profile(client, product_type=None, render_profile=None, style_hints=None):
    """Persist the LLM-judged product category + render/animation profile into brief.

    render_profile is a free-form dict the local model decides from product_type,
    e.g. {"visual":"科技快闪","motion":"粒子/电流光效,快切","palette_hint":"深色+品牌色",
          "pace":"快","font_feel":"无衬线粗体","video_style_prompt":"..."}.
    """
    brief = _load_brief(client)
    if product_type is not None:
        brief["product_type"] = product_type
    if render_profile is not None:
        brief["render_profile"] = render_profile
    if style_hints is not None:
        brief["style_hints"] = style_hints
    _save_brief(client, brief)
    return {"product_type": brief.get("product_type"),
            "render_profile": brief.get("render_profile"),
            "style_hints": brief.get("style_hints", [])}


def set_render_plan(client, plan):
    """Persist the client-chosen render + fusion plan into brief.

    plan is a free-form dict the LLM builds after advising the client and
    getting their pick, e.g. {"render_method":"videoType2图生+videoType4数字人",
    "fusion":"分段拼接","model":"kling-v3-omni-video","reason":"...","segments":[...]}.
    Recommendations are LLM-generated per client; this only stores the decision.
    """
    brief = _load_brief(client)
    brief["render_plan"] = plan
    _save_brief(client, brief)
    return {"render_plan": brief["render_plan"]}


def assess_assets(client, need_tags=None, segments=None):
    """素材完整性诊断：对照「成片所需镜位」检查现有素材图，报告缺口。

    成片方法论：每个镜位=图+文字→图生视频。所以每个需要出镜的镜位都必须有
    一张锚定图（产品图/场景图/数字人像）。本函数对照需求清单，报告：
      - have:    已有素材的镜位（tag→image 路径）
      - missing: 缺图的镜位（需引导用户上传或调 gen-image 生成）
      - orphan:  有图但没对应镜位的素材（可复用/可忽略）

    need_tags: 需求镜位 tag 列表，如 ["hero","detail","pack","scene"]。
    segments:  或直接传引导表 segments（含 image/image_role 字段）自动推导需求。
    返回 {complete: bool, have, missing, orphan, coverage}。
    """
    brief = _load_brief(client)

    def _usable(i):
        """可作锚点的图：明确 trusted_upload 的上传图或已 confirmed 的生成图。
        pending 候选（文生图 A/B 两版待客户选定确认）不算——铁律：只有 confirmed 才进出片。"""
        return bool(i.get("path") and is_confirmed(client, i.get("path")))

    have_imgs = {}       # 可用锚点：tag -> [path]
    pending_imgs = {}    # 待确认候选：tag -> [path]
    for i in brief.get("images", []):
        tag = i.get("tag") or ""
        if _usable(i):
            have_imgs.setdefault(tag, []).append(i.get("path"))
        elif i.get("status") == "pending":
            pending_imgs.setdefault(tag, []).append(i.get("path"))
    all_tags_have = set(t for t in have_imgs if t)

    # 推导需求：优先用显式 need_tags；否则从 segments 的 image_role 推
    needs = []
    if need_tags:
        needs = [{"tag": t, "reason": "需求镜位"} for t in need_tags]
    elif segments:
        for s in segments:
            role = s.get("image_role") or s.get("role") or ""
            has_img = bool(s.get("image") or s.get("urls"))
            needs.append({"tag": role, "reason": s.get("role", ""),
                          "seg_has_image": has_img, "id": s.get("id")})

    have, missing, pending = [], [], []
    for n in needs:
        tag = n.get("tag") or ""
        # 段内已带图 或 库里有同 tag 的【可用】图 → 视为满足
        satisfied = n.get("seg_has_image") or (tag and tag in all_tags_have)
        rec = {"tag": tag, "reason": n.get("reason", ""), "id": n.get("id")}
        if satisfied:
            rec["image"] = (have_imgs.get(tag, [None])[0]
                            if tag in all_tags_have else "(段内已指定)")
            have.append(rec)
        elif tag and tag in pending_imgs:
            # 有候选但还没确认 → 不算满足，提示去确认
            rec["candidates"] = pending_imgs[tag]
            pending.append(rec)
        else:
            missing.append(rec)

    needed_tags = set(n.get("tag") for n in needs if n.get("tag"))
    orphan = [{"tag": t, "images": have_imgs[t]}
              for t in all_tags_have if t not in needed_tags]

    total = len(needs) if needs else len(all_tags_have)
    covered = len(have)
    coverage = round(covered / total, 2) if total else 1.0
    # complete 要求：既无缺图，也无待确认候选（pending 图不能直接出片）
    return {"complete": len(missing) == 0 and len(pending) == 0,
            "coverage": coverage,
            "have": have, "missing": missing, "pending_confirmation": pending,
            "orphan": orphan, "total_needed": total, "covered": covered}


# 文生图精修（pass2）默认提示词：保持主体不变，只做画质/清晰度/瑕疵清洗
REFINE_PROMPT = ("保持画面主体、构图、配色完全不变，只做画质精修："
                 "提升清晰度与细节，修复瑕疵/噪点/畸形，边缘干净，商业级成品质感")


def _style_prompt(client, prompt):
    """拼接品牌风格前缀（若 brief 有 render_profile.video_style_prompt）。"""
    brief = _load_brief(client)
    rp = brief.get("render_profile") or {}
    style_prefix = rp.get("video_style_prompt") or ""
    return (style_prefix + " " + prompt).strip() if style_prefix else prompt


def _product_color_lock(client):
    """Return a factual, client-level color constraint for image cleanup."""
    brief = _load_brief(client)
    color = str(brief.get("product_color") or "").strip()
    if not color:
        return ""
    return (
        " PRODUCT COLOR LOCK: the physical product is %s. Preserve this exact color across every "
        "candidate and every angle. Neutral studio lighting may create natural highlights and shadows, "
        "but must not shift the body into white, black, silver, blue, gold, beige or any other hue. "
        "Do not recolor, tint, metallicize or brighten the product body. " % color
    )


def _create_one(k, prompt, image_urls, ratio, resolution, model):
    model, ratio, resolution = _validated_image_request_spec(
        model or "gpt-image-2", ratio, resolution)
    task_id = br_client.create_image_generation(
        k, prompt, model=model, count=1,
        resolution=resolution, ratio=ratio, image_urls=image_urls or [])
    print("[asset-prep] image task submitted: %s" % task_id, flush=True)
    urls = br_client.wait_image_generation(
        k, task_id, interval=5, max_wait=900,
        on_tick=_image_task_tick("asset image", task_id))
    if not urls:
        raise SystemExit("image generation returned no image")
    return urls[0]


def _validated_image_request_spec(model, ratio, resolution, *, model_specs=None):
    """Reject unsupported geometry before the paid async create request."""
    requested_model = str(model or "gpt-image-2").strip()
    specs = model_specs if model_specs is not None else br_client.list_image_models()
    selected = next((item for item in specs or [] if
                     str(item.get("id") or item.get("modelId") or "") == requested_model or
                     str(item.get("displayName") or item.get("modelName") or "") == requested_model),
                    None)
    if not selected:
        raise ValueError("IMAGE_MODEL_SPEC_UNAVAILABLE: %s" % requested_model)
    ratios = list(selected.get("ratios") or selected.get("ratio") or [])
    resolutions = list(selected.get("resolutions") or selected.get("resolution") or [])
    if ratio not in ratios:
        # Ratio changes composition and may not be silently rewritten.
        raise ValueError("IMAGE_RATIO_UNSUPPORTED_BEFORE_CREATE: %s/%s" %
                         (requested_model, ratio))
    if resolution not in resolutions:
        # Legacy video-style 1080p is safely normalized only to a published
        # image quality tier; all other unsupported values still block.
        if resolution == "1080p" and "2k" in resolutions:
            resolution = "2k"
        else:
            raise ValueError("IMAGE_RESOLUTION_UNSUPPORTED_BEFORE_CREATE: %s/%s" %
                             (requested_model, resolution))
    return requested_model, ratio, resolution


def _image_task_tick(kind, task_id):
    def tick(status, waited):
        if waited == 0:
            print("[asset-prep] %s task %s: %s" % (kind, task_id, status), flush=True)
        elif waited % 30 == 0:
            print("[asset-prep] %s task %s still processing (%ss)" %
                  (kind, task_id, waited), flush=True)
    return tick


def _save_image(client, url, tag, prompt, status="pending", variant=None,
                extra=None):
    """下载图 URL 到库并登记 brief。status=pending 表示待客户确认。"""
    import time as _t
    d = _client_dir(client)
    safe_tag = (tag or "gen").replace("/", "_").replace(" ", "_")
    suffix = ("_" + variant) if variant else ""
    dest = os.path.join(d, "images", "%s_%d%s.png" % (safe_tag, int(_t.time() * 1000) % 10**9, suffix))
    br_client.download(url, dest, allow_nonpublic_peer=True)
    rel = os.path.relpath(dest, ROOT)
    # brief 存相对路径（可移植）；返回给 agent 的 entry 额外带绝对路径，
    # 便于在支持 markdown 的 Agent 对话框中用 ![](abspath) 展示给客户预览。
    entry = {"path": rel, "tag": tag, "generated": True, "prompt": prompt,
             "status": status, "model": "gpt-image-2" if tag.startswith("product") else None}
    if variant:
        entry["variant"] = variant
    if extra:
        entry.update(extra)
    brief = _load_brief(client)
    brief["images"] = [i for i in brief["images"] if i.get("path") != rel] + [entry]
    _save_brief(client, brief)
    # 返回副本 + 绝对路径（不写进 brief，避免机器间路径失效）
    out = dict(entry)
    out["abspath"] = os.path.abspath(dest)
    return out


def clean_image(client, source, prompt, tag="product", ratio="1:1", resolution="2k"):
    """Run one async gpt-image-2 img2img cleanup and save it pending."""
    k = _session_api_key()
    if not k:
        raise SystemExit(ux.friendly_error("No API key. Run key onboarding first."))
    src = source if os.path.isfile(source) else os.path.join(ROOT, source)
    if not os.path.isfile(src):
        raise SystemExit("source not found: %s" % source)
    ref = br_client.to_image_ref(src)
    url = _create_one(
        k, _style_prompt(client, prompt) + _product_color_lock(client),
        [ref], ratio, resolution, "gpt-image-2")
    return _save_image(client, url, tag, prompt, status="pending",
                       extra={"source": os.path.relpath(src, ROOT),
                              "source_kind": "product_or_screenshot",
                              "processing_kind": "cleaned_img2img",
                              "via": "clean_image", "model": "gpt-image-2"})


def gen_image(client, prompt, tag="", ref=None, ratio="1:1", resolution="2k",
              model=None, refine=True):
    """补图生成（文生图/图生图）→ 产出 A/B 两个独立候选，由客户选用 A 还是 B。

    素材缺口补齐的核心：用户缺图时，不退回文生视频，而是**先生成素材图**再图生视频。
    两种模式：
      - 纯文生图（ref=None）：按 prompt 生成素材图。
      - 图生图（ref=已有图路径）：以现有产品图为基准衍生，保持一致性。

    **A/B 双版本（refine=True，默认）**：用同一 prompt（同一参考图）**独立生成两版**——
      版本 A(variant='a') 和版本 B(variant='b') 是平行候选、互不派生（不是 A 的精修得到 B），
      因模型采样随机性两版会有差异，供客户挑更满意的那一版。两版都存为 status='pending'，
      一并返回给客户看，由客户确认用 A 还是 B、或提修改项再精修（见 refine_image / confirm_image）。
      refine=False 时只出一版（快速/省 Credit）。

    model 走图像降级兜底（seedream-5.0 → kling-v3-omni-image）。
    返回 {tag, prompt, pass1, pass2, needs_confirmation}
      —— pass1=版本A，pass2=版本B（可能为 None：refine=False 或 B 版生成失败）。
    """
    k = _session_api_key()
    if not k:
        raise SystemExit(ux.friendly_error("No API key. Run key onboarding first."))

    image_urls = []
    if ref:
        src = ref if os.path.isfile(ref) else os.path.join(ROOT, ref)
        if not os.path.isfile(src):
            raise SystemExit("ref image not found: %s" % ref)
        image_urls = [br_client.to_image_ref(src)]

    full_prompt = _style_prompt(client, prompt)

    # ── 版本 A：首版 ────────────────────────────────────────────────────────
    url1 = _create_one(k, full_prompt, image_urls, ratio, resolution, model)
    pass1 = _save_image(client, url1, tag, prompt, status="pending",
                        variant="a" if refine else None,
                        extra={"variant_label": "A"})

    if not refine:
        return {"tag": tag, "prompt": prompt, "pass1": pass1, "pass2": None,
                "needs_confirmation": True}

    # ── 版本 B：同 prompt/同参考图独立再生成一版（平行候选，非 A 的精修）──────
    pass2 = None
    try:
        url2 = _create_one(k, full_prompt, image_urls, ratio, resolution, model)
        pass2 = _save_image(client, url2, tag, prompt, status="pending",
                            variant="b", extra={"variant_label": "B"})
    except (br_client.BRError, OSError, ValueError, RuntimeError) as e:
        # B 版生成失败不阻塞：保留 A 版让客户用/重试
        pass2 = None
        pass1["variant_b_error"] = str(e)

    return {"tag": tag, "prompt": prompt, "pass1": pass1, "pass2": pass2,
            "needs_confirmation": True}


def refine_image(client, rel_or_path, edit_prompt, ratio="1:1", resolution="2k",
                 model=None, feedback_ref=None):
    """客户提修改项后，对指定候选图做一次图生图精修，产出新的待确认版本。

    用于确认闸门里客户说「这版不错但把背景换成纯白 / logo 再大一点」的迭代。
    以选定图为参考，按 edit_prompt 精修，新版同样 status='pending' 待再确认。
    """
    k = _session_api_key()
    if not k:
        raise SystemExit("no API key; run key onboarding first")
    src = rel_or_path if os.path.isfile(rel_or_path) else os.path.join(ROOT, rel_or_path)
    if not os.path.isfile(src):
        raise SystemExit("image not found: %s" % rel_or_path)

    # 找原 entry 拿 tag
    brief = _load_brief(client)
    rel = os.path.relpath(src, ROOT)
    orig = next((i for i in brief.get("images", []) if i.get("path") == rel), {})
    tag = orig.get("tag", "")

    refs = [br_client.to_image_ref(src)]
    feedback_refs = ([feedback_ref] if isinstance(feedback_ref, str)
                     else list(feedback_ref or []))
    feedback_paths = []
    for index, feedback in enumerate(feedback_refs, 1):
        feedback_src = (feedback if os.path.isabs(feedback)
                        else os.path.join(ROOT, feedback))
        if not os.path.isfile(feedback_src):
            raise SystemExit("feedback image not found: %s" % feedback)
        refs.append(br_client.to_image_ref(feedback_src))
        feedback_paths.append(os.path.abspath(feedback_src))
    if feedback_paths:
        edit_prompt = (
            "参考后面的客户反馈图片/补充素材，理解并修正客户指出的问题；"
            "第一张候选图用于锁定主体身份，反馈图片只用于说明需要调整的细节。"
            "只修改客户明确指出的问题，保持未涉及的主体、产品外观、颜色、材质、比例、"
            "场景风格和构图不变；不要把反馈图片中的无关人物、文字、边框或界面复制进结果。"
            + edit_prompt
        )
    prompt = _style_prompt(client, edit_prompt)
    url = _create_one(k, prompt, refs, ratio, resolution, model)
    return _save_image(client, url, tag, edit_prompt, status="pending",
                       variant="edit", extra={"edited_from": rel,
                                               "edit_prompt": edit_prompt,
                                               "feedback_refs": feedback_paths} if feedback_paths else
                       {"edited_from": rel, "edit_prompt": edit_prompt})


def confirm_image(client, rel_or_path, discard_others=True,
                  customer_confirmation=None):
    """客户确认选定某版候选：标记 status='confirmed'，丢弃同 tag 的其它 pending 版本。

    确认后该图才是正式锚定素材，可进图生视频。discard_others=True 时把同 tag 的
    其它 pending/变体从 brief 移除并删文件（清理无用候选，省空间）。
    """
    images_root = os.path.join(_client_dir(client), "images")
    try:
        confirmation_text = run_manifest_store.require_explicit_customer_confirmation(
            customer_confirmation, "confirm-image")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    target_path = rel_or_path if os.path.isabs(rel_or_path) else os.path.join(ROOT, rel_or_path)
    require_contained_path(images_root, target_path, label="asset_image", must_exist=True)
    with open(target_path, "rb") as handle:
        target_sha256 = hashlib.sha256(handle.read()).hexdigest()
    removed = []
    candidates_to_delete = []

    def mutate(brief):
        target = None
        for item in brief.get("images", []):
            item_path = item.get("path")
            if not isinstance(item_path, str):
                continue
            item_abs = item_path if os.path.isabs(item_path) else os.path.join(ROOT, item_path)
            if os.path.realpath(item_abs) == os.path.realpath(target_path):
                target = item
                break
        if not target:
            raise SystemExit("image not in brief: %s" % rel_or_path)
        if (str(target.get("tag") or "").lower().startswith("product") and
                not (target.get("via") in {"standardize", "clean_image"} and
                     target.get("model", "") == "gpt-image-2")):
            raise SystemExit(
                "PRODUCT_CLEANUP_REQUIRED: 产品素材必须先经过 gpt-image-2 清洗，"
                "不能直接确认原始产品图。")
        target["status"] = "confirmed"
        target["sha256"] = target_sha256
        target["confirmed_by_customer"] = True
        target["customer_confirmation"] = confirmation_text
        target["confirmation_source"] = "customer_dialogue"
        target["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
        target.pop("variant", None)
        rel = target["path"]
        tag = target.get("tag", "")
        if discard_others:
            kept = []
            for item in brief.get("images", []):
                if (item.get("path") != rel and item.get("tag") == tag
                        and item.get("status") == "pending"):
                    fp = os.path.join(ROOT, item["path"])
                    try:
                        fp = require_contained_path(
                            images_root, fp, label="asset_candidate", must_exist=True)
                    except ValueError:
                        kept.append(item)
                        continue
                    candidates_to_delete.append(fp)
                    removed.append(item["path"])
                else:
                    kept.append(item)
            brief["images"] = kept
        return rel, tag

    (rel, tag), _brief = _patch_brief(client, mutate)
    # State is committed first; cleanup is best effort and cannot roll back confirmation.
    for candidate in candidates_to_delete:
        try:
            os.remove(candidate)
        except OSError:
            pass
    return {"confirmed": rel, "tag": tag, "discarded": removed,
            "sha256": target_sha256,
            "confirmed_by_customer": True,
            "customer_confirmation": confirmation_text}


_VIDEO_TEMPLATE_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}


def _extract_mid_frame(video_path):
    """从视频模板中间时刻抽一帧存为临时 png，作为标准化参考图。失败返回 None。"""
    import shutil
    import subprocess
    import tempfile
    ff, fp = media_tools.resolve_ffmpeg_pair()
    if not ff:
        return None
    duration = None
    if fp:
        r = subprocess.run(
            [fp, "-v", "quiet", "-print_format", "json", "-show_format", video_path],
            capture_output=True, text=True)
        try:
            duration = float(json.loads(r.stdout)["format"]["duration"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            duration = None
    fd, out = tempfile.mkstemp(suffix="_midframe.png")
    os.close(fd)
    os.unlink(out)
    cmd = [ff, "-hide_banner", "-y"]
    if duration:
        cmd += ["-ss", str(duration / 2)]
    cmd += ["-i", video_path, "-vframes", "1", out]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    return None


def standardize(client, source, prompt, tag="", model=None, ratio="1:1",
                resolution=None, count=1):
    """商品图 / 网页截图 / 视频模板帧 + 用户需求描述 → 标准化素材。

    调用方式对齐 BasicRouter 文档「图像生成」章节（异步 /v1/image-generations）：
    提交任务立即返回 taskId，轮询 GET /v1/image-generations/{taskId} 拿结果。
    gen_image()/clean/cutout 等素材生成入口也使用同一类异步提交+轮询，
    不依赖旧版同步长连接。

    source 支持三种输入形态：
      - 商品图 / 网页截图（图片文件）：直接作图生图参考图。
      - 视频模板（.mp4/.mov/.webm 等）：自动抽取中间帧作参考图（本地 ffmpeg，无模型）。
    prompt 是用户对"标准化"的具体需求描述（如"改成白底电商主图"/"把网页截图里的产品
    抠出来做成干净的产品图"/"按这个视频模板的构图风格生成一张同风格产品图"）。

    调用方式 = 图片(参考图) + 用户需求描述文字，与 BasicRouter 文档一致：imageUrls
    传参考图，text 传需求描述。resolution/ratio 先查 GET /v1/image-models 核对，
    仅接受该模型规格公布的值（不在列表内时自动回退到规格第一个可选值）。

    产出 status='pending'，复用现有 confirm-image/refine-image 确认闸门——标准化
    素材同样要客户确认后才能进出片，不因走了新接口就绕过确认闸门铁律。
    返回 {tag, prompt, model, task_id, candidates:[entry,...], needs_confirmation}。
    """
    k = _session_api_key()
    if not k:
        raise SystemExit("no API key; run key onboarding first")

    import urllib.parse
    is_remote = str(source).startswith(("http://", "https://"))
    tmp_source = None
    if is_remote:
        suffix = os.path.splitext(urllib.parse.urlparse(source).path)[1] or ".img"
        fd, tmp_source = tempfile.mkstemp(prefix="raw-source-", suffix=suffix)
        os.close(fd)
        try:
            br_client.download(source, tmp_source)
        except (br_client.BRError, OSError, ValueError, RuntimeError) as exc:
            if os.path.isfile(tmp_source):
                os.remove(tmp_source)
            raise SystemExit("REMOTE_SOURCE_DOWNLOAD_FAILED: %s" % exc) from exc
        src = tmp_source
    else:
        src = source if os.path.isfile(source) else os.path.join(ROOT, source)
        if not os.path.isfile(src):
            raise SystemExit("source not found: %s" % source)

    frame_path = src
    tmp_frame = None
    if os.path.splitext(src)[1].lower() in _VIDEO_TEMPLATE_EXTS:
        frame_path = _extract_mid_frame(src)
        tmp_frame = frame_path
        if not frame_path:
            raise SystemExit("无法从视频模板抽帧（缺 ffmpeg 或视频损坏）: %s" % source)

    # Demo/customer source material must be cleaned by gpt-image-2 before it
    # enters the asset library. Other models remain available only when an
    # explicit caller opts in for a non-demo specialized workflow.
    chosen_model = model or "gpt-image-2"
    if chosen_model != "gpt-image-2":
        raise SystemExit(
            "SOURCE_CLEANUP_MODEL_REQUIRED: 演示/客户原始素材必须先经过 gpt-image-2 清洗；"
            "不能用其它图像模型替代这一步。")
    req_ratio, req_resolution = _validated_image_request_spec(
        chosen_model, ratio, resolution)

    try:
        image_ref = br_client.to_image_ref(frame_path)
        full_prompt = _style_prompt(client, prompt) + _product_color_lock(client)
        task_id = br_client.create_image_generation(
            k, full_prompt, model=chosen_model, image_urls=[image_ref],
            count=count, resolution=req_resolution, ratio=req_ratio)
        urls = br_client.wait_image_generation(k, task_id, interval=5, max_wait=900)
    finally:
        if tmp_frame and os.path.isfile(tmp_frame):
            try:
                os.remove(tmp_frame)
            except OSError:
                pass
        if tmp_source and os.path.isfile(tmp_source):
            try:
                os.remove(tmp_source)
            except OSError:
                pass

    src_rel = source if is_remote else (os.path.relpath(src, ROOT) if os.path.isabs(src) else src)
    source_kind = "video_template_frame" if tmp_frame else "product_or_screenshot"
    candidates = []
    for i, url in enumerate(urls):
        variant = chr(ord("a") + i) if len(urls) > 1 else None
        entry = _save_image(
            client, url, tag, prompt, status="pending", variant=variant,
             extra={"source": src_rel, "source_kind": source_kind,
                    "processing_kind": "cleaned_img2img",
                    "via": "standardize", "model": "gpt-image-2"})
        candidates.append(entry)

    return {"tag": tag, "prompt": prompt, "model": chosen_model, "task_id": task_id,
            "candidates": candidates, "needs_confirmation": True}


def cutout(client, rel_or_path, prompt="remove background, clean product cutout on transparent/white"):
    """Background removal / scene fusion via BasicRouter image edit (img2img).

    Local files are sent as base64 data URLs (br_client.to_image_ref) — no host needed.
    Saves the result next to the source as <name>-cutout.png and registers it.
    """
    k = _session_api_key()
    if not k:
        raise SystemExit("no API key; run key onboarding first")
    src = rel_or_path if os.path.isfile(rel_or_path) else os.path.join(ROOT, rel_or_path)
    if not os.path.isfile(src):
        raise SystemExit("image not found: %s" % rel_or_path)
    ref = br_client.to_image_ref(src)
    url = _create_one(k, prompt, [ref], "1:1", "2k", "kling-v3-omni-image")
    base = os.path.splitext(os.path.basename(src))[0]
    dest = os.path.join(_client_dir(client), "images", base + "-cutout.png")
    br_client.download(url, dest, allow_nonpublic_peer=True)
    entry = {"path": os.path.relpath(dest, ROOT), "tag": "cutout",
             "generated": True, "status": "pending", "prompt": prompt}
    brief = _load_brief(client)
    brief["images"] = [i for i in brief["images"] if i.get("path") != entry["path"]] + [entry]
    _save_brief(client, brief)
    return entry


def main(argv):
    p = argparse.ArgumentParser(description="asset prep + product brief")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("ingest-image")
    pi.add_argument("--client", required=True)
    pi.add_argument("--file", required=True)
    pi.add_argument("--tag", default="")

    pcl = sub.add_parser("clean-image", help="用 gpt-image-2 清洗图片并生成待确认候选")
    pcl.add_argument("--client", required=True)
    pcl.add_argument("--file", required=True)
    pcl.add_argument("--tag", default="product")
    pcl.add_argument("--prompt", default=(
        "清洗并标准化这张产品图：保持真实产品外观、结构、颜色、材质、接口、控制件和原生印刷字不变；"
        "去除网页UI、浏览器边框、无关物体、压缩杂讯和杂乱背景；不要重新设计产品，不要添加生成文字。"))

    pai = sub.add_parser("analyze-image",
                         help="用 BasicRouter 在线视觉模型分析图片（走客户 key，不依赖宿主内置视觉工具）")
    pai.add_argument("--client", required=True)
    pai.add_argument("--file", required=True, help="本地图片路径或已可访问的图片 URL")
    pai.add_argument("--question", default=None, help="分析问题，缺省用产品素材分析默认问题")
    pai.add_argument("--model", default=None, help="视觉模型（缺省实时挑一个在线视觉模型）")

    pp = sub.add_parser("parse-doc")   # .pptx/.pdf/.docx/.doc/.rtf/.txt/.md/.xlsx/.csv
    pp.add_argument("--client", required=True)
    pp.add_argument("--file", required=True)

    pp2 = sub.add_parser("parse-ppt")  # backward-compat alias
    pp2.add_argument("--client", required=True)
    pp2.add_argument("--file", required=True)

    pb = sub.add_parser("brief")
    pb.add_argument("--client", required=True)

    pf = sub.add_parser("set-profile")   # LLM writes judged category + render profile
    pf.add_argument("--client", required=True)
    pf.add_argument("--product-type", dest="product_type")
    pf.add_argument("--render-profile", dest="render_profile",
                    help="JSON string of the render/animation profile")
    pf.add_argument("--style-hints", dest="style_hints", nargs="*")

    pr = sub.add_parser("set-render-plan")  # LLM stores client-chosen render+fusion plan
    pr.add_argument("--client", required=True)
    pr.add_argument("--plan", required=True, help="JSON string of the chosen render+fusion plan")

    pc = sub.add_parser("cutout")
    pc.add_argument("--client", required=True)
    pc.add_argument("--file", required=True)

    ps = sub.add_parser("standardize",
                        help="商品图/网页截图/视频模板帧 + 需求描述 → 标准化素材(异步 /v1/image-generations)")
    ps.add_argument("--client", required=True)
    ps.add_argument("--source", required=True,
                    help="商品图/网页截图路径，或视频模板路径(.mp4/.mov/.webm 等，自动抽中间帧)")
    ps.add_argument("--prompt", required=True, help="标准化需求描述")
    ps.add_argument("--tag", default="", help="镜位 tag，如 hero/detail")
    ps.add_argument("--model", default=None, help="图像模型（默认 gpt-image-2）")
    ps.add_argument("--ratio", default="1:1")
    ps.add_argument("--resolution", default=None)
    ps.add_argument("--count", type=int, default=1)

    pa = sub.add_parser("assess", help="素材完整性诊断：报告缺口镜位")
    pa.add_argument("--client", required=True)
    pa.add_argument("--need-tags", dest="need_tags", nargs="*",
                    help="需求镜位 tag，如 hero detail pack scene")
    pa.add_argument("--segments-file", dest="segments_file",
                    help="引导表 segments/rows JSON 文件，自动推导需求")

    pg = sub.add_parser("gen-image", help="补图生成(文生图，产出A/B两版)：产出待客户确认候选")
    pg.add_argument("--client", required=True)
    pg.add_argument("--prompt", required=True, help="素材图生成提示词")
    pg.add_argument("--tag", default="", help="镜位 tag，如 hero/scene")
    pg.add_argument("--ref", default=None, help="参考图路径（图生图，保持产品一致性）")
    pg.add_argument("--ratio", default="1:1")
    pg.add_argument("--resolution", default="2k")
    pg.add_argument("--model", default=None, help="图像模型（默认走降级兜底）")
    pg.add_argument("--no-refine", dest="no_refine", action="store_true",
                    help="只出一版（不出A/B两版供选，快速/省 Credit）")

    prf = sub.add_parser("refine-image", help="客户提修改项后对某候选图再精修一版")
    prf.add_argument("--client", required=True)
    prf.add_argument("--file", required=True, help="要精修的候选图路径")
    prf.add_argument("--edit", required=True, help="修改项描述，如 '背景换纯白/logo放大'")
    prf.add_argument("--ratio", default="1:1")
    prf.add_argument("--resolution", default="2k")
    prf.add_argument("--model", default=None)
    prf.add_argument("--feedback-ref", nargs="+", default=None,
                     help="用户上传的反馈图片/补充素材，可传一张或多张；不会直接成为成片素材")

    pcf = sub.add_parser("confirm-image", help="客户确认选定某版候选(丢弃其它)")
    pcf.add_argument("--client", required=True)
    pcf.add_argument("--file", required=True, help="客户选定的候选图路径")
    pcf.add_argument("--keep-others", dest="keep_others", action="store_true",
                     help="保留其它候选(默认删除同 tag 的其它 pending 版本)")
    pcf.add_argument("--customer-confirmation", required=True,
                     help="客户明确选择该候选图的原话")

    args = p.parse_args(argv)
    if args.cmd == "ingest-image":
        print(json.dumps(ingest_image(args.client, args.file, args.tag), ensure_ascii=False, indent=2))
    elif args.cmd == "clean-image":
        print(json.dumps(clean_image(args.client, args.file, args.prompt, tag=args.tag),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "analyze-image":
        print(json.dumps(analyze_image(args.client, args.file, question=args.question,
                                       model=args.model),
                         ensure_ascii=False, indent=2))
    elif args.cmd in ("parse-doc", "parse-ppt"):
        print(json.dumps(parse_doc(args.client, args.file), ensure_ascii=False, indent=2))
    elif args.cmd == "brief":
        print(json.dumps(_load_brief(args.client), ensure_ascii=False, indent=2))
    elif args.cmd == "set-profile":
        rp = json.loads(args.render_profile) if args.render_profile else None
        print(json.dumps(set_profile(args.client, product_type=args.product_type,
                                     render_profile=rp, style_hints=args.style_hints),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "set-render-plan":
        plan = json.loads(args.plan)
        print(json.dumps(set_render_plan(args.client, plan), ensure_ascii=False, indent=2))
    elif args.cmd == "cutout":
        cutout(args.client, args.file)
    elif args.cmd == "standardize":
        print(json.dumps(standardize(args.client, args.source, args.prompt,
                                     tag=args.tag, model=args.model,
                                     ratio=args.ratio, resolution=args.resolution,
                                     count=args.count),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "assess":
        segs = None
        if args.segments_file:
            with open(args.segments_file, encoding="utf-8") as f:
                data = json.load(f)
            # 支持传引导表(含 rows) 或 直接 segments 列表
            segs = data.get("rows") if isinstance(data, dict) else data
        print(json.dumps(assess_assets(args.client, need_tags=args.need_tags,
                                       segments=segs), ensure_ascii=False, indent=2))
    elif args.cmd == "gen-image":
        print(json.dumps(gen_image(args.client, args.prompt, tag=args.tag,
                                   ref=args.ref, ratio=args.ratio,
                                   resolution=args.resolution, model=args.model,
                                   refine=not args.no_refine),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "refine-image":
        print(json.dumps(refine_image(args.client, args.file, args.edit,
                                      ratio=args.ratio, resolution=args.resolution,
                                      model=args.model, feedback_ref=args.feedback_ref),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "confirm-image":
        print(json.dumps(confirm_image(args.client, args.file,
                                       discard_others=not args.keep_others,
                                       customer_confirmation=args.customer_confirmation),
                         ensure_ascii=False, indent=2))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
