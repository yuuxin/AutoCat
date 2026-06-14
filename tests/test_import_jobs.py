import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autokat.core.import_jobs import ImportJobService
from autokat.models import db


class ImportJobTests(unittest.TestCase):
    def test_image_import_is_persistent_and_does_not_transcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (32, 48), "red").save(source)
            assets = root / "assets"
            with (
                patch.object(db, "DB_DIR", root / "tasks"),
                patch.object(db, "DB_PATH", root / "tasks" / "autokat.db"),
                patch("autokat.core.import_jobs.ASSETS_ROOT", assets),
            ):
                db.init_db()
                service = ImportJobService()
                job_id = service.create_job([str(source)])
                stats = service.process_job(job_id)
                job = service.get_job(job_id)
                conn = db.get_conn()
                material = conn.execute("SELECT * FROM materials").fetchone()
                analysis = conn.execute("SELECT * FROM material_analysis").fetchone()
                conn.close()
        self.assertEqual(stats["added"], 1)
        self.assertEqual(job["status"], "done")
        self.assertEqual(material["width"], 32)
        self.assertEqual(material["height"], 48)
        self.assertEqual(material["source_kind"], "original")
        self.assertEqual(analysis["status"], "pending")

    def test_pause_and_failed_item_retry_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (12, 12), "blue").save(source)
            with patch.object(db, "DB_DIR", root / "tasks"), patch.object(
                db, "DB_PATH", root / "tasks" / "autokat.db"
            ):
                db.init_db()
                service = ImportJobService()
                job_id = service.create_job([str(source)])
                service.pause_job(job_id)
                self.assertEqual(service.get_job(job_id)["status"], "paused")
                conn = db.get_conn()
                conn.execute(
                    "UPDATE import_items SET status='failed',stage='failed',error_msg='bad' "
                    "WHERE job_id=?",
                    (job_id,),
                )
                conn.execute("UPDATE import_jobs SET status='failed' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                service.retry_failed(job_id)
                conn = db.get_conn()
                item = conn.execute(
                    "SELECT status,stage,error_msg FROM import_items WHERE job_id=?", (job_id,)
                ).fetchone()
                conn.close()
                job = service.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["stage"], "queued")
        self.assertIsNone(item["error_msg"])
