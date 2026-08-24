#!/usr/bin/env python3
"""Video model selection and capability-aware fallback chain.

Extracted from video_engine.py (v3 split). Contains:
  - Model catalog normalization and resolution
  - Capability-aware model picking (allowVideoType from gateway)
  - Fallback chain constants (VIDEO_MODEL_FALLBACK, VIDEO_MODEL_CAPS, etc.)
  - Image model selection

Dependencies: br_client (for live model catalog queries)
"""
import os
import sys
import json
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import br_client

# ── Constants ───────────────────────────────────────────────────────────

DEFAULT_MODEL = "seedance-2.0"

# Model fallback chain: seedance (fast/cheap) → kling (only type4 support) → wan
VIDEO_MODEL_FALLBACK = [
    "seedance-2.0",
    "seedance-2.5-white",
    "kling-v3-omni-video",
    "kling-v3-omni-video-white",
    "wan2.7-i2v",
]

# Offline capability table (only used when gateway is unreachable)
VIDEO_MODEL_CAPS = {
    "seedance-2.0": {1, 2, 3, 5},
    "seedance-2.5-white": {1, 2, 3, 4, 5},
    "kling-v3-omni-video": {1, 2, 3, 4, 5},
    "kling-v3-omni-video-white": {1, 2, 3, 4, 5},
    "wan2.7-i2v": {1, 2},
}

VIDEO_MODEL_INTEGRATED_AUDIO = {
    "seedance-2.0": True,
    "kling-v3-omni-video": True,
    "kling-v3-omni": True,
    # A formal paid canary on this exact BasicRouter route returned an MP4
    # with zero audio streams.  Provider-family marketing claims cannot
    # override route-level media evidence; keep it ineligible for dialogue
    # until a later audited route change supplies positive output-audio proof.
    "kling-v3-omni-video-white": False,
    "wan2.7-i2v": False,
}

VIDEO_MODEL_INTEGRATED_AUDIO_EVIDENCE = {}

IMAGE_MODEL_FALLBACK = [
    "seedream-5.0",
    "nano banana pro",
    "imagen 4 ultra",
    "kling-v3-omni-image",
]


# ── Catalog helpers ─────────────────────────────────────────────────────

def _catalog_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "online", "active", "enabled", "available"}:
            return True
        if normalized in {"0", "false", "no", "off", "offline", "inactive", "disabled", "unavailable"}:
            return False
    return None


def _catalog_list(value, *, integers=False):
    if isinstance(value, str):
        try:
            value = json.loads(value.strip())
        except (TypeError, ValueError):
            value = [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        value = [key for key, enabled in value.items() if _catalog_bool(enabled) is not False]
    if not isinstance(value, (list, tuple, set)):
        value = [value] if value not in (None, "") else []
    output = []
    for item in value:
        try:
            normalized = int(item) if integers else str(item).strip()
        except (TypeError, ValueError):
            continue
        if normalized not in output and normalized != "":
            output.append(normalized)
    return output


def _integrated_audio_evidence(record):
    """Separate input modalities from embedded output-audio capability.

    Gateway ``multimodelTypes`` describes accepted modalities.  Seeing
    ``audio`` there does not prove that a generated MP4 contains an audio
    stream.  Output-modal fields or an explicitly integrated-audio boolean are
    positive evidence; input modalities are only useful as negative evidence.
    """
    capabilities = record.get("capabilities") if isinstance(record.get("capabilities"), dict) else {}
    audio = capabilities.get("audio") if isinstance(capabilities.get("audio"), dict) else {}
    output_observations = []
    for source_name, source in (("record", record),
                                ("capabilities", capabilities)):
        for key in ("outputMultiType", "ouputMultiType",
                    "outputMultimodelTypes", "outputModalities",
                    "outputTypes", "output_modalities"):
            if key in source and source.get(key) is not None:
                modalities = [str(item).strip().lower()
                              for item in _catalog_list(source.get(key))]
                output_observations.append({
                    "source": "%s.%s" % (source_name, key),
                    "modalities": modalities,
                    # A returned ``video`` container may still embed an audio
                    # stream.  Presence of an explicit audio output is positive
                    # evidence; absence from this generic content-type list is
                    # not authoritative negative evidence.
                    "value": True if "audio" in modalities else None,
                })
    explicit_claims = []
    for source_name, source, key in (
            ("record", record, "integratedAudio"),
            ("record", record, "integrated_audio"),
            ("record", record, "supportsIntegratedAudio"),
            ("record", record, "audioIntegrated"),
            ("record", record, "outputAudioIntegrated"),
            ("capabilities", capabilities, "integratedAudio"),
            ("capabilities.audio", audio, "integrated")):
        if key in source:
            value = _catalog_bool(source.get(key))
            if value is not None:
                explicit_claims.append({
                    "source": "%s.%s" % (source_name, key), "value": value})
    positive_output_claims = [
        claim for claim in output_observations if claim["value"] is True]
    if positive_output_claims or explicit_claims:
        claims = positive_output_claims + explicit_claims
        values = {claim["value"] for claim in claims}
        return {
            "value": next(iter(values)) if len(values) == 1 else None,
            "source": (
                "conflicting_output_audio_evidence" if len(values) > 1 else
                "explicit_output_and_integrated_audio" if positive_output_claims and explicit_claims else
                "explicit_output_modalities" if positive_output_claims else
                "explicit_integrated_audio"),
            "claims": claims,
            "output_observations": output_observations,
            "conflict": len(values) > 1,
        }
    for source in (record, capabilities):
        for key in ("multimodelTypes", "multiModelTypes",
                    "multimodel_types", "modalities"):
            if key in source and source.get(key) is not None:
                modalities = sorted({
                    str(item).strip().lower()
                    for item in _catalog_list(source.get(key))
                })
                has_audio_input = "audio" in modalities
                return {
                    "value": None if has_audio_input else False,
                    "source": ("input_modalities_only" if has_audio_input else
                               "input_modalities_exclude_audio"),
                    "input_modalities": modalities,
                    "output_observations": output_observations,
                    "conflict": False,
                }
    return {"value": None, "source": "unproven",
            "output_observations": output_observations,
            "conflict": False}


def _integrated_audio_value(record):
    return _integrated_audio_evidence(record)["value"]


def _catalog_submission_names(raw):
    """Return only names explicitly advertised by the provider catalog."""
    names = set()
    for key in ("modelId", "id", "modelName", "name", "displayName"):
        value = raw.get(key)
        if value:
            names.add(str(value).strip())
    for key in ("alias", "aliases"):
        names.update(_catalog_list(raw.get(key)))
    return {name for name in names if name}


def _preferred_submission_name(raw, model_id):
    """Return the provider-facing name the legacy create endpoint expects."""
    for key in ("modelName", "name", "displayName", "alias", "aliases", "id"):
        values = _catalog_list(raw.get(key))
        if values:
            return values[0]
    return model_id


def _route_name_candidates(raw, model_id):
    """Return provider-advertised route names in deterministic preference order."""
    output = []
    seen = set()
    for key in ("modelName", "name", "displayName", "id", "alias", "aliases"):
        for value in _catalog_list(raw.get(key)):
            normalized = value.lower()
            if (value and normalized != model_id.lower() and
                    normalized not in seen):
                output.append(value)
                seen.add(normalized)
    return output


def _deduplicated_catalog_rows(models):
    """Remove byte-equivalent directory rows without merging distinct routes."""
    output = []
    seen = set()
    for raw in models or []:
        if not isinstance(raw, dict):
            continue
        fingerprint = hashlib.sha256(json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str).encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        output.append((raw, fingerprint))
    return output


# ── Catalog normalization ──────────────────────────────────────────────

def _normalize_model_catalog(models):
    """Normalize provider routes without merging records that share modelId.

    A gateway modelId is not globally unique across providers.  When it is
    shared, a unique advertised route name (normally modelName) becomes the
    internal route key and the shared ID remains an ambiguous alias.  Rows
    without any unique route name are retained but marked conflicted so no
    paid selection can silently choose one.
    """
    rows = _deduplicated_catalog_rows(models)
    id_counts = {}
    name_counts = {}
    prepared = []
    for raw, fingerprint in rows:
        model_id = str(raw.get("modelId") or raw.get("id") or
                       raw.get("modelName") or "").strip()
        if not model_id:
            continue
        candidates = _route_name_candidates(raw, model_id)
        id_counts[model_id.lower()] = id_counts.get(model_id.lower(), 0) + 1
        for name in candidates:
            lowered = name.lower()
            name_counts[lowered] = name_counts.get(lowered, 0) + 1
        prepared.append((raw, fingerprint, model_id, candidates))

    records = {}
    for raw, fingerprint, model_id, candidates in prepared:
        duplicate_id = id_counts[model_id.lower()] > 1
        unique_route_name = next(
            (name for name in candidates if name_counts[name.lower()] == 1),
            None)
        route_identity_conflict = bool(duplicate_id and not unique_route_name)
        if not duplicate_id:
            route_key = model_id
            route_identity_source = "unique_model_id"
        elif unique_route_name:
            route_key = unique_route_name
            route_identity_source = "unique_provider_route_name"
        else:
            route_key = "%s#unresolved-%s" % (model_id, fingerprint[:12])
            route_identity_source = "unresolved_duplicate_model_id"
        if unique_route_name:
            submission_name = unique_route_name
            submission_identity_source = "unique_provider_route_name"
        elif not duplicate_id:
            submission_name = model_id
            submission_identity_source = "unique_model_id"
        else:
            submission_name = _preferred_submission_name(raw, model_id)
            submission_identity_source = "unresolved_provider_route_name"
        aliases = set(_catalog_submission_names(raw))
        aliases.update({model_id, route_key})
        online, status = _catalog_bool(raw.get("online")), _catalog_bool(raw.get("status"))
        raw_types = raw.get("allowVideoType")
        audio_evidence = _integrated_audio_evidence(raw)
        capabilities = (raw.get("capabilities")
                        if isinstance(raw.get("capabilities"), dict) else {})
        candidate = {
            "id": route_key,
            "provider_model_id": model_id,
            "route_identity_source": route_identity_source,
            "route_identity_conflict": route_identity_conflict,
            "submission_identity_source": submission_identity_source,
            "aliases": aliases,
            "submission_names": _catalog_submission_names(raw),
            "submission_name": submission_name,
            "active": online is not False and status is not False and (online is True or status is True),
            "allow_types": (set(_catalog_list(raw_types, integers=True))
                            if raw_types not in (None, "") else None),
            "image_count": (int(raw.get("imageCount"))
                            if str(raw.get("imageCount", "")).strip().isdigit() else None),
            # Preserve every provider-advertised request/output constraint.
            # Selection code must not infer compatibility after normalization
            # has discarded the source directory evidence.
            "resolutions": set(_catalog_list(
                raw.get("resolution") or raw.get("resolutions") or
                raw.get("allowResolution") or
                raw.get("supportResolutions") or
                capabilities.get("resolution") or
                capabilities.get("resolutions"))),
            "multimodel_types": set(_catalog_list(
                raw.get("multimodelTypes") or raw.get("multiModelTypes") or
                raw.get("multiModalTypes") or raw.get("multimodalTypes") or
                raw.get("multimodel_types") or raw.get("modalities") or
                capabilities.get("multimodelTypes") or
                capabilities.get("inputModalities") or
                capabilities.get("modalities"))),
            "output_multi_type": set(_catalog_list(
                raw.get("outputMultiType") or raw.get("ouputMultiType") or
                raw.get("outputMultimodelTypes") or
                raw.get("outputModalities") or raw.get("outputTypes") or
                raw.get("output_modalities") or
                capabilities.get("outputMultiType") or
                capabilities.get("ouputMultiType") or
                capabilities.get("outputModalities") or
                capabilities.get("outputTypes"))),
            "integrated_audio": audio_evidence["value"],
            "audio_evidence": audio_evidence,
            "conflict": bool(audio_evidence.get("conflict") or
                             route_identity_conflict),
        }
        if route_key in records:
            # A case-sensitive key collision after uniqueness analysis is still
            # unsafe.  Preserve both records under non-submittable identities.
            candidate["route_identity_conflict"] = True
            candidate["route_identity_source"] = "route_key_collision"
            candidate["conflict"] = True
            route_key = "%s#collision-%s" % (route_key, fingerprint[:12])
            candidate["id"] = route_key
            candidate["aliases"].add(route_key)
        records[route_key] = candidate
    alias_index = {}
    for route_key, record in records.items():
        for alias in record["aliases"]:
            alias_index.setdefault(alias.lower(), set()).add(route_key)
    return {"records": records, "aliases": alias_index}


# ── Catalog queries ────────────────────────────────────────────────────

def _model_catalog(category="video"):
    try:
        return _normalize_model_catalog(br_client.list_models(category=category))
    except (br_client.BRError, OSError, ValueError, TypeError, RuntimeError):
        return {"records": {}, "aliases": {}}


def _resolve_catalog_model(value, catalog):
    matches = catalog.get("aliases", {}).get(str(value or "").strip().lower(), set())
    if len(matches) > 1:
        raise ValueError("AMBIGUOUS_MODEL_ALIAS: %s -> %s" % (value, ", ".join(sorted(matches))))
    return next(iter(matches)) if matches else None


def _catalog_record_route_names(route_key, record, catalog):
    """Return only names that resolve uniquely to one provider route."""
    names = (
        [route_key, record.get("submission_name")] +
        list(record.get("aliases") or []) +
        list(record.get("submission_names") or []))
    output = set()
    aliases = catalog.get("aliases") or {}
    for name in names:
        normalized = str(name or "").strip().lower()
        if normalized and aliases.get(normalized) == {route_key}:
            output.add(normalized)
    return output


def _is_kling_video_model(model):
    # This helper is used while compiling local prompt previews and must stay
    # side-effect free. Model catalog lookups happen in _pick_video_model; a
    # boolean naming check must not trigger network retries in offline tests or
    # customer confirmation gates.
    return "kling" in str(model or "").lower()


def _model_family(value):
    """Return a capability family without treating one route alias as canonical."""
    lowered = str(value or "").strip().lower()
    if "seedance" in lowered or "dreamina-seedance" in lowered:
        return "seedance"
    if "kling" in lowered or "3.0-omni" in lowered or "3.0 omni" in lowered:
        return "kling"
    if "wan" in lowered:
        return "wan"
    return None


def _catalog_record_family(model_id, record):
    names = [model_id]
    names.extend(record.get("aliases") or [])
    names.extend(record.get("submission_names") or [])
    families = {_model_family(name) for name in names}
    families.discard(None)
    return next(iter(families)) if len(families) == 1 else None


def _family_catalog_models(name, catalog):
    """Expand a logical family to the gateway's current concrete route IDs."""
    family = _model_family(name)
    if not family:
        return []
    matches = [
        model_id for model_id, record in (catalog.get("records") or {}).items()
        if _catalog_record_family(model_id, record) == family
    ]
    return sorted(matches, key=lambda model_id: (
        not bool(catalog["records"][model_id].get("active")), model_id))


def _available_models_set(category="video"):
    catalog = _model_catalog(category)
    return {model_id for model_id, record in catalog["records"].items() if record["active"]}


def _model_allow_types(category="video", *, model_catalog_fn=None):
    """Read authoritative allowVideoType from gateway (cached 60s)."""
    catalog = (model_catalog_fn or _model_catalog)(category)
    return {model_id: record["allow_types"] for model_id, record in catalog["records"].items()
            if record["allow_types"] and not record["conflict"]}


def _model_supports_type(model, video_type, *, model_allow_types_fn=None, caps=None):
    """Check if model supports the given videoType."""
    if not video_type:
        return True
    allow_types = model_allow_types_fn or _model_allow_types
    allow = allow_types("video").get(model)
    if allow is not None:
        return int(video_type) in allow
    table = VIDEO_MODEL_CAPS if caps is None else caps
    model_caps = table.get(model)
    if model_caps is None:
        return False
    return int(video_type) in model_caps


def _model_integrated_audio(model_id, record=None, *, integrated_audio_table=None):
    candidates = [model_id]
    if record:
        candidates.extend(record.get("aliases") or [])
        candidates.extend(record.get("submission_names") or [])
    table = VIDEO_MODEL_INTEGRATED_AUDIO if integrated_audio_table is None else integrated_audio_table
    for name in candidates:
        value = table.get(str(name))
        if value is not None:
            return value
    # Do not infer embedded output audio from a family/marketing name.  A live
    # route alias may expose materially different output semantics.
    return None


def _pick_video_model(preferred=None, video_type=None, dialogue=None, formal=False,
                      allow_fallback=True, reference_count=None, exclude_models=None,
                      allow_unproven_audio=False, proven_audio_models=(),
                      *,
                      model_catalog_fn=None, available_models_fn=None,
                      resolve_catalog_model_fn=None, model_supports_type_fn=None,
                      model_integrated_audio_fn=None, fallback_chain=None,
                      default_model=None):
    """Pick first available model from VIDEO_MODEL_FALLBACK that supports the type."""
    catalog_fn = model_catalog_fn or _model_catalog
    available_fn = available_models_fn or _available_models_set
    resolve_fn = resolve_catalog_model_fn or _resolve_catalog_model
    supports_type_fn = model_supports_type_fn or _model_supports_type
    integrated_audio_fn = model_integrated_audio_fn or _model_integrated_audio
    fallback = list(VIDEO_MODEL_FALLBACK if fallback_chain is None else fallback_chain)
    default = DEFAULT_MODEL if default_model is None else default_model
    catalog = catalog_fn("video")
    excluded = {str(item).strip().lower() for item in (exclude_models or [])}
    proven_audio = {str(item).strip().lower()
                    for item in (proven_audio_models or ()) if str(item).strip()}
    has_catalog = bool(catalog["records"])
    available = None if has_catalog else available_fn("video")
    requested = ([preferred] if preferred else [default])
    if allow_fallback:
        requested += [item for item in fallback if item != preferred]
    ordered = []
    skipped_ambiguous_fallbacks = []
    for request_index, name in enumerate(requested):
        try:
            canonical = resolve_fn(name, catalog) or name
        except ValueError as exc:
            if request_index == 0 or not str(exc).startswith(
                    "AMBIGUOUS_MODEL_ALIAS"):
                raise
            skipped_ambiguous_fallbacks.append(str(name))
            continue
        if canonical not in ordered:
            ordered.append(canonical)
        # Provider route aliases evolve (for example an active "white" route
        # replacing an inactive public alias).  The catalog is authoritative:
        # include current concrete routes from the same capability family,
        # while retaining the exact requested identity first when it is valid.
        if has_catalog:
            for family_model in _family_catalog_models(name, catalog):
                if family_model not in ordered:
                    ordered.append(family_model)

    for model_id in ordered:
        if str(model_id).lower() in excluded:
            continue
        record = catalog["records"].get(model_id) if has_catalog else None
        if has_catalog and record is None:
            continue
        if record:
            supports_type = (not video_type or record["allow_types"] is not None and
                             int(video_type) in record["allow_types"])
            supports_reference_count = (
                reference_count is None or record.get("image_count") is None or
                int(reference_count) <= int(record["image_count"]))
            integrated_audio = record["integrated_audio"]
            if integrated_audio is None:
                integrated_audio = integrated_audio_fn(model_id, record)
            record_names = _catalog_record_route_names(
                model_id, record, catalog)
            if integrated_audio is None and record_names.intersection(proven_audio):
                integrated_audio = True
            eligible = (record["active"] and not record["conflict"] and
                        supports_type and supports_reference_count)
        else:
            eligible = supports_type_fn(model_id, video_type)
            integrated_audio = integrated_audio_fn(model_id)
            if available:
                eligible = eligible and model_id in available
        audio_eligible = (integrated_audio is True or
                          (allow_unproven_audio and integrated_audio is None))
        if eligible and not (formal and dialogue and not audio_eligible):
            return ((record or {}).get("submission_name") or model_id)
    raise ValueError(
        "NO_CAPABLE_VIDEO_MODEL: type=%s integrated_audio=%s%s" % (
            video_type, bool(formal and dialogue),
            " skipped_ambiguous_fallbacks=%s" % ",".join(
                skipped_ambiguous_fallbacks)
            if skipped_ambiguous_fallbacks else ""))


def _pick_privacy_fallback_model(video_type, dialogue=None, *, formal=True,
                                 reference_count=None, catalog=None,
                                 pick_video_model_fn=None):
    """Resolve an active, capacity-proven Kling-family privacy fallback.

    A late real-person rejection is too expensive to route through an offline
    alias guess.  Formal execution therefore requires a live catalog record
    whose active state, video type, integrated audio and image capacity are all
    sufficient for the exact request (including a future chain tail frame).
    """
    catalog = catalog if catalog is not None else _model_catalog("video")
    records = catalog.get("records") or {}
    required = int(reference_count or 0)
    if records:
        family_records = [
            (model_id, record) for model_id, record in records.items()
            if _catalog_record_family(model_id, record) == "kling"
        ]
        eligible = []
        for model_id, record in family_records:
            allow_types = record.get("allow_types")
            image_count = record.get("image_count")
            audio = record.get("integrated_audio")
            if audio is None:
                audio = _model_integrated_audio(model_id, record)
            if (record.get("active") and not record.get("conflict") and
                    allow_types is not None and int(video_type or 1) in allow_types and
                    image_count is not None and required <= int(image_count) and
                    not (formal and dialogue and audio is not True)):
                eligible.append(model_id)
        if eligible:
            model_id = sorted(eligible)[0]
            return records[model_id].get("submission_name") or model_id
        active = [model_id for model_id, record in family_records
                  if record.get("active")]
        if active:
            capacities = {
                model_id: records[model_id].get("image_count")
                for model_id in sorted(active)
            }
            raise ValueError(
                "PRIVACY_FALLBACK_CAPACITY_UNAVAILABLE: type=%s required_references=%s "
                "active_kling_routes=%s" %
                (video_type, required, json.dumps(capacities, sort_keys=True)))
        raise ValueError("PRIVACY_FALLBACK_ROUTE_UNAVAILABLE: no active Kling-family route")
    if formal:
        raise ValueError(
            "PRIVACY_FALLBACK_CATALOG_REQUIRED: live video catalog is unavailable")
    picker = pick_video_model_fn or _pick_video_model
    return picker(
        "kling-v3-omni-video", video_type=video_type, dialogue=dialogue,
        formal=False, allow_fallback=False, reference_count=reference_count)


def _pick_image_model(preferred=None, *, available_models_fn=None, fallback_chain=None):
    candidates = ([preferred] if preferred else []) + [
        m for m in (IMAGE_MODEL_FALLBACK if fallback_chain is None else fallback_chain)
        if m != preferred]
    avail = (available_models_fn or _available_models_set)("image")
    if not avail:
        return candidates[0]
    for m in candidates:
        if m in avail:
            return m
    return candidates[0]
