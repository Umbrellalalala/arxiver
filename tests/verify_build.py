"""打包产物校验：确认 dist/Arxiver.exe 里真的是最新代码（确定性，不靠猜）。

PyInstaller onefile 里有两类东西：
- data 文件（前端 index.html 等）→ 存在 CArchive 里，可按字节比对；
- Python 模块 → 存在内嵌的 PYZ 归档里，是 code object，只能扫名字/常量。

注意：嵌套函数的局部变量会被编译进 co_cellvars（被闭包引用时），
只扫 co_consts / co_names / co_varnames 会漏，必须一并扫。

**标记扫描有个盲区**：它只能证明「这几个特征在 exe 里」，证明不了「exe 就是
当前源码打的」。改完源码忘了打包时，标记照样全中 → 校验报 PASS，而用户双击的
还是旧 exe。所以第 0 节先查**产物新鲜度**：打包范围内的源码不能比 exe 新。

运行: .venv/Scripts/python.exe tests/verify_build.py
"""
import hashlib
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "Arxiver.exe")

# 会被打包进 exe 的东西（tests/ 不算——它们不打包，改了不影响交付物）
SRC_PATHS = ("arxiver", "assets", "run.py", "Arxiver.spec")

# 本次改动应出现在产物里的特征
DATA_FILES = {
    "arxiver\\ui\\static\\index.html": "arxiver/ui/static/index.html",
}
DATA_MARKERS = ["onNewPapers", "scheduleLiveRefresh", "刚抓取", ".card.fresh", "liveGot=0"]

MODULE_MARKERS = {
    # _MAX_SEEDS 是「种子推荐被 seeds[:3] 静默截断」的修复特征；
    # _MAX_CITATION_IDS 是「引用数按抓取顺序取前 50」的修复特征
    "arxiver.core.pipeline": ["on_papers", "sync", "Pipeline", "hf_state",
                              "热榜抓取超时", "_MAX_SEEDS", "_MAX_CITATION_IDS"],
    # _MAX_RSS_WORKERS / _fetch_rss_retry / _MAX_KEYWORDS 是「分类截断 bug」
    # 「单分类失败无重试」「关键词静默截断」的修复特征：
    # 旧产物里没有这几个名字，说明分类仍被砍到 4 个、失败即静默丢弃
    "arxiver.core.clients.arxiv_client": ["on_batch", "search_keywords", "ThreadPoolExecutor",
                                          "_MAX_RSS_WORKERS", "_fetch_rss_retry", "_RSS_TIMEOUT",
                                          "_MAX_BACKFILL_CATS", "_MAX_KEYWORDS",
                                          "_MAX_IDS_PER_QUERY"],
    # hf-mirror.com / HF_ENDPOINT 是端点回退的关键特征：huggingface.co 在国内被
    # DNS 污染，没有这两样热榜永远拿不到东西
    "arxiver.core.clients.hf_daily": ["on_batch", "_get_daily_ex", "hf-mirror.com",
                                      "HF_ENDPOINT", "CONNECT_TIMEOUT"],
    # _S2_BATCH / _citations_batch 是「引用数超过 100 个 ID 静默截断」的修复特征：
    # 旧产物里只有单次请求，第 101 篇之后永远拿不到引用数
    "arxiver.core.clients.semantic_scholar": ["citations", "related", "未收录",
                                              "_S2_BATCH", "_citations_batch"],
    "arxiver.ui.webui": ["notify_new_papers", "sync_range", "_push_lock"],
    "arxiver.core.scheduler": ["on_papers", "on_done"],
    "arxiver.app": ["notify_new_papers", "on_papers"],
}


def collect_names(code, out, depth=0):
    """递归收集一个 code object 里所有可见的标识符（含闭包 cell 变量）."""
    if depth > 10:
        return
    out.update(code.co_names)
    out.update(code.co_varnames)
    out.update(getattr(code, "co_cellvars", ()))
    out.update(getattr(code, "co_freevars", ()))
    for c in code.co_consts:
        if isinstance(c, str):
            out.add(c)
        elif hasattr(c, "co_consts"):
            collect_names(c, out, depth + 1)


def check_freshness(exe_mtime: float) -> bool:
    """第 0 节：打包范围内的源码**不能**比 exe 新。

    为什么必须有这条：下面的标记扫描只能证明「这几个特征在 exe 里」，
    证明不了「exe 就是当前源码打的」。改完源码忘了打包时标记照样全中，
    校验报 PASS，而用户双击的还是旧 exe——**这是本项目最容易翻车的地方**
    （用户不跑源码，前端 index.html 也是打包进 exe 的 data 文件）。

    容差 1 秒：打包是读源码生成 exe，源码 mtime 必然早于 exe；
    给 1 秒是防文件系统时间戳粒度造成的假阳性。
    """
    stale: list[tuple[float, str]] = []
    for rel in SRC_PATHS:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            files = [p]
        elif os.path.isdir(p):
            files = [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs]
        else:
            continue
        for f in files:
            if "__pycache__" in f or f.endswith((".pyc", ".pyo")):
                continue
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if mt > exe_mtime + 1:
                stale.append((mt, os.path.relpath(f, ROOT)))

    if stale:
        stale.sort(reverse=True)
        print(f"  [FAIL] 有 {len(stale)} 个源码比 exe 新 —— exe 是旧的，先重新打包")
        for mt, rel in stale[:8]:
            print(f"         {datetime.fromtimestamp(mt):%H:%M:%S}  {rel}")
        if len(stale) > 8:
            print(f"         …… 另有 {len(stale) - 8} 个")
        print("         （用户双击的是 exe，不重新打包他完全看不到改动）")
        return False
    print("  OK   打包范围内没有源码比 exe 新")
    return True


def main() -> int:
    if not os.path.exists(EXE):
        print(f"[FAIL] 找不到产物 {EXE}")
        return 1
    size = os.path.getsize(EXE)
    print(f"产物: {EXE}\n大小: {size / 1024 / 1024:.1f} MB")

    ok = True

    print("\n== 0. 产物新鲜度（源码有没有比 exe 新） ==")
    if not check_freshness(os.path.getmtime(EXE)):
        ok = False

    arc = CArchiveReader(EXE)

    print("\n== 1. 前端 data 文件（与磁盘源码逐字节比对） ==")
    for entry, disk_path in DATA_FILES.items():
        try:
            bundled = arc.extract(entry)
        except KeyError:
            print(f"  [FAIL] 归档里找不到 {entry}")
            ok = False
            continue
        with open(disk_path, "rb") as f:
            disk = f.read()
        same = bundled == disk
        print(f"  {'OK  ' if same else 'FAIL'} {os.path.basename(disk_path)} "
              f"sha256={'一致' if same else '不一致'} ({len(bundled)} 字节)")
        if not same:
            ok = False
        text = bundled.decode("utf-8", "ignore")
        for mk in DATA_MARKERS:
            hit = mk in text
            ok = ok and hit
            print(f"       {'OK  ' if hit else 'MISS'} 特征 {mk!r}")

    print("\n== 2. Python 模块（扫 PYZ 里的 code object） ==")
    pyz = arc.open_embedded_archive("PYZ.pyz")
    for mod, needles in MODULE_MARKERS.items():
        try:
            code = pyz.extract(mod)
        except KeyError:
            print(f"  [FAIL] PYZ 里找不到 {mod}")
            ok = False
            continue
        found = set()
        collect_names(code, found)
        miss = [n for n in needles if not any(n in x for x in found)]
        ok = ok and not miss
        print(f"  {'OK  ' if not miss else 'FAIL'} {mod}"
              + (f"  缺少 {miss}" if miss else ""))

    print("\n产物校验:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
