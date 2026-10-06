#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片筛选基础冒烟测试：中文路径、特殊字符、图像读取和安全路径。"""

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from raw_loader import imread_bgr, imread_gray, open_image_pil


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    with tempfile.TemporaryDirectory(prefix="照片筛选_自检_") as td:
        root = Path(td) / "中文照片 文件夹（测试）"
        root.mkdir(parents=True, exist_ok=True)

        names = [
            "普通照片.jpg",
            "带 空格 的照片.png",
            "括号（样片）.jpg",
            "特殊_#_&_测试.jpg",
        ]

        for i, name in enumerate(names):
            p = root / name
            arr = np.zeros((120, 160, 3), dtype=np.uint8)
            arr[:, :, 0] = 20 + i * 20
            arr[:, :, 1] = 100
            arr[:, :, 2] = 180
            Image.fromarray(arr).save(p)

            bgr = imread_bgr(p, reduced=False)
            gray = imread_gray(p, reduced=False)
            assert_true(bgr is not None and bgr.shape[:2] == (120, 160),
                        f"OpenCV 彩色读取失败：{p}")
            assert_true(gray is not None and gray.shape[:2] == (120, 160),
                        f"OpenCV 灰度读取失败：{p}")

            with open_image_pil(p) as im:
                assert_true(im.size == (160, 120), f"Pillow 读取失败：{p}")

        # Importing the app after images exist lets us also test enumeration.
        import photo_curator
        found = photo_curator.list_images(root)
        assert_true(len(found) == len(names),
                    f"照片枚举数量错误：期望 {len(names)}，实际 {len(found)}")

        # Recursive library mode must include nested source folders but skip
        # PhotoCurator output directories so prior results are never re-scanned.
        nested = root / "2026" / "三亚" / "第一天"
        nested.mkdir(parents=True, exist_ok=True)
        nested_img = nested / "IMG_递归测试.jpg"
        Image.new("RGB", (40, 30), "white").save(nested_img)

        generated = root / "PhotoCurator_Result" / "Blurred"
        generated.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 30), "white").save(generated / "不应重新扫描.jpg")
        dup_generated = root / "Duplicates"
        dup_generated.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 30), "white").save(dup_generated / "也不应扫描.jpg")

        recursive = photo_curator.list_images(root, recursive=True)
        assert_true(nested_img in recursive,
                    "递归扫描没有包含子文件夹照片")
        assert_true(all("PhotoCurator_Result" not in str(p) and "Duplicates" not in str(p)
                        for p in recursive),
                    f"递归扫描错误包含了程序输出目录：{recursive}")

        rel = photo_curator.relative_folder(nested_img, root)
        assert_true("2026" in rel and "三亚" in rel and "第一天" in rel,
                    f"来源相对路径错误：{rel}")

        source_dest = photo_curator._output_destination(
            nested_img, "Blurred", root, "source", ""
        )
        assert_true(source_dest.parent == nested / "PhotoCurator_Result" / "Blurred",
                    f"默认输出位置错误：{source_dest}")

        photo_curator.state["folder"] = str(root)
        for p in found:
            safe = photo_curator._safe_image_path(str(p))
            assert_true(safe is not None, f"安全路径校验误拒绝：{p}")

        # Built-in test data must always be available through the same API the
        # sidebar uses. The canonical 12 files are persistent and may be
        # replenished if a previous file-move test moved one away.
        demo_dir = Path(photo_curator.ensure_builtin_demo())
        demo_files = sorted(p for p in demo_dir.iterdir() if p.suffix.lower() == ".jpg")
        assert_true(len(demo_files) >= 12,
                    f"内置测试数据数量不足：{len(demo_files)}")

        shortcuts = photo_curator.app.test_client().get(
            "/api/shortcuts",
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(shortcuts.status_code == 200,
                    f"快捷入口接口失败：HTTP {shortcuts.status_code}")
        shortcut_data = shortcuts.get_json()
        assert_true(shortcut_data.get("demo_folder") == str(demo_dir.resolve()),
                    f"内置测试数据入口缺失：{shortcut_data}")
        assert_true(shortcut_data.get("demo_count") == 12,
                    f"内置测试数据标称数量错误：{shortcut_data}")

        # Verify the local HTTP endpoints also survive Unicode/special paths.
        client = photo_curator.app.test_client()
        sample = str(found[0])
        thumb = client.get("/api/thumb", query_string={"path": sample},
                           headers={"Host": f"127.0.0.1:{photo_curator.PORT}"})
        assert_true(thumb.status_code == 200,
                    f"缩略图接口读取失败：HTTP {thumb.status_code}")
        full = client.get("/api/image", query_string={"path": sample},
                          headers={"Host": f"127.0.0.1:{photo_curator.PORT}"})
        assert_true(full.status_code == 200,
                    f"大图接口读取失败：HTTP {full.status_code}")
        # Windows keeps send_file handles locked until responses are closed.
        thumb.close()
        full.close()

        # Blurry classification and file-action selection are separate.
        # Unchecking a blurry frame must not change its classification and the
        # backend must report the exact selected/total move counts.
        photo_curator.state["cull"]["photos"] = [
            {"path": str(found[0]), "tier": "blurry", "move_selected": True},
            {"path": str(found[1]), "tier": "blurry", "move_selected": True},
            {"path": str(found[2]), "tier": "sharp", "move_selected": False},
        ]
        sel = client.post(
            "/api/select-blurry",
            json={"path": str(found[0]), "selected": False},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(sel.status_code == 200, f"模糊照片取消移动失败：HTTP {sel.status_code}")
        payload = sel.get_json()
        assert_true(payload["selected"] == 1 and payload["total"] == 2,
                    f"移动选择计数错误：{payload}")
        assert_true(photo_curator.state["cull"]["photos"][0]["tier"] == "blurry",
                    "取消移动不应改变模糊分类")

        bulk = client.post(
            "/api/select-blurry",
            json={"all": False},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(bulk.status_code == 200, f"模糊照片全不选失败：HTTP {bulk.status_code}")
        payload = bulk.get_json()
        assert_true(payload["selected"] == 0 and payload["total"] == 2,
                    f"全不选计数错误：{payload}")

        outside = Path(td) / "目录外照片.jpg"
        Image.new("RGB", (20, 20), "white").save(outside)
        assert_true(photo_curator._safe_image_path(str(outside)) is None,
                    "安全路径校验错误地允许了所选目录外文件")

    print("基础冒烟测试通过：中文路径 / 特殊字符 / 图像解码 / 安全路径均正常")


if __name__ == "__main__":
    main()
