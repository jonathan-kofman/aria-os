"""Web search tools for the ResearchAgent.

Multi-source fallback chain:
1. DuckDuckGo HTML search (reliable, no API key, retry with backoff)
2. Brave Search API (if BRAVE_API_KEY in env)
3. DuckDuckGo instant answer API (wiki-style only)

Never hard-fails — returns empty string if all sources fail.
"""
from __future__ import annotations

import os
import re
import json
import time
import random
import urllib.request
import urllib.parse
import urllib.error

# Rotating User-Agents to avoid rate limiting
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# Track last request time to self-rate-limit
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 1.5  # seconds between requests


def web_search(query: str, max_results: int = 8) -> str:
    """
    Search the web and return relevant snippets as plain text.
    Tries multiple backends in order of reliability.
    """
    # 1. DuckDuckGo HTML search (most reliable, no key needed)
    results = _ddg_html_search(query, max_results)
    if results:
        return results

    # 2. Brave Search API (if key available)
    brave_key = os.environ.get("BRAVE_API_KEY", "")
    if brave_key:
        results = _brave_search(query, brave_key, max_results)
        if results:
            return results

    # 3. DuckDuckGo instant answer API (only works for wiki-style queries)
    results = _ddg_instant(query)
    if results:
        return results

    # 4. Return empty — ResearchAgent will continue without web context
    return ""


def web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and return plain text content."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": random.choice(_USER_AGENTS)
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Backend 1: DuckDuckGo HTML search (with retry + rate limiting)
# ---------------------------------------------------------------------------

def _ddg_html_search(query: str, max_results: int = 8) -> str:
    """Search DuckDuckGo via HTML endpoint with retry and rate-limit backoff."""
    global _last_request_time

    # Self-rate-limit to avoid DDG blocking
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"

    for attempt in range(3):
        ua = _USER_AGENTS[attempt % len(_USER_AGENTS)]
        headers = {"User-Agent": ua}

        try:
            _last_request_time = time.time()
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="replace")

                # Extract result snippets
                snippets = re.findall(
                    r'result__snippet[^>]*>(.*?)</a',
                    html, re.DOTALL
                )

                # Extract result titles
                titles = re.findall(
                    r'class="result__a"[^>]*>(.*?)</a>',
                    html, re.DOTALL
                )

                if not snippets:
                    # Rate limited or CAPTCHA — retry with backoff
                    if attempt < 2:
                        time.sleep(2.0 + attempt * 1.5)
                        continue
                    return ""

                results = []
                for i in range(min(max_results, len(snippets))):
                    title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
                    snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                    snippet = (snippet.replace("&amp;", "&")
                                      .replace("&quot;", '"')
                                      .replace("&#x27;", "'")
                                      .replace("&lt;", "<")
                                      .replace("&gt;", ">"))
                    if snippet and len(snippet) > 20:
                        line = f"- {title}: {snippet}" if title else f"- {snippet}"
                        results.append(line[:300])

                return "\n".join(results) if results else ""

        except Exception:
            if attempt < 2:
                time.sleep(2.0 + attempt * 1.5)
                continue
            return ""

    return ""


# ---------------------------------------------------------------------------
# Backend 2: Brave Search API
# ---------------------------------------------------------------------------

def _brave_search(query: str, api_key: str, max_results: int = 5) -> str:
    """Brave Search API — free tier, 1 query/sec."""
    url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={max_results}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            results = []
            for item in data.get("web", {}).get("results", [])[:max_results]:
                title = item.get("title", "")
                desc = item.get("description", "")
                if desc:
                    results.append(f"- {title}: {desc}"[:300])
            return "\n".join(results) if results else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Backend 3: DuckDuckGo instant answer API (wiki-style only)
# ---------------------------------------------------------------------------

def _ddg_instant(query: str) -> str:
    """DuckDuckGo instant answer API — only works for encyclopedia-style queries."""
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ARIA-OS/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            parts = []
            if data.get("Abstract"):
                parts.append(f"Summary: {data['Abstract']}")
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    parts.append(f"- {topic['Text'][:200]}")
            return "\n".join(parts) if parts else ""
    except Exception:
        return ""
