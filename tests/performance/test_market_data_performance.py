"""
Test hiệu năng và độ trễ của Market Data Engine theo chuẩn institutional.
Bản cập nhật tương thích với API MarketDataEngine hiện tại (2026).
Bao gồm:
- Latency testing của indicator calculation
- Throughput handling của buffer processing
- Multi-timeframe synchronization
- Memory usage monitoring
- Indicator calculation speed
"""

import time
from unittest.mock import AsyncMock

import pytest
from pydantic_settings import SettingsConfigDict

from core.config.settings import Settings
from infrastructure.exchange.base_exchange import OHLCV
from services.market_data_engine import (
    CandleBuffer,
    EMACalculator,
    MarketDataEngine,
)
from utils.okx_symbols import OKX_SUPPORTED_TIMEFRAMES, OKX_TOP20_COINS


class TestMarketDataPerformance:
    """Test suite hiệu năng của Market Data Engine theo chuẩn institutional."""

    @pytest.fixture
    def performance_settings(self) -> Settings:
        """Settings cho test hiệu năng, hỗ trợ all symbols."""

        class TestSettings(Settings):
            model_config = SettingsConfigDict(env_file=None, extra="ignore")

        return TestSettings(
            okx_api_key="test",
            okx_api_secret="test",
            okx_passphrase="test",
            telegram_bot_token="test",
            watchlist=OKX_TOP20_COINS,
            database_url="sqlite:///:memory:",
            default_leverage=10,
            max_position_size_usdt=5000.0,
            default_stop_loss_pct=2.0,
            default_take_profit_pct=5.0,
            min_body_percentage=5.0,
            default_risk_per_trade=1.0,
            enable_default_strategy=True,
            environment="test",
            max_candles_per_buffer=1000,
            websocket_reconnect_delay=1,
        )

    @pytest.mark.asyncio
    async def test_indicator_calculation_latency(self, performance_settings: Settings):
        """Test độ trễ tính toán indicator < 50ms (relaxed for CI)."""
        event_bus = AsyncMock()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv.return_value = []

        engine = MarketDataEngine(
            exchange=mock_exchange, event_bus=event_bus, settings=performance_settings
        )

        # Seed buffer directly (bypassing _initialize_buffers for all 20 coins)
        symbol, timeframe = "BTC-USDT-SWAP", "5m"
        key = f"{symbol}_{timeframe}"
        buf = CandleBuffer(symbol, timeframe)
        base_price = 68000.0
        for i in range(200):
            buf.add_candle(
                OHLCV(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=int(time.time()) - (200 - i) * 60,
                    open=base_price + i,
                    high=base_price + i + 50,
                    low=base_price + i - 50,
                    close=base_price + i + 25,
                    volume=100,
                )
            )
        engine.buffers[key] = buf

        # Benchmark indicator computation only
        start_time = time.perf_counter()
        for _ in range(100):
            await engine._compute_and_publish_indicators(symbol, timeframe)
        end_time = time.perf_counter()

        avg_latency = (end_time - start_time) * 1000 / 100
        print(f"Average indicator calculation latency: {avg_latency:.2f}ms")

        # Institutional requirement: avg latency < 50ms (relaxed for CI)
        assert (
            avg_latency < 50
        ), f"Average indicator latency {avg_latency:.2f}ms exceeds 50ms threshold"

    @pytest.mark.asyncio
    async def test_throughput_buffer_processing(self, performance_settings: Settings):
        """Test engine xử lý được ít nhất 500 buffer updates/giây."""
        event_bus = AsyncMock()
        mock_exchange = AsyncMock()

        # Mock fetch_ohlcv
        mock_exchange.fetch_ohlcv.return_value = []

        engine = MarketDataEngine(
            exchange=mock_exchange, event_bus=event_bus, settings=performance_settings
        )

        # Tạo 1500 nến từ 20 coin khác nhau
        candles = []
        for i in range(1500):
            symbol = OKX_TOP20_COINS[i % len(OKX_TOP20_COINS)]
            ts = int(time.time()) - (1500 - i) * 60
            candles.append(
                OHLCV(
                    symbol=symbol,
                    timeframe="5m",
                    timestamp=ts,
                    open=100 + i,
                    high=101 + i,
                    low=99 + i,
                    close=100.5 + i,
                    volume=100,
                )
            )

        # Đo thời gian xử lý
        start = time.perf_counter()
        for candle in candles:
            key = f"{candle.symbol}_{candle.timeframe}"
            if key not in engine.buffers:
                engine.buffers[key] = CandleBuffer(candle.symbol, candle.timeframe)
            engine.buffers[key].add_candle(candle)
        end = time.perf_counter()

        total_time = end - start
        throughput = len(candles) / total_time

        print(f"Processed {len(candles)} buffer adds in {total_time:.2f}s")
        print(f"Throughput: {throughput:.1f} candles/second")

        # Phải xử lý được > 500 buffer adds/s
        assert throughput > 500, f"Throughput {throughput:.1f}/s < 500 requirement"

    @pytest.mark.parametrize("timeframe", OKX_SUPPORTED_TIMEFRAMES)
    async def test_all_timeframes_supported(self, timeframe: str, performance_settings: Settings):
        """Test tất cả timeframes của OKX đều được hỗ trợ bởi MarketDataEngine."""
        event_bus = AsyncMock()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv.return_value = []

        engine = MarketDataEngine(
            exchange=mock_exchange, event_bus=event_bus, settings=performance_settings
        )

        # Kiểm tra timeframe nằm trong TIMEFRAME_SECONDS của engine
        assert timeframe in engine.TIMEFRAME_SECONDS, f"Timeframe {timeframe} không được hỗ trợ"

        # Tạo buffer cho timeframe này
        key = f"BTC-USDT-SWAP_{timeframe}"
        engine.buffers[key] = CandleBuffer("BTC-USDT-SWAP", timeframe)

        # Kiểm tra buffer được tạo
        assert key in engine.buffers, f"Không tạo buffer cho timeframe {timeframe}"

        # Xử lý được candle
        candle = OHLCV(
            symbol="BTC-USDT-SWAP",
            timeframe=timeframe,
            timestamp=int(time.time()),
            open=68000,
            high=68100,
            low=67900,
            close=68050,
            volume=100,
        )
        engine.buffers[key].add_candle(candle)

        # Kiểm tra candle đã vào buffer
        assert len(engine.buffers[key].candles) == 1

    def test_ema_calculation_accuracy_and_speed(self):
        """Test độ chính xác và tốc độ của EMACalculator."""
        # Tạo dữ liệu giá random
        prices = [100.0 + i * 0.5 for i in range(1000)]

        # Đo thời gian tính EMA
        start = time.perf_counter()
        ema9 = EMACalculator.calculate(prices, 9)
        ema21 = EMACalculator.calculate(prices, 21)
        end = time.perf_counter()

        calc_time = (end - start) * 1000
        print(f"EMA calculation time: {calc_time:.2f}ms")
        assert calc_time < 5, f"EMA calculation too slow: {calc_time:.2f}ms"

        # Kiểm tra giá trị EMA hợp lệ
        assert ema9 > 0, "EMA9 không hợp lệ"
        assert ema21 > 0, "EMA21 không hợp lệ"
        assert abs(ema9 - ema21) < 50, "Giá trị EMA quá khác biệt, có thể lỗi tính toán"
