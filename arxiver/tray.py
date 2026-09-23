"""系统托盘（pystray）：常驻后台、开机自启后的轻量形态."""
from __future__ import annotations

from typing import Callable

import pystray
from PIL import Image

from .core import autostart as autostart_mod
from .core.errors import log
from .core.notifier import notifier
from .icon import make_icon

__all__ = ["create_tray"]

_MENU_LABELS = {
    "open": "打开主窗口",
    "sync": "立即同步",
    "autostart": "开机自启",
    "exit": "退出",
}


def _icon_image() -> Image.Image:
    return make_icon(64)


def create_tray(
    on_open: Callable[[], None],
    on_sync: Callable[[], None],
    on_exit: Callable[[], None],
    get_autostart: Callable[[], bool],
    set_autostart: Callable[[bool], None],
) -> pystray.Icon:

    def _toggle(icon, item):
        set_autostart(not item.checked)
        item.checked = not item.checked

    def _sync(icon, item):  # noqa: ARG001
        try:
            on_sync()
        except Exception as e:
            log.error("托盘同步失败: %s", e)

    def _exit(icon, item):  # noqa: ARG001
        try:
            icon.stop()
        except Exception:
            pass
        on_exit()

    menu = pystray.Menu(
        pystray.MenuItem(_MENU_LABELS["open"], lambda i, it: on_open(), default=True),
        pystray.MenuItem(_MENU_LABELS["sync"], _sync),
        pystray.MenuItem(
            _MENU_LABELS["autostart"], _toggle, checked=lambda _: get_autostart(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_MENU_LABELS["exit"], _exit),
    )
    icon = pystray.Icon("Arxiver", _icon_image(), "Arxiver — 顶会论文助手", menu)
    icon.run_detached()
    return icon
