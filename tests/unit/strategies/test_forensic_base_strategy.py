"""
Forensic Tests for BaseStrategy

Tests for forensic bugfixes:
- Timeframe validation (raise RuntimeError for unknown timeframe)
- Silent exception fixed (logger.exception instead of pass)
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from services.strategies.ema_crossover import EMACrossoverStrategy


class TestTimeframeValidation:
    """Test that unknown timeframe raises RuntimeError."""
    
    @pytest.fixture
    def strategy(self):
        """Create EMACrossoverStrategy instance (concrete implementation)."""
        config = Mock()
        config.timeframes = ["5m", "15m", "1H", "4H", "1D", "1W", "1M"]
        strategy = EMACrossoverStrategy(config)
        return strategy
    
    @pytest.mark.asyncio
    async def test_unknown_timeframe_in_get_market_snapshot_raises_error(self, strategy):
        """Unknown timeframe should raise RuntimeError in get_market_snapshot."""
        with patch('core.config.settings') as mock_settings:
            mock_settings.confirmation_candles_5m = 0
            mock_settings.confirmation_candles_15m = 0
            mock_settings.confirmation_candles_1h = 0
            mock_settings.confirmation_candles_4h = 0
            mock_settings.confirmation_candles_1d = 0
            mock_settings.confirmation_candles_1w = 0
            mock_settings.confirmation_candles_1m = 0
            
            with pytest.raises(RuntimeError) as exc_info:
                await strategy.get_market_snapshot("BTC-USDT-SWAP", "UNKNOWN_TF")
            
            assert "UNKNOWN_TIMEFRAME" in str(exc_info.value)


class TestSilentExceptionFixed:
    """Test that silent exception is fixed with logger.exception."""
    
    @pytest.fixture
    def strategy(self):
        """Create EMACrossoverStrategy instance (concrete implementation)."""
        config = Mock()
        config.timeframes = ["5m", "15m", "1H", "4H", "1D", "1W", "1M"]
        strategy = EMACrossoverStrategy(config)
        return strategy
    
    def test_get_candles_exception_logs_exception(self, strategy):
        """Exception in get_candles should log exception."""
        with patch('core.config.settings') as mock_settings:
            mock_settings.confirmation_candles_5m = 0
            mock_settings.confirmation_candles_15m = 0
            mock_settings.confirmation_candles_1h = 0
            mock_settings.confirmation_candles_4h = 0
            mock_settings.confirmation_candles_1d = 0
            mock_settings.confirmation_candles_1w = 0
            mock_settings.confirmation_candles_1m = 0
            
            with patch('core.container.container') as mock_container:
                # Mock container to raise exception
                mock_container.get.side_effect = Exception("Test exception")
                
                # Should log exception but not raise
                result = strategy.get_candles("BTC-USDT-SWAP", "5m", 100)
                
                # Should return empty list after exception
                assert result == []
    
    @pytest.mark.asyncio
    async def test_calculate_indicators_exception_logs_exception(self, strategy):
        """Exception in calculate_indicators should log exception."""
        with patch('core.config.settings') as mock_settings:
            mock_settings.confirmation_candles_5m = 0
            mock_settings.confirmation_candles_15m = 0
            mock_settings.confirmation_candles_1h = 0
            mock_settings.confirmation_candles_4h = 0
            mock_settings.confirmation_candles_1d = 0
            mock_settings.confirmation_candles_1w = 0
            mock_settings.confirmation_candles_1m = 0
            
            with patch('core.container.container') as mock_container:
                # Mock container to raise exception
                mock_container.get.side_effect = Exception("Test exception")
                
                # Should log exception but not raise
                result = await strategy.calculate_indicators("BTC-USDT-SWAP", "5m")
                
                # Should return None after exception
                assert result is None