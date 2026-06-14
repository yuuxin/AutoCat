"""Batch-level planning metrics and cache prewarm manifests."""

from __future__ import annotations

import json
from collections import Counter

from autokat.models.db import get_conn


class BatchPlanner:
    def build_manifest(self, scripts: list[dict]) -> dict:
        refs = []
        for script in scripts:
            for clip in script.get("clips", []):
                refs.append({
                    "source_path": clip.get("source_path"),
                    "offset_frame": clip.get(
                        "offset_frame",
                        int(round(float(clip.get("offset", 0)) * int(script.get("fps", 30)))),
                    ),
                    "duration_frames": clip.get("duration_frames"),
                    "fps": script.get("fps", 30),
                })
        counts = Counter(
            (item["source_path"], item["offset_frame"], item["duration_frames"], item["fps"])
            for item in refs
        )
        hotspots = [
            {"source_path": key[0], "offset_frame": key[1],
             "duration_frames": key[2], "fps": key[3], "references": count}
            for key, count in counts.most_common() if count > 1
        ]
        return {
            "references": len(refs),
            "unique_slices": len(counts),
            "hotspots": hotspots,
            "estimated_hit_rate": 1 - (len(counts) / max(1, len(refs))),
        }

    def save(self, task_id: int, planner_version: str, manifest: dict,
             metrics: dict | None = None) -> None:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO task_plans(task_id,planner_version,plan_json,prewarm_json,metrics_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "planner_version=excluded.planner_version,plan_json=excluded.plan_json,"
                "prewarm_json=excluded.prewarm_json,metrics_json=excluded.metrics_json,"
                "updated_at=datetime('now','localtime')",
                (task_id, planner_version, json.dumps(manifest), json.dumps(manifest["hotspots"]),
                 json.dumps(metrics or {})),
            )
            conn.commit()
        finally:
            conn.close()
