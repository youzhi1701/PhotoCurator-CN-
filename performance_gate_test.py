#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoCurator performance-architecture regression gate.

This test intentionally uses only the standard library and source contracts.
It protects the low-risk hot-path decisions that keep large libraries smooth
without benchmarking noisy CI hardware.
"""

from pathlib import Path
import re

SOURCE = Path("photo_curator.py").read_text(encoding="utf-8")
TASK_SOURCE = Path("background_tasks.py").read_text(encoding="utf-8")


def require(condition, message):
    if not condition:
        raise SystemExit("Performance gate failed: " + message)


def block(start, end):
    a = SOURCE.find(start)
    require(a >= 0, f"missing start marker: {start}")
    b = SOURCE.find(end, a + len(start))
    require(b > a, f"missing end marker after: {start}")
    return SOURCE[a:b]


require('APP_VERSION = "1.7.8"' in SOURCE, "expected v1.7.8 source version")

# 1) Task center: one lightweight heartbeat, adaptive cadence, no idle
#    fan-out to the three result-bearing progress endpoints.
task_js = block("async function refreshTaskCenter(){", "/* settings panels per step */")
require("fetch('/api/task-center')" in task_js, "task center must use unified heartbeat")
require("/api/progress/" not in task_js, "task center must not poll result endpoints")
require("setInterval(refreshTaskCenter" not in SOURCE, "fixed 1.4s idle polling reintroduced")
require("TASK_CENTER_BUSY_MS=1200" in SOURCE, "busy task-center cadence missing")
require("TASK_CENTER_IDLE_MS=8000" in SOURCE, "idle task-center backoff missing")
require("if(document.hidden)return;" in SOURCE, "hidden-page task heartbeat suspension missing")

task_api = block("@app.route('/api/task-center')", "@app.route('/api/tasks/<int:task_id>')")
require("build_topn(" not in task_api, "task heartbeat must never rebuild Rank Top-N")
require("'photos'" not in task_api and '"photos"' not in task_api,
        "task heartbeat must not serialize photo result payloads")

# 2) Rank preview: recompute only when preview inputs became dirty.
progress_api = block("@app.route('/api/progress/<step>')", "@app.route('/api/results/cull')")
require("preview_score_count" in progress_api and "preview_dirty" in progress_api,
        "Rank preview dirty tracking missing")
require("if preview_dirty and" in progress_api,
        "Rank Top-N rebuild must be guarded by dirty state")
require("if (not s['running']) or now - float(s.get('preview_at', 0.0)) >= 1.0:" not in progress_api,
        "old idle Rank rebuild condition reintroduced")

# 3) Shared scan: intermediate status publishes counts only. The immutable
#    final tuple is shared by Cull/Dedup rather than repeatedly copying a
#    growing path list.
scan = block("def _shared_list_images(", "def current_scan_snapshot(")
require("entry['paths'] = list(paths)" not in scan,
        "growing scan path list is being copied into progress snapshots")
require("snapshot = tuple(paths)" in scan, "final shared scan must be immutable")
require("'paths': snapshot" in scan and "return snapshot" in scan,
        "Cull/Dedup must reuse the same immutable scan result")
require("'fingerprints': fingerprints" in scan,
        "shared scan must publish one reusable file-metadata snapshot")
require("def _shared_scan_fingerprints(" in SOURCE,
        "shared scan fingerprint accessor missing")
require("threading.Timer(" in scan and "_release_heavy_scan_snapshot" in SOURCE,
        "large shared scan snapshots must release heavy RAM after startup")

# 4) Rendering: unchanged recursive Cull / similarity payloads must not
#    rebuild the whole gallery.
cull_render = block("function renderCullStep(items){", "function cullMoveCounts(){")
require("if(sig===lastCullSig&&lastStep===currentStep)" in cull_render,
        "recursive Cull unchanged-render guard missing")
dedup_render = block("function renderDedupGroups(groups){", "function selectDedupPhoto(")
require("dedupSig" in dedup_render and "dedupSig===lastDedupSig" in dedup_render,
        "Dedup unchanged-render guard missing")

# 5) High-frequency thumbnail resizing is coalesced into browser frames and
#    does not synchronously write localStorage for every wheel event.
thumb = block("/* Gallery thumbnail zoom:", "const WORKSPACE_COPY=")
require("function scheduleThumbSize" in thumb and "requestAnimationFrame" in thumb,
        "thumbnail resize frame batching missing")
require("thumbSaveTimer=setTimeout" in thumb,
        "thumbnail preference writes must remain debounced")

# 6) Offline Catalog uses a bounded, bidirectional DOM window instead of
#    accumulating every loaded history card forever.
catalog = block("const CATALOG_PAGE_SIZE=400,CATALOG_DOM_WINDOW=800;", "let lastStorageSummaryAt=0;")
require("previous.items.splice(0,drop)" in catalog,
        "Catalog next-page navigation must release old DOM/data rows")
require("previous.items.splice(previous.items.length-drop,drop)" in catalog,
        "Catalog previous-page navigation must release tail DOM/data rows")
require("catalogLoadEarlier" in catalog and "catalogLoadMore" in catalog,
        "Catalog bounded window must remain bidirectional")

# 7) File metadata and path rendering stay single-pass / lexical on large
#    external libraries instead of repeatedly touching the filesystem.
cull = block("def run_cull(", "def _cull_allowed_for_dedup(")
require("fingerprints = _shared_scan_fingerprints(folder, recursive) or _fingerprints(images)" in cull,
        "Cull must reuse shared scan fingerprints with a safe fallback")
require("_load_cull_metrics_map(images, fingerprints)" in cull,
        "Cull cache lookup must reuse the shared fingerprint pass")
require("_load_review_overrides(images, fingerprints)" in cull,
        "review overrides must reuse the shared fingerprint pass")
require("_shared_scan_fingerprints(folder, recursive)" in cull,
        "Cull must consume shared scan fingerprints")
require("def classify_all(validate_files=False):" in cull,
        "Cull live classification must separate final file validation")
live_classify = cull.split("def classify_all(validate_files=False):", 1)[1].split("t0 = time.time()", 1)[0]
require("if validate_files and not Path(it['path']).is_file()" in live_classify,
        "Cull must avoid per-photo is_file checks during live reclassification")
require("'rel_dir': it['rel_dir']" in live_classify and "'raw': it['raw']" in live_classify,
        "Cull live reclassification must reuse static per-photo display metadata")
require("current_paths = {str(p) for p in images}" not in cull,
        "unused full-library current_paths set must not return")
require("np.fromiter(" in cull,
        "Cull adaptive thresholds must avoid Python-list plus NumPy double allocation")
require("photos[::-1]" not in cull and "photos.reverse()" in cull,
        "Cull result reversal must stay in-place")
dedup = block("def run_dedup(", "# --------------------------------------------------------------------------- #\n#  RANK")
require("_shared_scan_fingerprints(folder, recursive)" in dedup,
        "Dedup must consume shared scan fingerprints")
require(".parent.resolve()" not in dedup,
        "Dedup folder grouping must not resolve every photo path")
relative = block("def relative_folder(", "def _path_reservation_key(")
require(".resolve()" not in relative,
        "relative folder formatting must not perform per-photo filesystem resolve I/O")

# 8) Thumbnail decode concurrency is bounded and cache writes are atomic.
thumb_py = block("def make_thumb_file(", "def _background_build_offline_previews(")
require("_THUMB_BUILD_SEMAPHORE" in SOURCE and "with _THUMB_BUILD_SEMAPHORE:" in thumb_py,
        "thumbnail decode concurrency guard missing")
require("os.replace(tmp, out)" in thumb_py,
        "thumbnail cache writes must remain atomic")
require("preview-v2" in SOURCE,
        "bounded RAW/HEIF display preview cache version must stay current")

# 9) Lightbox navigation preloads adjacent images/EXIF and bounds client cache.
lightbox = block("const exifCache=new Map()", "/* GPS map")
require("EXIF_CACHE_LIMIT=256" in lightbox and "prefetchLbNeighbors" in lightbox,
        "bounded Lightbox EXIF/image prefetch missing")
require("getExif(path)" in lightbox,
        "Lightbox EXIF cache path missing")

# 10) The periodic task heartbeat must use the compact task query, while the
#     full 30-row history remains available only for explicit task inspection.
require("def heartbeat(self):" in TASK_SOURCE,
        "compact background task heartbeat missing")
task_api = block("@app.route('/api/task-center')", "@app.route('/api/tasks/<int:task_id>')")
require("TASK_MANAGER.heartbeat()" in task_api,
        "task center must use compact task heartbeat")

# 11) Per-photo/group compositing filters and fixed background repaint are
#     intentionally avoided; the large visual shell may keep glass surfaces.
aurora = block("/* v1.4.0 Aurora + iOS glass visual system */", ".top-right{")
require("background-attachment:fixed" not in aurora,
        "fixed Aurora background repaint was reintroduced")
glass_line = next((line for line in aurora.splitlines()
                   if ".panel-box,.shortcut" in line), "")
require(".photo-card" not in glass_line and ".dedup-group" not in glass_line
        and ".folder-group" not in glass_line,
        "photo/group nodes must not receive backdrop blur")

# 12) Activity-log retention remains amortized rather than running a bounded
#     table cleanup query after every small UI action.
activity = block("def _activity(", "def _cached_cull_metrics(")
require("_ACTIVITY_TRIM_EVERY" in SOURCE and "% _ACTIVITY_TRIM_EVERY" in activity,
        "activity-log cleanup must stay amortized")

print("Performance regression gate OK: v1.7.8 hot paths, rendering, scan metadata, thumbnail scheduling, Lightbox prefetch and compact task heartbeat are intact")
