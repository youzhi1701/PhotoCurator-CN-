#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release-gate invariants that must hold before PhotoCurator can be Stable."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image

from catalog import (
    abort_catalog_scan,
    begin_catalog_scan,
    catalog_scan_batch,
    finish_catalog_scan,
    root_snapshot,
    storage_summary,
    clear_rebuildable_storage,
    update_media_lifecycle,
)
from db_runtime import quick_check
from raw_loader import imread_bgr


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="photocurator_release_gate_") as td:
        base = Path(td)
        data = base / "data"
        root = base / "library"
        root.mkdir(parents=True)
        db = data / "config" / "library_index.sqlite3"

        a = root / "A.jpg"
        b = root / "B.jpg"
        Image.new("RGB", (80, 60), "red").save(a)
        Image.new("RGB", (80, 60), "blue").save(b)

        first = begin_catalog_scan(db, root)
        catalog_scan_batch(db, first, [a, b])
        finish_catalog_scan(db, first, full_scan=True)
        snap = root_snapshot(db, first["root_id"], limit=20)
        require(snap and snap["counts"].get("present") == 2,
                "initial catalog scan did not persist both photos")

        # Interrupted scans may add/update observations, but must never declare
        # older rows missing because the walk did not reach the end.
        interrupted = begin_catalog_scan(db, root)
        catalog_scan_batch(db, interrupted, [a])
        abort_catalog_scan(db, interrupted, "simulated unplug")
        snap = root_snapshot(db, first["root_id"], limit=20)
        require(snap["counts"].get("present") == 2,
                "interrupted scan incorrectly marked unseen media missing")

        completed = begin_catalog_scan(db, root)
        catalog_scan_batch(db, completed, [a])
        finish_catalog_scan(db, completed, full_scan=True)
        snap = root_snapshot(db, first["root_id"], limit=20)
        require(snap["counts"].get("present") == 1,
                "completed generation did not retain the seen photo")
        require(snap["counts"].get("missing") == 1,
                "completed generation did not mark the unseen photo missing")

        # File lifecycle is a catalog fact, not only an in-memory UI state.
        moved = root / "PhotoCurator_RecycleBin（软件回收站）" / "A.jpg"
        update_media_lifecycle(db, a, moved, "pending_trash")
        snap = root_snapshot(db, first["root_id"], limit=20)
        row = next(item for item in snap["items"] if item["name"] == "A.jpg")
        require(row["lifecycle"] == "pending_trash",
                "catalog lifecycle did not follow accepted file operation")
        require(os.path.normcase(row["current_path"]) == os.path.normcase(str(moved)),
                "catalog current_path did not follow accepted file operation")

        # Durable offline previews are outside rebuildable thumbnail cache.
        offline = data / "offline_previews"
        temporary = data / "cache" / "thumbnails"
        offline.mkdir(parents=True, exist_ok=True)
        temporary.mkdir(parents=True, exist_ok=True)
        (offline / "keep.jpg").write_bytes(b"offline")
        (temporary / "drop.jpg").write_bytes(b"temporary")
        before = storage_summary(data, db)
        require(before["persistent_preview_bytes"] > 0,
                "offline preview storage was not counted")
        clear_rebuildable_storage(data, "previews")
        require((offline / "keep.jpg").is_file(),
                "ordinary preview cleanup deleted durable offline preview")
        require(not (temporary / "drop.jpg").exists(),
                "ordinary preview cleanup did not clear temporary preview")

        # A Pillow-readable non-JPEG format must remain analyzable even when
        # OpenCV's native decoder is unavailable/limited.
        tif = root / "fallback.tiff"
        Image.new("RGB", (64, 48), "green").save(tif)
        decoded = imread_bgr(tif)
        require(decoded is not None and decoded.shape[0] > 0,
                "Pillow-readable TIFF could not enter the analysis path")

        require(quick_check(db) == ["ok"], "SQLite quick_check failed")

    print("Release gate invariants OK")


if __name__ == "__main__":
    main()
