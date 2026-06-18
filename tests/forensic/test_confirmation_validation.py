"""
Test Confirmation Validation

Protects production hardening fix: confirmation_candles only allows values >= 1.
REALTIME MODE (confirmation_candles=0) is permanently disabled in production.

Allowed:
- confirmation_candles = 1 (CONFIRMATION MODE - only supported mode in production)

Rejected:
- 0, -1, 2, 3, 99 (any other value)

Must raise RuntimeError with "UNSUPPORTED_CONFIRMATION_CANDLES".
"""

import pytest
from unittest.mock import Mock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from services.market_data.indicators import IndicatorPipeline


class TestConfirmationValidation:
    """Test confirmation_candles validation."""
    
    @pytest.fixture
    def pipeline(self):
        """Create IndicatorPipeline instance."""
        return IndicatorPipeline()
    
    @pytest.fixture
    def buffer(self):
        """Create mock buffer with valid closed candles that have all required attributes."""
        mock_candle = Mock()
        mock_candle.is_closed = True
        mock_candle.close = 100.0
        mock_candle.open = 99.0
        mock_candle.high = 105.0
        mock_candle.low = 95.0
        mock_candle.timestamp = 1620000000000
        candles = [mock_candle] * 10  # At least 2 closed candles to pass validation
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        return buffer
    
    def test_confirmation_candles_0_rejected(self, pipeline, buffer):
        """confirmation_candles=0 should be rejected (REALTIME MODE disabled)."""
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=0)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=0" in str(exc_info.value)
    
    def test_confirmation_candles_1_allowed(self, pipeline, buffer):
        """confirmation_candles=1 should be allowed (CONFIRMATION MODE)."""
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert snapshot is not None
        assert snapshot.reference_candle_index == -2
        assert snapshot.candle_type == "closed"
    
    def test_confirmation_candles_negative_one_rejected(self, pipeline, buffer):
        """confirmation_candles=-1 should be rejected."""
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=-1)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=-1" in str(exc_info.value)
    
    def test_confirmation_candles_2_rejected(self, pipeline, buffer):
        """confirmation_candles=2 should be rejected."""
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=2)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=2" in str(exc_info.value)
    
    def test_confirmation_candles_3_rejected(self, pipeline, buffer):
        """confirmation_candles=3 should be rejected."""
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=3)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=3" in str(exc_info.value)
    
    def test_confirmation_candles_99_rejected(self, pipeline, buffer):
        """confirmation_candles=99 should be rejected."""
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=99)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=99" in str(exc_info.value)