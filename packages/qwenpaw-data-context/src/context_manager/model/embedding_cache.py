"""Cache immutable vectors by trusted identity, embedding space and text."""

from collections import OrderedDict
from threading import Lock
from time import monotonic

# Opaque caller identity, endpoint, model, dimension, clipped text.
CacheKey = tuple[str, str, str, int | None, str]
Vector = tuple[float, ...]


class EmbeddingCache:
    """Share bounded, expiring LRU entries across model clients in one process."""

    def __init__(self) -> None:
        self._entries: OrderedDict[CacheKey, tuple[float, Vector]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: CacheKey, *, ttl_seconds: float) -> Vector | None:
        """Read a live entry and mark it as recently used."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            created_at, vector = entry
            if monotonic() - created_at > ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return vector

    def put(self, key: CacheKey, vector: Vector, *, max_entries: int) -> None:
        """Publish a validated vector and evict least-recently-used entries."""
        with self._lock:
            self._entries[key] = (monotonic(), vector)
            self._entries.move_to_end(key)
            while len(self._entries) > max_entries:
                self._entries.popitem(last=False)


embedding_cache = EmbeddingCache()
