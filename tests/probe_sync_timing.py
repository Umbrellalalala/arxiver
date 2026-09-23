"""实测一次真实同步各阶段的耗时，用来判断「慢」到底慢在哪。

不做任何 mock，打真网络，写隔离的 ARXIVER_HOME。
输出每一批论文到达的时间点 + 各阶段耗时，据此决定要不要做并行抓取。

**为什么要有 `--real-config`**：默认配置只有 3 个大方向、没有种子论文，
而用户的真实画像可能是 5 个大方向 + 3 篇种子。种子推荐是**串行**的，
每个还带 3 次重试——画像不同，总时长能差一倍。拿默认画像测出来的数字
不能代表用户的实际体验。

`--real-config` 会把 `~/.arxiver/config.json` 复制进隔离目录（只保留画像相关字段，
并把会碰真实环境/产生副作用的开关全部关掉），**绝不写用户的数据**。

运行: .venv/Scripts/python.exe tests/probe_sync_timing.py [days] [--real-config]
"""
import json
import os
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _testenv import isolated_home  # noqa: E402

HOME = isolated_home("probe")   # 必须在 import arxiver.* 之前

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}
DAYS = int(ARGS[0]) if ARGS else 1

if "--real-config" in FLAGS:
    src = os.path.expanduser("~/.arxiver/config.json")
    if os.path.exists(src):
        cfg_in = json.load(open(src, encoding="utf-8"))
        # 关掉一切会碰真实环境或产生副作用的开关
        for k, v in (("auto_download", False), ("notify", False),
                     ("desktop_shortcut", False), ("autostart", False)):
            cfg_in[k] = v
        with open(os.path.join(HOME, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg_in, f, ensure_ascii=False)
        print(f"已载入真实画像：{src}")
    else:
        print(f"找不到 {src}，退回默认配置")

from arxiver.config import get_config              # noqa: E402
from arxiver.core.errors import setup_logging      # noqa: E402
from arxiver.core.library import Library           # noqa: E402
from arxiver.core.pipeline import Pipeline         # noqa: E402


def main() -> int:
    setup_logging()
    cfg = get_config()
    lib = Library()
    pipe = Pipeline(cfg, lib)

    print(f"画像：大方向={cfg.majors()}  小方向关键词={len(cfg.keywords())} 个  "
          f"种子={len(cfg.get('seed_papers', []))} 篇")
    print(f"抓取范围：近 {DAYS} 天\n")

    t0 = time.time()
    batches: list[tuple[float, str, int, int]] = []
    marks: list[tuple[float, str]] = []

    def on_papers(papers, new):
        batches.append((time.time() - t0, "?", len(papers), new))

    def on_progress(text, pct):
        marks.append((time.time() - t0, text))

    stat = pipe.sync(days=DAYS, auto_download=False,
                     on_progress=on_progress, on_papers=on_papers)
    total = time.time() - t0

    print("== 论文到达时间线（每条 = 一批入库并推送到界面） ==")
    prev = 0.0
    for i, (t, _lab, cnt, new) in enumerate(batches, 1):
        print(f"  {i:>2}. {t:>6.1f}s  (+{t - prev:>5.1f}s)  本批 {cnt:>3} 篇  新增 {new:>3} 篇")
        prev = t
    print(f"\n  首批出现在 {batches[0][0]:.1f}s" if batches else "  没有任何批次")

    print("\n== 阶段进度（后端文案） ==")
    prev = 0.0
    for t, text in marks:
        print(f"  {t:>6.1f}s  (+{t - prev:>5.1f}s)  {text}")
        prev = t

    print(f"\n== 总计 ==")
    print(f"  全部完成: {total:.1f}s")
    print(f"  用户第一次看到论文: {batches[0][0]:.1f}s（{batches[0][0] / total * 100:.0f}% 处）")
    print(f"  统计: {stat}")

    # 各阶段净耗时（用进度文案的时间戳近似）
    print("\n== 各阶段净耗时 ==")
    stage_start = 0.0
    for t, text in marks:
        if text.startswith(("arXiv 大方向完成", "关键词检索完成", "热榜完成",
                            "补充引用数", "入库完成")):
            print(f"  {t - stage_start:>6.1f}s  → {text}")
            stage_start = t
    print(f"  {total - stage_start:>6.1f}s  → 收尾")

    lib.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
