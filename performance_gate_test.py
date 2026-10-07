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


def require(condition, message):
    if not condition:
        raise SystemExit("Performance gate failed: " + message)


def block(start, end):
    a = SOURCE.find(start)
    require(a >= 0, f"missing start marker: {start}")
    b = SOURCE.find(end, a + len(start))
    require(b > a, f"missing end marker after: {start}")
    return SOURCE[a:b]


require('APP_VERSION = "1.7.1"' in SOURCE, "expected v1.7.1 source version")

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

# Per-photo compositing filters are intentionally avoided; the large visual
# shell may keep its two glass surfaces.
pbg_css = block(".pbg-toggle{", ".pbg-toggle:hover")
require("backdrop-filter" not in pbg_css, "per-card backdrop blur reintroduced")

print(
    "性能回归门禁通过：任务心跳、Rank 预览、共享扫描、增量渲染、"
    "缩略图节流与卡片合成契约均保持有效"
)
