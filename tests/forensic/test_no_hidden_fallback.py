"""
Test No Hidden Fallback

Protects forensic fix: Hidden fallback removed in indicators.py.

Check services/market_data/indicators.py:
- Should NOT contain "# Fallback: use all candles"
- Should NOT contain fallback assignment:
  adx_highs = highs
  adx_lows = lows
  adx_closes = closes
"""

import pytest
import re
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


class TestNoHiddenFallback:
    """Test that hidden fallback is removed in indicators.py."""
    
    def test_indicators_no_fallback_comment(self):
        """indicators.py should not contain '# Fallback: use all candles'."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/market_data/indicators.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for fallback comment
        assert "# Fallback: use all candles" not in content, \
            "indicators.py should not contain '# Fallback: use all candles'"
    
    def test_indicators_no_fallback_assignment(self):
        """indicators.py should not contain fallback assignment pattern."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/market_data/indicators.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for specific ADX fallback pattern with comment
        # Pattern: else: followed by comment about fallback and assignment
        pattern = r'else:\s*#.*[Ff]allback.*\s*adx_highs = highs\s*adx_lows = lows\s*adx_closes = closes'
        matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)
        
        assert len(matches) == 0, \
            f"indicators.py should not contain ADX fallback assignment pattern"
    
    def test_indicators_has_runtime_error_for_invalid_index(self):
        """indicators.py should raise RuntimeError for invalid reference_candle_index."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/market_data/indicators.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for RuntimeError with INVALID_REFERENCE_CANDLE_INDEX
        assert "INVALID_REFERENCE_CANDLE_INDEX" in content, \
            "indicators.py should raise RuntimeError for invalid reference_candle_index"
        
        # Check for raise RuntimeError
        assert "raise RuntimeError" in content, \
            "indicators.py should raise RuntimeError"
    
    def test_indicators_has_logger_error_for_invalid_index(self):
        """indicators.py should log error for invalid reference_candle_index."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/market_data/indicators.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for logger.error with INVALID_REFERENCE_CANDLE_INDEX
        assert "logger.error" in content and "INVALID_REFERENCE_CANDLE_INDEX" in content, \
            "indicators.py should log error for invalid reference_candle_index"
