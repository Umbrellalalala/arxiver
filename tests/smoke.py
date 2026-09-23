"""端到端冒烟测试：数据源抓取 → 同步入库 → 下载重命名归档.

运行: .venv/Scripts/python tests/smoke.py
数据写到 tests/_smoke_home/（隔离，不影响正式数据）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ARXIVER_HOME", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_smoke_home"))

from arxiver.config import get_config
from arxiver.core.clients import HFDailyClient
from arxiver.core.downloader import build_filename, sanitize_title
from arxiver.core.errors import setup_logging
from arxiver.core.library import Library
from arxiver.core.pipeline import Pipeline


def main() -> None:
    setup_logging()
    cfg = get_config()
    lib = Library()

    print("== 1. HuggingFace 每日热榜 ==")
    hp = HFDailyClient().get_daily(limit=10)
    print(f"   抓到 {len(hp)} 篇")
    for p in hp[:3]:
        print(f"   - {p.clean_id} upvotes={p.upvotes} {p.title[:50]}")

    print("== 2. 重命名规则 ==")
    t = 'Is this "paper" really good? Yes! 100% (best/worst: A/B)'
    print(f"   {t!r}")
    print(f"   -> {sanitize_title(t)!r}")

    print("== 3. 同步管道（arXiv 新论文 + 热榜 → 去重 → 打分 → 入库） ==")
    stat = Pipeline(cfg, lib).sync(days=3, auto_download=False)
    print(f"   {json.dumps(stat, ensure_ascii=False)}")
    print(f"   库统计: {lib.stats()}")

    print("== 4. 下载 + 重命名 + 归档（直连 PDF，不走 API） ==")
    rows = lib.get_papers(limit=500, order="score DESC")
    targets = [r for r in rows if r["arxiv_id"] and not r["local_path"]][:2]
    dl = Pipeline(cfg, lib).downloader
    for r in targets:
        from arxiver.core.models import Paper
        p = Paper(arxiv_id=r["arxiv_id"], title=r["title"], published=r["published"])
        path = dl.download(p)
        print(f"   {build_filename(p)}")
        print(f"   -> {path}")

    print("冒烟测试完成")


if __name__ == "__main__":
    main()
