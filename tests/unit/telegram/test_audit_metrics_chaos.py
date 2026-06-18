"""
Comprehensive unit and integration tests verifying:
1. OperationalMetricsAdapter metrics collection.
2. Cryptographic tamper detection and verification of the AuditJournal.
3. Decoupled AuditSubscriber pattern.
4. Chaos and failure injection scenarios (timeout, db locked, replay flood, DLQ).
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.events.payloads import PositionCloseRequest, PositionAction
from core.metrics import InMemoryMetricsAdapter
from core.audit_journal import AuditJournal, calculate_hash
from core.audit_subscriber import AuditSubscriber
from services.position_engine import PositionEngine, CircuitState, PositionStatus
from infrastructure.exchange.base_exchange import BaseExchange, Position
from interfaces.telegram.telegram_bot import TelegramBot, CallbackTokenStore
from telegram import Update
from telegram.ext import ContextTypes


def create_mock_update(user_id=123456789, chat_id=123456789, callback_data=""):
    """Create a mock Telegram Update object."""
    update = MagicMock(spec=Update)

    user = MagicMock()
    user.id = user_id
    update.effective_user = user

    chat = MagicMock()
    chat.id = chat_id
    update.effective_chat = chat

    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = user
    update.callback_query = query

    msg = AsyncMock()
    msg.message_id = 9999
    msg.reply_text = AsyncMock()
    update.message = msg

    return update


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object."""
    return create_mock_update()


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
    from core.config.settings import settings
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
# 1. Operational Metrics Adapter Tests
# =========================================================================
@pytest.mark.asyncio
async def test_metrics_adapter():
    """Verify that InMemoryMetricsAdapter tracks metrics asynchronously and safely."""
    adapter = InMemoryMetricsAdapter()
    await adapter.increment_lock_contention()
    await adapter.increment_cb_open()
    await adapter.increment_replay_attempts()
    await adapter.increment_exchange_timeout()

    metrics = await adapter.get_metrics()
    assert metrics["lock_contention_rate"] == 1
    assert metrics["circuit_breaker_open_rate"] == 1
    assert metrics["callback_replay_attempts"] == 1
    assert metrics["exchange_timeout_frequency"] == 1


# =========================================================================
# 2. Cryptographic Hash Chain & Verification Tests
# =========================================================================
@pytest.mark.asyncio
async def test_audit_journal_hash_chain():
    """Verify sequence IDs are monotonic and previous_hash chains cryptographically."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from infrastructure.storage.database import Base, AuditLog

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    with patch("infrastructure.storage.database.AsyncSessionLocal", session_factory):
        journal = AuditJournal(batch_size=2, flush_interval=0.01)
        await journal.start()

        # Enqueue 2 events
        journal.log_event("evt_1", "req_1", "corr_1", "cause_1", None, "topic_1", {"data": "val_1"}, "actor_1")
        journal.log_event("evt_2", "req_2", "corr_1", "evt_1", "req_1", "topic_2", {"data": "val_2"}, "actor_1")

        # Let batch flush run
        await asyncio.sleep(0.1)

        async with session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(select(AuditLog).order_by(AuditLog.sequence_id.asc()))
            logs = list(res.scalars().all())

            assert len(logs) == 2
            assert logs[0].sequence_id == 1
            assert logs[1].sequence_id == 2
            assert logs[0].previous_hash == "0" * 64
            assert logs[1].previous_hash == logs[0].event_hash

            # Recalculate hash of logs[0]
            expected_hash_0 = calculate_hash(
                sequence_id=logs[0].sequence_id,
                previous_hash=logs[0].previous_hash,
                event_type=logs[0].event_type,
                payload_str=logs[0].payload,
                timestamp=logs[0].timestamp
            )
            assert logs[0].event_hash == expected_hash_0

            # Recalculate hash of logs[1]
            expected_hash_1 = calculate_hash(
                sequence_id=logs[1].sequence_id,
                previous_hash=logs[1].previous_hash,
                event_type=logs[1].event_type,
                payload_str=logs[1].payload,
                timestamp=logs[1].timestamp
            )
            assert logs[1].event_hash == expected_hash_1

        await journal.stop()


# =========================================================================
# 3. Decoupled AuditSubscriber Tests
# =========================================================================
@pytest.mark.asyncio
async def test_audit_subscriber_dispatch():
    """Verify AuditSubscriber listens to all events on EventBus and routes to journal."""
    event_bus = EventBus()
    await event_bus.start()

    mock_journal = MagicMock(spec=AuditJournal)
    subscriber = AuditSubscriber(event_bus, mock_journal)
    subscriber.start()

    event = Event(
        event_type=EventTopic.POSITION_CLOSED,
        data={"position_id": "pos_1", "status": "closed"},
        source="engine",
        correlation_id="corr_123",
        causation_id="cause_123",
        parent_request_id="parent_123",
        event_version="1.0"
    )
    await event_bus.publish(event)
    await asyncio.sleep(0.05)  # Yield to worker thread

    mock_journal.log_event.assert_called_once()
    args, kwargs = mock_journal.log_event.call_args
    assert kwargs["event_id"] == event.event_id
    assert kwargs["correlation_id"] == "corr_123"
    assert kwargs["causation_id"] == "cause_123"
    assert kwargs["parent_request_id"] == "parent_123"
    assert kwargs["event_version"] == "1.0"

    subscriber.stop()
    await event_bus.stop()


# =========================================================================
# 4. Chaos/Failure Injection Tests
# =========================================================================
@pytest.mark.asyncio
async def test_chaos_database_locked():
    """Verify database locks (OperationalError) are handled gracefully without crashing."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock(side_effect=OperationalError("select", {}, sqlite3.OperationalError("database is locked")))
    mock_session.rollback = AsyncMock()
    mock_session.close = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_session_factory = MagicMock()
    mock_session_factory.return_value = mock_session

    with patch("infrastructure.storage.database.AsyncSessionLocal", mock_session_factory):
        journal = AuditJournal(batch_size=1, flush_interval=0.01)
        await journal.start()

        # Enqueue event (should run without crash)
        journal.log_event("evt_1", "req_1", "corr_1", "cause_1", None, "topic_1", {"data": "val_1"}, "actor_1")
        await asyncio.sleep(0.05)

        # Rollback must be called due to DB lock failure
        mock_session.rollback.assert_called_once()
        await journal.stop()


@pytest.mark.asyncio
async def test_chaos_exchange_timeout(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify exchange timeouts trip the circuit breaker and increment metrics."""
    mock_exchange.fetch_position = AsyncMock(side_effect=asyncio.TimeoutError())

    metrics = InMemoryMetricsAdapter()
    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings, metrics)

    local_pos = MagicMock()
    local_pos.symbol = "BTC-USDT-SWAP"
    local_pos.side = "long"
    local_pos.status = PositionStatus.OPENED
    engine.order_handler._positions["pos_1"] = local_pos

    request = PositionCloseRequest(
        request_id="req_timeout",
        correlation_id="corr_timeout",
        causation_id="cause_timeout",
        position_id="pos_1",
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
        evt_res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert evt_res.data["success"] is False
    assert "timeout" in evt_res.data["reason"].lower()

    metrics_data = await metrics.get_metrics()
    assert metrics_data["exchange_timeout_frequency"] == 1
    assert engine._cb_failure_count == 1


@pytest.mark.asyncio
async def test_chaos_partial_fills(mock_exchange, mock_session_factory, test_settings, event_bus):
    """Verify partial fills update position status correctly."""
    mock_exchange.normalize_position_size = MagicMock(return_value=5.0)
    mock_exchange.fetch_position = AsyncMock(return_value=Position(
        position_id="okx_pos_1",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=10.0,
        entry_price=60000.0,
        current_price=60000.0,
        unrealized_pnl=0.0,
        leverage=10,
        timestamp=int(time.time() * 1000)
    ))

    engine = PositionEngine(mock_exchange, event_bus, mock_session_factory, test_settings)

    from services.position.models import TrackedPosition
    local_pos = TrackedPosition(
        id="pos_partial",
        symbol="BTC-USDT-SWAP",
        side="long",
        amount=10.0,
        amount_remaining=10.0,
        entry_price=60000.0,
        current_price=60000.0,
        leverage=10,
        exchange_id="okx_pos_1",
        status=PositionStatus.OPENED
    )
    engine.order_handler._positions["pos_partial"] = local_pos

    async def mock_close(pos_id, close_amount, correlation_id=None):
        local_pos.amount_remaining -= close_amount
        if local_pos.amount_remaining > 0:
            local_pos.status = PositionStatus.PARTIAL_TP
        else:
            local_pos.status = PositionStatus.CLOSED
        return True

    engine.close_position = mock_close

    request = PositionCloseRequest(
        request_id="req_half",
        correlation_id="corr_half",
        causation_id="cause_half",
        position_id="pos_partial",
        action=PositionAction.CLOSE_HALF,
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
        evt_res = await asyncio.wait_for(future, 1.0)
    finally:
        await event_bus.stop()

    assert evt_res.data["success"] is True
    assert local_pos.status == PositionStatus.PARTIAL_TP
    assert local_pos.amount_remaining == 5.0


@pytest.mark.asyncio
async def test_chaos_eventbus_dlq():
    """Verify failed event handlers result in DLQ routing to system.dead_letter."""
    from core.config.settings import Settings
    from infrastructure.storage.database import async_init_database

    test_settings = Settings(
        okx_api_key="mock",
        okx_api_secret="mock",
        okx_passphrase="mock",
        database_url="sqlite:///:memory:"
    )
    await async_init_database(test_settings)

    event_bus = EventBus()
    event_bus.backoff_multiplier = 0.0
    await event_bus.start()

    async def bad_handler(evt):
        raise ValueError("Simulated handler crash")

    event_bus.subscribe(bad_handler, ["test.dlq_topic"], handler_id="bad_handler")

    dlq_future = asyncio.get_running_loop().create_future()
    async def dlq_handler(evt):
        dlq_future.set_result(evt)
    event_bus.subscribe(dlq_handler, ["system.dead_letter"], handler_id="dlq_handler")

    test_event = Event(
        event_type="test.dlq_topic",
        data={"some": "payload"},
        source="test_source",
        correlation_id="corr_dlq",
        causation_id="cause_dlq",
        parent_request_id="parent_dlq"
    )

    await event_bus.publish(test_event)

    dlq_event = await asyncio.wait_for(dlq_future, 1.0)
    assert dlq_event.event_type == "system.dead_letter"
    assert dlq_event.data["failed_topic"] == "test.dlq_topic"
    assert "Max retries exceeded" in dlq_event.data["error"]
    assert dlq_event.correlation_id == "corr_dlq"
    assert dlq_event.causation_id == test_event.event_id

    await event_bus.stop()




@pytest.mark.asyncio
async def test_chaos_telegram_api_failure(event_bus):
    """Verify Telegram API failures are handled gracefully without crashing trading."""
    from telegram.error import NetworkError

    bot = TelegramBot(event_bus)
    bot._enabled = True

    mock_dispatcher = MagicMock()
    mock_dispatcher.send_or_edit_message = AsyncMock(side_effect=NetworkError("Network failed"))
    bot._dispatcher = mock_dispatcher

    success_event = Event(
        event_type=EventTopic.POSITION_CLOSE_SUCCESS,
        data={"symbol": "BTC-USDT-SWAP", "side": "long", "size": 1.0},
        source="position_engine"
    )

    # Should handle failure without throwing exception
    await bot._on_position_close_success(success_event)
    mock_dispatcher.send_or_edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_chaos_duplicate_responses(event_bus):
    """Verify duplicate exchange responses are handled idempotently."""
    bot = TelegramBot(event_bus)
    bot._enabled = True

    future = asyncio.get_running_loop().create_future()
    bot._position_close_futures["corr_dup"] = future

    success_event = Event(
        event_type=EventTopic.POSITION_CLOSE_SUCCESS,
        data={"success": True, "correlation_id": "corr_dup", "symbol": "BTC-USDT-SWAP", "side": "long", "size": 1.0},
        source="position_engine"
    )

    await bot._on_position_close_success(success_event)
    assert future.done()
    assert (await future)["success"] is True

    # Subsequent duplicates must be ignored without exception
    await bot._on_position_close_success(success_event)


@pytest.mark.asyncio
async def test_chaos_replay_flood_attack(telegram_bot, mock_update, mock_context):
    """Verify replayed callback tokens update metrics instead of executing close."""
    telegram_bot._enabled = True
    telegram_bot._bot = mock_context.bot

    metrics = InMemoryMetricsAdapter()
    telegram_bot._metrics = metrics

    token = CallbackTokenStore.generate("pos_spam", PositionAction.CLOSE_FULL)
    mock_update.callback_query.data = f"confirm:{token}"

    with patch.object(telegram_bot.event_bus, "publish", AsyncMock()):
        await telegram_bot._handle_callback(mock_update, mock_context)

        # Second try (consumed token)
        mock_update2 = create_mock_update(callback_data=f"confirm:{token}")

        await telegram_bot._handle_callback(mock_update2, mock_context)
        await asyncio.sleep(0.05)

        # Replay attempts must increment
        metrics_data = await metrics.get_metrics()
        assert metrics_data["callback_replay_attempts"] == 1
