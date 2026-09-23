"""真机界面截图：启动 exe，抓窗口画面，用来人眼确认界面真的长得对。

为什么需要：`test_live_refresh.js` / `test_push_to_dom.js` 用的是最小 DOM 桩，
只能证明「逻辑对」，证明不了 **WebView2 里渲染出来是什么样**——
CSS 没生效、卡片没高亮、布局塌了，它们全都发现不了。

用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` **离屏**抓窗口：
不用把窗口调到前台，不抢你的焦点，也不用你当人肉测试机。

隔离：`ARXIVER_HOME` 指到临时目录，绝不碰 `~/.arxiver` 真库。
副作用：会在屏幕上弹出一个 Arxiver 窗口（默认最大化），抓完自动关掉。
        用的是空库，所以看到的是「首次同步」的样子——正好是要验证的场景。

运行: .venv/Scripts/python.exe tests/capture_ui.py
      默认在第 6s / 20s / 40s 各抓一张（覆盖「刚出几篇」到「全部抓完」）
      自定义: .venv/Scripts/python.exe tests/capture_ui.py 5,15,30
      真实画像: .venv/Scripts/python.exe tests/capture_ui.py 5,25,60,80 --real-config
                （空配置只有默认 3 个大方向，反映不出用户真实的 7 个分类场景；
                  真实画像跑得慢，截点要往后放）
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from _testenv import isolated_home  # noqa: E402

EXE = os.path.join(ROOT, "dist", "Arxiver.exe")
TITLE = "Arxiver — 顶会论文助手"
OUT_DIR = os.path.join(ROOT, "tests", "_ui_shots")

# 进程必须是 DPI 感知的，否则 GetWindowRect 拿到的是缩放后的逻辑像素，
# 抓出来的图会被裁掉一块
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def find_hwnd(expect_pid: int) -> int:
    """按标题找**属于本次启动进程**的窗口，最多等 40s（onefile 冷启动要解包）。

    必须校验 PID。应用有单实例守卫（`检测到已有 Arxiver 实例，已激活其窗口，
    本进程退出`）：若已有别的 Arxiver 在跑，我们启动的进程会**立刻退出**，
    而 `FindWindowW` 按标题找到的是**别人那个窗口**——抓出来画面完全正常、
    脚本报告成功，验证却是假的。

    踩过：抓到的是用户真实库的窗口（1283 篇），还以为是新 exe 的画面。
    """
    deadline = time.time() + 40
    foreign = False
    while time.time() < deadline:
        h = user32.FindWindowW(None, TITLE)
        if h:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == expect_pid:
                return h
            foreign = True
        time.sleep(0.5)
    if foreign:
        print("  [FAIL] 有同标题窗口但不属于本次进程——多半是已有 Arxiver 实例在跑")
    return 0


def capture(hwnd: int, path: str) -> bool:
    """离屏抓窗口。返回 False 表示拿到的是全黑图（DirectComposition 没渲染出来）。"""
    from PIL import Image

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h          # 负高度 = 自顶向下，省得再翻转
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if not got:
            return False
        img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
        img.convert("RGB").save(path)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)

    # 全黑 = WebView2 内容没被 PrintWindow 抓到，得换方案（见 docstring）
    try:
        ex = img.convert("L").getextrema()
        return ex[1] > 8
    except Exception:
        return True


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=20)


def _cleanup(path: str) -> None:
    import shutil
    for _ in range(6):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.5)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    shots = [6.0, 20.0, 40.0]
    if args:
        shots = [float(x) for x in args[0].split(",")]

    if not os.path.exists(EXE):
        print("没有 dist/Arxiver.exe，先跑 build.bat")
        return 1

    home = isolated_home("ui")
    cfg = {"desktop_shortcut": False, "autostart": False,
           "notify": False, "auto_download": False}
    if "--real-config" in flags:
        src = os.path.expanduser("~/.arxiver/config.json")
        if os.path.exists(src):
            with open(src, encoding="utf-8") as f:
                cfg = json.load(f)
            # 真实画像 + 关掉一切会碰真实环境或产生副作用的开关
            for k in ("desktop_shortcut", "autostart", "notify", "auto_download"):
                cfg[k] = False
            print(f"已载入真实画像：{src}")
        else:
            print(f"找不到 {src}，退回默认配置")
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.makedirs(OUT_DIR, exist_ok=True)

    env = dict(os.environ)
    env["ARXIVER_HOME"] = home
    print(f"启动 exe（窗口会弹出来，抓完自动关闭）\n  home={home}")
    proc = subprocess.Popen([EXE, "--no-tray"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    saved: list[str] = []
    try:
        # 单实例守卫：已有实例在跑时，我们这个进程会立刻退出。
        # 必须显式识别，否则后面会抓到**别人那个窗口**还报成功。
        log_path = os.path.join(home, "logs", "arxiver.log")
        started = False
        deadline = time.time() + 30
        while time.time() < deadline:
            txt = ""
            if os.path.exists(log_path):
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                except OSError:
                    txt = ""
            if "检测到已有 Arxiver 实例" in txt:
                print("  [FAIL] 已有 Arxiver 实例在跑，本次启动的进程已退出。")
                print("         先关掉它（Get-Process Arxiver | Stop-Process -Force）"
                      "再跑本脚本，否则抓到的会是那个窗口。")
                return 1
            if "Arxiver 启动" in txt:
                started = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not started:
            print(f"  [FAIL] 等不到「Arxiver 启动」（进程退出码 {proc.poll()}）"
                  f"\n         日志：{log_path}")
            return 1

        hwnd = find_hwnd(proc.pid)
        if not hwnd:
            print("  等不到属于本次进程的窗口（标题：%s）" % TITLE)
            return 1
        print(f"  找到窗口 hwnd={hwnd}（pid={proc.pid}，已校验归属）")

        t0 = time.time()
        for at in sorted(shots):
            wait = at - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            path = os.path.join(OUT_DIR, f"ui_{at:04.1f}s.png")
            ok = capture(hwnd, path)
            mark = "OK  " if ok else "全黑"
            # 打印哈希：连着几张哈希相同就说明要么画面真的静止、
            # 要么 PrintWindow 返回了缓存帧——必须去日志里对时间线才能区分
            digest = ""
            if os.path.exists(path):
                with open(path, "rb") as fp:
                    digest = hashlib.sha256(fp.read()).hexdigest()[:10]
            print(f"  {mark} {at:5.1f}s  sha={digest}  -> {path}")
            if ok:
                saved.append(path)
    finally:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        if "--keep-home" in flags:
            print(f"\n--keep-home：保留数据目录（含 arxiver.log）\n  {home}")
        else:
            _cleanup(home)

    if not saved:
        print("\n一张都没抓到（PrintWindow 拿不到 WebView2 内容）")
        return 1
    print(f"\n抓到 {len(saved)} 张：")
    for p in saved:
        print("  " + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
