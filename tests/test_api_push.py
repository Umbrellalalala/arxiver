"""Api 层推送链路验证（无 GUI）。

`ui/webui.py` 的 Api 是「后端 → 前端」的唯一出口，改动后必须真的跑一遍：
- sync_range 会不会按 onSyncStart → onProgress → onNewPapers → onSyncDone 的顺序推；
- onNewPapers 的 payload 是不是合法 JSON、带不带 ids；
- 后台定时同步的 on_papers 钩子（scheduler）有没有真的传下去；
- 前端窗口抛异常时 _push 会不会把同步线程带崩；
- 「正在同步」的守卫现在是 pipeline._SYNC_LOCK（手动 + 定时共用一把）：
  忙时要拒绝并且**不点亮进度条**，抢锁失败时也要把条收掉。

同时把捕获到的推送字符串写到 tests/_api_pushes.json，
交给 tests/test_push_to_dom.js 在 DOM 桩里真实重放（Python → JS 的端到端）。

运行: .venv/Scripts/python.exe tests/test_api_push.py
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

isolated_home("api")   # 必须在 import arxiver.* 之前

from arxiver.config import get_config                      # noqa: E402
from arxiver.core.errors import setup_logging              # noqa: E402
from arxiver.core.library import Library                   # noqa: E402
from arxiver.core.models import Paper                      # noqa: E402
from arxiver.ui.webui import Api                           # noqa: E402

PUSHES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_api_pushes.json")

FAKE_PAPERS = [
    {"arxiv_id": "2609.90001", "title": "First streamed paper",
     "published": "2026-09-18", "score": 55.0, "source": "arxiv", "tags": ""},
    {"arxiv_id": "2609.90002", "title": "Second streamed paper",
     "published": "2026-09-18", "score": 41.0, "source": "hf_daily", "tags": ""},
    {"arxiv_id": "2609.90003", "title": "Third streamed paper",
     "published": "2026-09-18", "score": 33.0, "source": "arxiv", "tags": ""},
]


class FakeWindow:
    """记录 evaluate_js 收到的每一段 JS。"""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[str] = []
        self.boom = boom

    def evaluate_js(self, js: str) -> None:
        # 先记再抛：boom 模式也要能让 wait_done 看到线程真的走到了最后一条推送
        self.calls.append(js)
        if self.boom:
            raise RuntimeError("模拟前端已关闭")


class FakePipeline:
    """按真实 pipeline 的约定回调：两批论文、两次进度、最后返回统计。"""

    def sync(self, days=1, auto_download=None, on_progress=None, on_papers=None) -> dict:
        on_progress("抓取 arXiv 最新论文…", 10)
        time.sleep(0.05)
        on_papers([Paper(arxiv_id="2609.90001", title="First streamed paper",
                         published="2026-09-18")], 1)
        on_progress("arXiv 大方向完成，累计新增 1 篇", 40)
        time.sleep(0.05)
        on_papers([Paper(arxiv_id="2609.90002", title="Second streamed paper",
                         published="2026-09-18"),
                   Paper(arxiv_id="2609.90003", title="Third streamed paper",
                         published="2026-09-18")], 2)
        on_progress("入库完成：本次新增 3 篇", 92)
        return {"papers": 3, "new": 3, "downloaded": [], "failed": [], "elapsed": 0.2}


class BusyThenSkipPipeline:
    """真实 Pipeline.sync 在抢不到锁时的行为：不抓取，直接返回 skipped 标记。"""

    def sync(self, days=1, auto_download=None, on_progress=None, on_papers=None) -> dict:
        return {"skipped": True, "reason": "正在同步中，请稍候",
                "new": 0, "papers": 0, "failed": [], "elapsed": 0}


def wait_done(win: "FakeWindow", since: int = 0, timeout: float = 10.0) -> bool:
    """等这次同步真的收尾（推送里出现 onSyncDone / onSyncError）。

    以前是 poll `api._syncing`，那个标志已经删掉了：全进程只有一个「正在同步」
    的权威来源（pipeline._SYNC_LOCK），而这里用的是假管道、不持锁，所以只能看推送。
    since 用来只看新一轮，避免被上一轮的 onSyncDone 立刻满足。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        for js in win.calls[since:]:
            if js.startswith("Arxiver.onSyncDone") or js.startswith("Arxiver.onSyncError"):
                return True
        time.sleep(0.05)
    return False


def main() -> int:
    setup_logging()
    ok = True

    def check(cond, label, detail=""):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            ok = False

    cfg = get_config()
    cfg.update({"notify": False, "major_fields": [], "minor_topics": {}})
    lib = Library()

    # ---------- 1. 手动同步：推送顺序与 payload ----------
    print("== 1. sync_range 的推送序列 ==")
    api = Api(cfg, lib)
    win = FakeWindow()
    api.attach(win)
    api.pipeline = FakePipeline()

    res = api.sync_range(1)
    check(res.get("started") is True, "sync_range 立刻返回 {started: True}")
    check(wait_done(win), "同步线程正常结束")

    kinds = []
    for js in win.calls:
        for k in ("onSyncStart", "onProgress", "onNewPapers", "onSyncDone", "onSyncError"):
            if js.startswith(f"Arxiver.{k}"):
                kinds.append(k)
                break
    print("     实际推送:", " → ".join(kinds))
    check(kinds[0] == "onSyncStart", "第一条是 onSyncStart")
    check(kinds[-1] == "onSyncDone", "最后一条是 onSyncDone")
    check(kinds.count("onNewPapers") == 2, "推了 2 次 onNewPapers（每批一次）",
          f"实际 {kinds.count('onNewPapers')}")
    check("onProgress" in kinds, "推了 onProgress")
    check(kinds.index("onNewPapers") < kinds.index("onSyncDone"),
          "onNewPapers 在 onSyncDone 之前（是增量而不是最后一次性）")
    # 第一次 onNewPapers 必须在最后一条 onProgress 之前 —— 证明边抓边推
    first_new = kinds.index("onNewPapers")
    last_prog = len(kinds) - 1 - kinds[::-1].index("onProgress")
    check(first_new < last_prog, "首批论文在进度走完之前就推出来了")

    # payload 合法性
    payloads = []
    for js in win.calls:
        if js.startswith("Arxiver.onNewPapers("):
            payloads.append(json.loads(js[len("Arxiver.onNewPapers("):-1]))
    check(len(payloads) == 2, "解析出 2 个 onNewPapers payload")
    check(all("ids" in p and p["ids"] for p in payloads), "payload 都带非空 ids")
    check(all("new" in p for p in payloads), "payload 都带 new 计数")
    all_ids = [i for p in payloads for i in p["ids"]]
    check(all_ids == ["2609.90001", "2609.90002", "2609.90003"],
          "ids 覆盖全部新论文且顺序正确", str(all_ids))
    sync_pushes = list(win.calls)   # 留一份给后面的 DOM 重放

    # ---------- 2. 重入保护 ----------
    print("\n== 2. 重入保护 ==")
    import arxiver.core.pipeline as pipeline_mod
    api.pipeline = FakePipeline()
    mark = len(win.calls)
    held = pipeline_mod._SYNC_LOCK.acquire(blocking=False)
    try:
        r2 = api.sync_range(1)
        check("error" in r2, "同步中再次调用被拒绝", str(r2))
    finally:
        if held:
            pipeline_mod._SYNC_LOCK.release()
    # 被拒绝时连进度都不该点亮（更要紧的是：真的没去抓第二遍）
    check(not any(js.startswith("Arxiver.onSyncStart") for js in win.calls[mark:]),
          "被拒绝的这次没有启动新同步", str(win.calls[mark:]))

    # ---------- 2b. 起跑前被定时任务抢锁 ----------
    print("\n== 2b. 查过没忙、起跑时却被定时任务抢了锁 ==")
    mark = len(win.calls)
    api.pipeline = BusyThenSkipPipeline()
    real_busy = pipeline_mod.sync_busy
    # 让 sync_range 开头的快速检查放行，模拟「查完到开跑之间被定时任务抢先」
    pipeline_mod.sync_busy = lambda: False
    try:
        api.sync_range(1)
        time.sleep(0.3)
    finally:
        pipeline_mod.sync_busy = real_busy
    tail = [js for js in win.calls[mark:] if js.startswith("Arxiver.on")]
    print("     实际推送:", " → ".join(t.split("(")[0].replace("Arxiver.", "") for t in tail))
    check(any(js.startswith("Arxiver.onSyncDone") for js in tail),
          "抢锁失败那轮仍然收尾（进度条不会永远挂着）", str(tail))
    check(any(js.startswith("Arxiver.onToast") for js in tail),
          "抢锁失败会明确告诉用户这次跳过了", str(tail))

    # ---------- 3. 前端挂掉不能带崩同步线程 ----------
    print("\n== 3. 前端 evaluate_js 抛异常 ==")
    api2 = Api(cfg, lib)
    win2 = FakeWindow(boom=True)
    api2.attach(win2)
    api2.pipeline = FakePipeline()
    api2.sync_range(1)
    check(wait_done(win2), "前端抛异常时同步线程仍然正常收尾")

    # ---------- 4. scheduler 的 on_papers 钩子真的传下去了 ----------
    print("\n== 4. 后台定时同步的钩子 ==")
    from arxiver.core.scheduler import Scheduler
    sched = Scheduler(cfg, lib)
    seen = {}

    class RecordingPipeline:
        def sync(self, days=1, auto_download=None, on_progress=None, on_papers=None):
            seen["on_papers"] = on_papers
            seen["days"] = days
            if on_papers:
                on_papers([Paper(arxiv_id="2609.91000", title="From scheduler",
                                 published="2026-09-18")], 1)
            return {"papers": 1, "new": 1, "downloaded": [], "failed": [], "elapsed": 0.1}

    sched.pipeline = RecordingPipeline()
    sched.on_papers = api.notify_new_papers
    sched._sync_until_success(days=1)
    # 注意：绑定方法是每次访问都新建的对象，`is` 永远为 False，
    # 要用 ==（比较 __self__ / __func__）或直接比 __func__
    got = seen.get("on_papers")
    check(getattr(got, "__func__", None) is Api.notify_new_papers,
          "Scheduler 把 on_papers 传给了 Pipeline.sync",
          repr(got))
    tail = win.calls[-1] if win.calls else ""
    check(tail.startswith("Arxiver.onNewPapers("), "后台同步也真的推到了前端", tail[:60])

    # ---------- 5. 导出给 DOM 重放 ----------
    replay = [js for js in sync_pushes if js.startswith("Arxiver.on")]
    with open(PUSHES_PATH, "w", encoding="utf-8") as f:
        json.dump({"pushes": replay, "papers": FAKE_PAPERS}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n已导出 {len(replay)} 条推送到 {os.path.basename(PUSHES_PATH)}")

    lib.close()
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
