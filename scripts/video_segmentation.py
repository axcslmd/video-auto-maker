#!/usr/bin/env python3
"""Shared duration partitioning for storyboard and video generation."""
from copy import deepcopy

SEEDANCE_MAX_SECONDS = 15
MAX_STORYBOARD_PANELS = 12

# Per-model maximum generation duration (seconds).
# Verified against BasicRouter /v1/video-models API on 2026-08-04.
# br_client.create_video() validates duration against the API at runtime;
# these values are for pre-splitting in script_splitter.split().
MODEL_MAX_SECONDS = {
    # Seedance family (via Dreamina endpoint)
    "dreamina-seedance-2-0-260128": 15,
    "dreamina-seedance-2-0-fast-260128": 15,
    "dreamina-seedance-2-5-260628": 30,  # offline as of 2026-08-04
    "seedance-1-5-pro-251215": 12,
    # Kling
    "kling-v3-omni": 15,
    "kling-v3": 15,  # offline as of 2026-08-04
    "kling-avatar-image2video": 15,  # offline as of 2026-08-04
    # Wan family
    "wan2.7-i2v": 15,
    "wan2.6-t2v": 15,
    "wan2.6-i2v-flash": 15,
    "wan2.6-r2v-flash": 15,
    "wan2.5-i2v-preview": 12,
    # HappyHorse
    "happyhorse-1.0-t2v": 15,
    "happyhorse-1.0-i2v": 15,
    "happyhorse-1.0-r2v": 15,
    # Google Veo
    "veo-3.1-generate-001": 8,
    "veo-3.1-lite-generate-001": 8,
    # Legacy aliases (mapped by br_client._legacy_video_model_candidates)
    "seedance-2.0": 15,
    "seedance-2.0-fast": 15,
    "kling-v3-omni-video": 15,
}


def max_seconds_for_model(model=None):
    """Return the max generation duration for the given model.

    Uses the actual API modelId when available; falls back to SEEDANCE_MAX_SECONDS.
    br_client.create_video() performs the authoritative runtime validation
    against the gateway's videoDurationMax field.
    """
    if model:
        return MODEL_MAX_SECONDS.get(str(model).lower(), SEEDANCE_MAX_SECONDS)
    return SEEDANCE_MAX_SECONDS


def _duration(shot, minimum=1):
    value = shot.get("duration") or shot.get("seconds") or minimum
    try:
        return max(float(value), float(minimum))
    except (TypeError, ValueError):
        return float(minimum)


def _panel_items(shot):
    value = shot.get("panel_plan") or shot.get("twelve_panel_plan") or []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _split_dialogue(dialogue, offset, take, total):
    """Split dialogue proportionally when a shot is split into time parts.

    When a 20s shot with 40 chars of dialogue is split into 0-10s and 10-20s,
    each part should get roughly its proportional share of the dialogue,
    not the full text. This prevents the prompt pollution where every segment
    repeats the entire dialogue (真实故障：段2重复段1台词、段3塞入CTA、段4又重复CTA)。

    Args:
        dialogue: the full dialogue string
        offset: start time of this part (seconds)
        take: duration of this part (seconds)
        total: total duration of the original shot (seconds)
    Returns:
        The proportional slice of dialogue for this time window.
    """
    if not dialogue or total <= 0:
        return dialogue
    text = str(dialogue).strip()
    if not text:
        return text
    # Calculate character positions based on time ratio
    chars = len(text)
    start_ratio = offset / total
    end_ratio = (offset + take) / total
    start_char = int(chars * start_ratio)
    end_char = int(chars * end_ratio)
    # Snap to sentence boundaries (。！？；) to avoid cutting mid-sentence
    if start_char > 0:
        for i in range(start_char, min(start_char + 10, chars)):
            if text[i] in "。！？；":
                start_char = i + 1
                break
    if end_char < chars:
        for i in range(end_char, max(end_char - 10, start_char), -1):
            if i > 0 and text[i - 1] in "。！？；":
                end_char = i
                break
    result = text[start_char:end_char].strip()
    return result if result else text  # fallback: if split produces empty, keep full


def partition_shots(shots, max_seconds=SEEDANCE_MAX_SECONDS, scene_aware="auto",
                    preserve_shots=False):
    """Partition shots by scene boundary first, then by model duration limit."""
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    shots = list(shots or [])
    use_scenes = scene_aware is True or (
        scene_aware == "auto" and any(str(s.get("scene_id") or "").strip() for s in shots)
    )
    if preserve_shots:
        result = []
        for shot in shots:
            duration = _duration(shot)
            if duration > max_seconds:
                raise ValueError("SHOT_EXCEEDS_MODEL_LIMIT: %s >%ss" % (duration, max_seconds))
            result.append(deepcopy(shot))
        return result
    result, current, current_seconds, current_scene = [], [], 0.0, None

    def flush():
        nonlocal current, current_seconds, current_scene
        if current:
            result.append(_make_segment(current, len(result) + 1))
            current, current_seconds, current_scene = [], 0.0, None

    for shot in shots:
        scene_id = str(shot.get("scene_id") or "").strip() or None
        if use_scenes and current and scene_id != current_scene:
            flush()
        if not current:
            current_scene = scene_id
        total = _duration(shot)
        remaining, offset, part = total, 0.0, 1
        panel_plan = _panel_items(shot)
        part_count = max(1, int((total + max_seconds - 1e-9) // max_seconds))
        while remaining > 1e-9:
            current_panel_count = sum(
                len(_panel_items(current_item)) for current_item in current)
            if (current and panel_plan and
                    current_panel_count + len(panel_plan) > MAX_STORYBOARD_PANELS):
                flush()
            room = max_seconds - current_seconds
            if room <= 1e-9:
                flush()
                room = max_seconds
            take = min(remaining, room)
            item = deepcopy(shot)
            original_id = str(shot.get("id") or len(result) + len(current) + 1)
            if take < total - 1e-9 or offset > 0:
                item["id"] = "%s_seg%02d" % (original_id, part)
                item["source_shot_id"] = original_id
                item["segment_offset"] = round(offset, 3)
                item["segment_part"] = part
                item["duration"] = round(take, 3)
                # Split authored dialogue fields proportionally across parts —
                # never duplicate one field into another or repeat the full line.
                # A 20s shot split into 0-10s + 10-20s must give each part only
                # the dialogue for its time window, not the full 20s dialogue.
                for dialogue_key in ("dialogue", "voiceover"):
                    dialogue = str(shot.get(dialogue_key) or "").strip()
                    if dialogue:
                        item[dialogue_key] = _split_dialogue(
                            dialogue, offset, take, total)
                if panel_plan:
                    start = int(round((offset / total) * len(panel_plan)))
                    end = int(round(((offset + take) / total) * len(panel_plan)))
                    local = panel_plan[start:max(end, start + 1)]
                    item["panel_plan"] = list(local) or [
                        "continuity beat %d/%d" % (part, part_count)]
            current.append(item)
            current_seconds += take
            remaining -= take
            offset += take
            part += 1
            if current_seconds >= max_seconds - 1e-9:
                flush()
    flush()
    return result


def _make_segment(items, index):
    if len(items) == 1:
        return items[0]
    first = deepcopy(items[0])
    first["id"] = "segment_%02d" % index
    first["source_shot_ids"] = list(dict.fromkeys(
        str(item.get("source_shot_id") or item.get("id") or "") for item in items))
    first["duration"] = round(sum(_duration(item) for item in items), 3)
    first["segment_index"] = index
    first["timeline"] = _merge_timeline(items)
    panels = [panel for item in items for panel in _panel_items(item)]
    if panels:
        first["panel_plan"] = panels
    first["dialogue"] = "\n".join(
        str(item.get("dialogue") or item.get("voiceover") or "").strip()
        for item in items if item.get("dialogue") or item.get("voiceover"))
    first["visual"] = "\n".join(
        str(item.get("visual") or item.get("scene_prompt") or "").strip()
        for item in items if item.get("visual") or item.get("scene_prompt"))
    for key in ("asset_refs", "characters", "motion_elements", "product_refs",
                "prop_prompts"):
        first[key] = _merge_values(items, key)
    return first


def _merge_values(items, key):
    """Merge list/dict metadata without dropping later shots in an aggregate."""
    values = [item.get(key) for item in items if item.get(key) not in (None, "", [], {})]
    if not values:
        return {} if key == "asset_refs" else []
    if any(isinstance(value, dict) for value in values):
        merged = {}
        for value in values:
            if not isinstance(value, dict):
                continue
            for subkey, subvalue in value.items():
                incoming = subvalue if isinstance(subvalue, list) else [subvalue]
                current = merged.setdefault(subkey, [])
                for item in incoming:
                    if item not in current:
                        current.append(deepcopy(item))
        return merged
    merged = []
    for value in values:
        incoming = value if isinstance(value, (list, tuple)) else [value]
        for item in incoming:
            if item not in merged:
                merged.append(deepcopy(item))
    return merged


def _split_dialogue(dialogue, offset, take, total):
    """Split dialogue proportionally across split parts.

    Splits by sentence boundaries (。！？；) when possible, falling back to
    character-count proportional split. Never duplicates dialogue across parts.
    """
    import re as _re
    if not dialogue or total <= 0:
        return dialogue
    # Split into sentences
    sentences = [s.strip() for s in _re.split(r"(?<=[。！？；!?.;])", dialogue) if s.strip()]
    if len(sentences) <= 1:
        # Single sentence: split by character count proportionally
        chars = list(dialogue)
        start_idx = int(round((offset / total) * len(chars)))
        end_idx = int(round(((offset + take) / total) * len(chars)))
        return "".join(chars[start_idx:end_idx]).strip()
    # Multi-sentence: assign sentences to time windows proportionally
    total_chars = sum(len(s) for s in sentences)
    start_ratio = offset / total
    end_ratio = (offset + take) / total
    result = []
    cursor = 0.0
    for sentence in sentences:
        s_start = cursor / total_chars
        s_end = (cursor + len(sentence)) / total_chars
        # Include sentence if it overlaps with this part's time window
        if s_end > start_ratio and s_start < end_ratio:
            result.append(sentence)
        cursor += len(sentence)
    return "".join(result).strip()


def _merge_timeline(items):
    timeline, cursor = [], 0.0
    for item in items:
        duration = _duration(item)
        source = item.get("timeline") or [{
            "start": 0, "end": duration,
            "action": item.get("visual") or item.get("scene_prompt"),
            "camera": item.get("camera_movement") or item.get("camera")
        }]
        for beat in source:
            beat = deepcopy(beat)
            start = max(0.0, min(duration, float(beat.get("start", 0))))
            end = max(start, min(duration, float(beat.get("end", duration))))
            beat["start"] = round(cursor + start, 3)
            beat["end"] = round(cursor + end, 3)
            beat.setdefault("source_shot_id", item.get("source_shot_id") or item.get("id"))
            if item.get("scene_id"):
                beat.setdefault("scene_id", item.get("scene_id"))
            timeline.append(beat)
        cursor += duration
    return timeline
