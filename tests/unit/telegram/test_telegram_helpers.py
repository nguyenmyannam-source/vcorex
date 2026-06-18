"""Unit tests for Telegram helper classes."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot

from core.event_bus import EventBus
from core.events.topics import EventTopic
from interfaces.telegram.dashboard_controller import DashboardController
from interfaces.telegram.message_dispatcher import MessageDispatcher
from interfaces.telegram.message_renderer import MessageRenderer
from interfaces.telegram.rate_limiter import RateLimiter


class TestMessageRenderer:
    """Test MessageRenderer."""

    def test_render_health_data(self):
        """Test rendering health data."""
        data = {
            "uptime": 3600,
            "cpu_usage": 45.5,
            "memory_usage": 60.2,
            "positions_count": 3,
        }
        result = MessageRenderer.render_health_data(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_trading_data_open_positions(self):
        """Test rendering trading data for open positions."""
        data = {
            "action": "open_positions",
            "positions": [
                {
                    "symbol": "BTC-USDT",
                    "side": "long",
                    "entry": 42000,
                    "current": 43000,
                    "pnl": 1000,
                }
            ],
        }
        result = MessageRenderer.render_trading_data(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_analytics_data(self):
        """Test rendering analytics data."""
        data = {
            "action": "pnp_dashboard",
            "total_pnl": 5000,
            "trades_count": 10,
            "winrate": 60.0,
        }
        result = MessageRenderer.render_analytics_data(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_history_data(self):
        """Test rendering history data."""
        data = {
            "action": "trade_history",
            "trades": [
                {
                    "symbol": "ETH-USDT",
                    "profit": 500,
                    "timestamp": "2026-05-17 10:00:00",
                }
            ],
        }
        result = MessageRenderer.render_history_data(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_positions_history_data(self):
        """Test rendering positions history data."""
        data = {
            "action": "positions_history",
            "history": [
                {
                    "symbol": "BTC-USDT-SWAP",
                    "side": "LONG",
                    "open_price": 60000.0,
                    "close_price": 61000.0,
                    "pnl": 100.0,
                    "pnl_ratio": 1.5,
                    "leverage": "10",
                    "margin_mode": "Cô lập",
                    "time": "12:00:00 19/05",
                    "close_type": "2",
                }
            ],
        }
        result = MessageRenderer.render_history_data(data)
        assert isinstance(result, str)
        assert "LỊCH SỬ VỊ THẾ" in result
        assert "BTC-USDT-SWAP" in result


class TestRateLimiter:
    """Test RateLimiter."""

    def test_init(self):
        """Test rate limiter initialization."""
        limiter = RateLimiter()
        assert not limiter.is_in_backoff()
        assert limiter.get_backoff_remaining() == 0.0

    def test_apply_backoff(self):
        """Test applying backoff."""
        limiter = RateLimiter()
        limiter.apply_backoff(60)
        assert limiter.is_in_backoff()
        remaining = limiter.get_backoff_remaining()
        assert 59 <= remaining <= 60

    def test_clear_backoff(self):
        """Test clearing backoff."""
        limiter = RateLimiter()
        limiter.apply_backoff(60)
        assert limiter.is_in_backoff()
        limiter.clear_backoff()
        assert not limiter.is_in_backoff()
        assert limiter.get_backoff_remaining() == 0.0


class TestMessageDispatcher:
    """Test MessageDispatcher."""

    @pytest.mark.asyncio
    async def test_init(self):
        """Test dispatcher initialization."""
        mock_bot = MagicMock(spec=Bot)
        event_bus = EventBus()
        rate_limiter = RateLimiter()

        dispatcher = MessageDispatcher(mock_bot, 12345, event_bus, rate_limiter)
        assert dispatcher._bot == mock_bot
        assert dispatcher._chat_id == 12345
        assert dispatcher.event_bus == event_bus

    @pytest.mark.asyncio
    async def test_send_message(self):
        """Test sending a new message."""
        mock_bot = MagicMock(spec=Bot)
        mock_bot.send_message = AsyncMock()
        event_bus = EventBus()
        rate_limiter = RateLimiter()

        dispatcher = MessageDispatcher(mock_bot, 12345, event_bus, rate_limiter)
        await dispatcher.send_or_edit_message(text="Test message")

        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        assert call_args[1]["chat_id"] == 12345
        assert call_args[1]["text"] == "Test message"

    @pytest.mark.asyncio
    async def test_edit_message(self):
        """Test editing an existing message."""
        mock_bot = MagicMock(spec=Bot)
        mock_bot.edit_message_text = AsyncMock()
        event_bus = EventBus()
        rate_limiter = RateLimiter()

        dispatcher = MessageDispatcher(mock_bot, 12345, event_bus, rate_limiter)
        await dispatcher.send_or_edit_message(text="Edited message", message_id=999)

        mock_bot.edit_message_text.assert_called_once()
        call_args = mock_bot.edit_message_text.call_args
        assert call_args[1]["chat_id"] == 12345
        assert call_args[1]["message_id"] == 999
        assert call_args[1]["text"] == "Edited message"

    @pytest.mark.asyncio
    async def test_publish_request_event(self):
        """Test publishing request events."""
        mock_bot = MagicMock(spec=Bot)
        event_bus = EventBus()
        await event_bus.start()  # Start event bus for processing
        rate_limiter = RateLimiter()

        dispatcher = MessageDispatcher(mock_bot, 12345, event_bus, rate_limiter)

        # Track published events
        published = []
        event_bus.subscribe(
            lambda event: published.append(event),
            [EventTopic.TELEGRAM_REQUEST_HEALTH_DATA],
        )

        await dispatcher.publish_request_event(
            EventTopic.TELEGRAM_REQUEST_HEALTH_DATA,
            "health",
            message_id=999,
        )

        await asyncio.sleep(0.2)  # Allow async event processing
        await event_bus.stop()

        assert len(published) == 1
        assert published[0].data["action"] == "health"
        assert published[0].data["message_id"] == 999


class TestDashboardController:
    """Test DashboardController."""

    @pytest.mark.asyncio
    async def test_init(self):
        """Test dashboard controller initialization."""
        event_bus = EventBus()
        dashboard = DashboardController(event_bus)

        assert not dashboard.has_active_dashboard()
        assert dashboard._message_id is None

    @pytest.mark.asyncio
    async def test_set_message_id(self):
        """Test setting message ID."""
        event_bus = EventBus()
        dashboard = DashboardController(event_bus)

        dashboard.set_message_id(12345)
        assert dashboard.has_active_dashboard()
        assert dashboard._message_id == 12345

    @pytest.mark.asyncio
    async def test_clear_message_id(self):
        """Test clearing message ID."""
        event_bus = EventBus()
        dashboard = DashboardController(event_bus)

        dashboard.set_message_id(12345)
        assert dashboard.has_active_dashboard()

        dashboard.clear_message_id()
        assert not dashboard.has_active_dashboard()
        assert dashboard._message_id is None

    @pytest.mark.asyncio
    async def test_auto_update_loop(self):
        """Test auto-update background loop."""
        event_bus = EventBus()
        await event_bus.start()  # Start event bus for processing
        dashboard = DashboardController(event_bus)
        dashboard._min_update_interval = 1  # Reduce for testing

        # Track published events
        published = []
        event_bus.subscribe(
            lambda event: published.append(event),
            [EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA],
        )

        # Set message ID and start auto-update
        dashboard.set_message_id(12345)
        await dashboard.start_auto_update()

        # Wait for at least one update
        await asyncio.sleep(2.5)

        # Stop auto-update
        await dashboard.stop_auto_update()
        await event_bus.stop()

        # Should have published at least one event
        assert len(published) >= 1
        assert published[0].data["action"] == "dashboard"

    @pytest.mark.asyncio
    async def test_mark_updated(self):
        """Test marking dashboard as updated."""
        event_bus = EventBus()
        dashboard = DashboardController(event_bus)

        before = datetime.now(timezone.utc)
        dashboard.mark_updated()
        after = datetime.now(timezone.utc)

        assert dashboard._last_update is not None
        assert before <= dashboard._last_update <= after
