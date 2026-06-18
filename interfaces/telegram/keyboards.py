"""
Telegram inline keyboard definitions according to UX specification.
Menu groups: Analytics, Trading, System, History, Settings, Control
"""

from typing import Any, Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.events.payloads import PositionAction
from interfaces.telegram.callback_tokens import CallbackTokenStore

# Telegram inline callback_data max length is 64 bytes
_MAX_CALLBACK_BYTES = 64


def _callback_len(data: str) -> int:
    return len(data.encode("utf-8"))


def validate_callback_data(callback_data: str) -> str:
    """
    Validate callback data length against Telegram's 64-byte limit.
    Raises ValueError if exceeds limit.
    """
    if _callback_len(callback_data) > 64:
        raise ValueError(
            f"Callback data exceeds Telegram's 64-byte limit: "
            f"'{callback_data}' ({_callback_len(callback_data)} bytes)"
        )
    return callback_data


class TelegramKeyboards:
    """Factory class cho tất cả các inline keyboards theo UX specs."""

    @staticmethod
    def get_main_menu() -> InlineKeyboardMarkup:
        """Main menu keyboard - groups 2x3 + quick actions."""
        keyboard = [
            [
                InlineKeyboardButton("💰 Quản lý Vốn (Capital)", callback_data=validate_callback_data("trading:capital_management")),
                InlineKeyboardButton("📰 Radar Tin tức (News)", callback_data=validate_callback_data("system:news")),
            ],
            [
                InlineKeyboardButton("📊 Thống kê (Analytics)", callback_data=validate_callback_data("menu:analytics")),
                InlineKeyboardButton("📦 Quản lý Vị thế (Positions)", callback_data=validate_callback_data("menu:trading")),
            ],
            [
                InlineKeyboardButton("🔌 Trạng thái OKX (System)", callback_data=validate_callback_data("menu:system")),
                InlineKeyboardButton("📜 Nhật ký Trade (History)", callback_data=validate_callback_data("menu:history")),
            ],
            [
                InlineKeyboardButton("⚙️ Tùy chỉnh (Settings)", callback_data=validate_callback_data("menu:settings")),
                InlineKeyboardButton("💻 Điều khiển (Control)", callback_data=validate_callback_data("menu:control")),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_analytics_menu() -> InlineKeyboardMarkup:
        """Analytics submenu."""
        keyboard = [
            [
                InlineKeyboardButton("💹 Bảng P&L", callback_data=validate_callback_data("analytics:pnp_dashboard")),
                InlineKeyboardButton("📈 Hiệu suất", callback_data=validate_callback_data("analytics:performance")),
            ],
            [
                InlineKeyboardButton("📊 Tỷ lệ thắng", callback_data=validate_callback_data("analytics:winrate")),
                InlineKeyboardButton("🏦 Lịch sử số dư", callback_data=validate_callback_data("analytics:balance_history")),
            ],
            [InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_trading_menu() -> InlineKeyboardMarkup:
        """Trading submenu."""
        keyboard = [
            [
                InlineKeyboardButton("📦 Vị thế Đang mở", callback_data=validate_callback_data("trading:open_positions")),
                InlineKeyboardButton("📡 Tín hiệu Hoạt động", callback_data=validate_callback_data("trading:active_signals")),
            ],
            [
                InlineKeyboardButton("⏳ Lệnh Chờ xử lý", callback_data=validate_callback_data("trading:pending_orders")),
                InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main")),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_system_menu() -> InlineKeyboardMarkup:
        """System submenu."""
        keyboard = [
            [
                InlineKeyboardButton("✅ Sức khỏe Hệ thống", callback_data=validate_callback_data("system:health")),
                InlineKeyboardButton("📋 Nhật ký Hệ thống", callback_data=validate_callback_data("system:logs")),
            ],
            [
                InlineKeyboardButton("🔌 Trạng thái OKX", callback_data=validate_callback_data("system:exchange_status")),
                InlineKeyboardButton("📊 Chỉ số Hiệu suất", callback_data=validate_callback_data("system:metrics")),
            ],
            [InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_history_menu() -> InlineKeyboardMarkup:
        """History submenu."""
        keyboard = [
            [
                InlineKeyboardButton("📜 Lịch sử Chốt vị thế", callback_data=validate_callback_data("history:positions_history")),
                InlineKeyboardButton("📋 Lịch sử Lệnh OKX", callback_data=validate_callback_data("history:orders_history")),
            ],
            [
                InlineKeyboardButton("🚨 Thanh lý (Liquidation)", callback_data=validate_callback_data("history:liquidations")),
                InlineKeyboardButton("📅 Báo cáo Hàng ngày", callback_data=validate_callback_data("history:daily_reports")),
            ],
            [
                InlineKeyboardButton("⛔ Tín hiệu Bị từ chối", callback_data=validate_callback_data("history:missed_signals")),
            ],
            [InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_settings_menu() -> InlineKeyboardMarkup:
        """Settings submenu."""
        keyboard = [
            [
                InlineKeyboardButton("⚙️ Cài đặt Bot", callback_data=validate_callback_data("settings:bot_settings")),
                InlineKeyboardButton("🛡️ Giới hạn Rủi ro", callback_data=validate_callback_data("settings:risk_limits")),
            ],
            [
                InlineKeyboardButton("👁️ Tầm Quét Radar (Watchlist)", callback_data=validate_callback_data("settings:radar_menu")),
                InlineKeyboardButton("🔔 Thông báo & Cảnh báo", callback_data=validate_callback_data("settings:notifications")),
            ],
            [InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_radar_limit_menu(current_limit: int = 20) -> InlineKeyboardMarkup:
        """Radar (Watchlist Limit) submenu - 4 lựa chọn tầm quét."""

        def _label(limit: int, icon: str, name: str) -> str:
            active = " ✅" if current_limit == limit else ""
            return f"{icon} Top {limit} {name}{active}"

        keyboard = [
            [
                InlineKeyboardButton(_label(5, "🔥", "An Toàn"), callback_data=validate_callback_data("radar:5")),
                InlineKeyboardButton(_label(10, "⚡", "Cân Bằng"), callback_data=validate_callback_data("radar:10")),
            ],
            [
                InlineKeyboardButton(_label(15, "🌪️", "Săn Mồi"), callback_data=validate_callback_data("radar:15")),
                InlineKeyboardButton(_label(20, "🐉", "Max"), callback_data=validate_callback_data("radar:20")),
            ],
            [InlineKeyboardButton("◀️ Về Cài đặt", callback_data=validate_callback_data("menu:settings"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_control_menu() -> InlineKeyboardMarkup:

        """Control panel submenu (heartbeat control)."""
        keyboard = [
            [
                InlineKeyboardButton("▶️ Khởi động Bot", callback_data=validate_callback_data("control:start_bot")),
                InlineKeyboardButton("⏸️ Tạm dừng", callback_data=validate_callback_data("control:pause_bot")),
            ],
            [
                InlineKeyboardButton("🛑 Dừng khẩn cấp", callback_data=validate_callback_data("control:emergency_stop")),
                InlineKeyboardButton("🔄 Làm mới Tín hiệu", callback_data=validate_callback_data("control:reset_signals")),
            ],
            [
                InlineKeyboardButton("🔄 Làm mới Toàn diện", callback_data=validate_callback_data("control:clean_bot")),
            ],
            [InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_confirmation_dialog(
        action: str, confirm_text: str = "✅ Xác nhận", cancel_text: str = "❌ Hủy"
    ) -> InlineKeyboardMarkup:
        """Generic confirmation dialog keyboard."""
        keyboard = [
            [
                InlineKeyboardButton(confirm_text, callback_data=validate_callback_data(f"confirm:{action}")),
                InlineKeyboardButton(cancel_text, callback_data=validate_callback_data(f"cancel:{action}")),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_confirmation_keyboard(action: str) -> InlineKeyboardMarkup:
        """Alias for dangerous-action confirmation keyboards."""
        return TelegramKeyboards.get_confirmation_dialog(action)

    @staticmethod
    def get_position_close_confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
        """Alias for token-based position close confirmation."""
        return TelegramKeyboards.get_position_confirm_keyboard(token)

    @staticmethod
    def get_loading_keyboard() -> InlineKeyboardMarkup:
        """Keyboard shown during loading states."""
        keyboard = [[InlineKeyboardButton("⏳ Đang tải...", callback_data=validate_callback_data("loading:none"))]]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_back_to_main_menu() -> InlineKeyboardMarkup:
        """Keyboard with just a back button to main menu."""
        keyboard = [[InlineKeyboardButton("◀️ Về trang chính", callback_data=validate_callback_data("menu:main"))]]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_detail_keyboard(action_data: str, parent_menu: str) -> InlineKeyboardMarkup:
        """Keyboard shown when viewing details (report/list) with refresh and back buttons."""
        keyboard = [
            [
                InlineKeyboardButton("🔄 Làm mới", callback_data=validate_callback_data(action_data)),
                InlineKeyboardButton("🔙 Quay lại", callback_data=validate_callback_data(f"menu:{parent_menu}")),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_open_positions_keyboard(positions: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
        """Keyboard for open positions list with per-position close actions (token-based, <64 bytes)."""
        keyboard = []
        for pos in positions:
            position_id = pos.get("position_id")
            if not position_id:
                continue
            symbol = str(pos.get("symbol", "?")).replace("-SWAP", "")[:14]
        keyboard.append(
            [
                InlineKeyboardButton("🔄 Làm mới", callback_data=validate_callback_data("trading:open_positions")),
                InlineKeyboardButton("🔙 Quay lại", callback_data=validate_callback_data("menu:trading")),
            ]
        )
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_positions_detail_keyboard(position_id: str) -> InlineKeyboardMarkup:
        """Keyboard cho chi tiết một vị thế."""
        keyboard = [
            [
                InlineKeyboardButton(
                    "◀️ Về danh sách vị thế", callback_data=validate_callback_data("trading:open_positions")
                )
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_position_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
        """Keyboard for position action confirmation using secure token."""
        keyboard = [
            [
                InlineKeyboardButton("✅ Xác nhận", callback_data=validate_callback_data(f"confirm:{token}")),
                InlineKeyboardButton("❌ Hủy", callback_data=validate_callback_data(f"cancel:{token}")),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_missed_signals_dashboard_keyboard() -> InlineKeyboardMarkup:
        """Keyboard for Risk & Strategy Dashboard (Missed Signals)."""
        keyboard = [
            [
                InlineKeyboardButton("🔄 Làm mới", callback_data=validate_callback_data("history:missed_signals")),
                InlineKeyboardButton("🗑️ Xóa Lịch Sử", callback_data=validate_callback_data("history:clear_missed_signals")),
            ],
            [
                InlineKeyboardButton("🔙 Quay lại", callback_data=validate_callback_data("menu:history")),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
