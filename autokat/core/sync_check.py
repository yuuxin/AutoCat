"""音/视/字幕时长一致性检查

渲染完成后调用 verify_sync() 检查:
- 实际视频时长 vs 配音时长 vs SRT 最后一句 end time
三者差 > 阈值就报警（不阻断, 但日志显眼）。

app-test 套件会强制跑这个 check。
"""

from pathlib import Path
from typing import Optional

from autokat.core.ffmpeg_utils import get_media_duration


def parse_srt_last_end(srt_path: str) -> Optional[float]:
    """从 SRT 文件读最后一句字幕的 end 时间。"""
    if not Path(srt_path).exists():
        return None
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        time_codes = []
        for line in content.splitlines():
            if "-->" in line:
                parts = line.split("-->", 1)
                if len(parts) == 2:
                    end_ts = parts[1].strip().split()[0]
                    time_codes.append(_srt_ts_to_seconds(end_ts))
        return time_codes[-1] if time_codes else None
    except (OSError, ValueError):
        return None


def _srt_ts_to_seconds(ts: str) -> float:
    """SRT 时间戳 'HH:MM:SS,mmm' -> 秒。"""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(ts) if ts else 0.0


def verify_sync(
    video_path: str,
    audio_path: Optional[str] = None,
    srt_last_end: Optional[float] = None,
    srt_path: Optional[str] = None,
    tolerance: float = 0.1,
) -> list:
    """检查视频/音频/字幕三者时长一致性。

    Args:
        video_path: 输出视频路径
        audio_path: 配音音频路径（None 跳过音频检查）
        srt_last_end: SRT 最后一句 end 时间（None 跳过字幕检查）
        srt_path: 备选 -- 直接传 SRT 文件路径, 内部会解析
        tolerance: 允许的时长差（秒）

    Returns:
        警告消息列表（空列表 = 全部通过）
    """
    warnings = []

    if not Path(video_path).exists():
        return ["[sync] video file not found: " + str(video_path)]

    video_dur = get_media_duration(video_path)
    if video_dur <= 0:
        warnings.append(f"[sync] cannot read video duration: {video_path}")
        return warnings

    # video vs audio
    if audio_path and Path(audio_path).exists():
        audio_dur = get_media_duration(audio_path)
        if audio_dur > 0:
            diff = abs(video_dur - audio_dur)
            if diff > tolerance:
                warnings.append(
                    f"[sync] audio/video duration mismatch: "
                    f"video {video_dur:.2f}s vs audio {audio_dur:.2f}s, "
                    f"diff {diff:.2f}s > {tolerance:.2f}s tolerance"
                )

    # srt vs video (and vs audio if provided)
    if srt_last_end is None and srt_path:
        srt_last_end = parse_srt_last_end(srt_path)
    if srt_last_end is not None and srt_last_end > 0:
        if srt_last_end > video_dur + tolerance:
            warnings.append(
                f"[sync] subtitle extends past video: "
                f"last subtitle end {srt_last_end:.2f}s > video {video_dur:.2f}s"
            )
        if audio_path and Path(audio_path).exists():
            audio_dur = get_media_duration(audio_path)
            if audio_dur > 0:
                sub_audio_diff = abs(srt_last_end - audio_dur)
                if sub_audio_diff > 0.3:
                    warnings.append(
                        f"[sync] subtitle vs audio end time gap: "
                        f"subtitle {srt_last_end:.2f}s vs audio {audio_dur:.2f}s, "
                        f"diff {sub_audio_diff:.2f}s > 0.3s"
                    )

    return warnings
