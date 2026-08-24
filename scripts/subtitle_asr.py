#!/usr/bin/env python3
"""ASR-based subtitle timestamp alignment for marketing video pipeline.

Three-tier strategy (best → fallback):
  1. **BasicRouter ASR** — if a speech-recognition model is available on the
     gateway, send the audio track and get word/sentence timestamps.
  2. **Silence-based segmentation** — use ffmpeg silencedetect (pure audio
     analysis, zero model) to find speech boundaries, then align known
     sentences to the detected speech segments.
  3. **Character-ratio estimation** — current fallback in script_splitter.

Public API:
  align_with_asr(lines, video_path, api_key=None) → aligned lines or None
  detect_speech_segments(video_path) → [(start, end), ...]
"""
import os
import re
import sys
import json
import subprocess
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import br_client  # noqa: E402
import media_tools  # noqa: E402


# ── ffmpeg helpers ─────────────────────────────────────────────────────

def _ffmpeg():
    return media_tools.resolve_ffmpeg_pair()[0]


def _ffprobe():
    return media_tools.resolve_ffmpeg_pair()[1]


def _probe_duration(video_path):
    fp = _ffprobe()
    if not fp:
        return None
    try:
        out = subprocess.check_output(
            [fp, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            stderr=subprocess.STDOUT, text=True)
        value = float(out.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _extract_audio_wav(video_path, out_path):
    """Extract audio track as 16kHz mono WAV for ASR consumption."""
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        subprocess.run(
            [ff, "-y", "-i", video_path, "-vn",
             "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            capture_output=True, check=True, timeout=60)
        return out_path if os.path.isfile(out_path) and os.path.getsize(out_path) > 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


# ── Silence-based speech segmentation ──────────────────────────────────

def detect_speech_segments(video_path, noise_db=-30, min_silence=0.35,
                           min_segment=0.3):
    """Detect speech segments via ffmpeg silencedetect (no model needed).

    Returns [(start_sec, end_sec), ...] sorted by start time.
    """
    ff = _ffmpeg()
    if not ff or not os.path.isfile(video_path):
        return []
    try:
        result = subprocess.run(
            [ff, "-i", video_path, "-af",
             "silencedetect=noise=%ddB:d=%.2f" % (noise_db, min_silence),
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return []

    # Parse silencedetect output:
    #   [silencedetect @ ...] silence_start: 2.5
    #   [silencedetect @ ...] silence_end: 3.8 | silence_duration: 1.3
    silences = []
    silence_start = None
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            silence_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and silence_start is not None:
            silences.append((silence_start, float(m.group(1))))
            silence_start = None

    duration = _probe_duration(video_path)
    if duration is None:
        return []

    # Invert silences → speech segments
    segments = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor + min_segment:
            segments.append((cursor, s_start))
        cursor = s_end
    if cursor < duration - min_segment:
        segments.append((cursor, duration))

    return segments


# ── BasicRouter ASR (cloud) ────────────────────────────────────────────

def _try_basicrouter_asr(video_path, api_key):
    """Try cloud ASR via BasicRouter if a speech model is available.

    Returns [{"start": float, "end": float, "text": str}] or None.
    """
    if not api_key:
        return None
    try:
        models = br_client.list_models(api_key, category="audio")
    except (br_client.BRError, OSError, ValueError, TypeError, RuntimeError):
        return None
    if not models:
        return None
    # Look for any ASR/speech-recognition model
    asr_model = None
    for m in (models if isinstance(models, list) else []):
        name = str(m.get("modelId") or m.get("modelName") or "").lower()
        if any(k in name for k in ("whisper", "asr", "speech", "transcri")):
            asr_model = m.get("modelId") or m.get("modelName")
            break
    if not asr_model:
        return None
    # Extract audio and upload for ASR
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix=".asr-")
    try:
        wav_path = os.path.join(tmpdir, "audio.wav")
        if not _extract_audio_wav(video_path, wav_path):
            return None
        audio_url = br_client.to_image_ref(wav_path, api_key=api_key,
                                           prefer_hosted=True)
        if not audio_url:
            return None
        payload = {"model": asr_model, "audio_url": audio_url,
                   "language": "zh", "word_timestamps": True}
        result = br_client.post(api_key, "/ai/asr", payload)
        if not isinstance(result, dict):
            return None
        segments = result.get("segments") or result.get("sentences") or []
        if not segments:
            return None
        return [{"start": float(s.get("start", 0)),
                 "end": float(s.get("end", 0)),
                 "text": str(s.get("text", "")).strip()}
                for s in segments if s.get("text")]
    except (br_client.BRError, OSError, ValueError, TypeError, RuntimeError):
        return None
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)


# ── Sentence-to-segment alignment ──────────────────────────────────────

def _align_sentences_to_segments(sentences, speech_segments, total_duration):
    """Align known sentence texts to detected speech segments by proportional
    character-density mapping.

    Strategy: compute character density (chars/sec) for the whole audio,
    then greedily assign each sentence to the time window that matches
    its expected duration.
    """
    if not sentences or not speech_segments:
        return None

    # Total characters across all sentences
    total_chars = sum(max(len(s), 1) for s in sentences)
    total_speech = sum(e - s for s, e in speech_segments)
    if total_speech <= 0:
        return None

    # Average speech rate (chars per second of speech)
    chars_per_sec = total_chars / total_speech

    aligned = []
    seg_idx = 0
    seg_offset = speech_segments[0][0] if speech_segments else 0.0

    for sent_text in sentences:
        expected_dur = max(len(sent_text), 1) / chars_per_sec
        expected_dur = max(expected_dur, 0.5)  # min 0.5s per sentence

        # Find best starting segment
        while seg_idx < len(speech_segments) - 1:
            remaining_in_seg = speech_segments[seg_idx][1] - seg_offset
            if remaining_in_seg >= expected_dur * 0.5:
                break
            seg_idx += 1
            seg_offset = speech_segments[seg_idx][0]

        start = seg_offset
        end = min(start + expected_dur, speech_segments[-1][1] if speech_segments else total_duration)

        # Clamp to speech segment boundaries
        if seg_idx < len(speech_segments):
            end = min(end, speech_segments[seg_idx][1])
            # If sentence extends beyond current segment, extend into next
            remaining = expected_dur - (end - start)
            next_idx = seg_idx + 1
            while remaining > 0.1 and next_idx < len(speech_segments):
                gap = speech_segments[next_idx][0] - speech_segments[next_idx - 1][1]
                if gap < 1.0:  # merge close segments
                    end = min(end + remaining, speech_segments[next_idx][1])
                    remaining = expected_dur - (end - start)
                    next_idx += 1
                else:
                    break

        aligned.append({"start": round(start, 3), "end": round(end, 3),
                        "text": sent_text})
        seg_offset = end

    return aligned


def align_with_asr(lines, video_path, api_key=None):
    """Align caption lines to actual audio using best available method.

    Args:
        lines: [{"start": float, "end": float, "text": str}, ...] from
               character-ratio estimation (used as sentence source).
        video_path: path to the rendered video with audio track.
        api_key: BasicRouter API key (optional, for cloud ASR).

    Returns:
        Updated lines list with ASR-aligned timestamps, or None if all
        methods failed (caller should keep original estimates).
    """
    if not lines or not os.path.isfile(video_path):
        return None

    sentences = [str(l.get("text") or "").strip() for l in lines if l.get("text")]
    if not sentences:
        return None

    duration = _probe_duration(video_path)
    if duration is None:
        return None

    # Tier 1: Cloud ASR via BasicRouter
    asr_segments = _try_basicrouter_asr(video_path, api_key)
    if asr_segments:
        return _align_asr_to_lines(asr_segments, lines, duration)

    # Tier 2: Silence-based segmentation
    speech_segments = detect_speech_segments(video_path)
    if speech_segments and len(speech_segments) >= 1:
        aligned = _align_sentences_to_segments(sentences, speech_segments, duration)
        if aligned:
            return aligned

    # Tier 3: Return None — caller keeps original character-ratio estimates
    return None


def _align_asr_to_lines(asr_segments, original_lines, total_duration):
    """Map cloud ASR segments back to original caption lines by text similarity."""
    aligned = []
    asr_idx = 0
    for line in original_lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        # Find matching ASR segment
        best_start, best_end = None, None
        search_text = text[:10]  # match on first 10 chars
        for i in range(asr_idx, min(asr_idx + 3, len(asr_segments))):
            if search_text in asr_segments[i]["text"]:
                best_start = asr_segments[i]["start"]
                best_end = asr_segments[i]["end"]
                asr_idx = i + 1
                break
        if best_start is None:
            # Fallback: use proportional position from original
            ratio = total_duration / max(
                original_lines[-1].get("end", total_duration), 0.001)
            best_start = line.get("start", 0) * ratio
            best_end = line.get("end", 0) * ratio
        aligned.append({"start": round(best_start, 3),
                        "end": round(best_end, 3), "text": text})
    return aligned


# ── SRT generation ─────────────────────────────────────────────────────

def _fmt_srt_ts(seconds):
    s = max(0.0, float(seconds))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return "%02d:%02d:%06.3f" % (h, m, sec)


def lines_to_srt(lines):
    """Convert aligned lines to SRT format string."""
    return "".join(
        "%d\n%s --> %s\n%s\n\n" % (i, _fmt_srt_ts(l["start"]),
                                      _fmt_srt_ts(l["end"]), l["text"])
        for i, l in enumerate(lines, 1)
    )
