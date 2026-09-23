"""测试用的隔离环境。

**为什么不用 `shutil.rmtree` 清目录**：测试数据目录一旦文件数变多（SQLite 的
WAL/SHM + 日志 + 归档 PDF，跑几次就 80+ 个文件），批量删除会被安全策略拦下
（SAFE_DELETE_BULK_CONFIRM_REQUIRED），测试进程会被直接掐断，表现为「测试莫名失败」。
另外把测试数据放在仓库里也会污染工作区。

改用 `tempfile.mkdtemp`：每次运行都是一个全新的空目录，既不需要删除，
也不会和上一次运行的残留（比如被占用的 SQLite 文件）互相干扰。
"""
import os
import tempfile

__all__ = ["isolated_home"]


def isolated_home(tag: str) -> str:
    """造一个隔离的 ARXIVER_HOME 并写进环境变量。

    必须在 `import arxiver.*` **之前**调用——`paths.py` 在 import 时就把
    BASE_DIR 定死了。
    """
    d = tempfile.mkdtemp(prefix=f"arxiver-test-{tag}-")
    os.environ["ARXIVER_HOME"] = d
    return d
