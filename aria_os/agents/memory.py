"""Memory system stub — available in ARIA-OS Pro."""
from __future__ import annotations
from typing import Any

def ensure_memory_dir() -> None: pass
def read_memory(filename: str, **kw) -> str: return ''
def search_memory(query: str, **kw) -> str: return ''
def record_generation(**kw) -> None: pass
def should_consolidate() -> bool: return False
def consolidate() -> None: pass
