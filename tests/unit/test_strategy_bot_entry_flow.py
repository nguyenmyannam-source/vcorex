import asyncio
from datetime import datetime, timezone

import pytest

from core.container import container
from core.event_bus_components import Event
from core.events.topics import EventTopic
from services.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalType,
    SignalStrength,
    StrategyConfig,
)
from services.strategies.strategy_engine import StrategyEngine


class DummyEventBus:
    def __init__(self):
        self.published = []

    def subscribe(self, callback, event_types, filter_func=None, handler_id=None):
        return handler_id or "dummy_handler"

    async def publish(self, event: Event):
        self.published.append(event)


class DummyMarketDataEngine:
    def __init__(self, readiness):
        self.readiness = readiness
        self.stream_health = {tf: "HEALTHY" for tf in readiness}

    def is_timeframe_ready(self, timeframe: str) -> bool:
        return self.readiness.get(timeframe, False)


class MultiTimeframeStrategy(BaseStrategy):
    def __init__(self, name: str, symbol: str, timeframes: list[str]):
        super().__init__(StrategyConfig(name=name, enabled=True, symbols=[symbol], timeframes=timeframes))
        self._candles = {tf: [object()] * 35 for tf in timeframes}

    def get_candles(self, symbol: str, timeframe: str, limit: int = 50):
        return self._candles.get(timeframe, [])

    async def generate_signal(self, symbol: str, timeframe: str):
        return Signal(
            symbol=symbol,
            timeframe=timeframe,
            strategy_name=self.config.name,
            signal_type=SignalType.BUY,
            entry_price=123.45,
            stop_loss_price=120.0,
            take_profit_prices=[{"price": 130.0, "exit_pct": 1.0}],
            timestamp=datetime.now(timezone.utc),
            signal_strength=SignalStrength.HIGH,
        )

    async def validate_signal(self, signal: Signal) -> bool:
        return True

    async def filters(self, symbol: str, timeframe: str) -> bool:
        return True

    async def build_trade_plan(self, signal: Signal) -> Signal:
        return signal


@pytest.mark.asyncio
async def test_strategy_engine_generates_multi_timeframe_entry_signals():
    event_bus = DummyEventBus()
    engine = StrategyEngine(event_bus=event_bus, exchange=None)
    container.register_instance("market_data_engine", DummyMarketDataEngine({"1H": True, "4H": True}))

    strategy = MultiTimeframeStrategy(name="multi_tf", symbol="BTC-USDT-SWAP", timeframes=["1H", "4H"])
    await engine.start()

    await engine._analyze_symbol_timeframe("BTC-USDT-SWAP", "1H", strategy)
    await engine._analyze_symbol_timeframe("BTC-USDT-SWAP", "4H", strategy)

    assert len(event_bus.published) == 2
    published_timeframes = {event.data["timeframe"] for event in event_bus.published}
    assert published_timeframes == {"1H", "4H"}
    assert all(event.event_type == EventTopic.STRATEGY_SIGNAL_GENERATED for event in event_bus.published)

    container._instances.pop("market_data_engine", None)


@pytest.mark.asyncio
async def test_strategy_engine_skips_signal_when_timeframe_not_ready():
    event_bus = DummyEventBus()
    engine = StrategyEngine(event_bus=event_bus, exchange=None)
    container.register_instance("market_data_engine", DummyMarketDataEngine({"1H": True, "4H": False}))

    strategy = MultiTimeframeStrategy(name="multi_tf", symbol="BTC-USDT-SWAP", timeframes=["1H", "4H"])
    await engine.start()

    await engine._analyze_symbol_timeframe("BTC-USDT-SWAP", "1H", strategy)
    await engine._analyze_symbol_timeframe("BTC-USDT-SWAP", "4H", strategy)

    assert len(event_bus.published) == 1
    assert event_bus.published[0].data["timeframe"] == "1H"
    assert event_bus.published[0].event_type == EventTopic.STRATEGY_SIGNAL_GENERATED

    container._instances.pop("market_data_engine", None)