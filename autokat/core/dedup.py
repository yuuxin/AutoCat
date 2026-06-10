"""感知哈希去重 — 素材级 + 成片级

导入素材时：文件 hash 去重（已在 material.py 中实现）
成片排重：用 imagehash 计算视频帧感知哈希，剔除高相似度成片
"""

import json
import hashlib
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional

import imagehash
from PIL import Image

from autokat.models.db import get_all_materials, get_conn, add_material

# ── 视频感知哈希 ──

def video_perceptual_hash(video_path: str, num_frames: int = 5) -> Optional[list]:
    """计算视频的感知哈希

    抽取均匀分布的 num_frames 帧，每帧计算 phash，
    返回帧哈希列表用于相似度比较。

    Args:
        video_path: 视频文件路径
        num_frames: 采样帧数

    Returns:
        [ImageHash, ...] 列表，或 None（失败时）
    """
    try:
        # 用 ffprobe 获取视频时长
        from autokat.core.ffmpeg_utils import FFPROBE
        cmd_dur = [
            FFPROBE, "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            video_path,
        ]
        dur = float(subprocess.run(cmd_dur, capture_output=True, text=True, timeout=15).stdout.strip())
        if dur <= 0:
            dur = 10  # 默认

        # 均匀采样 num_frames 帧
        hashes = []
        for i in range(num_frames):
            t = dur * (i + 0.5) / num_frames
            fd, frame_path = tempfile.mkstemp(suffix=".png", prefix="autokat_hash_")
            os.close(fd)

            cmd = [
                FFPROBE.replace("ffprobe", "ffmpeg"),
                "-y",
                "-ss", str(t),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                frame_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)

            # 计算感知哈希
            img = Image.open(frame_path)
            phash = imagehash.phash(img)
            hashes.append(phash)

            os.unlink(frame_path)

        return hashes

    except Exception as e:
        return None


def video_similarity(hash1: list, hash2: list) -> float:
    """比较两段视频的感知哈希相似度

    Returns:
        0.0~1.0，越大越相似
    """
    if not hash1 or not hash2:
        return 0.0

    # 逐帧比较取平均
    min_len = min(len(hash1), len(hash2))
    if min_len == 0:
        return 0.0

    total_dist = 0
    for i in range(min_len):
        # hamming distance / max possible (64 for phash)
        dist = (hash1[i] - hash2[i]) / 64.0
        total_dist += dist

    avg_dist = total_dist / min_len
    return 1.0 - avg_dist


# ── 成片去重 ──

# 默认相似度阈值。v2.3 起从 config.dedup_threshold 读，缺省 0.78（v2 之前是 0.85）。
# 阈值越低 = 越严格 = 越容易判重 = 同 batch 内的成片之间相似度差异更大。
# 0.78 是经验值：imagehash 单一指标不如抖音多模态 embedding 严，阈值放低一点
# 能在本地层先于抖音拦截"高度相似"成片。
DUPLICATE_THRESHOLD = 0.78  # 相似度超过此阈值视为重复


def check_duplicate(new_video_path: str, existing_videos: list[str],
                    threshold: Optional[float] = None):
    """检查新视频是否与已有视频重复。threshold=None 时用模块默认。"""
    if threshold is None:
        threshold = DUPLICATE_THRESHOLD
    new_hash = video_perceptual_hash(new_video_path)
    if not new_hash:
        return []

    duplicates = []
    for existing_path in existing_videos:
        if not os.path.exists(existing_path):
            continue

        existing_hash = video_perceptual_hash(existing_path)
        if not existing_hash:
            continue

        sim = video_similarity(new_hash, existing_hash)
        if sim >= threshold:
            duplicates.append({
                "path": existing_path,
                "similarity": sim,
            })

    return duplicates


def dedup_output_dir(output_dir: str, threshold: Optional[float] = None):
    """对输出目录中的视频进行两两去重, threshold=None 用模块默认。"""
    if threshold is None:
        threshold = DUPLICATE_THRESHOLD
    video_files = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.endswith(".mp4")
    ])

    if len(video_files) < 2:
        return 0

    # 计算所有视频的哈希
    print(f"[去重] 计算 {len(video_files)} 个视频的感知哈希...")
    hashes = {}
    for vf in video_files:
        h = video_perceptual_hash(vf)
        if h:
            hashes[vf] = h

    # 两两比较
    removed = 0
    paths = list(hashes.keys())
    for i in range(len(paths)):
        if not os.path.exists(paths[i]):
            continue
        for j in range(i + 1, len(paths)):
            if not os.path.exists(paths[j]):
                continue
            sim = video_similarity(hashes[paths[i]], hashes[paths[j]])
            if sim >= threshold:
                # 删除文件较小的那个
                size_i = os.path.getsize(paths[i])
                size_j = os.path.getsize(paths[j])
                if size_i <= size_j:
                    os.unlink(paths[i])
                    removed += 1
                    break  # paths[i] 已删除，不再比较
                else:
                    os.unlink(paths[j])

    print(f"[去重] 完成，删除了 {removed} 个重复视频")
    return removed


# ── 素材级去重 ──

def check_material_duplicate(filepath: str) -> Optional[int]:
    """检查素材是否已存在（基于文件 hash）

    Returns:
        已有素材的 id，或 None（无重复）
    """
    file_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            file_hash.update(chunk)

    hash_str = file_hash.hexdigest()
    materials = get_all_materials()
    for m in materials:
        if m["file_hash"] == hash_str:
            return m["id"]

    return None
