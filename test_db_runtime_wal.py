#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ported v1.8 WAL optimization must preserve concurrent SQLite behavior."""
import tempfile
import threading
import unittest
from pathlib import Path

from db_runtime import connect_db, quick_check


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

    def test_separate_database_is_initialized(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.sqlite", "b.sqlite"):
                with connect_db(Path(folder) / name) as db:
                    self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


if __name__ == "__main__":
    unittest.main()
