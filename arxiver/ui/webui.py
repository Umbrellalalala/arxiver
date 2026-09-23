"""桌面窗口（pywebview）：设置页 + 论文流 + 下载管理.

前端代码在 static/index.html，通过 window.pywebview.api 调用本模块 Api。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import webview

from ..config import Config, get_config
from ..core import autostart as autostart_mod
from ..core.clients.scholar import ScholarClient
from ..core.clients.semantic_scholar import SemanticScholarClient
from ..core.errors import log
from ..core.library import Library
from ..core.llm import LLMClient
from ..core.notifier import notifier
from ..core.pipeline import Pipeline
from ..core.profile import ARXIV_CS_CATEGORIES
from ..paths import BASE_DIR, atomic_write_text, resource_path

__all__ = ["create_window", "Api"]


class Api:
    """暴露给前端 JS 的接口（window.pywebview.api.*）."""

    def __init__(self, cfg: Config, lib: Library) -> None:
        self.cfg = cfg
        self.lib = lib
        self.pipeline = Pipeline(cfg, lib)
        self._window: Any = None
        self._embedded = False   # 被 LifeSystem 内嵌（无托盘模式）时才跟随它的主题
        self._llm: LLMClient | None = None
        self._summary_cache: dict[str, str] = {}
        self._s2: SemanticScholarClient | None = None
        self._scholar: ScholarClient | None = None
        self._cancels: dict[str, threading.Event] = {}  # 批量任务按类型各一个中断标志
        self._dl_tasks: list[dict] = []   # 下载任务列表（进行中 + 已完成）
        self._dl_lock = threading.Lock()
        # 串行化所有「后端 → 前端」推送：同步线程、下载线程、定时任务线程
        # 都会调 evaluate_js，并发调用可能让前端收到交错的脚本。
        self._push_lock = threading.Lock()

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.cfg)
        return self._llm

    def attach(self, window) -> None:
        self._window = window

    def _push(self, js: str) -> None:
        """后端 → 前端推送（多线程安全）."""
        if self._window is None:
            return
        with self._push_lock:
            try:
                self._window.evaluate_js(js)
            except Exception:
                pass

    def get_life_theme(self) -> str | None:
        """读取 LifeSystem 写入的主题同步文件（dark/light），用于内嵌时跟随主题。

        只在被内嵌（无托盘模式）时才读：独立运行时前端每 1.5s 轮询这里，
        只要那个文件存在就会把用户刚手选的主题抢回去。
        """
        if not self._embedded:
            return None
        try:
            p = os.path.join(os.path.expanduser("~"), ".life_system_theme")
            with open(p, encoding="utf-8") as f:
                v = f.read().strip()
            return v if v in ("dark", "light") else None
        except Exception:
            return None

    # ---------- 画像 / 设置 ----------
    def get_categories(self) -> dict:
        return ARXIV_CS_CATEGORIES

    def get_profile(self) -> dict:
        return {
            "majors": self.cfg.majors(),
            "minors": self.cfg.minors(),
            "seeds": self.cfg.get("seed_papers", []),
        }

    def save_profile(self, profile: dict) -> bool:
        try:
            majors = profile.get("majors") or []
            minors = {k: v for k, v in (profile.get("minors") or {}).items() if k in majors}
            self.cfg.update({
                "major_fields": majors,
                "minor_topics": minors,
            })
            return True
        except Exception as e:
            log.error("保存画像失败: %s", e)
            return False

    def get_settings(self) -> dict:
        from ..core.downloader import library_base
        d = self.cfg.to_dict()
        d["autostart_enabled"] = autostart_mod.is_enabled()
        d["library_dir"] = str(library_base(self.cfg))  # 返回实际生效路径
        return d

    def save_settings(self, s: dict) -> bool:
        try:
            allowed = {
                "library_dir", "auto_download", "download_top_n", "notify",
                "desktop_shortcut",
                "daily_hour", "daily_minute", "auto_sync_on_start", "pool_keep_days",
                "semanticscholar_key", "openalex_mailto",
                "llm_base_url", "llm_api_key", "llm_model", "theme",
                "progress_opacity",
            }
            old_time = (self.cfg.get("daily_hour", 8), self.cfg.get("daily_minute", 0))
            self.cfg.update({k: v for k, v in s.items() if k in allowed})
            # 自启开关：注册表操作成功后才同步配置
            if "autostart" in s:
                ok = autostart_mod.enable() if s["autostart"] else autostart_mod.disable()
                if ok:
                    self.cfg.set("autostart", bool(s["autostart"]))
                else:
                    return False
            # 桌面快捷方式开关立即生效
            if "desktop_shortcut" in s:
                from ..core import shortcut as shortcut_mod
                if s["desktop_shortcut"]:
                    shortcut_mod.ensure()
                else:
                    shortcut_mod.remove()
            # 只有改动每日同步时间才需要重挂 cron；其余设置管道/通知在下次
            # 运行时自己读配置。曾经这里是无条件 reload()，而 reload() 会重排
            # 「启动 3 秒后自动同步」，于是点一下深色模式就偷偷抓一遍全网。
            new_time = (self.cfg.get("daily_hour", 8), self.cfg.get("daily_minute", 0))
            if new_time != old_time:
                from ..core.scheduler import get_scheduler
                sched = get_scheduler(self.cfg, self.lib)
                sched.reload()
            return True
        except Exception as e:
            log.error("保存设置失败: %s", e)
            return False

    # ---------- 同步 ----------
    def sync_now(self) -> dict:
        return self.sync_range(1)

    def sync_range(self, days: int) -> dict:
        """按时间段同步：近 x 天 / 周 / 月 / 年 视图切换时抓取对应范围.

        抓取过程中每入库一批就通过 onNewPapers 推给前端，界面实时增量更新，
        不用等所有数据源抓完。
        """
        import json as _json
        from ..core.pipeline import sync_busy
        if sync_busy():
            # 「正在同步中」的来源现在是全进程唯一的那把锁（手动 + 定时共用），
            # 不再单独维护 self._syncing——那个标志只管手动，定时任务照样会再跑一遍。
            return {"error": "正在同步中，请稍候"}
        self._push("Arxiver.onSyncStart()")

        def _work():
            try:
                stat = self.pipeline.sync(
                    days=int(days or 1), auto_download=False,
                    on_progress=lambda text, pct: self._push(
                        f"Arxiver.onProgress({_json.dumps({'text': text, 'pct': pct})})"),
                    on_papers=self.notify_new_papers)
                if stat.get("skipped"):
                    # 极小概率：查锁之后、真正开跑之前被定时任务抢先了。
                    # 进度条归那一次同步所有，这里只收掉自己刚点亮的那下。
                    self._push("Arxiver.onToast('已有一次同步在跑，本次补抓跳过')")
                    self._push("Arxiver.onSyncDone({})")
                    return
                self._push(f"Arxiver.onSyncDone({_json.dumps(stat)})")
                if self.cfg.get("notify", True) and days <= 1:
                    notifier.toast("Arxiver 同步完成",
                                   f"新增 {stat['new']} 篇，共 {stat['papers']} 篇候选，耗时 {stat['elapsed']}s")
            except Exception as e:
                log.exception("同步失败: %s", e)
                self._push(f"Arxiver.onSyncError({_json.dumps(str(e))})")

        threading.Thread(target=_work, name="sync", daemon=True).start()
        return {"started": True}

    def notify_new_papers(self, papers: list, new: int = 0) -> None:
        """每入库一批就推给前端做增量刷新（线程安全）。

        只推新增数量和这批的 arXiv ID——列表内容由前端回查本地库，
        保证排序/分页与数据库完全一致，也避免长列表的序列化开销。
        """
        import json as _json
        try:
            ids = [p.clean_id or "" for p in (papers or []) if p.clean_id]
        except Exception:
            ids = []
        if not ids:
            return
        payload = {"new": int(new or 0), "count": len(ids), "ids": ids}
        self._push(f"Arxiver.onNewPapers({_json.dumps(payload)})")

    # ---------- 论文 ----------
    def get_papers(self, status: str = "", limit: int = 50, offset: int = 0,
                   query: str = "", order: str = "score DESC", tag: str = "",
                   since_days: int = 0, min_score: float = 0,
                   date_from: str = "", date_to: str = "") -> list:
        st = status or None
        return self.lib.get_papers(status=st, limit=limit, offset=offset,
                                   query=query, order=order, tag=tag or "",
                                   since_days=since_days or 0,
                                   min_score=min_score or 0,
                                   date_from=date_from or "", date_to=date_to or "")

    def count_papers(self, status: str = "", query: str = "", tag: str = "",
                     since_days: int = 0, min_score: float = 0,
                     date_from: str = "", date_to: str = "") -> int:
        """和 get_papers 同一套筛选条件下的总条数（分页器用）。

        以前前端是 `get_papers(limit=100000)` 把整库捞回本地再切片，
        库里 1800 行时每次刷新要过一遍 ~3MB 的 JSON，同步期间更是每
        600ms 一次。改成按页取之后总数只能单独问。
        """
        return self.lib.count_papers(
            status=status or None, query=query, tag=tag or "",
            since_days=since_days or 0, min_score=min_score or 0,
            date_from=date_from or "", date_to=date_to or "")

    def archive_pool(self, days: int = 0) -> dict:
        """设置页「立即整理一次」：不等下一轮同步就归档。"""
        try:
            d = int(days or self.cfg.get("pool_keep_days", 30) or 0)
            if d <= 0:
                return {"ok": False, "archived": 0, "msg": "未启用自动归档"}
            return {"ok": True, "archived": self.lib.archive_stale(d)}
        except Exception as e:
            log.error("归档失败: %s", e)
            return {"ok": False, "archived": 0, "msg": str(e)}

    def get_stats(self) -> dict:
        return self.lib.stats()

    def get_all_tags(self) -> list:
        return self.lib.all_tags()

    # ---------- 学术搜索（期刊/会议论文，排除 arXiv，标注 CCF 类别） ----------
    def scholar_search(self, query: str, limit: int = 20, since_year: int = 0,
                       page: int = 0, ccf_filter: list | None = None,
                       type_filter: list | None = None) -> list:
        """搜索谷歌学术。ccf_filter: ['A','B','C','none']；type_filter: ['journal','conference']."""
        if self._scholar is None:
            self._scholar = ScholarClient()
        results = self._scholar.search(query, limit=limit,
                                       since_year=since_year or None, page=page)
        # 过滤已删除的 DOI
        trashed_dois = self.lib.get_trashed_scholar_dois()
        if trashed_dois:
            results = [r for r in results if r.get("doi") not in trashed_dois]
        # CCF 等级筛选
        if ccf_filter:
            def _ccf_match(p):
                ccf = p.get("ccf", "")
                if "none" in ccf_filter and not ccf:
                    return True
                return ccf in ccf_filter
            results = [r for r in results if _ccf_match(r)]
        # 期刊/会议类型筛选（优先用 Crossref 的 type 字段，退回 venue 关键词推断）
        if type_filter:
            def _type_match(p):
                ctype = (p.get("type") or "").lower()
                venue = (p.get("venue") or "").lower()
                if ctype:
                    if ctype.startswith("journal") or ctype in ("article", "peer-review"):
                        is_journal, is_conf = True, False
                    elif ctype.startswith("proceedings") or ctype.startswith("conference"):
                        is_journal, is_conf = False, True
                    else:
                        is_journal = is_conf = False
                else:
                    journ_kw = ["journal", "transactions", "letters", "review", "acta"]
                    conf_kw = ["conference", "proceedings", "workshop", "symposium", "meeting"]
                    is_journal = any(k in venue for k in journ_kw)
                    is_conf = any(k in venue for k in conf_kw)
                out = []
                if "journal" in type_filter and is_journal:
                    out.append(True)
                if "conference" in type_filter and is_conf:
                    out.append(True)
                return bool(out)
            results = [r for r in results if _type_match(r)]
        return results

    def notify_sync_done(self, stat: dict) -> None:
        """供后台定时同步完成后通知前端刷新列表（线程安全）."""
        import json as _json
        self._push(f"Arxiver.onSyncDone({_json.dumps(stat or {})})")

    def get_paper(self, arxiv_id: str) -> dict | None:
        return self.lib.get(arxiv_id)

    # ---------- 谷歌学术结果缓存（别让每次开 tab 都重搜一遍）----------
    def get_scholar_cache(self) -> dict | None:
        """上次搜索的关键词/筛选/结果。

        放在数据目录里而不是前端 localStorage：pywebview 用字符串 HTML 启动，
        存储分区不保证跨启动存活，结果就是用户每次点开谷歌学术都要重搜
        （一次抓取要好几秒，还吃 Google 的限流）。
        """
        try:
            f = BASE_DIR / "scholar_cache.json"
            if not f.exists():
                return None
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.warning("读谷歌学术缓存失败: %s", e)
            return None

    def save_scholar_cache(self, payload: dict) -> bool:
        try:
            p = payload or {}
            data = {
                "query": str(p.get("query") or "")[:200],
                "year": p.get("year") or "",
                "ccf": p.get("ccf") or [],
                "types": p.get("types") or [],
                "items": (p.get("items") or [])[:80],   # 只留前几页，文件不无限涨
                "saved_at": time.time(),
            }
            atomic_write_text(BASE_DIR / "scholar_cache.json",
                              json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            log.warning("写谷歌学术缓存失败: %s", e)
            return False

    # ---------- 标签 / 笔记 ----------
    def set_tags(self, arxiv_id: str, tags: list) -> bool:
        try:
            self.lib.set_tags(arxiv_id, [str(t) for t in tags])
            return True
        except Exception as e:
            log.error("设置标签失败: %s", e)
            return False

    def set_note(self, arxiv_id: str, note: str) -> bool:
        try:
            self.lib.set_note(arxiv_id, note)
            return True
        except Exception as e:
            log.error("保存笔记失败: %s", e)
            return False

    # ---------- 剪贴板 ----------
    def copy_to_clipboard(self, text: str) -> bool:
        """可靠地复制文本到 Windows 剪贴板（支持中文）."""
        import ctypes
        from ctypes import wintypes
        try:
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.EmptyClipboard.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.CloseClipboard.restype = wintypes.BOOL
            data = (text or "").encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.OpenClipboard(None)
            user32.EmptyClipboard()
            user32.SetClipboardData(CF_UNICODETEXT, h)
            user32.CloseClipboard()
            return True
        except Exception as e:
            log.error("复制到剪贴板失败: %s", e)
            return False

    # ---------- AI 摘要 / 翻译（未配置 LLM 时自动用免费翻译+抽取摘要） ----------
    def ai_summarize(self, arxiv_id: str) -> str:
        p = self.lib.get(arxiv_id)
        if not p:
            return ""
        key = p["arxiv_id"]
        if p.get("summary"):
            return p["summary"]
        if key in self._summary_cache:
            return self._summary_cache[key]
        self._push("Arxiver.onProgress('AI 正在生成摘要…')")
        try:
            text = self.llm.summarize(p["title"], p["abstract"] or "")
        finally:
            self._push("Arxiver.onProgress('')")
        if text:
            self._summary_cache[key] = text
            self.lib.set_summary(key, text)
        return text or "AI 摘要失败，请检查 LLM 配置或稍后重试"

    def ai_translate(self, arxiv_id: str) -> str:
        p = self.lib.get(arxiv_id)
        if not p:
            return ""
        self._push("Arxiver.onProgress('AI 正在翻译…')")
        try:
            text = self.llm.translate(p["title"], p["abstract"] or "")
        finally:
            self._push("Arxiver.onProgress('')")
        return text or "AI 翻译失败"

    def ai_bilingual(self, arxiv_id: str) -> dict:
        """双语对照：返回 {title_zh, abstract_zh}（带缓存，作者名不翻译）.

        标题与摘要并发翻译以提速。
        """
        from concurrent.futures import ThreadPoolExecutor
        p = self.lib.get(arxiv_id)
        if not p:
            return {}
        title_zh = p.get("title_zh") or ""
        abstract_zh = p.get("abstract_zh") or ""

        def _title():
            nonlocal title_zh
            if not title_zh:
                t = self.llm.translate_title(p["title"])
                if t:
                    self.lib.set_title_zh(p["arxiv_id"], t)
                    title_zh = t

        def _abs():
            nonlocal abstract_zh
            if not abstract_zh and p.get("abstract"):
                a = self.llm.translate_abstract(p["abstract"])
                if a:
                    self.lib.set_abstract_zh(p["arxiv_id"], a)
                    abstract_zh = a

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_title)
            f2 = ex.submit(_abs)
            f1.result()
            f2.result()
        return {"title_zh": title_zh, "abstract_zh": abstract_zh}

    # ---------- 批量任务（翻译 / AI 摘要）----------
    def _cancel_event(self, kind: str) -> threading.Event:
        """每种批量任务各自一个中断标志。

        以前全进程共用一个 Event：中断翻译后，只要再点一次「AI 摘要」，
        开头那句 clear() 就把翻译的中断状态冲掉了——那批「已中断」的翻译
        又自己跑起来，还会抢同一条进度条。
        """
        return self._cancels.setdefault(kind, threading.Event())

    def cancel_batch(self, kind: str = "") -> dict:
        """中断批量任务。kind = translate / summarize；留空表示全部中断."""
        kinds = [kind] if kind else list(self._cancels)
        for k in kinds:
            self._cancel_event(k).set()
        log.info("请求中断批量任务: %s", kinds or "无")
        return {"ok": True, "cancelled": kinds}

    def _run_batch(self, ids: list[str], one, js_progress: str, js_done: str,
                   name: str, thread: str) -> None:
        """并发跑一批，推进度，最后按 成功/失败/跳过 如实收尾推送。"""
        import json as _json
        from concurrent.futures import ThreadPoolExecutor
        ev = self._cancel_event(name)
        ev.clear()  # 新一轮开始，只清自己这一类
        total = len(ids)
        state = {"done": 0, "ok": 0, "bad": 0, "skip": 0}
        lock = threading.Lock()

        def _task(i: str) -> None:
            if ev.is_set():
                with lock:
                    state["skip"] += 1
                return
            try:
                good = bool(one(i))
            except Exception as e:
                log.warning("%s失败 %s: %s", js_done, i, e)
                good = False
            with lock:
                state["done"] += 1
                state["ok" if good else "bad"] += 1
                done = state["done"]
            self._push(f"Arxiver.{js_progress}({_json.dumps({'done': done, 'total': total})})")

        def _work() -> None:
            try:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    list(ex.map(_task, ids))
            except Exception as e:
                log.exception("%s 异常: %s", js_done, e)
            finally:
                payload = {"done": state["ok"], "failed": state["bad"],
                           "skipped": state["skip"], "total": total,
                           "llm": self.llm.enabled}
                self._push(f"Arxiver.{js_done}({_json.dumps(payload)})")

        threading.Thread(target=_work, name=thread, daemon=True).start()

    def batch_translate(self, arxiv_ids=None) -> dict:
        """批量翻译论文（标题+摘要，并发，带进度推送）.

        不传 arxiv_ids 时自动翻译论文库中所有未翻译的论文（跨页全量）。
        """
        if arxiv_ids:
            ids = [str(i) for i in arxiv_ids if i and not str(i).startswith("local:")]
        else:
            ids = self.lib.ids_pending_translate()
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {"started": False, "count": 0, "msg": "这些论文都已经翻译过了"}

        def one(i: str) -> bool:
            """返回是否真的拿到了译文（拿不到不能算成功）。

            没配 LLM key 时走的是免费翻译源，它挂了以前也照样 +1，
            于是转圈二十秒后弹「翻译完成 300 篇」而实际一篇没翻。
            """
            p = self.lib.get(i)
            if not p:
                return False
            good = True
            if not p.get("title_zh"):
                t = self.llm.translate_title(p["title"])
                if t:
                    self.lib.set_title_zh(i, t)
                else:
                    good = False
            if not p.get("abstract_zh") and p.get("abstract"):
                a = self.llm.translate_abstract(p["abstract"])
                if a:
                    self.lib.set_abstract_zh(i, a)
                else:
                    good = False
            return good

        self._run_batch(ids, one, "onBatchTranslate", "onBatchTranslateDone",
                        "translate", "batch-translate")
        return {"started": True, "count": len(ids)}

    def batch_summarize(self, arxiv_ids=None) -> dict:
        """批量生成 AI 摘要（跨页全量，并发，带进度推送，结果存库）."""
        if arxiv_ids:
            ids = [str(i) for i in arxiv_ids if i and not str(i).startswith("local:")]
        else:
            ids = self.lib.ids_pending_summarize()
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {"started": False, "count": 0, "msg": "这些论文都已经生成过摘要了"}

        def one(i: str) -> bool:
            p = self.lib.get(i)
            if not p:
                return False
            s = p.get("summary") or self.llm.summarize(p["title"], p["abstract"] or "")
            if not s:
                return False
            self.lib.set_summary(i, s)
            return True

        self._run_batch(ids, one, "onBatchSummarize", "onBatchSummarizeDone",
                        "summarize", "batch-summary")
        return {"started": True, "count": len(ids)}

    # ---------- 相似论文 / 引用 ----------
    def similar(self, arxiv_id: str, limit: int = 8) -> list:
        """相似论文（自动入库，携带翻译/摘要缓存，使批量翻译可覆盖）."""
        clean = arxiv_id.split("v")[0]
        if clean.startswith("local:"):
            return []
        if self._s2 is None:
            self._s2 = SemanticScholarClient(self.cfg.get("semanticscholar_key", ""))
        papers = self._s2.related(clean, limit=limit)
        # 相似论文入库（带 arxiv_id 的），批量翻译/摘要自然覆盖它们
        self.lib.upsert([p for p in papers if p.arxiv_id])
        result = []
        for p in papers:
            row = p.to_row()
            if p.arxiv_id:
                rec = self.lib.get(p.arxiv_id)
                if rec:
                    row["title_zh"] = rec.get("title_zh", "")
                    row["abstract_zh"] = rec.get("abstract_zh", "")
                    row["summary"] = rec.get("summary", "")
            result.append(row)
        return result

    def bibtex(self, arxiv_id: str) -> str:
        p = self.lib.get(arxiv_id)
        if not p:
            return ""
        clean = arxiv_id.split("v")[0]
        if clean.startswith("local:"):
            return f"@misc{{{clean.split(':')[-1]},\n  title = {{{p['title']}}},\n  note = {{本地论文}}\n}}"
        year = (p.get("published") or "")[:4] or "????"
        authors = p.get("authors") or ""
        author_list = " and ".join(a.strip() for a in authors.split(",") if a.strip())
        primary = ""
        cats = (p.get("categories") or "").split(",")
        if cats:
            primary = cats[0].strip()
        return (
            f"@article{{{clean},\n"
            f"  title = {{{p['title']}}},\n"
            f"  author = {{{author_list}}},\n"
            f"  year = {{{year}}},\n"
            f"  eprint = {{{clean}}},\n"
            f"  archivePrefix = {{arXiv}},\n"
            f"  primaryClass = {{{primary}}}\n"
            f"}}"
        )

    def set_status(self, arxiv_id: str, status: str) -> bool:
        try:
            self.lib.set_status(arxiv_id, status)
            if status == "starred":
                self.cfg.add_seed(arxiv_id.split("v")[0])
            return True
        except Exception as e:
            log.error("更新状态失败: %s", e)
            return False

    # ---------- 垃圾桶 ----------
    def get_trash(self, limit: int = 200, offset: int = 0) -> list:
        """获取垃圾桶中的论文."""
        return self.lib.get_trash(limit=limit, offset=offset)

    def get_trash_count(self) -> int:
        """获取垃圾桶论文数量."""
        return self.lib.count_trash()

    def restore_trash(self, arxiv_ids: list) -> dict:
        """从垃圾桶恢复论文."""
        ids = [str(i) for i in arxiv_ids if i]
        restored = self.lib.restore_from_trash(ids)
        return {"ok": True, "restored": restored}

    def delete_trash_permanent(self, arxiv_ids: list) -> dict:
        """永久删除垃圾桶中的论文."""
        ids = [str(i) for i in arxiv_ids if i]
        deleted = self.lib.permanent_delete(ids)
        return {"ok": True, "deleted": deleted}

    def trash_scholar(self, doi: str, title: str = "", data: dict | None = None) -> bool:
        """将谷歌学术论文移入垃圾桶（DOI 黑名单）."""
        try:
            self.lib.add_trashed_scholar(doi, title, data)
            return True
        except Exception as e:
            log.error("删除谷歌学术论文失败: %s", e)
            return False

    def restore_scholar(self, doi: str) -> bool:
        """从垃圾桶恢复谷歌学术论文."""
        try:
            self.lib.remove_trashed_scholar(doi)
            return True
        except Exception as e:
            log.error("恢复谷歌学术论文失败: %s", e)
            return False

    def get_trashed_scholar(self) -> list:
        """获取所有已删除的谷歌学术论文."""
        return self.lib.get_trashed_scholar()

    def _fmt_bytes(self, n: int) -> str:
        """字节数格式化为 MB 文本."""
        if n <= 0:
            return "0.0MB"
        return f"{n / 1024 / 1024:.1f}MB"

    def _dl_task_add(self, arxiv_id: str, title: str) -> dict:
        """登记一个下载任务（进行中），插入列表头部并通知前端."""
        task = {
            "id": uuid.uuid4().hex[:8],
            "arxiv_id": arxiv_id,
            "title": title,
            "status": "downloading",   # downloading / done / failed
            "progress": 0,             # 0-100
            "detail": "排队中…",
            "path": "",
            "error": "",
            "size": 0,
            "done_size": 0,
        }
        with self._dl_lock:
            self._dl_tasks.insert(0, task)
        self._push("Arxiver.onDownloadChange()")
        return task

    def _dl_task_update(self, tid: str, **kw) -> None:
        """更新任务字段并通知前端刷新."""
        with self._dl_lock:
            for t in self._dl_tasks:
                if t["id"] == tid:
                    t.update(kw)
                    break
        self._push("Arxiver.onDownloadChange()")

    def get_download_tasks(self) -> list:
        """返回下载任务列表（进行中在前，已完成在后）."""
        with self._dl_lock:
            return [dict(t) for t in self._dl_tasks]

    def clear_download_tasks(self) -> int:
        """清除已完成的下载任务（保留进行中的），返回清除数量."""
        with self._dl_lock:
            before = len(self._dl_tasks)
            self._dl_tasks = [t for t in self._dl_tasks if t["status"] == "downloading"]
            removed = before - len(self._dl_tasks)
        self._push("Arxiver.onDownloadChange()")
        return removed

    def download(self, arxiv_id: str) -> str | None:
        """单篇下载（后台线程），返回任务 id；进度在「下载」tab 查看."""
        p = self.lib.get(arxiv_id)
        if not p:
            return None
        from ..core.models import Paper
        paper = Paper(
            arxiv_id=p["arxiv_id"], title=p["title"], abstract=p["abstract"],
            authors=p["authors"].split(", ") if p["authors"] else [],
            categories=p["categories"].split(", ") if p["categories"] else [],
            published=p["published"], pdf_url=p["pdf_url"], abs_url=p["abs_url"],
        )
        task = self._dl_task_add(arxiv_id, p["title"])

        def _work():
            try:
                _last = {"key": -1}
                def on_bytes(done, total):
                    pct = round(done / total * 100) if total else 0
                    # 节流：total 已知按百分比、未知按每 1MB 推送，避免频繁 evaluate_js
                    key = pct if total else done // (1024 * 1024)
                    if key == _last["key"]:
                        return
                    _last["key"] = key
                    detail = (f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}"
                              if total else f"{self._fmt_bytes(done)} 已下载")
                    self._dl_task_update(task["id"], done_size=done, size=total,
                                         progress=pct, detail=detail)
                path = self.pipeline.downloader.download(paper, on_bytes=on_bytes)
                if path:
                    self._dl_task_update(task["id"], status="done", progress=100,
                                         path=path, detail="已完成")
                else:
                    self._dl_task_update(task["id"], status="failed",
                                         error="下载失败", detail="下载失败")
            except Exception as e:
                log.exception("单篇下载异常: %s", arxiv_id)
                self._dl_task_update(task["id"], status="failed",
                                     error=str(e), detail="下载异常")

        threading.Thread(target=_work, name="dl-one", daemon=True).start()
        return task["id"]

    def download_top(self, limit: int = 5) -> dict:
        """下载相关度最高且还没下载的论文。

        任务先在这里登记好再进线程：点一下立刻能在「下载」页看到队列，
        也把数量返回给前端做反馈（以前只回 {"started": True}，点完没有任何反应）。
        """
        from ..core.models import Paper
        rows = self.lib.get_papers(limit=limit, order="score DESC")
        papers = [Paper(
            arxiv_id=r["arxiv_id"], title=r["title"],
            published=r["published"],
        ) for r in rows if r["arxiv_id"] and not r["local_path"]]
        total = len(papers)
        if not total:
            return {"started": False, "count": 0, "msg": "评分靠前的论文都已下载"}
        tasks = [self._dl_task_add(pp.arxiv_id, pp.title) for pp in papers]

        def _work():
            try:
                for i, (pp, task) in enumerate(zip(papers, tasks), 1):
                    self._dl_task_update(task["id"], detail=f"第 {i}/{total} 篇")
                    try:
                        path = self.pipeline.downloader.download(pp)
                        if path:
                            self._dl_task_update(task["id"], status="done",
                                                 progress=100, path=path, detail="已完成")
                        else:
                            self._dl_task_update(task["id"], status="failed",
                                                 error="下载失败", detail="下载失败")
                    except Exception as e:
                        self._dl_task_update(task["id"], status="failed",
                                             error=str(e), detail="下载异常")
            except Exception as e:
                log.exception("批量下载失败: %s", e)

        threading.Thread(target=_work, name="dl", daemon=True).start()
        return {"started": True, "count": total}

    def get_downloads(self) -> list:
        rows = self.lib.get_papers(limit=500, order="fetched_at DESC")
        return [r for r in rows if r.get("local_path")]

    # ---------- 系统动作 ----------
    def toggle_fullscreen(self) -> None:
        """切换全屏（Esc 键调用，便于用户退出/恢复全屏）."""
        try:
            if self._window is not None:
                self._window.toggle_fullscreen()
        except Exception as e:
            log.error("切换全屏失败: %s", e)

    def open_abs(self, arxiv_id: str) -> None:
        webbrowser.open(f"https://arxiv.org/abs/{arxiv_id.split('v')[0]}")

    def open_url(self, url: str) -> None:
        """在系统浏览器中打开外部链接（DOI 等）."""
        if url and url.startswith("http"):
            webbrowser.open(url)

    def open_pdf(self, arxiv_id: str) -> None:
        p = self.lib.get(arxiv_id)
        if p and p.get("local_path") and os.path.exists(p["local_path"]):
            os.startfile(p["local_path"])  # noqa: S606
        else:
            self._push("Arxiver.onToast('PDF 尚未下载')")

    def open_folder(self, path: str = "") -> None:
        from ..core.downloader import library_base
        target = path or str(library_base(self.cfg))
        if os.path.isdir(target):
            os.startfile(target)  # noqa: S606

    # ---------- 论文库文件夹浏览（需求：论文库 = 本地 papers 文件夹） ----------
    def _match_file_record(self, stem: str) -> dict | None:
        """从文件名反查 SQLite 元数据（arXiv ID / local 哈希两种方式）."""
        import hashlib
        from ..core.downloader import _ARXIV_ID_IN_NAME
        m = _ARXIV_ID_IN_NAME.search(stem)
        if m:
            rec = self.lib.get(m.group(0))
            if rec:
                return rec
        return self.lib.get(
            "local:" + hashlib.md5(stem.encode("utf-8")).hexdigest()[:12])

    def browse_library(self, rel: str = "") -> dict:
        """浏览论文库目录：返回当前目录、子文件夹与 PDF 文件列表（防路径穿越）.

        每个文件尝试关联 SQLite 元数据（备注/标签/分类/重要度/译文标题）。
        """
        import hashlib
        import time as _time
        from pathlib import Path
        from ..core.downloader import library_base
        base = library_base(self.cfg).resolve()
        try:
            target = (base / rel).resolve() if rel else base
        except OSError:
            target = base
        if target != base and base not in target.parents:
            target = base  # 防穿越
        def _rel(p: Path) -> str:
            return str(p.relative_to(base)).replace("\\", "/")

        dirs, files = [], []
        if target.is_dir():
            for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    dirs.append({"name": p.name, "rel": _rel(p)})
                elif p.suffix.lower() == ".pdf":
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    files.append({
                        "name": p.name,
                        "rel": _rel(p),
                        "size_kb": round(st.st_size / 1024, 1),
                        "mtime": _time.strftime("%Y-%m-%d", _time.localtime(st.st_mtime)),
                        "meta": self._match_file_record(p.stem),
                    })
        current = _rel(target) if target != base else ""
        parent = ""
        if target != base:
            parent = _rel(target.parent) if target.parent != base else ""
        return {
            "base": str(base), "current": current, "parent": parent,
            "dirs": dirs, "files": files,
        }

    def open_library_file(self, rel: str) -> None:
        """打开论文库中的 PDF 文件."""
        from pathlib import Path
        from ..core.downloader import library_base
        base = library_base(self.cfg).resolve()
        p = (base / rel).resolve()
        if base not in p.parents and p != base:
            self._push("Arxiver.onToast('非法路径')")
            return
        if p.exists():
            os.startfile(str(p))  # noqa: S606
        else:
            self._push("Arxiver.onToast('文件不存在')")

    # ---------- 文件重命名 / 删除 ----------
    def rename_file(self, rel: str, new_name: str) -> dict:
        """重命名论文库中的文件."""
        from pathlib import Path
        from ..core.downloader import library_base, sanitize_title
        base = library_base(self.cfg).resolve()
        p = (base / str(rel)).resolve()
        if (base not in p.parents and p != base) or not p.is_file():
            return {"ok": False, "msg": "文件不存在"}
        clean = sanitize_title(new_name or "")
        if not clean.lower().endswith(".pdf"):
            clean += ".pdf"
        new_path = p.parent / clean
        if new_path.exists() and new_path != p:
            return {"ok": False, "msg": "同名文件已存在"}
        try:
            rec = self._match_file_record(p.stem)   # 改名前还能用旧文件名反查记录
            p.rename(new_path)
            # 光改磁盘不改库的话：记录的 local_path 指向旧文件名，卡片的
            # 「📖 PDF」打不开，论文库列表的备注/分类/重要度也会全变成「-」
            # （那些信息是靠文件名反查 SQLite 的）。
            if rec and rec.get("arxiv_id"):
                self.lib.mark_downloaded(rec["arxiv_id"], str(new_path))
            return {"ok": True, "name": clean}
        except OSError as e:
            return {"ok": False, "msg": str(e)}

    def delete_files(self, rels: list) -> dict:
        """删除论文库中的文件/文件夹（确认由前端弹窗完成）."""
        import shutil
        from pathlib import Path
        from ..core.downloader import library_base
        base = library_base(self.cfg).resolve()
        deleted, failed = 0, 0
        for rel in (rels or []):
            try:
                p = (base / str(rel)).resolve()
                if base not in p.parents and p != base:
                    failed += 1
                    continue
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    failed += 1
                    continue
                # 文件没了但 local_path 还留着 → 卡片仍显示「📖 PDF」、
                # 首页「已下载」虚高，所以顺手清掉指向这个路径的记录
                self.lib.clear_local_paths(str(p))
                deleted += 1
            except OSError:
                failed += 1
        return {"ok": True, "deleted": deleted, "failed": failed}

    # ---------- 多应用打开（WPS / Chrome / 小绿鲸） ----------
    def open_with(self, app: str, id_or_path: str) -> dict:
        """app: wps / chrome / xlj / default.

        id_or_path：优先按 arxiv_id 查库取 local_path；否则视为论文库相对路径或绝对路径。
        """
        from pathlib import Path
        from ..core import apps as apps_mod
        from ..core.downloader import library_base
        target = ""
        rec = self.lib.get(id_or_path)
        if rec:
            if rec.get("local_path"):
                target = rec["local_path"]
            elif not rec["arxiv_id"].startswith("local:"):
                # 未下载时自动下载到论文库再打开
                from ..core.models import Paper
                paper = Paper(
                    arxiv_id=rec["arxiv_id"], title=rec["title"], abstract=rec["abstract"],
                    authors=rec["authors"].split(", ") if rec["authors"] else [],
                    categories=rec["categories"].split(", ") if rec["categories"] else [],
                    published=rec["published"], pdf_url=rec["pdf_url"], abs_url=rec["abs_url"],
                )
                target = self.pipeline.downloader.download(paper)
                if not target:
                    return {"ok": False, "msg": "下载失败，详见日志"}
        if not target:
            base = library_base(self.cfg)
            p = Path(id_or_path)
            if not p.is_absolute():
                p = base / id_or_path
            p = p.resolve()
            if base not in p.parents and p != base:
                return {"ok": False, "msg": "非法路径"}
            target = str(p)
        if not target or not os.path.exists(target):
            return {"ok": False, "msg": "文件不存在"}
        return apps_mod.open_with(app, target)

    # ---------- 标题翻译 / 新建文件夹 ----------
    def translate_title(self, arxiv_id: str) -> str:
        p = self.lib.get(arxiv_id)
        if not p:
            return ""
        if p.get("title_zh"):
            return p["title_zh"]
        text = self.llm.translate_title(p["title"])
        if text:
            self.lib.set_title_zh(arxiv_id.split("v")[0], text)
        return text or "翻译失败"

    def create_folder(self, rel: str, name: str) -> dict:
        """在论文库指定目录下新建文件夹."""
        from pathlib import Path
        from ..core.downloader import library_base, sanitize_title
        base = library_base(self.cfg).resolve()
        clean = sanitize_title(name or "新建文件夹")
        target = base if not rel else (base / rel).resolve()
        if target != base and base not in target.parents:
            return {"ok": False, "msg": "非法路径"}
        new_dir = target / clean
        try:
            new_dir.mkdir(exist_ok=True)
            return {"ok": True, "name": clean}
        except OSError as e:
            return {"ok": False, "msg": str(e)}

    # ---------- 本地导入（需求：本地上传 = 剪贴移动 + 自动归类） ----------
    def choose_folder(self) -> str | None:
        """目录选择对话框，返回所选路径."""
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if isinstance(res, (tuple, list)):
                return str(res[0]) if res else None
            return str(res) if res else None
        except Exception as e:
            log.error("选择目录失败: %s", e)
            return None

    def upload_papers(self) -> dict:
        """选择本地 PDF（可多选），移动归档进论文库."""
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("PDF 文件 (*.pdf)",))
        except Exception as e:
            log.error("打开文件对话框失败: %s", e)
            return {"imported": [], "failed": [[str(e), "对话框打开失败"]]}
        if not res:
            return {"imported": [], "failed": []}
        files = list(res) if isinstance(res, (tuple, list)) else [str(res)]
        files = [str(f) for f in files if f]
        if not files:
            return {"imported": [], "failed": []}
        self._push("Arxiver.onProgress('正在导入本地论文…')")
        try:
            result = self.pipeline.downloader.import_pdfs(files)
        except Exception as e:
            log.exception("本地导入异常: %s", e)
            result = {"imported": [], "failed": [[str(e), "导入异常"]]}
        self._push("Arxiver.onProgress('')")
        return result


def create_window(cfg: Config | None = None, lib: Library | None = None,
                  hidden: bool = False, no_tray: bool = False):
    cfg = cfg or get_config()
    lib = lib or Library()
    api = Api(cfg, lib)
    html_path = resource_path("ui/static/index.html")
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
    else:
        log.error("index.html 资源缺失: 尝试过 %s", html_path)
        html = "<h1>index.html 缺失</h1>"
    window = webview.create_window(
        "Arxiver — 顶会论文助手",
        html=html,
        js_api=api,
        width=1150,
        height=760,
        min_size=(900, 600),
        hidden=hidden,
        text_select=True,  # 允许拖动选择复制标题/摘要/BibTeX 等文本
    )
    api.attach(window)
    api._embedded = bool(no_tray)   # 内嵌模式才跟随 LifeSystem 主题

    _max_once = {"done": False}

    def _maximize_on_shown():
        """窗口首次显示后最大化（带边框填满屏幕），之后不再干扰用户调整."""
        if _max_once["done"]:
            return
        if hidden:
            # 后台托盘模式（开机自启 --minimized）：pywebview 初始化时会先 Show 再 Hide
            # （为了让 WebView2 渲染），这个 Show 会触发本事件；若此时 maximize 会把隐藏的
            # 窗口弹出来。这里直接跳过，用户从托盘点「打开」时由 _open_window 负责最大化。
            return
        _max_once["done"] = True
        try:
            window.maximize()
            _push_max(True)
        except Exception as e:
            log.warning("最大化窗口失败: %s", e)

    window.events.shown += _maximize_on_shown

    # 最大化 / 还原时通知前端：Windows 最大化后会把窗口描边藏掉，
    # 四周直接贴着屏幕边、看不出边界，前端自己补一圈 1px 框（见 CSS body.maxed）
    def _push_max(on):
        api._push(f"Arxiver.onMaximized({'true' if on else 'false'})")

    window.events.maximized += lambda: _push_max(True)
    window.events.restored += lambda: _push_max(False)

    # 点关闭按钮 → 静默最小化到系统托盘（不弹提示），托盘右键「退出」才真正退出
    def _on_closing():
        if no_tray:
            # 无托盘模式（如被 LifeSystem 内嵌）：关闭即真正退出，避免隐藏成僵尸进程
            log.info("无托盘模式：窗口关闭 → 真正退出")
            return True
        log.info("窗口关闭 → 最小化到系统托盘")
        try:
            window.hide()
        except Exception as e:
            log.warning("隐藏窗口失败: %s", e)
        return False  # 阻止默认关闭

    window.events.closing += _on_closing
    return window, api
