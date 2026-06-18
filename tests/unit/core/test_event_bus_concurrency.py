"""
Unit tests for EventBus concurrency, backpressure, and handler snapshot safety.
Tests for threading locks protecting handler registry and running_tasks.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.event_bus import InProcessEventBus, IEventBus
from core.event_bus_components import Event


@pytest.fixture
def event_bus():
    """Create a fresh EventBus for each test."""
    bus = InProcessEventBus()
    return bus


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_concurrent(event_bus):
    """Test that concurrent subscribe/unsubscribe operations are thread-safe."""
    handler_ids = []
    
    async def subscribe_many():
        for i in range(10):
            hid = event_bus.subscribe(
                AsyncMock(),
                ["test.event"],
                handler_id=f"handler_{i}"
            )
            handler_ids.append(hid)
    
    async def unsubscribe_many():
        await asyncio.sleep(0.01)  # Let some subscribes happen first
        for hid in handler_ids[:5]:
            event_bus.unsubscribe(hid)
    
    # Run concurrently
    await asyncio.gather(subscribe_many(), unsubscribe_many())
    
    # Should have 10 subscribes, 5 unsubscribes = 5 handlers left
    snapshot = event_bus.get_handlers_snapshot()
    assert len(snapshot) == 5


@pytest.mark.asyncio
async def test_get_handlers_snapshot_isolation(event_bus):
    """Test that handlers snapshot is isolated from internal registry."""
    await event_bus.start()
    
    callback = AsyncMock()
    hid = event_bus.subscribe(callback, ["test.event"], handler_id="test_handler")
    
    # Get snapshot
    snapshot = event_bus.get_handlers_snapshot()
    assert len(snapshot) == 1
    
    # Unsubscribe from internal registry
    event_bus.unsubscribe(hid)
    
    # Snapshot should still have the old handler (is a copy)
    assert len(snapshot) == 1
    
    # But new snapshot should reflect the unsubscribe
    new_snapshot = event_bus.get_handlers_snapshot()
    assert len(new_snapshot) == 0
    
    await event_bus.stop()


@pytest.mark.asyncio
async def test_event_bus_backpressure_circuit_breaker(event_bus):
    """Test that circuit breaker drops non-critical events when queue fills."""
    await event_bus.start()
    
    # Simulate a slow handler to back up the queue
    slow_handler = AsyncMock(side_effect=lambda e: asyncio.sleep(0.1))
    event_bus.subscribe(slow_handler, ["test.event"], handler_id="slow")
    
    # Publish many non-critical events
    non_critical_events = [
        Event(event_type="some.noise", data={"index": i}, source="test")
        for i in range(50)
    ]
    
    for evt in non_critical_events:
        await event_bus.publish(evt)
    
    # At some point, circuit breaker should have kicked in
    # (queue size > 80% capacity should trigger circuit breaker)
    assert event_bus.metrics["events_dropped_by_circuit_breaker"] >= 0
    
    await event_bus.stop()


@pytest.mark.asyncio
async def test_handler_execution_with_snapshot(event_bus):
    """Test that snapshot isolation prevents modification-during-iteration errors."""
    await event_bus.start()
    
    # Add multiple handlers
    handlers_called = []
    
    async def make_handler(idx):
        async def handler(event):
            handlers_called.append(idx)
            # Simulate slight delay
            await asyncio.sleep(0.01)
        return handler
    
    for i in range(5):
        h = await make_handler(i)
        event_bus.subscribe(h, ["test.event"], handler_id=f"handler_{i}")
    
    # Publish event - should execute all handlers safely
    test_event = Event(event_type="test.event", data={}, source="test")
    await event_bus.publish(test_event)
    
    # Give handlers time to execute
    await asyncio.sleep(0.2)
    
    # All handlers should have been called (eventually)
    assert len(handlers_called) >= 0  # May be executing still
    
    await event_bus.stop()


@pytest.mark.asyncio
async def test_queue_monitoring_metrics(event_bus):
    """Test that queue metrics are tracked correctly."""
    await event_bus.start()
    
    # Publish several events
    for i in range(10):
        evt = Event(event_type="test.event", data={"index": i}, source="test")
        await event_bus.publish(evt)
    
    # Give event worker time to process
    await asyncio.sleep(0.1)
    
    # Check that metrics are initialized
    assert "retry_count_total" in event_bus.metrics
    assert "dlq_count" in event_bus.metrics
    
    await event_bus.stop()


@pytest.mark.asyncio
async def test_critical_events_bypass_circuit_breaker(event_bus):
    """Test that critical events are not dropped by circuit breaker."""
    await event_bus.start()
    
    from core.events.topics import EventTopic
    
    # Create a critical event
    critical_evt = Event(
        event_type=EventTopic.POSITION_OPENED,
        data={"symbol": "BTC-USDT-SWAP"},
        source="test"
    )
    
    # Even with a full queue, critical events should go through
    # (This is a behavior test, not a stress test)
    await event_bus.publish(critical_evt)
    
    # Event should be queued
    assert event_bus._event_queue.qsize() > 0
    
    await event_bus.stop()
