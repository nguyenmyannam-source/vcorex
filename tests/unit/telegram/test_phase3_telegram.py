"""Phase 3 Telegram UI wiring tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.events.topics import EventTopic
from interfaces.telegram.keyboards import TelegramKeyboards


def test_open_positions_keyboard_includes_close_buttons():
    positions = [
        {"position_id": "pos_abc123", "symbol": "BTC-USDT-SWAP"},
        {"position_id": None, "symbol": "ETH-USDT-SWAP"},
    ]
    markup = TelegramKeyboards.get_open_positions_keyboard(positions)
    flat = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert not any(cb.startswith("pcl:") for cb in flat)
    assert not any(cb.startswith("pcf:") for cb in flat)
    assert "trading:open_positions" in flat
    assert all(len(cb.encode("utf-8")) <= 64 for cb in flat)

    long_id = "pos_ghost_ws_" + "a" * 36
    markup_long = TelegramKeyboards.get_open_positions_keyboard(
        [{"position_id": long_id, "symbol": "BTC-USDT-SWAP"}]
    )
    long_flat = [btn.callback_data for row in markup_long.inline_keyboard for btn in row]
    assert not any(cb.startswith("pcl:") for cb in long_flat)
    assert all(len(cb.encode("utf-8")) <= 64 for cb in long_flat)


def test_event_topic_emergency_stop_complete_exists():
    assert hasattr(EventTopic, "CONTROL_EMERGENCY_STOP_COMPLETE")
    assert EventTopic.CONTROL_EMERGENCY_STOP_COMPLETE.value == "control.emergency_stop_complete"


@pytest.mark.asyncio
async def test_trading_handler_skips_active_signals():
    from services.position.telegram_handler import PositionTelegramHandler

    handler = PositionTelegramHandler(MagicMock(), MagicMock())
    handler.engine = MagicMock()
    handler.event_bus = MagicMock()
    handler.event_bus.publish = AsyncMock()

    event = SimpleNamespace(
        data={"action": "active_signals", "message_id": 99},
    )
    await handler._handle_telegram_trading_request(event)
    handler.event_bus.publish.assert_not_awaited()


def test_format_pending_orders_with_data():
    from interfaces.telegram.message_templates import MessageTemplates

    text = MessageTemplates.format_pending_orders(
        [{"symbol": "BTC-USDT-SWAP", "side": "buy", "amount": 1.0, "price": 50000.0, "type": "limit"}]
    )
    assert "BTC-USDT-SWAP" in text
    assert "50,000" in text
