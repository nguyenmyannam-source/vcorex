"""
Unit tests for Position Engine.
Tests position lifecycle, initialization, start/stop.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from services.position_engine import PositionEngine, PositionStatus, TrackedPosition


class TestTrackedPosition:
    """Test cases for TrackedPosition data class."""

    def test_position_initialization(self):
        """Test position starts with correct default status."""
        pos = TrackedPosition(
            id="test_123",
            exchange_id=None,
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=50000.0,
            current_price=50000.0,
            amount=0.001,
            amount_remaining=0.001,
            leverage=10,
            opened_at=datetime.now(timezone.utc),
        )

        assert pos.id == "test_123"
        assert pos.symbol == "BTC-USDT-SWAP"
        assert pos.status == PositionStatus.PENDING
        assert pos.pnl == 0.0
        assert pos.amount_remaining == 0.001

    def test_partial_close(self):
        """Test partial position closure updates remaining amount."""
        pos = TrackedPosition(
            id="test_123",
            exchange_id="okx_12345",
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=50000.0,
            current_price=51000.0,
            amount=0.001,
            amount_remaining=0.001,
            leverage=10,
            opened_at=datetime.now(timezone.utc),
        )
        # Partial close 50%
        pos.amount_remaining = 0.0005
        pos.status = PositionStatus.PARTIAL_TP
        pos.add_update("Partial take profit executed", {"closed_amount": 0.0005})
        assert pos.amount_remaining == 0.0005
        assert pos.status == PositionStatus.PARTIAL_TP
        assert len(pos.updates) == 1

    def test_full_close(self):
        """Test full position closure marks as CLOSED."""
        pos = TrackedPosition(
            id="test_123",
            exchange_id="okx_12345",
            symbol="BTC-USDT-SWAP",
            side="long",
            entry_price=50000.0,
            current_price=52000.0,
            amount=0.001,
            amount_remaining=0.0005,
            leverage=10,
            opened_at=datetime.now(timezone.utc),
        )
        pos.amount_remaining = 0.0
        pos.status = PositionStatus.CLOSED
        pos.closed_at = datetime.now(timezone.utc)
        pos.add_update("Full position closed")
        assert pos.amount_remaining == 0.0
        assert pos.status == PositionStatus.CLOSED
        assert pos.closed_at is not None
        assert len(pos.updates) == 1


@pytest.mark.asyncio
async def test_position_engine_initialization(
    event_bus, mock_exchange, mock_session_factory, test_settings
):
    """Test PositionEngine initializes correctly."""
    engine = PositionEngine(
        exchange=mock_exchange,
        event_bus=event_bus,
        session_factory=mock_session_factory,
        settings=test_settings,
    )

    assert engine.exchange == mock_exchange
    assert engine.event_bus == event_bus
    assert engine.session_factory == mock_session_factory
    assert engine.settings == test_settings
    assert len(engine._positions) == 0
    assert not engine._running


@pytest.mark.asyncio
async def test_position_engine_start_stop(
    event_bus, mock_exchange, mock_session_factory, test_settings
):
    """Test engine starts and stops cleanly."""
    engine = PositionEngine(
        exchange=mock_exchange,
        event_bus=event_bus,
        session_factory=mock_session_factory,
        settings=test_settings,
    )

    await engine.start()
    assert engine._running

    await engine.stop()
    assert not engine._running
