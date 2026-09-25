"""本地论文库（SQLite）：元数据 + 状态（新/已读/收藏/忽略/垃圾桶）+ 多标签 + 笔记 + 下载记录."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from ..paths import BACKUP_DIR, DB_PATH, ensure_dirs
from .errors import log
from .models import Paper

__all__ = ["Library"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    abstract    TEXT DEFAULT '',
    authors     TEXT DEFAULT '',
    categories  TEXT DEFAULT '',
    published   TEXT DEFAULT '',
    updated     TEXT DEFAULT '',
    pdf_url     TEXT DEFAULT '',
    abs_url     TEXT DEFAULT '',
    upvotes     INTEGER DEFAULT 0,
    citations   INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'arxiv',
    status      TEXT DEFAULT 'new',       -- new / read / starred / ignored / trash / archived
    score       REAL DEFAULT 0,
    local_path  TEXT DEFAULT '',
    fetched_at  REAL DEFAULT 0,
    note        TEXT DEFAULT '',          -- 用户笔记（markdown）
    tags        TEXT DEFAULT ''           -- 多标签，存储为 ",tag1,tag2,"（带边界）
);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);
-- 列表查询固定是「按状态筛 + 按分数/日期排 + 取一页」，库里上万行时
-- 单列索引帮不上 ORDER BY，这两个复合索引才是分页不卡的关键。
CREATE INDEX IF NOT EXISTS idx_papers_status_score ON papers(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_papers_status_pub ON papers(status, published DESC);
-- 谷歌学术已删除论文 DOI 黑名单（同步/搜索时去重）
CREATE TABLE IF NOT EXISTS trashed_scholar (
    doi         TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    data        TEXT DEFAULT '',          -- JSON 序列化的完整卡片数据
    trashed_at  REAL DEFAULT 0
);
"""


def _encode_tags(tags: list[str]) -> str:
    clean = sorted({t.strip() for t in tags if t and t.strip()})
    return "," + ",".join(clean) + "," if clean else ""


def _decode_tags(s: str) -> list[str]:
    return [t for t in (s or "").split(",") if t.strip()]


def _like_escape(v: str) -> str:
    """把字符串变成 LIKE 里的字面量（转义符固定为 `!`）。

    先转义 `!` 本身，再转义 `%` 和 `_`，顺序反了会把刚补上的 `!` 再转一次。
    """
    return v.replace("!", "!!").replace("%", "!%").replace("_", "!_")


class Library:
    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：读写不互相阻塞，且崩溃后能自动回滚未提交事务；
        # busy_timeout：多实例/并发写时排队等待而不是立刻报 database is locked
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _backup_db(self) -> None:
        """结构变更（ALTER）前用 SQLite 在线备份 API 留一份一致快照。"""
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            dst_path = BACKUP_DIR / "library.db.bak"
            dst = sqlite3.connect(str(dst_path))
            try:
                self._conn.backup(dst)
            finally:
                dst.close()
        except (OSError, sqlite3.Error) as e:
            # 这是**改表前的安全网**：备份没成还继续 ALTER 的话，一旦改坏就无从回滚。
            # 不能静默——用户会丢库却看不到任何线索。
            log.warning("库结构变更前备份失败（仍会继续，但本次无法回滚）: %s", e)

    def _migrate(self) -> None:
        """为老库补充新增列（结构变更前自动备份，便于回滚）."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(papers)")}
        needed = [
            ("note", "TEXT DEFAULT ''"),
            ("tags", "TEXT DEFAULT ''"),
            ("title_zh", "TEXT DEFAULT ''"),
            ("abstract_zh", "TEXT DEFAULT ''"),
            ("summary", "TEXT DEFAULT ''"),
        ]
        missing = [(n, t) for n, t in needed if n not in cols]
        if missing:
            self._backup_db()
            for name, decl in missing:
                self._conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
        # 确保 trashed_scholar 表存在（旧库升级）
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS trashed_scholar ("
            "doi TEXT PRIMARY KEY,title TEXT DEFAULT '',data TEXT DEFAULT '',trashed_at REAL DEFAULT 0)")
        # 一次性清理：RSS 通道曾把 "arXiv:xxx Announce Type: ... Abstract:" 前缀存进摘要，
        # 也会污染 abstract_zh（翻译缓存）。兼容中英文、无空格粘连、HTML 标签等变体。
        try:
            import re as _re
            _ann = _re.compile(
                r"^arXiv\s*[：:]\s*\d{4}\.\d{4,5}(?:v\d+)?\s*"
                r"(?:Announce\s*Type|公告类型)\s*[：:]\s*"
                r"(?:new|replace|cross|withdraw|新|替换|交叉|撤回)\s*"
                r"(?:Abstract|摘要)\s*[：:]?\s*",
                _re.IGNORECASE)
            _fb = _re.compile(
                r"^arXiv\s*[：:]\s*\d{4}\.\d{4,5}(?:v\d+)?\s*[^\w]*", _re.IGNORECASE)
            _html = _re.compile(r"<[^>]+>")
            for _col in ("abstract", "summary", "abstract_zh", "title_zh"):
                _rows = self._conn.execute(
                    f"SELECT arxiv_id, {_col} FROM papers WHERE {_col} LIKE 'arXiv%'"
                ).fetchall()
                for _r in _rows:
                    _s = _html.sub("", _r[1] or "")
                    _s = _ann.sub("", _s)
                    if _s.startswith("arXiv") or _s.startswith("arxiv"):
                        _s = _fb.sub("", _s)
                    _clean = _s.strip()
                    if _clean != (_r[1] or "").strip():
                        self._conn.execute(
                            f"UPDATE papers SET {_col}=? WHERE arxiv_id=?", (_clean, _r[0]))
        except Exception as e:
            # 一次性历史数据清洗，失败不影响可用性，但**不能静默**——
            # 否则「摘要里一直带着 arXiv:xxx Announce Type 前缀」永远查不出原因。
            log.warning("历史摘要前缀清洗失败（不影响使用）: %s", e)

    # ---------- 写 ----------

    def upsert(self, papers: list[Paper]) -> int:
        """插入或更新论文元数据（不覆盖用户改过的状态/本地路径），跳过垃圾桶中的论文，返回新增数.

        自动归档（archived）的论文若又被抓回来，会放回推荐池。
        """
        added = 0
        with self._lock:
            for p in papers:
                row = p.to_row()
                if not row["arxiv_id"]:
                    continue
                cur = self._conn.execute(
                    "SELECT status, source, published FROM papers WHERE arxiv_id=?",
                    (row["arxiv_id"],)
                )
                existing = cur.fetchone()
                if existing and existing["status"] == "trash":
                    continue  # 垃圾桶论文不同步覆盖
                if existing:
                    # 又被数据源抓回来，说明它现在还在被推荐：先前自动归档的放回
                    # 推荐池。用户主动改过的状态（收藏/已读/忽略）一律不动。
                    keep = "new" if existing["status"] == "archived" else existing["status"]
                    # 来源只往「更权威」的方向改：arXiv 原文行不能被推荐/热榜源
                    # 认成自己的，否则卡片上的来源标记会随每次同步乱跳。
                    src = row["source"]
                    pub = row["published"]
                    if src != "arxiv" and existing["source"] == "arxiv":
                        src = existing["source"]
                    # S2 经常只给年份，norm_date 补成 "2026-01-01" 之后长度和真
                    # 日期一样，看不出是猜的。所以干脆规定：已有的日期不被 S2
                    # 改写——它没有信息量，改了只会把 3 月发的论文显示成 1 月。
                    # 判据用**进来的** row["source"]，src 上面可能已被改写成 arxiv。
                    if row["source"] == "s2" and existing["published"]:
                        pub = existing["published"]
                    self._conn.execute(
                        """UPDATE papers SET title=?, abstract=?, authors=?, categories=?,
                           published=?, updated=?, pdf_url=?, abs_url=?, upvotes=?,
                           citations=?, source=?, score=?, fetched_at=?, status=?
                           WHERE arxiv_id=?""",
                        (row["title"], row["abstract"], row["authors"], row["categories"],
                         pub, row["updated"], row["pdf_url"], row["abs_url"],
                         row["upvotes"], row["citations"], src, row["score"],
                         time.time(), keep, row["arxiv_id"]),
                    )
                else:
                    self._conn.execute(
                        """INSERT INTO papers (arxiv_id, title, abstract, authors, categories,
                           published, updated, pdf_url, abs_url, upvotes, citations,
                           source, status, score, local_path, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row["arxiv_id"], row["title"], row["abstract"], row["authors"],
                         row["categories"], row["published"], row["updated"], row["pdf_url"],
                         row["abs_url"], row["upvotes"], row["citations"], row["source"],
                         "new", row["score"], "", time.time()),
                    )
                    added += 1
            self._conn.commit()
        return added

    def set_status(self, arxiv_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET status=? WHERE arxiv_id=?",
                (status, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def set_score(self, arxiv_id: str, score: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET score=? WHERE arxiv_id=?",
                (score, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def mark_downloaded(self, arxiv_id: str, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET local_path=? WHERE arxiv_id=?",
                (path, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def clear_local_paths(self, path: str) -> int:
        """清掉指向该文件（或该目录之下）的 local_path，返回受影响行数。

        磁盘上删了/改了名却不更新库的话：卡片仍显示「📖 PDF」但点开报「尚未下载」，
        首页「已下载」计数也一直虚高。
        """
        # 路径本身要当字面量匹配：库目录里到处都是 `_inbox`，而 LIKE 的 `_` 是
        # 「任意一个字符」，不转义的话 `!_inbox` 能匹配到 `Xinbox`，把不相干论文
        # 的 local_path 一起清空。转义符用 `!` 不用 `\\`——Windows 路径里全是反斜杠。
        esc = _like_escape(path)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE papers SET local_path='' "
                "WHERE local_path=? "
                "OR local_path LIKE ? ESCAPE '!' OR local_path LIKE ? ESCAPE '!'",
                (path, esc + "/%", esc + "\\%"),
            )
            self._conn.commit()
            return cur.rowcount

    # ---------- 标签 / 笔记 ----------
    def set_tags(self, arxiv_id: str, tags: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET tags=? WHERE arxiv_id=?",
                (_encode_tags(tags), arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def set_note(self, arxiv_id: str, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET note=? WHERE arxiv_id=?",
                (note, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def set_title_zh(self, arxiv_id: str, title_zh: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET title_zh=? WHERE arxiv_id=?",
                (title_zh, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def set_abstract_zh(self, arxiv_id: str, abstract_zh: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET abstract_zh=? WHERE arxiv_id=?",
                (abstract_zh, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def set_summary(self, arxiv_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET summary=? WHERE arxiv_id=?",
                (summary, arxiv_id.split("v")[0]),
            )
            self._conn.commit()

    def all_tags(self) -> list[dict]:
        """返回 [{"name": tag, "count": n}]，按数量降序."""
        with self._lock:
            rows = self._conn.execute("SELECT tags FROM papers").fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            for t in _decode_tags(r["tags"]):
                counter[t] = counter.get(t, 0) + 1
        return [{"name": k, "count": v}
                for k, v in sorted(counter.items(), key=lambda x: -x[1])]

    # ---------- 读 ----------
    def _paper_filters(self, status: str | None = None, query: str = "", tag: str = "",
                       since_days: int = 0, min_score: float = 0,
                       date_from: str = "", date_to: str = "",
                       include_trash: bool = False) -> tuple[str, list]:
        """get_papers 与 count_papers **共用**的 WHERE 条件。

        分开写两份的话，分页总数和列表内容迟早会对不上（一边加了条件另一边忘了）。
        """
        conds: list[str] = []
        args: list = []
        if status:
            conds.append("status=?")
            args.append(status)
        elif not include_trash:
            # 默认视图既不看垃圾桶，也不看自动归档掉的旧论文（那边有单独入口）
            conds.append("status NOT IN ('trash','archived')")
        if query:
            conds.append("(title LIKE ? OR abstract LIKE ?)")
            args += [f"%{query}%", f"%{query}%"]
        if tag:
            conds.append("tags LIKE ?")
            args.append(f"%,{tag},%")
        if since_days and since_days > 0:
            cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
            conds.append("published >= ?")
            args.append(cutoff)
        if min_score and min_score > 0:
            conds.append("score >= ?")
            args.append(min_score)
        if date_from:
            conds.append("published >= ?")
            args.append(date_from)
        if date_to:
            conds.append("published <= ?")
            args.append(date_to)
        return (" WHERE " + " AND ".join(conds) if conds else ""), args

    def count_papers(self, status: str | None = None, query: str = "", tag: str = "",
                     since_days: int = 0, min_score: float = 0,
                     date_from: str = "", date_to: str = "",
                     include_trash: bool = False) -> int:
        """按列表同样的筛选条件统计总条数（分页器用）."""
        where, args = self._paper_filters(
            status, query, tag, since_days, min_score, date_from, date_to, include_trash)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM papers{where}", args).fetchone()
        return row[0] if row else 0

    def ids_pending_translate(self) -> list[str]:
        """还没翻译（标题或摘要缺中文）的 ID；只看需要的列，不捞整行.

        「全部翻译」以前先 `SELECT *` 拉全库（含摘要正文，一天涨 400 篇时
        几十 MB）再在 Python 里筛，只为拿到一串 ID。
        """
        return self._ids_pending("(IFNULL(title_zh,'')='' OR IFNULL(abstract_zh,'')='')")

    def ids_pending_summarize(self) -> list[str]:
        """还没生成 AI 摘要的 ID."""
        return self._ids_pending("IFNULL(summary,'')=''")

    def _ids_pending(self, cond: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT arxiv_id FROM papers "
                "WHERE status NOT IN ('trash','archived') AND arxiv_id NOT LIKE 'local:%' "
                f"AND arxiv_id != '' AND {cond} ORDER BY score DESC").fetchall()
        return [r[0] for r in rows]

    def archive_stale(self, days: int) -> int:
        """把「没人理」的老论文挪到 archived，返回归档条数（不删任何数据）。

        推荐池每天涨 300+ 条，一年十几万：列表、计数、下载与「全部翻译」的队列
        都会被这些早就没人翻的条目拖住。归档只改状态——首页筛选切到
        「已归档」还能看到，收藏过/下载过/打过标签笔记的一律不动。

        判据是 fetched_at（进池时间）而不是 published：各源的日期格式不统一，
        Semantic Scholar 只给年份 "2026"，按字符串比 "2026" < "2026-08-26" 成立，
        实测把当天刚抓到的 52 篇种子推荐**全部**归档了，等于这个功能白做。
        用 fetched_at 也更符合本意（「在池子里躺了 N 天没人管」），而且每次被
        重新抓到都会刷新，仍在被推荐的自然不会归档。fetched_at<=0 是脏数据，
        跳过而不是当成远古时间。
        """
        if days <= 0:
            return 0
        cutoff_ts = time.time() - int(days) * 86400
        with self._lock:
            cur = self._conn.execute(
                "UPDATE papers SET status='archived' "
                "WHERE status='new' AND IFNULL(fetched_at,0)>0 AND fetched_at<? "
                "AND IFNULL(local_path,'')='' AND IFNULL(tags,'')='' AND IFNULL(note,'')=''",
                (cutoff_ts,))
            self._conn.commit()
            return cur.rowcount

    def get_papers(self, status: str | None = None, limit: int = 100,
                   offset: int = 0, query: str = "", order: str = "score DESC",
                   tag: str = "", since_days: int = 0,
                   min_score: float = 0, date_from: str = "", date_to: str = "",
                   include_trash: bool = False) -> list[dict]:
        where, args = self._paper_filters(
            status, query, tag, since_days, min_score, date_from, date_to, include_trash)
        order_col = order if order in (
            "score DESC", "upvotes DESC", "citations DESC", "published DESC", "fetched_at DESC"
        ) else "score DESC"
        # 必须再加一个唯一列做 tiebreaker：热度/引用数大量并列（一堆 0），
        # 只按它们排的话 LIMIT/OFFSET 分页会出现「同一篇在第 1 页和第 2 页都出现、
        # 或者哪页都不出现」——服务端分页之后这个才会暴露出来。
        sql = f"SELECT * FROM papers{where} ORDER BY {order_col}, arxiv_id LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, args + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    def get_trash(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """返回垃圾桶中的论文（按 trash 时间降序）."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM papers WHERE status='trash' ORDER BY fetched_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_trash(self) -> int:
        """返回垃圾桶论文数量."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM papers WHERE status='trash'").fetchone()
        return row[0] if row else 0

    def restore_from_trash(self, arxiv_ids: list[str]) -> int:
        """将论文从垃圾桶恢复为 new，返回实际恢复数量."""
        restored = 0
        with self._lock:
            for aid in arxiv_ids:
                clean = (aid or "").split("v")[0]
                if clean:
                    cur = self._conn.execute(
                        "UPDATE papers SET status='new' WHERE arxiv_id=? AND status='trash'", (clean,))
                    restored += cur.rowcount
            self._conn.commit()
        return restored

    def permanent_delete(self, arxiv_ids: list[str]) -> int:
        """永久删除论文（从数据库移除），返回实际删除数量."""
        deleted = 0
        with self._lock:
            for aid in arxiv_ids:
                clean = (aid or "").split("v")[0]
                if clean:
                    cur = self._conn.execute("DELETE FROM papers WHERE arxiv_id=?", (clean,))
                    deleted += cur.rowcount
            self._conn.commit()
        return deleted

    # ---------- 谷歌学术已删除论文管理 ----------
    def add_trashed_scholar(self, doi: str, title: str = "", data: dict | None = None) -> None:
        """将谷歌学术论文加入删除黑名单."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trashed_scholar (doi, title, data, trashed_at) VALUES (?,?,?,?)",
                (doi, title, json.dumps(data or {}, ensure_ascii=False), time.time()))
            self._conn.commit()

    def remove_trashed_scholar(self, doi: str) -> None:
        """从删除黑名单移除."""
        with self._lock:
            self._conn.execute("DELETE FROM trashed_scholar WHERE doi=?", (doi,))
            self._conn.commit()

    def get_trashed_scholar_dois(self) -> set[str]:
        """返回已删除的谷歌学术 DOI 集合."""
        with self._lock:
            rows = self._conn.execute("SELECT doi FROM trashed_scholar").fetchall()
        return {r["doi"] for r in rows if r["doi"]}

    def get_trashed_scholar(self) -> list[dict]:
        """返回所有已删除的谷歌学术记录."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trashed_scholar ORDER BY trashed_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except Exception:
                d["data"] = {}
            result.append(d)
        return result

    def get(self, arxiv_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id.split("v")[0],)
            ).fetchone()
        return dict(row) if row else None

    def is_downloaded(self, arxiv_id: str) -> bool:
        p = self.get(arxiv_id)
        return bool(p and p.get("local_path"))

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status NOT IN ('trash','archived')"
            ).fetchone()[0]
            downloaded = self._conn.execute(
                "SELECT COUNT(*) FROM papers WHERE local_path!='' "
                "AND status NOT IN ('trash','archived')").fetchone()[0]
            starred = self._conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status='starred'").fetchone()[0]
            trash = self._conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status='trash'").fetchone()[0]
            archived = self._conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status='archived'").fetchone()[0]
        # total 只算「活跃推荐池」：把自动归档的也算进来，这个数字就会一直涨，
        # 而这个数字正是用户判断「今天有没有新东西」的依据。
        return {"total": total, "downloaded": downloaded, "starred": starred,
                "trash": trash, "archived": archived}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
