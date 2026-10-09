#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Protect user decisions and source files during a legacy SQLite WAL migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime_paths import _merge_sqlite_catalog, _snapshot_sqlite, migrate_legacy_config


def open_live_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute(
        "CREATE TABLE review_override (path TEXT PRIMARY KEY,"
        " updated_at REAL, decision TEXT)"
    )
    db.commit()
    return db


class WalMigrationSafetyTests(unittest.TestCase):
    def test_new_catalog_copies_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy.sqlite3"
            target = root / "target.sqlite3"
            live = open_live_db(source)
            try:
                live.execute(
                    "INSERT INTO review_override VALUES (?,?,?)",
                    ("offline/favorite.jpg", 100, "keep"),
                )
                live.commit()
                self.assertTrue(Path(str(source) + "-wal").is_file())
                result = _merge_sqlite_catalog(source, target)
                self.assertEqual(result["tables"]["whole_db"], "copied")
                with sqlite3.connect(target) as copied:
                    rows = copied.execute(
                        "SELECT path, decision FROM review_override"
                    ).fetchall()
                self.assertEqual(rows, [("offline/favorite.jpg", "keep")])
            finally:
                live.close()

    def test_existing_catalog_snapshot_contains_wal_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "old" / "config" / "library_index.sqlite3"
            current = root / "new" / "config" / "library_index.sqlite3"
            old = open_live_db(legacy)
            new = open_live_db(current)
            try:
                old.executemany(
                    "INSERT INTO review_override VALUES (?,?,?)",
                    [("photo-A", 100, "delete"), ("photo-B", 120, "keep")],
                )
                old.commit()
                new.execute(
                    "INSERT INTO review_override VALUES (?,?,?)",
                    ("photo-A", 200, "keep"),
                )
                new.commit()
                self.assertTrue(migrate_legacy_config(root / "old", root / "new"))
                with sqlite3.connect(current) as db:
                    records = dict(db.execute(
                        "SELECT path, decision FROM review_override"
                    ).fetchall())
                self.assertEqual(records, {"photo-A": "keep", "photo-B": "keep"})
                backups = list((root / "new" / "backups").glob(
                    "library_index-before-migration-*.sqlite3"
                ))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as snapshot:
                    rows = snapshot.execute(
                        "SELECT path, decision FROM review_override"
                    ).fetchall()
                self.assertEqual(rows, [("photo-A", "keep")])
            finally:
                new.close()
                old.close()

    def test_snapshot_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source.db", root / "previous.db"
            a, b = open_live_db(source), open_live_db(target)
            try:
                a.execute(
                    "INSERT INTO review_override VALUES (?,?,?)",
                    ("source", 20, "delete"),
                )
                b.execute(
                    "INSERT INTO review_override VALUES (?,?,?)",
                    ("previous", 30, "keep"),
                )
                a.commit()
                b.commit()
                self.assertFalse(_snapshot_sqlite(source, target))
                self.assertEqual(
                    b.execute("SELECT decision FROM review_override").fetchone()[0],
                    "keep",
                )
            finally:
                a.close()
                b.close()


if __name__ == "__main__":
    unittest.main()
