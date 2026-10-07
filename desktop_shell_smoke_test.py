#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression test for the pywebview close-to-tray deadlock."""

import os
import tempfile
import threading
import time
from pathlib import Path


def require(ok, message):
    if not ok:
        raise AssertionError(message)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    os.environ["PHOTOCURATOR_DATA_DIR"] = str(Path(td) / "data")

    import desktop_app

    class FakeWindow:
        def __init__(self):
            self.hidden = threading.Event()
            self.hide_calls = 0

        def hide(self):
            self.hide_calls += 1
            self.hidden.set()

    api = desktop_app.DesktopApi()
    fake = FakeWindow()
    api.attach_window(fake)

    started = time.perf_counter()
    decision = api.native_close_decision(True)
    elapsed = time.perf_counter() - started

    require(decision is False, "close-to-tray must cancel the native close")
    require(
        elapsed < 0.03,
        f"blocking close handler took too long: {elapsed:.4f}s",
    )
    require(
        fake.hidden.wait(1.0),
        "window hide was not dispatched after the blocking close event returned",
    )
    require(fake.hide_calls == 1, f"unexpected hide count: {fake.hide_calls}")

    api.allow_exit = True
    started = time.perf_counter()
    decision = api.native_close_decision(True)
    elapsed = time.perf_counter() - started
    require(decision is True, "explicit exit must allow the native close")
    require(elapsed < 0.03, "explicit close decision must remain non-blocking")

    # Duplicate native close signals while a hide is pending must not queue a
    # storm of window operations.
    api.allow_exit = False
    fake2 = FakeWindow()
    api.attach_window(fake2)
    api.native_close_decision(True)
    api.native_close_decision(True)
    require(fake2.hidden.wait(1.0), "deduplicated hide did not execute")
    require(fake2.hide_calls == 1, f"duplicate close queued {fake2.hide_calls} hides")

    try:
        desktop_app.TASK_MANAGER.shutdown(timeout=0.5)
    except Exception:
        pass

print("Desktop close-to-tray deadlock regression OK")
