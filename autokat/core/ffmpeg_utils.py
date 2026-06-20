"""FFmpeg path management using AutoCat's private tool bundle only."""

from functools import lru_cache
import re
import subprocess
import json

from autokat.core.tool_paths import ToolNotFoundError, tool_path


FFMPEG = str(tool_path("ffmpeg", required=False))
FFPROBE = str(tool_path("ffprobe", required=False))


@lru_cache(maxsize=4)
def _probe_xfade_transitions(ffmpeg: str) -> frozenset[str]:
    """Return transition names accepted by this exact FFmpeg binary.

    FFmpeg 6.0 only exposes xfade transitions 0..45, while 6.1 adds the
    wind/cover/reveal variants.  AutoCat ships different builds per
    architecture, so a static superset makes rendering fail at random.
    """
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "filter=xfade"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = f"{result.stdout}\n{result.stderr}"
        names = {
            match.group(1)
            for line in help_text.splitlines()
            if (match := re.match(r"^\s+([a-z][a-z0-9]*)\s+-?\d+\s+", line))
        }
        names.discard("custom")
        if "fade" in names:
            return frozenset(names)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # "fade" is the oldest/default xfade transition and is the safest choice
    # if capability probing is unavailable.
    return frozenset({"fade"})


@lru_cache(maxsize=1)
def get_supported_xfade_transitions() -> frozenset[str]:
    """Return xfade transitions supported by AutoCat's bundled FFmpeg."""
    ffmpeg, _ = require_media_tools()
    return _probe_xfade_transitions(ffmpeg)


def require_media_tools() -> tuple[str, str]:
    """Return validated private FFmpeg paths or raise a user-facing error."""
    return (
        str(tool_path("ffmpeg", required=True)),
        str(tool_path("ffprobe", required=True)),
    )


def run_ffmpeg(cmd: list, desc: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
    """运行 FFmpeg 命令，失败时打印详细错误"""
    ffmpeg, _ = require_media_tools()
    if not cmd:
        raise ValueError("FFmpeg command cannot be empty")
    if cmd[0] in {"ffmpeg", FFMPEG}:
        cmd = [ffmpeg, *cmd[1:]]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return result
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace")[:500]
        print(f"[FFmpeg 错误] {desc}")
        print(f"  返回码: {e.returncode}")
        print(f"  命令: {' '.join(cmd[:8])}...")
        for line in err.split("\n"):
            if "error" in line.lower() and "[" in line:
                print(f"  {line.strip()}")
                break
        else:
            print(f"  {err[:200]}")
        raise
    except subprocess.TimeoutExpired:
        print(f"[FFmpeg 超时] {desc} (timeout={timeout}s)")
        raise
    except ToolNotFoundError:
        raise


def get_media_duration(filepath: str) -> float:
    """获取音视频文件的时长(秒)

    优先用 ffprobe -show_entries format=duration 读 moov atom（毫秒级返回）。
    如果读不到或返回 0，再 fallback 用 ffmpeg 真正解码读最后一帧的 PTS
    （最可靠但慢几秒）。任务 223 之前因为依赖 moov 字段，concat demuxer
    -c copy 后 moov duration 不准时 total_video_dur 算错 → 画面冻结。
    """
    try:
        ffmpeg, ffprobe = require_media_tools()
    except ToolNotFoundError:
        return 0.0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        v = float(result.stdout.strip())
        if v > 0:
            return v
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    # Fallback: 用 ffmpeg 真正解码读最后一帧 PTS
    try:
        result = subprocess.run(
            [ffmpeg, "-v", "quiet", "-i", filepath,
             "-map", "0:v:0", "-vf", "showinfo", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        import re as _re
        last_pts = 0.0
        for line in result.stderr.splitlines():
            m = _re.search(r"pts_time:([\d.]+)", line)
            if m:
                try:
                    p = float(m.group(1))
                    if p > last_pts:
                        last_pts = p
                except ValueError:
                    pass
        return last_pts
    except (subprocess.TimeoutExpired, OSError):
        return 0.0


def get_video_duration(filepath: str) -> float:
    """获取首个视频流时长，忽略缓存文件中意外残留的音轨。"""
    try:
        _, ffprobe = require_media_tools()
    except ToolNotFoundError:
        return 0.0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        value = float(result.stdout.strip().splitlines()[0])
        if value > 0:
            return value
    except (ValueError, IndexError, subprocess.TimeoutExpired, OSError):
        pass
    return get_media_duration(filepath)


def get_media_info(filepath: str) -> dict:
    """获取音视频文件的宽高、时长等信息"""
    try:
        _, ffprobe = require_media_tools()
    except ToolNotFoundError:
        return {}
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_streams", "-show_format", filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    except Exception:
        return {}
