from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Debounce:
    interval_s: float
    _last_ts: float = 0.0

    def allow(self, now: float = None) -> bool:
        t = now if now is not None else time.time()
        if t - self._last_ts >= self.interval_s:
            self._last_ts = t
            return True
        return False
