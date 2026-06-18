"""
Data formatters for Telegram messages.
Consistent number formatting, date formatting, currency formatting.
"""

from datetime import datetime
from typing import Any, Dict, List, Union


class TelegramFormatters:
    """Shared formatting utilities for all Telegram messages."""

    @staticmethod
    def format_currency(value: float, decimals: int = 2) -> str:
        """Format giá trị tiền tệ với dấu phẩy và số thập phân phù hợp."""
        return f"${value:,.{decimals}f}"

    @staticmethod
    def format_crypto_price(price: float) -> str:
        """Format giá crypto với số chữ số thập phân tự động (6 số cho altcoins thấp)."""
        if price >= 1000:
            return f"${price:,.2f}"
        elif price >= 1:
            return f"${price:,.4f}"
        else:
            return f"${price:,.6f}"

    @staticmethod
    def format_timestamp(dt: Union[datetime, str]) -> str:
        """Format timestamp theo chuẩn UTC của bot."""
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except ValueError:
                return dt

        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def format_duration(seconds: int) -> str:
        """Format duration (uptime, position hold time)."""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    @staticmethod
    def format_percentage(value: float) -> str:
        """Format tỷ lệ phần trăm với 1 số thập phân."""
        return f"{value:.1f}%"

    @staticmethod
    def format_pnl(pnl: float) -> str:
        """Format P&L với màu sắc (emoji) theo dương/âm."""
        emoji = "🟢" if pnl >= 0 else "🔴"
        return f"{emoji} {TelegramFormatters.format_currency(pnl)}"

    @staticmethod
    def format_trade_list(trades: List[Dict[str, Any]], limit: int = 10) -> str:
        """Format danh sách giao dịch cho history menu."""
        lines = []
        for i, trade in enumerate(trades[:limit], 1):
            side_emoji = "🟢" if trade["side"] == "buy" else "🔴"
            lines.append(
                f"{i}. {side_emoji} {trade['symbol']} | "
                f"{TelegramFormatters.format_currency(trade['pnl'])} | "
                f"{TelegramFormatters.format_timestamp(trade['closed_at'])}"
            )

        return "\n".join(lines)

    @staticmethod
    def format_bot_settings(settings: Dict[str, Any]) -> str:
        """Format cấu hình bot hiện tại cho settings menu."""
        return (
            f"⚙️ <b>Cấu hình Bot hiện tại:</b>\n\n"
            f"📊 Leverage: {settings.get('leverage', 1)}x\n"
            f"💰 Max Position Size: {TelegramFormatters.format_currency(settings.get('max_position_size', 100))}\n"
            f"📉 Max Drawdown: {TelegramFormatters.format_percentage(settings.get('max_drawdown', 10))}\n"
            f"👁️ Watchlist: {', '.join(settings.get('watchlist', ['BTC-USDT']))}\n"
            f"⏱️ Scan Interval M5: {settings.get('scan_interval_m5', 60)}s\n"
            f"⚠️ Notifications: {'✅ Bật' if settings.get('telegram_notifications') else '❌ Tắt'}"
        )

    @staticmethod
    def escape_markdown(text: str) -> str:
        """Escape các ký tự đặc biệt trong MarkdownV2."""
        special_chars = [
            "_",
            "*",
            "[",
            "]",
            "(",
            ")",
            "~",
            "`",
            ">",
            "#",
            "+",
            "-",
            "=",
            "|",
            "{",
            "}",
            ".",
            "!",
        ]
        for char in special_chars:
            text = text.replace(char, f"\\{char}")
        return text
