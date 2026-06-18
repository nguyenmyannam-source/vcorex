from abc import ABC, abstractmethod
from typing import Optional, Dict


class MetricsAdapter(ABC):
    """Abstract base class for metrics adapters - standardizes metrics collection across components"""
    
    @abstractmethod
    def increment(self, name: str, value: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter metric"""
        pass
    
    @abstractmethod
    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge metric to a specific value"""
        pass
    
    @abstractmethod
    def timing(self, name: str, duration_ms: float, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a timing/duration metric"""
        pass


class InMemoryMetricsAdapter(MetricsAdapter):
    """In-memory implementation of MetricsAdapter for testing and development"""
    
    def __init__(self):
        self._counters: Dict[str, float] = {}
        self._gauges: Dict[str, float] = {}
        self._timings: Dict[str, list[float]] = {}

    def increment(self, name: str, value: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        if name not in self._counters:
            self._counters[name] = 0.0
        self._counters[name] += value

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        self._gauges[name] = value

    def timing(self, name: str, duration_ms: float, tags: Optional[Dict[str, str]] = None) -> None:
        if name not in self._timings:
            self._timings[name] = []
        self._timings[name].append(duration_ms)
        
    def get_counter(self, name: str) -> float:
        """Get current value of a counter"""
        return self._counters.get(name, 0.0)
        
    def get_gauge(self, name: str) -> float:
        """Get current value of a gauge"""
        return self._gauges.get(name, 0.0)
        
    def get_average_timing(self, name: str) -> float:
        """Get average timing for a metric"""
        timings = self._timings.get(name, [])
        return sum(timings) / len(timings) if timings else 0.0
        
    def reset(self) -> None:
        """Reset all metrics - useful for testing"""
        self._counters.clear()
        self._gauges.clear()
        self._timings.clear()

    async def get_metrics(self) -> Dict[str, float]:
        """Get all metrics"""
        return {**self._counters, **self._gauges}

    async def increment_phantom_verifications_attempted(self) -> None:
        """Increment phantom verifications attempted"""
        self.increment("phantom_verifications_attempted")

    async def increment_lock_contention(self) -> None:
        """Increment lock contention"""
        self.increment("lock_contention_rate")

    async def increment_cb_open(self) -> None:
        """Increment circuit breaker open"""
        self.increment("circuit_breaker_open_rate")

    async def increment_replay_attempts(self) -> None:
        """Increment replay attempts"""
        self.increment("callback_replay_attempts")

    async def increment_exchange_timeout(self) -> None:
        """Increment exchange timeout"""
        self.increment("exchange_timeout_frequency")

    async def increment_dlq_event(self) -> None:
        """Increment DLQ event"""
        self.increment("dlq_event_count")

    async def increment_retry_attempts(self) -> None:
        """Increment retry attempts"""
        self.increment("retry_attempts_total")

    async def increment_poison_event(self) -> None:
        """Increment poison event"""
        self.increment("poison_event_total")

    async def record_latency(self, name: str, value: float) -> None:
        """Record latency"""
        self.timing(name, value)

    async def increment_phantom_verifications_succeeded(self) -> None:
        """Increment phantom verifications succeeded"""
        self.increment("phantom_verifications_succeeded")

    async def increment_phantom_verifications_failed(self) -> None:
        """Increment phantom verifications failed"""
        self.increment("phantom_verifications_failed")

    async def increment_phantom_verifications_unknown(self) -> None:
        """Increment phantom verifications unknown"""
        self.increment("phantom_verifications_unknown")