"""Thread-safe reusable cache for normalized source slices."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import Counter
from pathlib import Path

from autokat.core.paths import DATA_ROOT
from autokat.core.ffmpeg_utils import FFMPEG, get_media_info, run_ffmpeg
from autokat.models.db import get_conn, run_write_transaction


CACHE_VERSION = "slice-v4-base-bt709"
CACHE_ROOT = DATA_ROOT / "cache" / "slices"
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def build_cache_key(source_path: str, offset_frames: int, duration_frames: int,
                    fps: int, width: int = 1080, height: int = 1920,
                    transform: dict | None = None) -> str:
    path = Path(source_path)
    stat = path.stat()
    payload = {
        "version": CACHE_VERSION,
        "source": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "offset_frames": int(offset_frames),
        "duration_frames": int(duration_frames),
        "fps": int(fps),
        "width": int(width),
        "height": int(height),
        "transform": transform or {},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SliceCache:
    def __init__(self, root: Path | None = None):
        self.root = root or CACHE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, cache_key: str) -> Path:
        return self.root / cache_key[:2] / f"{cache_key}.mp4"

    def peek(self, cache_key: str) -> Path | None:
        """Return a ready cache path without recording a consumer hit."""
        path = self.path_for(cache_key)
        if not path.exists() or path.stat().st_size == 0:
            return None
        return path

    def get(self, cache_key: str) -> Path | None:
        path = self.peek(cache_key)
        if path is None:
            return None
        run_write_transaction(
            lambda conn: conn.execute(
                "UPDATE cache_entries SET hit_count=hit_count+1,"
                "last_accessed_at=datetime('now','localtime') WHERE cache_key=?",
                (cache_key,),
            )
        )
        return path

    def build(self, cache_key: str, builder) -> Path:
        with _locks_guard:
            lock = _locks.setdefault(cache_key, threading.Lock())
        with lock:
            hit = self.get(cache_key)
            if hit:
                return hit
            path = self.path_for(cache_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".building.mp4")
            run_write_transaction(
                lambda conn: conn.execute(
                    "INSERT INTO cache_entries(cache_key,cache_type,file_path,status,build_count) "
                    "VALUES(?, 'slice', ?, 'building', 1) "
                    "ON CONFLICT(cache_key) DO UPDATE SET status='building',"
                    "build_count=cache_entries.build_count+1,error_msg=NULL",
                    (cache_key, str(path)),
                )
            )
            try:
                builder(tmp)
                os.replace(tmp, path)
                size_bytes = path.stat().st_size
                run_write_transaction(
                    lambda conn: conn.execute(
                        "UPDATE cache_entries SET status='ready',size_bytes=?,"
                        "last_accessed_at=datetime('now','localtime') WHERE cache_key=?",
                        (size_bytes, cache_key),
                    )
                )
                return path
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                error_text = str(exc)
                run_write_transaction(
                    lambda conn: conn.execute(
                        "UPDATE cache_entries SET status='failed',error_msg=? "
                        "WHERE cache_key=?",
                        (error_text, cache_key),
                    )
                )
                raise


def _source_color_metadata(path: str) -> dict:
    try:
        streams = [
            stream for stream in get_media_info(path).get("streams", [])
            if stream.get("codec_type") == "video"
        ]
        if not streams:
            return {}
        return streams[0]
    except Exception:
        return {}


def _needs_hdr_to_sdr(metadata: dict) -> bool:
    return (
        metadata.get("color_primaries") == "bt2020"
        or metadata.get("color_space") in {"bt2020nc", "bt2020_ncl"}
        or metadata.get("color_transfer") in {"arib-std-b67", "smpte2084"}
    )


def _bt709_output_args() -> list[str]:
    # 仅在 HDR→SDR tonemap 路径下使用 (输出实际是 bt709, 加 tag 正确)。
    # SDR 源绝对不能用 — 否则 ffmpeg 会隐性转色 (任务 239 用户报告:
    # 选择「不扰动」成片色调还是被改了)。
    # 调用点必须先判 is_hdr = _needs_hdr_to_sdr(metadata), 再决定是否加。
    return [
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
    ]


def cached_segment(clip: dict, fps: int, perturbation: dict | None = None,
                   render_frames: int | None = None) -> tuple[Path, str]:
    """Build/read the exact normalized segment used by rendering and prewarm."""
    source = str(clip["source_path"])
    offset = float(clip.get("offset", 0))
    duration_frames = int(render_frames or clip.get("duration_frames") or 0)
    if duration_frames <= 0:
        duration_frames = max(1, int(round(float(clip.get("duration", 0)) * fps)))
    # Cache only a stable normalized base slice. Per-output spatial
    # perturbations are applied once after composition so different outputs can
    # reuse the same expensive source trim and color conversion.
    width, height = 1080, 1920
    is_image = clip.get("source_type") == "image" or Path(source).suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".webp",
    }
    filters = [
        f"trim=start={offset}:duration={duration_frames / fps:.9f}",
        "setpts=PTS-STARTPTS",
    ]
    color_metadata = _source_color_metadata(source)
    is_hdr = _needs_hdr_to_sdr(color_metadata)
    if is_hdr:
        filters.append(
            "zscale=t=linear:npl=100,tonemap=mobius:desat=0,"
            "zscale=p=bt709:t=bt709:m=bt709:r=tv"
        )
    if is_image:
        filters.append(
            f"zoompan=z='min(zoom+0.0005,1.08)':d=1:s=1080x1920:fps={fps}"
        )
    filters.append(
        f"scale={width}:{height}:force_original_aspect_ratio=1,"
        f"pad={width}:{height}:({width}-iw)/2:({height}-ih)/2:black"
    )
    key = build_cache_key(
        source, int(round(offset * fps)), duration_frames, fps, width, height,
        transform={},
    )
    cache = SliceCache()

    def builder(target: Path) -> None:
        command = [FFMPEG, "-y"]
        if is_image:
            command.extend(["-loop", "1"])
        # v3.2: 只在 HDR→SDR tonemap 路径下加 bt709 tag (输出实际是 bt709)。
        # SDR 源绝对不加 — 否则 ffmpeg 会隐性转色 (任务 239 用户报告:
        # 选择「不扰动」成片色调还是被改了)。
        # 注意: 不能写 *_bt709_output_args() if is_hdr else [] —
        # Python 不允许 *starred 表达式内嵌 ternary (语法歧义), 必须先赋给变量。
        color_args = _bt709_output_args() if is_hdr else []
        command.extend([
            "-i", source, "-vf", ",".join(filters),
            "-frames:v", str(duration_frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "60",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            *color_args,
            str(target),
        ])
        run_ffmpeg(command, desc="缓存标准化片段")

    return cache.build(key, builder), key


def prewarm_scripts(scripts: list[dict], workers: int = 4,
                    target_reference_coverage: float = 0.70) -> dict:
    """Prewarm hot slices first and leave cold one-off slices on demand."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    requests = {}
    reference_counts = Counter()
    hotspot_scores = {}
    total_refs = 0
    for script in scripts:
        fps = int(script.get("fps", 30))
        perturbation = script.get("perturbation") or {}
        clips = script.get("clips", [])
        for index, clip in enumerate(clips):
            total_refs += 1
            render_frames = int(clip.get("duration_frames") or 0)
            if index == len(clips) - 1:
                render_frames += 2
            source = str(clip["source_path"])
            key = build_cache_key(
                source, int(round(float(clip.get("offset", 0)) * fps)),
                render_frames, fps,
                1080, 1920, transform={},
            )
            requests.setdefault(key, (clip, fps, perturbation, render_frames))
            reference_counts[key] += 1
            hotspot_scores[key] = max(
                hotspot_scores.get(key, 0.0), float(clip.get("hotspot_score") or 0)
            )
    coverage_goal = max(0, min(total_refs, math.ceil(
        total_refs * max(0.0, min(1.0, target_reference_coverage))
    )))
    ordered_keys = sorted(
        requests,
        key=lambda key: (reference_counts[key], hotspot_scores[key], key),
        reverse=True,
    )
    selected_keys = []
    selected_references = 0
    for key in ordered_keys:
        if selected_references >= coverage_goal:
            break
        selected_keys.append(key)
        selected_references += reference_counts[key]
    cache = SliceCache()
    before = sum(1 for key in selected_keys if cache.peek(key))
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = {
            pool.submit(cached_segment, *requests[key]): key
            for key in selected_keys
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                failures.append({"cache_key": futures[future], "error": str(exc)})
    selected = len(selected_keys)
    return {
        "references": total_refs,
        "unique_segments": len(requests),
        "prewarm_segments": selected,
        "prewarm_reference_coverage": (
            selected_references / total_refs if total_refs else 0.0
        ),
        "preexisting_hits": before,
        "prewarm_built": selected - before - len(failures),
        "expected_render_hit_rate": (
            selected_references / total_refs if total_refs else 0.0
        ),
        "failures": failures,
    }
