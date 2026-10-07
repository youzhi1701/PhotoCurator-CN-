#!/usr/bin/env python3
"""
PhotoCurator 中文版 — 模糊筛选 · 相似去重 · 智能优选

核心原则：
  1 · 模糊筛选先分析、再人工复核；只有明确确认后才移动文件。
  2 · 相似去重先分组、再选择保留项；支持同组保留多张与跨文件夹全局对比。
  3 · 智能优选提供质量评分、人工移除/恢复与明确导出。

支持单文件夹或递归子文件夹处理。递归结果按来源文件夹组织，程序生成的
结果目录会自动排除，避免二次扫描。核心照片分析在本机执行；GPS 地图是
可选联网显示，不影响筛选流程。
"""

import os
import sys
import io
import json
import time
import shutil
import hashlib
import logging
import sqlite3
import urllib.request
from logging.handlers import RotatingFileHandler
import threading
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, asdict
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from PIL import Image, ImageOps

from raw_loader import (RAW_EXTS, HAS_RAWPY, is_raw,
                        HEIF_EXTS, HAS_HEIF, is_heif, needs_jpeg_preview,
                        open_image_pil, imread_bgr, imread_gray)
from photo_ranking_v3 import AdvancedPhotoAnalyzer, PhotoScoreV3
from photo_dedup_batch import FastBatchDeduplicator
from background_tasks import BackgroundTaskManager
from runtime_paths import resolve_data_root
from db_runtime import connect_db, quick_check as sqlite_quick_check
from catalog import (
    catalog_media_scan,
    begin_catalog_scan,
    catalog_scan_batch,
    finish_catalog_scan,
    abort_catalog_scan,
    init_catalog_schema,
    list_sources as catalog_list_sources,
    discover_mounted_devices as catalog_discover_devices,
    register_source as catalog_register_source,
    storage_summary as catalog_storage_summary,
    clear_rebuildable_storage,
    root_snapshot as catalog_root_snapshot,
    media_record as catalog_media_record,
    media_id_for_path as catalog_media_id_for_path,
    update_media_lifecycle as catalog_update_media_lifecycle,
    recent_scan_sessions as catalog_recent_scan_sessions,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Writable state is independent from replaceable program files. The desktop
# launcher resolves/migrates the installed data directory first and passes the
# exact root through PHOTOCURATOR_DATA_DIR; direct source runs keep ~/.photo_curator.
IS_FROZEN = bool(getattr(sys, 'frozen', False))
if IS_FROZEN:
    INSTALL_ROOT = Path(sys.executable).resolve().parent.parent
    RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
else:
    INSTALL_ROOT = Path(__file__).resolve().parent
    RESOURCE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = resolve_data_root(frozen=IS_FROZEN)

for _dir in (DATA_ROOT, DATA_ROOT / 'logs', DATA_ROOT / 'cache', DATA_ROOT / 'config'):
    try:
        _dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# Persist runtime diagnostics for the no-console desktop launcher. Keep logs
# bounded so long photo-library sessions cannot grow them indefinitely.
try:
    LOG_DIR = DATA_ROOT / 'logs'
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        LOG_DIR / 'photocurator.log',
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8',
    )
    _file_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)s | %(name)s | %(message)s'
    ))
    logging.getLogger().addHandler(_file_handler)
except Exception:
    LOG_DIR = None

app = Flask(__name__)

INDEX_DB = DATA_ROOT / 'config' / 'library_index.sqlite3'
_DB_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_RUN_GATE_LOCK = threading.Lock()
_FILE_PLAN_LOCK = threading.Lock()
_GEOCODE_LOCK = threading.Lock()
_GEOCODE_LAST_AT = 0.0
CULL_METRICS_VERSION = 1

def _db_init():
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS cull_cache (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            region_s REAL NOT NULL, quality REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS cache_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        row = db.execute("SELECT value FROM cache_meta WHERE key='cull_metrics_version'").fetchone()
        if row is None:
            # Existing v1.4.x cache entries use the current v1 metric formula.
            db.execute("INSERT INTO cache_meta(key,value) VALUES('cull_metrics_version',?)",
                       (str(CULL_METRICS_VERSION),))
        elif str(row[0]) != str(CULL_METRICS_VERSION):
            db.execute("DELETE FROM cull_cache")
            db.execute("UPDATE cache_meta SET value=? WHERE key='cull_metrics_version'",
                       (str(CULL_METRICS_VERSION),))
        db.execute("""CREATE TABLE IF NOT EXISTS rank_cache (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            score_json TEXT NOT NULL, updated_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS review_override (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            tier TEXT NOT NULL, move_selected INTEGER NOT NULL, updated_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            action TEXT NOT NULL, path TEXT, detail TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS software_trash (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_path TEXT NOT NULL,
            trash_path TEXT NOT NULL UNIQUE,
            source_step TEXT NOT NULL,
            deleted_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS geocode_cache (
            key TEXT PRIMARY KEY, label TEXT NOT NULL, updated_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS media_state (
            original_path TEXT PRIMARY KEY,
            current_path TEXT NOT NULL,
            state TEXT NOT NULL,
            source_step TEXT NOT NULL DEFAULT '',
            group_key TEXT,
            updated_at REAL NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_media_state_state
                      ON media_state(state, updated_at)""")
        db.execute("""CREATE TABLE IF NOT EXISTS similarity_group_member (
            group_key TEXT NOT NULL,
            original_path TEXT NOT NULL,
            current_path TEXT NOT NULL,
            name TEXT NOT NULL,
            rel_dir TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 0,
            lifecycle TEXT NOT NULL DEFAULT 'normal',
            updated_at REAL NOT NULL,
            PRIMARY KEY(group_key, original_path)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_similarity_member_original
                      ON similarity_group_member(original_path)""")
        db.execute("""CREATE TABLE IF NOT EXISTS similarity_group_state (
            group_key TEXT PRIMARY KEY,
            folder_root TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            member_hash TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL
        )""")
        cols = {r[1] for r in db.execute("PRAGMA table_info(similarity_group_state)").fetchall()}
        if 'member_hash' not in cols:
            db.execute("ALTER TABLE similarity_group_state ADD COLUMN member_hash TEXT NOT NULL DEFAULT ''")
        db.commit()

def _media_state_set(original_path, current_path=None, state_name='normal',
                     source_step='', group_key=None, detail=''):
    """Persist user-visible lifecycle state separately from analysis caches."""
    original = os.path.realpath(str(original_path))
    current = os.path.realpath(str(current_path or original_path))
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.execute(
                """INSERT INTO media_state
                   (original_path,current_path,state,source_step,group_key,updated_at,detail)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(original_path) DO UPDATE SET
                     current_path=excluded.current_path,
                     state=excluded.state,
                     source_step=excluded.source_step,
                     group_key=COALESCE(excluded.group_key,media_state.group_key),
                     updated_at=excluded.updated_at,
                     detail=excluded.detail""",
                (original, current, str(state_name), str(source_step or ''),
                 str(group_key) if group_key is not None else None,
                 time.time(), str(detail or ''))
            )
            db.commit()
    except Exception:
        logger.debug("media state save failed", exc_info=True)


def _media_state_get(original_path):
    original = os.path.realpath(str(original_path))
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            row = db.execute(
                "SELECT current_path,state,source_step,group_key,updated_at,detail "
                "FROM media_state WHERE original_path=?", (original,)
            ).fetchone()
        if row:
            return {'original_path': original, 'current_path': row[0], 'state': row[1],
                    'source_step': row[2], 'group_key': row[3],
                    'updated_at': float(row[4]), 'detail': row[5]}
    except Exception:
        logger.debug("media state load failed", exc_info=True)
    return None


def _similarity_group_key(folder_root, compare_scope, member_paths):
    """Resolve a stable group identity by member overlap before creating one."""
    originals = [os.path.realpath(str(p)) for p in member_paths if p]
    if originals:
        try:
            marks = ','.join('?' for _ in originals)
            with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
                rows = db.execute(
                    f"""SELECT group_key,COUNT(*) AS hits
                        FROM similarity_group_member
                        WHERE original_path IN ({marks})
                        GROUP BY group_key ORDER BY hits DESC,group_key LIMIT 1""",
                    originals
                ).fetchall()
            if rows:
                return str(rows[0][0])
        except Exception:
            logger.debug("similarity group overlap lookup failed", exc_info=True)
    anchor = min((os.path.normcase(p) for p in originals), default='')
    seed = '|'.join([
        os.path.normcase(os.path.realpath(str(folder_root))),
        str(compare_scope or 'folder'),
        anchor,
    ])
    return hashlib.sha1(seed.encode('utf-8')).hexdigest()[:20]


def _persist_similarity_group_members(group_key, member_rows):
    now = time.time()
    rows = []
    for m in member_rows:
        original = os.path.realpath(str(m.get('original_path') or m.get('path') or ''))
        if not original:
            continue
        current = os.path.realpath(str(m.get('path') or original))
        rows.append((
            str(group_key), original, current,
            str(m.get('name') or Path(original).name),
            str(m.get('rel_dir') or ''), float(m.get('score') or 0),
            1 if m.get('selected') else 0, str(m.get('lifecycle') or 'normal'), now
        ))
    if not rows:
        return
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.executemany(
                """INSERT INTO similarity_group_member
                   (group_key,original_path,current_path,name,rel_dir,score,selected,lifecycle,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_key,original_path) DO UPDATE SET
                     current_path=excluded.current_path,
                     name=excluded.name,
                     rel_dir=excluded.rel_dir,
                     score=excluded.score,
                     selected=excluded.selected,
                     lifecycle=CASE
                       WHEN similarity_group_member.lifecycle IN ('trashed','pending_trash','pending_permanent_delete','permanently_deleted')
                       THEN similarity_group_member.lifecycle
                       ELSE excluded.lifecycle END,
                     updated_at=excluded.updated_at""",
                rows
            )
            db.commit()
    except Exception:
        logger.debug("similarity group members save failed", exc_info=True)


def _merge_historical_group_members(group_key, live_rows):
    """Reattach trashed/deleted members so a reviewed group remains auditable."""
    by_original = {
        os.path.normcase(os.path.realpath(str(m.get('original_path') or m.get('path') or ''))): m
        for m in live_rows if m.get('path') or m.get('original_path')
    }
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            rows = db.execute(
                """SELECT original_path,current_path,name,rel_dir,score,selected,lifecycle
                   FROM similarity_group_member WHERE group_key=? ORDER BY original_path""",
                (str(group_key),)
            ).fetchall()
        for original,current,name,rel_dir,score,selected,lifecycle in rows:
            key = os.path.normcase(os.path.realpath(str(original)))
            if key in by_original:
                m=by_original[key]
                if lifecycle in ('pending_trash','pending_permanent_delete','trashed','permanently_deleted'):
                    m['lifecycle']=lifecycle
                    m['original_path']=str(original)
                    m['path']=str(current)
                    m['thumb']=thumb_url(str(current)) if Path(str(current)).is_file() else ''
                    m['selected']=False
                continue
            if lifecycle not in ('pending_trash','pending_permanent_delete','trashed','permanently_deleted'):
                continue
            by_original[key]={
                'name': str(name), 'original_path': str(original), 'path': str(current),
                'thumb': thumb_url(str(current)) if Path(str(current)).is_file() else '',
                'score': float(score or 0), 'selected': False,
                'rel_dir': str(rel_dir or ''), 'lifecycle': str(lifecycle),
            }
    except Exception:
        logger.debug("similarity historical member merge failed", exc_info=True)
    return list(by_original.values())


def _similarity_group_state(group_key, folder_root, member_paths):
    """Return persistent group status and detect members discovered after review."""
    member_hash = hashlib.sha1(
        '\n'.join(sorted(os.path.normcase(os.path.realpath(str(p))) for p in member_paths))
        .encode('utf-8')
    ).hexdigest()
    now = time.time()
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            row = db.execute(
                "SELECT status,member_hash,revision FROM similarity_group_state WHERE group_key=?",
                (str(group_key),)
            ).fetchone()
            if not row:
                status, revision = 'pending', 1
                db.execute(
                    """INSERT INTO similarity_group_state
                       (group_key,folder_root,status,member_hash,revision,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (str(group_key), os.path.realpath(str(folder_root)), status,
                     member_hash, revision, now)
                )
            else:
                old_status, old_hash, revision = str(row[0]), str(row[1] or ''), int(row[2] or 1)
                status = old_status
                if old_hash and old_hash != member_hash:
                    revision += 1
                    status = 'updated' if old_status == 'reviewed' else 'pending'
                db.execute(
                    """UPDATE similarity_group_state
                       SET folder_root=?,status=?,member_hash=?,revision=?,updated_at=?
                       WHERE group_key=?""",
                    (os.path.realpath(str(folder_root)), status, member_hash,
                     revision, now, str(group_key))
                )
            db.commit()
        return {'status': status, 'member_hash': member_hash, 'revision': revision}
    except Exception:
        logger.debug("similarity group state load failed", exc_info=True)
        return {'status': 'pending', 'member_hash': member_hash, 'revision': 1}


def _set_similarity_group_status(group_key, status):
    if status not in ('pending', 'reviewed', 'updated'):
        status = 'pending'
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.execute(
                "UPDATE similarity_group_state SET status=?,updated_at=? WHERE group_key=?",
                (status, time.time(), str(group_key))
            )
            db.commit()
    except Exception:
        logger.debug("similarity group status save failed", exc_info=True)


def _activity(action, path='', detail=''):
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            db.execute("INSERT INTO activity_log(ts,action,path,detail) VALUES(?,?,?,?)",
                       (time.time(), str(action), str(path or ''), str(detail or '')))
            db.execute("""DELETE FROM activity_log
                          WHERE id NOT IN (SELECT id FROM activity_log ORDER BY id DESC LIMIT 5000)""")
            db.commit()
    except Exception:
        logger.debug("activity log write failed", exc_info=True)

def _cached_cull_metrics(path):
    try:
        p = Path(path); st = p.stat()
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            row = db.execute(
                "SELECT region_s,quality FROM cull_cache WHERE path=? AND size=? AND mtime_ns=?",
                (str(p), int(st.st_size), int(st.st_mtime_ns))
            ).fetchone()
        return (float(row[0]), float(row[1])) if row else None
    except Exception:
        return None

def _save_cull_metrics(path, region_s, quality):
    try:
        p = Path(path); st = p.stat()
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            db.execute("""INSERT INTO cull_cache(path,size,mtime_ns,region_s,quality,updated_at)
                          VALUES(?,?,?,?,?,?)
                          ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                          region_s=excluded.region_s,quality=excluded.quality,updated_at=excluded.updated_at""",
                       (str(p), int(st.st_size), int(st.st_mtime_ns),
                        float(region_s), float(quality), time.time()))
            db.commit()
    except Exception:
        logger.debug("cull cache write failed", exc_info=True)

def _fingerprints(paths):
    """Return path -> (size, mtime_ns) once for a batch of existing files."""
    out = {}
    for raw in paths:
        try:
            p = Path(raw)
            st = p.stat()
            out[str(p)] = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            continue
    return out


def _query_cache_rows(table, columns, path_keys, chunk_size=400):
    """Read path-keyed cache rows in bounded IN() queries using one DB connection."""
    if not path_keys:
        return []
    rows = []
    with _DB_LOCK, connect_db(INDEX_DB, timeout=30) as db:
        keys = list(path_keys)
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            marks = ','.join('?' for _ in chunk)
            rows.extend(db.execute(
                f"SELECT {columns} FROM {table} WHERE path IN ({marks})", chunk
            ).fetchall())
    return rows


def _load_cull_metrics_map(paths):
    """Bulk-load valid clear/quality metrics without opening SQLite per photo."""
    fps = _fingerprints(paths)
    out = {}
    try:
        rows = _query_cache_rows(
            'cull_cache', 'path,size,mtime_ns,region_s,quality', fps.keys()
        )
        for path, size, mtime_ns, region_s, quality in rows:
            fp = fps.get(str(path))
            if fp == (int(size), int(mtime_ns)):
                out[str(path)] = (float(region_s), float(quality))
    except Exception:
        logger.debug("bulk cull cache load failed", exc_info=True)
    return out


def _load_rank_scores_map(paths):
    """Bulk-load valid rank scores; stale file versions are ignored."""
    fps = _fingerprints(paths)
    out = {}
    try:
        rows = _query_cache_rows(
            'rank_cache', 'path,size,mtime_ns,score_json', fps.keys()
        )
        for path, size, mtime_ns, score_json in rows:
            fp = fps.get(str(path))
            if fp != (int(size), int(mtime_ns)):
                continue
            try:
                payload = json.loads(score_json)
                if int(payload.pop('_cache_version', 0)) != RANK_CACHE_VERSION:
                    continue
                out[str(path)] = PhotoScoreV3(**payload)
            except Exception:
                continue
    except Exception:
        logger.debug("bulk rank cache load failed", exc_info=True)
    return out


def _load_review_overrides(paths):
    """Load valid manual decisions only for this scan, never the whole table."""
    fps = _fingerprints(paths)
    out = {}
    try:
        rows = _query_cache_rows(
            'review_override', 'path,size,mtime_ns,tier,move_selected', fps.keys()
        )
        for path, size, mtime_ns, tier, move_selected in rows:
            key = str(path)
            if fps.get(key) != (int(size), int(mtime_ns)):
                continue
            tier = str(tier)
            if tier not in ('sharp', 'soft', 'blurry'):
                continue
            out[key] = {'tier': tier, 'move_selected': bool(move_selected)}
    except Exception:
        logger.debug("review override load failed", exc_info=True)
    return out


def _save_review_overrides(rows):
    """Persist manual review choices keyed to the exact current file version."""
    payload = []
    now = time.time()
    for path, tier, move_selected in rows:
        try:
            p = Path(path); st = p.stat()
            if tier not in ('sharp', 'soft', 'blurry'):
                continue
            payload.append((str(p), int(st.st_size), int(st.st_mtime_ns),
                            tier, 1 if move_selected else 0, now))
        except OSError:
            continue
    if not payload:
        return
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=20) as db:
            db.executemany("""INSERT INTO review_override(path,size,mtime_ns,tier,move_selected,updated_at)
                              VALUES(?,?,?,?,?,?)
                              ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                              tier=excluded.tier,move_selected=excluded.move_selected,
                              updated_at=excluded.updated_at""", payload)
            db.commit()
    except Exception:
        logger.debug("review override save failed", exc_info=True)


def _delete_review_override(path):
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            db.execute("DELETE FROM review_override WHERE path=?", (str(path),))
            db.commit()
    except Exception:
        logger.debug("review override cleanup failed", exc_info=True)


RANK_CACHE_VERSION = 1

def _cached_rank_score(path):
    try:
        p = Path(path); st = p.stat()
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            row = db.execute(
                "SELECT score_json FROM rank_cache WHERE path=? AND size=? AND mtime_ns=?",
                (str(p), int(st.st_size), int(st.st_mtime_ns))
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if int(payload.pop('_cache_version', 0)) != RANK_CACHE_VERSION:
            return None
        return PhotoScoreV3(**payload)
    except Exception:
        return None

def _save_rank_score(path, score):
    try:
        p = Path(path); st = p.stat()
        payload = asdict(score)
        payload['_cache_version'] = RANK_CACHE_VERSION
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            db.execute("""INSERT INTO rank_cache(path,size,mtime_ns,score_json,updated_at)
                          VALUES(?,?,?,?,?)
                          ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                          score_json=excluded.score_json,updated_at=excluded.updated_at""",
                       (str(p), int(st.st_size), int(st.st_mtime_ns),
                        json.dumps(payload, ensure_ascii=False), time.time()))
            db.commit()
    except Exception:
        logger.debug("rank cache write failed", exc_info=True)


def _save_cull_metrics_batch(rows):
    if not rows:
        return
    payload = []
    now = time.time()
    for path, region_s, quality in rows:
        try:
            p = Path(path); st = p.stat()
            payload.append((str(p), int(st.st_size), int(st.st_mtime_ns),
                            float(region_s), float(quality), now))
        except OSError:
            continue
    if not payload:
        return
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=20) as db:
            db.executemany("""INSERT INTO cull_cache(path,size,mtime_ns,region_s,quality,updated_at)
                              VALUES(?,?,?,?,?,?)
                              ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                              region_s=excluded.region_s,quality=excluded.quality,updated_at=excluded.updated_at""",
                           payload)
            db.commit()
    except Exception:
        logger.debug("cull cache batch write failed", exc_info=True)


def _save_rank_scores_batch(rows):
    if not rows:
        return
    payload = []
    now = time.time()
    for path, score in rows:
        try:
            p = Path(path); st = p.stat()
            data = asdict(score)
            data['_cache_version'] = RANK_CACHE_VERSION
            payload.append((str(p), int(st.st_size), int(st.st_mtime_ns),
                            json.dumps(data, ensure_ascii=False), now))
        except OSError:
            continue
    if not payload:
        return
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=20) as db:
            db.executemany("""INSERT INTO rank_cache(path,size,mtime_ns,score_json,updated_at)
                              VALUES(?,?,?,?,?)
                              ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
                              score_json=excluded.score_json,updated_at=excluded.updated_at""",
                           payload)
            db.commit()
    except Exception:
        logger.debug("rank cache batch write failed", exc_info=True)


try:
    _db_init()
    init_catalog_schema(INDEX_DB)
except Exception:
    logger.warning("library index unavailable", exc_info=True)

TASK_MANAGER = BackgroundTaskManager(INDEX_DB, workers=1)

def _prune_index_db():
    """Keep indexes bounded without doing multi-million-row DELETE work every launch."""
    try:
        geo_cutoff = time.time() - 180 * 86400
        with _DB_LOCK, connect_db(INDEX_DB, timeout=30) as db:
            # Analysis/review caches intentionally have no time-based expiry:
            # an unchanged archive should remain incremental even a year later.
            db.execute("DELETE FROM geocode_cache WHERE updated_at < ?", (geo_cutoff,))
            caps = (
                ('cull_cache', 'path', 2_000_000),
                ('rank_cache', 'path', 2_000_000),
                ('review_override', 'path', 2_000_000),
                ('geocode_cache', 'key', 50_000),
            )
            for table, key, cap in caps:
                count = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if count > cap:
                    db.execute(
                        f"""DELETE FROM {table} WHERE {key} NOT IN
                            (SELECT {key} FROM {table}
                             ORDER BY updated_at DESC LIMIT ?)""",
                        (cap,)
                    )
            db.commit()
    except Exception:
        logger.debug("index prune skipped", exc_info=True)

threading.Thread(target=_prune_index_db, daemon=True,
                 name='photocurator-index-prune').start()

APP_VERSION = "1.7.0"
IS_CODESPACES = os.environ.get('CODESPACES', '').strip().lower() == 'true'
CODESPACE_NAME = os.environ.get('CODESPACE_NAME', '').strip()
_CODESPACES_DOMAIN_RAW = os.environ.get(
    'GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'app.github.dev'
).strip().lower()
CODESPACES_FORWARDING_DOMAIN = _CODESPACES_DOMAIN_RAW.split('://')[-1].strip('/.')

# macOS reserves port 5000 for the AirPlay Receiver (returns HTTP 403).
# Prefer 5014 ('50mm f/1.4'). Desktop mode supplies PHOTOCURATOR_PORT before
# importing this module. Browser compatibility mode automatically falls back
# to a free loopback port when 5014 is already occupied.
def _resolve_local_port():
    explicit = os.environ.get('PHOTOCURATOR_PORT')
    if explicit:
        return int(explicit)

    preferred = 5014
    if os.environ.get('PHOTOCURATOR_OPEN_BROWSER') != '1':
        return preferred

    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('127.0.0.1', preferred))
        return preferred
    except OSError:
        try:
            probe.close()
        except Exception:
            pass
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])
    finally:
        try:
            probe.close()
        except Exception:
            pass


PORT = _resolve_local_port()

# Codespaces is opt-in and fail-closed: only expose the Flask listener beyond
# loopback when GitHub supplied enough metadata to derive the one exact
# authenticated forwarded-host name for this Codespace/port.
CODESPACES_PUBLIC_HOST = None
if IS_CODESPACES and CODESPACE_NAME and CODESPACES_FORWARDING_DOMAIN:
    CODESPACES_PUBLIC_HOST = (
        f'{CODESPACE_NAME}-{PORT}.{CODESPACES_FORWARDING_DOMAIN}'.lower()
    )
SERVER_HOST = '0.0.0.0' if CODESPACES_PUBLIC_HOST else '127.0.0.1'

# Host headers we accept. Local desktop/browser mode stays loopback-only.
# Codespaces additionally accepts only its exact GitHub forwarded hostname.
_ALLOWED_HOSTS = {
    f'127.0.0.1:{PORT}', f'localhost:{PORT}', '127.0.0.1', 'localhost'
}
_ALLOWED_ORIGIN_HOSTS = {'127.0.0.1', 'localhost'}
if CODESPACES_PUBLIC_HOST:
    _ALLOWED_HOSTS.add(CODESPACES_PUBLIC_HOST)
    _ALLOWED_HOSTS.add(f'{CODESPACES_PUBLIC_HOST}:443')
    _ALLOWED_ORIGIN_HOSTS.add(CODESPACES_PUBLIC_HOST)


@app.before_request
def _guard_request():
    """Block forged local/cloud requests while allowing the exact Codespaces proxy.

    Desktop/browser mode accepts loopback only. In Codespaces the server binds
    to 0.0.0.0 so GitHub's authenticated port proxy can reach it. The Host must
    still match the one exact forwarded Codespace hostname. When browsers send
    an Origin header, it must also be same-origin.

    Referer is intentionally not used as an access-control signal: opening a
    private forwarded port from the Codespaces editor legitimately sends a
    github.dev editor Referer on the top-level GET.
    """
    host = (request.host or '').lower().rstrip('.')
    if host not in _ALLOWED_HOSTS:
        abort(403)

    origin = request.headers.get('Origin')
    if origin:
        from urllib.parse import urlparse
        origin_host = (urlparse(origin).hostname or '').lower().rstrip('.')
        if origin_host not in _ALLOWED_ORIGIN_HOSTS:
            abort(403)


@app.after_request
def _security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers.setdefault(
        'Content-Security-Policy',
        # GPS map resources are requested only through connect-src. Core UI,
        # scripts and image previews remain local.
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {MAP_TILES_ORIGIN}; "
        "worker-src 'self' blob:; child-src 'self' blob:")
    return resp

# Image discovery is capability-driven rather than a hand-maintained short
# whitelist.  Pillow registers every format its current build can actually
# decode (JPEG/PNG/TIFF/WebP/GIF/ICO/JPEG2000/QOI/etc. when available), then
# PhotoCurator adds RAW + HEIF families only when their optional decoders are
# installed.  This keeps scanning broad without claiming formats the runtime
# cannot open.
try:
    Image.init()
    PILLOW_IMG_EXTS = {
        str(ext).lower()
        for ext, fmt in Image.registered_extensions().items()
        if fmt in Image.OPEN
    }
except Exception:
    PILLOW_IMG_EXTS = set()

CORE_IMG_EXTS = {
    '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp',
}
IMG_EXTS = set(CORE_IMG_EXTS) | PILLOW_IMG_EXTS

RESULT_ROOT_DIR = 'PhotoCurator_Result（照片筛选结果）'
SOFTWARE_TRASH_DIR = 'PhotoCurator_RecycleBin（软件回收站）'
TRASH_MANIFEST_NAME = '.photocurator-trash.json'
RESULT_KIND_DIRS = {
    'Blurred': 'Blurred（模糊照片）',
    'Duplicates': 'Duplicates（重复照片）',
}
LEGACY_RESULT_DIRS = {
    'photocurator_result', 'blurred', 'duplicates',
    'photocurator_result（照片筛选结果）',
    'blurred（模糊照片）', 'duplicates（重复照片）',
    'photocurator_recyclebin', 'photocurator_recyclebin（软件回收站）',
}

# RAW formats (CR2/CR3, NEF, ARW, DNG, ...) — decoded via rawpy if installed.
if HAS_RAWPY:
    IMG_EXTS |= RAW_EXTS
else:
    logger.warning("=" * 64)
    logger.warning("未安装 rawpy：RAW 文件（CR2/CR3/NEF/ARW/DNG 等）将被跳过。")
    logger.warning("如需 RAW 支持，请安装：")
    logger.warning("    pip install rawpy")
    logger.warning("安装后请重新启动“照片筛选”。")
    logger.warning("=" * 64)
# HEIC/HEIF (iPhone photos) — decoded via pillow-heif if installed.
if HAS_HEIF:
    IMG_EXTS |= HEIF_EXTS
else:
    logger.warning("=" * 64)
    logger.warning("未安装 pillow-heif：HEIC/HEIF 文件（包括 iPhone 照片）将被跳过。")
    logger.warning("如需 HEIC/HEIF 支持，请安装：")
    logger.warning("    pip install pillow-heif")
    logger.warning("安装后请重新启动“照片筛选”。")
    logger.warning("=" * 64)
# The GPS map. OpenStreetMap's own tile servers refuse app traffic (their tile
# usage policy forbids it, and they answer with an "Access blocked" tile), so
# the map is MapLibre + OpenFreeMap, which is built for exactly this.
MAP_TILES_ORIGIN = 'https://tiles.openfreemap.org'
MAP_STYLE_LIGHT = MAP_TILES_ORIGIN + '/styles/positron'
MAP_STYLE_DARK = MAP_TILES_ORIGIN + '/styles/dark'
# MapLibre is vendored (see vendor/) so the app pulls no script off a CDN.
VENDOR_DIR = RESOURCE_ROOT / 'vendor'
VENDOR_FILES = {'maplibre-gl-csp.js': 'text/javascript',
                'maplibre-gl-csp-worker.js': 'text/javascript',
                'maplibre-gl.css': 'text/css'}

RECENTS_FILE = DATA_ROOT / 'config' / 'recents.json'
THUMB_DIR = DATA_ROOT / 'cache' / 'thumbnails'
OFFLINE_PREVIEW_DIR = DATA_ROOT / 'offline_previews'
THUMB_DIR.mkdir(parents=True, exist_ok=True)
OFFLINE_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
DEDUP_SIGNATURE_VERSION = 1
DEDUP_CACHE_DIR = DATA_ROOT / 'config' / 'dedup_features'
DEDUP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _migrate_legacy_dedup_cache():
    """Move old signature JSON files out of the 30-day thumbnail cache."""
    try:
        for old in THUMB_DIR.glob('dedup_*.json'):
            suffix = old.name[len('dedup_'):]
            target = DEDUP_CACHE_DIR / f'dedup_v{DEDUP_SIGNATURE_VERSION}_{suffix}'
            try:
                if target.exists():
                    old.unlink()
                else:
                    os.replace(old, target)
            except OSError:
                continue
    except Exception:
        logger.debug("legacy dedup cache migration skipped", exc_info=True)

_migrate_legacy_dedup_cache()

CACHE_MAX_BYTES = int(os.environ.get('PHOTOCURATOR_CACHE_MAX_BYTES',
                                     str(2 * 1024 * 1024 * 1024)))
CACHE_MAX_AGE_DAYS = 30


def _prune_thumb_cache():
    """Bound temporary preview cache without touching any source photos."""
    try:
        now = time.time()
        cutoff = now - CACHE_MAX_AGE_DAYS * 86400
        entries = []
        total = 0
        for p in THUMB_DIR.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                try:
                    p.unlink()
                    continue
                except OSError:
                    pass
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size

        if total > CACHE_MAX_BYTES:
            target = int(CACHE_MAX_BYTES * 0.85)
            for _, size, p in sorted(entries, key=lambda x: x[0]):
                if total <= target:
                    break
                try:
                    p.unlink()
                    total -= size
                except OSError:
                    continue
    except Exception as e:
        logger.debug(f"cache prune skipped: {e}")


threading.Thread(target=_prune_thumb_cache, daemon=True,
                 name='photocurator-cache-prune').start()

DEFAULT_WEIGHTS = {'aesthetic': 30, 'composition': 22, 'technical': 20,
                   'sharpness': 16, 'color': 12}
CATEGORIES = ['composition', 'technical', 'sharpness', 'color', 'aesthetic']

# Keep the WebView responsive on very large folders. Processing/state remains
# complete; this cap affects only one HTTP response rendered by the UI.
UI_LIVE_RESULT_CAP = 300
UI_RESULT_CHUNK = 200
UI_RESULT_CAP = UI_RESULT_CHUNK


# --------------------------------------------------------------------------- #
#  Security (v5)
#  The server only ever serves files that live inside a folder the user has
#  explicitly chosen (the current selection or a recent one). This stops a
#  crafted ?path= request from reading arbitrary files off the disk
#  (e.g. ../../etc/passwd or /Users/you/.ssh/id_rsa) via a malicious web page
#  pointed at 127.0.0.1.
# --------------------------------------------------------------------------- #
def _allowed_roots():
    """Resolved real paths the app is permitted to read from: the current
    folder plus any recently-used folders."""
    roots = []
    cur = state.get('folder')
    if cur:
        roots.append(cur)
    scan = state.get('scan') or {}
    if scan.get('output_mode') == 'custom' and scan.get('custom_output'):
        # A custom output folder is user-selected app state too. Files moved
        # there must remain viewable/deletable by the same safe media endpoints.
        roots.append(scan.get('custom_output'))
    try:
        roots.extend(load_recents())
    except Exception:
        pass
    out = []
    for r in roots:
        try:
            out.append(os.path.realpath(r))
        except Exception:
            continue
    return out


def _safe_image_path(raw):
    """Resolve a user-supplied ?path= to a real file and confirm it is an
    allowed image inside an allowed root. Returns a Path or None.

    realpath() collapses '..' and resolves symlinks, so neither path
    traversal nor a symlink planted in a watched folder can escape."""
    if not raw:
        return None
    try:
        real = os.path.realpath(raw)
    except Exception:
        return None
    p = Path(real)
    if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
        return None
    roots = _allowed_roots()
    if not roots:
        return None
    real_cmp = os.path.normcase(real)
    for root in roots:
        try:
            root_cmp = os.path.normcase(os.path.realpath(root))
            # commonpath avoids prefix tricks (Photos vs Photos2), handles
            # Windows case-insensitivity, and safely rejects different drives.
            if os.path.commonpath([real_cmp, root_cmp]) == root_cmp:
                return p
        except (ValueError, OSError):
            continue
    return None


def _blank():
    return {'running': False, 'progress': 0, 'status': '等待开始', 'photos': []}


state = {
    'folder': None,
    'weights': dict(DEFAULT_WEIGHTS),
    'topn': 50,
    'excluded': set(),
    'phone_bg': set(),   # paths flagged "suitable as phone wallpaper"
    'scan': {'recursive': True, 'compare_scope': 'folder',
             'output_mode': 'source', 'custom_output': ''},
    'cull':  {**_blank(), 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': [], 'overrides': {}, 'removed_paths': set(), 'cache_hits': 0},
    'dedup': {**_blank(), 'groups': 0, 'kept_paths': [], 'groups_data': [],
              'singleton_paths': [], 'all_singleton_paths': [], 'seen_paths': set(), 'applied': False},
    'rank':  {**_blank(), 'scores': [], 'total': 0, 'analyzed': 0, 'preview': [], 'preview_at': 0.0},
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def collapse_raw_jpg_pairs(paths, prefer):
    """Collapse RAW+JPG pairs of the SAME frame (same folder + same filename
    stem, e.g. IMG_0001.CR2 + IMG_0001.JPG) down to one file.
    prefer: 'raw' keeps the RAW, 'jpg' keeps the JPG; anything else = no-op.
    Returns (paths, pairs_collapsed); original order is preserved."""
    if prefer not in ('raw', 'jpg'):
        return paths, 0
    from collections import defaultdict
    groups = defaultdict(list)
    for p in paths:
        pp = Path(p)
        groups[(str(pp.parent), pp.stem.lower())].append(p)
    keep, collapsed = set(), 0
    for g in groups.values():
        raws = [p for p in g if is_raw(p)]
        others = [p for p in g if not is_raw(p)]
        if raws and others:
            keep.update(map(str, raws if prefer == 'raw' else others))
            collapsed += 1
        else:
            keep.update(map(str, g))
    return [p for p in paths if str(p) in keep], collapsed


def fmt_of(path):
    """Display format of a file: 'CR2', 'NEF', 'JPG', ... ('.jpeg' -> 'JPG')."""
    ext = Path(str(path)).suffix.lstrip('.').upper()
    return 'JPG' if ext == 'JPEG' else ext


def ftype_label(ftype):
    """Human label for a file-type filter value ('ext:nef' -> 'NEF')."""
    return ftype[4:].upper() if str(ftype).startswith('ext:') else str(ftype).upper()


def filter_ftype(paths, ftype):
    """Keep only the selected file type.
    'raw' = any RAW, 'heic' = any HEIF, 'standard' = non-RAW/non-HEIF,
    'ext:nef' = that exact format, 'all'/empty = no filtering."""
    if ftype == 'raw':
        return [p for p in paths if is_raw(p)]
    if ftype == 'heic':
        return [p for p in paths if is_heif(p)]
    if ftype in ('jpg', 'standard'):
        return [p for p in paths if not is_raw(p) and not is_heif(p)]
    if str(ftype).startswith('ext:'):
        want = ftype[4:].lower()
        return [p for p in paths
                if Path(str(p)).suffix.lstrip('.').lower() == want
                or (want == 'jpg' and Path(str(p)).suffix.lower() == '.jpeg')]
    return paths


def _is_output_dir_name(name):
    """Directories generated by PhotoCurator must never be re-scanned."""
    low = str(name or '').strip().lower()
    return (low in LEGACY_RESULT_DIRS
            or low in {
                '$recycle.bin', 'recycler', 'system volume information',
                'lost+found', '__macosx',
            }
            or low.startswith('top_')
            or low.startswith('phonebg'))


def iter_images(folder, recursive=False):
    """Yield supported image paths without preloading the whole library.

    Recursive os.walk already tells us which entries are files, so extension
    filtering happens before any extra stat/open call.  This matters on slow
    multi-terabyte USB disks.
    """
    root = Path(folder)
    if not root.is_dir():
        return

    def supported_name(name):
        if not name or name.startswith('.') or name.startswith('._'):
            return False
        return Path(name).suffix.lower() in IMG_EXTS

    if not recursive:
        try:
            for p in root.iterdir():
                if supported_name(p.name) and p.is_file():
                    yield p
        except OSError:
            return
        return

    custom_cmp = None
    try:
        scan = state.get('scan') or {}
        custom = scan.get('custom_output') if scan.get('output_mode') == 'custom' else ''
        if custom:
            custom_cmp = os.path.normcase(os.path.realpath(os.path.expanduser(custom)))
    except Exception:
        custom_cmp = None

    for cur, dirs, files in os.walk(root):
        base = Path(cur)
        kept_dirs = []
        for d in dirs:
            if d.startswith('.') or _is_output_dir_name(d):
                continue
            if custom_cmp:
                try:
                    child_cmp = os.path.normcase(os.path.realpath(base / d))
                    if child_cmp == custom_cmp:
                        continue
                except Exception:
                    pass
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for name in files:
            if supported_name(name):
                yield base / name


def list_images(folder, recursive=False):
    """Compatibility materialization for operations that need a complete list."""
    return sorted(iter_images(folder, recursive=recursive),
                  key=lambda p: os.path.normcase(str(p)))


def relative_folder(path, root):
    """Human-readable source folder relative to the selected scan root."""
    try:
        rel = Path(path).resolve().parent.relative_to(Path(root).resolve())
        txt = str(rel).replace('\\', ' / ')
        return txt if txt not in ('', '.') else '当前文件夹'
    except Exception:
        return Path(path).parent.name or '当前文件夹'


def _path_reservation_key(path):
    return os.path.normcase(os.path.realpath(str(path)))


def _unique_destination(dest, reserved=None):
    """Avoid overwriting existing or already-planned destinations."""
    dest = Path(dest)
    reserved_keys = {
        _path_reservation_key(p) for p in (reserved or ()) if p
    }

    def blocked(path):
        return path.exists() or _path_reservation_key(path) in reserved_keys

    if not blocked(dest):
        return dest
    stamp = time.strftime('%Y%m%d_%H%M%S')
    candidate = dest.with_name(f"{dest.stem}_{stamp}{dest.suffix}")
    n = 1
    while blocked(candidate):
        n += 1
        candidate = dest.with_name(f"{dest.stem}_{stamp}_{n}{dest.suffix}")
    return candidate


def _active_restore_reservations():
    """Return destinations already owned by queued/running restore tasks."""
    reserved = set()
    try:
        with connect_db(INDEX_DB, timeout=15) as db:
            rows = db.execute(
                """SELECT payload_json FROM background_task
                   WHERE kind='restore_trash' AND state IN ('queued','running')"""
            ).fetchall()
        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json or '{}')
                path = str(payload.get('restore_path') or '').strip()
                if path:
                    reserved.add(_path_reservation_key(path))
            except Exception:
                continue
    except Exception:
        logger.debug("restore destination reservation lookup failed", exc_info=True)
    return reserved


def _output_destination(src, kind, root, mode='source', custom_output=''):
    """Return the destination for a reviewed file action.

    source (default): keep results next to each original leaf folder under
      PhotoCurator_Result（照片筛选结果）/<kind（中文备注）>/.
    root: collect under selected-root/PhotoCurator_Result（照片筛选结果）/<kind>/ while
      preserving the original relative directory structure.
    custom: same as root mode but under a user supplied destination.
    """
    src = Path(src)
    root = Path(root).resolve()
    kind_key = 'Blurred' if kind == 'Blurred' else 'Duplicates'
    kind_dir = RESULT_KIND_DIRS[kind_key]
    try:
        rel_parent = src.resolve().parent.relative_to(root)
    except Exception:
        rel_parent = Path()

    if mode == 'root':
        base = root / RESULT_ROOT_DIR / kind_dir / rel_parent
    elif mode == 'custom' and str(custom_output or '').strip():
        base = Path(os.path.expanduser(str(custom_output).strip())).resolve() / kind_dir / rel_parent
    else:
        base = src.parent / RESULT_ROOT_DIR / kind_dir
    base.mkdir(parents=True, exist_ok=True)
    return _unique_destination(base / src.name)


def _photo_sidecars(path):
    """Return sidecars that belong to the same photo capture."""
    p = Path(path)
    out = []
    seen = set()
    for ext in ('.xmp', '.XMP', '.aae', '.AAE'):
        candidate = p.with_suffix(ext)
        key = os.path.normcase(os.path.realpath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            out.append(candidate)
    return out


def _safe_move_file(src, dst):
    """Move without overwrite; cross-volume moves are copy/verify/commit/delete."""
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(src), str(dst))
        return dst
    except OSError:
        pass

    tmp = dst.with_name(dst.name + '.photocurator-part')
    if tmp.exists():
        tmp.unlink()
    try:
        shutil.copy2(str(src), str(tmp))
        if int(tmp.stat().st_size) != int(src.stat().st_size):
            raise IOError("跨盘复制校验失败：文件大小不一致")
        try:
            with open(tmp, 'rb') as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        os.replace(str(tmp), str(dst))
        src.unlink()
        return dst
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _move_photo_bundle(src, dst):
    """Move a photo and its XMP/AAE sidecars as one rollback-capable bundle."""
    src = Path(src)
    dst = Path(dst)
    sidecars = _photo_sidecars(src)
    moved = []
    try:
        _safe_move_file(src, dst)
        moved.append((dst, src))
        for sidecar in sidecars:
            side_dst = dst.with_suffix(sidecar.suffix)
            if side_dst.exists():
                side_dst = _unique_destination(side_dst)
            _safe_move_file(sidecar, side_dst)
            moved.append((side_dst, sidecar))
        return dst
    except Exception:
        for moved_path, original_path in reversed(moved):
            try:
                if moved_path.exists() and not original_path.exists():
                    _safe_move_file(moved_path, original_path)
            except Exception:
                logger.error("photo bundle rollback failed", exc_info=True)
        raise


def _move_reviewed_files(paths, kind, root, mode='source', custom_output=''):
    result = {'moved': 0, 'failed': 0, 'skipped': 0, 'destinations': []}
    for raw in paths:
        src = Path(raw)
        if not src.is_file():
            result['skipped'] += 1
            continue
        try:
            dst = _output_destination(src, kind, root, mode, custom_output)
            _move_photo_bundle(src, dst)
            _activity('移动文件', str(src), str(dst))
            result['moved'] += 1
            result['destinations'].append({'old': str(src), 'new': str(dst)})
        except Exception as e:
            result['failed'] += 1
            logger.warning(f"move reviewed file failed {src}: {e}")
    return result


def _trash_manifest_file(root):
    if not root:
        return None
    return Path(os.path.realpath(str(root))) / SOFTWARE_TRASH_DIR / TRASH_MANIFEST_NAME


def _write_trash_manifest(root):
    """Keep restore metadata beside trash files so reinstall can recover it."""
    target = _trash_manifest_file(root)
    if target is None:
        return
    try:
        root_real = os.path.realpath(str(root))
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            rows = db.execute(
                "SELECT original_path,trash_path,source_step,deleted_at FROM software_trash"
            ).fetchall()
        data = []
        for original, trash_path, source_step, deleted_at in rows:
            try:
                if os.path.commonpath([os.path.realpath(str(original)), root_real]) != root_real:
                    continue
            except Exception:
                continue
            if Path(str(trash_path)).is_file():
                data.append({'original_path': str(original), 'trash_path': str(trash_path),
                             'source_step': str(source_step or ''), 'deleted_at': float(deleted_at)})
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + '.tmp')
        tmp.write_text(json.dumps({'version': 1, 'items': data}, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(target))
    except Exception:
        logger.debug("trash manifest save failed", exc_info=True)


def _import_trash_manifest(root):
    target = _trash_manifest_file(root)
    if target is None or not target.is_file():
        return
    try:
        doc = json.loads(target.read_text(encoding='utf-8'))
        rows = []
        for item in doc.get('items') or []:
            original = str(item.get('original_path') or '')
            trash_path = str(item.get('trash_path') or '')
            if not original or not Path(trash_path).is_file():
                continue
            rows.append((original, trash_path, str(item.get('source_step') or ''),
                         float(item.get('deleted_at') or time.time())))
        if rows:
            with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
                db.executemany(
                    "INSERT OR IGNORE INTO software_trash "
                    "(original_path,trash_path,source_step,deleted_at) VALUES(?,?,?,?)", rows
                )
                db.commit()
    except Exception:
        logger.debug("trash manifest import failed", exc_info=True)

def _trash_destination(src, root):
    """Choose a reversible PhotoCurator-owned trash path on the source drive."""
    src = Path(src).resolve()
    root = Path(root).resolve()
    try:
        rel_parent = src.parent.relative_to(root)
    except Exception:
        rel_parent = Path()
    base = root / SOFTWARE_TRASH_DIR / rel_parent
    base.mkdir(parents=True, exist_ok=True)
    return _unique_destination(base / src.name)


def _ensure_software_trash_record(original, trash_path, source_step):
    """Idempotently persist one reversible-trash record and return its id."""
    original = os.path.realpath(str(original))
    trash_path = os.path.realpath(str(trash_path))
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        row = db.execute(
            "SELECT id FROM software_trash WHERE trash_path=?",
            (trash_path,)
        ).fetchone()
        if row:
            return int(row[0])
        db.execute(
            """INSERT INTO software_trash(original_path,trash_path,source_step,deleted_at)
               VALUES(?,?,?,?)""",
            (original, trash_path, str(source_step or ''), time.time())
        )
        trash_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.commit()
    return trash_id


def _move_to_software_trash(src, root, source_step, planned_trash_path=None):
    """Move one photo into the reversible trash using a persisted planned path.

    The planned path is stored in the background-task payload before filesystem
    work starts. A retried task can therefore reconcile a move that completed
    just before the previous process stopped.
    """
    src = Path(src)
    original = str(src.resolve())
    root = Path(root).resolve()
    if planned_trash_path:
        dst = Path(planned_trash_path).resolve()
        trash_root = (root / SOFTWARE_TRASH_DIR).resolve()
        try:
            if os.path.commonpath([
                os.path.normcase(str(dst)), os.path.normcase(str(trash_root))
            ]) != os.path.normcase(str(trash_root)):
                raise ValueError("后台回收站目标超出当前照片库")
        except ValueError:
            raise
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise FileExistsError("计划的回收站目标已存在，未覆盖任何文件")
    else:
        dst = _trash_destination(src, root)

    _move_photo_bundle(src, dst)
    trash_path = str(dst.resolve())
    try:
        trash_id = _ensure_software_trash_record(
            original, trash_path, source_step
        )
    except Exception:
        try:
            Path(original).parent.mkdir(parents=True, exist_ok=True)
            if not Path(original).exists() and Path(trash_path).is_file():
                _move_photo_bundle(Path(trash_path), Path(original))
        except Exception:
            logger.error("software trash rollback failed", exc_info=True)
        raise
    _write_trash_manifest(root)
    _activity('移入软件回收站', original, trash_path)
    return trash_id, trash_path


def _trash_rows(root=None):
    """Return live software-trash rows, pruning records whose files disappeared."""
    if root:
        _import_trash_manifest(root)
    root_cmp = os.path.normcase(os.path.realpath(str(root))) if root else None
    stale = []
    rows = []
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        for row in db.execute(
            "SELECT id,original_path,trash_path,source_step,deleted_at "
            "FROM software_trash ORDER BY deleted_at DESC"
        ).fetchall():
            tid, original, trash_path, source_step, deleted_at = row
            if not Path(trash_path).is_file():
                stale.append((int(tid),))
                continue
            if root:
                try:
                    if os.path.commonpath([
                        os.path.normcase(os.path.realpath(original)), root_cmp
                    ]) != root_cmp:
                        continue
                except Exception:
                    continue
            rows.append({
                'id': int(tid),
                'name': Path(trash_path).name,
                'path': str(trash_path),
                'original_path': str(original),
                'source_step': str(source_step or ''),
                'deleted_at': float(deleted_at),
                'thumb': thumb_url(str(trash_path)),
                'rel_dir': relative_folder(original, root) if root else str(Path(original).parent),
            })
        if stale:
            db.executemany("DELETE FROM software_trash WHERE id=?", stale)
            db.commit()
    return rows


def _restore_trash_item(trash_id):
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        row = db.execute(
            "SELECT original_path,trash_path FROM software_trash WHERE id=?",
            (int(trash_id),)
        ).fetchone()
    if not row:
        raise FileNotFoundError("回收站记录不存在")
    original, trash_path = map(str, row)
    src = Path(trash_path)
    if not src.is_file():
        raise FileNotFoundError("回收站中的照片已不存在")
    desired = Path(original)
    desired.parent.mkdir(parents=True, exist_ok=True)
    restored = _unique_destination(desired)
    _move_photo_bundle(src, restored)
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        db.execute("DELETE FROM software_trash WHERE id=?", (int(trash_id),))
        db.commit()
    _write_trash_manifest(state.get('folder'))
    _activity('从软件回收站恢复', str(restored), original)
    return str(restored)


def _purge_trash_item(trash_id):
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        row = db.execute(
            "SELECT trash_path FROM software_trash WHERE id=?", (int(trash_id),)
        ).fetchone()
    if not row:
        raise FileNotFoundError("回收站记录不存在")
    target = Path(str(row[0]))
    if target.is_file():
        sidecars = _photo_sidecars(target)
        target.unlink()
        for sidecar in sidecars:
            try:
                sidecar.unlink()
            except OSError:
                logger.warning("sidecar delete failed: %s", sidecar)
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        db.execute("DELETE FROM software_trash WHERE id=?", (int(trash_id),))
        db.commit()
    _write_trash_manifest(state.get('folder'))
    _activity('永久删除', str(target), '软件回收站')


def _find_original_for_path(path):
    """Resolve a current/trash path back to the stable original identity."""
    raw = os.path.realpath(str(path))
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            row = db.execute(
                "SELECT original_path FROM media_state WHERE current_path=? OR original_path=? "
                "ORDER BY updated_at DESC LIMIT 1", (raw, raw)
            ).fetchone()
        return str(row[0]) if row else raw
    except Exception:
        return raw


def _apply_media_lifecycle(original_path, current_path, lifecycle, source_step='', trash_id=None):
    """Synchronize one file lifecycle change into every in-memory view."""
    original = os.path.realpath(str(original_path))
    current = os.path.realpath(str(current_path or original_path))
    deleted_states = {'pending_trash','pending_permanent_delete','trashed','permanently_deleted'}
    is_deleted = lifecycle in deleted_states
    _media_state_set(original, current, lifecycle, source_step, detail=str(trash_id or ''))
    try:
        catalog_update_media_lifecycle(INDEX_DB, original, current, lifecycle)
    except Exception:
        logger.debug("media catalog lifecycle sync failed", exc_info=True)
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.execute(
                """UPDATE similarity_group_member
                   SET current_path=?,lifecycle=?,selected=0,updated_at=?
                   WHERE original_path=?""",
                (current, lifecycle, time.time(), original)
            )
            db.commit()
    except Exception:
        logger.debug("similarity member lifecycle sync failed", exc_info=True)

    with _STATE_LOCK:
        cull = state['cull']
        matched_cull = None
        for p in cull.get('photos', []):
            identity = os.path.realpath(str(p.get('original_path') or p.get('path') or ''))
            if identity == original:
                matched_cull = p
                p['original_path'] = original
                p['path'] = current
                p['thumb'] = thumb_url(current) if Path(current).is_file() else ''
                p['lifecycle'] = lifecycle
                p['trash_id'] = trash_id
                p['move_selected'] = False
        if is_deleted:
            cull['sharp_paths'] = [p for p in cull.get('sharp_paths', [])
                                   if os.path.realpath(str(p)) != original]
            cull.setdefault('removed_paths', set()).add(original)
        else:
            cull.setdefault('removed_paths', set()).discard(original)
            if matched_cull and matched_cull.get('tier') != 'blurry' and current not in cull.get('sharp_paths', []):
                cull.setdefault('sharp_paths', []).append(current)
        active_cull = [p for p in cull.get('photos', [])
                       if p.get('lifecycle') not in deleted_states]
        cull['sharp'] = sum(1 for p in active_cull if p.get('tier') == 'sharp')
        cull['soft'] = sum(1 for p in active_cull if p.get('tier') == 'soft')
        cull['blurry'] = sum(1 for p in active_cull if p.get('tier') == 'blurry')

        dedup = state['dedup']
        for group in dedup.get('groups_data', []):
            changed = False
            for m in group.get('members', []):
                identity = os.path.realpath(str(m.get('original_path') or m.get('path') or ''))
                if identity == original:
                    m['original_path'] = original
                    m['path'] = current
                    m['thumb'] = thumb_url(current) if Path(current).is_file() else ''
                    m['lifecycle'] = lifecycle
                    m['trash_id'] = trash_id
                    m['selected'] = False
                    changed = True
            if not changed:
                continue
            group['selected_paths'] = [
                p for p in (group.get('selected_paths') or [])
                if os.path.realpath(str(p)) != original
            ]
            active = [m for m in group.get('members', [])
                      if m.get('lifecycle') not in deleted_states]
            if is_deleted and len(active) == 1:
                active[0]['selected'] = True
                group['selected_paths'] = [active[0].get('path')]
                group['status'] = 'reviewed'
            elif not is_deleted and len(active) > 1:
                group['status'] = 'updated'
            elif len(active) == 0:
                group['status'] = 'reviewed'
            group['active_count'] = len(active)
            group['deleted_count'] = sum(1 for m in group.get('members', [])
                                         if m.get('lifecycle') in deleted_states)
            if group.get('group_key'):
                _set_similarity_group_status(group['group_key'], group['status'])

        dedup['photos'] = [g for g in dedup.get('groups_data', []) if g.get('count', 0) > 1]
        dedup['groups'] = len(dedup['photos'])
        dedup['kept_paths'] = [
            p for g in dedup.get('groups_data', [])
            for p in (g.get('selected_paths') or [])
            if p and Path(p).is_file()
        ]
        if is_deleted:
            rank = state['rank']
            rank['scores'] = [sc for sc in rank.get('scores', [])
                              if os.path.realpath(str(getattr(sc, 'path', ''))) != original]
            rank['total'] = len(rank['scores'])
            rank['preview_at'] = 0.0


def _restore_group_status_for_member(original_path, status):
    if status not in ('pending', 'reviewed', 'updated'):
        return
    original = os.path.realpath(str(original_path))
    with _STATE_LOCK:
        for group in state['dedup'].get('groups_data', []):
            if any(
                os.path.realpath(str(m.get('original_path') or m.get('path') or '')) == original
                for m in group.get('members', [])
            ):
                group['status'] = status
                if group.get('group_key'):
                    _set_similarity_group_status(group['group_key'], status)
                    _persist_similarity_group_members(group['group_key'], group.get('members', []))
                break


def _background_move_to_trash(payload):
    original = os.path.realpath(str(payload['path']))
    folder = os.path.realpath(str(payload['folder']))
    source_step = str(payload.get('step') or '')
    planned_trash = str(payload.get('trash_path') or '').strip()
    target = _safe_image_path(original)
    try:
        if target is None or not Path(target).is_file():
            row = _media_state_get(original)
            if row and row.get('state') == 'trashed':
                return {'ok': True, 'already_done': True, 'path': row.get('current_path')}

            # Crash-safe continuation: the same-drive rename may already have
            # completed even though SQLite/lifecycle updates did not.
            if planned_trash and Path(planned_trash).is_file():
                trash_id = _ensure_software_trash_record(
                    original, planned_trash, source_step
                )
                _delete_review_override(original)
                _apply_media_lifecycle(
                    original, planned_trash, 'trashed', source_step, trash_id
                )
                _write_trash_manifest(folder)
                return {
                    'ok': True, 'recovered': True,
                    'original_path': original,
                    'trash_path': os.path.realpath(planned_trash),
                    'trash_id': trash_id,
                }
            raise FileNotFoundError("待删除照片已不存在")

        trash_id, trash_path = _move_to_software_trash(
            target, folder, source_step,
            planned_trash_path=(planned_trash or None)
        )
        _delete_review_override(original)
        _apply_media_lifecycle(original, trash_path, 'trashed', source_step, trash_id)
        return {'ok': True, 'original_path': original, 'trash_path': trash_path,
                'trash_id': trash_id}
    except Exception:
        # Foreground uses optimistic pending state. A normal filesystem failure
        # restores the previous lifecycle. If the planned trash file exists,
        # the move itself succeeded and a recovered task must reconcile it.
        if (target is not None and Path(target).is_file()
                and not (planned_trash and Path(planned_trash).is_file())):
            _apply_media_lifecycle(
                original, str(target), str(payload.get('previous_lifecycle') or 'normal'),
                source_step
            )
            _restore_group_status_for_member(original, payload.get('previous_group_status'))
        raise


def _background_permanent_delete(payload):
    path = os.path.realpath(str(payload['path']))
    original = _find_original_for_path(path)
    target = Path(path)
    try:
        if target.is_file():
            sidecars = _photo_sidecars(target)
            target.unlink()
            for sidecar in sidecars:
                try:
                    sidecar.unlink()
                except OSError:
                    logger.warning("sidecar delete failed: %s", sidecar)
        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.execute("DELETE FROM software_trash WHERE trash_path=? OR original_path=?",
                       (path, original))
            db.commit()
        _apply_media_lifecycle(original, path, 'permanently_deleted',
                               str(payload.get('step') or ''))
        _activity('永久删除', original, path)
        return {'ok': True, 'original_path': original, 'deleted_path': path}
    except Exception:
        if target.is_file():
            _apply_media_lifecycle(
                original, path, str(payload.get('previous_lifecycle') or 'normal'),
                str(payload.get('step') or '')
            )
            _restore_group_status_for_member(original, payload.get('previous_group_status'))
        raise


def _background_restore_trash(payload):
    trash_id = int(payload['trash_id'])
    original_hint = str(payload.get('original_path') or '').strip()
    restore_hint = str(payload.get('restore_path') or '').strip()

    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        row = db.execute(
            "SELECT original_path,trash_path,source_step FROM software_trash WHERE id=?", (trash_id,)
        ).fetchone()

    if not row:
        # If a prior attempt already committed the trash-row deletion, the
        # persisted restore path lets this recovered task finish lifecycle sync.
        if original_hint and restore_hint and Path(restore_hint).is_file():
            _apply_media_lifecycle(
                original_hint, restore_hint, 'normal',
                str(payload.get('source_step') or '')
            )
            return {
                'ok': True, 'already_done': True, 'recovered': True,
                'original_path': os.path.realpath(original_hint),
                'restored_path': os.path.realpath(restore_hint),
            }
        raise FileNotFoundError("回收站记录不存在")

    original, trash_path, source_step = str(row[0]), str(row[1]), str(row[2] or '')
    planned_restore = restore_hint or str(_unique_destination(Path(original)).resolve())
    try:
        trash_file = Path(trash_path)
        restored_file = Path(planned_restore)

        if trash_file.is_file():
            restored_file.parent.mkdir(parents=True, exist_ok=True)
            if restored_file.exists():
                raise FileExistsError("计划的恢复目标已存在，未覆盖任何文件")
            _move_photo_bundle(trash_file, restored_file)
        elif not restored_file.is_file():
            raise FileNotFoundError("回收站中的照片和计划恢复目标均不存在")

        with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
            db.execute("DELETE FROM software_trash WHERE id=?", (trash_id,))
            db.commit()
        _write_trash_manifest(state.get('folder'))
        _activity('从软件回收站恢复', str(restored_file), original)
        _apply_media_lifecycle(original, str(restored_file), 'normal', source_step)
        return {
            'ok': True,
            'original_path': original,
            'restored_path': str(restored_file.resolve()),
        }
    except Exception:
        if Path(trash_path).is_file():
            _apply_media_lifecycle(original, trash_path, 'trashed', source_step, trash_id)
        raise


def _background_purge_trash(payload):
    trash_id = int(payload['trash_id'])
    with _DB_LOCK, connect_db(INDEX_DB, timeout=15) as db:
        row = db.execute(
            "SELECT original_path,trash_path,source_step FROM software_trash WHERE id=?", (trash_id,)
        ).fetchone()
    if not row:
        return {'ok': True, 'already_done': True}
    original, trash_path, source_step = map(str, row)
    try:
        _purge_trash_item(trash_id)
        _apply_media_lifecycle(original, trash_path, 'permanently_deleted', source_step)
        return {'ok': True, 'original_path': original, 'deleted_path': trash_path}
    except Exception:
        if Path(trash_path).is_file():
            _apply_media_lifecycle(original, trash_path, 'trashed', source_step, trash_id)
        raise


TASK_MANAGER.register('move_to_trash', _background_move_to_trash)
TASK_MANAGER.register('permanent_delete', _background_permanent_delete)
TASK_MANAGER.register('restore_trash', _background_restore_trash)
TASK_MANAGER.register('purge_trash', _background_purge_trash)


def _thumb_cache_path(image_path):
    # Catalog-backed previews use a stable media id, so a drive-letter change
    # (F: -> G:) does not invalidate the offline preview.
    try:
        media_id = catalog_media_id_for_path(INDEX_DB, image_path)
        if media_id:
            return OFFLINE_PREVIEW_DIR / f"{media_id}.jpg"
    except Exception:
        pass

    p = Path(image_path)
    try:
        st = p.stat()
        mtime_ns = int(st.st_mtime_ns)
        size_bytes = int(st.st_size)
    except OSError:
        mtime_ns = 0
        size_bytes = 0
    key = hashlib.md5(
        f"{image_path}:{mtime_ns}:{size_bytes}:v3".encode()
    ).hexdigest()
    return THUMB_DIR / f"{key}.jpg"


def make_thumb_file(image_path, size=300):
    out = _thumb_cache_path(image_path)
    if out.exists():
        return out
    try:
        with open_image_pil(image_path) as src:   # RAW-aware
            src.draft('RGB', (size * 2, size * 2))
            # Honor EXIF orientation and fully detach from the source file
            # before saving, so Windows never keeps the original photo locked.
            img = ImageOps.exif_transpose(src).convert('RGB')
            img.thumbnail((size, size), Image.Resampling.BILINEAR)
            img.save(out, format='JPEG', quality=80)
        return out
    except Exception as e:
        logger.warning(f"thumb fail {image_path}: {e}")
        return None


def _background_build_offline_previews(payload):
    """Generate a small durable preview batch, then yield the worker queue."""
    root_id = str(payload.get('root_id') or '').strip()
    offset = max(0, int(payload.get('offset') or 0))
    if not root_id:
        return {'ok': False, 'error': 'missing root_id'}
    snap = catalog_root_snapshot(INDEX_DB, root_id, limit=64, offset=offset)
    if not snap:
        return {'ok': False, 'error': 'library root missing'}
    built = 0
    skipped = 0
    for item in snap.get('items') or []:
        if TASK_MANAGER.foreground_busy():
            time.sleep(0.01)
        if str(item.get('state') or '') != 'present':
            skipped += 1
            continue
        media_id = str(item.get('media_id') or '')
        if not media_id:
            skipped += 1
            continue
        durable = OFFLINE_PREVIEW_DIR / f"{media_id}.jpg"
        if durable.is_file():
            skipped += 1
            continue
        candidate = str(item.get('current_path') or '')
        if not candidate or not Path(candidate).is_file():
            skipped += 1
            continue
        made = make_thumb_file(candidate, size=360)
        if made and Path(made).is_file():
            built += 1
        else:
            skipped += 1

    next_offset = offset + len(snap.get('items') or [])
    if snap.get('has_more') and next_offset > offset:
        try:
            TASK_MANAGER.enqueue(
                'build_offline_previews',
                {'root_id': root_id, 'offset': next_offset},
                priority=90,
                idempotency_key=f"offline_previews:{root_id}:{next_offset}",
            )
        except Exception:
            logger.debug("offline preview continuation queue failed", exc_info=True)
    return {
        'ok': True,
        'root_id': root_id,
        'offset': offset,
        'built': built,
        'skipped': skipped,
        'has_more': bool(snap.get('has_more')),
    }


TASK_MANAGER.register('build_offline_previews', _background_build_offline_previews)


def thumb_url(image_path):
    # The &v tag busts the BROWSER's HTTP cache when thumbnail logic changes
    # (v3 = high-resolution source fingerprint + EXIF orientation). Without
    # a URL version bump, a browser may keep a stale response across upgrades.
    return '/api/thumb?path=' + quote(str(image_path)) + '&v=3'


def load_recents():
    try:
        if RECENTS_FILE.exists():
            return json.loads(RECENTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return []


def save_recent(folder):
    recents = [r for r in load_recents() if r != folder]
    recents.insert(0, folder)
    try:
        RECENTS_FILE.write_text(json.dumps(recents[:8], ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logger.warning(f"save recents fail: {e}")


def ensure_builtin_demo():
    """Create a small but realistic multi-folder library for real UI testing.

    36 JPEGs live under several nested folders. The set deliberately contains
    sharp/soft/blurry frames and near-duplicate bursts so recursive scanning,
    source-folder grouping and similarity review can all be exercised.
    """
    demo = DATA_ROOT / '内置测试数据'
    try:
        demo.mkdir(parents=True, exist_ok=True)
        from PIL import ImageDraw, ImageFilter, ImageEnhance
        import random

        # Remove only the old canonical flat demo fixtures. User-created files
        # under the demo root are never touched.
        for old in demo.glob('测试_*_*.jpg'):
            try:
                old.unlink()
            except OSError:
                pass

        folders = [
            ('旅行/海边日落', 'coast'),
            ('旅行/山野徒步', 'mountain'),
            ('家庭/室内聚会', 'indoor'),
            ('城市/夜景', 'city'),
            ('手机导入/2026-10', 'daily'),
            ('重复测试/连拍组', 'burst'),
        ]
        kinds = ('sharp', 'soft', 'blurry')
        kind_zh = {'sharp': '清晰', 'soft': '轻微软', 'blurry': '模糊'}

        def gradient(size, top, bottom):
            w, h = size
            rows = []
            for y in range(h):
                t = y / max(1, h - 1)
                rows.append(tuple(
                    int(top[c] * (1 - t) + bottom[c] * t)
                    for c in range(3)
                ))
            strip = Image.new('RGB', (1, h))
            strip.putdata(rows)
            return strip.resize((w, h), Image.Resampling.BILINEAR)

        def draw_scene(scene, seed, variant):
            rng = random.Random(seed)
            w, h = 960, 640

            if scene == 'coast':
                img = gradient((w, h), (92, 139, 206), (246, 178, 122))
                d = ImageDraw.Draw(img)
                d.rectangle((0, 385, w, h), fill=(44, 111, 143))
                d.ellipse((690 + variant * 3, 105, 790 + variant * 3, 205),
                          fill=(255, 224, 151))
                d.polygon([(0, 430), (180, 380), (330, 430), (500, 392),
                           (650, 438), (w, 400), (w, 470), (0, 470)],
                          fill=(32, 68, 82))
                for x in (130, 330, 545, 760):
                    d.line((x, 420, x + 18, 520), fill=(28, 39, 45), width=5)
                    d.ellipse((x - 10, 390, x + 25, 430), fill=(28, 39, 45))
            elif scene == 'mountain':
                img = gradient((w, h), (116, 166, 213), (224, 232, 220))
                d = ImageDraw.Draw(img)
                d.polygon([(0, 410), (180, 205), (330, 410)], fill=(74, 104, 103))
                d.polygon([(210, 420), (480, 150), (720, 420)], fill=(61, 88, 91))
                d.polygon([(510, 430), (760, 235), (w, 430)], fill=(83, 112, 104))
                d.polygon([(410, h), (500 + variant * 4, 390), (585, h)],
                          fill=(172, 146, 111))
                for _ in range(28):
                    x = rng.randint(0, w - 1); y = rng.randint(360, h - 20)
                    d.polygon([(x, y), (x - 9, y + 32), (x + 9, y + 32)],
                              fill=(41, 89 + rng.randint(0, 30), 66))
            elif scene == 'indoor':
                img = gradient((w, h), (239, 211, 178), (183, 132, 99))
                d = ImageDraw.Draw(img)
                d.rectangle((585, 70, 890, 330), fill=(191, 222, 231),
                            outline=(247, 240, 220), width=14)
                d.rectangle((0, 430, w, h), fill=(111, 73, 53))
                d.rectangle((170, 365, 810, 500), fill=(156, 103, 67))
                for p in range(4):
                    cx = 250 + p * 155 + variant * (p % 2)
                    d.ellipse((cx - 38, 245, cx + 38, 321),
                              fill=(214, 168 - p * 6, 132))
                    d.rounded_rectangle((cx - 55, 315, cx + 55, 430),
                                        radius=24,
                                        fill=(80 + p * 25, 94 + p * 9, 126 + p * 12))
                for x in (310, 445, 590):
                    d.ellipse((x, 395, x + 55, 435), fill=(228, 204, 152))
            elif scene == 'city':
                img = gradient((w, h), (24, 28, 59), (78, 48, 88))
                d = ImageDraw.Draw(img)
                base = 555
                for x in range(-20, w, 95):
                    bw = rng.randint(70, 110); bh = rng.randint(180, 390)
                    d.rectangle((x, base - bh, x + bw, base),
                                fill=(28 + rng.randint(0, 20), 35, 54))
                    for wx in range(x + 14, x + bw - 10, 24):
                        for wy in range(base - bh + 20, base - 15, 30):
                            if rng.random() > .45:
                                d.rectangle((wx, wy, wx + 8, wy + 10),
                                            fill=(244, 197 + rng.randint(0, 40), 110))
                d.rectangle((0, 555, w, h), fill=(34, 35, 43))
                for x in range(0, w, 125):
                    d.ellipse((x + variant * 2, 575, x + 18 + variant * 2, 585),
                              fill=(245, 212, 150))
            elif scene == 'daily':
                img = gradient((w, h), (168, 206, 222), (224, 221, 185))
                d = ImageDraw.Draw(img)
                d.rectangle((0, 400, w, h), fill=(102, 149, 96))
                d.rectangle((90, 290, 405, 520), fill=(228, 222, 204))
                d.polygon([(65, 300), (250, 160), (435, 300)], fill=(117, 87, 74))
                d.rectangle((620, 290, 815, 520), fill=(201, 187, 164))
                for _ in range(22):
                    x = rng.randint(0, w); y = rng.randint(380, h)
                    d.ellipse((x, y, x + 10, y + 10), fill=(76, 127, 71))
            else:  # burst / near-duplicate people-like outdoor sequence
                img = gradient((w, h), (118, 174, 211), (210, 222, 188))
                d = ImageDraw.Draw(img)
                d.rectangle((0, 405, w, h), fill=(87, 137, 75))
                shift = (variant % 3) * 7
                for p in range(3):
                    cx = 330 + p * 130 + shift
                    d.ellipse((cx - 33, 235, cx + 33, 301),
                              fill=(221, 176, 139))
                    d.rounded_rectangle((cx - 52, 298, cx + 52, 448),
                                        radius=20,
                                        fill=((61 + p * 38), (92 + p * 15), (154 - p * 17)))
                d.rectangle((120, 190, 195, 405), fill=(92, 72, 54))
                d.ellipse((83, 120, 235, 250), fill=(72, 126, 74))

            # Keep startup generation cheap. Scene structure, blur levels and
            # burst variants are what the culling/dedup tests need; thousands
            # of Python-level texture mutations only steal time from the GUI.
            d = ImageDraw.Draw(img)
            for _ in range(120):
                x = rng.randrange(w); y = rng.randrange(h)
                c = rng.randint(0, 14)
                base = img.getpixel((x, y))
                d.point((x, y), fill=tuple(max(0, min(255, v + c - 7)) for v in base))
            return img

        global_i = 0
        for folder_rel, scene in folders:
            target_dir = demo / Path(folder_rel)
            target_dir.mkdir(parents=True, exist_ok=True)
            for j in range(6):
                kind = kinds[global_i % 3]
                target = target_dir / (
                    f"{global_i + 1:02d}_{scene}_{j + 1:02d}_{kind_zh[kind]}.jpg"
                )
                if not target.exists():
                    # Burst pairs deliberately reuse a scene seed; other folders
                    # vary enough to exercise grouping without becoming identical.
                    seed = (5000 + j // 2) if scene == 'burst' else (1000 + global_i)
                    img = draw_scene(scene, seed, j)
                    if scene == 'burst' and j % 2:
                        img = ImageEnhance.Brightness(img).enhance(1.025)
                    if kind == 'blurry':
                        img = img.filter(ImageFilter.GaussianBlur(radius=5.0))
                    elif kind == 'soft':
                        img = img.filter(ImageFilter.GaussianBlur(radius=1.35))
                    img.save(target, quality=92)
                global_i += 1

        return os.path.realpath(demo)
    except Exception as e:
        logger.warning(f"ensure built-in demo failed: {e}")
        return os.path.realpath(demo) if demo.is_dir() else None


DEMO_ROOT = DATA_ROOT / '内置测试数据'
_demo_prepare_lock = threading.Lock()
_demo_prepare_thread = None


def builtin_demo_status():
    """Cheap status check; never generates demo media on the caller thread."""
    try:
        if not DEMO_ROOT.is_dir():
            return {
                'folder': os.path.realpath(DEMO_ROOT),
                'ready': False,
                'count': 0,
                'folders': 0,
            }
        files = list(DEMO_ROOT.rglob('*.jpg'))
        folders = {p.parent for p in files}
        return {
            'folder': os.path.realpath(DEMO_ROOT),
            'ready': len(files) >= 36 and len(folders) >= 6,
            'count': len(files),
            'folders': len(folders),
        }
    except Exception:
        return {
            'folder': os.path.realpath(DEMO_ROOT),
            'ready': False,
            'count': 0,
            'folders': 0,
        }


def prepare_builtin_demo_async():
    """Prepare demo media without blocking first paint or the WebView thread."""
    global _demo_prepare_thread
    status = builtin_demo_status()
    if status['ready']:
        return status
    with _demo_prepare_lock:
        if _demo_prepare_thread is None or not _demo_prepare_thread.is_alive():
            _demo_prepare_thread = threading.Thread(
                target=ensure_builtin_demo,
                daemon=True,
                name='photocurator-demo-prep',
            )
            _demo_prepare_thread.start()
    status['preparing'] = True
    return status


def prepare_builtin_demo_wait(timeout=20.0):
    status = prepare_builtin_demo_async()
    deadline = time.monotonic() + max(0.1, float(timeout))
    while not status.get('ready') and time.monotonic() < deadline:
        time.sleep(0.08)
        status = builtin_demo_status()
    return status


# DCIM folder-name hints -> camera brand label shown on the SD shortcut.
_BRAND_HINTS = (('CANON', 'Canon'), ('EOS', 'Canon'),
                ('NIKON', 'Nikon'), ('NCD', 'Nikon'), ('NCZ', 'Nikon'),
                ('MSDCF', 'Sony'), ('SONY', 'Sony'),
                ('FUJI', 'Fujifilm'), ('OLYMP', 'Olympus'), ('OMSYS', 'OM System'),
                ('PANA', 'Panasonic'), ('LUMIX', 'Panasonic'),
                ('PENTX', 'Pentax'), ('RICOH', 'Ricoh'), ('LEICA', 'Leica'),
                ('GOPRO', 'GoPro'), ('APPLE', 'iPhone'), ('DJI', 'DJI'))


def _brand_of(name):
    up = name.upper()
    for key, brand in _BRAND_HINTS:
        if key in up:
            return brand
    return None


def _has_images(d):
    """True if the directory directly contains at least one supported image
    (JPEG or RAW). Cheap: stops at the first hit."""
    try:
        for e in os.scandir(d):
            if (e.is_file() and not e.name.startswith('.')
                    and Path(e.name).suffix.lower() in IMG_EXTS):
                return True
    except OSError:
        pass
    return False


def _sd_roots():
    """Mount points that may hold a camera card, per OS.

    Windows: only drives the OS reports as removable or fixed (a USB card
    reader shows up as either) — probing all of A:..Z: blindly can stall on
    empty optical or disconnected network drives.
    macOS: /Volumes/*.  Linux: /media/<user>/*, /run/media/<user>/*,
    /media/* and /mnt/*.
    """
    roots = []
    if os.name == 'nt':
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            mask = k32.GetLogicalDrives()
            for i in range(26):
                if not (mask >> i) & 1:
                    continue
                letter = '%s:\\' % chr(65 + i)
                if k32.GetDriveTypeW(letter) in (2, 3):  # removable, fixed
                    roots.append(Path(letter))
        except Exception:
            import string
            roots = [Path('%s:\\' % L) for L in string.ascii_uppercase]
    else:
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        bases = [Path('/Volumes')]
        if user:
            bases += [Path('/media') / user, Path('/run/media') / user]
        bases += [Path('/media'), Path('/mnt')]
        seen = set()
        for base in bases:
            try:
                if not base.is_dir():
                    continue
                for vol in sorted(base.iterdir()):
                    if vol.name.startswith('.') or not vol.is_dir():
                        continue
                    if str(vol) not in seen:
                        seen.add(str(vol))
                        roots.append(vol)
            except OSError:
                continue
    return roots


def detect_sd_cards(volumes_root=None):
    """Find camera folders on mounted cards for ALL brands.
    Every camera writes to DCIM/<something> (Canon 100CANON, Nikon 100NIKON,
    Sony 100MSDCF, Fuji 100_FUJI, ...). We list each DCIM subfolder that
    actually holds supported images (JPEG or RAW), look one level deeper for
    cameras that nest by date, skip hidden/junk entries, and label the brand.
    Returns [{'path': ..., 'brand': 'Nikon'|None}, ...]."""
    found = []
    if volumes_root is None:
        roots = _sd_roots()
    else:
        _vr = Path(volumes_root)
        roots = sorted(_vr.iterdir()) if _vr.is_dir() else []
    for vol in roots:
        dcim = vol / 'DCIM'
        if not dcim.is_dir():
            continue
        added = False
        try:
            subs = sorted(d for d in dcim.iterdir() if d.is_dir()
                          and not d.name.startswith('.')
                          and d.name.upper() != 'MISC')
        except OSError:
            continue
        for d in subs:
            if _has_images(d):
                found.append({'path': str(d), 'brand': _brand_of(d.name)})
                added = True
                continue
            # One level deeper — some cameras nest by date inside DCIM/<dir>.
            try:
                deeper = sorted(x for x in d.iterdir() if x.is_dir()
                                and not x.name.startswith('.'))
            except OSError:
                deeper = []
            for dd in deeper:
                if _has_images(dd):
                    found.append({'path': str(dd),
                                  'brand': _brand_of(d.name) or _brand_of(dd.name)})
                    added = True
        if not added and _has_images(dcim):
            found.append({'path': str(dcim), 'brand': None})
    return found


def native_folder_dialog(prompt="选择照片文件夹"):
    """Open a native folder picker on Windows/macOS/Linux."""
    try:
        if os.name == 'nt':
            safe_prompt = str(prompt).replace("'", "''")
            ps = (
                "$ErrorActionPreference='Stop';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description='{safe_prompt}';"
                "$d.ShowNewFolderButton=$true;"
                "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
                "{Write-Output $d.SelectedPath}"
            )
            out = subprocess.run(
                ['powershell.exe', '-NoProfile', '-STA', '-Command', ps],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=300
            )
            path = out.stdout.strip()
            if path:
                return path
        elif sys.platform == 'darwin':
            safe_prompt = str(prompt).replace('"', '\\"')
            script = f'POSIX path of (choose folder with prompt "{safe_prompt}")'
            out = subprocess.run(['osascript', '-e', script],
                                 capture_output=True, text=True, timeout=300)
            path = out.stdout.strip()
            if path:
                return path.rstrip('/')
        else:
            for cmd in (
                ['zenity', '--file-selection', '--directory', '--title', str(prompt)],
                ['kdialog', '--getexistingdirectory', '.', '--title', str(prompt)],
            ):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    path = out.stdout.strip()
                    if path:
                        return path.rstrip('/')
                except FileNotFoundError:
                    continue
    except Exception as e:
        logger.warning(f"folder dialog fail: {e}")
    return None

# --------------------------------------------------------------------------- #
#  Sharpness (shared by Cull, and as the dedup quality key)
# --------------------------------------------------------------------------- #
def sharpness_score(gray):
    """Whole-frame contrast-normalized focus measure (used by Dedup's quality
    key). Haze/low contrast does NOT read as blur."""
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge > 1024:
        sc = 1024.0 / long_edge
        gray = cv2.resize(gray, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    img_var = float(gray.astype('float32').var()) + 1e-6
    return lap_var / img_var * 1000.0


def region_sharpness(gray, tiles=8):
    """REGION-based sharpness for Cull: tile the frame, score each tile's
    contrast-normalized focus, and return a high percentile. This measures the
    SHARPEST meaningful region, so a tack-sharp subject against soft bokeh/sky
    is correctly recognized as in-focus instead of being dragged down by the
    smooth background (the key v3.1 accuracy fix)."""
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge > 1024:
        sc = 1024.0 / long_edge
        gray = cv2.resize(gray, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(gray, cv2.CV_64F)          # uint8 -> CV_64F (supported)
    g = gray.astype('float32')
    H, W = g.shape
    th, tw = max(1, H // tiles), max(1, W // tiles)
    scores = []
    for y in range(0, H, th):
        for x in range(0, W, tw):
            tg = g[y:y + th, x:x + tw]
            if tg.size < 64:
                continue
            v = float(tg.var()) + 1e-6
            scores.append(float(lap[y:y + th, x:x + tw].var()) / v * 1000.0)
    if not scores:
        return 0.0
    return float(np.percentile(np.array(scores), 90))


def quick_quality(bgr, gray):
    """Lightweight 0–100 'is this an interesting, detailed frame' proxy used
    only to RESCUE soft-but-well-composed shots from culling. Cheap on purpose;
    the real aesthetic judgement happens in the Rank step."""
    edges = cv2.Canny(gray, 60, 160)
    ed = float(np.count_nonzero(edges)) / edges.size
    contrast = float(gray.std())
    b, gg, r = (bgr[:, :, i].astype('float32') for i in range(3))
    rg, yb = r - gg, 0.5 * (r + gg) - b
    cf = (rg.std() ** 2 + yb.std() ** 2) ** 0.5 + 0.3 * (rg.mean() ** 2 + yb.mean() ** 2) ** 0.5
    q = 0.5 * min(1, ed / 0.12) + 0.25 * min(1, contrast / 70) + 0.25 * min(1, cf / 80)
    return float(max(0.0, min(1.0, q)) * 100)


def classify_sharpness(region_s, q, blur_lo, sharp_hi, q_rescue, rescue_on):
    """Three tiers + quality rescue. Returns (tier, rescued)."""
    if region_s >= sharp_hi:
        return 'sharp', False
    if region_s >= blur_lo:
        return 'soft', bool(rescue_on and q >= q_rescue)   # ★ if well-composed
    # below blur floor — normally Blurry, but rescue a near-miss that's gorgeous
    if rescue_on and q >= q_rescue and region_s >= blur_lo * 0.6:
        return 'soft', True
    return 'blurry', False


@dataclass
class _LiteScore:
    """Minimal score object for the deduplicator (needs path/filename/quality)."""
    path: str
    filename: str
    overall_score: float
    focus: float



# --------------------------------------------------------------------------- #
#  Shared source enumeration
#  Cull and Dedup start together in v1.5.0. Directory enumeration is shared
#  so a large library tree is walked once instead of once per analysis engine.
# --------------------------------------------------------------------------- #
_SCAN_SNAPSHOT_CV = threading.Condition()
_SCAN_SNAPSHOTS = {}


def _shared_list_images(folder, recursive=True, max_age=2.0):
    """Enumerate a source once and persist discovery in crash-safe batches.

    Cull/Dedup still receive one immutable snapshot for deterministic analysis,
    but the Media Catalog is updated while the filesystem walk is in progress.
    If the process or disk disappears mid-scan, the scan session remains
    interrupted and no older media rows are marked missing.
    """
    scan_cfg = state.get('scan') or {}
    output_mode = str(scan_cfg.get('output_mode') or 'source')
    custom_output = ''
    if output_mode == 'custom' and scan_cfg.get('custom_output'):
        custom_output = os.path.normcase(os.path.realpath(
            os.path.expanduser(str(scan_cfg.get('custom_output')))
        ))
    key = (
        os.path.normcase(os.path.realpath(str(folder))),
        bool(recursive),
        output_mode,
        custom_output,
    )
    now = time.time()
    with _SCAN_SNAPSHOT_CV:
        entry = _SCAN_SNAPSHOTS.get(key)
        if entry and entry.get('state') == 'ready' and now - entry.get('at', 0) <= max_age:
            return list(entry.get('paths') or [])
        while entry and entry.get('state') == 'scanning':
            _SCAN_SNAPSHOT_CV.wait(timeout=0.25)
            entry = _SCAN_SNAPSHOTS.get(key)
            if entry and entry.get('state') == 'ready':
                return list(entry.get('paths') or [])
            if not entry or entry.get('state') == 'failed':
                break
        _SCAN_SNAPSHOTS[key] = {
            'state': 'scanning', 'at': time.time(), 'paths': [],
            'discovered': 0,
        }

    scan_session = None
    paths = []
    pending_catalog = []
    try:
        scan_real = os.path.normcase(os.path.realpath(str(folder)))
        demo_real = os.path.normcase(os.path.realpath(str(DATA_ROOT / '内置测试数据')))
        if scan_real != demo_real:
            try:
                scan_session = begin_catalog_scan(INDEX_DB, folder)
            except Exception:
                scan_session = None
                logger.warning("catalog scan session start failed", exc_info=True)

        for p in iter_images(folder, recursive=recursive):
            paths.append(p)
            if scan_session is not None:
                pending_catalog.append(p)
                if len(pending_catalog) >= 512:
                    catalog_scan_batch(INDEX_DB, scan_session, pending_catalog)
                    pending_catalog.clear()
            if len(paths) % 128 == 0:
                with _SCAN_SNAPSHOT_CV:
                    entry = _SCAN_SNAPSHOTS.get(key)
                    if entry and entry.get('state') == 'scanning':
                        entry['discovered'] = len(paths)
                        entry['paths'] = list(paths)

        if scan_session is not None:
            if pending_catalog:
                catalog_scan_batch(INDEX_DB, scan_session, pending_catalog)
                pending_catalog.clear()
            finish_catalog_scan(
                INDEX_DB, scan_session, full_scan=bool(recursive)
            )
            try:
                TASK_MANAGER.enqueue(
                    'build_offline_previews',
                    {'root_id': scan_session['root_id'], 'offset': 0},
                    priority=90,
                    idempotency_key=f"offline_previews:{scan_session['root_id']}:0",
                )
            except Exception:
                logger.debug("offline preview queue skipped", exc_info=True)
    except Exception as exc:
        if scan_session is not None:
            abort_catalog_scan(INDEX_DB, scan_session, str(exc))
        with _SCAN_SNAPSHOT_CV:
            _SCAN_SNAPSHOTS[key] = {
                'state': 'failed', 'at': time.time(), 'error': str(exc),
                'paths': list(paths), 'discovered': len(paths),
            }
            _SCAN_SNAPSHOT_CV.notify_all()
        raise

    paths.sort(key=lambda p: os.path.normcase(str(p)))
    with _SCAN_SNAPSHOT_CV:
        _SCAN_SNAPSHOTS[key] = {
            'state': 'ready', 'at': time.time(), 'paths': list(paths),
            'discovered': len(paths),
        }
        if len(_SCAN_SNAPSHOTS) > 8:
            stale = sorted(
                ((k, v.get('at', 0)) for k, v in _SCAN_SNAPSHOTS.items() if k != key),
                key=lambda kv: kv[1],
            )
            for old_key, _ in stale[:max(0, len(_SCAN_SNAPSHOTS) - 8)]:
                _SCAN_SNAPSHOTS.pop(old_key, None)
        _SCAN_SNAPSHOT_CV.notify_all()
    return list(paths)


def current_scan_snapshot(folder=None):
    """Small status snapshot for UI/diagnostics without filesystem access."""
    with _SCAN_SNAPSHOT_CV:
        if folder:
            norm = os.path.normcase(os.path.realpath(str(folder)))
            matches = [
                v for k, v in _SCAN_SNAPSHOTS.items()
                if k and k[0] == norm
            ]
            entry = max(matches, key=lambda v: v.get('at', 0), default=None)
        else:
            entry = max(_SCAN_SNAPSHOTS.values(),
                        key=lambda v: v.get('at', 0), default=None)
        if not entry:
            return {'state': 'idle', 'discovered': 0}
        return {
            'state': str(entry.get('state') or 'idle'),
            'discovered': int(entry.get('discovered') or len(entry.get('paths') or [])),
            'error': str(entry.get('error') or ''),
        }


# --------------------------------------------------------------------------- #
#  CULL
# --------------------------------------------------------------------------- #
BASE_BLUR, BASE_SHARP = 90.0, 230.0   # region_s baselines when not adaptive


def _badge_for(tier, star):
    if tier == 'sharp':
        return '清晰', 'good'
    if tier == 'soft':
        return ('轻微软 ★' if star else '轻微软'), 'soft'
    return '模糊', 'bad'


def run_cull(folder, strictness, adaptive, rescue_on, recursive=True):
    s = state['cull']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在扫描照片…',
              'photos': [], 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': [],
              'cache_hits': 0, 'folder_status': {}, 'current_folder': '',
              'overrides': {},
              'removed_paths': set(),
              # complete=True only when cull runs to the end; a stopped cull must
              # not feed its partial survivor list into Dedup/Rank.
              'complete': False, 'src_folder': str(folder), 'recursive': bool(recursive)})
    try:
        images = _shared_list_images(folder, recursive=recursive)
        current_paths = {str(p) for p in images}
        cull_cache = _load_cull_metrics_map(images)
        s['overrides'] = _load_review_overrides(images)
        total = len(images) or 1
        items = []   # {name, path, region_s, q}
        cache_buffer = []

        def thresholds():
            if adaptive and items:
                M = float(np.median([it['region_s'] for it in items]))
                # Sharpness is ~log-distributed; keep the blur floor LOW and the
                # Soft band WIDE so slightly-soft (Topaz-recoverable) frames are
                # kept rather than culled. Only clearly-soft frames fall below.
                blur_lo = max(30.0, M * 0.25)
                sharp_hi = max(blur_lo * 1.5, M * 0.70)
            else:
                blur_lo, sharp_hi = BASE_BLUR, BASE_SHARP
            blur_lo *= strictness
            sharp_hi *= strictness
            qs = [it['q'] for it in items]
            q_rescue = float(np.percentile(qs, 70)) if qs else 65.0
            return blur_lo, sharp_hi, q_rescue

        def classify_all():
            blur_lo, sharp_hi, q_rescue = thresholds()
            photos, kept = [], []
            sharp = soft = blurry = 0
            overrides = s.get('overrides', {})
            removed = s.get('removed_paths', set())
            for it in items:
                if it['path'] in removed or not Path(it['path']).is_file():
                    continue
                tier, star = classify_sharpness(it['region_s'], it['q'],
                                                blur_lo, sharp_hi, q_rescue, rescue_on)
                ov = overrides.get(it['path']) or {}
                if ov.get('tier') in ('sharp', 'soft', 'blurry'):
                    tier = ov['tier']
                if tier == 'sharp':
                    sharp += 1
                elif tier == 'soft':
                    soft += 1
                else:
                    blurry += 1
                if tier != 'blurry':
                    kept.append(it['path'])
                badge, bt = _badge_for(tier, star)
                photos.append({'name': it['name'], 'path': it['path'],
                               'thumb': thumb_url(it['path']), 'score': f"{it['region_s']:.0f}",
                               'rel_dir': relative_folder(it['path'], folder),
                               'badge': badge, 'badgeType': bt, 'tier': tier,
                               'raw': is_raw(it['path']), 'fmt': fmt_of(it['path']),
                               'heic': is_heif(it['path']),
                               'kept': tier != 'blurry', 'rejected': tier == 'blurry',
                               # File-action selection is separate from the
                               # classification itself. Blurry frames start
                               # selected, but the user may uncheck any of them
                               # before the explicit Move action.
                               'move_selected': (overrides.get(it['path']) or {}).get('move_selected', tier == 'blurry')})
            # Newest-processed first in the live grid (no scrolling to bottom).
            # Only the display order is reversed; `kept` stays in capture order
            # so Dedup/Rank still receive survivors in their natural sequence.
            s['photos'] = photos[::-1]
            s['sharp'], s['soft'], s['blurry'] = sharp, soft, blurry
            s['sharp_paths'] = kept   # kept = sharp + soft (flows to Dedup/Rank)

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        def _tiers(done):
            k = (s['sharp'] + s['soft'] + s['blurry']) or 1
            return (f"清晰 {s['sharp']} ({s['sharp']/k*100:.0f}%) / "
                    f"轻微软 {s['soft']} ({s['soft']/k*100:.0f}%) / "
                    f"模糊 {s['blurry']} ({s['blurry']/k*100:.0f}%)")

        last_rel = None
        for idx, p in enumerate(images):
            if TASK_MANAGER.foreground_busy():
                time.sleep(0.015)
            rel = relative_folder(str(p), folder)
            if rel != last_rel:
                if last_rel is not None:
                    s['folder_status'][last_rel] = '已完成'
                s['folder_status'][rel] = '扫描中'
                s['current_folder'] = rel
                last_rel = rel
            if s.get('cancel'):
                _save_cull_metrics_batch(cache_buffer)
                cache_buffer.clear()
                classify_all()
                s['status'] = (f"已停止：{idx}/{total} · {_tiers(idx)} · "
                               f"已用时 {_fmt(time.time()-t0)}")
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"模糊筛选 {p.name}（{done}/{total}，{done/total*100:.0f}%）· "
                           f"{_tiers(done)} · 已用时 {_fmt(elapsed)} · 预计剩余 {_fmt(eta)}")
            cached = cull_cache.get(str(p))
            if cached is not None:
                region_s, quality = cached
                s['cache_hits'] = int(s.get('cache_hits', 0)) + 1
            else:
                bgr = imread_bgr(str(p))       # RAW-aware
                if bgr is None:
                    continue
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                region_s = region_sharpness(gray)
                quality = quick_quality(bgr, gray)
                cache_buffer.append((str(p), region_s, quality))
                if len(cache_buffer) >= 64:
                    _save_cull_metrics_batch(cache_buffer)
                    cache_buffer.clear()
            items.append({'name': p.name, 'path': str(p),
                          'region_s': region_s, 'q': quality})
            # Reclassification is for live UI only; final output is still
            # classified once more below. Throttle it so very large folders do
            # not repeatedly rescan the entire processed list every five files.
            now = time.time()
            classify_interval = 1.0 if idx < 1000 else (3.0 if idx < 10000 else 8.0)
            if (idx < 20 and idx % 5 == 0) or idx == len(images) - 1 \
                    or now - s.get('_last_classify_at', 0.0) >= classify_interval:
                classify_all()
                s['_last_classify_at'] = now
        _save_cull_metrics_batch(cache_buffer)
        cache_buffer.clear()
        classify_all()
        if last_rel is not None:
            s['folder_status'][last_rel] = '已完成'
        s['current_folder'] = ''
        s.pop('_last_classify_at', None)

        # Blurry photos are NOT moved automatically — they stay in place so you
        # can review them first, then move them with the "移动模糊照片 → Blurred/"
        # button (mirrors the TOP-N export flow).
        s['progress'] = 100
        s['complete'] = True   # full pass finished — survivors are safe to chain
        # Cull and Dedup run concurrently. If Dedup finished first, its last
        # eligibility sync saw Cull as incomplete. Reconcile once at Cull's
        # commit point so blurry photos cannot remain active duplicate keepers.
        _sync_dedup_with_cull()
        s['status'] = (f"完成 · 用时 {_fmt(time.time()-t0)} · {_tiers(total)}"
                       + (f" · 已复用 {s.get('cache_hits',0)} 张历史分析" if s.get('cache_hits') else "")
                       + (" · 请确认后再移动模糊照片" if s['blurry'] else ""))
        _activity('完成清晰度分析', folder, f"照片 {len(s['photos'])} · 复用 {s.get('cache_hits',0)}")
    except Exception as e:
        logger.error(f"cull failed: {e}", exc_info=True)
        s['status'] = f"发生错误：{e}"
    finally:
        s['running'] = False


def _relocate_for_status(path, now_kept):
    """Manual tier changes are metadata-only.

    The UI lets the user reclassify a photo between Sharp / Soft / Blurry.
    That action must never move or rename the source file. Physical file
    movement happens only through explicit organizer actions such as
    /api/move-blurry or the opt-in duplicate organizer.
    """
    try:
        p = Path(path)
        return str(p) if p.exists() else path
    except Exception:
        return path


def _request_rank_score(path):
    """Keep downstream ranking consistent with manual Cull review."""
    try:
        p = str(path or '')
        if not p or not Path(p).is_file():
            return
        rank = state['rank']
        if any(getattr(sc, 'path', None) == p for sc in rank.get('scores', [])):
            rank['preview_at'] = 0.0
            return
        if rank.get('running'):
            rank.setdefault('pending_paths', set()).add(p)
            return

        def worker():
            try:
                sc = _cached_rank_score(p)
                if sc is None:
                    sc = AdvancedPhotoAnalyzer().analyze_image(p)
                    if sc:
                        _save_rank_score(p, sc)
                if sc and p in set(state['cull'].get('sharp_paths') or []):
                    if not any(getattr(x, 'path', None) == p for x in rank.get('scores', [])):
                        rank.setdefault('scores', []).append(sc)
                    rank['analyzed'] = len(rank.get('scores', []))
                    rank['total'] = max(int(rank.get('total', 0) or 0), rank['analyzed'])
                    rank['preview'] = build_topn()
                    rank['preview_at'] = time.time()
            except Exception:
                logger.debug("incremental rank refresh failed", exc_info=True)

        threading.Thread(target=worker, daemon=True,
                         name='photocurator-rank-refresh').start()
    except Exception:
        logger.debug("rank refresh request failed", exc_info=True)


def _cull_allowed_for_dedup():
    """Current Cull survivors when Cull/Dedup refer to the same scan."""
    cull = state['cull']
    dedup = state['dedup']
    if not cull.get('complete'):
        return None
    if cull.get('src_folder') != dedup.get('src_folder'):
        return None
    if bool(cull.get('recursive', False)) != bool(dedup.get('recursive', False)):
        return None
    return set(cull.get('sharp_paths') or [])


def _sync_dedup_with_cull():
    """Keep duplicate keepers valid after manual clear/blurry reclassification."""
    dedup = state['dedup']
    allowed = _cull_allowed_for_dedup()
    if allowed is None:
        return
    new_groups = []
    for group in dedup.get('groups_data', []):
        members = [m for m in group.get('members', [])
                   if m.get('path') and Path(m.get('path')).is_file()]
        if not members:
            continue
        group['members'] = members
        group['count'] = len(members)
        eligible = [m.get('path') for m in members if m.get('path') in allowed]
        group['active_count'] = len(eligible)
        selected = [p for p in (group.get('selected_paths') or [])
                    if p in eligible]
        if eligible and not selected:
            # Members are quality-sorted; promote the best current Cull survivor.
            selected = [eligible[0]]
        group['selected_paths'] = selected
        selected_set = set(selected)
        for m in members:
            m['selected'] = m.get('path') in selected_set
        new_groups.append(group)
    dedup['groups_data'] = new_groups
    # A group only needs user review when at least two currently eligible
    # Cull survivors remain. Blurry members can stay in metadata without
    # polluting the duplicate-review count.
    dedup['photos'] = [g for g in new_groups if g.get('active_count', g.get('count', 0)) > 1]
    dedup['groups'] = len(dedup['photos'])
    source_singletons = dedup.get('all_singleton_paths')
    if source_singletons is None:
        source_singletons = dedup.get('singleton_paths') or []
    dedup['singleton_paths'] = [
        p for p in source_singletons
        if p in allowed and Path(p).is_file()
    ]
    dedup['kept_paths'] = list(dedup['singleton_paths']) + [
        p for g in new_groups for p in (g.get('selected_paths') or [])
    ]
    state['rank']['preview_at'] = 0.0


# --------------------------------------------------------------------------- #
#  DEDUP
# --------------------------------------------------------------------------- #
def run_dedup(folder, threshold, ftype='all', pair='both',
              recursive=True, compare_scope='folder'):
    s = state['dedup']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在准备…',
              'photos': [], 'groups': 0, 'kept_paths': [], 'groups_data': [],
              'singleton_paths': [], 'all_singleton_paths': [], 'seen_paths': set(),
              'applied': False, 'complete': False, 'src_folder': str(folder),
              'recursive': bool(recursive), 'compare_scope': compare_scope})
    try:
        cull = state['cull']
        chain_ok = (cull.get('complete')
                    and cull.get('src_folder') == str(folder)
                    and bool(cull.get('recursive', False)) == bool(recursive))
        # Build similarity metadata for the whole source set, but Cull remains
        # the eligibility gate for what the user can keep/process. This lets a
        # photo manually rescued from Blurry later enter an already-built
        # duplicate group without forcing a complete re-scan.
        paths = _shared_list_images(folder, recursive=recursive)
        if chain_ok:
            logger.info(f"Dedup: indexing {len(paths)} source photos; "
                        f"{len(cull.get('sharp_paths') or [])} currently eligible after Cull")
        else:
            logger.info(f"Dedup: scanning {'recursive tree' if recursive else 'folder'} "
                        f"({len(paths)} images)")

        if ftype and ftype != 'all':
            before = len(paths)
            paths = filter_ftype(paths, ftype)
            lbl = ftype_label(ftype)
            s['status'] = f"仅 {lbl} · {len(paths)}/{before} 张照片进入相似去重"

        paths, npairs = collapse_raw_jpg_pairs(paths, pair)
        if npairs:
            s['status'] = f"已合并 {npairs} 组 RAW+JPG 同帧照片 · 保留 {pair.upper()}"
        if not paths:
            s['complete'] = True
            s['progress'] = 100
            s['groups'] = 0
            s['kept_paths'] = []
            s['groups_data'] = []
            s['photos'] = []
            s['singleton_paths'] = []
            s['all_singleton_paths'] = []
            s['seen_paths'] = set()
            s['status'] = ('当前范围没有可建立相似索引的照片' if ftype == 'all'
                           else f'没有可建立相似索引的 {ftype_label(ftype)} 照片')
            return

        # Local mode compares only within each leaf/source folder. Global mode
        # intentionally allows one similarity group to contain different folders.
        if compare_scope == 'global':
            batches = [('全局', paths)]
        else:
            by_parent = {}
            for p in paths:
                by_parent.setdefault(os.path.normcase(str(p.parent.resolve())), []).append(p)
            batches = [(relative_folder(ps[0], folder), ps)
                       for _, ps in sorted(by_parent.items(), key=lambda kv: kv[0])]

        total = len(paths)
        cull_metric_cache = _load_cull_metrics_map(paths)
        processed = 0
        all_groups = []
        singleton_paths = []
        seen_paths = {str(p) for p in paths}
        kept = []
        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, ss = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{ss:02d}s" if m else f"{ss}s")

        for batch_label, batch_paths in batches:
            if s.get('cancel'):
                break
            dd = FastBatchDeduplicator(threshold=threshold)
            try:
                cache_seed = os.path.realpath(folder) + '|' + compare_scope + '|' + batch_label
                cache_key = hashlib.md5(cache_seed.encode('utf-8')).hexdigest()
                dd.enable_disk_cache(DEDUP_CACHE_DIR / f'dedup_v{DEDUP_SIGNATURE_VERSION}_{cache_key}.json')
            except Exception:
                pass
            dd.reset()

            for p in batch_paths:
                if TASK_MANAGER.foreground_busy():
                    time.sleep(0.015)
                if s.get('cancel'):
                    break
                processed += 1
                s['progress'] = int(processed / total * 100)
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (total - processed) / rate if rate > 0 else 0
                scope_txt = '全局对比' if compare_scope == 'global' else f'文件夹：{batch_label}'
                s['status'] = (f"相似去重 · {scope_txt} · {p.name}（{processed}/{total}）· "
                               f"已用时 {_fmt(elapsed)} · 预计剩余 {_fmt(eta)}")
                cached_metrics = cull_metric_cache.get(str(p))
                if cached_metrics is not None:
                    sharp = float(cached_metrics[0])
                else:
                    gray = imread_gray(str(p))
                    sharp = sharpness_score(gray) if gray is not None else 0.0
                dd.add_photo(_LiteScore(str(p), p.name, sharp, sharp))

            dd.save_disk_cache()
            for cluster in dd.clusters:
                members = sorted(cluster.members,
                                 key=lambda x: float(getattr(x, 'overall_score', 0.0) or 0.0),
                                 reverse=True)
                selected = cluster.rep.path
                member_rows = [{
                    'name': getattr(m, 'filename', Path(m.path).name),
                    'path': m.path,
                    'thumb': thumb_url(m.path),
                    'score': round(float(getattr(m, 'overall_score', 0.0) or 0.0), 1),
                    'selected': m.path == selected,
                    'rel_dir': relative_folder(m.path, folder),
                } for m in members]
                rels = sorted({m['rel_dir'] for m in member_rows})
                if len(member_rows) > 1:
                    group_key = _similarity_group_key(
                        folder, compare_scope, [m['path'] for m in member_rows]
                    )
                    member_rows = _merge_historical_group_members(group_key, member_rows)
                    persisted = _similarity_group_state(
                        group_key, folder,
                        [m.get('original_path') or m.get('path') for m in member_rows]
                    )
                    _persist_similarity_group_members(group_key, member_rows)
                    all_groups.append({
                        'group_id': len(all_groups),
                        'group_key': group_key,
                        'count': len(member_rows),
                        'active_count': len(member_rows),
                        'deleted_count': 0,
                        'status': persisted.get('status', 'pending'),
                        'revision': persisted.get('revision', 1),
                        'ready': True,
                        'selected_paths': [selected],
                        'folder_rel': (rels[0] if len(rels) == 1 else '跨文件夹重复'),
                        'members': member_rows,
                    })
                else:
                    singleton_paths.append(selected)
                kept.append(selected)

            # Folder-local batches are final as soon as that folder finishes.
            # Publish them immediately so the user can review earlier folders
            # while later folders are still being analyzed.
            s['groups_data'] = list(all_groups)
            s['photos'] = [g for g in all_groups if g.get('count', 0) > 1]
            s['groups'] = len(s['photos'])
            s['all_singleton_paths'] = list(singleton_paths)
            allowed_now = _cull_allowed_for_dedup()
            s['singleton_paths'] = [
                p for p in singleton_paths
                if allowed_now is None or p in allowed_now
            ]
            s['kept_paths'] = list(s['singleton_paths']) + [
                p for g in all_groups for p in (g.get('selected_paths') or [])
            ]
            _sync_dedup_with_cull()

        s['groups_data'] = all_groups
        s['photos'] = all_groups
        s['groups'] = len(all_groups)
        s['all_singleton_paths'] = list(singleton_paths)
        allowed_now = _cull_allowed_for_dedup()
        s['singleton_paths'] = [
            p for p in singleton_paths
            if allowed_now is None or p in allowed_now
        ]
        s['seen_paths'] = seen_paths
        # Recompute from the live group objects so manual selections made while
        # later folders were still scanning are never overwritten by defaults.
        s['kept_paths'] = list(s['singleton_paths']) + [
            p for g in all_groups for p in (g.get('selected_paths') or [])
        ]
        _sync_dedup_with_cull()

        if s.get('cancel'):
            s['status'] = (f"已停止 · 已扫描 {processed}/{total} 张 · "
                           f"已用时 {_fmt(time.time()-t0)}")
            return

        s['complete'] = True
        s['progress'] = 100
        dup_groups = len(s['photos'])
        scope_txt = '整个所选范围全局对比' if compare_scope == 'global' else '各子文件夹独立对比'
        if dup_groups:
            s['status'] = (f"筛选完成 · {scope_txt} · 发现 {dup_groups} 组相似照片 · "
                           f"已默认推荐每组最佳照片，请对比后确认处理")
        else:
            s['status'] = f"筛选完成 · {scope_txt} · 未发现需要处理的相似组"
    except Exception as e:
        logger.error(f"dedup failed: {e}", exc_info=True)
        s['status'] = f"发生错误：{e}"
    finally:
        s['running'] = False


# --------------------------------------------------------------------------- #
#  RANK
# --------------------------------------------------------------------------- #
def weighted_overall(score, weights):
    wsum = sum(max(0, v) for v in weights.values()) or 1.0
    return sum(max(0, weights.get(k, 0)) * getattr(score, k, 0.0)
               for k in CATEGORIES) / wsum


def build_topn(weights=None, topn=None):
    weights = weights or state['weights']
    topn = topn or state['topn']
    excluded = state['excluded']
    rank_state = state['rank']
    folder = str(rank_state.get('src_folder') or state.get('folder') or '')
    recursive = bool(rank_state.get('recursive', False))

    allowed = None
    cull = state['cull']
    if (cull.get('complete') and cull.get('src_folder') == folder
            and bool(cull.get('recursive', False)) == recursive):
        allowed = set(cull.get('sharp_paths') or [])

    dedup = state['dedup']
    if (dedup.get('complete') and dedup.get('src_folder') == folder
            and bool(dedup.get('recursive', False)) == recursive):
        dedup_allowed = set(dedup.get('kept_paths') or [])
        dedup_seen = set(dedup.get('seen_paths') or [])
        if allowed is None:
            allowed = {getattr(sc, 'path', '') for sc in rank_state.get('scores', [])
                       if getattr(sc, 'path', '') not in dedup_seen
                       or getattr(sc, 'path', '') in dedup_allowed}
        else:
            allowed = {p for p in allowed if p not in dedup_seen or p in dedup_allowed}

    scores = []
    for score in rank_state.get('scores', []):
        p = score.path
        if p in excluded:
            continue
        if allowed is not None and p not in allowed:
            continue
        scores.append(score)
    candidates = sorted(scores, key=lambda s: weighted_overall(s, weights), reverse=True)
    ranked = []
    for score in candidates:
        if not Path(score.path).is_file():
            continue
        ranked.append(score)
        if len(ranked) >= topn:
            break
    out = []
    for rank, s in enumerate(ranked, 1):
        ov = weighted_overall(s, weights)
        out.append({
            'name': s.filename, 'path': s.path, 'thumb': thumb_url(s.path),
            'rel_dir': relative_folder(s.path, state.get('folder') or Path(s.path).parent),
            'rank': rank, 'score': f"{ov:.1f}",
            'phonebg': s.path in state['phone_bg'],
            'scores': {'composition': round(s.composition), 'technical': round(s.technical),
                       'sharpness': round(s.sharpness), 'color': round(s.color),
                       'aesthetic': round(s.aesthetic)},
            'detail': {'三分法构图': round(s.rule_of_thirds), '水平线': round(s.horizon_level),
                       '画面平衡': round(s.balance), '曝光': round(s.exposure),
                       '动态范围': round(s.dynamic_range), '影调范围': round(s.tonal),
                       '白平衡': round(s.white_balance), '噪点控制': round(s.noise),
                       '色彩丰富度': round(s.colorfulness), '色彩协调': round(s.harmony)},
        })
    return out


def run_rank(folder, ftype='all', pair='both', recursive=True):
    s = state['rank']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在准备…',
              'scores': [], 'total': 0, 'analyzed': 0, 'preview': [], 'preview_at': 0.0,
              'cache_hits': 0, 'pending_paths': set(), 'complete': False,
              'src_folder': str(folder), 'recursive': bool(recursive)})
    state['excluded'] = set()
    try:
        # Rank every Cull survivor so later manual changes inside similarity
        # groups can update recommendations instantly without recomputing scores.
        cull = state['cull']
        if (cull.get('complete') and cull.get('src_folder') == str(folder)
                and bool(cull.get('recursive', False)) == bool(recursive)):
            paths = [Path(p) for p in cull['sharp_paths'] if Path(p).is_file()]
            chain = '清晰度复核后照片'
        else:
            paths = list_images(folder, recursive=recursive)
            chain = '全部照片（含子文件夹）' if recursive else '当前文件夹全部照片'
        # Honor the Cull file-type filter (RAW / JPG / specific format).
        if ftype and ftype != 'all':
            before = len(paths)
            paths = filter_ftype(paths, ftype)
            lbl = ftype_label(ftype)
            chain += f' · 仅 {lbl}（{len(paths)}/{before}）'
            logger.info(f"Rank: {lbl}-only filter — "
                        f"{len(paths)} of {before} photos continue")
        # Collapse RAW+JPG pairs too (covers ranking straight from Cull/folder).
        paths, npairs = collapse_raw_jpg_pairs(paths, pair)
        if npairs:
            chain += f' · {npairs} 组 RAW+JPG → {pair.upper()}'
            logger.info(f"Rank: {npairs} RAW+JPG pairs collapsed (kept {pair.upper()})")
        if not paths:
            s['progress'] = 100
            s['complete'] = True
            s['status'] = ('清晰度复核后没有需要评分的照片' if ftype == 'all'
                           else f'没有可评分的 {ftype_label(ftype)} 照片')
            return
        total = len(paths)
        s['total'] = total
        analyzer = AdvancedPhotoAnalyzer()
        rank_cache_map = _load_rank_scores_map(paths)
        rank_cache_buffer = []

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        for idx, p in enumerate(paths):
            if s.get('cancel'):
                _save_rank_scores_batch(rank_cache_buffer)
                rank_cache_buffer.clear()
                s['status'] = (f"已停止：{idx}/{total} · 已评分 {len(s['scores'])} 张 · "
                               f"已用时 {_fmt(time.time()-t0)}")
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"智能优选 {p.name}（{done}/{total}，{done/total*100:.0f}%）· "
                           f"已用时 {_fmt(elapsed)} · 预计剩余 {_fmt(eta)}")
            sc = rank_cache_map.get(str(p))
            if sc is not None:
                s['cache_hits'] = int(s.get('cache_hits', 0)) + 1
            else:
                sc = analyzer.analyze_image(str(p))
                if sc:
                    rank_cache_buffer.append((str(p), sc))
                    if len(rank_cache_buffer) >= 32:
                        _save_rank_scores_batch(rank_cache_buffer)
                        rank_cache_buffer.clear()
            if sc:
                s['scores'].append(sc)
            s['analyzed'] = len(s['scores'])
        _save_rank_scores_batch(rank_cache_buffer)
        rank_cache_buffer.clear()

        # Manual Cull review may add newly-kept photos while ranking is running.
        # Score those additions before declaring the task complete.
        while not s.get('cancel'):
            pending = list(s.get('pending_paths') or set())
            if not pending:
                break
            s['pending_paths'].clear()
            known = {getattr(x, 'path', None) for x in s['scores']}
            allowed_now = set(state['cull'].get('sharp_paths') or [])
            for raw in pending:
                if raw in known or raw not in allowed_now or not Path(raw).is_file():
                    continue
                sc = _cached_rank_score(raw)
                if sc is None:
                    sc = analyzer.analyze_image(raw)
                    if sc:
                        _save_rank_score(raw, sc)
                if sc:
                    s['scores'].append(sc)
                    known.add(raw)
            s['analyzed'] = len(s['scores'])

        s['progress'] = 100
        s['status'] = (f"完成 · 已评分 {len(s['scores'])} 张 · 来源：{chain} · 用时 {_fmt(time.time()-t0)}"
                       + (f" · 已复用 {s.get('cache_hits',0)} 张历史评分" if s.get('cache_hits') else ""))
        s['complete'] = True
        _activity('完成精选评分', folder, f"评分 {len(s['scores'])} · 复用 {s.get('cache_hits',0)}")
    except Exception as e:
        logger.error(f"rank failed: {e}", exc_info=True)
        s['status'] = f"发生错误：{e}"
    finally:
        s['running'] = False
        # A manual Cull change can land between the final pending-path check and
        # task completion. Once running=False, re-dispatch anything left so no
        # user-approved photo is silently missed by the final recommendation.
        late_pending = list(s.get('pending_paths') or set())
        s.setdefault('pending_paths', set()).clear()
        for raw in late_pending:
            _request_rank_score(raw)


# --------------------------------------------------------------------------- #
#  HTML
# --------------------------------------------------------------------------- #
HTML = r'''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>照片筛选 · PhotoCurator 中文版</title>
<style>
  :root{--bg:#f4f6fb;--panel:#fff;--panel2:#eef1f7;--text:#1c2330;--muted:#6b7280;
        --accent:#2563eb;--good:#16a34a;--warn:#d97706;--bad:#dc2626;--border:#dde3ec;--shadow:rgba(20,40,80,.10);color-scheme:light}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
  .top{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:54px;padding:8px 12px 8px 14px;background:linear-gradient(90deg,#1e40af,#2563eb);color:#fff}
  .brand{display:flex;align-items:center;gap:10px;min-width:0}
  .brand-mark{width:32px;height:32px;display:grid;place-items:center;flex:0 0 auto;border-radius:10px;
    background:linear-gradient(145deg,rgba(255,255,255,.34),rgba(255,255,255,.12));border:1px solid rgba(255,255,255,.42);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.36),0 6px 16px rgba(19,47,119,.18);font-size:18px;font-weight:800}
  .brand-copy{display:flex;flex-direction:column;min-width:0;line-height:1.12}
  .brand-copy b{font-size:15px;letter-spacing:.01em}.brand-copy small{margin-top:3px;font-weight:500;opacity:.72;font-size:10px;white-space:nowrap}
  .steps{display:flex;gap:8px;min-width:0;overflow-x:auto;scrollbar-width:none}.steps::-webkit-scrollbar{display:none}
  .step{padding:7px 16px;background:rgba(255,255,255,.18);border:2px solid transparent;border-radius:9px;cursor:pointer;font-weight:600;font-size:13px;color:#fff}
  .step:hover{background:rgba(255,255,255,.3)} .step.active{background:#fff;color:var(--accent)}
  .title-action{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.18);color:#fff;height:32px;padding:0 10px;border-radius:9px;cursor:pointer;font-size:11px;font-weight:700}
  .title-action:hover{background:rgba(255,255,255,.24)}
  .viewport{display:flex;height:calc(100vh - 54px);height:calc(100dvh - 54px);min-height:0}
  .sidebar{width:clamp(260px,22vw,320px);flex:0 0 clamp(260px,22vw,320px);background:var(--panel);border-right:1px solid var(--border);padding:16px;overflow:hidden;display:flex;flex-direction:column}
  /* Scrollable region holds folder + settings + stats; the action footer below
     is pinned so Start / Export / 移动模糊照片 stay above the fold. */
  .sidebar-scroll{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding-right:4px}
  .sidebar-actions{flex:0 0 auto;display:flex;flex-direction:column;gap:8px;padding-top:10px;margin-top:6px;border-top:1px solid var(--border)}
  #shortcuts{display:flex;flex-direction:column}
  .folder-row{display:flex;gap:8px;align-items:stretch}
  .folder-row input{flex:1;min-width:0}
  .folder-row .btn{width:auto;flex:0 0 auto;white-space:nowrap;padding:11px 16px}
  .main{flex:1;min-width:0;min-height:0;overflow-y:auto;padding:14px 18px;overscroll-behavior:contain}
  .sidebar-title{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
  input[type=text],input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel2);color:var(--text);-webkit-text-fill-color:var(--text)}
  input[type=text]::placeholder,input[type=number]::placeholder{color:var(--muted);-webkit-text-fill-color:var(--muted)}
  .btn{width:100%;padding:11px;border:none;border-radius:9px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;font-size:14px}
  .btn:hover{filter:brightness(1.07)}
  .btn.stopping{background:var(--bad)}
  .btn-ghost{width:100%;padding:9px;border:1px solid var(--border);border-radius:9px;background:var(--panel);color:var(--text);cursor:pointer;font-weight:500}
  /* Muted Start when a contextual primary action (e.g. 移动模糊照片) takes over */
  .btn.secondary{background:var(--panel2);color:var(--muted);border:1px solid var(--border)}
  .btn.secondary:hover{filter:none;border-color:var(--accent)}
  /* Emphasised contextual call-to-action */
  .btn.cta{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 30%,transparent)}
  .shortcut{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel);cursor:pointer;font-size:13px;margin-top:4px;width:100%;text-align:left}
  .shortcut:hover{border-color:var(--accent)}
  .tag{font-size:9px;font-weight:700;padding:2px 5px;border-radius:4px;color:#fff}
  .tag.sd{background:var(--good)} .tag.recent{background:var(--warn)}
  .wgroup{margin-bottom:6px}
  .wgroup label{display:flex;justify-content:space-between;font-size:12px;font-weight:600;margin-bottom:3px}
  .wgroup label b{color:var(--accent)} .wgroup input[type=range]{width:100%}
  .slider-value{font-size:11px;color:var(--muted)}
  .check{display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;padding:8px;background:var(--panel2);border-radius:8px}
  .stat-row{display:flex;justify-content:space-between;font-size:13px;padding:3px 0}.stat-row .v{font-weight:700;color:var(--accent)}
  .panel-box{background:var(--panel2);border-radius:10px;padding:12px}
  .progress-wrap{margin-bottom:12px;display:none}
  .progress-bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
  .progress-fill{height:100%;width:0;background:var(--accent);transition:width .25s}
  .progress-text{font-size:12px;color:var(--muted);margin-top:5px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .progress-line{display:flex;align-items:center;justify-content:space-between;gap:10px}
  .new-results{flex:0 0 auto;margin-top:5px;background:color-mix(in srgb,var(--accent) 9%,var(--panel))}
  .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(clamp(138px,12vw,175px),1fr));gap:12px;align-items:start}
  .empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:60px 0}.empty .icon{font-size:44px}
  .empty .title{font-size:19px;font-weight:700;color:var(--text);margin:12px 0 4px}
  .empty p{margin:0 0 16px;font-size:14px}
  .empty .lines{display:inline-flex;flex-direction:column;gap:9px;text-align:left;font-size:14px;line-height:1.4}
  .empty .lines b{color:var(--accent)}
  .photo-card{position:relative;border-radius:9px;overflow:hidden;background:var(--panel2);border:2px solid transparent;cursor:pointer}
  .photo-card:hover{border-color:var(--accent);box-shadow:0 4px 12px var(--shadow)}
  .photo-card.kept{border-color:var(--good)} .photo-card.rejected{opacity:.5}
  .photo-card.soft{border-color:var(--warn)}
  .badge.soft{background:var(--warn)}
  .filter-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .pager{display:flex;align-items:center;gap:14px;justify-content:center;margin:4px 0 14px;font-size:13px}
  .pager button{padding:6px 14px;border:1px solid var(--border);border-radius:6px;background:var(--panel);
                cursor:pointer;font-weight:600}
  .pager button:disabled{opacity:.4;cursor:not-allowed}
  .chip{padding:5px 12px;border:1px solid var(--border);border-radius:16px;background:var(--panel);color:var(--text);cursor:pointer;font-size:12px;font-weight:600}
  .chip:hover{border-color:var(--accent)} .chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .chip-sep{width:1px;height:18px;background:var(--border);margin:0 4px;align-self:center}
  .ftype{flex:none;margin-left:4px;padding:1px 5px;border-radius:3px;background:#8a2be2;color:#fff;font-size:9px;font-weight:700;letter-spacing:.5px}
  .ftype.jpg{background:#64748b}
  .ftype.heic{background:#0d9488}
  .wgroup select{width:100%;padding:7px 9px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);font-size:13px}
  .badge-tier{border:none;cursor:pointer;font:inherit;font-size:10px;font-weight:700}
  .badge-tier:hover{filter:brightness(1.15);box-shadow:0 0 0 2px rgba(0,0,0,.25)}
  .rank-num{position:absolute;top:6px;left:6px;background:var(--accent);color:#fff;min-width:24px;height:24px;padding:0 6px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;z-index:5}
  .badge{position:absolute;top:6px;right:6px;color:#fff;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;z-index:5}
  .badge.good{background:var(--good)} .badge.bad{background:var(--bad)}
  .move-select{position:absolute;top:6px;left:6px;z-index:8;width:25px;height:25px;border:2px solid #fff;border-radius:6px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;padding:0;font-size:15px;font-weight:900;cursor:pointer;box-shadow:0 1px 6px rgba(0,0,0,.28)}
  .move-select:hover{transform:scale(1.06)}
  .move-select.off{background:rgba(255,255,255,.82);color:#667085;border-color:#94a3b8;box-shadow:0 1px 5px rgba(0,0,0,.16)}
  .move-select.off:hover{color:var(--accent);border-color:var(--accent);background:#fff}
  .move-summary{display:inline-flex;align-items:center;gap:4px;padding:5px 8px;border-radius:8px;background:var(--panel2);font-size:11px;color:var(--muted)}
  .move-summary b{color:var(--accent);font-size:12px}
  .chip.move-bulk{padding-left:9px;padding-right:9px}
  .status-toggle{position:absolute;bottom:34px;right:6px;z-index:6;border:none;border-radius:5px;padding:4px 8px;font-size:10px;font-weight:700;cursor:pointer;background:rgba(0,0,0,.62);color:#fff}
  .status-toggle:hover{background:rgba(0,0,0,.85)}
  .photo-card.pbg{border-color:#e8632a!important;box-shadow:0 0 0 2px #e8632a}
  .pbg-toggle{position:absolute;top:6px;right:6px;z-index:7;border:none;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;background:rgba(0,0,0,.55);color:#fff;backdrop-filter:blur(2px);transition:background .15s,transform .15s}
  .pbg-toggle:hover{background:rgba(0,0,0,.8);transform:scale(1.08)}
  .pbg-toggle.on{background:#e8632a;box-shadow:0 1px 5px rgba(232,99,42,.6)}
  .lb-btn.pbg{background:rgba(232,99,42,.85)} .lb-btn.pbg:hover{background:#e8632a} .lb-btn.pbg.on{background:#e8632a}
  .photo-img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block}
  .photo-info{padding:6px 8px}.pi-row{display:flex;align-items:center;gap:6px}
  .photo-name{flex:1;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .photo-score{font-size:15px;font-weight:700;color:var(--accent)}
  .remove-btn{flex:0 0 auto;border:none;background:rgba(220,38,38,.12);color:#dc2626;border-radius:5px;font-size:10px;font-weight:700;padding:2px 7px;cursor:pointer;line-height:1.5}
  .remove-btn:hover{background:#dc2626;color:#fff}
  .delete-btn{flex:0 0 auto;border:none;background:rgba(220,38,38,.12);color:#dc2626;border-radius:5px;font-size:10px;font-weight:700;padding:2px 7px;cursor:pointer;line-height:1.5}
  .delete-btn:hover{background:#dc2626;color:#fff}
  .photo-card.pending-delete{border-color:#f59e0b!important;background:color-mix(in srgb,#f59e0b 7%,var(--panel2))}
  .photo-card.trashed{border-color:#ef4444!important;background:color-mix(in srgb,#ef4444 6%,var(--panel2))}
  .photo-card.pending-delete .photo-img,.photo-card.trashed .photo-img{filter:saturate(.82) brightness(.92)}
  .lifecycle-badge{position:absolute;top:7px;right:7px;z-index:3;padding:4px 7px;border-radius:7px;font-size:10px;font-weight:800;color:#fff;background:#d97706}
  .lifecycle-badge.trash{background:#dc2626}
  .dedup-choice.pending-delete{border-color:#f59e0b;background:color-mix(in srgb,#f59e0b 7%,var(--panel2))}
  .dedup-choice.trashed{border-color:#ef4444;background:color-mix(in srgb,#ef4444 6%,var(--panel2))}
  .dedup-choice.trashed img,.dedup-choice.pending-delete img{filter:saturate(.82) brightness(.92)}
  .dedup-recommend.state-trash{background:#dc2626}
  .dedup-recommend.state-pending{background:#d97706}
  .dedup-recommend.state-neutral{background:rgba(17,24,39,.62)}
  .dedup-more{margin-top:7px;font-size:11px}
  .dedup-more summary{cursor:pointer;color:var(--muted);user-select:none}
  .dedup-more .dedup-quick{margin-top:7px}
  .group-status{display:inline-flex;align-items:center;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:700}
  .group-status.pending{background:#eef2ff;color:#4f46e5}
  .group-status.reviewed{background:#dcfce7;color:#15803d}
  .group-status.updated{background:#fef3c7;color:#b45309}
  .pc-modal-backdrop{position:fixed;inset:0;z-index:520;display:none;align-items:center;justify-content:center;
    background:rgba(24,32,52,.20);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
  .pc-modal-backdrop.open{display:flex}
  .pc-modal{width:min(440px,calc(100vw - 32px));padding:20px;border-radius:22px;
    background:rgba(255,255,255,.86);border:1px solid rgba(255,255,255,.92);
    box-shadow:0 28px 70px rgba(46,61,110,.24);backdrop-filter:blur(28px) saturate(145%)}
  .pc-modal h3{margin:0 0 8px;font-size:18px}.pc-modal p{margin:0;color:var(--muted);font-size:13px;line-height:1.65}
  .pc-modal-file{margin-top:10px;padding:9px 11px;border-radius:10px;background:rgba(255,255,255,.58);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
  .pc-modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px;flex-wrap:wrap}
  .pc-modal-actions button{border:1px solid var(--border);border-radius:10px;padding:9px 13px;background:rgba(255,255,255,.74);
    color:var(--text);font-weight:700;cursor:pointer}
  .pc-modal-actions .danger{color:#b91c1c;border-color:rgba(185,28,28,.22);background:rgba(254,226,226,.72)}
  .pc-modal-actions .primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .pc-modal-hint{margin-top:10px!important;font-size:11px!important}
  .trash-restore-btn{flex:0 0 auto;border:none;background:rgba(37,99,235,.12);color:#2563eb;border-radius:5px;font-size:10px;font-weight:700;padding:2px 7px;cursor:pointer;line-height:1.5}
  .trash-restore-btn:hover{background:#2563eb;color:#fff}
  .trash-purge-btn{flex:0 0 auto;border:none;background:rgba(185,28,28,.12);color:#b91c1c;border-radius:5px;font-size:10px;font-weight:700;padding:2px 7px;cursor:pointer;line-height:1.5}
  .trash-purge-btn:hover{background:#b91c1c;color:#fff}
  .lb-btn.delete{background:rgba(185,28,28,.88)} .lb-btn.delete:hover{background:#b91c1c}
  #removedBox{font-size:12px;color:var(--muted);margin-top:2px}#removedBox a{color:var(--accent);cursor:pointer;text-decoration:underline}
  /* lightbox */
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:200;display:none;flex-direction:column;align-items:center;justify-content:center}
  .lightbox.open{display:flex}
  .lb-img{max-width:calc(100vw - 360px);max-height:calc(100dvh - 110px);object-fit:contain;border-radius:6px}
  .lb-bar{position:absolute;top:0;left:0;right:clamp(300px,25vw,360px);display:flex;justify-content:space-between;align-items:center;padding:14px 22px;color:#fff;z-index:20;background:linear-gradient(180deg,rgba(0,0,0,.6),transparent)}
  .lb-close{background:rgba(255,255,255,.2);border:none;color:#fff;font-size:22px;width:42px;height:42px;border-radius:50%;cursor:pointer;z-index:30}
  .lb-close:hover{background:rgba(255,255,255,.4)}
  .lb-actions{display:flex;gap:10px;align-items:center;z-index:30}
  .lb-btn{border:none;border-radius:9px;height:38px;padding:0 14px;font-size:13px;line-height:1;font-weight:700;cursor:pointer;color:#fff;display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box}
  .lb-btn.remove{background:rgba(220,38,38,.85)} .lb-btn.remove:hover{background:#dc2626}
  .lb-btn.restore{background:rgba(255,255,255,.22)} .lb-btn.restore:hover{background:rgba(255,255,255,.4)}
  .lb-btn.toggle{background:rgba(37,99,235,.85)} .lb-btn.toggle:hover{background:#2563eb}
  .lb-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);border:none;color:#fff;font-size:30px;width:54px;height:54px;border-radius:50%;cursor:pointer;z-index:20}
  .lb-prev{left:18px}.lb-next{right:clamp(298px,26vw,378px)}
  .lb-side{position:absolute;right:0;top:0;bottom:0;width:clamp(280px,24vw,340px);background:rgba(15,20,28,.95);color:#fff;padding:22px 20px 28px;overflow-y:auto;z-index:10}
  .lb-side h3{margin:16px 0 6px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;opacity:.65;display:flex;justify-content:space-between;align-items:baseline}
  .lb-side h3 span{font-size:12px;opacity:.9;color:#93c5fd}
  .bar{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:11px;cursor:help}
  .bar .lab{flex:0 0 104px;opacity:.85}
  .bar .track{flex:1;display:block;height:8px;background:rgba(255,255,255,.14);border-radius:4px;overflow:hidden}
  .bar .fill{display:block;height:100%;border-radius:4px;transition:width .25s}
  .bar .num{flex:0 0 26px;text-align:right;font-weight:700}.bar.cat .lab{font-weight:700;opacity:1}
  .exrow{display:grid;grid-template-columns:74px 1fr;gap:10px;align-items:baseline;margin:7px 0;font-size:12px}
  .exrow .lab{opacity:.55;font-size:11px}
  .exrow .val{text-align:right;line-height:1.35;overflow-wrap:break-word}
  .exrow .val a{color:#60a5fa;text-decoration:none}.exrow .val a:hover{text-decoration:underline}
  .exmap{display:block;margin:8px 0 4px}
  .exmap .mapslot{position:relative;width:256px;max-width:100%;height:256px;overflow:hidden;border-radius:8px;border:1px solid rgba(255,255,255,.12);background:var(--panel2)}
  .exmap .mappin{position:absolute;left:50%;top:50%;width:12px;height:12px;border-radius:50%;background:#ef4444;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.6);transform:translate(-50%,-50%);pointer-events:none;z-index:2}
  .exmap .cred{display:block;font-size:9px;opacity:.45;margin-top:3px}
  .exmap .cred a{color:inherit}
  .maplibregl-ctrl-attrib,.maplibregl-ctrl-logo{display:none!important}
  .top-right{display:flex;align-items:center;gap:10px;flex:0 0 auto}
  /* toasts */
  .toast-wrap{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:400;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
  .toast{background:var(--panel);color:var(--text);border:1px solid var(--border);border-left:4px solid var(--accent);
         box-shadow:0 10px 34px var(--shadow);border-radius:10px;padding:12px 18px;font-size:13px;max-width:460px;
         opacity:0;transform:translateY(12px);transition:opacity .25s,transform .25s;white-space:pre-line;text-align:center}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.good{border-left-color:var(--good)} .toast.bad{border-left-color:var(--bad)} .toast.info{border-left-color:var(--accent)}
  /* v1.5: foreground review stays interactive while background engines run.
     Only settings are frozen by the processing rule below; photo decisions are not. */
  .dedup-review{display:flex;flex-direction:column;gap:14px;width:100%;grid-column:1/-1}
  .dedup-group{border:1px solid var(--border);border-radius:12px;background:var(--panel);padding:12px}
  .dedup-group-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;font-size:12px}
  .dedup-group-head b{font-size:13px}
  .dedup-choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .dedup-choice{position:relative;border:2px solid transparent;border-radius:10px;overflow:hidden;background:var(--panel2);cursor:pointer;transition:border-color .15s,box-shadow .15s,transform .15s}
  .dedup-choice:hover{transform:translateY(-1px);border-color:rgba(37,99,235,.45)}
  .dedup-choice.selected{border-color:var(--good);box-shadow:0 0 0 2px color-mix(in srgb,var(--good) 18%,transparent)}
  .dedup-choice img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block}
  .dedup-choice-meta{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:6px;align-items:stretch;padding:6px 6px 6px 8px;font-size:11px;background:var(--panel2)}
  .dedup-choice-meta .delete-btn{width:34px;min-height:34px;font-size:14px;padding:0;align-self:stretch}
  .dedup-choice-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
  .dedup-choice-state{margin-top:4px;font-weight:700;color:var(--muted)}
  .dedup-choice.selected .dedup-choice-state{color:var(--good)}
  .dedup-recommend{position:absolute;top:6px;left:6px;border:0;background:var(--good);color:#fff;border-radius:6px;padding:4px 8px;font-size:10px;font-weight:700;z-index:2;cursor:pointer}
  .folder-results{display:flex;flex-direction:column;gap:14px;width:100%;grid-column:1/-1}
  .folder-group{border:1px solid var(--border);border-radius:12px;background:var(--panel);overflow:hidden}
  .folder-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 12px;background:var(--panel2);font-size:12px}
  .folder-head b{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .folder-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;padding:10px}
  .folder-head{cursor:pointer}
  .folder-head .fold-btn{border:0;background:transparent;color:var(--muted);font-size:12px;cursor:pointer;padding:2px 4px}
  .folder-group.collapsed .folder-body{display:none}
  .result-tools{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 10px;flex-wrap:wrap}
  .result-tools-left,.result-tools-right{display:flex;align-items:center;gap:6px}
  .result-tools .chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  #gallery.view-large .folder-grid{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
  #gallery.view-large .dedup-choices{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
  #gallery.view-list .folder-grid{grid-template-columns:1fr}
  #gallery.view-list .photo-card{display:grid;grid-template-columns:150px 1fr;min-height:96px}
  #gallery.view-list .photo-card .photo-img{width:150px;height:100%;min-height:96px;aspect-ratio:auto;object-fit:cover}
  #gallery.view-list .photo-card .photo-info{align-self:center;padding:10px 12px}
  #gallery.view-list .dedup-choices{grid-template-columns:1fr}
  #gallery.view-list .dedup-choice{display:grid;grid-template-columns:180px 1fr;min-height:110px}
  #gallery.view-list .dedup-choice img{width:180px;height:110px;aspect-ratio:auto;object-fit:cover}
  #gallery.view-list .dedup-choice-meta{align-self:center;padding:10px 12px}
  .source-path{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}
  .settings-subtitle{font-size:11px;font-weight:700;color:var(--muted);margin:11px 0 5px}
  body.processing .photo-card{cursor:default}

  .zoomctl{display:flex;align-items:center;gap:4px;background:rgba(255,255,255,.14);padding:3px;border-radius:8px;flex:0 0 auto}
  .zoomctl button{border:0;background:transparent;color:#fff;min-width:28px;height:26px;border-radius:6px;cursor:pointer;font-weight:700}
  .zoomctl button:hover{background:rgba(255,255,255,.18)}
  .zoomctl .zoomval{min-width:48px;text-align:center;font-size:11px;font-weight:700;user-select:none}
  @media (max-width: 820px){
    .zoomctl .zoomval{min-width:42px}.zoomctl button{min-width:26px}}
  @media (max-width: 620px){
    .lb-shortcuts{display:none}.zoomctl{order:2}.zoomctl .zoomval{display:none}}

  /* High in-app zoom needs a compact layout even if the native window itself
     is still wide enough that ordinary media queries would not fire. */
  .ui-zoom-large .brand small{display:none}
  .ui-zoom-large .step{padding:7px 10px;font-size:12px;white-space:nowrap}
  .ui-zoom-large .sidebar{width:235px;flex-basis:235px;padding:10px}
  .ui-zoom-large .folder-row{flex-direction:column}
  .ui-zoom-large .folder-row .btn{width:100%;padding:9px 10px}
  .ui-zoom-large .sidebar-scroll{gap:9px}
  .ui-zoom-large .sidebar-actions{gap:6px}
  .ui-zoom-large .btn,.ui-zoom-large .btn-ghost{padding-top:9px;padding-bottom:9px}
  .ui-zoom-large .main{padding:10px}
  .ui-zoom-large .gallery{grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:8px}

  /* Responsive desktop layout: important on Windows 125%/150%/200% DPI. */
  @media (max-width: 1100px){
    .top{padding:10px 12px}
    .brand{font-size:15px}.brand small{display:none}
    .step{padding:7px 10px;font-size:12px;white-space:nowrap}
    .sidebar{width:260px;flex-basis:260px;padding:12px}
    .main{padding:12px}
    .gallery{grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:10px}
    .lb-side{width:280px}
    .lb-bar{right:292px}
    .lb-next{right:298px}
    .lb-img{max-width:calc(100vw - 315px)}
  }
  @media (max-width: 820px){
    body{font-size:13px}
    .top{flex-wrap:wrap;align-content:center}
    .brand{flex:1 1 auto}
    .steps{order:3;flex:1 0 100%;justify-content:flex-start;padding-bottom:1px}
    .top-right{margin-left:auto}
    .viewport{height:calc(100vh - 96px);height:calc(100dvh - 96px)}
    .sidebar{width:235px;flex-basis:235px;padding:10px}
    .folder-row{flex-direction:column}
    .folder-row .btn{width:100%;padding:9px 10px}
    .sidebar-scroll{gap:9px}
    .sidebar-actions{gap:6px}
    .btn,.btn-ghost{padding-top:9px;padding-bottom:9px}
    .main{padding:10px}
    .gallery{grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:8px}
    .photo-info{padding:5px 6px}
    .lb-side{left:0;right:0;top:auto;bottom:0;width:100%;height:38dvh;padding:14px 16px 20px}
    .lb-img{max-width:94vw;max-height:52dvh;margin-bottom:34dvh}
    .lb-bar{right:0;padding:10px 12px}
    .lb-actions{gap:6px;flex-wrap:wrap;justify-content:flex-end}
    .lb-btn{height:34px;padding:0 10px;font-size:12px}
    .lb-close{width:36px;height:36px;font-size:18px}
    .lb-prev{left:10px}.lb-next{right:10px}
    .exmap .mapslot{width:min(256px,100%)}
  }
  @media (max-width: 620px){
    .top{position:relative;z-index:20}
    .viewport{height:calc(100vh - 54px);height:calc(100dvh - 54px);flex-direction:column}
    .sidebar{width:100%;flex:0 0 auto;max-height:44dvh;border-right:0;border-bottom:1px solid var(--border);padding:10px}
    .sidebar-scroll{max-height:26dvh}
    .sidebar-actions{display:grid;grid-template-columns:1fr 1fr;gap:6px}
    .sidebar-actions>*{min-width:0}
    .main{flex:1;min-height:0;padding:8px}
    .gallery{grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:7px}
    .filter-bar{gap:5px}.chip{padding:5px 9px}
    .pager{gap:8px;flex-wrap:wrap}
    .pager button{padding:6px 10px}
    .toast{max-width:calc(100vw - 24px)}
    #cn-build-badge{display:none}
  }
  @media (max-height: 700px){
    .sidebar-scroll{gap:8px}
    .sidebar-actions{gap:5px;padding-top:7px}
    .btn,.btn-ghost{padding-top:8px;padding-bottom:8px}
    .empty{padding:28px 0}
    .empty .icon{font-size:34px}
  }
  @media (prefers-reduced-motion: reduce){
    *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
  }

  /* v1.4.0 Aurora + iOS glass visual system */
  :root{--thumb-size:190px;--glass:rgba(255,255,255,.66);--glass-strong:rgba(255,255,255,.82)}
  body{background:
      radial-gradient(circle at 12% 4%,rgba(77,208,255,.22),transparent 32%),
      radial-gradient(circle at 88% 12%,rgba(170,118,255,.20),transparent 34%),
      radial-gradient(circle at 60% 95%,rgba(255,142,213,.16),transparent 36%),
      linear-gradient(145deg,#eef7ff 0%,#f7f5ff 46%,#fff5fb 100%);background-attachment:fixed}
  .top{background:
       radial-gradient(circle at 18% -80%,rgba(113,222,255,.34),transparent 44%),
       radial-gradient(circle at 78% -120%,rgba(205,148,255,.28),transparent 46%),
       rgba(33,78,191,.74);
       backdrop-filter:blur(24px) saturate(150%);-webkit-backdrop-filter:blur(24px) saturate(150%);
       border-bottom:1px solid rgba(255,255,255,.28);box-shadow:0 8px 28px rgba(52,72,140,.15)}
  .sidebar{background:rgba(255,255,255,.58);backdrop-filter:blur(24px) saturate(145%);-webkit-backdrop-filter:blur(24px) saturate(145%);
           border-right:1px solid rgba(255,255,255,.65)}
  .panel-box,.shortcut,.folder-group,.dedup-group,.photo-card,.btn-ghost,.chip,input[type=text],input[type=number],.wgroup select{
    backdrop-filter:blur(18px) saturate(135%);-webkit-backdrop-filter:blur(18px) saturate(135%)}
  .folder-group,.dedup-group,.photo-card{background:var(--glass);border-color:rgba(255,255,255,.72);box-shadow:0 8px 26px rgba(66,84,132,.08)}
  .folder-head{background:rgba(244,248,255,.72)}
  .main{padding-right:12px}
  .gallery,.folder-grid{grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr))}
  .photo-score{font-size:11px;font-weight:500;color:var(--muted)}
  .photo-info{min-height:48px}
  body.processing .photo-card,body.processing .remove-btn,body.processing .delete-btn,body.processing .status-toggle,
  body.processing .badge-tier,body.processing .move-select,body.processing .move-bulk{pointer-events:auto;opacity:1;filter:none}
  .top-right{display:flex;align-items:center;gap:8px;flex:0 0 auto}
  .window-controls{display:flex;gap:3px;padding-left:2px}
  .window-controls button{border:0;background:transparent;color:#fff;width:38px;height:32px;border-radius:8px;cursor:pointer;font-size:15px;line-height:1}
  .window-controls button:hover{background:rgba(255,255,255,.22)}
  .window-controls #winClose:hover{background:rgba(220,38,38,.88)}
  .lb-stage{position:absolute;left:0;top:0;right:clamp(280px,24vw,340px);bottom:0;overflow:hidden;display:flex;align-items:center;justify-content:center}
  .lb-img{transition:transform .08s linear;will-change:transform;cursor:grab;max-width:calc(100% - 36px);max-height:calc(100% - 90px)}
  .lb-img.dragging{cursor:grabbing}
  .lb-zoom-indicator{position:absolute;left:20px;bottom:18px;z-index:25;padding:5px 9px;border-radius:8px;background:rgba(0,0,0,.4);color:#fff;font-size:11px;pointer-events:none}
  .activity-panel{margin-top:4px;border:1px solid rgba(255,255,255,.72);border-radius:10px;background:rgba(255,255,255,.48);overflow:hidden}
  .activity-panel summary{cursor:pointer;padding:9px 10px;font-size:11px;font-weight:700;color:var(--muted)}
  .activity-log{max-height:170px;overflow:auto;padding:0 10px 9px;font-size:10px;color:var(--muted);display:flex;flex-direction:column;gap:6px}
  .activity-item{padding:6px 7px;border-radius:7px;background:rgba(255,255,255,.55)}
  .activity-item b{color:var(--text)}
  .workspace-nav{position:sticky;top:0;z-index:40;width:max-content;max-width:100%;margin:0 auto 10px;padding:5px;
    background:rgba(255,255,255,.58);border:1px solid rgba(255,255,255,.8);border-radius:14px;
    backdrop-filter:blur(20px) saturate(150%);-webkit-backdrop-filter:blur(20px) saturate(150%);
    box-shadow:0 8px 24px rgba(68,82,145,.08)}
  .workspace-nav .step{color:var(--text);background:transparent;border:0}
  .workspace-nav .step:hover{background:rgba(255,255,255,.7)}
  .workspace-nav .step.active{background:rgba(255,255,255,.9);color:var(--accent);box-shadow:0 4px 14px rgba(65,90,160,.10)}
  .dedup-quick{display:flex;gap:6px;flex-wrap:wrap;margin:-2px 0 10px}
  .dedup-quick button{border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,.72);color:var(--text);padding:5px 9px;font-size:10px;font-weight:700;cursor:pointer}
  .dedup-quick button:hover{border-color:var(--accent);color:var(--accent)}
  .task-center-head{display:flex;justify-content:space-between;align-items:center;margin:0}
  .task-center-head button{border:0;background:transparent;font-size:20px;color:var(--muted);cursor:pointer}
  .task-row{display:flex;justify-content:space-between;gap:10px;padding:6px 8px;border:1px solid rgba(120,135,170,.10);border-radius:10px;font-size:11px;background:rgba(255,255,255,.42)}
  .task-row b{color:var(--accent);font-weight:700}
  .task-tip{grid-column:1/-1;font-size:10px;color:var(--muted);line-height:1.4;padding-top:2px}
  .task-exit{width:auto;margin-top:0;padding:8px 12px;border:1px solid rgba(220,38,38,.18);border-radius:9px;background:rgba(255,255,255,.5);color:#b91c1c;font-size:11px;font-weight:700;cursor:pointer}
  .task-exit:hover{background:rgba(254,226,226,.8)}
  .sidebar-nav{display:flex;flex-direction:column;gap:5px;padding:0 0 10px;margin-bottom:10px;border-bottom:1px solid var(--border)}
  .sidebar-nav button{display:flex;align-items:center;gap:10px;width:100%;height:40px;padding:0 11px;border:1px solid transparent;border-radius:11px;background:transparent;color:var(--text);font-weight:700;cursor:pointer;text-align:left}
  .sidebar-nav button:hover{background:var(--panel2)}
  .sidebar-nav .step.active{background:color-mix(in srgb,var(--accent) 11%,var(--panel));color:var(--accent);border-color:color-mix(in srgb,var(--accent) 20%,transparent)}
  .nav-icon{width:22px;text-align:center;font-size:16px}.nav-label{white-space:nowrap}
  .source-panel,.settings-fold,.stats-fold{border:1px solid color-mix(in srgb,var(--border) 85%,transparent);border-radius:11px;padding:8px 9px;background:rgba(255,255,255,.42)}
  .source-panel>summary,.settings-fold>summary,.stats-fold>summary{cursor:pointer;font-size:12px;font-weight:700;color:var(--text);user-select:none}
  .workspace-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 10px;padding:10px 13px;border:1px solid rgba(255,255,255,.78);border-radius:14px;background:rgba(255,255,255,.55);backdrop-filter:blur(18px)}
  .workspace-heading>div{display:flex;align-items:baseline;gap:9px;min-width:0}.workspace-heading b{font-size:16px}.workspace-heading span{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toolbox-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}.toolbox-head>div{display:flex;flex-direction:column}.toolbox-head span{font-size:11px;color:var(--muted)}.toolbox-head button{border:0;background:transparent;font-size:22px;cursor:pointer;color:var(--muted)}
  .tool-card{display:flex;flex-direction:column;gap:3px;width:100%;padding:13px;margin:8px 0;border:1px solid var(--border);border-radius:13px;background:rgba(255,255,255,.55);color:var(--text);text-align:left;cursor:pointer}.tool-card:hover{border-color:var(--accent)}.tool-card span{font-size:11px;color:var(--muted)}
  details.tool-card{cursor:default}.tool-card summary{display:flex;flex-direction:column;gap:3px;cursor:pointer}
  body.sidebar-collapsed .sidebar{width:76px!important;flex-basis:76px!important;padding:10px 8px}
  body.sidebar-collapsed .sidebar-scroll{display:none}
  body.sidebar-collapsed .sidebar-actions>*:not(.sidebar-collapse){display:none!important}
  body.sidebar-collapsed .sidebar-actions{border:0;padding-top:0}
  body.sidebar-collapsed .sidebar-collapse{font-size:0;padding:9px}
  body.sidebar-collapsed .sidebar-collapse::after{content:'⇥';font-size:18px}
  body.sidebar-collapsed .nav-label{display:none}
  body.sidebar-collapsed .sidebar-nav button{justify-content:center;padding:0}
  body.sidebar-collapsed .nav-icon{width:auto}
  body.sidebar-collapsed .task-center{left:94px}
  .top button,.top input,.top .title-action,.top .window-controls{position:relative;z-index:2}
  .folder-grid .photo-card{content-visibility:auto;contain-intrinsic-size:190px 240px}
  body.processing #settingsPanel input,
  body.processing #settingsPanel select{opacity:.58;pointer-events:none}

  /* v1.5.1 workspace IA: global / source / content / inspector / status */
  body{height:100vh;height:100dvh;display:grid;grid-template-rows:54px minmax(0,1fr) 36px;overflow:hidden}
  .top{height:54px;min-height:54px;padding:7px 10px 7px 14px;flex-wrap:nowrap!important}
  .top-source{min-width:0;max-width:min(44vw,520px);display:flex;align-items:center;gap:8px;padding:5px 10px;border:1px solid rgba(255,255,255,.2);border-radius:12px;background:rgba(255,255,255,.11);backdrop-filter:blur(18px)}
  .top-source-copy{display:flex;flex-direction:column;min-width:0;line-height:1.15}.top-source-copy b,.top-source-copy small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.top-source-copy b{font-size:12px}.top-source-copy small{font-size:10px;opacity:.72;margin-top:2px}
  .source-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto;background:#94a3b8;box-shadow:0 0 0 3px rgba(148,163,184,.13)}.source-dot.online{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.16)}.source-dot.offline{background:#94a3b8}
  .top-primary{height:36px;min-width:118px;padding:0 16px;border:1px solid rgba(255,255,255,.42);border-radius:11px;background:rgba(255,255,255,.92);color:#3858c9;font-weight:800;cursor:pointer;box-shadow:0 6px 20px rgba(32,48,120,.16)}
  .top-primary:disabled{opacity:.45;cursor:not-allowed}.top-primary.stopping{background:#fee2e2;color:#b91c1c}
  .viewport{height:auto!important;min-height:0;display:flex;overflow:hidden}
  .sidebar{width:250px;flex:0 0 250px;padding:12px 10px;background:rgba(255,255,255,.68);backdrop-filter:blur(24px) saturate(145%);border-right:1px solid rgba(140,157,208,.18)}
  .sidebar-nav{flex:0 0 auto}.source-browser{flex:1;min-height:0;overflow-y:auto;margin-top:12px;padding:0 2px 8px}.section-head{display:flex;align-items:center;justify-content:space-between;margin:0 2px 8px;font-size:12px}.section-head b,.section-subhead{color:var(--muted);font-weight:800;letter-spacing:.03em}.section-subhead{font-size:10px;margin:14px 4px 5px}.source-add{border:0;background:rgba(87,109,226,.10);color:#4f63c9;border-radius:8px;padding:5px 8px;font-weight:800;cursor:pointer}.source-path-input{margin-bottom:8px;font-size:11px!important}
  .sources-list{display:flex;flex-direction:column;gap:6px}.source-card{width:100%;display:grid;grid-template-columns:10px minmax(0,1fr);gap:8px;text-align:left;padding:9px 10px;border:1px solid rgba(124,139,192,.18);border-radius:12px;background:rgba(255,255,255,.56);cursor:default}.source-card.connected{cursor:pointer}.source-card.connected:hover{border-color:rgba(91,111,218,.48);background:rgba(255,255,255,.78)}.source-card b,.source-card small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source-card b{font-size:12px}.source-card small{font-size:10px;color:var(--muted);margin-top:2px}.source-root-btn{margin-top:6px;border:0;border-radius:7px;padding:5px 7px;background:rgba(82,105,222,.09);color:#4257b8;font-size:10px;font-weight:700;cursor:pointer;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source-root-btn:disabled{cursor:not-allowed;color:#8b94a8;background:rgba(148,163,184,.10)}.source-card.offline .source-root-btn{color:#64748b;background:rgba(148,163,184,.09);cursor:pointer}.offline-preview-fallback{width:100%;aspect-ratio:3/2;place-items:center;background:linear-gradient(145deg,#edf1f8,#e4e9f5);color:#8490a7;font-size:11px}.catalog-meta{margin-top:3px;font-size:9px;color:var(--muted)}
  #shortcuts{display:flex;flex-direction:column}.shortcut{margin-top:5px;background:rgba(255,255,255,.52)}
  .sidebar-bottom{display:grid;grid-template-columns:1fr auto;gap:7px;border-top:1px solid rgba(124,139,192,.16);padding-top:9px}.sidebar-bottom .toolbox-open,.sidebar-bottom .sidebar-collapse{height:36px;border:1px solid rgba(124,139,192,.18);border-radius:10px;background:rgba(255,255,255,.55);color:var(--text);cursor:pointer;font-weight:700}.sidebar-bottom .toolbox-open{display:flex;align-items:center;justify-content:center;gap:7px}.sidebar-bottom .sidebar-collapse{padding:0 10px}
  .main{flex:1;min-width:0;padding:14px 16px 10px;overflow-y:auto;background:transparent}
  .workspace-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:10px}.workspace-heading>div:first-child{min-width:0}.workspace-heading b{font-size:18px}.workspace-heading span{display:block;color:var(--muted);font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.workspace-actions{display:flex;align-items:center;gap:6px;flex:0 0 auto}.workspace-action{height:34px;padding:0 11px;border:1px solid rgba(124,139,192,.22);border-radius:10px;background:rgba(255,255,255,.66);color:var(--text);font-size:11px;font-weight:800;cursor:pointer}.workspace-action:hover{border-color:var(--accent)}.workspace-action.danger-soft{color:#b91c1c;background:rgba(254,226,226,.62)}.workspace-action.cta{color:#fff;background:#d94a62;border-color:#d94a62;box-shadow:0 6px 16px rgba(185,28,28,.16)}.workspace-action:disabled{opacity:.45;cursor:not-allowed}
  .content-toolbar{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.content-toolbar .filter-bar{flex:1;min-width:0;margin-bottom:10px}.content-toolbar .result-tools{margin:0 0 10px;flex:0 0 auto}.filter-bar{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}.filter-bar::-webkit-scrollbar{display:none}.chip{white-space:nowrap}
  .inspector{width:300px;flex:0 0 300px;min-width:0;background:rgba(255,255,255,.64);backdrop-filter:blur(24px) saturate(145%);border-left:1px solid rgba(140,157,208,.18);display:flex;flex-direction:column;transition:width .18s,flex-basis .18s,opacity .18s}.inspector-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 12px 10px;border-bottom:1px solid rgba(124,139,192,.14)}.inspector-head>div{display:flex;flex-direction:column}.inspector-head b{font-size:13px}.inspector-head span{font-size:10px;color:var(--muted);margin-top:1px}.inspector-head button{width:30px;height:30px;border:0;border-radius:9px;background:rgba(90,105,180,.08);cursor:pointer;color:var(--muted)}.inspector-scroll{flex:1;min-height:0;overflow-y:auto;padding:10px}.inspector details{border:1px solid rgba(124,139,192,.16);border-radius:12px;background:rgba(255,255,255,.44);padding:9px 10px;margin-bottom:8px}.inspector summary{font-size:12px;font-weight:800;cursor:pointer}.data-summary{display:grid;gap:6px;margin-top:9px}.data-summary>div{display:flex;align-items:center;justify-content:space-between;font-size:11px}.data-summary span{color:var(--muted)}.data-summary small{display:block;margin-top:4px;color:var(--muted);font-size:9px;word-break:break-all}.storage-actions{display:grid!important;grid-template-columns:1fr 1fr;gap:5px;margin-top:5px}.storage-actions button{min-height:30px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,.66);color:var(--text);font-size:10px;font-weight:700;cursor:pointer}.storage-actions button[data-clean="features"]{grid-column:1/-1;color:#9a5a00;background:rgba(254,243,199,.55)}
  body.inspector-collapsed .inspector{width:0;flex-basis:0;opacity:0;border:0;overflow:hidden}body.inspector-collapsed #settingsQuick{background:rgba(91,111,218,.12);color:#4357ba}
  .statusbar{height:36px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 10px 0 14px;background:rgba(247,249,255,.82);backdrop-filter:blur(18px);border-top:1px solid rgba(124,139,192,.18);font-size:10px;color:var(--muted);z-index:30}.status-left{display:flex;align-items:center;gap:7px;min-width:0}.status-left b{color:var(--text)}.status-sep{width:1px;height:13px;background:var(--border)}.thumb-zoom{display:flex;align-items:center;gap:6px}.thumb-zoom button{width:25px;height:25px;border:1px solid var(--border);border-radius:7px;background:rgba(255,255,255,.72);cursor:pointer}.thumb-zoom input{width:110px}.thumb-zoom b{min-width:26px;text-align:right;color:var(--text)}
  :root{--thumb-size:180px}.gallery{grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr))}.folder-grid{grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr))}.dedup-choices{grid-template-columns:repeat(auto-fit,minmax(var(--thumb-size),1fr))}
  #gallery.view-large .folder-grid,#gallery.view-large .dedup-choices{grid-template-columns:repeat(auto-fill,minmax(max(260px,var(--thumb-size)),1fr))}
  .dedup-group-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;min-width:0}.dedup-group-title{display:flex;align-items:center;gap:7px;min-width:0}.dedup-group-meta{color:var(--muted);font-size:11px;white-space:nowrap}.dedup-group-actions{display:flex!important;align-items:center;justify-content:flex-end;gap:6px;margin:0!important;flex:0 0 auto}.dedup-group-actions .group-complete{min-height:32px;padding:5px 12px;border-radius:9px;background:color-mix(in srgb,var(--accent) 11%,white);border-color:color-mix(in srgb,var(--accent) 28%,var(--border));color:var(--accent);font-weight:800}.dedup-more{position:relative;margin:0!important}.dedup-more summary{list-style:none;width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--border);border-radius:9px;background:rgba(255,255,255,.68);font-size:0}.dedup-more summary::-webkit-details-marker{display:none}.dedup-more summary::after{content:'•••';font-size:12px;letter-spacing:1px}.dedup-more .dedup-quick{position:absolute;right:0;top:36px;z-index:40;min-width:150px;padding:6px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.96);box-shadow:0 14px 34px rgba(52,63,112,.18);display:grid;gap:4px}.dedup-more .dedup-quick button{border:0;border-radius:7px;padding:7px 8px;text-align:left;background:transparent;cursor:pointer;font-size:11px}.dedup-more .dedup-quick button:hover{background:var(--panel2)}
  body.sidebar-collapsed .sidebar{width:66px!important;flex-basis:66px!important;padding-left:7px;padding-right:7px}body.sidebar-collapsed .source-browser{display:none}body.sidebar-collapsed .sidebar-bottom{grid-template-columns:1fr}body.sidebar-collapsed .sidebar-bottom .nav-label{display:none}body.sidebar-collapsed .sidebar-collapse{font-size:0}body.sidebar-collapsed .sidebar-collapse::after{content:'⇥';font-size:17px}
  @media(max-width:1050px){.inspector{width:270px;flex-basis:270px}.sidebar{width:225px;flex-basis:225px}.top-source{max-width:34vw}.workspace-heading span{max-width:420px}}
  @media(max-width:820px){body{grid-template-rows:54px minmax(0,1fr) 36px}.viewport{height:auto!important;flex-direction:row}.inspector{display:none}.sidebar{width:210px;flex-basis:210px}.top-source{display:none}.workspace-heading span{display:none}.main{padding:10px}.gallery,.folder-grid,.dedup-choices{grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr))}}

  /* v1.5.3 native-window workspace: one sidebar + one temporary drawer */
  body{height:100vh;height:100dvh;display:grid!important;grid-template-rows:46px minmax(0,1fr) 34px!important;overflow:hidden!important;background:
    radial-gradient(circle at 18% 8%,rgba(125,211,252,.16),transparent 34%),
    radial-gradient(circle at 88% 12%,rgba(196,181,253,.18),transparent 34%),
    linear-gradient(135deg,#f7fbff 0%,#fbf8ff 100%)!important}
  .appbar{height:46px;display:flex;align-items:center;gap:10px;padding:0 10px;border-bottom:1px solid rgba(128,145,195,.16);background:rgba(242,247,255,.86);backdrop-filter:blur(22px) saturate(150%);z-index:60}
  .app-brand{display:flex;align-items:center;gap:8px;min-width:142px;padding-right:2px;color:#334155;user-select:none}.app-brand-mark{width:27px;height:27px;border-radius:9px;display:grid;place-items:center;background:linear-gradient(145deg,#7dd3fc,#6d73e6);color:#fff;font-weight:900;font-size:15px;box-shadow:0 5px 14px rgba(71,92,191,.18)}.app-brand-copy{display:flex;flex-direction:column;line-height:1.05}.app-brand-copy b{font-size:11px}.app-brand-copy small{font-size:8px;color:#8a94a8;margin-top:2px}.window-controls{display:flex;align-self:stretch;margin:-0px -10px 0 2px}.window-controls button{width:44px;height:46px;border:0;border-radius:0;background:transparent;color:#667085;font-size:15px;cursor:pointer}.window-controls button:hover{background:rgba(71,85,105,.09)}.window-controls #winClose:hover{background:#e5484d;color:#fff}.appbar button,.appbar input,.appbar .source-pill,.appbar .workspace-tabs,.appbar .window-controls{-webkit-app-region:no-drag}
  .workspace-tabs{display:flex;align-items:center;gap:4px;padding:3px;border:1px solid rgba(124,139,192,.16);border-radius:11px;background:rgba(255,255,255,.55)}
  .workspace-tabs .step{min-width:auto;height:30px;padding:0 11px;border:0;border-radius:8px;background:transparent;color:#64748b;font-size:11px;font-weight:800;display:flex;align-items:center;gap:6px;cursor:pointer}
  .workspace-tabs .step.active{background:#fff;color:#4861cf;box-shadow:0 3px 10px rgba(75,91,160,.11)}
  .appbar-spacer{flex:1;min-width:12px}
  .source-pill{min-width:190px;max-width:330px;height:34px;display:flex;align-items:center;gap:8px;padding:0 10px;border:1px solid rgba(124,139,192,.16);border-radius:11px;background:rgba(255,255,255,.58)}
  .source-pill-copy{display:flex;flex-direction:column;min-width:0;line-height:1.08}.source-pill-copy b,.source-pill-copy small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source-pill-copy b{font-size:10px}.source-pill-copy small{font-size:9px;color:var(--muted);margin-top:2px}
  .appbar-actions{display:flex;align-items:center;gap:6px}.appbar-btn{height:32px;padding:0 11px;border:1px solid rgba(124,139,192,.18);border-radius:10px;background:rgba(255,255,255,.68);color:#48536b;font-size:10px;font-weight:800;cursor:pointer}.appbar-btn:hover{border-color:#7b8fe4;background:#fff}.appbar-btn.primary{min-width:108px;background:linear-gradient(135deg,#5878ee,#6f63df);color:#fff;border-color:transparent;box-shadow:0 6px 16px rgba(76,93,210,.18)}.appbar-btn.primary:disabled{opacity:.38;box-shadow:none}.appbar-btn.icon-btn{width:32px;padding:0;font-size:14px}
  .workspace-shell{min-height:0;display:flex;overflow:hidden!important}
  .library-sidebar{width:224px;flex:0 0 224px;display:flex;flex-direction:column;min-height:0;padding:12px 10px 9px;border-right:1px solid rgba(124,139,192,.16);background:rgba(248,251,255,.72);backdrop-filter:blur(20px)}
  .library-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}.library-head>div{display:flex;flex-direction:column}.library-head b{font-size:12px}.library-head small{font-size:9px;color:var(--muted);margin-top:1px}.library-head .source-add{height:30px;padding:0 9px}
  .source-path-input{height:34px!important;margin-bottom:8px!important;border-radius:9px!important;font-size:10px!important}
  .source-browser{flex:1!important;min-height:0;overflow-y:auto;margin:0!important;padding:0 1px 8px!important}.library-footer{padding-top:8px;border-top:1px solid rgba(124,139,192,.14)}.library-footer .sidebar-collapse{width:100%;height:32px;border:1px solid rgba(124,139,192,.16);border-radius:9px;background:rgba(255,255,255,.52);color:var(--muted);font-size:10px;font-weight:700;cursor:pointer}
  .main{flex:1;min-width:0;min-height:0;padding:14px 15px 10px!important;overflow-y:auto!important;background:transparent!important}
  .dashboard-view{display:block;width:100%;min-width:0;min-height:100%;}.dashboard-view[hidden]{display:none!important}.photo-view{display:block;width:100%;min-width:0;min-height:100%}.photo-view[hidden]{display:none!important}
  .workspace-overview{width:100%!important;min-width:0!important;max-width:none!important;grid-column:1/-1!important}
  .content-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.content-title{min-width:0}.content-title b{font-size:18px}.content-title span{display:block;margin-top:2px;color:var(--muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:760px}.workspace-actions{display:flex;align-items:center;gap:6px;flex:0 0 auto}
  .content-toolbar{display:flex;align-items:center;justify-content:space-between;gap:8px}.filter-bar{flex-wrap:nowrap!important;overflow-x:auto}.result-tools{margin:0!important}
  .gallery{min-height:calc(100% - 88px)}
  .empty-start{min-height:420px;display:flex!important;flex-direction:column;align-items:center;justify-content:center;text-align:center}.empty-start .icon{font-size:42px}.empty-start .title{font-size:17px;font-weight:800;color:#39445a}.empty-start p{max-width:520px;margin:8px auto 16px;color:var(--muted);font-size:11px;line-height:1.6}.empty-add-source{height:36px;padding:0 14px;border:0;border-radius:10px;background:#5b72df;color:white;font-weight:800;cursor:pointer}

  .workspace-overview{display:block!important;min-height:100%;padding:2px 0 10px}.overview-hero{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(260px,.75fr);gap:12px;margin-bottom:12px}.overview-card{border:1px solid rgba(124,139,192,.16);border-radius:16px;background:rgba(255,255,255,.62);backdrop-filter:blur(18px);box-shadow:0 8px 24px rgba(70,83,140,.06)}.overview-primary{padding:18px 20px;display:flex;align-items:center;justify-content:space-between;gap:18px;background:linear-gradient(135deg,rgba(225,241,255,.78),rgba(240,231,255,.78))}.overview-primary-copy{min-width:0}.overview-primary-copy .eyebrow{font-size:10px;font-weight:800;color:#6677c7;letter-spacing:.08em;text-transform:uppercase}.overview-primary-copy h2{margin:5px 0 6px;font-size:22px;color:#35405a}.overview-primary-copy p{margin:0;max-width:680px;font-size:11px;line-height:1.65;color:var(--muted)}.overview-primary-actions{display:flex;flex-direction:column;gap:7px;flex:0 0 auto}.overview-primary-actions button{min-width:126px;height:36px;border-radius:10px;border:1px solid rgba(91,113,220,.22);background:rgba(255,255,255,.72);color:#4b5fc3;font-size:10px;font-weight:800;cursor:pointer}.overview-primary-actions .primary{border:0;background:linear-gradient(135deg,#5a79ef,#7367e0);color:#fff;box-shadow:0 7px 18px rgba(79,94,207,.18)}
  .overview-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:12px}.metric{padding:12px;border-radius:11px;background:rgba(246,249,255,.78);border:1px solid rgba(124,139,192,.10)}.metric span{display:block;font-size:9px;color:var(--muted);margin-bottom:4px}.metric b{font-size:18px;color:#374151}.metric small{display:block;margin-top:3px;font-size:8px;color:#8a94a8}
  .overview-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}.overview-section{padding:14px}.overview-section h3{margin:0 0 3px;font-size:12px;color:#3c465c}.overview-section>p{margin:0 0 10px;font-size:9px;color:var(--muted)}.overview-section.sources{grid-column:span 7}.overview-section.storage{grid-column:span 5}.overview-section.guide{grid-column:span 7}.overview-section.demo{grid-column:span 5}.overview-list{display:grid;gap:6px}.overview-source-row{display:grid;grid-template-columns:9px minmax(0,1fr) auto;align-items:center;gap:8px;padding:9px 10px;border-radius:10px;background:rgba(247,249,255,.78);border:1px solid rgba(124,139,192,.10)}.overview-source-row .copy{min-width:0}.overview-source-row b,.overview-source-row small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.overview-source-row b{font-size:10px}.overview-source-row small{margin-top:2px;font-size:8px;color:var(--muted)}.overview-source-row button{height:28px;padding:0 9px;border:1px solid rgba(124,139,192,.15);border-radius:8px;background:#fff;color:#5062bb;font-size:9px;font-weight:800;cursor:pointer}.overview-source-row button:disabled{cursor:default;color:#98a1b3;background:#f3f5f8}
  .overview-storage-list{display:grid;gap:6px}.overview-storage-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border-radius:9px;background:rgba(247,249,255,.72);font-size:9px}.overview-storage-row span{color:var(--muted)}.overview-storage-row b{font-size:10px}.workflow-guide{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.guide-step{padding:10px;border-radius:10px;background:rgba(247,249,255,.72);border:1px solid rgba(124,139,192,.10)}.guide-step b{display:block;font-size:10px;margin-bottom:3px}.guide-step small{font-size:8px;line-height:1.5;color:var(--muted)}
  .source-ready{display:grid!important;grid-template-columns:minmax(0,1.45fr) minmax(250px,.7fr);gap:12px;min-height:0}.source-ready-main,.source-ready-side{padding:16px}.source-ready-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}.source-ready-head h2{margin:0;font-size:19px}.source-ready-head p{margin:4px 0 0;font-size:10px;color:var(--muted);word-break:break-all}.source-ready-badge{display:flex;align-items:center;gap:6px;white-space:nowrap;padding:6px 9px;border-radius:999px;background:rgba(34,197,94,.09);color:#27834a;font-size:9px;font-weight:800}.source-ready-facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}.source-ready-fact{padding:11px;border-radius:10px;background:rgba(247,249,255,.76);border:1px solid rgba(124,139,192,.10)}.source-ready-fact span{display:block;font-size:8px;color:var(--muted);margin-bottom:4px}.source-ready-fact b{font-size:11px;color:#384257}.source-ready-actions{display:flex;gap:8px;flex-wrap:wrap}.source-ready-actions button{height:36px;padding:0 13px;border-radius:10px;border:1px solid rgba(124,139,192,.16);background:#fff;color:#4859ac;font-size:10px;font-weight:800;cursor:pointer}.source-ready-actions .primary{border:0;background:linear-gradient(135deg,#5b79ef,#7465df);color:#fff;box-shadow:0 7px 18px rgba(76,91,206,.18)}.source-ready-side h3{margin:0 0 10px;font-size:12px}.source-ready-check{display:flex;align-items:flex-start;gap:8px;padding:9px 0;border-bottom:1px solid rgba(124,139,192,.09)}.source-ready-check:last-child{border-bottom:0}.source-ready-check .dot{width:7px;height:7px;border-radius:50%;background:#78a1ef;margin-top:4px;flex:0 0 auto}.source-ready-check b{display:block;font-size:9px}.source-ready-check small{display:block;margin-top:2px;font-size:8px;line-height:1.45;color:var(--muted)}
  .source-card{display:block!important;padding:0!important;overflow:hidden}.source-card>summary{list-style:none;display:grid;grid-template-columns:10px minmax(0,1fr) 16px;gap:8px;align-items:center;padding:9px 10px;cursor:pointer}.source-card>summary::-webkit-details-marker{display:none}.source-card>summary::after{content:'›';color:#9aa4b7;font-size:15px;transition:transform .15s}.source-card[open]>summary::after{transform:rotate(90deg)}.source-card .source-summary-copy{min-width:0}.source-card .source-summary-copy b,.source-card .source-summary-copy small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source-card .source-summary-copy b{font-size:11px}.source-card .source-summary-copy small{font-size:8px;color:var(--muted);margin-top:2px}.source-roots{display:grid;gap:5px;padding:0 9px 9px 27px}.source-card.current{border-color:rgba(86,108,222,.42);box-shadow:0 0 0 2px rgba(86,108,222,.06)}
  @media(max-width:1050px){.overview-hero,.source-ready{grid-template-columns:1fr}.overview-grid{grid-template-columns:1fr}.overview-section.sources,.overview-section.storage,.overview-section.guide,.overview-section.demo{grid-column:auto}.overview-primary{align-items:flex-start}.overview-primary-actions{flex-direction:row}.workflow-guide{grid-template-columns:1fr 1fr 1fr}}
  @media(max-width:760px){.overview-primary{flex-direction:column}.overview-primary-actions{width:100%;flex-direction:row}.overview-primary-actions button{flex:1}.overview-metrics{grid-template-columns:1fr 1fr}.workflow-guide{grid-template-columns:1fr}.source-ready-facts{grid-template-columns:1fr 1fr}}
  .statusbar{height:34px!important;padding:0 10px 0 12px!important;z-index:70!important}
  body.sidebar-collapsed .library-sidebar{width:58px!important;flex-basis:58px!important;padding-left:7px!important;padding-right:7px!important}body.sidebar-collapsed .library-head>div,body.sidebar-collapsed .library-head .source-add,body.sidebar-collapsed .source-path-input,body.sidebar-collapsed .source-browser{display:none!important}body.sidebar-collapsed .library-head{height:28px;margin:0}body.sidebar-collapsed .library-footer{margin-top:auto}body.sidebar-collapsed .sidebar-collapse{font-size:0}body.sidebar-collapsed .sidebar-collapse::after{content:'⇥';font-size:16px}
  @media(max-width:1050px){.source-pill{max-width:220px;min-width:150px}.workspace-tabs .step{padding:0 9px}.library-sidebar{width:205px;flex-basis:205px}}
  @media(max-width:900px){.source-pill{display:none}.content-title span{display:none}.library-sidebar{width:190px;flex-basis:190px}}
  /* v1.6.0: one left control column + one photo workspace.
     Auxiliary UI never occupies the right or bottom of the gallery. */
  :root{--thumb-size:260px}
  .library-sidebar{
    width:304px!important;flex:0 0 304px!important;padding:10px!important;
    overflow-y:auto!important;overflow-x:hidden!important;gap:0!important
  }
  .source-browser{
    flex:0 0 auto!important;min-height:90px!important;max-height:230px!important;
    overflow-y:auto!important;padding:0 1px 8px!important
  }
  .sidebar-primary-action{padding:8px 0 9px;border-top:1px solid rgba(124,139,192,.14)}
  .sidebar-primary-action .sidebar-start{width:100%!important;height:38px!important;font-size:11px!important}
  .sidebar-utility-tabs{display:none!important}
  .sidebar-utility-host{
    display:grid!important;grid-template-columns:1fr;gap:9px;
    flex:0 0 auto!important;max-height:none!important;overflow:visible!important;
    padding:9px 0 4px;border-top:1px solid rgba(124,139,192,.14)
  }
  .sidebar-utility-host .inspector-drawer,
  .sidebar-utility-host .toolbox-panel,
  .sidebar-utility-host .task-center,
  .sidebar-utility-host .activity-panel-sidebar{
    position:static!important;inset:auto!important;width:100%!important;height:auto!important;
    max-width:none!important;max-height:none!important;transform:none!important;opacity:1!important;
    pointer-events:auto!important;display:block!important;margin:0!important;padding:10px!important;
    border:1px solid rgba(124,139,192,.15)!important;border-radius:13px!important;
    background:rgba(255,255,255,.58)!important;box-shadow:none!important;
    backdrop-filter:none!important;overflow:visible!important
  }
  .sidebar-utility-host .inspector-head,
  .sidebar-utility-host .toolbox-head,
  .sidebar-utility-host .task-center-head,
  .activity-panel-head{
    display:flex!important;align-items:center!important;justify-content:space-between!important;
    gap:8px;padding:0 0 8px!important;margin:0 0 8px!important;
    border-bottom:1px solid rgba(124,139,192,.11)!important
  }
  .sidebar-utility-host .inspector-head button,
  .sidebar-utility-host .toolbox-head button,
  .sidebar-utility-host .task-center-head button{display:none!important}
  .sidebar-utility-host .inspector-scroll{padding:0!important;overflow:visible!important}
  .sidebar-utility-host .task-center{display:grid!important;grid-template-columns:1fr!important;gap:6px!important}
  .sidebar-utility-host .task-row{font-size:11px;padding:8px 9px}
  .sidebar-utility-host .task-tip{grid-column:auto!important;font-size:9px;padding:2px 2px 0;line-height:1.5}
  .sidebar-utility-host .task-exit{width:100%;margin-top:2px}
  .sidebar-utility-host .tool-card{padding:10px;margin:6px 0;font-size:11px}
  .activity-panel-head b{font-size:12px}.activity-panel-head button{
    border:0;background:transparent;color:var(--accent);font-size:10px;font-weight:800;cursor:pointer
  }
  .activity-log{max-height:300px!important;overflow:auto!important;padding:0!important;font-size:11px!important;gap:7px!important}
  .catalog-load-more{display:block;margin:14px auto 20px;padding:9px 16px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.86);color:var(--accent);font-weight:800;cursor:pointer}
  .activity-item{padding:8px 9px!important;border:1px solid rgba(124,139,192,.10);line-height:1.45;background:rgba(248,250,255,.72)!important}
  .drawer-scrim{display:none!important}
  #settingsQuick,#taskToggle,#toolboxOpen{display:none!important}
  body.sidebar-collapsed .sidebar-primary-action,
  body.sidebar-collapsed .sidebar-utility-host{display:none!important}
  body.sidebar-collapsed .library-sidebar{width:58px!important;flex-basis:58px!important;padding-left:7px!important;padding-right:7px!important}
  body.sidebar-collapsed .library-head>div,
  body.sidebar-collapsed .library-head .source-add,
  body.sidebar-collapsed .source-path-input,
  body.sidebar-collapsed .source-browser{display:none!important}
  body.sidebar-collapsed .library-head{height:28px;margin:0}
  body.sidebar-collapsed .library-footer{margin-top:auto}
  body.sidebar-collapsed .sidebar-collapse{font-size:0}
  body.sidebar-collapsed .sidebar-collapse::after{content:'⇥';font-size:16px}

  /* One thumbnail size means one real card width in every photo result view. */
  .gallery,.folder-grid,.dedup-choices{
    grid-template-columns:repeat(auto-fill,var(--thumb-size))!important;
    justify-content:start!important;align-items:start!important;gap:12px!important
  }
  .photo-card,.dedup-choice{width:var(--thumb-size);max-width:100%}
  .photo-name{font-size:12px!important;font-weight:750}.photo-info{min-height:54px!important}
  #gallery.view-list .folder-grid,#gallery.view-list .dedup-choices{grid-template-columns:1fr!important}
  #gallery.view-list .photo-card,#gallery.view-list .dedup-choice{width:100%!important;max-width:none!important}
  #gallery.view-large .folder-grid,#gallery.view-large .dedup-choices{
    grid-template-columns:repeat(auto-fill,var(--thumb-size))!important
  }

  @media(max-width:1050px){.library-sidebar{width:270px!important;flex-basis:270px!important}}
  @media(max-width:900px){.library-sidebar{width:250px!important;flex-basis:250px!important}.source-pill{display:none}}
</style></head><body>
<header class="appbar pywebview-drag-region">
  <div class="app-brand" aria-label="PhotoCurator">
    <span class="app-brand-mark">C</span>
    <span class="app-brand-copy"><b>PhotoCurator</b><small>v{{ app_version }}</small></span>
  </div>
  <nav class="workspace-tabs" aria-label="照片整理工作区">
    <button class="step active" data-step="cull"><span class="nav-icon">◐</span><span class="nav-label">模糊废片</span></button>
    <button class="step" data-step="dedup"><span class="nav-icon">▱</span><span class="nav-label">相似照片</span></button>
    <button class="step" data-step="trash"><span class="nav-icon">♲</span><span class="nav-label">回收站</span></button>
  </nav>

  <div class="appbar-spacer"></div>

  <div class="source-pill" id="topSource">
    <span class="source-dot offline" id="topSourceDot"></span>
    <span class="source-pill-copy"><b id="topSourceName">未选择数据源</b><small id="topSourcePath">添加照片来源后开始</small></span>
  </div>

  <div class="appbar-actions">
    <button class="appbar-btn" id="settingsQuick">筛选</button>
    <button class="appbar-btn" id="taskToggle">任务</button>
    <button class="appbar-btn icon-btn" id="toolboxOpen" title="工具箱">⌘</button>
  </div>
  <div class="window-controls" aria-label="窗口控制">
    <button id="winMin" title="最小化">—</button>
    <button id="winMax" title="最大化/还原">□</button>
    <button id="winClose" title="关闭到后台">×</button>
  </div>
</header>

<div class="workspace-shell">
  <aside class="library-sidebar" id="sidebar">
    <div class="library-head">
      <div><b>数据源</b><small>硬盘 / U盘 / 照片文件夹</small></div>
      <button class="source-add" id="browseBtn">＋ 添加</button>
    </div>

    <input type="text" id="folderInput" class="source-path-input" placeholder="选择或粘贴照片文件夹路径">

    <div class="source-browser">
      <div id="sourcesList" class="sources-list"></div>
      <div class="section-subhead">示例与最近</div>
      <div id="shortcuts"></div>
    </div>

    <div class="sidebar-primary-action">
      <button class="appbar-btn primary sidebar-start" id="startBtn">▶ 开始分析</button>
    </div>
    <div class="sidebar-utility-host" id="sidebarUtilityHost"></div>

    <div class="library-footer">
      <button class="sidebar-collapse" id="sidebarCollapse" title="收起/展开数据源栏">⇤ 收起数据源</button>
    </div>
  </aside>

  <main class="main">
    <section class="dashboard-view" id="dashboardView"></section>

    <section class="photo-view" id="photoView" hidden>
      <section class="content-head" id="contentHead">
        <div class="content-title">
          <b id="workspaceTitle">模糊废片</b>
          <span id="workspaceHint">快速复核模糊与失焦照片，后台分析不会打断当前操作。</span>
        </div>
        <div class="workspace-actions">
          <button class="workspace-action" id="exportBtn" style="display:none">⬇ 导出</button>
          <button class="workspace-action" id="exportPbgBtn" style="display:none">📱 壁纸</button>
          <button class="workspace-action danger-soft" id="moveBlurryBtn" style="display:none">🗑 移入回收站</button>
          <button class="workspace-action" id="dedupApplyBtn" style="display:none!important" aria-hidden="true">旧版批量处理</button>
        </div>
      </section>

      <div class="progress-wrap" id="progressWrap">
        <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
        <div class="progress-line">
          <div class="progress-text" id="progressText">…</div>
          <button class="chip new-results" id="loadNewResults" style="display:none">加载新结果</button>
        </div>
      </div>

      <div class="content-toolbar">
        <div class="filter-bar" id="filterBar" style="display:none"></div>
        <div class="result-tools" id="resultTools" style="display:none">
          <div class="result-tools-left">
            <button class="chip" id="expandAllBtn">全部展开</button>
            <button class="chip" id="collapseAllBtn">全部收起</button>
          </div>
          <div class="result-tools-right">
            <button class="chip" data-view="small">网格</button>
            <button class="chip" data-view="list">列表</button>
          </div>
        </div>
      </div>

      <div class="pager" id="pager" style="display:none!important"></div>
      <div class="gallery" id="gallery"></div>
    </section>
  </main>
</div>

<aside class="inspector-drawer" id="inspector" aria-hidden="true">
  <div class="inspector-head">
    <div><b>筛选与数据</b><span>只在需要时打开，不占用照片工作区</span></div>
    <button id="inspectorClose" title="关闭">×</button>
  </div>
  <div class="inspector-scroll">
    <details class="settings-fold" id="settingsDetails" open>
      <summary>筛选设置</summary>
      <div id="settingsPanel" style="margin-top:10px"></div>
    </details>

    <details class="stats-fold" open>
      <summary>当前结果</summary>
      <div class="panel-box" style="margin-top:8px">
        <div class="stat-row" data-steps="cull dedup rank"><span>照片数量</span><span class="v" id="sImages">0</span></div>
        <div class="stat-row" data-steps="cull"><span>清晰</span><span class="v" id="sSharp">0</span></div>
        <div class="stat-row" data-steps="cull"><span>轻微软</span><span class="v" id="sSoft" style="color:var(--warn)">0</span></div>
        <div class="stat-row" data-steps="cull"><span>模糊</span><span class="v" id="sBlurry">0</span></div>
        <div class="stat-row" data-steps="dedup"><span>相似分组</span><span class="v" id="sGroups">0</span></div>
        <div class="stat-row" data-steps="cull dedup rank trash"><span>当前显示</span><span class="v" id="sShowing">0</span></div>
        <div class="stat-row" data-steps="trash"><span>软件回收站</span><span class="v" id="sTrash">0</span></div>
        <div id="removedBox" style="display:none">已移除 <b id="removedN">0</b> 张 · <a id="restoreAll">全部恢复</a></div>
      </div>
    </details>

    <details class="data-fold" open>
      <summary>数据与存储</summary>
      <div class="data-summary" id="dataSummary">
        <div><span>数据库</span><b id="dbUsage">—</b></div>
        <div><span>图库离线预览</span><b id="catalogPreviewUsage">—</b></div>
        <div><span>临时预览缓存</span><b id="previewUsage">—</b></div>
        <div><span>相似特征</span><b id="featureUsage">—</b></div>
        <div><span>日志</span><b id="logUsage">—</b></div>
        <div class="storage-actions">
          <button data-clean="previews">清理预览缓存</button>
          <button data-clean="logs">清理旧日志</button>
          <button data-clean="features">重建相似特征</button>
        </div>
        <small id="dataRootText">正在读取数据目录…</small>
      </div>
    </details>
  </div>
</aside>
<div class="drawer-scrim" id="drawerScrim"></div>

<footer class="statusbar">
  <div class="status-left">
    <span class="source-dot offline" id="statusSourceDot"></span>
    <span id="statusSourceText">数据源未连接</span>
    <span class="status-sep"></span>
    <span>显示 <b id="statusShowing">0</b> 张</span>
  </div>
  <div class="thumb-zoom" title="Ctrl + 鼠标滚轮也可以调整缩略图大小">
    <span>缩略图</span>
    <button id="thumbSmaller" aria-label="缩小缩略图">−</button>
    <input id="thumbSizeRange" type="range" min="160" max="420" step="10" value="260">
    <button id="thumbLarger" aria-label="放大缩略图">＋</button>
    <b id="thumbSizeValue">260</b>
  </div>
</footer>

<div class="lightbox" id="lightbox">
  <div class="lb-bar">
    <div><div id="lbName">—</div><div style="font-size:12px;opacity:.7" id="lbCount"></div></div>
    <div class="lb-actions">
      <button class="lb-btn pbg" id="lbPhoneBg" style="display:none">📱 手机壁纸</button>
      <button class="lb-btn restore" id="lbMoveSelect" style="display:none">☑ 加入移动</button>
      <button class="lb-btn toggle" id="lbToggle" style="display:none">→ 标记为模糊</button>
      <button class="lb-btn restore" id="lbRestore" style="display:none">↺ 全部恢复</button>
      <button class="lb-btn remove" id="lbRemove" style="display:none">✕ 移除</button>
      <button class="lb-btn delete" id="lbDelete" style="display:none">🗑 移入软件回收站</button>
      <button class="lb-btn restore" id="lbTrashRestore" style="display:none">↩ 恢复原位</button>
      <button class="lb-btn delete" id="lbTrashPurge" style="display:none">永久删除</button>
      <button class="lb-close" id="lbClose" title="关闭大图" aria-label="关闭大图">✕</button>
    </div>
  </div>
  <button class="lb-nav lb-prev" id="lbPrev" title="上一张" aria-label="上一张">‹</button>
  <div class="lb-stage" id="lbStage"><img class="lb-img" id="lbImg" src=""><div class="lb-zoom-indicator" id="lbZoom">适应窗口</div></div>
  <button class="lb-nav lb-next" id="lbNext" title="下一张" aria-label="下一张">›</button>
  <div class="lb-side" id="lbSide"></div>
  <div class="lb-shortcuts" style="position:absolute;left:16px;bottom:10px;color:rgba(255,255,255,.55);font-size:10px;z-index:21;pointer-events:none">大图快捷键：← → 切换 · Esc 关闭 · B 壁纸 · X 移除</div>
</div>

<div class="pc-modal-backdrop" id="confirmModal" aria-hidden="true">
  <div class="pc-modal" role="dialog" aria-modal="true" aria-labelledby="confirmModalTitle">
    <h3 id="confirmModalTitle">确认操作</h3>
    <p id="confirmModalText">—</p>
    <div class="pc-modal-actions">
      <button id="confirmCancel">取消</button>
      <button class="primary" id="confirmOk">确认</button>
    </div>
  </div>
</div>

<div class="pc-modal-backdrop" id="deleteModal" aria-hidden="true">
  <div class="pc-modal" role="dialog" aria-modal="true" aria-labelledby="deleteModalTitle">
    <h3 id="deleteModalTitle">删除照片</h3>
    <p>默认移入 PhotoCurator 软件回收站，可在回收站中恢复。只有“彻底删除”会永久移除原文件。</p>
    <div class="pc-modal-file" id="deleteModalFile">—</div>
    <p class="pc-modal-hint">Enter：移入软件回收站　·　P：彻底删除　·　Esc：取消</p>
    <div class="pc-modal-actions">
      <button id="deleteCancel">取消</button>
      <button class="danger" id="deletePermanent">彻底删除（P）</button>
      <button class="primary" id="deleteTrash">移入软件回收站（Enter）</button>
    </div>
  </div>
</div>

<aside class="toolbox-panel" id="toolboxPanel">
  <div class="toolbox-head"><div><b>工具箱</b><span>低频辅助与扩展功能</span></div><button id="toolboxClose">×</button></div>
  <button class="tool-card" id="openRankTool"><b>✦ 照片评分 / 精选推荐</b><span>独立扩展工具，不参与默认清理主流程。</span></button>
</aside>

<aside class="activity-panel-sidebar" id="activityPanel">
  <div class="activity-panel-head"><b>☷ 系统运行日志</b><button id="activityRefresh">刷新</button></div>
  <div id="activityLog" class="activity-log">暂无记录</div>
</aside>

<aside class="task-center" id="taskCenter">
  <div class="task-center-head"><b>任务中心</b><button id="taskClose">×</button></div>
  <div class="task-row"><span>清晰度分析</span><b id="taskCull">待开始</b></div>
  <div class="task-row"><span>相似分析</span><b id="taskDedup">待开始</b></div>
  <div class="task-row"><span>精选评分</span><b id="taskRank">待开始</b></div>
  <div class="task-row"><span>文件操作</span><b id="taskFiles">空闲</b></div>
  <div class="task-tip" id="taskFileHint">删除、恢复和永久删除在持久化后台队列中执行；异常退出后未完成任务会在下次启动继续。</div>
  <button class="task-exit" id="appExit">退出 PhotoCurator</button>
</aside>
<div class="toast-wrap" id="toastWrap"></div>
<div id="cn-build-badge" style="position:fixed;right:10px;bottom:8px;z-index:50;font-size:10px;color:var(--muted);opacity:.55;pointer-events:none">照片筛选 · 中文桌面版 v{{ app_version }}</div>

<script>
function reportUiFatal(reason){
  const msg=String((reason&&reason.message)||reason||'未知前端错误');
  document.documentElement.dataset.uiFatal='1';
  console.error('PhotoCurator UI fatal:',reason);
  let box=document.getElementById('uiFatalBanner');
  if(!box){
    box=document.createElement('div');
    box.id='uiFatalBanner';
    box.style.cssText='position:fixed;left:18px;right:18px;top:66px;z-index:9999;padding:12px 14px;border-radius:12px;background:#fff1f2;border:1px solid #fecdd3;color:#9f1239;font:600 12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;box-shadow:0 12px 40px rgba(127,29,29,.18)';
    document.body.appendChild(box);
  }
  box.textContent='界面运行异常，部分按钮可能不可用。请重启 PhotoCurator；若仍出现，请查看运行日志。错误：'+msg;
}
window.addEventListener('error',e=>reportUiFatal(e.error||e.message));
window.addEventListener('unhandledrejection',e=>reportUiFatal(e.reason));

function toast(msg,type){
  const w=document.getElementById('toastWrap');
  const el=document.createElement('div');el.className='toast '+(type||'good');el.textContent=msg;
  w.appendChild(el);requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),300);},3600);
}
function escHtml(v){
  return String(v==null?'':v).replace(/[&<>"']/g,ch=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[ch]);
}

let confirmDialogResolve=null;
function closeConfirmDialog(choice=false){
  const modal=document.getElementById('confirmModal');
  modal.classList.remove('open');modal.setAttribute('aria-hidden','true');
  const done=confirmDialogResolve;confirmDialogResolve=null;
  if(done)done(!!choice);
}
function askBatchConfirm(title,text,okText='确认'){
  if(confirmDialogResolve)closeConfirmDialog(false);
  document.getElementById('confirmModalTitle').textContent=title||'确认操作';
  document.getElementById('confirmModalText').textContent=text||'';
  document.getElementById('confirmOk').textContent=okText;
  const modal=document.getElementById('confirmModal');
  modal.classList.add('open');modal.setAttribute('aria-hidden','false');
  setTimeout(()=>document.getElementById('confirmOk').focus(),0);
  return new Promise(resolve=>{confirmDialogResolve=resolve;});
}
document.getElementById('confirmCancel').onclick=()=>closeConfirmDialog(false);
document.getElementById('confirmOk').onclick=()=>closeConfirmDialog(true);
document.getElementById('confirmModal').addEventListener('click',e=>{if(e.target.id==='confirmModal')closeConfirmDialog(false);});
document.addEventListener('keydown',e=>{
  const modal=document.getElementById('confirmModal');
  if(!modal.classList.contains('open'))return;
  if(e.key==='Escape'){e.preventDefault();closeConfirmDialog(false);}
  else if(e.key==='Enter'){e.preventDefault();closeConfirmDialog(true);}
});

let deleteDialogResolve=null;
function closeDeleteDialog(choice=null){
  const modal=document.getElementById('deleteModal');
  modal.classList.remove('open');modal.setAttribute('aria-hidden','true');
  const done=deleteDialogResolve;deleteDialogResolve=null;
  if(done)done(choice);
}
function askDeleteMode(name){
  if(deleteDialogResolve)closeDeleteDialog(null);
  const modal=document.getElementById('deleteModal');
  document.getElementById('deleteModalFile').textContent=name||'当前照片';
  modal.classList.add('open');modal.setAttribute('aria-hidden','false');
  setTimeout(()=>document.getElementById('deleteTrash').focus(),0);
  return new Promise(resolve=>{deleteDialogResolve=resolve;});
}
document.getElementById('deleteCancel').onclick=()=>closeDeleteDialog(null);
document.getElementById('deleteTrash').onclick=()=>closeDeleteDialog('trash');
document.getElementById('deletePermanent').onclick=()=>closeDeleteDialog('permanent');
document.getElementById('deleteModal').addEventListener('click',e=>{if(e.target.id==='deleteModal')closeDeleteDialog(null);});
document.addEventListener('keydown',e=>{
  const modal=document.getElementById('deleteModal');
  if(!modal.classList.contains('open'))return;
  if(e.key==='Escape'){e.preventDefault();closeDeleteDialog(null);}
  else if(e.key==='Enter'){e.preventDefault();closeDeleteDialog('trash');}
  else if(e.key==='p'||e.key==='P'){e.preventDefault();closeDeleteDialog('permanent');}
});
let folder=null, photos=[], lbList=[], lbIndex=0, currentStep='cull', folderStatus={};
let sourceCatalog=[], discoveredDevices=[], selectedSource=null, catalogRootView=null;
let latestStorageSummary=null, demoShortcutPath='', demoShortcutCount=0, demoShortcutReady=false;
const cullLiveStore=new Map();
const dedupLiveStore=new Map();
let isRunning=false, runningStep=null, codespacesMode=false;
let coreRunning=false, corePollTimer=null, coreSnapshots={cull:null,dedup:null};
let lastRankSig='', lastStep=null, weightTimer=null, removedCount=0, pollFailures=0, largeResultWarned=false;

// Result paging/filter state must exist before the first UI bootstrap call.
// Keep boot-critical state together here so setupFilterBar() cannot touch
// a later lexical declaration and abort the rest of the interaction bindings.
let cullChunkToken=0, cullVisibleTotal=0;
let dedupChunkToken=0;
let dedupStatusFilter='pending', dedupVisibleTotal=0;
let dedupStatusCounts={pending:0,reviewed:0,updated:0};
// These controls are needed by setupFilterBar() during initial page boot.
// Define them before the first setupFilterBar() call to avoid TDZ failures
// that would stop Codespaces shortcut/sample initialization.
const startBtn=document.getElementById('startBtn');
startBtn.disabled=true;
startBtn.setAttribute('aria-disabled','true');
startBtn.title='请先选择照片文件夹';
let cullReady=false;
const CATS=[['aesthetic','综合观感'],['composition','构图'],['technical','技术质量'],['sharpness','清晰度'],['color','色彩']];
const catColor=(i,n)=>`hsl(${Math.round(i*360/(n||CATS.length))},80%,62%)`;
const CATCOLORS=CATS.map((_,i)=>catColor(i,CATS.length));
const DEFAULTS={aesthetic:30,composition:22,technical:20,sharpness:16,color:12};
let weights={...DEFAULTS};
let recursiveScan=true;
let compareScope='folder';
let outputMode='source';
let customOutput='';
let cullStrictness=1.0;
let cullAdaptive=true;
let cullRescue=true;
let dedupThreshold=0.80;
let pairMode='both';
let rankTopN=50;
let resultView='small';
try{
  const v=localStorage.getItem('pc-result-view');
  if(v==='list')resultView='list'; else resultView='small';
}catch(_){}
const collapsedFolders=new Set();
const expandedFolders=new Set();
let defaultFoldersCollapsed=true;
try{
  const saved=JSON.parse(localStorage.getItem('pc-library-settings')||'{}');
  if(typeof saved.recursive==='boolean')recursiveScan=saved.recursive;
  if(['folder','global'].includes(saved.compareScope))compareScope=saved.compareScope;
  if(['source','root','custom'].includes(saved.outputMode))outputMode=saved.outputMode;
  if(typeof saved.customOutput==='string')customOutput=saved.customOutput;
  if(Number.isFinite(Number(saved.cullStrictness)))cullStrictness=Math.min(1.6,Math.max(.6,Number(saved.cullStrictness)));
  if(typeof saved.cullAdaptive==='boolean')cullAdaptive=saved.cullAdaptive;
  if(typeof saved.cullRescue==='boolean')cullRescue=saved.cullRescue;
  if(Number.isFinite(Number(saved.dedupThreshold)))dedupThreshold=Math.min(.95,Math.max(.5,Number(saved.dedupThreshold)));
  if(['both','raw','jpg'].includes(saved.pairMode))pairMode=saved.pairMode;
  if(Number.isFinite(Number(saved.rankTopN)))rankTopN=Math.min(500,Math.max(1,Number(saved.rankTopN)));
  if(saved.weights&&typeof saved.weights==='object'){
    CATS.forEach(([k])=>{const v=Number(saved.weights[k]);if(Number.isFinite(v))weights[k]=Math.min(100,Math.max(0,v));});
  }
}catch(_){}
function saveLibrarySettings(){
  try{localStorage.setItem('pc-library-settings',JSON.stringify({
    recursive:recursiveScan,compareScope,outputMode,customOutput,
    cullStrictness,cullAdaptive,cullRescue,dedupThreshold,pairMode,rankTopN,weights
  }));}catch(_){}
}
function librarySettingsHTML(step){
  let h='<div class="settings-subtitle">📂 扫描范围</div>'
    +'<div class="wgroup"><select id="scanScope">'
    +'<option value="recursive">当前文件夹 + 所有子文件夹</option>'
    +'<option value="current">仅当前文件夹</option></select></div>';
  if(step==='dedup')h+='<div class="settings-subtitle">🔎 对比范围</div>'
    +'<div class="wgroup"><select id="compareScope">'
    +'<option value="folder">文件夹内对比</option>'
    +'<option value="global">全局对比</option></select></div>';
  h+='<details style="margin-top:10px"><summary style="cursor:pointer;font-size:11px;color:var(--muted)">高级设置</summary>'
    +'<div class="wgroup" style="margin-top:8px"><label>处理文件存放位置</label><select id="outputMode">'
    +'<option value="source">跟随原文件夹（推荐）</option>'
    +'<option value="root">统一放到所选大文件夹</option>'
    +'<option value="custom">自定义输出目录</option></select>'
    +'<input id="customOutput" type="text" placeholder="例如 D:\\照片筛选结果" style="margin-top:7px"></div></details>';
  return h;
}

function applyResultView(){
  const g=document.getElementById('gallery');
  if(!g)return;
  g.classList.remove('view-small','view-large','view-list');
  g.classList.add('view-'+resultView);
  document.querySelectorAll('#resultTools [data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===resultView));
  try{localStorage.setItem('pc-result-view',resultView);}catch(_){}
}
function updateResultTools(){
  const tools=document.getElementById('resultTools');
  const g=document.getElementById('gallery');
  const hasGroups=!!g.querySelector('.folder-group');
  tools.style.display=hasGroups?'flex':'none';
  applyResultView();
}
function isFolderCollapsed(key){
  return defaultFoldersCollapsed ? !expandedFolders.has(key) : collapsedFolders.has(key);
}
function setAllFolders(collapsed){
  defaultFoldersCollapsed=collapsed;
  collapsedFolders.clear();
  expandedFolders.clear();
  if(currentStep==='cull')renderCullStep(photos);
  else if(currentStep==='dedup')renderDedupGroups(photos);
  else renderRank(photos);
}
document.getElementById('expandAllBtn').onclick=()=>setAllFolders(false);
document.getElementById('collapseAllBtn').onclick=()=>setAllFolders(true);
document.querySelectorAll('#resultTools [data-view]').forEach(btn=>btn.onclick=()=>{
  resultView=btn.dataset.view;applyResultView();
});
document.getElementById('gallery').addEventListener('click',e=>{
  const head=e.target.closest('.folder-head');
  if(!head)return;
  if(e.target.closest('button.delete-btn'))return;
  const group=head.closest('.folder-group');if(!group)return;
  const key=decodeURIComponent(group.dataset.folder||'');
  const next=!group.classList.contains('collapsed');
  group.classList.toggle('collapsed',next);
  if(defaultFoldersCollapsed){
    if(next)expandedFolders.delete(key);else expandedFolders.add(key);
  }else{
    if(next)collapsedFolders.add(key);else collapsedFolders.delete(key);
  }
  const btn=group.querySelector('.fold-btn');if(btn)btn.textContent=next?'展开':'收起';
  // Re-render so collapsed folders do not keep thousands of hidden thumbnail nodes.
  if(currentStep==='cull')renderCullStep(photos);
  else if(currentStep==='dedup')renderDedupGroups(photos);
  else renderRank(photos);
});

function loadActivity(){
  fetch('/api/activity?limit=120').then(r=>r.json()).then(d=>{
    const box=document.getElementById('activityLog'),items=d.items||[];
    if(!items.length){box.textContent='暂无记录';return;}
    const actionName={cull:'清晰度分析',dedup:'相似分析',rank:'精选评分'};
    box.innerHTML=items.map(x=>{
      const dt=new Date((x.ts||0)*1000);
      const t=dt.toLocaleString('zh-CN',{hour12:false});
      const name=(x.path||'').split(/[\\/]/).pop();
      const action=actionName[x.action]||x.action||'系统记录';
      return '<div class="activity-item"><b>'+escHtml(action)+'</b><span style="float:right;color:var(--muted)">'+escHtml(t)+'</span>'
        +(name?'<br><span>'+escHtml(name)+'</span>':'')
        +(x.detail?'<br><span>'+escHtml(x.detail)+'</span>':'')+'</div>';
    }).join('');
  }).catch(()=>{});
}
document.getElementById('activityRefresh').onclick=loadActivity;
loadActivity();

/* Gallery thumbnail zoom: Ctrl + wheel changes photo-card size, never page zoom. */
let thumbSize=260;
try{
  const saved=parseInt(localStorage.getItem('pc-thumb-size-v160')||'260',10);
  if(Number.isFinite(saved))thumbSize=Math.min(420,Math.max(160,saved));
}catch(_){}
function applyThumbSize(v){
  thumbSize=Math.min(420,Math.max(160,Math.round(v/10)*10));
  document.documentElement.style.setProperty('--thumb-size',thumbSize+'px');
  const range=document.getElementById('thumbSizeRange');
  const label=document.getElementById('thumbSizeValue');
  if(range)range.value=String(thumbSize);
  if(label)label.textContent=String(thumbSize);
  try{localStorage.setItem('pc-thumb-size-v160',String(thumbSize));}catch(_){}
}
applyThumbSize(thumbSize);
document.getElementById('thumbSizeRange').oninput=e=>applyThumbSize(Number(e.target.value));
document.getElementById('thumbSmaller').onclick=()=>applyThumbSize(thumbSize-20);
document.getElementById('thumbLarger').onclick=()=>applyThumbSize(thumbSize+20);
document.querySelector('.main').addEventListener('wheel',e=>{
  if(!e.ctrlKey||document.getElementById('lightbox').classList.contains('open'))return;
  e.preventDefault();
  applyThumbSize(thumbSize+(e.deltaY<0?20:-20));
},{passive:false});

const WORKSPACE_COPY={
  cull:['模糊废片','快速复核模糊与失焦照片，后台分析不会打断当前操作。'],
  dedup:['相似照片','删除不需要的重复照片，完成的分组会保留复核记录。'],
  trash:['回收站','最后一轮复核：恢复误删照片或执行永久删除。'],
  rank:['照片评分 / 精选推荐','工具箱扩展功能，不参与默认照片清理流程。']
};
function updateWorkspaceHeading(){
  const c=WORKSPACE_COPY[currentStep]||WORKSPACE_COPY.cull;
  document.getElementById('workspaceTitle').textContent=c[0];
  document.getElementById('workspaceHint').textContent=c[1];
}
const sidebarCollapse=document.getElementById('sidebarCollapse');
function setSidebarCollapsed(on){
  document.body.classList.toggle('sidebar-collapsed',!!on);
  sidebarCollapse.textContent=on?'⇥':'⇤ 收起侧栏';
  try{localStorage.setItem('pc-sidebar-collapsed',on?'1':'0');}catch(_){}
}
try{setSidebarCollapsed(localStorage.getItem('pc-sidebar-collapsed')==='1');}catch(_){}
sidebarCollapse.onclick=()=>setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
function nativeWindow(action){
  const api=window.pywebview&&window.pywebview.api;
  if(api&&api.window_action)return api.window_action(action).catch(()=>false);
  return Promise.resolve(false);
}
document.getElementById('winMin').onclick=()=>nativeWindow('minimize');
document.getElementById('winMax').onclick=()=>nativeWindow('toggle_maximize');
document.getElementById('winClose').onclick=()=>nativeWindow('close');
setTimeout(()=>{
  if(!(window.pywebview&&window.pywebview.api)){
    const controls=document.querySelector('.window-controls');
    if(controls)controls.style.display='none';
  }
},1200);

const sidebarUtilityHost=document.getElementById('sidebarUtilityHost');
const inspectorPanel=document.getElementById('inspector');
const toolboxPanel=document.getElementById('toolboxPanel');
const activityPanel=document.getElementById('activityPanel');
const taskCenter=document.getElementById('taskCenter');
[inspectorPanel,taskCenter,activityPanel,toolboxPanel].forEach(panel=>{
  if(panel)sidebarUtilityHost.appendChild(panel);
});
if(inspectorPanel)inspectorPanel.setAttribute('aria-hidden','false');
function focusSidebarPanel(panel){
  if(document.body.classList.contains('sidebar-collapsed'))setSidebarCollapsed(false);
  if(panel)panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function setInspectorOpen(on){
  if(on){
    const d=document.getElementById('settingsDetails');if(d)d.open=true;
    loadStorageSummary(true);
    focusSidebarPanel(inspectorPanel);
  }
}
document.getElementById('settingsQuick').onclick=()=>setInspectorOpen(true);
document.getElementById('inspectorClose').onclick=()=>{};
document.getElementById('drawerScrim').onclick=()=>{};
document.getElementById('toolboxOpen').onclick=()=>focusSidebarPanel(toolboxPanel);
document.getElementById('toolboxClose').onclick=()=>{};
document.getElementById('taskToggle').onclick=()=>focusSidebarPanel(taskCenter);
document.getElementById('taskClose').onclick=()=>{};
document.getElementById('openRankTool').onclick=()=>{
  activateStep('rank');
  fetch('/api/progress/rank').then(r=>r.json()).then(d=>{renderRank(d.photos||[]);updateVisibleStepStatus('rank',d);}).catch(()=>{});
};
document.getElementById('appExit').onclick=async()=>{
  let activeSteps=[],fileTasks=0;
  try{
    const [rows,tasks]=await Promise.all([
      Promise.all(['cull','dedup','rank'].map(step=>
        fetch('/api/progress/'+step).then(r=>r.json()).then(d=>({step,running:!!d.running}))
      )),
      fetch('/api/tasks').then(r=>r.json()).catch(()=>({active:0}))
    ]);
    activeSteps=rows.filter(x=>x.running).map(x=>x.step);
    fileTasks=Math.max(0,Number(tasks.active)||0);
  }catch(_){
    if(coreRunning)activeSteps.push('cull','dedup');
    if(isRunning&&runningStep)activeSteps.push(runningStep);
    activeSteps=[...new Set(activeSteps)];
  }
  const activeAny=activeSteps.length>0;
  let msg='确定退出 PhotoCurator 吗？';
  if(activeAny&&fileTasks){
    msg='当前仍有分析任务在运行，并有 '+fileTasks+' 个后台文件任务。退出前会先停止分析；未完成的文件任务已持久化，将在下次启动后自动继续。';
  }else if(activeAny){
    msg='当前仍有分析任务在运行。退出前将先发送停止请求；已完成的分析结果会继续保留。';
  }else if(fileTasks){
    msg='当前还有 '+fileTasks+' 个后台文件任务。未完成任务已持久化，退出后会在下次启动自动继续。确定退出吗？';
  }
  const ok=await askBatchConfirm('退出 PhotoCurator',msg,activeAny?'停止并退出':'退出');
  if(!ok)return;
  if(activeAny){
    await Promise.allSettled(activeSteps.map(step=>fetch('/api/stop/'+step,{method:'POST'})));
    const deadline=Date.now()+3500;
    while(Date.now()<deadline){
      try{
        const rows=await Promise.all(activeSteps.map(step=>fetch('/api/progress/'+step).then(r=>r.json())));
        if(rows.every(d=>!d.running))break;
      }catch(_){break;}
      await new Promise(resolve=>setTimeout(resolve,180));
    }
  }
  nativeWindow('exit');
};
function taskLabel(d){
  if(!d)return '待开始';
  if(d.src_folder&&folder&&!sameFolder(d.src_folder,folder))return '待开始';
  if(d.running)return Math.max(0,Math.min(100,Number(d.progress)||0))+'% · 处理中';
  const st=String(d.status||'');
  if(st.includes('错误')||st.includes('失败'))return '需要处理';
  if(st.includes('停止'))return '已停止';
  if((d.progress||0)>=100||st.startsWith('完成')||st.includes('筛选完成'))return '已完成';
  return st&&st!=='待开始'?'待继续':'待开始';
}
let lastBackgroundActive=0,lastBackgroundFailed=-1,lastAutoSyncAt=0;
function fileTaskLabel(kind){
  return ({move_to_trash:'移入软件回收站',permanent_delete:'永久删除',
    restore_trash:'恢复照片',purge_trash:'清理软件回收站'})[kind]||'文件操作';
}
async function refreshTaskCenter(){
  try{
    const [rows,tasks]=await Promise.all([
      Promise.all(['cull','dedup','rank'].map(k=>fetch('/api/progress/'+k).then(r=>r.json()).catch(()=>null))),
      fetch('/api/tasks').then(r=>r.json()).catch(()=>({active:0,counts:{}}))
    ]);
    document.getElementById('taskCull').textContent=taskLabel(rows[0]);
    document.getElementById('taskDedup').textContent=taskLabel(rows[1]);
    document.getElementById('taskRank').textContent=taskLabel(rows[2]);
    const counts=tasks.counts||{};
    const analysis=rows.some(x=>x&&x.running),queued=Math.max(0,Number(tasks.active)||0);
    const failed=Math.max(0,Number(counts.failed)||0);
    const fileStatus=document.getElementById('taskFiles');
    const fileHint=document.getElementById('taskFileHint');
    const latestFailed=(tasks.items||[]).find(x=>x&&x.state==='failed');
    if(queued){
      fileStatus.textContent=queued+' 个处理中 / 待处理';
    }else if(failed){
      fileStatus.textContent=failed+' 个失败';
    }else{
      fileStatus.textContent='空闲';
    }
    if(latestFailed){
      const err=String(latestFailed.error||'未知错误').replace(/\s+/g,' ').slice(0,120);
      fileHint.textContent='最近失败：'+fileTaskLabel(latestFailed.kind)+' · '+err;
    }else{
      fileHint.textContent='删除、恢复和永久删除在持久化后台队列中执行；异常退出后未完成任务会在下次启动继续。';
    }
    document.getElementById('taskToggle').textContent=failed?'!':((analysis||queued)?'●':'◉');
    document.getElementById('taskToggle').title=failed
      ?'有 '+failed+' 个文件任务失败 · 打开任务中心查看'
      :((analysis||queued)?'后台运行中 · '+queued+' 个文件任务':'后台任务空闲');
    if(failed>0&&failed!==lastBackgroundFailed){
      toast('有 '+failed+' 个后台文件任务失败，请打开任务中心查看','bad');
    }
    if(lastBackgroundActive>0&&queued===0&&Date.now()-lastAutoSyncAt>800){
      lastAutoSyncAt=Date.now();syncCurrentView();
    }
    lastBackgroundActive=queued;
    lastBackgroundFailed=failed;
  }catch(_){}
}
setInterval(refreshTaskCenter,1400);
window.addEventListener('focus',()=>{refreshTaskCenter();syncCurrentView();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshTaskCenter();syncCurrentView();}});
refreshTaskCenter();

/* settings panels per step */
function settingsHTML(step){
  if(step==='cull') return `<div class="wgroup"><label>筛选严格度 <b id="optVal">1.00</b></label>
      <input type="range" id="opt" min="0.6" max="1.6" step="0.05" value="1.0">
      <div class="slider-value">数值越低保留越多，越高筛选越严格</div></div>
      <label class="check"><input type="checkbox" id="cAdaptive" checked> 根据本次照片自动适应阈值</label>
      <label class="check" style="margin-top:6px"><input type="checkbox" id="cRescue" checked> 质量保护：保留轻微软但构图优秀的照片</label>${librarySettingsHTML(step)}`;
  if(step==='dedup') return `<div class="wgroup"><label>相似度阈值</label>
      <input type="range" id="opt" min="0.5" max="0.95" step="0.05" value="0.8">
      <div class="slider-value">相似度达到或高于 <b id="optVal">0.80</b> 时归为一组 · 数值越低合并越激进</div></div>
      <div class="wgroup" style="margin-top:10px"><label>RAW + JPG 同帧照片（原片与预览图）</label>
      <select id="pairMode">
        <option value="both">两者都保留</option>
        <option value="raw">仅保留 RAW 原片</option>
        <option value="jpg">仅保留 JPG 图片</option>
      </select>
      <div class="slider-value">同名 RAW/JPG（如 IMG_0001.CR2 + .JPG）会在去重前合并为一张</div></div>
      <div class="slider-value" style="margin-top:10px;line-height:1.55">先完成筛选和分组，不会立即移动文件。筛选后可逐组对比并切换“保留”照片，最后再统一确认处理。</div>${librarySettingsHTML(step)}`;
  if(step==='trash') return `<div class="panel-box">
      <div style="font-size:12px;font-weight:700;margin-bottom:6px">🗑️ 软件回收站 · 最后一轮复核</div>
      <div class="slider-value" style="line-height:1.6">这里的照片尚未永久删除。可以再次查看大图并决定“恢复原位”或“永久删除”。永久删除后不可从 PhotoCurator 恢复。</div>
    </div>`;
  // rank
  return `<div class="sidebar-title" style="margin-bottom:4px">⚖️ 评分权重</div>
    <div class="panel-box" id="weightPanel"></div>
    <button class="btn-ghost" id="resetWeights" style="margin-top:8px">↺ 恢复推荐权重</button>
    <div class="wgroup" style="margin-top:10px"><label>候选展示数量</label><input type="number" id="topn" min="1" max="500" value="50">
      <div class="slider-value">仅决定筛选完成后首轮展示多少张候选照片，不会直接移动、删除或导出文件。筛选后可继续手动移除 / 恢复，再确认导出。</div></div>${librarySettingsHTML(step)}`;
}
function renderWeights(){
  const wp=document.getElementById('weightPanel'); if(!wp)return;
  wp.innerHTML=CATS.map(([k,lab])=>`<div class="wgroup"><label>${lab} <b id="wv_${k}">${weights[k]}</b></label>
     <input type="range" min="0" max="50" value="${weights[k]}" id="w_${k}"></div>`).join('');
  CATS.forEach(([k])=>{const el=document.getElementById('w_'+k);
    el.oninput=()=>{weights[k]=parseInt(el.value);document.getElementById('wv_'+k).textContent=el.value;saveLibrarySettings();scheduleReweight();};});
}
function scheduleReweight(){clearTimeout(weightTimer);weightTimer=setTimeout(()=>{
  fetch('/api/weights',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({weights,topn:rankTopN})})
    .then(r=>r.json()).then(d=>{if(currentStep==='rank')renderRank(d.photos||[]);});},140);}
function applyStepStats(){
  // Show only the stat rows relevant to the current step so irrelevant zeros
  // (e.g. Sharp/Blurry/Unique while Ranking) don't look like errors.
  document.querySelectorAll('.stat-row[data-steps]').forEach(row=>{
    row.style.display=row.dataset.steps.split(' ').includes(currentStep)?'':'none';
  });
}
function updateVisibleStepStatus(step,d){
  if(step!==currentStep)return;
  const st=d.stats||{};
  document.getElementById('progressWrap').style.display='block';
  document.getElementById('progressFill').style.width=(d.progress||0)+'%';
  document.getElementById('progressText').textContent=d.status||'';
  if('images'in st)document.getElementById('sImages').textContent=st.images;
  if('sharp'in st)document.getElementById('sSharp').textContent=st.sharp;
  if('blurry'in st)document.getElementById('sBlurry').textContent=st.blurry;
  if('soft'in st)document.getElementById('sSoft').textContent=st.soft;
  if(st.folder_status)folderStatus=st.folder_status;
  if('duplicate_groups'in st)document.getElementById('sGroups').textContent=st.duplicate_groups;
  else if('groups'in st)document.getElementById('sGroups').textContent=st.groups;
}
function renderSettings(){
  applyStepStats();
  document.getElementById('settingsPanel').innerHTML=settingsHTML(currentStep);
  const opt=document.getElementById('opt'),val=document.getElementById('optVal');
  if(opt&&val){
    opt.value=currentStep==='cull'?cullStrictness:dedupThreshold;
    val.textContent=parseFloat(opt.value).toFixed(2);
    opt.oninput=()=>{
      const v=parseFloat(opt.value);
      if(currentStep==='cull')cullStrictness=v; else if(currentStep==='dedup')dedupThreshold=v;
      val.textContent=v.toFixed(2);saveLibrarySettings();
    };
  }
  const ad=document.getElementById('cAdaptive'),rs=document.getElementById('cRescue');
  if(ad){ad.checked=cullAdaptive;ad.onchange=()=>{cullAdaptive=ad.checked;saveLibrarySettings();};}
  if(rs){rs.checked=cullRescue;rs.onchange=()=>{cullRescue=rs.checked;saveLibrarySettings();};}
  const pm=document.getElementById('pairMode');
  if(pm){pm.value=pairMode;pm.onchange=()=>{pairMode=pm.value;saveLibrarySettings();};}
  const ss=document.getElementById('scanScope');
  if(ss){ss.value=recursiveScan?'recursive':'current';ss.onchange=()=>{recursiveScan=ss.value==='recursive';saveLibrarySettings();};}
  const cs=document.getElementById('compareScope');
  if(cs){cs.value=compareScope;cs.onchange=()=>{compareScope=cs.value;saveLibrarySettings();};}
  const om=document.getElementById('outputMode'),co=document.getElementById('customOutput');
  if(om){
    om.value=outputMode;
    om.onchange=()=>{outputMode=om.value;if(co)co.style.display=outputMode==='custom'?'block':'none';saveLibrarySettings();};
  }
  if(co){
    co.value=customOutput;
    co.style.display=outputMode==='custom'?'block':'none';
    co.oninput=()=>{customOutput=co.value;saveLibrarySettings();};
  }
  if(currentStep==='rank'){
    renderWeights();
    const tn=document.getElementById('topn');
    if(tn){tn.value=rankTopN;tn.onchange=()=>{rankTopN=Math.min(500,Math.max(1,parseInt(tn.value)||50));tn.value=rankTopN;saveLibrarySettings();scheduleReweight();};}
    const rw=document.getElementById('resetWeights');if(rw)rw.onclick=()=>{weights={...DEFAULTS};saveLibrarySettings();renderWeights();scheduleReweight();};
  }
}

/* Switch the visible step (used by tab clicks AND God mode). */
function activateStep(step){
  currentStep=step;
  document.querySelectorAll('.step').forEach(x=>x.classList.toggle('active',x.dataset.step===step));
  updateWorkspaceHeading();
  renderSettings();
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.remove('cta');}
  document.getElementById('dedupApplyBtn').style.display='none';
  document.getElementById('progressWrap').style.display='none';  // clear stale summary
  document.getElementById('resultTools').style.display='none';
  if(!photos.length&&!isRunning&&!coreRunning&&!catalogRootView){
    renderWorkspaceLanding();
  }else{
    showPhotoView();
    document.getElementById('gallery').innerHTML=emptyHTML(currentStep);
  }
  lastRankSig='';lastStep=null;
  gPage=0;lastGallerySig='';gItems=[];document.getElementById('pager').style.display='none';
  setupFilterBar();
  if(step==='cull')updateCullMoveButton();
}
/* step tabs (blocked while a step is running) */
document.querySelectorAll('.step').forEach(t=>t.onclick=()=>{
  activateStep(t.dataset.step);
  if(currentStep==='trash'){loadTrash();return;}
  fetch('/api/progress/'+currentStep).then(r=>r.json()).then(d=>{
    if(d.src_folder && folder && !sameFolder(d.src_folder,folder)){
      showPhotoView();
      document.getElementById('gallery').innerHTML=emptyHTML(currentStep);
      document.getElementById('progressWrap').style.display='none';
      return;
    }
    if(currentStep==='cull')renderCullStep(cullRowsForPayload(d));
    else if(currentStep==='dedup'){
      const st=d.stats||{};
      dedupStatusCounts={
        pending:Number(st.pending_groups||0),reviewed:Number(st.reviewed_groups||0),updated:Number(st.updated_groups||0)
      };
      loadDedupPage(true);
    }else renderRank(d.photos||[]);
    updateVisibleStepStatus(currentStep,d);
    if(currentStep==='cull')maybeLoadAllCull(d);
  }).catch(()=>{});
});
renderSettings();

/* cull filter chips */
let cullFilter='all', cullType='all', rankFilter='all', lastFmtSig='';
function setupFilterBar(){
  const bar=document.getElementById('filterBar');
  if(currentStep==='cull'){
    if(!photos.length && !coreRunning && !isRunning){
      bar.style.display='none';
      bar.innerHTML='';
      return;
    }
    const opts=[['all','全部'],['sharp','清晰'],['soft','轻微软 ★'],['blurry','模糊']];
    // Per-format chips (NEF, CR2, ARW, ...) built from what's actually loaded.
    const rawFmts=[...new Set(photos.filter(p=>p.raw).map(p=>p.fmt||'RAW'))].sort();
    const hasHeic=photos.some(p=>p.heic);
    const types=[['all','全部格式'],['raw','仅 RAW'],['standard','普通图片'],
      ...(hasHeic?[['heic','仅 HEIC']]:[]),
      ...(rawFmts.length>1?rawFmts.map(f=>['ext:'+f.toLowerCase(),'仅 '+f]):[])];
    if(!types.some(([k])=>k===cullType))cullType='all';
    bar.style.display='flex';
    const blurry=photos.filter(p=>p.tier==='blurry'&&!['pending_trash','pending_permanent_delete','trashed','permanently_deleted'].includes(p.lifecycle));
    const moveSelected=blurry.filter(p=>p.move_selected!==false).length;
    bar.innerHTML=opts.map(([k,l])=>`<button class="chip${k===cullFilter?' active':''}" data-f="${k}">${l}</button>`).join('')
      +`<span class="chip-sep"></span>`
      +types.map(([k,l])=>`<button class="chip${k===cullType?' active':''}" data-t="${k}">${l}</button>`).join('')
      +(blurry.length?`<span class="chip-sep"></span><span class="move-summary">待删除 <b id="cullMoveCount">${moveSelected}/${blurry.length}</b></span><button class="chip move-bulk" id="moveSelAll">全选</button><button class="chip move-bulk" id="moveSelNone">全不选</button>`:'')
      +`<span class="chip-sep"></span><button class="chip" id="cullLoadMore" style="display:none"></button>`;
    bar.querySelectorAll('.chip[data-f]').forEach(c=>c.onclick=()=>{cullFilter=c.dataset.f;gPage=0;
      bar.querySelectorAll('.chip[data-f]').forEach(x=>x.classList.toggle('active',x.dataset.f===cullFilter));
      renderCullStep(photos);});
    bar.querySelectorAll('.chip[data-t]').forEach(c=>c.onclick=()=>{cullType=c.dataset.t;gPage=0;
      bar.querySelectorAll('.chip[data-t]').forEach(x=>x.classList.toggle('active',x.dataset.t===cullType));
      renderCullStep(photos);});
    const ma=document.getElementById('moveSelAll'),mn=document.getElementById('moveSelNone');
    if(ma)ma.onclick=()=>setAllBlurryMoveSelection(true);
    if(mn)mn.onclick=()=>setAllBlurryMoveSelection(false);
    const more=document.getElementById('cullLoadMore');if(more)more.onclick=()=>loadCullPage(false);
    updateCullMoveButton();updateCullLoadMore();
    return;
  }
  if(currentStep==='dedup'){
    bar.style.display='flex';
    const counts=dedupStatusCounts||{};
    const opts=[['pending','待筛选'],['reviewed','已筛选'],['updated','新增待复核']];
    bar.innerHTML=opts.map(([k,l])=>`<button class="chip${dedupStatusFilter===k?' active':''}" data-dstatus="${k}">${l} <span>${Number(counts[k]||0)}</span></button>`).join('')
      +'<span class="chip-sep"></span><button class="chip" id="dedupLoadMore" style="display:none"></button>';
    bar.querySelectorAll('[data-dstatus]').forEach(b=>b.onclick=()=>{dedupStatusFilter=b.dataset.dstatus;loadDedupPage(true);});
    const more=document.getElementById('dedupLoadMore');if(more)more.onclick=()=>loadDedupPage(false);
    updateDedupLoadMore();
    return;
  }
  if(currentStep==='rank'){
    if(!photos.length){bar.style.display='none';return;}
    bar.style.display='flex';
    bar.innerHTML=`<button class="chip${rankFilter==='all'?' active':''}" data-f="all">全部优选照片</button>`
      +`<button class="chip${rankFilter==='pbg'?' active':''}" data-f="pbg">📱 手机壁纸 (<span id="pbgChipCount">0</span>)</button>`;
    bar.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{rankFilter=c.dataset.f;gPage=0;
      bar.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.f===rankFilter));
      lastRankSig='';renderRank(photos);});
    return;
  }
  if(currentStep==='trash'){
    bar.style.display='flex';
    bar.innerHTML=`<span class="move-summary">最后复核 <b>${photos.length}</b> 张</span>
      <button class="chip move-bulk" id="trashRestoreAll">全部恢复</button>
      <button class="chip move-bulk" id="trashPurgeAll">清空回收站</button>`;
    const ra=document.getElementById('trashRestoreAll');
    const pa=document.getElementById('trashPurgeAll');
    if(ra)ra.onclick=trashRestoreAll;
    if(pa)pa.onclick=trashPurgeAll;
    return;
  }
  bar.style.display='none';
}
setupFilterBar();
document.getElementById('gallery').innerHTML=emptyHTML(currentStep);  // step explainer on load

function normalizedFolder(p){
  return String(p||'').trim().replace(/\\/g,'/').replace(/\/+$/,'').toLowerCase();
}
function sameFolder(a,b){return normalizedFolder(a)===normalizedFolder(b);}
function resetWorkspaceForFolder(){
  cullChunkToken++;
  cullLiveStore.clear();dedupLiveStore.clear();
  cullReady=false;
  photos=[];lbList=[];folderStatus={};
  lastRankSig='';lastCullSig='';lastCullMoveSig='';lastGallerySig='';
  gItems=[];gPage=0;
  ['sImages','sSharp','sSoft','sBlurry','sGroups','sShowing','sTrash'].forEach(id=>{
    const el=document.getElementById(id);if(el)el.textContent='0';
  });
  document.getElementById('statusShowing').textContent='0';
  document.getElementById('progressWrap').style.display='none';
  document.getElementById('filterBar').style.display='none';
  document.getElementById('resultTools').style.display='none';
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  document.getElementById('moveBlurryBtn').style.display='none';
  document.getElementById('dedupApplyBtn').style.display='none';
  renderWorkspaceLanding();
}
function updateStartAvailability(){
  if(!startBtn)return;
  if(isRunning||coreRunning){
    startBtn.disabled=false;
    return;
  }
  const ready=!!String(folder||'').trim();
  startBtn.disabled=!ready;
  startBtn.setAttribute('aria-disabled',ready?'false':'true');
  startBtn.title=ready?'开始分析当前照片文件夹':'请先选择照片文件夹';
}
function sourceForPath(value){
  const wanted=normalizedFolder(value);
  for(const source of sourceCatalog||[]){
    for(const root of source.roots||[]){
      if(sameFolder(root.current_root,wanted)||sameFolder(root.original_root,wanted)){
        return {...source,root};
      }
    }
  }
  return null;
}
function updateSourceUi(){
  selectedSource=folder?sourceForPath(folder):null;
  const offline=catalogRootView&&catalogRootView.source?catalogRootView.source:null;
  const name=document.getElementById('topSourceName');
  const path=document.getElementById('topSourcePath');
  const topDot=document.getElementById('topSourceDot');
  const statusDot=document.getElementById('statusSourceDot');
  const statusText=document.getElementById('statusSourceText');
  const active=selectedSource||offline;
  const connected=!!(active&&active.connected);
  if(folder){
    name.textContent=selectedSource?selectedSource.display_name:'当前照片来源';
    path.textContent=folder;
    statusText.textContent=(selectedSource?(selectedSource.display_name+' · '):'')+(connected?'已连接':'路径已选择');
  }else if(offline){
    name.textContent=offline.display_name||'离线图库';
    path.textContent=(catalogRootView.root&&catalogRootView.root.original_root)||'原始位置未连接';
    statusText.textContent=(offline.display_name||'数据源')+' · 未连接 · 历史数据可查看';
  }else{
    name.textContent='未选择数据源';
    path.textContent='添加硬盘、U盘或照片文件夹后开始';
    statusText.textContent='数据源未连接';
  }
  [topDot,statusDot].forEach(dot=>{
    dot.classList.toggle('online',connected);
    dot.classList.toggle('offline',!connected);
  });
  if(!isRunning&&!coreRunning&&!photos.length&&!catalogRootView)renderWorkspaceLanding();
}
function selectFolderValue(value){
  const next=String(value||'').trim();
  if(next===folder){updateStartAvailability();updateSourceUi();return;}
  folder=next||null;
  catalogRootView=null;
  resetWorkspaceForFolder();
  updateStartAvailability();
  updateSourceUi();
}

function fmtDate(ts){
  const n=Number(ts)||0;
  if(!n)return '尚未扫描';
  try{return new Date(n*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});}catch(_){return '已有记录';}
}
function sourceTotals(){
  let roots=0,indexed=0,online=0,offline=0;
  for(const source of sourceCatalog||[]){
    if(source.connected)online++;else offline++;
    for(const root of source.roots||[]){
      roots++;indexed+=Number(root.photo_count||0);
    }
  }
  return {roots,indexed,online,offline,total:(sourceCatalog||[]).length};
}
function currentRootInfo(){
  if(!folder)return null;
  const match=sourceForPath(folder);
  return match&&match.root?match:null;
}
function overviewSourceRows(){
  const rows=[];
  for(const source of sourceCatalog||[]){
    for(const root of source.roots||[]){
      rows.push({source,root});
    }
  }
  rows.sort((a,b)=>{
    const ac=a.source.connected?1:0,bc=b.source.connected?1:0;
    if(ac!==bc)return bc-ac;
    return Number(b.root.last_scan_at||0)-Number(a.root.last_scan_at||0);
  });
  return rows.slice(0,5);
}
function workspaceOverviewHTML(){
  const t=sourceTotals();
  const rows=overviewSourceRows();
  const storage=latestStorageSummary;
  const storageText=key=>storage?formatBytes(storage[key]||0):'按需查看';
  const recent=rows.length?rows.map(({source,root})=>{
    const current=root.current_root||root.original_root||'';
    const canOpen=!!source.connected;
    return '<div class="overview-source-row">'
      +'<span class="source-dot '+(canOpen?'online':'offline')+'"></span>'
      +'<div class="copy"><b>'+escHtml(root.display_name||source.display_name||'照片库')+'</b>'
      +'<small>'+escHtml(source.display_name||'数据源')+' · '+Number(root.photo_count||0)+' 张 · '+(canOpen?'已连接':'未连接')+'</small></div>'
      +'<button data-overview-root="'+escHtml(root.root_id||'')+'" data-overview-path="'+escHtml(current)+'" data-overview-connected="'+(canOpen?'1':'0')+'">'+(canOpen?'打开':'看历史')+'</button>'
      +'</div>';
  }).join(''):'<div class="source-empty">还没有建立过图库索引，先添加一个数据源。</div>';

  return '<div class="workspace-overview">'
    +'<div class="overview-hero">'
      +'<section class="overview-card overview-primary"><div class="overview-primary-copy">'
      +'<div class="eyebrow">PHOTO LIBRARY</div><h2>照片整理工作台</h2>'
      +'<p>连接硬盘、U盘或照片文件夹后，PhotoCurator 会保留来源、扫描结果和人工复核记录。设备断开后，历史图库仍然可查。</p>'
      +'</div><div class="overview-primary-actions">'
      +'<button class="primary" data-overview-action="add">＋ 添加数据源</button>'
      +(demoShortcutPath?'<button data-overview-action="demo">'+(demoShortcutReady?'打开演示图库':'准备演示图库')+'</button>':'')
      +'</div></section>'
      +'<section class="overview-card overview-metrics">'
      +'<div class="metric"><span>数据源</span><b>'+t.total+'</b><small>'+t.online+' 已连接 · '+t.offline+' 未连接</small></div>'
      +'<div class="metric"><span>已索引照片</span><b>'+t.indexed.toLocaleString('zh-CN')+'</b><small>'+t.roots+' 个图库根目录</small></div>'
      +'<div class="metric"><span>图库数据库</span><b>'+storageText('database_bytes')+'</b><small>分析与人工复核记录</small></div>'
      +'<div class="metric"><span>离线预览</span><b>'+storageText('persistent_preview_bytes')+'</b><small>拔盘后仍可浏览</small></div>'
      +'</section></div>'
    +'<div class="overview-grid">'
      +'<section class="overview-card overview-section sources"><h3>最近图库</h3><p>在线数据源优先，离线数据源仍保留历史。</p><div class="overview-list">'+recent+'</div></section>'
      +'<section class="overview-card overview-section storage"><h3>软件占用</h3><p>持久数据与可清理缓存分开显示。</p><div class="overview-storage-list">'
      +'<div class="overview-storage-row"><span>数据库</span><b>'+storageText('database_bytes')+'</b></div>'
      +'<div class="overview-storage-row"><span>离线预览</span><b>'+storageText('persistent_preview_bytes')+'</b></div>'
      +'<div class="overview-storage-row"><span>临时缓存</span><b>'+storageText('preview_cache_bytes')+'</b></div>'
      +'<div class="overview-storage-row"><span>相似特征</span><b>'+storageText('dedup_feature_bytes')+'</b></div>'
      +'</div></section>'
      +'<section class="overview-card overview-section guide"><h3>整理流程</h3><p>常用流程一直可见，高级参数只在“筛选”抽屉里出现。</p>'
      +'<div class="workflow-guide">'
      +'<div class="guide-step"><b>1 · 连接数据源</b><small>识别硬盘/U盘身份，建立可追溯图库。</small></div>'
      +'<div class="guide-step"><b>2 · 开始分析</b><small>默认递归子文件夹，同时执行清晰度与相似分析。</small></div>'
      +'<div class="guide-step"><b>3 · 人工复核</b><small>先筛选、再处理；删除默认进入软件回收站。</small></div>'
      +'</div></section>'
      +'<section class="overview-card overview-section demo"><h3>演示图库</h3><p>用于测试子目录、模糊识别和近似连拍。</p>'
      +'<div class="overview-storage-list"><div class="overview-storage-row"><span>测试照片</span><b>'+(demoShortcutReady?(Number(demoShortcutCount||36)+' 张'):'首次点击生成')+'</b></div>'
      +'<div class="overview-storage-row"><span>子文件夹</span><b>6 个</b></div>'
      +'<div class="overview-storage-row"><span>内容</span><b>清晰 / 模糊 / 连拍</b></div></div></section>'
      +'</div></div>';
}
function sourceReadyHTML(){
  const match=currentRootInfo();
  const source=match||selectedSource;
  const root=match&&match.root?match.root:null;
  const indexed=Number(root&&root.photo_count||0);
  const lastScan=Number(root&&root.last_scan_at||0);
  const connected=source?!!source.connected:true;
  return '<div class="source-ready">'
    +'<section class="overview-card source-ready-main"><div class="source-ready-head"><div>'
    +'<h2>'+escHtml(root&&root.display_name||source&&source.display_name||'当前照片来源')+'</h2>'
    +'<p>'+escHtml(folder||'')+'</p></div><span class="source-ready-badge"><span class="source-dot '+(connected?'online':'offline')+'"></span>'+(connected?'已连接':'路径已选择')+'</span></div>'
    +'<div class="source-ready-facts">'
    +'<div class="source-ready-fact"><span>已索引照片</span><b>'+indexed.toLocaleString('zh-CN')+' 张</b></div>'
    +'<div class="source-ready-fact"><span>上次扫描</span><b>'+escHtml(fmtDate(lastScan))+'</b></div>'
    +'<div class="source-ready-fact"><span>扫描范围</span><b>'+(recursiveScan?'当前文件夹 + 所有子文件夹':'仅当前文件夹')+'</b></div>'
    +'</div><div class="source-ready-actions">'
    +'<button class="primary" data-ready-action="start">▶ 开始分析</button>'
    +'<button data-ready-action="settings">筛选设置</button>'
    +'</div></section>'
    +'<aside class="overview-card source-ready-side"><h3>本次分析</h3>'
    +'<div class="source-ready-check"><span class="dot"></span><div><b>清晰度筛选</b><small>识别清晰、轻微软和明显模糊照片。</small></div></div>'
    +'<div class="source-ready-check"><span class="dot"></span><div><b>相似照片分组</b><small>识别连拍与近似照片，先复核再处理。</small></div></div>'
    +'<div class="source-ready-check"><span class="dot"></span><div><b>增量复用</b><small>已经扫描且没有变化的文件优先复用历史数据。</small></div></div>'
    +'</aside></div>';
}
async function openDemoLibrary(button=null){
  if(!demoShortcutPath)return;
  if(demoShortcutReady){
    selectFolderValue(demoShortcutPath);
    document.getElementById('folderInput').value=demoShortcutPath;
    return;
  }
  const old=button?button.innerHTML:'';
  if(button){
    button.disabled=true;
    button.textContent='正在准备演示图库…';
  }
  try{
    const r=await fetch('/api/demo-prepare?wait=1',{method:'POST'});
    const d=await r.json();
    if(!r.ok||!d.ready)throw new Error(d.error||'演示图库准备失败');
    demoShortcutPath=d.folder||demoShortcutPath;
    demoShortcutCount=Number(d.count||36);
    demoShortcutReady=true;
    selectFolderValue(demoShortcutPath);
    document.getElementById('folderInput').value=demoShortcutPath;
    loadShortcuts();
  }catch(err){
    if(button){button.innerHTML=old;button.disabled=false;}
    toast('演示图库准备失败：'+(err.message||'未知错误'),'bad');
  }
}
function bindWorkspaceLanding(){
  const g=document.getElementById('dashboardView');
  g.querySelectorAll('[data-overview-action="add"]').forEach(b=>b.onclick=()=>document.getElementById('browseBtn').click());
  g.querySelectorAll('[data-overview-action="demo"]').forEach(b=>b.onclick=()=>openDemoLibrary(b));
  g.querySelectorAll('[data-overview-root]').forEach(b=>b.onclick=()=>{
    if(b.dataset.overviewConnected==='1'){
      selectFolderValue(b.dataset.overviewPath);
      document.getElementById('folderInput').value=folder||'';
    }else if(b.dataset.overviewRoot){
      loadCatalogRoot(b.dataset.overviewRoot);
    }
  });
  const start=g.querySelector('[data-ready-action="start"]');
  if(start)start.onclick=()=>document.getElementById('startBtn').click();
  const settings=g.querySelector('[data-ready-action="settings"]');
  if(settings)settings.onclick=()=>document.getElementById('settingsQuick').click();
}
function showDashboard(html){
  const dashboard=document.getElementById('dashboardView');
  const photoView=document.getElementById('photoView');
  dashboard.innerHTML=html||'';
  dashboard.hidden=false;
  photoView.hidden=true;
}
function showPhotoView(){
  document.getElementById('dashboardView').hidden=true;
  document.getElementById('photoView').hidden=false;
}
function renderWorkspaceLanding(){
  if(isRunning||coreRunning||photos.length||catalogRootView)return;
  showDashboard(folder?sourceReadyHTML():workspaceOverviewHTML());
  bindWorkspaceLanding();
}

/* shortcuts */
function sdLabel(p){const parts=p.split(/[\\/]/).filter(Boolean);
  const tail=parts.slice(-2).join('/');
  const m=/^([A-Za-z]:)/.exec(p);return m?m[1]+' '+tail:tail;}
function formatBytes(n){
  n=Math.max(0,Number(n)||0);
  const units=['B','KB','MB','GB','TB'];let i=0;
  while(n>=1024&&i<units.length-1){n/=1024;i++;}
  return (i<2?n.toFixed(0):n.toFixed(1))+' '+units[i];
}
function formatSourceKind(kind){
  return ({removable:'U盘 / 移动设备',fixed:'磁盘',network:'网络存储',volume:'存储设备'})[kind]||'存储设备';
}
function renderSources(){
  const box=document.getElementById('sourcesList');
  const registered=(sourceCatalog||[]).map(source=>{
    const current=!!(selectedSource&&selectedSource.source_id===source.source_id);
    const open=source.connected||current;
    const state=source.connected?'已连接':'未连接 · 历史保留';
    const roots=(source.roots||[]).map(root=>{
      const path=root.current_root||root.original_root||'';
      const title=(root.display_name||path||'照片库')+' · '+Number(root.photo_count||0)+' 张';
      return '<button class="source-root-btn" data-source-root="'+escHtml(path)+'"'
        +' data-root-id="'+escHtml(root.root_id)+'" data-source-id="'+escHtml(source.source_id)+'"'
        +' data-connected="'+(source.connected?'1':'0')+'" title="'+escHtml(path)+'">'+escHtml(title)+'</button>';
    }).join('');
    return '<details class="source-card '+(source.connected?'connected':'offline')+(current?' current':'')+'" '+(open?'open':'')+'>'
      +'<summary><span class="source-dot '+(source.connected?'online':'offline')+'"></span>'
      +'<span class="source-summary-copy"><b>'+escHtml(source.display_name)+'</b>'
      +'<small>'+escHtml(formatSourceKind(source.kind))+' · '+escHtml(state)
      +(source.capacity_bytes?' · '+formatBytes(source.capacity_bytes):'')+'</small></span></summary>'
      +'<div class="source-roots">'+(roots||'<div class="source-empty">暂无已索引目录</div>')+'</div></details>';
  }).join('');
  const fresh=(discoveredDevices||[]).filter(d=>!d.known).map(d=>
    '<button class="source-root-btn discovered-device" data-discovered-path="'+escHtml(d.mount_path||'')+'">'
    +'<b>＋ '+escHtml(d.display_name||d.mount_path||'新存储设备')+'</b>'
    +'<span>已连接 · 尚未加入图库</span></button>'
  ).join('');
  box.innerHTML=(registered||'<div class="source-empty">还没有已建立索引的数据源</div>')+fresh;
  box.querySelectorAll('.source-root-btn').forEach(btn=>{
    btn.onclick=e=>{
      e.preventDefault();e.stopPropagation();
      if(btn.dataset.connected==='1'){
        selectFolderValue(btn.dataset.sourceRoot);
        document.getElementById('folderInput').value=folder||'';
      }else{
        loadCatalogRoot(btn.dataset.rootId);
      }
    };
  });
  box.querySelectorAll('.discovered-device').forEach(btn=>{
    btn.onclick=e=>{
      e.preventDefault();e.stopPropagation();
      const path=btn.dataset.discoveredPath||'';
      if(path){
        selectFolderValue(path);
        document.getElementById('folderInput').value=path;
        toast('已选择新设备，请确认扫描范围后开始分析','info');
      }
    };
  });
}

let catalogRootLoading=false;
function catalogCardHtml(item){
  const missing=item.state==='missing';
  const life=String(item.lifecycle||'normal');
  const lifeBadge=life==='normal'?'':('<span class="lifecycle-badge trash">'+escHtml(
    life==='trashed'?'软件回收站':life==='permanently_deleted'?'已永久删除':'处理中'
  )+'</span>');
  const badge=missing?'<span class="lifecycle-badge trash">原文件缺失</span>':lifeBadge;
  const preview=item.thumb
    ?'<img class="photo-img" src="'+escHtml(item.thumb)+'" loading="lazy" decoding="async" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'grid\'">'
      +'<div class="offline-preview-fallback" style="display:none">离线预览未缓存</div>'
    :'<div class="offline-preview-fallback">离线预览未缓存</div>';
  return '<div class="photo-card catalog-card">'+badge+preview
    +'<div class="photo-info"><div class="photo-name">'+escHtml(item.name)+'</div>'
    +'<div class="source-path">'+escHtml(item.relative_path)+'</div>'
    +'<div class="catalog-meta">'+(item.has_cull_cache?'已分析':'仅索引')+' · '+formatBytes(item.size)+'</div></div></div>';
}
async function loadCatalogRoot(rootId,{append=false}={}){
  if(catalogRootLoading)return;
  catalogRootLoading=true;
  showPhotoView();
  try{
    const offset=append&&catalogRootView?Number(catalogRootView.loaded||0):0;
    const r=await fetch('/api/catalog-root/'+encodeURIComponent(rootId)+'?limit=400&offset='+offset);
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    const incoming=d.items||[];
    if(!append){
      catalogRootView={...d,items:incoming.slice(),loaded:incoming.length};
      folder=null;
      updateStartAvailability();
      updateSourceUi();
      const fi=document.getElementById('folderInput');
      fi.value=(d.root&&d.root.original_root)||'';
      document.getElementById('progressWrap').style.display='none';
      document.getElementById('filterBar').style.display='none';
      document.getElementById('resultTools').style.display='none';
      document.getElementById('workspaceTitle').textContent=(d.source&&d.source.display_name)||'离线图库';
      document.getElementById('workspaceHint').textContent='历史索引分批加载；设备未连接时仍可查看已生成的离线预览。';
      const g=document.getElementById('gallery');
      g.className='gallery catalog-history';
      g.innerHTML=incoming.length
        ?incoming.map(catalogCardHtml).join('')
        :'<div class="empty"><div class="icon">🗄️</div><div class="title">这个图库还没有持久化媒体索引</div><p>重新连接数据源并完成一次扫描后会建立历史目录。</p></div>';
    }else{
      catalogRootView.items.push(...incoming);
      catalogRootView.loaded+=incoming.length;
      catalogRootView.has_more=!!d.has_more;
      document.getElementById('gallery').insertAdjacentHTML('beforeend',incoming.map(catalogCardHtml).join(''));
    }
    catalogRootView.has_more=!!d.has_more;
    catalogRootView.total=Number(d.total||catalogRootView.total||0);
    const loaded=Number(catalogRootView.loaded||incoming.length);
    document.getElementById('sShowing').textContent=String(loaded);
    document.getElementById('sImages').textContent=String(catalogRootView.total||loaded);

    let more=document.getElementById('catalogLoadMore');
    if(more)more.remove();
    if(catalogRootView.has_more){
      more=document.createElement('button');
      more.id='catalogLoadMore';more.className='catalog-load-more';
      more.textContent='继续加载 · '+loaded+' / '+catalogRootView.total;
      more.onclick=()=>loadCatalogRoot(rootId,{append:true});
      document.getElementById('photoView').appendChild(more);
    }
  }catch(err){
    toast('读取离线图库失败：'+(err.message||'未知错误'),'bad');
  }finally{
    catalogRootLoading=false;
  }
}

let lastStorageSummaryAt=0;
function loadStorageSummary(force=false){
  const now=Date.now();
  if(!force && lastStorageSummaryAt && now-lastStorageSummaryAt<300000)return;
  lastStorageSummaryAt=now;
  fetch('/api/storage-summary').then(r=>r.json()).then(d=>{
    if(d.error)return;
    document.getElementById('dbUsage').textContent=formatBytes(d.database_bytes);
    document.getElementById('catalogPreviewUsage').textContent=formatBytes(d.persistent_preview_bytes);
    document.getElementById('previewUsage').textContent=formatBytes(d.preview_cache_bytes);
    document.getElementById('featureUsage').textContent=formatBytes(d.dedup_feature_bytes);
    document.getElementById('logUsage').textContent=formatBytes(d.log_bytes);
    document.getElementById('dataRootText').textContent='数据目录：'+(d.data_root||'—');
    latestStorageSummary=d;
    if(!folder&&!catalogRootView&&!isRunning&&!coreRunning&&!photos.length)renderWorkspaceLanding();
  }).catch(()=>{lastStorageSummaryAt=0;});
}

async function clearStorageCategory(category){
  const labels={previews:'预览缓存',logs:'旧日志',features:'相似照片特征缓存'};
  const risky=category==='features';
  const ok=await askBatchConfirm(
    risky?'重建相似照片特征':'清理'+labels[category],
    risky
      ?'这只会删除可重建的相似照片特征缓存，不会删除图库数据库、人工复核记录或原始照片。下次相似分析会重新计算。'
      :'只会清理可重建的临时软件文件，不会删除图库数据库、离线图库预览、人工复核记录或原始照片。',
    risky?'清理并在下次重建':'立即清理'
  );
  if(!ok)return;
  try{
    const r=await fetch('/api/storage-clear',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({category})
    });
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    toast('已清理 '+labels[category]+' · 释放 '+formatBytes(d.freed_bytes),'good');
    loadStorageSummary(true);
  }catch(err){
    toast('清理失败：'+(err.message||'未知错误'),'bad');
  }
}
document.querySelectorAll('[data-clean]').forEach(btn=>btn.onclick=()=>clearStorageCategory(btn.dataset.clean));
function loadShortcuts(){
  fetch('/api/shortcuts').then(r=>r.json()).then(d=>{
    let h='';
    codespacesMode=!!d.codespaces;
    sourceCatalog=Array.isArray(d.sources)?d.sources:[];
    renderSources();
    updateSourceUi();
    const fi=document.getElementById('folderInput');
    const bb=document.getElementById('browseBtn');

    if(codespacesMode){
      bb.disabled=true;bb.textContent='云端路径';
      fi.placeholder='输入 Codespaces 中的云端文件夹路径';
    }else{
      bb.disabled=isRunning||coreRunning;bb.textContent='＋ 添加';
      fi.placeholder='选择或粘贴照片文件夹路径';
    }

    demoShortcutPath=d.demo_folder||'';
    demoShortcutCount=Number(d.demo_count||0);
    demoShortcutReady=!!d.demo_ready;
    if(d.demo_folder){
      const p=d.demo_folder;
      const ready=!!d.demo_ready;
      const n=d.demo_count||36;
      h+='<button class="shortcut demo-shortcut" data-p="'+escHtml(p)+'" data-demo="1" data-ready="'+(ready?'1':'0')+'">'
        +'<span class="tag recent">示例</span>'
        +'<span><b>'+(ready?('多目录演示图库 · '+n+' 张'):'准备演示图库')+'</b>'
        +'<small>'+(ready?'6 个子文件夹 · 清晰/轻微软/模糊/近似连拍':'首次点击时后台生成，不阻塞软件启动')+'</small></span></button>';
      if(codespacesMode&&!folder&&ready){selectFolderValue(p);fi.value=p;}
    }

    (d.sd||[]).forEach(o=>{
      const p=(typeof o==='string')?o:o.path;
      const br=(o&&o.brand)?(' · '+o.brand):'';
      h+='<button class="shortcut" data-p="'+escHtml(p)+'"><span class="tag sd">相机卡'+escHtml(br)
        +'</span><span>'+escHtml(sdLabel(p))+'</span></button>';
    });
    (d.recent||[]).filter(p=>!sourceCatalog.some(s=>(s.roots||[]).some(r=>sameFolder(r.current_root,p)||sameFolder(r.original_root,p))))
      .slice(0,3).forEach(p=>{
        h+='<button class="shortcut" data-p="'+escHtml(p)+'"><span class="tag recent">最近</span><span>'+escHtml(sdLabel(p))+'</span></button>';
      });
    document.getElementById('shortcuts').innerHTML=h;
    document.querySelectorAll('.shortcut').forEach(b=>{
      b.disabled=isRunning||coreRunning;
      b.onclick=async()=>{
        if(isRunning||coreRunning)return;
        if(b.dataset.demo==='1'&&b.dataset.ready!=='1'){
          await openDemoLibrary(b);
          return;
        }
        selectFolderValue(b.dataset.p);fi.value=folder||'';
      };
    });
    if(!folder&&!catalogRootView&&!isRunning&&!coreRunning&&!photos.length)renderWorkspaceLanding();
  }).catch(()=>{});
}
async function refreshEnvironment(){
  if(document.hidden||isRunning||coreRunning)return;
  try{
    const d=await fetch('/api/environment-refresh').then(r=>r.json());
    if(Array.isArray(d.sources))sourceCatalog=d.sources;
    if(Array.isArray(d.devices))discoveredDevices=d.devices;
    renderSources();updateSourceUi();
    const shortcuts=document.getElementById('shortcuts');
    if(shortcuts&&Array.isArray(d.sd)&&d.sd.length){
      const existing=new Set([...shortcuts.querySelectorAll('.shortcut')].map(x=>x.dataset.p));
      d.sd.forEach(o=>{
        const p=(typeof o==='string')?o:o.path;
        if(!p||existing.has(p))return;
        const br=(o&&o.brand)?(' · '+o.brand):'';
        const b=document.createElement('button');
        b.className='shortcut';b.dataset.p=p;
        b.innerHTML='<span class="tag sd">相机卡'+escHtml(br)+'</span><span>'+escHtml(sdLabel(p))+'</span>';
        b.onclick=()=>{selectFolderValue(p);document.getElementById('folderInput').value=folder||'';};
        shortcuts.appendChild(b);
      });
    }
  }catch(_){}
}
loadShortcuts();
setTimeout(refreshEnvironment,5000);
setInterval(refreshEnvironment,60000);
document.getElementById('folderInput').onchange=e=>selectFolderValue(e.target.value);
document.getElementById('folderInput').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();selectFolderValue(e.target.value);}};
document.getElementById('browseBtn').onclick=async()=>{
  const btn=document.getElementById('browseBtn');
  const old=btn.textContent;btn.disabled=true;btn.textContent='正在选择…';
  try{
    let selected=null;
    let nativeError=null;
    let nativeAttempted=false;
    const nativeApi=(window.pywebview&&window.pywebview.api)||null;
    if(nativeApi&&nativeApi.pick_folder){
      nativeAttempted=true;
      try{
        selected=await nativeApi.pick_folder();
      }catch(err){
        nativeError=err;
      }
    }
    // The pywebview object can exist briefly before its API bridge is ready.
    // In that state, do not silently do nothing: use the HTTP/native-dialog
    // fallback. A real user cancellation from the native picker is respected.
    if(selected==null && (!nativeAttempted||nativeError)){
      const r=await fetch('/api/browse',{method:'POST'});
      if(!r.ok)throw new Error('HTTP '+r.status);
      const d=await r.json();
      selected=d.folder||null;
    }
    if(selected){
      selectFolderValue(selected);
      document.getElementById('folderInput').value=folder||'';
    }
  }catch(err){
    toast('无法打开文件夹选择器：'+(err.message||'未知错误'),'bad');
  }finally{
    btn.disabled=isRunning||coreRunning||codespacesMode;
    btn.textContent=old;
  }
};

/* start / stop (the same button toggles) */
function setStartBtn(running){
  isRunning=running;
  const busy=running;
  document.body.classList.toggle('processing',busy);
  startBtn.textContent=running?'■ 停止当前分析':'▶ 开始分析';
  startBtn.classList.toggle('stopping',running);
  const fi=document.getElementById('folderInput');
  const bb=document.getElementById('browseBtn');
  if(fi)fi.disabled=busy;
  if(bb)bb.disabled=busy||coreRunning||codespacesMode;
  document.querySelectorAll('.shortcut').forEach(x=>x.disabled=busy||coreRunning);
  updateStartAvailability();
}
function snapshotPipelineConfig(){
  return {
    cullStrictness,cullAdaptive,cullRescue,dedupThreshold,rankTopN,weights:{...weights},
    recursiveScan,compareScope,outputMode,customOutput,pairMode
  };
}
async function startStep(step,config=null){
  const cfg=config||snapshotPipelineConfig();
  runningStep=step;
  if(step==='cull'){cullReady=false;cullLiveStore.clear();}
  if(step==='dedup')dedupLiveStore.clear();
  pollFailures=0;largeResultWarned=false;
  document.getElementById('progressWrap').style.display='block';
  if(step===currentStep){
    document.getElementById('gallery').innerHTML='';
    lastRankSig='';lastStep=step;
    gPage=0;lastGallerySig='';document.getElementById('pager').style.display='none';
    document.getElementById('exportBtn').style.display='none';
    document.getElementById('exportPbgBtn').style.display='none';
    {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.remove('cta');}
  }
  setRemoved(0);
  setStartBtn(true);

  try{
    const response=await fetch('/api/run/'+step,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        folder,
        opt:step==='cull'?cfg.cullStrictness:cfg.dedupThreshold,
        adaptive:cfg.cullAdaptive,
        rescue:cfg.cullRescue,
        ftype:'all',
        pair:step==='cull'?'both':cfg.pairMode,
        recursive:cfg.recursiveScan,
        compare_scope:cfg.compareScope,
        output_mode:cfg.outputMode,
        custom_output:cfg.customOutput,
        topn:cfg.rankTopN,
        weights:cfg.weights
      })
    });
    let data={};
    try{data=await response.json();}catch(_){}
    if(!response.ok){
      throw new Error(data.error||('启动任务失败（HTTP '+response.status+'）'));
    }
    setTimeout(()=>poll(step),180);
    return true;
  }catch(err){
    const msg=err&&err.message?err.message:'无法启动照片处理任务';
    document.getElementById('progressText').textContent='启动失败：'+msg;
    setStartBtn(false);
    runningStep=null;
    toast('启动失败：'+msg,'bad');
    return false;
  }
}
function doStart(){
  if(!folder){toast('请先选择照片文件夹','bad');return;}
  startStep(currentStep);
}
function doStop(){
  if(!runningStep)return;
  startBtn.textContent='正在停止…';startBtn.disabled=true;
  fetch('/api/stop/'+runningStep,{method:'POST'}).finally(()=>{startBtn.disabled=false;});
}

/* ---- v1.5 core cleanup engines: Cull + Similarity run in parallel. ---- */
function corePayload(step,cfg){
  return {
    folder,
    opt:step==='cull'?cfg.cullStrictness:cfg.dedupThreshold,
    adaptive:cfg.cullAdaptive,
    rescue:cfg.cullRescue,
    ftype:'all',
    pair:step==='cull'?'both':cfg.pairMode,
    recursive:cfg.recursiveScan,
    compare_scope:cfg.compareScope,
    output_mode:cfg.outputMode,
    custom_output:cfg.customOutput,
    topn:cfg.rankTopN,
    weights:cfg.weights
  };
}
function updateNewResultsButton(){
  const b=document.getElementById('loadNewResults');
  if(!b)return;
  const d=coreSnapshots[currentStep];
  if(!d||!['cull','dedup'].includes(currentStep)){b.style.display='none';return;}
  let total=Number(d.result_total||((d.photos||[]).length)||0);
  if(currentStep==='dedup'){
    const st=d.stats||{};
    const key=dedupStatusFilter+'_groups';
    if(Number.isFinite(Number(st[key])))total=Number(st[key]);
  }
  const shown=Number(document.getElementById('sShowing').textContent||0);
  const pending=Math.max(0,total-shown);
  b.textContent=(d.running?'后台新增结果 ':'查看最新结果 ')+pending;
  b.style.display=pending||!d.running?'inline-flex':'none';
}
function applyLatestCoreSnapshot(){
  const d=coreSnapshots[currentStep];
  if(!d)return;
  if(currentStep==='cull'){
    const rows=d.photos||[];
    cullLiveStore.clear();rows.forEach(p=>{if(p&&p.path)cullLiveStore.set(p.path,p);});
    renderCullStep(Array.from(cullLiveStore.values()));
    if(!d.running)maybeLoadAllCull(d);
  }else if(currentStep==='dedup'){
    loadDedupPage(true);
  }
  const b=document.getElementById('loadNewResults');if(b)b.style.display='none';
}
document.getElementById('loadNewResults').onclick=applyLatestCoreSnapshot;

async function pollCore(){
  if(corePollTimer){clearTimeout(corePollTimer);corePollTimer=null;}
  try{
    const rows=await Promise.all(['cull','dedup'].map(k=>fetch('/api/progress/'+k).then(r=>r.json())));
    coreSnapshots.cull=rows[0];coreSnapshots.dedup=rows[1];
    if(currentStep==='cull')updateVisibleStepStatus('cull',rows[0]);
    if(currentStep==='dedup')updateVisibleStepStatus('dedup',rows[1]);
    updateNewResultsButton();
    const any=rows.some(d=>d&&d.running);
    coreRunning=any;
    setStartBtn(any);
    if(any){
      corePollTimer=setTimeout(pollCore,900);
    }else{
      document.getElementById('progressText').textContent='后台分析已完成 · 点击“查看最新结果”统一载入';
      updateNewResultsButton();
    }
  }catch(err){
    document.getElementById('progressText').textContent='后台状态同步中断，将自动重试';
    if(coreRunning)corePollTimer=setTimeout(pollCore,1400);
  }
}
async function coreRun(){
  if(!folder){toast('请先选择照片文件夹','bad');return;}
  if(coreRunning||stateBusyFromUi()){return;}
  const cfg=snapshotPipelineConfig();
  const coreSteps=['cull','dedup'];
  coreRunning=true;setStartBtn(true);
  showPhotoView();
  document.getElementById('gallery').innerHTML='<div class="empty"><div class="icon">◌</div><div class="title">正在分析照片</div><p>模糊筛选与相似照片分析正在后台并行启动，结果会持续进入当前工作区。</p></div>';
  document.getElementById('progressWrap').style.display='block';
  document.getElementById('progressText').textContent='正在启动模糊分析与相似分析…';
  try{
    const settled=await Promise.allSettled(coreSteps.map(step=>fetch('/api/run/'+step,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(corePayload(step,cfg))
    })));
    const payloads=await Promise.all(settled.map(async(entry,i)=>{
      const step=coreSteps[i];
      if(entry.status==='rejected'){
        return {step,ok:false,data:{error:entry.reason&&entry.reason.message?entry.reason.message:'网络请求失败'}};
      }
      const r=entry.value;
      return {step,ok:r.ok,data:await r.json().catch(()=>({}))};
    }));
    const failed=payloads.filter(x=>!x.ok);
    if(failed.length){
      const started=payloads.filter(x=>x.ok).map(x=>x.step);
      if(started.length){
        await Promise.allSettled(started.map(step=>fetch('/api/stop/'+step,{method:'POST'})));
      }
      const detail=failed.map(x=>x.step+'：'+(x.data.error||'启动失败')).join('；');
      throw new Error(detail||'核心分析启动失败');
    }
    toast('已在后台同时启动：模糊废片分析 + 相似照片分析','good');
    pollCore();
  }catch(err){
    coreRunning=false;setStartBtn(false);
    document.getElementById('progressText').textContent='核心分析未完整启动，已回滚已启动任务';
    toast('启动失败：'+(err.message||'未知错误'),'bad');
    if(!photos.length)renderWorkspaceLanding();
  }
}
function stateBusyFromUi(){return !!runningStep;}
async function stopCore(){
  if(!coreRunning)return;
  startBtn.disabled=true;startBtn.textContent='正在停止后台分析…';
  await Promise.all(['cull','dedup'].map(k=>fetch('/api/stop/'+k,{method:'POST'}).catch(()=>null)));
  startBtn.disabled=false;
  corePollTimer=setTimeout(pollCore,250);
}
startBtn.onclick=()=>{
  if(coreRunning){stopCore();return;}
  if(isRunning){doStop();return;}
  if(currentStep==='rank'){doStart();return;}
  coreRun();
};
function cullRowsForPayload(d){
  const rows=d.photos||[];
  if(!d.running)cullLiveStore.clear();
  rows.forEach(p=>{if(p&&p.path)cullLiveStore.set(p.path,p);});
  return Array.from(cullLiveStore.values());
}

async function loadCullPage(reset=false){
  if(currentStep!=='cull')return;
  const token=++cullChunkToken;
  const offset=reset?0:cullLiveStore.size;
  try{
    const d=await fetch('/api/results/cull?offset='+offset+'&limit=200')
      .then(async r=>{const x=await r.json();if(!r.ok)throw new Error(x.error||('HTTP '+r.status));return x;});
    if(token!==cullChunkToken||currentStep!=='cull')return;
    if(reset)cullLiveStore.clear();
    (d.photos||[]).forEach(p=>{if(p&&p.path)cullLiveStore.set(p.path,p);});
    cullVisibleTotal=Number(d.total||0);
    photos=Array.from(cullLiveStore.values());
    renderCullStep(photos);
    setupFilterBar();
    updateCullLoadMore();
  }catch(err){toast('载入清晰度结果失败：'+(err.message||'未知错误'),'bad');}
}
function updateCullLoadMore(){
  const btn=document.getElementById('cullLoadMore');
  if(!btn)return;
  const left=Math.max(0,cullVisibleTotal-cullLiveStore.size);
  btn.style.display=left?'inline-flex':'none';
  btn.textContent=left?'加载更多（剩余 '+left+'）':'';
}
async function loadRemainingCull(total,offset){return loadCullPage(false);}
function maybeLoadAllCull(d){
  cullVisibleTotal=Number(d.result_total||cullVisibleTotal||0);
  updateCullLoadMore();
}
function dedupRowsForPayload(d){
  const rows=d.photos||[];
  if(!d.running)dedupLiveStore.clear();
  rows.forEach(g=>{if(g&&g.group_id!=null)dedupLiveStore.set(String(g.group_id),g);});
  return Array.from(dedupLiveStore.values());
}
async function loadDedupPage(reset=false){
  if(currentStep!=='dedup')return;
  const offset=reset?0:dedupLiveStore.size;
  try{
    const d=await fetch('/api/results/dedup?offset='+offset+'&limit=200&status='+encodeURIComponent(dedupStatusFilter))
      .then(async r=>{const x=await r.json();if(!r.ok)throw new Error(x.error||('HTTP '+r.status));return x;});
    if(reset)dedupLiveStore.clear();
    (d.photos||[]).forEach(g=>dedupLiveStore.set(String(g.group_id),g));
    dedupVisibleTotal=Number(d.total||0);dedupStatusCounts=d.counts||dedupStatusCounts;
    photos=Array.from(dedupLiveStore.values());
    renderDedupGroups(photos);
    setupFilterBar();
    updateDedupLoadMore();
  }catch(err){toast('载入相似组失败：'+(err.message||'未知错误'),'bad');}
}
function updateDedupLoadMore(){
  const btn=document.getElementById('dedupLoadMore');
  if(!btn)return;
  const left=Math.max(0,dedupVisibleTotal-dedupLiveStore.size);
  btn.style.display=left?'inline-flex':'none';
  btn.textContent=left?'加载更多（剩余 '+left+'）':'';
}
async function loadRemainingDedup(total,offset){return loadDedupPage(false);}

function maybeLoadAllDedup(d){
  // v1.5 deliberately keeps a bounded foreground window. Full libraries are
  // loaded on demand so background completion never freezes the review UI.
  updateDedupLoadMore();
}

function poll(step){
  fetch('/api/progress/'+step)
    .then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(d=>{
      pollFailures=0;
      if(d.src_folder && folder && !sameFolder(d.src_folder,folder)){
        setTimeout(()=>poll(step),800);
        return;
      }
      updateVisibleStepStatus(step,d);
      if(step===currentStep){
        if(step==='rank')renderRank(d.photos||[]);
        else if(step==='cull')renderCullStep(cullRowsForPayload(d));
        else renderDedupGroups(dedupRowsForPayload(d));
      }

      if(d.running){
        setTimeout(()=>poll(step),800);
        return;
      }

      if(d.truncated&&!largeResultWarned){
        largeResultWarned=true;
        toast('本次已完整分析 '+(d.result_total||0)+' 张照片，完整结果正在后台分批载入。','info');
      }
      if(step==='cull'&&step===currentStep)maybeLoadAllCull(d);
      if(step==='dedup'&&step===currentStep)maybeLoadAllDedup(d);

      if(step===currentStep)document.getElementById('progressFill').style.width='100%';
      setStartBtn(false);runningStep=null;
      if(step==='rank'&&photos.length)document.getElementById('exportBtn').style.display='block';
      if(step==='cull'){
        cullReady=true;
        updateCullMoveButton();
      }
      if(step==='dedup'){
      }
    })
    .catch(err=>{
      pollFailures++;
      if(pollFailures<=5 && runningStep===step){
        document.getElementById('progressText').textContent='连接本地处理服务中…（'+pollFailures+'/5）';
        setTimeout(()=>poll(step),800);
        return;
      }
      const msg='无法读取处理进度：'+(err&&err.message?err.message:'本地服务连接失败');
      toast(msg,'bad');
      if(step===currentStep)document.getElementById('progressText').textContent=msg;
      setStartBtn(false);runningStep=null;
    });
}

/* Large-photo viewer: wheel zoom, drag pan, double-click fit/2x. */
let lbScale=1,lbX=0,lbY=0,lbDragging=false,lbDragX=0,lbDragY=0;
function applyLbTransform(){const img=document.getElementById('lbImg');img.style.transform=`translate(${lbX}px,${lbY}px) scale(${lbScale})`;document.getElementById('lbZoom').textContent=lbScale===1?'适应窗口':Math.round(lbScale*100)+'%';}
function resetLbZoom(){lbScale=1;lbX=0;lbY=0;applyLbTransform();}
document.getElementById('lbStage').addEventListener('wheel',e=>{e.preventDefault();lbScale=Math.min(8,Math.max(.5,lbScale*(e.deltaY<0?1.12:.89)));if(Math.abs(lbScale-1)<.04)lbScale=1;applyLbTransform();},{passive:false});
document.getElementById('lbImg').addEventListener('dblclick',()=>{if(lbScale===1)lbScale=2;else{lbScale=1;lbX=0;lbY=0;}applyLbTransform();});
document.getElementById('lbImg').addEventListener('mousedown',e=>{if(lbScale<=1)return;lbDragging=true;lbDragX=e.clientX-lbX;lbDragY=e.clientY-lbY;e.currentTarget.classList.add('dragging');e.preventDefault();});
window.addEventListener('mousemove',e=>{if(!lbDragging)return;lbX=e.clientX-lbDragX;lbY=e.clientY-lbDragY;applyLbTransform();});
window.addEventListener('mouseup',()=>{lbDragging=false;document.getElementById('lbImg').classList.remove('dragging');});

/* ---- radar ---- */
function radarSVG(metrics,size=210){
  const cx=size/2,cy=size/2,R=size/2-30,n=metrics.length;
  const ang=i=>-Math.PI/2+i*2*Math.PI/n,pt=(i,r)=>[cx+Math.cos(ang(i))*r,cy+Math.sin(ang(i))*r];
  let grid='';[0.25,0.5,0.75,1].forEach(f=>{const p=metrics.map((m,i)=>pt(i,R*f).map(v=>v.toFixed(1)).join(',')).join(' ');grid+=`<polygon points="${p}" fill="none" stroke="rgba(255,255,255,.18)"/>`;});
  const col=i=>catColor(i,n);
  let sp='',lb='';metrics.forEach((m,i)=>{const[x,y]=pt(i,R);sp+=`<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.18)"/>`;
    const[lx,ly]=pt(i,R+15);lb+=`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" font-size="9.5" font-weight="600" fill="${col(i)}" text-anchor="middle" dominant-baseline="middle">${m.label}</text>`;});
  // data vertices + a full multi-colour fill: each wedge blends its two corner colours
  const dpt=metrics.map((m,i)=>pt(i,R*Math.max(0,Math.min(1,(m.value||0)/100))));
  const uid='rg'+(radarSVG._n=(radarSVG._n||0)+1);
  let defs='',fill='';
  for(let i=0;i<n;i++){const j=(i+1)%n;const[ax,ay]=dpt[i],[bx,by]=dpt[j];const gid=uid+'_'+i;
    defs+=`<linearGradient id="${gid}" gradientUnits="userSpaceOnUse" x1="${ax.toFixed(1)}" y1="${ay.toFixed(1)}" x2="${bx.toFixed(1)}" y2="${by.toFixed(1)}"><stop offset="0" stop-color="${col(i)}"/><stop offset="1" stop-color="${col(j)}"/></linearGradient>`;
    fill+=`<polygon points="${cx},${cy} ${ax.toFixed(1)},${ay.toFixed(1)} ${bx.toFixed(1)},${by.toFixed(1)}" fill="url(#${gid})" fill-opacity=".5" stroke="none"/>`;}
  const dp=dpt.map(p=>p.map(v=>v.toFixed(1)).join(',')).join(' ');
  return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><defs>${defs}</defs>${grid}${sp}${fill}<polygon points="${dp}" fill="none" stroke="rgba(255,255,255,.65)" stroke-width="1.5"/>${lb}</svg>`;
}

/* ---- renderers ---- */
const EMPTY='<div class="empty"><div class="icon">🎞️</div><div>暂无结果</div></div>';
function emptyHTML(step){
  const C={
    cull:['✂️','清晰度结果','先识别并筛出失焦、明显模糊的照片。',
      ['🔍 评估真实清晰度，雾气和夜空不会被简单误判为模糊',
       '🟢 清晰 &nbsp;·&nbsp; 🟠 轻微软（可保留） &nbsp;·&nbsp; 🔴 模糊',
       '📁 默认扫描当前文件夹及所有子文件夹；结果按来源文件夹分组显示']],
    dedup:['🪢','相似组选优','将连拍或高度相似照片归组，保留其中最佳的一张。',
      ['📸 自动识别并归组近似照片',
       '⭐ 每组优先保留最清晰的一张',
       '🪢 可选择“各子文件夹独立对比”或“整个范围全局对比”，最后再统一处理未保留项']],
    rank:['🏆','精选推荐','综合画质、构图与色彩，找出更值得保留的照片。',
      ['🎯 综合评估构图、光线、清晰度、色彩与对比度',
       '🥇 先完成智能评分并展示候选照片，提供单张评分雷达图',
       '☑️ 大目录结果按来源文件夹分组；筛选后可继续移除、删除或导出优选照片']],
    trash:['🗑️','软件回收站','删除后的照片先进入这里，作为永久删除前的最后一轮复核。',
      ['👀 可继续查看缩略图和大图',
       '↩ 发现误删可恢复到原位置；同名文件存在时自动避让，不覆盖',
       '⚠️ 只有在这里点击“永久删除”后，照片才真正从磁盘删除']]
  };
  const c=C[step]||C.cull;
  return `<div class="empty"><div class="icon">${c[0]}</div>
    <div class="title">${c[1]}</div><p>${c[2]}</p>
    <div class="lines">${c[3].map(l=>`<div>${l}</div>`).join('')}</div></div>`;
}
let lastGallerySig='', gPage=0, gItems=[];
function dedupGroupStatusLabel(status){
  if(status==='reviewed')return ['已筛选','reviewed'];
  if(status==='updated')return ['已筛选 · 有新增','updated'];
  return ['待筛选','pending'];
}
function dedupMemberState(group,p){
  const life=p.lifecycle||'normal';
  if(life==='pending_trash')return ['待移入回收站','state-pending','pending-delete'];
  if(life==='pending_permanent_delete')return ['待彻底删除','state-pending','pending-delete'];
  if(life==='trashed')return ['↩ 已删除 · 恢复','state-trash','trashed'];
  if(life==='permanently_deleted')return ['已彻底删除','state-trash','trashed'];
  if(group.status==='reviewed'&&p.selected)return ['✓ 保留','','selected'];
  if(p.selected)return ['推荐保留','',''];
  return ['待筛选','state-neutral',''];
}
function renderDedupGroups(groups){
  showPhotoView();
  photos=groups||[];
  const reviewGroups=(groups||[]).filter(group=>
    (group.members||[]).filter(visibleInReview).length>1
  );
  const g=document.getElementById('gallery');
  document.getElementById('sShowing').textContent=reviewGroups.length;
  if(!reviewGroups.length){
    g.innerHTML='<div class="empty"><div class="icon">✓</div><div class="title">没有需要人工处理的相似组</div></div>';
    document.getElementById('resultTools').style.display='none';
    return;
  }
  const buckets={};
  reviewGroups.forEach(group=>{
    const key=group.folder_rel||'当前文件夹';
    (buckets[key]||(buckets[key]=[])).push(group);
  });
  const keys=Object.keys(buckets).sort((a,b)=>a.localeCompare(b,'zh-CN'));
  let seq=0,html='<div class="folder-results">';
  keys.forEach(key=>{
    const rows=buckets[key];
    const folded=isFolderCollapsed(key);
    html+='<section class="folder-group '+(folded?'collapsed':'')+'" data-folder="'+encodeURIComponent(key)+'"><div class="folder-head"><b>📁 '+escHtml(key)+'</b><span>'+rows.length+' 组 · <button class="fold-btn">'+(folded?'展开':'收起')+'</button></span></div>';
    html+='<div class="folder-body" style="padding:10px;display:flex;flex-direction:column;gap:10px">';
    if(!folded)rows.forEach(group=>{
      seq++;
      const allMembers=group.members||[];
      const members=allMembers.filter(visibleInReview);
      const deleted=allMembers.length-members.length;
      const active=members.length;
      const kept=members.filter(p=>p.selected).length;
      const [statusText,statusClass]=dedupGroupStatusLabel(group.status||'pending');
      html+='<div class="dedup-group" data-group="'+group.group_id+'">';
      html+='<div class="dedup-group-head">'
        +'<div class="dedup-group-title"><b>相似组 '+seq+' · '+members.length+' 张</b>'
        +'<span class="group-status '+statusClass+'">'+statusText+'</span>'
        +'<span class="dedup-group-meta">保留 '+kept+' · 删除 '+deleted+' · 待处理 '+Math.max(0,active-kept)+'</span></div>'
        +'<div class="dedup-group-actions">'
        +'<button class="chip group-complete" data-group="'+group.group_id+'">完成本组</button>'
        +'<details class="dedup-more"><summary title="更多操作">更多操作</summary><div class="dedup-quick">'
        +'<button data-dmode="best1" data-group="'+group.group_id+'">保留最佳 1 张</button>'
        +'<button data-dmode="best2" data-group="'+group.group_id+'">保留最佳 2 张</button>'
        +'<button data-dmode="all" data-group="'+group.group_id+'">全部保留</button>'
        +'</div></details></div></div>';
      html+='<div class="dedup-choices">';
      members.forEach(p=>{
        const [label,badgeClass,cardState]=dedupMemberState(group,p);
        const selectedClass=(group.status==='reviewed'&&p.selected)?' selected':'';
        html+='<div class="dedup-choice'+selectedClass+(cardState?' '+cardState:'')+'" data-group="'+group.group_id+'" data-path="'+escHtml(p.path)+'">';
        const life=p.lifecycle||'normal';
        const disabled=['pending_trash','pending_permanent_delete','permanently_deleted','pending_restore'].includes(life)?' disabled':'';
        const title=life==='trashed'?'恢复这张照片':(disabled?'后台处理中':'切换保留状态');
        html+='<button class="dedup-recommend '+badgeClass+'" data-group="'+group.group_id+'" data-path="'+escHtml(p.path)+'" data-life="'+escHtml(life)+'" data-trash-id="'+escHtml(p.trash_id||'')+'" title="'+title+'"'+disabled+'>'+label+'</button>';
        if(p.thumb)html+='<img src="'+p.thumb+'" loading="lazy" decoding="async">';
        else html+='<div style="aspect-ratio:3/2;display:grid;place-items:center;background:var(--panel2);color:var(--muted)">文件已删除</div>';
        html+='<div class="dedup-choice-meta"><div><div class="dedup-choice-name">'+escHtml(p.name)+'</div>';
        html+='<div class="source-path">'+escHtml(p.rel_dir||'当前文件夹')+'</div></div>';
        if(!['pending_trash','pending_permanent_delete','trashed','permanently_deleted'].includes(p.lifecycle))
          html+='<button class="delete-btn" data-step="dedup" data-path="'+escHtml(p.path)+'" title="删除">🗑</button>';
        html+='</div></div>';
      });
      html+='</div></div>';
    });
    html+='</div></section>';
  });
  html+='</div>';
  g.innerHTML=html;
  updateResultTools();
}

document.getElementById('gallery').addEventListener('click',e=>{
  const complete=e.target.closest('.group-complete');
  if(complete){
    e.stopPropagation();
    fetch('/api/dedup-complete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({group_id:Number(complete.dataset.group)})})
      .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
      .then(d=>{
        const idx=photos.findIndex(g=>g.group_id===Number(complete.dataset.group));
        if(idx>=0&&d.changed_group)photos[idx]=d.changed_group;
        renderDedupGroups(photos);
        toast('本组已完成筛选','good');
      }).catch(err=>toast('完成本组失败：'+(err.message||'未知错误'),'bad'));
    return;
  }
  const b=e.target.closest('.dedup-quick button');
  if(!b)return;
  e.stopPropagation();
  fetch('/api/dedup-group-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:Number(b.dataset.group),mode:b.dataset.dmode})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      (d.photos||[]).forEach(g=>{if(g&&g.group_id!=null)dedupLiveStore.set(String(g.group_id),g);});
      if(d.changed_group&&d.changed_group.group_id!=null)dedupLiveStore.set(String(d.changed_group.group_id),d.changed_group);
      renderDedupGroups(Array.from(dedupLiveStore.values()));
    })
    .catch(err=>toast('相似组选优失败：'+(err.message||'未知错误'),'bad'));
});
function selectDedupPhoto(groupId,path){
  fetch('/api/dedup-select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:Number(groupId),path})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      (d.photos||[]).forEach(g=>{if(g&&g.group_id!=null)dedupLiveStore.set(String(g.group_id),g);});
      if(d.changed_group&&d.changed_group.group_id!=null)dedupLiveStore.set(String(d.changed_group.group_id),d.changed_group);
      renderDedupGroups(Array.from(dedupLiveStore.values()));
      toast(d.selected?'已加入保留':'已取消保留','good');
    })
    .catch(err=>toast('切换失败：'+(err.message||'未知错误'),'bad'));
}
function applyDedupSelection(){
  const btn=document.getElementById('dedupApplyBtn');
  btn.disabled=true;btn.textContent='正在处理…';
  fetch('/api/dedup-apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      const first=(d.photos||[]);
      dedupLiveStore.clear();first.forEach(g=>{if(g&&g.group_id!=null)dedupLiveStore.set(String(g.group_id),g);});
      const remain=Number(d.result_total!=null?d.result_total:first.length);
      renderDedupGroups(first);
      if(d.truncated)loadRemainingDedup(remain,first.length);
      if(remain===0){
        btn.style.display='none';
        document.getElementById('gallery').innerHTML='<div class="empty"><div class="icon">✓</div><div class="title">相似照片处理完成</div><p>未保留照片已按当前设置处理。</p></div>';
        document.getElementById('resultTools').style.display='none';
      }else{
        btn.style.display='block';
      }
      const extra=(d.failed||0)?('，'+d.failed+' 张处理失败，可再次尝试'):'';
      toast('已处理 '+(d.moved||0)+' 张相似照片'+extra,(d.failed||0)?'bad':'good');
      document.getElementById('progressText').textContent=(remain===0?'处理完成':'部分处理完成')+' · 已移动 '+(d.moved||0)+' 张未保留照片'+extra;
    })
    .catch(err=>toast('处理失败：'+(err.message||'未知错误'),'bad'))
    .finally(()=>{btn.disabled=false;btn.textContent='✓ 确认处理未保留照片';});
}
document.getElementById('dedupApplyBtn').onclick=applyDedupSelection;

function renderFolderPage(slice,cardBuilder,startIndex){
  const buckets={};
  slice.forEach((p,k)=>{
    const key=p.rel_dir||'当前文件夹';
    (buckets[key]||(buckets[key]=[])).push({p:p,idx:startIndex+k});
  });
  const keys=Object.keys(buckets).sort((a,b)=>a.localeCompare(b,'zh-CN'));
  let html='<div class="folder-results">';
  keys.forEach(key=>{
    const rows=buckets[key];
    const folded=isFolderCollapsed(key);
    const fs=folderStatus[key]||'';
    html+='<section class="folder-group '+(folded?'collapsed':'')+'" data-folder="'+encodeURIComponent(key)+'"><div class="folder-head"><b>📁 '+escHtml(key)+'</b><span>'+rows.length+' 张'+(fs?' · '+escHtml(fs):'')+' · <button class="fold-btn">'+(folded?'展开':'收起')+'</button></span></div>';
    html+='<div class="folder-grid folder-body">';
    if(!folded)rows.forEach(x=>{html+=cardBuilder(x.p,x.idx);});
    html+='</div></section>';
  });
  html+='</div>';
  return html;
}

function updatePager(){
  const pager=document.getElementById('pager');
  if(pager)pager.style.display='none';
}
function rankCard(p,idx){const path=escHtml(p.path);
  const on=p.phonebg?' on':'';
  return `<div class="photo-card kept${p.phonebg?' pbg':''}" data-i="${idx}" data-path="${path}"><div class="rank-num">${p.rank!=null?p.rank:idx+1}</div>
    <button class="pbg-toggle${on}" data-path="${path}" title="${p.phonebg?'已设为手机壁纸，点击取消':'设为手机壁纸'}">📱</button>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span>
      <button class="remove-btn" data-path="${path}" title="从优选结果中移除（不会删除原文件）">✕ 移除</button>
      <button class="delete-btn" data-step="rank" data-path="${path}" title="移入软件回收站">🗑 删除</button></div>
      <div class="source-path">${escHtml(p.rel_dir||'当前文件夹')}</div></div></div>`;}
function renderRank(items){
  showPhotoView();
  photos=items;const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastRankSig='';lastStep=currentStep;gPage=0;}
  const fbar=document.getElementById('filterBar');
  if(!items.length){fbar.style.display='none';}
  else if(fbar.style.display==='none'||!fbar.querySelector('.chip')){setupFilterBar();}

  const activeRankItems=items.filter(visibleInReview);
  rankView=(rankFilter==='pbg')?activeRankItems.filter(p=>p.phonebg):activeRankItems;
  gItems=rankView;
  const pbgN=activeRankItems.filter(p=>p.phonebg).length;
  const pbgChip=document.getElementById('pbgChipCount');if(pbgChip)pbgChip.textContent=pbgN;
  document.getElementById('exportPbgBtn').style.display=(currentStep==='rank'&&pbgN>0)?'block':'none';

  if(!rankView.length){
    g.innerHTML=(items.length&&rankFilter==='pbg')
      ?'<div class="empty"><div class="icon">📱</div><div>还没有标记为手机壁纸的照片。<br>点击优选照片上的 📱 按钮即可标记。</div></div>'
      :EMPTY;
    document.getElementById('resultTools').style.display='none';
    lastRankSig='';updatePager();return;
  }

  gPage=0;
  const start=0,end=rankView.length;
  const slice=rankView;
  const sig=rankFilter+'#'+slice.map(p=>p.rank+':'+p.path+':'+(p.phonebg?1:0)).join('|');
  if(sig===lastRankSig){updatePager();return;}
  lastRankSig=sig;

  if(recursiveScan){
    g.innerHTML=renderFolderPage(slice,rankCard,start);
    document.getElementById('sShowing').textContent=rankView.length;
    updateResultTools();updatePager();return;
  }
  document.getElementById('resultTools').style.display='none';

  const emp=g.querySelector('.empty');if(emp)emp.remove();
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  slice.forEach((p,k)=>{
    const idx=start+k;
    let node=existing[p.path];
    if(node&&node.classList.contains('pbg')===!!p.phonebg){
      const rn=node.querySelector('.rank-num');if(rn)rn.textContent=p.rank!=null?p.rank:idx+1;
      const sc=node.querySelector('.photo-score');if(sc)sc.textContent=p.score;
      node.dataset.i=idx;delete existing[p.path];
    }else{
      if(node)node.remove();
      const w=document.createElement('div');w.innerHTML=rankCard(p,idx);node=w.firstElementChild;
    }
    frag.appendChild(node);
  });
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  document.getElementById('sShowing').textContent=rankView.length;
  updatePager();
}
function togglePhoneBg(path){
  fetch('/api/toggle-phonebg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      const pp=photos.find(x=>x.path===path);if(pp)pp.phonebg=d.phonebg;
      lastRankSig='';renderRank(photos);
      if(document.getElementById('lightbox').classList.contains('open')){
        lbList=(rankFilter==='pbg')?photos.filter(p=>p.phonebg):photos.slice();
        if(lbIndex>=lbList.length)lbIndex=Math.max(0,lbList.length-1);
        if(lbList.length)showLb();else closeLb();
      }
      toast(d.phonebg?'📱 已加入手机壁纸（'+d.count+'）':'已取消手机壁纸（'+d.count+'）','good');
    }).catch(err=>toast('壁纸标记失败：'+(err.message||'未知错误'),'bad'));
}

/* ---- PhotoCurator software recycle bin / final review ---- */
function trashCard(p,idx){
  const path=escHtml(p.path),original=escHtml(p.original_path||'');
  const when=p.deleted_at?new Date(p.deleted_at*1000).toLocaleString():'';
  return `<div class="photo-card rejected" data-i="${idx}" data-path="${path}" data-trash-id="${p.id}">
    <div class="badge bad">待最终确认</div>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info">
      <div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span>
        <button class="trash-restore-btn" data-id="${p.id}">↩ 恢复</button>
        <button class="trash-purge-btn" data-id="${p.id}">永久删除</button>
      </div>
      <div class="source-path" title="${original}">原位置：${original}</div>
      <div class="source-path">${when?'移入时间：'+escHtml(when):'软件回收站'}</div>
    </div>
  </div>`;
}
function renderTrash(items){
  showPhotoView();
  photos=items||[];
  const g=document.getElementById('gallery');
  document.getElementById('sTrash').textContent=photos.length;
  document.getElementById('sShowing').textContent=photos.length;
  document.getElementById('resultTools').style.display='none';
  if(!photos.length){
    g.innerHTML='<div class="empty"><div class="icon">🗑️</div><div class="title">软件回收站为空</div><p>没有等待最后复核的照片。</p></div>';
    setupFilterBar();return;
  }
  g.innerHTML=photos.map((p,i)=>trashCard(p,i)).join('');
  setupFilterBar();
}
function loadTrash(){
  fetch('/api/trash').then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>renderTrash(d.photos||[]))
    .catch(err=>toast('回收站读取失败：'+(err.message||'未知错误'),'bad'));
}
function waitTaskAndSync(taskId,doneText){
  const started=Date.now();
  const tick=()=>{
    fetch('/api/tasks/'+taskId).then(r=>r.json()).then(d=>{
      if(d.state==='done'){
        if(doneText)toast(doneText,'good');
        if(currentStep==='trash')loadTrash();
        else syncCurrentView();
        refreshTaskCenter();return;
      }
      if(d.state==='failed'){
        toast('后台任务失败：'+(d.error||'未知错误'),'bad');
        syncCurrentView();refreshTaskCenter();return;
      }
      if(Date.now()-started<120000)setTimeout(tick,500);
    }).catch(()=>{if(Date.now()-started<120000)setTimeout(tick,900);});
  };
  setTimeout(tick,350);
}
async function restoreFromReviewCard(trashId){
  if(!trashId)return;
  try{
    const r=await fetch('/api/trash-restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:Number(trashId)})});
    const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    const markPending=row=>{if(row&&Number(row.trash_id)===Number(trashId))row.lifecycle='pending_restore';};
    if(currentStep==='cull'){
      (photos||[]).forEach(markPending);renderCullStep(photos);
    }else if(currentStep==='dedup'){
      (photos||[]).forEach(g=>(g.members||[]).forEach(markPending));renderDedupGroups(photos);
    }
    toast('已提交后台恢复','good');
    waitTaskAndSync(d.task_id,'照片已恢复');
  }catch(err){toast('恢复失败：'+(err.message||'未知错误'),'bad');}
}

function trashRestoreOne(id,fromLightbox=false){
  fetch('/api/trash-restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      toast('已提交后台恢复','good');
      photos=(photos||[]).filter(x=>Number(x.id)!==Number(id));renderTrash(photos);
      if(fromLightbox){lbList=photos.slice();if(!lbList.length)closeLb();else{if(lbIndex>=lbList.length)lbIndex=lbList.length-1;showLb();}}
      waitTaskAndSync(d.task_id,'照片已恢复到原目录');
    }).catch(err=>toast('恢复失败：'+(err.message||'未知错误'),'bad'));
}
async function trashPurgeOne(id,fromLightbox=false){
  const ok=await askBatchConfirm('彻底删除','永久删除后无法从 PhotoCurator 恢复这张照片。','彻底删除');
  if(!ok)return;
  fetch('/api/trash-purge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      toast('已提交后台永久删除','info');
      photos=(photos||[]).filter(x=>Number(x.id)!==Number(id));renderTrash(photos);
      if(fromLightbox){lbList=photos.slice();if(!lbList.length)closeLb();else{if(lbIndex>=lbList.length)lbIndex=lbList.length-1;showLb();}}
      waitTaskAndSync(d.task_id,'照片已永久删除');
    }).catch(err=>toast('永久删除失败：'+(err.message||'未知错误'),'bad'));
}
async function trashRestoreAll(){
  if(!photos.length)return;
  const ok=await askBatchConfirm('全部恢复','恢复软件回收站中的全部 '+photos.length+' 张照片。','全部恢复');
  if(!ok)return;
  fetch('/api/trash-restore-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{toast('已提交后台恢复 '+(d.count||0)+' 张照片','good');renderTrash([]);refreshTaskCenter();})
    .catch(err=>toast('批量恢复失败：'+(err.message||'未知错误'),'bad'));
}
async function trashPurgeAll(){
  if(!photos.length)return;
  const ok=await askBatchConfirm('清空软件回收站','将永久删除当前软件回收站中的 '+photos.length+' 张照片，此操作不可恢复。','永久删除全部');
  if(!ok)return;
  fetch('/api/trash-purge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{toast('已提交后台永久删除 '+(d.count||0)+' 张照片','info');renderTrash([]);refreshTaskCenter();})
    .catch(err=>toast('清空回收站失败：'+(err.message||'未知错误'),'bad'));
}

async function syncCurrentView(){
  if(!folder)return;
  try{
    if(currentStep==='trash'){loadTrash();return;}
    if(currentStep==='dedup'){await loadDedupPage(true);return;}
    const d=await fetch('/api/progress/'+currentStep).then(r=>r.json());
    if(d.src_folder&&folder&&!sameFolder(d.src_folder,folder))return;
    if(currentStep==='cull')renderCullStep(cullRowsForPayload(d));
    else if(currentStep==='rank')renderRank(d.photos||[]);
    updateVisibleStepStatus(currentStep,d);
  }catch(_){}
}

/* ---- cull (3-tier, reconciling, filterable) ---- */
let cullView=[], rankView=[], lastCullSig='', lastCullMoveSig='';
const TIER_NAME={sharp:'清晰',soft:'轻微软',blurry:'模糊'};
const NEXT_TIER={sharp:'soft',soft:'blurry',blurry:'sharp'};
const REVIEW_HIDDEN_LIFECYCLES=new Set([
  'pending_trash','pending_permanent_delete','trashed','permanently_deleted','pending_restore'
]);
function visibleInReview(p){
  return !!p&&!REVIEW_HIDDEN_LIFECYCLES.has(p.lifecycle||'normal');
}
function cullLifecycleInfo(p){
  const life=p.lifecycle||'normal';
  if(life==='pending_trash')return ['待移入回收站','pending-delete',''];
  if(life==='pending_permanent_delete')return ['待彻底删除','pending-delete',''];
  if(life==='trashed')return ['已移入回收站','trashed','trash'];
  if(life==='permanently_deleted')return ['已彻底删除','trashed','trash'];
  if(life==='pending_restore')return ['正在恢复','pending-delete',''];
  return null;
}
function cullCardHtml(p,idx){const path=escHtml(p.path);
  const life=cullLifecycleInfo(p),deleted=!!life;
  const cls=(p.tier==='sharp'?'kept':p.tier==='soft'?'soft':'rejected')+(life?' '+life[1]:'');
  const moveOn=p.move_selected!==false;
  const moveSel=!deleted&&p.tier==='blurry'
    ?`<button class="move-select${moveOn?'':' off'}" data-path="${path}" data-selected="${moveOn?'1':'0'}" title="${moveOn?'已加入本次删除，点击保留在原位置':'保留在原位置，点击重新加入本次删除'}">${moveOn?'✓':'□'}</button>`
    :'';
  const stateBadge=life
    ?((p.lifecycle==='trashed'&&p.trash_id)
      ?`<button class="lifecycle-badge trash restore-inline" data-trash-id="${p.trash_id}" title="恢复这张照片">↩ 已删除 · 恢复</button>`
      :`<span class="lifecycle-badge ${life[2]}">${life[0]}</span>`)
    :'';
  const tierBadge=!deleted
    ?`<button class="badge ${p.badgeType} badge-tier" data-path="${path}" data-tier="${p.tier}" title="点击切换：清晰 → 轻微软 → 模糊">⇄ ${p.badge}</button>`
    :'';
  return `<div class="photo-card ${cls}" data-i="${idx}" data-path="${path}" data-tier="${p.tier}" data-life="${p.lifecycle||'normal'}" data-move-selected="${moveOn?'1':'0'}">
    ${moveSel}${tierBadge}${stateBadge}
    ${p.thumb?`<img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">`:'<div class="photo-img" style="display:grid;place-items:center;background:var(--panel2)">文件已删除</div>'}
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span><span class="ftype${p.raw?'':(p.heic?' heic':' jpg')}">${p.fmt||(p.raw?'RAW':p.heic?'HEIC':'JPG')}</span>${deleted?'':`<button class="delete-btn" data-step="cull" data-path="${path}" title="删除">🗑 删除</button>`}</div><div class="source-path">${escHtml(p.rel_dir||'当前文件夹')}</div></div></div>`;}
function syncCullCardNode(node,p,idx){
  const moveOn=p.move_selected!==false,life=cullLifecycleInfo(p),deleted=!!life;
  node.dataset.i=idx;node.dataset.tier=p.tier;node.dataset.life=p.lifecycle||'normal';
  node.dataset.moveSelected=moveOn?'1':'0';
  node.classList.toggle('kept',p.tier==='sharp');
  node.classList.toggle('soft',p.tier==='soft');
  node.classList.toggle('rejected',p.tier==='blurry');
  node.classList.toggle('pending-delete',!!life&&life[1]==='pending-delete');
  node.classList.toggle('trashed',!!life&&life[1]==='trashed');
  if(deleted){
    const existing=node.querySelector('.delete-btn');if(existing)existing.remove();
  }
  const badge=node.querySelector('.badge-tier');
  if(badge&&!deleted){
    badge.dataset.tier=p.tier;
    badge.classList.remove('good','soft','bad');badge.classList.add(p.badgeType);
    badge.textContent='⇄ '+p.badge;
  }
  const ms=node.querySelector('.move-select');
  if(p.tier==='blurry'&&ms&&!deleted){
    ms.dataset.selected=moveOn?'1':'0';ms.classList.toggle('off',!moveOn);
    ms.textContent=moveOn?'✓':'□';
  }
}

function renderCullStep(items){
  showPhotoView();
  photos=items;
  const fSig=[...new Set(items.filter(p=>p.raw).map(p=>p.fmt||'RAW'))].sort().join(',');
  if(fSig!==lastFmtSig){lastFmtSig=fSig;setupFilterBar();}

  const filtered=items.filter(p=>visibleInReview(p)
    &&(cullFilter==='all'||p.tier===cullFilter)
    &&(cullType==='all'||(cullType==='raw'?!!p.raw
      :cullType==='heic'?!!p.heic
      :cullType==='jpg'?(!p.raw&&!p.heic)
      :('ext:'+String(p.fmt||'').toLowerCase())===cullType)));

  gItems=filtered;
  gPage=0;
  cullView=filtered;

  const g=document.getElementById('gallery');
  if(!filtered.length){
    g.innerHTML=EMPTY;lastCullSig='';lastStep=currentStep;
    document.getElementById('resultTools').style.display='none';
    document.getElementById('sShowing').textContent=0;
    updatePager();
    return;
  }

  if(recursiveScan){
    g.innerHTML=renderFolderPage(cullView,cullCardHtml,0);
    document.getElementById('sShowing').textContent=filtered.length;
    updateResultTools();updatePager();return;
  }
  document.getElementById('resultTools').style.display='none';

  const moveSig=items.filter(p=>p.tier==='blurry')
    .map(p=>p.path+':'+(p.move_selected===false?'0':'1')).join('|');
  if(moveSig!==lastCullMoveSig){lastCullMoveSig=moveSig;setupFilterBar();}
  const sig=gPage+'#'+cullView.map(p=>p.path+':'+p.tier+':'+(p.lifecycle||'normal')+':'+(p.move_selected===false?'0':'1')).join('|');
  if(sig===lastCullSig&&lastStep===currentStep){
    document.getElementById('sShowing').textContent=filtered.length;
    updatePager();
    return;
  }
  lastCullSig=sig;lastStep=currentStep;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  cullView.forEach((p,idx)=>{
    let node=existing[p.path];
    if(node&&node.dataset.tier===p.tier&&node.dataset.life===(p.lifecycle||'normal')){
      // A card can keep the same classification while its independent
      // "move this file" choice changes. Reused DOM must still mirror that
      // state immediately, otherwise the counters and checkmarks disagree.
      syncCullCardNode(node,p,idx);
      delete existing[p.path];
    }else{
      if(node)node.remove();
      const w=document.createElement('div');w.innerHTML=cullCardHtml(p,idx);node=w.firstElementChild;
    }
    frag.appendChild(node);
  });
  Object.values(existing).forEach(n=>n.remove());
  g.appendChild(frag);
  document.getElementById('sShowing').textContent=filtered.length;
  updatePager();
}
function cullMoveCounts(){
  const blurry=photos.filter(p=>p.tier==='blurry'&&!['pending_trash','pending_permanent_delete','trashed','permanently_deleted'].includes(p.lifecycle));
  return {total:blurry.length,selected:blurry.filter(p=>p.move_selected!==false).length};
}
function updateCullMoveButton(){
  const mb=document.getElementById('moveBlurryBtn');if(!mb)return;
  const n=cullMoveCounts();
  const count=document.getElementById('cullMoveCount');if(count)count.textContent=n.selected+'/'+n.total;
  if(currentStep!=='cull'||!cullReady||!n.total){
    mb.style.display='none';mb.disabled=false;mb.classList.remove('cta');return;
  }
  mb.style.display='inline-flex';
  if(n.selected>0){
    mb.disabled=false;
    mb.textContent='🗑 '+n.selected+' 张移入回收站';
    mb.classList.add('cta');
  }else{
    mb.disabled=true;
    mb.textContent='未选择模糊照片';
    mb.classList.remove('cta');
  }
}
function applyMoveSelectionResponse(path,d){
  if(path){
    const pp=photos.find(x=>x.path===path);
    if(pp)pp.move_selected=!!d.move_selected;
  }
  lastCullSig='';lastCullMoveSig='';
  renderCullStep(photos);
  updateCullMoveButton();
  if(document.getElementById('lightbox').classList.contains('open')&&currentStep==='cull')showLb();
}
function setBlurryMoveSelection(path,selected){
  fetch('/api/select-blurry',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path,selected})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>applyMoveSelectionResponse(path,d))
    .catch(err=>toast('移动选择修改失败：'+(err.message||'未知错误'),'bad'));
}
function setAllBlurryMoveSelection(selected){
  fetch('/api/select-blurry',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({all:selected})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      photos.forEach(p=>{if(p.tier==='blurry')p.move_selected=selected;});
      lastCullSig='';lastCullMoveSig='';renderCullStep(photos);updateCullMoveButton();
      toast(selected?'已选择全部模糊照片':'已取消全部模糊照片的移动选择','good');
    }).catch(err=>toast('批量选择失败：'+(err.message||'未知错误'),'bad'));
}

function cullSetTier(path,tier){
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,tier})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sSoft').textContent=d.soft;document.getElementById('sBlurry').textContent=d.blurry;
      const pp=photos.find(x=>x.path===path||x.path===d.path);
      if(pp){pp.tier=d.tier;pp.badge=d.badge;pp.badgeType=d.badgeType;pp.kept=d.kept;pp.rejected=!d.kept;pp.move_selected=!!d.move_selected;if(d.path)pp.path=d.path;if(d.thumb)pp.thumb=d.thumb;}
      lastCullSig='';lastCullMoveSig='';renderCullStep(photos);updateCullMoveButton();
    }).catch(err=>toast('分类修改失败：'+(err.message||'未知错误'),'bad'));
}

/* ---- move to PhotoCurator software recycle bin (three review steps) ---- */
async function deletePhoto(step,path,fromLightbox=false){
  const p=(photos||[]).find(x=>x.path===path)
    ||((lbList||[]).find(x=>x.path===path));
  const name=p&&p.name?p.name:path.split(/[\\/]/).pop();
  const mode=await askDeleteMode(name);
  if(!mode)return;
  try{
    const r=await fetch('/api/delete-photo',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({step,path,mode})
    });
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    const lifecycle=mode==='trash'?'pending_trash':'pending_permanent_delete';
    const applyPending=row=>{
      if(!row||row.path!==path)return;
      row.original_path=row.original_path||path;
      row.lifecycle=lifecycle;
      row.selected=false;
    };
    if(step==='cull'){
      (photos||[]).forEach(applyPending);
      lastCullSig='';lastCullMoveSig='';renderCullStep(photos);updateCullMoveButton();
    }else if(step==='dedup'){
      (photos||[]).forEach(group=>{
        (group.members||[]).forEach(applyPending);
        const active=(group.members||[]).filter(m=>!['pending_trash','pending_permanent_delete','trashed','permanently_deleted'].includes(m.lifecycle));
        group.active_count=active.length;
        group.deleted_count=(group.members||[]).length-active.length;
        if(active.length===1){active[0].selected=true;group.selected_paths=[active[0].path];group.status='reviewed';}
      });
      renderDedupGroups(photos);
    }else if(step==='rank'){
      const row=(photos||[]).find(x=>x.path===path);if(row)row.lifecycle=lifecycle;
      renderRank(photos);
    }
    toast(mode==='trash'
      ?'已提交后台：移入软件回收站 · '+name
      :'已提交后台：彻底删除 · '+name,
      mode==='trash'?'good':'info');
    refreshTaskCenter();
    if(fromLightbox){
      lbList=(lbList||[]).filter(x=>x.path!==path);
      if(lbIndex>=lbList.length)lbIndex=Math.max(0,lbList.length-1);
      if(lbList.length)showLb();else closeLb();
    }
  }catch(err){
    toast((mode==='trash'?'移入软件回收站':'彻底删除')+'失败：'+(err.message||'未知错误'),'bad');
  }
}
function lbDeleteCurrent(){
  const p=lbList[lbIndex];if(p)deletePhoto(currentStep,p.path,true);
}

/* ---- remove / restore (rank) ---- */
function setRemoved(n){removedCount=n;document.getElementById('removedN').textContent=n;
  document.getElementById('removedBox').style.display=n>0?'block':'none';
  document.getElementById('lbRestore').style.display=(n>0&&currentStep==='rank')?'inline-block':'none';}
function removePhoto(path){fetch('/api/exclude',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;}).then(d=>{renderRank(d.photos||[]);setRemoved(d.removed);}).catch(err=>toast('移除失败：'+(err.message||'未知错误'),'bad'));}
function restoreAll(syncLb){fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;}).then(d=>{renderRank(d.photos||[]);setRemoved(d.removed);
  if(syncLb&&document.getElementById('lightbox').classList.contains('open')){lbList=photos.slice();if(lbIndex>=lbList.length)lbIndex=lbList.length-1;if(lbList.length)showLb();else closeLb();}}).catch(err=>toast('恢复失败：'+(err.message||'未知错误'),'bad'));}
document.getElementById('restoreAll').onclick=()=>restoreAll(false);

/* ---- cull status toggle ---- */
function toggleStatus(path,btn,cb){
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sBlurry').textContent=d.blurry;
    if(cb)cb(d);});
}

/* ---- gallery clicks ---- */
document.getElementById('gallery').addEventListener('click',e=>{
  const tr=e.target.closest('.trash-restore-btn');if(tr){e.stopPropagation();trashRestoreOne(Number(tr.dataset.id));return;}
  const tp=e.target.closest('.trash-purge-btn');if(tp){e.stopPropagation();trashPurgeOne(Number(tp.dataset.id));return;}
  const ri=e.target.closest('.restore-inline');if(ri){e.stopPropagation();restoreFromReviewCard(Number(ri.dataset.trashId));return;}
  const db=e.target.closest('.delete-btn');if(db){e.stopPropagation();deletePhoto(db.dataset.step||currentStep,db.dataset.path);return;}
  const keep=e.target.closest('.dedup-recommend');
  if(keep&&currentStep==='dedup'){
    e.stopPropagation();
    if(keep.dataset.life==='trashed'){restoreFromReviewCard(Number(keep.dataset.trashId));return;}
    if(['pending_trash','pending_permanent_delete','permanently_deleted','pending_restore'].includes(keep.dataset.life))return;
    selectDedupPhoto(keep.dataset.group,keep.dataset.path);return;
  }
  const dc=e.target.closest('.dedup-choice');
  if(dc&&currentStep==='dedup'){
    e.stopPropagation();
    const group=(photos||[]).find(g=>String(g.group_id)===String(dc.dataset.group));
    const members=(group&&group.members)||[];
    const idx=Math.max(0,members.findIndex(x=>x.path===dc.dataset.path));
    lbList=members.slice();openLb(idx);return;
  }
  const rm=e.target.closest('.remove-btn');if(rm){e.stopPropagation();removePhoto(rm.dataset.path);return;}
  const pb=e.target.closest('.pbg-toggle');
  if(pb){e.stopPropagation();togglePhoneBg(pb.dataset.path);return;}
  const ms=e.target.closest('.move-select');
  if(ms){e.stopPropagation();setBlurryMoveSelection(ms.dataset.path,ms.dataset.selected!=='1');return;}
  const tg=e.target.closest('.status-toggle,.badge-tier');
  if(tg){e.stopPropagation();cullSetTier(tg.dataset.path,NEXT_TIER[tg.dataset.tier||'sharp']);return;}
  const c=e.target.closest('.photo-card');if(!c)return;
  lbList=(currentStep==='cull')?cullView.slice():(currentStep==='rank'&&rankFilter==='pbg')?photos.filter(p=>p.phonebg):photos.slice();
  openLb(parseInt(c.dataset.i));});

/* ---- lightbox ---- */
function openLb(i){lbIndex=i;showLb();document.getElementById('lightbox').classList.add('open');}
function closeLb(){document.getElementById('lightbox').classList.remove('open');}
const CATINFO={aesthetic:'综合视觉表现：结合构图、色彩、清晰度、动态范围与曝光的整体评分。',
  composition:'主体位置、水平线与整体视觉平衡。',technical:'曝光、动态范围、影调、白平衡与噪点控制。',
  sharpness:'经对比度归一化后的清晰度评分。数值越高越清晰；雾霾或低对比度不会直接被判定为模糊。',color:'色彩鲜明程度与不同色相之间的协调关系。'};
const SUBINFO={'三分法构图':'主体与三分点或黄金分割位置的接近程度。','水平线':'画面主要水平线的平直程度。',
  '画面平衡':'画面左右视觉重量是否均衡。','曝光':'高光和暗部是否存在明显截断。','动态范围':'最暗阴影与最亮高光之间的层次跨度。',
  '影调范围':'影调在直方图中的分布丰富程度。','白平衡':'画面色偏的中性程度；刻意冷暖调会降低此项。','噪点控制':'平坦区域的纯净程度，数值越高越干净。',
  '色彩丰富度':'饱和度与色彩种类的丰富程度。','色彩协调':'主要色相之间是否形成协调、邻近或互补关系。'};
const GROUPS=[['composition','构图',['三分法构图','水平线','画面平衡']],
  ['technical','技术质量',['曝光','动态范围','影调范围','白平衡','噪点控制']],['color','色彩',['色彩丰富度','色彩协调']]];
function barColor(v){return v>=70?'#22c55e':v>=45?'#f59e0b':'#ef4444';}
function barRow(label,v,info,cat,color){const t=(info||'').replace(/"/g,'&quot;');
  const fillBg=color?color:barColor(v);
  const labStyle=color?` style="color:${color};font-weight:600;border-left:3px solid ${color};padding-left:6px"`:'';
  return `<div class="bar${cat?' cat':''}" title="${label}: ${t}"><span class="lab"${labStyle}>${label}</span><span class="track"><span class="fill" style="width:${v}%;background:${fillBg}"></span></span><span class="num">${v}</span></div>`;}
function showLb(){
  const p=lbList[lbIndex];if(!p)return;
  resetLbZoom();
  document.getElementById('lbImg').src='/api/image?path='+encodeURIComponent(p.path);
  document.getElementById('lbName').textContent=(p.rank!=null?'#'+p.rank+'  ':'')+p.name;
  const extra=(currentStep==='dedup')
    ? ((p.group>1)?('   ·   同组最佳 · 共 '+p.group+' 张（'+(p.group-1)+' 张相似照片已归组）'):'   ·   原始照片')
    : (p.score!=null?'   ·   '+p.score:'');
  document.getElementById('lbCount').textContent=(lbIndex+1)+' / '+lbList.length+extra;
  const rm=document.getElementById('lbRemove'),rs=document.getElementById('lbRestore'),tg=document.getElementById('lbToggle'),ms=document.getElementById('lbMoveSelect'),del=document.getElementById('lbDelete');
  const tr=document.getElementById('lbTrashRestore'),tp=document.getElementById('lbTrashPurge');
  rm.style.display=currentStep==='rank'?'inline-block':'none';
  const life=p.lifecycle||'normal';
  const inReview=['cull','dedup','rank'].includes(currentStep);
  del.style.display=(inReview&&!['pending_trash','pending_permanent_delete','trashed','permanently_deleted','pending_restore'].includes(life))?'inline-block':'none';
  tr.style.display=(currentStep==='trash'||(inReview&&life==='trashed'&&p.trash_id))?'inline-block':'none';
  tp.style.display=currentStep==='trash'?'inline-block':'none';
  rs.style.display=(currentStep==='rank'&&removedCount>0)?'inline-block':'none';
  tg.style.display=currentStep==='cull'?'inline-block':'none';
  ms.style.display=(currentStep==='cull'&&p.tier==='blurry')?'inline-block':'none';
  if(currentStep==='cull')tg.textContent='⇄ '+(TIER_NAME[p.tier]||'清晰')+' → '+(TIER_NAME[NEXT_TIER[p.tier||'sharp']]);
  if(currentStep==='cull'&&p.tier==='blurry'){
    const on=p.move_selected!==false;
    ms.textContent=on?'☑ 本次移动':'☐ 保留原位';
    ms.classList.toggle('toggle',on);ms.classList.toggle('restore',!on);
  }
  const pbg=document.getElementById('lbPhoneBg');
  pbg.style.display=currentStep==='rank'?'inline-block':'none';
  if(currentStep==='rank'){pbg.classList.toggle('on',!!p.phonebg);pbg.textContent=p.phonebg?'📱 手机壁纸 ✓':'📱 手机壁纸';}
  const side=document.getElementById('lbSide');
  if(currentStep==='rank'&&p.scores){
    const metrics=CATS.map(([k,lab])=>({label:lab,value:(p.scores&&p.scores[k])||0}));
    let html=`<div style="text-align:center">${radarSVG(metrics,150)}</div><h3>分类评分</h3>`;
    CATS.forEach(([k,lab],ci)=>html+=barRow(lab,(p.scores&&p.scores[k])||0,CATINFO[k],true,CATCOLORS[ci]));
    const d=p.detail||{};GROUPS.forEach(([k,lab,keys])=>{const cv=(p.scores&&p.scores[k]);html+=`<h3>${lab}<span>${cv!=null?cv:''}</span></h3>`;keys.forEach(key=>{if(key in d)html+=barRow(key,d[key],SUBINFO[key]);});});
    html+=`<div style="font-size:10px;opacity:.5;margin-top:14px">将鼠标停留在任意评分项上，可查看该指标的含义。</div>`;
    side.style.display='block';side.innerHTML=html;
  }else{
    const label=currentStep==='cull'?(p.badge||TIER_NAME[p.tier]||'清晰度结果'):(currentStep==='trash'?'待最终确认':'照片');
    side.style.display='block';
    side.innerHTML=`<h3>${currentStep==='cull'?'清晰度':currentStep==='trash'?'软件回收站':'照片'}</h3><div style="font-size:13px;opacity:.9">${escHtml(p.name)}</div><div style="font-size:18px;font-weight:700;margin-top:8px">${escHtml(label)}</div>${currentStep==='trash'?`<div style="font-size:11px;opacity:.7;margin-top:8px;line-height:1.5">原位置：${escHtml(p.original_path||'')}</div>`:''}${currentStep==='rank'&&p.score!=null?`<div style="font-size:11px;opacity:.58;margin-top:4px">综合评分 ${p.score}</div>`:''}`;
  }
  loadExif(p.path,side);
}
function exifRow(label,val,allowHtml=false){
  return `<div class="exrow"><span class="lab">${escHtml(label)}</span><span class="val">${allowHtml?val:escHtml(val)}</span></div>`;
}
function loadExif(path,side){
  const token=path;side.dataset.exifToken=token;
  fetch('/api/exif?path='+encodeURIComponent(path)).then(r=>r.json()).then(e=>{
    if(side.dataset.exifToken!==token)return; // user moved on
    let rows='',wantMap=null;
    if(e.date)rows+=exifRow('日期',e.date);
    if(e.time)rows+=exifRow('时间',(e.time||'').split('+')[0]);
    if(e.camera)rows+=exifRow('相机',e.camera);
    if(e.lens)rows+=exifRow('镜头',e.lens);
    const settings=[e.focal,e.aperture,e.shutter,e.iso].filter(Boolean)
      .map(v=>`<span style="white-space:nowrap">${escHtml(v)}</span>`).join(' · ');
    if(settings)rows+=exifRow('拍摄参数',settings,true);
    if(e.lat!=null&&e.lon!=null){
      const c=e.lat.toFixed(5)+',&nbsp;'+e.lon.toFixed(5);
      rows+=exifRow('位置',`<a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank" rel="noopener" style="white-space:nowrap">${c}</a>`,true);
      rows+=`<div class="exmap"><div class="mapslot" id="exMapSlot"><span class="mappin"></span></div>`
        +`<span class="cred"><a href="https://openfreemap.org/" target="_blank" rel="noopener">OpenFreeMap</a> © `
        +`<a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> · 地图数据 © `
        +`<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a></span></div>`;
      rows+=`<div class="exrow"><span class="lab">具体地点</span><span class="val" id="geoLabel">正在解析…</span></div>`;
      wantMap={lat:e.lat,lon:e.lon};
    }
    if(!rows)rows=`<div style="font-size:12px;opacity:.5">没有可读取的 EXIF 信息。</div>`;
    side.insertAdjacentHTML('beforeend',`<h3>照片信息</h3>${rows}`);
    if(wantMap){
      mountExifMap(wantMap.lat,wantMap.lon);
      fetch('/api/reverse-geocode?lat='+wantMap.lat+'&lon='+wantMap.lon).then(r=>r.json()).then(g=>{const el=document.getElementById('geoLabel');if(el)el.textContent=g.label||'未解析到具体地点';}).catch(()=>{const el=document.getElementById('geoLabel');if(el)el.textContent='地点解析失败';});
    }
  }).catch(()=>{});
}

/* GPS map — MapLibre over OpenFreeMap vector tiles.
   One map instance is built lazily on the first geotagged photo and then
   re-parented into each new sidebar; creating a WebGL context per photo would
   hit the browser's context limit within a few dozen frames. */
const MAP_STYLES={light:{{ map_style_light|tojson }},dark:{{ map_style_dark|tojson }}};
let exMapEl=null,exMap=null,exMapLoading=null,exMapTheme=null,exMapDead=false;
function hideExMap(){const s=document.getElementById('exMapSlot');
  if(s&&s.closest('.exmap'))s.closest('.exmap').style.display='none';}
function currentMapStyle(){return 'light';}
function loadMapLibre(){
  if(window.maplibregl)return Promise.resolve(true);
  if(exMapLoading)return exMapLoading;
  exMapLoading=new Promise(res=>{
    const css=document.createElement('link');
    css.rel='stylesheet';css.href='/vendor/maplibre-gl.css';
    document.head.appendChild(css);
    const js=document.createElement('script');
    js.src='/vendor/maplibre-gl-csp.js';
    js.onload=()=>{try{maplibregl.setWorkerUrl('/vendor/maplibre-gl-csp-worker.js');}catch(err){}res(true);};
    js.onerror=()=>res(false);
    document.head.appendChild(js);
  });
  return exMapLoading;}
function mountExifMap(lat,lon){
  const slot=document.getElementById('exMapSlot');
  if(!slot)return;
  if(exMapDead){slot.closest('.exmap').style.display='none';return;}
  loadMapLibre().then(ok=>{
    const s=document.getElementById('exMapSlot');
    if(!s)return;                       // user moved on while it loaded
    if(!ok){exMapDead=true;hideExMap();return;}   // no MapLibre: never retry
    const theme=currentMapStyle();
    if(!exMap){
      exMapEl=document.createElement('div');
      exMapEl.style.cssText='position:absolute;inset:0';
      s.insertBefore(exMapEl,s.firstChild);
      exMapTheme=theme;
      exMap=new maplibregl.Map({container:exMapEl,style:MAP_STYLES[theme],
        center:[lon,lat],zoom:13,attributionControl:false,
        interactive:false,refreshExpiredTiles:false});
      // No tiles (offline, or the provider is down) means an empty grey box —
      // hide the block rather than show one. Not latched: the next photo
      // tries again, so the map comes back on its own once the net does.
      exMap.on('error',()=>{
        if(exMap&&exMap.isStyleLoaded&&exMap.isStyleLoaded())return;
        hideExMap();});
    }else{
      if(exMapEl.parentNode!==s)s.insertBefore(exMapEl,s.firstChild);
      if(theme!==exMapTheme){exMapTheme=theme;exMap.setStyle(MAP_STYLES[theme]);}
      exMap.jumpTo({center:[lon,lat],zoom:13});
    }
    exMap.resize();
  });}
document.getElementById('lbClose').onclick=closeLb;
document.getElementById('lightbox').addEventListener('click',e=>{if(e.target.id==='lightbox')closeLb();});
document.getElementById('lbPrev').onclick=()=>{lbIndex=(lbIndex-1+lbList.length)%lbList.length;showLb();};
document.getElementById('lbNext').onclick=()=>{lbIndex=(lbIndex+1)%lbList.length;showLb();};
function lbRemoveCurrent(){const p=lbList[lbIndex];if(!p)return;
  fetch('/api/exclude',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p.path})}).then(r=>r.json()).then(d=>{
    renderRank(d.photos||[]);setRemoved(d.removed);lbList=photos.slice();
    if(!lbList.length){closeLb();return;}if(lbIndex>=lbList.length)lbIndex=lbList.length-1;showLb();});}
document.getElementById('lbRemove').onclick=lbRemoveCurrent;
document.getElementById('lbDelete').onclick=lbDeleteCurrent;
document.getElementById('lbTrashRestore').onclick=()=>{const p=lbList[lbIndex];if(!p)return;if(currentStep==='trash')trashRestoreOne(Number(p.id),true);else restoreFromReviewCard(Number(p.trash_id));};
document.getElementById('lbTrashPurge').onclick=()=>{const p=lbList[lbIndex];if(p)trashPurgeOne(Number(p.id),true);};
document.getElementById('lbRestore').onclick=()=>restoreAll(true);
document.getElementById('lbToggle').onclick=()=>{const p=lbList[lbIndex];if(!p)return;
  const next=NEXT_TIER[p.tier||'sharp'];
  fetch('/api/toggle-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p.path,tier:next})}).then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'bad');return;}
    document.getElementById('sSharp').textContent=d.sharp;document.getElementById('sSoft').textContent=d.soft;document.getElementById('sBlurry').textContent=d.blurry;
    const pp=photos.find(x=>x.path===p.path||x.path===d.path);
    if(pp){pp.tier=d.tier;pp.badge=d.badge;pp.badgeType=d.badgeType;pp.kept=d.kept;pp.rejected=!d.kept;pp.move_selected=!!d.move_selected;if(d.path)pp.path=d.path;if(d.thumb)pp.thumb=d.thumb;}
    p.tier=d.tier;p.move_selected=!!d.move_selected;if(d.path)p.path=d.path;
    lastCullSig='';lastCullMoveSig='';renderCullStep(photos);updateCullMoveButton();showLb();});};
document.getElementById('lbPhoneBg').onclick=()=>{const p=lbList[lbIndex];if(p)togglePhoneBg(p.path);};
document.getElementById('lbMoveSelect').onclick=()=>{const p=lbList[lbIndex];if(p&&p.tier==='blurry')setBlurryMoveSelection(p.path,p.move_selected===false);};
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lightbox').classList.contains('open'))return;
  if(e.key==='Escape')closeLb();
  if(e.key==='ArrowLeft')document.getElementById('lbPrev').click();
  if(e.key==='ArrowRight')document.getElementById('lbNext').click();
  if(currentStep==='rank'&&(e.key==='b'||e.key==='B')){e.preventDefault();const p=lbList[lbIndex];if(p)togglePhoneBg(p.path);}
  if(currentStep==='rank'&&(e.key==='x'||e.key==='X'||e.key==='Delete'||e.key==='Backspace')){e.preventDefault();lbRemoveCurrent();}});

/* export */
document.getElementById('exportBtn').onclick=async function(){
  const old='⬇ 导出优选照片…';
  this.disabled=true;this.textContent='正在导出…';
  try{
    const r=await fetch('/api/export',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({topn:parseInt((document.getElementById('topn')||{}).value)||50})
    });
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    const extra=d.failed?('；'+d.failed+' 张失败'):'';
    toast('✓ 已复制 '+d.copied+' 张照片'+extra+'\n'+d.dest,d.failed?'bad':'good');
  }catch(err){
    toast('导出失败：'+(err.message||'未知错误'),'bad');
  }finally{
    this.disabled=false;this.textContent=old;
  }
};

document.getElementById('exportPbgBtn').onclick=async function(){
  const old='📱 导出手机壁纸…';
  this.disabled=true;this.textContent='正在导出…';
  try{
    const r=await fetch('/api/export-phonebg',{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
    });
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    if(!d.copied&&!d.cropped){
      toast(d.note||'还没有标记为手机壁纸的照片。','bad');return;
    }
    const extra=d.failed?('；'+d.failed+' 项失败'):'';
    toast('✓ 原图 '+d.copied+' 张 · 壁纸 '+d.cropped+' 张'+extra+'\n'+d.dest,
          d.failed?'bad':'good');
  }catch(err){
    toast('导出失败：'+(err.message||'未知错误'),'bad');
  }finally{
    this.disabled=false;this.textContent=old;
  }
};

document.getElementById('moveBlurryBtn').onclick=async function(){
  const before=cullMoveCounts();
  if(!before.selected)return;
  const ok=await askBatchConfirm(
    '批量移入软件回收站',
    '将选中的 '+before.selected+' 张模糊照片移入 PhotoCurator 软件回收站。之后仍可恢复。',
    '移入回收站'
  );
  if(!ok)return;
  this.disabled=true;this.textContent='正在提交 '+before.selected+' 张…';
  try{
    const r=await fetch('/api/move-blurry',{method:'POST'});
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    (photos||[]).forEach(p=>{
      if(p&&p.tier==='blurry'&&p.move_selected!==false&&visibleInReview(p)){
        p.lifecycle='pending_trash';
        p.move_selected=false;
      }
    });
    toast('已提交后台处理 '+(d.queued||0)+' 张模糊照片','good');
    lastCullSig='';lastCullMoveSig='';renderCullStep(photos);refreshTaskCenter();
  }catch(err){
    toast('提交失败：'+(err.message||'未知错误'),'bad');
  }finally{
    this.disabled=false;updateCullMoveButton();
  }
}
const showingNode=document.getElementById('sShowing');
if(showingNode){
  const syncShowing=()=>{document.getElementById('statusShowing').textContent=showingNode.textContent||'0';};
  new MutationObserver(syncShowing).observe(showingNode,{childList:true,characterData:true,subtree:true});
  syncShowing();
}
updateSourceUi();
renderWorkspaceLanding();
document.documentElement.dataset.uiReady='1';
</script></body></html>'''


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.route('/')
def index():
    return render_template_string(
        HTML,
        map_style_light=MAP_STYLE_LIGHT,
        map_style_dark=MAP_STYLE_DARK,
        app_version=APP_VERSION,
    )


@app.route('/api/shortcuts')
def api_shortcuts():
    # First-paint contract: this route must be database-only and fast.
    demo = builtin_demo_status()
    demo_real = os.path.normcase(os.path.realpath(demo.get('folder') or str(DEMO_ROOT)))
    recent = []
    for item in load_recents():
        try:
            real = os.path.normcase(os.path.realpath(item))
            if real == demo_real or Path(real).name.lower() == '.codespaces_demo':
                continue
            recent.append(item)
        except Exception:
            recent.append(item)

    try:
        sources = catalog_list_sources(INDEX_DB, refresh=False)
    except Exception:
        sources = []
        logger.warning("fast data source catalog list failed", exc_info=True)

    return jsonify({
        'sd': [],
        'recent': recent,
        'sources': sources,
        'rawpy': HAS_RAWPY,
        'heif': HAS_HEIF,
        'codespaces': bool(CODESPACES_PUBLIC_HOST),
        'demo_folder': demo.get('folder'),
        'demo_ready': bool(demo.get('ready')),
        'demo_preparing': bool(demo.get('preparing')),
        'demo_count': int(demo.get('count') or 0),
        'demo_folders': int(demo.get('folders') or 0),
    })


@app.route('/api/environment-refresh')
def api_environment_refresh():
    # Periodic refresh is presence-only.  Never crawl DCIM or touch every
    # mounted filesystem from this timer path: a sleeping/slow USB disk can
    # stall Windows for long enough to make the pywebview host look hung.
    started = time.monotonic()
    try:
        sources = catalog_list_sources(INDEX_DB, refresh=True)
        devices = catalog_discover_devices(INDEX_DB)
    except Exception:
        sources = []
        devices = []
        logger.warning("data source connection refresh failed", exc_info=True)
    elapsed = time.monotonic() - started
    if elapsed > 1.0:
        logger.warning("slow environment refresh: %.3fs", elapsed)
    return jsonify({
        'sources': sources,
        'devices': devices,
        'sd': [],
        'elapsed_ms': int(elapsed * 1000),
    })


@app.route('/api/demo-status')
def api_demo_status():
    return jsonify(builtin_demo_status())


@app.route('/api/demo-prepare', methods=['POST'])
def api_demo_prepare():
    wait = str(request.args.get('wait') or '').strip() in {'1', 'true', 'yes'}
    status = (
        prepare_builtin_demo_wait(timeout=30.0)
        if wait else prepare_builtin_demo_async()
    )
    return jsonify(status)


@app.route('/api/sources')
def api_sources():
    try:
        refresh = request.args.get('refresh', '1') not in {'0', 'false', 'no'}
        return jsonify({'sources': catalog_list_sources(INDEX_DB, refresh=refresh)})
    except Exception as exc:
        logger.warning("data source catalog list failed", exc_info=True)
        return jsonify({'sources': [], 'error': str(exc)}), 500


@app.route('/api/storage-summary')
def api_storage_summary():
    try:
        return jsonify(catalog_storage_summary(DATA_ROOT, INDEX_DB))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/storage-clear', methods=['POST'])
def api_storage_clear():
    body = request.get_json(silent=True) or {}
    category = str(body.get('category') or '').strip().lower()
    if category not in {'previews', 'features', 'logs'}:
        return jsonify({'error': '不支持的清理类型'}), 400
    if category in {'previews', 'features'}:
        active = [
            key for key in ('cull', 'dedup', 'rank')
            if state.get(key, {}).get('running')
        ]
        if active:
            return jsonify({
                'error': '分析任务运行中，请等待分析结束后再清理可重建缓存',
                'active': active,
            }), 409
    try:
        result = clear_rebuildable_storage(DATA_ROOT, category)
        _activity('清理软件数据', '', f"{category} · {result.get('freed_bytes', 0)} bytes")
        result['storage'] = catalog_storage_summary(DATA_ROOT, INDEX_DB)
        return jsonify(result)
    except Exception as exc:
        logger.warning("storage cleanup failed", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/browse', methods=['POST'])
def api_browse():
    folder = native_folder_dialog("选择照片文件夹")
    if folder and Path(folder).is_dir():
        state['folder'] = folder
        save_recent(folder)
        source = None
        try:
            source = catalog_register_source(INDEX_DB, folder)
        except Exception:
            logger.warning("data source registration failed", exc_info=True)
        return jsonify({'folder': folder, 'source': source})
    return jsonify({'folder': None})


@app.route('/vendor/<path:name>')
def vendor_file(name):
    """Serve the vendored MapLibre build. Whitelisted by name so the route
    can't be walked out of the vendor directory."""
    mime = VENDOR_FILES.get(name)
    if not mime:
        abort(404)
    f = VENDOR_DIR / name
    if not f.is_file():
        abort(404)
    resp = send_file(str(f), mimetype=mime)
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.route('/api/catalog-root/<root_id>')
def api_catalog_root(root_id):
    try:
        # Refresh device connection state before returning an offline/online view.
        catalog_list_sources(INDEX_DB)
        snap = catalog_root_snapshot(
            INDEX_DB,
            root_id,
            limit=request.args.get('limit', 2000, type=int),
            offset=request.args.get('offset', 0, type=int),
        )
        if not snap:
            return jsonify({'error': '图库不存在'}), 404
        for item in snap.get('items') or []:
            item['thumb'] = '/api/catalog-thumb?media_id=' + quote(str(item['media_id']))
        return jsonify(snap)
    except Exception as exc:
        logger.warning("catalog root snapshot failed", exc_info=True)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/catalog-thumb')
def api_catalog_thumb():
    media_id = str(request.args.get('media_id') or '').strip()
    if not media_id:
        abort(404)
    try:
        record = catalog_media_record(INDEX_DB, media_id)
    except Exception:
        record = None
    if not record:
        abort(404)
    cached = OFFLINE_PREVIEW_DIR / f"{media_id}.jpg"
    if cached.is_file():
        return send_file(str(cached), mimetype='image/jpeg')

    candidates = []
    rel = str(record.get('relative_path') or '')
    current_root = str(record.get('current_root') or '')
    if current_root and rel:
        candidates.append(os.path.join(current_root, rel))
    candidates.extend([
        str(record.get('current_path') or ''),
        str(record.get('original_path') or ''),
    ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            made = make_thumb_file(candidate)
            if made and Path(made).is_file():
                return send_file(str(made), mimetype='image/jpeg')
    abort(404)


@app.route('/api/thumb')
def api_thumb():
    p = _safe_image_path(request.args.get('path', ''))
    if not p:
        abort(404)
    f = make_thumb_file(str(p))
    if not f:
        abort(404)
    resp = send_file(str(f))
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


def _raw_preview_file(image_path):
    """Browser-displayable JPEG for a RAW or HEIC file (browsers can't render
    CR2/NEF, and only Safari renders HEIC).
    Cached on disk like thumbnails, keyed by path+mtime."""
    try:
        mtime = os.path.getmtime(image_path)
    except OSError:
        return None
    key = hashlib.md5(f"{image_path}:{mtime}:preview-v1".encode()).hexdigest()
    out = THUMB_DIR / f"{key}_full.jpg"
    if out.exists():
        return out
    try:
        with open_image_pil(image_path) as src:
            img = ImageOps.exif_transpose(src).convert('RGB')
            img.save(out, format='JPEG', quality=90)
        return out
    except Exception as e:
        logger.warning(f"raw preview fail {image_path}: {e}")
        return None


@app.route('/api/image')
def api_image():
    p = _safe_image_path(request.args.get('path', ''))
    if not p:
        abort(404)
    if needs_jpeg_preview(p):
        f = _raw_preview_file(str(p))
        if not f:
            abort(404)
        resp = send_file(str(f))
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    return send_file(str(p))


def _ratio(v):
    try:
        return float(v)
    except Exception:
        try:
            return v.numerator / v.denominator
        except Exception:
            return None


def _gps_to_deg(val, ref):
    try:
        d = _ratio(val[0]); m = _ratio(val[1]); s = _ratio(val[2])
        deg = d + m / 60.0 + s / 3600.0
        if ref in ('S', 'W'):
            deg = -deg
        return round(deg, 6)
    except Exception:
        return None


def extract_exif(path):
    """Pull human-friendly capture details from a photo's EXIF."""
    from PIL.ExifTags import TAGS, GPSTAGS
    out = {}
    try:
        # RAW-aware: for RAW files this reads the embedded JPEG preview,
        # which carries the camera's full EXIF block.
        with open_image_pil(path) as img:
            exif = img.getexif()
            if not exif:
                return out
            tags = {TAGS.get(k, k): v for k, v in exif.items()}
            # Date / time
            dt = tags.get('DateTimeOriginal') or tags.get('DateTime')
            if isinstance(dt, str) and ' ' in dt:
                d, t = dt.split(' ', 1)
                out['date'] = d.replace(':', '-')
                out['time'] = t
            # Camera / lens
            make = (tags.get('Make') or '').strip()
            model = (tags.get('Model') or '').strip()
            if make or model:
                out['camera'] = (make + ' ' + model).strip() if model and not model.startswith(make) else (model or make)
            lens = tags.get('LensModel')
            if lens:
                out['lens'] = str(lens).strip()
            # Shooting settings (live in the Exif sub-IFD)
            try:
                sub = exif.get_ifd(0x8769)
                subtags = {TAGS.get(k, k): v for k, v in sub.items()}
            except Exception:
                subtags = {}
            dto = subtags.get('DateTimeOriginal')
            if isinstance(dto, str) and ' ' in dto and 'date' not in out:
                d, t = dto.split(' ', 1)
                out['date'] = d.replace(':', '-'); out['time'] = t
            fnum = _ratio(subtags.get('FNumber'))
            if fnum:
                out['aperture'] = 'f/' + (str(int(fnum)) if fnum == int(fnum) else str(round(fnum, 1)))
            exp = subtags.get('ExposureTime')
            if exp is not None:
                er = _ratio(exp)
                if er and er < 1:
                    out['shutter'] = '1/' + str(int(round(1 / er))) + 's'
                elif er:
                    out['shutter'] = str(round(er, 1)) + 's'
            iso = subtags.get('ISOSpeedRatings') or subtags.get('PhotographicSensitivity')
            if iso:
                out['iso'] = 'ISO ' + str(iso if not isinstance(iso, (list, tuple)) else iso[0])
            fl = _ratio(subtags.get('FocalLength'))
            if fl:
                out['focal'] = str(int(round(fl))) + 'mm'
            if 'lens' not in out:
                lm = subtags.get('LensModel')
                if lm:
                    out['lens'] = str(lm).strip()
            # GPS
            try:
                gps = exif.get_ifd(0x8825)
            except Exception:
                gps = None
            if gps:
                g = {GPSTAGS.get(k, k): v for k, v in gps.items()}
                lat = _gps_to_deg(g.get('GPSLatitude'), g.get('GPSLatitudeRef'))
                lon = _gps_to_deg(g.get('GPSLongitude'), g.get('GPSLongitudeRef'))
                if lat is not None and lon is not None:
                    out['lat'] = lat; out['lon'] = lon
    except Exception:
        pass
    return out


@app.route('/api/exif')
def api_exif():
    p = _safe_image_path(request.args.get('path', ''))
    if not p:
        abort(404)
    return jsonify(extract_exif(str(p)))


@app.route('/api/activity')
def api_activity():
    try:
        limit = min(200, max(1, int(request.args.get('limit', 60))))
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            rows = db.execute(
                "SELECT ts,action,path,detail FROM activity_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return jsonify({'items': [
            {'ts': r[0], 'action': r[1], 'path': r[2], 'detail': r[3]} for r in rows
        ]})
    except Exception:
        return jsonify({'items': []})


@app.route('/api/reverse-geocode')
def api_reverse_geocode():
    try:
        lat = float(request.args.get('lat')); lon = float(request.args.get('lon'))
        if not np.isfinite(lat) or not np.isfinite(lon):
            raise ValueError("non-finite coordinates")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("coordinates out of range")
    except Exception:
        return jsonify({'label': ''}), 400
    key = f"{lat:.4f},{lon:.4f}"
    try:
        with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
            row = db.execute("SELECT label FROM geocode_cache WHERE key=?", (key,)).fetchone()
        if row:
            return jsonify({'label': row[0], 'cached': True})
    except Exception:
        pass
    label = ''
    try:
        # Serialize uncached reverse-geocode requests and space them out. Rapid
        # lightbox browsing can otherwise create multiple concurrent requests.
        global _GEOCODE_LAST_AT
        with _GEOCODE_LOCK:
            wait = 1.05 - (time.monotonic() - _GEOCODE_LAST_AT)
            if wait > 0:
                time.sleep(wait)
            url = ('https://nominatim.openstreetmap.org/reverse?format=jsonv2&accept-language=zh-CN'
                   f'&zoom=18&addressdetails=1&lat={lat:.7f}&lon={lon:.7f}')
            req = urllib.request.Request(url, headers={'User-Agent': f'PhotoCurator/{APP_VERSION}'})
            try:
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
            finally:
                _GEOCODE_LAST_AT = time.monotonic()
        label = str(data.get('display_name') or '')
        if label:
            with _DB_LOCK, connect_db(INDEX_DB, timeout=10) as db:
                db.execute("INSERT OR REPLACE INTO geocode_cache(key,label,updated_at) VALUES(?,?,?)",
                           (key, label, time.time()))
                db.commit()
    except Exception:
        pass
    return jsonify({'label': label, 'cached': False})


@app.route('/api/run/<step>', methods=['POST'])
def api_run(step):
    data = request.get_json() or {}
    if step not in ('cull', 'dedup', 'rank'):
        return jsonify({'error': '无效处理步骤'}), 404

    raw_folder = str(data.get('folder') or '').strip()
    if not raw_folder:
        return jsonify({'error': '请先选择照片文件夹'}), 400
    folder = os.path.realpath(os.path.expanduser(raw_folder))
    if not Path(folder).is_dir():
        return jsonify({'error': '照片文件夹不存在或无法访问'}), 400

    recursive = bool(data.get('recursive', True))
    compare_scope = str(data.get('compare_scope') or 'folder')
    if compare_scope not in ('folder', 'global'):
        compare_scope = 'folder'
    output_mode = str(data.get('output_mode') or 'source')
    if output_mode not in ('source', 'root', 'custom'):
        output_mode = 'source'
    custom_output = str(data.get('custom_output') or '').strip()

    # Parse and validate all request-specific parameters before reserving the
    # run slot. A malformed request must never leave a false running flag.
    rank_config = None
    try:
        if step == 'cull':
            strictness = min(1.6, max(0.6, float(data.get('opt') or 1.0)))
            adaptive = bool(data.get('adaptive', True))
            rescue_on = bool(data.get('rescue', True))
            target, args = run_cull, (folder, strictness, adaptive, rescue_on, recursive)
        elif step == 'dedup':
            threshold = min(0.95, max(0.5, float(data.get('opt') or 0.8)))
            target, args = run_dedup, (
                folder, threshold, data.get('ftype', 'all'), data.get('pair', 'both'),
                recursive, compare_scope
            )
        else:
            topn = min(500, max(1, int(data.get('topn', 50))))
            raw_weights = data.get('weights') or state['weights']
            clean_weights = {}
            for k in CATEGORIES:
                v = float(raw_weights.get(k, DEFAULT_WEIGHTS[k]))
                if not np.isfinite(v):
                    v = DEFAULT_WEIGHTS[k]
                clean_weights[k] = min(100.0, max(0.0, v))
            rank_config = (topn, clean_weights)
            target, args = run_rank, (
                folder, data.get('ftype', 'all'), data.get('pair', 'both'), recursive
            )
    except (TypeError, ValueError):
        return jsonify({'error': '处理参数无效，请恢复默认设置后重试'}), 400

    # Flask serves API requests concurrently. Admission + reservation must be
    # one atomic section, otherwise simultaneous requests can both observe an
    # idle step and start duplicate workers or bypass the Rank/core exclusion.
    with _RUN_GATE_LOCK:
        active = [k for k in ('cull', 'dedup', 'rank') if state[k].get('running')]
        if state[step].get('running'):
            return jsonify({'error': '该分析任务已经在运行', 'active': step}), 409
        if ((step == 'rank' and active)
                or (step in ('cull', 'dedup') and state['rank'].get('running'))):
            return jsonify({
                'error': '精选评分正在运行，请先等待或停止评分任务',
                'active': 'rank'
            }), 409

        state['folder'] = folder
        state['scan'].update({
            'recursive': recursive,
            'compare_scope': compare_scope,
            'output_mode': output_mode,
            'custom_output': custom_output,
        })
        if rank_config is not None:
            state['topn'], state['weights'] = rank_config

        state[step]['running'] = True
        state[step]['cancel'] = False
        try:
            threading.Thread(
                target=target, args=args, daemon=True,
                name=f'photocurator-{step}'
            ).start()
        except Exception:
            state[step]['running'] = False
            raise

    save_recent(folder)
    _activity('启动分析', folder, step)
    return jsonify({'ok': True})

@app.route('/api/stop/<step>', methods=['POST'])
def api_stop(step):
    """Signal a running step to cancel at its next iteration (e.g. wrong folder)."""
    if step in ('cull', 'dedup', 'rank'):
        state[step]['cancel'] = True
        return jsonify({'ok': True})
    abort(404)


@app.route('/api/progress/<step>')
def api_progress(step):
    if step == 'cull':
        s = state['cull']
        all_photos = s['photos']
        limit = UI_LIVE_RESULT_CAP if s['running'] else UI_RESULT_CHUNK
        photos = all_photos[:limit]
        return jsonify({'running': s['running'], 'complete': bool(s.get('complete')),
                        'progress': s['progress'], 'status': s['status'],
                        'src_folder': s.get('src_folder'), 'photos': photos,
                        'truncated': (not s['running'] and len(all_photos) > len(photos)),
                        'result_total': len(all_photos),
                        'stats': {'images': len(all_photos), 'sharp': s['sharp'],
                                  'soft': s['soft'], 'blurry': s['blurry'],
                                  'move_selected': sum(
                                      1 for p in all_photos
                                      if p.get('tier') == 'blurry' and p.get('move_selected', True)
                                  ),
                                  'cache_hits': s.get('cache_hits',0),
                                  'folder_status': s.get('folder_status',{})}})
    if step == 'dedup':
        s = state['dedup']
        all_photos = s['photos']
        photos = all_photos[:UI_RESULT_CAP]
        duplicate_photos = sum(g.get('count', 0) for g in s.get('groups_data', []) if g.get('count', 0) > 1)
        return jsonify({'running': s['running'], 'complete': bool(s.get('complete')),
                        'progress': s['progress'], 'status': s['status'],
                        'src_folder': s.get('src_folder'), 'photos': photos,
                        'truncated': (not s['running'] and len(all_photos) > len(photos)),
                        'result_total': len(all_photos),
                        'stats': {'groups': s['groups'],
                                  'duplicate_groups': len(all_photos),
                                  'duplicate_photos': duplicate_photos,
                                  'pending_groups': sum(1 for g in all_photos if str(g.get('status') or 'pending') == 'pending'),
                                  'reviewed_groups': sum(1 for g in all_photos if str(g.get('status') or 'pending') == 'reviewed'),
                                  'updated_groups': sum(1 for g in all_photos if str(g.get('status') or 'pending') == 'updated')}})
    if step == 'rank':
        s = state['rank']
        now = time.time()
        if (not s['running']) or now - float(s.get('preview_at', 0.0)) >= 1.0:
            s['preview'] = build_topn()
            s['preview_at'] = now
        return jsonify({'running': s['running'], 'complete': bool(s.get('complete')),
                        'progress': s['progress'], 'status': s['status'],
                        'src_folder': s.get('src_folder'), 'photos': s.get('preview', []),
                        'stats': {'images': s['total'], 'cache_hits': s.get('cache_hits',0)}})
    abort(404)


@app.route('/api/results/cull')
def api_cull_results_chunk():
    """Chunked Cull result transport: no visible pagination, bounded payloads."""
    s = state['cull']
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = min(UI_RESULT_CHUNK, max(1, int(request.args.get('limit', UI_RESULT_CHUNK))))
    except (TypeError, ValueError):
        return jsonify({'error': '结果范围无效'}), 400
    all_photos = s.get('photos', [])
    rows = all_photos[offset:offset + limit]
    return jsonify({'photos': rows, 'offset': offset, 'next_offset': offset + len(rows),
                    'total': len(all_photos), 'done': offset + len(rows) >= len(all_photos)})


@app.route('/api/results/dedup')
def api_dedup_results_chunk():
    """Chunked duplicate-group transport for extreme libraries."""
    s = state['dedup']
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = min(UI_RESULT_CHUNK, max(1, int(request.args.get('limit', UI_RESULT_CHUNK))))
    except (TypeError, ValueError):
        return jsonify({'error': '结果范围无效'}), 400
    status_filter = str(request.args.get('status') or 'all')
    all_groups = s.get('photos', [])
    if status_filter in ('pending','reviewed','updated'):
        all_groups = [g for g in all_groups if str(g.get('status') or 'pending') == status_filter]
    rows = all_groups[offset:offset + limit]
    counts = {
        key: sum(1 for g in s.get('photos', []) if str(g.get('status') or 'pending') == key)
        for key in ('pending','reviewed','updated')
    }
    return jsonify({'photos': rows, 'offset': offset, 'next_offset': offset + len(rows),
                    'total': len(all_groups), 'done': offset + len(rows) >= len(all_groups),
                    'status': status_filter, 'counts': counts})


@app.route('/api/dedup-select', methods=['POST'])
def api_dedup_select():
    """Toggle one photo's kept state; each similarity group must keep >= 1."""
    _sync_dedup_with_cull()
    s = state['dedup']
    data = request.get_json() or {}
    try:
        gid = int(data.get('group_id'))
    except (TypeError, ValueError):
        return jsonify({'error': '相似组编号无效'}), 400
    path = str(data.get('path') or '')
    group = next((g for g in s.get('groups_data', []) if g.get('group_id') == gid), None)
    if not group:
        return jsonify({'error': '未找到这个相似组'}), 404
    if s.get('running') and not group.get('ready'):
        return jsonify({'error': '这个相似组仍在分析中'}), 409
    if not s.get('running') and not s.get('complete'):
        return jsonify({'error': '相似照片筛选尚未完成'}), 409
    member_paths = [m.get('path') for m in group.get('members', []) if m.get('path')]
    allowed = _cull_allowed_for_dedup()
    if allowed is not None and path not in allowed:
        return jsonify({'error': '这张照片已在清晰度结果中标为模糊，不能设为相似组保留项'}), 409
    if path not in member_paths:
        return jsonify({'error': '这张照片不属于当前相似组'}), 400

    selected = set(group.get('selected_paths') or [])
    if not selected:
        selected.add(member_paths[0])

    if path in selected:
        if len(selected) <= 1:
            return jsonify({'error': '每个相似组至少保留 1 张照片'}), 409
        selected.remove(path)
        now_selected = False
    else:
        selected.add(path)
        now_selected = True

    group['selected_paths'] = [p for p in member_paths if p in selected]
    for member in group.get('members', []):
        member['selected'] = member.get('path') in selected
    if group.get('group_key'):
        _persist_similarity_group_members(group['group_key'], group.get('members', []))

    s['kept_paths'] = list(s.get('singleton_paths') or []) + [
        p for g in s.get('groups_data', [])
        for p in (g.get('selected_paths') or [])
    ]
    s['photos'] = [g for g in s.get('groups_data', []) if g.get('count', 0) > 1]
    state['rank']['preview_at'] = 0.0
    _activity('相似组选优', path, '保留' if now_selected else '取消保留')
    return jsonify({'ok': True, 'photos': s['photos'][:UI_RESULT_CHUNK],
                    'changed_group': group,
                    'result_total': len(s['photos']),
                    'kept': len(s['kept_paths']), 'selected': now_selected})


@app.route('/api/dedup-complete', methods=['POST'])
def api_dedup_complete():
    """Explicitly finish one multi-photo group without forcing file movement."""
    data = request.get_json() or {}
    try:
        gid = int(data.get('group_id'))
    except (TypeError, ValueError):
        return jsonify({'error': '相似组编号无效'}), 400
    group = next((g for g in state['dedup'].get('groups_data', [])
                  if g.get('group_id') == gid), None)
    if not group:
        return jsonify({'error': '未找到这个相似组'}), 404
    active = [m for m in group.get('members', [])
              if m.get('lifecycle') not in
              ('pending_trash','pending_permanent_delete','trashed','permanently_deleted')]
    if not active:
        return jsonify({'error': '这个相似组没有可保留照片'}), 409
    # "完成本组" means every currently active photo is accepted as kept.
    selected = [m.get('path') for m in active if m.get('path')]
    group['selected_paths'] = selected
    selected_set = set(selected)
    for m in group.get('members', []):
        m['selected'] = m.get('path') in selected_set
    group['status'] = 'reviewed'
    if group.get('group_key'):
        _set_similarity_group_status(group['group_key'], 'reviewed')
        _persist_similarity_group_members(group['group_key'], group.get('members', []))
    state['dedup']['kept_paths'] = list(state['dedup'].get('singleton_paths') or []) + [
        p for g in state['dedup'].get('groups_data', [])
        for p in (g.get('selected_paths') or [])
    ]
    _activity('完成相似组复核', '', f'组 {gid} · 保留 {len(selected)} 张')
    return jsonify({'ok': True, 'changed_group': group,
                    'status': 'reviewed', 'kept': len(selected)})


@app.route('/api/dedup-group-action', methods=['POST'])
def api_dedup_group_action():
    """Fast keeper presets for one similarity group."""
    _sync_dedup_with_cull()
    data = request.get_json() or {}
    try:
        gid = int(data.get('group_id'))
    except (TypeError, ValueError):
        return jsonify({'error': '相似组编号无效'}), 400
    mode = str(data.get('mode') or 'best1')
    if mode not in ('best1', 'best2', 'all'):
        return jsonify({'error': '无效保留方式'}), 400
    s = state['dedup']
    group = next((g for g in s.get('groups_data', []) if g.get('group_id') == gid), None)
    if not group:
        return jsonify({'error': '未找到这个相似组'}), 404
    if s.get('running') and not group.get('ready'):
        return jsonify({'error': '这个相似组仍在分析中'}), 409
    if not s.get('running') and not s.get('complete'):
        return jsonify({'error': '相似照片筛选尚未完成'}), 409
    members = list(group.get('members') or [])
    allowed = _cull_allowed_for_dedup()
    eligible = [m for m in members if allowed is None or m.get('path') in allowed]
    if not eligible:
        return jsonify({'error': '这个相似组当前没有清晰度复核后可保留的照片'}), 409
    keep_n = len(eligible) if mode == 'all' else (2 if mode == 'best2' else 1)
    selected = [m.get('path') for m in eligible[:keep_n] if m.get('path')]
    group['selected_paths'] = selected
    selected_set = set(selected)
    for m in members:
        m['selected'] = m.get('path') in selected_set
    group['status'] = 'reviewed'
    if group.get('group_key'):
        _set_similarity_group_status(group['group_key'], 'reviewed')
        _persist_similarity_group_members(group['group_key'], members)
    s['kept_paths'] = list(s.get('singleton_paths') or []) + [
        p for g in s.get('groups_data', []) for p in (g.get('selected_paths') or [])
    ]
    s['photos'] = [g for g in s.get('groups_data', []) if g.get('count', 0) > 1]
    state['rank']['preview_at'] = 0.0
    _activity('相似组选优', '', f'组 {gid} · {mode} · 保留 {len(selected)} 张')
    return jsonify({'ok': True, 'photos': s['photos'][:UI_RESULT_CHUNK],
                    'changed_group': group,
                    'result_total': len(s['photos']),
                    'kept': len(s['kept_paths'])})


@app.route('/api/dedup-apply', methods=['POST'])
def api_dedup_apply():
    """Compatibility batch action: queue non-kept duplicates into software trash."""
    folder = state.get('folder')
    s = state['dedup']
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400
    _sync_dedup_with_cull()
    allowed = _cull_allowed_for_dedup()
    queued = []
    for group in s.get('groups_data', []):
        if group.get('status') not in ('reviewed',):
            continue
        selected = set(group.get('selected_paths') or [])
        for m in group.get('members', []):
            p = m.get('path')
            if not p or p in selected:
                continue
            if m.get('lifecycle') in ('pending_trash','pending_permanent_delete','trashed','permanently_deleted'):
                continue
            if allowed is not None and p not in allowed:
                continue
            original = _find_original_for_path(p)
            planned_trash = str(_trash_destination(Path(p), folder).resolve())
            _apply_media_lifecycle(original, p, 'pending_trash', 'dedup')
            task_id, _ = TASK_MANAGER.enqueue(
                'move_to_trash',
                {'path': p, 'folder': str(folder), 'step': 'dedup',
                 'trash_path': planned_trash},
                priority=12, idempotency_key=f"move_to_trash:{original}"
            )
            queued.append(task_id)
    return jsonify({'ok': True, 'queued': len(queued), 'task_ids': queued}), 202


def _known_step_paths(step):
    if step == 'cull':
        return {str(p.get('path')) for p in state['cull'].get('photos', []) if p.get('path')}
    if step == 'rank':
        return {str(getattr(sc, 'path', '')) for sc in state['rank'].get('scores', [])
                if getattr(sc, 'path', None)}
    if step == 'dedup':
        out = set()
        for group in state['dedup'].get('groups_data', []):
            for member in group.get('members', []):
                if member.get('path'):
                    out.add(str(member.get('path')))
        return out
    return set()


@app.route('/api/trash', methods=['GET'])
def api_trash():
    """List PhotoCurator's own recycle bin for the current selected library."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'photos': [], 'count': 0})
    rows = _trash_rows(folder)
    return jsonify({'photos': rows, 'count': len(rows)})


@app.route('/api/trash-restore', methods=['POST'])
def api_trash_restore():
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '请先选择照片文件夹'}), 400
    data = request.get_json() or {}
    try:
        trash_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({'error': '无效的回收站记录'}), 400
    current = _trash_rows(folder)
    row = next((x for x in current if x['id'] == trash_id), None)
    if not row:
        return jsonify({'error': '当前照片库的回收站中没有这条记录'}), 404
    with _FILE_PLAN_LOCK:
        reserved = _active_restore_reservations()
        restore_path = str(_unique_destination(
            Path(row['original_path']), reserved=reserved
        ).resolve())
        task_id, created = TASK_MANAGER.enqueue(
            'restore_trash',
            {'trash_id': trash_id,
             'original_path': row['original_path'],
             'trash_path': row['path'],
             'source_step': row.get('source_step') or '',
             'restore_path': restore_path},
            priority=8,
            idempotency_key=f"restore_trash:{trash_id}"
        )
    _media_state_set(row['original_path'], row['path'], 'pending_restore',
                     row.get('source_step') or '', detail=str(task_id))
    return jsonify({'ok': True, 'queued': True, 'created': created,
                    'task_id': task_id, 'id': trash_id}), 202


@app.route('/api/trash-purge', methods=['POST'])
def api_trash_purge():
    """Queue permanent deletion after final recycle-bin review."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '请先选择照片文件夹'}), 400
    data = request.get_json() or {}
    rows = _trash_rows(folder)
    if data.get('all'):
        task_ids = []
        for row in rows:
            task_id, _ = TASK_MANAGER.enqueue(
                'purge_trash', {'trash_id': row['id']}, priority=12,
                idempotency_key=f"purge_trash:{row['id']}"
            )
            task_ids.append(task_id)
        return jsonify({'ok': True, 'queued': True, 'task_ids': task_ids,
                        'count': len(task_ids)}), 202
    try:
        trash_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({'error': '无效的回收站记录'}), 400
    if trash_id not in {row['id'] for row in rows}:
        return jsonify({'error': '当前照片库的回收站中没有这条记录'}), 404
    task_id, created = TASK_MANAGER.enqueue(
        'purge_trash', {'trash_id': trash_id}, priority=7,
        idempotency_key=f"purge_trash:{trash_id}"
    )
    return jsonify({'ok': True, 'queued': True, 'created': created,
                    'task_id': task_id, 'id': trash_id}), 202


@app.route('/api/trash-restore-all', methods=['POST'])
def api_trash_restore_all():
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '请先选择照片文件夹'}), 400
    rows = _trash_rows(folder)
    task_ids = []
    planned_rows = []
    with _FILE_PLAN_LOCK:
        reserved = _active_restore_reservations()
        for row in rows:
            restore_path = str(_unique_destination(
                Path(row['original_path']), reserved=reserved
            ).resolve())
            task_id, created = TASK_MANAGER.enqueue(
                'restore_trash',
                {'trash_id': row['id'],
                 'original_path': row['original_path'],
                 'trash_path': row['path'],
                 'source_step': row.get('source_step') or '',
                 'restore_path': restore_path},
                priority=15,
                idempotency_key=f"restore_trash:{row['id']}"
            )
            task_ids.append(task_id)
            if created:
                reserved.add(_path_reservation_key(restore_path))
            planned_rows.append((row, task_id))
    for row, task_id in planned_rows:
        _media_state_set(row['original_path'], row['path'], 'pending_restore',
                         row.get('source_step') or '', detail=str(task_id))
    return jsonify({'ok': True, 'queued': True, 'task_ids': task_ids,
                    'count': len(task_ids)}), 202


@app.route('/api/delete-photo', methods=['POST'])
def api_delete_photo():
    """Queue one file lifecycle operation and return immediately.

    mode=trash (default) moves to PhotoCurator's reversible trash.
    mode=permanent physically deletes the file and is intentionally separate
    so the UI can require a stronger confirmation / P shortcut.
    """
    data = request.get_json() or {}
    step = str(data.get('step') or '')
    mode = str(data.get('mode') or 'trash')
    path = str(data.get('path') or '')
    if step not in ('cull', 'dedup', 'rank'):
        return jsonify({'error': '无效板块'}), 400
    if mode not in ('trash', 'permanent'):
        return jsonify({'error': '无效删除方式'}), 400
    if path not in _known_step_paths(step):
        return jsonify({'error': '当前结果中未找到这张照片'}), 404

    target = _safe_image_path(path)
    if target is None:
        return jsonify({'error': '照片路径无效或已不在允许的照片目录中'}), 400
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '请先选择有效的照片文件夹'}), 400

    original = _find_original_for_path(str(target))
    previous_state = _media_state_get(original) or {}
    previous_lifecycle = str(previous_state.get('state') or 'normal')
    previous_group_status = None
    if step == 'dedup':
        for group in state['dedup'].get('groups_data', []):
            if any(
                os.path.realpath(str(m.get('original_path') or m.get('path') or '')) == original
                for m in group.get('members', [])
            ):
                previous_group_status = str(group.get('status') or 'pending')
                break
    lifecycle = 'pending_trash' if mode == 'trash' else 'pending_permanent_delete'
    _apply_media_lifecycle(original, str(target), lifecycle, step)

    kind = 'move_to_trash' if mode == 'trash' else 'permanent_delete'
    priority = 10 if mode == 'trash' else 5
    task_payload = {
        'path': str(target), 'folder': str(folder), 'step': step,
        'previous_lifecycle': previous_lifecycle,
        'previous_group_status': previous_group_status,
    }
    if mode == 'trash':
        task_payload['trash_path'] = str(
            _trash_destination(Path(target), folder).resolve()
        )
    task_id, created = TASK_MANAGER.enqueue(
        kind,
        task_payload,
        priority=priority,
        idempotency_key=f"{kind}:{original}",
    )
    _activity('提交后台任务', original, f"{kind}#{task_id}")
    return jsonify({
        'ok': True,
        'queued': True,
        'created': bool(created),
        'task_id': int(task_id),
        'mode': mode,
        'lifecycle': lifecycle,
        'original_path': original,
        'path': str(target),
    }), 202


@app.route('/api/tasks')
def api_tasks():
    """Lightweight task-center snapshot for the desktop UI / tray."""
    return jsonify(TASK_MANAGER.summary())


@app.route('/api/tasks/<int:task_id>')
def api_task(task_id):
    row = TASK_MANAGER.get(task_id)
    if not row:
        return jsonify({'error': '后台任务不存在'}), 404
    return jsonify(row)


@app.route('/api/weights', methods=['POST'])
def api_weights():
    data = request.get_json() or {}
    w = data.get('weights') or {}
    try:
        clean = {}
        for k in CATEGORIES:
            v = float(w.get(k, DEFAULT_WEIGHTS[k]))
            if not np.isfinite(v):
                v = DEFAULT_WEIGHTS[k]
            clean[k] = min(100.0, max(0.0, v))
        topn = min(500, max(1, int(data.get('topn', state['topn']))))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': '评分参数无效'}), 400
    state['weights'] = clean
    state['topn'] = topn
    state['rank']['preview'] = build_topn()
    state['rank']['preview_at'] = time.time()
    return jsonify({'ok': True, 'photos': state['rank']['preview']})

def _active_task_name():
    """Return the currently running processing step, if any."""
    for key in ('cull', 'dedup', 'rank'):
        if state.get(key, {}).get('running'):
            return key
    return None


def _reject_mutation_while_running():
    """Prevent UI mutations while a processing thread is updating shared state."""
    active = _active_task_name()
    if active:
        return jsonify({
            'error': '照片处理任务正在运行，请先停止或等待完成后再执行此操作',
            'active': active,
        }), 409
    return None


def _known_rank_path(path):
    """Allow rank-only actions only for files present in the current rank set."""
    if not path:
        return False
    return any(getattr(sc, 'path', None) == path for sc in state['rank'].get('scores', []))


@app.route('/api/exclude', methods=['POST'])
def api_exclude():
    data = request.get_json() or {}
    path = str(data.get('path') or '')
    if not _known_rank_path(path):
        return jsonify({'error': '当前优选结果中未找到这张照片'}), 404
    state['excluded'].add(path)
    return jsonify({'ok': True, 'removed': len(state['excluded']), 'photos': build_topn()})

@app.route('/api/restore', methods=['POST'])
def api_restore():
    data = request.get_json() or {}
    if data.get('all'):
        state['excluded'].clear()
    elif data.get('path'):
        path = str(data.get('path') or '')
        if not _known_rank_path(path):
            return jsonify({'error': '当前优选结果中未找到这张照片'}), 404
        state['excluded'].discard(path)
    return jsonify({'ok': True, 'removed': len(state['excluded']), 'photos': build_topn()})

@app.route('/api/toggle-status', methods=['POST'])
def api_toggle_status():
    """Manually change review tier only; never move the underlying file."""
    data = request.get_json() or {}
    path = data.get('path', '')
    tier = data.get('tier', 'sharp')
    if tier not in ('sharp', 'soft', 'blurry'):
        tier = 'sharp'
    s = state['cull']
    photo = next((p for p in s['photos'] if p.get('path') == path), None)
    if not photo:
        return jsonify({'error': '未找到照片'}), 404
    now_kept = tier != 'blurry'
    s.setdefault('overrides', {}).setdefault(path, {})['tier'] = tier
    s['overrides'][path]['move_selected'] = (tier == 'blurry')
    _save_review_overrides([(path, tier, tier == 'blurry')])
    _activity('人工分类', path, tier)
    # Manual review is classification-only. Never move a file merely because
    # its badge was changed; disk changes happen only via an explicit Move action.
    new_path = path
    badge, bt = _badge_for(tier, False)
    photo.update({'path': new_path, 'thumb': thumb_url(new_path), 'tier': tier,
                  'kept': now_kept, 'rejected': not now_kept,
                  'badge': badge, 'badgeType': bt,
                  # Moving is a separate user choice. Entering Blurry selects
                  # the photo by default; leaving Blurry removes it from the
                  # pending-move set.
                  'move_selected': tier == 'blurry'})
    sp = s['sharp_paths']
    for old in (path, new_path):
        if old in sp:
            sp.remove(old)
    if now_kept:
        sp.append(new_path)
        _request_rank_score(new_path)
    else:
        state['rank']['preview_at'] = 0.0
    s['sharp'] = sum(1 for p in s['photos'] if p['tier'] == 'sharp')
    s['soft'] = sum(1 for p in s['photos'] if p['tier'] == 'soft')
    s['blurry'] = sum(1 for p in s['photos'] if p['tier'] == 'blurry')
    _sync_dedup_with_cull()
    move_total = sum(1 for p in s['photos'] if p.get('tier') == 'blurry')
    move_selected = sum(
        1 for p in s['photos']
        if p.get('tier') == 'blurry' and p.get('move_selected', True)
    )
    return jsonify({'ok': True, 'tier': tier, 'kept': now_kept, 'badge': badge,
                    'badgeType': bt, 'path': new_path, 'thumb': photo['thumb'],
                    'move_selected': photo.get('move_selected', False),
                    'move_selected_count': move_selected, 'move_total': move_total,
                    'sharp': s['sharp'], 'soft': s['soft'], 'blurry': s['blurry']})


def _blurry_move_counts():
    photos = state['cull'].get('photos', [])
    blurry = [p for p in photos if p.get('tier') == 'blurry']
    selected = [p for p in blurry if p.get('move_selected', True)]
    return len(selected), len(blurry)


@app.route('/api/select-blurry', methods=['POST'])
def api_select_blurry():
    """Select/unselect reviewed blurry photos for the next explicit Move action.

    Classification and file movement are intentionally separate: unselecting a
    blurry photo keeps its Blurry badge but leaves the source file in place.
    """

    data = request.get_json() or {}
    photos = state['cull'].get('photos', [])

    if 'all' in data:
        selected = bool(data.get('all'))
        for photo in photos:
            if photo.get('tier') == 'blurry':
                photo['move_selected'] = selected
                state['cull'].setdefault('overrides', {}).setdefault(photo.get('path'), {})['move_selected'] = selected
        _save_review_overrides([
            (p.get('path'), p.get('tier'), p.get('move_selected', True))
            for p in photos if p.get('tier') == 'blurry' and p.get('path')
        ])
        count, total = _blurry_move_counts()
        return jsonify({'ok': True, 'selected': count, 'total': total})

    path = str(data.get('path') or '')
    photo = next((p for p in photos if p.get('path') == path), None)
    if not photo:
        return jsonify({'error': '未找到照片'}), 404
    if photo.get('tier') != 'blurry':
        return jsonify({'error': '只有“模糊”照片可以加入移动列表'}), 400

    photo['move_selected'] = bool(data.get('selected', True))
    state['cull'].setdefault('overrides', {}).setdefault(path, {})['move_selected'] = photo['move_selected']
    _save_review_overrides([(path, photo.get('tier'), photo['move_selected'])])
    _activity('移动选择', path, '选中' if photo['move_selected'] else '取消')
    count, total = _blurry_move_counts()
    return jsonify({
        'ok': True,
        'path': photo.get('path'),
        'move_selected': photo['move_selected'],
        'selected': count,
        'total': total,
    })


@app.route('/api/move-blurry', methods=['POST'])
def api_move_blurry():
    """Queue selected blurry photos into PhotoCurator software trash."""
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400
    rows = [
        pp for pp in state['cull'].get('photos', [])
        if pp.get('tier') == 'blurry'
        and pp.get('move_selected', True)
        and pp.get('lifecycle') not in
        ('pending_trash','pending_permanent_delete','trashed','permanently_deleted')
        and pp.get('path')
    ]
    task_ids = []
    for pp in rows:
        path = str(pp['path'])
        original = _find_original_for_path(path)
        planned_trash = str(_trash_destination(Path(path), folder).resolve())
        _apply_media_lifecycle(original, path, 'pending_trash', 'cull')
        task_id, _ = TASK_MANAGER.enqueue(
            'move_to_trash',
            {'path': path, 'folder': str(folder), 'step': 'cull',
             'trash_path': planned_trash},
            priority=12, idempotency_key=f"move_to_trash:{original}"
        )
        task_ids.append(task_id)
    selected_left, blurry_total = _blurry_move_counts()
    return jsonify({'ok': True, 'queued': len(task_ids), 'task_ids': task_ids,
                    'selected': selected_left, 'total': blurry_total}), 202


@app.route('/api/export', methods=['POST'])
def api_export():
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    data = request.get_json() or {}
    try:
        topn = min(500, max(1, int(data.get('topn', state['topn']))))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': '导出数量无效'}), 400

    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400

    top = build_topn(topn=topn)
    if not top:
        return jsonify({'error': '当前没有可导出的优选照片'}), 400

    base = Path(folder) / f"TOP_{topn}"
    dest = base
    if dest.exists() and any(dest.iterdir()):
        stamp = time.strftime('%Y%m%d_%H%M%S')
        dest = Path(folder) / f"TOP_{topn}_{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = Path(folder) / f"TOP_{topn}_{stamp}_{n}"
    dest.mkdir(parents=True, exist_ok=True)

    copied = failed = 0
    for item in top:
        try:
            src = Path(item['path'])
            if src.is_file():
                shutil.copy2(str(src), str(dest / f"{item['rank']:03d}_{src.name}"))
                copied += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            logger.warning(f"export fail {item['path']}: {e}")

    return jsonify({'ok': failed == 0, 'copied': copied, 'failed': failed,
                    'dest': str(dest)})

# --------------------------------------------------------------------------- #
#  Phone Background selector
#  Flag TOP photos as "suitable as phone wallpaper", then export both the
#  original and a universal phone-cropped (1290 x 2796, 19.5:9) version.
#  19.5:9 covers iPhones pixel-perfect and nearly all Android (phones zoom to
#  fill, so 20:9 screens crop only a hair). One ratio, no device picker.
# --------------------------------------------------------------------------- #
WALLPAPER_W, WALLPAPER_H = 1290, 2796  # universal 19.5:9 portrait


@app.route('/api/toggle-phonebg', methods=['POST'])
def api_toggle_phonebg():
    """Flag / unflag a photo as suitable for a phone wallpaper."""
    data = request.get_json() or {}
    path = str(data.get('path') or '')
    if not path:
        return jsonify({'error': '路径为空'}), 400
    if not _known_rank_path(path):
        return jsonify({'error': '当前优选结果中未找到这张照片'}), 404
    if path in state['phone_bg']:
        state['phone_bg'].discard(path)
        on = False
    else:
        state['phone_bg'].add(path)
        on = True
    return jsonify({'ok': True, 'phonebg': on, 'count': len(state['phone_bg'])})


def crop_to_phone(img, target_w=WALLPAPER_W, target_h=WALLPAPER_H):
    """Center-crop a PIL image to the phone's aspect ratio, then resize to the
    native resolution. Honors EXIF orientation first."""
    img = ImageOps.exif_transpose(img).convert('RGB')
    w, h = img.size
    target_ar = target_w / target_h            # ~0.4615 (portrait)
    src_ar = w / h
    if src_ar > target_ar:
        # Source is too wide -> crop the sides (center).
        new_w = int(round(h * target_ar))
        left = (w - new_w) // 2
        box = (left, 0, left + new_w, h)
    else:
        # Source is too tall -> crop top/bottom (center).
        new_h = int(round(w / target_ar))
        top = (h - new_h) // 2
        box = (0, top, w, top + new_h)
    img = img.crop(box)
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return img


@app.route('/api/export-phonebg', methods=['POST'])
def api_export_phonebg():
    """Export flagged wallpaper originals and 1290x2796 crops."""
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400

    top = build_topn()
    flagged = [item for item in top if item['path'] in state['phone_bg']]
    if not flagged:
        return jsonify({'ok': True, 'copied': 0, 'cropped': 0, 'failed': 0,
                        'dest': str(Path(folder) / 'PhoneBG'),
                        'note': '还没有标记为手机壁纸的照片。'})

    base = Path(folder) / 'PhoneBG'
    dest = base
    if dest.exists() and any(dest.iterdir()):
        stamp = time.strftime('%Y%m%d_%H%M%S')
        dest = Path(folder) / f'PhoneBG_{stamp}'
        n = 1
        while dest.exists():
            n += 1
            dest = Path(folder) / f'PhoneBG_{stamp}_{n}'

    orig_dir = dest / '原始照片'
    crop_dir = dest / '壁纸_19.5x9'
    orig_dir.mkdir(parents=True, exist_ok=True)
    crop_dir.mkdir(parents=True, exist_ok=True)

    copied = cropped = failed = 0
    for item in flagged:
        src = Path(item['path'])
        if not src.is_file():
            failed += 1
            continue

        stem = f"{item['rank']:03d}_{src.stem}"
        try:
            shutil.copy2(str(src), str(orig_dir / f"{stem}{src.suffix}"))
            copied += 1
        except Exception as e:
            failed += 1
            logger.warning(f"phonebg original fail {src}: {e}")

        try:
            with open_image_pil(src) as im:
                crop_to_phone(im).save(
                    str(crop_dir / f"{stem}.jpg"), format='JPEG', quality=92
                )
            cropped += 1
        except Exception as e:
            failed += 1
            logger.warning(f"phonebg crop fail {src}: {e}")

    return jsonify({'ok': failed == 0, 'copied': copied, 'cropped': cropped,
                    'failed': failed, 'dest': str(dest)})


if __name__ == '__main__':
    # Browser compatibility mode opens only after the server has had time to
    # bind, avoiding the common first-load "connection refused" race.
    if os.environ.get('PHOTOCURATOR_OPEN_BROWSER') == '1':
        import webbrowser
        threading.Timer(
            0.8, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')
        ).start()
    if CODESPACES_PUBLIC_HOST:
        logger.info(
            "Codespaces 在线预览已启动：https://%s", CODESPACES_PUBLIC_HOST
        )
    # Local mode binds loopback only. Codespaces binds all interfaces solely
    # so GitHub's authenticated forwarding proxy can reach this container.
    app.run(host=SERVER_HOST, port=PORT, debug=False, threaded=True)
