"""
Unit tests for ExchangeMirrorCache: resync logic, is_consistent(), and failure recovery.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.position.exchange_mirror import ExchangeMirrorCache
from core.event_bus import InProcessEventBus
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
async def test_is_consistent_after_resync(exchange_mirror):
    """Test that is_consistent() returns True after successful atomic resync."""
    # Trigger resync
    exchange_mirror._is_syncing = True
    await exchange_mirror._run_atomic_resync()
    
    # Should now be consistent
    assert exchange_mirror.is_consistent(), "Should be consistent after successful resync"
    assert exchange_mirror._initial_snapshot_received
    assert not exchange_mirror._last_resync_failed


@pytest.mark.asyncio
async def test_resync_failure_flag(mock_exchange, event_bus):
    """Test that _last_resync_failed is set when resync fails."""
    mock_exchange.fetch_account_equity = AsyncMock(side_effect=Exception("API error"))
    mock_exchange._request = AsyncMock(side_effect=Exception("API error"))
    mock_exchange.fetch_balance = AsyncMock(side_effect=Exception("API error"))
    
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    
    # Trigger resync - should fail
    mirror._is_syncing = True
    await mirror._run_atomic_resync()
    
    # Should be marked as failed
    assert mirror._last_resync_failed
    assert not mirror.is_consistent(), "Should be inconsistent when resync fails"
    
    mirror.stop()


@pytest.mark.asyncio
async def test_mirror_resync_success_event(mock_exchange, event_bus):
    """Test that MIRROR_RESYNC_SUCCESS event is emitted after successful resync."""
    await event_bus.start()
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    
    # Capture published events
    published_events = []
    
    async def capture_event(event):
        published_events.append(event)
    
    event_bus.subscribe(capture_event, [EventTopic.MIRROR_RESYNC_SUCCESS], handler_id="test_capture")
    
    # Trigger resync
    mirror._is_syncing = True
    await mirror._run_atomic_resync()
    
    # Give event bus time to process
    await asyncio.sleep(0.5)
    
    # Should have published the success event
    assert len(published_events) > 0, "MIRROR_RESYNC_SUCCESS event not published"
    
    success_event = published_events[0]
    assert success_event.event_type == EventTopic.MIRROR_RESYNC_SUCCESS
    assert "positions" in success_event.data
    
    mirror.stop()


@pytest.mark.asyncio
async def test_atomic_resync_with_retry(mock_exchange, event_bus):
    """Test that resync retries on transient failures."""
    call_count = [0]
    
    async def flaky_equity():
        call_count[0] += 1
        if call_count[0] < 2:
            raise Exception("Transient error")
        return {"totalEq": 10000.0, "availEq": 5000.0}

    mock_exchange.fetch_account_equity = flaky_equity
    mock_exchange._request = AsyncMock(return_value={"data": []})
    mock_exchange.fetch_balance = AsyncMock(return_value={
        "USDT": MagicMock(total=10000, free=5000),
    })
    
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    
    # Trigger resync - should retry and succeed
    mirror._is_syncing = True
    await mirror._run_atomic_resync()
    
    # Should have succeeded after retry
    assert mirror.is_consistent()
    assert call_count[0] >= 2, "Should have retried at least once"
    
    mirror.stop()


@pytest.mark.asyncio
async def test_resync_clears_old_state(mock_exchange, event_bus):
    """Test that atomic resync clears old position/account state."""
    mirror = ExchangeMirrorCache(event_bus, mock_exchange)
    mirror.start()
    
    # Add some old data
    mirror._positions["OLD-POS"] = {"instId": "OLD-POS", "pos": "1"}
    assert "OLD-POS" in mirror._positions
    
    # Trigger resync
    mirror._is_syncing = True
    await mirror._run_atomic_resync()
    
    # Old data should be cleared
    assert "OLD-POS" not in mirror._positions, "Atomic resync should clear old positions"
    
    mirror.stop()


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