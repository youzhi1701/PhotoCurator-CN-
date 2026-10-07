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
import webbrowser
from pathlib import Path

APP_TITLE = "照片筛选 · PhotoCurator 中文版"
APP_VERSION = "1.5.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 5014

IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    INSTALL_ROOT = Path(sys.executable).resolve().parent.parent
    DATA_ROOT = INSTALL_ROOT / "data"
else:
    INSTALL_ROOT = Path(__file__).resolve().parent
    DATA_ROOT = Path(os.environ.get("PHOTOCURATOR_DATA_DIR", "")).expanduser() if os.environ.get("PHOTOCURATOR_DATA_DIR") else (Path.home() / ".photo_curator")
LOG_DIR = DATA_ROOT / "logs"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
os.environ.setdefault("PHOTOCURATOR_DATA_DIR", str(DATA_ROOT))


def _write_early_error_log():
    try:
        log = LOG_DIR / "startup-error.log"
        log.write_text(
            "\n".join([
                f"照片筛选 {APP_VERSION}",
                f"Python: {sys.version}",
                f"Executable: {sys.executable}",
                f"Platform: {sys.platform}",
                "",
                traceback.format_exc(),
            ]),
            encoding="utf-8",
        )
    except Exception:
        pass


try:
    from werkzeug.serving import make_server
    import webview
except Exception:
    _write_early_error_log()
    raise

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
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            hwnd = user32.FindWindowW(None, APP_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            else:
                user32.MessageBoxW(
                    None,
                    "照片筛选已经在运行，请切换到现有窗口。",
                    APP_TITLE,
                    0x40,
                )
        except Exception:
            pass
        raise SystemExit(0)
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

# Import only after PHOTOCURATOR_PORT is set; photo_curator builds its local
# security allow-list from this value at import time.
try:
    from photo_curator import app, state
except Exception:
    _write_early_error_log()
    raise

URL = f"http://{HOST}:{PORT}"


class DesktopApi:
    """Small native bridge used only by the desktop WebView."""

    def __init__(self):
        self._maximized = True
        self.allow_exit = False

    def window_action(self, action):
        window = webview.active_window()
        if window is None:
            return False
        if action == 'minimize' or action == 'close':
            # The custom close button is intentionally safe: it minimizes
            # instead of destroying the running analysis session.
            window.minimize()
            return True
        if action == 'exit':
            self.allow_exit = True
            window.destroy()
            return True
        if action == 'toggle_maximize':
            if self._maximized:
                window.restore()
                self._maximized = False
            else:
                window.maximize()
                self._maximized = True
            return True
        return False

    def pick_folder(self):
        window = webview.active_window()
        if window is None:
            return None
        enum = getattr(webview, 'FileDialog', None)
        dialog_type = getattr(enum, 'FOLDER', None) if enum else None
        if dialog_type is None:
            dialog_type = getattr(webview, 'FOLDER_DIALOG', None)
        if dialog_type is None:
            raise RuntimeError("当前 pywebview 不支持文件夹选择器")
        result = window.create_file_dialog(dialog_type)
        if not result:
            return None
        return str(result[0])


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
            "请重新启动 PhotoCurator；如仍失败，请查看安装目录 data/logs/startup-error.log。"
        )

    desktop_api = DesktopApi()
    window = webview.create_window(
        APP_TITLE,
        URL,
        js_api=desktop_api,
        width=1180,
        height=760,
        min_size=(720, 520),
        resizable=True,
        maximized=True,
        zoomable=False,
        confirm_close=False,
        text_select=True,
        background_color="#eef7ff",
        frameless=True,
        easy_drag=False,
    )

    def on_closing():
        if desktop_api.allow_exit:
            return True
        active = [k for k in ('cull', 'dedup', 'rank')
                  if state.get(k, {}).get('running')]
        if active:
            try:
                window.evaluate_js(
                    "toast('当前照片处理任务仍在运行，请先点击“停止”后再关闭窗口。','bad')"
                )
            except Exception:
                pass
            return False
        return True

    window.events.closing += on_closing

    try:
        # On Windows force Edge WebView2. Falling back to IE/MSHTML would open
        # a window but break the modern UI, which is worse than a clear error.
        if os.name == 'nt':
            webview.start(gui='edgechromium', debug=False)
        else:
            webview.start(debug=False)
    except Exception as exc:
        # Formal installer builds do not expose maintenance BAT/CMD files.
        # If WebView2 itself is unavailable, keep the already-running local
        # service alive and fall back to the user's default browser.
        try:
            opened = bool(webbrowser.open(URL, new=1))
        except Exception:
            opened = False
        if os.name == 'nt' and opened:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "PhotoCurator 桌面窗口引擎暂时无法启动。\n\n"
                    "已自动切换到默认浏览器兼容模式。照片仍只在本机处理。\n"
                    "使用完成后，再点击此提示框的“确定”即可退出本地服务。\n\n"
                    f"桌面窗口错误：{exc}",
                    APP_TITLE,
                    0x40,
                )
                return
            except Exception:
                pass
        raise RuntimeError(
            "桌面窗口引擎启动失败，并且未能自动打开浏览器兼容模式。"
            "请安装或修复 Microsoft Edge WebView2 Runtime。"
        ) from exc
    finally:
        server.stop()


def self_test():
    """Headless packaged-runtime smoke test used by release CI."""
    import json
    import raw_loader

    if not raw_loader.HAS_RAWPY:
        raise RuntimeError("packaged RAW support is unavailable")
    if not raw_loader.HAS_HEIF:
        raise RuntimeError("packaged HEIC support is unavailable")

    server = LocalServer()
    server.start()
    try:
        if not wait_until_ready(timeout=20.0):
            raise RuntimeError("self-test local server did not become ready")

        with urllib.request.urlopen(URL, timeout=3.0) as response:
            if response.status != 200:
                raise RuntimeError(f"self-test HTTP status: {response.status}")

        with urllib.request.urlopen(URL + "/vendor/maplibre-gl.css", timeout=3.0) as response:
            if response.status != 200:
                raise RuntimeError("packaged vendor resources are unavailable")

        with urllib.request.urlopen(URL + "/api/shortcuts", timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("demo_count") != 12:
                raise RuntimeError(f"packaged writable data test failed: {payload}")

        return 0
    finally:
        server.stop()


def write_error_log(exc):
    log = LOG_DIR / "startup-error.log"
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
        if "--self-test" in sys.argv:
            raise SystemExit(self_test())
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
