#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Desktop startup guard: do not show an old instance as the updated app."""
from pathlib import Path

source = Path("desktop_app.py").read_text(encoding="utf-8")
guard = source.split("if ctypes.get_last_error() == 183:", 1)[1].split("else:\n    _instance_guard", 1)[0]
assert "MessageBoxW(" in guard
assert "APP_VERSION" in guard
assert "系统托盘彻底退出旧程序" in guard
assert "FindWindowW" not in guard
assert "ShowWindow" not in guard
assert "SetForegroundWindow" not in guard
assert "raise SystemExit(0)" in guard
core=Path("photo_curator.py").read_text(encoding="utf-8")
index=core.split("@app.route('/')",1)[1].split("@app.route('/api/shortcuts')",1)[0]
assert "Cache-Control" in index and "no-store" in index
assert "X-PhotoCurator-Version" in index
print("Desktop upgrade instance and stale HTML guards OK")
