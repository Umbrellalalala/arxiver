"""用户配置与画像持久化（JSON，位于 ~/.arxiver/config.json）."""
from __future__ import annotations

import json
import shutil
import threading
from typing import Any

from .paths import BACKUP_DIR, CONFIG_PATH, atomic_write_text, ensure_dirs

__all__ = ["Config", "DEFAULT_CONFIG", "get_config"]

_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    # ---- 用户画像（需求 2：大方向 / 小方向，限计算机专业）----
    "major_fields": [],                         # 大方向（如「智能体 / 计算机视觉」），首次运行在设置里填
    "minor_topics": {},                       # {"cs.CV": ["key1", "key2"], ...}
    "seed_papers": [],                        # 小方向种子论文 arXiv ID（收藏自动加入）
    # ---- 下载归档 ----
    "library_dir": "",                        # 论文库路径（空 = 默认 ~/.arxiver/library）
    "auto_download": False,                   # 定时任务是否自动下载 Top N
    "download_top_n": 5,
    # ---- 通知与自启 ----
    "notify": True,
    "autostart": False,
    "desktop_shortcut": True,                 # 桌面快捷方式（首次启动自动创建）
    # ---- 定时同步（每日）----
    "daily_hour": 8, "daily_minute": 0,
    "auto_sync_on_start": True,               # 启动后自动同步（失败自动重试）
    # ---- 推荐池体积 ----
    # 没人看过的老论文自动归档（不删数据，首页切「已归档」还能看）。
    # 0 = 不自动归档。收藏/下载/打标/写过笔记的永远不会被归档。
    "pool_keep_days": 30,
    # ---- 可选 API 配置 ----
    "semanticscholar_key": "",                # 提高 S2 限速（可选）
    "openalex_mailto": "",                    # OpenAlex 礼貌参数（可选）
    # ---- LLM（AI 摘要/翻译，OpenAI 兼容接口，可选）----
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    # ---- 其它 ----
    "lang": "zh",
    "theme": "light",                          # light / dark
    "progress_opacity": 100,                   # 进度条不透明度 0-100
}


class Config:
    """线程安全的配置容器，改动后需显式 save()."""

    def __init__(self) -> None:
        ensure_dirs()
        self._data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    stored = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
                with _lock:
                    self._data = merged
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            # 损坏时保留现场并回滚上一版备份，不要静默丢弃用户配置
            from .core.errors import log
            log.error("配置加载失败: %s（保留损坏文件并回退备份）", e)
            self._restore_backup()

    def _restore_backup(self) -> None:
        bak = BACKUP_DIR / "config.json.bak"
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, BACKUP_DIR / "config.json.corrupt")
            if bak.exists():
                with open(bak, encoding="utf-8") as f:
                    stored = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
                with _lock:
                    self._data = merged
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            # 备份也坏了 → 只能退回默认值。**必须说出来**：用户会看到
            # 「研究方向/关键词全没了」，如果这里静默，日志里一片干净、根本查不到原因。
            from .core.errors import log
            log.error("配置备份也损坏，已退回默认配置（原文件保留在 %s）: %s",
                      BACKUP_DIR / "config.json.corrupt", e)

    def save(self) -> None:
        with _lock:
            data = json.dumps(self._data, ensure_ascii=False, indent=2)
        try:
            ensure_dirs()
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, BACKUP_DIR / "config.json.bak")
            atomic_write_text(CONFIG_PATH, data)
            # 首次保存后补一份备份，保证之后任何时候都有可回滚的版本
            bak = BACKUP_DIR / "config.json.bak"
            if not bak.exists():
                shutil.copy2(CONFIG_PATH, bak)
        except OSError as e:
            from .core.errors import log
            log.error("配置保存失败: %s", e)

    # -- 通用读写 --
    def get(self, key: str, default: Any = None) -> Any:
        with _lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with _lock:
            self._data[key] = value
        if save:
            self.save()

    def update(self, kv: dict[str, Any], save: bool = True) -> None:
        with _lock:
            self._data.update(kv)
        if save:
            self.save()

    def to_dict(self) -> dict[str, Any]:
        with _lock:
            return json.loads(json.dumps(self._data))

    # -- 画像便捷方法 --
    def majors(self) -> list[str]:
        return self.get("major_fields", [])

    def minors(self) -> dict[str, list[str]]:
        return self.get("minor_topics", {})

    def keywords(self) -> list[str]:
        """所有小方向关键词的扁平去重列表."""
        out: list[str] = []
        for ks in self.minors().values():
            out.extend(ks)
        return list(dict.fromkeys(out))

    def add_seed(self, arxiv_id: str) -> None:
        seeds = set(self.get("seed_papers", []))
        seeds.add(arxiv_id)
        self.set("seed_papers", sorted(seeds))


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
