"""用户画像（需求 2：仅限计算机专业的大方向/小方向）.

大方向 = arXiv cs.* 分类（内置常用分类中英文名，供 UI 下拉选择）
小方向 = 每个大方向下的自由关键词 + 种子论文（收藏论文自动成为种子）
"""
from __future__ import annotations

from ..config import Config
from .models import Paper

__all__ = ["ARXIV_CS_CATEGORIES", "THEME_CATEGORIES", "expand_majors",
           "match_major", "match_keywords"]

# 热门主题 → 多个 arXiv 分类（主题型大方向，抓取/归档时展开）
THEME_CATEGORIES: dict[str, list[str]] = {
    "multimodal": ["cs.CV", "cs.MM", "cs.CL", "cs.AI"],
    "agent": ["cs.MA", "cs.AI", "cs.CL"],
    "recsys": ["cs.IR", "cs.AI"],
    "intent": ["cs.CL", "cs.AI"],
    "compression": ["cs.LG", "cs.CV"],
}

# 计算机专业大方向：arXiv cs.* 常用分类（code -> 中文名）
ARXIV_CS_CATEGORIES: dict[str, str] = {
    "cs.AI": "人工智能",
    "cs.CL": "计算语言学 / NLP",
    "cs.CV": "计算机视觉",
    "cs.LG": "机器学习",
    "cs.IR": "信息检索",
    "cs.DS": "数据结构与算法",
    "cs.DB": "数据库",
    "cs.SE": "软件工程",
    "cs.NI": "网络与互联网",
    "cs.SY": "系统与控制",
    "cs.DC": "分布式与并行计算",
    "cs.CR": "密码学与安全",
    "cs.RO": "机器人",
    "cs.MM": "多媒体",
    "cs.HC": "人机交互",
    "cs.GR": "计算机图形学",
    "cs.CY": "计算机与社会",
    "cs.NE": "神经网络与进化计算",
    "cs.IT": "信息论",
    "cs.GT": "计算机科学与博弈论",
    "cs.PL": "编程语言",
    "cs.LO": "逻辑与计算理论",
    "cs.AR": "计算机体系结构",
    "cs.OS": "操作系统",
    "cs.CE": "计算工程与金融",
    "cs.ET": "新兴技术",
    "cs.FL": "形式语言与自动机",
    "cs.SC": "符号计算",
    "cs.SD": "声音与语音",
    "cs.CC": "计算复杂度",
    "cs.CG": "计算几何",
    "cs.MA": "多智能体系统",
    "cs.MS": "数学软件",
    "cs.PF": "性能",
    "cs.DL": "数字图书馆",
    # 主题型大方向
    "multimodal": "多模态",
    "agent": "智能体",
    "recsys": "推荐系统",
    "intent": "意图理解",
    "compression": "模型压缩",
}


def expand_majors(majors: list[str]) -> list[str]:
    """把主题型大方向展开为 arXiv 分类列表（抓取用），去重."""
    out: list[str] = []
    for m in majors:
        if m in THEME_CATEGORIES:
            out.extend(THEME_CATEGORIES[m])
        else:
            out.append(m)
    return list(dict.fromkeys(out))


def match_major(paper: Paper, cfg: Config) -> list[str]:
    """论文分类与用户大方向的交集（归档/打分用），支持主题型大方向."""
    majors = set(cfg.majors())
    hits: list[str] = []
    for c in paper.categories:
        if c in majors:
            hits.append(c)
    for theme, cats in THEME_CATEGORIES.items():
        if theme in majors and any(c in cats for c in paper.categories):
            hits.append(theme)
    return hits


def match_keywords(paper: Paper, cfg: Config) -> list[str]:
    """标题/摘要中命中的小方向关键词（不区分大小写）."""
    text = f"{paper.title} {paper.abstract}".lower()
    hits = []
    for kw in cfg.keywords():
        k = kw.strip().lower()
        if k and k in text:
            hits.append(kw.strip())
    return hits
