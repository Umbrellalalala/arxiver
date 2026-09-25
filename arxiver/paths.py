"""路径管理：数据目录 / PDF 库 / 日志 / 数据库.

默认位于 ``~/.arxiver``，可通过环境变量 ``ARXIVER_HOME`` 覆盖（开发与便携模式）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

__all__ = [
    "BASE_DIR", "LIBRARY_DIR", "LOG_DIR", "DB_PATH", "CONFIG_PATH",
    "BACKUP_DIR", "ensure_dirs", "resource_path", "atomic_write_text",
]


def _base() -> Path:
    env = os.environ.get("ARXIVER_HOME")
    if env:
        return Path(env)
    return Path.home() / ".arxiver"


BASE_DIR = _base()
LIBRARY_DIR = BASE_DIR / "library"      # PDF 归档根目录
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "library.db"
CONFIG_PATH = BASE_DIR / "config.json"
BACKUP_DIR = BASE_DIR / "backups"       # 配置/数据库损坏时的回滚快照


def ensure_dirs() -> None:
    for p in (BASE_DIR, LIBRARY_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """先写临时文件再 os.replace，避免写入中途崩溃/断电把文件截断成空。

    临时名必须**每次不同**：下载记录（downloads.json）会被多个下载线程同时
    写，共用一个 `.tmp` 时两个线程会互相截断对方的写入，再各自 replace 一次，
    结果是「文件里躺着半条 JSON」。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident():x}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()          # 别让失败的临时文件留在数据目录里
        except OSError:
            pass
        raise


def resource_path(rel: str) -> Path:
    """兼容 PyInstaller 打包后的资源定位.

    依次尝试多种布局：
    - 开发模式: {包目录}/{rel}
    - 打包模式: {_MEIPASS}/{rel} 或 {_MEIPASS}/arxiver/{rel}（--add-data 带包名前缀时）
    """
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
        bases.append(Path(sys.executable).resolve().parent)
    else:
        bases.append(Path(__file__).resolve().parent)
    for b in bases:
        for cand in (b / rel, b / "arxiver" / rel):
            if cand.exists():
                return cand
    return bases[0] / rel
