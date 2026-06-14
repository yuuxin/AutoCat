"""Persistent, restart-safe material import jobs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from autokat.core.paths import ASSETS_ROOT
from autokat.models.db import add_material, get_conn


ProgressCallback = Callable[[int, int, str, str], None]


class ImportJobService:
    """Import original assets once; expensive analysis continues independently."""

    def create_job(self, filepaths: list[str]) -> int:
        paths = [str(Path(path).expanduser().resolve()) for path in filepaths]
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO import_jobs(status,total,done) VALUES('queued',?,0)",
                (len(paths),),
            )
            job_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO import_items(job_id,source_path) VALUES(?,?)",
                [(job_id, path) for path in paths],
            )
            conn.commit()
            return int(job_id)
        finally:
            conn.close()

    def get_job(self, job_id: int) -> dict:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def list_resumable_jobs(self) -> list[dict]:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM import_jobs WHERE status IN ('queued','running','paused') ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def list_jobs(self, limit: int = 20) -> list[dict]:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM import_jobs ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def pause_job(self, job_id: int) -> None:
        """Request a safe pause after the current atomic file stage."""
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE import_jobs SET status='paused' "
                "WHERE id=? AND status IN ('queued','running')",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def retry_failed(self, job_id: int) -> None:
        """Reset only failed items and make the original persistent job resumable."""
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE import_items SET status='queued',stage='queued',error_msg=NULL "
                "WHERE job_id=? AND status='failed'",
                (job_id,),
            )
            conn.execute(
                "UPDATE import_jobs SET status='queued',error_msg=NULL,completed_at=NULL "
                "WHERE id=? AND EXISTS("
                "SELECT 1 FROM import_items WHERE job_id=? AND status='queued')",
                (job_id, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _paused(self, job_id: int) -> bool:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return bool(row and row["status"] == "paused")
        finally:
            conn.close()

    def process_job(self, job_id: int, on_progress: Optional[ProgressCallback] = None) -> dict:
        from autokat.core.material import (
            SUPPORTED_IMAGES, SUPPORTED_VIDEOS, _file_hash, _get_image_size,
            _get_video_info, _resolve_display_name_conflict, _sanitize_display_name,
            clear_material_pool_cache,
        )

        conn = get_conn()
        try:
            conn.execute(
                "UPDATE import_jobs SET status='running',resumed_at=datetime('now','localtime') "
                "WHERE id=?",
                (job_id,),
            )
            # A process interrupted mid-file safely repeats that item.
            conn.execute(
                "UPDATE import_items SET status='queued',stage='queued' "
                "WHERE job_id=? AND status='processing'",
                (job_id,),
            )
            conn.commit()
            items = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM import_items WHERE job_id=? ORDER BY id", (job_id,)
                ).fetchall()
            ]
        finally:
            conn.close()

        stats = {"images": 0, "videos": 0, "clips": 0, "kenburns": 0,
                 "added": 0, "skipped": 0, "errors": [], "job_id": job_id}
        total = len(items)
        for index, item in enumerate(items):
            if self._paused(job_id):
                break
            if item["status"] in ("ready", "done"):
                stats["skipped"] += 1
                continue
            source = Path(item["source_path"])
            if on_progress:
                on_progress(index, total, source.name, "processing")
            try:
                suffix = source.suffix.lower()
                if suffix not in SUPPORTED_IMAGES | SUPPORTED_VIDEOS:
                    raise ValueError(f"不支持的格式: {suffix}")
                if not source.exists():
                    raise FileNotFoundError(f"文件不存在: {source}")
                file_hash = _file_hash(str(source))
                conn = get_conn()
                duplicate = conn.execute(
                    "SELECT id FROM materials WHERE file_hash=? AND clip_parent IS NULL LIMIT 1",
                    (file_hash,),
                ).fetchone()
                conn.close()
                if duplicate:
                    material_id = int(duplicate["id"])
                    target = str(source)
                    stats["skipped"] += 1
                else:
                    is_video = suffix in SUPPORTED_VIDEOS
                    target_dir = ASSETS_ROOT / ("videos" if is_video else "images")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_path = target_dir / f"{file_hash[:16]}{suffix}"
                    if not target_path.exists():
                        tmp_path = target_path.with_suffix(target_path.suffix + ".importing")
                        shutil.copy2(source, tmp_path)
                        os.replace(tmp_path, target_path)
                    if is_video:
                        width, height, duration = _get_video_info(str(target_path))
                        mat_type = "video"
                    else:
                        width, height = _get_image_size(str(target_path))
                        duration, mat_type = 0.0, "image"
                    if is_video and duration <= 0:
                        raise ValueError("无法读取视频时长")
                    display_name = _resolve_display_name_conflict(
                        _sanitize_display_name(source.stem)
                    )
                    material_id = add_material(
                        file_path=str(target_path), file_hash=file_hash, mat_type=mat_type,
                        duration=duration, width=width, height=height,
                        display_name=display_name,
                    )
                    probe = json.dumps({
                        "duration": duration, "width": width, "height": height,
                    }, ensure_ascii=False)
                    conn = get_conn()
                    conn.execute(
                        "UPDATE materials SET status='ready',probe_json=?,source_kind='original' "
                        "WHERE id=?",
                        (probe, material_id),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO material_analysis(material_id,status) VALUES(?,'pending')",
                        (material_id,),
                    )
                    if is_video:
                        fps = 30.0
                        total_frames = int(round(duration * fps))
                        window = int(round(5.0 * fps))
                        start = 0
                        while start < total_frames:
                            end = min(total_frames, start + window)
                            if total_frames - end < int(fps):
                                end = total_frames
                            conn.execute(
                                "INSERT OR IGNORE INTO virtual_slices"
                                "(material_id,start_frame,end_frame,duration_frames,fps,hotspot_score) "
                                "VALUES(?,?,?,?,?,?)",
                                (material_id, start, end, end - start, fps, 0.5),
                            )
                            start = end
                    conn.commit()
                    conn.close()
                    target = str(target_path)
                    stats[mat_type + "s"] += 1
                    stats["added"] += 1

                conn = get_conn()
                conn.execute(
                    "UPDATE import_items SET target_path=?,file_hash=?,material_id=?,"
                    "status='ready',stage='ready',error_msg=NULL,"
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (target, file_hash, material_id, item["id"]),
                )
                conn.execute(
                    "UPDATE import_jobs SET done=(SELECT COUNT(*) FROM import_items "
                    "WHERE job_id=? AND status IN ('ready','done')) WHERE id=?",
                    (job_id, job_id),
                )
                conn.commit()
                conn.close()
                if on_progress:
                    on_progress(index + 1, total, source.name, "done")
            except Exception as exc:
                message = str(exc)
                stats["errors"].append(message)
                conn = get_conn()
                conn.execute(
                    "UPDATE import_items SET status='failed',stage='failed',error_msg=?,"
                    "retry_count=retry_count+1,updated_at=datetime('now','localtime') WHERE id=?",
                    (message, item["id"]),
                )
                conn.commit()
                conn.close()
                if on_progress:
                    on_progress(index + 1, total, source.name, "error")

        conn = get_conn()
        try:
            paused = conn.execute(
                "SELECT status FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()["status"] == "paused"
            failed = conn.execute(
                "SELECT COUNT(*) AS c FROM import_items WHERE job_id=? AND status='failed'",
                (job_id,),
            ).fetchone()["c"]
            status = "paused" if paused else ("failed" if failed else "done")
            conn.execute(
                "UPDATE import_jobs SET status=?,done=(SELECT COUNT(*) FROM import_items "
                "WHERE job_id=? AND status IN ('ready','done')),"
                "completed_at=CASE WHEN ?='paused' THEN NULL ELSE datetime('now','localtime') END "
                "WHERE id=?",
                (status, job_id, status, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        clear_material_pool_cache()
        # Analysis is intentionally detached from the import UI/job worker.
        threading = __import__("threading")
        from autokat.core.material_analysis import analyze_pending_materials
        threading.Thread(target=analyze_pending_materials, daemon=True).start()
        return stats


def resume_import_jobs() -> None:
    """Resume queued/interrupted imports after application restart."""
    service = ImportJobService()
    for job in service.list_resumable_jobs():
        service.process_job(int(job["id"]))
