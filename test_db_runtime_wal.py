#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ported v1.8 WAL optimization must preserve concurrent SQLite behavior."""
import tempfile
import sqlite3
import time
import threading
from contextlib import closing
import unittest
from pathlib import Path

from db_runtime import connect_db, quick_check, backup_database


class WalOnceRegression(unittest.TestCase):
    def test_repeated_and_parallel_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "shared.sqlite"
            with connect_db(db_path) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                db.execute("CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO checks VALUES (1)")
                db.commit()

            errors = []
            def reader():
                try:
                    with connect_db(db_path) as db:
                        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                        assert db.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 1
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=reader) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(quick_check(db_path), ["ok"])

    def test_verified_backup_contains_live_wal_and_preserves_manual_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "shared.sqlite3"
            backup_dir = root / "backups"
            backup_dir.mkdir()
            old_snapshot = backup_dir / "shared-before-migration-historical.sqlite3"
            old_snapshot.write_bytes(b"previous manually managed backup")
            live = sqlite3.connect(db_path)
            try:
                live.execute("PRAGMA journal_mode=WAL")
                live.execute("PRAGMA wal_autocheckpoint=0")
                live.execute("CREATE TABLE decisions (path TEXT PRIMARY KEY, choice TEXT)")
                live.execute("INSERT INTO decisions VALUES (?, ?)", ("p1", "keep"))
                live.commit()
                self.assertTrue(Path(str(db_path) + "-wal").exists())

                first = backup_database(db_path, backup_dir, "schema-upgrade", keep=1)
                with closing(sqlite3.connect(first)) as snapshot:
                    self.assertEqual(snapshot.execute(
                        "SELECT choice FROM decisions WHERE path='p1'"
                    ).fetchone()[0], "keep")

                live.execute("INSERT INTO decisions VALUES (?, ?)", ("p2", "delete"))
                live.commit()
                time.sleep(0.03)
                second = backup_database(db_path, backup_dir, "schema-upgrade", keep=1)
                self.assertNotEqual(first, second)
                self.assertTrue(second.exists())
                self.assertFalse(first.exists())
                self.assertEqual(old_snapshot.read_bytes(), b"previous manually managed backup")
                with closing(sqlite3.connect(second)) as snapshot:
                    self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(snapshot.execute(
                        "SELECT COUNT(*) FROM decisions"
                    ).fetchone()[0], 2)

                safe = backup_database(db_path, backup_dir, "../outside", keep=1)
                self.assertEqual(safe.parent, backup_dir)
                self.assertTrue(safe.name.startswith("shared-before-auto-"))
            finally:
                live.close()

    def test_separate_database_is_initialized(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.sqlite", "b.sqlite"):
                with connect_db(Path(folder) / name) as db:
                    self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


if __name__ == "__main__":
    unittest.main()
