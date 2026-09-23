"""Windows 通知（需求 4：在 exe 中推送）.

使用开源库 winotify 发系统 Toast；不可用时降级为日志 + 托盘气泡。
"""
from __future__ import annotations

from .errors import log

__all__ = ["Notifier", "notifier"]

try:
    from winotify import Notification
    _HAS_WINOTIFY = True
except Exception:  # pragma: no cover
    _HAS_WINOTIFY = False


class Notifier:
    app_id = "Arxiver"

    def toast(self, title: str, message: str, launch: str | None = None,
              duration: str = "short") -> None:
        if not _HAS_WINOTIFY:
            log.info("[通知] %s — %s", title, message)
            return
        try:
            n = Notification(app_id=self.app_id, title=title, msg=message, duration=duration)
            if launch:
                n.set_audio(None, loop=False)
                n.add_actions(label="查看", launch=launch)
            n.show()
        except Exception as e:
            log.warning("Toast 通知失败（降级为日志）: %s", e)


notifier = Notifier()
