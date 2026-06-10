"""BGM 管理 + 随机混配

用 librosa 做节拍检测和音频对齐，实现 BGM 与配音的智能混配。
"""

import os
import random
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional
import json

from autokat.core.ffmpeg_utils import FFMPEG, FFPROBE
from autokat.core.paths import ASSETS_ROOT, BUNDLED_ASSETS_ROOT

BGM_DIR = ASSETS_ROOT / "bgm"
BUNDLED_BGM_DIR = BUNDLED_ASSETS_ROOT / "bgm"
BGM_DIR.mkdir(parents=True, exist_ok=True)

# ── BGM 管理 ──

def get_bgm_files() -> list[str]:
    """获取 BGM 目录中所有音频文件"""
    files = set()
    for directory in (BGM_DIR, BUNDLED_BGM_DIR):
        if directory.exists():
            files.update(
                str(f) for f in directory.iterdir()
                if f.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
            )
    return sorted(files)


def pick_random_bgm(exclude: Optional[list[str]] = None) -> Optional[str]:
    """随机选取一首 BGM（优先选未用过的）"""
    files = get_bgm_files()
    if not files:
        return None

    if exclude:
        remaining = [f for f in files if f not in exclude]
        if remaining:
            return random.choice(remaining)

    return random.choice(files)


def get_bgm_duration(bgm_path: str) -> Optional[float]:
    """获取 BGM 时长"""
    cmd = [
        FFPROBE, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        bgm_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return None


# ── 节拍检测 + 自动裁剪 ──

def detect_bpm(bgm_path: str) -> Optional[float]:
    """检测 BGM 的 BPM（节拍数/分钟）

    使用 librosa 进行节拍检测。
    返回 BPM 值，失败时返回 None。
    """
    try:
        import librosa
        import numpy as np

        # 取中段 30s：避开前奏/尾奏，更能代表整曲的稳定节拍
        dur = get_bgm_duration(bgm_path)
        if dur and dur > 60:
            offset = max(0.0, (dur - 30) / 2)
            y, sr = librosa.load(bgm_path, sr=None, offset=offset, duration=30)
        else:
            y, sr = librosa.load(bgm_path, sr=None)  # 短曲全取
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        return float(tempo)
    except Exception as e:
        print(f"[BGM] BPM 检测失败: {e}")
        return None


def auto_trim_bgm(bgm_path: str, target_duration: float,
                  output_path: str) -> Optional[str]:
    """根据目标时长自动裁剪 BGM

    如果 BGM 比目标时长短，循环拼接；
    如果 BGM 比目标时长长，随机截取一段。

    Args:
        bgm_path: 原始 BGM 路径
        target_duration: 目标时长（秒）
        output_path: 输出路径

    Returns:
        输出路径，或 None（失败时）
    """
    dur = get_bgm_duration(bgm_path)
    if dur is None:
        return None

    tmpdir = None
    try:
        if dur >= target_duration:
            # 随机截取一段
            max_start = max(0, dur - target_duration)
            start = random.uniform(0, max_start)
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", bgm_path,
                "-t", str(target_duration),
                "-c", "copy",
                output_path,
            ]
        else:
            # 循环拼接
            # 先用 concat 循环
            loop_count = int(target_duration / dur) + 1
            tmpdir = tempfile.mkdtemp(prefix="autokat_bgm_")
            filelist = os.path.join(tmpdir, "list.txt")
            with open(filelist, "w") as f:
                for _ in range(loop_count):
                    f.write(f"file '{bgm_path}'\n")

            concat_path = os.path.join(tmpdir, "looped.mp3")
            cmd_concat = [
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0",
                "-i", filelist,
                "-c", "copy",
                concat_path,
            ]
            subprocess.run(cmd_concat, check=True, capture_output=True, timeout=30)

            # 再截取目标时长
            cmd = [
                "ffmpeg", "-y",
                "-i", concat_path,
                "-t", str(target_duration),
                "-c", "copy",
                output_path,
            ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        return output_path

    except Exception as e:
        print(f"[BGM] 裁剪失败: {e}")
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── BGM 素材库 ──

def download_sample_bgm(output_dir: Optional[str] = None) -> list[str]:
    """下载几首免费可商用的 BGM 样本

    从 Freesound、Pixabay 等免费音源下载轻量 BGM。
    如果没有网络，则提示用户手动放入。

    Returns:
        下载的文件路径列表
    """
    # 由于版权原因，这里只提供指引
    info = """📝 BGM 使用说明：

    1. 将你的 BGM 文件放入 assets/bgm/ 目录
    2. 支持的格式: mp3, wav, m4a, flac, ogg
    3. 免费商用 BGM 推荐来源:
       - Pixabay Music (pixabay.com/music)
       - Free Music Archive (freemusicarchive.org)
       - Uppbeat (uppbeat.io)
       - YouTube Audio Library (studio.youtube.com)
    4. 没有 BGM 时系统会自动跳过，不影响成片生成
    """
    print(info)
    return []


# ── 混音参数 ──

def get_mix_params(bgm_path: str, perturbation: Optional[dict] = None) -> dict:
    """获取 BGM 混音参数

    根据 BGM 类型和 BPM 给出推荐的配音/音乐音量比。
    v2.3 增强：可选 perturbation 字典让 BGM 抖动（音量 + 淡入淡出时长），
    从同一条 BGM 也能产生差异化的混合效果。
    """
    bpm = detect_bpm(bgm_path)

    # 默认音量参数
    params = {
        "bgm_volume": 0.12,    # BGM 音量（相对配音）
        "narration_volume": 1.0,
        "fade_in": 1.0,        # BGM 淡入时长
        "fade_out": 2.0,       # BGM 淡出时长
    }

    # 根据 BPM 调整
    if bpm:
        if bpm > 120:
            params["bgm_volume"] = 0.08  # 快节奏降低音量
        elif bpm < 80:
            params["bgm_volume"] = 0.15  # 慢节奏可以稍大

    # v2.3: perturbation 抖动
    if perturbation is not None:
        if perturbation.get("bgm_volume_jitter") is not None:
            params["bgm_volume"] = max(0.02, min(0.4, params["bgm_volume"] * perturbation["bgm_volume_jitter"]))
        if perturbation.get("fade_jitter") is not None:
            f = perturbation["fade_jitter"]
            params["fade_in"] = max(0.3, params["fade_in"] * f)
            params["fade_out"] = max(0.5, params["fade_out"] * f)

    return params


# ── 智能拆段 ──

def split_bgm_to_segments(
    bgm_path: str,
    output_dir: Optional[str] = None,
    segment_length: float = 30.0,
    num_segments: int = 3,
    skip_existing: bool = True,
) -> list[dict]:
    """智能拆 BGM 为多个高质量短段

    用 librosa 计算整首的 RMS 能量曲线，贪心挑选能量最高且互不重叠的
    N 段（默认 30s × 3 段），导出到 output_dir（默认与 BGM 同目录）。
    配合 pick_random_bgm() 等价于把 3 个长 BGM 扩展成 ~9 段随机池。
    """
def split_bgm_to_segments(
    bgm_path: str,
    output_dir: Optional[str] = None,
    segment_length: float = 30.0,
    num_segments: int = 3,
    min_gap: float = 3.0,
    skip_existing: bool = True,
) -> list[dict]:
    """智能拆 BGM 为多个高质量短段

    用 librosa 计算整首的 RMS 能量曲线，贪心挑选能量最高且互不重叠的
    N 段（默认 30s × 3 段），导出到 output_dir（默认与 BGM 同目录）。
    配合 pick_random_bgm() 等价于把 3 个长 BGM 扩展成 ~9 段随机池。

    Args:
        bgm_path: 原始 BGM 路径
        output_dir: 输出目录（默认同 BGM 目录）
        segment_length: 每段时长（秒），默认 30
        num_segments: 拆几段，默认 3
        min_gap: 段间最小间隔（秒），默认 3（避免挑出的段挤在一起）
        skip_existing: 已存在的输出文件是否跳过

    Returns:
        [{"path", "start", "end", "energy", "skipped"}, ...]
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        print("[BGM] 需要安装 librosa: pip install librosa")
        return []

    from autokat.core.ffmpeg_utils import FFMPEG, run_ffmpeg

    bgm_path = os.path.abspath(bgm_path)
    p = Path(bgm_path)
    out_dir = Path(output_dir) if output_dir else p.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    dur = get_bgm_duration(bgm_path)
    if dur is None or dur < segment_length + 2.0:
        print(f"[BGM] {p.name} 时长 {dur:.1f}s 太短（<{segment_length + 2:.0f}s），跳过拆段")
        return []

    # 1) 加载音频 + 算 RMS 能量曲线
    y, sr = librosa.load(bgm_path, sr=None, mono=True)
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    times = librosa.times_like(rms, sr=sr, hop_length=hop)

    # 2) 滑动窗口（1 秒步长）算每段平均能量
    seg_frames = int(segment_length * sr / hop)
    step_frames = int(1.0 * sr / hop)
    if seg_frames >= len(rms):
        print(f"[BGM] {p.name} 时长不足以容纳一段 {segment_length}s")
        return []

    candidates = []  # (start_sec, avg_rms, end_sec)
    for s in range(0, len(rms) - seg_frames + 1, step_frames):
        e = s + seg_frames
        avg = float(np.mean(rms[s:e]))
        candidates.append((float(times[s]), avg, float(times[min(e - 1, len(times) - 1)])))

    if not candidates:
        print(f"[BGM] {p.name} 无法解析能量曲线")
        return []

    # 3) 贪心挑能量最高的不重叠段（要求段间至少留 min_gap 秒空白）
    candidates.sort(key=lambda x: -x[1])
    picked = []
    for start, energy, end in candidates:
        conflict = any(
            not (end + min_gap <= ps or start >= pe + min_gap)
            for ps, _, pe in picked
        )
        if not conflict:
            picked.append((start, energy, end))
            if len(picked) >= num_segments:
                break

    # 4) 按时间排序后导出
    picked.sort(key=lambda x: x[0])
    results = []
    for idx, (start, energy, end) in enumerate(picked):
        letter = chr(ord("a") + idx)
        out_path = out_dir / f"{p.stem}_seg_{letter}.mp3"

        if skip_existing and out_path.exists():
            results.append({
                "path": str(out_path),
                "start": start,
                "end": end,
                "energy": energy,
                "skipped": True,
            })
            continue

        cmd = [
            FFMPEG, "-y",
            "-ss", f"{start:.2f}",
            "-i", bgm_path,
            "-t", f"{segment_length:.2f}",
            "-c", "copy",
            str(out_path),
        ]
        try:
            run_ffmpeg(cmd, desc=f"拆段 {p.name}#{letter}", timeout=60)
            results.append({
                "path": str(out_path),
                "start": start,
                "end": end,
                "energy": energy,
                "skipped": False,
            })
        except Exception as e:
            print(f"[BGM] 拆段失败 {p.name}#{letter}: {e}")

    return results
