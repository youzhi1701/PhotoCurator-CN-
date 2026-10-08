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
print("Navigation, scroll restoration and format filter guards OK")
