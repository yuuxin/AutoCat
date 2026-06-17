"""Import-time-only local material understanding and capability summaries."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from autokat.core.ffmpeg_utils import FFMPEG, run_ffmpeg
from autokat.core.paths import ASSETS_ROOT, BUNDLED_MODELS_ROOT
from autokat.models.db import get_conn


ANALYSIS_VERSION = "mobileclip-s0-v4"
VISUAL_MODEL = BUNDLED_MODELS_ROOT / "mobileclip_s0_image.onnx"
VISUAL_LABELS = BUNDLED_MODELS_ROOT / "mobileclip_s0_labels.npz"

LABEL_RULES = {
    "subject": {
        "女鞋": ("女鞋", "鞋", "玛丽珍珠", "高跟", "平底"),
        "人物": ("人", "模特", "穿搭", "口播"),
        "商品": ("商品", "产品", "细节", "展示"),
    },
    "action": {
        "行走": ("走", "步行", "街拍"),
        "展示": ("展示", "细节", "特写", "旋转"),
        "讲解": ("口播", "讲解", "介绍"),
    },
    "scene": {
        "室内": ("室内", "房间", "店内"),
        "街道": ("街", "户外", "通勤"),
        "棚拍": ("棚拍", "白底", "静物"),
    },
}


def _match_label(text: str, group: str, default: str) -> str:
    for label, keywords in LABEL_RULES[group].items():
        if any(keyword in text for keyword in keywords):
            return label
    return default


def _representative_image(material: dict) -> Image.Image:
    source = Path(material["file_path"])
    # v3.22 守护: 启动时如果 DB 残留了测试 fixture 的 /tmp/xxx.mp4 假路径
    # (来自 unit test 写入), 抽帧会触发 ffmpeg 报 'No such file' 把日志刷屏.
    # 在调 ffmpeg 之前先 check 文件存在, 不存在直接 raise 让上层跳过/标记 failed.
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(
            f"素材文件不存在 (id={material.get('id')}, path={source}). "
            f"该记录可能来自测试 fixture 残留, 建议清理 DB."
        )
    if material["mat_type"] == "image":
        return Image.open(source).convert("RGB")
    with tempfile.TemporaryDirectory(prefix="autokat_analysis_") as tmp:
        frame = Path(tmp) / "frame.jpg"
        seek = max(
            0.0,
            float(material.get("analysis_seek") or float(material["duration"] or 0) * 0.5),
        )
        run_ffmpeg(
            [FFMPEG, "-y", "-ss", f"{seek:.3f}", "-i", str(source),
             "-frames:v", "1", "-q:v", "2", str(frame)],
            desc="素材视觉分析抽帧", timeout=60,
        )
        return Image.open(frame).convert("RGB")


def _visual_embedding_and_traits(material: dict, save_thumbnail: bool = True) -> tuple[np.ndarray, dict]:
    image = _representative_image(material)
    thumbnail_path = None
    if save_thumbnail:
        thumbnail_dir = ASSETS_ROOT / "thumbnails"
        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        thumbnail_path = thumbnail_dir / f"{int(material['id'])}.jpg"
        thumb = image.copy()
        thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
        thumb.save(thumbnail_path, "JPEG", quality=85)
    resized = image.resize((256, 256), Image.Resampling.BICUBIC)
    array = np.asarray(resized, dtype=np.float32) / 255.0
    pixels = np.transpose(array, (2, 0, 1))[None, ...]
    if not VISUAL_MODEL.exists() or not VISUAL_LABELS.exists():
        raise FileNotFoundError(f"内置 MobileCLIP 模型或标签向量缺失: {VISUAL_MODEL}")
    import onnxruntime as ort
    session = ort.InferenceSession(str(VISUAL_MODEL), providers=["CPUExecutionProvider"])
    embedding = session.run(None, {"pixel_values": pixels})[0][0].astype(np.float32)
    label_data = np.load(VISUAL_LABELS)
    label_embeddings = label_data["embeddings"].astype(np.float32)
    metadata = json.loads(str(label_data["metadata"]))
    scores = label_embeddings @ embedding
    visual_labels = {}
    visual_confidence = {}
    for group in {item["group"] for item in metadata}:
        indexes = [index for index, item in enumerate(metadata) if item["group"] == group]
        best = max(indexes, key=lambda index: float(scores[index]))
        visual_labels[group] = metadata[best]["label"]
        visual_confidence[group] = round(float(scores[best]), 4)
    gray = array.mean(axis=2)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    sharpness = float(
        np.mean(np.abs(np.diff(gray, axis=0))) + np.mean(np.abs(np.diff(gray, axis=1)))
    )
    traits = {
        "brightness": brightness,
        "contrast": contrast,
        "sharpness": sharpness,
        "lighting": "明亮" if brightness >= 0.58 else "低调" if brightness < 0.32 else "自然光",
        "labels": visual_labels,
        "confidence": visual_confidence,
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
    }
    return embedding, traits


def _analyze_virtual_slices(conn, material: dict) -> None:
    """Score each virtual window once so batch prewarm can prioritize real hotspots."""
    slices = conn.execute(
        "SELECT * FROM virtual_slices WHERE material_id=? ORDER BY start_frame",
        (material["id"],),
    ).fetchall()
    for virtual in slices:
        fps = float(virtual["fps"])
        seek = (int(virtual["start_frame"]) + int(virtual["end_frame"])) / 2 / fps
        sample = dict(material)
        sample["analysis_seek"] = seek
        _, visual = _visual_embedding_and_traits(sample, save_thumbnail=False)
        tags = [
            visual["labels"][key]
            for key in ("subject", "shot_type", "action", "scene", "content_role")
        ]
        # Preserve ranking headroom. The previous formula saturated most
        # ordinary footage at 1.0, so hotspot-first prewarming could not rank it.
        hotspot = max(
            0.0,
            min(
                1.0,
                0.10 + float(visual["contrast"]) * 0.8
                + float(visual["sharpness"]) * 1.2,
            ),
        )
        conn.execute(
            "UPDATE virtual_slices SET hotspot_score=?,capability_tags=?,analysis_version=? "
            "WHERE id=?",
            (round(hotspot, 4), json.dumps(tags, ensure_ascii=False), ANALYSIS_VERSION, virtual["id"]),
        )


def analyze_material(material_id: int) -> dict:
    """Analyze one material locally. Never called from renderer/task execution."""
    conn = get_conn()
    try:
        material = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not material:
            raise ValueError(f"素材不存在: {material_id}")
        tags = json.loads(material["tags"] or "[]")
        text = " ".join([material["display_name"] or Path(material["file_path"]).stem, *tags])
        width, height = int(material["width"] or 0), int(material["height"] or 0)
        duration = float(material["duration"] or 0)
        embedding, visual = _visual_embedding_and_traits(dict(material))
        subject = _match_label(text, "subject", visual["labels"]["subject"])
        action = _match_label(text, "action", visual["labels"]["action"])
        scene = _match_label(text, "scene", visual["labels"]["scene"])
        shot_type = (
            "特写" if "特写" in text or "细节" in text
            else visual["labels"]["shot_type"]
        )
        content_role = (
            "钩子" if any(word in text for word in ("开场", "钩子", "亮点"))
            else "细节" if shot_type == "特写"
            else visual["labels"]["content_role"]
        )
        resolution_score = min(1.0, (width * height) / (1080 * 1920)) if width and height else 0.3
        duration_score = min(1.0, duration / 5.0) if material["mat_type"] == "video" else 0.7
        visual_score = min(1.0, visual["contrast"] * 4 + visual["sharpness"] * 5)
        quality = round(
            0.45 * resolution_score + 0.20 * duration_score + 0.35 * visual_score, 4
        )
        summary = "、".join(dict.fromkeys(
            (subject, shot_type, action, scene, content_role, visual["lighting"])
        ))
        conn.execute(
            "INSERT INTO material_analysis(material_id,analysis_version,status,subject,"
            "shot_type,action,scene,content_role,quality_score,capability_summary,embedding,"
            "error_msg,updated_at) "
            "VALUES(?,?, 'done', ?,?,?,?,?,?,?,?,NULL,datetime('now','localtime')) "
            "ON CONFLICT(material_id) DO UPDATE SET analysis_version=excluded.analysis_version,"
            "status='done',subject=excluded.subject,shot_type=excluded.shot_type,"
            "action=excluded.action,scene=excluded.scene,content_role=excluded.content_role,"
            "quality_score=excluded.quality_score,capability_summary=excluded.capability_summary,"
            "embedding=excluded.embedding,"
            "error_msg=NULL,updated_at=datetime('now','localtime')",
            (material_id, ANALYSIS_VERSION, subject, shot_type, action, scene,
             content_role, quality, summary, embedding.tobytes()),
        )
        _analyze_virtual_slices(conn, dict(material))
        conn.execute(
            "UPDATE materials SET thumbnail_path=? WHERE id=?",
            (visual["thumbnail_path"], material_id),
        )
        conn.commit()
        return {
            "material_id": material_id, "subject": subject, "shot_type": shot_type,
            "action": action, "scene": scene, "content_role": content_role,
            "quality_score": quality, "capability_summary": summary,
            "visual_confidence": visual["confidence"],
        }
    except Exception as exc:
        conn.execute(
            "UPDATE material_analysis SET status='failed',error_msg=?,"
            "updated_at=datetime('now','localtime') WHERE material_id=?",
            (str(exc), material_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def analyze_pending_materials() -> int:
    conn = get_conn()
    try:
        ids = [
            row["material_id"] for row in conn.execute(
                "SELECT material_id FROM material_analysis WHERE status IN ('pending','running') "
                "OR analysis_version!=? "
                "ORDER BY material_id"
                , (ANALYSIS_VERSION,)
            ).fetchall()
        ]
    finally:
        conn.close()
    completed = 0
    for material_id in ids:
        try:
            analyze_material(int(material_id))
            completed += 1
        except Exception:
            pass
    return completed


def capability_summary(material_ids: list[int] | None = None) -> str:
    conn = get_conn()
    try:
        if material_ids:
            marks = ",".join("?" for _ in material_ids)
            rows = conn.execute(
                f"SELECT capability_summary FROM material_analysis "
                f"WHERE status='done' AND material_id IN ({marks})", material_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT capability_summary FROM material_analysis WHERE status='done'"
            ).fetchall()
    finally:
        conn.close()
    values = []
    for row in rows:
        values.extend(part for part in row["capability_summary"].split("、") if part)
    return "、".join(dict.fromkeys(values))[:300]


def infer_topic(material_ids: list[int] | None = None) -> str:
    """根据所选素材推断一个简洁的选题字符串 (用于 AI 文案对话框默认填充).

    策略:
      1. 取每个素材 material_analysis.subject 字段 (仅 status='done' 的分析,
         local vision tag); 返回出现频次最高者; 平局时按第一次出现的顺序.
      2. 兜底: 若所有素材都没有可用 subject, 用第一个素材的 display_name
         (或 file_path 的 stem) 作为 fallback.
      3. 任何异常 / 无输入 → 返回空串 "" (调用方应当把空串视为 '无法推断').

    说明:
      - 不会修改数据库, 不会触发任何模型推理; 只查本地 SQLite.
      - 用户可在 UI 里手动编辑推断结果, 不强制覆盖.
      - tie-break 自己实现 (Counter.most_common(1) 在 heap 实现里平局不稳定).
    """
    if not material_ids:
        return ""
    conn = get_conn()
    try:
        marks = ",".join("?" for _ in material_ids)
        rows = conn.execute(
            f"SELECT m.id AS mid, ma.subject, ma.status, "
            f"m.display_name, m.file_path "
            f"FROM materials m "
            f"LEFT JOIN material_analysis ma ON ma.material_id = m.id "
            f"WHERE m.id IN ({marks})",
            material_ids,
        ).fetchall()
    finally:
        conn.close()

    # 按输入 id 顺序索引, 避免依赖 SQL 返回顺序
    by_id: dict[int, object] = {r["mid"]: r for r in rows}

    # 1. 多数主体 (按输入顺序; 平局保留首次出现)
    subjects: list[str] = []
    for mid in material_ids:
        r = by_id.get(mid)
        if not r:
            continue
        # 只采纳 status='done' 的分析; pending/running/failed 视为无 subject
        if r["status"] != "done":
            continue
        subj = (r["subject"] or "").strip()
        if subj:
            subjects.append(subj)
    if subjects:
        counts = Counter(subjects)
        best_count = max(counts.values())
        # 自己实现 stable tie-break: 第一次遍历, 首个达 best_count 的胜出
        for s in subjects:
            if counts[s] == best_count:
                return s

    # 2. 兜底 — 第一个有 display_name / stem 的素材 (按输入顺序)
    for mid in material_ids:
        r = by_id.get(mid)
        if not r:
            continue
        name = (r["display_name"] or "").strip() or Path(r["file_path"]).stem.strip()
        if name:
            return name

    return ""


def analyze_text_intent(text: str) -> dict:
    from autokat.core.tagger import extract_keywords
    keywords = extract_keywords(text)
    role = "商品推荐" if any(word in text for word in ("推荐", "入手", "搭配", "商品")) else "口播讲解"
    return {"version": ANALYSIS_VERSION, "keywords": keywords, "content_role": role}
