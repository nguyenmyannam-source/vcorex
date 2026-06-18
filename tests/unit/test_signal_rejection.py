
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.event_bus_components import Event
from core.events.topics import EventTopic
from services.strategies.ema_crossover import EMACrossoverStrategy

@pytest.mark.asyncio
async def test_publish_rejection_weak_trend():
    """
    Verifies that a 'SIGNAL_REJECTED' event is published correctly
    when a signal is rejected due to a weak trend (low ADX).
    """
    # 1. Setup
    mock_event_bus = AsyncMock()
    
    # Mock the dependencies for EMACrossoverStrategy
    mock_exchange = MagicMock()
    mock_symbol = "BTC-USDT-SWAP"
    mock_settings = MagicMock()
    
    from services.strategies.base_strategy import StrategyConfig
    config = StrategyConfig(
        name="test_ema_crossover",
        symbols=[mock_symbol],
        timeframes=["1h"]
    )
    strategy = EMACrossoverStrategy(
        config=config,
        event_bus=mock_event_bus
    )

    # 2. Define rejection data
    rejection_data = {
        "symbol": mock_symbol,
        "timeframe": "1h",
        "signal_type": "long",
        "reason": "weak_trend",
        "details": {
            "adx_value": 18.5,
            "adx_threshold": 20.0
        }
    }

    # 3. Action: Create a Signal object and call _publish_rejection
    from services.strategies.base_strategy import Signal, SignalType
    signal = Signal(
        symbol=mock_symbol,
        timeframe="1h",
        signal_type=SignalType.BUY,
        entry_price=100.0,
        strategy_name="test_ema_crossover"
    )
    await strategy._publish_rejection(signal, "weak_trend", {"adx_value": 18.5, "adx_threshold": 20.0})

    # 4. Assert
    # Check if event_bus.publish was called
    mock_event_bus.publish.assert_awaited_once()
    
    # Get the actual event that was published
    published_event_call = mock_event_bus.publish.call_args
    published_event: Event = published_event_call.args[0]

    # Verify the event's content
    assert published_event.event_type == EventTopic.SIGNAL_REJECTED
    assert published_event.data["symbol"] == mock_symbol
    assert published_event.data["reason"] == "weak_trend"
    assert published_event.data["details"]["adx_value"] == 18.5

@pytest.mark.asyncio
async def test_publish_rejection_small_body():
    """
    Verifies that a 'SIGNAL_REJECTED' event is published correctly
    when a signal is rejected due to a small candle body.
    """
    # 1. Setup
    mock_event_bus = AsyncMock()
    mock_exchange = MagicMock()
    mock_symbol = "ETH-USDT-SWAP"
    mock_settings = MagicMock()

    from services.strategies.base_strategy import StrategyConfig
    config = StrategyConfig(
        name="test_ema_crossover",
        symbols=[mock_symbol],
        timeframes=["4h"]
    )
    strategy = EMACrossoverStrategy(
        config=config,
        event_bus=mock_event_bus
    )

    # 2. Define rejection data
    rejection_data = {
        "symbol": mock_symbol,
        "timeframe": "4h",
        "signal_type": "short",
        "reason": "small_body",
        "details": {
            "body_pct": 0.45,
            "min_body_pct": 0.5
        }
    }

    # 3. Action: Create a Signal object and call _publish_rejection
    from services.strategies.base_strategy import Signal, SignalType
    signal = Signal(
        symbol=mock_symbol,
        timeframe="4h",
        signal_type=SignalType.SELL,
        entry_price=100.0,
        strategy_name="test_ema_crossover"
    )
    await strategy._publish_rejection(signal, "small_body", {"body_pct": 0.45, "min_body_pct": 0.5})

    # 4. Assert
    mock_event_bus.publish.assert_awaited_once()
    
    published_event: Event = mock_event_bus.publish.call_args.args[0]

    assert published_event.event_type == EventTopic.SIGNAL_REJECTED
    assert published_event.data["symbol"] == mock_symbol
    assert published_event.data["reason"] == "small_body"
    assert published_event.data["details"]["body_pct"] == 0.45