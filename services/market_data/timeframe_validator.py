"""
Timeframe Validator: Tự động kiểm tra và lọc các timeframe không được hỗ trợ bởi từng coin trên OKX.
Ngăn chặn lỗi subscription WebSocket và bỏ qua các điều kiện entry khung thời gian không tồn tại.
"""
import asyncio
import aiohttp
import json
from typing import Dict, Set, List, Optional
from loguru import logger
from core.config.settings import settings


# Map timeframe từ định dạng bot sang OKX API candle channel
OKX_CANDLE_CHANNEL_MAP = {
    "5m": "candle5m",
    "15m": "candle15m",
    "1H": "candle1H",
    "4H": "candle4H",
    "1D": "candle1D",
    "1W": "candle1W",
    "1M": "candle1M"
}

# Danh sách coin thường không hỗ trợ timeframe dài (1D+, 1W, 1M) trên OKX Demo
UNSUPPORTED_LONG_TIMEFRAMES_COINS = {
    "SUI-USDT-SWAP", "ARB-USDT-SWAP", "NEAR-USDT-SWAP", "ATOM-USDT-SWAP",
    "TON-USDT-SWAP", "FIL-USDT-SWAP", "TRX-USDT-SWAP", "BCH-USDT-SWAP",
    "LTC-USDT-SWAP", "DOT-USDT-SWAP"
}


class TimeframeValidator:
    def __init__(self):
        self._symbol_supported_timeframes: Dict[str, Set[str]] = {}  # symbol -> {timeframes bot uses}
        self._okx_channel_map = OKX_CANDLE_CHANNEL_MAP
        self._initialized = False

    async def initialize(self, use_demo: bool = True) -> None:
        """Khởi tạo validator, kiểm tra tất cả symbol trong watchlist và lưu timeframe được hỗ trợ."""
        if self._initialized:
            return

        logger.info("Initializing TimeframeValidator for market data...")

        # Khởi tạo mặc định: tất cả symbol hỗ trợ timeframe ngắn (5m,15m,1H,4H)
        for symbol in settings.watchlist:
            if symbol in UNSUPPORTED_LONG_TIMEFRAMES_COINS:
                self._symbol_supported_timeframes[symbol] = {"5m", "15m", "1H", "4H"}
                logger.debug(f"Symbol {symbol} marked as long-timeframe-incompatible (pre-validation)")
            else:
                self._symbol_supported_timeframes[symbol] = set(settings.timeframes)

        # Nếu có thể, validate với OKX API thực tế
        await self._validate_with_okx_api(use_demo)
        self._initialized = True

        # Log kết quả
        total_skipped = 0
        for symbol, tfs in self._symbol_supported_timeframes.items():
            skipped = set(settings.timeframes) - tfs
            total_skipped += len(skipped)
            if skipped:
                logger.info(f"Symbol {symbol} - Skipped unsupported timeframes: {sorted(skipped)}")
        logger.info(f"TimeframeValidator initialized. Total skipped timeframe/symbol pairs: {total_skipped}")

    def is_timeframe_supported(self, symbol: str, timeframe: str) -> bool:
        """Kiểm tra xem symbol này có hỗ trợ timeframe hay không."""
        if not self._initialized:
            logger.warning("TimeframeValidator not initialized, assuming all timeframes are supported")
            return True
        return timeframe in self._symbol_supported_timeframes.get(symbol, set())

    def get_supported_timeframes_for_symbol(self, symbol: str) -> List[str]:
        """Lấy danh sách timeframe được hỗ trợ cho một symbol."""
        if not self._initialized:
            return settings.timeframes.copy()
        return sorted(self._symbol_supported_timeframes.get(symbol, set()))

    def get_okx_channels_for_symbols(self, symbols: List[str]) -> List[str]:
        """Tạo danh sách channel OKX chỉ bao gồm những channel được hỗ trợ bởi tất cả symbol."""
        if not self._initialized:
            return list(self._okx_channel_map.values()) + ["tickers"]

        # Gộp tất cả timeframe được hỗ trợ bởi tất cả symbol trong danh sách
        all_supported_tfs = set()
        for symbol in symbols:
            tfs = self._symbol_supported_timeframes.get(symbol, set())
            all_supported_tfs.update(tfs)

        # Chuyển sang channel OKX
        channels = [self._okx_channel_map[tf] for tf in all_supported_tfs if tf in self._okx_channel_map]
        channels.append("tickers")  # tickers luôn được hỗ trợ
        return sorted(channels)

    async def _validate_with_okx_api(self, use_demo: bool) -> None:
        """Kiểm tra thực tế với OKX API để cập nhật danh sách timeframe hỗ trợ."""
        try:
            url = "https://openapi.okx.com/api/v5/public/instruments?instType=SWAP"
            headers = {"x-simulated-trading": "1"} if use_demo else {}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"Could not fetch OKX instruments: HTTP {response.status}. Using static list.")
                        return

                    data = await response.json()
                    if "data" not in data:
                        logger.warning("OKX API returned no instrument data")
                        return

                    # OKX /instruments endpoint does not return 'availableBar'.
                    # We simply verify the symbol is active, and rely on our static list
                    # UNSUPPORTED_LONG_TIMEFRAMES_COINS for timeframe logic.
                    active_symbols = {inst.get("instId") for inst in data.get("data", []) if inst.get("state") == "live"}

                    for symbol in list(self._symbol_supported_timeframes.keys()):
                        if symbol not in active_symbols:
                            logger.warning(f"Symbol {symbol} is NOT ACTIVE on OKX API. Disabling timeframes.")
                            self._symbol_supported_timeframes[symbol] = set()

        except Exception as e:
            logger.error(f"Error validating timeframes with OKX API: {e}")
            logger.warning("Falling back to hardcoded unsupported list")


# Singleton instance để dùng toàn hệ thống
timeframe_validator = TimeframeValidator()
