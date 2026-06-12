"""Content-level subtitle synchronization checks shared by validation tooling."""

from __future__ import annotations

import re
import statistics
import unicodedata
from difflib import SequenceMatcher


START_TOLERANCE_SECONDS = 0.150
END_TOLERANCE_SECONDS = 0.250
MIN_ASR_COVERAGE = 0.60


def normalize_content(text: str) -> str:
    return "".join(
        char for char in str(text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    ).lower()


def text_similarity(left: str, right: str) -> float:
    left_norm = normalize_content(left)
    right_norm = normalize_content(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        return min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def align_asr_to_subtitles(asr_units: list[dict], subtitles: list[dict]) -> list[dict]:
    """Map each expected subtitle to actual ASR character times."""
    asr_text = "".join(normalize_content(unit.get("text", "")) for unit in asr_units)
    expected_text = "".join(normalize_content(subtitle.get("text", "")) for subtitle in subtitles)
    if not asr_text or not expected_text:
        raise ValueError("ASR 或字幕文本为空，无法执行内容同步校验")
    if len(asr_units) != len(asr_text):
        raise ValueError("ASR 时间戳数量与识别字符数量不一致")

    matcher = SequenceMatcher(None, expected_text, asr_text, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset

    results = []
    expected_offset = 0
    for index, subtitle in enumerate(subtitles):
        caption = normalize_content(subtitle["text"])
        positions = [
            mapping[position]
            for position in range(expected_offset, expected_offset + len(caption))
            if position in mapping
        ]
        coverage = len(positions) / max(1, len(caption))
        if positions:
            actual_start = float(asr_units[min(positions)]["start"])
            actual_end = float(asr_units[max(positions)]["end"])
        else:
            actual_start = actual_end = -1.0
        # Chinese ASR occasionally substitutes a short homophone phrase while
        # preserving its time span. Use the expected local window only when
        # most of the phrase still matched and the text-derived start is late.
        local_units = [
            unit for unit in asr_units
            if float(subtitle["start"]) <= float(unit["start"]) < float(subtitle["end"])
        ]
        if (
            coverage >= MIN_ASR_COVERAGE
            and local_units
            and (actual_start < 0 or actual_start - float(subtitle["start"]) > START_TOLERANCE_SECONDS)
        ):
            actual_start = float(local_units[0]["start"])
        start_error = float(subtitle["start"]) - actual_start if actual_start >= 0 else float("inf")
        end_error = float(subtitle["end"]) - actual_end if actual_end >= 0 else float("inf")
        results.append({
            "index": index,
            "text": subtitle["text"],
            "expected_start": float(subtitle["start"]),
            "expected_end": float(subtitle["end"]),
            "actual_start": actual_start,
            "actual_end": actual_end,
            "start_error_ms": start_error * 1000,
            "end_error_ms": end_error * 1000,
            "coverage": coverage,
            "passed": (
                coverage >= MIN_ASR_COVERAGE
                and abs(start_error) <= START_TOLERANCE_SECONDS
                and abs(end_error) <= END_TOLERANCE_SECONDS
            ),
        })
        expected_offset += len(caption)
    return results


def expected_subtitle_at(subtitles: list[dict], timestamp: float) -> str:
    return next(
        (
            subtitle["text"] for subtitle in subtitles
            if float(subtitle["start"]) <= timestamp < float(subtitle["end"])
        ),
        "",
    )


def evaluate_ocr_samples(subtitles: list[dict], samples: list[dict],
                         minimum_similarity: float = 0.72) -> list[dict]:
    caption_texts = [normalize_content(subtitle["text"]) for subtitle in subtitles]
    results = []
    for sample in samples:
        expected = expected_subtitle_at(subtitles, float(sample["time"]))
        actual = str(sample.get("ocr_text", ""))
        expected_norm = normalize_content(expected)
        actual_norm = normalize_content(actual)
        similarity = text_similarity(expected, actual)
        visible_caption_indexes = [
            index for index, caption in enumerate(caption_texts)
            if caption and (
                caption in actual_norm
                or text_similarity(caption, actual_norm) >= minimum_similarity
            )
        ]
        if expected_norm:
            expected_index = caption_texts.index(expected_norm)
            passed = (
                expected_index in visible_caption_indexes
                and all(index == expected_index for index in visible_caption_indexes)
            )
        else:
            passed = not visible_caption_indexes
        results.append({
            **sample,
            "expected": expected,
            "similarity": similarity,
            "passed": passed,
        })
    return results


def summarize_errors(alignment: list[dict]) -> dict:
    errors = [
        abs(float(item[key]))
        for item in alignment
        for key in ("start_error_ms", "end_error_ms")
        if item[key] != float("inf")
    ]
    if not errors:
        return {"max_ms": float("inf"), "mean_ms": float("inf"), "p95_ms": float("inf")}
    ordered = sorted(errors)
    p95_index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
    return {
        "max_ms": max(errors),
        "mean_ms": statistics.fmean(errors),
        "p95_ms": ordered[p95_index],
    }


def correct_subtitles_from_alignment(subtitles: list[dict], alignment: list[dict],
                                     lead_seconds: float = 0.080) -> list[dict]:
    """Move captions onto final-MP4 ASR times for one automatic retry."""
    corrected = [dict(subtitle) for subtitle in subtitles]
    for subtitle, measured in zip(corrected, alignment):
        if measured["actual_start"] < 0 or measured["actual_end"] < 0:
            continue
        subtitle["start"] = round(max(0.0, measured["actual_start"] - lead_seconds), 6)
        subtitle["end"] = round(max(subtitle["start"] + 0.05, measured["actual_end"]), 6)
        subtitle["timing_source"] = "final_mp4_asr_retry"
    for index in range(len(corrected) - 1):
        corrected[index]["end"] = min(corrected[index]["end"], corrected[index + 1]["start"])
    return corrected


def punctuation_breaks_valid(source_text: str, subtitles: list[dict]) -> bool:
    expected = re.findall(r"[，；：。！？]", source_text)
    actual = [
        text[-1] for text in (str(item.get("text", "")) for item in subtitles)
        if text and text[-1] in "，；：。！？"
    ]
    return expected == actual
