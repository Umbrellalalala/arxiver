"""启动流程冒烟测试（无 GUI）。

为什么需要它：`verify_build.py` 只能证明「代码装进 exe 了」，证明不了「跑得起来」。
而 `dist/Arxiver.exe` 是 windowed 程序，直接跑会弹窗口，不适合自动化。

关键：pywebview 的 `create_window()` 源码里写着

    # This immediately creates the window only if `start` has already been called
    if threading.current_thread().name != 'MainThread' and guilib:
        ...
        guilib.create_window(window)

`app.main()` 是在**主线程**、且在 `webview.start()` **之前**调用的，
所以只要把 `webview.start` 换成 no-op，就能把整个启动流程（配置 → 数据库 →
窗口对象 → Api → 调度器接线）完整跑一遍，而不创建任何原生窗口。

顺带守住一个真实踩过的顺序问题：`Scheduler.start()` 内部会安排
「启动 3 秒后自动同步」，必须在 `on_done` / `on_papers` 接好之后才调用，
否则每次开机那次同步既不会实时推送、结束时也不会刷新列表。

运行: .venv/Scripts/python.exe tests/test_app_startup.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

HOME = isolated_home("startup")

# 关掉会动到用户真实环境的东西：桌面快捷方式、注册表自启、系统通知
with open(os.path.join(HOME, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"desktop_shortcut": False, "autostart": False, "notify": False}, f)

import threading  # noqa: E402

import webview  # noqa: E402

from arxiver.core.models import Paper  # noqa: E402
from arxiver.core.scheduler import Scheduler  # noqa: E402

OK = True


def check(cond, label, detail=""):
    global OK
    print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        OK = False


class _ExitCalled(Exception):
    """`app._exit()` 最后会调 os._exit(0)，用它把控制权抢回来。"""


def main() -> int:
    import arxiver.app as appmod
    import arxiver.ui.webui as webui

    rec = {"webview_start": None, "window": None, "api": None,
           "start_call": None, "trigger_sync": 0}

    # ---- 1. 拦掉会碰真实环境的副作用 ----
    appmod._ensure_single_instance = lambda: True          # 不因用户正开着 app 而提前退出
    appmod._cache_own_hwnd = lambda: None                  # 别去改用户真窗口的图标
    appmod._watch_parent_death = lambda: None
    os._exit = lambda code=0: (_ for _ in ()).throw(_ExitCalled(code))

    # ---- 2. 记录启动到哪一步 ----
    orig_create_window = webui.create_window

    def spy_create_window(cfg=None, lib=None, hidden=False, no_tray=False):
        window, api = orig_create_window(cfg, lib, hidden=hidden, no_tray=no_tray)
        rec["window"], rec["api"] = window, api
        return window, api

    webui.create_window = spy_create_window

    orig_start = Scheduler.start

    def spy_sched_start(self):
        # 关键断言点：调度器启动的这一刻，回调必须已经接好
        rec["start_call"] = {"on_done": self.on_done, "on_papers": self.on_papers}
        return orig_start(self)

    Scheduler.start = spy_sched_start
    Scheduler.trigger_sync = lambda self: rec.__setitem__("trigger_sync", rec["trigger_sync"] + 1)

    def fake_webview_start(*a, **k):
        rec["webview_start"] = {"args": a, "kwargs": k}
        return None

    webview.start = fake_webview_start

    # ---- 3. 跑真实启动流程 ----
    argv = sys.argv[:]
    sys.argv = ["arxiver", "--no-tray"]
    try:
        appmod.main()
    except _ExitCalled:
        pass
    except Exception as e:
        import traceback
        print("  启动抛异常:")
        traceback.print_exc()
        check(False, "启动流程不抛异常", repr(e))
    finally:
        sys.argv = argv

    print("\n== 1. 启动流程走通 ==")
    check(rec["webview_start"] is not None, "到达 webview.start（启动流程完整走完）")
    window, api = rec["window"], rec["api"]
    check(window is not None and api is not None, "create_window 返回了窗口和 Api")
    if window is not None:
        check("Arxiver" in (window.title or ""), "窗口标题正确", repr(getattr(window, "title", None)))
    if api is not None:
        check(hasattr(api, "notify_new_papers"), "Api 暴露了 notify_new_papers")
        check(api._window is window, "Api 已 attach 到窗口")

    # 前端资源必须真的被读到（否则会退化成「index.html 缺失」占位页）
    if window is not None:
        html = getattr(window, "html", "") or ""
        check("缺失" not in html and "Arxiver" in html,
              "index.html 资源被正确加载（非缺失兜底）", html[:60])
        check("onNewPapers" in html, "加载到的 HTML 含前端增量刷新逻辑")

    print("\n== 2. 调度器回调接线顺序（本次修的竞态）==")
    call = rec["start_call"]
    check(call is not None, "Scheduler.start() 被调用了")
    if call:
        check(call["on_done"] is not None, "start() 时 on_done 已接好")
        check(call["on_papers"] is not None, "start() 时 on_papers 已接好")
        if call["on_papers"] is not None and api is not None:
            check(getattr(call["on_papers"], "__func__", None) is type(api).notify_new_papers,
                  "on_papers 指向 Api.notify_new_papers")

    print("\n== 3. 启动后推送链路可用 ==")
    if api is not None:
        captured = []

        class _W:
            def evaluate_js(self, js):
                captured.append(js)

        api.attach(_W())
        api.notify_new_papers([Paper(arxiv_id="2609.70001", title="Startup probe",
                                     published="2026-09-18")], 1)
        check(len(captured) == 1, "推送了一段 JS", str(captured))
        if captured:
            js = captured[0]
            check(js.startswith("Arxiver.onNewPapers(") and js.endswith(")"),
                  "推送的是 onNewPapers 调用")
            try:
                payload = json.loads(js[len("Arxiver.onNewPapers("):-1])
                check(payload.get("ids") == ["2609.70001"], "payload 可解析且 ids 正确", str(payload))
            except Exception as e:
                check(False, "payload 是合法 JSON", repr(e))

    print("\n结果:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
