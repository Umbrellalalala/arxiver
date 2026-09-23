"""客户端层单元测试：并发抓取 + 上游异常数据容错。

覆盖三处真实踩过的坑：
1. Semantic Scholar 的 batch 接口对**查不到的 ID 会返回 null**，
   旧代码直接 `item.get(...)` → AttributeError → 被上层 except 吞成
   「引用数补充失败（忽略）」，导致引用数从来没补上过。
2. S2 返回的 ArXiv ID 可能带版本号，要和库里的 clean_id 对齐。
3. arXiv 各分类的 RSS 应该并行抓（串行时 4 个大分类要 60s+），
   而且**不许截断分类数**（旧代码 `categories[:4]` 会静默丢掉后 3 个分类）。
4. 热榜接口挂掉时不能逐天各重试 3 次（月视图会卡十几分钟）。

运行: .venv/Scripts/python.exe tests/test_clients_unit.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

isolated_home("unit")   # 必须在 import arxiver.* 之前

from arxiver.core.clients import HFDailyClient                     # noqa: E402
from arxiver.core.clients import semantic_scholar as s2mod         # noqa: E402
from arxiver.core.clients.arxiv_client import ArxivClient          # noqa: E402
from arxiver.core.models import Paper                              # noqa: E402

OK = True


def check(cond, label, detail=""):
    global OK
    print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        OK = False


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


# ---------------- 1. S2 citations 容错 ----------------
def test_s2_citations_null_entries():
    print("== 1. S2 citations：数组里的 null 条目 ==")
    seen_body = {}

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        seen_body["ids"] = json["ids"]
        return FakeResp([
            None,                                                        # 查不到
            {"externalIds": {"ArXiv": "2609.10001v2"}, "citationCount": 42},  # 带版本号
            {"externalIds": {}, "citationCount": 7},                      # 没有 ArXiv
            None,
        ])

    orig = s2mod.httpx.post
    s2mod.httpx.post = fake_post
    try:
        c = s2mod.SemanticScholarClient()
        out = c.citations(["2609.10001", "2609.10002"])
    finally:
        s2mod.httpx.post = orig

    check(out == {"2609.10001": 42}, "跳过 null、剥掉版本号", str(out))
    check(seen_body["ids"] == ["arXiv:2609.10001", "arXiv:2609.10002"], "请求体格式正确")


# ---------------- 1b. S2 citations 超过接口上限要分批，不能截断 ----------------
def test_s2_citations_batches_beyond_limit():
    """S2 `paper/batch` 一次最多 100 个 ID —— 这是**接口限制**，超过必须分批。

    旧代码是 `arxiv_ids[:100]`：第 101 个之后的论文永远拿不到引用数，
    而界面里有「引用数」排序 + 「被引 N」徽标，等于它们永远显示 0 被引、
    排序永远垫底，日志里却一片干净。

    判别力：`sent[-1]` 那条断言在旧写法下必挂（旧代码根本不会请求第 101 个）。
    """
    print("\n== 1b. S2 citations：超过 100 个 ID 要分批（不是截断）==")
    batches: list[list[str]] = []

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        ids = json["ids"]
        batches.append(ids)
        return FakeResp([
            {"externalIds": {"ArXiv": i.replace("arXiv:", "")}, "citationCount": 1}
            for i in ids
        ])

    orig = s2mod.httpx.post
    s2mod.httpx.post = fake_post
    try:
        ids = [f"2609.{i:05d}" for i in range(250)]
        out = s2mod.SemanticScholarClient().citations(ids)
    finally:
        s2mod.httpx.post = orig

    check(len(batches) == 3, "250 个 ID 分成 3 批（100/100/50）", f"实际 {len(batches)} 批")
    check(all(len(b) <= s2mod._S2_BATCH for b in batches),
          f"每批都不超过接口上限 {s2mod._S2_BATCH}", str([len(b) for b in batches]))
    sent = [i for b in batches for i in b]
    check(len(sent) == 250 and len(set(sent)) == 250,
          "250 个 ID 全都发出去了（一个都没截断）", f"实际发出 {len(sent)} 个")
    check(sent[-1] == "arXiv:2609.00249",
          "最后一个 ID 也在最后一批里（旧写法这条必挂）", sent[-1] if sent else "无")
    check(len(out) == 250, "250 篇都拿到了引用数", str(len(out)))


def test_s2_citations_stops_on_429():
    """一批撞 429 就停手，但要**说清楚**剩下多少篇没拿到引用数。

    免费池是共享的，429 随机出现；继续硬打只会更糟。
    判别力：旧写法只有一次请求，根本不会出现「第 2 批中断」这句话。
    """
    print("\n== 1c. S2 citations：某批 429 就停手，并说明剩余篇数 ==")
    import logging

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    calls = []

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        calls.append(json["ids"])
        if len(calls) == 1:                      # 第一批成功
            return FakeResp([{"externalIds": {"ArXiv": i.replace("arXiv:", "")},
                              "citationCount": 5} for i in json["ids"]])
        return FakeResp(None, status=429)        # 第二批开始限速

    orig = s2mod.httpx.post
    s2mod.httpx.post = fake_post
    h = Collect()
    s2mod.log.addHandler(h)
    try:
        ids = [f"2609.{i:05d}" for i in range(250)]
        out = s2mod.SemanticScholarClient().citations(ids)
    finally:
        s2mod.httpx.post = orig
        s2mod.log.removeHandler(h)

    check(len(calls) == 2, "撞 429 后没有继续打第 3 批", f"实际打了 {len(calls)} 批")
    check(len(out) == 100, "第一批的结果保住了（不是全丢）", str(len(out)))
    check(any("中断" in m and "剩余" in m for m in h.msgs),
          "日志说明了剩余多少篇没拿到", str(h.msgs[-2:]))


# ---------------- 2. S2 related 容错 ----------------
def test_s2_related_null_entries():
    print("\n== 2. S2 related：推荐列表里的 null 条目 ==")

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResp({
            "recommendedPapers": [
                None,
                {"title": "Good paper", "externalIds": {"ArXiv": "2609.20001"},
                 "authors": [{"name": "A"}], "year": 2026},
                {"title": "", "externalIds": {}},      # 无标题，应被跳过
                None,
            ]
        })

    orig = s2mod.httpx.get
    s2mod.httpx.get = fake_get
    try:
        out = s2mod.SemanticScholarClient().related("2609.10001", limit=10)
    finally:
        s2mod.httpx.get = orig

    check(len(out) == 1 and out[0].title == "Good paper", "跳过 null / 空标题", str([p.title for p in out]))


# ---------------- 3. arXiv RSS 并行 ----------------
def test_arxiv_rss_parallel():
    """并行 + **不许截断分类数**。

    这里刻意用 7 个分类：旧代码写的是 `categories[:4]`，会把第 5 个之后的
    分类静默丢掉。用户配 5 个大方向会展开成 7 个分类，cs.LG / cs.CV / cs.MM
    直接一篇都抓不到，连日志都没有——等于两个研究方向每天同步都是空的。
    """
    print("\n== 3. arXiv 各分类 RSS 并行抓取（且不许截断） ==")
    PER_CAT = 0.5
    cats_seen = []

    def fake_rss(category, max_results):
        cats_seen.append(category)
        time.sleep(PER_CAT)
        return [Paper(arxiv_id=f"2609.{30000 + len(cats_seen)}",
                      title=f"Paper from {category}", published="2026-09-18")]

    ac = ArxivClient()
    ac._fetch_rss = fake_rss
    batches = []
    want = ["cs.MA", "cs.AI", "cs.CL", "cs.IR", "cs.LG", "cs.CV", "cs.MM"]
    t0 = time.time()
    out = ac.fetch_recent(want, days=1,
                          max_results=10, on_batch=lambda b, l: batches.append((l, len(b))))
    elapsed = time.time() - t0

    check(len(out) == len(want), f"{len(want)} 个分类都拿到了（不许截断）", str(len(out)))
    check(sorted(cats_seen) == sorted(want),
          "每个分类都真的请求了（旧代码会砍掉后 3 个）", str(cats_seen))
    check(len(batches) == len(want), "每个分类各回调一次", str(len(batches)))
    check(elapsed < PER_CAT * 2.5, f"并行耗时 {elapsed:.2f}s（串行应 ≥ {PER_CAT * len(want):.1f}s）")


# ---------------- 3b. 单个分类失败要重试 ----------------
def test_arxiv_rss_retry_once():
    """网络抖动不该让一整个研究方向静默消失。

    实测 rss.arxiv.org 会卡握手（cs.IR 出现过 ConnectTimeout）。
    旧代码在 as_completed 里 `except: continue`，一次失败就整类丢掉、无重试。
    """
    print("\n== 3b. 单个分类失败重试一次 ==")
    calls = {"cs.IR": 0}

    def flaky(category, max_results):
        calls[category] = calls.get(category, 0) + 1
        if category == "cs.IR" and calls[category] == 1:
            raise TimeoutError("模拟握手超时")
        return [Paper(arxiv_id=f"2609.{40000 + calls[category]}",
                      title=f"Paper from {category}", published="2026-09-18")]

    ac = ArxivClient()
    ac._fetch_rss = flaky
    out = ac.fetch_recent(["cs.AI", "cs.IR"], days=1, max_results=10)

    check(calls["cs.IR"] == 2, "失败后重试了一次", str(calls))
    check(len(out) == 2, "重试成功后该分类的论文没有丢", str(len(out)))

    # 一直失败的话：重试 2 次后放弃，但不能影响其它分类
    calls.clear()

    def always_fail(category, max_results):
        calls[category] = calls.get(category, 0) + 1
        if category == "cs.IR":
            raise TimeoutError("一直失败")
        return [Paper(arxiv_id="2609.50001", title="ok", published="2026-09-18")]

    ac2 = ArxivClient()
    ac2._fetch_rss = always_fail
    out2 = ac2.fetch_recent(["cs.AI", "cs.IR"], days=1, max_results=10)
    check(calls["cs.IR"] == 2, "一直失败时重试 1 次即放弃（共 2 次请求）", str(calls))
    check(len(out2) == 1, "坏分类不影响好分类", str(len(out2)))


# ---------------- 3c. 大范围历史补抓也不许静默截断 ----------------
def test_arxiv_backfill_not_truncated():
    """周/月/年视图的历史补抓走官方 API，有并发上限，但上限生效必须留日志。

    旧代码这里是 `for cat in categories[:4]`，和第 3 节同一个病：
    7 个分类里后 3 个（cs.LG/cs.CV/cs.MM）在周/月/年视图下拿不到历史论文，
    而且一句日志都没有。
    """
    print("\n== 3c. 大范围历史补抓：分类不被静默截断 ==")
    import logging

    import arxiver.core.clients.arxiv_client as acmod

    cats = ["cs.MA", "cs.AI", "cs.CL", "cs.IR", "cs.LG", "cs.CV", "cs.MM"]

    def make_client(searched):
        ac = ArxivClient()
        ac._fetch_rss = lambda cat, n: [
            Paper(arxiv_id="2609.60001", title=f"RSS {cat}", published="2026-09-18")]
        ac._search = lambda query, n: (searched.append(query.split("cat:")[1].split(" ")[0]), [])[1]
        return ac

    searched: list[str] = []
    make_client(searched).fetch_recent(cats, days=7, max_results=50)
    check(len(searched) == len(cats),
          f"{len(cats)} 个分类都做了历史补抓（旧代码只抓前 4 个）", str(searched))
    check(searched == cats, "补抓顺序与分类顺序一致", str(searched))
    check(len(cats) <= acmod._MAX_BACKFILL_CATS,
          f"上限 {acmod._MAX_BACKFILL_CATS} 能覆盖这 {len(cats)} 个分类")

    # 真的超过上限时：只抓上限个，但必须留下日志（不能像旧代码那样闷声丢）
    class Collect(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    h = Collect()
    acmod.log.addHandler(h)
    try:
        many = [f"cs.{c}" for c in "ABCDEFGHIJKLMNOPQ"]
        over: list[str] = []
        make_client(over).fetch_recent(many, days=7, max_results=50)
    finally:
        acmod.log.removeHandler(h)

    check(len(over) == acmod._MAX_BACKFILL_CATS,
          f"超上限时只补抓 {acmod._MAX_BACKFILL_CATS} 个", str(len(over)))
    check(any("历史补抓分类数超过上限" in m for m in h.msgs),
          "超上限时留下了日志（不是静默丢）", str(h.msgs[-2:]))


# ---------------- 3d. 关键词超过上限要留日志 ----------------
def test_keywords_cap_is_logged():
    """关键词上限存在（每 4 个一组、走 3s 限速的官方 API），但丢东西必须可见。

    旧代码 `keywords[:8]` 静默切掉——用户加了第 9 个关键词却一直搜不到东西，
    是最难查的那类问题。
    """
    print("\n== 3d. 关键词超过上限：留日志而不是静默丢 ==")
    import logging

    import arxiver.core.clients.arxiv_client as acmod

    class Collect(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    ac = ArxivClient()
    queries: list[str] = []

    def fake_search(query, n):
        queries.append(query)
        return []

    ac._search = fake_search

    kws = [f"topic-{i}" for i in range(11)]
    h = Collect()
    acmod.log.addHandler(h)
    try:
        ac.search_keywords(kws, days=7, max_results=40)
    finally:
        acmod.log.removeHandler(h)

    want_groups = -(-acmod._MAX_KEYWORDS // 4)   # 每 4 个关键词一组
    check(len(queries) == want_groups,
          f"11 个关键词被截到上限 {acmod._MAX_KEYWORDS} 个，分 {want_groups} 组请求",
          str(len(queries)))
    check(any("关键词" in m and "超过上限" in m for m in h.msgs),
          "超上限时留下了日志", str(h.msgs[-2:]))
    check(any("topic-8" in m for m in h.msgs),
          "日志里点名了被跳过的关键词", str(h.msgs[-2:]))

    # 没超上限时不该有噪音日志
    h2 = Collect()
    acmod.log.addHandler(h2)
    try:
        ac.search_keywords(kws[:8], days=7, max_results=40)
    finally:
        acmod.log.removeHandler(h2)
    check(not any("超过上限" in m for m in h2.msgs), "没超上限时不打日志", str(h2.msgs))


# ---------------- 4. 热榜失败快速放弃 ----------------
def test_hf_fail_fast():
    print("\n== 4. 热榜接口挂掉：不逐天重试 ==")
    calls = []

    def boom(date=None):
        calls.append(date)
        raise RuntimeError("502 Bad Gateway")

    hf = HFDailyClient()
    hf._get = boom
    t0 = time.time()
    out = hf.get_recent_days(days=30, limit=20)
    elapsed = time.time() - t0

    check(out == [], "返回空列表")
    check(len(calls) == 1, f"只请求了 1 次（旧逻辑 30 次），实际 {len(calls)} 次")
    check(elapsed < 2.0, f"立刻放弃（{elapsed:.2f}s）")


# ---------------- 5. 热榜逐天回调 ----------------
def test_hf_per_day_batch():
    print("\n== 5. 热榜逐天回调 ==")

    def fake_get(date=None):
        return [{"title": f"HF paper {date}", "paper": {
            "arxivId": f"2609.4{abs(hash(date)) % 9000 + 1000}",
            "summary": "abs", "publishedAt": date}}]

    hf = HFDailyClient()
    hf._get = fake_get
    batches = []
    out = hf.get_recent_days(days=3, limit=20, on_batch=lambda b, l: batches.append((l, len(b))))

    check(len(out) == 3, "3 天共 3 篇", str(len(out)))
    check(len(batches) == 3, "每天各回调一次", str(len(batches)))
    check(all("热榜" in l for l, _ in batches), "回调 label 带日期")


# ---------------- 6. 热榜连接超时必须够短 ----------------
def test_hf_connect_timeout_is_short():
    """huggingface.co 在国内会被 DNS 污染到不可达 IP，TCP 握手要耗到系统级超时。

    实测（无代理直连，2026-09-18）：用统一 30s 超时时单次请求 ~41s，
    3 次重试 + 退避共 **137.5s**；而热榜是并行线程、主流程会等它，
    等于把整次同步从 50s 拖到 137s。
    把 connect 单独压到 5s 后降到 41.1s，低于 arXiv 的耗时，不再拖后腿。

    这条断言就是防止有人把超时改回统一值。
    """
    print("\n== 6. 热榜连接超时够短（否则会拖垮整次同步）==")
    hf = HFDailyClient()
    t = hf._timeout
    check(t.connect is not None and t.connect <= 5.0,
          f"connect 超时 <= 5s，实际 {t.connect}")
    check(t.read is not None and t.read >= 10.0,
          f"read 超时保留余量（>=10s），实际 {t.read}")
    check(t.connect < t.read, "connect 必须明显短于 read")

    # 默认构造出来的 client 必须真的用上这个超时对象
    import inspect
    src = inspect.getsource(HFDailyClient._get)
    check("self._timeout" in src, "_get 用的是分离超时对象，不是裸的 float")


# ---------------- 7. 热榜端点回退（官方连不通时走镜像） ----------------
def test_hf_endpoint_fallback():
    """huggingface.co 在国内被 DNS 污染，必须能自动回退到镜像。

    实测（无代理，2026-09-18）：官方端点 10s 超时，镜像 1.7s 返回 200。
    没有回退的话热榜永远是空的，而且还要白等。
    """
    print("\n== 7. 热榜端点回退 ==")
    import httpx

    from arxiver.core.clients import hf_daily as mod

    tried: list[str] = []

    def fake_get(url, **kw):
        tried.append(url)
        if "huggingface.co" in url and "mirror" not in url:
            raise httpx.ConnectTimeout("blocked")
        return httpx.Response(200, json=[{"title": "From mirror", "paper": {
            "arxivId": "2609.11111", "summary": "a", "publishedAt": "2026-09-18"}}],
            request=httpx.Request("GET", url))

    orig = httpx.get
    httpx.get = fake_get
    try:
        c = HFDailyClient()
        check(len(c._endpoints) >= 2, "默认配置了多个端点（官方 + 镜像）",
              str(c._endpoints))
        papers = c.get_daily("2026-09-18", limit=10)
        check(len(tried) == 2, "两个端点都试了", str(tried))
        check("huggingface.co" in tried[0], "先试官方端点", tried[0] if tried else "")
        check(any("mirror" in u for u in tried), "回退到了镜像")
        check(len(papers) == 1 and papers[0].title == "From mirror",
              "镜像返回的数据被正确解析", str([p.title for p in papers]))

        # 显式设了 HF_ENDPOINT 就只用它，不再回退
        os.environ["HF_ENDPOINT"] = "https://my.proxy.example/"
        try:
            c2 = HFDailyClient()
            check(c2._endpoints == ("https://my.proxy.example",),
                  "HF_ENDPOINT 生效且去掉末尾斜杠", str(c2._endpoints))
        finally:
            os.environ.pop("HF_ENDPOINT", None)
    finally:
        httpx.get = orig


def main() -> int:
    test_s2_citations_null_entries()
    test_s2_citations_batches_beyond_limit()
    test_s2_citations_stops_on_429()
    test_s2_related_null_entries()
    test_arxiv_rss_parallel()
    test_arxiv_rss_retry_once()
    test_arxiv_backfill_not_truncated()
    test_keywords_cap_is_logged()
    test_hf_fail_fast()
    test_hf_per_day_batch()
    test_hf_connect_timeout_is_short()
    test_hf_endpoint_fallback()
    print("\n结果:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
