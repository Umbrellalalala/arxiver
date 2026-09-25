"""统一论文数据模型：所有数据源客户端都返回 Paper 列表."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Paper:
    arxiv_id: str | None            # 如 "2203.00001v1"；非 arXiv 来源可为 None
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    published: str = ""             # ISO 日期 "2026-09-13"
    updated: str = ""
    pdf_url: str = ""
    abs_url: str = ""
    upvotes: int = 0                # HuggingFace 热度
    citations: int = 0              # Semantic Scholar / OpenAlex 引用
    source: str = "arxiv"           # arxiv / hf_daily / s2 / openalex
    score: float = 0.0              # 推荐打分（rank 时填充）

    @property
    def clean_id(self) -> str:
        """去掉版本号：2203.00001v1 -> 2203.00001."""
        if not self.arxiv_id:
            return ""
        m = re.match(r"^(.*?)(v\d+)?$", self.arxiv_id)
        return m.group(1) if m else self.arxiv_id

    @property
    def abs_link(self) -> str:
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.clean_id}"
        return self.abs_url

    def to_row(self) -> dict:
        return {
            "arxiv_id": self.clean_id or None,
            "title": self.title,
            "abstract": self.abstract,
            "authors": ", ".join(self.authors),
            "categories": ", ".join(self.categories),
            "published": norm_date(self.published),
            "updated": self.updated,
            "pdf_url": self.pdf_url,
            "abs_url": self.abs_link,
            "upvotes": self.upvotes,
            "citations": self.citations,
            "source": self.source,
            "score": self.score,
        }


def norm_date(s) -> str:
    """把来源给的日期统一成 YYYY-MM-DD（缺的补 0）。

    库里 published 全程按**字符串**比较：首页「近 N 天」、按最新排序、自动归档。
    来源格式不统一（Semantic Scholar 常只给 "2026"，偶见 "2026-03"），而
    "2026" < "2026-08-26" 成立，于是当年份存进去后：种子推荐抓回来当天就被
    判成远古论文整批归档（线上实测 52/52 全中），「近 N 天」也永远筛掉它们。
    统一在这里做——to_row 是所有数据源进库的唯一出口，比在每个客户端各修一遍可靠。
    """
    s = str(s or "").strip()
    if not s:
        return ""
    parts = s.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return s
    if len(parts) == 2 and len(parts[0]) == 4:      # "2026-03" -> "2026-03-01"
        return f"{s}-01"
    if len(parts) == 1 and len(s) == 4 and s.isdigit():
        return f"{s}-01-01"                          # "2026" -> "2026-01-01"
    return s
