#!/usr/bin/env python3
"""Generate a confirmed 3x3 product reference board for Seedance shots."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import br_client  # noqa: E402
import key_setup  # noqa: E402
import product_library  # noqa: E402
from project_utils import atomic_write_json  # noqa: E402


DEFAULT_MODEL = "gpt-image-2"
IMAGE_MAX_WAIT = 900
IMAGE_RETRIES = 2
PRODUCT_BOARD_POLICY_VERSION = "product-board-policy-v2-identity-contract"


def _sha256_file(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_fingerprint(reference_urls):
    """Bind a board candidate to the exact source images used to create it."""
    digest = hashlib.sha256()
    for value in reference_urls or []:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _first(meta, *keys):
    for key in keys:
        value = meta.get(key)
        if value:
            return value
    return ""


def product_identity_contract(meta):
    """Normalize product facts into a generation-time identity contract."""
    meta = meta or {}
    distinctive = _first(
        meta, "distinctive_details", "visible_details", "key_visual_features",
        "features", "usps", "specs")
    forbidden = _first(meta, "forbidden_variants", "forbidden", "negative_constraints")
    return {
        "policy_version": PRODUCT_BOARD_POLICY_VERSION,
        "product_name": _first(meta, "product_name", "name", "sku"),
        "product_type": _first(meta, "product_type", "category"),
        "model": _first(meta, "product_model", "model", "sku"),
        "color": _first(meta, "product_color", "color"),
        "material_finish": _first(meta, "material", "materials", "finish", "surface_finish"),
        "silhouette_proportions": _first(
            meta, "silhouette", "shape", "dimensions", "size", "scale"),
        "controls_buttons": _first(meta, "buttons", "controls", "control_layout"),
        "ports_connectors": _first(meta, "ports", "connectors", "interfaces"),
        "markings_logo": _first(meta, "logo", "markings", "labels"),
        "functional_surfaces": _first(
            meta, "functional_surfaces", "contact_surfaces", "magnetic_surface",
            "speaker_grille", "wearing_surface"),
        "distinctive_details": distinctive,
        "forbidden_variants": forbidden,
    }


def product_identity_contract_text(meta):
    contract = product_identity_contract(meta)
    lines = [
        "[PRODUCT IDENTITY CONTRACT / 产品身份合同]",
        "Before generating any view, resolve the uploaded references into a fixed physical product specification. This contract is mandatory and overrides generic product-category guesses.",
        "- Product name/model: %s %s" % (contract.get("product_name") or "", contract.get("model") or ""),
        "- Category: %s" % (contract.get("product_type") or "same as uploaded reference"),
        "- Color: %s" % (contract.get("color") or "exact uploaded color, no hue shift"),
        "- Material/finish: %s" % (contract.get("material_finish") or "exact uploaded material and finish"),
        "- Silhouette/proportions/size: %s" % (contract.get("silhouette_proportions") or "exact silhouette, thickness, curvature, aspect ratio and scale cues from references"),
        "- Controls/buttons: %s" % (contract.get("controls_buttons") or "all visible controls/buttons copied exactly from references"),
        "- Ports/connectors/openings: %s" % (contract.get("ports_connectors") or "all visible ports, connectors, holes and openings copied exactly"),
        "- Markings/logo/native labels: %s" % (contract.get("markings_logo") or "preserve only markings physically present in the uploaded reference"),
        "- Functional/contact surfaces: %s" % (contract.get("functional_surfaces") or "preserve any visible functional, magnetic, wearing, mounting or connection surfaces"),
        "- Distinctive details: %s" % (json.dumps(contract.get("distinctive_details"), ensure_ascii=False) if contract.get("distinctive_details") else "copy every distinctive seam, grille pattern, bevel, curvature and part boundary from the uploaded reference"),
        "- Forbidden variants: %s" % (json.dumps(contract.get("forbidden_variants"), ensure_ascii=False) if contract.get("forbidden_variants") else "no category swap, no generic accessory, no simplified silhouette, no changed color/material/buttons/ports/logo/scale"),
        "Nine panels are invalid if any panel changes category, color family, material finish, button count/layout, grille/opening pattern, connector/port position, logo/marking placement, thickness/volume, or relative size.",
        "中文：产品图先锁定品类、外轮廓、比例大小、颜色、材质、按钮/孔位/接口/Logo、功能/接触面和独特细节；任何一格改变这些身份信息都视为无效。",
    ]
    return "\n".join(lines) + "\n"


def product_board_source_fingerprint(reference_urls, meta):
    return reference_fingerprint(
        list(reference_urls or [])[:4] + [
            PRODUCT_BOARD_POLICY_VERSION,
            json.dumps(product_identity_contract(meta), ensure_ascii=False,
                       sort_keys=True, default=str),
        ])


def _state_path(out_dir):
    return os.path.join(out_dir, "product_board_state.json")


def _load_state(out_dir):
    path = _state_path(out_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_state(out_dir, state):
    path = _state_path(out_dir)
    atomic_write_json(path, state)


def _result_handoff_fields(state):
    fields = {}
    if not isinstance(state, dict):
        return fields
    if state.get("result_url"):
        fields["url"] = state["result_url"]
        fields["result_url"] = state["result_url"]
    for key in ("task_id", "request_id", "board_sha256",
                "product_board_policy_version", "product_identity_contract"):
        if state.get(key):
            fields[key] = state[key]
    return fields


def _progress_tick(kind, task_id):
    def tick(status, waited):
        if waited == 0:
            print("[gpt-image-2] %s task %s: %s" % (kind, task_id, status), flush=True)
        elif waited % 30 == 0:
            print("[gpt-image-2] %s task %s still processing (%ss)" %
                  (kind, task_id, waited), flush=True)
    return tick


def product_board_prompt(meta):
    product_type = meta.get("product_type") or "the exact product"
    product_color = meta.get("product_color") or ""
    style = meta.get("style_hint") or "commercial product reference photography"
    color_lock = (" PRODUCT COLOR LOCK: the exact product body is %s. Preserve this color in all nine panels; "
                  "natural highlights and shadows are allowed, but do not recolor or shift the product into another hue."
                  % product_color) if product_color else ""
    return (
        "Create one 16:9 landscape 3x3 product consistency reference board for %s. "
        "%s"
        "SOURCE-LOCK: the uploaded reference images are the sole authoritative identity source. "
        "Reconstruct new camera views of that exact physical object; do not substitute a generic item, "
        "redesign it, simplify it, or infer a different product from the text description. "
        "The exact same single product must appear in all nine panels with identical geometry, "
        "materials, colors, proportions, markings and distinctive details. "
        "Panel order: 1 front hero, 2 front-left 45 degree, 3 front-right 45 degree, "
        "4 strict left side, 5 strict right side, 6 rear, 7 top/controls, "
         "8 macro material/detail, 9 bottom/connection/detail. "
        "Use clean neutral backgrounds, controlled studio light except panel 9, sharp silhouette, "
        "Preserve every visible hinge, opening, control, seam, grille, connector, surface transition, "
        "case shape and color boundary from the references. Any product-specific housing, magnetic surface, "
        "charging port, button, display, hinge, connector or control geometry must match the uploaded "
        "reference exactly; do not invent a different product, generic accessory housing, alternate port layout "
        "or unrelated product parts. "
        "No extra products, no invented accessories, no readable generated text, no watermark or logo. "
         "All nine panels show only the product itself; no person, hand, usage scene or lifestyle context. "
        "This is a product identity board, not an advertisement poster. Style: %s.%s" %
        (product_type, product_identity_contract_text(meta), style, color_lock)
    )


def generate(client, sku, *, model=DEFAULT_MODEL, out_dir=None):
    key = key_setup.load_key()
    if not key:
        raise br_client.BRError("No API key. Run key onboarding first.")
    meta = product_library._load_meta(client, sku)
    resolved = product_library.resolve(client, sku)
    hero = resolved.get("hero")
    if not hero or not os.path.isfile(hero):
        raise br_client.BRError(
            "产品缺少具备当前哈希凭证的正式主图，请重新导入真实产品图或确认生成候选。")
    out_dir = out_dir or product_library._sku_dir(client, sku)
    os.makedirs(out_dir, exist_ok=True)
    source_paths = [hero]
    source_paths.extend(resolved.get("refs") or [])
    source_paths = list(dict.fromkeys(source_paths))[:4]
    reference_urls = [br_client.to_image_ref(path) for path in source_paths]
    source_fingerprint = product_library.source_fingerprint(client, sku)
    pending = os.path.join(out_dir, "product_board_pending.png")
    state = _load_state(out_dir)
    reusable = (os.path.isfile(pending) and os.path.getsize(pending) > 0 and
                state.get("source_fingerprint") == source_fingerprint and
                state.get("model") == model and
                state.get("board_sha256") == _sha256_file(pending))
    resumable_download = (not reusable and state.get("status") == "download_pending" and
                          state.get("source_fingerprint") == source_fingerprint and
                          state.get("model") == model and state.get("result_url"))
    if resumable_download:
        result_url = state["result_url"]
        br_client.download(result_url, pending, allow_nonpublic_peer=True)
        state.update({"status": "pending", "board_sha256": _sha256_file(pending)})
        _save_state(out_dir, state)
        reusable = True
    if not reusable:
        last_error = None
        result_url = None
        request_id = "product-board-" + reference_fingerprint(
            [source_fingerprint, model, "16:9"])
        for attempt in range(IMAGE_RETRIES + 1):
            try:
                task_id = br_client.create_image_generation(
                    key, product_board_prompt(meta), model=model, count=1,
                    ratio="16:9", resolution="2k", image_urls=reference_urls,
                    request_id=request_id)
                print("[gpt-image-2] product board submitted: %s" % task_id, flush=True)
                urls = br_client.wait_image_generation(
                    key, task_id, interval=5, max_wait=IMAGE_MAX_WAIT,
                    on_tick=_progress_tick("product board", task_id))
                if not urls:
                    raise br_client.BRError("产品九宫格生成未返回图片。")
                result_url = urls[0]
                _save_state(out_dir, {
                    "status": "download_pending", "model": model,
                    "source_fingerprint": source_fingerprint,
                    "source_manifest": product_library.source_manifest(client, sku),
                    "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
                    "product_identity_contract": product_identity_contract(meta),
                    "reference_count": len(reference_urls),
                    "task_id": task_id,
                    "request_id": request_id,
                    "result_url": result_url,
                })
                br_client.download(result_url, pending, allow_nonpublic_peer=True)
                _save_state(out_dir, {
                    "status": "pending", "model": model,
                    "source_fingerprint": source_fingerprint,
                    "source_manifest": product_library.source_manifest(client, sku),
                    "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
                    "product_identity_contract": product_identity_contract(meta),
                    "reference_count": len(reference_urls),
                    "task_id": task_id,
                    "request_id": request_id,
                    "result_url": result_url,
                    "board_sha256": _sha256_file(pending),
                })
                break
            except (br_client.BRError, OSError, ValueError, RuntimeError) as exc:
                last_error = exc
                if result_url:
                    raise
                if attempt < IMAGE_RETRIES:
                    time.sleep(5 * (attempt + 1))
        else:
            raise last_error
    record = {"client": client, "sku": sku, "model": model, "status": "pending",
              "path": os.path.relpath(pending, ROOT), "prompt": product_board_prompt(meta),
              "source_fingerprint": source_fingerprint, "skipped": reusable,
              "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
              "product_identity_contract": product_identity_contract(meta)}
    record.update(_result_handoff_fields(_load_state(out_dir)))
    atomic_write_json(os.path.join(out_dir, "product_board.json"), record)
    return record


def generate_from_reference_urls(api_key, reference_urls, out_dir, *, product_type="the exact product",
                                 product_color="", style_hint="commercial product reference photography",
                                 model=DEFAULT_MODEL, product_meta=None):
    """Generate a pending product board directly from uploaded plan references."""
    if not reference_urls:
        raise br_client.BRError("产品板至少需要一张用户上传的产品参考图。")
    os.makedirs(out_dir, exist_ok=True)
    meta = dict(product_meta or {})
    meta.update({k: v for k, v in {
        "product_type": product_type,
        "product_color": product_color,
        "style_hint": style_hint,
    }.items() if v and not meta.get(k)})
    source_fingerprint = product_board_source_fingerprint(reference_urls[:4], meta)
    pending = os.path.join(out_dir, "product_board_pending.jpg")
    state = _load_state(out_dir)
    reusable = (os.path.isfile(pending) and os.path.getsize(pending) > 0 and
                state.get("source_fingerprint") == source_fingerprint and
                state.get("model") == model and
                state.get("board_sha256") == _sha256_file(pending))
    resumable_download = (not reusable and state.get("status") == "download_pending" and
                          state.get("source_fingerprint") == source_fingerprint and
                          state.get("model") == model and state.get("result_url"))
    if resumable_download:
        result_url = state["result_url"]
        br_client.download(result_url, pending, allow_nonpublic_peer=True)
        state.update({"status": "pending", "board_sha256": _sha256_file(pending)})
        _save_state(out_dir, state)
        reusable = True
    if reusable:
        result = {"status": "pending", "model": model, "path": os.path.abspath(pending),
                  "reference_count": len(reference_urls[:4]), "skipped": True,
                  "source_fingerprint": source_fingerprint,
                  "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
                  "product_identity_contract": product_identity_contract(meta)}
        result.update(_result_handoff_fields(_load_state(out_dir)))
        return result
    last_error = None
    result_url = None
    request_id = "product-board-" + reference_fingerprint(
        [source_fingerprint, model, "16:9"])
    for attempt in range(IMAGE_RETRIES + 1):
        try:
            task_id = br_client.create_image_generation(
                api_key, product_board_prompt(meta), model=model, count=1,
                ratio="16:9", resolution="2k", image_urls=reference_urls[:4],
                request_id=request_id)
            print("[gpt-image-2] product board submitted: %s" % task_id, flush=True)
            urls = br_client.wait_image_generation(
                api_key, task_id, interval=5, max_wait=IMAGE_MAX_WAIT,
                on_tick=_progress_tick("product board", task_id))
            if not urls:
                raise br_client.BRError("产品九宫格生成未返回图片。")
            result_url = urls[0]
            _save_state(out_dir, {
                "status": "download_pending", "model": model,
                "source_fingerprint": source_fingerprint,
                "reference_count": len(reference_urls[:4]),
                "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
                "product_identity_contract": product_identity_contract(meta),
                "task_id": task_id,
                "request_id": request_id,
                "result_url": result_url,
            })
            br_client.download(result_url, pending, allow_nonpublic_peer=True)
            _save_state(out_dir, {
                "status": "pending", "model": model,
                "source_fingerprint": source_fingerprint,
                "reference_count": len(reference_urls[:4]),
                "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
                "product_identity_contract": product_identity_contract(meta),
                "task_id": task_id,
                "request_id": request_id,
                "result_url": result_url,
                "board_sha256": _sha256_file(pending),
            })
            break
        except (br_client.BRError, OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            if result_url:
                raise
            if attempt < IMAGE_RETRIES:
                print("[gpt-image-2] product board retrying: %s" % exc, flush=True)
                time.sleep(5 * (attempt + 1))
    else:
        raise last_error
    result = {"status": "pending", "model": model, "path": os.path.abspath(pending),
              "reference_count": len(reference_urls[:4]), "prompt": product_board_prompt(meta),
              "source_fingerprint": source_fingerprint,
              "product_board_policy_version": PRODUCT_BOARD_POLICY_VERSION,
              "product_identity_contract": product_identity_contract(meta)}
    result.update(_result_handoff_fields(_load_state(out_dir)))
    return result


def confirm(client, sku, customer_confirmation=None):
    customer_confirmation = str(customer_confirmation or "").strip()
    if not customer_confirmation:
        raise SystemExit("CUSTOMER_CONFIRMATION_REQUIRED")
    directory = product_library._sku_dir(client, sku)
    pending = os.path.join(directory, "product_board_pending.png")
    confirmed = os.path.join(directory, "product_board.png")
    if not os.path.isfile(pending):
        raise SystemExit("未找到待确认产品九宫格: %s" % pending)
    state = _load_state(directory)
    current_fingerprint = product_library.source_fingerprint(client, sku)
    if not product_library.source_manifest(client, sku):
        state.update({"status": "stale",
                      "current_source_fingerprint": current_fingerprint})
        _save_state(directory, state)
        raise SystemExit("PRODUCT_SOURCE_EVIDENCE_REQUIRED")
    if state.get("source_fingerprint") != current_fingerprint:
        state.update({"status": "stale", "current_source_fingerprint": current_fingerprint})
        _save_state(directory, state)
        raise SystemExit("产品源图内容已变化，待确认产品板已失效，请重新生成。")
    pending_sha256 = product_library.file_sha256(pending)
    if state.get("board_sha256") != pending_sha256:
        raise SystemExit("PRODUCT_BOARD_PENDING_HASH_MISMATCH")
    record_path = os.path.join(directory, "product_board.json")
    record_existed = os.path.isfile(record_path)
    if record_existed:
        try:
            with open(record_path, encoding="utf-8") as handle:
                old_record = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            old_record = {}
    else:
        old_record = {}
    old_state = dict(state)
    backup = None
    if os.path.isfile(confirmed):
        fd, backup = tempfile.mkstemp(prefix=".product-board.previous-",
                                      suffix=".png", dir=directory)
        os.close(fd)
        os.unlink(backup)
        os.replace(confirmed, backup)
    confirmed_at = datetime.now(timezone.utc).isoformat()
    evidence = {
        "confirmed_by_customer": True,
        "customer_confirmation": customer_confirmation,
        "confirmation_source": "customer_dialogue",
        "confirmed_at": confirmed_at,
    }
    committed = False
    try:
        os.replace(pending, confirmed)
        board_sha256 = product_library.file_sha256(confirmed)
        state.update({
            "status": "confirmed",
            "source_fingerprint": current_fingerprint,
            "source_manifest": product_library.source_manifest(client, sku),
            "board_sha256": board_sha256,
            **evidence,
        })
        _save_state(directory, state)
        record = dict(old_record)
        record.update({
            "status": "confirmed",
            "path": os.path.relpath(confirmed, ROOT),
            "source_fingerprint": current_fingerprint,
            "board_sha256": board_sha256,
            **evidence,
        })
        atomic_write_json(record_path, record)
        committed = True
    finally:
        if not committed:
            if os.path.isfile(confirmed):
                os.replace(confirmed, pending)
            if backup and os.path.isfile(backup):
                os.replace(backup, confirmed)
            _save_state(directory, old_state)
            if record_existed:
                atomic_write_json(record_path, old_record)
            elif os.path.isfile(record_path):
                os.unlink(record_path)
    if backup and os.path.isfile(backup):
        os.unlink(backup)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Product 3x3 consistency board")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--client", required=True)
    gen.add_argument("--sku", required=True)
    gen.add_argument("--model", default=DEFAULT_MODEL)
    con = sub.add_parser("confirm")
    con.add_argument("--client", required=True)
    con.add_argument("--sku", required=True)
    con.add_argument("--customer-confirmation", required=True,
                     help="客户看过产品板后确认的原话")
    args = parser.parse_args(argv)
    result = (generate(args.client, args.sku, model=args.model)
              if args.command == "generate"
              else confirm(args.client, args.sku, args.customer_confirmation))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
