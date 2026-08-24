#!/usr/bin/env python3
"""Pure canonical contract for a paid identity-plate image request.

The customer-facing capture packet, the async create call and failure recovery
must all derive from this module.  Keeping the exact body and its compact audit
identity together prevents a later retry from rewriting historical request
parameters or reference bytes.
"""
import hashlib
import json
import os

import br_client


def json_sha256(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_bundle(source_references, *, include_data_urls=False):
    identities, data_urls = [], []
    for item in source_references or []:
        path = os.path.abspath(str(item.get("path") or ""))
        if not os.path.isfile(path):
            raise ValueError("IDENTITY_IMAGE_REFERENCE_MISSING: %s" % path)
        source_sha = file_sha256(path)
        if source_sha != item.get("sha256"):
            raise ValueError("IDENTITY_IMAGE_REFERENCE_STALE")
        data_url = br_client.to_image_ref(path)
        identities.append({
            "transport": "data_url_from_local_file",
            "source_path": path,
            "source_sha256": source_sha,
            "data_url_sha256": hashlib.sha256(
                data_url.encode("utf-8")).hexdigest(),
        })
        data_urls.append(data_url)
    if not identities:
        raise ValueError("IDENTITY_IMAGE_REFERENCE_REQUIRED")
    return (identities, data_urls) if include_data_urls else identities


def submission_request_spec(request, *, resolution=None):
    return {
        "model": request.get("model"),
        "ratio": request.get("ratio"),
        "resolution": resolution or request.get("resolution"),
        "prompt_zh": request.get("prompt_zh"),
        "image_references": reference_bundle(request.get("source_references")),
    }


def submission_payload(request):
    _, image_urls = reference_bundle(
        request.get("source_references"), include_data_urls=True)
    return {
        "model": request.get("model"),
        "text": request.get("prompt_zh"),
        "imageUrls": image_urls,
        "count": 1,
        "resolution": request.get("resolution"),
        "ratio": request.get("ratio"),
    }


def catalog_contract(request, catalog):
    selected = next((item for item in catalog or [] if
                     str(item.get("id") or item.get("modelId") or "") ==
                     str(request.get("model") or "")), None)
    if not selected:
        raise ValueError("IDENTITY_CAPTURE_MODEL_NOT_IN_LIVE_CATALOG")
    ratios = list(selected.get("ratios") or selected.get("ratio") or [])
    resolutions = list(
        selected.get("resolutions") or selected.get("resolution") or [])
    if request.get("ratio") not in ratios:
        raise ValueError("IDENTITY_CAPTURE_RATIO_NOT_IN_LIVE_CATALOG")
    if request.get("resolution") not in resolutions:
        raise ValueError("IDENTITY_CAPTURE_RESOLUTION_NOT_IN_LIVE_CATALOG")
    contract = {
        "model": request.get("model"),
        "selected_ratio": request.get("ratio"),
        "selected_resolution": request.get("resolution"),
        "published_ratios": ratios,
        "published_resolutions": resolutions,
        "selected_model_spec": selected,
    }
    contract["fingerprint"] = json_sha256(contract)
    return contract


def assert_request_matches_plan(request):
    spec = submission_request_spec(request)
    fingerprint = json_sha256(spec)
    if request.get("planned_submission_request_spec") != spec:
        raise ValueError("IDENTITY_REQUEST_SPEC_DIVERGED_FROM_APPROVED_PLAN")
    if request.get("planned_submission_request_fingerprint") != fingerprint:
        raise ValueError("IDENTITY_REQUEST_FINGERPRINT_DIVERGED_FROM_APPROVED_PLAN")
    return spec, fingerprint
