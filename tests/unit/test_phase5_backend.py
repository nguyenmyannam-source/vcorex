"""Phase 5: archive cleanup, ghost recovery consolidation, production risk path."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.events.topics import EventTopic
from domain.risk.risk_utilities import _calculate_max_positions
from services.position.models import PositionStatus


def test_root_bootstrap_removed():
    assert not Path("bootstrap.py").exists()
    assert not Path("archive/bootstrap_legacy_demo.py").exists()


def test_calculate_max_positions_production_mode():
    settings = MagicMock()
    settings.production_risk_mode = True
    settings.max_open_positions = 5
    assert _calculate_max_positions(settings) == 5


def test_calculate_max_positions_demo_with_explicit_cap():
    settings = MagicMock()
    settings.production_risk_mode = False
    settings.max_open_positions = 3
    assert _calculate_max_positions(settings) == 3


def test_calculate_max_positions_demo_unlimited():
    settings = MagicMock()
    settings.production_risk_mode = False
    settings.max_open_positions = 9999
    assert _calculate_max_positions(settings) == 9999


@pytest.mark.asyncio
async def test_risk_manager_rejects_when_max_positions_exceeded():
    from core.event_bus import Event
    from domain.risk.risk_manager import RiskManager
    from services.strategies.base_strategy import Signal, SignalType

    bus = MagicMock()
    exchange = AsyncMock()
    exchange.fetch_positions.return_value = [MagicMock()] * 3
    exchange.fetch_balance.return_value = {}

    settings = MagicMock()
    settings.production_risk_mode = True
    settings.max_open_positions = 2
    settings.default_leverage = 10
    settings.ENABLE_STRICT_ACCOUNT_SEEDING = False
    settings.max_symbol_concentration = 9999.0
    settings.sl_roe_pct = 50.0
    settings.fee_roe_buffer_pct = 0.0
    settings.max_risk_allowed_pct = 9999.0
    settings.min_risk_reward_ratio = 0.0
    settings.max_leverage = 10

    rm = RiskManager(bus, exchange, settings_obj=settings)
    rm._portfolio_metrics.total_open_positions = 2
    rm._in_flight_orders_count = 1

    signal = Signal(
        strategy_name="test",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        signal_type=SignalType.BUY,
        entry_price=100.0,
        position_size_usdt=1000.0,
        stop_loss_price=90.0,
    )
    signal.leverage = 10

    with patch("domain.risk.risk_manager.settings", settings):
        assessment = await rm.assess_signal(signal)

    assert assessment.approved is False
    assert "Max open positions" in assessment.reason


@pytest.mark.asyncio
async def test_ws_ghost_publishes_event_instead_of_direct_insert():
    from core.event_bus import Event
    from services.position_engine import PositionEngine

    bus = AsyncMock()
    exchange = MagicMock()
    settings = MagicMock()
    settings.default_leverage = 10
    settings.cb_threshold = 9999
    settings.cb_cooldown_seconds = 0

    engine = PositionEngine(exchange, bus, MagicMock(), settings)
    engine.order_handler._positions = {}

    await engine._handle_ws_position(
        Event(
            event_type=EventTopic.WS_RAW_POSITION,
            data={
                "symbol": "BTC-USDT-SWAP",
                "data": {
                    "instId": "BTC-USDT-SWAP",
                    "pos": "2",
                    "posSide": "long",
                    "avgPx": "50000",
                    "lever": "10",
                },
            },
            source="test",
        )
    )

    bus.publish.assert_called_once()
    published = bus.publish.call_args[0][0]
    assert published.event_type == EventTopic.POSITION_GHOST_DETECTED
    assert published.data["reason"] == "auto_recovery"
    assert len(engine.order_handler._positions) == 0
