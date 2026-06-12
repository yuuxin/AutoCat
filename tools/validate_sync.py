#!/usr/bin/env python3
"""Render deterministic sync fixtures and write Markdown/JSON validation reports."""

from __future__ import annotations

import argparse
import copy
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autokat.core.editor import generate_script
from autokat.core.ffmpeg_utils import FFMPEG, FFPROBE
from autokat.core.renderer import render_simple
from autokat.core.timeline import SYNC_TOLERANCE_SECONDS, frames_to_seconds
from autokat.core.tts import build_phrase_timings


TEXT = "姐妹们，今天推荐一款夏日女鞋。穿着舒服吗？当然舒服！没有标点的长句需要按十二个字智能拆分"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def make_source(path: Path) -> None:
    if path.exists():
        return
    run([
        FFMPEG, "-y", "-f", "lavfi", "-i",
        "testsrc2=size=360x640:rate=30:duration=40",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ])


def make_audio(path: Path, duration: float) -> None:
    run([
        FFMPEG, "-y", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-c:a", "pcm_s16le", str(path),
    ])


def make_sentences(duration: float) -> list[dict]:
    spoken_chars = [char for char in TEXT if not char.isspace()]
    step = (duration - 0.2) / len(spoken_chars)
    boundaries = [
        {"text": char, "start": 0.1 + index * step, "end": 0.1 + (index + 1) * step}
        for index, char in enumerate(spoken_chars)
    ]
    phrases = build_phrase_timings(boundaries, source_text=TEXT, max_chars=12)
    for phrase in phrases:
        phrase["_narration_duration"] = duration
    return phrases


def make_script(source: Path, narration_duration: float, fps: int = 30) -> dict:
    random.seed(20260611 + int(narration_duration))
    script = generate_script(
        make_sentences(narration_duration),
        material_pool=[{
            "id": 1, "source_id": 1, "path": str(source),
            "duration": 40.0, "type": "video", "tags": [],
        }],
        config={
            "fps": fps, "transition_duration": 0.3, "tail_duration": 0.5,
            "min_shot_duration": 3.0, "source_safety_margin": 2.0,
        },
    )
    script["lang"] = "zh"
    for clip in script["clips"]:
        clip["offset"] = 0
    return script


def probe(path: Path) -> dict:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    streams = {
        stream["codec_type"]: float(stream.get("duration") or 0)
        for stream in data["streams"]
    }
    return {
        "format": float(data["format"]["duration"]),
        "video": streams.get("video", 0),
        "audio": streams.get("audio", 0),
    }


def validate_output(name: str, script: dict, output: Path) -> dict:
    durations = probe(output)
    values = list(durations.values())
    pair_diff = max(values) - min(values)
    target_diff = max(abs(value - script["final_duration"]) for value in values)
    punctuation = [char for char in TEXT if char in "，。！？"]
    caption_punctuation = [
        sub["text"][-1] for sub in script["subtitles"]
        if sub["text"] and sub["text"][-1] in "，。！？"
    ]
    per_second = []
    for second in range(int(script["final_duration"]) + 1):
        frame = min(second * script["fps"], script["target_video_frames"] - 1)
        clip = next(
            (index for index, item in enumerate(script["clips"])
             if item["start_frame"] <= frame < item["end_frame"]),
            None,
        )
        subtitle = next(
            (item["text"] for item in script["subtitles"]
             if item["start"] <= second < item["end"]),
            "",
        )
        per_second.append({
            "second": second, "frame": frame, "clip": clip,
            "audio_time": min(second, script["final_duration"]),
            "subtitle": subtitle, "visual_present": clip is not None,
        })
    return {
        "name": name,
        "output": str(output),
        "target_duration": script["final_duration"],
        "target_video_frames": script["target_video_frames"],
        "target_audio_samples": script["target_audio_samples"],
        "durations": durations,
        "pair_diff_ms": pair_diff * 1000,
        "target_diff_ms": target_diff * 1000,
        "last_subtitle_end": script["subtitles"][-1]["end"],
        "tail_gap_ms": (script["final_duration"] - script["subtitles"][-1]["end"]) * 1000,
        "punctuation_expected": punctuation,
        "punctuation_actual": caption_punctuation,
        "clip_timeline": [
            {
                key: clip.get(key) for key in (
                    "start_frame", "end_frame", "duration_frames",
                    "transition_start_frame", "transition_end_frame",
                    "transition_frames",
                )
            }
            for clip in script["clips"]
        ],
        "subtitles": script["subtitles"],
        "per_second": per_second,
        "passed": (
            pair_diff <= SYNC_TOLERANCE_SECONDS
            and target_diff <= SYNC_TOLERANCE_SECONDS
            and punctuation == caption_punctuation
            and all(item["visual_present"] for item in per_second)
        ),
    }


def render_case(name: str, script: dict, audio: Path, out_dir: Path,
                bgm: Path | None = None) -> dict:
    output = out_dir / f"{name}.mp4"
    error = render_simple(
        script, str(output), str(audio), fps=script["fps"],
        bgm_path=str(bgm) if bgm else None,
    )
    if error:
        raise RuntimeError(f"{name}: {error}")
    return validate_output(name, script, output)


def write_report(results: list[dict], boundaries: dict, out_dir: Path) -> None:
    payload = {"passed": all(item["passed"] for item in results) and boundaries["passed"],
               "cases": results, "boundaries": boundaries}
    (out_dir / "sync_validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    lines = ["# 混剪音画同步校验报告", "", f"总结果：**{'PASS' if payload['passed'] else 'FAIL'}**", ""]
    for item in results:
        d = item["durations"]
        lines += [
            f"## {item['name']}",
            f"- 目标：{item['target_duration']:.6f}s / {item['target_video_frames']} 帧 / {item['target_audio_samples']} samples",
            f"- 视频 / 音频 / 容器：{d['video']:.6f}s / {d['audio']:.6f}s / {d['format']:.6f}s",
            f"- 流最大差值：{item['pair_diff_ms']:.3f}ms；目标最大差值：{item['target_diff_ms']:.3f}ms",
            f"- 最后字幕：{item['last_subtitle_end']:.6f}s；静音动态尾声：{item['tail_gap_ms']:.3f}ms",
            f"- 标点断句：{'PASS' if item['punctuation_expected'] == item['punctuation_actual'] else 'FAIL'}",
            "",
            "### 全部字幕",
        ]
        lines += [
            f"- `{sub['start']:.6f} --> {sub['end']:.6f}` {sub['text']}"
            for sub in item["subtitles"]
        ]
        lines += [
            "", "### 视频与转场帧时间轴",
            "| 段 | 起始帧 | 结束帧 | 渲染帧数 | 转场起始 | 转场结束 | 转场帧数 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {index} | {clip['start_frame']} | {clip['end_frame']} | "
            f"{clip['duration_frames']} | {clip['transition_start_frame']} | "
            f"{clip['transition_end_frame']} | {clip['transition_frames']} |"
            for index, clip in enumerate(item["clip_timeline"])
        ]
        lines += ["", "### 逐秒结构时间轴（内容匹配由 validate_content_sync.py 验证）",
                  "| 秒 | 帧 | 画面段 | 音频时间 | 预期字幕 | 动态画面 |",
                  "|---:|---:|---:|---:|---|---|"]
        lines += [
            f"| {row['second']} | {row['frame']} | {row['clip']} | {row['audio_time']:.3f} | {row['subtitle']} | {'PASS' if row['visual_present'] else 'FAIL'} |"
            for row in item["per_second"]
        ]
        lines.append("")
    lines += [
        "## 边界场景",
        f"- 视频更长：裁剪到 TTS 目标，{boundaries['long_video']}",
        f"- 视频短一帧：允许 tpad 精确补一帧，{boundaries['one_frame_short']}",
        f"- 视频短超过一帧：明确拒绝，{boundaries['over_one_frame_short']}",
    ]
    (out_dir / "sync_validation.md").write_text("\n".join(lines), encoding="utf-8")
    if not payload["passed"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="outputs/sync_validation")
    args = parser.parse_args()
    out_dir = (ROOT / args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / "moving_source.mp4"
    make_source(source)

    results = []
    scripts = {}
    bgm = out_dir / "short_bgm.wav"
    make_audio(bgm, 3.0)
    for name, narration_duration, fps in (
        ("case_20s_30fps", 19.5, 30),
        ("case_30s_60fps_bgm", 29.5, 60),
    ):
        audio = out_dir / f"{name}.wav"
        make_audio(audio, narration_duration)
        script = make_script(source, narration_duration, fps=fps)
        scripts[name] = script
        results.append(render_case(
            name, script, audio, out_dir,
            bgm=bgm if name.endswith("_bgm") else None,
        ))

    base = scripts["case_20s_30fps"]
    audio = out_dir / "case_20s_30fps.wav"
    long_video = copy.deepcopy(base)
    long_video["clips"][0]["duration_frames"] += 15
    long_video["clips"][0]["duration"] += 0.5
    long_result = render_case("boundary_video_long", long_video, audio, out_dir)

    one_short = copy.deepcopy(base)
    one_short["clips"][-1]["duration_frames"] -= 1
    one_short["clips"][-1]["duration"] = frames_to_seconds(
        one_short["clips"][-1]["duration_frames"], one_short["fps"],
    )
    one_result = render_case("boundary_one_frame_short", one_short, audio, out_dir)

    too_short = copy.deepcopy(base)
    too_short["clips"][-1]["duration_frames"] -= 3
    too_short["clips"][-1]["duration"] = frames_to_seconds(
        too_short["clips"][-1]["duration_frames"], too_short["fps"],
    )
    rejected = render_simple(
        too_short, str(out_dir / "boundary_over_one_frame_short.mp4"),
        str(audio), fps=too_short["fps"],
    )
    boundaries = {
        "long_video": "PASS" if long_result["passed"] else "FAIL",
        "one_frame_short": "PASS" if one_result["passed"] else "FAIL",
        "over_one_frame_short": "PASS" if rejected else "FAIL",
    }
    boundaries["passed"] = all(value == "PASS" for value in boundaries.values())
    results.extend([long_result, one_result])
    write_report(results, boundaries, out_dir)
    print(out_dir / "sync_validation.md")


if __name__ == "__main__":
    main()
