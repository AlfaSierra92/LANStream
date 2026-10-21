"""Bounded, process-local metadata cache with concurrent request coalescing."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import threading
import time


class MetadataCache:
    def __init__(self, maxsize=128, ttl=300):
        self.maxsize = maxsize
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._pending = {}

    def get(self, key, loader):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                expires, value = entry
                if time.monotonic() < expires:
                    self._entries.move_to_end(key)
                    return deepcopy(value)
                del self._entries[key]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return deepcopy(future.result())
        try:
            value = loader()
            with self._lock:
                self._entries[key] = (time.monotonic() + self.ttl, value)
                while len(self._entries) > self.maxsize:
                    self._entries.popitem(last=False)
            future.set_result(value)
            return deepcopy(value)
        except BaseException as exc:
            # Wake all waiting callers, but allow a subsequent request to retry.
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)
