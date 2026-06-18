"""
Unit tests for the Telegram Bot Clean & Reset Bot feature.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update

from core.container import container
from core.event_bus import Event
from core.events.topics import EventTopic
from interfaces.telegram.telegram_bot import TelegramBot
from interfaces.telegram.keyboards import TelegramKeyboards


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object"""
    update = AsyncMock(spec=Update)
    update.effective_chat.id = 123456789
    update.effective_user.id = 123456789
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.message_id = 99
    return update


@pytest.fixture
def telegram_bot(event_bus):
    """Create TelegramBot instance with mocked settings"""
    with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_chat_id = "123456789"
        mock_settings.telegram_bot_token = "test_token"
        mock_settings.telegram_admin_ids = ["123456789"]
        bot = TelegramBot(event_bus)
        return bot


@pytest.mark.asyncio
async def test_control_menu_contains_clean_bot():
    """Test control menu keyboard has Clean & Reset Bot button side-by-side with Reset Signals."""
    keyboard = TelegramKeyboards.get_control_menu()
    buttons = []
    for row in keyboard.inline_keyboard:
        for btn in row:
            buttons.append(btn.callback_data)
    assert "control:clean_bot" in buttons
    assert "control:reset_signals" in buttons


@pytest.mark.asyncio
async def test_handle_clean_bot_control_callback(telegram_bot, mock_update):
    """Test that clicking Clean & Reset Bot shows a confirmation dialog."""
    query = mock_update.callback_query

    await telegram_bot._handle_control_callback(query, "clean_bot")

    query.edit_message_text.assert_called_once()
    args, kwargs = query.edit_message_text.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "⚠️ <b>XÁC NHẬN RESET TOÀN DIỆN?</b>" in text

    reply_markup = kwargs.get("reply_markup") or (args[2] if len(args) > 2 else None)
    assert reply_markup is not None
    assert "clean_bot" in str(reply_markup)


@pytest.mark.asyncio
async def test_handle_clean_bot_confirm_publishes_event(telegram_bot, mock_update):
    """Confirming clean bot publishes CONTROL_CLEAN_BOT (backend handles reset)."""
    query = mock_update.callback_query

    with patch.object(telegram_bot.event_bus, "publish", AsyncMock()) as mock_pub:
        await telegram_bot._handle_confirm_callback(query, "clean_bot")

    mock_pub.assert_called_once()
    event = mock_pub.call_args[0][0]
    assert event.event_type == EventTopic.CONTROL_CLEAN_BOT
    assert event.data["message_id"] == 99

    query.edit_message_text.assert_called_once()
    args, kwargs = query.edit_message_text.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Đang reset toàn diện" in text


@pytest.mark.asyncio
async def test_position_engine_clean_bot_blocked_on_open_positions():
    """Backend blocks clean_bot when open positions exist."""
    from services.position_engine import PositionEngine

    mock_exchange = MagicMock()
    mock_bus = AsyncMock()
    mock_settings = MagicMock()
    mock_settings.default_leverage = 10
    mock_settings.cb_threshold = 9999
    mock_settings.cb_cooldown_seconds = 0

    engine = PositionEngine(
        exchange=mock_exchange,
        event_bus=mock_bus,
        session_factory=MagicMock(),
        settings=mock_settings,
    )

    mock_pos = MagicMock()
    mock_pos.symbol = "BTC-USDT-SWAP"
    engine.order_handler.get_active_positions = MagicMock(return_value=[mock_pos])

    await engine._handle_clean_bot(
        Event(
            event_type=EventTopic.CONTROL_CLEAN_BOT,
            data={"message_id": 42},
            source="test",
        )
    )

    mock_bus.publish.assert_called_once()
    event = mock_bus.publish.call_args[0][0]
    assert event.event_type == EventTopic.CONTROL_CLEAN_BOT_COMPLETE
    assert event.data["blocked"] is True
    assert "BTC-USDT-SWAP" in event.data["symbols"]
