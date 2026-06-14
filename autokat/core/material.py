"""素材预处理 — 导入、标准化、视频拆分子镜头、Ken Burns 动效"""

import hashlib
import json
import functools
import random
from pathlib import Path
from typing import Optional
from concurrent.futures import ProcessPoolExecutor, as_completed

from PIL import Image
from tqdm import tqdm

from autokat.models.db import add_material, get_all_materials
from autokat.core.paths import ASSETS_ROOT

# ── FFmpeg 路径（优先用 ffmpeg-full 确保字幕 filter 可用） ---
from autokat.core.ffmpeg_utils import FFMPEG, FFPROBE, run_ffmpeg, get_media_duration, get_media_info

# ── 常量 ──
TARGET_W = 1080
TARGET_H = 1920
MIN_CLIP_DUR = 1.0   # 子镜头最短 1s
MAX_CLIP_DUR = 3.0   # 子镜头最长 3s



# ── 命名工具 ──

def _sanitize_display_name(stem: str) -> str:
    """把原始文件名 stem 清洗成可读 display_name。
    - 替换不安全字符为 _
    - 去掉头尾空白/点
    - 限制 60 字符
    """
    import re as _re
    s = _re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', stem or '')
    s = s.strip(' ._')
    if not s:
        s = "素材"
    return s[:60]


def _resolve_display_name_conflict(base: str) -> str:
    """如果 base 已存在于 materials.display_name，自动加 (2)/(3)... 直到不冲突。"""
    from autokat.models.db import get_conn
    conn = get_conn()
    name = base
    n = 1
    while True:
        row = conn.execute("SELECT 1 FROM materials WHERE display_name=?", (name,)).fetchone()
        if not row:
            conn.close()
            return name
        n += 1
        name = f"{base}({n})"


def _clip_display_name(source_name: str, idx: int) -> str:
    """生成切片可读名：「源名 [001]」。idx 永远递增所以切片名天然唯一。"""
    return f"{source_name} [{idx:03d}]"


MIN_VIDEO_DUR = 3.0  # 原视频最短 3s，否则不拆分

ASSETS_DIR = ASSETS_ROOT
ASSETS_IMAGES = ASSETS_DIR / "images"
ASSETS_VIDEOS = ASSETS_DIR / "videos"
ASSETS_KENBURNS = ASSETS_DIR / "kenburns"
ASSETS_CLIPS = ASSETS_DIR / "clips"

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".avi", ".mkv"}

# 确保目录存在
for d in [ASSETS_IMAGES, ASSETS_VIDEOS, ASSETS_KENBURNS, ASSETS_CLIPS]:
    d.mkdir(parents=True, exist_ok=True)


# ── 工具函数 ──




def _file_hash(filepath: str) -> str:
    """SHA256 文件哈希，用于去重"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_video_info(filepath: str) -> tuple:
    """获取视频宽高、时长(s)"""
    try:
        info = get_media_info(filepath)
        streams = info.get("streams", [])
        if streams:
            s = streams[0]
            w = int(s.get("width", 0))
            h = int(s.get("height", 0))
            dur = float(s.get("duration", 0) or 0)
            return w, h, dur
    except Exception:
        pass
    return 0, 0, 0


def _get_image_size(filepath: str) -> tuple:
    """获取图片尺寸"""
    try:
        with Image.open(filepath) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


# ── 图片预处理 ──

def _process_image(filepath: str) -> Optional[dict]:
    """处理单张图片：缩放至竖屏、复制到 assets/images"""
    fpath = Path(filepath)
    file_hash = _file_hash(filepath)

    # 检查是否已存在
    existing = get_all_materials("image")
    for m in existing:
        if m["file_hash"] == file_hash:
            return None  # 已入库，跳过

    dest = ASSETS_IMAGES / f"{file_hash[:12]}{fpath.suffix}"
    w, h = _get_image_size(filepath)

    # 缩放至 1080x1920
    cmd = [
        FFMPEG, "-y",
        "-i", str(fpath),
        "-vf", f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=1,pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2",
        "-q:v", "2",
        str(dest)
    ]
    try:
        run_ffmpeg(cmd, desc=f"缩放图片 {fpath.name}", timeout=120)
    except Exception:
        return None
        return None

    img_display = _resolve_display_name_conflict(_sanitize_display_name(fpath.stem))
    mat_id = add_material(
        file_path=str(dest),
        file_hash=file_hash,
        mat_type="image",
        width=TARGET_W,
        height=TARGET_H,
        display_name=img_display
    )
    return {"id": mat_id, "path": str(dest), "type": "image"}


def _generate_kenburns(image_path: str, output_dir: Path,
                       duration: float = 2.5, fps: int = 30) -> Optional[Path]:
    """为单张图片生成 Ken Burns 动效视频片段

    用 ffmpeg 的 zoompan 实现推拉/平移效果。
    如果 zoompan 不可用，回退到简单缩放+循环。
    """
    img = Path(image_path)
    stem = img.stem
    out_path = output_dir / f"{stem}_kenburns.mp4"

    # 简单可靠的 Ken Burns：用 scale + fps + 截取一部分
    # 先生成图片序列，然后合成视频
    cmd = [
        FFMPEG, "-y",
        "-loop", "1",
        "-i", str(img),
        "-vf", f"scale={int(TARGET_W*1.15)}:{int(TARGET_H*1.15)}:flags=lanczos,"
               f"crop={TARGET_W}:{TARGET_H}:(iw-{TARGET_W})*t/{duration}:0,"
               f"fps={fps}",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out_path)
    ]
    try:
        run_ffmpeg(cmd, desc=f"KenBurns {img.name}", timeout=120)
    except Exception:
        pass
    if out_path.exists():
        kb_display = _resolve_display_name_conflict(f"[KB] {_sanitize_display_name(img.stem)}")
        _mat_id = add_material(
            file_path=str(out_path),
            file_hash=_file_hash(str(out_path)),
            mat_type="video",
            duration=duration,
            width=TARGET_W,
            height=TARGET_H,
            tags=["kenburns"],
            display_name=kb_display
        )
        return _mat_id
    # 走 fallback 路径
    
    # 如果 crop 方式失败，用最简单的缩放+循环
    cmd2 = [
        FFMPEG, "-y",
        "-loop", "1",
        "-i", str(img),
        "-vf", f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=1,"
               f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,"
               f"fps={fps}",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out_path)
    ]
    try:
        run_ffmpeg(cmd2, desc=f"KenBurns(回退) {img.name}", timeout=120)
    except Exception:
        return None
    if out_path.exists():
        kb_display = _resolve_display_name_conflict(f"[KB] {_sanitize_display_name(img.stem)}")
        _mat_id = add_material(
            file_path=str(out_path),
            file_hash=_file_hash(str(out_path)),
            mat_type="video",
            duration=duration,
            width=TARGET_W,
            height=TARGET_H,
            tags=["kenburns"],
            display_name=kb_display
        )
        return _mat_id
    return None

    return None
# ── 视频预处理 ──

def _split_video(filepath: str) -> list[dict]:
    """将短视频拆分为 1~3s 子镜头"""
    fpath = Path(filepath)
    w, h, dur = _get_video_info(filepath)
    if dur < MIN_VIDEO_DUR:
        return []

    file_hash = _file_hash(filepath)
    # 源视频可读名（去冲突）
    base = _sanitize_display_name(fpath.stem)
    source_display = _resolve_display_name_conflict(base)
    # 落盘仍用 hash 防重，display_name 单独存
    dest = ASSETS_VIDEOS / f"{file_hash[:12]}{fpath.suffix}"

    # 先标准化缩放
    cmd = [
        FFMPEG, "-y",
        "-i", str(fpath),
        "-vf", f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=1,pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(dest)
    ]
    try:
        run_ffmpeg(cmd, desc=f"标准化视频 {fpath.name}", timeout=120)
    except Exception:
        return []

    # 入库原始素材
    mat_id = add_material(
        file_path=str(dest),
        file_hash=file_hash,
        mat_type="video",
        duration=dur,
        width=TARGET_W,
        height=TARGET_H,
        display_name=source_display
    )
    # 如果已存在（INSERT OR IGNORE 返回 0），从数据库查询 id
    if mat_id == 0:
        from autokat.models.db import get_all_materials
        for _m in get_all_materials("video"):
            if _m["file_hash"] == file_hash:
                mat_id = _m["id"]
                break

    # 拆分 1~3s 子镜头
    clips = []
    current = 0.0
    seg_idx = 0
    while current < dur - 0.5:
        seg_dur = random.uniform(MIN_CLIP_DUR, min(MAX_CLIP_DUR, dur - current))
        seg_dur = max(MIN_CLIP_DUR, min(seg_dur, dur - current))
        if seg_dur < MIN_CLIP_DUR:
            break

        # 切片可读文件名：源名 [001].mp4，冲突时（罕见）加 hash 后缀
        clip_name = f"{source_display} [{seg_idx:03d}].mp4"
        clip_path = ASSETS_CLIPS / clip_name
        if clip_path.exists():
            # 同名源已存在切片，hash 后缀避免覆盖
            clip_name = f"{file_hash[:8]}_{source_display} [{seg_idx:03d}].mp4"
            clip_path = ASSETS_CLIPS / clip_name

        cmd = [
            FFMPEG, "-y",
            "-ss", f"{current:.2f}",
            "-i", str(dest),
            "-t", f"{seg_dur:.2f}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-avoid_negative_ts", "1",
            str(clip_path)
        ]
        try:
            run_ffmpeg(cmd, desc=f"拆分 {fpath.name} seg{seg_idx}", timeout=120)
        except Exception:
            current += seg_dur
            continue
        
        clip_mat_id = add_material(
            file_path=str(clip_path),
            file_hash=_file_hash(str(clip_path)),
            mat_type="video",
            duration=seg_dur,
            width=TARGET_W,
            height=TARGET_H,
            clip_parent=mat_id,
            display_name=_clip_display_name(source_display, seg_idx)
        )
        clips.append({
            "id": clip_mat_id,
            "path": str(clip_path),
            "duration": seg_dur,
            "type": "video"
        })
        seg_idx += 1
        current += seg_dur

    return clips


# ── 批量导入 ──

def import_files(filepaths: list[str],
                 generate_kenburns: bool = True,
                 workers: int = 1) -> dict:
    """快速登记原始素材；兼容旧调用签名，不再导入时转码或物理切片。"""
    from autokat.core.import_jobs import ImportJobService
    service = ImportJobService()
    return service.process_job(service.create_job(filepaths))


def build_material_pool(mat_types: Optional[list[str]] = None,
                        mat_ids: Optional[list[int]] = None,
                        include_legacy_slices: bool = False) -> list[dict]:
    """构建素材池

    只包含 video 类型（含 Ken Burns），因为 image 需要实时转 video。
    image 类型会由调用方通过 kenburns 映射处理。

    结果缓存通过 functools.lru_cache 加速重复调用。
    导入新素材后调用 clear_material_pool_cache() 清除缓存。

    Args:
        mat_types: 按类型过滤
        mat_ids: 按素材 ID 过滤，为 None 则不过滤
    """
    _key = (tuple(sorted(mat_types)) if mat_types else (),
            tuple(sorted(mat_ids)) if mat_ids else (),
            bool(include_legacy_slices))
    pool = _build_material_pool_cached(_key)
    if mat_ids is not None:
        id_set = set(mat_ids)
        pool = [m for m in pool if m["id"] in id_set or m["source_id"] in id_set]
    return pool


@functools.lru_cache(maxsize=4)
def _build_material_pool_cached(_key: tuple) -> list[dict]:
    pool = []
    from autokat.models.db import get_conn
    conn = get_conn()
    virtual_by_material = {}
    for row in conn.execute("SELECT * FROM virtual_slices ORDER BY material_id,start_frame").fetchall():
        virtual_by_material.setdefault(row["material_id"], []).append(dict(row))
    analysis_by_material = {
        row["material_id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM material_analysis WHERE status='done'"
        ).fetchall()
    }
    conn.close()
    for m in get_all_materials():
        if _key[0] and m["mat_type"] not in _key[0]:
            continue
        if m["mat_type"] not in ("video", "image"):
            continue
        # Legacy physical slices remain readable by old scripts, but new tasks
        # use original assets plus virtual slices to avoid short-shot and color
        # artifacts from the retired import-time transcode path.
        if m.get("source_kind") == "legacy_slice" and not _key[2]:
            continue
        if m["mat_type"] == "video" and m["duration"] <= 0:
            continue
        fp = m["file_path"]
        # 跳过文件不存在的素材（防渲染失败）
        if not __import__("os").path.exists(fp):
            print(f"[素材] 跳过: {fp} 文件不存在")
            continue
        virtuals = virtual_by_material.get(m["id"], []) if not m.get("clip_parent") else []
        analysis = analysis_by_material.get(m["clip_parent"] or m["id"], {})
        analysis_tags = [
            analysis.get(key) for key in ("subject", "shot_type", "action", "scene", "content_role")
            if analysis.get(key)
        ]
        combined_tags = list(dict.fromkeys(json.loads(m["tags"] or "[]") + analysis_tags))
        if virtuals:
            for virtual in virtuals:
                virtual_tags = json.loads(virtual.get("capability_tags") or "[]")
                slice_tags = list(dict.fromkeys(combined_tags + virtual_tags))
                pool.append({
                    "id": -int(virtual["id"]),
                    "virtual_slice_id": int(virtual["id"]),
                    "source_id": m["id"],
                    "path": fp,
                    "duration": virtual["duration_frames"] / float(virtual["fps"]),
                    "base_offset": virtual["start_frame"] / float(virtual["fps"]),
                    "type": m["mat_type"],
                    "tags": slice_tags,
                    "quality_score": max(
                        float(analysis.get("quality_score") or 0),
                        float(virtual.get("hotspot_score") or 0),
                    ),
                    "hotspot_score": float(virtual.get("hotspot_score") or 0),
                    "capability_summary": analysis.get("capability_summary") or "",
                })
        else:
            pool.append({
                "id": m["id"],
                "source_id": m["clip_parent"] or m["id"],
                "path": fp,
                "duration": m["duration"] if m["mat_type"] == "video" else 30.0,
                "type": m["mat_type"],
                "tags": combined_tags,
                "quality_score": float(analysis.get("quality_score") or 0),
                "capability_summary": analysis.get("capability_summary") or "",
            })
    return pool


def clear_material_pool_cache():
    """导入新素材后调用，清除缓存"""
    _build_material_pool_cached.cache_clear()

# ── 带进度回调的批量导入（供 UI 使用） ──

def import_files_with_callback(
    filepaths: list[str],
    on_progress: callable,
    generate_kenburns: bool = True,
) -> dict:
    """Create and process a persistent import job with UI-compatible callbacks."""
    from autokat.core.import_jobs import ImportJobService
    service = ImportJobService()
    return service.process_job(service.create_job(filepaths), on_progress=on_progress)
