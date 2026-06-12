"""FFmpeg 渲染管线 — 简易版（稳定可靠）

用 ffmpeg-full 确保 drawtext/subtitles filter 可用。
流程：每个 clip 单独处理 -> concat 拼接 -> drawtext 字幕 -> 混音
"""

import json
import os
import re
import time
from datetime import datetime
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from autokat.models.db import (
    add_clip, update_clip_status, get_pending_clips,
    update_task_status, get_task, init_db, update_clip_progress,
    get_task_clip_counts,
)
from autokat.core.tts import generate_narration
from autokat.core.editor import (
    generate_batch, save_script_to_file, load_script_from_file,
)
from autokat.core.material import build_material_pool
from autokat.core.paths import OUTPUT_ROOT, TASKS_ROOT

# ── FFmpeg 路径（统一从 ffmpeg_utils 导入） ──
from autokat.core.ffmpeg_utils import (
    FFMPEG, FFPROBE, run_ffmpeg, get_media_duration, get_media_info,
)
from autokat.core.perturbation import build_perturbation, is_level_enabled
from autokat.core.sync_check import verify_sync, parse_srt_last_end
from autokat.core.progress_log import emit as _log_emit
from autokat.core.timeline import (
    SAMPLE_RATE, SYNC_TOLERANCE_SECONDS, apply_integer_timeline,
    build_target_clock, frames_to_seconds,
)
from autokat.core.subtitle_sync import audio_onset
import random

_STOP_EVENT = threading.Event()


def request_stop():
    """Request cancellation of the active generation job."""
    _STOP_EVENT.set()


def clear_stop_request():
    """Clear a previous cancellation request before starting a new job."""
    _STOP_EVENT.clear()


def stop_requested() -> bool:
    return _STOP_EVENT.is_set()


# ── 友好日志辅助 ──────────────────────────────────────────────
def _fmt_size(n_bytes: int) -> str:
    """字节数 → 人读格式（B/KB/MB）"""
    if n_bytes < 1024:
        return f"{n_bytes}B"
    if n_bytes < 1024 * 1024:
        return f"{n_bytes/1024:.0f}KB"
    return f"{n_bytes/(1024*1024):.1f}MB"


def _dir_size(path: Path) -> int:
    """目录里所有文件总字节数（递归）"""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total

def _get_encoder_label() -> str:
    """探测当前 ffmpeg 编码器 (供 main_window wizard 日志显示用)

    当前 _h264_encoder_args 已简化为只用 libx264 (CPU), 所以这里固定返回
    libx264 标签, 避免误导用户以为在用 GPU 加速。保留函数名是因为
    main_window.py:2857/2889 还在 import 它。
    """
    return "libx264（CPU）"


def _h264_encoder_args(bitrate: str = "8M") -> list:
    """Return the stable, color-consistent H.264 encoder settings."""
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

# ── 59 种 xfade 转场效果 ──
XFADE_TRANSITIONS = [
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance",
    "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose",
    "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
]

OUTPUT_DIR = OUTPUT_ROOT
SCRIPTS_DIR = TASKS_ROOT / "scripts"
_FINAL_CLIP_GUARD_FRAMES = 2


def _needs_hdr_to_sdr(path: str) -> bool:
    info = get_media_info(path)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        return False
    stream = streams[0]
    return (
        stream.get("color_primaries") == "bt2020"
        or stream.get("color_space") == "bt2020nc"
        or stream.get("color_transfer") in {"arib-std-b67", "smpte2084"}
    )


def _tail_freeze_duration(path: str, threshold: float = 0.5) -> float:
    """Return a freeze duration only when the freeze reaches the file tail."""
    duration = get_media_duration(path)
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostats", "-v", "info", "-i", path,
             "-vf", f"freezedetect=n=-45dB:d={threshold}", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        starts = [
            float(value) for value in re.findall(
                r"lavfi\.freezedetect\.freeze_start: ([0-9.]+)", result.stderr
            )
        ]
        ends = [
            float(value) for value in re.findall(
                r"lavfi\.freezedetect\.freeze_end: ([0-9.]+)", result.stderr
            )
        ]
        if starts and len(starts) > len(ends):
            return max(0.0, duration - starts[-1])
    except Exception:
        pass
    return 0.0


def _resolve_target_clock(script: dict, audio_duration: float, fps: int) -> dict:
    """Return one authoritative, frame-aligned video/audio clock."""
    target_frames = int(script.get("target_video_frames") or 0)
    target_samples = int(script.get("target_audio_samples") or 0)
    if target_frames > 0 and target_samples > 0:
        return {
            "sample_rate": int(script.get("sample_rate") or SAMPLE_RATE),
            "target_video_frames": target_frames,
            "target_audio_samples": target_samples,
            "final_duration": frames_to_seconds(target_frames, fps),
        }
    return build_target_clock(
        audio_duration, fps, float(script.get("tail_duration", 0.5)),
    )


def _resolve_final_duration(script: dict, audio_duration: float) -> float:
    """Compatibility helper; current rendering uses the integer target clock."""
    fps = int(script.get("fps") or 30)
    return _resolve_target_clock(script, audio_duration, fps)["final_duration"]


def _duration_tolerance(fps: int) -> float:
    """Public sync contract for final streams."""
    del fps
    return SYNC_TOLERANCE_SECONDS


def _segment_render_frames(clip: dict, index: int, clip_count: int, fps: int) -> int:
    """Render the final source a little long so xfade/concat cannot eat the tail."""
    frames = int(
        clip.get("duration_frames")
        or round(float(clip["duration"]) * fps)
    )
    if index == clip_count - 1:
        frames += _FINAL_CLIP_GUARD_FRAMES
    return frames

def _safe_progress(clip_id: Optional[int], detail: str,
                     log: bool = False, clip_idx: Optional[int] = None):
    """DB 写入失败时静默忽略，避免影响渲染主流程
    log=True 时把 detail 也推到跨线程队列，由 UI 端 drain 写入 _wiz_log
    """
    if clip_id is None:
        return
    try:
        update_clip_progress(clip_id, detail)
    except Exception as e:
        print(f"[进度] 写入失败: {e}")
    if log:
        prefix = f"第 {clip_idx+1} 条 · " if clip_idx is not None else ""
        try:
            _log_emit(prefix + detail)
        except Exception:
            pass



def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)


# v2.4: 删除 save_task_metadata, 整文件不再写 *.metadata.json (改由 titles.txt 提供发布标题)


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ass_time(seconds: float) -> str:
    """Format an ASS timestamp using its required centisecond precision."""
    total_centiseconds = max(0, int(round(float(seconds) * 100)))
    h, remainder = divmod(total_centiseconds, 360000)
    m, remainder = divmod(remainder, 6000)
    s, cs = divmod(remainder, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _parse_margin_v(subtitle_position: Optional[str]) -> int:
    """从百分比字符串解析 ASS MarginV（PlayResY=1920）

    10% → 192, 13% → 250, 16% → 307
    解析失败/None → 默认 250（13%）
    """
    if not subtitle_position:
        return 250
    try:
        pct = float(str(subtitle_position).strip().rstrip("%"))
        return int(1920 * pct / 100)
    except (ValueError, AttributeError):
        return 250


def _make_srt(subtitles: list[dict], lang: str = "zh", subtitle_position: Optional[str] = None) -> str:
    """生成 SRT 字幕文件（UTF-8）

    纯文本 SRT：序号 + 时间戳 (,) + 文本，符合 SRT 规范。
    empty 输入返回空文件。subtitle_position 仅用作兼容占位，真实位置在 _make_ass 里走 MarginV。
    """
    del subtitle_position  # SRT 不支持 margin，仅 ASS 支持
    fd, path = tempfile.mkstemp(suffix=".srt", prefix="autokat_sub_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for i, sub in enumerate(subtitles, 1):
            start = _fmt_srt_time(sub["start"])
            end = _fmt_srt_time(sub["end"])
            text = sub["text"].replace("\n", " ").strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
    return path


# ── v2.3 6 种 ASS 动效 (字幕层差异化, 防止 OCR 文本指纹重复) ──
# 6 种风格轮换, 跟画面/语速一起作为差异化维度
# 每种动效通过 ASS \\fad / \\move / \\t / \\kf 等转义码实现
_ASS_ANIMATIONS = ("none", "fade", "pop", "slide", "typewriter", "karaoke")


def _ass_animate(text: str, style: str) -> str:
    """给单条字幕文本包 ASS 动效转义码。

    风格说明:
    - none: 无动效, 原样返回
    - fade: \\fad(150,150) 淡入淡出 (150ms)
    - pop: \\fscx100 \\fscy100 起始 0% 然后 \\t(0,200,\\fscx110 \\fscy110) 弹出
    - slide: \\move(540,1900,540,1500) 从底部 1900 滑入到 1500
    - typewriter: \\t(0,400,\\clip) 不易实现纯 typewriter, 改用 \\fad 慢淡入
    - karaoke: \\k<dur> 在文本里逐字染色, 简单实现是 \\k30 每个字 30/100 秒
    """
    if not style or style == "none":
        return text
    if style == "fade":
        return f"{{\\fad(150,150)}}{text}"
    if style == "pop":
        return f"{{\\fscx90\\fscy90}}{text}{{\\t(0,180,\\fscx110\\fscy110)}}"
    if style == "slide":
        return f"{{\\move(540,1980,540,1500,0,200)}}{text}"
    if style == "typewriter":
        # typewriter 完整版 (逐字 \k) 需要按字时长, 简化用慢淡入
        return f"{{\\fad(400,80)}}{text}"
    if style == "karaoke":
        # 在每个字之间插入 \k30 (0.3s 染色), 简单实现: 每 2 字插一个 \k30
        out = []
        for i, ch in enumerate(text):
            out.append(ch)
            if i % 2 == 0 and i < len(text) - 1:
                out.append("{\\k30}")
        return "".join(out)
    return text


def _pick_ass_animation(rng=None) -> str:
    """从 6 种动效里随机选一种。"""
    import random as _r
    rng = rng or _r
    return rng.choice(_ASS_ANIMATIONS)


def _make_ass(subtitles: list[dict], lang: str = "zh", subtitle_position: Optional[str] = None,
              animation: Optional[str] = None, font_name: Optional[str] = None,
              font_size: Optional[int] = None) -> str:
    """生成 ASS 字幕文件（UTF-8），给 ffmpeg subtitles= 滤镜用。

    全语言统一走 ASS 格式（带 [V4+ Styles]），
    通过 Style 行的 MarginV 控制字幕距离画面底部的像素数。

    字体配置：
    v2.4: 字号上调到手机端"中等"档位 (zh/en 68pt, th 60pt), outline 同步加到 3 避免字变大后描边发飘
    - 中文: Source Han Sans, 68pt, Outline=3
    - 英文: Arial, 68pt, Outline=3
    - 泰文: Thonburi, 60pt, Outline=3
    """
    margin_v = _parse_margin_v(subtitle_position)

    # 默认字体/字号 (按 lang) — 调用方传入的 font_name / font_size 优先
    if lang == "th":
        _df_font, _df_size, outline = "Thonburi", 60, 3
    elif lang == "en":
        _df_font, _df_size, outline = "Arial", 68, 3
    else:  # zh 或其他
        _df_font, _df_size, outline = "Source Han Sans", 68, 3
    fn = font_name or _df_font
    fs = int(font_size) if font_size else _df_size

    fd, path = tempfile.mkstemp(suffix=".ass", prefix="autokat_sub_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\n")
        f.write("PlayResX: 1080\n")
        f.write("PlayResY: 1920\n\n")
        f.write("[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        f.write(f"Style: Default,{fn},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},0,2,20,20,{margin_v},1\n\n")
        f.write("[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for sub in subtitles:
            start = _fmt_ass_time(sub["start"])
            end = _fmt_ass_time(sub["end"])
            text = sub["text"].replace("\n", "\\N")
            text = _ass_animate(text, animation or "none")
            f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
    return path





def render_simple(script: dict, output_path: str, audio_path: str,
                   bgm_files: Optional[list[str]] = None,  # 多BGM文件列表，每个clip随机选一个
    bgm_path: Optional[str] = None,  # 兼容单BGM路径
                   fps: int = 30, bitrate: str = "8M",
                   clip_id: Optional[int] = None,
                   perturbation: Optional[dict] = None) -> Optional[str]:
    """渲染单条视频 — v2.3 增强

    Args:
        perturbation: 可选扰动 dict（由 perturbation.build_perturbation 生成）。
            None 时走旧管线（hardcoded 1080x1920 + 默认编码参数）。
            给定时：应用 scale/rotate/translate/hflip/nonstd_resolution。
            全部应用。

    Returns:
        None=成功，str=错误信息（首行 ffmpeg 错误）。
    """
    if not os.path.exists(audio_path):
        print(f"[渲染错误] 配音文件不存在: {audio_path}")
        return f"配音文件缺失: {audio_path}"
    source_audio_duration = get_media_duration(audio_path)
    if source_audio_duration <= 0:
        return "无法读取配音时长"
    if not script.get("target_video_frames") or not script.get("target_audio_samples"):
        apply_integer_timeline(
            script, source_audio_duration, fps,
            tail_duration=float(script.get("tail_duration", 0.5)),
        )

    clips = script.get("clips", [])
    subtitles = script.get("subtitles", [])
    lang = script.get("lang", "zh")
    if not clips:
        print("[渲染错误] 没有 clip")
        return "脚本无 clip 数据（编排脚本里 clips 字段为空）"

        # 实时进度：开始准备
        if clip_id is not None:
            _safe_progress(clip_id, f"准备 {len(clips)} 段素材...", log=True, clip_idx=script.get("index"))

        tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="autokat_xfade_")

        # Step 1: 每个 clip 单独处理（裁剪+缩放），并行加速
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if clip_id is not None:
            _safe_progress(clip_id, f"切分片段 0/{len(clips)}", log=True, clip_idx=script.get("index"))

        def _src_color_args(path: str) -> list:
            """从源片 probe 颜色元数据, 返回 ffmpeg 输出端显式 -color_* 参数.

            不显式写的话 ffmpeg 默认会填 BT.709 (不限 source), HDR 源 (BT.2020+HLG)
            在不显式写的情况下被 ffmpeg "默默" 改成 BT.709, 视觉上就过曝.
            缓存避免每个 segment 重复 probe.
            """
            try:
                info = get_media_info(path)
                streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
                if not streams:
                    return []
                s = streams[0]
                cs = s.get("color_space") or "bt709"
                cp = s.get("color_primaries") or "bt709"
                ct = s.get("color_transfer") or "bt709"
                cr = s.get("color_range") or "tv"
                # ffmpeg 内部代号: bt2020nc -> bt2020_ncl 等; 保持原值即可
                return [
                    "-colorspace", cs,
                    "-color_primaries", cp,
                    "-color_trc", ct,
                    "-color_range", cr,
                ]
            except Exception:
                return []

        def _render_segment(i, clip):
            src = clip["source_path"]
            offset = clip.get("offset", 0)
            duration_frames = _segment_render_frames(clip, i, len(clips), fps)
            dur = frames_to_seconds(duration_frames, fps)
            seg_file = os.path.join(tmpdir, f"seg{i:04d}.mp4")

            # 同步修复: 视觉时长严格等于音频时长 (dur = clip["duration"] 不再 ±0.3s 抖动)
            # 扰动参数只影响画面 (scale/rotate/translate/hflip/bg/encoding)，
            # 不动 dur 本身，保证下游 SRT/音轨/视频三者 end_time 一致。

            filters = [
                f"trim=start={offset}:duration={dur}",
                "setpts=PTS-STARTPTS",
            ]
            # v2.4: 禁止任何调色 / tonemap. 原素材是什么颜色元数据, 成片就是什么.
            # 之前 _needs_hdr_to_sdr() 对 HDR 源 (BT.2020+HLG) 走 tonemap,
            # 输出被强制成 BT.709/SDR, 在 SDR 显示器上看比原片"亮" (HLG 曲线
            # 被截断到 0-1 范围, 亮部被拉伸). 用户要求"使用原片色调", 这里
            # 不再做任何颜色转换, 由最终输出 -color_range/colorspace/primaries/trc auto
            # 透传源片元数据.

            # --- 1) 水平翻转 (perturbation.flip) ---
            if perturbation and perturbation.get("hflip"):
                filters.append("hflip")

            # --- 2) 缩放 / 旋转 / 平移 (perturbation.scale_rotate) ---
            if perturbation and perturbation.get("scale") is not None:
                scale = perturbation["scale"]
                # 用 scale filter 缩放（保持原比例，force_original_aspect_ratio 由 pad 接管）
                filters.append(f"scale=iw*{scale}:ih*{scale}")
            if perturbation and perturbation.get("rotate_deg") is not None:
                rot = perturbation["rotate_deg"]
                # v2.4: 禁止调色, 旋转 fillcolor 写死 0x000000@1
                filters.append(f"rotate={rot}*PI/180:fillcolor=0x000000@1")

            # --- 4) 分辨率 + pad (perturbation.nonstd_resolution 决定目标尺寸) ---
            if perturbation and perturbation.get("resolution"):
                tw, th = perturbation["resolution"]
            else:
                tw, th = 1080, 1920
            # v2.4: 禁止调色, pad 背景色永远固定为纯黑
            pad_color = "black"
            tx = perturbation.get("tx_px", 0) if perturbation else 0
            ty = perturbation.get("ty_px", 0) if perturbation else 0
            filters.append(
                f"scale={tw}:{th}:force_original_aspect_ratio=1,"
                f"pad={tw}:{th}:({tw}-iw)/2+{tx}:({th}-ih)/2+{ty}:{pad_color}"
            )
            vf = ",".join(filters)

            # segment 是中间产物，用 CPU veryfast（避免 6+ worker 争抢 VT 编码器导致失败）
            # 4M 码率 + veryfast 编码速度极快，节省时间
            # 关键：必须加 -t {dur} 严格限制输出时长！
            # 当前 ffmpeg（/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg）的 trim filter 在
            # 源比 duration 长时不严格截断（trim=start:duration 和 trim=start:end
            # 都会输出到源结束），只有 -t flag 能保证精确时长。
            # 没有这个 flag，seg 文件会包含源的全部内容（seg0000 请求 4.59s
            # 实际输出 5.184s，因为源是 5.182s），下游 xfade 用错时长，整条视频
            # 比脚本期望长几秒到十几秒。
            cmd = [
                FFMPEG, "-y", "-i", src, "-vf", vf,
                "-frames:v", str(duration_frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-g", "60",
                "-pix_fmt", "yuv420p", "-r", str(fps),
                # v2.4: 显式从源片 probe 颜色元数据, 避免 ffmpeg 默认填 BT.709
                *_src_color_args(src),
                seg_file,
            ]
            run_ffmpeg(cmd, desc=f"裁剪片段 {i}")
            # ffprobe 获取实际生成文件的真实时长（素材或裁剪边界可能偏短）
            actual_dur = get_media_duration(seg_file) or dur
            # 实际输出 < 请求 × 50%，意味着 trim 越界（offset 超过素材结尾）
            # 或者源素材本身就比请求短。下游 concat+tpad 会把这一段冻结成静帧
            # 来补齐，但用户看到的就是一段死画面，必须显式告警以便排查
            if actual_dur < dur - 0.10:
                raise RuntimeError(
                    f"segment {i} 实际输出 {actual_dur:.2f}s < 请求 {dur:.2f}s，"
                    f"拒绝使用短素材 (src={os.path.basename(src)} offset={offset:.2f}s)"
                )
            return seg_file, actual_dur

        # 实时进度：每完成 1 段就推一次（避免 50 段时 UI 5 分钟没动静）
        # UI 端 _handle_render_log 用 in-place 替换，所以屏上始终 1 行
        seg_files = []
        seg_durations = []
        # segment 是 CPU 编码（libx264 veryfast），多 worker 时与 Step 2/3 抢 CPU 会拖慢整体
        # 留 4 个并发就够，再多反而是负优化（实测 seg=8 + workers=6 wall 69s vs seg=4 + workers=4 wall 60s）
        with ThreadPoolExecutor(max_workers=min(4, len(clips))) as executor:
            futures = {executor.submit(_render_segment, i, c): i for i, c in enumerate(clips)}
            for future in as_completed(futures):
                seg_file, dur = future.result()
                seg_files.append((futures[future], seg_file, dur))
                if clip_id is not None:
                    # 节流：每完成 2 段（或最后一段）才推一次，避免 ffmpeg 频繁启动期产生 50 条消息
                    # 每段都推一次（100 段时 ffmpeg 启动 ~1s/段，2s 一行足够看；
                    # 50 段以下完全够用，50 段以上也不会刷爆日志框，因为 UI 端是原位替换）
                    if len(seg_files) % 2 == 0 or len(seg_files) >= max(1, len(clips) // 10) or len(seg_files) == len(clips):
                        _safe_progress(clip_id, f"切分片段 {len(seg_files)}/{len(clips)}", log=True, clip_idx=script.get("index"))

        # 按原始顺序排序（先取时长，再裁成纯路径列表）
        seg_files.sort(key=lambda x: x[0])
        seg_durations = [float(s[2]) for s in seg_files]
        seg_files = [s[1] for s in seg_files]

        # 拼接前体检：把所有"实际输出 < 请求 × 70%"的段挑出来集中告警一次
        # 单段 50% 告警在 _render_segment 内做，这里 70% 阈值更松——
        # 目的是让用户在拼接前就看到「这条片子有 N 段会被冻结」，方便排查
        # 是不是某个素材普遍偏短或 offset 设错了
        short_segs = []
        for i_seg, (req_clip, actual_dur_seg) in enumerate(zip(clips, seg_durations)):
            req_dur = float(req_clip.get("duration", 0))
            if req_dur > 0 and actual_dur_seg < req_dur * 0.7:
                short_segs.append((i_seg, actual_dur_seg, req_dur,
                                    os.path.basename(req_clip.get("source_path", "?"))))
        if short_segs:
            raise RuntimeError(f"{len(short_segs)} 个素材片段时长不足，拒绝冻结末帧补齐")

        # 实时进度：进入 xfade 拼接（这一步对 50+ 段输入很重，是主要等待点）
        if clip_id is not None and len(seg_files) > 1:
            # 拼接阶段通常 30s-3min，告诉用户"开始拼了"以及输入段数，让他们知道没卡死
            _safe_progress(clip_id, f"拼接视频 (xfade · {len(seg_files)} 段输入)...", log=True, clip_idx=script.get("index"))
        elif clip_id is not None:
            _safe_progress(clip_id, "复制片段...", log=True, clip_idx=script.get("index"))

        # Step 2: 用 xfade 滤镜拼接
        # xfade 对同时输入的 filtergraph 节点数有限制（通常约 20-30 个输入标签），
        # 段数过多时直接报 "Error binding filtergraph inputs/outputs"。
        # 分批处理：每批最多 5 段，组内 xfade 串联，组间再 concat 合并。
        # 例：11 段 → [1..5] xfade → [6..10] xfade → [组1, 组2, 段11] concat
        if len(seg_files) == 1:
            concat_video = seg_files[0]
            total_video_dur = seg_durations[0]
        else:
            trans_dur = float(clips[0].get("transition_duration", 0.3))
            GROUP_SIZE = 5  # 每批 xfade 输入上限

            def _xfade_group(seg_batch, dur_batch, group_idx):
                """对一批 segment 做链式 xfade，返回输出文件路径和总时长。

                关键修复: 之前用估算的 accumulated 作为返回值，但 xfade filter
                在某些 transition 上输出时长可能比请求短（特别是过渡偏移超
                出已合成的范围时）。任务 223 之后 xfade_group 返回估算时长
                后续 concat + tpad 都基于这个错的值，导致 0004 在 21.3s 后
                冻结（实际视频 ~27s 但被估算成 21s）。
                现在 xfade 跑完后用 get_media_duration(out_path) 读真实输出
                覆盖估算值，确保下游 concat + tpad 用对的时长。
                """
                if len(seg_batch) == 1:
                    return seg_batch[0], dur_batch[0]
                filter_parts = []
                input_labels = []
                for i, seg in enumerate(seg_batch):
                    label = f"g{group_idx}s{i}"
                    input_labels.append(label)
                    filter_parts.append(f"[{i}:v]setpts=PTS-STARTPTS[{label}]")
                current_label = input_labels[0]
                accumulated = dur_batch[0]
                for i in range(1, len(seg_batch)):
                    trans_val = random.choice(XFADE_TRANSITIONS)
                    xfade_label = f"g{group_idx}x{i}"
                    xfade_offset = max(0.0, accumulated - trans_dur)
                    filter_parts.append(
                        f"[{current_label}][{input_labels[i]}]"
                        f"xfade=transition={trans_val}:duration={trans_dur}:offset={xfade_offset}[{xfade_label}]"
                    )
                    current_label = xfade_label
                    accumulated = accumulated + dur_batch[i] - trans_dur
                filter_complex = ";".join(filter_parts)
                out_path = os.path.join(tmpdir, f"group{group_idx}.mp4")
                cmd = [FFMPEG, "-y"]
                for seg in seg_batch:
                    cmd.extend(["-i", seg])
                # v2.4: xfade 组内输出, 用第一个 segment 源片颜色参数 (同 batch 同池共享)
                nonlocal _batch_color_holder
                _batch_color_holder = _src_color_args(seg_batch[0]) if seg_batch else _batch_color_holder
                cmd.extend(_h264_encoder_args("4M"))
                cmd.extend([
                    "-filter_complex", filter_complex,
                    "-map", f"[{current_label}]",
                    "-pix_fmt", "yuv420p", "-r", str(fps), "-an",
                    *_batch_color_holder,
                    # v2.4: 透传源片颜色元数据
                    out_path,
                ])
                run_ffmpeg(cmd, desc=f"xfade 组{group_idx}({len(seg_batch)}段)", timeout=300)
                # 读真实输出时长覆盖估算（关键修复）
                actual_dur = get_media_duration(out_path) or accumulated
                if abs(actual_dur - accumulated) > 0.3:
                    print(f"[xfade 修复] 组{group_idx} 估算 {accumulated:.2f}s → 实际 {actual_dur:.2f}s，"
                          f"差异 {abs(actual_dur - accumulated):.2f}s（xfade filter 边界情况）")
                return out_path, actual_dur

            # 分批 xfade，结果文件列表
            group_files = []
            group_durations = []
            # v2.4: 同 batch 同池的源片颜色 (用 nonlocal 在 _xfade_group 里更新)
            _batch_color_holder = []
            for gi in range(0, len(seg_files), GROUP_SIZE):
                g_segs = seg_files[gi:gi+GROUP_SIZE]
                g_durs = seg_durations[gi:gi+GROUP_SIZE]
                gout_path, gout_dur = _xfade_group(g_segs, g_durs, gi // GROUP_SIZE)
                group_files.append(gout_path)
                group_durations.append(gout_dur)

            # 组间拼接：如果只有一组直接返回；多组用 concat+过渡视频方式合并
            if len(group_files) == 1:
                concat_video = group_files[0]
                total_video_dur = group_durations[0]
            else:
                # 关键修复：之前把每组都 tpad 到 max_dur 再 concat，最终时长 = max_dur * n_groups
                # （视频被拉长近一倍，例：2 组各 19s → concat 后 38s，但脚本期望 21s）。
                # 改用 -c copy 的 concat demuxer 直接拼，每组保留原始时长，
                # 总长 = sum(group_durations)，与脚本 total_duration 一致。
                # 各组之间是硬切（无转场），但每个组内部已经有 xfade 转场，视觉上不突兀。
                padded_groups = list(group_files)

                # concat 拼接（用 filter_complex 方式，文件级 concat 更可靠）
                concat_list = os.path.join(tmpdir, "group_concat_list.txt")
                with open(concat_list, "w") as f:
                    for pg in padded_groups:
                        f.write(f"file '{pg}'\n")
                concat_video = os.path.join(tmpdir, "concated.mp4")
                # 之前用 `-c copy` 的 concat demuxer 在 xfade 输出后 moov atom 的
                # duration 字段会被第一个 group 的 moov 覆盖，导致 get_media_duration
                # 读出错的时长（如 21.3s），进而 total_video_dur 偏小、tpad_dur 算错，
                # 画面在 21s 后就冻结（音频/字幕继续走）。改成 libx264 重新编码走
                # concat demuxer（moov 由 ffmpeg 重新生成），多 1-2s 换稳定时长。
                # 帧率/pix_fmt 与 xfade 输出保持一致，避免色彩空间被改。
                cmd_cat = [
                    FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_list,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-r", str(fps), "-an",
                    # v2.4: 组间 concat 透传源片颜色元数据 (复用 _batch_color 缓存)
                    *_batch_color_holder,
                    # v2.4: 透传源片颜色元数据
                    concat_video,
                ]
                run_ffmpeg(cmd_cat, desc="组合并concat(重编码)", timeout=120)
                # 关键修复：之前用 sum(group_durations) 算的是 padding 之前各组原始时长之和，
                # 但每个组都被 tpad 到 max_dur 再 concat，真实视频时长是 max_dur * n_groups。
                # 下面用 ffprobe 读真实文件时长，避免 tpad 算错导致视频=配音对不上。
                _actual_concat_dur = get_media_duration(concat_video) or 0.0
                if _actual_concat_dur > 0 and abs(_actual_concat_dur - sum(group_durations)) > 0.3:
                    print(f"[tpad 修复] 真实 concat 时长 {_actual_concat_dur:.2f}s ≠ "
                          f"sum(group_durations)={sum(group_durations):.2f}s，"
                          f"采用真实值（max_dur * n_groups 的影响）")
                total_video_dur = _actual_concat_dur or sum(group_durations)

        # 实时进度：进入最终合成（字幕+音频+BGM）
        if clip_id is not None:
            # 最终合成 30-60s，把"总时长"和"是否带 BGM"一起报上去
            detail = f"合成最终视频 (字幕+配音{'+BGM' if bgm_path and os.path.exists(bgm_path) else ''}, 总时长 {total_video_dur:.1f}s)..."
            _safe_progress(clip_id, detail, log=True, clip_idx=script.get("index"))

        # Step 3: subtitles + narration + BGM. The narration clock is authoritative.
        inputs = [concat_video, audio_path]
        bgm_seek = None
        bgm_dur = None
        audio_dur = get_media_duration(audio_path)
        if audio_dur <= 0:
            raise RuntimeError("无法读取配音时长")
        clock = _resolve_target_clock(script, audio_dur, fps)
        final_dur = clock["final_duration"]
        target_frames = clock["target_video_frames"]
        target_samples = clock["target_audio_samples"]
        actual_video_frames = int(round(total_video_dur * fps))
        if actual_video_frames < target_frames - 1:
            raise RuntimeError(
                f"动态画面 {total_video_dur:.3f}s < 目标 {final_dur:.3f}s，"
                f"缺少 {target_frames - actual_video_frames} 帧，拒绝使用明显静止帧补齐"
            )
        if bgm_path and os.path.exists(bgm_path):
            bgm_dur = get_media_duration(bgm_path)
            if bgm_dur and bgm_dur > final_dur + 1.0:
                max_start = max(0.0, bgm_dur - final_dur)
                bgm_seek = random.uniform(0, max_start)
            inputs.append(bgm_path)
        cmd = [FFMPEG, "-y"]
        for i, inp in enumerate(inputs):
            if i == 2:
                # BGM 是第三个输入（index 2）
                if bgm_dur and bgm_dur < final_dur:
                    loop_count = max(1, int(final_dur / bgm_dur) + 2)
                    cmd.extend(["-stream_loop", str(loop_count), "-i", inp])
                else:
                    if bgm_seek is not None:
                        cmd.extend(["-ss", f"{bgm_seek:.2f}", "-i", inp])
                    else:
                        cmd.extend(["-i", inp])
            else:
                cmd.extend(["-i", inp])
        cmd.extend(["-map", "0:v:0"])

        if subtitles:
            srt_path = _make_ass(
                subtitles,
                lang=script.get("lang", "zh"),
                subtitle_position=script.get("subtitle_position"),
                animation=script.get("subtitle_animation", "none"),
                font_name=script.get("subtitle_font"),
                font_size=script.get("font_size"),
            )
            _vf_parts = [
                f"tpad=stop_mode=clone:stop_duration={1 / fps:.9f}",
                f"trim=end_frame={target_frames}",
                "setpts=PTS-STARTPTS",
                f"subtitles={srt_path}",
            ]
        else:
            _vf_parts = [
                f"tpad=stop_mode=clone:stop_duration={1 / fps:.9f}",
                f"trim=end_frame={target_frames}",
                "setpts=PTS-STARTPTS",
            ]
        cmd.extend(["-vf", ",".join(_vf_parts)])

        if bgm_path and os.path.exists(bgm_path):
            fade_start = max(0.0, final_dur - 0.5)
            cmd.extend([
                "-filter_complex",
                f"[1:a]aresample={SAMPLE_RATE}:first_pts=0,volume=1.0,"
                f"apad=whole_len={target_samples},atrim=end_sample={target_samples}[a0];"
                f"[2:a]volume=0.15,afade=t=out:st={fade_start:.3f}:d=0.5,"
                f"aresample={SAMPLE_RATE}:first_pts=0,apad=whole_len={target_samples},"
                f"atrim=end_sample={target_samples}[a1];"
                f"[a0][a1]amix=inputs=2:duration=first,"
                f"atrim=end_sample={target_samples}[outa]",
                "-map", "[outa]",
            ])
        else:
            cmd.extend([
                "-af", f"aresample={SAMPLE_RATE}:first_pts=0,"
                f"apad=whole_len={target_samples},atrim=end_sample={target_samples}",
                "-map", "1:a:0",
            ])

        enc_args = _h264_encoder_args(bitrate)
        cmd.extend(enc_args)
        # v2.4: 最终输出, 透传 _batch_color_holder (同 batch 同池共享)
        cmd.extend([
            "-pix_fmt", "yuv420p", "-r", str(fps),
            *_batch_color_holder,
            # v2.4: 透传源片颜色元数据, 不强制写成 bt709/tv (HDR 源会保留 BT.2020+HLG)
            "-c:a", "aac", "-b:a", "192k",
            "-ar", str(SAMPLE_RATE),
            output_path,
        ])
        # 诊断日志：把最终 ffmpeg 命令的关键参数打印出来
        # 方便排查"tpad/apad 是否真的应用"和"时长对不上"问题
        try:
            _log_cmd = " ".join(repr(x) if " " in str(x) else str(x) for x in cmd)
            # 截断过长的 log（不要把整个 ffmpeg 命令塞进日志）
            print(f"[时长诊断] script_final={script.get('final_duration')} "
                  f"video={total_video_dur:.3f}s audio={audio_dur:.3f}s "
                  f"target={final_dur:.6f}s frames={target_frames} "
                  f"samples={target_samples} tolerance={SYNC_TOLERANCE_SECONDS:.3f}s "
                  f"bgm={'yes' if bgm_path and os.path.exists(bgm_path) else 'no'}")
        except Exception:
            pass

        input_audio_onset = audio_onset(audio_path) if subtitles else None
        run_ffmpeg(cmd, desc="最终渲染(字幕+音频+BGM)", timeout=600)
        if subtitles and input_audio_onset is not None:
            output_audio_onset = audio_onset(output_path)
            encoding_shift = (
                output_audio_onset - input_audio_onset
                if output_audio_onset is not None else 0.0
            )
            if abs(encoding_shift) > 0.05:
                for subtitle in subtitles:
                    subtitle["start"] = round(max(0.0, subtitle["start"] + encoding_shift), 6)
                    subtitle["end"] = round(min(final_dur, subtitle["end"] + encoding_shift), 6)
                corrected_ass = _make_ass(
                    subtitles,
                    lang=script.get("lang", "zh"),
                    subtitle_position=script.get("subtitle_position"),
                    animation=script.get("subtitle_animation", "none"),
                    font_name=script.get("subtitle_font"),
                    font_size=script.get("font_size"),
                )
                vf_index = cmd.index("-vf") + 1
                cmd[vf_index] = re.sub(r"subtitles=[^,]+", f"subtitles={corrected_ass}", cmd[vf_index])
                script["subtitle_encoding_offset"] = round(encoding_shift, 6)
                print(f"[字幕校准] 最终 AAC 起始偏移 {encoding_shift * 1000:.1f}ms，重新渲染字幕层")
                run_ffmpeg(cmd, desc="最终渲染(编码偏移校准重试)", timeout=600)
        probe = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,duration", "-of", "json", output_path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        probe_data = json.loads(probe.stdout)
        format_dur = float(probe_data.get("format", {}).get("duration") or 0)
        stream_durs = {
            stream.get("codec_type"): float(stream.get("duration") or 0)
            for stream in probe_data.get("streams", [])
        }
        durations = [format_dur, stream_durs.get("video", 0), stream_durs.get("audio", 0)]
        max_pair_diff = max(durations) - min(durations)
        max_target_diff = max(abs(duration - final_dur) for duration in durations)
        print(
            f"[同步校验] format/video/audio={durations} "
            f"pair_diff={max_pair_diff * 1000:.3f}ms "
            f"target_diff={max_target_diff * 1000:.3f}ms"
        )
        if (
            any(duration <= 0 for duration in durations)
            or max_pair_diff > SYNC_TOLERANCE_SECONDS
            or max_target_diff > SYNC_TOLERANCE_SECONDS
        ):
            raise RuntimeError(
                f"最终时长不一致: target={final_dur:.3f}, "
                f"format/video/audio={durations}, "
                f"pair_diff={max_pair_diff * 1000:.1f}ms"
            )
        freeze_duration = _tail_freeze_duration(output_path)
        if freeze_duration > 0.5:
            raise RuntimeError(f"检测到尾部静止画面 {freeze_duration:.2f}s，拒绝输出")
        return None  # 成功

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")
        Path('/tmp/ffmpeg_last_err.txt').write_text(err)
        print(f"[渲染错误] FFmpeg 返回码={e.returncode}")
        print(err[-2000:] if len(err)>2000 else err)
        for line in err.split("\n"):
            if "error" in line.lower() and "[" in line and "size" not in line.lower():
                print(f"  {line.strip()}")
                break
        else:
            print(f"  {err[:200]}")
        # 提取最有信息量的一行：通常是 ffmpeg 的最后一行 "[xxx] xxx error"
        first_error_line = ""
        for line in err.split("\n"):
            if "error" in line.lower() and "[" in line and "size" not in line.lower():
                first_error_line = line.strip()
                break
        if not first_error_line:
            first_error_line = err.split("\n")[-2].strip() if err.count("\n") >= 2 else err[:200].strip()
        return f"ffmpeg 退出码 {e.returncode}: {first_error_line[:300]}"
    except subprocess.TimeoutExpired:
        output_file = os.path.basename(output_path) if 'output_path' in dir() else "?"
        print(f"[渲染错误] FFmpeg 超时 (>=300s) - {output_file}")
        return f"ffmpeg 超时 (>=300s) - {output_file}"
    except Exception as e:
        import traceback
        print(f"[渲染异常] {type(e).__name__}: {e}")
        traceback.print_exc()
        return f"渲染异常: {type(e).__name__}: {str(e)[:200]}"
    finally:
        if tmpdir and os.path.exists(tmpdir):
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

def create_and_run_batch(
    script_id: int,
    narration_text: str,
    narration_config: Optional[dict] = None,
    count: int = 100,
    workers: int = 2,
    fps: int = 30,
    enable_bgm: bool = False,
    bgm_path: Optional[str] = None,
    bgm_files: Optional[list[str]] = None,  # 多BGM文件列表，每个clip随机选一个
    lang: str = "zh",
    material_ids: Optional[list[int]] = None,
    config: Optional[dict] = None,
    subtitle_position: Optional[str] = None,
    log_fn=None,
) -> int:
    from autokat.models.db import create_task as db_create_task
    from autokat.core.progress_log import set_stage as _set_stage

    clear_stop_request()

    # 统一日志出口：有 log_fn 就走 log_fn（GUI 显示），否则落 print（CLI 终端）
    def _log(msg: str) -> None:
        if log_fn is not None:
            try:
                log_fn(msg)
            except Exception:
                pass
        else:
            print(msg)

    init_db()
    ensure_dirs()

    _log("─── [1/4] 生成配音 ───")
    _set_stage("🎙️ 配音生成中 (1/4)")
    from autokat.core.tts import LANG_CONFIG
    _cfg = LANG_CONFIG.get(lang, LANG_CONFIG["zh"])
    voice = (narration_config or {}).get("voice", _cfg["voice"])
    rate = (narration_config or {}).get("rate", _cfg["rate"])
    pitch = (narration_config or {}).get("pitch", _cfg["pitch"])

    import hashlib
    name_hash = hashlib.md5(narration_text.encode()).hexdigest()[:8]

    # 多段文案（--- 分隔）：每段独立 TTS，5 段 → 5 个短视频（每个 15-30s）
    # 避免之前 bug：5 段拼一起变成 154s 音频 + 5 个 2 分多视频
    parts = [p.strip() for p in narration_text.split("---") if p.strip()]
    is_multi = len(parts) > 1
    if is_multi:
        _log(f"   🔊 共 {len(parts)} 段文案，逐段合成…")
        audio_segments = []  # [(audio_path, sentences, dur), ...]
        for i, part in enumerate(parts):
            _set_stage(f"🎙️ 配音生成中 · 段 {i+1}/{len(parts)}")
            # 逐句 TTS 进度回调：让 UI 顶部的"当前活动"标签每完成一句更新一次，
            # 避免 30s+ 的 TTS 期间界面静止让用户误以为卡死
            def _on_tts_sentence(done: int, total: int, sentence: str, _seg_idx: int = i) -> None:
                preview = (sentence[:18] + "…") if len(sentence) > 18 else sentence
                _set_stage(f"🎙️ 配音 · 段 {_seg_idx+1}/{len(parts)} · 句 {done}/{total}：{preview}")
            try:
                seg = generate_narration(
                    part, voice=voice, rate=rate, pitch=pitch,
                    output_name=f"script_{name_hash}_{i}",
                    lang=lang,
                    on_sentence=_on_tts_sentence,
                )
            except Exception as exc:
                raise RuntimeError(f"第 {i+1}/{len(parts)} 段配音或字幕时间轴失败: {exc}") from exc
            if not seg:
                raise RuntimeError(f"第 {i+1} 段配音生成失败")
            for sentence in seg["sentences"]:
                sentence["_narration_duration"] = seg["total_duration"]
            audio_segments.append((seg["audio_path"], seg["sentences"], seg["total_duration"]))
            seg_name = Path(seg["audio_path"]).name
            seg_size = _fmt_size(os.path.getsize(seg["audio_path"])) if os.path.exists(seg["audio_path"]) else "?"
            _log(f"      段 {i+1}/{len(parts)}: {seg['total_duration']:.1f}s  →  {seg_name}  ({seg_size})")
    else:
        # 单段文案：原有逻辑
        _set_stage("🎙️ 配音生成中")
        def _on_tts_sentence(done: int, total: int, sentence: str) -> None:
            preview = (sentence[:24] + "…") if len(sentence) > 24 else sentence
            _set_stage(f"🎙️ 配音 · 句 {done}/{total}：{preview}")
        seg = generate_narration(
            parts[0], voice=voice, rate=rate, pitch=pitch,
            output_name=f"script_{name_hash}",
            lang=lang,
            on_sentence=_on_tts_sentence,
        )
        if not seg:
            raise RuntimeError("配音生成失败，请检查语音是否支持当前文字内容")
        for sentence in seg["sentences"]:
            sentence["_narration_duration"] = seg["total_duration"]
        audio_segments = [(seg["audio_path"], seg["sentences"], seg["total_duration"])]

    total_audio_dur = sum(s[2] for s in audio_segments)
    total_audio_bytes = sum(
        os.path.getsize(s[0]) for s in audio_segments if os.path.exists(s[0])
    )
    _log(
        f"✅ 配音完成: {len(parts)} 段  ·  "
        f"总时长 {total_audio_dur:.1f}s  ·  文件 {_fmt_size(total_audio_bytes)}"
    )
    if stop_requested():
        raise RuntimeError("生成已停止")

    _log("─── [2/4] 编排脚本 ───")
    _set_stage(f"📝 编排脚本生成中 (2/4) · 0/{count}")
    pool = build_material_pool(mat_ids=material_ids)
    if not pool:
        raise RuntimeError("素材池为空，请先导入素材")
    _avg_dur = sum(m.get("duration") or 0 for m in pool) / len(pool) if pool else 0
    _total_dur = sum(m.get("duration") or 0 for m in pool)
    _log(
        f"   🧮 素材池: {len(pool)} 个 video  ·  "
        f"平均时长 {_avg_dur:.1f}s  ·  总素材时长 {_total_dur:.1f}s"
    )

    # 多段时：每段分配 1 个视频（count 必须 ≥ 段数）
    if is_multi and count < len(parts):
        # 强制把 count 拉高到段数
        _log(f"   [多段] count={count} < {len(parts)} 段，自动调整 count={len(parts)}")
        count = len(parts)

    # 分配每段给哪几个 clip
    if is_multi:
        # 段数 = N，每个段分配 videos_per_seg 个 clip（最后一组吃剩余）
        videos_per_seg = max(1, count // len(parts))
        seg_for_clip = []
        for seg_idx in range(len(parts)):
            for _ in range(videos_per_seg):
                seg_for_clip.append(seg_idx)
            if len(seg_for_clip) >= count:
                break
        # 补齐 count 个
        while len(seg_for_clip) < count:
            seg_for_clip.append(len(parts) - 1)
        seg_for_clip = seg_for_clip[:count]
    else:
        seg_for_clip = [0] * count

    batch_cfg = {
        # allow_reuse=True：每个视频内部允许复用素材；去重由 editor.py 的 fallback 机制在素材耗尽时兜底
        # 不再设 allow_reuse=False（会导致后续视频无素材可用）
        "allow_reuse": True, "fps": fps, "narration_text": parts[0],
        "transition_duration": (narration_config or {}).get("transition_duration", 0.3),
        "subtitle_position": subtitle_position,
    }
    if config:
        batch_cfg.update(config)  # 调用方传入的 min_shot_duration 等覆盖默认
    sentence_groups = [audio_segments[idx][1] for idx in seg_for_clip]
    batch_scripts = generate_batch(
        audio_segments[0][1], count=count, material_pool=pool, config=batch_cfg,
        sentence_groups=sentence_groups,
    )
    _div_report = batch_scripts[0].get("diversity_report", {}) if batch_scripts else {}
    if _div_report:
        _log(
            "   ♻️ 多样性: "
            f"切片覆盖 {_div_report.get('slice_coverage', 0):.0%}  ·  "
            f"未使用 {_div_report.get('unused_slices', 0)}  ·  "
            f"最大复用 {_div_report.get('max_slice_uses', 0)} 次  ·  "
            f"最大组合相似度 {_div_report.get('max_slice_jaccard', 0):.0%}"
        )
    if stop_requested():
        raise RuntimeError("生成已停止")
    # 统计 batch 里所有 script 的 clip 数（一次素材引用 = 一个 clip）
    _total_shot_refs = sum(len(s.get("clips", [])) for s in batch_scripts)

    config = {
        "fps": fps, "count": count, "voice": voice,
        "rate": rate, "pitch": pitch, "enable_bgm": enable_bgm,
        "lang": lang,
    }
    task_id = db_create_task(
        script_id=script_id, config=config,
        output_dir=str(OUTPUT_DIR), total=count,
    )

    _log("─── [3/4] 输出准备 ───")
    _set_stage(f"💾 编排脚本保存中 (3/4) · 0/{count}")
    for script in batch_scripts:
        # 多段时每段用自己段的音频（之前是错的，5 个视频全用 154s 的拼起来的音频）
        seg_idx = seg_for_clip[script["index"]] if seg_for_clip else 0
        seg_path, seg_sents, seg_dur = audio_segments[seg_idx]
        script["audio_path"] = seg_path
        script["narration_text"] = parts[seg_idx]
        apply_integer_timeline(script, seg_dur, fps, tail_duration=0.5)
        script["color_processing"] = "bt709_passthrough_or_hdr_to_sdr_mobius"
        if enable_bgm:
            if bgm_files:
                script["bgm_path"] = random.choice(bgm_files)
            elif bgm_path:
                script["bgm_path"] = bgm_path
            else:
                script["bgm_path"] = None
        script_file = SCRIPTS_DIR / f"task_{task_id}_clip_{script['index']:04d}.json"
        # 将语言设置写入编排脚本（字幕渲染需要）
        script["lang"] = lang
        save_script_to_file(script, str(script_file))
        add_clip(task_id, script["index"], str(script_file))
        # 100 条 save_script_to_file + add_clip 是磁盘 IO，50 条时 ~3s；
        # 每 10 条刷一次 stage 让用户知道还在写
        if (script["index"] + 1) % 10 == 0 or (script["index"] + 1) == count:
            _set_stage(f"💾 编排脚本保存中 · {script['index']+1}/{count}")

    _log(f"   📐 为 {count} 条成片规划切片组合…  ·  共 {_total_shot_refs} 个切片引用")
    _log(f"─── [4/4] 启动渲染  (并发: {workers}) ───")
    _set_stage(f"🎬 视频渲染中 · 0/{count}")
    update_task_status(task_id, "running")
    _render_task(task_id, workers, batch_cfg=batch_cfg)

    return task_id


def _render_task(task_id: int, workers: int = 2, batch_cfg: Optional[dict] = None):
    _render_start_ts = time.time()
    from autokat.core.progress_log import emit as _log_emit_fn
    def _log(msg: str) -> None:
        try:
            _log_emit_fn(msg)
        except Exception:
            pass
    task = get_task(task_id)
    if not task:
        return

    clips = get_pending_clips(task_id)
    if not clips:
        print("  没有待渲染的成片")
        return

    # 整个 task 共用一个目录：{脚本名}_{YYYYMMDDHHMMSS}（一次性算好）
    from autokat.models.db import get_script_by_id
    _script_obj = get_script_by_id(task.get("script_id")) or {}
    raw_name = _script_obj.get("name") or f"task_{task_id:04d}"
    safe_name = "".join(c if (c.isalnum() or c in " _-") else "_" for c in str(raw_name)).strip().replace(" ", "_")[:60] or f"task_{task_id:04d}"
    # 目录名格式: {任务ID}_{任务名}_{时间戳}
    # 例: 125_玛丽珍珠_20260608140105
    # 之前只有 {任务名}_{时间戳}，用户取名"未命名"或留空时根本分不清哪个目录是哪个任务
    task_ts = datetime.now().strftime("%Y%m%d%H%M%S")
    task_dir = OUTPUT_DIR / f"{task_id}_{safe_name}_{task_ts}"
    task_dir.mkdir(parents=True, exist_ok=True)
    _log(f"   📂 输出目录: output/{task_dir.name}")

    from autokat.core.progress_log import set_stage as _set_stage
    _total_clips = len(clips)

    def _render_one(clip: dict) -> bool:
        script = load_script_from_file(clip["script_path"])
        # v2.3 E1: 把 batch_cfg 里的字幕字体/字号/动效 merge 到 script (下游 _make_ass 用)
        for _k in ("subtitle_font", "font_size", "subtitle_animation", "subtitle_position"):
            _v = (batch_cfg or {}).get(_k)
            if _v is not None and (not script.get(_k) or _k in ("subtitle_font", "subtitle_animation", "font_size")):
                script[_k] = _v
        output_filename = f"{safe_name}_{clip['idx']+1:04d}.mp4"
        output_path = str(task_dir / output_filename)
        audio_path = script.get("audio_path", "")
        bgm_path = script.get("bgm_path")

        clip_idx = clip.get("idx", 0)
        total = len(clips)
        try:
            _log_emit(f"▶️  [{clip_idx+1}/{total}] 第 {clip_idx+1} 条开始  · 编排: {Path(clip['script_path']).name}")
        except Exception:
            pass
        t0 = time.time()
        update_clip_status(clip["id"], "rendering")
        # render_simple 现在返回 Optional[str] — None=成功，str=错误信息（含 ffmpeg stderr 关键行）
        # 这样失败时 error_msg 写进 DB，任务详情页能直接看到根因（之前是写死的"渲染失败"）
        # v2.3: 从 batch_cfg / config 读 perturbation 强度, 为每条成片生成独立扰动参数
        # perturbation_level: off/low/med/high（默认 med）
        _pert_level = (batch_cfg or {}).get("perturbation_level", "med")
        _pert = build_perturbation(_pert_level) if is_level_enabled(_pert_level) else None
        err_msg = render_simple(
            script, output_path, audio_path,
            bgm_path=bgm_path, fps=script.get("fps", 30),
            clip_id=clip["id"],
            perturbation=_pert,
        )
        elapsed = time.time() - t0
        if err_msg is None:
            # 渲染成功后 ffprobe 拿真实时长写回 DB（任务详情/Step 4 显示用）
            try:
                dur = get_media_duration(output_path) if os.path.exists(output_path) else None
            except Exception:
                dur = None
            fsize = _fmt_size(os.path.getsize(output_path)) if os.path.exists(output_path) else "?"
            update_clip_status(clip["id"], "done", output_path=output_path, duration=dur)
            try:
                if dur:
                    _log_emit(f"✅  [{clip_idx+1}/{total}] 第 {clip_idx+1} 条完成  · 渲染 {elapsed:.0f}s  · 实际成片 {dur:.1f}s  · {fsize}")
                else:
                    _log_emit(f"✅  [{clip_idx+1}/{total}] 第 {clip_idx+1} 条完成  · 渲染 {elapsed:.0f}s  · {fsize}")
            except Exception:
                pass
        else:
            update_clip_status(clip["id"], "failed", error_msg=err_msg)
            try:
                # 日志里只放前 120 字（err_msg 可能很长含 ffmpeg stderr 全文）
                _log_emit(f"❌  [{clip_idx+1}/{total}] 第 {clip_idx+1} 条失败  · {err_msg[:120]}")
            except Exception:
                pass
        return err_msg is None

    done_count = get_task_clip_counts(task_id)["done"]
    def _task_stop_requested() -> bool:
        current = get_task(task_id)
        if stop_requested() and current and current["status"] == "running":
            update_task_status(task_id, "failed", done=done_count)
            current = get_task(task_id)
        return stop_requested() or bool(current and current["status"] in ("pending", "failed"))

    if workers > 1:
        # 多进程并行渲染
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(lambda c=c: _render_one(c)): c for c in clips}
            for future in as_completed(futures):
                if future.result():
                    done_count += 1
                # 暂停检测必须放在 update_task_status 之前，否则会被自己覆盖成 running
                if _task_stop_requested():
                    print(f"  ⏸ 任务 #{task_id} 已暂停或停止，跳过剩余 clip（{done_count}/{len(clips)} 已完成）")
                    for f in futures:
                        f.cancel()
                    return
                update_task_status(task_id, "running", done=done_count)
                _set_stage(f"🎬 视频渲染中 · {done_count}/{_total_clips}")
    else:
        # 单线程
        for c in clips:
            # 暂停检测放在 _render_one 之前（也防止 _render_one 内调 update_clip_status 干扰判断）
            if _task_stop_requested():
                print(f"  ⏸ 任务 #{task_id} 已暂停或停止，跳过剩余 clip（{done_count}/{len(clips)} 已完成）")
                return
            if _render_one(c):
                done_count += 1
            if _task_stop_requested():
                print(f"  ⏸ 任务 #{task_id} 已暂停或停止，跳过剩余 clip（{done_count}/{len(clips)} 已完成）")
                return
            update_task_status(task_id, "running", done=done_count)
            _set_stage(f"🎬 视频渲染中 · {done_count}/{_total_clips}")

    final_counts = get_task_clip_counts(task_id)
    done_count = final_counts["done"]
    final_status = "done" if done_count == final_counts["total"] else "failed"
    update_task_status(task_id, final_status, done=done_count)
    # ── Phase 5：渲染收尾 ──
    elapsed_total = time.time() - _render_start_ts
    fail_count = final_counts["failed"]
    out_size = _dir_size(task_dir)
    out_files = sum(1 for _ in task_dir.glob("*.mp4")) if task_dir.exists() else 0
    _log("─── 渲染收尾 ───")
    if fail_count == 0:
        _log(f"   📊 统计: {len(clips)} 条  ·  成功 {done_count}  ·  失败 0")
    else:
        _log(f"   📊 统计: {len(clips)} 条  ·  成功 {done_count}  ·  失败 {fail_count}")
    _log(f"   ⏱️ 渲染阶段耗时: {elapsed_total:.0f}s")
    _log(f"   📁 输出目录: {task_dir}  ({out_files} 文件 · {_fmt_size(out_size)})")
    _log(f"   🆔 任务 #{task_id}  →  状态: {final_status}")
    # v2.4: 写 titles.txt (同 batch 共用一条 AI 标题, 一行一条 filename\ttitle)
    try:
        from autokat.core.writer import generate_publish_title as _gen_title
        _title_src = (_script_obj.get("narration") or "").split("---", 1)[0].strip()
        _script_lang = _script_obj.get("lang") or "zh"
        _title_lang = _script_lang.split("-", 1)[0]
        if _title_lang not in ("zh", "en", "th"):
            _title_lang = "zh"
        _publish_title = _gen_title(_title_src, lang=_title_lang, max_chars=20)
        if _publish_title:
            _titles_path = task_dir / "titles.txt"
            with open(_titles_path, "w", encoding="utf-8") as _tf:
                for _i in range(int(task.get("total") or len(clips))):
                    _fn = f"{safe_name}_{_i+1:04d}.mp4"
                    _tf.write(f"{_fn}\t{_publish_title}\n")
            _log(f"   📝 发布标题已生成 → titles.txt ({task.get('total') or len(clips)} 行, 标题: {_publish_title})")
    except Exception as _t_exc:
        print(f"[titles] 生成/写盘失败 (不影响渲染): {_t_exc}")
    _log("─── 全部完成 ✅ ───")


def resume_pending_tasks(workers: int = 2):
    from autokat.models.db import get_pending_tasks
    clear_stop_request()
    pending = get_pending_tasks()
    if not pending:
        print("没有未完成的任务")
        return
    for task in pending:
        print(f"恢复任务 task_id={task['id']}...")
        _render_task(task["id"], workers)
