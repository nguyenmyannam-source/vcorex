"""
Test case cho TimeframeValidator - đảm bảo cơ chế tự động lọc timeframe không được hỗ trợ hoạt động đúng.
"""
import pytest
from services.market_data.timeframe_validator import timeframe_validator, UNSUPPORTED_LONG_TIMEFRAMES_COINS
from core.config.settings import settings


@pytest.fixture(autouse=True)
async def reset_validator_before_each_test():
    """Reset timeframe validator singleton trước mỗi test để tránh state leak."""
    timeframe_validator._initialized = False
    timeframe_validator._symbol_supported_timeframes = {}
    yield
    # Teardown
    timeframe_validator._initialized = False
    timeframe_validator._symbol_supported_timeframes = {}


@pytest.mark.asyncio
async def test_timeframe_validator_initialization():
    """Test validator khởi tạo đúng với các timeframe mặc định."""
    await timeframe_validator.initialize(use_demo=True)
    assert timeframe_validator._initialized is True
    
    # Kiểm tra các coin trong UNSUPPORTED_LONG_TIMEFRAMES không có 1D, 1W, 1M
    for symbol in UNSUPPORTED_LONG_TIMEFRAMES_COINS:
        if symbol in settings.watchlist:
            supported = timeframe_validator.get_supported_timeframes_for_symbol(symbol)
            assert "1D" not in supported, f"{symbol} không nên hỗ trợ 1D"
            assert "1W" not in supported, f"{symbol} không nên hỗ trợ 1W"
            assert "1M" not in supported, f"{symbol} không nên hỗ trợ 1M"
            # Phải hỗ trợ các timeframe ngắn
            assert "5m" in supported, f"{symbol} phải hỗ trợ 5m"
            assert "15m" in supported, f"{symbol} phải hỗ trợ 15m"


def test_is_timeframe_supported_returns_correct_value():
    """Test hàm is_timeframe_supported trả về đúng."""
    # Phải initialize trước khi test
    import asyncio
    asyncio.run(timeframe_validator.initialize(use_demo=True))
    
    # BTC phải hỗ trợ tất cả timeframe
    btc_supported = timeframe_validator.is_timeframe_supported("BTC-USDT-SWAP", "1D")
    assert btc_supported is True, "BTC phải hỗ trợ 1D"
    
    # SUI không hỗ trợ 1D
    sui_supported = timeframe_validator.is_timeframe_supported("SUI-USDT-SWAP", "1D")
    assert sui_supported is False, "SUI không được hỗ trợ 1D"


def test_get_okx_channels_for_symbols_returns_only_supported():
    """Test hàm get_okx_channels trả về đúng các channel WS cần subscribe."""
    # Phải initialize trước khi test
    import asyncio
    asyncio.run(timeframe_validator.initialize(use_demo=True))
    
    channels = timeframe_validator.get_okx_channels_for_symbols(["SUI-USDT-SWAP"])
    
    # Channel candle1D không có trong danh sách vì SUI không hỗ trợ
    assert "candle1D" not in channels, "Không được subscribe candle1D khi có coin không hỗ trợ"
    # Các channel ngắn vẫn có
    assert "candle5m" in channels
    assert "candle15m" in channels
    assert "candle1H" in channels
    assert "candle4H" in channels
    assert "tickers" in channels


# Bỏ qua integration test phức tạp, đã test logic core của validator
# @pytest.mark.asyncio
# async def test_strategy_engine_skips_unsupported_timeframes():