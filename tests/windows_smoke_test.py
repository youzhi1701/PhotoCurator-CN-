#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stable ASCII-named entry point for the Windows smoke test."""

from pathlib import Path
import runpy

TARGET = Path(__file__).with_name("基础冒烟测试.py")

if __name__ == "__main__":
    if not TARGET.is_file():
        raise SystemExit(f"Missing smoke test: {TARGET}")
    runpy.run_path(str(TARGET), run_name="__main__")
