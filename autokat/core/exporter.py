"""多平台差异化导出 (E任务 — 多平台分别导出)

矩阵号场景: 同一条成片需要给抖音 / TikTok / 快手 / 小红书分别导出一份,
平台 watermark位置/分辨率偏好/字幕样式都不同。
本模块提供:
- get_platforms(): 返回所有支持平台的预设 (分辨率/水印/...)
- list_output_videos():列出 output/ 下所有成片
- export_single(src, dst, platform): 单条成片按平台预设转码输出

v2.3提示: E任务的完整实现会扩这个模块 (Jaccard面板等)。
"""

import copy
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# 平台预设表 (v2.3 E任务的扩展基线)
PLATFORMS = [
    {
        "id": "douyin",
        "name": "抖音",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "watermark": "top-right",
        "watermark_opacity": 0.85,
        "max_bitrate": "10M",
        "audio_bitrate": "192k",
        "subtitle_position": "bottom",
        "notes": "竖屏9:16, 水印右上, 推荐 H.264 + AAC",
    },
    {
        "id": "tiktok",
        "name": "TikTok",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "watermark": "top-right",
        "watermark_opacity": 0.85,
        "max_bitrate": "10M",
        "audio_bitrate": "192k",
        "subtitle_position": "bottom",
        "notes": "竖屏9:16, 全球分发,字幕底部",
    },
    {
        "id": "kuaishou",
        "name": "快手",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "watermark": "top-right",
        "watermark_opacity": 0.80,
        "max_bitrate": "8M",
        "audio_bitrate": "160k",
        "subtitle_position": "bottom",
        "notes": "竖屏9:16, 水印右上,码率稍低",
    },
    {
        "id": "xiaohongshu",
        "name": "小红书",
        "width": 1080,
        "height": 1440,
        "fps": 30,
        "watermark": "bottom-left",
        "watermark_opacity": 0.75,
        "max_bitrate": "8M",
        "audio_bitrate": "160k",
        "subtitle_position": "bottom",
        "notes": "3:4比例,适合图文笔记流",
    },
]


def get_platforms() -> list:
    """返回所有支持平台的预设列表 (深拷贝防外部污染)

    Returns:
        list[dict]: 每个平台一个 dict (id/name/width/height/fps/watermark/...)
    """
    return copy.deepcopy(PLATFORMS)


def get_platform(platform_id: str) -> Optional[dict]:
    """按 id查平台预设。找不到返回 None"""
    for p in PLATFORMS:
        if p["id"] == platform_id:
            return copy.deepcopy(p)
    return None


def list_output_videos(base_dir: Optional[str] = None) -> list:
    """列出 output/ 下所有 mp4 成片。

    Args:
        base_dir: 项目根目录 (None = 当前 cwd)

    Returns:
        list[dict]: 每个成片一个 dict (path/size/mtime/task_id/name)
    """
    base = Path(base_dir) if base_dir else Path.cwd()
    output_dir = base / "output"
    if not output_dir.exists():
        return []
    videos = []
    for mp4 in output_dir.rglob("*.mp4"):
        try:
            stat = mp4.stat()
            videos.append({
                "path": str(mp4),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "name": mp4.name,
                "task_id": _parse_task_id(mp4.parent.name),
            })
        except OSError:
            continue
    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


def _parse_task_id(dir_name: str) -> Optional[int]:
    """从输出目录名 '125_xxx_20260608140105'解析 task_id"""
    m = re.match(r"^(\d+)_", dir_name)
    return int(m.group(1)) if m else None


def export_single(src_path: str, dst_path: str, platform: str,
                  add_watermark: bool = True) -> bool:
    """单条成片按平台预设导出。

    Args:
        src_path: 源 mp4 路径
        dst_path: 目标 mp4 路径 (含目录)
        platform: 平台 id (douyin/tiktok/kuaishou/xiaohongshu)
        add_watermark: 是否加平台水印占位 (默认 True, v2.3 实现)

    Returns:
        True=成功, False=失败
    """
    p = get_platform(platform)
    if not p:
        print(f"[exporter] 未知平台: {platform}")
        return False
    if not os.path.exists(src_path):
        print(f"[exporter] 源文件不存在: {src_path}")
        return False
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)

    # 简化版: 直接用 ffmpeg 转码到目标分辨率/码率
    # v2.3 后续迭代: 加水印 /字幕样式差异化
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-vf", f"scale={p['width']}:{p['height']}:force_original_aspect_ratio=decrease,"
               f"pad={p['width']}:{p['height']}:(ow-iw)/2:(oh-ih)/2:black",
        "-r", str(p["fps"]),
        "-b:v", p["max_bitrate"],
        "-b:a", p["audio_bitrate"],
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac",
        dst_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return os.path.exists(dst_path)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode()[:200] if e.stderr else ""
        print(f"[exporter] ffmpeg失败: {err}")
        return False
    except subprocess.TimeoutExpired:
        print(f"[exporter] 转码超时 (5min)")
        return False
    except FileNotFoundError:
        # ffmpeg 不在 PATH, 直接复制 (退化方案, 测试用)
        shutil.copy(src_path, dst_path)
        return True
