#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared SQLite runtime policy for PhotoCurator.

All runtime components use the same local database file.  Keeping connection
settings centralized prevents one subsystem from silently using a different
locking/foreign-key policy than the others.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


DEFAULT_BUSY_TIMEOUT_MS = 8_000


def connect_db(db_path, *, timeout=30.0, row_factory=None):
    """Open a PhotoCurator SQLite connection with release-grade defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=float(timeout))
    if row_factory is not None:
        db.row_factory = row_factory

    # Busy timeout protects short UI/API queries from transient write locks
    # created by analysis/cache/background-task transactions.
    db.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    try:
        # WAL allows readers to proceed while background workers commit.
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # Some unusual/network filesystems may reject WAL.  Runtime state
        # normally lives under LOCALAPPDATA; fall back without preventing boot.
        pass
    return db


def quick_check(db_path):
    """Return SQLite quick_check output for diagnostics/release validation."""
    with connect_db(db_path, timeout=30.0) as db:
        rows = db.execute("PRAGMA quick_check").fetchall()
    return [str(row[0]) for row in rows]
