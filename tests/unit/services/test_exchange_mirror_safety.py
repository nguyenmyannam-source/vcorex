"""
Unit tests for ExchangeMirrorCache: resync logic, is_consistent(), and failure recovery.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.position.exchange_mirror import ExchangeMirrorCache
from core.event_bus import InProcessEventBus, Event
from core.events.topics import EventTopic


@pytest.fixture
def mock_exchange():
    """Create a mock OKX exchange."""
    exchange = MagicMock()
    exchange.fetch_account_equity = AsyncMock(return_value={
        "totalEq": 10000.0,
        "availEq": 5000.0,
    })
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": MagicMock(total=10000, free=5000),
    })
    exchange._request = AsyncMock(return_value={
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "pos": "1.5",
                "avgPx": "42000",
                "upl": "500",
                "uplLastPx": "500",
                "uplRatio": "0.05",
                "margin": "8400",
                "markPx": "42500",
                "liqPx": "30000",
                "cTime": 1717000000000,
                "uTime": 1717000000000,
            }
        ]
    })
    return exchange


@pytest.fixture
def event_bus():
    """Create a fresh event bus."""
    return InProcessEventBus()


@pytest.fixture
async def exchange_mirror(mock_exchange, event_bus):
    """Create an ExchangeMirrorCache with mocked dependencies."""
    await event_bus.start()
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    yield mirror
    mirror.stop()
    await event_bus.stop()


@pytest.mark.asyncio
async def test_is_consistent_initial_state(exchange_mirror):
    """Test that is_consistent() returns False before initial snapshot."""
    assert not exchange_mirror.is_consistent(), "Should be inconsistent before initial snapshot"


@pytest.mark.asyncio
async def test_is_consistent_after_resync(exchange_mirror, event_bus):
    """Test that is_consistent() returns True after successful atomic resync."""
    # Trigger resync via event bus
    await event_bus.publish(Event(EventTopic.WS_RECONNECTED, data={}, source="test"))
    
    # Give time for debounced resync to complete
    await asyncio.sleep(2.1) # 2 seconds debounce + a little extra
    
    # Should now be consistent
    assert exchange_mirror.is_consistent(), "Should be consistent after successful resync"
    assert exchange_mirror._initial_snapshot_received
    assert not exchange_mirror._last_resync_failed


@pytest.mark.asyncio
async def test_resync_failure_flag(mock_exchange, event_bus):
    """Test that _last_resync_failed is set when resync fails."""
    await event_bus.start()
    mock_exchange.fetch_account_equity = AsyncMock(side_effect=Exception("API error"))
    mock_exchange._request = AsyncMock(side_effect=Exception("API error"))
    mock_exchange.fetch_balance = AsyncMock(side_effect=Exception("API error"))
    
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    
    # Trigger resync via event bus - should fail
    await event_bus.publish(Event(EventTopic.WS_RECONNECTED, data={}, source="test"))
    
    # Give time for debounced resync to complete with all retries
    # 2s debounce + 1s + 2s + 4s + 8s + 16s (exponential backoff for 5 retries)
    await asyncio.sleep(20)
    
    # Should be marked as failed
    assert mirror._last_resync_failed
    assert not mirror.is_consistent(), "Should be inconsistent when resync fails"
    
    mirror.stop()
    await event_bus.stop()


@pytest.mark.asyncio
async def test_mirror_resync_success_event(exchange_mirror, event_bus):
    """Test that MIRROR_RESYNC_SUCCESS event is emitted after successful resync."""
    # Capture published events
    published_events = []
    
    async def capture_event(event):
        published_events.append(event)
    
    event_bus.subscribe(capture_event, [EventTopic.MIRROR_RESYNC_SUCCESS], handler_id="test_capture")
    
    # Trigger resync via event bus
    await event_bus.publish(Event(EventTopic.WS_RECONNECTED, data={}, source="test"))
    
    # Give time for debounced resync to complete
    await asyncio.sleep(2.1) # 2 seconds debounce + a little extra
    
    # Should have published the success event
    assert len(published_events) > 0, "MIRROR_RESYNC_SUCCESS event not published"
    
    success_event = published_events[0]
    assert success_event.event_type == EventTopic.MIRROR_RESYNC_SUCCESS
    assert "positions" in success_event.data


@pytest.mark.asyncio
async def test_atomic_resync_with_retry(exchange_mirror, mock_exchange, event_bus):
    """Test that resync retries on transient failures."""
    call_count = [0]
    
    async def flaky_equity():
        call_count[0] += 1
        if call_count[0] < 2:
            raise Exception("Transient error")
        return {"totalEq": 10000.0, "availEq": 5000.0}

    mock_exchange.fetch_account_equity.side_effect = flaky_equity
    mock_exchange._request.return_value = {"data": []}
    mock_exchange.fetch_balance.return_value = {
        "USDT": MagicMock(total=10000, free=5000),
    }
    
    # Trigger resync via event bus
    await event_bus.publish(Event(EventTopic.WS_RECONNECTED, data={}, source="test"))
    
    # Give time for debounced resync to complete with retry
    # 2s debounce + 1s retry wait + buffer for success
    await asyncio.sleep(5)
    
    # Should have succeeded after retry
    assert exchange_mirror.is_consistent()
    assert call_count[0] >= 2, "Should have retried at least once"


@pytest.mark.asyncio
async def test_resync_clears_old_state(exchange_mirror, mock_exchange, event_bus):
    """Test that atomic resync clears old position/account state."""
    # Add some old data to the mirror provided by the fixture
    exchange_mirror._positions["OLD-POS"] = {"instId": "OLD-POS", "pos": "1"}
    assert "OLD-POS" in exchange_mirror._positions
    
    # Mock exchange to return empty positions for this resync
    mock_exchange._request.return_value = {"data": []}

    # Trigger resync via event bus
    await event_bus.publish(Event(EventTopic.WS_RECONNECTED, data={}, source="test"))
    
    # Give time for debounced resync to complete
    await asyncio.sleep(2.1) # 2 seconds debounce + a little extra
    
    # Old data should be cleared
    assert "OLD-POS" not in exchange_mirror._positions, "Atomic resync should clear old positions"


@pytest.mark.asyncio
async def test_duplicate_event_deduplication(exchange_mirror):
    """Test that duplicate WS events are dropped."""
    from core.event_bus import Event
    
    # Create a duplicate event
    evt1 = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"instId": "BTC-USDT-SWAP", "uTime": 1717000000000},
        source="test"
    )
    
    evt2 = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"instId": "BTC-USDT-SWAP", "uTime": 1717000000000},
        source="test"
    )
    
    # Process first event
    is_dup1 = exchange_mirror._is_duplicate_or_stale(
        EventTopic.WS_RAW_POSITION, 
        {"instId": "BTC-USDT-SWAP", "uTime": 1717000000000},
        "BTC-USDT-SWAP"
    )
    assert not is_dup1, "First event should not be a duplicate"
    
    # Process second identical event
    is_dup2 = exchange_mirror._is_duplicate_or_stale(
        EventTopic.WS_RAW_POSITION,
        {"instId": "BTC-USDT-SWAP", "uTime": 1717000000000},
        "BTC-USDT-SWAP"
    )
    assert is_dup2, "Second identical event should be detected as duplicate"
    assert exchange_mirror.metrics["duplicate_events_dropped"] > 0