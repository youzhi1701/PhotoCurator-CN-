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

from runtime_paths import (
    legacy_frozen_data_root,
    migrate_legacy_config,
    resolve_data_root,
)

APP_TITLE = "PhotoCurator"
APP_VERSION = "1.5.4"
HOST = "127.0.0.1"
DEFAULT_PORT = 5014

IS_FROZEN = bool(getattr(sys, "frozen", False))
INSTALL_ROOT = (
    Path(sys.executable).resolve().parent.parent
    if IS_FROZEN else Path(__file__).resolve().parent
)
DATA_ROOT = resolve_data_root(frozen=IS_FROZEN)
DATA_MIGRATION_WARNING = None
if IS_FROZEN:
    # Merge every known historical catalog location. The stable per-user data
    # root remains authoritative; old install-local/source-run databases are
    # imported into it instead of switching the whole app back to an old root.
    migration_sources = [
        legacy_frozen_data_root(INSTALL_ROOT),
        Path.home() / ".photo_curator",
    ]
    migration_errors = []
    for legacy_root in migration_sources:
        try:
            migrate_legacy_config(legacy_root, DATA_ROOT)
        except Exception as exc:
            migration_errors.append(
                f"{legacy_root}: {type(exc).__name__}: {exc}"
            )
    if migration_errors:
        DATA_MIGRATION_WARNING = "\n".join(migration_errors)

LOG_DIR = DATA_ROOT / "logs"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
os.environ["PHOTOCURATOR_DATA_DIR"] = str(DATA_ROOT)
if DATA_MIGRATION_WARNING:
    try:
        (LOG_DIR / "data-migration-warning.log").write_text(
            DATA_MIGRATION_WARNING, encoding="utf-8"
        )
    except Exception:
        pass


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
    import pystray
    from PIL import Image
except Exception:
    _write_early_error_log()
    raise

# Keep one desktop instance only. On Windows use a named mutex so we do not
# reserve an unrelated TCP port or mistake another local service for our app.
_instance_guard = None
_mutex_handle = None
if os.name == 'nt':
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhotoCurator.CN")
    except Exception:
        pass
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
    from photo_curator import app, state, TASK_MANAGER
except Exception:
    _write_early_error_log()
    raise

URL = f"http://{HOST}:{PORT}"


def stop_analysis_and_wait(timeout=3.0):
    """Request every analysis worker to stop before an explicit process exit."""
    active = [
        key for key in ('cull', 'dedup', 'rank')
        if state.get(key, {}).get('running')
    ]
    for key in active:
        state[key]['cancel'] = True
    deadline = time.monotonic() + max(0.0, float(timeout))
    while active and time.monotonic() < deadline:
        if all(not state.get(key, {}).get('running') for key in active):
            break
        time.sleep(0.05)
    return all(not state.get(key, {}).get('running') for key in active)


def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", INSTALL_ROOT))
    return base.joinpath(*parts)


def load_tray_image():
    """Use the approved multi-size C icon for the Windows notification area."""
    candidates = [
        resource_path("packaging", "PhotoCurator.ico"),
        INSTALL_ROOT / "packaging" / "PhotoCurator.ico",
    ]
    for path in candidates:
        try:
            if Path(path).is_file():
                return Image.open(str(path)).convert("RGBA")
        except Exception:
            pass
    # Last-resort clean C glyph so the tray is never invisible.
    from PIL import ImageDraw
    img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    d = ImageDraw.Draw(img)
    d.arc((12, 10, 52, 54), 55, 305, fill=(255, 255, 255, 255), width=10)
    return img


class DesktopApi:
    """Minimal native bridge. Windows owns all window geometry/state."""

    def __init__(self):
        self.tray = None
        self.window = None

    def attach_tray(self, tray):
        self.tray = tray

    def attach_window(self, window):
        self.window = window

    def _window(self):
        if self.window is not None:
            return self.window
        try:
            return webview.active_window()
        except Exception:
            return None

    def _native_hwnd(self):
        if os.name != 'nt':
            return None
        window = self._window()
        if window is None:
            return None
        try:
            native = window.native
            handle = native.Handle
            try:
                return int(handle.ToInt64())
            except Exception:
                return int(handle)
        except Exception:
            return None

    def apply_native_titlebar_theme(self):
        """Theme native chrome without taking over Windows hit-testing."""
        if os.name != 'nt':
            return False
        hwnd = self._native_hwnd()
        if not hwnd:
            return False
        try:
            import ctypes

            def colorref(hex_color):
                value = str(hex_color).lstrip('#')
                r = int(value[0:2], 16)
                g = int(value[2:4], 16)
                b = int(value[4:6], 16)
                return ctypes.c_uint32(r | (g << 8) | (b << 16))

            dwm = ctypes.windll.dwmapi
            attrs = (
                (34, colorref('#2A3D68')),  # DWMWA_BORDER_COLOR
                (35, colorref('#18243C')),  # DWMWA_CAPTION_COLOR
                (36, colorref('#F8FAFF')),  # DWMWA_TEXT_COLOR
            )
            for attr, value in attrs:
                try:
                    dwm.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
                    )
                except Exception:
                    pass

            try:
                corner = ctypes.c_int(2)  # DWMWCP_ROUND
                dwm.DwmSetWindowAttribute(
                    hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner)
                )
            except Exception:
                pass

            # Windows 10 does not honor the newer per-window caption-color
            # attributes consistently, but it does support immersive dark
            # captions on current builds. This is the compatibility fallback.
            try:
                dark = ctypes.c_int(1)
                for dark_attr in (20, 19):
                    try:
                        if dwm.DwmSetWindowAttribute(
                            hwnd, dark_attr, ctypes.byref(dark), ctypes.sizeof(dark)
                        ) == 0:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            ctypes.windll.user32.SetWindowTextW(hwnd, APP_TITLE)
            return True
        except Exception:
            return False

    def pick_folder(self):
        window = self._window()
        if window is None:
            raise RuntimeError("桌面主窗口尚未就绪")
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
            f"请重新启动 PhotoCurator；如仍失败，请查看 {LOG_DIR / 'startup-error.log'}。"
        )

    desktop_api = DesktopApi()
    window = webview.create_window(
        APP_TITLE,
        URL,
        js_api=desktop_api,
        width=1280,
        height=820,
        min_size=(860, 600),
        resizable=True,
        maximized=True,
        zoomable=False,
        confirm_close=False,
        text_select=True,
        background_color="#eef7ff",
        frameless=False,
        easy_drag=False,
    )
    desktop_api.attach_window(window)

    def show_window(icon=None, item=None):
        try:
            window.show()
        except Exception:
            pass

    def tray_status_text(item=None):
        active = [k for k in ('cull', 'dedup', 'rank') if state.get(k, {}).get('running')]
        queued = 0
        try:
            queued = int(TASK_MANAGER.summary().get('active') or 0)
        except Exception:
            pass
        labels = {'cull': '模糊分析', 'dedup': '相似分析', 'rank': '照片评分'}
        if active:
            return "运行中：" + " + ".join(labels.get(k, k) for k in active) + (f" · 文件任务 {queued}" if queued else "")
        if queued:
            return f"后台文件任务：{queued} 个"
        return "后台空闲"

    def analysis_running(item=None):
        return any(state.get(k, {}).get('running') for k in ('cull', 'dedup', 'rank'))

    def stop_analysis(icon=None, item=None):
        for key in ('cull', 'dedup', 'rank'):
            if state.get(key, {}).get('running'):
                state[key]['cancel'] = True

    def exit_from_tray(icon=None, item=None):
        stop_analysis_and_wait()
        try:
            TASK_MANAGER.shutdown()
        except Exception:
            pass
        try:
            if icon is not None:
                icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    tray = None

    def start_tray_after_ui():
        nonlocal tray
        if os.name != 'nt' or tray is not None:
            return
        try:
            tray = pystray.Icon(
                "PhotoCurator",
                load_tray_image(),
                "PhotoCurator · 照片整理工作区",
                menu=pystray.Menu(
                    pystray.MenuItem("打开 PhotoCurator", show_window, default=True),
                    pystray.MenuItem(tray_status_text, None, enabled=False),
                    pystray.MenuItem("停止当前分析", stop_analysis, enabled=analysis_running),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出 PhotoCurator", exit_from_tray),
                ),
            )
            desktop_api.attach_tray(tray)
            tray.run_detached()
        except Exception:
            tray = None
            _write_early_error_log()

    def after_webview_start():
        # webview.start callbacks run after the GUI loop is live.  Only DWM
        # attributes are touched here; no pywebview lifecycle re-entry.
        if os.name == 'nt':
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if desktop_api.apply_native_titlebar_theme():
                    break
                time.sleep(0.10)
        start_tray_after_ui()

    # Native Windows title bar owns close/maximize/restore.  In particular,
    # there is intentionally NO pywebview closing/shown/loaded callback here:
    # those callbacks previously re-entered native window APIs and could lock
    # WebView2's GUI message loop.  Closing the native window now exits the
    # desktop shell normally; unfinished file tasks are persisted.

    try:
        # On Windows force Edge WebView2. Falling back to IE/MSHTML would open
        # a window but break the modern UI, which is worse than a clear error.
        if os.name == 'nt':
            webview.start(after_webview_start, gui='edgechromium', debug=False)
        else:
            webview.start(after_webview_start, debug=False)
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
        # Covers browser fallback, tray failure and native window teardown too.
        # Explicit exits already call this helper; repeating it is harmless.
        try:
            stop_analysis_and_wait()
        except Exception:
            pass
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        try:
            TASK_MANAGER.shutdown()
        except Exception:
            pass
        server.stop()


def self_test():
    """Headless packaged-runtime smoke test used by release CI."""
    import json
    import raw_loader

    if not raw_loader.HAS_RAWPY:
        raise RuntimeError("packaged RAW support is unavailable")
    if not raw_loader.HAS_HEIF:
        raise RuntimeError("packaged HEIC support is unavailable")

    if IS_FROZEN:
        try:
            if DATA_ROOT.resolve() == legacy_frozen_data_root(INSTALL_ROOT).resolve():
                raise RuntimeError("packaged writable data root is still coupled to the install directory")
        except OSError:
            pass
        icon_path = resource_path("packaging", "PhotoCurator.ico")
        if not icon_path.is_file():
            raise RuntimeError("packaged tray/taskbar icon is unavailable")
        try:
            with Image.open(str(icon_path)) as icon:
                sizes = set(icon.ico.sizes()) if getattr(icon, "ico", None) else {icon.size}
            required = {(16, 16), (20, 20), (32, 32), (48, 48), (256, 256)}
            if not required.issubset(sizes):
                raise RuntimeError(f"packaged icon is missing DPI sizes: {sorted(required - sizes)}")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"packaged icon validation failed: {exc}") from exc

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

        started = time.monotonic()
        with urllib.request.urlopen(URL + "/api/shortcuts", timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload.get("sources"), list):
                raise RuntimeError("packaged data-source catalog API is unavailable")
        if time.monotonic() - started > 2.5:
            raise RuntimeError("first-paint shortcuts API is doing blocking startup work")

        request = urllib.request.Request(
            URL + "/api/demo-prepare?wait=1",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=35.0) as response:
            demo = json.loads(response.read().decode("utf-8"))
            if not demo.get("ready") or int(demo.get("count") or 0) < 36:
                raise RuntimeError(f"packaged writable demo preparation failed: {demo}")

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
