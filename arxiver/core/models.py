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
            "published": self.published,
            "updated": self.updated,
            "pdf_url": self.pdf_url,
            "abs_url": self.abs_link,
            "upvotes": self.upvotes,
            "citations": self.citations,
            "source": self.source,
            "score": self.score,
        }
