"""
Unit tests for EventBus Circuit Breaker functionality.
Tests the circuit breaker logic that drops non-critical events when queue is >80% capacity.
"""

import asyncio
import pytest
from unittest.mock import patch
from core.event_bus import InProcessEventBus, Event
from core.events.topics import EventTopic


@pytest.mark.asyncio
async def test_circuit_breaker_drops_non_critical_events():
    """Test that circuit breaker drops non-critical events when queue is >80% capacity."""
    event_bus = InProcessEventBus()
    await event_bus.start()
    
    # Mock queue.qsize() to return 8500 (>80% of 10000)
    original_qsize = event_bus._event_queue.qsize
    event_bus._event_queue.qsize = lambda: 8500
    
    try:
        # Trigger circuit breaker to open by recording enough failures
        for _ in range(event_bus._circuit_breaker.threshold):
            event_bus._circuit_breaker.record_failure()
            
        # Publish a non-critical event (should be dropped by circuit breaker)
        non_critical_event = Event(
            event_type=EventTopic.MARKET_WS_TICKER,
            data={"symbol": "ETH-USDT-SWAP", "price": "3000"},
            correlation_id="non-critical",
            causation_id="cause",
            parent_request_id="parent",
        )
        await event_bus.publish(non_critical_event)
        
        # Circuit breaker should have dropped the non-critical event
        assert event_bus.metrics["events_dropped_by_circuit_breaker"] >= 1
        
    finally:
        # Restore original qsize
        event_bus._event_queue.qsize = original_qsize
        await event_bus.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_allows_critical_events():
    """Test that circuit breaker allows critical events even when queue is >80% capacity."""
    event_bus = InProcessEventBus()
    await event_bus.start()
    
    # Mock queue.qsize() to return 8500 (>80% of 10000)
    original_qsize = event_bus._event_queue.qsize
    event_bus._event_queue.qsize = lambda: 8500
    
    try:
        # Publish a critical event (should NOT be dropped)
        critical_event = Event(
            event_type=EventTopic.STRATEGY_SIGNAL_GENERATED,
            data={"symbol": "BTC-USDT-SWAP", "signal": "BUY"},
            correlation_id="critical",
            causation_id="cause",
            parent_request_id="parent",
        )
        await event_bus.publish(critical_event)
        
        # Circuit breaker should NOT have dropped the critical event
        # (metrics should still be 0 for dropped events)
        assert event_bus.metrics.get("events_dropped_by_circuit_breaker", 0) == 0
        
    finally:
        # Restore original qsize
        event_bus._event_queue.qsize = original_qsize
        await event_bus.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_inactive_below_threshold():
    """Test that circuit breaker does not drop events when queue is below 80% capacity."""
    event_bus = InProcessEventBus()
    await event_bus.start()
    
    # Mock queue.qsize() to return 5000 (<80% of 10000)
    original_qsize = event_bus._event_queue.qsize
    event_bus._event_queue.qsize = lambda: 5000
    
    try:
        # Publish a non-critical event (should NOT be dropped)
        non_critical_event = Event(
            event_type=EventTopic.MARKET_WS_TICKER,
            data={"symbol": "ETH-USDT-SWAP", "price": "3000"},
            correlation_id="non-critical",
            causation_id="cause",
            parent_request_id="parent",
        )
        await event_bus.publish(non_critical_event)
        
        # Circuit breaker should NOT have dropped the event
        assert event_bus.metrics.get("events_dropped_by_circuit_breaker", 0) == 0
        
    finally:
        # Restore original qsize
        event_bus._event_queue.qsize = original_qsize
        await event_bus.stop()


@pytest.mark.asyncio
async def test_circuit_breaker_allows_all_critical_event_types():
    """Test that all critical event types are allowed through circuit breaker."""
    event_bus = InProcessEventBus()
    await event_bus.start()
    
    # Mock queue.qsize() to return 8500 (>80% of 10000)
    original_qsize = event_bus._event_queue.qsize
    event_bus._event_queue.qsize = lambda: 8500
    
    try:
        # Test all critical event types
        critical_event_types = [
            EventTopic.STRATEGY_SIGNAL_GENERATED,
            EventTopic.RISK_SIGNAL_APPROVED,
            EventTopic.POSITION_OPENED,
            EventTopic.POSITION_CLOSED,
            EventTopic.POSITION_CLOSE_REQUEST,
        ]
        
        for event_type in critical_event_types:
            critical_event = Event(
                event_type=event_type,
                data={"test": "data"},
                correlation_id=f"critical-{event_type}",
                causation_id="cause",
                parent_request_id="parent",
            )
            await event_bus.publish(critical_event)
        
        # Circuit breaker should NOT have dropped any critical events
        assert event_bus.metrics.get("events_dropped_by_circuit_breaker", 0) == 0
        
    finally:
        # Restore original qsize
        event_bus._event_queue.qsize = original_qsize
        await event_bus.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])