#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoCurator runtime path policy and legacy data migration."""

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ENV_DATA_DIR = "PHOTOCURATOR_DATA_DIR"
APP_DIR_NAME = "PhotoCurator"


def _env_path(name):
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def default_user_data_root():
    """Return the stable per-user writable data root for installed builds."""
    if os.name == "nt":
        base = _env_path("LOCALAPPDATA") or _env_path("APPDATA")
        if base is None:
            base = Path.home() / "AppData" / "Local"
        return base / APP_DIR_NAME / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = _env_path("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return base / APP_DIR_NAME


def resolve_data_root(*, frozen):
    """Resolve writable runtime state, honoring an explicit environment override."""
    override = _env_path(ENV_DATA_DIR)
    if override is not None:
        return override
    if frozen:
        return default_user_data_root()
    return Path.home() / ".photo_curator"


def legacy_frozen_data_root(install_root):
    """Previous v1.5 layout stored writable state next to the installed app."""
    return Path(install_root) / "data"


def _dir_has_entries(path):
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _link_or_copy(src, dst):
    """Prefer hard links for same-volume migration; fall back to a real copy."""
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _merge_recents(src, dst):
    try:
        old = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else []
    except Exception:
        old = []
    try:
        current = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else []
    except Exception:
        current = []
    merged = []
    for item in list(current) + list(old):
        value = str(item or "").strip()
        if value and value not in merged:
            merged.append(value)
    if merged:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps(merged[:24], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return len(merged)


def _merge_feature_tree(src_dir, dst_dir):
    copied = 0
    if not src_dir.is_dir():
        return copied
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(src, dst)
        copied += 1
    return copied


def _table_columns(db, schema, table):
    try:
        rows = db.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return [], []
    cols = [str(row[1]) for row in rows]
    pk = [
        str(row[1]) for row in sorted(rows, key=lambda row: int(row[5] or 0))
        if int(row[5] or 0) > 0
    ]
    return cols, pk


def _snapshot_sqlite(source_path, destination_path):
    """Create a verified, no-clobber SQLite snapshot, including committed WAL.

    Copying only the .sqlite3 file while a writer has uncheckpointed WAL pages
    can silently lose the latest catalog decisions. SQLite's online backup
    API reads a consistent committed snapshot across the database and WAL.
    Keep the temporary file beside the destination for atomic hard-link
    installation without overwriting a concurrent backup or database.
    """
    source_path, destination_path = Path(source_path), Path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.{time.time_ns()}.snapshot"
    )
    source = destination = None
    try:
        source = sqlite3.connect(str(source_path), timeout=30)
        destination = sqlite3.connect(str(temporary), timeout=30)
        source.backup(destination)
        verdict = destination.execute("PRAGMA quick_check").fetchone()
        if not verdict or verdict[0] != "ok":
            raise sqlite3.DatabaseError("SQLite snapshot integrity check failed")
        destination.close()
        destination = None
        source.close()
        source = None
        # Both files reside on the same filesystem. A hard link publishes
        # the complete snapshot atomically and fails if the name exists.
        try:
            os.link(temporary, destination_path)
        except FileExistsError:
            return False
        return True
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_sqlite_catalog(legacy_db, target_db):
    """Merge durable catalog rows without replacing newer target decisions."""
    if not legacy_db.is_file():
        return {"tables": {}, "rows": 0}
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if not target_db.exists():
        if _snapshot_sqlite(legacy_db, target_db):
            return {"tables": {"whole_db": "copied"}, "rows": -1}
        # Another startup created the destination concurrently: merge into
        # it rather than replacing its newer state.

    durable_tables = (
        "cull_cache",
        "rank_cache",
        "review_override",
        "geocode_cache",
        "media_state",
        "similarity_group_member",
        "similarity_group_state",
        "data_source",
        "library_root",
        "media_catalog",
        "cache_meta",
    )
    report = {"tables": {}, "rows": 0}
    # sqlite3.Connection's context manager commits/rolls back but does NOT
    # close the connection. Explicit close is required on Windows or the
    # attached legacy DB can remain locked after migration completes.
    db = sqlite3.connect(str(target_db), timeout=30)
    attached = False
    try:
        db.execute("ATTACH DATABASE ? AS legacy", (str(legacy_db),))
        attached = True
        for table in durable_tables:
            main_cols, main_pk = _table_columns(db, "main", table)
            old_cols, _ = _table_columns(db, "legacy", table)
            common = [c for c in main_cols if c in old_cols]
            if not common or not main_pk or not all(c in common for c in main_pk):
                continue
            old_rows = db.execute(
                f"SELECT {','.join(common)} FROM legacy.{table}"
            ).fetchall()
            if not old_rows:
                continue
            index = {name: idx for idx, name in enumerate(common)}
            updated_idx = index.get("updated_at")
            merged = 0
            for row in old_rows:
                key_vals = tuple(row[index[c]] for c in main_pk)
                where = " AND ".join(f"{c}=?" for c in main_pk)
                current = db.execute(
                    f"SELECT updated_at FROM main.{table} WHERE {where}",
                    key_vals,
                ).fetchone() if "updated_at" in main_cols else db.execute(
                    f"SELECT 1 FROM main.{table} WHERE {where}",
                    key_vals,
                ).fetchone()
                if current is not None:
                    if updated_idx is None:
                        continue
                    try:
                        if float(row[updated_idx] or 0) <= float(current[0] or 0):
                            continue
                    except Exception:
                        continue
                marks = ",".join("?" for _ in common)
                db.execute(
                    f"INSERT OR REPLACE INTO main.{table} "
                    f"({','.join(common)}) VALUES({marks})",
                    tuple(row),
                )
                merged += 1
            if merged:
                report["tables"][table] = merged
                report["rows"] += merged
        db.commit()
    finally:
        if attached:
            try:
                db.execute("DETACH DATABASE legacy")
            except sqlite3.DatabaseError:
                pass
        db.close()
    return report


def migrate_legacy_config(legacy_root, target_root):
    """Merge durable state from a legacy data root into the stable user catalog.

    Unlike the old all-or-nothing migration, an already-populated target no
    longer blocks recovery of older scan results. Newer target decisions win.
    Before a two-database merge, the current target DB is backed up.
    """
    legacy_root = Path(legacy_root)
    target_root = Path(target_root)
    legacy_config = legacy_root / "config"
    target_config = target_root / "config"

    try:
        if legacy_root.resolve() == target_root.resolve():
            return False
    except OSError:
        if str(legacy_root) == str(target_root):
            return False

    if not _dir_has_entries(legacy_config):
        return False

    target_root.mkdir(parents=True, exist_ok=True)
    target_config.mkdir(parents=True, exist_ok=True)
    report = {
        "source": str(legacy_root),
        "target": str(target_root),
        "at": time.time(),
        "sqlite": {},
        "dedup_features_copied": 0,
        "recents": 0,
    }

    legacy_db = legacy_config / "library_index.sqlite3"
    target_db = target_config / "library_index.sqlite3"
    if legacy_db.is_file() and target_db.is_file():
        backup_dir = target_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / (
            f"library_index-before-migration-{stamp}-{time.time_ns()}.sqlite3"
        )
        if not _snapshot_sqlite(target_db, backup):
            raise RuntimeError("migration snapshot destination already exists")

    if legacy_db.is_file():
        report["sqlite"] = _merge_sqlite_catalog(legacy_db, target_db)

    report["dedup_features_copied"] = _merge_feature_tree(
        legacy_config / "dedup_features",
        target_config / "dedup_features",
    )
    report["recents"] = _merge_recents(
        legacy_config / "recents.json",
        target_config / "recents.json",
    )

    # Preserve any other durable config file that does not exist in the target.
    for src in legacy_config.iterdir():
        if src.name in {"library_index.sqlite3", "dedup_features", "recents.json"}:
            continue
        dst = target_config / src.name
        if dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst, copy_function=_link_or_copy)
        elif src.is_file():
            _link_or_copy(src, dst)

    marker = target_root / ".migration-history.json"
    try:
        history = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history.append(report)
    marker.write_text(
        json.dumps(history[-20:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True
