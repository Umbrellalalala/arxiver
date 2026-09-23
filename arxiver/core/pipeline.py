"""多源同步管道：抓取 → 合并去重 → 打分 → 入库 → 可选自动下载.

定时任务与手动刷新共用此入口。

增量策略：每个数据源（arXiv 分类 / 关键词分组 / 热榜每天 / 种子推荐）抓到结果后
立即去重、打分、入库，并通过 `on_papers` 回调把这一批推给界面——
不用等所有源抓完，用户能边抓边看到论文出现。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..config import Config
from .clients import ArxivClient, HFDailyClient, OpenAlexClient, SemanticScholarClient
from .downloader import Downloader
from .errors import log
from .library import Library
from .models import Paper
from .recommender import rank

__all__ = ["Pipeline", "sync_busy"]

# 进程内唯一的「正在同步」互斥锁（Pipeline.sync 取它，见那里的说明）。
_SYNC_LOCK = threading.Lock()


def sync_busy() -> bool:
    """当前是否已有一次同步在跑（给界面做「点了但被跳过」的即时反馈）."""
    return _SYNC_LOCK.locked()

# 种子论文相关推荐的上限。收藏论文会自动进 seed_papers（Config.add_seed 不限量），
# 而每个种子要一次 Semantic Scholar 请求（免费池是共享的、随机吃 429，见
# clients/semantic_scholar.py 的说明），种子上百个会把同步尾巴拖很长，
# 所以上限是必要的——但**被跳过的种子必须写进日志**：
# 旧代码是 `seeds[:3]` 硬切，用户收藏第 4 篇之后那篇的推荐就再也没出现过，
# 界面上一点提示都没有。
#
# 实测单个种子约 2~3s；撞上 429 时约 7~10s（重试 delay=5.0 起步）。
# 所以 8 个种子约 16~25s（坏运气下更久）。这段在**所有论文都已可见之后**才跑，
# 只影响「同步完成」提示的时机，不挡用户看到论文，8 是可接受的。
_MAX_SEEDS = 8

# 补引用数时最多查多少篇。S2 一次请求的成本 + 随机 429，全量补（一天 400+ 篇）
# 会让同步尾巴变得很长，所以上限是必要的。
# **但取哪 N 篇必须按分数，不能按抓取顺序**：`seen` 是抓取顺序，前 N 篇永远是
# 最早到达的那几个分类，cs.LG/cs.CV/cs.AI/cs.CL 会永远拿不到引用数——
# 而界面里有「引用数」排序、卡片上也有「被引 N」徽标，等于这几个方向的论文
# 永远显示 0 被引、排序永远垫底。旧代码就是 `seen.values()[:50]`。
_MAX_CITATION_IDS = 50


def _merge_key(p: Paper) -> str:
    """去重键：优先 arXiv ID（去版本号），否则用标题."""
    return p.clean_id or f"t:{p.title.lower()}"


class Pipeline:
    def __init__(self, cfg: Config, lib: Library) -> None:
        self.cfg = cfg
        self.lib = lib
        self.downloader = Downloader(cfg, lib)
        self.arxiv = ArxivClient()
        self.hf = HFDailyClient()
        self._s2 = None

    @property
    def s2(self) -> SemanticScholarClient:
        if self._s2 is None:
            self._s2 = SemanticScholarClient(self.cfg.get("semanticscholar_key", ""))
        return self._s2

    def sync(self, days: int = 1, auto_download: bool | None = None,
             on_progress: Callable[[str, int | None], None] | None = None,
             on_papers: Callable[[list[Paper], int], None] | None = None) -> dict:
        """同步一次（进程内同一时刻只允许一个）。

        手动刷新、时间芯片补抓、开机自动同步、每日定时任务都走这里。以前
        Api 和 Scheduler 各持一个布尔标志、互不知情，实测两个线程会交错把
        同一轮抓取跑两遍（429 也翻倍）。守卫放在这里才盖得住所有入口。
        """
        if not _SYNC_LOCK.acquire(blocking=False):
            log.info("sync: 已有一次同步在跑，本次跳过")
            return {"skipped": True, "reason": "正在同步中，请稍候",
                    "new": 0, "papers": 0, "failed": [], "elapsed": 0}
        try:
            return self._sync(days=days, auto_download=auto_download,
                              on_progress=on_progress, on_papers=on_papers)
        finally:
            _SYNC_LOCK.release()

    def _sync(self, days: int = 1, auto_download: bool | None = None,
              on_progress: Callable[[str, int | None], None] | None = None,
              on_papers: Callable[[list[Paper], int], None] | None = None) -> dict:
        """同步一次，返回统计字典。

        on_progress(text, pct)      —— 前端进度条文案/百分比
        on_papers(papers, new_count) —— 每入库一批就回调一次，用于界面实时增量更新
        """
        def _prog(msg: str, pct: int | None = None):
            log.info("sync: %s", msg)
            if on_progress:
                try:
                    on_progress(msg, pct)
                except Exception as e:
                    # 和下面 on_papers 的处理保持一致：**别静默**。
                    # 进度回调挂了的表现是「进度条卡住不动」，静默的话完全查不出来。
                    log.warning("on_progress 回调失败（忽略）: %s", e)

        t0 = time.time()
        majors = self.cfg.majors()
        keywords = self.cfg.keywords()

        seen: dict[str, Paper] = {}      # 本次同步已见过的论文（跨源去重）
        new_total = 0
        ingest_lock = threading.Lock()

        def _ingest(batch: list[Paper], label: str = "", quiet: bool = False) -> int:
            """去重 → 打分 → 立即入库 → 立即推送。返回本批新增篇数.

            同源/跨源重复的论文不会重复入库，但若补到了新信息（热榜热度、
            引用数）会重新打分写回并一起推给前端，让界面上的分数同步修正。

            quiet=True 时只推论文不改进度条文案（给并行跑的源用，避免两个
            线程的进度文案互相打断）。
            """
            nonlocal new_total
            if not batch:
                return 0
            fresh: list[Paper] = []
            touched: list[Paper] = []
            with ingest_lock:
                for p in batch:
                    key = _merge_key(p)
                    prev = seen.get(key)
                    if prev is None:
                        seen[key] = p
                        fresh.append(p)
                        continue
                    changed = False
                    if p.upvotes > prev.upvotes:
                        prev.upvotes = p.upvotes
                        changed = True
                    if p.citations > prev.citations:
                        prev.citations = p.citations
                        changed = True
                    if p.source == "hf_daily" and prev.source != "hf_daily":
                        prev.source = "hf_daily"
                        changed = True
                    if not prev.abstract and p.abstract:
                        prev.abstract = p.abstract
                        changed = True
                    if changed:
                        touched.append(prev)
                todo = fresh + touched
                if not todo:
                    return 0
                scored = rank(todo, self.cfg, limit=len(todo))
                added = self.lib.upsert(scored)
                new_total += added
            # 推送放在锁外：前端渲染慢时不应该阻塞另一个抓取线程入库
            if on_papers:
                try:
                    on_papers(scored, added)
                except Exception as e:
                    log.warning("on_papers 回调失败（忽略）: %s", e)
            if label and not quiet:
                _prog(f"{label}：{len(batch)} 篇（新增 {added}）", None)
            return added

        def _emit_batch(batch: list[Paper], label: str) -> None:
            _ingest(batch, label)

        # 0) 热榜与 arXiv 并行启动：两者互不依赖，且 HF 接口偶尔 502 会重试很久
        #    （实测 55s）。串行的话这段等待会直接加到总时长上，并行则被 arXiv 掩盖。
        hf_state = {"done": threading.Event(), "n": 0}

        def _hf_work() -> None:
            try:
                got = self.hf.get_recent_days(days=days, limit=40,
                                              on_batch=lambda b, lab: _ingest(b, lab, quiet=True))
                hf_state["n"] = len(got)
            except Exception as e:
                log.warning("热榜获取异常（忽略）: %s", e)
            finally:
                hf_state["done"].set()

        threading.Thread(target=_hf_work, name="hf-daily", daemon=True).start()

        # 1) arXiv 大方向新论文（RSS 优先，速度快；主题型大方向展开为分类）
        if majors:
            from .profile import expand_majors
            cats = expand_majors(majors)
            _prog("抓取 arXiv 最新论文…", 10)
            self.arxiv.fetch_recent(cats, days=days, max_results=80,
                                    on_batch=_emit_batch)
            _prog(f"arXiv 大方向完成，累计新增 {new_total} 篇", 40)
        # 2) arXiv 小方向关键词检索（按关键词分组，每组回来即入库）
        if keywords:
            _prog("按小方向关键词检索…", 45)
            self.arxiv.search_keywords(keywords, days=max(days, 7),
                                       max_results=40, on_batch=_emit_batch)
            _prog(f"关键词检索完成，累计新增 {new_total} 篇", 58)

        # 3) 等热榜收尾（并行启动的，通常这时早就回来了，无需等待）
        #    上限不能大：热榜是「锦上添花」的源，不该拖住主流程。客户端已把连接
        #    超时压到 5s（连不通时整体约 41s），这里再兜一道——就算它再出别的
        #    幺蛾子，最多也只多等 60s。等不到也无所谓：hf-daily 线程是 daemon，
        #    它稍后抓到论文照样会通过 on_papers 推到界面上，只是不计入本次统计。
        if not hf_state["done"].is_set():
            _prog("等待每日热榜…", 62)
            if not hf_state["done"].wait(timeout=60):
                log.warning("热榜抓取超时（>60s），继续后续流程")
        _prog(f"热榜完成（{hf_state['n']} 篇）", 72)

        # 4) 种子论文相关推荐（Semantic Scholar，逐个种子入库）
        all_seeds = list(self.cfg.get("seed_papers", []) or [])
        seeds = all_seeds[:_MAX_SEEDS]
        if len(all_seeds) > _MAX_SEEDS:
            log.warning("种子论文 %d 篇超过上限 %d，本次跳过：%s",
                        len(all_seeds), _MAX_SEEDS, all_seeds[_MAX_SEEDS:])
        if seeds:
            _prog("获取种子论文相关推荐…", 75)
            for sid in seeds:
                try:
                    _ingest(self.s2.related(sid, limit=10), "种子推荐")
                except Exception as e:
                    log.warning("种子推荐失败（忽略）%s: %s", sid, e)

        papers_count = len(seen)
        if papers_count == 0:
            _prog("没有抓到新论文", 100)
            return {"papers": 0, "new": 0, "downloaded": [], "failed": [],
                    "elapsed": round(time.time() - t0, 1)}

        # 5) 补引用数（尽力而为，限制批量大小避免拖慢）
        try:
            _prog("补充引用数…", 82)
            all_papers = list(seen.values())
            # 按**分数**取前 N（不是抓取顺序），理由见 _MAX_CITATION_IDS 的注释。
            # rank() 会顺手把 p.score 写好，后面 upsert 正好用得上。
            with_ids = [p for p in rank(all_papers, self.cfg, limit=len(all_papers))
                        if p.arxiv_id]
            if len(with_ids) > _MAX_CITATION_IDS:
                log.info("引用数只补分数最高的 %d 篇（有 ID 的候选共 %d 篇）",
                         _MAX_CITATION_IDS, len(with_ids))
            ids = [p.clean_id for p in with_ids[:_MAX_CITATION_IDS]]
            cites = self.s2.citations(ids)
            touched = []
            for p in all_papers:
                if p.clean_id in cites and cites[p.clean_id] > p.citations:
                    p.citations = cites[p.clean_id]
                    touched.append(p)
            if touched:
                rescored = rank(touched, self.cfg, limit=len(touched))
                self.lib.upsert(rescored)
                if on_papers:
                    try:
                        on_papers(rescored, 0)  # 只修正分数，不算新增
                    except Exception:
                        pass
                _prog(f"引用数已更新 {len(touched)} 篇", 90)
        except Exception as e:
            log.warning("引用数补充失败（忽略）: %s", e)

        _prog(f"入库完成：本次新增 {new_total} 篇", 92)

        # 6) 自动下载 Top N
        downloaded, failed = [], []
        do_download = auto_download if auto_download is not None else self.cfg.get("auto_download", False)
        if do_download:
            ranked = rank(list(seen.values()), self.cfg, limit=len(seen))
            top = [p for p in ranked if p.arxiv_id][: self.cfg.get("download_top_n", 5)]
            _prog(f"自动下载 Top {len(top)} 篇…", 95)
            ok_paths, failed_papers = self.downloader.download_many(top)
            downloaded = ok_paths
            failed = [p.title for p in failed_papers]

        # 收尾：把没人理的老论文挪出推荐池（只改状态，不删数据）
        archived = 0
        keep_days = int(self.cfg.get("pool_keep_days", 30) or 0)
        try:
            archived = self.lib.archive_stale(keep_days)
            if archived:
                log.info("sync: 自动归档 %d 篇（%d 天没被收藏/下载/标注）", archived, keep_days)
        except Exception as e:
            log.warning("sync: 自动归档失败（不影响本次同步）: %s", e)

        return {
            "papers": papers_count,
            "new": new_total,
            "downloaded": downloaded,
            "failed": failed,
            "archived": archived,
            "elapsed": round(time.time() - t0, 1),
        }

    def top_period(self, days: int, limit: int = 15) -> list[Paper]:
        """本地库中最近 N 天入库的论文按热度排序（周报用）."""
        rows = self.lib.get_papers(limit=500, order="upvotes DESC")
        papers = [Paper(
            arxiv_id=r["arxiv_id"], title=r["title"], abstract=r["abstract"],
            authors=r["authors"].split(", ") if r["authors"] else [],
            categories=r["categories"].split(", ") if r["categories"] else [],
            published=r["published"], pdf_url=r["pdf_url"], abs_url=r["abs_url"],
            upvotes=r["upvotes"], citations=r["citations"], source=r["source"],
        ) for r in rows]
        return papers[:limit]
