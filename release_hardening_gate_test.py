#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static release-hardening contracts for PhotoCurator v1.7.2."""

from pathlib import Path


def require(value, message):
    if not value:
        raise SystemExit("Release hardening gate failed: " + message)


def block(text, start, end):
    a = text.find(start)
    require(a >= 0, "missing marker: " + start)
    b = text.find(end, a + len(start))
    require(b > a, "missing end marker after: " + start)
    return text[a:b]


core = Path("photo_curator.py").read_text(encoding="utf-8")
tasks = Path("background_tasks.py").read_text(encoding="utf-8")
catalog = Path("catalog.py").read_text(encoding="utf-8")
optional = Path("requirements-optional.txt").read_text(encoding="utf-8")
candidate = Path(".github/workflows/candidate-windows.yml").read_text(encoding="utf-8")
release = Path(".github/workflows/build-release.yml").read_text(encoding="utf-8")
codespaces = Path("在线预览.sh").read_text(encoding="utf-8")
installer_cmd = Path("PhotoCurator-Install.cmd").read_text(encoding="utf-8")

require('APP_VERSION = "1.7.2"' in core, "source version must be v1.7.2")

# Cross-volume move must verify content and reconcile a committed destination.
move = block(core, "def _sha256_file(", "def _move_photo_bundle(")
require("hashlib.sha256()" in move, "SHA-256 verification helper missing")
require("_files_identical(src, tmp)" in move, "cross-volume temp copy is not content verified")
require("dst.is_file() and _files_identical(src, dst)" in move,
        "interrupted destination reconciliation missing")
require("_resume_bundle_sidecars" in core, "sidecar crash recovery missing")

# A running analysis may never be rebound to another source.
select = block(core, "function selectFolderValue(value){", "function fmtDate(")
require("if(isRunning||coreRunning)" in select, "source switch is not blocked while processing")
poll_core = block(core, "async function pollCore(){", "async function coreRun(){")
require("d.src_folder" in poll_core and "!sameFolder(d.src_folder,activeFolder)" in poll_core,
        "parallel core polling does not isolate foreign-folder results")
run_api = block(core, "@app.route('/api/run/<step>'", "@app.route('/api/stop/<step>'")
require("禁止跨图库并行" in run_api, "backend cross-library admission guard missing")

# Dedup review completion changes Rank eligibility and must invalidate preview.
complete = block(core, "@app.route('/api/dedup-complete'", "@app.route('/api/dedup-group-action'")
require("state['rank']['preview_at'] = 0.0" in complete,
        "dedup completion does not invalidate Rank preview")

# High-cardinality surfaces must not create one backdrop blur compositor per card.
require(".folder-group,.dedup-group,.photo-card{backdrop-filter:none" in core,
        "high-cardinality backdrop blur suppression missing")
blur_selector = ".panel-box,.shortcut,.folder-group,.dedup-group,.photo-card"
require(blur_selector not in core, "old per-card backdrop blur selector returned")

# Support bundles redact paths; precise reverse geocoding is explicit opt-in.
require("def _diagnostic_redactions(" in core and "def _sanitize_diagnostic_value(" in core,
        "diagnostic path redaction missing")
diag = block(core, "def _diagnostic_redactions(", "@app.route('/api/browse'")
require("zf.write(log_file" not in diag, "raw unredacted log files are still copied into diagnostics")
require("geoLookupBtn" in core and "lookup.onclick" in core,
        "precise reverse geocode must require an explicit user action")

# Queue idempotency must be atomic and preserve completed attempts.
enqueue = block(tasks, "    def enqueue(", "    def _next_task(")
require('db.execute("BEGIN IMMEDIATE")' in enqueue, "task admission is not serialized")
require("idempotency_key=NULL" in enqueue, "completed task history is not preserved")
require("DELETE FROM background_task" not in enqueue,
        "completed background task history is still deleted during retry")

# Mount rebasing must not delete another source's path-keyed row on collision.
rebase = block(catalog, "def _rebase_persisted_paths", "def refresh_connections")
require("UPDATE OR REPLACE" not in rebase, "mount rebasing can still replace another library state")
require("UPDATE OR IGNORE" in rebase, "collision-preserving rebase policy missing")

# Supply-chain floors and real installer upgrade validation.
require("pillow-heif>=1.3,<2.0" in optional, "pillow-heif security floor missing")
require('"pyinstaller>=6.22.1,<7"' in candidate and '"pyinstaller>=6.22.1,<7"' in release,
        "PyInstaller security floor missing")
require("pip-audit -r requirements-optional.txt" in candidate
        and "pip-audit -r requirements-optional.txt" in release,
        "optional dependency vulnerability audit missing")
require("Get-AuthenticodeSignature" in candidate and "Get-AuthenticodeSignature" in release,
        "WebView2 publisher signature verification missing")
require("Resolve candidate metadata" in candidate and 'CANDIDATE_VERSION: "1.7.' not in candidate,
        "Candidate version is still duplicated outside release_manifest.json")
require("Verify real in-place upgrade from published Stable" in candidate
        and "Verify real in-place upgrade from published Stable" in release,
        "real Stable-to-candidate installer upgrade test missing")
require('manifest["channel"] = "stable"' in release,
        "successful publication does not transition manifest channel to stable")

# Runtime dependency compatibility: Pillow 12.x no longer supports Python 3.9.
require("python3.9" not in codespaces and "Python 3.9" not in codespaces,
        "Codespaces bootstrap still advertises unsupported Python 3.9")
require("py -3.9" not in installer_cmd and "Python 3.9" not in installer_cmd,
        "Windows source installer still advertises unsupported Python 3.9")

print("Release hardening gate OK")
