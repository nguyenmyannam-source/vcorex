import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from core.config.settings import Settings
from core.event_bus import EventBus
from core.event_bus_components import Event
from services.market_data_engine import MarketDataEngine
from services.strategies.base_strategy import BaseStrategy, Signal, SignalType, StrategyConfig
from services.strategies.strategy_engine import StrategyEngine


@pytest.fixture
def mock_exchange_black_swan():
    """Mock exchange trả về một cây nến Thiên nga đen."""
    exchange = AsyncMock()
    exchange.get_historical_candles.return_value = []

    # Sử dụng from_list để tạo đối tượng OHLCV đúng cách
    from infrastructure.exchange.base_exchange import OHLCV

    # Tạo 50 nến bình thường trước đó
    normal_candles = [
        OHLCV.from_list(
            data=[1672531200000 + i * 60000, 100, 101, 99, 100, 1000],
            symbol="BTC-USDT-SWAP",
            timeframe="5m",
        )
        for i in range(50)
    ]

    # Nến Thiên nga đen - giá giảm 50% (ở cuối danh sách)
    black_swan_candle = OHLCV.from_list(
        data=[1672531200000 + 50 * 60000, 100, 101, 50, 51, 5000],
        symbol="BTC-USDT-SWAP",
        timeframe="5m",
    )

    all_candles = normal_candles + [black_swan_candle]

    # Mock fetch_ohlcv để trả về 51 candles (50 bình thường + 1 thiên nga đen)
    exchange.fetch_ohlcv = AsyncMock(return_value=all_candles)
    exchange.fetch_latest_candle = AsyncMock(return_value=black_swan_candle)
    return exchange


@pytest.fixture
async def setup_scenario(mock_exchange_black_swan):
    """Setup các engine cho kịch bản test."""
    event_bus = EventBus()
    settings = Settings(
        watchlist=["BTC-USDT-SWAP"],
        timeframes=["5m"],
        _env_file=None,
        okx_api_key="test_key",
        okx_api_secret="test_secret",
        okx_passphrase="test_passphrase",
    )

    mde = MarketDataEngine(
        exchange=mock_exchange_black_swan, event_bus=event_bus, settings=settings
    )
    se = StrategyEngine(event_bus=event_bus, exchange=mock_exchange_black_swan)

    # Strategy đơn giản chỉ để tạo tín hiệu
    class SimpleStrategy(BaseStrategy):
        async def generate_signal(self, symbol: str, timeframe: str):
            candles = self.get_candles(symbol, timeframe)
            if len(candles) < 2:
                return None

            last_candle = candles[-1]  # Nến mới nhất (black swan)
            prev_candle = candles[-2]  # Nến trước đó (bình thường)

            # Nếu giá giảm hơn 30% -> tạo tín hiệu bán
            price_drop_pct = (prev_candle.close - last_candle.close) / prev_candle.close
            if price_drop_pct > 0.3:
                return Signal(
                    symbol=symbol,
                    timeframe="5m",
                    strategy_name=self.config.name,
                    signal_type=SignalType.SELL,
                    entry_price=last_candle.close,
                    stop_loss_price=last_candle.close * 1.1,
                    take_profit_prices=[last_candle.close * 0.8],
                    timestamp=datetime.now(timezone.utc),
                )
            return None

        async def validate_signal(self, signal: Signal) -> bool:
            return True

        async def filters(self, symbol: str, timeframe: str) -> bool:
            return True

        async def build_trade_plan(self, signal: Signal) -> Signal:
            return signal

    strategy_config = StrategyConfig(
        name="SimpleStrat", symbols=["BTC-USDT-SWAP"], timeframes=["5m"]
    )
    strategy = SimpleStrategy(strategy_config)
    await se.register_strategy(strategy)

    await event_bus.start()
    await mde.start()
    await se.start()

    yield mde, se, event_bus, strategy

    await se.stop()
    await mde.stop()
    await event_bus.stop()


@pytest.mark.asyncio
class TestBlackSwanScenario:
    async def test_system_handles_price_crash_without_error(self, setup_scenario):
        """
        Test kịch bản Thiên nga đen:
        1. MDE xử lý nến giảm giá mạnh.
        2. SE tạo ra tín hiệu BÁN.
        3. Hệ thống không bị crash.
        """
        mde, se, event_bus, strategy = setup_scenario

        # Lấy các nến từ mock trực tiếp
        from infrastructure.exchange.base_exchange import OHLCV

        # Tạo 51 nến (50 bình thường + 1 thiên nga đen) để đáp ứng yêu cầu 30+ candles
        normal_candles = [
            OHLCV.from_list(
                data=[1672531200000 + i * 60000, 100, 101, 99, 100, 1000],
                symbol="BTC-USDT-SWAP",
                timeframe="5m",
            )
            for i in range(50)
        ]
        black_swan_candle = OHLCV.from_list(
            data=[1672531200000 + 50 * 60000, 100, 101, 50, 51, 5000],
            symbol="BTC-USDT-SWAP",
            timeframe="5m",
        )

        # Cập nhật trực tiếp candle data bằng cách mock hàm get_candles thay vì gọi update_candle_data
        strategy.get_candles = Mock(return_value=normal_candles + [black_swan_candle])
        await asyncio.sleep(0.01)

        # Spy vào event bus để kiểm tra tín hiệu đã được publish
        publish_spy = AsyncMock(wraps=event_bus.publish)
        event_bus.publish = publish_spy

        # Chạy _analyze_symbol_timeframe để kích hoạt generate_signal và publish
        await se._analyze_symbol_timeframe("BTC-USDT-SWAP", "5m", strategy)
        await asyncio.sleep(0.05)

        # Kiểm tra rằng event_bus.publish đã được gọi với sự kiện đúng
        publish_spy.assert_called()
        signal_event_found = False
        for call in publish_spy.call_args_list:
            if len(call.args) > 0:
                event = call.args[0]
                if isinstance(event, Event) and event.event_type == "strategy.signal_generated":
                    signal_event_found = True
                    assert event.data["signal_type"] == SignalType.SELL
                    assert event.data["entry_price"] == 51  # Giá đóng cửa của nến thiên nga đen
                    break
        assert signal_event_found, "Không tìm thấy event strategy.signal_generated"