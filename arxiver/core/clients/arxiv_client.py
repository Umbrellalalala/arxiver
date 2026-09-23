"""arXiv 官方 API 客户端（基于开源库 arxiv，https://github.com/lukasschwab/arxiv）.

全进程共享同一个 arxiv.Client 实例以统一限速（官方要求 ≥3s/请求）。
API 被限速(429)时自动降级到 arXiv 官方 RSS 通道，保证每日推送不中断。
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable

import arxiv
import feedparser
import httpx

from ..errors import log, retry
from ..models import Paper

_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z][a-z-]+/\d{7}(?:v\d+)?)")

# arXiv RSS 的 description 形如 "arXiv:2609.12078v1 Announce Type: new  Abstract: ..."
# 也可能出现中文 "arXiv: 2609.12099v1公告类型：新摘要：..."（无空格粘连）或带空格变体。
# 关键：公告类型值(new/新)与 Abstract/摘要 标签之间可能无空格，不能用 \S+ 匹配值。
_ANNOUNCE_RE = re.compile(
    r"^arXiv\s*[：:]\s*\d{4}\.\d{4,5}(?:v\d+)?\s*"
    r"(?:Announce\s*Type|公告类型)\s*[：:]\s*"
    r"(?:new|replace|cross|withdraw|新|替换|交叉|撤回)\s*"
    r"(?:Abstract|摘要)\s*[：:]?\s*",
    re.IGNORECASE)
# 兜底：只要开头是 arXiv ID，剥掉 ID 及其后的非字母符号（含中文标点）
_ANNOUNCE_FALLBACK_RE = re.compile(
    r"^arXiv\s*[：:]\s*\d{4}\.\d{4,5}(?:v\d+)?\s*[^\w]*", re.IGNORECASE)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_id(raw: str) -> str:
    """从各种来源提取干净的 arXiv ID：oai:arXiv.org:2609.12078v1 -> 2609.12078v1."""
    m = _ID_RE.search(raw or "")
    return m.group(1) if m else ""


def clean_abstract(text: str) -> str:
    """去掉 RSS 通道残留的 'arXiv:xxx Announce Type: ... Abstract:' 前缀.

    兼容中英文公告类型、arXiv: 后有无空格、HTML 标签等变体。
    """
    s = (text or "").strip()
    s = _HTML_TAG_RE.sub("", s)  # feedparser 可能返回 HTML
    s = _ANNOUNCE_RE.sub("", s)
    if s.startswith("arXiv") or s.startswith("arxiv"):
        s = _ANNOUNCE_FALLBACK_RE.sub("", s)
    return s.strip()

_client: arxiv.Client | None = None
_client_lock = threading.Lock()

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Arxiver/0.1 "
                  "(mailto:research-assistant@example.com)"
}

# RSS 连接超时单独设短：跨境链路会卡握手（实测 cs.IR 出现过 ConnectTimeout，
# 而默认 30s 超时意味着白等 30s）。连不通就快点失败，交给上层重试。
_RSS_TIMEOUT = httpx.Timeout(connect=8.0, read=30.0, write=30.0, pool=8.0)
# 并发上限只用于「用户选了特别多方向」时避免开上百个连接；**不是**分类数上限
_MAX_RSS_WORKERS = 8
# 周/月/年视图的历史补抓走官方 API，全进程共享 3s 限速、必须串行，
# 每多一个分类就多 ~4s。这里给一个上限防止用户选了几十个方向时卡死，
# 但**必须**在上限生效时打日志说明丢了哪些分类——静默截断等于研究方向凭空消失。
# 8 覆盖常见配置（用户 5 个大方向展开 7 个分类）。
_MAX_BACKFILL_CATS = 8
# 关键词检索上限：每 4 个关键词一组、每组一次官方 API 请求（共享 3s 限速），
# 不设上限的话用户填几十个关键词会卡很久。同样地，被丢掉的关键词必须留日志。
_MAX_KEYWORDS = 8
# 按 ID 批量查元数据时，一次查询最多拼多少个 `id:xxx`（arXiv 官方 API 的查询串长度
# 限制）。超过要**分批**而不是截断——截断等于后面的收藏论文静默补不全。
_MAX_IDS_PER_QUERY = 50


def _get_client() -> arxiv.Client:
    """模块级单例：整个进程共享节流状态."""
    global _client
    with _client_lock:
        if _client is None:
            _client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=1)
        return _client


class ArxivClient:
    # ---- 内部 ----
    @retry(times=2, delay=5.0)
    def _search(self, query: str, max_results: int) -> list[arxiv.Result]:
        s = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        try:
            return list(_get_client().results(s))
        except Exception as e:
            log.warning("arXiv 查询失败: %s | query=%s", e, query)
            raise

    @retry(times=2, delay=10.0)
    def _fetch_rss(self, category: str, max_results: int) -> list[Paper]:
        """arXiv 官方 RSS 备用通道（API 被限速时兜底），每类最新约 100 篇."""
        url = f"https://rss.arxiv.org/rss/{category}"
        r = httpx.get(url, headers=_HEADERS, timeout=_RSS_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        out: list[Paper] = []
        for e in feed.entries[:max_results]:
            aid = _extract_id(e.get("id") or "")
            if not aid:
                continue
            pub = ""
            if e.get("published_parsed"):
                pub = time.strftime("%Y-%m-%d", e.published_parsed)
            out.append(Paper(
                arxiv_id=aid,
                title=(e.get("title") or "").strip(),
                abstract=clean_abstract((e.get("summary") or "").replace("\n", " ")),
                authors=[a.get("name", "") for a in e.get("authors", [])],
                categories=[t.get("term", "") for t in e.get("tags", [])],
                published=pub,
                updated=pub,
                pdf_url=f"https://arxiv.org/pdf/{aid}" if aid else "",
                abs_url=e.get("link", "") or f"https://arxiv.org/abs/{aid}",
                source="arxiv",
            ))
        return out

    @staticmethod
    def _to_paper(r: arxiv.Result, source: str = "arxiv") -> Paper:
        pub = r.published
        return Paper(
            arxiv_id=r.get_short_id(),
            title=r.title.strip(),
            abstract=(r.summary or "").strip().replace("\n", " "),
            authors=[a.name for a in r.authors],
            categories=list(r.categories),
            published=pub.strftime("%Y-%m-%d") if pub else "",
            updated=r.updated.strftime("%Y-%m-%d") if r.updated else "",
            pdf_url=r.pdf_url or "",
            abs_url=r.entry_id or "",
            source=source,
        )

    # ---- 对外 ----
    def fetch_recent(self, categories: list[str], days: int = 1,
                     max_results: int = 60,
                     on_batch: Callable[[list[Paper], str], None] | None = None) -> list[Paper]:
        """抓取指定分类最近 N 天新论文.

        RSS 优先：每个分类 1 个请求拿到最新 ~100 篇，速度快且不受 429 影响。
        days > 3（周/月/年视图）时额外用 API 补抓更大时间范围的历史论文。

        on_batch(papers, label)：每拿到一个分类的结果就立刻回调一次，
        供上层边抓边入库边推送到界面（不用等全部分类抓完）。
        """
        if not categories:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        def _emit(batch: list[Paper], label: str) -> None:
            """按时间窗过滤后立刻回调；整批都被过滤掉时保留原批（与原整体兜底语义一致）."""
            if not on_batch or not batch:
                return
            kept = [p for p in batch if (p.published or "") >= cutoff_str]
            try:
                on_batch(kept or batch, label)
            except Exception as e:
                log.warning("on_batch 回调失败（忽略）: %s", e)

        papers: list[Paper] = []
        # 各分类的 RSS 是互相独立的 HTTP 请求，并行抓取。
        # 实测串行时大分类（cs.AI/cs.CL）单个要 45s+，4 个分类串起来 60s+；
        # 并行后按「谁先回来谁先推」的顺序回调，首批出现得更早，总时长也短。
        # 注意：这里只并行 RSS；走 arXiv 官方 API 的 _search 必须保持串行
        # （官方要求 ≥3s/请求，共用限速状态）。
        #
        # **不要截断分类数**。这里原来写的是 `categories[:4]`，把排在后面的分类
        # 静默丢掉：用户配 5 个大方向会展开成 7 个分类，cs.LG / cs.CV / cs.MM
        # 直接一篇都抓不到，连日志都没有——等于两个研究方向每天同步都是空的。
        # 宁可多花十几秒也要抓全；`_MAX_RSS_WORKERS` 只限制并发连接数。
        cats = list(categories)
        workers = max(1, min(len(cats), _MAX_RSS_WORKERS))
        log.info("arXiv 分类抓取：%d 个分类（并发 %d）", len(cats), workers)

        def _fetch_rss_retry(cat: str) -> list[Paper]:
            """单分类失败重试一次。

            网络抖动是常态（实测 cs.IR 出现过 ConnectTimeout），一次失败就让
            整个研究方向消失，代价远大于重试一次的开销。
            """
            try:
                return self._fetch_rss(cat, max_results)
            except Exception as e:
                log.warning("RSS 获取失败 %s: %s，重试一次", cat, e)
                time.sleep(1.5)
                return self._fetch_rss(cat, max_results)

        if len(cats) > 1:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="arxiv-rss") as ex:
                futs = {ex.submit(_fetch_rss_retry, c): c for c in cats}
                for fut in as_completed(futs):
                    cat = futs[fut]
                    try:
                        got = fut.result()
                    except Exception as e:
                        log.error("RSS 获取失败 %s（已重试）: %s", cat, e)
                        continue
                    papers += got
                    _emit(got, f"arXiv {cat}")
        else:
            for cat in cats:
                try:
                    got = _fetch_rss_retry(cat)
                except Exception as e:
                    log.error("RSS 获取失败 %s（已重试）: %s", cat, e)
                    continue
                papers += got
                _emit(got, f"arXiv {cat}")
        if not papers:  # RSS 全挂 → API 兜底
            query = " OR ".join(f"cat:{c}" for c in categories)
            try:
                results = self._search(query, max_results)
                papers = [self._to_paper(r) for r in results]
                _emit(papers, "arXiv API 兜底")
            except Exception as e:
                log.error("arXiv 全部通道失败: %s", e)
        # 大范围（本周/本月/今年）：API 按日期区间补抓历史论文
        if days > 3:
            now = datetime.now(timezone.utc)
            dates = f"submittedDate:[{cutoff:%Y%m%d}0000 TO {now:%Y%m%d}2359]"
            per_cat = min(max(max_results, 100), 300)
            backfill = list(categories)[:_MAX_BACKFILL_CATS]
            dropped = list(categories)[_MAX_BACKFILL_CATS:]
            if dropped:
                log.warning(
                    "历史补抓分类数超过上限 %d，以下分类只会有 RSS 最近结果、"
                    "拿不到历史论文：%s", _MAX_BACKFILL_CATS, dropped)
            for cat in backfill:
                try:
                    results = self._search(f"cat:{cat} AND {dates}", per_cat)
                    got = [self._to_paper(r) for r in results]
                    papers += got
                    log.info("大范围补抓 %s：+%d 篇", cat, len(results))
                    _emit(got, f"arXiv {cat} 历史")
                except Exception as e:
                    log.warning("大范围补抓失败 %s（跳过，依赖 RSS 结果）: %s", cat, e)
        kept = [p for p in papers if (p.published or "") >= cutoff_str]
        return (kept or papers)[: max(max_results, 300)]

    def search_keywords(self, keywords: list[str], days: int = 7,
                        max_results: int = 30,
                        on_batch: Callable[[list[Paper], str], None] | None = None) -> list[Paper]:
        """按小方向关键词检索（标题+摘要）.

        关键词按每 4 个一组分多次请求，每请求回来就回调一次——
        单个请求要等 arXiv 3s 限速，分组能让结果更早出现在界面上。
        """
        if not keywords:
            return []
        # 被丢掉的关键词必须可见：用户加了第 9 个关键词却一直搜不到东西，
        # 是最难查的那类问题（旧代码 `keywords[:8]` 静默切掉，无任何提示）。
        if len(keywords) > _MAX_KEYWORDS:
            log.warning("关键词 %d 个超过上限 %d，本次跳过：%s",
                        len(keywords), _MAX_KEYWORDS, keywords[_MAX_KEYWORDS:])
        kws = [k for k in keywords[:_MAX_KEYWORDS] if k and k.strip()]
        if not kws:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
        papers: list[Paper] = []
        groups = [kws[i:i + 4] for i in range(0, len(kws), 4)]
        per_group = max(10, max_results // len(groups))
        for grp in groups:
            terms = " OR ".join(f'all:"{k}"' for k in grp)
            query = f"({terms}) AND submittedDate:[{cutoff:%Y%m%d}0000 TO 999912312359]"
            try:
                results = self._search(query, per_group)
            except Exception as e:
                log.error("search_keywords 失败（%s）: %s", grp, e)
                continue
            got = [self._to_paper(r) for r in results]
            papers += got
            if on_batch and got:
                try:
                    on_batch(got, f"关键词 {'/'.join(grp[:2])}")
                except Exception as e:
                    log.warning("on_batch 回调失败（忽略）: %s", e)
        return papers

    def fetch_by_ids(self, arxiv_ids: list[str]) -> list[Paper]:
        """按 ID 批量拉取元数据（用于收藏/种子论文补全）.

        注意：**目前没有调用方**（收藏/种子补全走的是 S2）。留着但不要让它带坑——
        `_MAX_IDS_PER_QUERY` 是「一次查询能拼多少个 id」，超过要**分批**，
        截断的话后面的收藏论文会静默补不全（同族 bug 见 `_MAX_KEYWORDS` 的注释）。
        """
        if not arxiv_ids:
            return []
        out: list[Paper] = []
        for i in range(0, len(arxiv_ids), _MAX_IDS_PER_QUERY):
            chunk = arxiv_ids[i:i + _MAX_IDS_PER_QUERY]
            query = " OR ".join(f'id:{x.split("v")[0]}' for x in chunk)
            try:
                results = self._search(query, len(chunk))
            except Exception as e:
                log.error("fetch_by_ids 第 %d 批失败: %s", i // _MAX_IDS_PER_QUERY + 1, e)
                continue
            out += [self._to_paper(r) for r in results]
        return out
