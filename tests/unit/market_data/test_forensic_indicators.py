"""
Forensic Tests for IndicatorPipeline

Tests for forensic bugfixes:
- EMA50, EMA200, ema9_above_ema21 removed
- Confirmation validation (only 0 or 1 allowed)
- Hidden fallback removed (raise RuntimeError for invalid reference_candle_index)
"""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from services.market_data.indicators import IndicatorPipeline, EMACalculator, ADXCalculator
from services.market_data.snapshot import MarketSnapshot


class TestEMA50EMA200Removed:
    """Test that EMA50, EMA200, ema9_above_ema21 are removed."""
    
    def test_ema50_not_in_calculators(self):
        """EMA50 should not be in calculators dict."""
        pipeline = IndicatorPipeline()
        assert "ema50" not in pipeline.calculators
        assert "ema200" not in pipeline.calculators
    
    def test_ema50_not_in_compute_indicators(self):
        """EMA50 should not be computed in compute_indicators."""
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        mock_candle.close = 100.0
        mock_candle.open = 99.0
        mock_candle.high = 105.0
        mock_candle.low = 95.0
        mock_candle.timestamp = 1620000000000
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert "ema50" not in snapshot.indicators
        assert "ema200" not in snapshot.indicators
    
    def test_ema9_above_ema21_not_in_compute_indicators(self):
        """ema9_above_ema21 should not be computed in compute_indicators."""
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        mock_candle.close = 100.0
        mock_candle.open = 99.0
        mock_candle.high = 105.0
        mock_candle.low = 95.0
        mock_candle.timestamp = 1620000000000
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert "ema9_above_ema21" not in snapshot.indicators


class TestConfirmationValidation:
    """Test that confirmation_candles only allows 1 (REALTIME MODE disabled in production)."""
    
    def test_confirmation_candles_0_raises_error(self):
        """confirmation_candles=0 should be rejected (REALTIME MODE disabled)."""
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=0)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=0" in str(exc_info.value)
    
    def test_confirmation_candles_1_allowed(self):
        """confirmation_candles=1 should work (CONFIRMATION MODE - only supported mode)."""
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        mock_candle.close = 100.0
        mock_candle.open = 99.0
        mock_candle.high = 105.0
        mock_candle.low = 95.0
        mock_candle.timestamp = 1620000000000
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        snapshot = pipeline.compute_indicators(buffer, confirmation_candles=1)
        
        assert snapshot.reference_candle_index == -2
        assert snapshot.candle_type == "closed"
    
    def test_confirmation_candles_2_raises_error(self):
        """confirmation_candles=2 should raise RuntimeError."""
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=2)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=2" in str(exc_info.value)
    
    def test_confirmation_candles_negative_raises_error(self):
        """confirmation_candles=-1 should raise RuntimeError."""
        pipeline = IndicatorPipeline()
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=[])
        
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=-1)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=-1" in str(exc_info.value)


class TestHiddenFallbackRemoved:
    """Test that hidden fallback is removed and raises RuntimeError."""
    
    def test_invalid_confirmation_candles_raises_error(self):
        """Invalid confirmation_candles should raise RuntimeError."""
        # reference_candle_index is now hardcoded to -2, only confirmation_candles=1 is allowed
        pipeline = IndicatorPipeline()
        mock_candle = Mock()
        mock_candle.is_closed = True
        mock_candle.close = 100.0
        mock_candle.open = 99.0
        mock_candle.high = 105.0
        mock_candle.low = 95.0
        mock_candle.timestamp = 1620000000000
        candles = [mock_candle] * 10
        
        buffer = Mock()
        buffer.symbol = "BTC-USDT-SWAP"
        buffer.timeframe = "5m"
        buffer.get_close_prices = Mock(return_value=[100.0] * 500)
        buffer.get_high_prices = Mock(return_value=[105.0] * 500)
        buffer.get_low_prices = Mock(return_value=[95.0] * 500)
        buffer.get_candles = Mock(return_value=candles)
        
        # Invalid case: confirmation_candles=0 should raise error
        with pytest.raises(RuntimeError) as exc_info:
            pipeline.compute_indicators(buffer, confirmation_candles=0)
        
        assert "UNSUPPORTED_CONFIRMATION_CANDLES=0" in str(exc_info.value)


class TestIndicatorBundleMismatchIntact:
    """Test that indicator_bundle_mismatch validation is still intact."""
    
    def test_indicator_bundle_mismatch_validation_exists(self):
        """Indicator bundle mismatch validation should still exist."""
        import inspect
        from services.market_data.indicators import IndicatorPipeline
        
        source = inspect.getsource(IndicatorPipeline.compute_indicators)
        
        assert "INDICATOR_BUNDLE_MISMATCH" in source
        assert "ema_actual_index" in source
        assert "adx_actual_index" in source
        assert "body_actual_index" in source
        assert "price_actual_index" in source
        assert "signal_actual_index" in source