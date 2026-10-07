#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片筛选基础冒烟测试：中文路径、特殊字符、图像读取和安全路径。"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from raw_loader import imread_bgr, imread_gray, open_image_pil


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def wait_task(photo_curator, task_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = photo_curator.TASK_MANAGER.get(task_id)
        if row and row.get("state") == "done":
            return row
        if row and row.get("state") == "failed":
            raise AssertionError(f"后台任务失败：{row.get('error')}")
        time.sleep(0.05)
    raise AssertionError(f"后台任务超时：{task_id}")


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

        # v1.5 Cull + Dedup must share one source enumeration snapshot rather
        # than recursively calling the snapshot helper or walking the HDD twice.
        photo_curator._SCAN_SNAPSHOTS.clear()
        shared_first = photo_curator._shared_list_images(root, recursive=True, max_age=60.0)
        shared_second = photo_curator._shared_list_images(root, recursive=True, max_age=60.0)
        assert_true(shared_first == shared_second,
                    "共享扫描快照前后结果不一致")
        assert_true(nested_img in shared_first,
                    "共享扫描快照没有包含递归子目录照片")

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

        # Shared snapshots are configuration-sensitive. Switching to a custom
        # output exclusion must not reuse the earlier source-mode snapshot.
        shared_custom = photo_curator._shared_list_images(
            root, recursive=True, max_age=60.0
        )
        assert_true(all(custom_inside not in p.parents for p in shared_custom),
                    f"共享扫描快照错误复用了未排除自定义目录的旧结果：{shared_custom}")

        # The snapshot cache may legitimately reuse the earlier source-mode
        # entry until max_age expires. The regression boundary is key identity:
        # source and custom configurations must coexist as distinct entries.
        snapshot_keys = list(photo_curator._SCAN_SNAPSHOTS)
        custom_real = os.path.normcase(os.path.realpath(str(custom_inside)))
        assert_true(
            any(len(k) >= 4 and k[2] == "source" and k[3] == ""
                for k in snapshot_keys),
            f"共享扫描缓存缺少 source 配置身份：{snapshot_keys}",
        )
        assert_true(
            any(len(k) >= 4 and k[2] == "custom" and k[3] == custom_real
                for k in snapshot_keys),
            f"共享扫描缓存缺少 custom 配置身份：{snapshot_keys}",
        )

        photo_curator.state["scan"].update({
            "output_mode": "source",
            "custom_output": "",
        })
        shared_source_again = photo_curator._shared_list_images(
            root, recursive=True, max_age=60.0
        )
        assert_true(shared_source_again == shared_first,
                    "切回 source 配置后没有复用原 source 快照")

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

        # Analysis admission is atomic at the HTTP boundary: Cull + Dedup are
        # the two allowed parallel core engines, duplicate starts are rejected,
        # Rank stays exclusive, and malformed input never reserves a run slot.
        original_run_cull = photo_curator.run_cull
        original_run_dedup = photo_curator.run_dedup
        release_core = threading.Event()
        entered_cull = threading.Event()
        entered_dedup = threading.Event()

        def _blocking_cull(folder, strictness, adaptive, rescue_on, recursive=True):
            entered_cull.set()
            release_core.wait(timeout=5)
            photo_curator.state["cull"]["running"] = False

        def _blocking_dedup(folder, threshold, ftype="all", pair="both",
                            recursive=True, compare_scope="folder"):
            entered_dedup.set()
            release_core.wait(timeout=5)
            photo_curator.state["dedup"]["running"] = False

        photo_curator.run_cull = _blocking_cull
        photo_curator.run_dedup = _blocking_dedup
        try:
            bad_start = client.post(
                "/api/run/cull",
                json={"folder": str(root), "opt": "not-a-number"},
                headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
            )
            assert_true(bad_start.status_code == 400
                        and not photo_curator.state["cull"].get("running"),
                        "无效分析参数不应预占运行状态")

            cull_start = client.post(
                "/api/run/cull",
                json={"folder": str(root), "opt": 1.0, "recursive": True},
                headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
            )
            assert_true(cull_start.status_code == 200 and entered_cull.wait(2),
                        f"Cull 启动门禁异常：{cull_start.get_json()}")

            duplicate_cull = client.post(
                "/api/run/cull",
                json={"folder": str(root), "opt": 1.0, "recursive": True},
                headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
            )
            assert_true(duplicate_cull.status_code == 409,
                        "同一个 Cull 任务运行中不应允许重复启动")

            dedup_start = client.post(
                "/api/run/dedup",
                json={"folder": str(root), "opt": 0.8, "recursive": True},
                headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
            )
            assert_true(dedup_start.status_code == 200 and entered_dedup.wait(2),
                        f"Dedup 不应被正在运行的 Cull 阻塞：{dedup_start.get_json()}")

            rank_during_core = client.post(
                "/api/run/rank",
                json={"folder": str(root), "recursive": True},
                headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
            )
            assert_true(rank_during_core.status_code == 409,
                        "核心分析运行时 Rank 必须保持互斥")
        finally:
            release_core.set()
            deadline = time.time() + 3
            while time.time() < deadline and (
                    photo_curator.state["cull"].get("running")
                    or photo_curator.state["dedup"].get("running")):
                time.sleep(0.02)
            photo_curator.run_cull = original_run_cull
            photo_curator.run_dedup = original_run_dedup
            photo_curator.state["cull"]["running"] = False
            photo_curator.state["dedup"]["running"] = False

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

        # A reviewed dedup group now queues only non-kept members into the
        # PhotoCurator software recycle bin; the HTTP response is intentionally
        # immediate so the foreground can continue reviewing.
        apply_dir = root / "去重处理测试"
        apply_dir.mkdir(parents=True, exist_ok=True)
        keep_a = apply_dir / "保留A.jpg"
        keep_b = apply_dir / "保留B.jpg"
        drop_c = apply_dir / "待处理C.jpg"
        for p in (keep_a, keep_b, drop_c):
            Image.new("RGB", (50, 40), "white").save(p)
        photo_curator.state["folder"] = str(root)
        photo_curator.state["dedup"].update({
            "complete": True,
            "applied": False,
            "running": False,
            "groups_data": [{
                "group_id": 7,
                "group_key": "smoke-group-7",
                "count": 3,
                "status": "reviewed",
                "selected_paths": [str(keep_a), str(keep_b)],
                "folder_rel": "去重处理测试",
                "members": [
                    {"path": str(keep_a), "selected": True, "lifecycle": "normal"},
                    {"path": str(keep_b), "selected": True, "lifecycle": "normal"},
                    {"path": str(drop_c), "selected": False, "lifecycle": "normal"},
                ],
            }],
        })
        photo_curator.state["dedup"]["photos"] = photo_curator.state["dedup"]["groups_data"]
        applied = client.post(
            "/api/dedup-apply", json={},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(applied.status_code == 202,
                    f"相似照片后台提交失败：HTTP {applied.status_code}")
        payload = applied.get_json()
        assert_true(payload.get("queued") == 1 and payload.get("task_ids"),
                    f"相似照片后台任务数量错误：{payload}")
        for task_id in payload["task_ids"]:
            wait_task(photo_curator, task_id)
        assert_true(keep_a.exists() and keep_b.exists() and not drop_c.exists(),
                    "相似照片后台处理错误移动了保留项，或未处理待删除项")
        trash_rows = photo_curator._trash_rows(root)
        drop_c_norm = os.path.normcase(os.path.realpath(str(drop_c)))
        assert_true(any(
            os.path.normcase(os.path.realpath(str(x["original_path"]))) == drop_c_norm
            for x in trash_rows
        ), f"待删除相似照片没有进入软件回收站：{trash_rows}")

        # Multi-photo groups are not implicitly complete merely because the
        # user removed one item. "完成本组" explicitly accepts all active members
        # and persists the reviewed group state.
        complete_dir = root / "完成组选优测试"
        complete_dir.mkdir(parents=True, exist_ok=True)
        cg1, cg2, cg3 = [complete_dir / n for n in ("1.jpg", "2.jpg", "3.jpg")]
        for p in (cg1, cg2, cg3):
            Image.new("RGB", (44, 34), "white").save(p)
        group_key = "smoke-complete-group"
        photo_curator.state["dedup"]["groups_data"] = [{
            "group_id": 91, "group_key": group_key, "count": 3,
            "status": "pending", "selected_paths": [str(cg1)],
            "members": [
                {"path": str(cg1), "original_path": str(cg1), "selected": True, "lifecycle": "normal"},
                {"path": str(cg2), "original_path": str(cg2), "selected": False, "lifecycle": "normal"},
                {"path": str(cg3), "original_path": str(cg3), "selected": False, "lifecycle": "normal"},
            ],
        }]
        photo_curator.state["dedup"]["photos"] = photo_curator.state["dedup"]["groups_data"]
        photo_curator._similarity_group_state(group_key, root, [cg1, cg2, cg3])
        completed = client.post(
            "/api/dedup-complete", json={"group_id": 91},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(completed.status_code == 200, f"完成相似组失败：{completed.get_json()}")
        completed_payload = completed.get_json()
        assert_true(completed_payload["status"] == "reviewed"
                    and completed_payload["kept"] == 3,
                    f"完成相似组结果不正确：{completed_payload}")
        with sqlite3.connect(str(photo_curator.INDEX_DB)) as db:
            persisted_status = db.execute(
                "SELECT status FROM similarity_group_state WHERE group_key=?",
                (group_key,)
            ).fetchone()
            persisted_members = db.execute(
                "SELECT COUNT(*),SUM(selected) FROM similarity_group_member WHERE group_key=?",
                (group_key,)
            ).fetchone()
        assert_true(persisted_status and persisted_status[0] == "reviewed",
                    f"完成相似组状态没有持久化：{persisted_status}")
        assert_true(persisted_members == (3, 3),
                    f"完成相似组成员选择没有持久化：{persisted_members}")

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

        # PhotoCurator uses its own recycle bin, not the Windows recycle bin.
        # All destructive operations are asynchronous and expose task state.
        photo_curator.state["folder"] = str(root)

        # Fault injection: simulate a hard stop after the file rename but before
        # the background task committed its metadata. The persisted destination
        # in payload_json must make both move and restore operations recoverable.
        interrupted_src = root / "中断恢复事务测试.jpg"
        Image.new("RGB", (51, 37), "white").save(interrupted_src)
        planned_trash = photo_curator._trash_destination(
            interrupted_src, root
        ).resolve()
        photo_curator._media_state_set(
            interrupted_src, interrupted_src, "pending_trash", "cull"
        )
        interrupted_src.rename(planned_trash)
        recovered_move = photo_curator._background_move_to_trash({
            "path": str(interrupted_src),
            "folder": str(root),
            "step": "cull",
            "trash_path": str(planned_trash),
            "previous_lifecycle": "normal",
        })
        assert_true(recovered_move.get("recovered") is True,
                    f"中断后的回收站移动没有被任务恢复：{recovered_move}")
        interrupted_trash_id = recovered_move.get("trash_id")
        assert_true(interrupted_trash_id and planned_trash.is_file(),
                    "中断恢复后回收站文件或记录缺失")

        photo_curator._media_state_set(
            interrupted_src, planned_trash, "pending_restore", "cull"
        )
        planned_trash.rename(interrupted_src)
        recovered_restore = photo_curator._background_restore_trash({
            "trash_id": interrupted_trash_id,
            "original_path": str(interrupted_src),
            "trash_path": str(planned_trash),
            "restore_path": str(interrupted_src),
            "source_step": "cull",
        })
        assert_true(recovered_restore.get("restored_path") == str(interrupted_src.resolve()),
                    f"中断后的恢复操作没有完成生命周期提交：{recovered_restore}")
        interrupted_state = photo_curator._media_state_get(interrupted_src)
        assert_true(interrupted_src.is_file()
                    and interrupted_state
                    and interrupted_state.get("state") == "normal",
                    f"中断恢复后的媒体状态错误：{interrupted_state}")
        with sqlite3.connect(str(photo_curator.INDEX_DB)) as db:
            stale_interrupted = db.execute(
                "SELECT id FROM software_trash WHERE id=?",
                (interrupted_trash_id,)
            ).fetchone()
        assert_true(stale_interrupted is None,
                    "中断恢复完成后仍残留软件回收站记录")

        trash_src = root / "软件回收站复核测试.jpg"
        Image.new("RGB", (52, 38), "white").save(trash_src)
        photo_curator.state["cull"].update({
            "running": False, "complete": True, "src_folder": str(root),
            "recursive": True,
            "photos": [{"path": str(trash_src), "tier": "sharp", "move_selected": False,
                        "lifecycle": "normal"}],
            "sharp_paths": [str(trash_src)], "sharp": 1, "soft": 0, "blurry": 0,
        })
        deleted_to_trash = client.post(
            "/api/delete-photo",
            json={"step": "cull", "path": str(trash_src), "mode": "trash"},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(deleted_to_trash.status_code == 202,
                    f"移入软件回收站未异步受理：HTTP {deleted_to_trash.status_code}")
        delete_payload = deleted_to_trash.get_json()
        task = wait_task(photo_curator, delete_payload["task_id"])
        trash_id = (task.get("result") or {}).get("trash_id")
        assert_true(trash_id and not trash_src.exists(),
                    f"源照片没有进入软件回收站：{task}")
        trash_rows = photo_curator._trash_rows(root)
        trash_row = next((x for x in trash_rows if x["id"] == trash_id), None)
        assert_true(trash_row is not None and Path(trash_row["path"]).is_file(),
                    f"软件回收站记录或文件缺失：{trash_rows}")
        assert_true(photo_curator.SOFTWARE_TRASH_DIR in trash_row["path"],
                    f"软件回收站目录错误：{trash_row}")
        assert_true(Path(trash_row["path"]) not in photo_curator.list_images(root, recursive=True),
                    "软件回收站中的照片不应重新进入递归扫描")

        manifest = root / photo_curator.SOFTWARE_TRASH_DIR / photo_curator.TRASH_MANIFEST_NAME
        assert_true(manifest.is_file(), "软件回收站没有生成图库内恢复清单")
        # Simulate reinstall/config loss: remove only the central trash row,
        # then confirm _trash_rows() re-imports it from the library sidecar.
        with sqlite3.connect(str(photo_curator.INDEX_DB)) as db:
            db.execute("DELETE FROM software_trash WHERE id=?", (trash_id,))
            db.commit()
        recovered_rows = photo_curator._trash_rows(root)
        recovered = next((x for x in recovered_rows
                          if os.path.normcase(os.path.realpath(str(x["original_path"])))
                          == os.path.normcase(os.path.realpath(str(trash_src)))), None)
        assert_true(recovered is not None,
                    "中央回收站记录丢失后没有从图库恢复清单重新导入")
        trash_id = recovered["id"]

        trash_list = client.get(
            "/api/trash",
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(trash_list.status_code == 200
                    and any(x["id"] == trash_id for x in trash_list.get_json().get("photos", [])),
                    "软件回收站复核接口没有返回已删除照片")

        restored = client.post(
            "/api/trash-restore", json={"id": trash_id},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(restored.status_code == 202,
                    f"软件回收站恢复未异步受理：{restored.get_json()}")
        wait_task(photo_curator, restored.get_json()["task_id"])
        assert_true(trash_src.exists(), "软件回收站恢复后原路径不存在")
        assert_true(all(x["id"] != trash_id for x in photo_curator._trash_rows(root)),
                    "恢复后软件回收站记录没有清除")

        purge_src = root / "软件回收站永久删除测试.jpg"
        Image.new("RGB", (53, 39), "white").save(purge_src)
        photo_curator.state["cull"].update({
            "photos": [{"path": str(purge_src), "tier": "sharp",
                        "move_selected": False, "lifecycle": "normal"}],
            "sharp_paths": [str(purge_src)], "sharp": 1, "soft": 0, "blurry": 0,
        })
        deleted_again = client.post(
            "/api/delete-photo",
            json={"step": "cull", "path": str(purge_src), "mode": "trash"},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        delete_task = wait_task(photo_curator, deleted_again.get_json()["task_id"])
        purge_id = delete_task["result"]["trash_id"]
        purge_row = next(x for x in photo_curator._trash_rows(root) if x["id"] == purge_id)
        purged = client.post(
            "/api/trash-purge", json={"id": purge_id},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(purged.status_code == 202,
                    f"软件回收站永久删除未异步受理：{purged.get_json()}")
        wait_task(photo_curator, purged.get_json()["task_id"])
        assert_true(not Path(purge_row["path"]).exists() and not purge_src.exists(),
                    "软件回收站永久删除后文件仍存在")

        # Direct permanent deletion is a distinct queued mode used by the P
        # shortcut in the confirmation dialog.
        direct_src = root / "直接彻底删除测试.jpg"
        Image.new("RGB", (40, 30), "white").save(direct_src)
        photo_curator.state["cull"]["photos"] = [
            {"path": str(direct_src), "tier": "sharp", "lifecycle": "normal"}
        ]
        permanent = client.post(
            "/api/delete-photo",
            json={"step": "cull", "path": str(direct_src), "mode": "permanent"},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(permanent.status_code == 202, "直接彻底删除没有进入后台队列")
        wait_task(photo_curator, permanent.get_json()["task_id"])
        assert_true(not direct_src.exists(), "后台永久删除任务完成后文件仍存在")

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

        # Persistent similarity signatures must use nanosecond-resolution file
        # fingerprints too. A same-size file whose mtime changes within one
        # second must not reuse the previous perceptual hash.
        dedup_cache_img = root / "相似缓存精度测试.jpg"
        Image.new("RGB", (48, 36), "white").save(dedup_cache_img)
        key_before = photo_curator.FastBatchDeduplicator._cache_key(str(dedup_cache_img))
        st = dedup_cache_img.stat()
        os.utime(dedup_cache_img, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        key_after = photo_curator.FastBatchDeduplicator._cache_key(str(dedup_cache_img))
        assert_true(key_before != key_after,
                    "相似特征缓存必须使用纳秒级修改时间，不能复用同秒旧特征")

        thumb_before = photo_curator._thumb_cache_path(str(dedup_cache_img))
        st2 = dedup_cache_img.stat()
        os.utime(dedup_cache_img, ns=(st2.st_atime_ns, st2.st_mtime_ns + 1_000_000))
        thumb_after = photo_curator._thumb_cache_path(str(dedup_cache_img))
        assert_true(thumb_before != thumb_after,
                    "缩略图缓存必须随同秒文件变化失效，不能继续显示旧图")

        # Cross-stage consistency: when a former keeper is no longer eligible,
        # the reviewed duplicate batch must queue only active non-kept members
        # and leave the user's retained photo untouched.
        sync_dir = root / "跨阶段联动测试"
        sync_dir.mkdir(parents=True, exist_ok=True)
        blurry_a = sync_dir / "A_后改模糊.jpg"
        keep_b = sync_dir / "B_应自动保留.jpg"
        drop_c = sync_dir / "C_重复待处理.jpg"
        for p in (blurry_a, keep_b, drop_c):
            Image.new("RGB", (55, 41), "white").save(p)

        photo_curator.state["folder"] = str(root)
        photo_curator.state["cull"].update({
            "complete": True, "running": False, "src_folder": str(root),
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
            "complete": True, "running": False, "applied": False,
            "src_folder": str(root), "recursive": True,
            "groups_data": [{
                "group_id": 11, "group_key": "smoke-group-11",
                "count": 3, "ready": True, "status": "reviewed",
                "selected_paths": [str(blurry_a)],
                "folder_rel": "跨阶段联动测试",
                "members": [
                    {"path": str(blurry_a), "selected": True, "lifecycle": "normal"},
                    {"path": str(keep_b), "selected": False, "lifecycle": "normal"},
                    {"path": str(drop_c), "selected": False, "lifecycle": "normal"},
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
            "/api/dedup-apply", json={},
            headers={"Host": f"127.0.0.1:{photo_curator.PORT}"},
        )
        assert_true(applied_sync.status_code == 202,
                    f"跨阶段相似处理未异步受理：HTTP {applied_sync.status_code}")
        payload = applied_sync.get_json()
        assert_true(payload.get("queued") == 1,
                    f"跨阶段相似处理任务数错误：{payload}")
        for task_id in payload.get("task_ids", []):
            wait_task(photo_curator, task_id)
        assert_true(blurry_a.exists() and keep_b.exists() and not drop_c.exists(),
                    "相似处理错误删除了模糊照片或当前保留项")

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
