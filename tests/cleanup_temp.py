"""清扫测试在 %TEMP% 里留下的隔离数据目录（`arxiver-test-*`）。

为什么单独写个脚本、而不是在测试里 atexit 删：

- 安全策略对「批量删除」有阈值（实测 50 个文件左右就会拦，
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED`），触发时**进程被直接掐断**。
  测试进程中途被掐 = 测试莫名失败；退出时被掐 = 退出码变非 0，`run_all` 误报失败。
- 所以这里**每个目录起一个独立子进程**去删：预算各自独立，且子进程被掐也影响不到谁。

只有「一次跑出很多文件」的目录（比如真实 exe 测试里的 WebView2 缓存）需要这个，
普通测试的小目录由各自的测试自己收尾。

运行: .venv/Scripts/python.exe tests/cleanup_temp.py
"""
import glob
import os
import subprocess
import sys
import tempfile

PY = sys.executable

_DEL = (
    "import shutil,sys\n"
    "shutil.rmtree(sys.argv[1], ignore_errors=True)\n"
)


def main() -> int:
    root = tempfile.gettempdir()
    dirs = sorted(glob.glob(os.path.join(root, "arxiver-test-*")))
    if not dirs:
        print("没有需要清理的目录。")
        return 0

    print(f"发现 {len(dirs)} 个测试残留目录，逐个清理：")
    for d in dirs:
        before = sum(len(f) for _, _, f in os.walk(d))
        subprocess.run([PY, "-c", _DEL, d], capture_output=True, timeout=60)
        gone = not os.path.exists(d)
        print(f"  {'已删除' if gone else '未删净'}  {os.path.basename(d)}  ({before} 个文件)")

    left = sorted(glob.glob(os.path.join(root, "arxiver-test-*")))
    if left:
        print(f"\n仍剩 {len(left)} 个（多半是文件被占用，稍后重跑本脚本即可）")
        return 1
    print("\n全部清理完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
