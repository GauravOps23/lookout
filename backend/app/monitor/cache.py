import threading
import time


class TtlCache:
    """Small in-process cache. Callers asking for the same key at once share one computation."""

    def __init__(self):
        self._data: dict = {}
        self._locks: dict = {}
        self._guard = threading.Lock()

    def get_or_compute(self, key, ttl_s: float, fn, fresh: bool = False):
        """Returns (value, from_cache, computed_at_epoch)."""
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._data.get(key)
            if hit and not fresh and time.monotonic() - hit[0] < ttl_s:
                return hit[1], True, hit[2]
            value = fn()
            self._data[key] = (time.monotonic(), value, time.time())
            return value, False, self._data[key][2]

    def clear(self):
        with self._guard:
            self._data.clear()
