"""FaultInjector: make any tool time out, return 503, return empty or return malformed data,
with a probability or on the Nth call. For tests, eval and the demo.

Spec (dict, or JSON in env SKYDESK_FAULTS), keyed by tool name or "*" for every tool:
    {"lookup_booking": {"mode": "timeout", "nth": [1]},          # first call times out
     "quote_amount":   {"mode": "malformed", "p": 0.3},           # 30% of calls
     "*":              {"mode": "503", "p": 0.05}}
modes: timeout | 503 | empty | malformed. Optional "delay_s" sleeps before a timeout.
SKYDESK_FAULT_SEED makes the probabilistic faults reproducible (default 7).
A fault fires BEFORE the tool body runs, so an injected failure never leaves a half-done write;
retrying the same idempotency_key is always safe.
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import Counter

MODES = ("timeout", "503", "empty", "malformed")


class FaultInjector:
    def __init__(self, spec: dict | None = None, seed: int = 7):
        self.spec = spec or {}
        for name, f in self.spec.items():
            if f.get("mode") not in MODES:
                raise ValueError(f"fault for {name}: mode must be one of {MODES}")
        self.rng = random.Random(seed)
        self.calls = Counter()
        self.fired: list[tuple[str, int, str]] = []

    @classmethod
    def from_env(cls) -> "FaultInjector":
        raw = os.environ.get("SKYDESK_FAULTS", "").strip()
        return cls(json.loads(raw) if raw else {}, int(os.environ.get("SKYDESK_FAULT_SEED", "7")))

    def check(self, tool: str) -> dict | None:
        """Count the call; return the faulty result to use instead of calling the tool, or None."""
        self.calls[tool] += 1
        n = self.calls[tool]
        f = self.spec.get(tool) or self.spec.get("*")
        if not f:
            return None
        nth = f.get("nth")
        hit = (n in nth if isinstance(nth, list) else n == nth) if nth is not None else self.rng.random() < f.get("p", 0)
        if not hit:
            return None
        mode = f["mode"]
        self.fired.append((tool, n, mode))
        if mode == "timeout":
            time.sleep(f.get("delay_s", 0))
            return {"ok": False, "data": None, "error": f"TIMEOUT: {tool} did not respond in time", "retryable": True}
        if mode == "503":
            return {"ok": False, "data": None, "error": f"HTTP 503 Service Unavailable from {tool} backend",
                    "retryable": True}
        if mode == "empty":
            return {"ok": True, "data": None, "error": None, "retryable": False}
        return {"ok": True, "data": "<html>502 B\x00d gat{eway \"amount\": NaN", "error": None, "retryable": False}


_active = FaultInjector.from_env()


def injector() -> FaultInjector:
    return _active


def set_injector(fi: FaultInjector | dict | None) -> FaultInjector:
    """Install a spec (or injector) for the whole process; None clears all faults."""
    global _active
    _active = fi if isinstance(fi, FaultInjector) else FaultInjector(fi or {})
    return _active
