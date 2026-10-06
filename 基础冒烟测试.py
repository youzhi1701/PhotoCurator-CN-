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

        generated = root / "PhotoCurator_Result（照片筛选结果）" / "Blurred（模糊照片）"
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
        assert_true(source_dest.parent == nested / "PhotoCurator_Result（照片筛选结果）" / "Blurred（模糊照片）",
                    f"默认输出位置错误：{source_dest}")

        # A custom result directory inside the selected tree must also be
        # excluded from later recursive scans.
        custom_inside = root / "我的筛选结果"
        custom_inside.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 30), "white").save(custom_inside / "不应扫描_自定义.jpg")
        photo_curator.state["scan"].update({
            "output_mode": "custom",
            "custom_output": str(custom_inside),
        })
        recursive_custom = photo_curator.list_images(root, recursive=True)
        assert_true(all(custom_inside not in p.parents for p in recursive_custom),
                    f"递归扫描错误包含自定义结果目录：{recursive_custom}")
        photo_curator.state["scan"].update({
            "output_mode": "source",
            "custom_output": "",
        })

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

        # Similarity groups now allow multiple kept photos, but never zero.
        p0, p1, p2 = map(str, found[:3])
        photo_curator.state["dedup"].update({
            "complete": True,
            "running": False,
            "groups_data": [{
                "group_id": 0,
                "count": 3,
                "selected_paths": [p0],
                "folder_rel": "当前文件夹",
                "members": [
                    {"path": p0, "selected": True},
                    {"path": p1, "selected": False},
                    {"path": p2, "selected": False},
                ],
            }],
        })
        photo_curator.state["dedup"]["photos"] = photo_curator.state["dedup"]["groups_data"]

        keep_more = client.post(
            "/api/dedup-select",
            json={"group_id": 0, "path": p1},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(keep_more.status_code == 200,
                    f"相似组多选保留失败：HTTP {keep_more.status_code}")
        payload = keep_more.get_json()
        assert_true(payload.get("selected") is True,
                    f"第二张照片未加入保留：{payload}")
        assert_true(set(photo_curator.state["dedup"]["groups_data"][0]["selected_paths"]) == {p0, p1},
                    "相似组多选保留状态错误")

        unkeep_first = client.post(
            "/api/dedup-select",
            json={"group_id": 0, "path": p0},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(unkeep_first.status_code == 200,
                    f"取消其中一张保留失败：HTTP {unkeep_first.status_code}")

        refuse_zero = client.post(
            "/api/dedup-select",
            json={"group_id": 0, "path": p1},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(refuse_zero.status_code == 409,
                    "相似组不应允许取消最后一张保留照片")

        # Applying a reviewed dedup group should move only unselected members,
        # keep all selected originals, and clear resolved review cards.
        apply_dir = root / "去重处理测试"
        apply_dir.mkdir(parents=True, exist_ok=True)
        keep_a = apply_dir / "保留A.jpg"
        keep_b = apply_dir / "保留B.jpg"
        drop_c = apply_dir / "待处理C.jpg"
        for p in (keep_a, keep_b, drop_c):
            Image.new("RGB", (50, 40), "white").save(p)
        photo_curator.state["folder"] = str(root)
        photo_curator.state["scan"].update({
            "output_mode": "source",
            "custom_output": "",
        })
        photo_curator.state["dedup"].update({
            "complete": True,
            "applied": False,
            "running": False,
            "groups_data": [{
                "group_id": 7,
                "count": 3,
                "selected_paths": [str(keep_a), str(keep_b)],
                "folder_rel": "去重处理测试",
                "members": [
                    {"path": str(keep_a), "selected": True},
                    {"path": str(keep_b), "selected": True},
                    {"path": str(drop_c), "selected": False},
                ],
            }],
        })
        photo_curator.state["dedup"]["photos"] = photo_curator.state["dedup"]["groups_data"]
        applied = client.post(
            "/api/dedup-apply",
            json={},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(applied.status_code == 200,
                    f"相似照片确认处理失败：HTTP {applied.status_code}")
        payload = applied.get_json()
        assert_true(payload.get("moved") == 1 and not payload.get("photos"),
                    f"相似照片处理结果错误：{payload}")
        assert_true(keep_a.exists() and keep_b.exists() and not drop_c.exists(),
                    "相似照片多选保留后错误移动了保留项，或未移动待处理项")
        moved_c = apply_dir / "PhotoCurator_Result（照片筛选结果）" / "Duplicates（重复照片）" / drop_c.name
        assert_true(moved_c.exists(),
                    f"待处理相似照片没有进入默认结果目录：{moved_c}")

        # Custom output is an explicit user-selected root and must remain
        # accessible to thumbnails / previews after a reviewed file is moved.
        custom_root = Path(td) / "自定义筛选结果"
        custom_root.mkdir(parents=True, exist_ok=True)
        custom_img = custom_root / "已移动照片.jpg"
        Image.new("RGB", (30, 20), "white").save(custom_img)
        photo_curator.state["scan"].update({
            "output_mode": "custom",
            "custom_output": str(custom_root),
        })
        assert_true(photo_curator._safe_image_path(str(custom_img)) is not None,
                    "自定义输出目录中的已移动照片无法通过安全路径校验")

        outside = Path(td) / "目录外照片.jpg"
        Image.new("RGB", (20, 20), "white").save(outside)
        assert_true(photo_curator._safe_image_path(str(outside)) is None,
                    "安全路径校验错误地允许了所选目录外文件")

        # Incremental index: unchanged files must reuse cached analysis, while
        # a changed file version must invalidate the old cache entry.
        cache_img = root / "增量缓存测试.jpg"
        Image.new("RGB", (44, 33), "white").save(cache_img)
        photo_curator._save_cull_metrics(cache_img, 123.0, 66.0)
        cached = photo_curator._load_cull_metrics_map([cache_img])
        assert_true(str(cache_img) in cached and cached[str(cache_img)] == (123.0, 66.0),
                    f"增量清晰度缓存读取失败：{cached}")
        Image.new("RGB", (45, 33), "white").save(cache_img)
        invalidated = photo_curator._load_cull_metrics_map([cache_img])
        assert_true(str(cache_img) not in invalidated,
                    "照片内容变化后不应继续复用旧清晰度缓存")

        # Cross-stage consistency: when the currently selected duplicate keeper
        # is later marked blurry, promote the best still-kept Cull survivor.
        # Applying duplicate cleanup must not move the blurry photo as a duplicate.
        sync_dir = root / "跨阶段联动测试"
        sync_dir.mkdir(parents=True, exist_ok=True)
        blurry_a = sync_dir / "A_后改模糊.jpg"
        keep_b = sync_dir / "B_应自动保留.jpg"
        drop_c = sync_dir / "C_重复待处理.jpg"
        for p in (blurry_a, keep_b, drop_c):
            Image.new("RGB", (55, 41), "white").save(p)

        photo_curator.state["folder"] = str(root)
        photo_curator.state["cull"].update({
            "complete": True,
            "running": False,
            "src_folder": str(root),
            "recursive": True,
            "sharp_paths": [str(keep_b), str(drop_c)],
            "photos": [
                {"path": str(blurry_a), "tier": "blurry", "move_selected": True},
                {"path": str(keep_b), "tier": "sharp", "move_selected": False},
                {"path": str(drop_c), "tier": "sharp", "move_selected": False},
            ],
            "sharp": 2, "soft": 0, "blurry": 1,
        })
        photo_curator.state["dedup"].update({
            "complete": True,
            "running": False,
            "applied": False,
            "src_folder": str(root),
            "recursive": True,
            "groups_data": [{
                "group_id": 11,
                "count": 3,
                "ready": True,
                "selected_paths": [str(blurry_a)],
                "folder_rel": "跨阶段联动测试",
                "members": [
                    {"path": str(blurry_a), "selected": True},
                    {"path": str(keep_b), "selected": False},
                    {"path": str(drop_c), "selected": False},
                ],
            }],
            "singleton_paths": [],
            "seen_paths": {str(blurry_a), str(keep_b), str(drop_c)},
        })
        photo_curator.state["dedup"]["photos"] = photo_curator.state["dedup"]["groups_data"]
        photo_curator._sync_dedup_with_cull()
        selected_now = photo_curator.state["dedup"]["groups_data"][0]["selected_paths"]
        assert_true(selected_now == [str(keep_b)],
                    f"模糊化原保留项后没有自动晋升可用照片：{selected_now}")

        applied_sync = client.post(
            "/api/dedup-apply",
            json={},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(applied_sync.status_code == 200,
                    f"跨阶段相似处理失败：HTTP {applied_sync.status_code}")
        payload = applied_sync.get_json()
        assert_true(payload.get("moved") == 1 and payload.get("ok") is True,
                    f"跨阶段相似处理结果错误：{payload}")
        assert_true(blurry_a.exists() and keep_b.exists() and not drop_c.exists(),
                    "相似处理错误移动了模糊照片或当前保留项")

        # Similarity metadata keeps the full source relationship so a photo
        # rescued from Blurry later can immediately re-enter the eligible set.
        rescued = sync_dir / "D_后续救回.jpg"
        Image.new("RGB", (56, 42), "white").save(rescued)
        photo_curator.state["dedup"].update({
            "complete": True,
            "running": False,
            "src_folder": str(root),
            "recursive": True,
            "groups_data": [],
            "photos": [],
            "all_singleton_paths": [str(rescued)],
            "singleton_paths": [],
            "seen_paths": {str(rescued)},
        })
        photo_curator.state["cull"]["sharp_paths"] = []
        photo_curator._sync_dedup_with_cull()
        assert_true(str(rescued) not in photo_curator.state["dedup"]["kept_paths"],
                    "仍为模糊状态的相似索引单例不应进入保留集合")
        photo_curator.state["cull"]["sharp_paths"] = [str(rescued)]
        photo_curator._sync_dedup_with_cull()
        assert_true(str(rescued) in photo_curator.state["dedup"]["kept_paths"],
                    "人工救回照片没有从完整相似索引恢复到保留集合")

        # Duplicate groups support chunked transport so extreme libraries do not
        # silently lose groups beyond one response cap.
        photo_curator.state["dedup"]["photos"] = [
            {"group_id": 101, "count": 2, "members": []},
            {"group_id": 102, "count": 2, "members": []},
            {"group_id": 103, "count": 2, "members": []},
        ]
        chunk = client.get(
            "/api/results/dedup?offset=1&limit=1",
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(chunk.status_code == 200, f"相似组分块接口失败：HTTP {chunk.status_code}")
        cp = chunk.get_json()
        assert_true(cp.get("total") == 3 and cp.get("next_offset") == 2
                    and cp.get("photos", [{}])[0].get("group_id") == 102,
                    f"相似组分块结果错误：{cp}")

        # A completed Cull with zero survivors is a valid result. Dedup/Rank
        # must never fall back to scanning the original folder again, otherwise
        # photos the user/algorithm rejected as blurry would re-enter later stages.
        photo_curator.state["folder"] = str(root)
        photo_curator.state["cull"].update({
            "complete": True,
            "running": False,
            "src_folder": str(root),
            "recursive": True,
            "sharp_paths": [],
            "photos": [],
            "sharp": 0,
            "soft": 0,
            "blurry": len(photo_curator.list_images(root, recursive=True)),
        })
        photo_curator.run_dedup(
            str(root), threshold=0.8, ftype="all", pair="both",
            recursive=True, compare_scope="folder"
        )
        assert_true(photo_curator.state["dedup"].get("complete") is True,
                    "零保留照片时相似分析应正常完成")
        assert_true(not photo_curator.state["dedup"].get("kept_paths"),
                    "零保留照片时相似分析错误地重新引入了原照片")

        photo_curator.run_rank(str(root), ftype="all", pair="both", recursive=True)
        assert_true(photo_curator.state["rank"].get("complete") is True,
                    "零保留照片时精选评分应正常完成")
        assert_true(not photo_curator.state["rank"].get("scores"),
                    "零保留照片时精选评分错误地重新引入了原照片")

    print("基础冒烟测试通过：中文路径 / 特殊字符 / 图像解码 / 安全路径均正常")


if __name__ == "__main__":
    main()
