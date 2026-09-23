"""程序入口：托盘常驻 + 定时任务 + 桌面窗口.

用法：
  python -m arxiver              正常打开窗口
  python -m arxiver --minimized  后台启动（开机自启模式，仅托盘+定时任务）
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

from .config import get_config
from .core import autostart as autostart_mod
from .core.errors import log, setup_global_handlers, setup_logging
from .core.library import Library
from .core.scheduler import get_scheduler

_WINDOW_TITLE = "Arxiver — 顶会论文助手"
_mutex_handle = None  # 保持 mutex 存活


def _ensure_single_instance() -> bool:
    """单实例：若已有实例运行，激活其窗口后让本进程退出，返回 False."""
    global _mutex_handle
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        _mutex_handle = k32.CreateMutexW(None, False, "Arxiver_SingleInstance_Mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            hwnd = u32.FindWindowW(None, _WINDOW_TITLE)
            if hwnd:
                u32.ShowWindow(hwnd, 9)  # SW_RESTORE
                u32.SetForegroundWindow(hwnd)
            return False
        return True
    except Exception:
        return True


_hwnd_cache: dict[str, int] = {"value": 0}


def _find_own_hwnd() -> int:
    """查找本进程主窗口句柄（只在窗口还是顶层窗口时才能按标题找到）."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        h = int(u32.FindWindowW(None, _WINDOW_TITLE))
        if h:
            return h
        # 兜底：标题可能被运行时微调，枚举顶层窗口模糊匹配
        found: list[int] = []
        _PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):  # noqa: ANN001
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if "Arxiver" in buf.value:
                    found.append(int(hwnd))
            return True

        u32.EnumWindows(_PROC(_cb), 0)
        return found[0] if found else 0
    except Exception:
        return 0


_APP_USER_MODEL_ID = "Arxiver.Research.Assistant"


def _set_app_user_model_id() -> None:
    """设置进程 AppUserModelID（必须在创建任何窗口之前调用）.

    Windows 7+ 任务栏按钮图标由 AppUserModelID 决定：未显式设置时，任务栏用
    进程 exe 图标（源码模式是 pythonw.exe → Python 图标），会覆盖窗口级
    WM_SETICON。设置 AppID 后再通过 IPropertyStore 关联 Arxiver 图标即可纠正。
    """
    try:
        import ctypes
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except Exception as e:
        log.warning("设置 AppUserModelID 失败: %s", e)


def _set_taskbar_icon(hwnd: int, ico: Path) -> None:
    """用 IPropertyStore 设置窗口的 RelaunchIconResource，纠正任务栏图标."""
    try:
        import ctypes
        from ctypes import (wintypes, POINTER, Structure, c_void_p, c_ushort,
                            c_wchar_p, c_ubyte, byref, cast)

        class _GUID(Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", c_ubyte * 8)]

        class _PROPERTYKEY(Structure):
            _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]

        class _PROPVARIANT(Structure):
            _fields_ = [("vt", c_ushort), ("w1", c_ushort), ("w2", c_ushort),
                        ("w3", c_ushort), ("pwszVal", c_wchar_p)]

        _IID_IPropertyStore = _GUID(
            0x886D8EEB, 0x8CF2, 0x4446,
            (c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
        # PKEY_AppUserModel_RelaunchIconResource {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid=3
        _PKEY_Icon = _PROPERTYKEY(
            _GUID(0x9F4C2855, 0x9F79, 0x4B39,
                  (c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3)), 3)

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        pps = c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(hwnd, byref(_IID_IPropertyStore), byref(pps))
        if hr < 0 or not pps.value:
            return
        vtbl = cast(pps, POINTER(POINTER(c_void_p))).contents
        pv = _PROPVARIANT()
        pv.vt = 31  # VT_LPWSTR
        pv.pwszVal = str(ico)
        _SetValue = ctypes.WINFUNCTYPE(
            ctypes.c_long, c_void_p, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))(vtbl[6])
        _Commit = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p)(vtbl[7])
        _Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])
        _SetValue(pps, byref(_PKEY_Icon), byref(pv))
        _Commit(pps)
        _Release(pps)
        log.info("已设置任务栏图标(AppID RelaunchIconResource) hwnd=%d", hwnd)
    except Exception as e:
        log.warning("设置任务栏图标失败: %s", e)


def _find_icon_path() -> Path | None:
    """定位 arxiver.ico：打包后从 _MEIPASS，开发模式从项目根 assets/."""
    from pathlib import Path as _Path
    from .paths import resource_path
    for _cand in (resource_path("assets/arxiver.ico"),
                  _Path(__file__).resolve().parents[1] / "assets" / "arxiver.ico"):
        try:
            if _cand.exists():
                return _cand
        except OSError:
            continue
    return None


def _force_window_icon(hwnd: int) -> None:
    """Win32 强制设置窗口图标（任务栏 + 标题栏）.

    兜底：pywebview 用 .NET System.Drawing.Icon 加载窗口图标，它不支持
    PNG 压缩帧的 ICO（旧版 arxiver.ico 正是这种，导致任务栏显示成 Python
    默认图标）。LoadImageW 由系统 shell 解析，两种格式都支持，直接设上。
    """
    try:
        import ctypes
        ico = _find_icon_path()
        if not ico:
            log.warning("未找到 arxiver.ico，跳过窗口图标设置")
            return
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        hbig = u32.LoadImageW(None, str(ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        hsmall = u32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hbig:
            u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hbig)
        if hsmall:
            u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hsmall)
        # 关键：WM_SETICON 只改窗口级图标，Windows 11 任务栏按钮图标由
        # AppUserModelID 决定（默认取进程 exe 图标），需额外关联 ico。
        _set_taskbar_icon(hwnd, ico)
        log.info("已强制设置窗口图标 hwnd=%d ico=%s hbig=%s hsmall=%s",
                 hwnd, ico, hbig, hsmall)
    except Exception as e:
        log.warning("强制设置窗口图标失败: %s", e)


def _force_icon_delayed(hwnd: int, delay: float) -> None:
    """延迟后强制设置窗口图标（防止被 pywebview 后续的 Form.Icon 覆盖）."""
    import time
    time.sleep(delay)
    _force_window_icon(hwnd)


def _cache_own_hwnd() -> None:
    """窗口创建后、被 LifeSystem 内嵌前尽快缓存 HWND（内嵌后不再是顶层，无法按标题枚举）."""
    import time
    for _ in range(200):  # 最多约 20 秒
        h = _find_own_hwnd()
        if h:
            _hwnd_cache["value"] = h
            log.info("已缓存主窗口句柄 %d", h)
            # 关键：FindWindowW 可能在 pywebview 的 Form.__init__ 尚未执行到
            # self.Icon 之前就按标题找到句柄，立即 WM_SETICON 会被后续 Form.Icon
            # 覆盖。故延迟 0.5s / 2s 各重设一次，确保最终图标正确。
            for delay in (0.5, 2.0):
                threading.Thread(target=_force_icon_delayed, args=(h, delay),
                                 daemon=True).start()
            return
        time.sleep(0.1)


def _detach_if_embedded() -> None:
    """若主窗口被 LifeSystem 用 SetParent 内嵌，先还原成独立顶层窗口.

    这样点托盘「打开主窗口」时，内嵌窗口会脱离 LifeSystem 变成独立窗口，
    保证同一时刻只有一个 Arxiver 窗口（内嵌 XOR 独立），避免一次开多个。
    """
    if sys.platform != "win32":
        return
    hwnd = _hwnd_cache.get("value") or _find_own_hwnd()
    if not hwnd:
        return
    try:
        import ctypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        if not u32.IsWindow(hwnd):
            return
        parent = u32.GetParent(hwnd)
        if not parent or parent == u32.GetDesktopWindow():
            return  # 未被嵌入（parent 为 0/桌面）
        # 恢复标题栏/边框并挂回桌面。
        # 注意：不要加 WS_POPUP(0x80000000)。窗口原本是 WS_OVERLAPPED 类型，
        # 若加了 WS_POPUP，style 高位会被 ctypes 解释成有符号负数，导致
        # LifeSystem 之后再次内嵌时 SetWindowLongW 参数溢出、内嵌失败。
        GWL_STYLE = -16
        u32.GetWindowLongW.restype = ctypes.c_uint32
        u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        style = u32.GetWindowLongW(hwnd, GWL_STYLE)
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_CHILD = 0x40000000
        WS_SYSMENU = 0x00080000
        new_style = (style | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU) & ~WS_CHILD
        u32.SetWindowLongW(hwnd, GWL_STYLE, new_style)
        u32.SetParent(hwnd, 0)
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        log.info("已从 LifeSystem 内嵌还原为独立窗口")
    except Exception as e:
        log.error("内嵌还原失败: %s", e)


def _watch_parent_death() -> None:
    """后台守护：若 LifeSystem 内嵌宿主被强杀/崩溃销毁，自动把窗口还原为独立窗口。

    轮询检测主窗口的父窗口是否仍存活；一旦父窗口失效（IsWindow 返回 False），
    立即脱离内嵌，保证 Arxiver 在 LifeSystem 关闭后仍可独立使用。
    """
    if sys.platform != "win32":
        return
    import time
    while True:
        time.sleep(0.8)
        try:
            import ctypes
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = _hwnd_cache.get("value") or _find_own_hwnd()
            if not hwnd or not u32.IsWindow(hwnd):
                continue
            parent = u32.GetParent(hwnd)
            if not parent or parent == u32.GetDesktopWindow():
                continue  # 未被嵌入，无需处理
            if not u32.IsWindow(parent):
                _detach_if_embedded()
                log.info("检测到 LifeSystem 宿主已关闭，已还原为独立窗口")
                break
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Arxiver — arXiv 论文追踪下载助手")
    parser.add_argument("--minimized", action="store_true", help="后台启动，仅托盘")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    args = parser.parse_args()

    setup_logging()
    setup_global_handlers()
    if not _ensure_single_instance():
        log.info("检测到已有 Arxiver 实例，已激活其窗口，本进程退出")
        return
    log.info("Arxiver 启动 minimized=%s", args.minimized)

    # 必须在创建任何窗口前设置进程 AppUserModelID，否则任务栏图标取进程
    # exe 图标（源码模式 pythonw.exe → Python 图标）。
    _set_app_user_model_id()

    cfg = get_config()
    lib = Library()
    sched = get_scheduler(cfg, lib)
    # 注意：sched.start() 会安排「启动 3 秒后自动同步」，必须等下面把
    # on_done / on_papers 回调接好之后再调用，否则开机那次同步既不会实时
    # 推送到界面、结束时也不会刷新列表。

    # 自启开关与注册表保持一致
    if cfg.get("autostart", False):
        autostart_mod.enable()

    # 桌面快捷方式（需求：桌面有快捷方式 + ico logo）
    if cfg.get("desktop_shortcut", True):
        try:
            from .core import shortcut as shortcut_mod
            shortcut_mod.ensure()
        except Exception as e:
            log.error("桌面快捷方式处理失败: %s", e)

    # 清掉以前下载中断留下的半截 PDF（进程被杀/断电时下载里的 finally 来不及跑）
    try:
        from .core.downloader import clean_stale_tmp, library_base
        n = clean_stale_tmp(library_base(cfg))
        if n:
            log.info("清理下载残留：删除 %d 个半截文件", n)
    except Exception as e:
        log.warning("清理下载残留失败: %s", e)

    # 延迟导入 UI 依赖
    from .tray import create_tray
    from .ui.webui import create_window

    window = None
    _maxed_once = {"done": False}  # 后台托盘启动后，首次从托盘打开时最大化

    def _open_window():
        try:
            _detach_if_embedded()  # 被 LifeSystem 内嵌时先还原成独立窗口
            if window is not None:
                try:
                    window.restore()  # 若处于最小化状态先还原
                except Exception:
                    pass
                window.show()
                # 后台托盘模式（--minimized）启动的窗口：首次从托盘打开时最大化
                # （正常模式已在 webui.create_window 的 shown 事件里最大化）
                if args.minimized and not _maxed_once["done"]:
                    _maxed_once["done"] = True
                    try:
                        window.maximize()
                    except Exception:
                        pass
                # 临时置顶再取消，确保窗口弹到最前（修复点击托盘图标不显示的问题）
                try:
                    window.on_top = True
                    window.on_top = False
                except Exception:
                    pass
        except Exception as e:
            log.error("打开窗口失败: %s", e)

    def _sync_now():
        try:
            if window is not None and getattr(window, "js_api", None):
                window.js_api.sync_now()
        except Exception as e:
            log.error("托盘同步失败: %s", e)

    def _exit():
        log.info("用户退出")
        try:
            sched.shutdown()
        except Exception:
            pass
        try:
            lib.close()
        except Exception:
            pass
        os._exit(0)  # 干净利落地结束所有线程

    tray = None
    if not args.no_tray:
        try:
            tray = create_tray(
                on_open=_open_window,
                on_sync=_sync_now,
                on_exit=_exit,
                get_autostart=autostart_mod.is_enabled,
                set_autostart=lambda v: autostart_mod.enable() if v else autostart_mod.disable(),
            )
        except Exception as e:
            log.error("托盘创建失败（继续运行）: %s", e)

    window, api = create_window(cfg, lib, hidden=args.minimized, no_tray=args.no_tray)

    # 缓存主窗口句柄：必须在 LifeSystem 内嵌前抓取（内嵌后窗口不再是顶层，
    # 无法再按标题枚举到），供托盘「打开主窗口」时判断并脱离内嵌使用
    if sys.platform == "win32":
        threading.Thread(target=_cache_own_hwnd, daemon=True).start()
        # 自愈守护：LifeSystem 内嵌宿主被强杀时，自动脱离内嵌恢复独立窗口
        threading.Thread(target=_watch_parent_death, daemon=True).start()

    # 后台定时同步完成后，通知前端刷新列表（重启后也能看到已抓取的论文）
    sched.on_done = api.notify_sync_done
    # 定时同步过程中也实时增量推送到界面（边抓边出现，不用等全部抓完）
    sched.on_papers = api.notify_new_papers

    # 回调接好了才启动调度：start() 内部会安排启动后自动同步
    sched.start()

    if args.minimized:
        log.info("后台模式：窗口已隐藏，托盘可用")

    try:
        import webview
        # 窗口图标：pywebview 的 icon 只能在 start() 传入。不传时 pywebview 会从
        # sys.executable 提取图标——开发/pythonw 运行时提取到的是 Python 图标，
        # 导致任务栏显示成 Python。打包后从 _MEIPASS 取（需 --add-data "assets;assets"），
        # 开发模式从项目根 assets/ 取。
        from pathlib import Path as _Path
        from .paths import BASE_DIR, resource_path
        _icon = None
        for _cand in (resource_path("assets/arxiver.ico"),
                      _Path(__file__).resolve().parents[1] / "assets" / "arxiver.ico"):
            try:
                if _cand.exists():
                    _icon = str(_cand)
                    break
            except OSError:
                continue
        # private_mode=False + storage_path：让 localStorage（视图模式、
        # 侧栏折叠、学术搜索结果等前端状态）真正落盘。
        # pywebview 默认 private_mode=True，退出时会清掉用户数据目录，
        # 导致每次重启 UI 偏好都被重置。
        webview.start(debug=False, icon=_icon, private_mode=False,
                      storage_path=str(BASE_DIR / "webview"))
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Arxiver 窗口关闭")
        _exit()


if __name__ == "__main__":
    main()
