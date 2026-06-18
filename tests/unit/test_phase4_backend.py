"""Phase 4: backend control events, settings schema, EMA wiring, analytics helpers."""

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from core.events.topics import EventTopic
from services.market_data.indicators import IndicatorPipeline, EMACalculator
from services.position.telegram_handler import _compute_performance_metrics, _build_ascii_balance_chart


def test_phase4_control_event_topics_exist():
    assert EventTopic.CONTROL_RESET_SIGNALS.value == "control.reset_signals"
    assert EventTopic.CONTROL_RESET_SIGNALS_COMPLETE.value == "control.reset_signals_complete"
    assert EventTopic.CONTROL_CLEAN_BOT.value == "control.clean_bot"
    assert EventTopic.CONTROL_CLEAN_BOT_COMPLETE.value == "control.clean_bot_complete"


def test_settings_has_max_open_positions_and_redis_url():
    from core.config.settings import Settings

    s = Settings(
        okx_api_key="k",
        okx_api_secret="s",
        okx_passphrase="p",
        max_open_positions=5,
        redis_url="redis://127.0.0.1:6379/0",
    )
    assert s.max_open_positions == 5
    assert s.redis_url == "redis://127.0.0.1:6379/0"


def test_indicator_pipeline_uses_settings_ema_periods():
    mock_settings = MagicMock()
    mock_settings.min_candles = 1
    mock_settings.ema_fast_period = 12
    mock_settings.ema_slow_period = 26

    with patch("services.market_data.indicators.settings", mock_settings):
        pipeline = IndicatorPipeline()
        assert pipeline.fast_period == 12
        assert pipeline.slow_period == 26

        buffer = MagicMock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "15m"
        prices = [float(i) for i in range(1, 40)]
        buffer.get_close_prices.return_value = prices
        buffer.get_high_prices.return_value = prices
        buffer.get_low_prices.return_value = prices
        buffer.get_candles.return_value = tuple()

        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        assert snapshot is not None
        assert snapshot.symbol == "BTC-USDT-SWAP"
        assert snapshot.timeframe == "15m"
        assert snapshot.candle_type == "closed"
        assert snapshot.reference_candle_index == -2
        assert "ema12" in snapshot.indicators
        assert "ema26" in snapshot.indicators
        assert snapshot.indicators["ema9"] == snapshot.indicators["ema12"]
        assert snapshot.indicators["ema21"] == snapshot.indicators["ema26"]


def test_ema_crossover_detection_with_custom_periods():
    closes = [float(i) for i in range(1, 50)]
    fast_series = EMACalculator.calculate_series(closes, 9)
    slow_series = EMACalculator.calculate_series(closes, 21)
    assert len(fast_series) > 0
    assert len(slow_series) > 0


def test_compute_performance_metrics():
    metrics = _compute_performance_metrics([10.0, -5.0, 15.0, -3.0])
    assert "max_drawdown" in metrics
    assert "sharpe_ratio" in metrics
    assert metrics["max_drawdown"] >= 0


def test_build_ascii_balance_chart():
    chart = _build_ascii_balance_chart(
        {"2026-06-01": 100.0, "2026-06-02": 150.0, "2026-06-03": 120.0}
    )
    assert "06-01" in chart
    assert "▁" in chart or "█" in chart


@pytest.mark.asyncio
async def test_strategy_engine_reset_signals_publishes_complete():
    from unittest.mock import AsyncMock

    from core.event_bus import Event, EventBus
    from services.strategies.strategy_engine import StrategyEngine

    bus = EventBus()
    await bus.start()
    engine = StrategyEngine(bus, MagicMock())
    engine.reset_signal_buffers = AsyncMock(return_value=None)

    received = []

    async def capture(event: Event):
        received.append(event)

    bus.subscribe(capture, [EventTopic.CONTROL_RESET_SIGNALS_COMPLETE], handler_id="test_cap")

    await engine._handle_reset_signals(
        Event(
            event_type=EventTopic.CONTROL_RESET_SIGNALS,
            data={"message_id": 42},
            source="test",
        )
    )

    await asyncio.sleep(0.05)
    await bus.stop()
    assert len(received) == 1
    assert received[0].data["success"] is True
    assert received[0].data["message_id"] == 42
