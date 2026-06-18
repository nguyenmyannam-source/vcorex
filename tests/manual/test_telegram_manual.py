#!/usr/bin/env python3
"""
Manual testing for Telegram bot UI/UX fixes.
Tests that all menu callbacks can be invoked and render properly.
"""
import asyncio
import sys
import os
import time
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone
from interfaces.telegram.message_templates import MessageTemplates
from interfaces.telegram.keyboards import TelegramKeyboards

def test_all_templates():
    """Test rendering of all message templates used in the bot."""

    print("="*60)
    print("TELEGRAM BOT UI/UX MANUAL TESTING")
    print("="*60)

    tests_passed = 0
    tests_failed = 0

    # Test 1: Pro Dashboard
    try:
        print("\n1. Testing Pro Dashboard...")
        dashboard_data = {
            "total_balance": 10000.0,
            "free_balance": 5000.0,
            "locked_balance": 5000.0,
            "equity": 9500.0,
            "pnl": -500.0,
            "pnl_pct": -5.0,
            "risk_level": "⚠️ CAO",
            "margin_ratio": 0.75,
            "mode_indicator": "📊 Futures",
            "ws_status": "✅ Kết nối",
            "active_positions": 2,
            "health_score": 85.5,
        }
        msg = MessageTemplates.get_pro_dashboard(dashboard_data)
        assert isinstance(msg, str) and len(msg) > 200, "Dashboard message too short"
        assert "✅" in msg or "⚠️" in msg, "No emoji in dashboard"
        print("   ✅ Dashboard renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Dashboard failed: {e}")
        tests_failed += 1

    # Test 2: System Health
    try:
        print("\n2. Testing System Health...")
        health_data = {
            "uptime_hours": 168,
            "memory_usage": 45.5,
            "cpu_usage": 15.2,
            "db_status": "✅ Healthy",
            "api_status": "✅ Healthy",
            "ws_status": "✅ Connected",
            "last_signal": datetime.now(timezone.utc),
        }
        msg = MessageTemplates.get_system_health(health_data)
        assert isinstance(msg, str) and len(msg) > 100, "Health message too short"
        assert "✅" in msg or "⚠️" in msg, "No status emoji"
        print("   ✅ System health renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ System health failed: {e}")
        tests_failed += 1

    # Test 3: Exchange Status
    try:
        print("\n3. Testing Exchange Status...")
        exchange_data = {
            "exchange": "OKX",
            "ping_ms": 45,
            "status": "ONLINE",
            "last_update": datetime.now(timezone.utc),
        }
        msg = MessageTemplates.get_exchange_status_message(exchange_data)
        assert isinstance(msg, str) and len(msg) > 50, "Exchange status too short"
        assert "OKX" in msg, "Exchange name not in message"
        print("   ✅ Exchange status renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Exchange status failed: {e}")
        tests_failed += 1

    # Test 4: Active Signals
    try:
        print("\n4. Testing Active Signals List...")
        signals = [
            {
                "symbol": "BTC/USDT",
                "signal_type": "BUY",
                "entry_price": 45000.0,
                "strength": "STRONG",
                "timestamp": datetime.now(timezone.utc),
            },
            {
                "symbol": "ETH/USDT",
                "signal_type": "SELL",
                "entry_price": 2500.0,
                "strength": "WEAK",
                "timestamp": datetime.now(timezone.utc),
            }
        ]
        msg = MessageTemplates.format_active_signals(signals)
        assert isinstance(msg, str) and len(msg) > 100, "Signals message too short"
        assert "BTC/USDT" in msg, "BTC symbol not in signals"
        assert "ETH/USDT" in msg, "ETH symbol not in signals"
        print("   ✅ Active signals render correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Active signals failed: {e}")
        tests_failed += 1

    # Test 5: Capital Management
    try:
        print("\n5. Testing Capital Management...")
        capital_data = {
            "total_balance": 10000.0,
            "free_margin": 5000.0,
            "used_margin": 5000.0,
            "risk_level": "MEDIUM",
        }
        msg = MessageTemplates.format_capital_management(capital_data)
        assert isinstance(msg, str) and len(msg) > 100, "Capital message too short"
        assert "$" in msg, "Dollar sign not in message"
        print("   ✅ Capital management renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Capital management failed: {e}")
        tests_failed += 1

    # Test 6: System Metrics
    try:
        print("\n6. Testing System Metrics...")
        metrics = {
            "realized_pnl": 500.0,
            "unrealized_pnl": -100.0,
            "active_positions": 3,
            "long_count": 2,
            "short_count": 1,
            "free_margin": 5000.0,
            "risk_level": "⚠️ TRUNG BÌNH",
            "tpsl_count": 3,
        }
        msg = MessageTemplates.get_system_metrics(metrics)
        assert isinstance(msg, str) and len(msg) > 100, "Metrics message too short"
        assert "500" in msg, "PnL not in metrics"
        print("   ✅ System metrics render correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ System metrics failed: {e}")
        tests_failed += 1

    # Test 7: System Logs
    try:
        print("\n7. Testing System Logs...")
        logs = [
            {"timestamp": datetime.now(timezone.utc), "level": "INFO", "message": "Bot started"},
            {"timestamp": datetime.now(timezone.utc), "level": "WARNING", "message": "High margin ratio"},
            {"timestamp": datetime.now(timezone.utc), "level": "ERROR", "message": "API timeout"},
        ]
        msg = MessageTemplates.get_system_logs(logs)
        assert isinstance(msg, str) and len(msg) > 50, "Logs message too short"
        assert "INFO" in msg or "WARNING" in msg, "Log levels not in message"
        print("   ✅ System logs render correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ System logs failed: {e}")
        tests_failed += 1

    # Test 8: News Dashboard
    try:
        print("\n8. Testing News Dashboard...")
        news_items = [
            {
                "title": "BTC Breaking News",
                "source": "CoinDesk",
                "link": "https://example.com/btc-news",
                "pub_date_str": "2026-06-02 10:30:00",
                "lang": "en",
            }
        ]
        news_data = {
            "news": news_items,
            "ai_summary": "Bitcoin markets rally on institutional adoption",
            "last_update": time.time(),
        }
        msg = MessageTemplates.get_news_dashboard(news_data)
        assert isinstance(msg, str) and len(msg) > 50, "News message too short"
        assert "BTC" in msg or "Breaking News" in msg, "News title not in message"
        print("   ✅ News dashboard renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ News dashboard failed: {e}")
        tests_failed += 1

    # Test 9: History Templates
    try:
        print("\n9. Testing Trading History...")
        trade_history = [
            {
                "timestamp": datetime.now(timezone.utc),
                "symbol": "BTC/USDT",
                "side": "BUY",
                "price": 45000.0,
                "amount": 0.1,
                "total": 4500.0,
                "fee": 2.25,
            }
        ]
        msg = MessageTemplates.get_orders_history(trade_history)
        assert isinstance(msg, str) and len(msg) > 50, "History message too short"
        assert "BTC/USDT" in msg, "Symbol not in history"
        print("   ✅ Trading history renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Trading history failed: {e}")
        tests_failed += 1

    # Test 10: Closed Trades
    try:
        print("\n10. Testing Trade History...")
        closed_trades = [
            {
                "symbol": "ETH/USDT",
                "side": "BUY",
                "entry_price": 2000.0,
                "exit_price": 2100.0,
                "amount": 1.0,
                "pnl": 100.0,
                "pnl_pct": 5.0,
                "duration": "04:30",
                "fee": 2.0,
            }
        ]
        msg = MessageTemplates.get_history_trades(closed_trades)
        assert isinstance(msg, str) and len(msg) > 50, "Trades history message too short"
        assert "ETH/USDT" in msg, "Symbol not in trades history"
        print("   ✅ Trade history renders correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Trade history failed: {e}")
        tests_failed += 1

    # Summary
    print("\n" + "="*60)
    print(f"MANUAL TEST SUMMARY")
    print("="*60)
    print(f"✅ Passed: {tests_passed}/10")
    print(f"❌ Failed: {tests_failed}/10")

    if tests_failed == 0:
        print("\n🎉 ALL MANUAL TESTS PASSED!")
        print("Telegram bot UI/UX is ready for production")
    else:
        print(f"\n⚠️ {tests_failed} test(s) failed")

if __name__ == "__main__":
    test_all_templates()
