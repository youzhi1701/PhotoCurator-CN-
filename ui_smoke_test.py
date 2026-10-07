#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows UI smoke test: real browser + real local API + real core analysis."""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Keep smoke-test state isolated from the runner profile.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_tmp = tempfile.TemporaryDirectory(
    prefix="photocurator_ui_smoke_", ignore_cleanup_errors=True
)
os.environ["PHOTOCURATOR_DATA_DIR"] = str(Path(_tmp.name) / "data")
os.environ.setdefault("PHOTOCURATOR_PORT", "5014")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

import photo_curator


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    server = make_server(
        photo_curator.SERVER_HOST,
        photo_curator.PORT,
        photo_curator.app,
        threaded=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-default-apps")

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 20)
        driver.get(f"http://127.0.0.1:{photo_curator.PORT}/")

        wait.until(lambda d: d.execute_script(
            "return document.documentElement.dataset.uiReady || ''"
        ) == "1")
        require(driver.execute_script(
            "return document.documentElement.dataset.uiFatal || ''"
        ) != "1", "页面初始化期间出现前端致命错误")

        start = wait.until(lambda d: d.find_element(By.ID, "startBtn"))
        require(start.get_attribute("disabled") is not None,
                "未选择文件夹时“开始分析”必须禁用")

        demo = wait.until(
            lambda d: d.find_element(By.CSS_SELECTOR, "#shortcuts .shortcut[data-p]")
        )
        demo_path = demo.get_attribute("data-p")
        require(demo_path, "内置测试数据快捷入口没有路径")
        demo.click()

        wait.until(lambda d: d.find_element(By.ID, "folderInput").get_attribute("value"))
        folder_value = driver.find_element(By.ID, "folderInput").get_attribute("value")
        require(os.path.normcase(os.path.realpath(folder_value))
                == os.path.normcase(os.path.realpath(demo_path)),
                f"点击内置测试数据后路径未写入：{folder_value!r} != {demo_path!r}")

        wait.until(lambda d: d.find_element(By.ID, "startBtn").is_enabled())
        start = driver.find_element(By.ID, "startBtn")
        start.click()

        # The progress panel becomes visible synchronously before the two HTTP
        # start requests necessarily reach Flask. Prove both backend steps were
        # actually admitted before waiting for completion; otherwise a fast CI
        # machine can observe two idle states and report a false failure/pass.
        started_deadline = time.time() + 12
        while time.time() < started_deadline:
            cull = photo_curator.state["cull"]
            dedup = photo_curator.state["dedup"]
            cull_seen = bool(cull.get("running") or cull.get("complete")
                             or cull.get("src_folder") == demo_path)
            dedup_seen = bool(dedup.get("running") or dedup.get("complete")
                              or dedup.get("src_folder") == demo_path)
            if cull_seen and dedup_seen:
                break
            time.sleep(0.05)
        require(cull_seen, "UI 点击开始后 Cull 没有真正进入后台")
        require(dedup_seen, "UI 点击开始后 Dedup 没有真正进入后台")

        deadline = time.time() + 45
        while time.time() < deadline:
            cull = photo_curator.state["cull"]
            dedup = photo_curator.state["dedup"]
            if (not cull.get("running") and not dedup.get("running")
                    and (cull.get("complete") or "发生错误" in str(cull.get("status") or ""))
                    and (dedup.get("complete") or "发生错误" in str(dedup.get("status") or ""))):
                break
            time.sleep(0.1)

        require(not photo_curator.state["cull"].get("running"),
                "UI 启动后的真实 Cull 超时")
        require(not photo_curator.state["dedup"].get("running"),
                "UI 启动后的真实 Dedup 超时")
        require(photo_curator.state["cull"].get("complete") is True,
                f"UI Cull 未完成：{photo_curator.state['cull'].get('status')}")
        require(photo_curator.state["dedup"].get("complete") is True,
                f"UI Dedup 未完成：{photo_curator.state['dedup'].get('status')}")
        require("发生错误" not in str(photo_curator.state["cull"].get("status") or ""),
                f"UI Cull 错误：{photo_curator.state['cull'].get('status')}")
        require("发生错误" not in str(photo_curator.state["dedup"].get("status") or ""),
                f"UI Dedup 错误：{photo_curator.state['dedup'].get('status')}")

        # Navigation and secondary panels must still be clickable after analysis.
        driver.find_element(By.CSS_SELECTOR, ".step[data-step='dedup']").click()
        wait.until(lambda d: "相似照片" in d.find_element(By.ID, "workspaceTitle").text)

        driver.find_element(By.ID, "toolboxOpen").click()
        wait.until(lambda d: "open" in d.find_element(By.ID, "toolboxPanel").get_attribute("class"))

        driver.find_element(By.ID, "taskToggle").click()
        wait.until(lambda d: "open" in d.find_element(By.ID, "taskCenter").get_attribute("class"))

        print("UI 冒烟测试通过：启动 / 选择测试数据 / 开始分析 / 导航 / 工具箱 / 任务中心均可交互")
    finally:
        try:
            if driver is not None:
                driver.quit()
        finally:
            try:
                server.shutdown()
                server.server_close()
            finally:
                try:
                    photo_curator.TASK_MANAGER.shutdown()
                except Exception:
                    pass
                _tmp.cleanup()


if __name__ == "__main__":
    main()
