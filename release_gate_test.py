#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release-gate invariants that must hold before PhotoCurator can be Stable."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

from catalog import (
    abort_catalog_scan,
    begin_catalog_scan,
    catalog_scan_batch,
    finish_catalog_scan,
    recent_scan_sessions,
    root_snapshot,
    storage_summary,
    clear_rebuildable_storage,
    update_media_lifecycle,
)
from db_runtime import connect_db, quick_check
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

        # Recoverable I/O/stat errors make the generation partial.  A partial
        # generation may update observations but must not declare unseen media missing.
        partial = begin_catalog_scan(db, root)
        catalog_scan_batch(db, partial, [a, root / "temporarily-unreadable.jpg"])
        finish_catalog_scan(db, partial, full_scan=True)
        snap = root_snapshot(db, first["root_id"], limit=20)
        require(snap["counts"].get("present") == 2,
                "partial scan incorrectly marked unseen media missing")
        latest_session = recent_scan_sessions(db, first["root_id"], limit=1)[0]
        require(latest_session["state"] == "partial",
                "scan I/O errors were not persisted as a partial generation")
        require(int(latest_session["error_count"] or 0) >= 1,
                "partial scan did not retain its error count")

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
        require(
            os.path.normcase(os.path.realpath(row["current_path"]))
            == os.path.normcase(os.path.realpath(str(moved))),
            "catalog current_path did not follow accepted file operation",
        )

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

        # Prove managed SQLite contexts release the file handle on Windows.
        with connect_db(db, timeout=5) as probe_db:
            probe_db.execute("SELECT 1").fetchone()
        moved_db = db.with_name("library_index.handle-check.sqlite3")
        os.replace(db, moved_db)
        os.replace(moved_db, db)

    # Performance architecture is a release invariant too. These source-level
    # checks protect hot paths from silently regressing in later UI work.
    core = Path("photo_curator.py").read_text(encoding="utf-8")
    catalog_src = Path("catalog.py").read_text(encoding="utf-8")
    db_src = Path("db_runtime.py").read_text(encoding="utf-8")
    tasks_src = Path("background_tasks.py").read_text(encoding="utf-8")
    manifest = json.loads(
        Path("packaging/release_manifest.json").read_text(encoding="utf-8")
    )
    version = re.search(r'^APP_VERSION = "([^"]+)"', core, re.M)
    require(version and version.group(1) == str(manifest["version"]),
            "runtime / release manifest version mismatch")
    require("@app.route('/api/status')" in core,
            "compact runtime status endpoint missing")
    require("def status_snapshot(self):" in tasks_src,
            "compact task status snapshot missing")
    require("setInterval(refreshTaskCenter" not in core,
            "task center regressed to unconditional fixed polling")
    require("fetchRuntimeStatus" in core and "runtimeStatusCache" in core,
            "runtime status request coalescing missing")
    require("nextDelay=(analysis||queued)?1200:8000" in core,
            "active/idle adaptive task polling missing")

    scan_start = core.index("def _shared_list_images")
    scan_end = core.index("def current_scan_snapshot", scan_start)
    scan_src = core[scan_start:scan_end]
    require("ready_paths = tuple(paths)" in scan_src,
            "shared scan immutable snapshot missing")
    require("entry['paths'] = list(paths)" not in scan_src,
            "shared scan regressed to repeated full-list copies")

    require("idx_media_catalog_current_path" in catalog_src
            and "idx_media_catalog_original_path" in catalog_src,
            "catalog path indexes missing")
    require("_SCHEMA_READY" in catalog_src and "_SCHEMA_INIT_LOCK" in catalog_src,
            "catalog schema one-time initialization guard missing")
    require("_WAL_READY" in db_src and "_WAL_LOCK" in db_src,
            "SQLite WAL one-time initialization guard missing")
    require("_ALLOWED_ROOTS_CACHE_KEY" in core and "_RECENTS_CACHE" in core,
            "thumbnail security-root cache missing")
    require("def _catalog_media_id_cached" in core,
            "thumbnail media-id cache missing")

    require("queueThumbSize" in core and "requestAnimationFrame" in core,
            "thumbnail resize is not frame-coalesced")
    require(".folder-group,.dedup-group,.photo-card,.dedup-choice{backdrop-filter:none!important" in core,
            "high-cardinality gallery blur returned")
    require("grid-template-columns:repeat(auto-fill,minmax(min(var(--thumb-size),100%),1fr))" in core,
            "adaptive gallery fill contract missing")
    require(".photo-card,.dedup-choice{content-visibility:auto" in core,
            "off-screen card rendering guard missing")
    require("fingerprints = _fingerprints(images)" in core,
            "Cull metadata reuse missing")
    require("rank_fingerprints = _fingerprints(paths)" in core,
            "Rank metadata reuse missing")
    require("Path(it['path']).is_file()" not in core,
            "Cull live classification regressed to repeated filesystem probes")

    print("Release gate invariants + performance architecture OK")


if __name__ == "__main__":
    main()
