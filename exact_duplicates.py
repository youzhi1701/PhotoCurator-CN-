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


def _file_identity(info_stat):
    if not stat.S_ISREG(info_stat.st_mode):
        raise OSError("not a regular file")
    return (int(info_stat.st_size), int(info_stat.st_mtime_ns),
            int(getattr(info_stat, "st_ctime_ns", 0)),
            int(info_stat.st_dev), int(info_stat.st_ino))


def _stat_key(path):
    info = _file_identity(os.stat(path, follow_symlinks=False))
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
        if not isinstance(doc, dict) or doc.get("version") != CACHE_VERSION:
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



class _HashCancelled(Exception):
    """Abort the entire duplicate pass; a partial group is never approval."""


def _sha256_guarded(raw, expected, cancelled=None):
    """Hash only the exact regular-file identity indexed before the read.

    A cache key or a matching path name cannot prove the opened bytes belong
    to the originally indexed photo (notably during USB hot-swap).
    """
    if cancelled is not None and cancelled():
        raise _HashCancelled()
    if _stat_key(raw)[0] != expected:
        return None
    sha = hashlib.sha256()
    with open(raw, "rb") as fh:
        if _file_identity(os.fstat(fh.fileno())) != expected:
            return None
        while True:
            if cancelled is not None and cancelled():
                raise _HashCancelled()
            block = fh.read(CHUNK)
            if not block:
                break
            sha.update(block)
        if _file_identity(os.fstat(fh.fileno())) != expected:
            return None
    if _stat_key(raw)[0] != expected:
        return None
    return sha.hexdigest()

def exact_duplicate_groups(paths, *, cache_path=None, cancelled=None):
    """Return byte-identical groups, preserving the original traversal order.

    Distinct paths with the same size are streamed through SHA-256 (1 MiB
    chunks). A file changed while being hashed is discarded, never accepted.
    Unique-size files are never opened for hashing. Unreadable/missing files
    are safely ignored. A cached hash is always reverified if it could prove
    a duplicate; caches never authorize a false byte-exact match.

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
    cached_paths = set()
    for size, bucket in by_size.items():
        if len(bucket) < 2:
            continue
        for raw in bucket:
            if cancelled is not None and cancelled():
                return []
            previous_identity, cache_key = stats[raw]
            try:
                if _stat_key(raw)[0] != previous_identity:
                    continue
                digest = old_cache.get(cache_key)
                if digest is None:
                    digest = _sha256_guarded(raw, previous_identity, cancelled)
                else:
                    cached_paths.add(raw)
                if digest is None or _stat_key(raw)[0] != previous_identity:
                    continue
            except _HashCancelled:
                return []
            except (OSError, ValueError):
                continue
            keep_cache[cache_key] = digest
            by_content[(size, digest)].append(raw)

    # The JSON cache is a performance hint, *not* cryptographic evidence.
    # If a cached hash participates in a candidate duplicate group, stream
    # its real bytes again before exposing that group as "byte-exact".
    # A forged/stale cache can otherwise claim two different images are equal
    # despite no actual SHA-256 calculation in the current scan.
    verified = defaultdict(list)
    for (size, digest), members in by_content.items():
        if len(members) < 2:
            continue
        if not any(path in cached_paths for path in members):
            verified[(size, digest)].extend(members)
            continue
        for raw in members:
            if cancelled is not None and cancelled():
                return []
            if raw not in cached_paths:
                verified[(size, digest)].append(raw)
                continue
            identity, cache_key = stats[raw]
            try:
                actual_digest = _sha256_guarded(raw, identity, cancelled)
            except _HashCancelled:
                return []
            except (OSError, ValueError):
                actual_digest = None
            if actual_digest is None:
                keep_cache.pop(cache_key, None)
                continue
            keep_cache[cache_key] = actual_digest
            verified[(size, actual_digest)].append(raw)

    if cancelled is not None and cancelled():
        return []
    _save_cache(cache_path, keep_cache)
    return [members for members in verified.values() if len(members) > 1]
