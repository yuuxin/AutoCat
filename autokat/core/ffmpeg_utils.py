"""FFmpeg 路径管理与工具函数 — 统一入口"""

import os
import subprocess
import json
from pathlib import Path

# ── FFmpeg 路径 ──
FFMPEG_CANDIDATES = [
    os.environ.get("AUTOKAT_FFMPEG", ""),
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    # 打包后内嵌路径
    str(Path(__file__).resolve().parent.parent.parent / "dist" / "AutoCat.app" / "Contents" / "Resources" / "ffmpeg"),
    str(Path(__file__).resolve().parent.parent / "Resources" / "ffmpeg"),
]

FFMPEG = None
FFPROBE = None

for _p in FFMPEG_CANDIDATES:
    if _p and os.path.exists(_p):
        FFMPEG = _p
        FFPROBE = _p.replace("ffmpeg", "ffprobe")
        if not os.path.exists(FFPROBE):
            FFPROBE = _p[:-6] + "ffprobe" if _p.endswith("ffmpeg") else _p + "_probe"
        break

if not FFMPEG:
    # 最后回退 PATH
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"


def run_ffmpeg(cmd: list, desc: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
    """运行 FFmpeg 命令，失败时打印详细错误"""
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


def get_media_duration(filepath: str) -> float:
    """获取音视频文件的时长(秒)

    优先用 ffprobe -show_entries format=duration 读 moov atom（毫秒级返回）。
    如果读不到或返回 0，再 fallback 用 ffmpeg 真正解码读最后一帧的 PTS
    （最可靠但慢几秒）。任务 223 之前因为依赖 moov 字段，concat demuxer
    -c copy 后 moov duration 不准时 total_video_dur 算错 → 画面冻结。
    """
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
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
            [FFMPEG, "-v", "quiet", "-i", filepath,
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


def get_media_info(filepath: str) -> dict:
    """获取音视频文件的宽高、时长等信息"""
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_streams", "-show_format", filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    except Exception:
        return {}
