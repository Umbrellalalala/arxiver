"""Semantic Scholar API 客户端（相关论文推荐 + 引用数）.

免费；无 key 限速较严（共享池），可在设置中填 API key 提速。

**别在这里加客户端限速（试过，回滚了）**：无 key 的 429 是**随机**的，
因为免费池是所有无 key 用户共享的，跟你自己的请求速率基本无关。实测 8 个种子：
- 主动限速到 1.2s 间隔：两次测量分别 40.1s / 32.9s，429 撞 3 次 / 2 次
- 不限速：两次测量分别 18.1s / 41.5s，429 撞 0 次 / 3 次

**两次结论相反 → 方差由 429 主导，限速只是白加 8.4s 固定延迟**（8 次请求 × 1.2s）。
每次 429 的代价是 `delay=5.0` 起步（见 `_get`），这才是种子阶段耗时的大头。
"""
from __future__ import annotations

import httpx

from ..errors import log, retry
from ..models import Paper

BASE = "https://api.semanticscholar.org"

# S2 的 `paper/batch` 一次最多收 100 个 ID。这是**接口限制**，不是「只需要 100 个」，
# 所以超过就分批发（见 `citations`），绝不能截断。
_S2_BATCH = 100


class SemanticScholarClient:
    def __init__(self, api_key: str = "", timeout: float = 30.0):
        self._key = api_key
        self._t = timeout

    def _headers(self) -> dict:
        return {"x-api-key": self._key} if self._key else {}

    @retry(times=3, delay=5.0)
    def _get(self, url: str, params: dict) -> dict | list:
        r = httpx.get(url, params=params, headers=self._headers(), timeout=self._t)
        if r.status_code == 429:
            raise ConnectionError("Semantic Scholar 限速(429)")
        r.raise_for_status()
        return r.json()

    def related(self, arxiv_id: str, limit: int = 10) -> list[Paper]:
        """基于种子论文的个性化推荐（需求 4：与用户小方向密切相关）."""
        clean = arxiv_id.split("v")[0]
        url = f"{BASE}/recommendations/v1/papers/forpaper/arXiv:{clean}"
        params = {
            "limit": limit,
            "fields": "title,abstract,externalIds,url,year,authors",
        }
        try:
            data = self._get(url, params)
        except Exception as e:
            log.error("S2 related(%s) 失败: %s", clean, e)
            return []
        if not isinstance(data, dict):
            return []
        out: list[Paper] = []
        for item in data.get("recommendedPapers") or []:
            if not isinstance(item, dict):
                continue          # S2 对查不到的条目会返回 null
            ext = (item.get("externalIds") or {})
            arxiv_id = ext.get("ArXiv")
            title = item.get("title") or ""
            if not title:
                continue
            authors = [a.get("name", "") for a in (item.get("authors") or [])]
            out.append(Paper(
                arxiv_id=arxiv_id,
                title=title,
                abstract=item.get("abstract") or "",
                authors=authors,
                published=str(item.get("year") or ""),
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
                abs_url=item.get("url") or "",
                source="s2",
            ))
        return out

    def citations(self, arxiv_ids: list[str]) -> dict[str, int]:
        """批量查询引用数（用于「最热/最主要」排序）.

        注意：S2 的 batch 接口对**查不到的 ID 会在数组里返回 null**
        （如 `[null, {...}, null]`）。之前这里直接 `item.get(...)`，
        一遇到 null 就抛 AttributeError，而调用方的 except 会把它吞成
        「引用数补充失败（忽略）」—— 结果就是引用数**从来没补上过**。

        `_S2_BATCH` 是**接口一次能收多少个**，不是「只需要这么多」——超过就分批发，
        **绝不能截断**。截断的后果是排在后面的论文永远拿不到引用数，而界面里有
        「引用数」排序和「被引 N」徽标，等于它们永远显示 0 被引、排序永远垫底，
        日志里却一片干净。（同族 bug 见 `pipeline._MAX_CITATION_IDS` 的注释。）
        """
        if not arxiv_ids:
            return {}
        out: dict[str, int] = {}
        for i in range(0, len(arxiv_ids), _S2_BATCH):
            chunk = arxiv_ids[i:i + _S2_BATCH]
            got = self._citations_batch(chunk)
            if got is None:
                # 429 或网络错误：别再继续打了（免费池限速时继续只会更糟），
                # 但要**点名说清楚**有多少篇本次没拿到
                rest = len(arxiv_ids) - i - len(chunk)
                log.warning("S2 引用查询在第 %d 批中断，剩余 %d 篇本次没有引用数",
                            i // _S2_BATCH + 1, rest + len(chunk))
                break
            out.update(got)
        return out

    def _citations_batch(self, arxiv_ids: list[str]) -> dict[str, int] | None:
        """查一批（≤ `_S2_BATCH` 个 ID）。返回 None 表示这批没查成（429/网络错误）."""
        body = {"ids": [f"arXiv:{i.split('v')[0]}" for i in arxiv_ids]}
        try:
            r = httpx.post(
                f"{BASE}/graph/v1/paper/batch",
                params={"fields": "externalIds,citationCount"},
                json=body,
                headers=self._headers(),
                timeout=self._t,
            )
            if r.status_code == 429:
                log.warning("S2 引用查询限速，本次跳过")
                return None
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("S2 citations 失败: %s", e)
            return None
        if not isinstance(data, list):
            return None
        out: dict[str, int] = {}
        skipped = 0
        for item in data:
            if not isinstance(item, dict):   # 查不到的条目是 null
                skipped += 1
                continue
            ext = (item.get("externalIds") or {})
            aid = ext.get("ArXiv")
            if aid:
                # S2 返回的 ArXiv ID 可能带版本号，统一去掉，才能和库里的
                # clean_id（无版本号）对上
                out[aid.split("v")[0]] = item.get("citationCount") or 0
        if skipped:
            log.info("S2 引用查询：%d/%d 条未收录（跳过）", skipped, len(data))
        return out
