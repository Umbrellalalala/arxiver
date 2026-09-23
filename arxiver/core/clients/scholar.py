"""学术搜索客户端：期刊/会议论文（Crossref 数据源），排除 arXiv，标注 CCF 类别.

谷歌学术(429 反爬)、DBLP(Anubis 挑战)、OpenAlex(每日配额) 在大批量抓取时
都不可靠；Crossref 是 DOI 注册机构，免费、稳定、无需 key，覆盖几乎所有期刊
与会议（IEEE/ACM/Springer/Elsevier 等出版商的会议论文均有 DOI）。
"""
from __future__ import annotations

import httpx

from ..ccf_venues import CCF_CONF, CCF_JOURN
from ..errors import log

__all__ = ["ScholarClient", "ccf_rank"]

_API = "https://api.crossref.org/works"
_MAILTO = "research-assistant@example.com"

# 预印本/arXiv 相关来源关键词（排除）
_EXCLUDE_VENUE = (
    "arxiv", "preprint", "ssrn", "research square", "biorxiv", "medrxiv",
    "chemrxiv", "techrxiv",
)


def ccf_rank(venue: str) -> str:
    """匹配 CCF 推荐目录，返回 A/B/C，未命中返回空字符串."""
    if not venue:
        return ""
    v = venue.strip().lower()
    # 1. 全称匹配（双向包含，容忍 "IEEE/CVF ..." 与目录全称的差异）
    for table in (CCF_CONF, CCF_JOURN):
        for _abbr, (full, rank) in table.items():
            f = (full or "").lower()
            if not f:
                continue
            if f == v or f in v or v in f:
                return rank
    # 2. 简称精确匹配（≥3 字符，避免 TC/SC 等短词误伤）
    up = venue.strip().upper()
    if len(up) >= 3:
        for table in (CCF_CONF, CCF_JOURN):
            if up in table:
                return table[up][1]
    return ""


class ScholarClient:
    """搜索期刊/会议论文（Crossref），排除 arXiv 预印本，标注 CCF 类别."""

    def search(self, query: str, limit: int = 20, since_year: int | None = None,
               page: int = 0) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        rows = min(max(int(limit), 1), 50)
        offset = max(int(page), 0) * rows
        params = {
            "query.title": query,
            "rows": rows,
            "offset": offset,
            "sort": "relevance",
            "select": "DOI,title,container-title,published,is-referenced-by-count,type,author",
            "mailto": _MAILTO,
        }
        if since_year:
            params["filter"] = f"from-pub-date:{int(since_year)}-01-01"
        try:
            r = httpx.get(_API, params=params, timeout=30.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("学术搜索失败: %s", e)
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for w in data.get("message", {}).get("items", []):
            title = (w.get("title") or [""])[0].strip()
            venue = (w.get("container-title") or [""])[0].strip()
            if not title:
                continue
            if w.get("type") == "posted-content":  # 预印本
                continue
            doi = w.get("DOI", "")
            if doi:
                if doi in seen:
                    continue
                seen.add(doi)
            vl = venue.lower()
            if any(k in vl for k in _EXCLUDE_VENUE):  # arXiv 等预印本来源
                continue
            year = ""
            dp = (w.get("published") or {}).get("date-parts") or [[None]]
            if dp and dp[0] and dp[0][0]:
                year = str(dp[0][0])
            authors = []
            for a in (w.get("author") or [])[:6]:
                name = f"{(a.get('given') or '')} {(a.get('family') or '')}".strip()
                if name:
                    authors.append(name)
            doi = w.get("DOI", "")
            out.append({
                "title": title,
                "venue": venue,
                "year": year,
                "authors": authors,
                "citations": w.get("is-referenced-by-count") or 0,
                "doi": doi,
                "ccf": ccf_rank(venue),
                "url": f"https://doi.org/{doi}" if doi else "",
                "source": "scholar",
                "type": w.get("type", ""),
            })
        return out
