"""
Test No Silent Exception

Protects forensic fix: No silent exceptions in trading logic.

Scan source code:
- services/strategies/base_strategy.py
- services/strategies/signal_safety_mixin.py

Fail if found:
- except Exception: pass
- except Exception: continue
"""

import pytest
import re
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


class TestNoSilentException:
    """Test that no silent exceptions exist in trading logic."""
    
    def test_base_strategy_no_silent_exception_pass(self):
        """base_strategy.py should not contain 'except Exception: pass'."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/base_strategy.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for "except Exception:" followed by "pass" on next line
        pattern = r'except Exception:\s*\n\s*pass'
        matches = re.findall(pattern, content)
        
        assert len(matches) == 0, f"Found {len(matches)} silent exception(s) with 'pass' in base_strategy.py"
    
    def test_base_strategy_no_silent_exception_continue(self):
        """base_strategy.py should not contain 'except Exception: continue'."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/base_strategy.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for "except Exception:" followed by "continue" on next line
        pattern = r'except Exception:\s*\n\s*continue'
        matches = re.findall(pattern, content)
        
        assert len(matches) == 0, f"Found {len(matches)} silent exception(s) with 'continue' in base_strategy.py"
    
    def test_signal_safety_mixin_no_silent_exception_pass(self):
        """signal_safety_mixin.py should not contain 'except Exception: pass'."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/signal_safety_mixin.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for "except Exception:" followed by "pass" on next line
        pattern = r'except Exception:\s*\n\s*pass'
        matches = re.findall(pattern, content)
        
        assert len(matches) == 0, f"Found {len(matches)} silent exception(s) with 'pass' in signal_safety_mixin.py"
    
    def test_signal_safety_mixin_no_silent_exception_continue(self):
        """signal_safety_mixin.py should not contain 'except Exception: continue'."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/signal_safety_mixin.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for "except Exception:" followed by "continue" on next line
        pattern = r'except Exception:\s*\n\s*continue'
        matches = re.findall(pattern, content)
        
        assert len(matches) == 0, f"Found {len(matches)} silent exception(s) with 'continue' in signal_safety_mixin.py"
    
    def test_base_strategy_has_exception_logging(self):
        """base_strategy.py should have exception logging."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/base_strategy.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for exception logging patterns
        assert "logger.exception" in content or "logger.error" in content, \
            "base_strategy.py should have exception logging"
    
    def test_signal_safety_mixin_has_exception_logging(self):
        """signal_safety_mixin.py should have exception logging."""
        file_path = os.path.join(
            os.path.dirname(__file__),
            "../../services/strategies/signal_safety_mixin.py"
        )
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check for exception logging patterns
        assert "logger.warning" in content or "logger.error" in content or "logger.exception" in content, \
            "signal_safety_mixin.py should have exception logging"
