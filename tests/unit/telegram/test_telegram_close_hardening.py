"""
Unit and integration tests for Telegram Control Layer Hardening:
- CallbackTokenStore lifecycle, expiration, and cleanup
- UI Layer Lock (Layer 1) behavior and concurrency safety
- Circuit Breaker states (CLOSED, OPEN, HALF_OPEN) and transitions
- Real-time exchange position verification and size normalization
"""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.ext import ContextTypes

from core.config.settings import Settings, settings
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.events.payloads import PositionCloseRequest, PositionAction
from interfaces.telegram.telegram_bot import TelegramBot, CallbackTokenStore
from services.position_engine import PositionEngine, CircuitState, PositionStatus
from infrastructure.exchange.base_exchange import BaseExchange, Position


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object."""
    update = AsyncMock(spec=Update)
    update.effective_chat.id = 123456789
    update.effective_user.id = 123456789
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = ""
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock ContextTypes object."""
    context = AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    return context


@pytest.fixture
def telegram_bot(event_bus):
    """Create a TelegramBot instance configured for tests."""
    settings.telegram_chat_id = "123456789"
    settings.telegram_bot_token = "test_token"
    settings.telegram_admin_ids = ["123456789"]
    settings.telegram_enabled = True
    bot = TelegramBot(event_bus)
    return bot


# =========================================================================
# 1. CallbackTokenStore Tests
# =========================================================================
@pytest.mark.asyncio
async def test_callback_token_store_lifecycle():
    """Test generating, retrieving, and consuming callback tokens."""
    token = CallbackTokenStore.generate("pos_test_123", PositionAction.CLOSE_FULL)
    assert len(token) <= 16

    # Verify retrieval
    meta = CallbackTokenStore.get(token)
    assert meta is not None
    assert meta["position_id"] == "pos_test_123"
    assert meta["action"] == PositionAction.CLOSE_FULL

    # Verify consumption
    consumed = CallbackTokenStore.consume(token)
    assert consumed is not None
    assert consumed["position_id"] == "pos_test_123"

    # Verify single-use (subsequent retrieve is None)
    assert CallbackTokenStore.get(token) is None
    assert CallbackTokenStore.consume(token) is None


@pytest.mark.asyncio
async def test_callback_token_store_expiration():
    """Test that expired tokens are not returned and are cleaned up."""
    token = CallbackTokenStore.generate("pos_test_123", PositionAction.CLOSE_HALF)

    # Force expiration
    CallbackTokenStore._store[token]["expires_at"] = time.time() - 10

    # Expired token must return None
    assert CallbackTokenStore.get(token) is None
    assert CallbackTokenStore.consume(token) is None

    # Run cleanup manually
    CallbackTokenStore.cleanup()
    assert token not in CallbackTokenStore._store


# =========================================================================
# 2. UI Layer 1 Locking Tests
# =========================================================================
@pytest.mark.asyncio
async def test_telegram_bot_layer1_lock(telegram_bot, mock_update, mock_context):
    """Test that concurrent requests for the same position are locked/rejected."""
    telegram_bot._enabled = True
    telegram_bot._bot = mock_context.bot

    token1 = CallbackTokenStore.generate("pos_locked", PositionAction.CLOSE_FULL)
    token2 = CallbackTokenStore.generate("pos_locked", PositionAction.CLOSE_FULL)

    # First request starts
    mock_update.callback_query.data = f"confirm:{token1}"

    # We mock event bus publish to block/simulate latency
    event_loop = asyncio.get_running_loop()
    future = event_loop.create_future()

    async def mock_publish(event):
        await future

    with patch.object(telegram_bot.event_bus, "publish", mock_publish):
        task1 = asyncio.create_task(telegram_bot._handle_callback(mock_update, mock_context))
        await asyncio.sleep(0.05) # Give task1 time to run and acquire lock

        # Try to trigger second callback while first is running
        mock_update2 = AsyncMock(spec=Update)
        mock_update2.effective_chat.id = 123456789
        mock_update2.effective_user.id = 123456789
        mock_update2.effective_user.username = "admin"
        mock_update2.callback_query = AsyncMock()
        mock_update2.callback_query.data = f"confirm:{token2}"

        await telegram_bot._handle_callback(mock_update2, mock_context)

        # Second request should have been rejected immediately
        mock_update2.callback_query.answer.assert_called_with(
            text="⚠️ Một yêu cầu đóng vị thế khác đang được xử lý. Vui lòng đợi.",
            show_alert=True
        )

        # Complete first task
        future.set_result({"success": True, "size": 1.0})
        await task1


# =========================================================================
# 3. Circuit Breaker Tests
# =========================================================================
@pytest.mark.asyncio
async def test_position_engine_circuit_breaker(mock_exchange, mock_session_factory, test_settings_cb, event_bus):
    """Test Circuit Breaker state transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings_cb)
    engine._cb_cooldown = 0.1 # short cooldown for testing

    assert engine._cb_state == CircuitState.CLOSED
    assert engine._cb_can_execute() is True

    # Trip the circuit (3 consecutive failures)
    engine._cb_record_failure()
    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.CLOSED

    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.OPEN
    assert engine._cb_can_execute() is False

    # Wait for cooldown to transition to HALF_OPEN
    await asyncio.sleep(0.15)
    assert engine._cb_can_execute() is True
    assert engine._cb_state == CircuitState.HALF_OPEN

    # Failure in HALF_OPEN trips it back to OPEN immediately
    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.OPEN

    # Wait for cooldown again
    await asyncio.sleep(0.15)
    assert engine._cb_can_execute() is True
    assert engine._cb_state == CircuitState.HALF_OPEN

    # Success in HALF_OPEN resets state to CLOSED
    engine._cb_record_success()
    assert engine._cb_state == CircuitState.CLOSED
    assert engine._cb_failure_count == 0


# =========================================================================
# 4. Real-time Verification & Normalization Tests
# =========================================================================
@pytest.mark.asyncio
async def test_close_position_secure_normalization(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Test that close_half normalizes target size using exchange lot specification."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Setup mock exchange positions and specs
    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_pos_1",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=1.5, # 1.5 contracts
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=int(time.time() * 1000)
    ))

    # lot size is 1.0 (so 1.5 / 2 = 0.75 -> normalized to 0.0, which should reject or return error)
    mock_exchange._markets = {
        "BTC-USDT-SWAP": {"ctVal": 1.0, "tickSz": 0.1, "lotSz": 1.0, "minSz": 1.0}
    }

    mock_exchange.normalize_position_size = MagicMock(return_value=0.0)

    # Setup local position
    local_pos = MagicMock()
    local_pos.symbol = "BTC-USDT-SWAP"
    local_pos.side = "long"
    local_pos.status = PositionStatus.OPENED
    engine.order_handler._positions["pos_123"] = local_pos

    # Test CLOSE_HALF that results in below min lot size (1.5 / 2 = 0.75 -> 0.0)
    request = PositionCloseRequest(
        request_id="req_1",
        correlation_id="corr_1",
        causation_id="cause_1",
        position_id="pos_123",
        action=PositionAction.CLOSE_HALF,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_failure(event):
        future.set_result(event.data)

    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        result = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert result["success"] is False
    assert "less than minimum lot size" in result["reason"]
