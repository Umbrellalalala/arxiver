"""引用数补充的取数顺序：必须按**分数**取前 N，不能按抓取顺序。

旧代码是：

```python
ids = [p.clean_id for p in list(seen.values()) if p.arxiv_id][:50]
```

`seen` 是**抓取顺序**（先 arXiv 分类、再关键词、再热榜……），所以「前 50 篇」
永远是**最早到达的那几个分类**。用户配 5 个大方向会展开成 7 个分类，
排在后面的 cs.LG / cs.CV / cs.AI / cs.CL 就永远拿不到引用数——
而界面里有「引用数」排序选项、卡片上也有「被引 N」徽标，
等于这几个方向的论文永远显示 0 被引、按引用数排序永远垫底。

这个测试构造「低分论文先到、高分论文后到」的场景，然后断言：
1. 传给 S2 的 ID 就是**分数最高的那 N 篇**；
2. **不是**抓取顺序的前 N 篇 —— 这一条是判别力所在，改回旧写法必挂。

运行: .venv/Scripts/python.exe tests/test_citation_order.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

isolated_home("citerank")   # 必须在 import arxiver.* 之前

import arxiver.core.pipeline as pipeline_mod                # noqa: E402
from arxiver.config import get_config                       # noqa: E402
from arxiver.core.library import Library                    # noqa: E402
from arxiver.core.models import Paper                       # noqa: E402
from arxiver.core.pipeline import Pipeline                  # noqa: E402

CAP = 2                     # 故意设小，好让截断真的发生
LOW_N = 4                   # 先到、低分
HIGH_N = 4                  # 后到、高分


def _paper(i: int, title: str) -> Paper:
    return Paper(
        arxiv_id=f"2609.{70000 + i}",
        title=title,
        abstract="generic abstract with no matching term",
        authors=["A. Author"],
        categories=["cs.CV"],
        published="2026-09-18",
        pdf_url=f"https://arxiv.org/pdf/2609.{70000 + i}",
        abs_url=f"https://arxiv.org/abs/2609.{70000 + i}",
        source="arxiv",
    )


def main() -> int:
    cfg = get_config()
    cfg.update({
        # 只留一个小方向关键词。大方向故意用一个不匹配任何论文分类的名字，
        # 这样分数差异**只**由关键词命中决定，场景可预测。
        "major_fields": ["人工智能"],
        "minor_topics": {"人工智能": ["vision transformer"]},
        "seed_papers": [],
        "auto_download": False,
    })

    lib = Library()
    pipe = Pipeline(cfg, lib)

    low = [_paper(i, f"Low relevance paper {i}") for i in range(LOW_N)]
    high = [_paper(100 + i, f"Vision transformer study {i}") for i in range(HIGH_N)]

    def fake_fetch_recent(categories, days=1, max_results=80, on_batch=None):
        """先到的一批：低分（标题不含关键词）。"""
        if on_batch:
            on_batch(low, "arXiv 低分")
        return list(low)

    def fake_search_keywords(keywords, days=7, max_results=30, on_batch=None):
        """后到的一批：高分（标题命中关键词 → +12）。"""
        if on_batch:
            on_batch(high, "关键词 高分")
        return list(high)

    def fake_hf(days=1, limit=40, on_batch=None):
        return []

    pipe.arxiv.fetch_recent = fake_fetch_recent
    pipe.arxiv.search_keywords = fake_search_keywords
    pipe.hf.get_recent_days = fake_hf

    got_ids: list[str] = []

    class _FakeS2:
        def related(self, sid, limit=10):
            return []

        def citations(self, ids):
            got_ids.extend(ids)
            return {}

    pipe._s2 = _FakeS2()

    old_cap = pipeline_mod._MAX_CITATION_IDS
    pipeline_mod._MAX_CITATION_IDS = CAP
    try:
        stat = pipe.sync(days=1, auto_download=False)
    finally:
        pipeline_mod._MAX_CITATION_IDS = old_cap

    low_ids = [p.clean_id for p in low]
    high_ids = [p.clean_id for p in high]
    first_arrived = low_ids[:CAP]          # 旧写法会取这个
    expected_top = high_ids[:CAP]          # 新写法应该取这个

    print(f"候选 {stat['papers']} 篇（低分 {LOW_N} + 高分 {HIGH_N}），上限 {CAP}")
    print(f"  抓取顺序前 {CAP}（旧行为）: {first_arrived}")
    print(f"  分数最高 {CAP}（新行为）  : {expected_top}")
    print(f"  实际传给 S2 的           : {got_ids}")

    ok = True

    def check(cond, label, detail=""):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + label
              + (f"   {detail}" if detail and not cond else ""))
        if not cond:
            ok = False

    check(len(got_ids) == CAP, f"只查了 {CAP} 篇（上限生效）", str(len(got_ids)))
    check(set(got_ids) == set(expected_top),
          "取的是分数最高的那几篇", f"{sorted(got_ids)} vs {sorted(expected_top)}")
    # 判别力：这一条挂了就说明有人把顺序改回抓取顺序
    check(set(got_ids) != set(first_arrived),
          "不是抓取顺序的前几篇（改回旧写法这条必挂）",
          f"{sorted(got_ids)} == {sorted(first_arrived)}")
    check(all(i in high_ids for i in got_ids),
          "拿到的全部是关键词命中的高分论文", str(got_ids))

    print("\n结果:", "PASS" if ok else "FAIL")
    lib.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
