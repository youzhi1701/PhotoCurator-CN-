#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only, content-exact duplicate index for PhotoCurator-CN.

This is deliberately independent of pHash/ORB: a visual match is NOT evidence
of byte-for-byte equality. Hash only equal-size candidates, recheck stat after
reading, and never move/delete a file here. The caller must still obtain
explicit per-batch human consent for any file lifecycle action.
"""
from collections import defaultdict
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
import tempfile

log = logging.getLogger(__name__)
CACHE_VERSION = 1
CHUNK = 1024 * 1024


def _stat_key(path):
    info_stat = os.stat(path)
    if not stat.S_ISREG(info_stat.st_mode):
        raise OSError("not a regular file")
    info = (int(info_stat.st_size), int(info_stat.st_mtime_ns),
            int(getattr(info_stat, "st_ctime_ns", 0)),
            int(info_stat.st_dev), int(info_stat.st_ino))
    return info, json.dumps([os.path.realpath(str(path)), *info],
                            ensure_ascii=False, separators=(",", ":"))


def _load_cache(cache_path):
    if cache_path is None:
        return {}
    path = Path(cache_path)
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("version") != CACHE_VERSION:
            return {}
        raw = doc.get("entries")
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str)
                and len(value) == 64 and all(c in "0123456789abcdef" for c in value)}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(cache_path, entries):
    if cache_path is None:
        return
    path = Path(cache_path)
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
            tmp_name = fh.name
            json.dump({"version": CACHE_VERSION, "entries": entries}, fh,
                      separators=(",", ":"), ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError:
        log.debug("exact-duplicate cache write failed", exc_info=True)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def exact_duplicate_groups(paths, *, cache_path=None, cancelled=None):
    """Return byte-identical groups, preserving the original traversal order.

    Distinct paths with the same size are streamed through SHA-256 (1 MiB
    chunks). A file changed while being hashed is discarded, never accepted.
    Unique-size files are never opened for hashing. Unreadable/missing files
    are safely ignored. The optional cache uses path + high-resolution stat
    identity and is pruned each completed scan.

    Results are read-only recommendations; a matching digest never authorizes
    file deletion.
    """
    unique = {}
    for path in paths:
        raw = str(path)
        key = os.path.normcase(os.path.realpath(raw))
        unique.setdefault(key, raw)
    by_size = defaultdict(list)
    stats = {}
    for raw in unique.values():
        if cancelled is not None and cancelled():
            return []
        try:
            identity, key = _stat_key(raw)
            by_size[identity[0]].append(raw)
            stats[raw] = (identity, key)
        except OSError:
            continue

    old_cache = _load_cache(cache_path)
    keep_cache = {}
    by_content = defaultdict(list)
    for size, bucket in by_size.items():
        if len(bucket) < 2:
            continue
        for raw in bucket:
            if cancelled is not None and cancelled():
                return []
            previous_identity, cache_key = stats[raw]
            try:
                current_identity, _ = _stat_key(raw)
                if current_identity != previous_identity:
                    continue
                digest = old_cache.get(cache_key)
                if digest is None:
                    sha = hashlib.sha256()
                    with open(raw, "rb") as fh:
                        for block in iter(lambda: fh.read(CHUNK), b""):
                            if cancelled is not None and cancelled():
                                return []
                            sha.update(block)
                    digest = sha.hexdigest()
                after_identity, _ = _stat_key(raw)
                if after_identity != previous_identity:
                    continue
            except (OSError, ValueError):
                continue
            keep_cache[cache_key] = digest
            by_content[(size, digest)].append(raw)

    if cancelled is None or not cancelled():
        _save_cache(cache_path, keep_cache)
    return [members for members in by_content.values() if len(members) > 1]
