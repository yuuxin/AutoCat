import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from autokat.core.quality import (
    QualityPolicy, _deep_validation_python, summarize_task_quality,
    technical_report_text,
)
from autokat.models import db


class QualityPolicyTests(unittest.TestCase):
    def test_deep_sampling_is_bounded_and_covers_edges(self):
        sample = QualityPolicy.deep_sample_indexes(100)
        self.assertEqual(len(sample), 10)
        self.assertEqual(sample[0], 0)
        self.assertEqual(sample[-1], 99)

    def test_small_batch_is_fully_sampled(self):
        self.assertEqual(QualityPolicy.deep_sample_indexes(5), [0, 1, 2, 3, 4])

    @patch("autokat.core.quality.subprocess.run")
    def test_deep_validation_environment_requires_asr_and_ocr(self, run):
        run.return_value.returncode = 0
        with patch("autokat.core.quality.Path.is_file", return_value=True), patch(
            "autokat.core.quality.os.access", return_value=True
        ):
            self.assertIsNotNone(_deep_validation_python())
        self.assertIn("import funasr,paddleocr", run.call_args.args[0])

    def test_summary_counts_only_quick_results_and_marks_unavailable_deep(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            db, "DB_DIR", Path(tmp) / "tasks"
        ), patch.object(db, "DB_PATH", Path(tmp) / "tasks" / "autokat.db"):
            db.init_db()
            conn = db.get_conn()
            script_id = conn.execute(
                "INSERT INTO scripts(name,narration) VALUES('test','test')"
            ).lastrowid
            task_id = conn.execute(
                "INSERT INTO tasks(script_id,config,total,done,output_dir,status) "
                "VALUES(?, '{}', 1, 1, '/tmp', 'done')", (script_id,)
            ).lastrowid
            clip_id = conn.execute(
                "INSERT INTO clips(task_id,idx,script_path,status) "
                "VALUES(?,0,'/tmp/script.json','done')", (task_id,)
            ).lastrowid
            quick_run = conn.execute(
                "INSERT INTO quality_runs(task_id,level,status) VALUES(?,'quick','done')",
                (task_id,),
            ).lastrowid
            deep_run = conn.execute(
                "INSERT INTO quality_runs(task_id,level,status,metrics_json) "
                "VALUES(?,'sampled_deep','failed',?)",
                (task_id, json.dumps({
                    "status": "unavailable", "reason": "ASR/OCR environment missing",
                    "sample_indexes": [0],
                })),
            ).lastrowid
            conn.execute(
                "INSERT INTO quality_results(run_id,clip_id,status,auto_fix_count) "
                "VALUES(?,?, 'passed', 1)", (quick_run, clip_id),
            )
            conn.execute(
                "INSERT INTO quality_results(run_id,clip_id,status) "
                "VALUES(?,?, 'failed')", (deep_run, clip_id),
            )
            conn.commit()
            conn.close()
            summary = summarize_task_quality(task_id)
            report = technical_report_text(task_id)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["auto_fixed"], 1)
        self.assertEqual(summary["deep_status"], "unavailable")
        self.assertIn("ASR/OCR environment missing", report)
