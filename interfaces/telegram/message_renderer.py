"""Message rendering for Telegram bot - converts event payloads into formatted UI messages.

This module is responsible for transforming domain data into text output that can
be sent to Telegram users. Rendering failures are logged and replaced with safe
fallback text to keep the bot UI responsive.
"""

import html
from typing import Any, Dict

from loguru import logger

from core.exceptions import TelegramRenderError
from interfaces.telegram.message_templates import MessageTemplates


class MessageRenderer:
    """Renders system, trading, analytics, history, and news messages for Telegram."""

    @staticmethod
    def render_health_data(data: Dict[str, Any]) -> str:
        """Render system health data.

        Args:
            data: Health payload from the system event.

        Returns:
            Formatted HTML text for Telegram.
        """
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (Health):</b>\n<code>{html.escape(str(error_msg))}</code>"
            if not data:
                raise TelegramRenderError("Health data is empty", {"data": data})
            return MessageTemplates.get_system_health(data)
        except TelegramRenderError:
            raise
        except Exception as e:
            logger.error(f"Failed to render health data: {e}", exc_info=True)
            logger.debug(f"Health data content: {data}")
            return "⚠️ Failed to load health data"

    @staticmethod
    def render_trading_data(data: Dict[str, Any]) -> str:
        """Render trading data based on action.

        Args:
            data: Trading payload containing action and related values.

        Returns:
            Message text for trading dashboard or signals.
        """
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (Trading):</b>\n<code>{html.escape(str(error_msg))}</code>"
            action = data.get("action")
            if action in ("active_positions", "open_positions"):
                return MessageTemplates.format_open_positions(data.get("positions", []))
            elif action == "active_signals":
                return MessageTemplates.format_active_signals(data.get("signals", []))
            elif action == "pending_orders":
                return MessageTemplates.format_pending_orders(data.get("orders", []))
            elif action == "capital_management":
                return MessageTemplates.format_capital_management(data)
            return "Unknown trading action."
        except Exception as e:
            logger.error(f"Failed to render trading data: {e}", exc_info=True)
            logger.debug(f"Trading payload: {data}")
            return "⚠️ Failed to load trading data"

    @staticmethod
    def render_analytics_data(data: Dict[str, Any]) -> str:
        """Render analytics data based on action.

        Args:
            data: Analytics payload from the system.

        Returns:
            Formatted analytics text for Telegram.
        """
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (Analytics):</b>\n<code>{html.escape(str(error_msg))}</code>"
            action = data.get("action")
            if action == "pnp_dashboard":
                return MessageTemplates.get_pnl_dashboard(data)
            elif action == "winrate":
                return MessageTemplates.get_winrate_stats(data)
            elif action == "balance_history":
                return MessageTemplates.get_balance_history(data)
            elif action == "performance":
                return MessageTemplates.get_performance_stats(data)
            return "Unknown analytics action."
        except Exception as e:
            logger.error(f"Failed to render analytics data: {e}", exc_info=True)
            logger.debug(f"Analytics payload: {data}")
            return "⚠️ Failed to load analytics data"

    @staticmethod
    def render_settings_data(data: Dict[str, Any]) -> str:
        """Render settings data from backend."""
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (Settings):</b>\n<code>{html.escape(str(error_msg))}</code>"
            action = data.get("action")
            settings_payload = data.get("settings", {})
            if action == "bot_settings":
                return MessageTemplates.get_settings_bot(settings_payload)
            elif action == "risk_limits":
                return MessageTemplates.get_settings_risk(settings_payload)
            elif action == "watchlist":
                symbols = settings_payload.get("symbols", [])
                return MessageTemplates.get_settings_watchlist(
                    symbols,
                    radar_limit=settings_payload.get("radar_limit", 0),
                    total_watchlist=settings_payload.get("total_watchlist", 0),
                )
            elif action == "notifications":
                return MessageTemplates.get_settings_notifications(settings_payload)
            return f"ℹ️ Unknown settings action: {action}"
        except Exception as e:
            logger.error(f"Failed to render settings data: {e}", exc_info=True)
            return "⚠️ Failed to load settings data"

    @staticmethod
    def render_history_data(data: Dict[str, Any]) -> str:
        """Render history data based on action.

        Args:
            data: History payload including action and history list.

        Returns:
            Formatted historical report text.
        """
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (History):</b>\n<code>{html.escape(str(error_msg))}</code>"
            action = data.get("action")
            history_payload = data.get("history")
            if action == "closed_trades":
                return MessageTemplates.get_history_trades(history_payload)
            elif action == "liquidations":
                return MessageTemplates.get_history_liquidations(history_payload)
            elif action == "orders_history":
                return MessageTemplates.get_orders_history(history_payload)
            elif action == "positions_history":
                return MessageTemplates.get_positions_history(history_payload)
            elif action == "missed_signals":
                return MessageTemplates.get_history_missed_signals(history_payload)
            elif action in ["daily_reports", "weekly_report"]:
                return MessageTemplates.get_period_report(data)
            elif action == "periodic_report":
                return MessageTemplates.get_periodic_report(data)
            return "ℹ️ Không rõ thao tác lịch sử."
        except Exception as e:
            logger.error(f"Failed to render history data: {e}", exc_info=True)
            logger.debug(f"History payload: {data}")
            return "⚠️ Failed to load history data"

    @staticmethod
    def render_exchange_status(data: Dict[str, Any]) -> str:
        """Render exchange status data."""
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (Exchange Status):</b>\n<code>{html.escape(str(error_msg))}</code>"
            return MessageTemplates.get_exchange_status_message(data)
        except Exception as e:
            logger.error(f"Failed to render exchange status: {e}", exc_info=True)
            logger.debug(f"Exchange status payload: {data}")
            return "⚠️ Failed to load exchange status"

    @staticmethod
    def render_system_data(data: Dict[str, Any]) -> str:
        """Render system data based on action."""
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (System):</b>\n<code>{html.escape(str(error_msg))}</code>"
            if "custom_formatted_text" in data:
                return data["custom_formatted_text"]
                
            action = data.get("action")
            if action == "metrics":
                return MessageTemplates.get_system_metrics(data)
            elif action == "logs":
                return MessageTemplates.get_system_logs(data.get("logs", []))
            elif action == "dashboard":
                return MessageTemplates.get_pro_dashboard(data)
            return f"ℹ️ Unknown system action: {action}"
        except Exception as e:
            logger.error(f"Failed to render system data: {e}", exc_info=True)
            logger.debug(f"System payload: {data}")
            return "⚠️ Failed to load system data"

    @staticmethod
    def render_news_data(data: Dict[str, Any]) -> str:
        """Render news data."""
        try:
            if data and data.get("success") is False:
                error_msg = data.get("error", "Unknown error")
                return f"⚠️ <b>Lỗi tải dữ liệu (News):</b>\n<code>{html.escape(str(error_msg))}</code>"
            return MessageTemplates.get_news_dashboard(data)
        except Exception as e:
            logger.error(f"Failed to render news data: {e}", exc_info=True)
            logger.debug(f"News payload: {data}")
            return "⚠️ Failed to load news data"

    @staticmethod
    def render_signal_rejection(data: Dict[str, Any]) -> str:
        """Render a signal rejection notification."""
        try:
            return MessageTemplates.get_signal_rejection_message(data)
        except Exception as e:
            logger.error(f"Failed to render signal rejection: {e}", exc_info=True)
            logger.debug(f"Rejection payload: {data}")
            return "⚠️ Failed to render signal rejection alert"

    @staticmethod
    def render_clean_bot_complete(data: Dict[str, Any]) -> str:
        """Render a clean bot complete notification."""
        try:
            return MessageTemplates.get_clean_bot_complete_message(data)
        except Exception as e:
            logger.error(f"Failed to render clean bot complete: {e}", exc_info=True)
            logger.debug(f"Clean bot payload: {data}")
            return "✅ Đã hoàn thành reset toàn diện bot"

    @staticmethod
    def render_reset_signals_complete(data: Dict[str, Any]) -> str:
        """Render a reset signals complete notification."""
        try:
            if data.get("success"):
                return MessageTemplates.get_reset_signals_complete_message(data)
            else:
                error_msg = data.get("error", "Unknown error")
                return f"❌ <b>Lỗi khi reset tín hiệu:</b>\n<code>{html.escape(str(error_msg))}</code>"
        except Exception as e:
            logger.error(f"Failed to render reset signals complete: {e}", exc_info=True)
            logger.debug(f"Reset signals payload: {data}")
            return "❌ Lỗi khi render thông báo reset tín hiệu"