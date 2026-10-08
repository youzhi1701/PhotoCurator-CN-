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

        # Visual regression: verify the installed UI actually uses the
        # Aurora Bubble Glass design rather than silently rendering old CSS.
        visual = driver.execute_script("""
          const sidebar=document.querySelector('.library-sidebar');
          const top=document.querySelector('.appbar');
          const tabs=document.querySelector('.workspace-tabs');
          return {
            sidebarWidth:sidebar.getBoundingClientRect().width,
            topHeight:top.getBoundingClientRect().height,
            glass:getComputedStyle(sidebar).backdropFilter,
            tabRadius:getComputedStyle(tabs).borderTopLeftRadius,
            bodyBg:getComputedStyle(document.body).backgroundImage
          };
        """)
        require(visual["topHeight"] >= 53,
                f"Aurora 顶栏高度未生效: {visual}")
        require(220 <= visual["sidebarWidth"] <= 260,
                f"Aurora 侧栏布局未生效: {visual}")
        require("blur(" in visual["glass"],
                f"Aurora 固定侧栏玻璃材质未生效: {visual}")
        require(visual["tabRadius"] == "17px",
                f"Bubble 导航圆角未生效: {visual}")
        require("gradient" in visual["bodyBg"],
                f"Aurora 背景没有加载: {visual}")

        # Manual delete selection must be opt-in and leave the photo unchanged.
        markers = driver.execute_script("""
          const base={path:'demo.jpg',name:'demo.jpg',thumb:'/fake.jpg',
                      tier:'blurry',badge:'模糊',badgeType:'bad',
                      lifecycle:'normal',move_selected:false};
          const off=cullCardHtml(base,0);
          const marked=cullCardHtml({...base,move_selected:true},0);
          const test=document.createElement('div');
          test.className='photo-card rejected';
          test.innerHTML='<img class="photo-img" src="/fake.jpg">';
          document.body.appendChild(test);
          const opacity=getComputedStyle(test).opacity;
          const imageFilter=getComputedStyle(test.querySelector('img')).filter;
          test.remove();
          return {
            off:off.includes('move-select off'),
            marked:marked.includes('move-select"'),
            icon:marked.includes('🗑'),
            opacity,imageFilter
          };
        """)
        require(markers["off"] and markers["marked"] and markers["icon"],
                f"待删除标记不是人工选择状态: {markers}")
        require(markers["opacity"] == "1" and markers["imageFilter"] == "none",
                f"模糊结果不可降低原缩略图显示质量: {markers}")

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

        # A quality model must not silently opt photos into real file movement.
        default_selected = [
            p.get("path") for p in photo_curator.state["cull"].get("photos", [])
            if p.get("tier") == "blurry" and p.get("move_selected") is True
        ]
        require(not default_selected,
                f"模糊算法不能默认选择照片待删除: {default_selected[:5]}")

        # V2 tool ownership: the source rail remains compact, tools float
        # independently and must be movable without reloading the gallery.
        driver.find_element(By.CSS_SELECTOR, ".step[data-step='dedup']").click()
        wait.until(lambda d: "相似照片" in d.find_element(By.ID, "workspaceTitle").text)
        floating = driver.execute_script("""
          const host=document.getElementById('sidebarUtilityHost');
          return ['inspector','taskCenter','activityPanel','toolboxPanel'].map(id=>{
            const p=document.getElementById(id);
            return {id,inside:host.contains(p),visible:getComputedStyle(p).display!=='none'};
          });
        """)
        require(all(not p["inside"] and not p["visible"] for p in floating),
                f"浮动工具不应永久占据图库侧栏: {floating}")
        moved = driver.execute_script("""
          document.getElementById('settingsQuick').click();
          const p=document.getElementById('inspector');
          const header=p.querySelector('.inspector-head');
          const before=p.getBoundingClientRect();
          header.setPointerCapture=()=>{};
          header.dispatchEvent(new PointerEvent('pointerdown',{
            bubbles:true,pointerId:42,button:0,
            clientX:before.left+30,clientY:before.top+15
          }));
          header.dispatchEvent(new PointerEvent('pointermove',{
            bubbles:true,pointerId:42,clientX:before.left+90,
            clientY:before.top+55
          }));
          header.dispatchEvent(new PointerEvent('pointerup',{
            bubbles:true,pointerId:42,clientX:before.left+90,
            clientY:before.top+55
          }));
          const after=p.getBoundingClientRect();
          return {open:p.classList.contains('is-open'),
                  pos:getComputedStyle(p).position,
                  deltaX:after.left-before.left,deltaY:after.top-before.top};
        """)
        require(moved["open"] and moved["pos"] == "fixed"
                and moved["deltaX"] >= 40 and moved["deltaY"] >= 20,
                f"筛选窗口不能拖动或位置不正确: {moved}")
        driver.find_element(By.ID, "inspectorClose").click()
        require(driver.execute_script(
            "return !document.getElementById('inspector').classList.contains('is-open')"
        ), "关闭设置窗口失败")
        for button, panel in (("taskToggle", "taskCenter"),
                              ("toolboxOpen", "toolboxPanel"),
                              ("logsQuick", "activityPanel")):
            driver.execute_script("document.getElementById(arguments[0]).click()", button)
            require(driver.execute_script(
                "return document.getElementById(arguments[0]).classList.contains('is-open')",
                panel), f"{panel} 没有打开")
            driver.execute_script("closeFloatingPanel(document.getElementById(arguments[0]))", panel)

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

        print("UI 冒烟测试通过：启动 / 分析 / 可移动工具窗口 / 相似照片真实缩放均符合 v1.7.8 契约")
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
