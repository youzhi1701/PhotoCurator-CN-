#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dynamic file-lifecycle safety checks for PhotoCurator."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

_temp = tempfile.TemporaryDirectory(prefix="photocurator_file_safety_")
os.environ["PHOTOCURATOR_DATA_DIR"] = str(Path(_temp.name) / "runtime")

import photo_curator as pc  # noqa: E402


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    root = Path(_temp.name)
    src_dir = root / "source"
    dst_dir = root / "trash"
    src_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)

    payload = (b"PhotoCurator-cross-volume-recovery\n" * 4096)

    # Simulate a crash after the destination was committed but before source unlink.
    src = src_dir / "A.jpg"
    dst = dst_dir / "A.jpg"
    src.write_bytes(payload)
    dst.write_bytes(payload)
    require(pc._files_identical(src, dst), "identical committed files were not recognized")
    pc._safe_move_file(src, dst)
    require(not src.exists(), "recovery did not finish source unlink")
    require(dst.read_bytes() == payload, "recovery changed committed destination content")

    # A different pre-existing destination must never be overwritten or treated
    # as a recovered commit.
    src2 = src_dir / "B.jpg"
    dst2 = dst_dir / "B.jpg"
    src2.write_bytes(b"source-B")
    dst2.write_bytes(b"different-B")
    try:
        pc._safe_move_file(src2, dst2)
    except FileExistsError:
        pass
    else:
        raise AssertionError("different existing destination was overwritten")
    require(src2.read_bytes() == b"source-B", "source changed after collision rejection")
    require(dst2.read_bytes() == b"different-B", "destination changed after collision rejection")

    # Main photo may already be committed while XMP/AAE sidecars remain at source.
    original = src_dir / "C.jpg"
    committed = dst_dir / "C.jpg"
    committed.write_bytes(b"main-already-committed")
    xmp = original.with_suffix(".xmp")
    xmp.write_text("<xmp>preserve</xmp>", encoding="utf-8")
    pc._resume_bundle_sidecars(original, committed)
    require(not xmp.exists(), "source sidecar remained after recovery")
    require(committed.with_suffix(".xmp").read_text(encoding="utf-8") == "<xmp>preserve</xmp>",
            "sidecar recovery lost content")

    # Diagnostic sanitization must hide known local roots.
    replacements = [(str(src_dir), "<SOURCE>")]
    safe = pc._sanitize_diagnostic_value(
        {"path": str(src_dir / "private" / "photo.jpg"), "detail": ["ok", str(src_dir)]},
        replacements,
    )
    require(str(src_dir) not in str(safe), "diagnostic sanitizer leaked a known source path")

    print("File lifecycle safety checks OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            pc.TASK_MANAGER.shutdown(timeout=1.0)
        except Exception:
            pass
        # Windows keeps FileHandler targets locked until logging is shut down.
        # Close them explicitly so the test also proves there are no hidden
        # worker-owned handles beyond the normal logging lifecycle.
        logging.shutdown()
        _temp.cleanup()
