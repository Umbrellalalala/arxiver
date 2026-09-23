"""开机自启（需求 1）：HKCU Run 键，用户级无需管理员权限."""
from __future__ import annotations

import sys

from .errors import log

__all__ = ["is_enabled", "enable", "disable"]

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE = "Arxiver"


def _exe_cmd() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return f'"{exe}" --minimized'
    # 开发模式：python -m arxiver
    return f'"{exe}" -m arxiver --minimized'


def is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _VALUE)
        return True
    except OSError:
        return False


def enable() -> bool:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.SetValueEx(k, _VALUE, 0, winreg.REG_SZ, _exe_cmd())
        log.info("已注册开机自启: %s", _exe_cmd())
        return True
    except OSError as e:
        log.error("注册开机自启失败: %s", e)
        return False


def disable() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _VALUE)
        log.info("已取消开机自启")
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log.error("取消开机自启失败: %s", e)
        return False
