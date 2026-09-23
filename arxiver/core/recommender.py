"""推荐打分：把多来源论文按用户画像排序（需求 4 的核心）."""
from __future__ import annotations

import math

from ..config import Config
from .models import Paper
from .profile import match_keywords, match_major

__all__ = ["score", "rank"]


def score(paper: Paper, cfg: Config) -> float:
    """综合打分：
    - 大方向命中：+30
    - 标题关键词命中：每个 +12；摘要命中：每个 +6（上限 50）
    - 热度（HF upvotes）：0~10（log 缩放）
    - 引用数：0~10（log 缩放）
    """
    s = 0.0
    majors = match_major(paper, cfg)
    if majors:
        s += 30.0

    title_l = paper.title.lower()
    abstract_l = paper.abstract.lower()
    kw_score = 0.0
    for kw in cfg.keywords():
        k = kw.strip().lower()
        if not k:
            continue
        if k in title_l:
            kw_score += 12.0
        elif k in abstract_l:
            kw_score += 6.0
    s += min(kw_score, 50.0)

    if paper.upvotes > 0:
        s += min(math.log10(paper.upvotes + 1) * 4, 10.0)
    if paper.citations > 0:
        s += min(math.log10(paper.citations + 1) * 4, 10.0)
    return round(s, 2)


def rank(papers: list[Paper], cfg: Config, limit: int = 20) -> list[Paper]:
    scored = [(score(p, cfg), p) for p in papers]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, p in scored[: max(limit, 1)]:
        p.score = s
        out.append(p)
    return out
