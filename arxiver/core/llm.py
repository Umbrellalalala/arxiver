"""LLM 客户端（OpenAI 兼容接口）：AI 摘要 / 中文翻译.

兼容 OpenAI / DeepSeek / Moonshot / 通义 / 智谱 等任何兼容 `/chat/completions` 的服务。
用户只需在设置中填 base_url、api_key、model 即可启用。
"""
from __future__ import annotations

import httpx

from ..config import Config
from .errors import log, retry

__all__ = ["LLMClient"]

_SUMMARY_PROMPT = (
    "你是一名计算机领域资深研究者。请用简洁的中文总结下面这篇论文，"
    "按【研究问题】【核心方法】【主要结论】三点输出，总共 180 字以内，"
    "不要客套话，直接给干货。\n\n标题：{title}\n摘要：{abstract}"
)

_TRANSLATE_PROMPT = (
    "请把下面这篇论文的标题和摘要翻译成通顺的中文，保留专有名词的英文原文。"
    "\n\n标题：{title}\n摘要：{abstract}"
)


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    # ---------- 配置 ----------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("llm_base_url") and self._cfg.get("llm_api_key"))

    def _endpoint(self) -> str:
        base = (self._cfg.get("llm_base_url") or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    @retry(times=2, delay=3.0)
    def _chat(self, messages: list[dict], max_tokens: int = 1200) -> str:
        payload = {
            "model": self._cfg.get("llm_model") or "gpt-4o-mini",
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        r = httpx.post(
            self._endpoint(),
            json=payload,
            headers={"Authorization": f"Bearer {self._cfg.get('llm_api_key')}"},
            timeout=90.0,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    # ---------- 对外（未配置 LLM 时自动回退免费翻译/抽取摘要） ----------
    def summarize(self, title: str, abstract: str, lang: str = "zh") -> str:
        """生成中文摘要：有 LLM 用 LLM；否则免费抽取式摘要 + 免费翻译."""
        if self.enabled:
            try:
                prompt = _SUMMARY_PROMPT.format(title=title, abstract=abstract[:2500])
                return self._chat([{"role": "user", "content": prompt}])
            except Exception as e:
                log.error("AI 摘要失败: %s", e)
                return ""
        from .free_llm import free_summarize_zh
        return free_summarize_zh(title, abstract)

    def translate(self, title: str, abstract: str) -> str:
        """翻译标题 + 摘要为中文."""
        if self.enabled:
            try:
                prompt = _TRANSLATE_PROMPT.format(title=title, abstract=abstract[:2500])
                return self._chat([{"role": "user", "content": prompt}], max_tokens=1500)
            except Exception as e:
                log.error("AI 翻译失败: %s", e)
                return ""
        from .free_llm import free_translate
        try:
            zh_title = free_translate(title)
            zh_abs = free_translate(abstract or "")
            return f"【标题】{zh_title}\n\n【摘要】{zh_abs}"
        except Exception as e:
            log.error("免费翻译失败: %s", e)
            return ""

    def translate_title(self, title: str) -> str:
        """仅翻译标题（用于 Zotero 视图的译文标题列，短请求）."""
        if self.enabled:
            try:
                prompt = (
                    "把下面的学术论文标题翻译成通顺的中文，保留专有名词英文，"
                    "只输出译文本身：\n\n" + title[:300]
                )
                return self._chat([{"role": "user", "content": prompt}], max_tokens=200)
            except Exception as e:
                log.error("标题翻译失败: %s", e)
                return ""
        from .free_llm import free_translate
        try:
            return free_translate(title[:600])
        except Exception as e:
            log.error("免费标题翻译失败: %s", e)
            return ""

    def translate_abstract(self, abstract: str) -> str:
        """翻译摘要（双语对照用）."""
        if self.enabled:
            try:
                prompt = (
                    "把下面这段英文学术论文摘要翻译成通顺的中文，保留专有名词英文，"
                    "只输出译文：\n\n" + abstract[:3000]
                )
                return self._chat([{"role": "user", "content": prompt}], max_tokens=1500)
            except Exception as e:
                log.error("摘要翻译失败: %s", e)
                return ""
        from .free_llm import free_translate
        try:
            return free_translate(abstract[:3000])
        except Exception as e:
            log.error("免费摘要翻译失败: %s", e)
            return ""
