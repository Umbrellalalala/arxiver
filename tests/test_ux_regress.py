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
10. 自动归档按 published 判新旧，而 Semantic Scholar 只给年份 "2026"，
   字符串比 "2026" < "2026-08-26" 成立 → 线上 52 篇种子推荐**进池当天**全被
   归档，种子推荐功能等于静默失效。判据改成 fetched_at（进池时间），
   并且日期在入库时统一补齐成 YYYY-MM-DD。
11. 三个「平时不出事」的隐患：clear_local_paths 把路径当 LIKE 模式（`_inbox`
    的下划线是通配符，会连不相干的行一起清空）；download_top 可能挑中
    local: 行去抓 arxiv.org/pdf/local:xxx；atomic_write_text 用固定 .tmp 名，
    多个下载线程同时写 downloads.json 会互相截断。

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


def _s2_stub(i: int) -> Paper:
    """Semantic Scholar 那种只有年份的日期（曾经把整批种子推荐秒归档）。"""
    return Paper(arxiv_id=f"2609.{93000 + i}", title=f"S2 rec {i}",
                 abstract="abs", authors=["A"], categories=["cs.CL"],
                 published=str(datetime.now().year), source="s2")


def _in_pool_since(lib, arxiv_id: str, days: int) -> None:
    """伪造「进池时间」：upsert 总会把 fetched_at 写成 now，测试要能往回拨。"""
    with lib._lock:
        lib._conn.execute("UPDATE papers SET fetched_at=? WHERE arxiv_id=?",
                          (time.time() - days * 86400, arxiv_id))
        lib._conn.commit()


def test_auto_archive(lib) -> None:
    api = Api(get_config(), lib)
    stale = [0, 1, 2, 3, 4]
    fresh = [5, 6]
    for i in stale + fresh:
        lib.upsert([_dated(60 if i in stale else 2, i)])
    ids = lambda n: f"2609.{92000 + n}"
    for i in stale:
        _in_pool_since(lib, ids(i), 60)
    api.lib.set_status(ids(1), "starred")           # 收藏过
    lib.set_tags(ids(2), ["cv"])                    # 打过标签
    lib.set_note(ids(3), "看过一点")                 # 写过笔记
    lib.mark_downloaded(ids(4), "D:/x/4.pdf")        # 下载过
    before_active = lib.count_papers()

    n = lib.archive_stale(30)
    check(n == 1, "只有「进池 60 天 + 完全没人理」的那篇被归档", f"archived={n}")
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

    # ---- 当年这个功能上线后，线上库里 52/52 条种子推荐**当天**就被归档光 ----
    # 判据以前是 published，而 "2026" < "2026-08-26" 按字符串成立。
    s2_ids = [f"2609.{93000 + i}" for i in range(4)]
    lib.upsert([_s2_stub(i) for i in range(4)])
    check(lib.archive_stale(30) == 0,
          "只有年份的种子推荐刚进池就归档 → 必须为 0 篇",
          str([p["arxiv_id"] for p in lib.get_papers(limit=500, status="archived")]))
    check(all(lib.get(i)["status"] == "new" for i in s2_ids), "它们仍在推荐池里")
    check(s2_ids[0] in {p["arxiv_id"] for p in lib.get_papers(limit=500, status="new")},
          "默认视图能看到它们")
    # 真的躺了 60 天（进池时间）才归档，跟日期格式无关
    for i in s2_ids:
        _in_pool_since(lib, i, 60)
    check(lib.archive_stale(30) == 4, "年付日期论文满 60 天后照样能归档")
    for i in s2_ids:
        lib.set_status(i, "new")
        _in_pool_since(lib, i, 0)      # 放回池子的同时算「刚进池」，别被下一轮又归档

    # 绕开 norm_date，直接塞一行「库里已经是年份格式」的历史数据：
    # 这才能单独证明 archive_stale 换判据有效（线上升级后库里就是这种行）。
    with lib._lock:
        lib._conn.execute(
            "INSERT INTO papers (arxiv_id,title,abstract,status,published,source,"
            "fetched_at) VALUES ('legacy.1','Legacy','a','new','2026','s2',?)",
            (time.time(),))
        lib._conn.commit()
    check(lib.archive_stale(30) == 0,
          "库里存量的年份日期也不再触发归档（判据是进池时间）",
          str(lib.get("legacy.1")["status"]))
    _in_pool_since(lib, "legacy.1", 45)
    check(lib.archive_stale(30) == 1, "存量年份行满 45 天后正常归档")

    # upsert 不许把精确日期退化成年份，也不许把 arxiv 来源改成推荐来源
    exact = _dated(2, 7)
    lib.upsert([exact])
    lib.upsert([Paper(arxiv_id=ids(7), title="Same paper", abstract="abs",
                      authors=["A"], published=str(datetime.now().year), source="s2")])
    row = lib.get(ids(7))
    check(row["published"] == exact.published,
          "S2 的年份日期不会覆盖已有的精确日期", f"{row['published']} vs {exact.published}")
    check(row["source"] == "arxiv", "s2 不会把 arXiv 论文的来源改掉", row["source"])
    # 反过来：arXiv 抓到 s2 行时要能纠正来源
    lib.upsert([_dated(2, 8)])
    check(lib.get(ids(8))["source"] == "arxiv", "arXiv 来源不被推荐源污染")

    # 只有年份的日期进库前就补齐成 YYYY-MM-DD（库里按字符串比较日期）
    lib.upsert([_s2_stub(9)])
    check(len(lib.get(f"2609.{93009}")["published"]) == 10,
          "年份日期在入库时被补齐", str(lib.get(f"2609.{93009}")["published"]))

    cfg = get_config()
    cfg.update({"pool_keep_days": 0})           # 设置里选「不自动归档」
    r = api.archive_pool(0)
    check(r.get("ok") is False and r.get("archived") == 0,
          "设置成「不自动归档」时，点「立即整理」也不会偷偷归档", str(r))
    cfg.update({"pool_keep_days": 30})


def test_archive_runs_on_empty_sync(lib) -> None:
    """一轮什么都没抓到的同步，也必须照样瘦身推荐池。

    归档代码原先写在 `papers_count == 0` 的早退分支**之后**：连着几天没新论文
    （或全网抓取都失败）时，归档永远不会执行。现在两条路径都走 _finish()。
    """
    from arxiver.core.pipeline import Pipeline
    cfg = get_config()
    cfg.update({"pool_keep_days": 30, "major_fields": [], "minor_topics": {},
                "seed_papers": [], "auto_download": False})
    pipe = Pipeline(cfg, lib)
    # 所有源都空手而归
    pipe.arxiv.fetch_recent = lambda *a, **k: []
    pipe.arxiv.search_keywords = lambda *a, **k: []
    pipe.hf.get_recent_days = lambda *a, **k: []
    pipe.s2.related = lambda *a, **k: []

    aid = f"2609.{92071}"
    lib.upsert([_dated(1, 71)])
    _in_pool_since(lib, aid, 45)
    check(lib.get(aid)["status"] == "new", "归档前它还在池子里")

    stat = pipe.sync(days=1, auto_download=False)
    check(stat.get("papers") == 0, "这一轮确实一篇都没抓到", str(stat.get("papers")))
    check(stat.get("archived", 0) >= 1,
          "零论文的同步也会归档（旧代码在这条路径上直接 return，从不归档）", str(stat))
    check(lib.get(aid)["status"] == "archived", "该被归档的那篇真的归档了",
          str(lib.get(aid)["status"]))


def test_paths_and_write_safety(api, lib) -> None:
    """LIKE 通配符、local: 行、并发写文件——三个都是「平时不出事」的坑。"""
    from arxiver.core.models import Paper as _P

    hit, keep = "2609.95102", "2609.95101"
    lib.upsert([_P(arxiv_id=hit, title="Inside inbox", abstract="x", authors=["A"],
                   categories=["cs.CL"], published="2026-09-10"),
                _P(arxiv_id=keep, title="Lookalike", abstract="x", authors=["A"],
                   categories=["cs.CL"], published="2026-09-10")])
    lib.mark_downloaded(hit, "D:\\lib\\_inbox\\2026\\hit.pdf")
    lib.mark_downloaded(keep, "D:\\lib\\Xinbox\\2026\\keep.pdf")
    n = lib.clear_local_paths("D:\\lib\\_inbox")
    check(n == 1 and lib.get(hit)["local_path"] == "" and lib.get(keep)["local_path"],
          "清目录时路径里的 _ 不被当成 LIKE 通配符（否则连 Xinbox 一起清）",
          f"n={n} keep={lib.get(keep)['local_path']!r}")

    # download_top 不能挑中本地导入的行：那没有 arXiv 编号可下
    loc = "local:abcdef123456"
    lib.upsert([_P(arxiv_id=loc, title="Local only", abstract="x", authors=["A"],
                   categories=["cs.CL"], published="2026-09-09", source="local")])
    with lib._lock:
        lib._conn.execute("UPDATE papers SET local_path='', score=9999 WHERE arxiv_id=?",
                          (loc,))
        lib._conn.execute("UPDATE papers SET local_path='' WHERE arxiv_id IN (?,?)",
                          (hit, keep))
        lib._conn.commit()
    tried: list[str] = []
    api.pipeline.downloader.download = lambda p: (tried.append(p.arxiv_id), "")[1]
    api.download_top(5)
    time.sleep(0.5)
    check(loc not in tried,
          "download_top 跳过 local: 行（否则去抓 arxiv.org/pdf/local:xxx）", str(tried[:6]))

    # atomic_write_text：并发写不能共用同一个 .tmp，失败时也不许留下半个文件
    from arxiver.paths import atomic_write_text
    import arxiver.paths as pmod
    d = Path(tempfile.mkdtemp(prefix="arxiver-awt-"))
    tgt = d / "x.json"

    def _w(_: int) -> None:
        atomic_write_text(tgt, json.dumps({"pad": "x" * 20000}))

    th = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    check(json.loads(tgt.read_text(encoding="utf-8")).get("pad"),
          "8 线程并发写之后文件仍是完整 JSON")
    check(not [p for p in d.iterdir() if p.name.endswith(".tmp")],
          "并发写没有留下孤儿 .tmp", str([p.name for p in d.iterdir()]))

    pmod.open = lambda *a, **k: (_ for _ in ()).throw(OSError("模拟写盘失败"))
    try:
        try:
            atomic_write_text(d / "y.json", "junk")
        except OSError:
            pass
    finally:
        del pmod.open          # paths 里原本没有这个全局，删掉才恢复原状
    check(not [p for p in d.iterdir() if p.name.endswith(".tmp")],
          "写失败时把 .tmp 一起收掉", str([p.name for p in d.iterdir()]))

    # 设置里改了 S2 key 要立刻生效（key 是构造时固定的，不重建就得等重启）
    cfg = api.cfg
    cfg.update({"semanticscholar_key": "key-one"})
    first = api.pipeline.s2
    cfg.update({"semanticscholar_key": "key-two"})
    check(api.pipeline.s2 is not first, "换 key 后马上重建 S2 客户端",
          str(api.pipeline.s2 is first))
    check(api.pipeline.s2 is api.pipeline.s2, "key 没变时不反复重建")
    cfg.update({"semanticscholar_key": ""})


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


def test_download_history(api) -> None:
    """下载队列以前只在内存里：重启就空，看不出昨晚下没下成；排队的也撤不掉。"""
    queued = api._dl_task_add("2609.77777", "Queued paper")
    running = api._dl_task_add("2609.77778", "Running paper", started=True)
    check(queued.get("started") is False and running.get("started") is True,
          "任务区分「排队中」和「已经在传」")
    r = api.skip_download(queued["id"])
    check(r.get("ok") is True and api._dl_skipped(queued["id"]),
          "排队中的能撤掉，worker 轮到它会跳过", str(r))
    r2 = api.skip_download(running["id"])
    check(r2.get("ok") is False and "取消不了" in (r2.get("msg") or ""),
          "已经在传的不给假取消按钮", str(r2))

    restarted = Api(get_config(), api.lib)     # 等价于重启进程
    tasks = {t["id"]: t for t in restarted.get_download_tasks()}
    check(queued["id"] in tasks and running["id"] in tasks, "重启后下载记录还在")
    check(tasks[queued["id"]]["status"] == "skipped", "已取消的仍然是已取消")
    check(tasks[running["id"]]["status"] == "failed"
          and "中断" in tasks[running["id"]].get("detail", ""),
          "重启时进行中的标成被中断，而不是永远转圈", str(tasks[running["id"]]))
    cleared = restarted.clear_download_tasks()
    check(cleared >= 2 and not restarted.get_download_tasks(),
          "清除已完成会连带清掉磁盘记录（否则下次启动又复活）", str(cleared))


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
    print("[12] 零论文同步也要归档")
    test_archive_runs_on_empty_sync(lib)
    print("[13] LIKE 通配符 / local: 过滤 / 并发写文件")
    test_paths_and_write_safety(api, lib)
    print("[14] 谷歌学术结果缓存")
    test_scholar_cache(api)
    print("[15] 下载记录持久化与排队取消")
    test_download_history(api)

    print("\n结果:", "PASS" if ok else "FAIL")
    lib.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
