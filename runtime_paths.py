#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoCurator runtime path policy and legacy data migration."""

import os
import shutil
import sys
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


def migrate_legacy_config(legacy_root, target_root):
    """Copy durable config/state from the old install-local data directory once.

    Cache, logs and built-in demo data are intentionally not migrated because
    they are rebuildable. The config directory carries the SQLite index,
    background task state, manual decisions, recents and dedup feature store.
    Existing target config always wins.
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

    if not _dir_has_entries(legacy_config) or _dir_has_entries(target_config):
        return False

    target_root.mkdir(parents=True, exist_ok=True)
    temp_config = target_root / f".config-migrate-{os.getpid()}"
    if temp_config.exists():
        shutil.rmtree(temp_config, ignore_errors=True)

    try:
        shutil.copytree(legacy_config, temp_config, copy_function=_link_or_copy)
        if target_config.exists() and not _dir_has_entries(target_config):
            target_config.rmdir()
        os.replace(temp_config, target_config)
        marker = target_root / ".migrated-from-install-data"
        marker.write_text(str(legacy_root), encoding="utf-8")
        return True
    except Exception:
        shutil.rmtree(temp_config, ignore_errors=True)
        raise
