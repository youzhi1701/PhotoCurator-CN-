#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片筛选基础冒烟测试：中文路径、特殊字符、图像读取和安全路径。"""

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from raw_loader import imread_bgr, imread_gray, open_image_pil


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def main():
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

        photo_curator.state["folder"] = str(root)
        for p in found:
            safe = photo_curator._safe_image_path(str(p))
            assert_true(safe is not None, f"安全路径校验误拒绝：{p}")

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

        outside = Path(td) / "目录外照片.jpg"
        Image.new("RGB", (20, 20), "white").save(outside)
        assert_true(photo_curator._safe_image_path(str(outside)) is None,
                    "安全路径校验错误地允许了所选目录外文件")

    print("基础冒烟测试通过：中文路径 / 特殊字符 / 图像解码 / 安全路径均正常")


if __name__ == "__main__":
    main()
