"""定时调度（需求：每日定时同步 + 启动后自动同步，失败不断重试直到成功）.

只保留「每日」一个定时点；周/月/年热度浏览已移到「前沿探索」页（本地库按时间段+排序）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone

from ..config import Config
from .errors import log
from .library import Library
from .notifier import notifier
from .pipeline import Pipeline

__all__ = ["Scheduler", "scheduler"]

_RETRY_SECONDS = 300  # 同步失败后 5 分钟重试
_BUSY_RETRY_SECONDS = 20  # 只是被并发守卫挡了一下，很快再来


class Scheduler:
    def __init__(self, cfg: Config, lib: Library) -> None:
        self.cfg = cfg
        self.lib = lib
        self.pipeline = Pipeline(cfg, lib)
        self._sched = BackgroundScheduler(timezone=get_localzone())
        self._lock = threading.Lock()
        self._syncing = False
        self.on_done = None      # 同步成功后回调（UI 刷新等），由 app 注入
        self.on_papers = None    # 每入库一批就回调（UI 增量刷新），由 app 注入

    # ---------- 带重试的同步 ----------
    def _sync_until_success(self, days: int = 1) -> None:
        """不断尝试同步，直到抓到论文为止（失败则每 5 分钟重试）."""
        with self._lock:
            if self._syncing:
                return
            self._syncing = True
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    stat = self.pipeline.sync(days=days, on_papers=self.on_papers)
                except Exception as e:
                    log.error("自动同步异常（第 %d 次）：%s，%.0f 秒后重试", attempt, e, _RETRY_SECONDS)
                    time.sleep(_RETRY_SECONDS)
                    continue
                if stat.get("skipped"):
                    # 不是失败，是并发守卫让路（比如开机自动同步还没跑完）。
                    # 等一小会儿再来：按「没抓到论文」去睡 5 分钟的话，
                    # 08:00 的定时同步可能就此让位给一次手动刷新，当天不再抓。
                    log.info("自动同步让路（第 %d 次）：%s，%.0f 秒后重试",
                             attempt, stat.get("reason", ""), _BUSY_RETRY_SECONDS)
                    time.sleep(_BUSY_RETRY_SECONDS)
                    continue
                if stat.get("papers", 0) > 0:
                    log.info("自动同步成功（第 %d 次尝试）：%d 篇候选，新增 %d 篇",
                             attempt, stat["papers"], stat["new"])
                    self._push_daily_report(stat)
                    if self.on_done:
                        try:
                            self.on_done(stat)
                        except Exception as e:
                            log.warning("同步完成回调失败: %s", e)
                    break
                log.warning("自动同步未抓到论文（第 %d 次），%.0f 秒后重试", attempt, _RETRY_SECONDS)
                time.sleep(_RETRY_SECONDS)
        finally:
            self._syncing = False

    def _push_daily_report(self, stat: dict) -> None:
        try:
            top = self.lib.get_papers(limit=5, order="score DESC")
            lines = [f"{i + 1}. {p['title']}" for i, p in enumerate(top[:5])]
            body = "\n".join(lines) if lines else "今天没有新论文"
            notifier.toast(
                "Arxiver 同步完成",
                f"新增 {stat['new']} 篇相关论文，共 {stat['papers']} 篇候选\n{body}",
            )
            if stat.get("failed"):
                notifier.toast("Arxiver 下载失败",
                               f"{len(stat['failed'])} 篇论文下载失败，详见日志")
        except Exception as e:
            log.exception("日报推送异常: %s", e)

    def trigger_sync(self) -> None:
        """在后台线程触发一次带重试的同步（供定时任务 / 启动自动同步使用）."""
        threading.Thread(target=self._sync_until_success, name="auto-sync", daemon=True).start()

    # ---------- 生命周期 ----------
    def start(self, arm_startup_sync: bool = True) -> None:
        self._sched.add_job(
            self.trigger_sync,
            CronTrigger(hour=self.cfg.get("daily_hour", 8),
                        minute=self.cfg.get("daily_minute", 0)),
            id="daily", misfire_grace_time=3600,
        )
        try:
            self._sched.start()
            log.info("定时任务已启动：%s", ", ".join(j.id for j in self._sched.get_jobs()))
        except Exception as e:
            log.error("定时任务启动失败: %s", e)
        # 启动后自动同步（稍延迟，避免与窗口初始化竞争）
        if arm_startup_sync and self.cfg.get("auto_sync_on_start", True):
            threading.Timer(3.0, self.trigger_sync).start()
            log.info("已安排启动后自动同步")

    def reload(self) -> None:
        """配置变更后重启调度。

        只重挂 cron，绝不重排「启动后自动同步」——否则用户点一下深色模式
        （任何一次 save_settings 都会走到这里）就会在 3 秒后偷偷抓一遍全网。
        """
        try:
            if self._sched.running:
                self._sched.shutdown(wait=False)
            self._sched = BackgroundScheduler(timezone=get_localzone())
            self.start(arm_startup_sync=False)
        except Exception as e:
            log.error("调度重载失败: %s", e)

    def shutdown(self) -> None:
        try:
            if self._sched.running:
                self._sched.shutdown(wait=False)
        except Exception:
            pass


scheduler: Scheduler | None = None


def get_scheduler(cfg: Config, lib: Library) -> Scheduler:
    global scheduler
    if scheduler is None:
        scheduler = Scheduler(cfg, lib)
    return scheduler
