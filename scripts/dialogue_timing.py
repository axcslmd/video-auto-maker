#!/usr/bin/env python3
"""Deterministic dialogue-duration contracts for pre-render quality gates.

The estimator is intentionally local and conservative.  It does not claim to
predict a provider's exact speech synthesis; it prevents obviously impossible
shot allocations from reaching expensive image/video or previs rendering.
"""
import hashlib
import json
import math
import re


VOICE_SPEECH_RATES = {
    "slow": 3.2,
    "calm": 3.5,
    "narrator": 3.8,
    "standard": 4.2,
    "professional": 4.5,
    "energetic": 5.0,
    "fast": 5.5,
    "promo": 5.8,
}
DEFAULT_SPEECH_RATE = 4.2
TOLERANCE_RATIO = 0.10
CONTRACT_KIND = "dialogue_timing_contract"


def _json_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def speech_units(dialogue):
    """Estimate Chinese-equivalent spoken units for mixed-language dialogue."""
    compact = re.sub(r"\s", "", str(dialogue or ""))
    units = 0
    cursor = 0
    for match in re.finditer(r"[A-Za-z]+", compact):
        units += len(compact[cursor:match.start()])
        token = match.group(0)
        if token.isupper() and len(token) <= 8:
            units += len(token)
        else:
            units += max(1, int(math.ceil(len(token) * 0.45)))
        cursor = match.end()
    return units + len(compact[cursor:])


def speech_rate(item=None, plan_voice_type=None):
    item = item if isinstance(item, dict) else {}
    audio = item.get("audio_contract") or item.get("audio") or {}
    voice = str(
        audio.get("voice") or item.get("voice_type") or
        plan_voice_type or "standard").lower()
    for name, rate in VOICE_SPEECH_RATES.items():
        if name in voice:
            return rate
    return DEFAULT_SPEECH_RATE


def analyze(dialogue, duration_seconds, *, item=None, plan_voice_type=None):
    dialogue = str(dialogue or "").strip()
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("DIALOGUE_TIMING_DURATION_REQUIRED") from exc
    if duration <= 0:
        raise ValueError("DIALOGUE_TIMING_DURATION_REQUIRED")
    rate = speech_rate(item, plan_voice_type=plan_voice_type)
    units = speech_units(dialogue)
    estimated = units / rate if dialogue else 0.0
    maximum = duration * (1.0 + TOLERANCE_RATIO)
    minimum_allocated = (
        int(math.ceil(estimated / (1.0 + TOLERANCE_RATIO)))
        if dialogue else 0)
    return {
        "estimator": "mixed_language_spoken_units_v1",
        "dialogue_sha256": hashlib.sha256(
            dialogue.encode("utf-8")).hexdigest(),
        "duration_seconds": round(duration, 3),
        "speech_units": units,
        "speech_rate_units_per_second": rate,
        "estimated_speech_seconds": round(estimated, 3),
        "tolerance_ratio": TOLERANCE_RATIO,
        "maximum_estimated_seconds": round(maximum, 3),
        "minimum_allocated_seconds": minimum_allocated,
        "fits": estimated <= maximum + 1e-9,
    }


def build_contract(shots, dialogue_by_id=None, *, plan_voice_type=None):
    """Build a content-bound timing contract in authored shot order."""
    shots = list(shots or [])
    dialogue_by_id = dialogue_by_id or {}
    ids = [str(shot.get("id") or "") for shot in shots]
    if not ids or any(not shot_id for shot_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("DIALOGUE_TIMING_SHOT_IDS_INVALID")
    items = []
    for shot in shots:
        shot_id = str(shot["id"])
        source = dialogue_by_id.get(shot_id)
        if isinstance(source, dict):
            dialogue = source.get("dialogue") or source.get("voiceover")
            duration = (source.get("duration_seconds") or
                        source.get("duration") or source.get("seconds"))
            rate_item = dict(shot, **source)
        else:
            dialogue = source
            duration = None
            rate_item = shot
        dialogue = (dialogue if dialogue is not None else
                    shot.get("dialogue") or shot.get("voiceover"))
        duration = (duration if duration is not None else
                    shot.get("duration") or shot.get("seconds"))
        timing = analyze(
            dialogue, duration, item=rate_item,
            plan_voice_type=plan_voice_type)
        items.append(dict(timing, shot_id=shot_id))
    unsigned = {
        "schema_version": 1,
        "kind": CONTRACT_KIND,
        "estimator": "mixed_language_spoken_units_v1",
        "tolerance_ratio": TOLERANCE_RATIO,
        "items": items,
        "total_allocated_seconds": round(sum(
            item["duration_seconds"] for item in items), 3),
        "total_estimated_speech_seconds": round(sum(
            item["estimated_speech_seconds"] for item in items), 3),
        "fits_all": all(item["fits"] for item in items),
    }
    return dict(unsigned, contract_sha256=_json_sha256(unsigned))


def validate_contract(contract, shots, dialogue_by_id=None, *,
                      plan_voice_type=None):
    if not isinstance(contract, dict) or contract.get("kind") != CONTRACT_KIND:
        raise ValueError("DIALOGUE_TIMING_CONTRACT_REQUIRED")
    expected = build_contract(
        shots, dialogue_by_id, plan_voice_type=plan_voice_type)
    if contract != expected:
        raise ValueError("DIALOGUE_TIMING_CONTRACT_MISMATCH")
    return expected


def propose_reallocation(shots, dialogue_by_id=None, *, plan_voice_type=None,
                         max_segment_seconds=15):
    """Propose, but never apply, a fixed-total timing repair.

    Applying this proposal changes the frozen timeline, so callers must rebuild
    the execution layout/animatic and obtain a new customer approval.
    """
    contract = build_contract(
        shots, dialogue_by_id, plan_voice_type=plan_voice_type)
    original = {
        item["shot_id"]: int(round(item["duration_seconds"]))
        for item in contract["items"]}
    required = {
        item["shot_id"]: max(1, int(item["minimum_allocated_seconds"]))
        for item in contract["items"]}
    total = sum(original.values())
    over_limit = [shot_id for shot_id, seconds in required.items()
                  if seconds > int(max_segment_seconds)]
    if over_limit:
        return {
            "feasible": False,
            "reason": "requires_shot_split_for_model_limit",
            "max_segment_seconds": int(max_segment_seconds),
            "shot_ids": over_limit,
            "requires_execution_previs_rebuild_and_customer_reapproval": True,
        }
    if sum(required.values()) > total:
        return {
            "feasible": False,
            "reason": "requires_total_extension_or_dialogue_edit",
            "total_duration_seconds": total,
            "minimum_required_seconds": sum(required.values()),
            "requires_execution_previs_rebuild_and_customer_reapproval": True,
        }
    proposed = dict(original)
    deficit = 0
    for shot_id, seconds in required.items():
        if seconds > proposed[shot_id]:
            deficit += seconds - proposed[shot_id]
            proposed[shot_id] = seconds
    donors = sorted(
        ((proposed[shot_id] - required[shot_id], shot_id)
         for shot_id in proposed if proposed[shot_id] > required[shot_id]),
        key=lambda item: (-item[0], item[1]))
    for capacity, shot_id in donors:
        take = min(capacity, deficit)
        proposed[shot_id] -= take
        deficit -= take
        if deficit == 0:
            break
    if deficit:
        raise ValueError("DIALOGUE_TIMING_REALLOCATION_INTERNAL_ERROR")
    changes = [{
        "shot_id": shot_id,
        "from_seconds": original[shot_id],
        "to_seconds": proposed[shot_id],
    } for shot_id in original if original[shot_id] != proposed[shot_id]]
    return {
        "feasible": True,
        "reason": "fixed_total_reallocation_available",
        "total_duration_seconds": total,
        "proposed_durations": proposed,
        "changes": changes,
        "dialogue_unchanged": True,
        "requires_execution_previs_rebuild_and_customer_reapproval": bool(changes),
    }


def assert_fits(contract, *, error_code="DIALOGUE_DURATION_OVERFLOW",
                proposal=None):
    failures = [item for item in (contract.get("items") or [])
                if item.get("fits") is not True]
    if not failures:
        return True
    details = ", ".join(
        "%s estimated=%.1fs allocated=%.1fs minimum=%ss" % (
            item.get("shot_id"), item.get("estimated_speech_seconds") or 0,
            item.get("duration_seconds") or 0,
            item.get("minimum_allocated_seconds") or 0)
        for item in failures)
    suffix = ("; proposal=" + json.dumps(
        proposal, ensure_ascii=False, sort_keys=True)) if proposal else ""
    raise ValueError("%s: %s%s" % (error_code, details, suffix))
