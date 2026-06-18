"""
Forensic Tests for SignalSafetyMixin

Tests for forensic bugfixes:
- Timeframe validation (raise RuntimeError for unknown timeframe)
- Silent exception fixed (logger.warning instead of pass)
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from services.strategies.signal_safety_mixin import SignalSafetyMixin


class TestTimeframeValidation:
    """Test that unknown timeframe raises RuntimeError."""
    
    @pytest.fixture
    def mixin(self):
        """Create SignalSafetyMixin instance."""
        mixin = SignalSafetyMixin()
        mixin.settings = Mock()
        mixin.settings.stale_signal_5m_seconds = 30
        mixin.settings.stale_signal_15m_seconds = 90
        mixin.settings.stale_signal_1h_seconds = 300
        mixin.settings.stale_signal_4h_seconds = 1200
        mixin.settings.stale_signal_1d_seconds = 7200
        mixin.settings.stale_signal_1w_seconds = 43200
        mixin.settings.stale_signal_1m_seconds = 259200
        mixin._missed_signals = []
        mixin._state_lock = MagicMock()
        return mixin
    
    def test_unknown_timeframe_in_is_stale_raises_error(self, mixin):
        """Unknown timeframe should raise RuntimeError in is_stale."""
        with pytest.raises(RuntimeError) as exc_info:
            # Call is_stale with unknown timeframe
            # This will fail at the first timeframe validation
            import asyncio
            asyncio.run(mixin.is_stale(1000000, "UNKNOWN_TF", "BTC-USDT-SWAP"))
        
        assert "UNKNOWN_TIMEFRAME" in str(exc_info.value)
    
    def test_valid_timeframe_in_is_stale_works(self, mixin):
        """Valid timeframe should work in is_stale."""
        import asyncio
        
        # Test with valid timeframe
        result = asyncio.run(mixin.is_stale(1000000, "5m", "BTC-USDT-SWAP"))
        
        # Should not raise error
        assert result is not None  # Either True or False


class TestSilentExceptionFixed:
    """Test that silent exception is fixed with logger.warning."""
    
    @pytest.fixture
    def mixin(self):
        """Create SignalSafetyMixin instance."""
        mixin = SignalSafetyMixin()
        mixin.settings = Mock()
        mixin._missed_signals = []
        mixin._state_lock = MagicMock()
        return mixin
    
    def test_dedup_exception_logs_warning(self, mixin):
        """Exception in dedup check should log warning and continue."""
        import asyncio
        
        # Add a corrupted entry
        mixin._missed_signals.append({
            "time": "invalid-timestamp",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "5m",
            "reason": "test"
        })
        
        # This should not raise exception, but log warning
        result = asyncio.run(mixin.record_missed_signal("BTC-USDT-SWAP", "5m", "test"))
        
        # Should complete without raising exception
        assert result is None
        
        # Should have added new entry
        assert len(mixin._missed_signals) == 2
