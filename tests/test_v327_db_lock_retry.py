import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autokat.models import db


class DatabaseLockRetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path_patch = patch.object(
            db, "DB_PATH", Path(self._tmp.name) / "lock-test.db",
        )
        self._path_patch.start()
        db.init_db()
        conn = db.get_conn()
        script_id = conn.execute(
            "INSERT INTO scripts(name,narration) VALUES('lock-test','test')"
        ).lastrowid
        conn.execute(
            "INSERT INTO tasks(id,script_id,status,total,done,output_dir) "
            "VALUES(783,?,'pending',5,0,'/tmp')",
            (script_id,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._path_patch.stop()
        self._tmp.cleanup()

    def test_get_conn_sets_timeout_before_any_journal_change(self):
        with db.get_conn() as conn:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 30000)

    def test_get_conn_does_not_repeat_journal_mode_change(self):
        source = __import__("inspect").getsource(db.get_conn)
        self.assertNotIn('execute("PRAGMA journal_mode', source)

    def test_write_transaction_retries_locked_error(self):
        calls = []

        def operation(conn):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch("autokat.models.db.time.sleep"):
            self.assertEqual(db.run_write_transaction(operation), "ok")
        self.assertEqual(len(calls), 3)

    def test_parallel_status_updates_do_not_lock(self):
        errors = []

        def worker(index):
            try:
                for _ in range(20):
                    db.update_task_status(783, "pending", done=index % 5)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))


if __name__ == "__main__":
    unittest.main()
