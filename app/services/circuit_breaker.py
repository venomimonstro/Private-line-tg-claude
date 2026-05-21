from __future__ import annotations
import asyncio
import logging
import time
from enum import Enum
from typing import TypeVar, Awaitable

logger = logging.getLogger(__name__)
T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit breaker is OPEN or a probe is already in flight."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 120.0) -> None:
        self.name = name
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._lock = asyncio.Lock()
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self._timeout:
                return CircuitState.HALF_OPEN
        return self._state

    async def call(self, coro: Awaitable[T]) -> T:
        async with self._lock:
            current = self.state
            if current == CircuitState.OPEN:
                raise CircuitOpenError(f"Circuit '{self.name}' is OPEN — server unavailable")
            if current == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(f"Circuit '{self.name}' probe already in flight")
                self._probe_in_flight = True
                self._state = CircuitState.HALF_OPEN

        try:
            result = await coro
            async with self._lock:
                self._probe_in_flight = False
                if self._failure_count > 0:
                    logger.info("Circuit '%s' recovered (was %d failures)", self.name, self._failure_count)
                self._failure_count = 0
                self._state = CircuitState.CLOSED
            return result
        except Exception:
            async with self._lock:
                self._probe_in_flight = False
                self._failure_count += 1
                self._last_failure_time = time.monotonic()
                if self._failure_count >= self._threshold:
                    if self._state != CircuitState.OPEN:
                        logger.warning("Circuit '%s' OPENED after %d failures", self.name, self._failure_count)
                    self._state = CircuitState.OPEN
            raise

    def is_available(self) -> bool:
        return self.state != CircuitState.OPEN

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._probe_in_flight = False


_registry: dict[int, CircuitBreaker] = {}


def get_circuit_breaker(server_id: int) -> CircuitBreaker:
    if server_id not in _registry:
        _registry[server_id] = CircuitBreaker(name=f"server_{server_id}")
    return _registry[server_id]


def reset_circuit(server_id: int) -> None:
    if server_id in _registry:
        _registry[server_id].reset()
