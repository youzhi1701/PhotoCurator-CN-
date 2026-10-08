#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent PhotoCurator catalog.

A catalog source is a physical storage identity (disk / USB / card) or a
fallback folder identity. Library roots and media rows remain queryable while
that storage source is offline.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from db_runtime import connect_db


DRIVE_KIND = {
    2: "removable",
    3: "fixed",
    4: "network",
    5: "optical",
    6: "ramdisk",
}


CATALOG_SCHEMA_VERSION = 3


def _connect(db_path):
    return connect_db(db_path, timeout=30, row_factory=sqlite3.Row)


def _canonical_path(path):
    """Return one stable path spelling for persisted catalog comparisons."""
    return os.path.normpath(os.path.realpath(os.fspath(path)))


def note_catalog_scan_error(db_path, session, error, path=""):
    """Record a recoverable scan I/O error without unsafe missing reconciliation."""
    now = time.time()
    where = str(path or getattr(error, "filename", "") or "")
    detail = (f"{where}: {error}" if where else str(error))[:2000]
    with _connect(db_path) as db:
        db.execute(
            """UPDATE scan_session
               SET error_count=error_count+1,updated_at=?,
                   error=CASE WHEN error='' THEN ? ELSE error END
               WHERE session_id=? AND state='running'""",
            (now, detail, session["session_id"]),
        )
        db.commit()
    session["error_count"] = int(session.get("error_count") or 0) + 1


def init_catalog_schema(db_path):
    with _connect(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS data_source (
            source_id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            display_name TEXT NOT NULL,
            volume_guid TEXT NOT NULL DEFAULT '',
            volume_serial TEXT NOT NULL DEFAULT '',
            volume_label TEXT NOT NULL DEFAULT '',
            fs_type TEXT NOT NULL DEFAULT '',
            capacity_bytes INTEGER NOT NULL DEFAULT 0,
            last_mount TEXT NOT NULL DEFAULT '',
            connected INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS library_root (
            root_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            relative_root TEXT NOT NULL DEFAULT '',
            original_root TEXT NOT NULL,
            current_root TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL DEFAULT 0,
            last_scan_at REAL NOT NULL DEFAULT 0,
            photo_count INTEGER NOT NULL DEFAULT 0,
            analyzed_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_id, relative_root)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_library_root_source
                      ON library_root(source_id)""")
        db.execute("""CREATE TABLE IF NOT EXISTS media_catalog (
            media_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            original_path TEXT NOT NULL,
            current_path TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'present',
            lifecycle TEXT NOT NULL DEFAULT 'normal',
            scan_generation INTEGER NOT NULL DEFAULT 0,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            missing_since REAL,
            UNIQUE(root_id, relative_path)
        )""")
        media_cols = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(media_catalog)").fetchall()
        }
        if "lifecycle" not in media_cols:
            db.execute(
                "ALTER TABLE media_catalog ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'normal'"
            )
        if "scan_generation" not in media_cols:
            db.execute(
                "ALTER TABLE media_catalog ADD COLUMN scan_generation INTEGER NOT NULL DEFAULT 0"
            )
        db.execute("""CREATE INDEX IF NOT EXISTS idx_media_catalog_source_state
                      ON media_catalog(source_id, state)""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_media_catalog_root
                      ON media_catalog(root_id, relative_path)""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_media_catalog_generation
                      ON media_catalog(root_id, scan_generation)""")
        db.execute("""CREATE TABLE IF NOT EXISTS scan_session (
            session_id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            state TEXT NOT NULL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            finished_at REAL,
            files_seen INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_scan_session_root_state
                      ON scan_session(root_id, state, started_at)""")
        db.execute(
            """INSERT INTO schema_meta(key,value) VALUES('catalog_schema_version',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(CATALOG_SCHEMA_VERSION),),
        )
        db.commit()


def _windows_volume_info(path):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    resolved = str(Path(path).resolve())

    mount = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(resolved, mount, len(mount)):
        drive, _ = os.path.splitdrive(resolved)
        mount.value = (drive + "\\") if drive else resolved
    mount_path = mount.value

    guid = ctypes.create_unicode_buffer(32768)
    volume_guid = ""
    try:
        if kernel32.GetVolumeNameForVolumeMountPointW(
            mount_path, guid, len(guid)
        ):
            volume_guid = guid.value
    except Exception:
        volume_guid = ""

    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    max_component = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    ok = kernel32.GetVolumeInformationW(
        mount_path,
        label,
        len(label),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs,
        len(fs),
    )
    label_text = label.value if ok else ""
    fs_text = fs.value if ok else ""
    serial_text = f"{int(serial.value):08X}" if ok else ""
    drive_type = int(kernel32.GetDriveTypeW(mount_path))
    try:
        capacity = int(shutil.disk_usage(mount_path).total)
    except OSError:
        capacity = 0

    guid_key = volume_guid.strip().lower()
    # A vanished disk must not be registered as an anonymous all-zero volume.
    if not guid_key and not ok:
        raise OSError("无法读取磁盘唯一标识，请确认原设备已连接")
    if guid_key:
        identity_key = "win-guid:" + guid_key
    else:
        basis = f"{serial_text}|{fs_text}|{capacity}|{label_text}".lower()
        identity_key = "win-volume:" + hashlib.sha256(
            basis.encode("utf-8", errors="replace")
        ).hexdigest()

    return {
        "identity_key": identity_key,
        "kind": DRIVE_KIND.get(drive_type, "volume"),
        "mount_path": os.path.realpath(mount_path),
        "volume_guid": volume_guid,
        "volume_serial": serial_text,
        "volume_label": label_text,
        "fs_type": fs_text,
        "capacity_bytes": capacity,
    }


def volume_info_for_path(path):
    path = os.path.realpath(os.path.expanduser(str(path)))
    if os.name == "nt":
        # Falling back to a POSIX-style anchor on Windows silently treats an
        # unreadable/replaced volume as a different fake identity.
        return _windows_volume_info(path)

    anchor = Path(path).anchor or str(Path(path).parent)
    try:
        st = os.stat(anchor)
        device = str(st.st_dev)
    except OSError:
        device = "unknown"
    try:
        capacity = int(shutil.disk_usage(anchor).total)
    except OSError:
        capacity = 0
    basis = f"{device}|{anchor}|{capacity}"
    return {
        "identity_key": "posix-volume:" + hashlib.sha256(
            basis.encode("utf-8", errors="replace")
        ).hexdigest(),
        "kind": "volume",
        "mount_path": os.path.realpath(anchor),
        "volume_guid": "",
        "volume_serial": device,
        "volume_label": Path(anchor).name or anchor,
        "fs_type": "",
        "capacity_bytes": capacity,
    }


def _relative_to_mount(folder, mount_path):
    try:
        rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(mount_path))
        return "" if rel in (".", "") else rel
    except Exception:
        return ""


def _display_name(info):
    label = str(info.get("volume_label") or "").strip()
    kind = str(info.get("kind") or "volume")
    if label:
        return label
    if kind == "removable":
        return "可移动存储"
    if kind == "fixed":
        return "本地磁盘"
    return "照片数据源"


def register_source(db_path, folder, display_name=None):
    init_catalog_schema(db_path)
    folder = os.path.realpath(os.path.expanduser(str(folder)))
    if not Path(folder).is_dir():
        raise ValueError("照片数据源目录不存在")

    info = volume_info_for_path(folder)
    now = time.time()
    relative_root = _relative_to_mount(folder, info["mount_path"])

    with _connect(db_path) as db:
        row = db.execute(
            "SELECT source_id,display_name FROM data_source WHERE identity_key=?",
            (info["identity_key"],),
        ).fetchone()
        if row:
            source_id = str(row["source_id"])
            name = str(display_name or row["display_name"] or _display_name(info))
            db.execute(
                """UPDATE data_source SET kind=?,display_name=?,volume_guid=?,
                   volume_serial=?,volume_label=?,fs_type=?,capacity_bytes=?,
                   last_mount=?,connected=1,last_seen_at=? WHERE source_id=?""",
                (
                    info["kind"], name, info["volume_guid"],
                    info["volume_serial"], info["volume_label"], info["fs_type"],
                    int(info["capacity_bytes"]), info["mount_path"], now, source_id,
                ),
            )
        else:
            source_id = uuid.uuid4().hex
            name = str(display_name or _display_name(info))
            db.execute(
                """INSERT INTO data_source
                   (source_id,identity_key,kind,display_name,volume_guid,
                    volume_serial,volume_label,fs_type,capacity_bytes,last_mount,
                    connected,created_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, info["identity_key"], info["kind"], name,
                    info["volume_guid"], info["volume_serial"],
                    info["volume_label"], info["fs_type"],
                    int(info["capacity_bytes"]), info["mount_path"], 1, now, now,
                ),
            )

        root = db.execute(
            """SELECT root_id,original_root FROM library_root
               WHERE source_id=? AND relative_root=?""",
            (source_id, relative_root),
        ).fetchone()
        if root:
            root_id = str(root["root_id"])
            db.execute(
                """UPDATE library_root SET current_root=?,display_name=?,
                   last_seen_at=? WHERE root_id=?""",
                (folder, Path(folder).name or name, now, root_id),
            )
        else:
            root_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO library_root
                   (root_id,source_id,relative_root,original_root,current_root,
                    display_name,created_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    root_id, source_id, relative_root, folder, folder,
                    Path(folder).name or name, now, now,
                ),
            )
        db.commit()

    return {
        "source_id": source_id,
        "root_id": root_id,
        "display_name": name,
        "kind": info["kind"],
        "connected": True,
        "mount_path": info["mount_path"],
        "root_path": folder,
        "relative_root": relative_root,
        "identity_key": info["identity_key"],
        "volume_serial": info["volume_serial"],
        "volume_guid": info["volume_guid"],
    }


def _mounted_volume_map():
    """Return mounted Windows volumes without touching their filesystems.

    This function is used by the periodic UI connection refresh.  It must stay
    non-blocking: do not call Path.resolve(), GetVolumeInformationW,
    shutil.disk_usage(), os.stat() or enumerate directories here.  Those calls
    can stall for seconds/minutes on sleeping USB disks, empty card readers,
    disconnected network mappings and unhealthy volumes.

    Registration of a source still uses volume_info_for_path(), where richer
    metadata is appropriate because the user explicitly selected that source.
    """
    if os.name != "nt":
        return {}
    out = {}
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = int(kernel32.GetLogicalDrives())
        for i in range(26):
            if not ((mask >> i) & 1):
                continue
            root = f"{chr(65 + i)}:\\"
            try:
                drive_type = int(kernel32.GetDriveTypeW(root))
                # Skip unknown/no-root/CD-ROM drives.  Fixed/removable/network
                # volumes are presence-only probes here; no filesystem access.
                if drive_type in (0, 1, 5):
                    continue
                guid = ctypes.create_unicode_buffer(32768)
                volume_guid = ""
                try:
                    if kernel32.GetVolumeNameForVolumeMountPointW(
                        root, guid, len(guid)
                    ):
                        volume_guid = str(guid.value or "")
                except Exception:
                    volume_guid = ""
                info = {
                    "identity_key": (
                        "win-guid:" + volume_guid.strip().lower()
                        if volume_guid else ""
                    ),
                    "kind": DRIVE_KIND.get(drive_type, "volume"),
                    "mount_path": root,
                    "volume_guid": volume_guid,
                }
                if info["identity_key"]:
                    out[info["identity_key"]] = info
                # Also index by drive letter for legacy/fallback identities.
                out["win-mount:" + root.lower()] = info
            except Exception:
                continue
    except Exception:
        return {}
    return out


def _rebase_path(path, old_root, new_root):
    raw = str(path or "")
    if not raw:
        return raw
    try:
        old_cmp = os.path.normcase(os.path.realpath(str(old_root)))
        raw_real = os.path.realpath(raw)
        if os.path.commonpath([
            os.path.normcase(raw_real), old_cmp
        ]) != old_cmp:
            return raw
        rel = os.path.relpath(raw_real, os.path.realpath(str(old_root)))
        return os.path.realpath(os.path.join(str(new_root), rel))
    except Exception:
        return raw


def _rebase_persisted_paths(db, root_id, old_root, new_root):
    """Rebase drive-letter/mount changes without changing stable media_id."""
    if not old_root or not new_root:
        return 0
    if os.path.normcase(os.path.realpath(str(old_root))) == os.path.normcase(
        os.path.realpath(str(new_root))
    ):
        return 0

    media_rows = db.execute(
        """SELECT media_id,original_path,current_path FROM media_catalog
           WHERE root_id=?""",
        (str(root_id),),
    ).fetchall()
    path_map = {}
    for row in media_rows:
        old_original = str(row["original_path"] or "")
        old_current = str(row["current_path"] or "")
        new_original = _rebase_path(old_original, old_root, new_root)
        new_current = _rebase_path(old_current, old_root, new_root)
        db.execute(
            """UPDATE media_catalog SET original_path=?,current_path=?
               WHERE media_id=?""",
            (new_original, new_current, row["media_id"]),
        )
        if old_original != new_original:
            path_map[old_original] = new_original
        if old_current != new_current:
            path_map[old_current] = new_current

    tables = {
        str(row["name"])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for old_path, new_path in path_map.items():
        for table in ("cull_cache", "rank_cache", "review_override"):
            if table in tables:
                db.execute(
                    f"UPDATE {table} SET path=? WHERE path=?",
                    (new_path, old_path),
                )
        if "media_state" in tables:
            # original_path is a legacy identity key; rebase it together with
            # current_path while stable media identity remains media_catalog.media_id.
            db.execute(
                """UPDATE media_state
                   SET original_path=?,current_path=
                     CASE WHEN current_path=? THEN ? ELSE current_path END
                   WHERE original_path=?""",
                (new_path, old_path, new_path, old_path),
            )
            db.execute(
                "UPDATE media_state SET current_path=? WHERE current_path=?",
                (new_path, old_path),
            )
        if "similarity_group_member" in tables:
            db.execute(
                """UPDATE similarity_group_member
                   SET original_path=?,current_path=
                     CASE WHEN current_path=? THEN ? ELSE current_path END
                   WHERE original_path=?""",
                (new_path, old_path, new_path, old_path),
            )
            db.execute(
                """UPDATE similarity_group_member SET current_path=?
                   WHERE current_path=?""",
                (new_path, old_path),
            )
        if "software_trash" in tables:
            db.execute(
                "UPDATE software_trash SET original_path=? WHERE original_path=?",
                (new_path, old_path),
            )
            db.execute(
                "UPDATE software_trash SET trash_path=? WHERE trash_path=?",
                (new_path, old_path),
            )
    return len(path_map)


def refresh_connections(db_path):
    """Refresh only presence/mount state; never scan media or storage usage."""
    init_catalog_schema(db_path)
    mounted = _mounted_volume_map()
    now = time.time()
    with _connect(db_path) as db:
        sources = db.execute("SELECT * FROM data_source").fetchall()
        for row in sources:
            identity = str(row["identity_key"])
            if os.name == "nt":
                info = mounted.get(identity)
                if not info and identity.startswith("win-mount:"):
                    info = mounted.get(identity)
                connected = bool(info)
                mount_path = info["mount_path"] if info else str(row["last_mount"] or "")
            else:
                mount_path = str(row["last_mount"] or "")
                connected = bool(mount_path and Path(mount_path).exists())

            if connected:
                db.execute(
                    """UPDATE data_source SET connected=1,last_mount=?,
                       last_seen_at=? WHERE source_id=?""",
                    (mount_path, now, row["source_id"]),
                )
                roots = db.execute(
                    "SELECT root_id,relative_root,current_root FROM library_root WHERE source_id=?",
                    (row["source_id"],),
                ).fetchall()
                for root in roots:
                    current = os.path.realpath(
                        os.path.join(mount_path, str(root["relative_root"] or ""))
                    )
                    old_current = str(root["current_root"] or "")
                    _rebase_persisted_paths(
                        db, root["root_id"], old_current, current
                    )
                    db.execute(
                        """UPDATE library_root SET current_root=?,last_seen_at=?
                           WHERE root_id=?""",
                        (current, now, root["root_id"]),
                    )
            else:
                db.execute(
                    "UPDATE data_source SET connected=0 WHERE source_id=?",
                    (row["source_id"],),
                )
        db.commit()


def discover_mounted_devices(db_path):
    """Return lightweight mounted-device presence without crawling filesystems.

    Devices are matched to a persisted data source only by a stable identity.
    A reused drive letter alone is never enough to claim that an old source
    has returned.
    """
    init_catalog_schema(db_path)
    if os.name != "nt":
        return []
    mounted = _mounted_volume_map()
    unique = {}
    for key, info in mounted.items():
        mount = str(info.get("mount_path") or "")
        if not mount:
            continue
        existing = unique.get(mount)
        candidate = dict(info)
        # Prefer the stable GUID record when both GUID and mount aliases exist.
        if existing is None or str(candidate.get("identity_key") or "").startswith("win-guid:"):
            unique[mount] = candidate
    with _connect(db_path) as db:
        rows = db.execute(
            """SELECT source_id,identity_key,display_name,kind,last_mount,volume_guid
               FROM data_source"""
        ).fetchall()
    by_identity = {str(row["identity_key"]): row for row in rows}
    out = []
    for info in unique.values():
        identity = str(info.get("identity_key") or "")
        known = by_identity.get(identity) if identity.startswith("win-guid:") else None
        out.append({
            "identity_key": identity,
            "mount_path": str(info.get("mount_path") or ""),
            "kind": str(info.get("kind") or "volume"),
            "known": bool(known),
            "source_id": str(known["source_id"]) if known else "",
            "display_name": (
                str(known["display_name"])
                if known else Path(str(info.get("mount_path") or "")).drive or "新存储设备"
            ),
        })
    return sorted(out, key=lambda item: (not item["known"], item["mount_path"].lower()))


def list_sources(db_path, *, refresh=True):
    """List persisted sources without forcing hardware probes on first paint.

    Use refresh=False for fast UI startup; refresh physical device state in
    a later background request.
    """
    if refresh:
        refresh_connections(db_path)
    with _connect(db_path) as db:
        rows = db.execute(
            """SELECT s.*,r.root_id,r.relative_root,r.original_root,r.current_root,
                      r.display_name AS root_name,r.last_scan_at,r.photo_count,
                      r.analyzed_count
               FROM data_source s
               LEFT JOIN library_root r ON r.source_id=s.source_id
               ORDER BY s.connected DESC,s.last_seen_at DESC,r.created_at ASC"""
        ).fetchall()
    grouped = {}
    for row in rows:
        sid = str(row["source_id"])
        item = grouped.setdefault(sid, {
            "source_id": sid,
            "unique_photo_count": 0,
            "display_name": str(row["display_name"]),
            "kind": str(row["kind"]),
            "connected": bool(row["connected"]),
            "mount_path": str(row["last_mount"] or ""),
            "volume_serial": str(row["volume_serial"] or ""),
            "capacity_bytes": int(row["capacity_bytes"] or 0),
            "last_seen_at": float(row["last_seen_at"] or 0),
            "roots": [],
        })
        if row["root_id"]:
            item["roots"].append({
                "root_id": str(row["root_id"]),
                "relative_root": str(row["relative_root"] or ""),
                "original_root": str(row["original_root"] or ""),
                "current_root": str(row["current_root"] or ""),
                "display_name": str(row["root_name"] or ""),
                "last_scan_at": float(row["last_scan_at"] or 0),
                "photo_count": int(row["photo_count"] or 0),
                "analyzed_count": int(row["analyzed_count"] or 0),
            })
    # Single-root sources can use the already persisted scan count. Only
    # devices containing overlapping roots need a stable device-relative identity aggregate;
    # this avoids a whole-media-table walk on ordinary first paint.
    multiple_roots = []
    for item in grouped.values():
        roots = item["roots"]
        item["unique_photo_count"] = sum(int(r["photo_count"]) for r in roots)
        if len(roots) > 1:
            multiple_roots.append(item["source_id"])
    if multiple_roots:
        placeholders = ",".join("?" for _ in multiple_roots)
        with _connect(db_path) as db:
            unique_rows = db.execute(
                """SELECT m.source_id,
                          COUNT(DISTINCT LOWER(TRIM(REPLACE(
                            r.relative_root || '/' || m.relative_path,
                            CHAR(92), '/'), '/')))
                   FROM media_catalog m
                   JOIN library_root r ON r.root_id=m.root_id
                                       AND r.source_id=m.source_id
                   WHERE m.state='present' AND m.source_id IN (""" + placeholders + """)
                   GROUP BY m.source_id""",
                multiple_roots,
            ).fetchall()
        for sid, count in unique_rows:
            grouped[str(sid)]["unique_photo_count"] = int(count)
    return list(grouped.values())


def begin_catalog_scan(db_path, folder):
    """Start a durable full-scan generation.

    A previous interrupted scan is marked interrupted, never completed.  Only a
    completed generation is allowed to mark older catalog rows missing.
    """
    info = register_source(db_path, folder)
    now = time.time()
    generation = int(time.time_ns())
    session_id = uuid.uuid4().hex
    with _connect(db_path) as db:
        # Atomically replace an earlier running scan.  This serializes scan
        # starts with batch writes and finalization on the same SQLite file.
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE scan_session
               SET state='interrupted',finished_at=?,updated_at=?,
                   error=CASE WHEN error='' THEN 'previous scan did not complete' ELSE error END
               WHERE root_id=? AND state='running'""",
            (now, now, info["root_id"]),
        )
        db.execute(
            """INSERT INTO scan_session
               (session_id,root_id,source_id,generation,state,started_at,updated_at,
                files_seen,error_count,error)
               VALUES(?,?,?,?, 'running', ?, ?, 0, 0, '')""",
            (
                session_id, info["root_id"], info["source_id"],
                generation, now, now,
            ),
        )
        db.commit()
    return {
        **info,
        "session_id": session_id,
        "generation": generation,
        "files_seen": 0,
    }


def catalog_scan_batch(db_path, session, paths):
    """Persist one discovered batch without declaring unseen rows missing."""
    if not paths:
        return 0
    now = time.time()
    root = _canonical_path(session["root_path"])
    root_path = Path(root)
    generation = int(session["generation"])
    # Validate the physical device before each durable 512-file checkpoint.
    # Finalization still validates again before marking old paths missing.
    try:
        observed = volume_info_for_path(root)
        if (not Path(root).is_dir() or
                str(observed.get("identity_key") or "") !=
                str(session.get("identity_key") or "")):
            raise RuntimeError("扫描期间磁盘身份发生变化")
    except Exception as exc:
        abort_catalog_scan(db_path, session, f"source changed mid-scan: {exc}")
        raise RuntimeError("原始图库磁盘已断开或更换，扫描批次未写入") from exc
    rows = []
    errors = []
    for raw in paths:
        canonical = _canonical_path(raw)
        path = Path(canonical)
        try:
            st = path.stat()
        except OSError as exc:
            errors.append(f"{canonical}: {exc}")
            continue
        try:
            rel = str(path.relative_to(root_path))
        except Exception:
            try:
                rel = str(path.resolve().relative_to(root_path.resolve()))
            except Exception:
                # A scanner must never register a file outside its selected
                # root under a misleading basename.
                errors.append(f"{canonical}: file is outside scan root {root}")
                continue
        media_key = hashlib.sha256(
            f"{session['root_id']}|{os.path.normcase(rel)}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        rows.append((
            media_key, session["source_id"], session["root_id"], rel,
            canonical, canonical, int(st.st_size), int(st.st_mtime_ns),
            "present", "normal", generation, now, now,
        ))

    if not rows and not errors:
        return 0
    with _connect(db_path) as db:
        # Scan cancellation/replacement and batch persistence must be
        # serialized. Never let a stale worker overwrite a newer generation.
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            """SELECT state,generation,root_id FROM scan_session
               WHERE session_id=?""", (session["session_id"],),
        ).fetchone()
        if (not active or active["state"] != "running"
                or int(active["generation"]) != generation
                or active["root_id"] != session["root_id"]):
            raise RuntimeError("scan session is no longer active")
        if rows:
            db.executemany(
                """INSERT INTO media_catalog
                   (media_id,source_id,root_id,relative_path,original_path,current_path,
                    size,mtime_ns,state,lifecycle,scan_generation,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(root_id,relative_path) DO UPDATE SET
                     original_path=excluded.original_path,
                     current_path=excluded.current_path,
                     size=excluded.size,
                     mtime_ns=excluded.mtime_ns,
                     state='present',
                     scan_generation=excluded.scan_generation,
                     last_seen_at=excluded.last_seen_at,
                     missing_since=NULL""",
                rows,
            )
            db.execute(
                """UPDATE scan_session
                   SET files_seen=files_seen+?,updated_at=?
                   WHERE session_id=? AND state='running'""",
                (len(rows), now, session["session_id"]),
            )
        if errors:
            db.execute(
                """UPDATE scan_session
                   SET error_count=error_count+?,updated_at=?,
                       error=CASE WHEN error='' THEN ? ELSE error END
                   WHERE session_id=? AND state='running'""",
                (len(errors), now, errors[0][:2000], session["session_id"]),
            )
        db.execute(
            """UPDATE library_root SET last_seen_at=? WHERE root_id=?""",
            (now, session["root_id"]),
        )
        db.commit()
    session["files_seen"] = int(session.get("files_seen") or 0) + len(rows)
    session["error_count"] = int(session.get("error_count") or 0) + len(errors)
    return len(rows)


def finish_catalog_scan(db_path, session, *, full_scan=True):
    """Complete only if the same physical source is still mounted.

    A walk on a removed/reused drive letter may finish without a raised
    scandir error. Never interpret that as a deliberate empty library.
    """
    if full_scan:
        try:
            root = str(session["root_path"])
            if not Path(root).is_dir():
                raise FileNotFoundError(root)
            observed = volume_info_for_path(root)
            if str(observed.get("identity_key") or "") != str(session.get("identity_key") or ""):
                raise RuntimeError("扫描期间照片磁盘发生切换，拒绝把旧照片标记为丢失")
        except Exception as exc:
            abort_catalog_scan(db_path, session, f"source revalidation: {exc}")
            raise RuntimeError("数据源已断开或身份发生变化，扫描中止且历史图库得到保留") from exc
    now = time.time()
    with _connect(db_path) as db:
        # Finalization is serialized with cancellation and new scans.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT state,error_count,generation,root_id,files_seen FROM scan_session
               WHERE session_id=?""",
            (session["session_id"],),
        ).fetchone()
        if (not row or str(row["state"]) != "running"
                or int(row["generation"]) != int(session["generation"])
                or str(row["root_id"]) != str(session["root_id"])):
            raise RuntimeError("scan session is not active")
        error_count = int(row["error_count"] or 0)
        seen = int(row["files_seen"] or 0)
        # An unexpectedly empty traversal must never mark every historical
        # photo missing. A powered-down external drive can appear as an empty
        # directory on some Windows/virtualized mounts without a scandir error.
        prior = int(db.execute(
            """SELECT COUNT(*) FROM media_catalog
               WHERE root_id=? AND state='present'""",
            (session["root_id"],),
        ).fetchone()[0])
        suspect_empty = bool(full_scan and seen == 0 and prior > 0)
        if suspect_empty:
            error_count += 1
            db.execute(
                """UPDATE scan_session SET error_count=error_count+1,
                          error='empty scan refused: preserved existing media'
                   WHERE session_id=?""",
                (session["session_id"],),
            )
        reconcile_missing = bool(full_scan and error_count == 0)
        if reconcile_missing:
            db.execute(
                """UPDATE media_catalog
                   SET state='missing',missing_since=COALESCE(missing_since,?)
                   WHERE root_id=? AND scan_generation<>? AND state='present'""",
                (now, session["root_id"], int(session["generation"])),
            )
        present = int(db.execute(
            """SELECT COUNT(*) FROM media_catalog
               WHERE root_id=? AND state='present'""",
            (session["root_id"],),
        ).fetchone()[0])
        has_cull_cache = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cull_cache'"
        ).fetchone()
        if has_cull_cache:
            analyzed = int(db.execute(
                """SELECT COUNT(*) FROM media_catalog m
                   WHERE m.root_id=? AND EXISTS(
                     SELECT 1 FROM cull_cache c
                     WHERE c.path=m.current_path AND c.size=m.size AND c.mtime_ns=m.mtime_ns
                   )""",
                (session["root_id"],),
            ).fetchone()[0])
        else:
            analyzed = 0
        db.execute(
            """UPDATE library_root
               SET photo_count=?,analyzed_count=?,last_scan_at=?,last_seen_at=?
               WHERE root_id=?""",
            (present, analyzed, now, now, session["root_id"]),
        )
        terminal_state = "completed" if error_count == 0 else "partial"
        db.execute(
            """UPDATE scan_session
               SET state=?,finished_at=?,updated_at=?,files_seen=?
               WHERE session_id=?""",
            (
                terminal_state, now, now,
                seen,
                session["session_id"],
            ),
        )
        db.commit()
    session["photo_count"] = present
    session["analyzed_count"] = analyzed
    session["error_count"] = error_count
    session["state"] = terminal_state
    session["missing_reconciled"] = reconcile_missing
    return session


def abort_catalog_scan(db_path, session, error=""):
    """Record an interrupted scan without changing existing missing/present state."""
    now = time.time()
    try:
        with _connect(db_path) as db:
            db.execute(
                """UPDATE scan_session
                   SET state='interrupted',finished_at=?,updated_at=?,
                       error_count=error_count+1,error=?
                   WHERE session_id=? AND state='running'""",
                (now, now, str(error or "")[:2000], session["session_id"]),
            )
            db.commit()
    except Exception:
        pass


def catalog_media_scan(db_path, folder, paths, *, full_scan=True):
    """Compatibility wrapper for callers that already own a complete path list."""
    session = begin_catalog_scan(db_path, folder)
    try:
        for i in range(0, len(paths), 512):
            catalog_scan_batch(db_path, session, paths[i:i + 512])
        return finish_catalog_scan(db_path, session, full_scan=full_scan)
    except Exception as exc:
        abort_catalog_scan(db_path, session, str(exc))
        raise


def update_media_lifecycle(db_path, original_path, current_path, lifecycle):
    """Keep Media Catalog path/lifecycle aligned with accepted file operations."""
    original = _canonical_path(original_path)
    current = _canonical_path(current_path or original_path)
    with _connect(db_path) as db:
        db.execute(
            """UPDATE media_catalog
               SET current_path=?,lifecycle=?,last_seen_at=?
               WHERE original_path COLLATE NOCASE = ?
                  OR current_path COLLATE NOCASE = ?""",
            (current, str(lifecycle), time.time(), original, original),
        )
        db.commit()


def recent_scan_sessions(db_path, root_id=None, limit=20):
    init_catalog_schema(db_path)
    limit = max(1, min(100, int(limit or 20)))
    with _connect(db_path) as db:
        if root_id:
            rows = db.execute(
                """SELECT * FROM scan_session WHERE root_id=?
                   ORDER BY started_at DESC LIMIT ?""",
                (str(root_id), limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM scan_session ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def root_snapshot(db_path, root_id, limit=2000, offset=0):
    """Return persisted media metadata for connected or offline library roots."""
    init_catalog_schema(db_path)
    limit = max(1, min(5000, int(limit or 2000)))
    offset = max(0, int(offset or 0))
    with _connect(db_path) as db:
        root = db.execute(
            """SELECT r.*,s.display_name AS source_name,s.kind,s.connected,
                      s.last_mount,s.capacity_bytes,s.last_seen_at AS source_last_seen
               FROM library_root r
               JOIN data_source s ON s.source_id=r.source_id
               WHERE r.root_id=?""",
            (str(root_id),),
        ).fetchone()
        if not root:
            return None

        counts = {
            str(row["state"]): int(row["n"])
            for row in db.execute(
                """SELECT state,COUNT(*) AS n FROM media_catalog
                   WHERE root_id=? GROUP BY state""",
                (str(root_id),),
            ).fetchall()
        }
        total = sum(counts.values())
        tables = {
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        review_select = "r.tier AS review_tier" if "review_override" in tables else "'' AS review_tier"
        cull_select = (
            "CASE WHEN c.path IS NULL THEN 0 ELSE 1 END AS has_cull_cache"
            if "cull_cache" in tables else "0 AS has_cull_cache"
        )
        joins = []
        if "review_override" in tables:
            joins.append("LEFT JOIN review_override r ON r.path=m.current_path")
        if "cull_cache" in tables:
            joins.append(
                "LEFT JOIN cull_cache c ON c.path=m.current_path "
                "AND c.size=m.size AND c.mtime_ns=m.mtime_ns"
            )
        sql = f"""SELECT m.media_id,m.relative_path,m.original_path,m.current_path,
                         m.size,m.mtime_ns,m.state,m.lifecycle,m.first_seen_at,m.last_seen_at,
                         m.missing_since,{review_select},{cull_select}
                  FROM media_catalog m
                  {' '.join(joins)}
                  WHERE m.root_id=?
                  ORDER BY m.relative_path COLLATE NOCASE
                  LIMIT ? OFFSET ?"""
        rows = db.execute(sql, (str(root_id), limit, offset)).fetchall()

    return {
        "source": {
            "source_id": str(root["source_id"]),
            "display_name": str(root["source_name"]),
            "kind": str(root["kind"]),
            "connected": bool(root["connected"]),
            "last_mount": str(root["last_mount"] or ""),
            "capacity_bytes": int(root["capacity_bytes"] or 0),
            "last_seen_at": float(root["source_last_seen"] or 0),
        },
        "root": {
            "root_id": str(root["root_id"]),
            "display_name": str(root["display_name"]),
            "relative_root": str(root["relative_root"] or ""),
            "original_root": str(root["original_root"] or ""),
            "current_root": str(root["current_root"] or ""),
            "last_scan_at": float(root["last_scan_at"] or 0),
            "photo_count": int(root["photo_count"] or 0),
        },
        "counts": counts,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
        "items": [
            {
                "media_id": str(row["media_id"]),
                "relative_path": str(row["relative_path"]),
                "name": Path(str(row["relative_path"])).name,
                "original_path": str(row["original_path"]),
                "current_path": str(row["current_path"]),
                "size": int(row["size"] or 0),
                "mtime_ns": int(row["mtime_ns"] or 0),
                "state": str(row["state"]),
                "lifecycle": str(row["lifecycle"] or "normal"),
                "review_tier": str(row["review_tier"] or ""),
                "has_cull_cache": bool(row["has_cull_cache"]),
                "missing_since": (
                    float(row["missing_since"]) if row["missing_since"] is not None else None
                ),
            }
            for row in rows
        ],
    }


def media_record(db_path, media_id):
    init_catalog_schema(db_path)
    with _connect(db_path) as db:
        row = db.execute(
            """SELECT m.*,r.current_root,r.original_root
               FROM media_catalog m
               JOIN library_root r ON r.root_id=m.root_id
               WHERE m.media_id=?""",
            (str(media_id),),
        ).fetchone()
    return dict(row) if row else None


def media_id_for_path(db_path, path):
    real = _canonical_path(path)
    init_catalog_schema(db_path)
    with _connect(db_path) as db:
        row = db.execute(
            """SELECT media_id FROM media_catalog
               WHERE current_path COLLATE NOCASE = ?
                  OR original_path COLLATE NOCASE = ?
               ORDER BY last_seen_at DESC LIMIT 1""",
            (real, real),
        ).fetchone()
    return str(row["media_id"]) if row else None


def clear_offline_previews(data_root, db_path, root_id=None):
    """Explicitly clear durable offline previews; never called by cache cleanup."""
    preview_dir = Path(data_root) / "offline_previews"
    if not preview_dir.is_dir():
        return {"removed": 0, "freed_bytes": 0}
    allowed = None
    if root_id:
        with _connect(db_path) as db:
            allowed = {
                str(row["media_id"])
                for row in db.execute(
                    "SELECT media_id FROM media_catalog WHERE root_id=?",
                    (str(root_id),),
                ).fetchall()
            }
    removed = 0
    freed = 0
    for p in preview_dir.glob("*.jpg"):
        if allowed is not None and p.stem not in allowed:
            continue
        try:
            freed += int(p.stat().st_size)
            p.unlink()
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "freed_bytes": freed}


def remove_library_root(db_path, root_id):
    """Remove one PhotoCurator index only; never touch source photo files."""
    init_catalog_schema(db_path)
    with _connect(db_path) as db:
        root = db.execute(
            "SELECT root_id,source_id,display_name FROM library_root WHERE root_id=?",
            (str(root_id),),
        ).fetchone()
        if not root:
            return None
        media_ids = [
            str(row["media_id"])
            for row in db.execute(
                "SELECT media_id FROM media_catalog WHERE root_id=?",
                (str(root_id),),
            ).fetchall()
        ]
        count = len(media_ids)
        db.execute("DELETE FROM scan_session WHERE root_id=?", (str(root_id),))
        db.execute("DELETE FROM media_catalog WHERE root_id=?", (str(root_id),))
        db.execute("DELETE FROM library_root WHERE root_id=?", (str(root_id),))
        remaining = int(db.execute(
            "SELECT COUNT(*) FROM library_root WHERE source_id=?",
            (str(root["source_id"]),),
        ).fetchone()[0])
        source_removed = False
        if remaining == 0:
            db.execute(
                "DELETE FROM data_source WHERE source_id=?",
                (str(root["source_id"]),),
            )
            source_removed = True
        db.commit()
    return {
        "root_id": str(root_id),
        "source_id": str(root["source_id"]),
        "display_name": str(root["display_name"] or ""),
        "media_ids": media_ids,
        "media_count": count,
        "source_removed": source_removed,
    }


def storage_summary(data_root, db_path):
    data_root = Path(data_root)

    def tree_size(path):
        total = 0
        path = Path(path)
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return int(path.stat().st_size)
            except OSError:
                return 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += int(p.stat().st_size)
            except OSError:
                continue
        return total

    temporary_preview_bytes = tree_size(data_root / "cache" / "thumbnails")
    persistent_preview_bytes = tree_size(data_root / "offline_previews")
    db_bytes = tree_size(db_path)
    feature_bytes = tree_size(data_root / "config" / "dedup_features")
    log_bytes = tree_size(data_root / "logs")
    demo_bytes = tree_size(data_root / "内置测试数据")
    total = (
        db_bytes + persistent_preview_bytes + temporary_preview_bytes
        + feature_bytes + log_bytes + demo_bytes
    )
    return {
        "data_root": str(data_root),
        "database_bytes": db_bytes,
        "persistent_preview_bytes": persistent_preview_bytes,
        "preview_cache_bytes": temporary_preview_bytes,
        "dedup_feature_bytes": feature_bytes,
        "log_bytes": log_bytes,
        "demo_bytes": demo_bytes,
        "total_bytes": total,
    }


def clear_rebuildable_storage(data_root, category):
    """Clear only rebuildable runtime data; never touch catalog databases."""
    data_root = Path(data_root)
    category = str(category or "").strip().lower()
    targets = {
        "features": data_root / "config" / "dedup_features",
    }
    if category == "logs":
        log_dir = data_root / "logs"
        removed = 0
        freed = 0
        if log_dir.is_dir():
            for p in log_dir.iterdir():
                if not p.is_file() or p.name == "photocurator.log":
                    continue
                try:
                    freed += int(p.stat().st_size)
                    p.unlink()
                    removed += 1
                except OSError:
                    continue
        return {"category": category, "removed": removed, "freed_bytes": freed}

    if category == "previews":
        target = data_root / "cache" / "thumbnails"
        removed = 0
        freed = 0
        if target.is_dir():
            for p in target.iterdir():
                if not p.is_file():
                    continue
                try:
                    freed += int(p.stat().st_size)
                    p.unlink()
                    removed += 1
                except OSError:
                    continue
        target.mkdir(parents=True, exist_ok=True)
        # Durable offline previews live in data/offline_previews and are never
        # removed by the ordinary rebuildable-preview cleanup.
        return {"category": category, "removed": removed, "freed_bytes": freed}

    target = targets.get(category)
    if target is None:
        raise ValueError("unsupported storage cleanup category")

    removed = 0
    freed = 0
    if target.is_dir():
        for p in list(target.rglob("*")):
            if not p.is_file():
                continue
            try:
                freed += int(p.stat().st_size)
                p.unlink()
                removed += 1
            except OSError:
                continue
        for p in sorted(
            (x for x in target.rglob("*") if x.is_dir()),
            key=lambda x: len(x.parts),
            reverse=True,
        ):
            try:
                p.rmdir()
            except OSError:
                pass
    target.mkdir(parents=True, exist_ok=True)
    return {"category": category, "removed": removed, "freed_bytes": freed}
