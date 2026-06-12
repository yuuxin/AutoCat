"""Subtitle segmentation and decoded-audio timing calibration."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from autokat.core.ffmpeg_utils import FFMPEG
from autokat.core.timeline import SAMPLE_RATE


CAPTION_LEAD_SECONDS = 0.080
MIN_CAPTION_CHARS = 7
PREFERRED_CAPTION_CHARS = 14
MAX_CAPTION_CHARS = 20
MEANINGFUL_PAUSE_SECONDS = 0.180
_STRONG_BREAKS = set("。！？")
_SOFT_BREAKS = set("，；：")
_ALL_BREAKS = _STRONG_BREAKS | _SOFT_BREAKS
_BAD_LEFT_EDGE = set("的地得和与或及而但就也都又更很把被让给在")
_BAD_RIGHT_EDGE = set("的地得和与或及而但就也都又更很把被让给在")
_MEASURE_RE = re.compile(r"\d+(?:\.\d+)?(?:个|双|件|款|套|厘米|米|秒|分钟|元|折|%|％)")


def split_punctuation_clauses(text: str) -> list[str]:
    """Split Chinese text at normal sentence punctuation, retaining punctuation."""
    return [
        clause.strip()
        for clause in re.findall(r".*?[，；：。！？]|.+$", str(text), re.S)
        if clause.strip()
    ]


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [(m.start(), m.end()) for m in _MEASURE_RE.finditer(text)]
    for pattern in (r"“[^”]+”", r"「[^」]+」", r"《[^》]+》", r"\([^)]{1,20}\)", r"（[^）]{1,20}）"):
        ranges.extend((m.start(), m.end()) for m in re.finditer(pattern, text))
    return ranges


def _inside_protected_boundary(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < position < end for start, end in ranges)


def semantic_unit_chunks(units: list[dict], clause_text: str,
                         min_chars: int = MIN_CAPTION_CHARS,
                         preferred_chars: int = PREFERRED_CAPTION_CHARS,
                         max_chars: int = MAX_CAPTION_CHARS) -> list[list[dict]]:
    """Chunk boundary units at readable semantic/pause boundaries."""
    if not units:
        return []
    total = sum(unit["chars"] for unit in units)
    if total <= max_chars:
        return [units]

    spoken = "".join(unit["text"] for unit in units)
    protected = _protected_ranges(clause_text)
    cumulative = []
    count = 0
    for unit in units:
        count += unit["chars"]
        cumulative.append(count)

    chunks = []
    start_index = 0
    start_char = 0
    while total - start_char > max_chars:
        candidates = []
        for end_index in range(start_index, len(units) - 1):
            end_char = cumulative[end_index]
            size = end_char - start_char
            if size < min_chars:
                continue
            if size > max_chars:
                break
            if _inside_protected_boundary(end_char, protected):
                continue
            left = spoken[end_char - 1] if end_char else ""
            right = spoken[end_char] if end_char < len(spoken) else ""
            pause = max(0.0, units[end_index + 1]["start"] - units[end_index]["end"])
            score = -abs(size - preferred_chars)
            if pause >= MEANINGFUL_PAUSE_SECONDS:
                score += 12
            elif pause >= 0.10:
                score += 5
            if left in _BAD_LEFT_EDGE:
                score -= 5
            if right in _BAD_RIGHT_EDGE:
                score -= 5
            remaining = total - end_char
            if 0 < remaining < min_chars:
                score -= 10
            candidates.append((score, end_index, end_char))
        if not candidates:
            end_index = min(
                range(start_index, len(units)),
                key=lambda idx: abs((cumulative[idx] - start_char) - max_chars),
            )
            end_char = cumulative[end_index]
        else:
            _, end_index, end_char = max(candidates)
        chunks.append(units[start_index:end_index + 1])
        start_index = end_index + 1
        start_char = end_char
    if start_index < len(units):
        chunks.append(units[start_index:])

    if len(chunks) > 1 and sum(item["chars"] for item in chunks[-1]) < min_chars:
        if sum(item["chars"] for item in chunks[-2] + chunks[-1]) <= max_chars:
            chunks[-2].extend(chunks.pop())
    return chunks


def convert_to_pcm_wav(input_path: str, output_path: str) -> str:
    """Decode TTS output to the PCM clock used by the renderer."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-i", input_path,
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", output_path,
        ],
        check=True, capture_output=True, timeout=120,
    )
    return output_path


def detect_speech_intervals(audio_path: str, top_db: float = 35.0) -> list[tuple[float, float]]:
    """Return decoded-audio speech intervals using librosa's energy VAD."""
    import librosa

    samples, sample_rate = librosa.load(audio_path, sr=SAMPLE_RATE, mono=True)
    intervals = librosa.effects.split(
        samples, top_db=top_db, frame_length=1024, hop_length=128,
    )
    return [
        (float(start) / sample_rate, float(end) / sample_rate)
        for start, end in intervals if end > start
    ]


def audio_onset(audio_path: str) -> float | None:
    source = Path(audio_path)
    if source.suffix.lower() == ".wav":
        intervals = detect_speech_intervals(str(source))
        return intervals[0][0] if intervals else None
    with tempfile.TemporaryDirectory(prefix="autokat_onset_") as tmpdir:
        decoded = str(Path(tmpdir) / "audio.wav")
        convert_to_pcm_wav(str(source), decoded)
        intervals = detect_speech_intervals(decoded)
        return intervals[0][0] if intervals else None


def calibrate_phrase_timings(phrases: list[dict], speech_intervals: list[tuple[float, float]],
                             narration_duration: float,
                             lead_seconds: float = CAPTION_LEAD_SECONDS) -> list[dict]:
    """Calibrate WordBoundary captions against actual decoded speech and pauses."""
    if not phrases or not speech_intervals:
        raise ValueError("无法从实际 PCM 音频检测到有效发声区间")

    calibrated = [dict(phrase) for phrase in phrases]
    global_shift = speech_intervals[0][0] - float(calibrated[0]["start"])
    global_shift = max(-0.5, min(0.5, global_shift))

    interval_starts = [start for start, _ in speech_intervals]
    interval_ends = [end for _, end in speech_intervals]
    for phrase in calibrated:
        raw_start = max(0.0, float(phrase["start"]) + global_shift)
        raw_end = min(narration_duration, float(phrase["end"]) + global_shift)
        near_start = min(interval_starts, key=lambda value: abs(value - raw_start))
        near_end = min(interval_ends, key=lambda value: abs(value - raw_end))
        if abs(near_start - raw_start) <= 0.20:
            raw_start = near_start
        if abs(near_end - raw_end) <= 0.25:
            raw_end = near_end
        phrase["word_boundary_start"] = round(float(phrase["start"]), 6)
        phrase["word_boundary_end"] = round(float(phrase["end"]), 6)
        phrase["start"] = max(0.0, raw_start - lead_seconds)
        phrase["end"] = max(phrase["start"] + 0.05, raw_end)
        phrase["timing_source"] = "word_boundary+pcm_vad"

    for index, phrase in enumerate(calibrated):
        if index + 1 < len(calibrated):
            next_phrase = calibrated[index + 1]
            phrase["end"] = min(phrase["end"], max(phrase["start"] + 0.05, next_phrase["start"]))
        phrase["start"] = round(max(0.0, phrase["start"]), 6)
        phrase["end"] = round(min(narration_duration, phrase["end"]), 6)
    return calibrated


def prepare_pcm_and_calibrate(compressed_audio_path: str, phrases: list[dict],
                              narration_duration: float) -> tuple[str, list[dict], float]:
    import soundfile as sf

    pcm_path = str(Path(compressed_audio_path).with_suffix(".wav"))
    convert_to_pcm_wav(compressed_audio_path, pcm_path)
    narration_duration = float(sf.info(pcm_path).duration)
    intervals = detect_speech_intervals(pcm_path)
    calibrated = calibrate_phrase_timings(phrases, intervals, narration_duration)
    return pcm_path, calibrated, narration_duration
