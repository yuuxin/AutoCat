import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

from autokat.core.material_analysis import analyze_material, analyze_text_intent
from autokat.core.material import build_material_pool, clear_material_pool_cache
from autokat.models import db


class MaterialAnalysisTests(unittest.TestCase):
    def test_analysis_is_persisted_and_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, "DB_DIR", root), patch.object(db, "DB_PATH", root / "db.sqlite"):
                db.init_db()
                mid = db.add_material(
                    "/tmp/女鞋特写.mp4", "hash", "video", duration=5,
                    width=1080, height=1920, display_name="女鞋特写",
                )
                conn = db.get_conn()
                conn.execute(
                    "INSERT INTO material_analysis(material_id,status) VALUES(?,'pending')", (mid,)
                )
                conn.commit()
                conn.close()
                with patch(
                    "autokat.core.material_analysis._visual_embedding_and_traits",
                    return_value=(
                        np.ones(512, dtype=np.float32),
                        {
                            "brightness": 0.6, "contrast": 0.2, "sharpness": 0.1,
                            "lighting": "明亮",
                            "labels": {
                                "subject": "商品", "shot_type": "中景", "action": "展示",
                                "scene": "棚拍", "content_role": "通用",
                            },
                            "confidence": {"subject": 0.4},
                            "thumbnail_path": "/tmp/thumb.jpg",
                        },
                    ),
                ):
                    result = analyze_material(mid)
                conn = db.get_conn()
                row = conn.execute(
                    "SELECT * FROM material_analysis WHERE material_id=?", (mid,)
                ).fetchone()
                conn.close()
        self.assertEqual(result["subject"], "女鞋")
        self.assertEqual(row["status"], "done")
        self.assertGreater(row["quality_score"], 0)
        self.assertEqual(len(row["embedding"]), 512 * 4)
        self.assertEqual(result["visual_confidence"], {"subject": 0.4})

    def test_user_script_intent_is_local_and_deterministic(self):
        intent = analyze_text_intent("今天推荐一款适合通勤搭配的时尚女鞋")
        self.assertEqual(intent["content_role"], "商品推荐")

    def test_new_material_pool_excludes_legacy_physical_slices_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.mp4"
            legacy = root / "legacy.mp4"
            original.write_bytes(b"original")
            legacy.write_bytes(b"legacy")
            with patch.object(db, "DB_DIR", root / "tasks"), patch.object(
                db, "DB_PATH", root / "tasks" / "db.sqlite"
            ):
                db.init_db()
                parent = db.add_material(
                    str(original), "original-hash", "video", duration=5,
                    width=1080, height=1920,
                )
                child = db.add_material(
                    str(legacy), "legacy-hash", "video", duration=2,
                    width=1080, height=1920, clip_parent=parent,
                )
                conn = db.get_conn()
                conn.execute("UPDATE materials SET source_kind='original' WHERE id=?", (parent,))
                conn.execute("UPDATE materials SET source_kind='legacy_slice' WHERE id=?", (child,))
                conn.commit()
                conn.close()
                clear_material_pool_cache()
                default_pool = build_material_pool()
                clear_material_pool_cache()
                compatibility_pool = build_material_pool(include_legacy_slices=True)
                clear_material_pool_cache()
        self.assertNotIn(child, {item["id"] for item in default_pool})
        self.assertIn(child, {item["id"] for item in compatibility_pool})
