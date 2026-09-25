"""一键跑完 Arxiver 的全部校验。

顺序：后端逻辑 → Api 推送链路 → 前端 DOM → 端到端重放 → 打包产物 → 真实 exe 启动。
后两步需要先跑过 pyinstaller；**没有 dist/Arxiver.exe 会判失败**——exe 就是交付物
本体，用户双击的就是它，缺了它等于什么都没验证。

最后一步会真的启动 exe（隔离数据目录 + 隐藏窗口），冷启动加一次真实同步
大约 1.5~2 分钟，所以整套跑完要几分钟——这是为了守住「装进去 ≠ 跑得起来」。

三条防「静默绿」的规矩（都是踩过坑才加的）：

1. **跳过的步骤留在汇总里**，明确标成「本次未验证」。旧版是把跳过项直接不放进
   results，于是汇总打印「共 7 项，失败 0 项 / 全部通过」——看汇总根本不知道有
   两层没跑。**一个经常被跳过的验证，等于没有这个验证。**
2. **`run()` 除了退出码，还要扫输出里有没有沙箱的批量删除保护消息**
   （`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。被它掐断时进程**也是 0 退出**
   （打包那边已实测），只看 returncode 会把「没跑完」当成通过。
3. **步骤规划抽成纯函数 `plan()` 并自检**（第 0 步）。跳过逻辑最容易悄悄退化，
   抽成纯函数就能用毫秒级断言守住，不用真跑那 4 分钟。

运行: .venv/Scripts/python.exe tests/run_all.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 沙箱的批量删除保护。命中说明测试进程被中途掐断，**不能**当成跑完。
GUARD_MSG = "SAFE_DELETE_BULK_CONFIRM_REQUIRED"

# 全部 9 步，按展示/执行顺序。kind: "py" 用解释器跑，"node" 用 node 跑。
ALL_STEPS = (
    ("客户端：并发抓取 + 上游异常容错", "py", "test_clients_unit.py"),
    ("后端：增量同步（假客户端）", "py", "test_incremental_sync.py"),
    ("后端：引用数按分数取前 N（不是抓取顺序）", "py", "test_citation_order.py"),
    ("后端：Api 推送链路", "py", "test_api_push.py"),
    ("后端：应用启动流程（无 GUI）", "py", "test_app_startup.py"),
    ("后端：交互回归（主题误触发同步/归档误杀/并发写文件等 78 项）", "py", "test_ux_regress.py"),
    ("前端：增量刷新逻辑", "node", "test_live_refresh.js"),
    ("端到端：重放后端推送到 DOM", "node", "test_push_to_dom.js"),
    ("产物：dist/Arxiver.exe 静态校验", "py", "verify_build.py"),
    ("产物：真实 exe 启动 + 增量节奏（约 2 分钟）", "py", "test_exe_launch.py"),
)
# 需要 dist/Arxiver.exe 才能跑的两步
EXE_FILES = {"verify_build.py", "test_exe_launch.py"}


def find_node() -> str | None:
    """找 node。

    托管版本的路径里带版本号（`.../versions/22.22.2-2/node.exe`），**别写死**——
    写死了哪天 node 一升级就找不到，前端两层会静默消失。用 glob 扫所有版本取最新。
    """
    cands = [shutil.which("node")]
    cands += sorted(glob.glob(
        os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "binaries",
                     "node", "versions", "*", "node.exe")))
    for cand in cands:
        if cand and os.path.exists(cand):
            return cand
    return None


def plan(root: str = ROOT, node: str | None = None,
         node_known: bool = False) -> tuple[list, list, bool]:
    """算出这次要跑哪些步骤、跳过哪些、交付物在不在。

    返回 `(steps, skipped, missing_exe)`：`steps` 是 `[(label, cmd), ...]`，
    `skipped` 是 `[label, ...]`（**带编号**，所以汇总里一眼能看出第几项没跑）。

    单独抽成纯函数是为了**可验证**：跳过逻辑最容易悄悄退化（跳过的项从汇总里
    凭空消失），抽出来就能直接断言，不用真跑那 4 分钟。见 `selfcheck()`。
    """
    if not node_known:
        node = find_node()

    missing_exe = not os.path.exists(os.path.join(root, "dist", "Arxiver.exe"))
    total = len(ALL_STEPS)
    steps: list[tuple[str, list[str]]] = []
    skipped: list[str] = []

    for i, (label, kind, fname) in enumerate(ALL_STEPS, 1):
        tag = f"{i}/{total} {label}"
        if kind == "node" and not node:
            skipped.append(tag)            # 没有 node → 前端两层没得跑
            continue
        if fname in EXE_FILES and missing_exe:
            skipped.append(tag)            # 没有 exe → 产物两层没得跑
            continue
        runner = node if kind == "node" else PY
        steps.append((tag, [runner, os.path.join(root, "tests", fname)]))

    return steps, skipped, missing_exe


def run(label: str, cmd: list[str]) -> tuple[str, str, str]:
    """跑一个测试脚本，返回 (label, status, output)。status ∈ {PASS, FAIL}。

    除了退出码，还扫输出里的沙箱保护消息——见模块 docstring 第 2 条。
    """
    print(f"\n{'=' * 62}\n{label}\n{'=' * 62}", flush=True)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    # 过滤掉日志行，只留测试输出
    for line in out.splitlines():
        if "[INFO]" in line or "[WARNING]" in line:
            continue
        print(" ", line)

    if GUARD_MSG in out:
        print(f"  [FAIL] 进程被沙箱的批量删除保护掐断（{GUARD_MSG}）——测试没跑完，"
              f"退出码 {p.returncode} 不可信")
        return label, "FAIL", out
    return label, ("PASS" if p.returncode == 0 else "FAIL"), out


def selfcheck() -> bool:
    """第 0 步：自检「跳过逻辑」不会静默。

    三个方向，缺一不可：
    - 完整环境 → 一步都不许跳过（跳过了说明 node/exe 探测坏了）
    - 空环境   → 产物两层**必须**进 skipped（旧版的病就是它们凭空消失）
    - 无 node  → 前端两层**必须**进 skipped
    """
    print(f"\n{'=' * 62}\n0/0 校验器自检：跳过逻辑不会静默\n{'=' * 62}")
    ok = True

    steps, skipped, _ = plan()
    print(f"  真实环境：跑 {len(steps)} 项，跳过 {len(skipped)} 项")
    if skipped:
        # 真实环境允许有跳过（用户开着应用 / 没装 node），但必须**说出来**
        print("  !    本次真实环境有未验证项（允许，但下面这些层不会被证明）：")
        for label in skipped:
            print(f"         {label}")
    else:
        print(f"  OK   真实环境无跳过项（{len(steps)} 项全跑）")

    def expect(cond: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail and not cond else ""))
        if not cond:
            ok = False

    with tempfile.TemporaryDirectory(prefix="arxiver-test-plan-") as tmp:
        # 数字一律从 ALL_STEPS 推，别写死：写死后每加一个测试就得记得来改这里，
        # 忘了就是「自检本身在骗人」。
        n_total = len(ALL_STEPS)
        n_exe = len(EXE_FILES)

        # 空环境 + 有 node：产物两层应该被跳过，其余照跑
        s, sk, missing = plan(root=tmp, node="/fake/node", node_known=True)
        expect(missing is True, "空环境判定 missing_exe=True", str(missing))
        expect(len(s) == n_total - n_exe, f"空环境仍有 {n_total - n_exe} 项可跑",
               f"实际 {len(s)}")
        expect(len(sk) == n_exe, "产物两层进了 skipped（不是凭空消失）", str(sk))
        expect(all(any(k in t for k in ("静态校验", "真实 exe")) for t in sk),
               "skipped 里确实是被跳过的产物两层", str(sk))

        # 无 node：前端两层也进 skipped
        s2, sk2, _ = plan(root=tmp, node=None, node_known=True)
        expect(len(sk2) == n_exe + 2, f"无 node → 跳过 {n_exe + 2} 项（前端 2 + 产物 2）",
               str(sk2))
        expect(len(s2) == n_total - n_exe - 2, "无 node → 只剩后端各项可跑",
               f"实际 {len(s2)}")

        # 编号必须连续覆盖 1..N，跳过项也在里面（汇总才看得出「第几项没跑」）
        nums = sorted(int(t.split("/")[0]) for t in [l for l, _ in s2] + sk2)
        expect(nums == list(range(1, n_total + 1)),
               f"{n_total} 个编号连续且都出现过", str(nums))

    print("\n自检结果:", "PASS" if ok else "FAIL")
    return ok


def main() -> int:
    if not selfcheck():
        print("\n校验器自检失败——先修 run_all.py 自己，再谈跑测试。")
        return 1

    steps, skipped, missing_exe = plan()

    if skipped:
        print(f"\n[注意] 本次有 {len(skipped)} 项不会跑（汇总里会标成「未验证」）：")
        for label in skipped:
            print(f"  - {label}")
        if any("前端" in t or "DOM" in t for t in skipped):
            print("  （前端两层需要 node：装 node，或改 find_node 的候选路径）")
        if missing_exe:
            print("  （没有 dist/Arxiver.exe —— 先打包，源码改完不打包用户看不到）")

    results = [run(label, cmd) for label, cmd in steps]

    print(f"\n{'=' * 62}\n汇总\n{'=' * 62}")
    for label, status, _ in results:
        print(("  PASS  " if status == "PASS" else "  FAIL  ") + label)
    for label in skipped:
        print("  SKIP  " + label + "   ← 本次未验证")

    failed = [label for label, status, _ in results if status == "FAIL"]
    total = len(results) + len(skipped)
    print(f"\n共 {total} 项：通过 {len(results) - len(failed)}，"
          f"失败 {len(failed)}，未验证 {len(skipped)}")

    if failed:
        print("结果: 有失败")
        return 1
    if missing_exe:
        print("结果: 缺交付物 dist/Arxiver.exe —— 打包后再跑")
        return 1
    if skipped:
        print("结果: 通过，但上面标了「本次未验证」的项没跑 —— 别当成全覆盖")
        return 0
    print("结果: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
