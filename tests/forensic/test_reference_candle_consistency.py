"""
Test Reference Candle Consistency

Protects forensic fix: All indicators use the same reference candle.

REALTIME MODE (confirmation_candles=0):
- EMA, ADX, BODY, PRICE, SIGNAL all use candle[-1]
- All actual_index == -1

CONFIRMATION MODE (confirmation_candles=1):
- EMA, ADX, BODY, PRICE, SIGNAL all use candle[-2]
- All actual_index == -2
"""

import pytest
from unittest.mock import Mock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from services.market_data.indicators import IndicatorPipeline


class TestConfirmationModeConsistency:
    """Test CONFIRMATION MODE (confirmation_candles=1) consistency."""
    
    @pytest.fixture
    def pipeline(self):
        """Create IndicatorPipeline instance."""
        return IndicatorPipeline()
    
    @pytest.fixture
    def buffer(self):
        """Create mock buffer."""
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=[])
        return buffer
    
    def test_confirmation_mode_all_actual_index_minus_two(self, pipeline, buffer):
        """CONFIRMATION MODE: All actual_index should be -2."""
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert snapshot.indicators["ema_actual_index"] == -2
        assert snapshot.indicators["adx_actual_index"] == -2
        assert snapshot.indicators["body_actual_index"] == -2
        assert snapshot.indicators["price_actual_index"] == -2
        assert snapshot.indicators["signal_actual_index"] == -2
    
    def test_confirmation_mode_reference_candle_index_minus_two(self, pipeline, buffer):
        """CONFIRMATION MODE: reference_candle_index should be -2."""
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert snapshot.reference_candle_index == -2
    
    def test_confirmation_mode_candle_type_closed(self, pipeline, buffer):
        """CONFIRMATION MODE: candle_type should be closed."""
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert snapshot.candle_type == "closed"
