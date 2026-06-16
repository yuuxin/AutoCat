"""v3.12 守护测试: AI 文案对话框选题自动推断 (infer_topic)."""
from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autokat.core.material_analysis import infer_topic
from autokat.models import db


@contextmanager
def _isolated_db():
    """Build a fresh DB under tmp and yield a connection (patched for whole lifetime).

    v3.12 注: 必须把 DB_DIR/DB_PATH 的 patch 持续到 conn.close 之后,
    否则 get_conn() 会回到生产路径, 多个测试之间的 UNIQUE 约束会互相打架.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tasks"
        root.mkdir(parents=True, exist_ok=True)
        with patch.object(db, "DB_DIR", root), \
             patch.object(db, "DB_PATH", root / "db.sqlite"):
            db.init_db()
            conn = db.get_conn()
            try:
                yield conn
            finally:
                conn.close()


def _add_material(
    conn, file_path: str, display_name=None, mat_type="video", duration=5.0,
):
    """Insert a material row directly and return its id."""
    cur = conn.execute(
        "INSERT INTO materials(file_path, file_hash, mat_type, duration, "
        "width, height, tags, display_name) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (file_path, "h", mat_type, duration, 1080, 1920, "[]", display_name),
    )
    conn.commit()
    return int(cur.lastrowid)


def _set_analysis(conn, material_id, subject, status="done"):
    """Insert/update material_analysis for a material."""
    conn.execute(
        "INSERT INTO material_analysis(material_id, status, subject) "
        "VALUES(?,?,?) "
        "ON CONFLICT(material_id) DO UPDATE SET "
        "status=excluded.status, subject=excluded.subject",
        (material_id, status, subject),
    )
    conn.commit()


class InferTopicTests(unittest.TestCase):
    """v3.12: AI 文案对话框根据所选素材自动推断默认选题."""

    def test_empty_ids_returns_empty_string(self):
        self.assertEqual(infer_topic([]), "")
        self.assertEqual(infer_topic(None), "")

    def test_single_material_with_subject_returns_subject(self):
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/a.mp4", display_name="女鞋特写")
            _set_analysis(conn, mid, "女鞋")
            self.assertEqual(infer_topic([mid]), "女鞋")

    def test_multiple_materials_returns_most_common_subject(self):
        """3 个女鞋 + 1 个商品 → 应返回 '女鞋'."""
        with _isolated_db() as conn:
            ids = []
            for i in range(3):
                mid = _add_material(conn, f"/data/shoe{i}.mp4", display_name=f"shoe{i}")
                _set_analysis(conn, mid, "女鞋")
                ids.append(mid)
            mid_other = _add_material(conn, "/data/other.mp4", display_name="other")
            _set_analysis(conn, mid_other, "商品")
            ids.append(mid_other)
            self.assertEqual(infer_topic(ids), "女鞋")

    def test_ties_preserve_first_inserted_subject(self):
        """两个不同 subject 各出现一次 → 返回第一次出现的 (stable tie-break)."""
        with _isolated_db() as conn:
            mid_a = _add_material(conn, "/data/a.mp4", display_name="a")
            _set_analysis(conn, mid_a, "女鞋")
            mid_b = _add_material(conn, "/data/b.mp4", display_name="b")
            _set_analysis(conn, mid_b, "商品")
            # 顺序: 女鞋 先出现 → 平局时返回 女鞋
            self.assertEqual(infer_topic([mid_a, mid_b]), "女鞋")
            # 反过来: 商品 先出现 → 平局时返回 商品
            self.assertEqual(infer_topic([mid_b, mid_a]), "商品")

    def test_fallback_to_display_name_when_no_analysis(self):
        """素材没有 material_analysis 行 → 用 display_name 兜底."""
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/x.mp4", display_name="春夏单鞋")
            # 没有 _set_analysis → material_analysis 行不存在
            self.assertEqual(infer_topic([mid]), "春夏单鞋")

    def test_fallback_to_file_stem_when_no_display_name(self):
        """display_name 为空时, 用 file_path 的 stem."""
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/厨房收纳好物.mp4", display_name=None)
            self.assertEqual(infer_topic([mid]), "厨房收纳好物")

    def test_skips_pending_and_failed_analysis(self):
        """status != 'done' 时不计入主体统计 (视为无 subject)."""
        with _isolated_db() as conn:
            mid_pending = _add_material(conn, "/data/p.mp4", display_name="p")
            _set_analysis(conn, mid_pending, "女鞋", status="pending")
            mid_failed = _add_material(conn, "/data/f.mp4", display_name="f")
            _set_analysis(conn, mid_failed, "商品", status="failed")
            # 两个都非 done → 退回到 display_name
            # (Counter 收不到任何 subject; 按输入顺序, pending 先)
            self.assertEqual(infer_topic([mid_pending, mid_failed]), "p")

    def test_mixed_analyzed_and_unanalyzed(self):
        """部分素材有 subject, 部分没有 → 只在有 subject 的中取众数."""
        with _isolated_db() as conn:
            mid_done = _add_material(conn, "/data/d.mp4", display_name="done")
            _set_analysis(conn, mid_done, "女鞋")
            mid_blank = _add_material(conn, "/data/b.mp4", display_name="blank-name")
            # mid_blank 没有 analysis 行 → 该素材 subject 为 NULL
            self.assertEqual(infer_topic([mid_done, mid_blank]), "女鞋")
            self.assertEqual(infer_topic([mid_blank, mid_done]), "女鞋")

    def test_empty_subject_string_treated_as_no_subject(self):
        """subject 字段存了空串 '' (而不是 NULL) → 不计入众数."""
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/e.mp4", display_name="dn")
            _set_analysis(conn, mid, "")
            self.assertEqual(infer_topic([mid]), "dn")

    def test_nonexistent_material_ids_are_silently_ignored(self):
        """不存在的 id 不应抛异常 — 应当被 SQL 忽略后 fallback."""
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/r.mp4", display_name="real-name")
            _set_analysis(conn, mid, "女鞋")
            # 99999 不存在, 但应被静默跳过
            self.assertEqual(infer_topic([mid, 99999]), "女鞋")
            # 全部不存在 → 返回空串 (无 display_name 兜底)
            self.assertEqual(infer_topic([99998, 99999]), "")

    def test_does_not_trigger_model_inference(self):
        """v3.12: infer_topic 只查 DB, 不能触发 vision 模型/ffmpeg.

        故意指向不存在的文件 + 有分析 → 应直接返回 subject,
        如果误调 analyze_material/_visual_embedding_and_traits 会试图
        打开模型文件或 ffmpeg, 在临时空目录里就会 FileNotFoundError.
        """
        with _isolated_db() as conn:
            mid = _add_material(conn, "/data/no_such.mp4", display_name="x")
            _set_analysis(conn, mid, "女鞋")
            self.assertEqual(infer_topic([mid]), "女鞋")


if __name__ == "__main__":
    unittest.main()
