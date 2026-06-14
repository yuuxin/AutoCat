"""SQLite 数据库模型 — 素材元数据管理"""

import sqlite3
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

from autokat.core.paths import TASKS_ROOT

DB_DIR = TASKS_ROOT
DB_PATH = DB_DIR / "autokat.db"
SCHEMA_VERSION = 3


def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 修: WAL 模式下并发写仍会锁, busy_timeout 让 SQLite 等 5s 而不是 OperationalError
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _backup_database() -> Path:
    backup_dir = DB_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = backup_dir / f"autokat_pre_migration_{stamp}.db"
    if DB_PATH.exists() and DB_PATH.stat().st_size:
        shutil.copy2(DB_PATH, backup_path)
    return backup_path


def _execute_migration_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute DDL without sqlite3.executescript's implicit transaction commit."""
    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete migration statement")


def _migration_v1(conn: sqlite3.Connection) -> None:
    """Persistent jobs, virtual slices, cache, planning and QA foundations."""
    _execute_migration_script(conn, """
        CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','running','paused','done','failed')),
            total INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,
            error_msg TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            resumed_at TEXT DEFAULT NULL,
            completed_at TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS import_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            target_path TEXT DEFAULT NULL,
            file_hash TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','processing','ready','done','failed')),
            stage TEXT NOT NULL DEFAULT 'queued',
            error_msg TEXT DEFAULT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            material_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(job_id) REFERENCES import_jobs(id),
            FOREIGN KEY(material_id) REFERENCES materials(id)
        );
        CREATE INDEX IF NOT EXISTS idx_import_items_job ON import_items(job_id);
        CREATE INDEX IF NOT EXISTS idx_import_items_status ON import_items(status);

        CREATE TABLE IF NOT EXISTS material_analysis (
            material_id INTEGER PRIMARY KEY,
            analysis_version TEXT NOT NULL DEFAULT 'v1',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','done','failed')),
            subject TEXT DEFAULT '',
            shot_type TEXT DEFAULT '',
            action TEXT DEFAULT '',
            scene TEXT DEFAULT '',
            content_role TEXT DEFAULT '',
            quality_score REAL DEFAULT 0,
            capability_summary TEXT DEFAULT '',
            embedding BLOB DEFAULT NULL,
            error_msg TEXT DEFAULT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(material_id) REFERENCES materials(id)
        );
        CREATE TABLE IF NOT EXISTS virtual_slices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL,
            start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            duration_frames INTEGER NOT NULL,
            fps REAL NOT NULL,
            hotspot_score REAL DEFAULT 0,
            capability_tags TEXT DEFAULT '[]',
            analysis_version TEXT NOT NULL DEFAULT 'v1',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(material_id,start_frame,end_frame,analysis_version),
            FOREIGN KEY(material_id) REFERENCES materials(id)
        );
        CREATE INDEX IF NOT EXISTS idx_virtual_slices_material ON virtual_slices(material_id);

        CREATE TABLE IF NOT EXISTS cache_entries (
            cache_key TEXT PRIMARY KEY,
            cache_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','building','ready','failed')),
            size_bytes INTEGER DEFAULT 0,
            hit_count INTEGER DEFAULT 0,
            build_count INTEGER DEFAULT 0,
            error_msg TEXT DEFAULT NULL,
            last_accessed_at TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS task_materials (
            task_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            PRIMARY KEY(task_id, material_id),
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(material_id) REFERENCES materials(id)
        );
        CREATE TABLE IF NOT EXISTS task_plans (
            task_id INTEGER PRIMARY KEY,
            planner_version TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}',
            prewarm_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS quality_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            level TEXT NOT NULL CHECK(level IN ('quick','sampled_deep','full_deep')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','done','failed')),
            report_path TEXT DEFAULT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            completed_at TEXT DEFAULT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS quality_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            clip_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            auto_fix_count INTEGER NOT NULL DEFAULT 0,
            blocking_reason TEXT DEFAULT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            report_path TEXT DEFAULT NULL,
            FOREIGN KEY(run_id) REFERENCES quality_runs(id),
            FOREIGN KEY(clip_id) REFERENCES clips(id)
        );
        CREATE INDEX IF NOT EXISTS idx_quality_results_run ON quality_results(run_id);
        INSERT OR IGNORE INTO material_analysis(material_id,status)
            SELECT id,'pending' FROM materials WHERE clip_parent IS NULL;
    """)
    mat_cols = _table_columns(conn, "materials")
    for name, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'ready'"),
        ("probe_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("thumbnail_path", "TEXT DEFAULT NULL"),
        ("source_kind", "TEXT NOT NULL DEFAULT 'legacy'"),
    ):
        if name not in mat_cols:
            conn.execute(f"ALTER TABLE materials ADD COLUMN {name} {definition}")


def _migration_v2(conn: sqlite3.Connection) -> None:
    """Backfill virtual slices for reusable legacy source videos."""
    for material in conn.execute(
        "SELECT id,duration FROM materials WHERE mat_type='video' AND clip_parent IS NULL "
        "AND duration>0 AND NOT EXISTS("
        "SELECT 1 FROM virtual_slices WHERE virtual_slices.material_id=materials.id)"
    ).fetchall():
        fps = 30.0
        total_frames = int(round(float(material["duration"]) * fps))
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
                (material["id"], start, end, end - start, fps, 0.5),
            )
            start = end


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Classify legacy rows without changing their paths or rendering behavior."""
    conn.execute(
        "UPDATE materials SET source_kind='legacy_slice' "
        "WHERE clip_parent IS NOT NULL AND source_kind='legacy'"
    )
    conn.execute(
        "UPDATE materials SET source_kind='original' "
        "WHERE clip_parent IS NULL AND source_kind='legacy'"
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    """Add tasks.wizard_snapshot column for full UI state replay & view-only inspection.

    Captured at task creation time so that any future task can be opened in the
    wizard in either of two modes: view (read-only, audit Steps 1-3 after the
    task finished) or fork (editable, copy the configuration into a new task).
    Old tasks created before this migration have NULL here and gracefully
    degrade to showing only the trimmed subset of fields that tasks.config
    already records.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "wizard_snapshot" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN wizard_snapshot TEXT DEFAULT NULL"
        )


MIGRATIONS = {
    1: ("persistent library, planning, cache and QA foundations", _migration_v1),
    2: ("backfill virtual slices for legacy source videos", _migration_v2),
    3: ("classify legacy original materials and physical slices", _migration_v3),
    4: ("wizard_snapshot column on tasks for full UI replay", _migration_v4),
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now','localtime')),
            description TEXT NOT NULL
        )
    """)
    applied = {
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    pending = [version for version in sorted(MIGRATIONS) if version not in applied]
    if not pending:
        return
    _backup_database()
    for version in pending:
        description, migrate = MIGRATIONS[version]
        try:
            conn.execute("BEGIN IMMEDIATE")
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, description) VALUES(?, ?)",
                (version, description),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS materials (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT    NOT NULL UNIQUE,
            file_hash   TEXT    NOT NULL,
            mat_type    TEXT    NOT NULL CHECK(mat_type IN ('image','video')),
            duration    REAL    DEFAULT 0,          -- 视频时长(秒)，图片为0
            width       INTEGER DEFAULT 0,
            height      INTEGER DEFAULT 0,
            tags        TEXT    DEFAULT '[]',        -- JSON 数组
            clip_parent INTEGER DEFAULT NULL,        -- 如果是从视频拆分的子镜头，指向父素材id
            feature     BLOB    DEFAULT NULL,         -- CLIP 特征向量(二期用)
            display_name TEXT    DEFAULT NULL,        -- 可读名（与文件名不同，用于UI展示）
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (clip_parent) REFERENCES materials(id)
        );

        CREATE INDEX IF NOT EXISTS idx_mat_type ON materials(mat_type);
        CREATE INDEX IF NOT EXISTS idx_tags ON materials(tags);
        CREATE INDEX IF NOT EXISTS idx_clip_parent ON materials(clip_parent);

        CREATE TABLE IF NOT EXISTS scripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            narration   TEXT    NOT NULL,             -- 原始口播文案
            lang        TEXT    DEFAULT 'zh-CN',
            tts_config  TEXT    DEFAULT '{}',         -- JSON: voice, rate, pitch
            bgm_file    TEXT    DEFAULT NULL,
            created_at  TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id   INTEGER NOT NULL,
            config      TEXT    NOT NULL DEFAULT '{}', -- 生成参数 JSON
            wizard_snapshot TEXT DEFAULT NULL,         -- 完整 UI 状态（用于查看配置/基于此新建）
            status      TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','done','failed')),
            total       INTEGER DEFAULT 0,
            done        INTEGER DEFAULT 0,
            output_dir  TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (script_id) REFERENCES scripts(id)
        );

        CREATE TABLE IF NOT EXISTS clips (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     INTEGER NOT NULL,
            idx         INTEGER NOT NULL,             -- 第几条成片
            script_path TEXT    NOT NULL,              -- 编排脚本 JSON 路径
            output_path TEXT    DEFAULT NULL,           -- 渲染后视频路径
            status      TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','rendering','done','failed')),
            retry_count INTEGER DEFAULT 0,
            error_msg   TEXT    DEFAULT NULL,
            progress_detail TEXT DEFAULT '',          -- 实时阶段文案（Step 4 显示用）
            progress_at TEXT DEFAULT '',              -- progress_detail 写入时间
            duration_seconds REAL DEFAULT NULL,       -- 渲染后视频时长（秒，ffprobe 拿）
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_clips_task ON clips(task_id);
        CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);

        CREATE TABLE IF NOT EXISTS tags (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            color       TEXT    DEFAULT '#6B7280',
            created_at  TEXT    DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    # 迁移：旧库补 progress_detail 字段（用于实时显示每个成片当前阶段）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(clips)").fetchall()]
    if "progress_detail" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN progress_detail TEXT DEFAULT ''")
    if "progress_at" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN progress_at TEXT DEFAULT ''")
    if "duration_seconds" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN duration_seconds REAL DEFAULT NULL")
    # materials 表加 display_name 列（可读名，UI 展示用）
    mat_cols = [r[1] for r in conn.execute("PRAGMA table_info(materials)").fetchall()]
    if "display_name" not in mat_cols:
        conn.execute("ALTER TABLE materials ADD COLUMN display_name TEXT DEFAULT NULL")
        # 旧记录从 file_path 拍一个可读名占位（实际仅在迁移时有意义）
        import os as _os
        for row in conn.execute("SELECT id, file_path FROM materials WHERE display_name IS NULL OR display_name=''").fetchall():
            stem = _os.path.splitext(_os.path.basename(row["file_path"]))[0]
            conn.execute("UPDATE materials SET display_name=? WHERE id=?", (stem, row["id"]))
    conn.commit()
    _apply_migrations(conn)
    conn.close()


# ── 素材 CRUD ──

def add_material(file_path: str, file_hash: str, mat_type: str,
                 duration: float = 0, width: int = 0, height: int = 0,
                 tags: Optional[list] = None,
                 clip_parent: Optional[int] = None,
                 display_name: Optional[str] = None) -> int:
    """添加素材，已存在则返回已有 ID（原子操作）"""
    conn = get_conn()
    # 先检查是否已存在
    existing = conn.execute(
        "SELECT id FROM materials WHERE file_hash=? AND file_path=?",
        (file_hash, file_path)
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"]
    # 不存在则插入
    cur = conn.execute(
        """INSERT INTO materials
           (file_path, file_hash, mat_type, duration, width, height, tags, clip_parent, display_name)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (file_path, file_hash, mat_type, duration, width, height,
         json.dumps(tags or []), clip_parent, display_name)
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_all_materials(mat_type: Optional[str] = None) -> list:
    conn = get_conn()
    if mat_type:
        rows = conn.execute(
            "SELECT * FROM materials WHERE mat_type=? ORDER BY id", (mat_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM materials ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 标签字典 CRUD ──

def register_tag(name: str, color: str = "#6B7280") -> int:
    """注册或获取一个标签（按 name 唯一），返回 id。空名返回 0。"""
    name = (name or "").strip()
    if not name:
        return 0
    conn = get_conn()
    row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute("INSERT INTO tags(name, color) VALUES(?, ?)", (name, color))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_all_tags_with_usage() -> list[dict]:
    """所有已注册标签 + 使用次数（扫所有 materials.tags JSON 统计出现频次）。"""
    conn = get_conn()
    rows = conn.execute("SELECT id, name, color, created_at FROM tags ORDER BY name").fetchall()
    tags = [dict(r) for r in rows]
    usage = {t["name"]: 0 for t in tags}
    for r in conn.execute("SELECT tags FROM materials WHERE tags IS NOT NULL AND tags != '' AND tags != '[]'").fetchall():
        try:
            for tn in json.loads(r["tags"]):
                if tn in usage:
                    usage[tn] += 1
        except Exception:
            pass
    for t in tags:
        t["usage"] = usage.get(t["name"], 0)
    conn.close()
    return tags


def update_tag(tag_id: int, name: str = None, color: str = None) -> bool:
    """重命名/重染色标签，同时同步所有 materials.tags JSON 中的引用。重名返回 False。"""
    if name is not None:
        name = name.strip()
        if not name:
            return False
    conn = get_conn()
    row = conn.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()
    if not row:
        conn.close()
        return False
    old_name = row["name"]
    if name is not None and name != old_name:
        dup = conn.execute("SELECT id FROM tags WHERE name=? AND id!=?", (name, tag_id)).fetchone()
        if dup:
            conn.close()
            return False
        conn.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
        # 同步所有 materials.tags
        for r in conn.execute("SELECT id, tags FROM materials WHERE tags LIKE ?", (f'%"{old_name}"%',)).fetchall():
            try:
                tlist = json.loads(r["tags"])
                if old_name in tlist:
                    tlist = [name if x == old_name else x for x in tlist]
                    conn.execute("UPDATE materials SET tags=? WHERE id=?", (json.dumps(tlist, ensure_ascii=False), r["id"]))
            except Exception:
                pass
    if color is not None:
        conn.execute("UPDATE tags SET color=? WHERE id=?", (color, tag_id))
    conn.commit()
    conn.close()
    return True


def delete_tag(tag_id: int) -> bool:
    """删除标签（从字典 + 所有 materials.tags JSON 中移除）。"""
    conn = get_conn()
    row = conn.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()
    if not row:
        conn.close()
        return False
    name = row["name"]
    for r in conn.execute("SELECT id, tags FROM materials WHERE tags LIKE ?", (f'%"{name}"%',)).fetchall():
        try:
            tlist = json.loads(r["tags"])
            if name in tlist:
                tlist = [x for x in tlist if x != name]
                conn.execute("UPDATE materials SET tags=? WHERE id=?", (json.dumps(tlist, ensure_ascii=False), r["id"]))
        except Exception:
            pass
    conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    conn.commit()
    conn.close()
    return True


def apply_tags_to_materials(material_ids: list[int], tags_add: list[str], tags_remove: list[str]) -> int:
    """批量给多个素材加/去 tag，自动注册未存在的 tag。返回实际改动的素材数。"""
    if not material_ids or (not tags_add and not tags_remove):
        return 0
    for t in (tags_add or []):
        t = (t or "").strip()
        if t:
            register_tag(t)
    conn = get_conn()
    affected = 0
    for mid in material_ids:
        row = conn.execute("SELECT tags FROM materials WHERE id=?", (mid,)).fetchone()
        if not row:
            continue
        try:
            tlist = list(json.loads(row["tags"] or "[]"))
        except Exception:
            tlist = []
        orig = list(tlist)
        for t in (tags_add or []):
            t = (t or "").strip()
            if t and t not in tlist:
                tlist.append(t)
        for t in (tags_remove or []):
            t = (t or "").strip()
            if t in tlist:
                tlist.remove(t)
        if tlist != orig:
            conn.execute("UPDATE materials SET tags=? WHERE id=?", (json.dumps(tlist, ensure_ascii=False), mid))
            affected += 1
    conn.commit()
    conn.close()
    return affected


def get_materials_by_ids(material_ids: list[int]) -> list[dict]:
    """按 id 列表获取素材记录。空列表返回空。"""
    if not material_ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in material_ids)
    rows = conn.execute(f"SELECT * FROM materials WHERE id IN ({placeholders})", material_ids).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_materials_by_tag(tag_name: str) -> list[dict]:
    """按单个 tag 名查询素材（JSON LIKE 简单匹配）。"""
    if not tag_name:
        return []
    conn = get_conn()
    rows = conn.execute("SELECT * FROM materials WHERE tags LIKE ? ORDER BY id", (f'%"{tag_name}"%',)).fetchall()
    conn.close()
    return [dict(r) for r in rows]



def get_material(mat_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM materials WHERE id=?", (mat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_clip_count(mat_id: int) -> int:
    """获取某个素材拆分的子镜头数量"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM materials WHERE clip_parent=?", (mat_id,)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ── 任务 CRUD ──

def create_task(
    script_id: int, config: dict, output_dir: str, total: int,
    wizard_snapshot: Optional[str] = None,
) -> int:
    """Create a new task. ``wizard_snapshot`` is an opaque JSON blob capturing
    every wizard widget's value at creation time, so the task can later be
    reopened in view/fork mode. Older callers can pass ``None`` and the row
    will get a NULL column (graceful degradation)."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO tasks (script_id, config, total, output_dir, wizard_snapshot) "
        "VALUES (?,?,?,?,?)",
        (script_id, json.dumps(config), total, output_dir, wizard_snapshot)
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def update_task_wizard_snapshot(task_id: int, wizard_snapshot: str) -> None:
    """Persist the wizard UI snapshot for an existing task. Used by
    MainWindow when the user clicks 开始生成; we write the snapshot after
    all the per-step state is finalized so a later view/fork reopen shows
    exactly what the user saw at generation time."""
    conn = get_conn()
    conn.execute(
        "UPDATE tasks SET wizard_snapshot=? WHERE id=?",
        (wizard_snapshot, task_id)
    )
    conn.commit()
    conn.close()


def get_task(task_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_latest_task(limit_seconds: int = 60) -> Optional[dict]:
    """获取最近 N 秒内创建的最新任务。

    wizard 启动后 worker 线程异步创建 task 会有几十 ms 延迟，
    主线程的 1s 轮询如果赶上这个窗口就拿不到 _current_task_id，
    之前会直接 return，导致 done list / 进度条 / 日志都收不到 worker 的输出。
    现在 poll 里如果 _current_task_id 为 None，就用这个找最近创建的任务补上。
    """
    from datetime import datetime, timedelta
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(seconds=limit_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT * FROM tasks WHERE created_at >= ? ORDER BY id DESC LIMIT 1",
        (cutoff,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def update_task_status(task_id: int, status: str, done: Optional[int] = None):
    conn = get_conn()
    if done is not None:
        conn.execute(
            "UPDATE tasks SET status=?, done=? WHERE id=?",
            (status, done, task_id)
        )
    else:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    conn.commit()
    conn.close()


def prepare_task_retry(task_id: int) -> int:
    """Reset failed/interrupted clips and task progress in one transaction."""
    conn = get_conn()
    try:
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM clips WHERE task_id=? AND status='done'",
            (task_id,),
        ).fetchone()["c"]
        cur = conn.execute(
            "UPDATE clips SET status='pending', retry_count=retry_count+1, "
            "error_msg=NULL, output_path=NULL, progress_detail='', progress_at='', "
            "duration_seconds=NULL "
            "WHERE task_id=? AND status IN ('failed','rendering')",
            (task_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='pending', done=? WHERE id=?",
            (done, task_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_task_clip_counts(task_id: int) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM clips WHERE task_id=? GROUP BY status",
        (task_id,),
    ).fetchall()
    conn.close()
    counts = {row["status"]: row["c"] for row in rows}
    return {
        "total": sum(counts.values()),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "rendering": counts.get("rendering", 0),
    }


def get_pending_tasks() -> list:
    """获取未完成的任务（用于中断续跑）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status IN ('pending','running') ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 单条成片 CRUD ──

def add_clip(task_id: int, idx: int, script_path: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO clips (task_id, idx, script_path) VALUES (?,?,?)",
        (task_id, idx, script_path)
    )
    conn.commit()
    clip_id = cur.lastrowid
    conn.close()
    return clip_id


def update_clip_status(clip_id: int, status: str, output_path: Optional[str] = None,
                       error_msg: Optional[str] = None,
                       duration: Optional[float] = None):
    conn = get_conn()
    fields = {"status": status}
    if output_path:
        fields["output_path"] = output_path
    if error_msg:
        fields["error_msg"] = error_msg
    if duration is not None:
        fields["duration_seconds"] = float(duration)
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE clips SET {set_clause} WHERE id=?",
        (*fields.values(), clip_id)
    )
    conn.commit()
    conn.close()


def get_pending_clips(task_id: int) -> list:
    """获取未渲染的成片"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM clips WHERE task_id=? AND status='pending' ORDER BY idx",
        (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_clip_progress(clip_id: int, detail: str):
    """更新单条成片的实时进度文案（用于 Step 4 进度页显示）
    调用方：renderer 在切分片段 / xfade / 最终合成等阶段写一句中文文案。
    UI 端每 2 秒轮询一次，从 DB 读出来显示，避免阻塞主线程。
    """
    conn = get_conn()
    conn.execute(
        "UPDATE clips SET progress_detail=?, progress_at=datetime('now','localtime') WHERE id=?",
        (detail, clip_id)
    )
    conn.commit()
    conn.close()


def get_rendering_clips(task_id: int) -> list:
    """获取正在渲染中的成片及其当前进度文案，按 idx 排序"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT idx, progress_detail, progress_at FROM clips "
        "WHERE task_id=? AND status='rendering' ORDER BY idx",
        (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def count_pending_clips() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM clips WHERE status='pending'").fetchone()
    conn.close()
    return row["cnt"] if row else 0


def init_db_if_empty():
    """安全初始化：仅当表不存在时创建"""
    conn = get_conn()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='materials'"
    ).fetchall()
    conn.close()
    if not tables:
        init_db()


# ── 新 UI 辅助查询 ──

def get_all_tasks(limit: int = 100) -> list[dict]:
    """获取所有任务，按时间倒序"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tasks_by_status(status: str, limit: int = 100) -> list[dict]:
    """按状态筛选任务"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
        (status, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_clips_by_task(task_id: int) -> list[dict]:
    """获取某个任务的所有成片"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM clips WHERE task_id=? ORDER BY idx", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_task_stats() -> dict:
    """获取任务统计数据"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    running = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='running'").fetchone()["c"]
    done = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='done'").fetchone()["c"]
    failed = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='failed'").fetchone()["c"]
    paused = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='pending'").fetchone()["c"]
    conn.close()
    return {"total": total, "running": running, "done": done, "failed": failed, "pending": paused}


def delete_material_cascade(material_id: int, delete_files: bool = True) -> int:
    """级联删除一个素材及其子镜头(从视频拆出的子素材)。

    处理 ``materials.clip_parent`` 自引用外键:先递归把所有以本 id 为父的子素材
    删掉,再删本体。返回被删除的子素材数量(不含本体)。

    若 ``delete_files=True``,会尝试删除磁盘上的文件(单文件,失败不抛错)。
    """
    if not material_id:
        return 0
    import os as _os
    conn = get_conn()
    try:
        # 先拿到本体信息(供删除文件用)
        parent = conn.execute(
            "SELECT id, file_path FROM materials WHERE id=?", (material_id,)
        ).fetchone()

        # 递归收集所有以 material_id 为根的子素材 id
        all_ids: list[int] = []
        stack = [material_id]
        while stack:
            cur = stack.pop()
            kids = [r[0] for r in conn.execute(
                "SELECT id FROM materials WHERE clip_parent=?", (cur,)
            ).fetchall()]
            all_ids.extend(kids)
            stack.extend(kids)

        # 收集待删文件的路径(子素材 + 本体)
        file_paths: list[str] = []
        if all_ids:
            qmarks = ",".join("?" for _ in all_ids)
            for r in conn.execute(
                f"SELECT file_path FROM materials WHERE id IN ({qmarks})", all_ids
            ).fetchall():
                if r["file_path"]:
                    file_paths.append(r["file_path"])
        if parent and parent["file_path"]:
            file_paths.append(parent["file_path"])

        # 先删子素材,再删本体(顺序对调会再次触发外键约束)
        if all_ids:
            qmarks = ",".join("?" for _ in all_ids)
            conn.execute(f"DELETE FROM materials WHERE id IN ({qmarks})", all_ids)
        conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
        conn.commit()

        if delete_files:
            for fp in file_paths:
                try:
                    if fp and _os.path.exists(fp):
                        _os.remove(fp)
                except Exception:
                    pass
        return len(all_ids)
    finally:
        conn.close()


def delete_task(task_id: int):
    """删除任务及其关联的 clips"""
    conn = get_conn()
    conn.execute("DELETE FROM clips WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def get_script_by_id(script_id: int) -> Optional[dict]:
    """根据 ID 获取文案"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
