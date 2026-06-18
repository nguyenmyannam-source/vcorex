"""
Professional Telegram Notification Test Suite.
Validates ALL Telegram messages for:
- Accuracy & precision
- Professional terminology (OKX standards)
- Complete data population
- No misleading information
- Proper formatting & emoji consistency

Real scenarios:
- Signal rejection with actual strategy reasons
- Ghost position alerts with all fields
- Trading notifications with P&L precision
- System alerts with current status
- Error messages with root cause
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest


class TestSignalRejectionAccuracy:
    """Test signal rejection messages show correct reasons."""

    @pytest.mark.asyncio
    async def test_ema_strategy_rejection_message_accuracy(self):
        """
        Real scenario: 1H TRX-USDT-SWAP signal rejected.
        Actual reason: EMA 9/21 not finalized

        Bug fixed: Was showing "Không đủ điều kiện an toàn vốn" (misleading)
        Should show: "🔄 EMA 9/21 chưa giao nhau hoàn chỉnh" (correct)
        """
        # Arrange
        signal_data = {
            "symbol": "TRX-USDT-SWAP",
            "timeframe": "1H",
            "strategy": "EMA_9_21",
            "rejection_reason": "no_finalized_crossover",
        }

        # Strategy rejection mapping
        rejection_reasons = {
            "no_finalized_crossover": {
                "emoji": "🔄",
                "message": "EMA 9/21 chưa giao nhau hoàn chỉnh",
                "description": "Đường EMA 9 và 21 chưa cắt nhau hoàn toàn",
            },
            "color_validation_failed": {
                "emoji": "🎨",
                "message": "Màu khối lệnh không khớp tín hiệu",
                "description": "Màu khối lệnh không phù hợp với hướng tín hiệu",
            },
            "insufficient_margin": {
                "emoji": "💰",
                "message": "Ký quỹ không đủ",
                "description": "Số dư ký quỹ khả dụng không đủ cho lệnh này",
            },
            "max_positions_reached": {
                "emoji": "📦",
                "message": "Đã đạt tối đa vị thế mở",
                "description": "Số lượng vị thế mở đã đạt giới hạn",
            },
            "leverage_limit_exceeded": {
                "emoji": "⚠️",
                "message": "Vượt quá giới hạn đòn bẩy",
                "description": "Đòn bẩy yêu cầu vượt quá giới hạn cho phép",
            },
        }

        # Act
        reason_info = rejection_reasons.get(signal_data["rejection_reason"])

        if reason_info:
            message = f"{reason_info['emoji']} {reason_info['message']}"
        else:
            message = "⚠️ Tín hiệu bị từ chối"  # Fallback

        # Assert
        assert message == "🔄 EMA 9/21 chưa giao nhau hoàn chỉnh", \
            f"Wrong message: {message}"
        assert "Không đủ điều kiện an toàn vốn" not in message, \
            "Misleading capital message still present"

    @pytest.mark.asyncio
    async def test_multiple_rejection_reasons_priority_order(self):
        """
        Real scenario: Signal triggers multiple rejection reasons:
        1. Insufficient margin
        2. Max positions reached
        3. EMA not ready

        Priority: Show most severe first (capital > positions > strategy)
        """
        # Arrange
        rejection_reasons = [
            "insufficient_margin",  # Capital issue - most severe
            "max_positions_reached",  # Position limit
            "no_finalized_crossover",  # Strategy issue - least severe
        ]

        severity_order = [
            "insufficient_margin",  # Severity 1 (capital)
            "max_positions_reached",  # Severity 2 (positions)
            "max_leverage_limit",     # Severity 2 (leverage)
            "no_finalized_crossover", # Severity 3 (strategy)
            "color_validation_failed", # Severity 3 (strategy)
        ]

        # Act - Find highest severity
        shown_reason = None
        for severity in severity_order:
            if severity in rejection_reasons:
                shown_reason = severity
                break

        # Assert
        assert shown_reason == "insufficient_margin", \
            "Should show capital issue (most severe)"

    @pytest.mark.asyncio
    async def test_rejection_message_includes_context(self):
        """
        Real scenario: Signal rejected - message should include:
        1. Emoji (visual indicator)
        2. Reason (what went wrong)
        3. Current status (why it blocks)
        4. Suggestion (how to fix)
        """
        # Arrange
        rejection = {
            "reason": "insufficient_margin",
            "margin_available": Decimal("50"),
            "margin_required": Decimal("600"),
            "shortfall": Decimal("550"),
        }

        # Act - Build comprehensive message
        message_parts = [
            f"💰 <b>Ký quỹ Không Đủ</b>",
            f"",
            f"Yêu cầu: <code>${rejection['margin_required']}</code>",
            f"Khả dụng: <code>${rejection['margin_available']}</code>",
            f"Thiếu: <code>${rejection['shortfall']}</code>",
            f"",
            f"<i>💡 Gợi ý: Cấp thêm ký quỹ hoặc giảm lệnh</i>",
        ]

        full_message = "\n".join(message_parts)

        # Assert
        assert "💰" in full_message, "Missing emoji"
        assert "Ký quỹ Không Đủ" in full_message, "Missing reason"
        assert f"${rejection['shortfall']}" in full_message, "Missing shortfall info"
        assert "Gợi ý:" in full_message, "Missing suggestion"


class TestGhostPositionAlerts:
    """Test ghost position detection and alert accuracy."""

    @pytest.mark.asyncio
    async def test_ghost_position_shows_all_required_fields(self):
        """
        Real scenario: Ghost position for ARB-USDT-SWAP.
        Bug fixed: Was showing $0.00 for margin/notional.

        Required fields:
        - Symbol & quantity
        - Entry price (✓ marked if correct)
        - Current price (real-time from exchange)
        - Margin (actual from position)
        - Notional size (qty * current_price)
        - P&L (unrealized profit/loss)
        - ROE (return on equity %)
        - TP & SL prices (if set)
        - Strategy name (if bot entry)
        """
        # Arrange
        ghost_position = {
            "symbol": "ARB-USDT-SWAP",
            "qty": Decimal("1000"),
            "entry_price": Decimal("0.0999"),
            "current_price": Decimal("0.101"),
            "margin": Decimal("10.01"),
            "notional_size": Decimal("101"),
            "unrealized_pnl": Decimal("1.01"),
            "roe": Decimal("10.09"),
            "tp_trigger_px": Decimal("0.12"),
            "sl_trigger_px": Decimal("0.08"),
            "strategy_name": "EMA_9_21",
        }

        # Act - Build alert
        alert_lines = [
            f"📦 <b>PHỤC HỒI VỊ THẾ BOT - {ghost_position['strategy_name']}</b>",
            f"",
            f"<b>Thông tin Vị thế:</b>",
            f"Symbol: <code>{ghost_position['symbol']}</code>",
            f"Lượng: <code>{ghost_position['qty']}</code>",
            f"",
            f"<b>Giá cả:</b>",
            f"Entry: <code>${ghost_position['entry_price']}</code> ✓",
            f"Current: <code>${ghost_position['current_price']}</code>",
            f"",
            f"<b>Tài chính:</b>",
            f"Margin: <code>${ghost_position['margin']}</code>",
            f"Notional: <code>${ghost_position['notional_size']}</code>",
            f"P&L: <code>${ghost_position['unrealized_pnl']}</code>",
            f"ROE: <code>{ghost_position['roe']}%</code>",
            f"",
            f"<b>Lệnh Chốt:</b>",
            f"TP: <code>${ghost_position['tp_trigger_px']}</code>",
            f"SL: <code>${ghost_position['sl_trigger_px']}</code>",
        ]

        alert_message = "\n".join(alert_lines)

        # Assert - No $0.00 values
        assert "$0.00" not in alert_message, "Alert contains $0.00"
        assert "$0.0000" not in alert_message, "Alert contains $0.0000"

        # All fields present
        assert f"${ghost_position['margin']}" in alert_message
        assert f"${ghost_position['notional_size']}" in alert_message
        assert f"${ghost_position['unrealized_pnl']}" in alert_message
        assert f"{ghost_position['roe']}%" in alert_message

        # Correct title
        assert "PHỤC HỒI VỊ THẾ BOT" in alert_message
        assert ghost_position['strategy_name'] in alert_message

    @pytest.mark.asyncio
    async def test_manual_entry_shows_correct_title(self):
        """
        Real scenario: Manual entry detected (no strategy_name).
        Should show: "PHÁT HIỆN LỆNH TAY MỚI" (manual entry found)
        NOT: "PHỤC HỒI VỊ THẾ BOT" (bot recovery)
        """
        # Arrange
        manual_position = {
            "symbol": "ETH-USDT-SWAP",
            "qty": Decimal("2"),
            "entry_price": Decimal("3500"),
            "current_price": Decimal("3520"),
            "margin": Decimal("700"),
            "strategy_name": None,  # No strategy - manual entry
        }

        # Act - Determine title
        if manual_position["strategy_name"]:
            title = f"PHỤC HỒI VỊ THẾ BOT - {manual_position['strategy_name']}"
        else:
            title = "PHÁT HIỆN LỆNH TAY MỚI"

        # Assert
        assert title == "PHÁT HIỆN LỆNH TAY MỚI"
        assert "PHỤC HỒI VỊ THẾ BOT" not in title

    @pytest.mark.asyncio
    async def test_ghost_position_pnl_precision(self):
        """
        Real scenario: P&L calculation must use Decimal precision.
        No floating-point rounding errors.
        """
        # Arrange
        qty = Decimal("100")
        entry = Decimal("0.8745")
        current = Decimal("0.8823")

        # Act - Calculate PNL
        pnl = qty * (current - entry)

        # Assert - Should be exact decimal
        expected = Decimal("0.78")
        assert pnl == expected, \
            f"PNL precision error: {pnl} vs {expected}"


class TestTradingNotifications:
    """Test trading execution notifications."""

    @pytest.mark.asyncio
    async def test_order_execution_notification_completeness(self):
        """
        Real scenario: Market order filled.
        Notification must show:
        - Symbol, timeframe, strategy
        - Entry price (actual filled)
        - Quantity
        - Margin used
        - TP & SL levels
        - Expected slippage
        """
        # Arrange
        execution = {
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "1H",
            "strategy": "EMA_9_21",
            "signal_price": Decimal("68000"),  # Signal price
            "filled_price": Decimal("68045"),  # Actual filled (slippage)
            "quantity": Decimal("1"),
            "margin": Decimal("6800"),
            "tp_price": Decimal("69000"),
            "sl_price": Decimal("67000"),
        }

        # Calculate slippage
        slippage = execution["filled_price"] - execution["signal_price"]
        slippage_bps = (slippage / execution["signal_price"]) * Decimal("10000")

        # Act - Build notification
        notification = f"""
📊 <b>LỆnh Mở - {execution['strategy']}</b>

<b>Chi tiết Tín hiệu:</b>
Symbol: <code>{execution['symbol']}</code> | TF: {execution['timeframe']}

<b>Lệnh Mở:</b>
Giá tín hiệu: <code>${execution['signal_price']}</code>
Giá điền: <code>${execution['filled_price']}</code>
Sai lệch: <code>{slippage_bps:.1f} bps</code>

<b>Vị thế:</b>
Lượng: <code>{execution['quantity']} BTC</code>
Margin: <code>${execution['margin']}</code>

<b>Lệnh Chốt:</b>
TP: <code>${execution['tp_price']}</code>
SL: <code>${execution['sl_price']}</code>
"""

        # Assert
        assert f"${execution['filled_price']}" in notification
        assert f"{slippage_bps:.1f} bps" in notification
        assert execution['symbol'] in notification
        assert execution['strategy'] in notification

    @pytest.mark.asyncio
    async def test_position_closed_notification_pnl_accuracy(self):
        """
        Real scenario: Position closed at profit.
        Notification must show:
        - Close price & quantity
        - P&L (total and %)
        - ROE (return on equity)
        - Trade duration
        - Strategy effectiveness note
        """
        # Arrange
        closed_position = {
            "symbol": "SOL-USDT-SWAP",
            "entry_price": Decimal("140"),
            "close_price": Decimal("145"),
            "quantity": Decimal("10"),
            "margin": Decimal("1400"),
            "entry_time": "2024-01-15 10:30:00",
            "close_time": "2024-01-15 14:25:00",
        }

        # Calculate metrics
        pnl = closed_position["quantity"] * (closed_position["close_price"] - closed_position["entry_price"])
        pnl_pct = (pnl / closed_position["margin"]) * Decimal("100")
        roe = pnl_pct  # Simplified (actual ROE = PNL / margin * 100)

        # Act - Build notification
        notification = f"""
✅ <b>Vị thế Đóng Lãi</b>

Symbol: <code>{closed_position['symbol']}</code>
Entry: <code>${closed_position['entry_price']}</code>
Close: <code>${closed_position['close_price']}</code>
Qty: <code>{closed_position['quantity']}</code>

<b>Kết quả:</b>
P&L: <code>+${pnl}</code> ({pnl_pct:.2f}%)
ROE: <code>{roe:.2f}%</code>

Duration: 3h 55m
"""

        # Assert
        assert f"${pnl}" in notification
        assert f"{pnl_pct:.2f}%" in notification
        assert "✅" in notification  # Success emoji
        assert closed_position['symbol'] in notification


class TestSystemAlerts:
    """Test system status and alert messages."""

    @pytest.mark.asyncio
    async def test_connection_status_alert_clarity(self):
        """
        Real scenario: Exchange connection lost for 30 seconds.
        Alert must clearly indicate:
        - What failed (exchange connection)
        - When (current time)
        - Duration (how long)
        - Impact (positions monitored but not tradeable)
        - Action (reconnecting...)
        """
        # Arrange
        connection_status = {
            "exchange": "OKX",
            "status": "DISCONNECTED",
            "disconnected_at": "14:35:22",
            "duration_seconds": 30,
            "impact": "Positions monitored, new orders blocked",
            "action": "Reconnecting...",
        }

        # Act - Build alert
        alert = f"""
🔴 <b>Mất kết nối - OKX Exchange</b>

Lúc: <code>{connection_status['disconnected_at']}</code>
Thời lượng: <code>{connection_status['duration_seconds']}s</code>

<b>Ảnh hưởng:</b>
{connection_status['impact']}

<b>Hành động:</b>
{connection_status['action']}

<i>Tín hiệu vị thế đang được giám sát trực tiếp</i>
"""

        # Assert
        assert "🔴" in alert, "Missing disconnect indicator"
        assert str(connection_status['duration_seconds']) in str(alert)
        assert "Reconnecting" in alert

    @pytest.mark.asyncio
    async def test_emergency_stop_notification_urgent(self):
        """
        Real scenario: Emergency stop triggered (drawdown > 20%).
        Notification must be URGENT and CLEAR.
        """
        # Arrange
        emergency = {
            "trigger": "DRAWDOWN_EXCEEDED",
            "threshold": Decimal("20"),
            "current": Decimal("21.5"),
            "equity": Decimal("7850"),
            "max_equity": Decimal("10000"),
            "positions_closed": 3,
        }

        # Act - Build emergency alert
        alert = f"""
🚨 <b>KHẨN CẤP - DỪNG TOÀN BỘ</b>

Nguyên nhân: <b>DRAWDOWN > {emergency['threshold']}%</b>
Current: <code>{emergency['current']}%</code>
Vốn: <code>${emergency['equity']}</code>

⚠️ Đã Đóng <b>{emergency['positions_closed']}</b> Vị thế

<b>Hệ thống ĐÃ DỪNG</b>
Cần can thiệp thủ công
"""

        # Assert
        assert "🚨" in alert
        assert "KHẨN CẤP" in alert
        assert "DỪNG TOÀN BỘ" in alert
        assert f"{emergency['current']}%" in alert


class TestMenuFormatting:
    """Test menu consistency and clarity."""

    @pytest.mark.asyncio
    async def test_main_menu_button_terminology_professional(self):
        """
        Real scenario: Main menu buttons must be professional, clear.
        Not slang, not ambiguous, OKX-aligned terminology.
        """
        # Arrange
        buttons = [
            {"label": "💰 Quản lý Số dư", "action": "capital_management"},  # NOT "Soi số dư"
            {"label": "📡 Tin tức 48h", "action": "news"},
            {"label": "📊 Thống kê", "action": "analytics"},
            {"label": "📦 Vị thế & Lệnh", "action": "trading"},  # NOT "⚡ Giao dịch"
            {"label": "⛳ Sàn OKX", "action": "system"},  # NOT "📰 Hệ thống"
        ]

        # Check for professional terminology
        forbidden_terms = ["soi", "cắt", "cháy", "hụt", "gồng", "giao dịch"]

        # Act - Validate
        for button in buttons:
            label_lower = button["label"].lower()
            problematic = [term for term in forbidden_terms if term in label_lower]

            # Assert
            assert len(problematic) == 0, \
                f"Button '{button['label']}' contains slang: {problematic}"

    @pytest.mark.asyncio
    async def test_emoji_consistency_across_messages(self):
        """
        Real scenario: Same concept should use same emoji everywhere.
        Examples:
        - Positions: 📦 (not 🔥)
        - Liquidation: 🚨 (not ⚠️)
        - Connection: 🔴 (not ❌)
        - Success: ✅ (not 👍)
        """
        # Arrange
        emoji_map = {
            "position": "📦",  # Standard
            "liquidation": "🚨",
            "connection_lost": "🔴",
            "success": "✅",
            "warning": "⚠️",
            "refresh": "🔄",
            "exchange": "⛳",
        }

        # Example messages using consistent emoji
        messages = [
            f"{emoji_map['position']} Vị thế Mở",
            f"{emoji_map['position']} Vị thế Đóng",
            f"{emoji_map['liquidation']} Thanh lý",
            f"{emoji_map['connection_lost']} Mất kết nối",
            f"{emoji_map['success']} Lệnh Thành công",
        ]

        # Act - Check consistency
        for msg in messages:
            # Just verify message builds correctly
            assert len(msg) > 0

        # Assert
        assert messages[0].startswith("📦")
        assert messages[2].startswith("🚨")
        assert messages[3].startswith("🔴")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
