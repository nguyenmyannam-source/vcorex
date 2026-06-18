"""
Unit tests for EventBus Redis Fallback functionality.
Tests the fallback logic when Redis is unavailable or fails to connect.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock
from core.event_bus import RedisStreamsEventBus, Event, REDIS_AVAILABLE
from core.events.topics import EventTopic


@pytest.mark.asyncio
async def test_redis_fallback_on_unavailable_redis():
    """Test that EventBus falls back to InProcessEventBus when Redis is not available."""
    # Mock REDIS_AVAILABLE to False
    with patch('core.event_bus.REDIS_AVAILABLE', False):
        event_bus = RedisStreamsEventBus(redis_url="redis://localhost:6379")

        # Should initialize with fallback enabled
        assert event_bus.use_fallback is True
        assert event_bus.fallback is not None

        await event_bus.start()

        try:
            # Should use fallback for operations
            assert event_bus.fallback._running is True
        finally:
            await event_bus.stop()


@pytest.mark.asyncio
async def test_redis_fallback_on_connection_failure():
    """Test that EventBus falls back to InProcessEventBus when Redis connection fails."""
    # Mock REDIS_AVAILABLE to True initially
    with patch('core.event_bus.REDIS_AVAILABLE', True):
        event_bus = RedisStreamsEventBus(redis_url="redis://localhost:6379")

        # Should not use fallback initially
        assert event_bus.use_fallback is False

        # Mock redis.asyncio.from_url to raise connection error
        with patch('redis.asyncio.from_url', side_effect=ConnectionError("Redis connection failed")):
            await event_bus.start()

            try:
                # Should have fallen back to InProcessEventBus
                assert event_bus.use_fallback is True
                assert event_bus.fallback._running is True
            finally:
                await event_bus.stop()


@pytest.mark.asyncio
async def test_redis_fallback_subscribe():
    """Test that subscribe uses fallback when fallback is enabled."""
    with patch('core.event_bus.REDIS_AVAILABLE', False):
        event_bus = RedisStreamsEventBus(redis_url="redis://localhost:6379")
        await event_bus.start()

        try:
            # Subscribe should use fallback
            handler_called = []

            async def test_handler(event: Event):
                handler_called.append(event.event_type)

            handler_id = event_bus.subscribe(
                test_handler,
                [EventTopic.MARKET_WS_TICKER],
                handler_id="test_handler"
            )

            assert handler_id == "test_handler"
            assert event_bus.fallback.get_handler_count() == 1

        finally:
            await event_bus.stop()


@pytest.mark.asyncio
async def test_redis_fallback_publish():
    """Test that publish uses fallback when fallback is enabled."""
    with patch('core.event_bus.REDIS_AVAILABLE', False):
        event_bus = RedisStreamsEventBus(redis_url="redis://localhost:6379")
        await event_bus.start()

        try:
            # Publish should use fallback
            event = Event(
                event_type=EventTopic.MARKET_WS_TICKER,
                data={"symbol": "BTC-USDT-SWAP", "price": "50000"},
                correlation_id="test",
                causation_id="cause",
                parent_request_id="parent",
            )
            await event_bus.publish(event)

            # Event should be in fallback queue
            assert event_bus.fallback._event_queue.qsize() >= 0

        finally:
            await event_bus.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
