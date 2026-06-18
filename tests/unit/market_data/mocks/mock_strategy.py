"""
Mock strategy cho test TimeframeValidator
"""
from datetime import datetime
from typing import List, Dict, Optional
from services.strategies.base_strategy import BaseStrategy, StrategyConfig, Signal, SignalType
from domain.models.signals import SignalStrength


class MockStrategy(BaseStrategy):
    """Mock strategy cho test."""
    def __init__(self, name: str, symbols: List[str], timeframes: List[str]):
        self.config = StrategyConfig(
            name=name,
            enabled=True,
            symbols=symbols,
            timeframes=timeframes,
            min_confidence=SignalStrength.MEDIUM
        )
        self._candles: Dict[str, Dict[str, List]] = {}  # symbol -> timeframe -> candles
        
    def get_candles(self, symbol: str, timeframe: str, limit: int = 50) -> List:
        return self._candles.get(symbol, {}).get(timeframe, [])
        
    async def update_candle_data(self, symbol: str, timeframe: str, new_candles: List) -> None:
        if symbol not in self._candles:
            self._candles[symbol] = {}
        self._candles[symbol][timeframe] = new_candles
        
    async def generate_signal(self, symbol: str, timeframe: str) -> Optional[Signal]:
        """Mock luôn trả về HOLD để test không gây lỗi."""
        return Signal(
            symbol=symbol,
            timeframe=timeframe,
            signal_type=SignalType.HOLD,
            timestamp=datetime.utcnow(),
            confidence=SignalStrength.LOW,
            price=0.0
        )