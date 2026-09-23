"""第三方应用查找与打开（WPS / Google Chrome / 小绿鲸文献翻译器）.

查找优先级：注册表 App Paths → 注册表 Uninstall 信息（DisplayIcon/InstallLocation）
→ 常见安装路径。找不到时回退系统默认程序打开。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import log

try:
    import winreg
except ImportError:  # 非 Windows
    winreg = None  # type: ignore

__all__ = ["find_exe", "open_with"]

_APPS = ("wps", "chrome", "xlj")


def _q(key, name: str) -> str:
    try:
        v, _ = winreg.QueryValueEx(key, name)
        return v or ""
    except OSError:
        return ""


def _app_paths(name: str) -> str:
    """HKCU/HKLM Software\\Microsoft\\Windows\\CurrentVersion\\App Paths."""
    if winreg is None:
        return ""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}"
            ) as k:
                v, _ = winreg.QueryValueEx(k, "")
                if v:
                    exe = v.split(",")[0].strip().strip('"')
                    if exe and Path(exe).exists():
                        return exe
        except OSError:
            continue
    return ""


def _uninstall_info(keyword: str) -> dict | None:
    """在 Uninstall 注册表中按 DisplayName 模糊查找."""
    if winreg is None:
        return None
    subs = [
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in subs:
            try:
                base = winreg.OpenKey(root, sub)
            except OSError:
                continue
            with base:
                i = 0
                while True:
                    try:
                        key_name = winreg.EnumKey(base, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(base, key_name) as k:
                            display = _q(k, "DisplayName")
                            if keyword.lower() not in (display or "").lower():
                                continue
                            return {
                                "display": display,
                                "loc": _q(k, "InstallLocation"),
                                "icon": _q(k, "DisplayIcon"),
                            }
                    except OSError:
                        continue
    return None


def _icon_to_exe(icon: str) -> str:
    exe = (icon or "").split(",")[0].strip().strip('"')
    return exe if exe and Path(exe).exists() else ""


def find_exe(app: str) -> str:
    """返回应用 exe 绝对路径；找不到返回空串."""
    if app == "wps":
        for n in ("wps.exe", "wpspdf.exe"):
            p = _app_paths(n)
            if p:
                return p
        info = _uninstall_info("WPS Office")
        if info:
            if info["icon"]:
                exe = _icon_to_exe(info["icon"])
                if exe:
                    return exe
            if info["loc"] and Path(info["loc"]).is_dir():
                for exe in sorted(Path(info["loc"]).glob("wps*.exe")):
                    return str(exe)
        return ""
    if app == "chrome":
        p = _app_paths("chrome.exe")
        if p:
            return p
        for c in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ):
            if Path(c).exists():
                return c
        return ""
    if app == "xlj":
        for n in ("xiaolvjing.exe", "xlj.exe", "xljreader.exe"):
            p = _app_paths(n)
            if p:
                return p
        info = _uninstall_info("小绿鲸")
        if info:
            exe = _icon_to_exe(info["icon"])
            if exe:
                return exe
            if info["loc"] and Path(info["loc"]).is_dir():
                for exe in sorted(Path(info["loc"]).rglob("*.exe")):
                    n = exe.name.lower()
                    if any(k in n for k in ("xlj", "xiaolvjing", "green", "sci")):
                        return str(exe)
                for exe in sorted(Path(info["loc"]).glob("*.exe")):
                    return str(exe)
        return ""
    return ""


def open_with(app: str, file_path: str) -> dict:
    """用指定应用打开文件。app: wps / chrome / xlj / default.

    返回 {"ok": bool, "msg": str}。msg 只在回退/失败这种需要告诉用户的情况下
    非空——正常打开不提示，目标应用自己弹出来就是最好的反馈。
    （以前返回的是元组：pywebview 会序列化成数组，前端读 r.msg 永远是 undefined，
    「没装小绿鲸所以用了默认程序」这类提示从来没显示过。）
    """
    target = Path(file_path).resolve()
    names = {"wps": "WPS", "chrome": "Google Chrome", "xlj": "小绿鲸"}
    if app in _APPS:
        exe = find_exe(app)
        if exe:
            try:
                if app == "chrome":
                    subprocess.Popen([exe, target.as_uri()])
                else:
                    subprocess.Popen([exe, str(target)])
                return {"ok": True, "msg": ""}
            except Exception as e:
                log.error("用 %s(%s) 打开失败: %s", names[app], exe, e)
        else:
            log.info("未检测到 %s，回退系统默认程序", names[app])
    # 默认程序回退
    try:
        os.startfile(str(target))  # noqa: S606
        if app in _APPS:
            return {"ok": True, "msg": f"未检测到 {names.get(app, app)}，已用系统默认程序打开"}
        return {"ok": True, "msg": ""}
    except Exception as e:
        return {"ok": False, "msg": f"打开失败: {e}"}
