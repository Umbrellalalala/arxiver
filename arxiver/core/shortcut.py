"""桌面快捷方式管理（需求：桌面有快捷方式）.

通过 PowerShell COM (WScript.Shell) 创建 .lnk，使用 -EncodedCommand 传脚本
完全规避路径转义问题；无额外 Python 依赖。
"""
from __future__ import annotations

import base64
import subprocess
import sys

from .errors import log

__all__ = ["create", "remove", "exists", "ensure"]

_APP_LNK = "Arxiver.lnk"

_PS_CREATE = r"""
$ErrorActionPreference = 'Stop'
$exe = '{exe}'
$desk = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut((Join-Path $desk '{lnk}'))
$s.TargetPath = $exe
$s.WorkingDirectory = (Split-Path $exe)
$s.IconLocation = "$exe,0"
$s.Description = 'Arxiver - 顶会论文助手'
$s.Save()
"""

_PS_EXISTS = r"""
$desk = [Environment]::GetFolderPath('Desktop')
Test-Path (Join-Path $desk '{lnk}')
"""

_PS_REMOVE = r"""
$ErrorActionPreference = 'Stop'
$desk = [Environment]::GetFolderPath('Desktop')
$p = Join-Path $desk '{lnk}'
if (Test-Path $p) {{ Remove-Item $p -Force }}
"""


def _run_ps(script: str) -> tuple[bool, str]:
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", enc],
            capture_output=True, timeout=30, startupinfo=si,
        )
        err = (r.stderr or r.stdout or b"").decode(errors="ignore").strip()
        return r.returncode == 0, err
    except Exception as e:
        return False, str(e)


def _exe_path() -> str:
    """exe 真实路径：打包后为自身，开发模式尝试项目 dist 产物."""
    if getattr(sys, "frozen", False):
        return sys.executable
    from pathlib import Path
    cand = Path(__file__).resolve().parents[2] / "dist" / "Arxiver.exe"
    if cand.exists():
        return str(cand)
    return ""


def create() -> bool:
    exe = _exe_path()
    if not exe:
        log.info("未找到 Arxiver.exe，跳过快捷方式创建")
        return False
    ok, err = _run_ps(_PS_CREATE.format(exe=exe.replace("'", "''"), lnk=_APP_LNK))
    if ok:
        log.info("已创建桌面快捷方式 -> %s", exe)
    else:
        log.error("创建桌面快捷方式失败: %s", err)
    return ok


def remove() -> bool:
    ok, err = _run_ps(_PS_REMOVE.format(lnk=_APP_LNK))
    if ok:
        log.info("已删除桌面快捷方式")
    else:
        log.warning("删除桌面快捷方式失败: %s", err)
    return ok


def exists() -> bool:
    ok, out = _run_ps(_PS_EXISTS.format(lnk=_APP_LNK))
    return ok and "true" in out.lower()


def ensure() -> bool:
    """启动时按需创建（幂等）。"""
    if exists():
        return True
    return create()
