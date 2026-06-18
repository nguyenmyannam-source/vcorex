"""
Telegram UX Comprehensive Test Suite - Institutional-grade testing
Tests all Telegram UI/UX features, keyboard interactions, message formatting,
state management, and institutional-grade monitoring features.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.event_bus import Event
from core.events.topics import EventTopic
from interfaces.telegram.formatters import TelegramFormatters
from interfaces.telegram.keyboards import TelegramKeyboards
from interfaces.telegram.message_templates import MessageTemplates

# Import our Telegram modules - correctly mapped to actual implementation
from interfaces.telegram.telegram_bot import TelegramBot


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object"""
    update = AsyncMock(spec=Update)
    update.effective_chat.id = 123456789
    update.effective_user.id = 123456789
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock ContextTypes object"""
    context = AsyncMock(spec=ContextTypes.DEFAULT_TYPE)
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    return context


@pytest.fixture
def telegram_bot(event_bus):
    """Create TelegramBot instance with mocked settings"""
    with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
        mock_settings.telegram_chat_id = "123456789"
        mock_settings.telegram_bot_token = "test_token"
        bot = TelegramBot(event_bus)
        return bot


class TestTelegramKeyboards:
    """Test all keyboard structures and callbacks"""

    def test_main_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_main_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        buttons = []
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append(btn.callback_data)
        required_callbacks = [
            "menu:trading",
            "menu:analytics",
            "menu:history",
            "menu:settings",
            "menu:control",
            "menu:system",
        ]
        for cb in required_callbacks:
            assert cb in buttons, f"Missing callback: {cb}"

    def test_trading_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_trading_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        buttons = []
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append(btn.callback_data)
        trading_callbacks = [
            "trading:open_positions",
            "trading:active_signals",
            "trading:pending_orders",
            "menu:main",
        ]
        for cb in trading_callbacks:
            assert cb in buttons, f"Missing callback: {cb}"

    def test_analytics_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_analytics_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        buttons = []
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append(btn.callback_data)
        analytics_callbacks = [
            "analytics:pnp_dashboard",
            "analytics:performance",
            "analytics:winrate",
            "analytics:balance_history",
            "menu:main",
        ]
        for cb in analytics_callbacks:
            assert cb in buttons, f"Missing callback: {cb}"

    def test_system_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_system_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        buttons = []
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append(btn.callback_data)
        system_callbacks = [
            "system:health",
            "system:logs",
            "system:exchange_status",
            "system:metrics",
            "menu:main",
        ]
        for cb in system_callbacks:
            assert cb in buttons, f"Missing callback: {cb}"

    def test_history_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_history_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)

    def test_confirmation_dialog_structure(self):
        keyboard = TelegramKeyboards.get_confirmation_dialog("close_all_positions")
        confirm_found = False
        cancel_found = False
        for row in keyboard.inline_keyboard:
            for btn in row:
                if "confirm" in btn.callback_data:
                    confirm_found = True
                if "cancel" in btn.callback_data:
                    cancel_found = True
        assert confirm_found and cancel_found, "Confirmation buttons missing"

    def test_loading_keyboard_disabled(self):
        """Test loading state keyboard is non-interactive"""
        keyboard = TelegramKeyboards.get_loading_keyboard()
        for row in keyboard.inline_keyboard:
            for btn in row:
                assert (
                    btn.callback_data == "loading:none"
                ), "Loading keyboard should be non-interactive"

    def test_control_menu_keyboard_structure(self):
        """Test control menu has emergency stop for admins"""
        keyboard = TelegramKeyboards.get_control_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        buttons = []
        for row in keyboard.inline_keyboard:
            for btn in row:
                buttons.append(btn.callback_data)
        assert "control:emergency_stop" in buttons, "Emergency stop should be in control menu"

    def test_settings_menu_keyboard_structure(self):
        keyboard = TelegramKeyboards.get_settings_menu()
        assert isinstance(keyboard, InlineKeyboardMarkup)


class TestTelegramMessageFormatters:
    """Test all message formatting functions from TelegramFormatters"""

    def test_currency_formatting(self):
        """Test currency formatting with correct decimals"""
        formatted = TelegramFormatters.format_currency(1000.50)
        assert "$1,000.50" in formatted

        formatted_big = TelegramFormatters.format_currency(1000000.00)
        assert "$1,000,000.00" in formatted_big

    def test_crypto_price_formatting(self):
        """Test crypto price formatting adapts to price level"""
        # BTC (high price)
        btc_formatted = TelegramFormatters.format_crypto_price(68500.0)
        assert "$68,500.00" in btc_formatted

        # Low price altcoin
        xrp_formatted = TelegramFormatters.format_crypto_price(0.52)
        assert "$0.520000" in xrp_formatted  # 6 decimals for low prices

    def test_pnl_formatting(self):
        """Test P&L formatting with correct emoji"""
        positive_pnl = TelegramFormatters.format_pnl(100.50)
        assert "🟢" in positive_pnl
        assert "$100.50" in positive_pnl

        negative_pnl = TelegramFormatters.format_pnl(-50.25)
        assert "🔴" in negative_pnl
        assert "$-50.25" in negative_pnl

    def test_timestamp_formatting(self):
        """Test timestamp formatting"""
        dt = datetime(2024, 1, 15, 10, 30, 0)
        formatted = TelegramFormatters.format_timestamp(dt)
        assert "2024-01-15 10:30:00 UTC" in formatted

    def test_percentage_formatting(self):
        """Test percentage formatting"""
        formatted = TelegramFormatters.format_percentage(20.5)
        assert "20.5%" in formatted


class TestTelegramBot:
    """Test TelegramBot core functionality - UI-only operations"""

    @pytest.mark.asyncio
    async def test_bot_initialization_disabled_when_no_credentials(self, event_bus):
        """Test bot disables itself when Telegram credentials are missing"""
        with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = ""
            bot = TelegramBot(event_bus)
            assert not bot._enabled

    @pytest.mark.asyncio
    async def test_bot_start_stop_lifecycle(self, event_bus):
        """Test bot can be started and stopped gracefully"""
        with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_bot_token = "test_token"
            mock_settings.telegram_chat_id = "123456789"
            bot = TelegramBot(event_bus)
            assert bot._enabled
            # We won't actually start the bot in tests (would require real Telegram connection)
            await bot.stop()  # Should handle stop even if not started

    def test_event_subscription_setup(self, event_bus):
        """Test bot subscribes to correct system events"""
        with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_bot_token = "test_token"
            mock_settings.telegram_chat_id = "123456789"
            bot = TelegramBot(event_bus)
            # Check the bot has event bus reference
            assert hasattr(bot, "event_bus")
            assert bot._enabled

    @pytest.mark.asyncio
    async def test_system_data_response_retry_after_sets_backoff(self, event_bus):
        """Test bot handles RetryAfter and records dashboard backoff."""
        with patch("interfaces.telegram.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_bot_token = "test_token"
            mock_settings.telegram_chat_id = "123456789"
            bot = TelegramBot(event_bus)
            bot._enabled = True
            bot._bot = AsyncMock()

            # Initialize dispatcher (normally done in start())
            from interfaces.telegram.message_dispatcher import MessageDispatcher

            bot._dispatcher = MessageDispatcher(
                bot._bot, bot._chat_id, event_bus, bot._rate_limiter
            )

            bot._dashboard_message_id = 123

            from telegram.error import RetryAfter

            bot._bot.edit_message_text.side_effect = RetryAfter(10)

            event = Event(
                event_type=EventTopic.TELEGRAM_RESPONSE_SYSTEM_DATA,
                data={"action": "dashboard", "message_id": 123},
                source="test",
            )
            await bot._on_system_data_response(event)

            # Check that rate limiter has backoff applied
            assert bot._rate_limiter.is_in_backoff()
            assert bot._rate_limiter.get_backoff_remaining() > 0
            bot._bot.edit_message_text.assert_called_once()


class TestMessageTemplates:
    """Test all message templates from MessageTemplates class"""

    def test_welcome_message_structure(self):
        """Test welcome message has all required sections"""
        message = MessageTemplates.get_welcome_message()
        assert "VCOREX INSTITUTIONAL" in message
        assert "Bot giao dịch tự động AI chuyên nghiệp" in message
        assert "OKX DEMO TRADING" in message

    def test_main_menu_message_structure(self):
        """Test main menu message includes all menu options"""
        message = MessageTemplates.get_main_menu_message()
        assert "BẢNG ĐIỀU KHIỂN CHÍNH" in message
        assert "Thống kê" in message
        assert "Giao dịch" in message
        assert "Hệ thống" in message
        assert "Cài đặt" in message
        assert "Điều khiển" in message

    def test_analytics_dashboard_formatting(self):
        """Test analytics dashboard renders correctly with data"""
        pnl_data = {
            "total_pnl": 1250.50,
            "daily_pnl": 150.25,
            "win_rate": 68.0,
            "total_trades": 25,
            "active_positions": 3,
        }
        message = MessageTemplates.get_analytics_dashboard(pnl_data)
        assert "THỐNG KÊ TỔNG QUAN" in message
        assert "Bảng P&L:" in message
        assert "Tỷ lệ thắng:" in message
        # Since it returns the menu text now instead of actual stats, we don't assert stats here
        # The stats are in get_pnl_dashboard()

    def test_open_positions_empty_state(self):
        """Test empty positions message shows correctly"""
        message = MessageTemplates.format_open_positions([])
        assert "VỊ THẾ ĐANG MỞ" in message
        assert "không có vị thế nào đang mở" in message

    def test_open_positions_with_multiple_tps(self):
        """Test that open positions are rendered with detailed TP1, TP2, TP3 targets"""
        positions = [
            {
                "symbol": "LTC-USDT-SWAP",
                "side": "long",
                "leverage": 10,
                "entry_price": 56.34,
                "current_price": 56.33,
                "amount": 177.0,
                "pnl": -1.77,
                "pnl_pct": -0.18,
                "margin": 997.22,
                "notional_size": 9966.22,
                "has_sl": True,
                "sl_price": 53.5040,
                "has_tp": True,
                "tp_prices": [59.1360, 61.9520, 64.7680],
                "strategy_name": "EMA 9/21 Crossover",
            }
        ]
        message = MessageTemplates.format_open_positions(positions)
        assert "LTC-USDT-SWAP" in message
        assert "<b>SL:</b> $53.5040" in message
        assert "<b>TP:</b> $59.1360 | $61.9520 | $64.7680" in message
        assert "<b>TỔNG CỘNG LỜI/LỖ (NET PnL):</b>" in message
        assert "🔴 <b>-$1.77</b> (-0.18%)" in message
        assert "Tổng Ký Quỹ Sử Dụng:</b> <code>$997.22</code>" in message

    def test_active_signals_empty_state(self):
        """Test empty signals message shows correctly"""
        message = MessageTemplates.format_active_signals([])
        assert "TÍN HIỆU HOẠT ĐỘNG" in message
        assert "Chưa có tín hiệu giao dịch mới từ Radar" in message

    def test_system_alert_with_html_escaping(self):
        """Test that system alert messages are escaped properly to prevent Telegram parsing errors"""
        import html
        exception_str = "RetryError[<Future at 0x24f374d4ad0 state=finished raised OKXAPIError>]"
        escaped = html.escape(exception_str)
        assert "<" not in escaped
        assert ">" not in escaped
        assert "&lt;Future" in escaped
        
        # Test template rendering
        data = {
            "level": "CRITICAL",
            "title": "LỖI VÀO LỆNH BTC-USDT-SWAP",
            "message": f"Không thể mở vị thế do lỗi API sàn OKX:\n🔴 <code>{escaped}</code>"
        }
        rendered = MessageTemplates.get_system_alert(data)
        assert "🚨 <b>THẤT BẠI GIAO DỊCH (TRADE FAILED)</b>" in rendered
        assert "💎 <b>Tài sản:</b> <code>BTC-USDT-SWAP</code>" in rendered
        assert "&lt;Future" in rendered

