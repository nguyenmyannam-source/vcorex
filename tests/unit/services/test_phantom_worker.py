import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.event_bus import EventBus
from core.metrics import InMemoryMetricsAdapter
from services.position.order_handler import OrderHandler
from services.position.models import TrackedPosition, PositionStatus


@pytest.mark.asyncio
async def test_phantom_worker_filled_flow():
    ex = AsyncMock()
    # verify returns FILLED
    ex.verify_order_status = AsyncMock(return_value="FILLED")
    ex.query_order_details = AsyncMock(return_value={
        "ordId": "ORD-123",
        "fillPx": "123.45",
        "sz": "1"
    })

    persistence = AsyncMock()
    persistence.save_position = AsyncMock()

    eb = EventBus()
    await eb.start()

    metrics = InMemoryMetricsAdapter()
    oh = OrderHandler(ex, eb, persistence, metrics=metrics)

    pos = TrackedPosition(
        id="pos-1",
        exchange_id=None,
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=0.0,
        current_price=123.45,
        amount=1.0,
        amount_remaining=1.0,
        leverage=1.0,
        status=PositionStatus.PENDING_RECONCILE,
        signal_id="sig-1",
    )

    oh._positions[pos.id] = pos

    await oh._verify_phantom_position_worker(pos.id)

    # Position should be marked OPENED and persisted
    assert pos.status == PositionStatus.OPENED
    assert persistence.save_position.await_count >= 1

    m = await metrics.get_metrics()
    assert m["phantom_verifications_attempted"] >= 1
    assert m["phantom_verifications_succeeded"] >= 1

    await eb.stop()


@pytest.mark.asyncio
async def test_phantom_worker_canceled_flow():
    ex = AsyncMock()
    ex.verify_order_status = AsyncMock(return_value="CANCELED")
    ex.query_order_details = AsyncMock()

    persistence = AsyncMock()
    persistence.save_position = AsyncMock()

    eb = EventBus()
    await eb.start()

    metrics = InMemoryMetricsAdapter()
    oh = OrderHandler(ex, eb, persistence, metrics=metrics)

    pos = TrackedPosition(
        id="pos-2",
        exchange_id=None,
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=0.0,
        current_price=123.45,
        amount=1.0,
        amount_remaining=1.0,
        leverage=1.0,
        status=PositionStatus.PENDING_RECONCILE,
        signal_id="sig-2",
    )

    oh._positions[pos.id] = pos

    await oh._verify_phantom_position_worker(pos.id)

    assert pos.status == PositionStatus.FAILED
    assert persistence.save_position.await_count >= 1

    m = await metrics.get_metrics()
    assert m["phantom_verifications_attempted"] >= 1
    assert m["phantom_verifications_failed"] >= 1

    await eb.stop()


@pytest.mark.asyncio
async def test_phantom_worker_unknown_exhausts():
    ex = AsyncMock()

    # Always return UNKNOWN
    ex.verify_order_status = AsyncMock(return_value="UNKNOWN")
    ex.query_order_details = AsyncMock()

    persistence = AsyncMock()
    persistence.save_position = AsyncMock()

    eb = EventBus()
    await eb.start()

    metrics = InMemoryMetricsAdapter()
    oh = OrderHandler(ex, eb, persistence, metrics=metrics)

    pos = TrackedPosition(
        id="pos-3",
        exchange_id=None,
        symbol="BTC-USDT-SWAP",
        side="long",
        entry_price=0.0,
        current_price=123.45,
        amount=1.0,
        amount_remaining=1.0,
        leverage=1.0,
        status=PositionStatus.PENDING_RECONCILE,
        signal_id="sig-3",
    )

    oh._positions[pos.id] = pos

    await oh._verify_phantom_position_worker(pos.id)

    # After exhausting retries, position should still be PENDING_RECONCILE
    assert pos.status == PositionStatus.PENDING_RECONCILE

    m = await metrics.get_metrics()
    assert m["phantom_verifications_attempted"] >= 1
    assert m["phantom_verifications_unknown"] >= 1

    await eb.stop()