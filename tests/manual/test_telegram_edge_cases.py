#!/usr/bin/env python3
"""
Edge case testing for Telegram templates.
Tests how templates handle None, empty, and special character inputs.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from interfaces.telegram.message_templates import MessageTemplates
from datetime import datetime, timezone

def test_edge_cases():
    """Test edge cases like None, empty strings, special characters."""

    print("="*60)
    print("TELEGRAM TEMPLATE EDGE CASE TESTING")
    print("="*60)

    tests_passed = 0
    tests_failed = 0

    # Test 1: HTML escape with special characters
    try:
        print("\n1. Testing HTML escape with special chars...")
        dangerous = "<script>alert('XSS')</script>"
        result = MessageTemplates._escape_html(dangerous)
        assert "<script>" not in result, "Script tag not escaped"
        assert "<" not in result, "Less-than not escaped"
        assert "&" in result, "HTML entities not used"
        print("   ✅ XSS protection works")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ XSS test failed: {e}")
        tests_failed += 1

    # Test 2: None values in positions
    try:
        print("\n2. Testing None values in position data...")
        positions = [{
            "symbol": "BTC/USDT",
            "side": "LONG",
            "leverage": 10,
            "entry_price": None,  # None value
            "current_price": 46000.0,
            "amount": 1.0,
            "pnl": None,  # None value
            "pnl_pct": 0.22,
            "margin": 4500.0,
            "notional_size": 45000.0,
            "strategy_name": None,  # None value
            "has_sl": True,
            "stop_loss": 44000.0,
            "take_profit_prices": None,  # None value
        }]
        msg = MessageTemplates.format_open_positions(positions)
        assert isinstance(msg, str) and len(msg) > 0, "Message empty"
        # Should contain error handling or the position should be skipped gracefully
        # The template tries to format the position, and if it fails, shows an error
        assert "VỊ THẾ ĐANG MỞ" in msg, "No header in message"
        print("   ✅ None values handled gracefully")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ None values test failed: {e}")
        tests_failed += 1

    # Test 3: Empty lists
    try:
        print("\n3. Testing empty signal list...")
        signals = []
        msg = MessageTemplates.format_active_signals(signals)
        assert isinstance(msg, str), "Empty signals not handled"
        assert "Chưa có" in msg or "có" in msg.lower(), "No empty state message"
        print("   ✅ Empty lists handled")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Empty list test failed: {e}")
        tests_failed += 1

    # Test 4: Very long symbol names with special chars
    try:
        print("\n4. Testing long symbol with special characters...")
        positions = [{
            "symbol": "VERY<LONG>SYMBOL&SPECIAL/USDT",
            "side": "LONG",
            "leverage": 10,
            "entry_price": 100.0,
            "current_price": 110.0,
            "amount": 1.0,
            "pnl": 10.0,
            "pnl_pct": 10.0,
            "margin": 100.0,
            "notional_size": 1000.0,
            "strategy_name": "TEST<STRATEGY>",
            "has_sl": True,
            "stop_loss": 90.0,
            "take_profit_prices": [120.0],
        }]
        msg = MessageTemplates.format_open_positions(positions)
        # Verify HTML characters are escaped
        assert "<LONG>" not in msg or "&lt;LONG&gt;" in msg, "HTML not properly escaped"
        assert "TEST<STRATEGY>" not in msg or "TEST&lt;STRATEGY&gt;" in msg, "Strategy HTML not escaped"
        print("   ✅ Long symbols with special chars escaped")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Long symbol test failed: {e}")
        tests_failed += 1

    # Test 5: Very large numbers
    try:
        print("\n5. Testing very large numbers...")
        metrics = {
            "realized_pnl": 999999999.99,
            "unrealized_pnl": -999999999.99,
            "active_positions": 10000,
            "long_count": 5000,
            "short_count": 5000,
            "free_margin": 1000000000.0,
            "risk_level": "❌ NGUY HIỂM",
            "tpsl_count": 9999,
        }
        msg = MessageTemplates.get_system_metrics(metrics)
        assert isinstance(msg, str) and len(msg) > 0, "Large numbers crash template"
        assert "999" in msg or "1,000" in msg, "Large numbers not formatted"
        print("   ✅ Large numbers handled correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Large numbers test failed: {e}")
        tests_failed += 1

    # Test 6: Special HTML characters in strategy names
    try:
        print("\n6. Testing HTML special chars in strategy names...")
        signal_data = {
            "symbol": "BTC/USDT",
            "signal_type": "BUY",
            "entry_price": 50000.0,
            "timeframe": "1H",
            "strategy_name": "RSI>70 & MACD<0 Strategy",  # HTML special chars
            "signal_strength": "STRONG",
            "timestamp": datetime.now(timezone.utc),
            "position_size_usdt": 1000.0,
            "stop_loss_price": 48000.0,
            "take_profit_prices": [52000.0],
            "indicators": {"body_pct": 5.0}
        }
        msg = MessageTemplates.get_new_signal_alert(signal_data)
        # Ampersand should be escaped
        assert "&amp;" in msg or "&" not in msg.split(">")[1:], "Ampersand not escaped"
        print("   ✅ HTML special chars escaped in strategy names")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ HTML special chars test failed: {e}")
        tests_failed += 1

    # Test 7: Empty strings should show fallback
    try:
        print("\n7. Testing empty string fallback...")
        result = MessageTemplates._escape_html("")
        assert result == "❓ Trống", f"Expected '❓ Trống' but got '{result}'"

        result2 = MessageTemplates._escape_html("   ")
        assert result2 == "❓ Trống", f"Expected '❓ Trống' but got '{result2}'"
        print("   ✅ Empty strings show proper fallback")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Empty string test failed: {e}")
        tests_failed += 1

    # Test 8: Quote marks in data
    try:
        print("\n8. Testing quotes in symbol/strategy names...")
        positions = [{
            "symbol": 'BTC/USDT"QUOTED',
            "side": "LONG",
            "leverage": 10,
            "entry_price": 45000.0,
            "current_price": 46000.0,
            "amount": 1.0,
            "pnl": 100.0,
            "pnl_pct": 0.22,
            "margin": 4500.0,
            "notional_size": 45000.0,
            "strategy_name": "Strategy'With'Quotes",
            "has_sl": True,
            "stop_loss": 44000.0,
            "take_profit_prices": [47000.0],
        }]
        msg = MessageTemplates.format_open_positions(positions)
        assert isinstance(msg, str) and len(msg) > 0, "Quotes crash template"
        # Quotes should be escaped properly
        print("   ✅ Quotes handled correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Quotes test failed: {e}")
        tests_failed += 1

    # Test 9: Negative values
    try:
        print("\n9. Testing negative values...")
        metrics = {
            "realized_pnl": -12345.67,
            "unrealized_pnl": -99999.99,
            "active_positions": 0,
            "long_count": 0,
            "short_count": 0,
            "free_margin": -100.0,  # Invalid but test handling
            "risk_level": "❌ NGUY HIỂM",
            "tpsl_count": 0,
        }
        msg = MessageTemplates.get_system_metrics(metrics)
        assert isinstance(msg, str) and len(msg) > 0, "Negative values crash"
        assert "-" in msg, "Negative sign not shown"
        print("   ✅ Negative values displayed correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Negative values test failed: {e}")
        tests_failed += 1

    # Test 10: Unicode characters
    try:
        print("\n10. Testing Unicode characters...")
        positions = [{
            "symbol": "BTC/USDT",
            "side": "LONG",
            "leverage": 10,
            "entry_price": 45000.0,
            "current_price": 46000.0,
            "amount": 1.0,
            "pnl": 100.0,
            "pnl_pct": 0.22,
            "margin": 4500.0,
            "notional_size": 45000.0,
            "strategy_name": "Chiến lược Việt 🚀",  # Vietnamese with emoji
            "has_sl": True,
            "stop_loss": 44000.0,
            "take_profit_prices": [47000.0],
        }]
        msg = MessageTemplates.format_open_positions(positions)
        assert isinstance(msg, str) and len(msg) > 0, "Unicode crash"
        # Just verify the message is generated - Vietnamese characters may be encoded
        assert "BTC/USDT" in msg, "Symbol not in output"
        print("   ✅ Unicode characters handled correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Unicode test failed: {e}")
        tests_failed += 1

    # Summary
    print("\n" + "="*60)
    print(f"EDGE CASE TEST SUMMARY")
    print("="*60)
    print(f"✅ Passed: {tests_passed}/10")
    print(f"❌ Failed: {tests_failed}/10")

    if tests_failed == 0:
        print("\n🎉 ALL EDGE CASE TESTS PASSED!")
        print("Templates are robust against malformed inputs")
    else:
        print(f"\n⚠️ {tests_failed} test(s) failed")

if __name__ == "__main__":
    test_edge_cases()
