#!/usr/bin/env python3
"""
Photo Curator v3 — full pipeline (Cull · Dedup · Rank)
=============================================================================
v3 combines v2's proven Cull and Dedup steps with the advanced, magazine-style
Ranking Studio:

  1 · CULL   contrast-normalized sharpness (haze/night/low-contrast shots are
             kept; only true blur is flagged). Per-photo Sharp/Blurry override
             that physically moves files in/out of Blurred/.
  2 · DEDUP  global perceptual-hash clustering (FastBatchDeduplicator) — burst
             sequences collapse to their sharpest frame; optional auto-move of
             duplicates to Duplicates/.
  3 · RANK   the advanced engine (photo_ranking_v3) with LIVE weight sliders,
             per-photo radar + sub-score breakdown, and non-destructive
             Remove/Restore from both the grid and the lightbox.

Steps feed each other: Rank uses Dedup survivors if present, else Cull
survivors, else the whole folder.

Pure cv2 / numpy / Pillow + Flask. Fully offline. Port 5001.
"""

import os
import sys
import io
import json
import time
import shutil
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import threading
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from PIL import Image, ImageOps

from raw_loader import (RAW_EXTS, HAS_RAWPY, is_raw,
                        HEIF_EXTS, HAS_HEIF, is_heif, needs_jpeg_preview,
                        open_image_pil, imread_bgr, imread_gray)
from photo_ranking_v3 import AdvancedPhotoAnalyzer
from photo_dedup_batch import FastBatchDeduplicator
from photo_file_organizer import PhotoOrganizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Persist runtime diagnostics for the no-console desktop launcher. Keep logs
# bounded so long photo-library sessions cannot grow them indefinitely.
try:
    LOG_DIR = Path.home() / '.photo_curator' / 'logs'
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

APP_VERSION = "1.2.3-cn.4"
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

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
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
VENDOR_DIR = Path(__file__).resolve().parent / 'vendor'
VENDOR_FILES = {'maplibre-gl-csp.js': 'text/javascript',
                'maplibre-gl-csp-worker.js': 'text/javascript',
                'maplibre-gl.css': 'text/css'}

RECENTS_FILE = Path.home() / '.photo_curator_recents.json'
THUMB_DIR = Path(tempfile.gettempdir()) / 'photocurator_thumbs'
THUMB_DIR.mkdir(parents=True, exist_ok=True)

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
UI_RESULT_CAP = 5000


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
    'auto': {'cull': False, 'dedup': False},
    'cull':  {**_blank(), 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': []},
    'dedup': {**_blank(), 'groups': 0, 'kept_paths': [], 'groups_data': [], 'applied': False},
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
    'raw' = any RAW, 'heic' = any HEIF, 'jpg' = everything else,
    'ext:nef' = that exact format, 'all'/empty = no filtering."""
    if ftype == 'raw':
        return [p for p in paths if is_raw(p)]
    if ftype == 'heic':
        return [p for p in paths if is_heif(p)]
    if ftype == 'jpg':
        return [p for p in paths if not is_raw(p) and not is_heif(p)]
    if str(ftype).startswith('ext:'):
        want = ftype[4:].lower()
        return [p for p in paths
                if Path(str(p)).suffix.lstrip('.').lower() == want
                or (want == 'jpg' and Path(str(p)).suffix.lower() == '.jpeg')]
    return paths


def list_images(folder):
    p = Path(folder)
    if not p.is_dir():
        return []
    return sorted(f for f in p.iterdir()
                  if f.is_file() and f.suffix.lower() in IMG_EXTS
                  # Skip macOS AppleDouble sidecars (._foo.jpg) and hidden files —
                  # they aren't real images and break decoding/thumbnails.
                  and not f.name.startswith('._')
                  and not f.name.startswith('.'))


def _thumb_cache_path(image_path):
    p = Path(image_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    # The trailing version tag invalidates old cached thumbnails when the
    # thumbnail logic changes (v2 = EXIF orientation applied).
    key = hashlib.md5(f"{image_path}:{mtime}:v2".encode()).hexdigest()
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


def thumb_url(image_path):
    # The &v tag busts the BROWSER's HTTP cache when thumbnail logic changes
    # (v2 = EXIF orientation applied). Without it, the browser keeps serving the
    # previously-cached (sideways) thumbnail for the same URL.
    return '/api/thumb?path=' + quote(str(image_path)) + '&v=2'


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
#  CULL
# --------------------------------------------------------------------------- #
BASE_BLUR, BASE_SHARP = 90.0, 230.0   # region_s baselines when not adaptive


def _badge_for(tier, star):
    if tier == 'sharp':
        return '清晰', 'good'
    if tier == 'soft':
        return ('轻微软 ★' if star else '轻微软'), 'soft'
    return '模糊', 'bad'


def run_cull(folder, strictness, adaptive, rescue_on):
    s = state['cull']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在扫描照片…',
              'photos': [], 'sharp': 0, 'soft': 0, 'blurry': 0, 'sharp_paths': [],
              # complete=True only when cull runs to the end; a stopped cull must
              # not feed its partial survivor list into Dedup/Rank.
              'complete': False, 'src_folder': str(folder)})
    try:
        images = list_images(folder)
        total = len(images) or 1
        items = []   # {name, path, region_s, q}

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
            for it in items:
                tier, star = classify_sharpness(it['region_s'], it['q'],
                                                blur_lo, sharp_hi, q_rescue, rescue_on)
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
                               'badge': badge, 'badgeType': bt, 'tier': tier,
                               'raw': is_raw(it['path']), 'fmt': fmt_of(it['path']),
                               'heic': is_heif(it['path']),
                               'kept': tier != 'blurry', 'rejected': tier == 'blurry',
                               # File-action selection is separate from the
                               # classification itself. Blurry frames start
                               # selected, but the user may uncheck any of them
                               # before the explicit Move action.
                               'move_selected': tier == 'blurry'})
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

        for idx, p in enumerate(images):
            if s.get('cancel'):
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
            bgr = imread_bgr(str(p))       # RAW-aware
            if bgr is None:
                continue
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            items.append({'name': p.name, 'path': str(p),
                          'region_s': region_sharpness(gray),
                          'q': quick_quality(bgr, gray)})
            # Reclassification is for live UI only; final output is still
            # classified once more below. Throttle it so very large folders do
            # not repeatedly rescan the entire processed list every five files.
            now = time.time()
            if (idx < 20 and idx % 5 == 0) or idx == len(images) - 1 \
                    or now - s.get('_last_classify_at', 0.0) >= 1.0:
                classify_all()
                s['_last_classify_at'] = now
        classify_all()
        s.pop('_last_classify_at', None)

        # Blurry photos are NOT moved automatically — they stay in place so you
        # can review them first, then move them with the "移动模糊照片 → Blurred/"
        # button (mirrors the TOP-N export flow).
        s['progress'] = 100
        s['complete'] = True   # full pass finished — survivors are safe to chain
        s['status'] = (f"完成 · 用时 {_fmt(time.time()-t0)} · {_tiers(total)}"
                       + (" · 请确认后再移动模糊照片" if s['blurry'] else ""))
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


# --------------------------------------------------------------------------- #
#  DEDUP
# --------------------------------------------------------------------------- #
def run_dedup(folder, threshold, ftype='all', pair='both'):
    s = state['dedup']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在准备…',
              'photos': [], 'groups': 0, 'kept_paths': [], 'groups_data': [],
              'applied': False, 'complete': False, 'src_folder': str(folder)})
    try:
        # Chain off Cull's survivors ONLY if Cull finished a full pass on THIS
        # folder. A stopped/partial cull (or a cull of a different folder) must
        # not silently shrink Dedup's input — fall back to the whole folder.
        cull = state['cull']
        chain_ok = (cull.get('complete') and cull.get('sharp_paths')
                    and cull.get('src_folder') == str(folder))
        if chain_ok:
            paths = [Path(p) for p in cull['sharp_paths']]
            logger.info(f"Dedup: chaining {len(paths)} Cull survivors")
        else:
            paths = list_images(folder)
            logger.info(f"Dedup: scanning full folder ({len(paths)} images) "
                        f"— no completed Cull for this folder")
        # Honor the Cull file-type filter (RAW / JPG / specific format).
        if ftype and ftype != 'all':
            before = len(paths)
            paths = filter_ftype(paths, ftype)
            lbl = ftype_label(ftype)
            logger.info(f"Dedup: {lbl}-only filter — "
                        f"{len(paths)} of {before} photos continue")
            s['status'] = (f"仅 {lbl} · {len(paths)}/{before} "
                           f"张照片进入相似去重")
        # Collapse RAW+JPG pairs of the same frame (setting in Dedup panel).
        paths, npairs = collapse_raw_jpg_pairs(paths, pair)
        if npairs:
            logger.info(f"Dedup: {npairs} RAW+JPG pairs collapsed (kept {pair.upper()})")
            s['status'] = f"已合并 {npairs} 组 RAW+JPG 同帧照片 · 保留 {pair.upper()}"
        if not paths:
            s['status'] = ('没有可处理的照片' if ftype == 'all'
                           else f'没有可去重的 {ftype_label(ftype)} 照片')
            return
        dd = FastBatchDeduplicator(threshold=threshold)
        # Persist perceptual signatures so a repeat run on this folder is fast.
        try:
            cache_key = hashlib.md5(os.path.realpath(folder).encode('utf-8')).hexdigest()
            dd.enable_disk_cache(THUMB_DIR / f'dedup_{cache_key}.json')
        except Exception:
            pass
        dd.reset()
        total = len(paths)

        # Live-grid limits: rebuilding + shipping the full survivor list (and the
        # browser re-rendering every thumbnail) gets expensive past a few
        # thousand uniques. Cap the displayed thumbnails to the most recent
        # GRID_CAP and refresh on a time interval, not every Nth photo.
        GRID_CAP = 400
        REFRESH_SECS = 1.0
        last_refresh = [0.0]

        def refresh(final=False):
            # Dedup review is group-first: scan first, then let the user compare
            # every member in a similar set and choose which frame to keep.
            clusters = dd.clusters
            first = 0 if final else max(0, len(clusters) - GRID_CAP)
            groups_data = []
            for gi in range(len(clusters)):
                c = clusters[gi]
                members = sorted(
                    c.members,
                    key=lambda x: float(getattr(x, 'overall_score', 0.0) or 0.0),
                    reverse=True,
                )
                selected_path = c.rep.path
                group = {
                    'group_id': gi,
                    'count': len(members),
                    'selected_path': selected_path,
                    'members': [{
                        'name': getattr(m, 'filename', Path(m.path).name),
                        'path': m.path,
                        'thumb': thumb_url(m.path),
                        'score': round(float(getattr(m, 'overall_score', 0.0) or 0.0), 1),
                        'selected': m.path == selected_path,
                    } for m in members],
                }
                groups_data.append(group)
            s['groups_data'] = groups_data
            visible = groups_data if final else groups_data[first:]
            # Only duplicate groups need human review; singleton groups are kept
            # automatically and still remain in kept_paths for the next stage.
            s['photos'] = [g for g in visible if g['count'] > 1]
            s['groups'] = len(clusters)
            last_refresh[0] = time.time()

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        for idx, p in enumerate(paths):
            if s.get('cancel'):
                refresh(final=True)
                el = time.time() - t0
                s['status'] = (f"已停止 · {idx} 张中保留 {len(dd.clusters)} 张 "
                               f"（{(len(dd.clusters)/idx*100) if idx else 0:.0f}%）· "
                               f"已用时 {_fmt(el)}")
                state['dedup']['kept_paths'] = [sc.path for sc in dd.current_survivors()]
                return
            done = idx + 1
            s['progress'] = int(done / total * 100)
            uniq = len(dd.clusters)
            pct_uniq = (uniq / done * 100) if done else 0
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0          # photos/sec
            eta = (total - done) / rate if rate > 0 else 0
            s['status'] = (f"相似去重 {p.name}（{done}/{total}）· "
                           f"保留 {uniq} 张（{pct_uniq:.0f}%）· "
                           f"已用时 {_fmt(elapsed)} · 预计剩余 {_fmt(eta)}")
            gray = imread_gray(str(p))     # RAW-aware
            sharp = sharpness_score(gray) if gray is not None else 0.0
            dd.add_photo(_LiteScore(str(p), p.name, sharp, sharp))
            if time.time() - last_refresh[0] >= REFRESH_SECS or idx == total - 1:
                refresh()
        dd.save_disk_cache()
        refresh(final=True)
        kept = [sc.path for sc in dd.current_survivors()]
        s['kept_paths'] = kept
        s['complete'] = True   # full pass finished — survivors safe to chain
        removed = total - len(kept)
        took = _fmt(time.time() - t0)
        pct_uniq = (len(kept) / total * 100) if total else 0
        duplicate_groups = sum(1 for g in s.get('groups_data', []) if g.get('count', 0) > 1)
        if duplicate_groups:
            s['status'] = (f"筛选完成 · 用时 {took} · 发现 {duplicate_groups} 组相似照片 · "
                           f"已默认推荐每组最佳照片，请对比后确认处理")
        else:
            s['status'] = (f"筛选完成 · 用时 {took} · {total} 张照片未发现需要处理的相似组")
        s['progress'] = 100
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
    scores = [s for s in state['rank']['scores'] if s.path not in excluded]
    ranked = sorted(scores, key=lambda s: weighted_overall(s, weights), reverse=True)[:topn]
    out = []
    for rank, s in enumerate(ranked, 1):
        ov = weighted_overall(s, weights)
        out.append({
            'name': s.filename, 'path': s.path, 'thumb': thumb_url(s.path),
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


def run_rank(folder, ftype='all', pair='both'):
    s = state['rank']
    s.update({'running': True, 'cancel': False, 'progress': 0, 'status': '正在准备…',
              'scores': [], 'total': 0, 'analyzed': 0, 'preview': [], 'preview_at': 0.0})
    state['excluded'] = set()
    try:
        # Prefer a COMPLETED Dedup on this folder, then a COMPLETED Cull on this
        # folder; otherwise rank the whole folder. Never chain off a partial run.
        dd, cull = state['dedup'], state['cull']
        if dd.get('complete') and dd.get('kept_paths') and dd.get('src_folder') == str(folder):
            paths = [Path(p) for p in dd['kept_paths']]
            chain = '去重后保留照片'
        elif cull.get('complete') and cull.get('sharp_paths') and cull.get('src_folder') == str(folder):
            paths = [Path(p) for p in cull['sharp_paths']]
            chain = '模糊筛选后保留照片'
        else:
            paths = list_images(folder)
            chain = '全部照片'
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
            s['status'] = ('没有可评分的照片' if ftype == 'all'
                           else f'没有可评分的 {ftype_label(ftype)} 照片')
            return
        total = len(paths)
        s['total'] = total
        analyzer = AdvancedPhotoAnalyzer()

        t0 = time.time()

        def _fmt(sec):
            sec = int(max(0, sec)); h, r = divmod(sec, 3600); m, s_ = divmod(r, 60)
            return f"{h}h{m:02d}m" if h else (f"{m}m{s_:02d}s" if m else f"{s_}s")

        for idx, p in enumerate(paths):
            if s.get('cancel'):
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
            sc = analyzer.analyze_image(str(p))
            if sc:
                s['scores'].append(sc)
            s['analyzed'] = len(s['scores'])
        s['progress'] = 100
        s['status'] = f"完成 · 已评分 {len(s['scores'])} 张 · 来源：{chain} · 用时 {_fmt(time.time()-t0)}"
    except Exception as e:
        logger.error(f"rank failed: {e}", exc_info=True)
        s['status'] = f"发生错误：{e}"
    finally:
        s['running'] = False


# --------------------------------------------------------------------------- #
#  HTML
# --------------------------------------------------------------------------- #
HTML = r'''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>照片筛选 · PhotoCurator 中文版</title>
<style>
  :root{--bg:#f4f6fb;--panel:#fff;--panel2:#eef1f7;--text:#1c2330;--muted:#6b7280;
        --accent:#2563eb;--good:#16a34a;--warn:#d97706;--bad:#dc2626;--border:#dde3ec;--shadow:rgba(20,40,80,.10);color-scheme:light}
  [data-theme=dark]{--bg:#0f141c;--panel:#161d28;--panel2:#1d2633;--text:#e8edf5;--muted:#9aa6b6;
        --accent:#3b82f6;--border:#27313f;--shadow:rgba(0,0,0,.5);color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
  .top{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:58px;padding:12px 20px;background:linear-gradient(90deg,#1e40af,#2563eb);color:#fff}
  .brand{font-size:17px;font-weight:700}.brand small{font-weight:400;opacity:.8;font-size:12px}
  .steps{display:flex;gap:8px;min-width:0;overflow-x:auto;scrollbar-width:none}.steps::-webkit-scrollbar{display:none}
  .step{padding:7px 16px;background:rgba(255,255,255,.18);border:2px solid transparent;border-radius:9px;cursor:pointer;font-weight:600;font-size:13px;color:#fff}
  .step:hover{background:rgba(255,255,255,.3)} .step.active{background:#fff;color:var(--accent)}
  .theme{background:rgba(255,255,255,.18);border:none;color:#fff;width:38px;height:32px;border-radius:8px;cursor:pointer}
  .viewport{display:flex;height:calc(100vh - 58px);height:calc(100dvh - 58px);min-height:0}
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
  .btn.god{background:linear-gradient(90deg,#f59e0b,#d946ef);font-weight:700}
  .btn.god.stopping{background:var(--bad)}
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
  .progress-text{font-size:12px;color:var(--muted);margin-top:5px}
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
  .move-select.off{background:rgba(20,25,35,.42);color:transparent}
  .move-select.off:hover{color:#fff;background:rgba(37,99,235,.75)}
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
  body.processing .pbg-toggle,
  body.processing .remove-btn,
  body.processing .status-toggle,
  body.processing .badge-tier,
  body.processing .move-select,
  body.processing .move-bulk,
  body.processing #restoreAll,
  body.processing #exportBtn,
  body.processing #exportPbgBtn,
  body.processing #moveBlurryBtn,
  body.processing #dedupApplyBtn{pointer-events:none;opacity:.45;filter:grayscale(.25)}
  .dedup-review{display:flex;flex-direction:column;gap:14px;width:100%;grid-column:1/-1}
  .dedup-group{border:1px solid var(--border);border-radius:12px;background:var(--panel);padding:12px}
  .dedup-group-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;font-size:12px}
  .dedup-group-head b{font-size:13px}
  .dedup-choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .dedup-choice{position:relative;border:2px solid transparent;border-radius:10px;overflow:hidden;background:var(--panel2);cursor:pointer;transition:border-color .15s,box-shadow .15s,transform .15s}
  .dedup-choice:hover{transform:translateY(-1px);border-color:rgba(37,99,235,.45)}
  .dedup-choice.selected{border-color:var(--good);box-shadow:0 0 0 2px color-mix(in srgb,var(--good) 18%,transparent)}
  .dedup-choice img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block}
  .dedup-choice-meta{padding:7px 8px;font-size:11px}
  .dedup-choice-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
  .dedup-choice-state{margin-top:4px;font-weight:700;color:var(--muted)}
  .dedup-choice.selected .dedup-choice-state{color:var(--good)}
  .dedup-recommend{position:absolute;top:6px;left:6px;background:var(--good);color:#fff;border-radius:5px;padding:3px 7px;font-size:10px;font-weight:700;z-index:2}
  body.processing .photo-card{cursor:default}

  .zoomctl{display:flex;align-items:center;gap:4px;background:rgba(255,255,255,.14);padding:3px;border-radius:8px;flex:0 0 auto}
  .zoomctl button{border:0;background:transparent;color:#fff;min-width:28px;height:26px;border-radius:6px;cursor:pointer;font-weight:700}
  .zoomctl button:hover{background:rgba(255,255,255,.18)}
  .zoomctl .zoomval{min-width:48px;text-align:center;font-size:11px;font-weight:700;user-select:none}
  @media (max-width: 820px){.zoomctl .zoomval{min-width:42px}.zoomctl button{min-width:26px}}
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
    .viewport{height:calc(100vh - 96px);height:calc(100dvh - 96px);flex-direction:column}
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
</style></head><body>
<div class="top">
  <div class="brand">🖼️ 照片筛选 <small>PhotoCurator 中文版 · v{{ app_version }}</small></div>
  <div class="steps">
    <div class="step active" data-step="cull">1 · 模糊筛选</div>
    <div class="step" data-step="dedup">2 · 相似去重</div>
    <div class="step" data-step="rank">3 · 智能优选</div>
  </div>
  <div class="top-right">
    <div class="zoomctl" title="界面缩放（Ctrl + / Ctrl - / Ctrl 0）">
      <button id="zoomOut" type="button" aria-label="缩小界面">−</button>
      <span class="zoomval" id="zoomVal">100%</span>
      <button id="zoomIn" type="button" aria-label="放大界面">＋</button>
      <button id="zoomReset" type="button" aria-label="恢复100%">↺</button>
    </div>
    <button class="theme" id="themeToggle" title="切换浅色 / 深色主题" aria-label="切换浅色 / 深色主题">🌙</button>
  </div>
</div>
<div class="viewport">
  <div class="sidebar">
    <div class="sidebar-scroll">
      <div class="sidebar-title">📁 照片文件夹</div>
      <div class="folder-row">
        <input type="text" id="folderInput" placeholder="请选择或粘贴照片文件夹路径">
        <button class="btn" id="browseBtn">选择文件夹…</button>
      </div>
      <div id="shortcuts"></div>

      <div class="sidebar-title" style="margin-top:6px" id="settingsTitle">⚙️ 当前设置</div>
      <div id="settingsPanel"></div>

      <div class="panel-box">
        <div class="stat-row" data-steps="cull dedup rank"><span>照片数量</span><span class="v" id="sImages">0</span></div>
        <div class="stat-row" data-steps="cull"><span>清晰</span><span class="v" id="sSharp">0</span></div>
        <div class="stat-row" data-steps="cull"><span>轻微软（可保留）</span><span class="v" id="sSoft" style="color:var(--warn)">0</span></div>
        <div class="stat-row" data-steps="cull"><span>模糊</span><span class="v" id="sBlurry">0</span></div>
        <div class="stat-row" data-steps="dedup"><span>去重后保留</span><span class="v" id="sGroups">0</span></div>
        <div class="stat-row" data-steps="cull dedup rank"><span>当前显示</span><span class="v" id="sShowing">0</span></div>
        <div id="removedBox" style="display:none">已移除 <b id="removedN">0</b> 张 · <a id="restoreAll">全部恢复</a></div>
      </div>
    </div>

    <!-- Pinned action footer: always visible regardless of scroll / window height -->
    <div class="sidebar-actions">
      <button class="btn-ghost" id="exportBtn" style="display:none">⬇ 导出优选照片…</button>
      <button class="btn-ghost" id="exportPbgBtn" style="display:none">📱 导出手机壁纸…</button>
      <button class="btn-ghost" id="moveBlurryBtn" style="display:none">🗂️ 移动模糊照片 → 模糊照片（Blurred）</button>
      <button class="btn cta" id="dedupApplyBtn" style="display:none">✓ 确认处理未保留照片</button>
      <button class="btn" id="startBtn">🚀 开始筛选</button>
      <button class="btn god" id="godBtn" title="自动执行：模糊筛选 → 相似去重 → 智能优选">⚡ 一键全流程</button>
    </div>
  </div>
  <div class="main">
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-text" id="progressText">…</div>
    </div>
    <div class="filter-bar" id="filterBar" style="display:none"></div>
    <div class="pager" id="pager" style="display:none"></div>
    <div class="gallery" id="gallery"><div class="empty"><div class="icon">🎞️</div><div>先选择照片文件夹，然后点击“开始筛选”</div></div></div>
  </div>
</div>

<div class="lightbox" id="lightbox">
  <div class="lb-bar">
    <div><div id="lbName">—</div><div style="font-size:12px;opacity:.7" id="lbCount"></div></div>
    <div class="lb-actions">
      <button class="lb-btn pbg" id="lbPhoneBg" style="display:none">📱 手机壁纸</button>
      <button class="lb-btn restore" id="lbMoveSelect" style="display:none">☑ 加入移动</button>
      <button class="lb-btn toggle" id="lbToggle" style="display:none">→ 标记为模糊</button>
      <button class="lb-btn restore" id="lbRestore" style="display:none">↺ 全部恢复</button>
      <button class="lb-btn remove" id="lbRemove" style="display:none">✕ 移除</button>
      <button class="lb-close" id="lbClose" title="关闭大图" aria-label="关闭大图">✕</button>
    </div>
  </div>
  <button class="lb-nav lb-prev" id="lbPrev" title="上一张" aria-label="上一张">‹</button>
  <img class="lb-img" id="lbImg" src="">
  <button class="lb-nav lb-next" id="lbNext" title="下一张" aria-label="下一张">›</button>
  <div class="lb-side" id="lbSide"></div>
  <div class="lb-shortcuts" style="position:absolute;left:16px;bottom:10px;color:rgba(255,255,255,.55);font-size:10px;z-index:21;pointer-events:none">大图快捷键：← → 切换 · Esc 关闭 · B 壁纸 · X 移除</div>
</div>

<div class="toast-wrap" id="toastWrap"></div>
<div id="cn-build-badge" style="position:fixed;right:10px;bottom:8px;z-index:50;font-size:10px;color:var(--muted);opacity:.55;pointer-events:none">照片筛选 · 中文桌面版 v{{ app_version }}</div>

<script>
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
let folder=null, photos=[], lbList=[], lbIndex=0, currentStep='cull';
let isRunning=false, runningStep=null, codespacesMode=false;
let lastRankSig='', renderedCount=0, photoIdx=0, lastStep=null, weightTimer=null, removedCount=0, pollFailures=0, largeResultWarned=false;
// These controls are needed by setupFilterBar() during initial page boot.
// Define them before the first setupFilterBar() call to avoid TDZ failures
// that would stop Codespaces shortcut/sample initialization.
const startBtn=document.getElementById('startBtn');
let godMode=false, godAbort=false, godResolve=null;
const CATS=[['aesthetic','综合观感'],['composition','构图'],['technical','技术质量'],['sharpness','清晰度'],['color','色彩']];
const catColor=(i,n)=>`hsl(${Math.round(i*360/(n||CATS.length))},80%,62%)`;
const CATCOLORS=CATS.map((_,i)=>catColor(i,CATS.length));
const DEFAULTS={aesthetic:30,composition:22,technical:20,sharpness:16,color:12};
let weights={...DEFAULTS};
let autoDedup=false;

/* theme */
const tt=document.getElementById('themeToggle');
tt.onclick=()=>{const d=document.documentElement.getAttribute('data-theme')==='dark';
  document.documentElement.setAttribute('data-theme',d?'light':'dark');tt.textContent=d?'🌙':'☀️';
  if(exMap){exMapTheme=currentMapStyle();exMap.setStyle(MAP_STYLES[exMapTheme]);}};

/* UI zoom: independent from Windows DPI scaling; persisted per user. */
const ZOOM_MIN=0.80, ZOOM_MAX=1.40, ZOOM_STEP=0.10;
let uiZoom=1;
try{
  const saved=parseFloat(localStorage.getItem('pc-ui-zoom')||'1');
  if(Number.isFinite(saved))uiZoom=Math.min(ZOOM_MAX,Math.max(ZOOM_MIN,saved));
}catch(_){}
function applyUiZoom(v){
  uiZoom=Math.round(Math.min(ZOOM_MAX,Math.max(ZOOM_MIN,v))*100)/100;
  document.documentElement.style.zoom=String(uiZoom);
  document.documentElement.classList.toggle('ui-zoom-large',uiZoom>=1.20);
  const z=document.getElementById('zoomVal');if(z)z.textContent=Math.round(uiZoom*100)+'%';
  try{localStorage.setItem('pc-ui-zoom',String(uiZoom));}catch(_){}
}
document.getElementById('zoomOut').onclick=()=>applyUiZoom(uiZoom-ZOOM_STEP);
document.getElementById('zoomIn').onclick=()=>applyUiZoom(uiZoom+ZOOM_STEP);
document.getElementById('zoomReset').onclick=()=>applyUiZoom(1);
document.addEventListener('keydown',e=>{
  if(!e.ctrlKey)return;
  if(e.key==='+'||e.key==='='){e.preventDefault();applyUiZoom(uiZoom+ZOOM_STEP);}
  else if(e.key==='-'){e.preventDefault();applyUiZoom(uiZoom-ZOOM_STEP);}
  else if(e.key==='0'){e.preventDefault();applyUiZoom(1);}
});
applyUiZoom(uiZoom);

/* settings panels per step */
function settingsHTML(step){
  if(step==='cull') return `<div class="wgroup"><label>筛选严格度 <b id="optVal">1.00</b></label>
      <input type="range" id="opt" min="0.6" max="1.6" step="0.05" value="1.0">
      <div class="slider-value">数值越低保留越多，越高筛选越严格</div></div>
      <label class="check"><input type="checkbox" id="cAdaptive" checked> 根据当前文件夹自适应阈值</label>
      <label class="check" style="margin-top:6px"><input type="checkbox" id="cRescue" checked> 质量保护：保留轻微软但构图优秀的照片</label>`;
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
      <div class="slider-value" style="margin-top:10px;line-height:1.55">先完成筛选和分组，不会立即移动文件。筛选后可逐组对比并切换“保留”照片，最后再统一确认处理。</div>`;
  // rank
  return `<div class="sidebar-title" style="margin-bottom:4px">⚖️ 评分权重</div>
    <div class="panel-box" id="weightPanel"></div>
    <button class="btn-ghost" id="resetWeights" style="margin-top:8px">↺ 恢复推荐权重</button>
    <div class="wgroup" style="margin-top:10px"><label>候选展示数量</label><input type="number" id="topn" min="1" max="500" value="50">
      <div class="slider-value">仅决定筛选完成后首轮展示多少张候选照片，不会直接移动、删除或导出文件。筛选后可继续手动移除 / 恢复，再确认导出。</div></div>`;
}
function renderWeights(){
  const wp=document.getElementById('weightPanel'); if(!wp)return;
  wp.innerHTML=CATS.map(([k,lab])=>`<div class="wgroup"><label>${lab} <b id="wv_${k}">${weights[k]}</b></label>
     <input type="range" min="0" max="50" value="${weights[k]}" id="w_${k}"></div>`).join('');
  CATS.forEach(([k])=>{const el=document.getElementById('w_'+k);
    el.oninput=()=>{weights[k]=parseInt(el.value);document.getElementById('wv_'+k).textContent=el.value;scheduleReweight();};});
}
function scheduleReweight(){clearTimeout(weightTimer);weightTimer=setTimeout(()=>{
  fetch('/api/weights',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({weights,topn:parseInt((document.getElementById('topn')||{}).value)||50})})
    .then(r=>r.json()).then(d=>renderRank(d.photos||[]));},140);}
function applyStepStats(){
  // Show only the stat rows relevant to the current step so irrelevant zeros
  // (e.g. Sharp/Blurry/Unique while Ranking) don't look like errors.
  document.querySelectorAll('.stat-row[data-steps]').forEach(row=>{
    row.style.display=row.dataset.steps.split(' ').includes(currentStep)?'':'none';
  });
}
function renderSettings(){
  applyStepStats();
  document.getElementById('settingsPanel').innerHTML=settingsHTML(currentStep);
  const opt=document.getElementById('opt'),val=document.getElementById('optVal');
  if(opt&&val)opt.oninput=()=>{val.textContent=(currentStep==='dedup'||currentStep==='cull')?parseFloat(opt.value).toFixed(2):opt.value;};
  const pm=document.getElementById('pairMode');
  if(pm){pm.value=pairMode;pm.onchange=()=>{pairMode=pm.value;};}
  if(currentStep==='rank'){renderWeights();
    const rw=document.getElementById('resetWeights');if(rw)rw.onclick=()=>{weights={...DEFAULTS};renderWeights();scheduleReweight();};}
}

/* Switch the visible step (used by tab clicks AND God mode). */
function activateStep(step){
  currentStep=step;
  document.querySelectorAll('.step').forEach(x=>x.classList.toggle('active',x.dataset.step===step));
  renderSettings();
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.add('btn-ghost');mb.classList.remove('btn','cta');startBtn.classList.remove('secondary');}
  document.getElementById('dedupApplyBtn').style.display='none';
  document.getElementById('progressWrap').style.display='none';  // clear stale summary
  document.getElementById('gallery').innerHTML=emptyHTML(currentStep);
  lastRankSig='';renderedCount=0;photoIdx=0;lastStep=null;
  gPage=0;lastGallerySig='';gItems=[];document.getElementById('pager').style.display='none';
  setupFilterBar();
  if(step==='cull')updateCullMoveButton();
}
/* step tabs (blocked while a step is running) */
document.querySelectorAll('.step').forEach(t=>t.onclick=()=>{
  if(isRunning){toast('请先停止当前正在执行的任务。','bad');return;}
  activateStep(t.dataset.step);
});
renderSettings();

/* cull filter chips */
let cullFilter='all', cullType='all', rankFilter='all', pairMode='both', lastFmtSig='';
function setupFilterBar(){
  const bar=document.getElementById('filterBar');
  if(currentStep==='cull'){
    const opts=[['all','全部'],['sharp','清晰'],['soft','轻微软 ★'],['blurry','模糊']];
    // Per-format chips (NEF, CR2, ARW, ...) built from what's actually loaded.
    const rawFmts=[...new Set(photos.filter(p=>p.raw).map(p=>p.fmt||'RAW'))].sort();
    const hasHeic=photos.some(p=>p.heic);
    const types=[['all','全部格式'],['raw','仅 RAW'],['jpg','仅 JPG'],
      ...(hasHeic?[['heic','仅 HEIC']]:[]),
      ...(rawFmts.length>1?rawFmts.map(f=>['ext:'+f.toLowerCase(),'仅 '+f]):[])];
    if(!types.some(([k])=>k===cullType))cullType='all';
    bar.style.display='flex';
    const blurry=photos.filter(p=>p.tier==='blurry');
    const moveSelected=blurry.filter(p=>p.move_selected!==false).length;
    bar.innerHTML=opts.map(([k,l])=>`<button class="chip${k===cullFilter?' active':''}" data-f="${k}">${l}</button>`).join('')
      +`<span class="chip-sep"></span>`
      +types.map(([k,l])=>`<button class="chip${k===cullType?' active':''}" data-t="${k}">${l}</button>`).join('')
      +(blurry.length?`<span class="chip-sep"></span><span class="move-summary">待移动 <b id="cullMoveCount">${moveSelected}/${blurry.length}</b></span><button class="chip move-bulk" id="moveSelAll">全选</button><button class="chip move-bulk" id="moveSelNone">全不选</button>`:'');
    bar.querySelectorAll('.chip[data-f]').forEach(c=>c.onclick=()=>{cullFilter=c.dataset.f;gPage=0;
      bar.querySelectorAll('.chip[data-f]').forEach(x=>x.classList.toggle('active',x.dataset.f===cullFilter));
      renderCullStep(photos);});
    bar.querySelectorAll('.chip[data-t]').forEach(c=>c.onclick=()=>{cullType=c.dataset.t;gPage=0;
      bar.querySelectorAll('.chip[data-t]').forEach(x=>x.classList.toggle('active',x.dataset.t===cullType));
      renderCullStep(photos);});
    const ma=document.getElementById('moveSelAll'),mn=document.getElementById('moveSelNone');
    if(ma)ma.onclick=()=>setAllBlurryMoveSelection(true);
    if(mn)mn.onclick=()=>setAllBlurryMoveSelection(false);
    updateCullMoveButton();
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
  bar.style.display='none';
}
setupFilterBar();
document.getElementById('gallery').innerHTML=emptyHTML(currentStep);  // step explainer on load

/* shortcuts */
function sdLabel(p){const parts=p.split(/[\\/]/).filter(Boolean);
  const tail=parts.slice(-2).join('/');
  const m=/^([A-Za-z]:)/.exec(p);return m?m[1]+' '+tail:tail;}
function loadShortcuts(){fetch('/api/shortcuts').then(r=>r.json()).then(d=>{
  let h='';
  codespacesMode=!!d.codespaces;
  const fi=document.getElementById('folderInput');
  const bb=document.getElementById('browseBtn');

  if(codespacesMode){
    bb.disabled=true;bb.textContent='云端路径模式';
    fi.placeholder='输入 Codespaces 中的云端文件夹路径';
    h+=`<div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:9px 10px;font-size:11px;line-height:1.55;margin-bottom:7px">☁️ <b>Codespaces 在线预览</b><br>当前只能访问云端工作区文件，不能直接读取你电脑的 C:/F: 等本地硬盘。</div>`;
    if(d.demo_folder){
      const p=d.demo_folder;
      h+=`<button class="shortcut" data-p="${escHtml(p)}"><span class="tag recent">在线样例</span>${escHtml(sdLabel(p))}</button>`;
      if(!folder){folder=p;fi.value=p;}
    }
  }else{
    bb.disabled=isRunning;bb.textContent='选择文件夹…';
    fi.placeholder='请选择或粘贴照片文件夹路径';
  }

  (d.sd||[]).forEach(o=>{const p=(typeof o==='string')?o:o.path;
    const br=(o&&o.brand)?(' · '+o.brand):'';
    h+=`<button class="shortcut" data-p="${escHtml(p)}"><span class="tag sd">SD${escHtml(br)}</span>${escHtml(sdLabel(p))}</button>`;});
  (d.recent||[]).slice(0,4).forEach(p=>h+=`<button class="shortcut" data-p="${escHtml(p)}"><span class="tag recent">最近</span>${escHtml(sdLabel(p))}</button>`);
  if(d.rawpy===false)h=`<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;`
    +`padding:8px 10px;font-size:11px;line-height:1.5;margin-bottom:6px">⚠️ <b>RAW 支持未启用</b> — `
    +`未安装 rawpy，CR2/NEF/ARW/DNG 等 RAW 文件会被跳过。<br>`
    +`请重新运行依赖安装后再试。</div>`+h;
  if(d.heif===false)h=`<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;`
    +`padding:8px 10px;font-size:11px;line-height:1.5;margin-bottom:6px">⚠️ <b>HEIC 支持未启用</b> — `
    +`未安装 pillow-heif，iPhone 的 HEIC/HEIF 文件会被跳过。<br>`
    +`请重新运行依赖安装后再试。</div>`+h;
  if(!(d.sd||[]).length&&!codespacesMode)h+=`<div style="font-size:11px;color:var(--muted);margin-top:6px">未检测到相机存储卡；插入后会自动出现在这里，也可以直接选择文件夹。</div>`;
  document.getElementById('shortcuts').innerHTML=h;
  document.querySelectorAll('.shortcut').forEach(b=>{b.disabled=isRunning;b.onclick=()=>{if(isRunning)return;folder=b.dataset.p;fi.value=folder;};});
}).catch(()=>{});
}
loadShortcuts();
setInterval(()=>{if(!document.hidden&&!isRunning)loadShortcuts();},30000);  // pick up a card inserted later
document.getElementById('folderInput').oninput=e=>folder=e.target.value.trim();
document.getElementById('browseBtn').onclick=async()=>{
  const btn=document.getElementById('browseBtn');
  const old=btn.textContent;btn.disabled=true;btn.textContent='正在选择…';
  try{
    let selected=null;
    let nativeError=null;
    if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_folder){
      try{
        selected=await window.pywebview.api.pick_folder();
      }catch(err){
        nativeError=err;
      }
    }
    if(selected==null && (!window.pywebview||nativeError)){
      const r=await fetch('/api/browse',{method:'POST'});
      if(!r.ok)throw new Error('HTTP '+r.status);
      const d=await r.json();
      selected=d.folder||null;
    }
    if(selected){
      folder=selected;
      document.getElementById('folderInput').value=folder;
    }
  }catch(err){
    toast('无法打开文件夹选择器：'+(err.message||'未知错误'),'bad');
  }finally{
    btn.disabled=false;btn.textContent=old;
  }
};

/* start / stop (the same button toggles) */
function setStartBtn(running){
  isRunning=running;
  document.body.classList.toggle('processing',running);
  const idleLabel=currentStep==='rank'?'🏆 开始智能筛选':(currentStep==='dedup'?'🪢 开始相似筛选':'✂️ 开始模糊筛选');
  startBtn.textContent=running?'■ 停止':idleLabel;
  startBtn.classList.toggle('stopping',running);
  const fi=document.getElementById('folderInput');
  const bb=document.getElementById('browseBtn');
  if(fi)fi.disabled=running;
  if(bb)bb.disabled=running||codespacesMode;
  document.querySelectorAll('.shortcut').forEach(x=>x.disabled=running);
}
async function startStep(step){
  runningStep=step;
  if(step==='cull')cullReady=false;
  pollFailures=0;largeResultWarned=false;
  document.getElementById('progressWrap').style.display='block';
  document.getElementById('gallery').innerHTML='';
  lastRankSig='';renderedCount=0;photoIdx=0;lastStep=step;
  gPage=0;lastGallerySig='';document.getElementById('pager').style.display='none';
  document.getElementById('exportBtn').style.display='none';
  document.getElementById('exportPbgBtn').style.display='none';
  {const mb=document.getElementById('moveBlurryBtn');mb.style.display='none';mb.classList.add('btn-ghost');mb.classList.remove('btn','cta');startBtn.classList.remove('secondary');}
  setRemoved(0);
  setStartBtn(true);

  const opt=document.getElementById('opt');
  const ad=document.getElementById('cAdaptive'),rs=document.getElementById('cRescue');
  if(step!=='cull'&&cullType!=='all'){
    const tl=cullType.startsWith('ext:')?cullType.slice(4).toUpperCase():cullType.toUpperCase();
    toast('继续处理：仅 '+tl+' 格式。若要包含全部照片，请将“模糊筛选”的格式切换为“全部格式”。','info');
  }

  try{
    const response=await fetch('/api/run/'+step,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        folder,
        opt:opt?parseFloat(opt.value):0,
        adaptive:ad?ad.checked:true,
        rescue:rs?rs.checked:true,
        ftype:step==='cull'?'all':cullType,
        pair:step==='cull'?'both':pairMode,
        topn:parseInt((document.getElementById('topn')||{}).value)||50
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
    if(godMode){
      godAbort=true;
      if(godResolve){const r=godResolve;godResolve=null;r();}
    }
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
startBtn.onclick=()=>{ isRunning?doStop():doStart(); };

/* ---- God mode: run Cull → Dedup → Rank back-to-back ---- */
const godBtn=document.getElementById('godBtn');
function setGodBtn(on){ godBtn.textContent=on?'■ 停止一键全流程':'⚡ 一键全流程'; godBtn.classList.toggle('stopping',on); }
async function godRun(){
  if(!folder){toast('请先选择照片文件夹','bad');return;}
  if(isRunning){toast('请先停止当前正在执行的任务。','bad');return;}
  godMode=true;godAbort=false;setGodBtn(true);startBtn.disabled=true;
  try{
    for(const step of ['cull','dedup','rank']){
      if(godAbort)break;
      activateStep(step);
      // Lock the auto-move checkbox during a God-mode run (only present on the
      // Dedup step) so it can't be toggled mid-pipeline.
      {const ao=document.getElementById('autoOrg'); if(ao)ao.disabled=true;}
      await new Promise(res=>{ godResolve=res; startStep(step); });
    }
    if(!godAbort)toast('✨ 一键全流程完成，优选照片已排序','good');
  } finally {
    godMode=false;setGodBtn(false);startBtn.disabled=false;
    const ao=document.getElementById('autoOrg'); if(ao)ao.disabled=false;  // re-enable
  }
}
godBtn.onclick=()=>{
  if(godMode){ godAbort=true; doStop(); toast('正在停止一键全流程…','bad'); }
  else godRun();
};
function poll(step){
  fetch('/api/progress/'+step)
    .then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(d=>{
      pollFailures=0;
      document.getElementById('progressFill').style.width=d.progress+'%';
      document.getElementById('progressText').textContent=d.status;
      const st=d.stats||{};
      if('images'in st)document.getElementById('sImages').textContent=st.images;
      if('sharp'in st)document.getElementById('sSharp').textContent=st.sharp;
      if('blurry'in st)document.getElementById('sBlurry').textContent=st.blurry;
      if('soft'in st)document.getElementById('sSoft').textContent=st.soft;
      if('groups'in st)document.getElementById('sGroups').textContent=st.groups;
      if(step==='rank')renderRank(d.photos||[]);
      else if(step==='cull')renderCullStep(d.photos||[]);
      else renderDedupGroups(d.photos||[]);

      if(d.running){
        setTimeout(()=>poll(step),350);
        return;
      }

      if(d.truncated&&!largeResultWarned){
        largeResultWarned=true;
        toast('本次已完整分析 '+(d.result_total||0)+' 张照片。为保持界面流畅，当前界面只展示前 5000 条结果；统计和后续处理仍使用完整结果。','info');
      }

      document.getElementById('progressFill').style.width='100%';
      setStartBtn(false);runningStep=null;
      if(step==='rank'&&photos.length)document.getElementById('exportBtn').style.display='block';
      if(step==='cull'){
        cullReady=true;
        updateCullMoveButton();
      }
      if(step==='dedup'){
        const dg=Number((d.stats||{}).duplicate_groups||0);
        const ab=document.getElementById('dedupApplyBtn');
        ab.style.display=(!godMode&&dg>0)?'block':'none';
      }
      if(godResolve){const r=godResolve;godResolve=null;r();}
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
      document.getElementById('progressText').textContent=msg;
      setStartBtn(false);runningStep=null;
      if(godMode){
        godAbort=true;
        if(godResolve){const r=godResolve;godResolve=null;r();}
      }
    });
}

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
    cull:['✂️','步骤 1 · 模糊筛选','先识别并筛出失焦、明显模糊的照片。',
      ['🔍 评估真实清晰度，雾气和夜空不会被简单误判为模糊',
       '🟢 清晰 &nbsp;·&nbsp; 🟠 轻微软（可保留） &nbsp;·&nbsp; 🔴 模糊',
       '📁 选择照片文件夹，然后点击 <b>开始模糊筛选</b>；完成后再确认是否移动模糊照片']],
    dedup:['🪢','步骤 2 · 相似去重','将连拍或高度相似照片归组，保留其中最佳的一张。',
      ['📸 自动识别并归组近似照片',
       '⭐ 每组优先保留最清晰的一张',
       '🪢 点击 <b>开始相似筛选</b>；完成后逐组对比并确认保留照片，再统一处理未保留项']],
    rank:['🏆','步骤 3 · 智能优选','综合画质、构图与色彩，找出更值得保留的照片。',
      ['🎯 综合评估构图、光线、清晰度、色彩与对比度',
       '🥇 先完成智能评分并展示候选照片，提供单张评分雷达图',
       '☑️ 筛选后可手动移除 / 恢复候选；确认无误后再导出优选照片']]
  };
  const c=C[step]||C.cull;
  return `<div class="empty"><div class="icon">${c[0]}</div>
    <div class="title">${c[1]}</div><p>${c[2]}</p>
    <div class="lines">${c[3].map(l=>`<div>${l}</div>`).join('')}</div></div>`;
}
function cullCard(p){
  const i=photoIdx++;const path=escHtml(p.path);
  const isDedup=currentStep==='dedup';
  // Dedup: the badge tells you this frame won a burst ("同组最佳"); the info
  // line says how many near-duplicates were set aside. The raw sharpness number
  // (used only to pick the winner) is no longer shown — it wasn't meaningful.
  const g=p.group||1;
  const badge=isDedup
    ? `<div class="badge good">${g>1?('★ 同组最佳 · 共 '+g+' 张'):'保留'}</div>`
    : (p.badge?`<div class="badge ${p.badgeType}">${p.badge}</div>`:'');
  const toggle=currentStep==='cull'?`<button class="status-toggle" data-path="${path}">${p.kept?'→ 模糊':'✓ 保留'}</button>`:'';
  const cls=p.kept?'kept':(p.rejected?'rejected':'');
  const info=isDedup
    ? `<div class="photo-score" style="font-weight:500;opacity:.75">${g>1?((g-1)+' 张相似照片已归组'):'原始照片'}</div>`
    : `<div class="photo-score">${p.score}</div>`;
  return `<div class="photo-card ${cls}" data-i="${i}" data-path="${path}">${badge}${toggle}
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span></div>${info}</div></div>`;
}
let lastGallerySig='', gPage=0, gItems=[];
const PAGE_SIZE=200;
function renderGallery(items){   /* dedup: paginated + reconciling (order-stable) */
  gItems=items;photos=items;const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastGallerySig='';lastStep=currentStep;gPage=0;}
  if(!items.length){g.innerHTML=EMPTY;lastGallerySig='';renderedCount=0;updatePager();document.getElementById('sShowing').textContent=0;return;}
  const pages=Math.max(1,Math.ceil(items.length/PAGE_SIZE));
  if(gPage>=pages)gPage=pages-1;if(gPage<0)gPage=0;
  const start=gPage*PAGE_SIZE,end=Math.min(items.length,start+PAGE_SIZE);
  const slice=items.slice(start,end);
  // Signature includes the page + group size so paging and growing clusters
  // ("同组最佳") always re-render; reconcile within.
  const sig=gPage+'#'+slice.map(p=>p.path+':'+(p.group||1)).join('|');
  if(sig===lastGallerySig){updatePager();document.getElementById('sShowing').textContent=items.length;return;}
  lastGallerySig=sig;
  const emp=g.querySelector('.empty');if(emp)emp.remove();
  // Reuse existing card nodes by path so reordering/paging never duplicates
  // thumbnails or reloads images. data-i keeps the GLOBAL index (lightbox).
  const existing={};g.querySelectorAll('.photo-card').forEach(n=>existing[n.dataset.path]=n);
  const frag=document.createDocumentFragment();
  slice.forEach((p,k)=>{const i=start+k;const key=String(p.path);let node=existing[key];
    if(node){
      // Keep reused cards in sync. For Dedup show "同组最佳"/"无相似重复"
      // (never the raw sharpness number); the badge updates as clusters grow.
      const g=p.group||1;
      const b=node.querySelector('.badge');
      const sc=node.querySelector('.photo-score');
      if(b)b.textContent=(g>1?('★ 同组最佳 · 共 '+g+' 张'):'保留');
      if(sc)sc.textContent=(g>1?((g-1)+' 张相似照片已归组'):'原始照片');
      node.dataset.i=i;delete existing[key];}
    else{const w=document.createElement('div');w.innerHTML=cullCard(p);node=w.firstElementChild;node.dataset.i=i;}
    frag.appendChild(node);});
  Object.values(existing).forEach(n=>n.remove());g.appendChild(frag);
  renderedCount=slice.length;updatePager();
  document.getElementById('sShowing').textContent=items.length;
}
function renderDedupGroups(groups){
  photos=groups||[];
  const g=document.getElementById('gallery');
  document.getElementById('sShowing').textContent=groups.length;
  if(!groups.length){
    g.innerHTML='<div class="empty"><div class="icon">✓</div><div class="title">没有需要人工处理的相似组</div><p>单独照片会自动保留；只有检测到 2 张及以上相似照片时才会出现在这里。</p></div>';
    return;
  }
  g.innerHTML='<div class="dedup-review">'+groups.map((group,idx)=>{
    const members=(group.members||[]);
    return '<div class="dedup-group" data-group="'+group.group_id+'">'
      +'<div class="dedup-group-head"><b>相似组 '+(idx+1)+' · '+members.length+' 张</b><span>点击任意照片切换保留项</span></div>'
      +'<div class="dedup-choices">'+members.map((p,mi)=>{
        const sel=!!p.selected;
        return '<div class="dedup-choice '+(sel?'selected':'')+'" data-group="'+group.group_id+'" data-path="'+escHtml(p.path)+'">'
          +(sel?'<div class="dedup-recommend">✓ 当前保留</div>':'')
          +'<img src="'+p.thumb+'" loading="lazy" decoding="async">'
          +'<div class="dedup-choice-meta"><div class="dedup-choice-name">'+escHtml(p.name)+'</div>'
          +'<div class="dedup-choice-state">'+(sel?'系统推荐 / 当前选择':'点击改为保留')+'</div></div></div>';
      }).join('')+'</div></div>';
  }).join('')+'</div>';
}
function selectDedupPhoto(groupId,path){
  fetch('/api/dedup-select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:Number(groupId),path})})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{renderDedupGroups(d.photos||[]);toast('已切换本组保留照片','good');})
    .catch(err=>toast('切换失败：'+(err.message||'未知错误'),'bad'));
}
function applyDedupSelection(){
  const btn=document.getElementById('dedupApplyBtn');
  btn.disabled=true;btn.textContent='正在处理…';
  fetch('/api/dedup-apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d;})
    .then(d=>{
      btn.style.display='none';
      toast('已按当前选择处理 '+(d.moved||0)+' 张相似照片','good');
      document.getElementById('progressText').textContent='处理完成 · 已移动 '+(d.moved||0)+' 张未保留照片到 Duplicates 文件夹';
    })
    .catch(err=>toast('处理失败：'+(err.message||'未知错误'),'bad'))
    .finally(()=>{btn.disabled=false;btn.textContent='✓ 确认处理未保留照片';});
}
document.getElementById('dedupApplyBtn').onclick=applyDedupSelection;

function updatePager(){
  const pager=document.getElementById('pager');if(!pager)return;
  const total=gItems.length,pages=Math.max(1,Math.ceil(total/PAGE_SIZE));
  if(!['cull','dedup','rank'].includes(currentStep)||total<=PAGE_SIZE){
    pager.style.display='none';return;
  }
  const start=gPage*PAGE_SIZE+1,end=Math.min(total,(gPage+1)*PAGE_SIZE);
  pager.style.display='flex';
  pager.innerHTML=`<button id="pgPrev" ${gPage===0?'disabled':''}>← 上一页</button>`
    +`<span>第 ${gPage+1} / ${pages} 页 · ${start}–${end} / 共 ${total}</span>`
    +`<button id="pgNext" ${gPage>=pages-1?'disabled':''}>下一页 →</button>`;
  const rerender=()=>{
    lastGallerySig='';lastCullSig='';
    if(currentStep==='cull')renderCullStep(photos);
    else if(currentStep==='rank')renderRank(photos);
    else renderGallery(gItems);
    document.querySelector('.main')?.scrollTo({top:0,behavior:'auto'});
  };
  document.getElementById('pgPrev').onclick=()=>{if(gPage>0){gPage--;rerender();}};
  document.getElementById('pgNext').onclick=()=>{if(gPage<pages-1){gPage++;rerender();}};
}
function rankCard(p,idx){const path=escHtml(p.path);
  const on=p.phonebg?' on':'';
  return `<div class="photo-card kept${p.phonebg?' pbg':''}" data-i="${idx}" data-path="${path}"><div class="rank-num">${p.rank!=null?p.rank:idx+1}</div>
    <button class="pbg-toggle${on}" data-path="${path}" title="${p.phonebg?'已设为手机壁纸，点击取消':'设为手机壁纸'}">📱</button>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span>
      <button class="remove-btn" data-path="${path}" title="从优选结果中移除（不会删除原文件）">✕ 移除</button></div>
      <div class="photo-score">${p.score}</div></div></div>`;}
function renderRank(items){
  photos=items;const g=document.getElementById('gallery');
  if(lastStep!==currentStep){g.innerHTML='';lastRankSig='';lastStep=currentStep;gPage=0;}
  const fbar=document.getElementById('filterBar');
  if(!items.length){fbar.style.display='none';}
  else if(fbar.style.display==='none'||!fbar.querySelector('.chip')){setupFilterBar();}

  rankView=(rankFilter==='pbg')?items.filter(p=>p.phonebg):items;
  gItems=rankView;
  const pbgN=items.filter(p=>p.phonebg).length;
  const pbgChip=document.getElementById('pbgChipCount');if(pbgChip)pbgChip.textContent=pbgN;
  document.getElementById('exportPbgBtn').style.display=(currentStep==='rank'&&pbgN>0)?'block':'none';

  if(!rankView.length){
    g.innerHTML=(items.length&&rankFilter==='pbg')
      ?'<div class="empty"><div class="icon">📱</div><div>还没有标记为手机壁纸的照片。<br>点击优选照片上的 📱 按钮即可标记。</div></div>'
      :EMPTY;
    lastRankSig='';updatePager();return;
  }

  const pages=Math.max(1,Math.ceil(rankView.length/PAGE_SIZE));
  if(gPage>=pages)gPage=pages-1;if(gPage<0)gPage=0;
  const start=gPage*PAGE_SIZE,end=Math.min(rankView.length,start+PAGE_SIZE);
  const slice=rankView.slice(start,end);
  const sig=rankFilter+'#'+gPage+'|'+slice.map(p=>p.rank+':'+p.path+':'+(p.phonebg?1:0)).join('|');
  if(sig===lastRankSig){updatePager();return;}
  lastRankSig=sig;

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

/* ---- cull (3-tier, reconciling, filterable) ---- */
let cullView=[], rankView=[], lastCullSig='', lastCullMoveSig='';
let cullReady=false;
const TIER_NAME={sharp:'清晰',soft:'轻微软',blurry:'模糊'};
const NEXT_TIER={sharp:'soft',soft:'blurry',blurry:'sharp'};
function cullCardHtml(p,idx){const path=escHtml(p.path);
  const cls=p.tier==='sharp'?'kept':p.tier==='soft'?'soft':'rejected';
  const moveSel=p.tier==='blurry'
    ?`<button class="move-select${p.move_selected===false?' off':''}" data-path="${path}" data-selected="${p.move_selected===false?'0':'1'}" title="${p.move_selected===false?'未加入本次移动，点击重新选择':'已加入本次移动，点击保留在原位置'}">${p.move_selected===false?'':'✓'}</button>`
    :'';
  return `<div class="photo-card ${cls}" data-i="${idx}" data-path="${path}" data-tier="${p.tier}">
    ${moveSel}
    <button class="badge ${p.badgeType} badge-tier" data-path="${path}" data-tier="${p.tier}" title="点击切换：清晰 → 轻微软 → 模糊">⇄ ${p.badge}</button>
    <img class="photo-img" src="${p.thumb}" loading="lazy" decoding="async">
    <div class="photo-info"><div class="pi-row"><span class="photo-name">${escHtml(p.name)}</span><span class="ftype${p.raw?'':(p.heic?' heic':' jpg')}">${p.fmt||(p.raw?'RAW':p.heic?'HEIC':'JPG')}</span></div><div class="photo-score">${p.score}</div></div></div>`;}
function renderCullStep(items){
  photos=items;
  const fSig=[...new Set(items.filter(p=>p.raw).map(p=>p.fmt||'RAW'))].sort().join(',');
  if(fSig!==lastFmtSig){lastFmtSig=fSig;setupFilterBar();}

  const filtered=items.filter(p=>(cullFilter==='all'||p.tier===cullFilter)
    &&(cullType==='all'||(cullType==='raw'?!!p.raw
      :cullType==='heic'?!!p.heic
      :cullType==='jpg'?(!p.raw&&!p.heic)
      :('ext:'+String(p.fmt||'').toLowerCase())===cullType)));

  gItems=filtered;
  const pages=Math.max(1,Math.ceil(filtered.length/PAGE_SIZE));
  if(gPage>=pages)gPage=pages-1;
  if(gPage<0)gPage=0;
  const start=gPage*PAGE_SIZE,end=Math.min(filtered.length,start+PAGE_SIZE);
  cullView=filtered.slice(start,end);

  const g=document.getElementById('gallery');
  if(!filtered.length){
    g.innerHTML=EMPTY;lastCullSig='';lastStep=currentStep;
    document.getElementById('sShowing').textContent=0;
    updatePager();
    return;
  }

  const moveSig=items.filter(p=>p.tier==='blurry')
    .map(p=>p.path+':'+(p.move_selected===false?'0':'1')).join('|');
  if(moveSig!==lastCullMoveSig){lastCullMoveSig=moveSig;setupFilterBar();}
  const sig=gPage+'#'+cullView.map(p=>p.path+':'+p.tier+':'+(p.move_selected===false?'0':'1')).join('|');
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
    if(node&&node.dataset.tier===p.tier){
      node.dataset.i=idx;delete existing[p.path];
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
  const blurry=photos.filter(p=>p.tier==='blurry');
  return {total:blurry.length,selected:blurry.filter(p=>p.move_selected!==false).length};
}
function updateCullMoveButton(){
  const mb=document.getElementById('moveBlurryBtn');if(!mb)return;
  const n=cullMoveCounts();
  const count=document.getElementById('cullMoveCount');if(count)count.textContent=n.selected+'/'+n.total;
  if(currentStep!=='cull'||!cullReady||!n.total||godMode){
    mb.style.display='none';mb.disabled=false;mb.classList.remove('cta','btn');mb.classList.add('btn-ghost');
    startBtn.classList.remove('secondary');return;
  }
  mb.style.display='block';
  if(n.selected>0){
    mb.disabled=false;
    mb.textContent='🗂️ 移动 '+n.selected+' 张 → 模糊照片（Blurred）';
    mb.classList.remove('btn-ghost');mb.classList.add('btn','cta');
    startBtn.classList.add('secondary');
  }else{
    mb.disabled=true;
    mb.textContent='🗂️ 未选择需要移动的模糊照片';
    mb.classList.remove('btn','cta');mb.classList.add('btn-ghost');
    startBtn.classList.remove('secondary');
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
  const dc=e.target.closest('.dedup-choice');if(dc&&currentStep==='dedup'){e.stopPropagation();selectDedupPhoto(dc.dataset.group,dc.dataset.path);return;}
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
  document.getElementById('lbImg').src='/api/image?path='+encodeURIComponent(p.path);
  document.getElementById('lbName').textContent=(p.rank!=null?'#'+p.rank+'  ':'')+p.name;
  const extra=(currentStep==='dedup')
    ? ((p.group>1)?('   ·   同组最佳 · 共 '+p.group+' 张（'+(p.group-1)+' 张相似照片已归组）'):'   ·   原始照片')
    : (p.score!=null?'   ·   '+p.score:'');
  document.getElementById('lbCount').textContent=(lbIndex+1)+' / '+lbList.length+extra;
  const rm=document.getElementById('lbRemove'),rs=document.getElementById('lbRestore'),tg=document.getElementById('lbToggle'),ms=document.getElementById('lbMoveSelect');
  rm.style.display=currentStep==='rank'?'inline-block':'none';
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
  }else{side.style.display='block';side.innerHTML=`<h3>${currentStep==='cull'?'清晰度':'照片'}</h3><div style="font-size:13px;opacity:.85">${escHtml(p.name)}</div><div style="font-size:26px;font-weight:700;margin-top:8px">${p.score!=null?p.score:''}</div>`;}
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
    if(settings)rows+=exifRow('拍摄参数',settings);
    if(e.lat!=null&&e.lon!=null){
      const c=e.lat.toFixed(5)+',&nbsp;'+e.lon.toFixed(5);
      rows+=exifRow('位置',`<a href="https://www.google.com/maps?q=${e.lat},${e.lon}" target="_blank" rel="noopener" style="white-space:nowrap">${c}</a>`,true);
      rows+=`<div class="exmap"><div class="mapslot" id="exMapSlot"><span class="mappin"></span></div>`
        +`<span class="cred"><a href="https://openfreemap.org/" target="_blank" rel="noopener">OpenFreeMap</a> © `
        +`<a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> · 地图数据 © `
        +`<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a></span></div>`;
      wantMap={lat:e.lat,lon:e.lon};
    }
    if(!rows)rows=`<div style="font-size:12px;opacity:.5">没有可读取的 EXIF 信息。</div>`;
    side.insertAdjacentHTML('beforeend',`<h3>照片信息</h3>${rows}`);
    if(wantMap)mountExifMap(wantMap.lat,wantMap.lon);
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
function currentMapStyle(){
  return document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';}
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
  if(!confirm('是否将已选择的 '+before.selected+' 张“模糊”照片移动到“模糊照片（Blurred）”子文件夹？\n\n未勾选的模糊照片会保留在原位置；只会移动，不会删除原文件。'))return;
  this.disabled=true;this.textContent='正在移动 '+before.selected+' 张…';
  try{
    const r=await fetch('/api/move-blurry',{method:'POST'});
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    const extra=d.failed?('；'+d.failed+' 张失败'):'';
    toast('✓ 已移动 '+d.moved+' 张'+extra+'\n'+d.dest,d.failed?'bad':'good');

    // Pull the authoritative paths/selections back after files were moved so
    // thumbnails, large-image viewing and later actions never keep stale paths.
    const pr=await fetch('/api/progress/cull');
    if(pr.ok){
      const snap=await pr.json();
      photos=snap.photos||[];
      lastCullSig='';lastCullMoveSig='';renderCullStep(photos);
    }
  }catch(err){
    toast('移动失败：'+(err.message||'未知错误'),'bad');
  }finally{
    this.disabled=false;updateCullMoveButton();
  }
};
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
    demo_folder = None
    if CODESPACES_PUBLIC_HOST:
        raw_demo = (
            os.environ.get('PHOTOCURATOR_DEMO_DIR')
            or str(Path(__file__).resolve().parent / '.codespaces_demo')
        )
        if Path(raw_demo).is_dir():
            demo_folder = os.path.realpath(raw_demo)

    return jsonify({
        'sd': [] if CODESPACES_PUBLIC_HOST else detect_sd_cards(),
        'recent': load_recents(),
        'rawpy': HAS_RAWPY,
        'heif': HAS_HEIF,
        'codespaces': bool(CODESPACES_PUBLIC_HOST),
        'demo_folder': demo_folder,
    })


@app.route('/api/browse', methods=['POST'])
def api_browse():
    folder = native_folder_dialog("选择照片文件夹")
    if folder and Path(folder).is_dir():
        state['folder'] = folder
        save_recent(folder)
        return jsonify({'folder': folder})
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


@app.route('/api/set-auto', methods=['POST'])
def api_set_auto():
    data = request.get_json() or {}
    step = data.get('step')
    if step in state['auto']:
        state['auto'][step] = bool(data.get('enabled'))
    return jsonify({'ok': True})


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

    active = [k for k in ('cull', 'dedup', 'rank') if state[k].get('running')]
    if active:
        return jsonify({
            'error': '已有照片处理任务正在运行，请先停止或等待完成',
            'active': active[0]
        }), 409

    state['folder'] = folder
    save_recent(folder)

    try:
        if step == 'cull':
            strictness = min(1.6, max(0.6, float(data.get('opt') or 1.0)))
            adaptive = bool(data.get('adaptive', True))
            rescue_on = bool(data.get('rescue', True))
            target, args = run_cull, (folder, strictness, adaptive, rescue_on)
        elif step == 'dedup':
            threshold = min(0.95, max(0.5, float(data.get('opt') or 0.8)))
            target, args = run_dedup, (
                folder, threshold, data.get('ftype', 'all'), data.get('pair', 'both')
            )
        else:
            state['topn'] = min(500, max(1, int(data.get('topn', 50))))
            target, args = run_rank, (
                folder, data.get('ftype', 'all'), data.get('pair', 'both')
            )
    except (TypeError, ValueError):
        return jsonify({'error': '处理参数无效，请恢复默认设置后重试'}), 400

    state[step]['running'] = True
    try:
        threading.Thread(target=target, args=args, daemon=True,
                         name=f'photocurator-{step}').start()
    except Exception:
        state[step]['running'] = False
        raise
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
        limit = 200 if s['running'] else UI_RESULT_CAP
        photos = all_photos[:limit]
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': photos,
                        'truncated': (not s['running'] and len(all_photos) > len(photos)),
                        'result_total': len(all_photos),
                        'stats': {'images': len(all_photos), 'sharp': s['sharp'],
                                  'soft': s['soft'], 'blurry': s['blurry'],
                                  'move_selected': sum(
                                      1 for p in all_photos
                                      if p.get('tier') == 'blurry' and p.get('move_selected', True)
                                  )}})
    if step == 'dedup':
        s = state['dedup']
        all_photos = s['photos']
        photos = all_photos[:UI_RESULT_CAP]
        duplicate_photos = sum(g.get('count', 0) for g in s.get('groups_data', []) if g.get('count', 0) > 1)
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': photos,
                        'truncated': (not s['running'] and len(all_photos) > len(photos)),
                        'result_total': len(all_photos),
                        'stats': {'groups': s['groups'],
                                  'duplicate_groups': len(all_photos),
                                  'duplicate_photos': duplicate_photos}})
    if step == 'rank':
        s = state['rank']
        now = time.time()
        if (not s['running']) or now - float(s.get('preview_at', 0.0)) >= 1.0:
            s['preview'] = build_topn()
            s['preview_at'] = now
        return jsonify({'running': s['running'], 'progress': s['progress'], 'status': s['status'],
                        'photos': s.get('preview', []),
                        'stats': {'images': s['total']}})
    abort(404)


@app.route('/api/dedup-select', methods=['POST'])
def api_dedup_select():
    """Switch the kept photo inside one completed similarity group."""
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    s = state['dedup']
    if not s.get('complete'):
        return jsonify({'error': '请先完成相似照片筛选'}), 409
    data = request.get_json() or {}
    try:
        gid = int(data.get('group_id'))
    except (TypeError, ValueError):
        return jsonify({'error': '相似组编号无效'}), 400
    path = str(data.get('path') or '')
    group = next((g for g in s.get('groups_data', []) if g.get('group_id') == gid), None)
    if not group:
        return jsonify({'error': '未找到这个相似组'}), 404
    if path not in {m.get('path') for m in group.get('members', [])}:
        return jsonify({'error': '这张照片不属于当前相似组'}), 400
    group['selected_path'] = path
    for member in group.get('members', []):
        member['selected'] = member.get('path') == path
    s['kept_paths'] = [g.get('selected_path') for g in s.get('groups_data', [])
                       if g.get('selected_path')]
    s['photos'] = [g for g in s.get('groups_data', []) if g.get('count', 0) > 1]
    return jsonify({'ok': True, 'photos': s['photos'], 'kept': len(s['kept_paths'])})


@app.route('/api/dedup-apply', methods=['POST'])
def api_dedup_apply():
    """Move non-selected members only after the user explicitly confirms."""
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    folder = state.get('folder')
    s = state['dedup']
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400
    if not s.get('complete'):
        return jsonify({'error': '请先完成相似照片筛选'}), 409
    if s.get('applied'):
        return jsonify({'ok': True, 'moved': 0, 'already_applied': True})
    dups = []
    for group in s.get('groups_data', []):
        if group.get('count', 0) <= 1:
            continue
        selected = group.get('selected_path')
        dups.extend(m.get('path') for m in group.get('members', [])
                    if m.get('path') and m.get('path') != selected)
    if not dups:
        s['applied'] = True
        return jsonify({'ok': True, 'moved': 0, 'dest': str(Path(folder) / 'Duplicates')})
    try:
        org = PhotoOrganizer(folder)
        result = org.move_duplicate_photos(dups)
        moved = int(result.get('moved', 0) or 0)
        failed = int(result.get('failed', 0) or 0)
        s['applied'] = failed == 0
        s['status'] = (f"处理完成 · 已移动 {moved} 张未保留照片到重复照片（Duplicates）文件夹"
                       + (f" · {failed} 张失败" if failed else ''))
        return jsonify({'ok': failed == 0, 'moved': moved, 'failed': failed,
                        'dest': str(Path(folder) / 'Duplicates')})
    except Exception as e:
        logger.warning(f"dedup apply failed: {e}")
        return jsonify({'error': f'处理相似照片失败：{e}'}), 500


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
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    data = request.get_json() or {}
    path = str(data.get('path') or '')
    if not _known_rank_path(path):
        return jsonify({'error': '当前优选结果中未找到这张照片'}), 404
    state['excluded'].add(path)
    return jsonify({'ok': True, 'removed': len(state['excluded']), 'photos': build_topn()})

@app.route('/api/restore', methods=['POST'])
def api_restore():
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
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
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
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
    s['sharp'] = sum(1 for p in s['photos'] if p['tier'] == 'sharp')
    s['soft'] = sum(1 for p in s['photos'] if p['tier'] == 'soft')
    s['blurry'] = sum(1 for p in s['photos'] if p['tier'] == 'blurry')
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
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked

    data = request.get_json() or {}
    photos = state['cull'].get('photos', [])

    if 'all' in data:
        selected = bool(data.get('all'))
        for photo in photos:
            if photo.get('tier') == 'blurry':
                photo['move_selected'] = selected
        count, total = _blurry_move_counts()
        return jsonify({'ok': True, 'selected': count, 'total': total})

    path = str(data.get('path') or '')
    photo = next((p for p in photos if p.get('path') == path), None)
    if not photo:
        return jsonify({'error': '未找到照片'}), 404
    if photo.get('tier') != 'blurry':
        return jsonify({'error': '只有“模糊”照片可以加入移动列表'}), 400

    photo['move_selected'] = bool(data.get('selected', True))
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
    """Move reviewed Blurry-tier photos only after explicit user action."""
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
    folder = state.get('folder')
    if not folder or not Path(folder).is_dir():
        return jsonify({'error': '未选择有效的照片文件夹'}), 400

    blurry_photos = [
        pp for pp in state['cull'].get('photos', [])
        if pp.get('tier') == 'blurry' and pp.get('move_selected', True)
    ]
    if not blurry_photos:
        return jsonify({'ok': True, 'moved': 0,
                        'dest': str(Path(folder) / 'Blurred')})

    dest_dir = Path(folder) / 'Blurred'
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = failed = 0

    for photo in blurry_photos:
        src = Path(photo.get('path', ''))
        if not src.is_file():
            failed += 1
            continue
        if src.parent.resolve() == dest_dir.resolve():
            photo['move_selected'] = False
            continue

        dst = dest_dir / src.name
        if dst.exists():
            stamp = time.strftime('%Y%m%d_%H%M%S')
            n = 1
            candidate = dest_dir / f"{src.stem}_{stamp}{src.suffix}"
            while candidate.exists():
                n += 1
                candidate = dest_dir / f"{src.stem}_{stamp}_{n}{src.suffix}"
            dst = candidate

        try:
            shutil.move(str(src), str(dst))
            old = str(src)
            new = str(dst)
            photo['path'] = new
            photo['thumb'] = thumb_url(new)
            photo['move_selected'] = False
            moved += 1
            # Keep downstream survivor references coherent if the user changed
            # tiers after a completed cull.
            sp = state['cull'].get('sharp_paths', [])
            state['cull']['sharp_paths'] = [new if p == old else p for p in sp]
        except Exception as e:
            failed += 1
            logger.warning(f"move-blurry failed {src}: {e}")

    selected_left, blurry_total = _blurry_move_counts()
    return jsonify({'ok': failed == 0, 'moved': moved, 'failed': failed,
                    'selected': selected_left, 'total': blurry_total,
                    'dest': str(dest_dir)})

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
    blocked = _reject_mutation_while_running()
    if blocked:
        return blocked
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
