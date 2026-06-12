import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autokat.models import db


class TaskRetryTests(unittest.TestCase):
    def test_prepare_task_retry_preserves_done_and_resets_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "retry.db"
            with patch.object(db, "DB_PATH", db_path), patch.object(db, "DB_DIR", Path(tmpdir)):
                db.init_db()
                conn = db.get_conn()
                script_id = conn.execute(
                    "INSERT INTO scripts (name,narration) VALUES ('test','text')"
                ).lastrowid
                task_id = conn.execute(
                    "INSERT INTO tasks (script_id,config,status,total,done,output_dir) "
                    "VALUES (?, '{}', 'failed', 3, 1, ?)",
                    (script_id, tmpdir),
                ).lastrowid
                for index, status in enumerate(("done", "failed", "rendering")):
                    conn.execute(
                        "INSERT INTO clips "
                        "(task_id,idx,script_path,status,output_path,error_msg,duration_seconds) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (task_id, index, f"/tmp/{index}.json", status,
                         f"/tmp/{index}.mp4", "error", 10.0),
                    )
                conn.commit()
                conn.close()

                self.assertEqual(db.prepare_task_retry(task_id), 2)
                task = db.get_task(task_id)
                clips = db.get_clips_by_task(task_id)

                self.assertEqual(task["status"], "pending")
                self.assertEqual(task["done"], 1)
                self.assertEqual(clips[0]["status"], "done")
                for clip in clips[1:]:
                    self.assertEqual(clip["status"], "pending")
                    self.assertEqual(clip["retry_count"], 1)
                    self.assertIsNone(clip["error_msg"])
                    self.assertIsNone(clip["output_path"])
                    self.assertIsNone(clip["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
