"""Multi-source web search fallback chain with health checks and caching."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("aria.search")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source_provider: str
    timestamp: datetime = field(default_factory=datetime.now)
    relevance_score: float = 0.5


class ProviderError(Exception):
    pass


class AllProvidersFailedError(Exception):
    pass


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class SearchProvider(ABC):
    name: str = "base"
    timeout: float = 10.0
    max_retries: int = 2

    # Health cache: (healthy: bool, expires: float)
    _health_cache: tuple[bool, float] = (True, 0.0)
    _HEALTH_TTL = 300.0  # 5 minutes

    def is_healthy(self) -> bool:
        healthy, expires = self._health_cache
        if time.time() < expires:
            return healthy
        return True  # assume healthy if cache expired

    def mark_unhealthy(self) -> None:
        self._health_cache = (False, time.time() + self._HEALTH_TTL)

    def mark_healthy(self) -> None:
        self._health_cache = (True, time.time() + self._HEALTH_TTL)

    @abstractmethod
    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Synchronous search — called from sync or async context."""
        ...

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Async wrapper around sync search (runs in thread pool)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.search_sync, query, max_results)

    async def health_check(self) -> bool:
        return self.is_healthy()


# ---------------------------------------------------------------------------
# Provider: DuckDuckGo HTML (primary — no API key)
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]
_last_ddg_time = 0.0


class DDGHTMLProvider(SearchProvider):
    name = "duckduckgo_html"
    timeout = 12.0

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        global _last_ddg_time
        # Rate limit
        elapsed = time.time() - _last_ddg_time
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)

        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        for attempt in range(self.max_retries):
            ua = _USER_AGENTS[attempt % len(_USER_AGENTS)]
            try:
                _last_ddg_time = time.time()
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    html = resp.read().decode("utf-8", errors="replace")

                snippets = re.findall(r'result__snippet[^>]*>(.*?)</a', html, re.DOTALL)
                titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
                urls = re.findall(r'class="result__url"[^>]*>(.*?)</a>', html, re.DOTALL)

                if not snippets:
                    if attempt < self.max_retries - 1:
                        time.sleep(2.0 + attempt * 1.5)
                        continue
                    self.mark_unhealthy()
                    return []

                results = []
                for i in range(min(max_results, len(snippets))):
                    title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
                    snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                    snippet = (snippet.replace("&amp;", "&").replace("&quot;", '"')
                               .replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">"))
                    result_url = re.sub(r'<[^>]+>', '', urls[i]).strip() if i < len(urls) else ""
                    if snippet and len(snippet) > 20:
                        results.append(SearchResult(
                            title=title, url=result_url, snippet=snippet[:300],
                            source_provider=self.name,
                            relevance_score=1.0 - i * 0.08,
                        ))
                self.mark_healthy()
                return results

            except Exception:
                if attempt < self.max_retries - 1:
                    time.sleep(2.0)
                    continue
                self.mark_unhealthy()
                return []
        return []


# ---------------------------------------------------------------------------
# Provider: Brave Search API
# ---------------------------------------------------------------------------

class BraveSearchProvider(SearchProvider):
    name = "brave"
    timeout = 10.0

    def __init__(self):
        self._api_key = os.environ.get("BRAVE_API_KEY", "")

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self._api_key:
            return []
        url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={max_results}"
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key,
            })
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                results = []
                for i, item in enumerate(data.get("web", {}).get("results", [])[:max_results]):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("description", "")[:300],
                        source_provider=self.name,
                        relevance_score=1.0 - i * 0.05,
                    ))
                self.mark_healthy()
                return results
        except Exception:
            self.mark_unhealthy()
            return []

    async def health_check(self) -> bool:
        return bool(self._api_key) and self.is_healthy()


# ---------------------------------------------------------------------------
# Provider: SearXNG (self-hosted)
# ---------------------------------------------------------------------------

class SearXNGProvider(SearchProvider):
    name = "searxng"
    timeout = 8.0

    def __init__(self):
        self._base_url = os.environ.get("SEARXNG_URL", "")

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self._base_url:
            return []
        url = f"{self._base_url}/search?q={urllib.parse.quote(query)}&format=json&categories=general"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                results = []
                for i, item in enumerate(data.get("results", [])[:max_results]):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", "")[:300],
                        source_provider=self.name,
                        relevance_score=float(item.get("score", 0.5)),
                    ))
                self.mark_healthy()
                return results
        except Exception:
            self.mark_unhealthy()
            return []

    async def health_check(self) -> bool:
        return bool(self._base_url) and self.is_healthy()


# ---------------------------------------------------------------------------
# Provider: Google Custom Search
# ---------------------------------------------------------------------------

class GoogleCSEProvider(SearchProvider):
    name = "google_cse"
    timeout = 10.0

    def __init__(self):
        self._api_key = os.environ.get("GOOGLE_CSE_KEY", os.environ.get("GOOGLE_API_KEY", ""))
        self._cx = os.environ.get("GOOGLE_CSE_CX", "")

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not self._api_key or not self._cx:
            return []
        url = (f"https://www.googleapis.com/customsearch/v1"
               f"?key={self._api_key}&cx={self._cx}"
               f"&q={urllib.parse.quote(query)}&num={min(max_results, 10)}")
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                results = []
                for i, item in enumerate(data.get("items", [])[:max_results]):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("link", ""),
                        snippet=item.get("snippet", "")[:300],
                        source_provider=self.name,
                        relevance_score=1.0 - i * 0.06,
                    ))
                self.mark_healthy()
                return results
        except Exception:
            self.mark_unhealthy()
            return []

    async def health_check(self) -> bool:
        return bool(self._api_key and self._cx) and self.is_healthy()


# ---------------------------------------------------------------------------
# Provider: Direct Scraper (last resort)
# ---------------------------------------------------------------------------

class DirectScraperProvider(SearchProvider):
    name = "direct_scraper"
    timeout = 15.0
    max_retries = 1  # slow, don't retry

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Scrape Google search results directly. Fragile but no API key needed."""
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&num={max_results}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _USER_AGENTS[0],
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # Extract snippets
            snippets = re.findall(r'<span[^>]*>(.*?)</span>', html)
            useful = [s for s in snippets if len(s) > 40 and
                      any(kw in s.lower() for kw in
                          ["mm", "inch", "spec", "dimension", "size", "width",
                           "height", "length", "material", "yield", "strength"])]
            results = []
            for i, s in enumerate(useful[:max_results]):
                clean = re.sub(r'<[^>]+>', '', s).strip()
                if clean:
                    results.append(SearchResult(
                        title="", url="", snippet=clean[:300],
                        source_provider=self.name,
                        relevance_score=0.3 - i * 0.02,
                    ))
            return results
        except Exception:
            self.mark_unhealthy()
            return []


# ---------------------------------------------------------------------------
# Search Chain
# ---------------------------------------------------------------------------

class SearchChain:
    """Fallback chain of search providers. Tries each until one succeeds."""

    def __init__(self, providers: list[SearchProvider] | None = None):
        if providers is None:
            # Default chain: DDG HTML → Brave → SearXNG → Google CSE → Scraper
            providers = [
                DDGHTMLProvider(),
                BraveSearchProvider(),
                SearXNGProvider(),
                GoogleCSEProvider(),
                DirectScraperProvider(),
            ]
        self.providers = providers

    def search_sync(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Synchronous search through the fallback chain."""
        for provider in self.providers:
            if not provider.is_healthy():
                continue
            try:
                results = provider.search_sync(query, max_results)
                if results:
                    return results
            except Exception as e:
                log.warning(f"{provider.name} failed: {e}")
                provider.mark_unhealthy()
                continue
        return []

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Async search through the fallback chain."""
        for provider in self.providers:
            if not await provider.health_check():
                continue
            try:
                results = await asyncio.wait_for(
                    provider.search(query, max_results),
                    timeout=provider.timeout,
                )
                if results:
                    return results
            except (asyncio.TimeoutError, Exception) as e:
                log.warning(f"{provider.name} failed: {e}")
                provider.mark_unhealthy()
                continue
        return []

    def to_text(self, results: list[SearchResult]) -> str:
        """Convert results to plain text for agent consumption."""
        lines = []
        for r in results:
            if r.title and r.snippet:
                lines.append(f"- {r.title}: {r.snippet}")
            elif r.snippet:
                lines.append(f"- {r.snippet}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_chain: SearchChain | None = None


def get_search_chain() -> SearchChain:
    global _chain
    if _chain is None:
        _chain = SearchChain()
    return _chain


def web_search(query: str, max_results: int = 8) -> str:
    """Drop-in replacement for the old _web_tools.web_search. Returns plain text."""
    chain = get_search_chain()
    results = chain.search_sync(query, max_results)
    return chain.to_text(results)
