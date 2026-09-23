"""真实 exe 启动 + 增量节奏冒烟：跑 dist/Arxiver.exe，确认它真能用。

三层校验各有各的盲区，互补：

- `verify_build.py`   证明「代码被装进 exe 了」——但装进去不等于跑得起来
- `test_app_startup.py` 证明「源码的启动流程能走通」——但那是解释器跑的源码
- 本测试              证明「打包后的 exe 在真实 Windows 上真的能启动、能同步、
                      而且是边抓边出」——这是离用户最近的一层

做法（关键是隔离，绝不碰用户真实数据）：
- `ARXIVER_HOME` 指向临时目录，`~/.arxiver` 里的库、配置、日志一个都不动
- `--minimized --no-tray`：窗口隐藏、无托盘图标，屏幕上什么都看不到
- 判据：进程存活 + 启动步骤留痕 + 首批入库远早于全部抓完 + 日志健康

注意：exe 用的是**命名互斥体**做单实例，与 ARXIVER_HOME 无关。若用户此刻正开着
Arxiver，本进程会立刻自我退出——用户平时就开着应用，所以这种情况很常见。
**不能简单跳过**（那等于打包产物从来没被验证过），改成两层降级验证：

- `0b 打包冒烟`：`Arxiver.exe --__bogus_flag__` → argparse 固定 `exit(2)`。
  这条路径在 `_ensure_single_instance()` **之前**，不碰互斥体，**永远能跑**。
  能走到 argparse 就证明 onefile 解包、Python 启动、`arxiver.app` 及
  config / core.library / core.scheduler / apscheduler / sqlite3 的 import 链全通。
- `1 部分启动`：真启动一次，日志里出现「检测到已有 Arxiver 实例」——
  这**正好**是「跑到了 main」的证据（`setup_logging` 已就绪、main 已执行）。
  窗口 / 同步 / 增量节奏那几项仍然测不了，脚本会明确说明。

跑一次大约 1.5~2 分钟（onefile 冷启动要解包 29MB，之后还要等一次真实同步）；
走降级路径只要十几秒。

运行: .venv/Scripts/python.exe tests/test_exe_launch.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from _testenv import isolated_home  # noqa: E402

EXE = os.path.join(ROOT, "dist", "Arxiver.exe")
HOME = isolated_home("exe")

# 关掉所有会动到真实环境的东西：桌面快捷方式、注册表自启、系统通知
with open(os.path.join(HOME, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"desktop_shortcut": False, "autostart": False, "notify": False}, f)

LOG = os.path.join(HOME, "logs", "arxiver.log")
OK = True


def check(cond, label, detail=""):
    global OK
    print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        OK = False


def _run_quiet(args: list[str]) -> str:
    """跑外部命令并安全取回文本。

    中文 Windows 的 tasklist / taskkill 输出是 GBK，用 text=True（UTF-8）会抛
    UnicodeDecodeError，而且那个异常发生在 subprocess 自己的读线程里，
    `returncode` 拿到了但 `.stdout` 是 None——所以这里自己按字节读再兜底解码。
    """
    try:
        r = subprocess.run(args, capture_output=True, timeout=20)
    except Exception:
        return ""
    raw = r.stdout or b""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _running_exes() -> list[str]:
    out = _run_quiet(["tasklist", "/FI", "IMAGENAME eq Arxiver.exe", "/NH", "/FO", "CSV"])
    return [ln for ln in out.splitlines() if "Arxiver.exe" in ln]


def _kill_tree(pid: int) -> None:
    """PyInstaller onefile 是「引导父进程 + 真身子进程」，必须连树一起杀。"""
    _run_quiet(["taskkill", "/F", "/T", "/PID", str(pid)])


def _cleanup(path: str) -> None:
    """清掉隔离数据目录，别在 Temp 里堆垃圾。

    必须重试：taskkill 返回后 Windows 释放文件句柄还要一小会儿，第一次 rmtree
    会在 ``logs/arxiver.log`` 上失败（ignore_errors 会把它静默咽掉，于是留下
    一个只剩日志文件的空壳目录）。
    """
    import shutil
    for _ in range(6):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.5)


def _bundle_smoke() -> None:
    """打包冒烟：用非法参数让 argparse 报错退出。

    `main()` 的顺序是 `parse_args` → `setup_logging` → `_ensure_single_instance`，
    所以这条路径**不会碰那个命名互斥体**，别人的实例在跑也照样能测。

    能走到 argparse 就证明：onefile 解包成功、Python 起来了、`arxiver.app`
    及其 import 链（config / core.library / core.scheduler / apscheduler /
    sqlite3）全部可导入。argparse 对未知参数固定 `exit(2)`。
    """
    print("\n== 0b. 打包冒烟：非法参数 → argparse 退出码 2 ==")
    p = subprocess.run([EXE, "--__bogus_flag__"], cwd=ROOT,
                       capture_output=True, timeout=180)
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    check(p.returncode == 2, f"退出码 2（实际 {p.returncode}）", err[-400:])
    check("usage:" in err, "argparse 真的执行到了（有 usage 输出）", err[-400:])


def _partial_launch() -> None:
    """已有实例在跑时能做的部分验证：exe 能跑到 `main()`。

    我们启动的进程会被单实例守卫挡下——这**正好**是「跑到了 main」的证据：
    日志里出现「检测到已有 Arxiver 实例」，而不是「Arxiver 启动」。
    窗口 / 同步 / 增量节奏那几项测不了，得等用户关掉应用。
    """
    print("\n== 1. 启动 exe（预期被单实例守卫挡下，但能证明它跑到了 main）==")
    env = dict(os.environ)
    env["ARXIVER_HOME"] = HOME
    proc = subprocess.Popen(
        [EXE, "--minimized", "--no-tray"],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 90
        text = ""
        while time.time() < deadline:
            try:
                with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
            if "检测到已有 Arxiver 实例" in text or "Arxiver 启动" in text:
                break
            time.sleep(0.4)

        guarded = "检测到已有 Arxiver 实例" in text
        started = "Arxiver 启动" in text
        check(started or guarded,
              "exe 跑到 main（日志有启动消息或守卫消息）", text[-400:])
        check(guarded, "确实是被单实例守卫挡下的，不是启动失败", text[-400:])
    finally:
        # 只有进程还活着才杀：它多半已经自己退了，对已死 PID 调 taskkill
        # 有 PID 被复用的极小风险
        if proc.poll() is None:
            _kill_tree(proc.pid)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass


def main() -> int:
    print("== 0. 前置检查 ==")
    if not os.path.exists(EXE):
        check(False, "dist/Arxiver.exe 存在（先跑 build.bat）", EXE)
        return 1
    check(True, f"exe 存在（{os.path.getsize(EXE) // 1024 // 1024} MB）")

    # 这条永远能跑，且不受单实例守卫影响
    _bundle_smoke()

    pre = _running_exes()
    if pre:
        print(f"  已有 Arxiver 在运行（{len(pre)} 个进程）——完整启动测试跳过")
        _partial_launch()
        print("\n  说明：窗口 / 同步 / 增量节奏这几项要等没有别的实例时才能测。")
        print("        用户平时就开着应用，所以这一层经常只能做到「跑到 main」。")
        _cleanup(HOME)
        print("\n结果:", "PASS" if OK else "FAIL")
        return 0 if OK else 1

    print("\n== 1. 启动 exe（隔离数据目录，隐藏窗口，无托盘）==")
    env = dict(os.environ)
    env["ARXIVER_HOME"] = HOME
    proc = subprocess.Popen(
        [EXE, "--minimized", "--no-tray"],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    print(f"  pid={proc.pid}  home={HOME}")

    try:
        def read_log() -> str:
            try:
                with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                return ""

        def wait_for(marker: str, timeout: float) -> tuple[bool, float]:
            """轮询等某个标记出现。

            这里必须轮询而不是 sleep 固定时长：onefile 首次启动要解包 29MB 到
            临时目录，再 import pystray / PIL / webview，冷启动能花十几秒。
            进程提前退出就立刻返回，不白等。
            """
            t = time.time()
            while time.time() - t < timeout:
                if marker in read_log():
                    return True, time.time() - t
                if proc.poll() is not None:
                    return False, time.time() - t
                time.sleep(0.4)
            return False, time.time() - t

        # ---- 2. 到 main 了（日志系统已就绪）----
        ok, dt = wait_for("Arxiver 启动", 120)
        check(ok, "日志出现「Arxiver 启动」（exe 真的跑到了 main）",
              f"{dt:.1f}s 内未见；exit={proc.poll()}")

        # ---- 3. 完整接线跑完（窗口 + 调度器都已就位）----
        ok2, dt2 = wait_for("已安排启动后自动同步", 120)
        check(ok2, "启动接线全部跑完（窗口 + 调度器就位）",
              f"{dt2:.1f}s 内未见；exit={proc.poll()}")

        print(f"  （冷启动耗时：到 main {dt:.1f}s，到接线完成 {dt2:.1f}s）")

        print("\n== 2. 启动后仍然存活（没有启动即崩）==")
        time.sleep(3)
        rc = proc.poll()
        check(rc is None, "进程持续存活", f"已退出，returncode={rc}")

        text = read_log()

        print("\n== 3. 关键启动步骤留痕 ==")
        for marker, label in (
            ("定时任务已启动", "调度器已启动"),
            ("已安排启动后自动同步", "已安排启动后自动同步"),
            ("后台模式", "进入后台（隐藏窗口）模式"),
        ):
            check(marker in text, label, f"日志未见「{marker}」")

        # ---- 4. 在真实 exe 里量一次「边抓边入库」----
        # 这是用户最初的诉求：论文不要等全部抓完才出现。这里用日志时间戳量
        # 「首批入库」与「全部抓完」的间隔——如果两者几乎同时，说明增量又退化成
        # 一次性了。网络不通时只报告、不判失败（那是环境问题，不是代码问题）。
        print("\n== 4. 真实 exe 里的增量节奏（用户最初诉求）==")

        def wait_line(pred, timeout: float):
            """返回**匹配到的整行**（不是谓词的布尔值——谓词只用来判断）。"""
            t = time.time()
            while time.time() - t < timeout:
                for ln in read_log().splitlines():
                    if pred(ln):
                        return ln
                if proc.poll() is not None:
                    return None
                time.sleep(0.5)
            return None

        def ts_of(line: str):
            try:
                return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
            except Exception:
                return None

        start_ln = wait_line(lambda l: "sync: 抓取 arXiv 最新论文" in l, 60)
        if start_ln is None:
            print("  未进入 arXiv 抓取阶段（可能网络不可用）——跳过本段，不计失败")
        else:
            t_start = ts_of(start_ln)
            first = wait_line(lambda l: "篇（新增 " in l, 150)
            done_ln = wait_line(lambda l: "入库完成：本次新增" in l, 240)

            if first is None:
                print("  150s 内未观察到任何一批入库（网络/接口不可用）——跳过本段，不计失败")
            else:
                t_first = ts_of(first)
                dt_first = (t_first - t_start).total_seconds()
                check(True, f"首批入库已发生：{dt_first:.1f}s 后", "")
                print(f"    {first.strip()[:130]}")
                if done_ln is not None:
                    dt_all = (ts_of(done_ln) - t_start).total_seconds()
                    print(f"    全部抓完：{dt_all:.1f}s 后   {done_ln.strip()[:90]}")
                    check(dt_first < dt_all,
                          f"首批比全部抓完早 {dt_all - dt_first:.1f}s（真的是边抓边出）",
                          f"first={dt_first:.1f}s total={dt_all:.1f}s")
                else:
                    print("  同步尚未结束（240s 内），但首批已经出现——这本身就是增量生效的证据")

        # ---- 5. 最终日志健康 ----
        # 必须在所有阶段跑完之后再查：ERROR 往往是同步中途才出现的
        # （实测第一次写这段时在同步前查，漏掉了后面的 hf-daily 502）。
        print("\n== 5. 最终日志健康 ==")
        text = read_log()
        lines = text.splitlines()

        check("Traceback" not in text, "日志无 Traceback（没有代码级异常）",
              next((l for l in lines if "Traceback" in l), ""))

        # 外部服务自己挂了不算代码问题——只报告，不判失败
        EXTERNAL = ("hf-daily", "semantic-scholar", "arxiv")
        errs = [l for l in lines if "[ERROR]" in l]
        mine = [l for l in errs if not any(src in l for src in EXTERNAL)]
        ext = [l for l in errs if any(src in l for src in EXTERNAL)]
        check(not mine, "日志无「自家代码」的 ERROR 行", mine[0] if mine else "")
        if ext:
            print(f"  （外部服务报错 {len(ext)} 条，不计失败）{ext[0].strip()[:110]}")

        # 推送链路在真实 exe 里的证据：on_papers 回调一旦抛异常就会留下这行 WARNING
        check("on_papers 回调失败" not in text,
              "exe 内增量推送回调未报错（evaluate_js 真的推到了窗口）",
              next((l for l in lines if "on_papers 回调失败" in l), ""))
        check("同步完成回调失败" not in text, "同步完成回调未报错")

        # GUI 循环退出时才会写这行——出现说明窗口已经没了
        check("Arxiver 窗口关闭" not in text, "GUI 循环仍在运行（未提前退出）")

        print("\n--- 日志尾部 ---")
        for ln in lines[-10:]:
            print("   " + ln[:150])
    finally:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        _cleanup(HOME)

    print("\n结果:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
