from enum import Enum
import time
import logging

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BaseCircuitBreaker:
    """Base circuit breaker implementation shared across all components"""
    
    def __init__(self, threshold: int, cooldown: float, name: str = "default"):
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.last_state_change = time.time()
        self.threshold = threshold
        self.cooldown = cooldown
        logger.info(f"CircuitBreaker '{name}' initialized: threshold={threshold}, cooldown={cooldown}s")

    def record_failure(self) -> None:
        """Record a failure and potentially open the circuit"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.threshold and self.state == CircuitState.CLOSED:
            self._open_circuit()

    def record_success(self) -> None:
        """Record a success and close the circuit if it was half-open"""
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self._close_circuit()

    def allow_request(self) -> bool:
        """Check if requests are allowed through the circuit breaker"""
        if self.state == CircuitState.CLOSED:
            return True
            
        # If cooldown period has passed, allow a test request (half-open)
        if time.time() - self.last_state_change > self.cooldown:
            self._half_open_circuit()
            return True
            
        return False

    def _open_circuit(self) -> None:
        """Transition circuit to OPEN state"""
        self.state = CircuitState.OPEN
        self.last_state_change = time.time()
        logger.warning(f"CircuitBreaker '{self.name}' OPENED after {self.failure_count} failures")

    def _half_open_circuit(self) -> None:
        """Transition circuit to HALF_OPEN state"""
        if self.state != CircuitState.HALF_OPEN:
            self.state = CircuitState.HALF_OPEN
            logger.info(f"CircuitBreaker '{self.name}' entering HALF_OPEN state to test recovery")

    def _close_circuit(self) -> None:
        """Transition circuit to CLOSED state"""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        logger.info(f"CircuitBreaker '{self.name}' CLOSED - service recovered")

    def reset(self) -> None:
        """Manually reset the circuit breaker to closed state"""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_state_change = time.time()
        logger.info(f"CircuitBreaker '{self.name}' manually reset")