"""Thread-safe rotation of server-side AI provider keys.

Keys are read from environment variables only. This module never logs or
returns a key value; it exposes only availability, counts, and the current
opaque value to the provider adapter that needs to construct an SDK client.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable

logger = logging.getLogger(__name__)


class KeyRotator:
    """Cycle through configured keys after quota, auth, or availability errors."""

    ROTATION_MARKERS = (
        "quota",
        "rate limit",
        "ratelimit",
        "resource_exhausted",
        "too many requests",
        "exhausted",
        "429",
        "service unavailable",
        "temporarily unavailable",
        "overloaded",
        "high demand",
        "try again",
        "unauthenticated",
        "invalid api key",
        "api key not valid",
        "invalid authentication",
        "permission_denied",
        "access_token_type_unsupported",
        "401",
        "403",
    )

    def __init__(
        self,
        env_var: str,
        fallback_env_vars: Iterable[str] = (),
    ) -> None:
        self.env_var = env_var
        self.fallback_env_vars = tuple(fallback_env_vars)
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._index = 0
        self.reload()

    def reload(self) -> None:
        """Reload configured keys without exposing them to callers or logs."""
        names = (self.env_var, *self.fallback_env_vars)
        values: list[str] = []
        for name in names:
            raw = os.getenv(name, "")
            values.extend(part.strip() for part in raw.split(",") if part.strip())

        # Preserve order while preventing the same credential being tried twice.
        unique = list(dict.fromkeys(values))
        with self._lock:
            current = self._keys[self._index] if self._keys and self._index < len(self._keys) else None
            self._keys = unique
            self._index = self._keys.index(current) if current in self._keys else 0
        logger.info("%s: %d provider key(s) available", self.env_var, len(unique))

    @property
    def available(self) -> bool:
        with self._lock:
            return bool(self._keys)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._keys)

    def current(self) -> str | None:
        with self._lock:
            if not self._keys:
                return None
            return self._keys[self._index]

    def rotate(self) -> str | None:
        """Advance to the next credential, returning the opaque value."""
        with self._lock:
            if not self._keys:
                return None
            self._index = (self._index + 1) % len(self._keys)
            logger.warning("%s rotated to provider key slot %d", self.env_var, self._index + 1)
            return self._keys[self._index]

    def is_rotation_error(self, exc: BaseException) -> bool:
        """Return whether a failure is plausibly fixed by another credential."""
        message = f"{type(exc).__name__} {exc}".lower()
        return any(marker in message for marker in self.ROTATION_MARKERS)

    def rotate_after(self, exc: BaseException) -> bool:
        """Rotate only when another key exists and the error is retryable."""
        if self.count < 2 or not self.is_rotation_error(exc):
            return False
        self.rotate()
        return True