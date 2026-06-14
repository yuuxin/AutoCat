import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autokat.models import db


class DatabaseMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_creates_foundation_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autokat.db"
            with patch.object(db, "DB_DIR", Path(tmp)), patch.object(db, "DB_PATH", path):
                db.init_db()
                db.init_db()
                conn = sqlite3.connect(path)
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
                conn.close()
        self.assertEqual(versions, [(1,), (2,), (3,)])
        self.assertTrue({
            "import_jobs", "import_items", "material_analysis", "virtual_slices",
            "cache_entries", "task_materials", "task_plans", "quality_runs",
            "quality_results",
        }.issubset(tables))

    def test_failed_migration_rolls_back_without_partial_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autokat.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE schema_migrations("
                "version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT NOT NULL)"
            )
            conn.commit()

            def fail_after_ddl(connection):
                connection.execute("CREATE TABLE partial_migration(id INTEGER)")
                raise RuntimeError("interrupted")

            with patch.object(db, "DB_DIR", Path(tmp)), patch.object(
                db, "DB_PATH", path
            ), patch.object(db, "MIGRATIONS", {99: ("interrupted", fail_after_ddl)}):
                with self.assertRaises(RuntimeError):
                    db._apply_migrations(conn)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
            conn.close()
        self.assertNotIn("partial_migration", tables)
        self.assertEqual(versions, [])
