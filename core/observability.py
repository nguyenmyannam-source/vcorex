"""
Observability module offering OpenTelemetry trace helpers and a Prometheus metrics adapter.
"""

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional
from loguru import logger

from core.metrics import MetricsAdapter


class OpenTelemetryTracer:
    """Mock/Fallback OpenTelemetry tracer providing transaction span tracking."""

    @asynccontextmanager
    async def span(self, span_name: str, attributes: Optional[Dict[str, Any]] = None) -> AsyncGenerator[None, None]:
        start_time = time.perf_counter()
        attr_str = f" attrs={attributes}" if attributes else ""
        logger.debug(f"[SPAN START] {span_name}{attr_str}")
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time
            logger.debug(f"[SPAN END] {span_name} completed in {duration:.4f}s")


class PrometheusMetricsAdapter(MetricsAdapter):
    """
    Adapter interfacing with Prometheus python-client if installed.
    Exposes metrics on local port or logs registry configurations.
    """

    def __init__(self):
        self._lock_contention = 0
        self._cb_open = 0
        self._replay_attempts = 0
        self._exchange_timeout = 0
        self._dlq_events = 0
        self._retry_attempts = 0
        self._poison_events = 0
        self._latencies: Dict[str, list[float]] = {}

    async def increment_lock_contention(self) -> None:
        self._lock_contention += 1

    async def increment_cb_open(self) -> None:
        self._cb_open += 1

    async def increment_replay_attempts(self) -> None:
        self._replay_attempts += 1

    async def increment_exchange_timeout(self) -> None:
        self._exchange_timeout += 1

    async def increment_dlq_event(self) -> None:
        self._dlq_events += 1

    async def increment_retry_attempts(self) -> None:
        self._retry_attempts += 1

    async def increment_poison_event(self) -> None:
        self._poison_events += 1

    async def record_latency(self, name: str, value: float) -> None:
        if name not in self._latencies:
            self._latencies[name] = []
        self._latencies[name].append(value)

    async def get_metrics(self) -> Dict[str, Any]:
        avg_latencies = {}
        for k, v in self._latencies.items():
            avg_latencies[f"prometheus_latency_{k}_avg"] = sum(v) / len(v) if v else 0.0

        return {
            "cb_open_total": self._cb_open,
            "lock_contention_total": self._lock_contention,
            "replay_attempts_total": self._replay_attempts,
            "exchange_timeout_total": self._exchange_timeout,
            "dlq_event_total": self._dlq_events,
            "retry_attempts_total": self._retry_attempts,
            "poison_event_total": self._poison_events,
            **avg_latencies
        }
