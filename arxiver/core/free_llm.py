"""免费翻译与抽取式摘要（无需任何 API Key，LLM 未配置时的回退方案）.

翻译：多源回退链 MyMemory → Google gtx → Lingva 公共实例（自动分段）
摘要：抽取式摘要（词频 + 位置加权选关键句），可再翻译成中文
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx

from .errors import log

__all__ = ["free_translate", "extractive_summary", "free_summarize_zh"]

_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_STOP = set("the and for with this that from have been are was were which their using "
            "based these those more than such but not can will".split())

_LINGVA_HOSTS = ("lingva.ml", "translate.plausibility.cloud", "lingva.lunar.icu")


def _src_mymemory(text: str) -> str:
    r = httpx.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": "en|zh-CN"},
        timeout=20.0,
    )
    r.raise_for_status()
    t = (r.json().get("responseData") or {}).get("translatedText", "")
    t = _html.unescape(t or "").strip()
    if not t or t.startswith("MYMEMORY WARNING"):
        raise ValueError("MyMemory 配额用尽")
    return t


def _src_google(text: str) -> str:
    r = httpx.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
        timeout=20.0,
    )
    r.raise_for_status()
    data = r.json()
    segs = data[0] if data else []
    return "".join(s[0] for s in segs if s and s[0])


def _src_lingva(text: str) -> str:
    q = urllib.parse.quote(text, safe="")
    for host in _LINGVA_HOSTS:
        try:
            r = httpx.get(f"https://{host}/api/v1/en/zh-CN/{q}", timeout=15.0)
            if r.status_code != 200:
                continue
            t = (r.json() or {}).get("translation")
            if t:
                return t.strip()
        except Exception:
            continue
    raise ValueError("Lingva 全部实例不可用")


_SOURCES = (("mymemory", _src_mymemory), ("google", _src_google), ("lingva", _src_lingva))


def _translate_segment(text: str) -> str:
    """单段翻译：依次尝试各免费源."""
    for name, fn in _SOURCES:
        try:
            t = fn(text)
            if t:
                return t
        except Exception as e:
            # 原来是 debug 级：日志级别是 INFO，所以「哪个源挂了、为什么挂了
            # 二十秒」从来没进过日志，用户只觉得翻译莫名其妙不动了。
            log.warning("翻译源 %s 失败: %s", name, e)
    return ""


def free_translate(text: str, chunk: int = 400) -> str:
    """英文 → 中文（分段并发请求，各源均受字符数限制）."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    if len(parts) == 1:
        return _translate_segment(parts[0])
    with ThreadPoolExecutor(max_workers=min(3, len(parts))) as ex:
        out = list(ex.map(_translate_segment, parts))
    if not any(out):
        raise ConnectionError("所有免费翻译源均不可用")
    return "".join(out)


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_RE.split((text or "").replace("\n", " ").strip())
    return [p.strip() for p in parts if len(p.strip()) > 20]


def extractive_summary(text: str, n: int = 3) -> str:
    """抽取式摘要：按词频（罕见词加分）与位置加权选取关键句."""
    sents = _split_sentences(text)
    if not sents:
        return (text or "")[:400]
    if len(sents) <= n:
        return " ".join(sents)
    freq: dict[str, int] = {}
    for s in sents:
        for w in re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", s.lower()):
            freq[w] = freq.get(w, 0) + 1

    def score(i: int, s: str) -> float:
        words = re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", s.lower())
        s1 = sum(1.0 / freq.get(w, 1) for w in words if w not in _STOP) / max(len(words), 1)
        s2 = 1.0 if i < 2 else (0.8 if i < len(sents) // 2 else 0.5)
        return s1 + 0.3 * s2

    ranked = sorted(enumerate(sents), key=lambda x: score(*x), reverse=True)
    top = sorted(i for i, _ in ranked[:n])
    return " ".join(sents[i] for i in top)


def free_summarize_zh(title: str, abstract: str, n: int = 3) -> str:
    """未配置 LLM 时的中文摘要：抽取关键句 + 免费翻译."""
    try:
        raw = extractive_summary(abstract or title, n=n)
        try:
            zh = free_translate(raw)
        except Exception as e:
            log.warning("免费翻译失败（仅返回英文抽取摘要）: %s", e)
            zh = ""
        if zh:
            return f"{raw}\n\n〔中文〕{zh}"
        return raw
    except Exception as e:
        log.error("免费摘要失败: %s", e)
        return ""
