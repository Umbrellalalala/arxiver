"""下载 + 重命名 + 自动归类归档（需求 5 的核心）.

重命名方案：arXiv 下载文件默认是纯 ID 命名，不可读。这里改为
`{标题}_{arXivID}.pdf`；Windows 非法字符 `? ! : * " < > / \\ |` 用
中文全角（？！：＊＂＜＞／＼｜）替换，标题超长则截断并保留 ID 保证唯一可查。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from ..config import Config
from ..paths import LIBRARY_DIR
from .errors import log, retry
from .library import Library
from .models import Paper

__all__ = ["sanitize_title", "build_filename", "archive_dir", "library_base",
           "clean_stale_tmp", "Downloader"]

_ARXIV_ID_IN_NAME = re.compile(r"\d{4}\.\d{4,5}")

# Windows 非法字符 -> 中文全角（需求：感叹号/问号用中文替代，避免查不到）
_ILLEGAL_MAP = {
    "?": "？", "!": "！", ":": "：", "*": "＊", '"': "＂",
    "<": "＜", ">": "＞", "/": "／", "\\": "＼", "|": "｜",
}
_TRAILING = re.compile(r"[ .]+$")   # 结尾空格/点也非法
_MAX_TITLE = 100


def sanitize_title(title: str) -> str:
    """清洗标题为合法文件名（保留可读性，不用下划线糊掉）."""
    t = title.strip()
    for k, v in _ILLEGAL_MAP.items():
        t = t.replace(k, v)
    # 控制字符兜底
    t = "".join(ch for ch in t if ord(ch) >= 32)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > _MAX_TITLE:
        t = t[:_MAX_TITLE].rstrip()
    return _TRAILING.sub("", t) or "untitled"


def build_filename(paper: Paper) -> str:
    cid = paper.clean_id or "nonarxiv"
    return f"{sanitize_title(paper.title)}_{cid}.pdf"


def clean_stale_tmp(base: Path | None = None, older_than_min: int = 60) -> int:
    """清掉下载中断留下的半截 PDF（`归档目录/**/.tmp/*.pdf`），返回删除个数。

    下载失败时只有同一个目录再下一次才会被 finally 扫掉；进程被杀/断电就永远
    没人管了——磁盘上攒一堆看不见也删不掉的半个文件。只删一小时前的，
    避免误删正在写的那个。
    """
    base = Path(base) if base else LIBRARY_DIR
    removed = 0
    cutoff = time.time() - older_than_min * 60
    dirs = [base / ".tmp"] + [p for p in base.rglob("*")
                             if p.is_dir() and p.name == ".tmp"]
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for f in d.glob("*.pdf"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed


def library_base(cfg: Config) -> Path:
    """论文库根路径：用户可在设置中自定义（默认 ~/.arxiver/library）."""
    custom = cfg.get("library_dir", "") or ""
    if custom:
        p = Path(custom)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError as e:
            log.error("论文库路径不可用(%s)，回退默认: %s", custom, e)
    return LIBRARY_DIR


def archive_dir(paper: Paper, cfg: Config) -> Path:
    """下载归档目录：直接放论文库根目录（不再按分类/年份分层）."""
    return library_base(cfg)


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Arxiver/0.1 "
                  "(mailto:research-assistant@example.com)"
}


class Downloader:
    def __init__(self, cfg: Config, lib: Library) -> None:
        self._cfg = cfg
        self._lib = lib

    @retry(times=3, delay=5.0)
    def _download_raw(self, paper: Paper, tmp_dir: Path,
                      on_bytes=None) -> Path:
        """直连 arxiv.org/pdf/{id} 下载（不占用 API 限额），校验 %PDF 魔数.

        on_bytes(done, total)：流式下载的字节进度回调。
        """
        url = paper.pdf_url or f"https://arxiv.org/pdf/{paper.clean_id}"
        tmp = tmp_dir / f"{paper.clean_id}.pdf"
        with httpx.stream("GET", url, headers=_HEADERS, timeout=60.0,
                          follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    done += len(chunk)
                    if on_bytes:
                        try:
                            on_bytes(done, total)
                        except Exception:
                            pass
        if tmp.stat().st_size == 0:
            raise OSError("下载得到空文件")
        with open(tmp, "rb") as f:
            if f.read(4) != b"%PDF":
                raise OSError("响应不是 PDF（可能被限流或跳转到错误页）")
        return tmp

    def download(self, paper: Paper, on_bytes=None) -> str | None:
        """下载单篇并归档重命名。成功返回最终路径，失败返回 None（不抛，记录日志）."""
        if not paper.arxiv_id:
            log.info("跳过非 arXiv 论文: %s", paper.title)
            return None
        dest = archive_dir(paper, self._cfg)
        final_path = dest / build_filename(paper)
        if final_path.exists():
            log.info("已存在，跳过下载: %s", final_path.name)
            self._lib.mark_downloaded(paper.clean_id, str(final_path))
            return str(final_path)
        dest.mkdir(parents=True, exist_ok=True)
        tmp_dir = dest / ".tmp"
        tmp_dir.mkdir(exist_ok=True)
        try:
            raw = self._download_raw(paper, tmp_dir, on_bytes=on_bytes)
            raw.rename(final_path)
        except Exception as e:
            log.error("下载失败 [%s] %s: %s", paper.clean_id, paper.title, e)
            return None
        finally:
            # 清理临时目录残留
            for f in tmp_dir.glob("*.pdf"):
                try:
                    f.unlink()
                except OSError:
                    pass
        self._lib.mark_downloaded(paper.clean_id, str(final_path))
        log.info("已下载归档: %s", final_path)
        return str(final_path)

    def download_many(self, papers: list[Paper],
                      on_progress=None) -> tuple[list[str], list[Paper]]:
        """批量下载。返回 (成功路径列表, 失败论文列表)；on_progress(i, total, paper)."""
        ok, failed = [], []
        total = len(papers)
        for i, p in enumerate(papers, 1):
            try:
                path = self.download(p)
                if path:
                    ok.append(path)
                else:
                    failed.append(p)
            except Exception as e:  # 兜底：单篇失败不影响整体
                log.error("下载异常 %s: %s", p.title, e)
                failed.append(p)
            if on_progress:
                try:
                    on_progress(i, total, p)
                except Exception:
                    pass
        return ok, failed

    # ---------- 本地论文导入（需求：本地上传 = 剪贴移动，非复制） ----------
    def _match_field(self, text: str) -> str | None:
        """按小方向关键词匹配大方向：文件名/标题命中某关键词 → 归档到对应分类."""
        t = text.lower()
        for field, kws in self._cfg.minors().items():
            for kw in kws:
                k = kw.strip().lower()
                if k and k in t:
                    return field
        return None

    def import_pdfs(self, paths: list[str]) -> dict:
        """把本地 PDF 移动（剪贴）进论文库并自动归类、入库.

        - 从文件名提取 arXiv ID（若有），重命名为 `{标题}_{ID}.pdf`
        - 按小方向关键词匹配大方向归类；否则放入 _inbox
        - 全程使用 shutil.move（移动而非复制）
        返回 {"imported": [path...], "imported_dirs": ["归类目录/年份"...],
             "failed": [[name, reason]...]}
        """
        imported: list[str] = []
        imported_dirs: list[str] = []
        failed: list[list[str]] = []
        for raw in paths:
            src = Path(raw)
            try:
                if not src.exists() or src.suffix.lower() != ".pdf":
                    failed.append([src.name, "文件不存在或不是 PDF"])
                    continue
                title = src.stem.strip()
                m = _ARXIV_ID_IN_NAME.search(title)
                aid = m.group(0) if m else ""
                # 归类目录
                field = self._match_field(title) or "_inbox"
                year = str(datetime.now().year)
                dest_dir = library_base(self._cfg) / field / year
                dest_dir.mkdir(parents=True, exist_ok=True)
                # 重命名（沿用非法字符中文全角规则）
                if aid:
                    final = dest_dir / build_filename(
                        Paper(arxiv_id=aid, title=title, source="local"))
                else:
                    final = dest_dir / f"{sanitize_title(title)}.pdf"
                if final.exists():
                    final = dest_dir / f"{sanitize_title(title)}_{uuid.uuid4().hex[:6]}.pdf"
                shutil.move(str(src), str(final))  # 剪贴：移动而非复制
                # 入库
                pid = aid or f"local:{hashlib.md5(final.name.encode('utf-8')).hexdigest()[:12]}"
                p = Paper(
                    arxiv_id=pid, title=title,
                    published=f"{year}-01-01",
                    pdf_url=f"https://arxiv.org/pdf/{aid}" if aid else "",
                    abs_url=f"https://arxiv.org/abs/{aid}" if aid else "",
                    source="local",
                )
                self._lib.upsert([p])
                self._lib.mark_downloaded(pid, str(final))
                imported.append(str(final))
                imported_dirs.append(f"{field}/{year}")
                log.info("本地导入归档: %s", final)
            except Exception as e:  # 单文件失败不影响整体
                log.error("本地导入失败 %s: %s", src.name, e)
                failed.append([src.name, str(e)])
        # 去重保序：前端要告诉用户这批文件到底被移动到哪儿了
        dirs = list(dict.fromkeys(imported_dirs))
        return {"imported": imported, "imported_dirs": dirs, "failed": failed}
