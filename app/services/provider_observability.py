"""Small, process-safe provider health metrics for PrepSage.

The counters intentionally contain no prompts, answers, keys, or user data.
They are suitable for operational dashboards and a user-safe degraded status.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

_WINDOW = dt.timedelta(minutes=15)
_ALERT_MINIMUM = 5
_ALERT_RATE = 0.35


class ProviderMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[str, dict[str, int]] = defaultdict(
            lambda: {"success": 0, "fallback": 0, "failure": 0}
        )
        self._events: deque[tuple[dt.datetime, str, str]] = deque()
        self._last_alert: dict[str, dt.datetime] = {}

    def record(self, operation: str, outcome: str, *, provider: str = "unknown", code: str = "") -> None:
        operation = (operation or "unknown")[:64]
        outcome = outcome if outcome in {"success", "fallback", "failure"} else "failure"
        provider = (provider or "unknown")[:64]
        timestamp = dt.datetime.now(dt.UTC)
        with self._lock:
            self._totals[operation][outcome] += 1
            self._events.append((timestamp, operation, outcome))
            self._prune(timestamp)
            recent = [item for item in self._events if item[1] == operation]
            fallbacks = sum(1 for _, _, item_outcome in recent if item_outcome == "fallback")
            failures = sum(1 for _, _, item_outcome in recent if item_outcome == "failure")
            total = len(recent)
            last_alert = self._last_alert.get(operation)
            should_alert = (
                total >= _ALERT_MINIMUM
                and (fallbacks + failures) / total >= _ALERT_RATE
                and (last_alert is None or timestamp - last_alert > _WINDOW)
            )
            if should_alert:
                self._last_alert[operation] = timestamp
        logger.info(
            "provider_event operation=%s provider=%s outcome=%s code=%s",
            operation,
            provider,
            outcome,
            code[:64],
        )
        if should_alert:
            logger.warning(
                "provider_degraded operation=%s fallback_or_failure_rate=%.3f sample_size=%s",
                operation,
                (fallbacks + failures) / max(total, 1),
                total,
            )

    def _prune(self, timestamp: dt.datetime) -> None:
        cutoff = timestamp - _WINDOW
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def snapshot(self) -> dict[str, Any]:
        timestamp = dt.datetime.now(dt.UTC)
        with self._lock:
            self._prune(timestamp)
            operations: dict[str, Any] = {}
            for operation, values in self._totals.items():
                total = sum(values.values())
                operations[operation] = {
                    **values,
                    "total": total,
                    "fallback_or_failure_rate": round(
                        (values["fallback"] + values["failure"]) / max(total, 1), 4
                    ),
                }
            recent_total = len(self._events)
            recent_degraded = sum(
                1 for _, _, outcome in self._events if outcome in {"fallback", "failure"}
            )
            return {
                "window_minutes": int(_WINDOW.total_seconds() // 60),
                "status": "degraded"
                if recent_total >= _ALERT_MINIMUM
                and recent_degraded / max(recent_total, 1) >= _ALERT_RATE
                else "ok",
                "recent_calls": recent_total,
                "recent_fallback_or_failure": recent_degraded,
                "recent_degraded_rate": round(recent_degraded / max(recent_total, 1), 4),
                "operations": operations,
            }


metrics = ProviderMetrics()


def record_provider_event(
    operation: str, outcome: str, *, provider: str = "unknown", code: str = ""
) -> None:
    metrics.record(operation, outcome, provider=provider, code=code)


def provider_snapshot() -> dict[str, Any]:
    return metrics.snapshot()


def user_safe_provider_status(provider: str | None, *, pending: bool = False) -> dict[str, Any]:
    normalized = (provider or "local").lower()
    degraded = pending or normalized.startswith("local") or "fallback" in normalized
    return {
        "provider": provider or "local",
        "degraded": degraded,
        "notice": (
            "The AI provider is temporarily unavailable. Your session is continuing "
            "with PrepSage's built-in coach."
            if degraded
            else ""
        ),
    }