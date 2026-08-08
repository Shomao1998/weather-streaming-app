"""Two caches: one for retrieval, one for generation.

Both are keyed on everything that could change the answer, so a stale entry is
not possible by construction rather than by remembering to invalidate. In
particular `index_version` is in both keys: re-ingesting the corpus retires
every cached retrieval *and* every cached card that was grounded in it, which
is what stops a withdrawn document from continuing to be cited.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 3),
        }


class TtlCache(Generic[T]):
    """A bounded LRU with per-entry expiry.

    Bounded because a Function worker is long-lived and an unbounded dict keyed
    by user question is a memory leak with a friendly name.
    """

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        # Injectable so expiry can be tested without sleeping.
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self.stats = CacheStats()

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                self.stats.misses += 1
                self.stats.evictions += 1
                return None
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return value

    def put(self, key: str, value: T) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
                self.stats.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def generation_key(
    *,
    weather_snapshot_id: str,
    trigger: str,
    chunk_ids: list[str],
    prompt_version: str,
    model: str,
    index_version: str,
    question: str | None = None,
) -> str:
    """Identity of "this card, from this evidence, by this prompt and model".

    Chunk ids are sorted so a reordered retrieval reuses the answer, and the
    question is included because the same weather may be asked about in two
    different ways.
    """
    raw = json.dumps(
        {
            "snapshot": weather_snapshot_id,
            "trigger": trigger,
            "chunks": sorted(chunk_ids),
            "prompt_version": prompt_version,
            "model": model,
            "index_version": index_version,
            "question": (question or "").strip().lower(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
