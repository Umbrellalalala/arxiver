"""OpenAlex 客户端（免费学术目录，240M+ 论文）—— 用于「月/年最热、最主要」.

按用户小方向关键词 + 时间区间，以引用数排序，代表该时期最重要的工作。
"""
from __future__ import annotations

import httpx

from ..errors import log, retry
from ..models import Paper

BASE = "https://api.openalex.org"


class OpenAlexClient:
    def __init__(self, mailto: str = "", timeout: float = 30.0):
        self._mailto = mailto
        self._t = timeout

    @retry(times=3, delay=3.0)
    def _get(self, url: str, params: dict) -> dict:
        r = httpx.get(url, params=params, timeout=self._t)
        r.raise_for_status()
        return r.json()

    def top_cited(self, keywords: list[str], from_date: str, to_date: str,
                  limit: int = 20) -> list[Paper]:
        """区间内与关键词相关、引用数最高的论文（需求 4：月/年最热）."""
        if not keywords:
            return []
        search = " OR ".join(k for k in keywords[:6])
        params = {
            "search": search,
            "filter": f"from_publication_date:{from_date},to_publication_date:{to_date}",
            "sort": "cited_by_count:desc",
            "per-page": limit,
        }
        if self._mailto:
            params["mailto"] = self._mailto
        try:
            data = self._get(f"{BASE}/works", params)
        except Exception as e:
            log.error("OpenAlex top_cited 失败: %s", e)
            return []
        out: list[Paper] = []
        for w in data.get("results") or []:
            ids = w.get("ids") or {}
            arxiv_id = ids.get("openalex", "").removeprefix("https://openalex.org/")
            authors = [
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])
            ]
            loc = w.get("primary_location") or {}
            src = loc.get("source") or {}
            is_arxiv = "arxiv" in (src.get("host_organization_name") or "").lower()
            out.append(Paper(
                arxiv_id=None if is_arxiv else None,  # OpenAlex 论文多数非 arXiv，保留 None 由下载层跳过
                title=w.get("title") or w.get("display_name") or "",
                abstract="",
                authors=[a for a in authors if a],
                published=(w.get("publication_date") or "")[:10],
                pdf_url=(w.get("open_access") or {}).get("oa_url", "") or "",
                abs_url=w.get("doi") or (w.get("id") or ""),
                citations=w.get("cited_by_count") or 0,
                source="openalex",
            ))
        return out
