#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared SQLite runtime policy for PhotoCurator.

All runtime components use the same local database file.  Keeping connection
settings centralized prevents one subsystem from silently using a different
locking/foreign-key policy than the others.
"""

from __future__ import annotations

import sqlite3
import os
import re
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
    """Publish a verified WAL-consistent backup without touching unrelated ones.

    Never expose a partial .sqlite3 file as a usable backup. Automatic backup
    retention is isolated from manual/pre-migration recovery snapshots.
    """
    src_path = Path(db_path)
    if not src_path.is_file() or src_path.stat().st_size <= 0:
        return None
    backup_dir = Path(backup_dir)
    if backup_dir.is_symlink():
        raise ValueError("refusing database backup through a linked directory")
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "-", str(label))[:48] or "manual"
    stem = f"{src_path.stem}-before-auto-{safe_label}-"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{stem}{stamp}-{time.time_ns()}.sqlite3"
    temporary = backup_dir / f".{target.name}.partial"
    source = destination = None
    try:
        source = sqlite3.connect(str(src_path), timeout=30)
        destination = sqlite3.connect(str(temporary), timeout=30)
        source.backup(destination)
        verdict = destination.execute("PRAGMA quick_check").fetchone()
        if not verdict or verdict[0] != "ok":
            raise sqlite3.DatabaseError("database backup integrity check failed")
        destination.close()
        destination = None
        source.close()
        source = None
        # Hard-link publishing is atomic on the same volume and refuses to
        # overwrite another backup. Only finished snapshots are discoverable.
        os.link(temporary, target)
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    candidates = sorted(
        backup_dir.glob(f"{stem}*.sqlite3"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for old in candidates[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
    return target
