#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片筛选 · PhotoCurator 中文桌面版启动器。"""

import os
import sys
import time
import socket
import tempfile
import threading
import traceback
import urllib.request
from pathlib import Path

from werkzeug.serving import make_server

APP_TITLE = "照片筛选 · PhotoCurator 中文版"
APP_VERSION = "1.1.0-cn.1"
HOST = "127.0.0.1"
DEFAULT_PORT = 5014

# Keep one desktop instance only. On Windows use a named mutex so we do not
# reserve an unrelated TCP port or mistake another local service for our app.
_instance_guard = None
_mutex_handle = None
if os.name == 'nt':
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _mutex_handle = kernel32.CreateMutexW(
        None, False, "Local\\PhotoCurator_CN_youzh1701"
    )
    if not _mutex_handle:
        raise OSError("无法创建应用单实例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        raise SystemExit("照片筛选已经在运行，请先切换到现有窗口。")
else:
    _instance_guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_guard.bind((HOST, 5013))
        _instance_guard.listen(1)
    except OSError:
        raise SystemExit("照片筛选已经在运行，请先切换到现有窗口。")


def choose_port():
    """Prefer 5014 for compatibility, otherwise ask Windows for a free port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOST, DEFAULT_PORT))
        return DEFAULT_PORT
    except OSError:
        probe.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])
    finally:
        try:
            probe.close()
        except Exception:
            pass


PORT = choose_port()
os.environ["PHOTOCURATOR_PORT"] = str(PORT)

try:
    import webview
except ImportError:
    raise RuntimeError("缺少 pywebview，请先运行“一键安装并启动.bat”。")

# Import only after PHOTOCURATOR_PORT is set; photo_curator builds its local
# security allow-list from this value at import time.
from photo_curator import app

URL = f"http://{HOST}:{PORT}"


class LocalServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="photocurator-local-server")
        self.server = make_server(HOST, PORT, app, threaded=True)

    def run(self):
        self.server.serve_forever()

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


def wait_until_ready(timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=0.8) as response:
                if 200 <= response.status < 500:
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
            f"本地服务启动失败（端口 {PORT}）。"
            "请运行“调试运行.bat”或查看“启动错误.log”。"
        )

    webview.create_window(
        APP_TITLE,
        URL,
        width=1180,
        height=760,
        min_size=(640, 440),
        resizable=True,
        maximized=True,
        zoomable=True,
        confirm_close=True,
        text_select=True,
    )

    try:
        # Let pywebview use the best native backend available. On current
        # Windows 10/11 systems this is normally Microsoft Edge WebView2 and
        # follows Windows DPI scaling automatically.
        webview.start(debug=False)
    finally:
        server.stop()


def write_error_log(exc):
    log = Path(__file__).with_name("启动错误.log")
    details = [
        f"照片筛选 {APP_VERSION}",
        f"Python: {sys.version}",
        f"Executable: {sys.executable}",
        f"Platform: {sys.platform}",
        f"Port: {PORT}",
        "",
        f"{type(exc).__name__}: {exc}",
        "",
        traceback.format_exc(),
    ]
    log.write_text("\n".join(details), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        try:
            write_error_log(exc)
        except Exception:
            pass
        if getattr(sys, "stderr", None):
            print(f"启动失败：{exc}", file=sys.stderr)
            traceback.print_exc()
        raise
