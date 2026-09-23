"""增量同步验证：抓一批 → 立刻入库 → 立刻回调（不等到全部抓完）.

用假的客户端替换网络请求，断言：
1. on_papers 被多次调用（每个数据源批次一次），而不是最后只调一次；
2. 每次回调时对应论文已经能立刻从本地库查到（说明是实时入库而非最后统一入库）；
3. 跨源重复论文不会重复计数。

运行: .venv/Scripts/python tests/test_incremental_sync.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

isolated_home("incr")   # 必须在 import arxiver.* 之前

from arxiver.config import get_config  # noqa: E402
from arxiver.core.errors import setup_logging  # noqa: E402
from arxiver.core.library import Library  # noqa: E402
from arxiver.core.models import Paper  # noqa: E402
from arxiver.core.pipeline import Pipeline, _MAX_SEEDS  # noqa: E402

BATCH_DELAY = 0.6  # 模拟每个分类/每天的抓取耗时


def _mk(i: int, title: str = "") -> Paper:
    return Paper(
        arxiv_id=f"2609.{10000 + i}",
        title=title or f"Test Paper {i} about vision transformer",
        abstract="We study efficient attention for large-scale recognition.",
        authors=["A. Author"],
        categories=["cs.CV"],
        published="2026-09-18",
        pdf_url=f"https://arxiv.org/pdf/2609.{10000 + i}",
        abs_url=f"https://arxiv.org/abs/2609.{10000 + i}",
        source="arxiv",
    )


def main() -> int:
    setup_logging()
    cfg = get_config()
    cfg.update({
        "major_fields": ["人工智能"],
        "minor_topics": {"人工智能": ["vision transformer"]},
        # 5 个种子：旧代码是 `seeds[:3]`，第 4、5 篇会被静默跳过（见下面的断言）
        "seed_papers": [f"2609.{20000 + i}" for i in range(5)],
    })
    lib = Library()
    pipe = Pipeline(cfg, lib)

    # ---- 用假客户端替换网络请求，每次调用间隔 BATCH_DELAY 秒 ----
    def fake_fetch_recent(categories, days=1, max_results=80, on_batch=None):
        out = []
        for idx, cat in enumerate(categories[:4]):
            time.sleep(BATCH_DELAY)
            batch = [_mk(idx * 10 + j) for j in range(5)]
            out += batch
            if on_batch:
                on_batch(batch, f"arXiv {cat}")
        return out

    def fake_search_keywords(keywords, days=7, max_results=30, on_batch=None):
        time.sleep(BATCH_DELAY)
        batch = [_mk(100 + j) for j in range(4)]
        if on_batch:
            on_batch(batch, "关键词")
        return batch

    def fake_hf(days=1, limit=40, on_batch=None):
        out = []
        for d in range(days):
            time.sleep(BATCH_DELAY)
            # 故意混入一篇 arXiv 批次里已经出现过的论文，验证跨源去重
            batch = [_mk(0)] + [_mk(200 + d * 10 + j) for j in range(3)]
            out += batch
            if on_batch:
                on_batch(batch, f"热榜 day{d}")
        return out

    pipe.arxiv.fetch_recent = fake_fetch_recent
    pipe.arxiv.search_keywords = fake_search_keywords
    pipe.hf.get_recent_days = fake_hf

    s2_calls: list[str] = []

    class _FakeS2:
        def related(self, sid, limit=10):
            s2_calls.append(sid)
            time.sleep(BATCH_DELAY)
            return [_mk(300 + j) for j in range(3)]

        def citations(self, ids):
            time.sleep(0.2)
            return {i: 42 for i in ids[:2]}

    pipe._s2 = _FakeS2()

    # ---- 记录回调 ----
    events: list[dict] = []
    t0 = time.time()

    def on_papers(papers, new):
        ids = [p.clean_id for p in papers]
        # 关键断言：回调时论文必须已经能立刻从库里查到
        missing = [i for i in ids if lib.get(i) is None]
        events.append({
            "t": round(time.time() - t0, 2),
            "new": new,
            "count": len(papers),
            "in_db_now": not missing,
        })

    progress: list[str] = []
    stat = pipe.sync(days=1, auto_download=False,
                     on_progress=lambda text, pct: progress.append(text),
                     on_papers=on_papers)

    print("== 回调时间线 ==")
    for e in events:
        print(f"  t={e['t']:>5}s  本批 {e['count']:>2} 篇  新增 {e['new']:>2}  "
              f"回调时已入库={e['in_db_now']}")
    print("\n== 统计 ==", stat)
    print("== 进度文案 ==")
    for m in progress:
        print("  ", m)

    ok = True
    if len(events) < 5:
        print(f"\n[FAIL] 期望多次增量回调，实际只有 {len(events)} 次")
        ok = False
    if not all(e["in_db_now"] for e in events):
        print("\n[FAIL] 有回调发生时论文还没入库（说明不是实时入库）")
        ok = False
    first_new = next((e for e in events if e["new"] > 0), None)
    if first_new is None or first_new["t"] > BATCH_DELAY * 2 + 0.8:
        print("\n[FAIL] 首批新论文出现得太晚，没有做到边抓边更新")
        ok = False
    # 各批 new 之和必须等于最终统计（口径一致，不重复计数）
    if sum(e["new"] for e in events) != stat["new"]:
        print(f"\n[FAIL] 分批新增之和 {sum(e['new'] for e in events)} "
              f"!= 最终统计 {stat['new']}")
        ok = False
    # 每篇论文在库里只有一行（跨源重复被去重）
    total = lib.stats()["total"]
    if stat["new"] != total:
        print(f"\n[FAIL] 新增 {stat['new']} 篇但库里共 {total} 篇，存在重复入库")
        ok = False
    # 只有 1 个大方向（人工智能 → 1 个 arXiv 分类），去重后期望 = 5 + 4 + 3 + 3 = 15
    if stat["new"] != 15:
        print(f"\n[FAIL] 新增篇数 {stat['new']}（预期 15：arXiv 5 + 关键词 4 + "
              f"热榜 3 + 种子 3；热榜里那篇重复的应被去重）")
        ok = False

    # 种子推荐不许静默截断：配置了 5 个种子就必须请求 5 次。
    # 旧代码写的是 `seeds[:3]`，第 4 篇之后收藏的论文永远拿不到相关推荐，
    # 界面上毫无提示——用户只会觉得「推荐怎么变少了」。
    if len(s2_calls) != 5:
        print(f"\n[FAIL] 只对 {len(s2_calls)} 个种子做了相关推荐（预期 5；"
              f"旧代码 seeds[:3] 会砍成 3）")
        ok = False
    if len(s2_calls) > _MAX_SEEDS:
        print(f"\n[FAIL] 种子请求数 {len(s2_calls)} 超过上限 {_MAX_SEEDS}，"
              f"本用例的前提不成立")
        ok = False

    print(f"\n本地库共 {total} 篇，统计新增 {stat['new']} 篇，回调 {len(events)} 次，"
          f"种子推荐 {len(s2_calls)} 次")
    print("结果:", "PASS" if ok else "FAIL")
    lib.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
