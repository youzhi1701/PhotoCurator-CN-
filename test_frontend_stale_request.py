#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Protect asynchronous result loading from stale navigation responses."""
from pathlib import Path

source=Path("photo_curator.py").read_text(encoding="utf-8")
dedup=source.split("async function loadDedupPage(",1)[1].split("function updateDedupLoadMore()",1)[0]
assert "requestSerial=++dedupRequestSerial" in dedup
assert "requestFilter!==dedupStatusFilter" in dedup
assert "requestSerial!==dedupRequestSerial" in dedup
assert "!sameFolder(requestFolder,folder)" in dedup
assert dedup.index("requestSerial!==dedupRequestSerial") < dedup.index("if(reset)dedupLiveStore.clear()")
tabs=source.split("document.querySelectorAll('.step').forEach(t=>t.onclick=",1)[1].split("renderSettings();",1)[0]
assert "requestedStep=currentStep, requestedFolder=folder" in tabs
assert "currentStep!==requestedStep" in tabs
assert "!sameFolder(requestedFolder,folder)" in tabs
cull=source.split("function renderCullStep(items){",1)[1].split("function cullMoveCounts()",1)[0]
assert "cullType==='standard'?(!p.raw&&!p.heic)" in cull
assert "workspaceScrollByStep.set(workspaceViewKey(currentStep,folder)" in source
assert "workspaceScrollByStep.get(workspaceViewKey(requestedStep,requestedFolder))" in tabs
assert "if(requestedStep==='dedup')return;" in tabs
assert "workspaceScrollByStep.get(workspaceViewKey('dedup',requestFolder))" in dedup
catalog=source.split("async function loadCatalogRoot(",1)[1].split("let lastStorageSummaryAt=",1)[0]
assert "token=++catalogRootRequestSerial" in catalog
assert "token!==catalogRootRequestSerial" in catalog
assert "if(token===catalogRootRequestSerial)catalogRootLoading=false" in catalog
assert "catalogRootRequestSerial++" in source.split("function resetWorkspaceForFolder()",1)[1].split("function updateStartAvailability()",1)[0]
cull_loader=source.split("async function loadCullPage(",1)[1].split("function updateCullLoadMore()",1)[0]
assert "requestFilter!==cullFilter" in cull_loader
assert "requestedType!==cullType" in cull_loader
assert "token!==cullChunkToken" in cull_loader
assert "CULL_WINDOW_CAP" in cull_loader
assert "cullNextOffset" in cull_loader
dedup_loader=source.split("async function loadDedupPage(",1)[1].split("function updateDedupLoadMore()",1)[0]
assert "DEDUP_WINDOW_CAP" in dedup_loader
assert "dedupNextOffset" in dedup_loader
assert "captureGalleryAnchor" in source and "restoreGalleryAnchor" in source
assert "loadCullPage(false)" in source and "loadDedupPage(false)" in source
print("Stale catalog/root navigation, paged review and scroll guards OK")
