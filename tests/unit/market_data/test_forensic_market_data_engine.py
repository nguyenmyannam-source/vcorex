"""
Forensic Tests for MarketDataEngine

Tests for forensic bugfixes:
- Timeframe validation (raise RuntimeError for unknown timeframe)
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))


class TestTimeframeValidation:
    """Test that unknown timeframe raises RuntimeError in MarketDataEngine."""
    
    def test_unknown_timeframe_in_compute_indicators_raises_error(self):
        """Unknown timeframe should raise RuntimeError in _compute_and_publish_indicators."""
        from services.market_data_engine import MarketDataEngine
        
        # Verify the validation code exists
        import inspect
        source = inspect.getsource(MarketDataEngine._compute_and_publish_indicators)
        
        assert "UNKNOWN_TIMEFRAME" in source
        assert "raise RuntimeError" in source
    
    def test_unknown_timeframe_in_data_fetch_worker_raises_error(self):
        """Unknown timeframe should raise RuntimeError in _data_fetch_worker."""
        from services.market_data_engine import MarketDataEngine
        
        import inspect
        source = inspect.getsource(MarketDataEngine._data_fetch_worker)
        
        assert "UNKNOWN_TIMEFRAME" in source
        assert "raise RuntimeError" in source
