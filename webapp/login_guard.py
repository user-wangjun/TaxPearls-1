"""Bounded, single-process login failure throttling by account and client IP."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from hashlib import sha256
from math import ceil
from threading import RLock
import time


WINDOW_SECONDS = 15 * 60
MAX_KEYS = 8192


@dataclass
class _Failure:
    count: int
    last: float
    next_at: float
    locked_until: float


class LoginGuard:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._lock = RLock()
        self._failures: dict[tuple[str, str], _Failure] = {}
        self._inflight: set[tuple[str, str]] = set()

    @staticmethod
    def fingerprint(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    def _keys(self, username: str, ip: str):
        return (("account", self.fingerprint(username.strip().lower())),
                ("ip", self.fingerprint(ip)))

    def _fresh(self, key, now):
        current = self._failures.get(key)
        if current and now - current.last >= WINDOW_SECONDS:
            del self._failures[key]
            return None
        return current

    def retry_after(self, username: str, ip: str) -> int:
        with self._lock:
            now = self.clock()
            wait = 0
            for key in self._keys(username, ip):
                current = self._fresh(key, now)
                if current:
                    wait = max(wait, current.next_at - now, current.locked_until - now)
            return max(0, ceil(wait))

    @contextmanager
    def reserve(self, username: str, ip: str):
        """Only one verification per account or IP may run at a time."""
        keys = self._keys(username, ip)
        with self._lock:
            wait = self.retry_after(username, ip)
            if not wait and any(key in self._inflight for key in keys):
                wait = 1
            reserved = not wait
            if reserved:
                self._inflight.update(keys)
        try:
            yield wait
        finally:
            if reserved:
                with self._lock:
                    self._inflight.difference_update(keys)

    def record_failure(self, username: str, ip: str) -> int:
        with self._lock:
            now = self.clock()
            keys = self._keys(username, ip)
            for key in keys:
                self._fresh(key, now)
            if len(self._failures) >= MAX_KEYS - 1:
                self._failures = {key: value for key, value in self._failures.items()
                                  if now - value.last < WINDOW_SECONDS}
            needed = sum(key not in self._failures for key in keys)
            while len(self._failures) + needed > MAX_KEYS:
                oldest = min((key for key in self._failures if key not in keys),
                             key=lambda key: self._failures[key].last)
                del self._failures[oldest]
            counts = []
            for key in keys:
                current = self._failures.get(key)
                count = (current.count if current else 0) + 1
                delay = min(2 ** (count - 5), 32) if count >= 5 else 0
                locked = now + WINDOW_SECONDS if count >= 10 else 0
                self._failures[key] = _Failure(count, now, now + delay, locked)
                counts.append(count)
            return max(counts)

    def record_success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(("account", self.fingerprint(username.strip().lower())), None)


class RateLimiter:
    """按命名维度限制请求频率（内存滑动窗口）。

    本项目约束为单进程单副本（FR-G08），内存态成立；若改为多进程，
    须迁移到集中式存储，否则计数失效。
    字典容量有硬上限；只清理过期键，容量不足时拒绝新请求，不能通过
    洪泛淘汰仍有效的限流记录。规则的所有维度必须同时提供。
    """

    def __init__(self, rules: dict[str, tuple[int, int]], max_keys: int = 8192, clock=None):
        # rules: 维度名 -> (窗口内最大次数, 窗口秒数)
        self._rules = dict(rules)
        if (not self._rules or type(max_keys) is not int or max_keys < len(self._rules)
                or any(type(limit) is not int or type(window) is not int or limit <= 0 or window <= 0
                       for limit, window in self._rules.values())):
            raise ValueError("限流规则和容量必须为正整数，并容纳全部维度。")
        self._max_keys = max_keys
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._hits: dict[tuple[str, str], list[float]] = {}

    def allow(self, **identifiers: str) -> bool:
        """所有维度均未超限时返回 True，并记录本次请求；任一超限返回 False。"""
        if identifiers.keys() != self._rules.keys():
            raise ValueError("限流请求必须提供全部且仅提供已配置维度。")
        keys = [(name, self._key(value)) for name, value in identifiers.items()]
        with self._lock:
            now = self._clock()
            # Prune by each dimension's own window, not the largest window.
            for key in list(self._hits):
                hits = [t for t in self._hits[key] if now - t < self._rules[key[0]][1]]
                if hits:
                    self._hits[key] = hits
                else:
                    del self._hits[key]
            for name, key in keys:
                limit, window = self._rules[name]
                hits = self._hits.get((name, key), [])
                if len(hits) >= limit:
                    return False
            if len(self._hits) + sum(key not in self._hits for key in keys) > self._max_keys:
                return False
            for name, key in keys:
                self._hits.setdefault((name, key), []).append(now)
            return True

    @staticmethod
    def _key(value: str) -> str:
        return sha256(value.strip().lower().encode("utf-8")).hexdigest()
