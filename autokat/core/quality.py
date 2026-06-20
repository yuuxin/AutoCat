"""Tiered task quality validation and compact result summaries."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import os
from pathlib import Path

from autokat.core.ffmpeg_utils import FFMPEG, FFPROBE
from autokat.core.timeline import SYNC_TOLERANCE_SECONDS
from autokat.models.db import get_conn, run_write_transaction

INTERNAL_FREEZE_WARNING_SECONDS = 1.5
INTERNAL_FREEZE_AUTOFIX_SECONDS = 3.0
TAIL_FREEZE_LIMIT_SECONDS = 0.5


class QualityPolicy:
    @staticmethod
    def deep_sample_indexes(total: int) -> list[int]:
        if total <= 0:
            return []
        size = min(10, max(5, int(math.ceil(total * 0.10))))
        size = min(total, size)
        indexes = {0, total - 1}
        if size > 2:
            step = (total - 1) / max(1, size - 1)
            indexes.update(round(index * step) for index in range(size))
        return sorted(indexes)[:size]


def _probe(path: str) -> dict:
    data = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration", "-of", "json", path],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout)
    durations = {"format": float(data.get("format", {}).get("duration") or 0)}
    durations.update({
        stream["codec_type"]: float(stream.get("duration") or 0)
        for stream in data.get("streams", [])
        if stream.get("codec_type") in ("video", "audio")
    })
    return durations


def quick_validate(output_path: str, script: dict) -> dict:
    durations = _probe(output_path)
    values = [durations.get(key, 0) for key in ("format", "video", "audio")]
    pair_diff_ms = (max(values) - min(values)) * 1000
    target = float(script.get("final_duration") or 0)
    target_diff_ms = max(abs(value - target) for value in values) * 1000
    visual = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", output_path,
         "-vf", "freezedetect=n=-45dB:d=0.5,blackdetect=d=0.5:pix_th=0.10",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=180,
    ).stderr
    freeze_starts = [
        float(value) for value in re.findall(
            r"(?:lavfi\.freezedetect\.)?freeze_start:\s*([0-9.]+)", visual
        )
    ]
    freeze_ends = [
        float(value) for value in re.findall(
            r"(?:lavfi\.freezedetect\.)?freeze_end:\s*([0-9.]+)", visual
        )
    ]
    freeze_durations = [
        float(value) for value in re.findall(
            r"(?:lavfi\.freezedetect\.)?freeze_duration:\s*([0-9.]+)", visual
        )
    ]
    output_duration = durations.get("video") or durations.get("format") or target
    freeze_details = []
    for index, start in enumerate(freeze_starts):
        end = freeze_ends[index] if index < len(freeze_ends) else output_duration
        duration = (
            freeze_durations[index]
            if index < len(freeze_durations)
            else max(0.0, end - start)
        )
        reaches_tail = end >= output_duration - 0.10
        if reaches_tail:
            severity = "blocking" if duration > TAIL_FREEZE_LIMIT_SECONDS else "warning"
        elif duration <= INTERNAL_FREEZE_WARNING_SECONDS:
            severity = "warning"
        elif duration <= INTERNAL_FREEZE_AUTOFIX_SECONDS:
            severity = "auto_fix"
        else:
            severity = "blocking"
        freeze_details.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(duration, 3),
            "reaches_tail": reaches_tail,
            "severity": severity,
        })
    freeze_events = len(freeze_details)
    blocking_freezes = [
        event for event in freeze_details
        if event["severity"] in {"auto_fix", "blocking"}
    ]
    hard_freezes = [
        event for event in freeze_details if event["severity"] == "blocking"
    ]
    auto_fixable_freezes = [
        event for event in freeze_details if event["severity"] == "auto_fix"
    ]
    black_events = len(re.findall(r"black_start:", visual))
    subtitles = script.get("subtitles", [])
    subtitles_valid = all(
        0 <= float(item["start"]) < float(item["end"]) <= target
        for item in subtitles
    )
    passed = (
        all(value > 0 for value in values)
        and pair_diff_ms <= SYNC_TOLERANCE_SECONDS * 1000
        and target_diff_ms <= SYNC_TOLERANCE_SECONDS * 1000
        and black_events == 0
        and not blocking_freezes
        and subtitles_valid
    )
    return {
        "passed": passed, "durations": durations, "pair_diff_ms": pair_diff_ms,
        "target_diff_ms": target_diff_ms, "freeze_events": freeze_events,
        "freeze_details": freeze_details,
        "blocking_freeze_events": len(blocking_freezes),
        "auto_fixable": bool(auto_fixable_freezes) and not hard_freezes,
        "black_events": black_events, "subtitles_valid": subtitles_valid,
    }


def record_result(task_id: int, clip_id: int, level: str, result: dict,
                  auto_fix_count: int = 0) -> None:
    def _write(conn):
        run = conn.execute(
            "SELECT id FROM quality_runs WHERE task_id=? AND level=? ORDER BY id DESC LIMIT 1",
            (task_id, level),
        ).fetchone()
        if run:
            run_id = run["id"]
        else:
            run_id = conn.execute(
                "INSERT INTO quality_runs(task_id,level,status) VALUES(?,?,'running')",
                (task_id, level),
            ).lastrowid
        reason = None if result.get("passed") else json.dumps(result, ensure_ascii=False)
        conn.execute(
            "INSERT INTO quality_results(run_id,clip_id,status,auto_fix_count,"
            "blocking_reason,metrics_json) VALUES(?,?,?,?,?,?)",
            (run_id, clip_id, "passed" if result.get("passed") else "failed",
             auto_fix_count, reason, json.dumps(result, ensure_ascii=False)),
        )
    run_write_transaction(_write)


def finalize_task_quality(task_id: int, total: int) -> dict:
    sample = QualityPolicy.deep_sample_indexes(total)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE quality_runs SET status='done',completed_at=datetime('now','localtime') "
            "WHERE task_id=? AND level='quick'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO quality_runs(task_id,level,status,metrics_json) VALUES(?,?,'pending',?)",
            (task_id, "sampled_deep", json.dumps({"sample_indexes": sample})),
        )
        conn.commit()
    finally:
        conn.close()
    return summarize_task_quality(task_id)


def _deep_validation_python() -> Path | None:
    candidates = []
    configured = os.environ.get("AUTOKAT_CONTENT_SYNC_PYTHON")
    if configured:
        candidates.append(Path(configured))
    candidates.extend([
        Path(__file__).resolve().parents[2] / ".validation-venv" / "bin" / "python",
        Path(__file__).resolve().parents[2] / ".validation-venv" / "bin" / "python3",
    ])
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            check = subprocess.run(
                [str(candidate), "-c", "import funasr,paddleocr"],
                capture_output=True, text=True, timeout=30,
            )
            if check.returncode == 0:
                return candidate
    return None


def run_deep_validation(task_id: int, full: bool = False) -> dict:
    """Execute sampled/full final-MP4 ASR+OCR validation in the dev environment."""
    root = Path(__file__).resolve().parents[2]
    python = _deep_validation_python()
    level = "full_deep" if full else "sampled_deep"
    conn = get_conn()
    clips = [
        dict(row) for row in conn.execute(
            "SELECT * FROM clips WHERE task_id=? AND status='done' ORDER BY idx", (task_id,)
        ).fetchall()
    ]
    indexes = [int(clip["idx"]) for clip in clips]
    if not full:
        selected_positions = QualityPolicy.deep_sample_indexes(len(clips))
        indexes = [indexes[position] for position in selected_positions]
    run = conn.execute(
        "SELECT id FROM quality_runs WHERE task_id=? AND level=? ORDER BY id DESC LIMIT 1",
        (task_id, level),
    ).fetchone()
    if run:
        run_id = int(run["id"])
        conn.execute("UPDATE quality_runs SET status='running' WHERE id=?", (run_id,))
    else:
        run_id = conn.execute(
            "INSERT INTO quality_runs(task_id,level,status) VALUES(?,?,'running')",
            (task_id, level),
        ).lastrowid
    conn.commit()
    conn.close()
    if not python:
        result = {
            "passed": False, "status": "unavailable",
            "reason": "未找到包含 FunASR/PaddleOCR 的独立验证环境",
            "sample_indexes": indexes,
        }
        conn = get_conn()
        conn.execute(
            "UPDATE quality_runs SET status='failed',metrics_json=?,completed_at=datetime('now','localtime') "
            "WHERE id=?",
            (json.dumps(result, ensure_ascii=False), run_id),
        )
        conn.commit()
        conn.close()
        return result

    output_dir = root / "outputs" / "content_sync_validation"
    command = [
        str(python), str(root / "tools" / "validate_content_sync.py"),
        "--task-id", str(task_id), "--output-dir", str(output_dir),
        "--clip-indexes", ",".join(str(index) for index in indexes),
    ]
    started = __import__("time").time()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    report_path = output_dir / f"task_{task_id}" / "content_sync_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        report = {"passed": False, "videos": [], "stderr": completed.stderr[-2000:]}
    report["elapsed_seconds"] = round(__import__("time").time() - started, 3)
    report["sample_indexes"] = indexes
    report["returncode"] = completed.returncode
    conn = get_conn()
    try:
        by_index = {int(clip["idx"]): clip for clip in clips}
        for video_result in report.get("videos", []):
            output_path = str(video_result.get("video", ""))
            clip = next(
                (item for item in clips if str(item.get("output_path")) == output_path), None
            )
            if not clip:
                continue
            conn.execute(
                "INSERT INTO quality_results(run_id,clip_id,status,auto_fix_count,"
                "blocking_reason,metrics_json,report_path) VALUES(?,?,?,?,?,?,?)",
                (run_id, clip["id"], "passed" if video_result.get("passed") else "failed",
                 1 if video_result.get("auto_retry") else 0,
                 None if video_result.get("passed") else "内容级 ASR/OCR 同步验收失败",
                 json.dumps(video_result, ensure_ascii=False), str(report_path)),
            )
        status = "done" if report.get("passed") else "failed"
        conn.execute(
            "UPDATE quality_runs SET status=?,metrics_json=?,report_path=?,"
            "completed_at=datetime('now','localtime') WHERE id=?",
            (status, json.dumps(report, ensure_ascii=False), str(report_path), run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return report


def summarize_task_quality(task_id: int) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT qr.status,qr.auto_fix_count FROM quality_results qr "
            "JOIN quality_runs q ON q.id=qr.run_id "
            "WHERE q.task_id=? AND q.level='quick'",
            (task_id,),
        ).fetchall()
        deep = conn.execute(
            "SELECT level,status,metrics_json,report_path FROM quality_runs WHERE task_id=? "
            "AND level IN ('sampled_deep','full_deep') ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    deep_metrics = json.loads(deep["metrics_json"] or "{}") if deep else {}
    deep_status = deep["status"] if deep else "not_scheduled"
    if deep_metrics.get("status") == "unavailable":
        deep_status = "unavailable"
    return {
        "passed": sum(row["status"] == "passed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "auto_fixed": sum(int(row["auto_fix_count"] or 0) > 0 for row in rows),
        "deep_status": deep_status,
        "deep_level": deep["level"] if deep else None,
        "report_path": deep["report_path"] if deep else None,
        "deep_metrics": deep_metrics,
    }


def technical_report_text(task_id: int) -> str:
    """Build a compact, human-readable report for the folded task detail panel."""
    conn = get_conn()
    try:
        runs = [
            dict(row) for row in conn.execute(
                "SELECT level,status,report_path,metrics_json,created_at,completed_at "
                "FROM quality_runs WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
        ]
        plan = conn.execute(
            "SELECT planner_version,plan_json,metrics_json FROM task_plans WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    lines = []
    if plan:
        manifest = json.loads(plan["plan_json"] or "{}")
        metrics = json.loads(plan["metrics_json"] or "{}")
        lines.extend([
            f"编排器: {plan['planner_version']}",
            f"素材引用/唯一切片: {manifest.get('references', 0)}/{manifest.get('unique_slices', 0)}",
            f"热点切片: {len(manifest.get('hotspots', []))}",
            f"缓存预热: 新建 {metrics.get('prewarm_built', 0)}，"
            f"已有 {metrics.get('preexisting_hits', 0)}，失败 {len(metrics.get('failures', []))}",
            f"预热引用覆盖率: {metrics.get('prewarm_reference_coverage', 0):.1%}",
            f"缓存预热耗时: {metrics.get('elapsed_seconds', 0)}s",
            "",
        ])
    for run in runs:
        metrics = json.loads(run["metrics_json"] or "{}")
        display_status = (
            "unavailable" if metrics.get("status") == "unavailable" else run["status"]
        )
        lines.append(
            f"{run['level']}: {display_status} · "
            f"{run.get('created_at') or '-'} → {run.get('completed_at') or '-'}"
        )
        if metrics.get("reason"):
            lines.append(f"  原因: {metrics['reason']}")
        if "elapsed_seconds" in metrics:
            lines.append(f"  耗时: {metrics['elapsed_seconds']}s")
        if "sample_indexes" in metrics:
            lines.append(f"  抽样索引: {metrics['sample_indexes']}")
        if run.get("report_path"):
            lines.append(f"  报告: {run['report_path']}")
    return "\n".join(lines) if lines else "尚无新版质量与性能技术报告。"
