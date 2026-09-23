"""HuggingFace Daily Papers 客户端（每日最热榜单，需求 4）.

接口文档: https://github.com/0x0is1/hf-papers-api-docs
GET {endpoint}/api/daily_papers?date=YYYY-MM-DD

**端点按顺序回退**：`huggingface.co` 在国内会被 DNS 污染到不可达的 IP
（实测解析到 104.244.46.63，那是 Twitter 的段），直连必然超时，
于是热榜既拿不到东西又要白等。镜像 `hf-mirror.com` 实测 1.7s 返回 200，
所以默认「官方 → 镜像」依次尝试。可用环境变量 `HF_ENDPOINT` 覆盖
（与 huggingface_hub 的约定一致；设了它就只用它，不再回退）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import httpx

from ..errors import log
from ..models import Paper

API_PATH = "/api/daily_papers"
DEFAULT_ENDPOINTS = ("https://huggingface.co", "https://hf-mirror.com")

# 连接超时必须单独设短：huggingface.co 在国内会被 DNS 污染到不可达的 IP，
# 此时 TCP 握手会一直耗到系统级超时（实测单次 ~41s）。用统一的 30s 超时的话，
# 3 次重试 + 退避 = 137s，而热榜是并行线程、主流程会等它——直接拖垮整次同步。
# 连不上就快速放弃：重试救不回「网络不通」，只会白等。
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 20.0


def _resolve_endpoints() -> tuple[str, ...]:
    """显式配置优先，否则「官方 → 镜像」。"""
    env = (os.environ.get("HF_ENDPOINT") or "").strip().rstrip("/")
    if env:
        return (env,)
    return DEFAULT_ENDPOINTS


class HFDailyClient:
    def __init__(self, timeout: float = READ_TIMEOUT,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 endpoints: tuple[str, ...] | None = None):
        self._t = timeout
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=connect_timeout,
        )
        self._endpoints = tuple(endpoints) if endpoints else _resolve_endpoints()

    def _get(self, date: str | None) -> list[dict]:
        """依次尝试各端点，第一个成功的即返回。

        这里**刻意不再叠加 `@retry` 退避重试**：端点回退本身就是重试，
        叠上去会让「全都不通」的情况从 20s 变成 69s（每个端点各重试 3 次）。
        热榜是锦上添花的源，宁可快速放弃。
        """
        params = {"date": date} if date else {}
        last_err: Exception | None = None
        for base in self._endpoints:
            try:
                r = httpx.get(base + API_PATH, params=params,
                              timeout=self._timeout, follow_redirects=True)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001 — 换下一个端点，最后统一抛
                last_err = e
                log.warning("热榜端点 %s 不可用：%s", base, e)
        assert last_err is not None
        raise last_err

    @staticmethod
    def _to_paper(item: dict) -> Paper | None:
        # 宽容解析：结构为 {"paper": {...}, "title": ..., "numUpvotes": n, ...}
        p = item.get("paper") or {}
        if not isinstance(p, dict):
            p = {}
        arxiv_id = (p.get("arxivId") or p.get("id") or "").strip()
        title = (item.get("title") or p.get("title") or "").strip()
        if not title:
            return None
        authors = p.get("authors") or []
        if isinstance(authors, list):
            author_names = [
                a.get("name") if isinstance(a, dict) else str(a) for a in authors
            ]
        else:
            author_names = []
        # publishedAt 在 paper 内为论文发布日期；摘要用 paper.summary
        published = (p.get("publishedAt") or item.get("publishedAt") or "")
        if isinstance(published, str) and len(published) >= 10:
            published = published[:10]
        try:
            upvotes = int(p.get("upvotes") or item.get("numUpvotes") or 0)
        except (TypeError, ValueError):
            upvotes = 0
        return Paper(
            arxiv_id=arxiv_id or None,
            title=title,
            abstract=(p.get("summary") or "").strip(),
            authors=author_names,
            published=published,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
            abs_url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
            upvotes=upvotes,
            source="hf_daily",
        )

    def get_daily(self, date: str | None = None, limit: int = 30) -> list[Paper]:
        """获取某日（默认最新一期）热门论文榜."""
        papers, _ok = self._get_daily_ex(date, limit)
        return papers

    def _get_daily_ex(self, date: str | None, limit: int) -> tuple[list[Paper], bool]:
        """带成功标志的取榜：返回 (论文列表, 是否真的拿到了数据).

        「接口挂了」和「那天没有论文」必须区分开——前者要立刻放弃剩余日期，
        否则月/年视图会逐天各重试 3 次，白白卡住十几分钟。
        """
        try:
            data = self._get(date)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (400, 404):
                # 那天根本没有榜单（当天还没发布、周末、未来日期都会 400），
                # 不是接口挂了。以前这算「接口不可用」直接中断整轮，结果每天
                # 早晨的热榜都贡献 0 篇；返回 ok=True 让上层继续往前一天试。
                log.info("热榜 %s 暂无榜单（HTTP %s），改试前一天", date, code)
                return [], True
            log.error("HF Daily Papers 获取失败: %s", e)
            return [], False
        except Exception as e:  # noqa: BLE001 — 连接类失败，交给上层中断剩余日期
            log.error("HF Daily Papers 获取失败: %s", e)
            return [], False
        if not isinstance(data, list):
            return [], False
        papers = []
        for item in data[:limit]:
            p = self._to_paper(item)
            if p:
                papers.append(p)
        return papers, True

    def get_recent_days(self, days: int = 1, limit: int = 30,
                        on_batch=None) -> list[Paper]:
        """抓取最近 N 天的榜单并合并去重.

        on_batch(papers, label)：每抓完一天就回调一次，便于边抓边更新界面。
        某天接口失败时直接停止（接口不可用时后续日期必然同样失败）。
        """
        seen: set[str] = set()
        out: list[Paper] = []
        today = datetime.now()
        for d in range(days):
            date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
            papers, ok = self._get_daily_ex(date, limit)
            if not ok:
                log.warning("热榜 %s 不可用，跳过剩余 %d 天", date, days - d - 1)
                break
            got: list[Paper] = []
            for p in papers:
                key = p.clean_id or p.title
                if key and key not in seen:
                    seen.add(key)
                    out.append(p)
                    got.append(p)
            if on_batch and got:
                try:
                    on_batch(got, f"热榜 {date}")
                except Exception as e:
                    log.warning("on_batch 回调失败（忽略）: %s", e)
        return out
