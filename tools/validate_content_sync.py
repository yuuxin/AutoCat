#!/usr/bin/env python3.11
"""Validate final MP4 speech/subtitle content with FunASR and PaddleOCR."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL_CACHE = ROOT / "outputs" / "content_sync_models"
os.environ["MODELSCOPE_CACHE"] = str(MODEL_CACHE)
os.environ["PADDLE_PDX_CACHE_HOME"] = str(MODEL_CACHE / "paddlex")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

from autokat.core.content_sync import (
    align_asr_to_subtitles, correct_subtitles_from_alignment, evaluate_ocr_samples, normalize_content,
    punctuation_breaks_valid, summarize_errors,
)
from autokat.core.subtitle_sync import detect_speech_intervals

FFMPEG = os.environ.get("AUTOKAT_FFMPEG", "/opt/homebrew/bin/ffmpeg")
FFPROBE = FFMPEG.replace("ffmpeg", "ffprobe")


def extract_audio(video: Path, wav: Path) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        check=True, capture_output=True,
    )


def build_asr_model():
    from funasr import AutoModel
    return AutoModel(model="paraformer-zh", disable_update=True)


def recognize_units(model, wav: Path) -> tuple[str, list[dict]]:
    result = model.generate(input=str(wav), batch_size_s=300, hotword="时尚女鞋 玛丽珍珠鞋")
    item = result[0]
    text = normalize_content(item.get("text", ""))
    timestamps = item.get("timestamp") or []
    if len(timestamps) != len(text):
        raise RuntimeError(
            f"FunASR 字符时间戳数量不一致: text={len(text)}, timestamp={len(timestamps)}"
        )
    units = [
        {"text": char, "start": float(stamp[0]) / 1000, "end": float(stamp[1]) / 1000}
        for char, stamp in zip(text, timestamps)
    ]
    intervals = detect_speech_intervals(str(wav))
    if units and intervals and units[-1]["end"] - intervals[-1][1] > 0.25:
        units[-1]["end"] = intervals[-1][1]
    return text, units


def build_ocr():
    from paddleocr import PaddleOCR
    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        lang="ch", use_doc_orientation_classify=False,
        use_doc_unwarping=False, use_textline_orientation=False,
    )


def _ocr_result_text(result) -> str:
    texts = []
    data = getattr(result, "json", result)
    if callable(data):
        data = data()
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and "res" in data:
        data = data["res"]
    if isinstance(data, dict):
        texts.extend(data.get("rec_texts") or [])
    return "".join(texts)


def _ocr_text(ocr, image) -> str:
    return "".join(_ocr_result_text(result) for result in ocr.predict(image))


def _rendered_subtitles(subtitles: list[dict]) -> list[dict]:
    rendered = []
    for subtitle in subtitles:
        item = dict(subtitle)
        item["start"] = round(float(item["start"]) * 100) / 100
        item["end"] = round(float(item["end"]) * 100) / 100
        rendered.append(item)
    return rendered


def sample_times(subtitles: list[dict], duration: float) -> list[float]:
    times = {round(index / 10, 3) for index in range(int(duration * 10) + 1)}
    for subtitle in subtitles:
        for boundary in (float(subtitle["start"]), float(subtitle["end"])):
            for delta in (-0.2, -0.1, 0.0, 0.1, 0.2):
                times.add(round(max(0.0, min(duration - 0.001, boundary + delta)), 3))
    return sorted(times)


def ocr_video(ocr, video: Path, subtitles: list[dict], duration: float) -> list[dict]:
    import cv2

    cap = cv2.VideoCapture(str(video))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    crop_top, crop_bottom = int(height * 0.72), int(height * 0.92)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30)
    requested_by_frame = {}
    for timestamp in sample_times(subtitles, duration):
        frame_index = max(0, int(round(timestamp * fps)))
        requested_by_frame.setdefault(frame_index, []).append(timestamp)
    samples, crops = [], []
    frame_index = 0
    final_frame = max(requested_by_frame, default=-1)
    while frame_index <= final_frame:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_index in requested_by_frame:
            actual_timestamp = frame_index / fps
            crop = frame[crop_top:crop_bottom, 0:width]
            if width > 720:
                crop = cv2.resize(
                    crop, (720, max(1, int(crop.shape[0] * 720 / width))),
                    interpolation=cv2.INTER_AREA,
                )
            for timestamp in requested_by_frame[frame_index]:
                is_contact = any(
                    abs(timestamp - float(sub[key])) <= 0.201
                    for sub in subtitles for key in ("start", "end")
                )
                samples.append({
                    "time": round(actual_timestamp, 6),
                    "requested_time": timestamp,
                    "ocr_text": "",
                    "contact_point": is_contact,
                })
                crops.append(crop)
        frame_index += 1
    cap.release()

    for start in range(0, len(crops), 64):
        results = list(ocr.predict(crops[start:start + 64]))
        for offset, result in enumerate(results):
            samples[start + offset]["ocr_text"] = _ocr_result_text(result)

    return samples


def _asr_phrase_at(units: list[dict], subtitles: list[dict], timestamp: float) -> str:
    subtitle = next(
        (
            item for item in subtitles
            if float(item["start"]) <= timestamp < float(item["end"])
        ),
        None,
    )
    if subtitle:
        start, end = float(subtitle["start"]), float(subtitle["end"])
        return "".join(
            unit["text"] for unit in units
            if start <= (float(unit["start"]) + float(unit["end"])) / 2 < end
        )
    return "".join(
        unit["text"] for unit in units
        if float(unit["start"]) <= timestamp < float(unit["end"])
    )


def _short_lines(label: str, text: str, width: int = 20, limit: int = 40) -> list[str]:
    value = str(text or "")[:limit]
    return [f"{label}{value[index:index + width]}" for index in range(0, len(value), width)] or [label]


def write_contact_sheet(video: Path, samples: list[dict], contact_dir: Path) -> None:
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    contact_samples = [sample for sample in samples if sample.get("contact_point")]
    cap = cv2.VideoCapture(str(video))
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 15)
    tiles = []
    for sample in contact_samples:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(sample["time"]) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((360, 640))
        tile = Image.new("RGB", (360, 760), "#111111")
        tile.paste(image, (0, 120))
        draw = ImageDraw.Draw(tile)
        lines = [
            f"{float(sample['time']):07.3f}s  {'PASS' if sample['passed'] else 'FAIL'}",
            *_short_lines("ASR: ", sample.get("asr_current", "")),
            *_short_lines("预期: ", sample.get("expected", "")),
            *_short_lines("OCR: ", sample.get("ocr_text", "")),
        ][:6]
        color = "#6ee7a0" if sample["passed"] else "#ff7b7b"
        for index, line in enumerate(lines):
            draw.text((6, 5 + index * 18), line, font=font, fill=color if index == 0 else "white")
        tiles.append(tile)
    cap.release()
    if not tiles:
        return
    cols = 5
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 360, rows * 760), "black")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % cols) * 360, (index // cols) * 760))
    contact_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(contact_dir / f"{video.stem}_contact_sheet.jpg", quality=88)


def probe_durations(video: Path) -> dict:
    data = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration", "-of", "json", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout)
    durations = {"format": float(data["format"]["duration"])}
    durations.update({
        stream["codec_type"]: float(stream.get("duration") or 0)
        for stream in data["streams"] if stream.get("codec_type") in {"audio", "video"}
    })
    return durations


def detect_visual_failures(video: Path) -> dict:
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(video),
         "-vf", "freezedetect=n=-45dB:d=0.5,blackdetect=d=0.5:pix_th=0.10",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return {
        "freeze_events": len(re.findall(r"freeze_start:", result.stderr)),
        "black_events": len(re.findall(r"black_start:", result.stderr)),
    }


def validate_one(asr, ocr, video: Path, script_path: Path, output_dir: Path) -> dict:
    script = json.loads(script_path.read_text(encoding="utf-8"))
    subtitles = script["subtitles"]
    with tempfile.TemporaryDirectory(prefix="autokat_content_sync_") as tmp:
        wav = Path(tmp) / "final.wav"
        extract_audio(video, wav)
        asr_text, units = recognize_units(asr, wav)
    alignment = align_asr_to_subtitles(units, subtitles)
    durations = probe_durations(video)
    rendered_subtitles = _rendered_subtitles(subtitles)
    ocr_samples = evaluate_ocr_samples(
        rendered_subtitles,
        ocr_video(ocr, video, rendered_subtitles, durations["format"]),
    )
    for sample in ocr_samples:
        sample["asr_current"] = _asr_phrase_at(units, rendered_subtitles, float(sample["time"]))
    write_contact_sheet(video, ocr_samples, output_dir / "contact_sheets")
    visuals = detect_visual_failures(video)
    pair_diff_ms = (max(durations.values()) - min(durations.values())) * 1000
    punctuation_ok = punctuation_breaks_valid(
        script.get("narration_text") or "".join(subtitle["text"] for subtitle in subtitles),
        subtitles,
    )
    result = {
        "video": str(video), "script": str(script_path), "asr_text": asr_text,
        "alignment": alignment, "errors": summarize_errors(alignment),
        "ocr_samples": ocr_samples, "durations": durations,
        "pair_diff_ms": pair_diff_ms, "punctuation_ok": punctuation_ok, **visuals,
    }
    result["passed"] = (
        all(item["passed"] for item in alignment)
        and all(item["passed"] for item in ocr_samples)
        and pair_diff_ms <= 20
        and punctuation_ok
        and not visuals["freeze_events"] and not visuals["black_events"]
    )
    return result


def write_report(results: list[dict], output_dir: Path) -> None:
    payload = {"passed": all(item["passed"] for item in results), "videos": results}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "content_sync_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    lines = ["# 配音字幕内容级同步与视觉验收报告", "", f"总结果：**{'PASS' if payload['passed'] else 'FAIL'}**", ""]
    for item in results:
        lines += [
            f"## {Path(item['video']).name}",
            f"- 结果：{'PASS' if item['passed'] else 'FAIL'}",
            f"- 最大 / 平均 / P95 内容同步误差：{item['errors']['max_ms']:.1f} / {item['errors']['mean_ms']:.1f} / {item['errors']['p95_ms']:.1f} ms",
            f"- 音视频流最大差值：{item['pair_diff_ms']:.3f} ms",
            f"- OCR 样本：{sum(row['passed'] for row in item['ocr_samples'])}/{len(item['ocr_samples'])} 通过",
            f"- 黑屏 / 停帧：{item['black_events']} / {item['freeze_events']}",
            f"- 切换点联系表：`contact_sheets/{Path(item['video']).stem}_contact_sheet.jpg`",
            "", "### 字幕与最终成片 ASR",
            "| 字幕 | 预期 | ASR | 起始误差 | 结束误差 | 覆盖率 | 结果 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        lines += [
            f"| {row['text']} | {row['expected_start']:.3f}-{row['expected_end']:.3f} | "
            f"{row['actual_start']:.3f}-{row['actual_end']:.3f} | {row['start_error_ms']:.1f}ms | "
            f"{row['end_error_ms']:.1f}ms | {row['coverage']:.0%} | {'PASS' if row['passed'] else 'FAIL'} |"
            for row in item["alignment"]
        ]
        lines.append("")
        lines += [
            "### 切换点 ASR / 预期字幕 / OCR",
            "| 时间 | ASR 当前内容 | 预期字幕 | OCR 实际字幕 | 结果 |",
            "|---:|---|---|---|---|",
        ]
        lines += [
            f"| {row['time']:.3f}s | {row.get('asr_current') or '（静音）'} | "
            f"{row.get('expected') or '（应隐藏）'} | {row.get('ocr_text') or '（未识别到字幕）'} | "
            f"{'PASS' if row['passed'] else 'FAIL'} |"
            for row in item["ocr_samples"] if row.get("contact_point")
        ]
        lines.append("")
    (output_dir / "content_sync_report.md").write_text("\n".join(lines), encoding="utf-8")
    if not payload["passed"]:
        raise SystemExit(1)


def auto_correct_and_rerender(clip: dict, result: dict) -> bool:
    script_path = Path(clip["script_path"])
    script = json.loads(script_path.read_text(encoding="utf-8"))
    script["subtitles"] = correct_subtitles_from_alignment(
        script["subtitles"], result["alignment"],
    )
    script["content_sync_retry_count"] = int(script.get("content_sync_retry_count") or 0) + 1
    script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    output = Path(clip["output_path"])
    retry_output = output.with_name(output.stem + ".content_sync_retry.mp4")
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "tools" / "rerender_clip.py"),
            "--script", str(script_path), "--output", str(retry_output),
        ],
        capture_output=True, text=True,
    )
    if completed.returncode != 0 or not retry_output.exists():
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        return False
    retry_output.replace(output)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--output-dir", default="outputs/content_sync_validation")
    parser.add_argument(
        "--clip-indexes", default="",
        help="Comma-separated clip indexes to validate; empty validates all clips.",
    )
    args = parser.parse_args()
    import sqlite3
    conn = sqlite3.connect(ROOT / "tasks" / "autokat.db")
    conn.row_factory = sqlite3.Row
    clips = [dict(row) for row in conn.execute(
        "SELECT * FROM clips WHERE task_id=? ORDER BY idx", (args.task_id,),
    )]
    conn.close()
    if args.clip_indexes:
        selected = {int(value) for value in args.clip_indexes.split(",") if value.strip()}
        clips = [clip for clip in clips if int(clip["idx"]) in selected]
    if not clips or any(clip["status"] != "done" for clip in clips):
        raise SystemExit("任务不存在或并非全部完成")
    asr, ocr = build_asr_model(), build_ocr()
    output_dir = (ROOT / args.output_dir / f"task_{args.task_id}").resolve()
    results = []
    for clip in clips:
        result = validate_one(asr, ocr, Path(clip["output_path"]), Path(clip["script_path"]), output_dir)
        if not result["passed"] and auto_correct_and_rerender(clip, result):
            result = validate_one(asr, ocr, Path(clip["output_path"]), Path(clip["script_path"]), output_dir)
            result["auto_retry"] = True
        results.append(result)
    if not all(result["passed"] for result in results):
        failed_ids = [clips[index]["id"] for index, result in enumerate(results) if not result["passed"]]
        conn = sqlite3.connect(ROOT / "tasks" / "autokat.db")
        conn.executemany(
            "UPDATE clips SET status='failed', error_msg=? WHERE id=?",
            [("内容级 ASR/OCR 同步验收失败", clip_id) for clip_id in failed_ids],
        )
        done = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE task_id=? AND status='done'", (args.task_id,),
        ).fetchone()[0]
        conn.execute("UPDATE tasks SET status='failed', done=? WHERE id=?", (done, args.task_id))
        conn.commit()
        conn.close()
    write_report(results, output_dir)
    print(output_dir / "content_sync_report.md")


if __name__ == "__main__":
    main()
