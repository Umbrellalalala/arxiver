"""这几种「点了没反应 / 偷偷干活 / 数字骗人」的坑必须有断言守着（都不碰网络）。

对应修掉的问题：
1. 任何一次 save_settings（包括只点一下「🌙 深色」）都会 sched.reload()，
   而 reload() 会重排「启动 3 秒后自动同步」→ 换个主题就偷偷抓一遍全网。
2. apps.open_with 返回的是元组，pywebview 序列化成数组，前端读 r.msg 永远是
   undefined →「没装小绿鲸，已用默认程序打开」这类提示从来没显示过。
3. 论文库改名/删除只动磁盘不动库 → 卡片仍显示「📖 PDF」但打不开，
   「已下载」计数虚高，列表里的备注/分类/重要度全变「-」。
4. 热榜某天没发布时接口回 400，旧代码当成「接口挂了」中断整轮 →
   每天早晨的热榜都贡献 0 篇。
5. download_top 在「都下载过了」时静默返回，点完什么都没发生。
6. 导入 PDF 是「移动」，但从不告诉用户文件最后去了哪个目录。
7. 首页每次渲染 get_papers(limit=100000) 拉全库；改后端分页后总数靠
   count_papers，两者条件必须完全一致。
8. Pipeline.sync 现在带进程内唯一互斥锁：手动 + 定时同时到也只有一个真跑。
9. 批量翻译/摘要的完成数以前把失败也计入（没配 AI 时「翻译完成 300 篇」
   其实一篇没翻）；中断标志也从一个共享 Event 拆成按类型各一个。

运行: .venv/Scripts/python.exe tests/test_ux_regress.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

isolated_home("uxregress")   # 必须在 import arxiver.* 之前

import httpx                                          # noqa: E402

import arxiver.core.pipeline as pipeline_mod          # noqa: E402
import arxiver.core.scheduler as sched_mod            # noqa: E402
from arxiver.config import get_config                 # noqa: E402
from arxiver.core import apps                         # noqa: E402
from arxiver.core.clients.hf_daily import HFDailyClient  # noqa: E402
from arxiver.core.downloader import (Downloader, clean_stale_tmp,
                                     library_base)  # noqa: E402
from arxiver.core.library import Library              # noqa: E402
from arxiver.core.models import Paper                 # noqa: E402
from arxiver.ui.webui import Api                      # noqa: E402

ok = True


def check(cond, label, detail=""):
    global ok
    print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail else ""))
    if not cond:
        ok = False


class _Boom:
    """替换 get_scheduler：被调用就说明又去重排调度了。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, cfg, lib):
        self.calls += 1
        raise AssertionError("不该被调用")


def test_save_settings_no_phantom_sync(api, boom) -> None:
    sched_mod.get_scheduler = boom
    cfg = api.cfg
    old = (cfg.get("daily_hour"), cfg.get("daily_minute"))
    api.save_settings({"theme": "dark", "notify": False, "progress_opacity": 60})
    check(boom.calls == 0, "改主题/通知等设置不会重排调度（旧代码必触发一次全网同步）",
          f"calls={boom.calls}")
    api.save_settings({"daily_hour": (old[0] or 8) + 1, "daily_minute": old[1] or 0})
    check(boom.calls == 1, "改了每日同步时间才会重排调度", f"calls={boom.calls}")
    api.save_settings({"daily_hour": old[0], "daily_minute": old[1]})


def test_reload_does_not_arm_startup_sync() -> None:
    timers = []
    real_timer = sched_mod.threading.Timer

    class _FakeTimer:
        def __init__(self, *a, **k):
            timers.append(a)

        def start(self):
            return None

    sched_mod.threading.Timer = _FakeTimer
    try:
        s = sched_mod.Scheduler(get_config(), Library())
        s.reload()
        check(not timers, "reload() 不再安排「启动 3 秒后自动同步」", str(timers))
        s.shutdown()
        s2 = sched_mod.Scheduler(get_config(), Library())
        s2.start()
        check(len(timers) == 1, "start() 仍然会安排启动后自动同步", str(timers))
        s2.shutdown()
    finally:
        sched_mod.threading.Timer = real_timer


def test_open_with_returns_dict() -> None:
    # 用「未知 app + 不存在的文件」这条路径：只走 os.startfile 失败分支，
    # 不会真的把外部阅读器拉起来（本机装了小绿鲸时会弹一个空窗口）。
    missing = Path(tempfile.gettempdir()) / "arxiver-不存在的文件.pdf"
    r = apps.open_with("nosuchapp", str(missing))
    check(isinstance(r, dict) and "msg" in r and "ok" in r,
          "apps.open_with 返回 {ok,msg}（前端读得到 r.msg）", repr(r))
    check(r["ok"] is False and "打开失败" in r["msg"],
          "打不开不存在的文件时 ok=False 且带原因", repr(r))


def test_paths_stay_in_sync(lib) -> None:
    cfg = get_config()
    dl = Downloader(cfg, lib)
    src_dir = Path(tempfile.mkdtemp(prefix="arxiver-src-"))
    pdf = src_dir / "Some Paper 2609.88888.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    res = dl.import_pdfs([str(pdf)])
    dirs = res.get("imported_dirs") or []
    check(bool(res["imported"]), "导入成功", str(res))
    check(dirs and dirs[0].replace("\\", "/").startswith("_inbox/"),
          "导入返回落点目录（前端据此告诉用户文件去哪了）", str(dirs))
    check(not pdf.exists(), "原文件已被移动（不是复制）")
    final = Path(res["imported"][0])
    rec = lib.get("2609.88888")
    check(bool(rec) and rec.get("local_path") == str(final),
          "入库记录带 local_path", str(rec and rec.get("local_path")))

    api = Api(cfg, lib)
    base = library_base(cfg).resolve()
    renamed = api.rename_file(final.relative_to(base).as_posix(), "Renamed Title.pdf")
    row = lib.get("2609.88888")
    check(bool(renamed.get("ok")) and row["local_path"].endswith("Renamed Title.pdf"),
          "论文库改名会同步 local_path（否则「📖 PDF」永远打不开）",
          f"{renamed} -> {row['local_path']}")

    new_rel = Path(row["local_path"]).relative_to(base).as_posix()
    deleted = api.delete_files([new_rel])
    check(deleted.get("deleted") == 1, "删除文件成功", str(deleted))
    check(lib.get("2609.88888")["local_path"] == "",
          "删除文件后 local_path 被清掉（否则「已下载」永远虚高）")
    check(lib.clear_local_paths("D:\\something\\else\\x.pdf") == 0,
          "clear_local_paths 不会误伤别的路径")


def test_hf_daily_400_falls_back() -> None:
    c = HFDailyClient(endpoints=("https://example.invalid",))

    def raise_status(_date):
        req = httpx.Request("GET", "https://example.invalid/api/daily_papers")
        raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))

    def raise_connect(_date):
        raise httpx.ConnectError("connection refused")

    c._get = raise_status
    papers, good = c._get_daily_ex("2026-09-21", 30)
    check(good is True and papers == [], "热榜 400 = 那天没榜单，继续试前一天（旧代码整轮中断）",
          f"{papers},{good}")
    c._get = raise_connect
    papers, good = c._get_daily_ex("2026-09-21", 30)
    check(good is False, "热榜连不上才算真失败，中断剩余日期", f"{papers},{good}")


def test_download_top_reports(api) -> None:
    # 先把所有行标成「已下载」，否则这里会真的去下载。
    for row in api.lib.get_papers(limit=1000):
        api.lib.mark_downloaded(row["arxiv_id"], "D:\\fake\\already.pdf")
    r = api.download_top(5)
    check(r.get("started") is False and r.get("msg"),
          "Top 论文都已下载时给出明确原因（旧代码点完毫无反应）", str(r))


def _mk_paper(i: int) -> Paper:
    return Paper(arxiv_id=f"2609.{90000 + i}", title=f"Paging paper {i}",
                 abstract=f"Abstract {i}", authors=["A Author"], categories=["cs.CL"],
                 published="2026-09-10", source="arxiv")


def test_paging_consistency(lib) -> None:
    for i in range(25):
        lib.upsert([_mk_paper(i)])
    lib.set_status("2609.90003", "trash")

    # 首页分页之后「共 N 条」来自 count_papers，两个查询条件必须完全一致
    for f in ({}, {"status": "trash"}, {"min_score": 1}, {"since_days": 3650},
              {"since_days": 1}, {"tag": "不存在的标签"}):
        full = lib.get_papers(limit=100000, **f)
        n = lib.count_papers(**f)
        check(n == len(full), f"count_papers 与列表条件一致 {f or '无筛选'}",
              f"{n} vs {len(full)}")
    full = lib.get_papers(limit=100000)
    paged: list[dict] = []
    for off in range(0, len(full) + 5, 5):
        paged += lib.get_papers(limit=5, offset=off)
    check([p["arxiv_id"] for p in paged[:len(full)]] == [p["arxiv_id"] for p in full],
          "一页页取回来拼起来 == 一次取全（顺序/条数都不差）")


def test_sync_lock_guard() -> None:
    pipe = pipeline_mod.Pipeline(get_config(), Library())
    check(pipeline_mod._SYNC_LOCK.acquire(blocking=False), "前置：此刻没有同步在跑")
    try:
        check(pipeline_mod.sync_busy() is True, "sync_busy 反映锁状态")
        stat = pipe.sync(days=1)
        check(stat.get("skipped") is True and stat.get("papers") == 0,
              "已有同步在跑时第二次直接跳过（旧代码手动+定时会各跑一遍全网）", str(stat))
    finally:
        pipeline_mod._SYNC_LOCK.release()
    check(pipeline_mod.sync_busy() is False, "释放后 sync_busy 归位")


class _HalfBadLLM:
    """故意让标题以 7 结尾的那篇失败，模拟没配 AI 时免费源挂掉。"""

    enabled = False

    def translate_title(self, title):
        return "" if title.endswith("7") else "中文-" + title

    def translate_abstract(self, abstract):
        return "中文摘要"

    def summarize(self, title, abstract):
        return "" if title.endswith("3") else "中文总结"


def _wait_done(pushes, name, since=0, timeout=8.0) -> dict:
    """等某类批量任务收尾的推送（since 用来只看新一轮，避免拿到上一次的）。"""
    rx = re.compile(r"Arxiver\." + name + r"\((\{.*\})\)")
    end = time.time() + timeout
    while time.time() < end:
        for s in pushes[since:]:
            m = rx.search(s)
            if m:
                return json.loads(m.group(1))
        time.sleep(0.05)
    return {}


def test_batch_counts_and_cancel(api, lib) -> None:
    pushes: list[str] = []
    api._push = lambda js: pushes.append(js)
    api._llm = _HalfBadLLM()
    ids = [f"2609.{91000 + i}" for i in range(8)]
    lib.upsert([Paper(arxiv_id=i, title=f"T{i[-1]}", abstract="abs", authors=["A"],
                      categories=["cs.CL"], published="2026-09-10") for i in ids])

    r = api.batch_translate(ids)
    check(r.get("started") and r.get("count") == 8, "批量翻译接了 8 篇", str(r))
    p = _wait_done(pushes, "onBatchTranslateDone")
    check(p.get("done") == 7 and p.get("failed") == 1,
          "失败的那篇不算成功（旧代码会报「翻译完成 8 篇」）", str(p))
    check(p.get("llm") is False, "收尾带上「有没有配 AI」，前端才能解释失败原因", str(p))

    api.cancel_batch("translate")
    check(api._cancels["translate"].is_set(), "中断只置位翻译这一类")
    check(not api._cancels.get("summarize", threading.Event()).is_set(),
          "翻译的中断不牵连摘要（以前是一个共享 Event）")
    mark = len(pushes)
    api.batch_translate(ids[:1])
    check(not api._cancels["translate"].is_set(),
          "新一轮翻译清掉的是自己那类的标志", str(api._cancels))
    # 等它跑完再往下走：否则线程会在 lib.close() 之后继续用已关的连接
    p2 = _wait_done(pushes, "onBatchTranslateDone", since=mark)
    check(p2.get("done") == 1 and p2.get("failed") == 0 and p2.get("total") == 1,
          "新一轮只处理交给它的那一篇（中断标志已复位、没被上一轮拖累）", str(p2))

    pend = set(lib.ids_pending_translate())
    expect = {x["arxiv_id"] for x in lib.get_papers(limit=100000)
              if x["arxiv_id"] and not x["arxiv_id"].startswith("local:")
              and not (x.get("title_zh") and x.get("abstract_zh"))}
    check(pend == expect,
          "「全部翻译」改用 SQL 取待办后，与原来的 Python 筛选等价",
          f"{len(pend)} vs {len(expect)}")


def test_stale_tmp_cleanup() -> None:
    """下载被杀/断电留下的半截 PDF 以前永远没人清（只有同一目录再下一次才扫）。"""
    base = Path(tempfile.mkdtemp(prefix="arxiver-tmp-"))
    past = time.time() - 3 * 3600

    stale = base / "agent" / "2026" / ".tmp"
    stale.mkdir(parents=True)
    old = stale / "old.pdf"
    old.write_bytes(b"%PDF partial")
    os.utime(old, (past, past))

    busy = base / "llm" / "2026" / ".tmp"
    busy.mkdir(parents=True)
    fresh = busy / "fresh.pdf"
    fresh.write_bytes(b"%PDF partial")

    n = clean_stale_tmp(base)
    check(n == 1 and not old.exists(), "删掉一小时前的残留", f"removed={n}")
    check(fresh.exists() and busy.is_dir(), "正在写的（刚落地）不碰")
    check(not stale.exists(), "只剩空壳的 .tmp 目录一并收掉")


def _dated(days: int, i: int) -> Paper:
    return Paper(arxiv_id=f"2609.{92000 + i}", title=f"Stale paper {i}",
                 abstract="abs", authors=["A"], categories=["cs.CL"],
                 published=(datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"))


def test_auto_archive(lib) -> None:
    api = Api(get_config(), lib)
    stale = [0, 1, 2, 3, 4]
    fresh = [5, 6]
    for i in stale + fresh:
        lib.upsert([_dated(60 if i in stale else 2, i)])
    ids = lambda n: f"2609.{92000 + n}"
    api.lib.set_status(ids(1), "starred")           # 收藏过
    lib.set_tags(ids(2), ["cv"])                    # 打过标签
    lib.set_note(ids(3), "看过一点")                 # 写过笔记
    lib.mark_downloaded(ids(4), "D:/x/4.pdf")        # 下载过
    before_active = lib.count_papers()

    n = lib.archive_stale(30)
    check(n == 1, "只有「60 天 + 完全没人理」的那篇被归档", f"archived={n}")
    check(lib.get_papers(limit=500, status="archived")[0]["arxiv_id"] == ids(0),
          "归档的就是它", str([p["arxiv_id"] for p in lib.get_papers(limit=500, status="archived")]))
    act = {p["arxiv_id"] for p in lib.get_papers(limit=100000)}
    check(ids(0) not in act and all(ids(i) in act for i in stale[1:] + fresh),
          "默认视图不再出现归档条目，其它都还在")
    st = lib.stats()
    check(st["total"] == before_active - 1 and st["archived"] == 1,
          "「推荐池总数」不含归档（不然这个数字只会一直涨）", str(st))

    # 又被数据源抓回来 → 放回推荐池（说明它现在还在被推荐）
    lib.upsert([_dated(60, 0)])
    check(lib.get(ids(0))["status"] == "new", "重新被抓回来的归档论文自动放回推荐池")
    lib.archive_stale(30)

    cfg = get_config()
    cfg.update({"pool_keep_days": 0})           # 设置里选「不自动归档」
    r = api.archive_pool(0)
    check(r.get("ok") is False and r.get("archived") == 0,
          "设置成「不自动归档」时，点「立即整理」也不会偷偷归档", str(r))
    cfg.update({"pool_keep_days": 30})


def test_scholar_cache(api) -> None:
    check(api.get_scholar_cache() is None, "一开始没有缓存（首次仍会自动搜）")
    items = [{"doi": f"10.1/{i}", "title": f"S{i}"} for i in range(120)]
    ok = api.save_scholar_cache({"query": "diffusion policy", "year": "2025",
                                 "ccf": ["A"], "types": [], "items": items})
    check(ok is True, "写缓存成功")
    c = api.get_scholar_cache()
    check(c and c["query"] == "diffusion policy" and c["ccf"] == ["A"],
          "关键词与筛选一起存下来（重开不用重敲）", str(c and c.get("query")))
    check(len(c["items"]) == 80, "结果只留前几页，缓存文件不会无限涨",
          str(len(c["items"])))
    check(c["saved_at"] > 0, "带时间戳，前端才能说「这是 2 小时前存的」")


def main() -> int:
    cfg = get_config()
    lib = Library()
    api = Api(cfg, lib)

    print("[1] save_settings 与调度")
    test_save_settings_no_phantom_sync(api, _Boom())
    print("[2] Scheduler.reload")
    test_reload_does_not_arm_startup_sync()
    print("[3] apps.open_with 返回结构")
    test_open_with_returns_dict()
    print("[4] 导入 / 改名 / 删除与数据库同步")
    test_paths_stay_in_sync(lib)
    print("[5] 热榜 400 回退")
    test_hf_daily_400_falls_back()
    print("[6] 下载 Top 的反馈")
    test_download_top_reports(api)
    print("[7] 分页与总数条件一致")
    test_paging_consistency(lib)
    print("[8] 同步并发守卫")
    test_sync_lock_guard()
    print("[9] 批量任务计数与按类型中断")
    test_batch_counts_and_cancel(api, lib)
    print("[10] 下载残留清理")
    test_stale_tmp_cleanup()
    print("[11] 推荐池自动归档")
    test_auto_archive(lib)
    print("[12] 谷歌学术结果缓存")
    test_scholar_cache(api)

    print("\n结果:", "PASS" if ok else "FAIL")
    lib.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
