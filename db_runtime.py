#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared SQLite runtime policy for PhotoCurator.

All runtime components use the same local database file.  Keeping connection
settings centralized prevents one subsystem from silently using a different
locking/foreign-key policy than the others.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


DEFAULT_BUSY_TIMEOUT_MS = 8_000

# WAL is persistent database metadata, not a per-connection preference. Reissuing
# PRAGMA journal_mode=WAL on every short-lived API connection adds needless
# coordination and can briefly contend with writers. Initialize it once per
# database path in this process.
_WAL_READY = set()
_WAL_LOCK = threading.Lock()


class PhotoCuratorConnection(sqlite3.Connection):
    """Close the OS database handle when a managed transaction exits.

    Python's stock sqlite3 context manager commits or rolls back but leaves the
    connection open. On Windows that can retain file locks after the with block.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db(db_path, *, timeout=30.0, row_factory=None):
    """Open a PhotoCurator SQLite connection with release-grade defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=float(timeout), factory=PhotoCuratorConnection)
    if row_factory is not None:
        db.row_factory = row_factory

    # Busy timeout protects short UI/API queries from transient write locks
    # created by analysis/cache/background-task transactions.
    db.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    wal_key = str(path.resolve())
    if wal_key not in _WAL_READY:
        with _WAL_LOCK:
            if wal_key not in _WAL_READY:
                try:
                    # WAL allows readers to proceed while background workers commit.
                    db.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    # Some unusual/network filesystems may reject WAL. Runtime
                    # state normally lives under LOCALAPPDATA; fall back safely.
                    pass
                _WAL_READY.add(wal_key)
    return db


def quick_check(db_path):
    """Return SQLite quick_check output for diagnostics/release validation."""
    with connect_db(db_path, timeout=30.0) as db:
        rows = db.execute("PRAGMA quick_check").fetchall()
    return [str(row[0]) for row in rows]


def read_schema_version(db_path, key):
    """Read a schema version marker without creating one."""
    path = Path(db_path)
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(str(path), timeout=5) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if not exists:
                return 0
            row = db.execute(
                "SELECT value FROM schema_meta WHERE key=?", (str(key),)
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def backup_database(db_path, backup_dir, label, *, keep=5):
    """Create a consistent SQLite backup and retain only the newest copies."""
    src_path = Path(db_path)
    if not src_path.is_file() or src_path.stat().st_size <= 0:
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{src_path.stem}-before-{label}-{stamp}.sqlite3"
    source = sqlite3.connect(str(src_path), timeout=30)
    dest = sqlite3.connect(str(target), timeout=30)
    try:
        source.backup(dest)
        dest.commit()
    finally:
        dest.close()
        source.close()

    candidates = sorted(
        backup_dir.glob(f"{src_path.stem}-before-*.sqlite3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return target
