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
APP_VERSION = "1.7.13"
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

_DIAGNOSTIC_TRACE_FILE = None
if os.environ.get("PHOTOCURATOR_DIAGNOSTICS") == "1":
    try:
        import faulthandler
        _DIAGNOSTIC_TRACE_FILE = open(
            LOG_DIR / "hang-trace.log",
            "a",
            encoding="utf-8",
            buffering=1,
        )
        _DIAGNOSTIC_TRACE_FILE.write(
            "\n===== PhotoCurator diagnostic session "
            + time.strftime("%Y-%m-%d %H:%M:%S")
            + f" · v{APP_VERSION} · pid={os.getpid()} =====\n"
        )
        faulthandler.enable(file=_DIAGNOSTIC_TRACE_FILE, all_threads=True)
        # Independent watchdog thread inside CPython. If Windows reports the
        # pywebview/Python host as unresponsive, repeated dumps preserve the
        # Python thread stacks around the freeze instead of forcing us to guess.
        faulthandler.dump_traceback_later(
            5.0,
            repeat=True,
            file=_DIAGNOSTIC_TRACE_FILE,
            exit=False,
        )
    except Exception:
        _DIAGNOSTIC_TRACE_FILE = None


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
        # Never silently raise an older running window when launching a new
        # installer build. That makes the upgraded UI look unchanged.
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.MessageBoxW(
                None,
                f"PhotoCurator v{APP_VERSION} 无法启动：另一份 PhotoCurator 仍在运行。\\n\\n"
                "请先从右下角系统托盘彻底退出旧程序，然后重新启动新版。"
                "\\n如果找不到托盘图标，请使用任务管理器结束 PhotoCurator.exe。",
                f"{APP_TITLE} · 版本切换提醒",
                0x30,
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


def _windows_work_area(point=None):
    """Return the Windows work area for a screen point.

    The desktop runtime stays on the proven pywebview lifecycle: this uses only
    monitor geometry APIs and never reaches into pywebview native/DWM handles.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        if point is None:
            probe = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(probe)):
                return None
        else:
            probe = wintypes.POINT(int(point[0]), int(point[1]))
        monitor = user32.MonitorFromPoint(probe, 2)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return (
            int(work.left), int(work.top),
            max(1, int(work.right - work.left)),
            max(1, int(work.bottom - work.top)),
        )
    except Exception:
        return None


def _window_bounds(window):
    try:
        return (
            int(window.x), int(window.y),
            max(1, int(window.width)), max(1, int(window.height)),
        )
    except Exception:
        return None


def _work_area_for_window(window):
    bounds = _window_bounds(window)
    if bounds:
        x, y, width, height = bounds
        return _windows_work_area((x + width // 2, y + height // 2))
    return _windows_work_area()


def _fit_window_to_work_area(window):
    area = _work_area_for_window(window)
    if not area:
        window.maximize()
        return True
    left, top, width, height = area
    window.move(left, top)
    window.resize(width, height)
    return True


def _restore_window_bounds(window, bounds=None):
    if not bounds:
        area = _work_area_for_window(window) or _windows_work_area()
        if not area:
            window.restore()
            return True
        left, top, work_width, work_height = area
        width, height = min(1180, work_width), min(760, work_height)
        x = left + max(0, (work_width - width) // 2)
        y = top + max(0, (work_height - height) // 2)
    else:
        x, y, width, height = [int(v) for v in bounds]
        area = _windows_work_area((x + width // 2, y + height // 2))
        if area:
            left, top, work_width, work_height = area
            width = min(max(720, width), work_width)
            height = min(max(520, height), work_height)
            x = min(max(x, left), left + max(0, work_width - width))
            y = min(max(y, top), top + max(0, work_height - height))
    window.resize(width, height)
    window.move(x, y)
    return True


class DesktopApi:
    """Small native bridge used only by the desktop WebView."""

    def __init__(self):
        self._maximized = True
        self._normal_bounds = None
        self.allow_exit = False
        self.tray = None

    def attach_tray(self, tray):
        self.tray = tray

    def window_action(self, action):
        window = webview.active_window()
        if window is None:
            return False
        if action == 'minimize':
            window.minimize()
            return True
        if action == 'close':
            # Close-to-tray keeps background analysis and queued file work alive.
            try:
                window.hide()
            except Exception:
                window.minimize()
            return True
        if action == 'exit':
            self.allow_exit = True
            stop_analysis_and_wait()
            try:
                TASK_MANAGER.shutdown()
            except Exception:
                pass
            try:
                if self.tray is not None:
                    self.tray.stop()
            except Exception:
                pass
            window.destroy()
            return True
        if action == 'toggle_maximize':
            if self._maximized:
                _restore_window_bounds(window, self._normal_bounds)
                self._maximized = False
            else:
                self._normal_bounds = _window_bounds(window) or self._normal_bounds
                _fit_window_to_work_area(window)
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
            f"请重新启动 PhotoCurator；如仍失败，请查看 {LOG_DIR / 'startup-error.log'}。"
        )

    desktop_api = DesktopApi()
    work_area = _windows_work_area() if os.name == 'nt' else None
    create_width = work_area[2] if work_area else 1180
    create_height = work_area[3] if work_area else 760
    create_x = work_area[0] if work_area else None
    create_y = work_area[1] if work_area else None
    window = webview.create_window(
        APP_TITLE,
        URL,
        js_api=desktop_api,
        width=create_width,
        height=create_height,
        x=create_x,
        y=create_y,
        min_size=(720, 520),
        resizable=True,
        maximized=False,
        zoomable=False,
        confirm_close=False,
        text_select=True,
        background_color="#eef7ff",
        frameless=True,
        easy_drag=False,
    )

    def show_window(icon=None, item=None):
        try:
            window.show()
            if desktop_api._maximized:
                _fit_window_to_work_area(window)
            else:
                _restore_window_bounds(window, desktop_api._normal_bounds)
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
        desktop_api.allow_exit = True
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
    if os.name == 'nt':
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

    def on_closing():
        if desktop_api.allow_exit:
            return True
        # Native close / Alt+F4 follows the same close-to-tray rule as the
        # custom title-bar button. Background tasks are never stopped here.
        if tray is not None:
            try:
                window.hide()
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
        # The rank screen offers actual local human-face evidence. OpenCV's
        # XML cascade is a package data asset, not a Python import: fail the
        # candidate self-test instead of silently downgrading every photo to
        # "未识别" when PyInstaller forgot to bundle it.
        import scene_labels
        if scene_labels._face_detector() is None:
            raise RuntimeError("packaged OpenCV face-cascade data unavailable")
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
