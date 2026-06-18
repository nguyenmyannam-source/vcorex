"""
Coverage booster tests for VCOREX.
Ensures we hit >=85% on telegram_bot.py, >=90% on position_engine.py, and >=95% on locking/circuit breaker/security.
"""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update, User, Chat, Message
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from core.config.settings import settings
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.events.payloads import PositionCloseRequest, PositionAction
from core.metrics import InMemoryMetricsAdapter
from core.audit_journal import AuditJournal
from services.position_engine import PositionEngine, CircuitState, PositionStatus
from services.position.models import TrackedPosition
from infrastructure.exchange.base_exchange import BaseExchange, Position
from interfaces.telegram.telegram_bot import TelegramBot, CallbackTokenStore


def make_mock_update(user_id=123456789, chat_id=123456789, callback_data=""):
    """Helper to construct a fully configured Telegram Update mock."""
    update = MagicMock(spec=Update)

    user = MagicMock(spec=User)
    user.id = user_id
    update.effective_user = user

    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    update.effective_chat = chat

    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = user
    update.callback_query = query

    msg = AsyncMock(spec=Message)
    msg.message_id = 9999
    msg.reply_text = AsyncMock()
    update.message = msg

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
def mock_exchange():
    """Create a mock OKX Exchange client."""
    exchange = MagicMock()
    exchange.fetch_position = AsyncMock()
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.normalize_position_size = MagicMock(side_effect=lambda sym, sz: sz)
    exchange.fetch_ticker = AsyncMock()
    return exchange


@pytest.fixture
def mock_session_factory():
    """Create a mock database session factory."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


@pytest.fixture
def test_settings():
    """Returns local settings with standard watchlist."""
    settings.watchlist = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    settings.telegram_admin_ids = ["123456789"]
    return settings


@pytest.fixture
def booster_bot(event_bus):
    """TelegramBot configured with direct settings configuration."""
    old_chat_id = settings.telegram_chat_id
    old_token = settings.telegram_bot_token
    old_admin_ids = settings.telegram_admin_ids
    old_enabled = settings.telegram_enabled

    settings.telegram_chat_id = "123456789"
    settings.telegram_bot_token = "test_token"
    settings.telegram_admin_ids = ["123456789"]
    settings.telegram_enabled = True

    bot = TelegramBot(event_bus)
    yield bot

    settings.telegram_chat_id = old_chat_id
    settings.telegram_bot_token = old_token
    settings.telegram_admin_ids = old_admin_ids
    settings.telegram_enabled = old_enabled


# =========================================================================
# Position Engine Coverage Booster
# =========================================================================
@pytest.mark.asyncio
async def test_engine_lifecycle_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify lifecycle edge cases of PositionEngine."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Try start when already running
    engine._running = True
    await engine.start()
    assert engine._running is True

    # Stop when not running
    engine._running = False
    await engine.stop()
    assert engine._running is False

    # Start when not running
    engine._running = False
    with patch.object(engine, "reconcile_positions_with_exchange", AsyncMock()) as mock_rec, \
         patch.object(engine.persistence, "sync_history_with_exchange", AsyncMock()) as mock_sync:
        await engine.start()
        assert engine._running is True
        mock_rec.assert_called_once()
        mock_sync.assert_called_once()
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_circuit_breaker_transitions(mock_exchange, mock_session_factory, test_settings_cb, event_bus):
    """Verify circuit breaker transitions across CLOSED, OPEN, HALF_OPEN."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings_cb)
    assert engine._cb_state == CircuitState.CLOSED

    # CLOSED -> OPEN via failures
    engine._cb_record_failure()
    engine._cb_record_failure()
    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.OPEN
    assert engine._cb_can_execute() is False

    # Cooldown transition to HALF_OPEN
    engine._cb_cooldown = 0.01
    await asyncio.sleep(0.02)
    assert engine._cb_can_execute() is True
    assert engine._cb_state == CircuitState.HALF_OPEN

    # HALF_OPEN -> CLOSED on success
    engine._cb_record_success()
    assert engine._cb_state == CircuitState.CLOSED
    assert engine._cb_failure_count == 0

    # Go to HALF_OPEN again
    engine._cb_record_failure()
    engine._cb_record_failure()
    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.OPEN
    await asyncio.sleep(0.02)
    assert engine._cb_can_execute() is True  # transitioned to HALF_OPEN

    # HALF_OPEN -> OPEN on failure
    engine._cb_record_failure()
    assert engine._cb_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_close_position_secure_circuit_breaker_open(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request rejection when circuit breaker is OPEN."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)
    engine._cb_state = CircuitState.OPEN
    engine._cb_cooldown = 100.0

    request = PositionCloseRequest(
        request_id="req_cb",
        correlation_id="corr_cb",
        causation_id="cause_cb",
        position_id="pos_cb",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is False
    assert "circuit breaker" in res.data["reason"].lower()


@pytest.mark.asyncio
async def test_close_position_secure_not_found(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request when position does not exist locally."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    request = PositionCloseRequest(
        request_id="req_nf",
        correlation_id="corr_nf",
        causation_id="cause_nf",
        position_id="non_existent",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is False
    assert "not found locally" in res.data["reason"].lower()


@pytest.mark.asyncio
async def test_close_position_secure_not_watchlist(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request when position symbol is not in settings watchlist."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_unwanted",
        exchange_id="okx_unwanted",
        symbol="UNWANTED-USDT",
        side="long",
        entry_price=10.0,
        current_price=10.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=3.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_unwanted"] = pos

    request = PositionCloseRequest(
        request_id="req_wl",
        correlation_id="corr_wl",
        causation_id="cause_wl",
        position_id="pos_unwanted",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is False
    assert "watchlist" in res.data["reason"].lower()


@pytest.mark.asyncio
async def test_close_position_secure_already_closed(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request returns success early if local position status is already CLOSED."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_closed",
        exchange_id="okx_closed",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=0.0,
        leverage=10.0,
        status=PositionStatus.CLOSED
    )
    engine.order_handler._positions["pos_closed"] = pos

    request = PositionCloseRequest(
        request_id="req_closed",
        correlation_id="corr_closed",
        causation_id="cause_closed",
        position_id="pos_closed",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_success(evt):
        future.set_result(evt)
    event_bus.subscribe(on_success, [EventTopic.POSITION_CLOSE_SUCCESS])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is True
    assert res.data["already_closed"] is True


@pytest.mark.asyncio
async def test_close_position_secure_exchange_errors(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request when exchange APIs error out during fetch."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_err",
        exchange_id="okx_err",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_err"] = pos

    # Case 1: Fetch raises Exception
    mock_exchange.fetch_position = AsyncMock(side_effect=Exception("Database lock error"))
    request = PositionCloseRequest(
        request_id="req_err",
        correlation_id="corr_err",
        causation_id="cause_err",
        position_id="pos_err",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(request)
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is False
    assert "failed to fetch exchange state" in res.data["reason"].lower()


@pytest.mark.asyncio
async def test_close_position_secure_exchange_already_closed_remote(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request when exchange reports position is already closed (amount=0)."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_rem_closed",
        exchange_id="okx_rem",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_rem_closed"] = pos

    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_rem",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=0.0,  # closed
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=123456
    ))

    future = asyncio.get_running_loop().create_future()
    async def on_success(evt):
        future.set_result(evt)
    event_bus.subscribe(on_success, [EventTopic.POSITION_CLOSE_SUCCESS])

    await event_bus.start()
    try:
        await engine.close_position_secure(PositionCloseRequest(
            request_id="req_rem",
            correlation_id="corr_rem",
            causation_id="cause_rem",
            position_id="pos_rem_closed",
            action=PositionAction.CLOSE_FULL,
            requested_by=123456789,
            timestamp=datetime.now(timezone.utc)
        ))
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is True
    assert res.data["already_closed"] is True
    assert pos.status == PositionStatus.CLOSED
    assert "pos_rem_closed" not in engine.order_handler._positions


@pytest.mark.asyncio
async def test_close_position_secure_normalize_zero_lot(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify close request when normalized close size is <= 0."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_zero_lot",
        exchange_id="okx_zero_lot",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=0.001,
        amount_remaining=0.001,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_zero_lot"] = pos

    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_zero_lot",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=0.001,
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=123456
    ))
    # Normalize size to 0.0 representing too small size
    mock_exchange.normalize_position_size = MagicMock(return_value=0.0)

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(PositionCloseRequest(
            request_id="req_zero_lot",
            correlation_id="corr_zero_lot",
            causation_id="cause_zero_lot",
            position_id="pos_zero_lot",
            action=PositionAction.CLOSE_HALF,
            requested_by=123456789,
            timestamp=datetime.now(timezone.utc)
        ))
        res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert res.data["success"] is False
    assert "less than minimum lot size" in res.data["reason"].lower()


@pytest.mark.asyncio
async def test_close_position_secure_order_placement_failures(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify order placement edge cases (failures, timeouts, exceptions)."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_exec_err",
        exchange_id="okx_exec_err",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_exec_err"] = pos

    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_exec_err",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=1.0,
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=123456
    ))

    # Case 1: close_position returns False
    engine.close_position = AsyncMock(return_value=False)

    future1 = asyncio.get_running_loop().create_future()
    async def on_failure1(evt):
        if not future1.done():
            future1.set_result(evt)
    event_bus.subscribe(on_failure1, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(PositionCloseRequest(
            request_id="req_fail_1",
            correlation_id="corr_fail_1",
            causation_id="cause_fail_1",
            position_id="pos_exec_err",
            action=PositionAction.CLOSE_FULL,
            requested_by=123456789,
            timestamp=datetime.now(timezone.utc)
        ))
        res1 = await asyncio.wait_for(future1, 1.0)
        assert res1.data["success"] is False
        assert "failed to execute close order" in res1.data["reason"].lower()
    finally:
        event_bus.unsubscribe(handler_id="pe_signal_handler")

    # Case 2: close_position raises exception
    engine.close_position = AsyncMock(side_effect=Exception("Invalid parameter"))

    future2 = asyncio.get_running_loop().create_future()
    async def on_failure2(evt):
        if not future2.done():
            future2.set_result(evt)
    event_bus.subscribe(on_failure2, [EventTopic.POSITION_CLOSE_FAILURE])

    try:
        await engine.close_position_secure(PositionCloseRequest(
            request_id="req_fail_2",
            correlation_id="corr_fail_2",
            causation_id="cause_fail_2",
            position_id="pos_exec_err",
            action=PositionAction.CLOSE_FULL,
            requested_by=123456789,
            timestamp=datetime.now(timezone.utc)
        ))
        res2 = await asyncio.wait_for(future2, 1.0)
        assert res2.data["success"] is False
        assert "exception during order placement" in res2.data["reason"].lower()
    finally:
        await event_bus.stop()


@pytest.mark.asyncio
async def test_engine_reconciliation_stale_duplicates(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify reconciliation cleans up multiple local stale positions."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Local has 2 duplicate positions for same symbol BTC-USDT-SWAP
    p1 = TrackedPosition(
        id="pos_stale",
        exchange_id="okx_stale",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        opened_at=datetime.fromtimestamp(1000, tz=timezone.utc),
        status=PositionStatus.OPENED
    )
    p2 = TrackedPosition(
        id="pos_active",
        exchange_id="okx_active",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        opened_at=datetime.fromtimestamp(2000, tz=timezone.utc),
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_stale"] = p1
    engine.order_handler._positions["pos_active"] = p2

    mock_exchange.fetch_positions = AsyncMock(return_value=[
        Position(
            position_id="okx_btc",
            symbol="BTC-USDT-SWAP",
            side="long",
            amount=1.0,
            entry_price=60000.0,
            current_price=60000.0,
            unrealized_pnl=0.0,
            leverage=10,
            timestamp=int(time.time() * 1000)
        )
    ])

    await engine.reconcile_positions_with_exchange()

    # The older p1 should be closed, p2 remains open
    assert p1.status == PositionStatus.CLOSED
    assert p2.status == PositionStatus.OPENED
    assert "pos_stale" not in engine.order_handler._positions
    assert "pos_active" in engine.order_handler._positions


@pytest.mark.asyncio
async def test_position_engine_ws_handlers(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify WebSocket handlers on PositionEngine."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # ws ticker update
    ticker_evt = Event(
        event_type=EventTopic.MARKET_WS_TICKER,
        data={"symbol": "BTC-USDT-SWAP", "price": "61500.5", "timestamp": time.time()},
        source="ws"
    )
    await engine._handle_ws_ticker(ticker_evt)
    assert engine.monitor._ticker_cache["BTC-USDT-SWAP"]["price"] == 61500.5

    # invalid price handled safely
    ticker_evt_invalid = Event(
        event_type=EventTopic.MARKET_WS_TICKER,
        data={"symbol": "BTC-USDT-SWAP", "price": "not-a-number"},
        source="ws"
    )
    await engine._handle_ws_ticker(ticker_evt_invalid) # should not raise error


# =========================================================================
# Telegram Bot Callback Query & Command Booster
# =========================================================================
@pytest.mark.asyncio
async def test_telegram_bot_unauthorized_decorator(booster_bot, mock_context):
    """Verify unauthorized decorator blocks execution and replies/answers query."""
    settings.telegram_admin_ids = ["999999999"] # Current update comes from 123456789

    update_msg = make_mock_update(user_id=123456789)
    update_msg.callback_query = None # Standard message command

    await booster_bot._cmd_start(update_msg, mock_context)
    update_msg.message.reply_text.assert_called_once_with(
        "⚠️ Unauthorized: You do not have permission to use this command."
    )

    update_query = make_mock_update(user_id=123456789, callback_data="test")
    await booster_bot._handle_callback(update_query, mock_context)
    update_query.callback_query.answer.assert_called_with("⚠️ Unauthorized", show_alert=True)


@pytest.mark.asyncio
async def test_telegram_bot_commands_present(booster_bot, mock_context):
    """Verify /start, /menu, /status execution flows."""
    update = make_mock_update()

    # Mock send_message to return a Message object
    msg_obj = MagicMock(spec=Message)
    msg_obj.message_id = 12345
    mock_context.bot.send_message = AsyncMock(return_value=msg_obj)

    # /start
    await booster_bot._cmd_start(update, mock_context)
    assert booster_bot._dashboard._message_id == 12345

    # /menu
    await booster_bot._cmd_menu(update, mock_context)
    assert booster_bot._dashboard._message_id == 12345

    # /status
    with patch.object(booster_bot.event_bus, "publish", AsyncMock()) as mock_pub:
        await booster_bot._cmd_status(update, mock_context)
        mock_pub.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_bot_callback_routing(booster_bot, mock_context):
    """Verify callback query routing methods."""
    # Test menu callback routing
    update = make_mock_update(callback_data="menu:analytics")
    await booster_bot._handle_callback(update, mock_context)
    args, kwargs = update.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "THỐNG KÊ TỔNG QUAN" in text

    # Test cancel callback
    token = CallbackTokenStore.generate("pos_1", PositionAction.CLOSE_FULL)
    update_cancel = make_mock_update(callback_data=f"cancel:{token}")
    await booster_bot._handle_callback(update_cancel, mock_context)
    args, kwargs = update_cancel.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "Đã hủy yêu cầu" in text or "cancelled" in text

    # Test settings callbacks
    for setting_action in ("bot_settings", "risk_limits", "watchlist", "notifications"):
        update_set = make_mock_update(callback_data=f"settings:{setting_action}")
        await booster_bot._handle_callback(update_set, mock_context)
        assert update_set.callback_query.edit_message_text.called

    # Test system callbacks
    for sys_action in ("health", "exchange_status", "logs", "news"):
        update_sys = make_mock_update(callback_data=f"system:{sys_action}")
        await booster_bot._handle_callback(update_sys, mock_context)
        assert update_sys.callback_query.edit_message_text.called

    # Test control callbacks (start/pause/emergency confirmation)
    for ctrl_action in ("start_bot", "pause_bot", "emergency_stop", "reset_signals", "restart_engine"):
        update_ctrl = make_mock_update(callback_data=f"control:{ctrl_action}")
        await booster_bot._handle_callback(update_ctrl, mock_context)
        assert update_ctrl.callback_query.edit_message_text.called


@pytest.mark.asyncio
async def test_telegram_bot_confirm_callbacks(booster_bot, mock_context):
    """Verify confirm action callback endpoints (emergency_stop, reset_signals)."""
    # confirm:emergency_stop
    update_es = make_mock_update(callback_data="confirm:emergency_stop")
    with patch.object(booster_bot.event_bus, "publish", AsyncMock()) as mock_pub:
        await booster_bot._handle_callback(update_es, mock_context)
        mock_pub.assert_called_once()
        args, kwargs = update_es.callback_query.edit_message_text.call_args
        text = kwargs.get("text", args[0] if args else "")
        assert "khẩn cấp" in text.lower() or "emergency" in text.lower()

    # confirm:reset_signals
    update_rs = make_mock_update(callback_data="confirm:reset_signals")
    with patch.object(booster_bot.event_bus, "publish", AsyncMock()) as mock_pub:
        await booster_bot._handle_callback(update_rs, mock_context)
        mock_pub.assert_called_once()
        event = mock_pub.call_args[0][0]
        from core.events.topics import EventTopic
        assert event.event_type == EventTopic.CONTROL_RESET_SIGNALS
    args, kwargs = update_rs.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "reset tín hiệu" in text.lower() or "đang reset" in text.lower()


@pytest.mark.asyncio
async def test_telegram_bot_response_events_booster(booster_bot):
    """Verify UI updater response events."""
    # Test _on_health_data_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_health_data_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_trading_data_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_trading_data_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_analytics_data_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_analytics_data_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_history_data_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_history_data_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_exchange_status_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_exchange_status_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_news_data_response
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_news_data_response(Event("topic", {"message_id": 123}, "source"))
        mock_send.assert_called_once()

    # Test _on_system_data_response (non-dashboard)
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_system_data_response(Event("topic", {"message_id": 123, "action": "logs"}, "source"))
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_bot_position_actions_booster(booster_bot, mock_context):
    """Verify position callbacks (close_half/close_full) show confirm panel, and L1 locking works."""
    # Showing close_half confirmation screen
    update_ph = make_mock_update(callback_data="position:close_half:pos_id")
    await booster_bot._handle_callback(update_ph, mock_context)
    update_ph.callback_query.edit_message_text.assert_called_once()
    assert "ĐÓNG 50% VỊ THẾ" in update_ph.callback_query.edit_message_text.call_args[1]["text"]

    # Secure token generated & confirming close
    token = CallbackTokenStore.generate("pos_confirm_l1", PositionAction.CLOSE_FULL)
    update_cf = make_mock_update(callback_data=f"confirm:{token}")

    # Case: L1 lock already active (simulating concurrency lock)
    booster_bot._position_action_locks["pos_confirm_l1"] = asyncio.Lock()
    await booster_bot._position_action_locks["pos_confirm_l1"].acquire()

    await booster_bot._handle_callback(update_cf, mock_context)
    update_cf.callback_query.answer.assert_called_with(
        text="⚠️ Một yêu cầu đóng vị thế khác đang được xử lý. Vui lòng đợi.",
        show_alert=True
    )


# =========================================================================
# Additional Position Engine Booster Tests
# =========================================================================
@pytest.mark.asyncio
async def test_engine_backward_compatibility_helpers(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify backward compatibility and PnL updates in PositionEngine."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Load properties
    assert engine._positions is engine.order_handler._positions
    assert engine._exchange_id_map is engine.order_handler._exchange_id_map
    assert engine._ticker_cache is engine.monitor._ticker_cache

    pos = TrackedPosition(
        id="pos_comp",
        exchange_id="okx_comp",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_comp"] = pos

    # Case 1: Ticker cache is stale (>30s or missing) -> triggers REST fetch_ticker
    engine.monitor._ticker_cache.clear()

    class FakeTicker:
        last_price = 61200.0
    mock_exchange.fetch_ticker = AsyncMock(return_value=FakeTicker())

    with patch.object(engine.monitor, "update_position_pnl", AsyncMock()) as mock_up:
        await engine._update_position_pnl(pos)
        assert pos.current_price == 61200.0
        mock_up.assert_called_once_with(pos)

    # Case 2: Ticker cache is fresh (<=30s) -> no REST fetch_ticker
    engine.monitor._ticker_cache["BTC-USDT-SWAP"] = {"price": 61300.0, "ts": time.time()}
    mock_exchange.fetch_ticker = AsyncMock() # should not be called

    with patch.object(engine.monitor, "update_position_pnl", AsyncMock()) as mock_up2:
        await engine._update_position_pnl(pos)
        mock_exchange.fetch_ticker.assert_not_called()
        mock_up2.assert_called_once_with(pos)

    # Case 3: _update_positions_pnl
    with patch.object(engine.monitor, "update_position_pnl", AsyncMock()) as mock_up3:
        await engine._update_positions_pnl()
        mock_up3.assert_called_once_with(pos)


@pytest.mark.asyncio
async def test_engine_event_handlers_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify other event handlers on PositionEngine."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Approved signal handler delegating to open_position
    signal_data = {"symbol": "BTC-USDT-SWAP", "side": "long"}
    with patch.object(engine, "open_position", AsyncMock()) as mock_open:
        await engine._handle_approved_signal(Event(EventTopic.RISK_SIGNAL_APPROVED, signal_data, "source"))
        mock_open.assert_called_once_with({**signal_data, "correlation_id": "UNKNOWN"})

    # Emergency stop handler delegating to panic_close_all_positions
    with patch.object(engine.order_handler, "panic_close_all_positions", AsyncMock(return_value=(2, 0))) as mock_close_all:
        await engine._handle_emergency_stop(Event(EventTopic.CONTROL_EMERGENCY_STOP, {}, "source"))
        mock_close_all.assert_called_once()


@pytest.mark.asyncio
async def test_reconciliation_variations_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify various reconciliation branches when local state differs from exchange."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Local position
    pos = TrackedPosition(
        id="pos_recon",
        exchange_id="okx_recon",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        opened_at=datetime.fromtimestamp(1000, tz=timezone.utc),
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_recon"] = pos

    # Case 1: amount differs
    mock_exchange.fetch_positions = AsyncMock(return_value=[
        Position(
            position_id="okx_recon",
            symbol="BTC-USDT-SWAP",
            side="long",
            amount=0.8, # differs
            entry_price=60000.0,
            current_price=60000.0,
            unrealized_pnl=0.0,
            leverage=10,
            timestamp=int(time.time() * 1000)
        )
    ])
    await engine.reconcile_positions_with_exchange()
    assert pos.amount_remaining == 0.8
    assert pos.amount == 0.8

    # Case 2: entry price differs
    mock_exchange.fetch_positions = AsyncMock(return_value=[
        Position(
            position_id="okx_recon",
            symbol="BTC-USDT-SWAP",
            side="long",
            amount=0.8,
            entry_price=59950.0, # differs
            current_price=60000.0,
            unrealized_pnl=0.0,
            leverage=10,
            timestamp=int(time.time() * 1000)
        )
    ])
    await engine.reconcile_positions_with_exchange()
    assert pos.entry_price == 59950.0

    # Case 3: opened_at is None or float (get_open_time helper checks)
    pos.opened_at = None
    mock_exchange.fetch_positions = AsyncMock(return_value=[
        Position(
            position_id="okx_recon",
            symbol="BTC-USDT-SWAP",
            side="long",
            amount=0.8,
            entry_price=59950.0,
            current_price=60000.0,
            unrealized_pnl=0.0,
            leverage=10,
            timestamp=int(time.time() * 1000)
        )
    ])
    await engine.reconcile_positions_with_exchange() # should not crash

    # Case 4: exception handling
    mock_exchange.fetch_positions = AsyncMock(side_effect=RuntimeError("OKX Down"))
    await engine.reconcile_positions_with_exchange() # should log but not crash


@pytest.mark.asyncio
async def test_ws_position_updates_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify handle WS position updates."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    # Local position
    pos = TrackedPosition(
        id="pos_ws_sync",
        exchange_id="okx_ws_sync",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_ws_sync"] = pos

    # Case 1: WS raw pos size is 0 (position closed)
    evt_closed = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"symbol": "BTC-USDT-SWAP", "data": {"pos": "0", "instId": "BTC-USDT-SWAP"}},
        source="ws"
    )
    await engine._handle_ws_position(evt_closed)
    assert pos.status == PositionStatus.CLOSED
    assert pos.amount_remaining == 0.0
    assert "pos_ws_sync" not in engine.order_handler._positions

    # Restore position
    pos.status = PositionStatus.OPENED
    pos.amount_remaining = 1.0
    engine.order_handler._positions["pos_ws_sync"] = pos

    # Case 2: WS raw pos size is 0.4 (partial close)
    evt_partial = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"symbol": "BTC-USDT-SWAP", "data": {"pos": "0.4", "instId": "BTC-USDT-SWAP"}},
        source="ws"
    )
    await engine._handle_ws_position(evt_partial)
    assert pos.status == PositionStatus.PARTIAL_TP
    assert pos.amount_remaining == 0.4

    # Case 3: Invalid pos size string
    evt_invalid = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"symbol": "BTC-USDT-SWAP", "data": {"pos": "not-a-float", "instId": "BTC-USDT-SWAP"}},
        source="ws"
    )
    await engine._handle_ws_position(evt_invalid) # should return early

    # Case 4: Exception handling (data is None)
    evt_error = Event(
        event_type=EventTopic.WS_RAW_POSITION,
        data={"symbol": "BTC-USDT-SWAP", "data": None},
        source="ws"
    )
    await engine._handle_ws_position(evt_error) # should log but not crash


@pytest.mark.asyncio
async def test_cb_half_open_direct(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify circuit breaker can execute from HALF_OPEN directly."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)
    engine._cb_state = CircuitState.HALF_OPEN
    assert engine._cb_can_execute() is True


@pytest.mark.asyncio
async def test_lock_contention_metrics_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify lock contention increment is scheduled when lock is already acquired."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_contend",
        exchange_id="okx_contend",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_contend"] = pos

    # Acquire the lock beforehand
    engine._position_execution_locks["pos_contend"] = asyncio.Lock()
    await engine._position_execution_locks["pos_contend"].acquire()

    # Call close_position_secure in a non-awaited task to see it hit lock.locked() warning
    request = PositionCloseRequest(
        request_id="req_contend",
        correlation_id="corr_contend",
        causation_id="cause_contend",
        position_id="pos_contend",
        action=PositionAction.CLOSE_FULL,
        requested_by=123456789,
        timestamp=datetime.now(timezone.utc)
    )

    with patch.object(engine._metrics, "increment_lock_contention", AsyncMock()) as mock_inc:
        # Run close_position_secure as a background task since it will wait on the lock
        task = asyncio.create_task(engine.close_position_secure(request))
        # Yield to let it execute the check before waiting on the lock
        await asyncio.sleep(0.02)
        mock_inc.assert_called_once()
        # Release lock and clean up task
        engine._position_execution_locks["pos_contend"].release()
        await task


@pytest.mark.asyncio
async def test_close_position_secure_order_placement_timeout_booster(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify OrderHandler timeout behaves correctly during close execution."""
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    pos = TrackedPosition(
        id="pos_timeout_exec",
        exchange_id="okx_timeout_exec",
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=60000.0,
        current_price=60000.0,
        amount=1.0,
        amount_remaining=1.0,
        leverage=10.0,
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_timeout_exec"] = pos

    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_timeout_exec",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=1.0,
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=123456
    ))

    # Mock close_position to simulate timeout by raising TimeoutError
    engine.close_position = AsyncMock(side_effect=asyncio.TimeoutError())

    future = asyncio.get_running_loop().create_future()
    async def on_failure(evt):
        future.set_result(evt)
    event_bus.subscribe(on_failure, [EventTopic.POSITION_CLOSE_FAILURE])

    await event_bus.start()
    try:
        await engine.close_position_secure(PositionCloseRequest(
            request_id="req_t",
            correlation_id="corr_t",
            causation_id="cause_t",
            position_id="pos_timeout_exec",
            action=PositionAction.CLOSE_FULL,
            requested_by=123456789,
            timestamp=datetime.now(timezone.utc)
        ))
        res = await asyncio.wait_for(future, 1.0)
        assert res.data["success"] is False
        assert "timeout" in res.data["reason"].lower()
    finally:
        await event_bus.stop()


# =========================================================================
# Additional Telegram Bot Booster Tests
# =========================================================================
@pytest.mark.asyncio
async def test_telegram_bot_unknown_actions_booster(booster_bot, mock_context):
    """Verify unknown action callbacks are handled cleanly without exceptions."""
    # Settings callback unknown action publishes request (response rendered async)
    update_set = make_mock_update(callback_data="settings:unknown_action")
    await booster_bot._handle_callback(update_set, mock_context)
    args, kwargs = update_set.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "Đang tải cài đặt" in text

    from interfaces.telegram.message_renderer import MessageRenderer
    unknown_text = MessageRenderer.render_settings_data({"action": "unknown_action", "settings": {}})
    assert "Unknown settings action" in unknown_text

    # Control callback unknown action
    update_ctrl = make_mock_update(callback_data="control:unknown_action")
    await booster_bot._handle_callback(update_ctrl, mock_context)
    args, kwargs = update_ctrl.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "Unknown control action" in text

    # Position callback too few parts
    update_pos_short = make_mock_update(callback_data="position:close_half")
    await booster_bot._handle_callback(update_pos_short, mock_context)
    update_pos_short.callback_query.edit_message_text.assert_not_called()

    # Position callback unknown action
    update_pos_unknown = make_mock_update(callback_data="position:unknown_act:pos_1")
    await booster_bot._handle_callback(update_pos_unknown, mock_context)
    update_pos_unknown.callback_query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_bot_close_execution_flows_booster(booster_bot, mock_context):
    """Verify the full execution flow of close position confirm callbacks."""
    token = CallbackTokenStore.generate("pos_confirm_flow", PositionAction.CLOSE_FULL)
    update = make_mock_update(callback_data=f"confirm:{token}")

    # Case 1: Successful response from Engine
    with patch.object(booster_bot.event_bus, "publish", AsyncMock()) as mock_pub:
        # Schedule response to the future shortly
        async def simulate_engine_response():
            await asyncio.sleep(0.05)
            # Find correlation ID in futures mapping
            corrs = list(booster_bot._position_close_futures.keys())
            if corrs:
                booster_bot._position_close_futures[corrs[0]].set_result({"success": True, "size": 1.5})

        asyncio.create_task(simulate_engine_response())

        await booster_bot._handle_callback(update, mock_context)
        # Yield to let tasks run
        await asyncio.sleep(0.1)

        # Check confirmation message was updated to success screen
        args, kwargs = update.callback_query.edit_message_text.call_args
        text = kwargs.get("text", args[0] if args else "")
        assert "thành công" in text.lower()
        assert "1.5 contracts" in text.lower()

    # Case 2: Failed response from Engine
    token2 = CallbackTokenStore.generate("pos_confirm_flow2", PositionAction.CLOSE_FULL)
    update2 = make_mock_update(callback_data=f"confirm:{token2}")

    with patch.object(booster_bot.event_bus, "publish", AsyncMock()) as mock_pub2:
        async def simulate_engine_failure():
            await asyncio.sleep(0.05)
            corrs = list(booster_bot._position_close_futures.keys())
            if corrs:
                booster_bot._position_close_futures[corrs[0]].set_result({"success": False, "reason": "Insufficient margin"})

        asyncio.create_task(simulate_engine_failure())

        await booster_bot._handle_callback(update2, mock_context)
        await asyncio.sleep(0.1)

        args, kwargs = update2.callback_query.edit_message_text.call_args
        text = kwargs.get("text", args[0] if args else "")
        assert "thất bại" in text.lower()
        assert "insufficient margin" in text.lower()

    # Case 3: Timeout during wait for Engine response
    token3 = CallbackTokenStore.generate("pos_confirm_flow3", PositionAction.CLOSE_FULL)
    update3 = make_mock_update(callback_data=f"confirm:{token3}")

    with patch.object(asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError())):
        await booster_bot._handle_callback(update3, mock_context)
        await asyncio.sleep(0.05)

        args, kwargs = update3.callback_query.edit_message_text.call_args
        text = kwargs.get("text", args[0] if args else "")
        assert "timeout" in text.lower() or "hết hạn" in text.lower()

    # Case 4: Exception raised inside execute_close_task
    token4 = CallbackTokenStore.generate("pos_confirm_flow4", PositionAction.CLOSE_FULL)
    update4 = make_mock_update(callback_data=f"confirm:{token4}")

    # Force exception by making edit_message_text raise exception
    update4.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("Telegram disconnected"))
    await booster_bot._handle_callback(update4, mock_context)
    await asyncio.sleep(0.05) # should handle cleanly, logging error


@pytest.mark.asyncio
async def test_telegram_bot_notifications_booster(booster_bot):
    """Verify close success/failure event notifications trigger rendering and sending."""
    # Success event with raw dict data
    evt_succ = Event(
        event_type=EventTopic.POSITION_CLOSE_SUCCESS,
        data={"success": True, "symbol": "BTC-USDT-SWAP", "side": "long", "size": 1.2},
        source="engine",
        correlation_id="corr_succ"
    )

    # Setup mock future to check resolved state
    fut = asyncio.get_running_loop().create_future()
    booster_bot._position_close_futures["corr_succ"] = fut

    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send:
        await booster_bot._on_position_close_success(evt_succ)
        assert fut.done()
        assert fut.result()["success"] is True
        mock_send.assert_not_called()

    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send_external:
        await booster_bot._on_position_close_success(
            Event(
                event_type=EventTopic.POSITION_CLOSE_SUCCESS,
                data={"success": True, "symbol": "BTC-USDT-SWAP", "side": "long", "size": 1.2},
                source="engine",
            )
        )
        mock_send_external.assert_called_once()
        assert "đóng vị thế thành công" in mock_send_external.call_args[1]["text"].lower()

    # Failure event with raw dict data
    evt_fail = Event(
        event_type=EventTopic.POSITION_CLOSE_FAILURE,
        data={"success": False, "position_id": "pos_123", "reason": "Network timeout"},
        source="engine",
        correlation_id="corr_fail"
    )

    fut2 = asyncio.get_running_loop().create_future()
    booster_bot._position_close_futures["corr_fail"] = fut2

    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send2:
        await booster_bot._on_position_close_failure(evt_fail)
        assert fut2.done()
        assert fut2.result()["success"] is False
        mock_send2.assert_not_called()

    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock()) as mock_send2_external:
        await booster_bot._on_position_close_failure(
            Event(
                event_type=EventTopic.POSITION_CLOSE_FAILURE,
                data={"success": False, "position_id": "pos_123", "reason": "Network timeout"},
                source="engine",
            )
        )
        mock_send2_external.assert_called_once()
        assert "thất bại đóng vị thế" in mock_send2_external.call_args[1]["text"].lower()


@pytest.mark.asyncio
async def test_telegram_bot_start_stop_mocked(booster_bot):
    """Test start and stop methods of TelegramBot using mocked ApplicationBuilder."""
    booster_bot._enabled = True

    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.initialize = AsyncMock()
    mock_app.start = AsyncMock()
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()

    mock_updater = MagicMock()
    mock_updater.start_polling = AsyncMock()
    mock_updater.stop = AsyncMock()
    mock_app.updater = mock_updater

    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.connection_pool_size.return_value = mock_builder
    mock_builder.pool_timeout.return_value = mock_builder
    mock_builder.get_updates_connection_pool_size.return_value = mock_builder
    mock_builder.get_updates_pool_timeout.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch("interfaces.telegram.telegram_bot.ApplicationBuilder", return_value=mock_builder), \
         patch.object(settings, "telegram_bot_token", "fake_token"), \
         patch.object(booster_bot._dashboard, "start_auto_update", AsyncMock()) as mock_dash_start, \
         patch.object(booster_bot._dashboard, "stop_auto_update", AsyncMock()) as mock_dash_stop:

        await booster_bot.start()

        mock_builder.token.assert_called_with("fake_token")
        mock_app.initialize.assert_called_once()
        mock_app.start.assert_called_once()
        mock_updater.start_polling.assert_called_once()
        mock_dash_start.assert_called_once()

        # Test stop
        await booster_bot.stop()
        mock_updater.stop.assert_called_once()
        mock_app.stop.assert_called_once()
        mock_app.shutdown.assert_called_once()
        mock_dash_stop.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_bot_menu_main_callback(booster_bot, mock_context):
    """Verify menu:main callback logic."""
    update_main = make_mock_update(callback_data="menu:main")
    await booster_bot._handle_callback(update_main, mock_context)
    update_main.callback_query.edit_message_text.assert_called_once()
    args, kwargs = update_main.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "Đang tải Bảng điều khiển" in text


@pytest.mark.asyncio
async def test_telegram_bot_trading_callbacks(booster_bot, mock_context):
    """Verify trading callback variations."""
    # Trading manual order screen
    update_manual = make_mock_update(callback_data="trading:manual_order")
    await booster_bot._handle_callback(update_manual, mock_context)
    update_manual.callback_query.edit_message_text.assert_called_once()
    args, kwargs = update_manual.callback_query.edit_message_text.call_args
    text = kwargs.get("text", args[0] if args else "")
    assert "thủ công" in text.lower() or "manual" in text.lower()

    # Other trading submenus
    for act in ("open_positions", "active_signals", "pending_orders", "capital_management"):
        update_tr = make_mock_update(callback_data=f"trading:{act}")
        await booster_bot._handle_callback(update_tr, mock_context)
        update_tr.callback_query.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_bot_settings_exception_branch(booster_bot, mock_context):
    """Verify settings callback edit message exceptions."""
    update_fail = make_mock_update(callback_data="settings:bot_settings")
    update_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("Message not modified"))
    # Should handle cleanly and not crash callback loop
    await booster_bot._handle_callback(update_fail, mock_context)


@pytest.mark.asyncio
async def test_telegram_bot_extra_guards_and_exceptions(booster_bot, mock_context):
    """Verify other TelegramBot guards, exception handling, and edge cases to maximize coverage."""
    # 1. Start skipped when not enabled
    booster_bot._enabled = False
    await booster_bot.start() # should return early

    # Restore enabled
    booster_bot._enabled = True

    # 2. Start returns early when telegram_bot_token is missing
    with patch.object(settings, "telegram_bot_token", None):
        await booster_bot.start() # should return early

    # 3. Start throws exception on startup notification
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.initialize = AsyncMock()
    mock_app.start = AsyncMock()
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()
    mock_updater = MagicMock()
    mock_updater.start_polling = AsyncMock()
    mock_updater.stop = AsyncMock()
    mock_app.updater = mock_updater
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.connection_pool_size.return_value = mock_builder
    mock_builder.pool_timeout.return_value = mock_builder
    mock_builder.get_updates_connection_pool_size.return_value = mock_builder
    mock_builder.get_updates_pool_timeout.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch("interfaces.telegram.telegram_bot.ApplicationBuilder", return_value=mock_builder), \
         patch.object(settings, "telegram_bot_token", "fake_token"), \
         patch.object(booster_bot._dashboard, "start_auto_update", AsyncMock()), \
         patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock(side_effect=RuntimeError("API Error"))):
        # Should start successfully but log the warning/error when send_or_edit_message fails
        await booster_bot.start()
        await booster_bot.stop()

    # 4. Command guards checks
    # _cmd_start with missing message/bot
    update_no_msg = make_mock_update()
    update_no_msg.message = None
    await booster_bot._cmd_start(update_no_msg, mock_context) # returns early

    # _cmd_menu with missing message/bot
    await booster_bot._cmd_menu(update_no_msg, mock_context) # returns early

    # 5. Callback query missing
    update_no_query = make_mock_update()
    update_no_query.callback_query = None
    await booster_bot._handle_callback(update_no_query, mock_context) # returns early

    # 6. Callback query data missing
    update_no_data = make_mock_update()
    update_no_data.callback_query.data = None
    await booster_bot._handle_callback(update_no_data, mock_context) # returns early

    # 7. BadRequest during query.answer()
    update_bad_answer = make_mock_update(callback_data="menu:analytics")
    update_bad_answer.callback_query.answer = AsyncMock(side_effect=BadRequest("Query expired"))
    await booster_bot._handle_callback(update_bad_answer, mock_context) # should handle cleanly

    # 8. BadRequest / Exception during menu callbacks editing
    update_menu_fail = make_mock_update(callback_data="menu:analytics")
    update_menu_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("API issue"))
    await booster_bot._handle_callback(update_menu_fail, mock_context) # should handle cleanly

    # 9. Exception during trading callback manual_order edit
    update_tr_man_fail = make_mock_update(callback_data="trading:manual_order")
    update_tr_man_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("API issue"))
    await booster_bot._handle_callback(update_tr_man_fail, mock_context)

    # 10. Exception during trading callback regular action edit
    update_tr_fail = make_mock_update(callback_data="trading:open_positions")
    update_tr_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("API issue"))
    await booster_bot._handle_callback(update_tr_fail, mock_context)

    # 11. Exception during control callback actions
    for action in ("start_bot", "pause_bot", "restart_engine", "unknown_action"):
        update_ctrl_fail = make_mock_update(callback_data=f"control:{action}")
        update_ctrl_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("API issue"))
        await booster_bot._handle_callback(update_ctrl_fail, mock_context)

    # 12. Exception during confirm callback actions
    update_conf_fail = make_mock_update(callback_data="confirm:restart_engine")
    update_conf_fail.callback_query.edit_message_text = AsyncMock(side_effect=RuntimeError("API issue"))
    await booster_bot._handle_callback(update_conf_fail, mock_context)

    # 13. Exception during close failure notification
    evt_fail = Event(
        event_type=EventTopic.POSITION_CLOSE_FAILURE,
        data={"success": False, "position_id": "pos_123", "reason": "Network timeout"},
        source="engine"
    )
    with patch.object(booster_bot._dispatcher, "send_or_edit_message", AsyncMock(side_effect=RuntimeError("API Error"))):
        await booster_bot._on_position_close_failure(evt_fail) # should handle cleanly



