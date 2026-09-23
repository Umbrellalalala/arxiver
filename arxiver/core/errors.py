"""统一错误处理（需求 6）：日志 + 非阻塞弹窗 + 重试.

任何网络/磁盘异常都不会让程序卡死：
- 后台线程异常 → 写入日志 + 弹窗提醒（带冷却，避免刷屏）
- 网络请求 → 自动重试（指数退避）
"""
from __future__ import annotations

import ctypes
import functools
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

from ..paths import LOG_DIR, ensure_dirs

__all__ = ["log", "setup_logging", "setup_global_handlers", "alert", "retry"]

log = logging.getLogger("arxiver")

_alert_lock = threading.Lock()
_last_alert = 0.0
_ALERT_COOLDOWN = 5.0  # 秒


def setup_logging() -> None:
    if log.handlers:  # 幂等
        return
    ensure_dirs()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
    fh = RotatingFileHandler(
        LOG_DIR / "arxiver.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)


def alert(title: str, message: str, icon: int = 0x30) -> None:
    """非阻塞弹窗提醒（MB_ICONWARNING=0x30, MB_ICONERROR=0x10）.

    在独立线程中显示，绝不影响主流程；带冷却避免错误风暴刷屏。
    """
    global _last_alert

    def _show() -> None:
        with _alert_lock:
            global _last_alert
            now = time.time()
            if now - _last_alert < _ALERT_COOLDOWN:
                return
            _last_alert = now
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, icon | 0x0)
        except Exception:  # 非 Windows 或调用失败时静默降级为日志
            log.error("弹窗失败: %s — %s", title, message)

    threading.Thread(target=_show, name="alert", daemon=True).start()


def _handle_exc(exc: BaseException, where: str) -> None:
    log.error("[%s 未捕获异常] %s\n%s", where, exc, traceback.format_exc())
    alert("Arxiver 出错", f"{where} 发生异常：\n{exc}\n\n详情见日志 {LOG_DIR / 'arxiver.log'}")


def setup_global_handlers() -> None:
    def excepthook(etype, value, tb):
        if issubclass(etype, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(etype, value, tb)
            return
        _handle_exc(value, "主线程")

    def threadhook(args):
        _handle_exc(args.exc_value, f"后台线程 {args.thread.name}")

    sys.excepthook = excepthook
    threading.excepthook = threadhook


def retry(times: int = 3, delay: float = 2.0, backoff: float = 2.0,
          exceptions: tuple[type[BaseException], ...] = (Exception,)):
    """指数退避重试装饰器，网络请求标配."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            d = delay
            for i in range(times):
                try:
                    return fn(*a, **k)
                except exceptions as e:
                    if i == times - 1:
                        raise
                    log.warning("[%s] 第 %d/%d 次失败: %s，%.0fs 后重试", fn.__name__, i + 1, times, e, d)
                    time.sleep(d)
                    d *= backoff
            return None  # pragma: no cover
        return wrapper
    return deco
