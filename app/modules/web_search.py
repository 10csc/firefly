# -*- coding: utf-8 -*-
"""联网搜索（AI 建卡向导的原料层）——DuckDuckGo Instant Answer API + HTML 搜索兜底。

无 Key、纯 requests。AI 只基于搜索到的原文构建角色，不凭记忆编。
模块铁律：输入审查 → 处理（HTTP）→ 验证结果 → 降级（失败返回空，不阻断向导）。
"""

import logging
import re
import threading
from html import unescape

import requests

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_SEARCH_COUNT = 0
_SEARCH_ERRORS = 0

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefly/1.0"}
_TIMEOUT = 15


def get_counters() -> dict:
    with _lock:
        return {"search_count": _SEARCH_COUNT, "search_errors": _SEARCH_ERRORS}


def _strip_html(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def search_character(query: str, max_results: int = 4) -> dict:
    """搜角色资料。返回 {"abstract": 摘要, "source": 来源名, "url": 链接, "results": [摘要列表]}。
    失败/无结果返回空 dict（调用方走降级：用户描述 + AI 直接构建）。"""
    global _SEARCH_COUNT, _SEARCH_ERRORS
    if not isinstance(query, str) or not query.strip():
        return {}
    query = query.strip()[:120]
    with _lock:
        _SEARCH_COUNT += 1
    # 1. Instant Answer API（JSON，知名角色通常有摘要）
    try:
        r = requests.get("https://api.duckduckgo.com/",
                         params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                         headers=_UA, timeout=_TIMEOUT)
        if r.ok:
            d = r.json()
            abstract = _strip_html(d.get("Abstract") or d.get("AbstractText") or "")
            if abstract:
                return {"abstract": abstract[:800],
                        "source": _strip_html(d.get("AbstractSource") or ""),
                        "url": d.get("AbstractURL") or "",
                        "results": [abstract]}
            # RelatedTopics 兜底
            topics = d.get("RelatedTopics") or []
            texts = [_strip_html(t.get("Text") or "") for t in topics[:max_results]
                     if isinstance(t, dict) and t.get("Text")]
            texts = [t for t in texts if t]
            if texts:
                return {"abstract": texts[0][:800], "source": "DuckDuckGo 相关", "url": "",
                        "results": texts}
    except Exception as e:
        logger.warning("DuckDuckGo Instant Answer 失败: %s", e)
    # 2. HTML 搜索兜底（解析结果链接与摘要）
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query}, headers=_UA, timeout=_TIMEOUT)
        if r.ok:
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
            texts = [_strip_html(s) for s in snippets[:max_results]]
            texts = [t for t in texts if t]
            if texts:
                return {"abstract": texts[0][:800], "source": "DuckDuckGo 搜索", "url": "",
                        "results": texts}
    except Exception as e:
        logger.warning("DuckDuckGo HTML 搜索失败: %s", e)
    # 3. Bing 国内版（国内网络可达）
    try:
        r = requests.get("https://cn.bing.com/search",
                         params={"q": query}, headers=_UA, timeout=_TIMEOUT)
        if r.ok:
            snippets = re.findall(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', r.text, re.S)
            texts = [_strip_html(s) for s in snippets[:max_results]]
            texts = [t for t in texts if t]
            if texts:
                return {"abstract": texts[0][:800], "source": "Bing 搜索", "url": "",
                        "results": texts}
    except Exception as e:
        logger.warning("Bing 搜索失败: %s", e)
    # 4. 萌娘百科 MediaWiki API（ACG 角色资料密集，国内可达）
    try:
        r = requests.get("https://zh.moegirl.org.cn/api.php",
                         params={"action": "query", "list": "search", "srsearch": query,
                                 "format": "json", "utf8": 1},
                         headers=_UA, timeout=_TIMEOUT)
        if r.ok:
            hits = (r.json().get("query") or {}).get("search") or []
            texts = [_strip_html(x.get("snippet") or "") for x in hits[:max_results]]
            texts = [t for t in texts if t]
            if texts:
                return {"abstract": texts[0][:800], "source": "萌娘百科", "url": "",
                        "results": texts}
    except Exception as e:
        logger.warning("萌娘百科搜索失败: %s", e)
    with _lock:
        _SEARCH_ERRORS += 1
    return {}
