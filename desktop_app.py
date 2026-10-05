#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片筛选 · PhotoCurator 中文桌面版
默认启动方式：独立桌面窗口（pywebview）+ 本地 Flask 服务。
核心算法继续复用 photo_curator.py，避免二次重写造成筛选/去重/排序逻辑偏差。
"""
import sys
import time
import threading
import urllib.request
from werkzeug.serving import make_server

try:
    import webview
except ImportError:
    print("缺少 pywebview，请先运行“一键安装并启动.bat”。")
    raise

from photo_curator import app, PORT

APP_TITLE = "照片筛选 · PhotoCurator 中文版"
APP_VERSION = "1.0.0-cn.1"
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"


class LocalServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = make_server(HOST, PORT, app, threaded=True)

    def run(self):
        self.server.serve_forever()

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


def wait_until_ready(timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=0.8) as r:
                if 200 <= r.status < 500:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def main():
    server = LocalServer()
    server.start()

    if not wait_until_ready():
        server.stop()
        raise RuntimeError(
            f"本地服务启动失败。请检查端口 {PORT} 是否被占用，"
            "或使用“浏览器兼容模式.bat”查看详细错误。"
        )

    window = webview.create_window(
        APP_TITLE,
        URL,
        width=1480,
        height=920,
        min_size=(1080, 680),
        confirm_close=True,
        text_select=True,
    )

    try:
        # 不指定 GUI 引擎，让 pywebview 在 Windows 上自动选择可用的 WebView2/MSHTML 后端。
        # Windows 10/11 通常会直接使用 Edge WebView2。
        webview.start(debug=False)
    finally:
        server.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # pythonw 启动时没有控制台，错误写入日志方便排查。
        try:
            from pathlib import Path
            log = Path(__file__).with_name("启动错误.log")
            log.write_text(str(exc), encoding="utf-8")
        except Exception:
            pass
        # 调试模式/命令行启动时仍打印错误。
        if getattr(sys, "stderr", None):
            print(f"启动失败：{exc}", file=sys.stderr)
        raise
