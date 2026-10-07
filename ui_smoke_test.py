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
        wait = WebDriverWait(driver, 40)
        driver.get(f"http://127.0.0.1:{photo_curator.PORT}/")

        wait.until(lambda d: d.execute_script(
            "return document.documentElement.dataset.uiReady || ''"
        ) == "1")
        require(driver.execute_script(
            "return document.documentElement.dataset.uiFatal || ''"
        ) != "1", "页面初始化期间出现前端致命错误")

        # Dashboard and photo gallery are separate view owners. The dashboard
        # must fill the main work area instead of becoming one CSS-grid photo cell.
        layout = driver.execute_script("""
          const main=document.querySelector('.main').getBoundingClientRect();
          const dash=document.getElementById('dashboardView').getBoundingClientRect();
          const photo=document.getElementById('photoView');
          return {
            mainWidth: main.width,
            dashboardWidth: dash.width,
            dashboardHidden: document.getElementById('dashboardView').hidden,
            photoHidden: photo.hidden,
            duplicateBrand: !!document.querySelector('.appbar .brand')
          };
        """)
        require(not layout["dashboardHidden"], "首屏 Dashboard 不应隐藏")
        require(layout["photoHidden"], "首屏照片 Grid 不应抢占 Dashboard")
        require(layout["dashboardWidth"] >= layout["mainWidth"] * 0.94,
                f"Dashboard 没有占满主工作区：{layout}")
        require(not layout["duplicateBrand"], "原生标题栏模式不应再显示第二套软件品牌标题")

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

        # Release layout contract: all auxiliary panels belong to the one left
        # control column.  Nothing may reopen as a fixed right/bottom drawer.
        driver.find_element(By.CSS_SELECTOR, ".step[data-step='dedup']").click()
        wait.until(lambda d: "相似照片" in d.find_element(By.ID, "workspaceTitle").text)

        panels = driver.execute_script("""
          const host=document.getElementById('sidebarUtilityHost');
          const ids=['inspector','taskCenter','activityPanel','toolboxPanel'];
          const result={};
          for(const id of ids){
            const el=document.getElementById(id);
            const cs=getComputedStyle(el);
            result[id]={
              parent:el.parentElement&&el.parentElement.id,
              position:cs.position,
              display:cs.display,
              width:el.getBoundingClientRect().width
            };
          }
          return result;
        """)
        for panel_id, info in panels.items():
            require(info["parent"] == "sidebarUtilityHost",
                    f"{panel_id} 没有归入左侧控制栏：{info}")
            require(info["position"] != "fixed",
                    f"{panel_id} 仍然是浮动窗口：{info}")
            require(info["display"] != "none",
                    f"{panel_id} 在左侧控制栏中不可见：{info}")

        # One global thumbnail size must materially change duplicate-card width.
        cards = driver.find_elements(By.CSS_SELECTOR, "#gallery .dedup-choice")
        if cards:
            before = driver.execute_script(
                "return document.querySelector('#gallery .dedup-choice').getBoundingClientRect().width"
            )
            driver.execute_script("applyThumbSize(360)")
            time.sleep(0.15)
            after = driver.execute_script(
                "return document.querySelector('#gallery .dedup-choice').getBoundingClientRect().width"
            )
            require(after > before + 40,
                    f"相似照片缩放没有改变真实卡片宽度：before={before}, after={after}")

        print("UI 冒烟测试通过：启动 / 分析 / 左侧控制栏 / 相似照片真实缩放均符合 v1.8.0 契约")
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
