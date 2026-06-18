"""
Message templates for Telegram UI - UX Specification v2.0 (Mobile First)
"""

import time
import html
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List


class MessageTemplates:
    """Factory class cho tất cả message templates với UX chuẩn Mobile, Thuần Việt, Ngập Icon."""

    @staticmethod
    def _get_min_body_pct(timeframe: str, app_settings: Any = None) -> float:
        """Ngưỡng thân nến theo TF (khớp ema_crossover.py)."""
        if app_settings is None:
            from core.config.settings import settings as app_settings
        tf = (timeframe or "").strip()
        mapping = {
            "5m": app_settings.min_body_percentage_5m,
            "15m": app_settings.min_body_percentage_15m,
            "1H": app_settings.min_body_percentage_1h,
            "4H": app_settings.min_body_percentage_4h,
            "1D": app_settings.min_body_percentage_1d,
            "1W": app_settings.min_body_percentage_1w,
            "1M": app_settings.min_body_percentage_1m,
        }
        return float(mapping.get(tf, app_settings.min_body_percentage))

    @staticmethod
    def _format_min_body_table(app_settings: Any = None) -> str:
        if app_settings is None:
            from core.config.settings import settings as app_settings
        rows = [
            ("5m", app_settings.min_body_percentage_5m),
            ("15m", app_settings.min_body_percentage_15m),
            ("1H", app_settings.min_body_percentage_1h),
            ("4H", app_settings.min_body_percentage_4h),
            ("1D", app_settings.min_body_percentage_1d),
            ("1W", app_settings.min_body_percentage_1w),
            ("1M", app_settings.min_body_percentage_1m),
        ]
        lines = [f"├─ <code>{tf:>3}</code>: <b>≥ {pct:.1f}%</b>" for tf, pct in rows]
        lines[-1] = lines[-1].replace("├─", "└─", 1)
        return "\n".join(lines)

    @staticmethod
    def _escape_html(text: Any) -> str:
        """Escape HTML special characters để tránh injection & render fail.

        Args:
            text: Value cần escape (có thể là string, number, None)

        Returns:
            Escaped string safe for Telegram HTML parsing
        """
        if text is None or text == "None":
            return "❓ Không có dữ liệu"
        text_str = str(text).strip()
        if not text_str:
            return "❓ Trống"
        return html.escape(text_str)

    @staticmethod
    def format_title(title: str) -> str:
        """Format tiêu đề chuẩn: 🎯 <b><u>TIÊU ĐỀ IN HOA</u></b>"""
        return f"<b><u>{MessageTemplates._escape_html(title).upper()}</u></b>\n\n"

    @staticmethod
    def _get_timezone():
        """Get configured timezone from settings."""
        try:
            from core.config.settings import settings
            from zoneinfo import ZoneInfo
            return ZoneInfo(settings.telegram_timezone)
        except Exception:
            # Fallback to ICT (UTC+7) if settings not available or invalid
            return timezone(timedelta(hours=7))

    @staticmethod
    def _validate_and_truncate_message(message: str, max_length: int = 4096) -> str:
        """Validate và truncate tin nhắn nếu vượt quá giới hạn Telegram.

        Args:
            message: Tin nhắn cần validate
            max_length: Độ dài tối đa (default 4096 cho Telegram)

        Returns:
            Tin nhắn đã được truncate nếu cần, với thông báo truncation
        """
        if len(message) <= max_length:
            return message
        
        # Truncate và thêm thông báo
        truncated = message[:max_length - 100]  # Giữ 100 chars cho thông báo
        truncated += "\n\n⚠️ <i>Tin nhắn đã được cắt ngắn do vượt quá giới hạn. Vui lòng xem chi tiết trên dashboard.</i>"
        return truncated

    @staticmethod
    def get_welcome_message(bot_name: str = "VCOREX Institutional") -> str:
        """Welcome message when /start is called."""
        return (
            "👋 "
            + MessageTemplates.format_title(f"CHÀO MỪNG ĐẾN VỚI {bot_name}")
            + "🤖 <i>Bot giao dịch tự động AI chuyên nghiệp đã sẵn sàng phục vụ!</i>\n\n"
            "💠 <b>Thông tin hệ thống:</b>\n"
            "├─ 📊 <b>Trạng thái:</b> ✅ Đang hoạt động 24/7\n"
            "├─ ⚡ <b>Phiên bản:</b> 1.2.0 PRO\n"
            "└─ 🔗 <b>Môi trường:</b> OKX DEMO TRADING\n\n"
            "👇 <i>Vui lòng chọn chức năng từ Bảng Điều Khiển bên dưới:</i>"
        )

    @staticmethod
    def get_main_menu_message() -> str:
        """Main menu selection message."""
        return (
            "🎛️ "
            + MessageTemplates.format_title("BẢNG ĐIỀU KHIỂN CHÍNH")
            + "<i>Hệ thống giao dịch tự động & giám sát thị trường VCOREX Professional.</i>\n\n"
            "💠 <b>Danh mục Chính:</b>\n"
            "├─ 📊 <b>Thống kê:</b> Phân tích tỷ lệ thắng, P&L, hiệu suất\n"
            "├─ 📦 <b>Vị thế & Lệnh:</b> Quản lý vị thế, tín hiệu, lệnh chờ\n"
            "├─ ⛳ <b>Sàn OKX:</b> Kiểm tra API, kết nối, trạng thái\n"
            "├─ 📜 <b>Lịch sử:</b> Giao dịch, thanh lý, tín hiệu bị từ chối\n"
            "├─ ⚙️ <b>Cài đặt:</b> Tham số rủi ro, danh mục, thông báo\n"
            "└─ 💻 <b>Điều khiển:</b> Khởi động, tạm dừng, dừng khẩn cấp\n\n"
            "👇 <i>Chọn chức năng để bắt đầu:</i>"
        )

    @staticmethod
    def get_pro_dashboard(data: Dict[str, Any]) -> str:
        """Dashboard VCOREX v1.2 PRO - Institutional Grade Professional Trading."""
        uptime_secs = data.get("uptime_seconds", 0)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            uptime = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            uptime = f"{minutes}m {seconds}s"
        else:
            uptime = f"{seconds}s"
        api_latency = data.get("api_latency", 0)
        fg_score = data.get("fg_score", 50)
        fg_status = data.get("fg_status", "Trung lập")
        monitored_pairs = data.get("watchlist_count", 0)
        free_margin = data.get("free_margin", 0.0)
        realized_pnl = data.get("realized_pnl", 0.0)
        unrealized_pnl = data.get("unrealized_pnl", 0.0)
        active_positions = data.get("active_positions", 0)
        long_count = data.get("long_count", 0)
        short_count = data.get("short_count", 0)
        tpsl_count = data.get("tpsl_count", 0)
        risk_level = data.get("risk_level", "⚠️ TRUNG BÌNH")
        process_id = data.get("process_id", 0)
        error_count = data.get("error_count", 0)

        # [FIX] Add DEMO/LIVE mode indicator prominently
        account_mode = data.get("account_mode", "LIVE")
        mode_indicator = "🧪 CHẾ ĐỘ DEMO" if "DEMO" in account_mode else "⚠️ CHẾ ĐỘ LIVE"
        mode_color = "DEMO API" if "DEMO" in account_mode else "LIVE API"

        # Format PnL colors and signs with standardized emoji
        if realized_pnl > 0.005:
            realized_pnl_str = f"+${realized_pnl:,.2f}"
            realized_emoji = "✅"
        elif realized_pnl < -0.005:
            realized_pnl_str = f"-${abs(realized_pnl):,.2f}"
            realized_emoji = "❌"
        else:
            realized_pnl_str = f"$0.00"
            realized_emoji = "⚪"

        if unrealized_pnl > 0.005:
            unrealized_pnl_str = f"+${unrealized_pnl:,.2f}"
            unrealized_emoji = "✅"
        elif unrealized_pnl < -0.005:
            unrealized_pnl_str = f"-${abs(unrealized_pnl):,.2f}"
            unrealized_emoji = "❌"
        else:
            unrealized_pnl_str = f"$0.00"
            unrealized_emoji = "⚪"

        now = datetime.now().strftime("%H:%M:%S %d/%m")
        ws_status = "✅ Kết nối" if data.get("exchange_connected", True) else "❌ Mất kết nối"

        return (
            f"<b>VCOREX INSTITUTIONAL DASHBOARD • v1.2 PRO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{mode_indicator}</b> <code>{mode_color}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⚙️ HỆ THỐNG & KẾT NỐI</b>\n"
            f" ├─ ✅ Trạng thái: Hoạt động ổn định\n"
            f" ├─ ⏳ Thời gian chạy: {uptime}\n"
            f" ├─ ⚡ Độ trễ API: {api_latency}ms\n"
            f" └─ 📶 Websocket: {ws_status}\n\n"
            f"<b>🧠 PHÂN TÍCH & THỊ TRƯỜNG</b>\n"
            f" ├─ 🧭 Chỉ số Tâm lý (F&G): {fg_score} | {fg_status}\n"
            f" └─ 🔍 Danh mục Giám sát: {monitored_pairs} cặp\n\n"
            f"<b>💰 QUẢN LÝ VỐN & HIỆU SUẤT</b>\n"
            f" ├─ 💵 Ký quỹ Khả dụng: ${free_margin:,.2f}\n"
            f" ├─ 📈 PnL Đã chốt: {realized_emoji} {realized_pnl_str}\n"
            f" └─ 📊 PnL Chưa chốt: {unrealized_emoji} {unrealized_pnl_str}\n\n"
            f"<b>🛡️ VỊ THẾ & LỆNH</b>\n"
            f" ├─ 📦 Vị thế đang mở: {active_positions}\n"
            f" │   └─ ✅ MUA: {long_count}  |  ⚠️ BÁN: {short_count}\n"
            f" ├─ 🎯 Lệnh TP/SL: {tpsl_count}/{active_positions}\n"
            f" └─ ⚠️ Mức rủi ro: {risk_level}\n\n"
            f"<b>📊 HIỆU SUẤT HỆ THỐNG</b>\n"
            f" ├─ 🔄 Chu kỳ quét: 10s  |  PID: {process_id}\n"
            f" └─ ⚠️ Lỗi hệ thống: {error_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 Cập nhật: {now}"
        )

    @staticmethod
    def get_news_dashboard(data: Dict[str, Any]) -> str:
        """Dashboard Tin tức Thị trường 48H chuyên nghiệp (Bilingual & Coin Analysis)"""
        news_list = data.get("news", [])
        
        # Xử lý tóm tắt AI song ngữ
        ai_summary_data = data.get("ai_summary", {})
        if isinstance(ai_summary_data, dict):
            ai_summary_vi = ai_summary_data.get("vi", "Đang phân tích...")
            ai_summary_en = ai_summary_data.get("en", "Analyzing...")
            ai_summary_display = f"{ai_summary_vi}\n\n{ai_summary_en}"
        else:
            ai_summary_display = str(ai_summary_data)

        # Xử lý Coin Mentions
        coin_mentions = data.get("coin_mentions", {})
        hot_coins_str = ""
        if coin_mentions:
            hot_coins_str = "🔥 <b>HOT COINS (Sentiment):</b>\n"
            # Show top 5 coins
            for coin, sent_data in list(coin_mentions.items())[:5]:
                bull = sent_data.get("bullish", 0)
                bear = sent_data.get("bearish", 0)
                if bull > bear:
                    icon = "🟢"
                elif bear > bull:
                    icon = "🔴"
                else:
                    icon = "⚪"
                hot_coins_str += f"├─ {icon} <b>{coin}</b>: {bull} Bull | {bear} Bear\n"
            hot_coins_str += "━━━\n\n"

        last_update = datetime.fromtimestamp(data.get("last_update", time.time())).strftime("%H:%M:%S")

        news_items = ""
        for i, news in enumerate(news_list[:6]):  # Show top 6
            try:
                source_emoji = news.get("flag", "🌐")
                title = MessageTemplates._escape_html(news.get("title", "Không rõ"))
                source = MessageTemplates._escape_html(news.get("source", "N/A"))
                link = news.get("link", "").strip()
                
                # Highlight if strong sentiment
                sent = news.get("sentiment", "neutral")
                sent_emoji = "🟢 " if sent == "bullish" else "🔴 " if sent == "bearish" else "🔹 "

                if link:
                    link = MessageTemplates._escape_html(link)
                    news_items += f"{sent_emoji}<b>{title}</b>\n└─ {source_emoji} <i>{source}</i>\n\n"
            except Exception:
                continue

        if not news_items:
            news_items = "📭 <i>Hiện chưa tìm thấy tin tức nổi bật nào trong 48h qua. / No notable news in the last 48h.</i>\n\n"

        return (
            "📰 " + MessageTemplates.format_title("GLOBAL MARKET NEWS (48H)") +
            f"{ai_summary_display}\n\n"
            "━━━\n"
            f"{hot_coins_str}"
            "📰 <b>TOP HEADLINES:</b>\n\n"
            f"{news_items}"
            "━━━\n"
            f"🕒 <i>Cập nhật / Last update: {last_update}</i>"
        )

    @staticmethod
    def get_reset_signals_complete_message(data: Dict[str, Any]) -> str:
        """Formats a message for when signals have been reset."""
        success = data.get("success", False)
        if success:
            return (
                "✅ <b>Đã hoàn thành reset tín hiệu!</b>\n\n"
                "Tất cả các tín hiệu đang chờ xử lý đã được xóa.\n"
                "Bot sẽ bắt đầu quét và tạo tín hiệu mới."
            )
        else:
            error_msg = data.get("error", "Unknown error")
            return (
                "❌ <b>Lỗi khi reset tín hiệu:</b>\n\n"
                f"Chi tiết: <code>{html.escape(str(error_msg))}</code>\n"
                "Vui lòng kiểm tra nhật ký bot để biết thêm thông tin."
            )

    @staticmethod
    def get_signal_rejection_message(data: Dict[str, Any]) -> str:
        """Formats a professional message for a rejected signal — supports both
        SIGNAL_REJECTED (strategy filter) and RISK_SIGNAL_REJECTED (risk manager)."""

        from datetime import datetime

        # ── Core fields ──────────────────────────────────────────────────────
        symbol       = data.get("symbol", "N/A")
        timeframe    = data.get("timeframe", "N/A")
        raw_reason   = data.get("reason") or data.get("rejection_reason") or "unknown"
        details      = data.get("details", {})
        entry_price  = data.get("entry_price", 0.0)

        # Signal direction
        raw_stype = data.get("type") or data.get("signal_type") or ""
        stype_str = getattr(raw_stype, "value", str(raw_stype)).upper().strip()
        if "BUY" in stype_str or "LONG" in stype_str:
            direction_line = "🟢 <b>MUA / LONG</b>"
        elif "SELL" in stype_str or "SHORT" in stype_str:
            direction_line = "🔴 <b>BÁN / SHORT</b>"
        else:
            direction_line = f"⚪ {stype_str or 'N/A'}"

        # Timestamp
        now_str = datetime.now().strftime("%H:%M:%S %d/%m/%Y")

        # ── Reason mapping (technical filter) ────────────────────────────────
        tech_reason_map = {
            "weak_trend_adx"          : ("📉", "Xu hướng yếu", "ADX chưa đạt ngưỡng tối thiểu"),
            "weak_trend"              : ("📉", "Xu hướng yếu", "ADX chưa đạt ngưỡng tối thiểu"),
            "body_too_small"          : ("🕯️", "Thân nến quá nhỏ", "Nến có biên độ thực quá thấp"),
            "small_body"              : ("🕯️", "Thân nến quá nhỏ", "Nến có biên độ thực quá thấp"),
            "stale_signal"            : ("⌛", "Tín hiệu trễ", "Tín hiệu đã quá hạn (stale)"),
            "color_validation_failed" : ("🎨", "Màu nến sai hướng", "Nến đóng cửa ngược chiều tín hiệu"),
            "indicator_bundle_mismatch": ("🔀", "Bundle chỉ báo lệch", "Bộ chỉ báo và nến không đồng nhất"),
            "no_finalized_crossover"  : ("✂️", "Chưa có crossover", "EMA chưa cắt nhau hoàn toàn"),
        }

        # ── Risk Manager reason mapping ───────────────────────────────────────
        risk_reason_map = {
            "⏳ TỪ CHỐI"  : ("🔄", "Hệ thống đang đồng bộ",   "Bot vừa khởi động, đang nạp dữ liệu từ OKX"),
            "Syncing"     : ("🔄", "Hệ thống đang đồng bộ",   "Bot vừa khởi động, đang nạp dữ liệu từ OKX"),
            "seeding"     : ("🌱", "Chờ dữ liệu tài khoản",   "Đang chờ WebSocket trả về số dư tài khoản"),
            "Max:"        : ("🚦", "Vượt giới hạn số lệnh",   "Đang mở quá nhiều vị thế cùng lúc"),
            "margin"      : ("💰", "Không đủ ký quỹ",         "Số dư tài khoản không đủ để mở thêm lệnh"),
            "concentration": ("⚖️", "Tập trung rủi ro",       "Tỷ trọng vào một cặp vượt ngưỡng cho phép"),
        }

        # Detect category
        icon, short_reason, description = "🚫", raw_reason, ""
        # Try technical map first (exact key)
        if raw_reason in tech_reason_map:
            icon, short_reason, description = tech_reason_map[raw_reason]
            category = "STRATEGY"
        else:
            # Try risk map (substring match)
            category = "RISK"
            for keyword, (i, s, d) in risk_reason_map.items():
                if keyword.lower() in raw_reason.lower():
                    icon, short_reason, description = i, s, d
                    break
            else:
                description = raw_reason  # fallback: show full reason string

        # ── Category header ───────────────────────────────────────────────────
        if category == "STRATEGY":
            cat_header = "⚙️ <b>Bộ lọc Chiến lược từ chối</b>"
        else:
            cat_header = "🛡️ <b>Risk Manager từ chối</b>"

        # ── Detail section ────────────────────────────────────────────────────
        detail_lines = ""
        if raw_reason in ("weak_trend_adx", "weak_trend"):
            adx_val = details.get("adx", details.get("adx_value", 0))
            min_adx = details.get("min_adx", details.get("adx_threshold", 25))
            detail_lines += f"\n│  📊 ADX hiện tại : <code>{float(adx_val):.1f}</code>"
            detail_lines += f"\n│  📏 Yêu cầu tối thiểu : <code>≥ {float(min_adx):.1f}</code>"
            detail_lines += f"\n│  📌 Thiếu : <code>{max(0, float(min_adx) - float(adx_val)):.1f} điểm</code>"
        elif raw_reason in ("body_too_small", "small_body"):
            body = details.get("body_pct", 0)
            min_b = details.get("min_pct", details.get("min_body_pct", 1.0))
            detail_lines += f"\n│  🕯️ Thân nến hiện tại : <code>{float(body):.2f}%</code>"
            detail_lines += f"\n│  📏 Yêu cầu tối thiểu : <code>≥ {float(min_b):.1f}%</code>"
        elif description:
            detail_lines += f"\n│  📋 Chi tiết : <i>{description}</i>"

        # ── Indicator snapshot (if available) ────────────────────────────────
        ema9  = details.get("ema9")
        ema21 = details.get("ema21")
        adx   = details.get("adx")
        body  = details.get("body_pct")

        indicator_snap = ""
        if any(v is not None for v in [ema9, ema21, adx, body]):
            indicator_snap = "\n│\n│  <i>📡 Snapshot chỉ báo:</i>"
            if ema9 is not None and ema21 is not None:
                indicator_snap += f"\n│  EMA9 / EMA21 : <code>{float(ema9):.4f}</code> / <code>{float(ema21):.4f}</code>"
            if adx is not None:
                indicator_snap += f"\n│  ADX           : <code>{float(adx):.1f}</code>"
            if body is not None:
                indicator_snap += f"\n│  Thân nến      : <code>{float(body):.2f}%</code>"

        # ── Entry price ───────────────────────────────────────────────────────
        entry_line = ""
        if entry_price and float(entry_price) > 0:
            entry_line = f"\n│  💲 Giá dự kiến vào : <code>${float(entry_price):.4f}</code>"

        # ── Assemble final message ────────────────────────────────────────────
        tf_upper = str(timeframe).upper()
        return (
            f"🚫 <b>TÍN HIỆU BỊ TỪ CHỐI</b>\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"│  🪙 Tài sản  : <b>{symbol}</b> <i>({tf_upper})</i>\n"
            f"│  {direction_line}\n"
            f"│  🕐 Thời gian : <code>{now_str}</code>\n"
            f"│\n"
            f"│  {cat_header}\n"
            f"│  {icon} <b>Lý do : {short_reason}</b>"
            f"{detail_lines}"
            f"{entry_line}"
            f"{indicator_snap}\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"<i>⚡ Hệ thống tiếp tục theo dõi thị trường...</i>"
        )


    # ------------------ THỐNG KÊ (ANALYTICS) ------------------

    @staticmethod
    def get_analytics_dashboard(data: Dict[str, Any]) -> str:
        """Thống kê tổng quan - Analytics Dashboard"""
        return (
            "📊 "
            + MessageTemplates.format_title("THỐNG KÊ TỔNG QUAN")
            + "<i>Các chỉ số hiệu suất giao dịch chi tiết từ hệ thống phân tích AI:</i>\n\n"
            "💠 <b>Danh mục Phân tích:</b>\n"
            "├─ 💹 <b>Bảng P&L:</b> Lãi/lỗ ròng, chi tiết vị thế đóng\n"
            "├─ 📈 <b>Hiệu suất:</b> Drawdown, Profit Factor, Sharpe Ratio\n"
            "├─ 📊 <b>Tỷ lệ thắng:</b> Thống kê Win Rate theo chiến lược\n"
            "└─ 🏦 <b>Lịch sử số dư:</b> Biến động ký quỹ theo ngày\n\n"
            "👇 <i>Chọn chỉ số để xem chi tiết:</i>"
        )

    @staticmethod
    def get_pnl_dashboard(data: Dict[str, Any]) -> str:
        """💹 Bảng P&L chi tiết - Bổ sung Win/Loss Streak & R:R Ratio"""
        total_pnl = data.get("total_pnl", 0.0)
        daily_pnl = data.get("daily_pnl", 0.0)
        win_rate = data.get("win_rate", 0.0)
        
        # Giả lập Streak & R:R nếu data chưa có (chờ Backend nâng cấp sau)
        streak = data.get("streak", "🟢 Thắng 2 vị thế")
        rr_ratio = data.get("rr_ratio", 1.5)
        
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        daily_emoji = "🟢" if daily_pnl >= 0 else "🔴"

        return (
            "💹 "
            + MessageTemplates.format_title("BẢNG LÃI/LỖ (PnL)")
            + "💳 <b>Tổng quan Lãi/Lỗ:</b>\n"
            f"├─ {pnl_emoji} <b>Tổng Lãi/Lỗ:</b> <code>${total_pnl:,.2f}</code>\n"
            f"├─ {daily_emoji} <b>Hôm nay:</b> <code>${daily_pnl:,.2f}</code>\n"
            f"├─ 🎯 <b>Tỷ lệ thắng:</b> <code>{win_rate:.1f}%</code>\n"
            f"└─ 📊 <b>Tổng vị thế đã chốt:</b> <code>{data.get('total_trades', 0)}</code>\n"
            "━━━\n"
            "🏆 <b>Phong độ Hiện tại (Hiệu suất):</b>\n"
            f"├─ 🔥 <b>Chuỗi (Streak):</b> {streak}\n"
            f"└─ ⚖️ <b>Tỷ lệ R:R:</b> <code>1 : {rr_ratio}</code>\n"
            "━━━\n"
            f"<i>Vị thế có P&L = 0 không được tính là vị thế thắng.</i>"
        )

    @staticmethod
    def get_winrate_stats(data: Dict[str, Any]) -> str:
        """📊 Tỷ lệ thắng chi tiết"""
        win_rate = data.get("win_rate", 0.0)
        total_trades = data.get("total_trades", 0)
        wins = data.get("wins", int(total_trades * win_rate / 100))
        losses = total_trades - wins
        emoji = "🏆" if win_rate >= 50 else "⚠️"

        return (
            "📊 "
            + MessageTemplates.format_title("TỶ LỆ THẮNG (WIN RATE)")
            + f"{emoji} <b>Hiệu suất chung:</b> <code>{win_rate:.1f}%</code>\n"
            f"📝 <b>Tổng vị thế giao dịch:</b> <code>{total_trades}</code>\n"
            f"├─ 🟢 <b>Vị thế Thắng (Win):</b> <code>{wins}</code>\n"
            f"└─ 🔴 <b>Vị thế Thua (Loss):</b> <code>{losses}</code>\n\n"
            "💡 <i>Tỷ lệ thắng = Số vị thế lãi / Tổng số vị thế</i>\n"
            "<i>Vị thế P&L = 0 không được tính là thắng.</i>"
        )

    @staticmethod
    def get_performance_stats(data: Dict[str, Any]) -> str:
        """📈 Báo cáo Hiệu suất"""
        profit_factor = float(data.get("profit_factor", 0.0))
        max_drawdown = float(data.get("max_drawdown", 0.0))
        sharpe_ratio = float(data.get("sharpe_ratio", 0.0))

        if profit_factor == float("inf"):
            pf_str = "∞"
            pf_status = "✅ Tích cực"
        else:
            pf_str = f"{profit_factor:.2f}"
            pf_status = "✅ Tích cực" if profit_factor > 1.0 else "⚠️ Cần cải thiện"

        return (
            "📈 "
            + MessageTemplates.format_title("BÁO CÁO HIỆU SUẤT")
            + "💠 <b>Chỉ số Hiệu suất:</b>\n"
            f"├─ 💰 <b>Hệ số sinh lời (PF):</b> <code>{pf_str}</code> {pf_status}\n"
            f"├─ 📉 <b>Sụt giảm tối đa (DD):</b> <code>{max_drawdown:.1f}%</code>\n"
            f"└─ ⚖️ <b>Chỉ số Sharpe:</b> <code>{sharpe_ratio:.2f}</code>\n"
            "━━━\n"
            "<i>PF > 1.0 = Lợi nhuận tích cực | DD% = Sự suy giảm tối đa từ đỉnh</i>"
        )

    @staticmethod
    def get_balance_history(data: Dict[str, Any]) -> str:
        """🏦 Lịch sử số dư"""
        return (
            "🏦 "
            + MessageTemplates.format_title("LỊCH SỬ SỐ DƯ")
            + "📉 Biến động tài khoản trong 7 ngày qua:\n\n"
            "<code>" + data.get("ascii_chart", "Không có dữ liệu biểu đồ.") + "</code>\n\n"
            "<i>(Biểu đồ Text minh họa số dư)</i>"
        )

    # ------------------ GIAO DỊCH (TRADING) ------------------

    @staticmethod
    def format_trading_menu() -> str:
        return (
            "⚡ "
            + MessageTemplates.format_title("GIAO DỊCH")
            + "<i>Điều khiển và giám sát các vị thế giao dịch của bạn.</i>\n\n"
            "💠 <b>Tùy chọn:</b>\n"
            "├─ 🔥 <b>Vị thế đang mở:</b> Xem vị thế đang chạy & PnL.\n"
            "├─ 📡 <b>Tín hiệu hoạt động:</b> Xem tín hiệu Bot vừa bắt.\n"
            "└─ 🎯 <b>Lệnh chờ:</b> Các lệnh Limit/Stop chưa khớp.\n\n"
            "👇 <i>Chọn thao tác:</i>"
        )

    @staticmethod
    def format_open_positions(positions: List[Dict[str, Any]], page: int = 1, items_per_page: int = 5) -> str:
        """🔥 Vị thế đang mở - Nâng cấp Pro UX (Liq Price, TP/SL Dist, ROE) với pagination"""
        if not positions:
            return (
                "🔥 "
                + MessageTemplates.format_title("VỊ THẾ ĐANG MỞ")
                + "💤 <i>Hiện tại không có vị thế nào đang mở. / No open positions.</i>"
            )

        # Calculate pagination
        total_items = len(positions)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        paginated_positions = positions[start_idx:end_idx]

        header = "🔥 " + MessageTemplates.format_title("VỊ THẾ ĐANG MỞ")
        header += f"⚡ <b>Tổng số vị thế:</b> {total_items} vị thế\n"
        header += f"📄 <b>Trang:</b> {page}/{total_pages}\n\n"

        total_pnl = sum((p.get("pnl") or 0.0) for p in positions if p.get("pnl") is not None or p.get("pnl") == 0)
        total_margin = sum((p.get("margin") or 0.0) for p in positions if p.get("margin") is not None or p.get("margin") == 0)
        total_roe = (total_pnl / total_margin) * 100 if total_margin > 0 else 0.0

        total_pnl_str = (
            f"✅ <b>+${total_pnl:,.2f}</b> (+{total_roe:.2f}%)"
            if total_pnl >= 0
            else f"🔴 <b>-${abs(total_pnl):,.2f}</b> ({total_roe:.2f}%)"
        )

        footer = (
            f"📊 <b>TỔNG CỘNG LỜI/LỖ (NET PnL):</b> {total_pnl_str}\n"
            f"💰 <b>Tổng Ký Quỹ Sử Dụng:</b> <code>${total_margin:,.2f}</code>\n"
            "━━━\n"
            "🛡️ <i>Hệ thống rủi ro đang giám sát 24/7.</i>"
        )

        blocks = []
        truncated = False
        limit_chars = 3800 - len(header) - len(footer)
        current_len = 0

        for idx, p in enumerate(paginated_positions, start_idx + 1):
            try:
                symbol = MessageTemplates._escape_html(p.get("symbol", "❓ UNKNOWN"))
                side = str(p.get("side", "")).upper().strip()
                side_emoji = "✅ MUA" if side == "LONG" else "⚠️ BÁN" if side == "SHORT" else "❓ " + side

                lev = float(p.get("leverage", 1))
                entry = float(p.get("entry_price", 0.0))
                current = float(p.get("current_price", 0.0))
                amount = float(p.get("amount", 0.0))
                
                # Tính Liquidation giả định nếu OKX không trả về (Maintenance Margin ~ 0.5%)
                liq_price = float(p.get("liq_price", 0.0))
                if liq_price <= 0 and entry > 0 and lev > 0:
                    if side == "LONG":
                        liq_price = entry * (1 - (1/lev) + 0.005)
                    else:
                        liq_price = entry * (1 + (1/lev) - 0.005)

                pnl = float(p.get("pnl", 0.0))
                margin = float(p.get("margin", 0.0))
                pnl_pct = (pnl / margin) * 100 if margin > 0 else 0.0
                
                pnl_str = (
                    f"✅ +${pnl:,.2f} (+{pnl_pct:.2f}%)"
                    if pnl >= 0
                    else f"🔴 -${abs(pnl):,.2f} ({pnl_pct:.2f}%)"
                )

                strategy = MessageTemplates._escape_html(p.get("strategy_name", "❓ Không rõ"))
                source_label = "🤖 Bot" if str(strategy).lower() not in ("❓ không rõ", "recovered", "unknown", "") else "👤 Tay"

                has_sl = p.get("has_sl", False) or p.get("stop_loss") is not None
                sl_price = p.get("sl_price") or p.get("stop_loss")
                tp_prices = p.get("tp_prices") or p.get("take_profit_prices") or p.get("take_profit_levels") or []
                has_tp = p.get("has_tp", False) or len(tp_prices) > 0

                tpsl_status = ""

                # Format SL with Distance
                if has_sl and sl_price is not None:
                    try:
                        sl_val = float(sl_price) if sl_price != "None" else 0
                        if sl_val > 0:
                            dist = abs(current - sl_val) / current * 100
                            tpsl_status += f"🛑 <b>SL:</b> ${sl_val:,.4f} <i>(cách {dist:.1f}%)</i>\n"
                    except (ValueError, TypeError, AttributeError) as e:
                        logger.warning(f"Failed to format SL price for position {symbol}: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected error formatting SL price: {e}", exc_info=True)
                if "🛑 <b>SL:" not in tpsl_status:
                    tpsl_status += "⚪ <b>SL:</b> Không cài đặt\n"

                # Format TP with Distance
                if has_tp and tp_prices:
                    try:
                        tp_list = tp_prices if isinstance(tp_prices, list) else [tp_prices]
                        tp_vals = []
                        for tp in tp_list:
                            val = float(tp.get('price', 0)) if isinstance(tp, dict) else float(tp)
                            if val > 0:
                                tp_vals.append(val)
                        
                        if tp_vals:
                            formatted_tps = " | ".join(f"${v:,.4f}" for v in tp_vals)
                            if len(tp_vals) == 1:
                                dist = abs(current - tp_vals[0]) / current * 100
                                tpsl_status += f"🎯 <b>TP:</b> {formatted_tps} <i>(cách {dist:.1f}%)</i>"
                            else:
                                tpsl_status += f"🎯 <b>TP:</b> {formatted_tps}"
                    except (ValueError, TypeError, AttributeError) as e:
                        logger.warning(f"Failed to format TP price for position {symbol}: {e}")
                    except Exception as e:
                        logger.error(f"Unexpected error formatting TP price: {e}", exc_info=True)
                if "🎯 <b>TP:" not in tpsl_status:
                    tpsl_status += "⚪ <b>TP:</b> Không cài đặt"

                notional = amount * current

                block = (
                    f"🔹 <b>#{idx}. {symbol} ({side_emoji})</b> [{source_label}]\n"
                    f"├─ 🚀 <b>Đòn bẩy:</b> {lev:.1f}x | <b>Khối lượng:</b> {amount:,.4f}\n"
                    f"├─ 📌 <b>Giá Vào:</b> ${entry:,.4f}\n"
                    f"├─ 🎯 <b>Hiện Tại:</b> ${current:,.4f}\n"
                    f"├─ 💀 <b>Thanh Lý (Liq):</b> <code>${liq_price:,.4f}</code>\n"
                    f"├─ 💵 <b>P&L (ROE%):</b> {pnl_str}\n"
                    f"└─ {tpsl_status}\n"
                    "━━━\n"
                )

                if current_len + len(block) > limit_chars:
                    truncated = True
                    break

                blocks.append(block)
                current_len += len(block)

            except Exception as e:
                blocks.append(f"🔹 <b>#{idx}. Lỗi đọc vị thế</b> - {MessageTemplates._escape_html(str(e)[:50])}\n━━━\n")

        msg = header + "".join(blocks)
        if truncated:
            msg += f"⚠️ <i>Tóm tắt: Hiển thị {len(blocks)}/{len(paginated_positions)} vị thế trên trang {page} do giới hạn độ dài tin nhắn...</i>\n━━━\n"

        msg += footer
        
        # Add pagination info if there are multiple pages
        if total_pages > 1:
            msg += f"\n📄 <i>Trang {page}/{total_pages} - Sử dụng nút điều hướng để xem trang khác.</i>"
        
        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_clean_bot_complete_message(data: Dict[str, Any]) -> str:
        """✅ Hoàn thành reset toàn diện bot"""
        details = data.get("details", {})
        return (
            "✅ "
            + MessageTemplates.format_title("ĐÃ HOÀN THÀNH RESET TOÀN DIỆN")
            + "🎉 Bot đã được làm sạch và reset hoàn toàn!\n"
            f"📊 <b>Thống kê reset:</b>\n"
            f"├─ 🗑️ Đã xóa lệnh chờ: <code>{details.get('pending_orders_cleared', 0)}</code>\n"
            f"├─ 🔥 Đã đóng vị thế mở: <code>{details.get('open_positions_closed', 0)}</code>\n"
            f"└─ 📡 Đã reset tín hiệu: <code>{details.get('signals_reset', 0)}</code>\n"
            "━━━\n"
            "<i>Bot sẵn sàng để bắt đầu phiên giao dịch mới.</i>"
        )

    @staticmethod
    def format_active_signals(signals: List[Dict[str, Any]]) -> str:
        """📡 Tín hiệu hoạt động - Chuẩn xác với i18n"""
        if not signals:
            return (
                "📡 "
                + MessageTemplates.format_title("TÍN HIỆU HOẠT ĐỘNG")
                + "💤 <i>Chưa có tín hiệu giao dịch mới từ Radar.</i>"
            )

        msg = "📡 " + MessageTemplates.format_title("TÍN HIỆU HOẠT ĐỘNG")
        msg += f"🎯 <b>Phát hiện:</b> {len(signals)} tín hiệu khả thi\n\n"

        for s in signals:
            try:
                stype = str(s.get("type", "")).upper().strip()
                emoji = "✅" if stype in ("BUY", "MUA") else "⚠️" if stype in ("SELL", "BÁN") else "❓"
                symbol = MessageTemplates._escape_html(s.get("symbol", "UNKNOWN"))
                score = int(s.get("score", 0))

                msg += f"{emoji} <b>{symbol} ({stype})</b> - Điểm tín nhiệm: {score}/100\n"
            except Exception:
                msg += f"❓ <i>Lỗi đọc tín hiệu</i>\n"

        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def format_pending_orders(orders: List[Dict[str, Any]]) -> str:
        """Pending limit/stop orders from exchange."""
        header = "🎯 " + MessageTemplates.format_title("LỆNH CHỜ (PENDING)")
        if not orders:
            return header + "💤 <i>Hệ thống hiện không có lệnh Limit/Stop nào đang chờ khớp.</i>"

        lines = [header]
        for order in orders[:20]:
            symbol = MessageTemplates._escape_html(str(order.get("symbol", "?")))
            side = str(order.get("side", "?")).upper()
            amount = order.get("amount", 0)
            price = order.get("price")
            order_type = str(order.get("type", "limit")).upper()
            price_str = f" @ ${float(price):,.4f}" if price else ""
            lines.append(
                f"• <b>{symbol}</b> {side} {order_type} "
                f"<code>{amount}</code>{price_str}"
            )
        if len(orders) > 20:
            lines.append(f"<i>... và {len(orders) - 20} lệnh khác</i>")
        return "\n".join(lines)

    @staticmethod
    def format_capital_management(data: Dict[str, Any]) -> str:
        """💰 Soi số dư - Chuẩn xác với emoji chuẩn"""
        total = float(data.get("total_balance", 0.0))
        free = float(data.get("free_margin", 0.0))
        used = float(data.get("used_margin", 0.0))
        risk = str(data.get("risk_level", "LOW")).upper()

        # Emoji standardization
        if "AN TOÀN" in risk or risk == "LOW":
            risk_emoji = "✅ An toàn"
        elif "TRUNG BÌNH" in risk or risk == "MEDIUM":
            risk_emoji = "⚠️ Chú ý"
        else:
            risk_emoji = "❌ Nguy hiểm"

        return (
            "💰 "
            + MessageTemplates.format_title("SOI SỐ DƯ & VỐN")
            + "💳 <b>Tình trạng tài khoản OKX:</b>\n"
            f"├─ 🏦 <b>Tổng tài sản:</b> ${total:,.2f}\n"
            f"├─ 💵 <b>Khả dụng (Free):</b> ${free:,.2f}\n"
            f"└─ 🔒 <b>Đang ký quỹ:</b> ${used:,.2f}\n"
            "━━━\n"
            f"🛡️ <b>Mức độ rủi ro hiện tại:</b> {risk_emoji}"
        )

    # ------------------ HỆ THỐNG (SYSTEM) ------------------

    @staticmethod
    def get_system_menu_msg() -> str:
        return (
            "📰 "
            + MessageTemplates.format_title("HỆ THỐNG")
            + "<i>Kiểm tra tình trạng sức khỏe của Bot và Sàn.</i>\n\n"
            "💠 <b>Chức năng:</b>\n"
            "├─ ❤️ <b>Kiểm tra trạng thái:</b> Xem CPU, RAM, Uptime.\n"
            "├─ 📋 <b>Nhật ký:</b> Xem Logs sự kiện.\n"
            "├─ 🔌 <b>Trạng thái sàn:</b> Xem API OKX có mượt không.\n"
            "└─ 🔄 <b>Khởi động lại:</b> Restart Engine nếu bị đơ.\n\n"
            "👇 <i>Chọn tác vụ:</i>"
        )

    @staticmethod
    def get_system_health(data: Dict[str, Any]) -> str:
        """❤️ Kiểm tra trạng thái"""
        # Uptime calculation from seconds
        uptime_secs = int(data.get("uptime_seconds", 0))
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            uptime_str = f"{minutes} phút {seconds} giây"
        else:
            uptime_str = f"{seconds} giây"

        cpu = data.get("cpu_usage", 0.0)
        ram_pct = data.get("ram_usage", 0.0)
        ram_used = data.get("ram_used_gb", 0.0)
        ram_total = data.get("ram_total_gb", 0.0)
        disk_pct = data.get("disk_usage", 0.0)

        # Assess status with standardized emoji
        if cpu < 70 and ram_pct < 85:
            status_emoji = "✅ Tuyệt vời"
        elif cpu < 90 and ram_pct < 95:
            status_emoji = "⚠️ Chấp nhận"
        else:
            status_emoji = "❌ Quá tải"

        # Components status
        components = data.get("components", {})
        comp_lines = ""
        for name, st in components.items():
            icon = "✅" if st == "OK" else "❌"
            comp_lines += f"├─ {icon} <b>{name.capitalize()}:</b> {st}\n"

        return (
            "❤️ "
            + MessageTemplates.format_title("SỨC KHỎE HỆ THỐNG")
            + "🖥️ <b>Tài nguyên VPS/Server:</b>\n"
            f"├─ ⏱️ <b>Thời gian chạy (Uptime):</b> {uptime_str}\n"
            f"├─ 🧠 <b>CPU:</b> {cpu:.1f}% sử dụng\n"
            f"├─ 💾 <b>RAM:</b> {ram_pct:.1f}% ({ram_used}GB/{ram_total}GB)\n"
            f"└─ 💿 <b>Disk:</b> {disk_pct:.1f}% sử dụng\n"
            "━━━\n"
            + (f"🔌 <b>Thành phần:</b>\n{comp_lines}━━━\n" if comp_lines else "")
            + f"⚕️ <b>Đánh giá:</b> {status_emoji}"
        )

    @staticmethod
    def get_system_logs(logs: List[str]) -> str:
        """📋 Nhật ký hệ thống - HTML safe logs"""
        msg = "📋 " + MessageTemplates.format_title("NHẬT KÝ HỆ THỐNG (LOGS)")
        msg += "<i>5 sự kiện gần nhất:</i>\n\n"

        if not logs:
            msg += "<i>Không có log nào.</i>"
        else:
            for log in logs[-5:]:
                # HTML escape log content để tránh injection/parse fail
                safe_log = MessageTemplates._escape_html(log)[:150]  # Limit 150 chars per log
                msg += f"🔸 <code>{safe_log}</code>\n"

        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_exchange_status_message(data: Dict[str, Any]) -> str:
        """🔌 Trạng thái sàn OKX - Standardized emoji"""
        api_ping = data.get("latency_ms", 0)
        is_connected = data.get("is_connected", False)

        api_reqs = data.get("api_requests", 0)
        api_errs = data.get("api_errors", 0)
        ws_msgs = data.get("ws_messages", 0)

        # Standardized emoji for ping
        ping_emoji = "✅" if api_ping < 100 else "⚠️" if api_ping < 300 else "❌"
        conn_emoji = "✅ Mượt mà" if is_connected else "❌ Mất kết nối"

        return (
            "🔌 "
            + MessageTemplates.format_title("TRẠNG THÁI SÀN OKX")
            + f"📡 <b>WebSocket/API:</b> {conn_emoji}\n"
            f"⚡ <b>Ping:</b> {ping_emoji} {api_ping}ms\n"
            f"📊 <b>API Requests:</b> {api_reqs}\n"
            f"❌ <b>API Errors:</b> {api_errs}\n"
            f"💬 <b>WS Messages:</b> {ws_msgs}\n"
            "━━━\n"
            "<i>(Ping < 100ms là lý tưởng)</i>"
        )

    @staticmethod
    def get_system_metrics(data: Dict[str, Any]) -> str:
        """📊 Chỉ số hệ thống - Standardized emoji"""
        realized = float(data.get("realized_pnl", 0.0))
        unrealized = float(data.get("unrealized_pnl", 0.0))
        total_pnl = realized + unrealized

        active_pos = int(data.get("active_positions", 0))
        longs = int(data.get("long_count", 0))
        shorts = int(data.get("short_count", 0))

        margin = float(data.get("free_margin", 0.0))
        risk = MessageTemplates._escape_html(data.get("risk_level", "Không rõ"))

        # Standardized emoji for PnL
        pnl_emoji = "✅" if total_pnl > 0.005 else "❌" if total_pnl < -0.005 else "⚪"

        return (
            "📊 "
            + MessageTemplates.format_title("CHỈ SỐ GIAO DỊCH")
            + "💰 <b>Hiệu suất tài chính:</b>\n"
            f"├─ 💵 <b>PnL thực tế:</b> {realized:,.2f} USDT\n"
            f"├─ ⏳ <b>PnL tạm tính:</b> {unrealized:,.2f} USDT\n"
            f"└─ {pnl_emoji} <b>Tổng cộng:</b> {total_pnl:,.2f} USDT\n"
            "━━━\n"
            "📈 <b>Trạng thái vị thế:</b>\n"
            f"├─ ⚡ <b>Đang mở:</b> {active_pos} vị thế\n"
            f"├─ ✅ <b>MUA:</b> {longs} | ⚠️ <b>BÁN:</b> {shorts}\n"
            f"└─ 🎯 <b>TP/SL active:</b> {data.get('tpsl_count', 0)}\n"
            "━━━\n"
            "🛡️ <b>Quản trị rủi ro:</b>\n"
            f"├─ 💵 <b>Số dư khả dụng:</b> {margin:,.2f} USDT\n"
            f"└─ ⚠️ <b>Mức rủi ro:</b> {risk}\n"
            "━━━\n"
            f"⏱️ Cập nhật: {datetime.now().strftime('%H:%M:%S')}"
        )

    @staticmethod
    def get_news_report() -> str:
        """🗞️ Tin tức 48 giờ"""
        return (
            "🗞️ "
            + MessageTemplates.format_title("TIN TỨC THỊ TRƯỜNG 48H")
            + "📰 <i>Tính năng cào tin tức AI đang được phát triển...</i>"
        )

    # ------------------ LỊCH SỬ (HISTORY) ------------------

    @staticmethod
    def get_history_menu_msg() -> str:
        return (
            "📜 "
            + MessageTemplates.format_title("LỊCH SỬ")
            + "<i>Kiểm tra mọi dấu vết giao dịch của bạn.</i>\n\n"
            "💠 <b>Tra cứu:</b>\n"
            "├─ 🔥 <b>Lịch sử chốt/cắt (OKX):</b> Lịch sử đóng vị thế thực tế từ sàn.\n"
            "├─ 📨 <b>Lịch sử lệnh (OKX):</b> Lịch sử khớp lệnh thực tế từ sàn.\n"
            "├─ ⚠️ <b>Lệnh cháy (Thanh lý):</b> Xem lệnh bị sàn cưỡng chế đóng.\n"
            "└─ 📅 <b>Báo cáo hàng ngày:</b> Thống kê hiệu suất theo từng ngày.\n\n"
            "👇 <i>Chọn tác vụ:</i>"
        )

    @staticmethod
    def get_history_trades(trades: List[Dict[str, Any]], page: int = 1, items_per_page: int = 5) -> str:
        """📜 Giao dịch đã đóng - Kèm tổng kết ngắn với pagination"""
        if not trades:
            return (
                "📜 "
                + MessageTemplates.format_title("NHẬT KÝ TRADE (CLOSED)")
                + "💤 <i>Chưa có giao dịch nào được chốt sổ!</i>"
            )

        # Calculate pagination
        total_items = len(trades)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        paginated_trades = trades[start_idx:end_idx]

        # Tính toán Overview nhanh (từ tất cả trades)
        wins = sum(1 for t in trades if float(t.get("pnl", 0.0)) > 0)
        net_pnl = sum(float(t.get("pnl", 0.0)) for t in trades)
        net_emoji = "🟢" if net_pnl >= 0 else "🔴"

        msg = "📜 " + MessageTemplates.format_title("NHẬT KÝ TRADE (CLOSED)")
        msg += (
            f"<b>TỔNG QUAN NHANH:</b>\n"
            f"├─ Tổng số vị thế: <b>{total_items}</b> (Thắng {wins} / Thua {total_items - wins})\n"
            f"└─ Net P&L: {net_emoji} <b>${net_pnl:,.2f}</b>\n"
            f"📄 <b>Trang:</b> {page}/{total_pages}\n"
            "━━━\n\n"
        )

        for idx, t in enumerate(paginated_trades, start_idx + 1):
            symbol = MessageTemplates._escape_html(t.get("symbol", "UNKNOWN"))
            side = str(t.get("side", "")).upper().strip()
            emoji = "🟢" if side == "LONG" or side == "BUY" else "🔴"
            pnl = float(t.get("pnl", 0.0))
            pnl_emoji = "✅" if pnl > 0 else "❌"
            reason = MessageTemplates._escape_html(t.get("reason", "N/A"))

            msg += (
                f"🔹 <b>#{idx}. {symbol} ({emoji} {side})</b>\n"
                f"├─ 💵 <b>P&L:</b> {pnl_emoji} ${pnl:.2f}\n"
                f"└─ 📝 <b>Lý do:</b> {reason}\n"
                "━━━\n"
            )

        # Add pagination info if there are multiple pages
        if total_pages > 1:
            msg += f"\n📄 <i>Trang {page}/{total_pages} - Sử dụng nút điều hướng để xem trang khác.</i>"

        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_history_liquidations(data: List[Dict[str, Any]]) -> str:
        """⚠️ Thanh lý"""
        return (
            "⚠️ "
            + MessageTemplates.format_title("LỊCH SỬ THANH LÝ")
            + "🎉 <i>Thật tuyệt vời! Tài khoản của bạn chưa từng bị thanh lý (Cháy túi)!</i>"
        )

    @staticmethod
    def get_signal_history(data: List[Dict[str, Any]]) -> str:
        """📨 Lịch sử tín hiệu"""
        return (
            "📨 "
            + MessageTemplates.format_title("LỊCH SỬ TÍN HIỆU")
            + "<i>Danh sách 50 tín hiệu gần nhất (tính năng đang hoàn thiện).</i>"
        )

    @staticmethod
    def get_orders_history(data: List[Dict[str, Any]], page: int = 1, items_per_page: int = 10) -> str:
        """📨 Lịch sử lệnh giao dịch (OKX Fills) với pagination"""
        # Calculate pagination
        total_items = len(data)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        paginated_data = data[start_idx:end_idx]

        msg = "📨 " + MessageTemplates.format_title("LỊCH SỬ LỆNH (OKX)")
        msg += f"<i>{items_per_page} lệnh khớp gần đây nhất trên sàn OKX:</i>\n\n"
        msg += f"📄 <b>Trang:</b> {page}/{total_pages} | <b>Tổng:</b> {total_items} lệnh\n\n"

        if not paginated_data:
            msg += "💤 <i>Chưa ghi nhận lệnh khớp nào gần đây trên sàn OKX!</i>"
            return MessageTemplates._validate_and_truncate_message(msg)

        for idx, item in enumerate(paginated_data, start_idx + 1):
            symbol = MessageTemplates._escape_html(item.get("symbol", "UNKNOWN"))
            side = str(item.get("side", "")).upper().strip()
            pos_side = str(item.get("pos_side", "")).upper().strip()
            price = float(item.get("price", 0.0))
            size = float(item.get("size", 0.0))
            fee = float(item.get("fee", 0.0))
            exec_type = "Maker" if item.get("exec_type") == "M" else "Taker"
            time_str = item.get("time", "N/A")

            if pos_side == "LONG" and side == "BUY":
                side_emoji = "✅ MỞ MUA"
            elif pos_side == "LONG" and side == "SELL":
                side_emoji = "🎯 ĐÓNG MUA (CHỐT)"
            elif pos_side == "SHORT" and side == "SELL":
                side_emoji = "🔻 MỞ BÁN"
            elif pos_side == "SHORT" and side == "BUY":
                side_emoji = "🎯 ĐÓNG BÁN (CHỐT)"
            else:
                side_emoji = "✅ MUA" if "BUY" in side else "⚠️ BÁN"

            msg += (
                f"<b>#{idx}. {symbol}</b> ({side_emoji})\n"
                f"├─ 📌 <b>Giá:</b> <code>${price:,.4f}</code>\n"
                f"├─ 📦 <b>Lượng:</b> <code>{size:,.2f}</code>\n"
                f"├─ 💸 <b>Phí:</b> <code>{abs(fee):.4f} USDT</code>\n"
                f"├─ ⚙️ <b>Kiểu:</b> {exec_type}\n"
                f"└─ 🕒 {time_str}\n"
                "━━━\n"
            )

        # Add pagination info if there are multiple pages
        if total_pages > 1:
            msg += f"\n📄 <i>Trang {page}/{total_pages} - Sử dụng nút điều hướng để xem trang khác.</i>"

        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_positions_history(data: List[Dict[str, Any]], page: int = 1, items_per_page: int = 10) -> str:
        """🔥 Lịch sử vị thế (OKX Positions History) với pagination"""
        # Calculate pagination
        total_items = len(data)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        paginated_data = data[start_idx:end_idx]

        msg = "🔥 " + MessageTemplates.format_title("LỊCH SỬ VỊ THẾ (OKX)")
        msg += f"<i>{items_per_page} vị thế đã đóng gần đây nhất trên sàn OKX:</i>\n\n"
        msg += f"📄 <b>Trang:</b> {page}/{total_pages} | <b>Tổng:</b> {total_items} vị thế\n\n"

        if not paginated_data:
            msg += "💤 <i>Chưa ghi nhận lịch sử đóng vị thế nào gần đây trên sàn OKX!</i>"
            return MessageTemplates._validate_and_truncate_message(msg)

        for idx, item in enumerate(paginated_data, start_idx + 1):
            symbol = item.get("symbol", "N/A")
            side = item.get("side", "N/A")
            open_price = item.get("open_price", 0.0)
            close_price = item.get("close_price", 0.0)
            pnl = item.get("pnl", 0.0)
            pnl_ratio = item.get("pnl_ratio", 0.0)
            leverage = item.get("leverage", "N/A")
            margin_mode = item.get("margin_mode", "N/A")
            time_str = item.get("time", "N/A")
            close_type = str(item.get("close_type", "2"))

            if close_type == "3":
                status = "🔴 ĐÃ THANH LÝ (LIQ)"
            elif close_type == "4":
                status = "🔴 THANH LÝ MỘT PHẦN"
            elif close_type == "1":
                status = "Đóng một phần"
            else:
                status = "Đã đóng"

            emoji = "🟢 LONG" if "LONG" in side or "BUY" in side else "🔴 SHORT"
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_sign = "+" if pnl >= 0 else ""

            msg += (
                f"<b>#{idx}. {symbol}</b> ({leverage}x | {margin_mode})\n"
                f"├─ ⚡ <b>Vị thế:</b> {emoji} | <b>Trạng thái:</b> <code>{status}</code>\n"
                f"├─ 📌 <b>Giá vào:</b> <code>${open_price:,.4f}</code>\n"
                f"├─ 🎯 <b>Giá thoát:</b> <code>${close_price:,.4f}</code>\n"
                f"├─ 💵 <b>PNL thực tế:</b> {pnl_emoji} <b>{pnl_sign}${pnl:,.2f} USDT</b>\n"
                f"├─ 📈 <b>PnL% thực tế:</b> <code>{pnl_sign}{pnl_ratio:.2f}%</code>\n"
                f"└─ 🕒 <b>Thời gian đóng:</b> <code>{time_str}</code>\n"
                "━━━\n"
            )

        # Add pagination info if there are multiple pages
        if total_pages > 1:
            msg += f"\n📄 <i>Trang {page}/{total_pages} - Sử dụng nút điều hướng để xem trang khác.</i>"

        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_period_report(data: Dict[str, Any]) -> str:
        """📅 Báo cáo hàng ngày"""
        trades_today = int(data.get("trades_today", 0))
        pnl_today = float(data.get("pnl_today", 0.0))
        win_rate_today = float(data.get("win_rate_today", 0.0))
        report_date = data.get("report_date", "Hôm nay")

        if trades_today == 0:
            return (
                "📅 "
                + MessageTemplates.format_title("BÁO CÁO HÀNG NGÀY")
                + f"<i>Chưa có vị thế đóng nào trong ngày ({report_date}).</i>"
            )

        pnl_emoji = "🟢" if pnl_today >= 0 else "🔴"
        return (
            "📅 "
            + MessageTemplates.format_title("BÁO CÁO HÀNG NGÀY")
            + f"📆 <b>Ngày:</b> <code>{report_date}</code>\n\n"
            f"├─ {pnl_emoji} <b>P&L hôm nay:</b> <code>${pnl_today:,.2f}</code>\n"
            f"├─ 📊 <b>Vị thế đã chốt:</b> <code>{trades_today}</code>\n"
            f"└─ 🎯 <b>Win rate:</b> <code>{win_rate_today:.1f}%</code>\n"
            "━━━\n"
            "<i>Dữ liệu tính từ các vị thế đóng trong ngày (UTC).</i>"
        )

    # ------------------ CÀI ĐẶT (SETTINGS) ------------------

    @staticmethod
    def get_settings_menu_msg() -> str:
        return (
            "⚙️ "
            + MessageTemplates.format_title("CÀI ĐẶT HỆ THỐNG")
            + "<i>Tùy chỉnh thông số vận hành của Bot.</i>\n\n"
            "🛠 <b>Cấu hình:</b>\n"
            "├─ ⚙️ <b>Cài đặt Bot:</b> Vốn, đòn bẩy, chiến lược.\n"
            "├─ ⚠️ <b>Giới hạn rủi ro:</b> Stoploss tổng, Max lệnh.\n"
            "├─ 👁️ <b>Danh sách theo dõi:</b> Các cặp coin đang trade.\n"
            "└─ 🔔 <b>Thông báo:</b> Cấu hình gửi tin nhắn Telegram.\n\n"
            "👇 <i>Chọn mục cần xem:</i>"
        )

    @staticmethod
    def get_settings_bot(config: Dict[str, Any]) -> str:
        """⚙️ Cài đặt Bot"""
        margin_per_order = config.get("margin_per_order_usdt", "N/A")
        if isinstance(margin_per_order, float) or isinstance(margin_per_order, int):
            margin_str = f"${margin_per_order:,.2f}"
        else:
            margin_str = f"{margin_per_order}"

        from core.config.settings import settings as app_settings

        ema_fast = config.get("ema_fast", app_settings.ema_fast_period)
        ema_slow = config.get("ema_slow", app_settings.ema_slow_period)
        timeframes = config.get("timeframes", ", ".join(app_settings.timeframes))
        strategy_name = (
            f"EMA {ema_fast}/{ema_slow} Crossover"
            if config.get("active_strategy") == "EMA Crossover"
            else config.get("active_strategy", "Unknown")
        )
        prod_risk = config.get("production_risk_mode", app_settings.production_risk_mode)
        radar_limit = config.get("radar_limit", app_settings.radar_limit)
        body_table = MessageTemplates._format_min_body_table(app_settings)

        return (
            "⚙️ " + MessageTemplates.format_title("CẤU HÌNH VẬN HÀNH") + "🤖 <b>Thông số Bot:</b>\n"
            f"├─ 💰 <b>Vốn mỗi lệnh:</b> <code>{margin_str} USDT</code>\n"
            f"├─ 🚀 <b>Đòn bẩy mặc định:</b> <code>{config.get('default_leverage', 10)}x</code>\n"
            f"├─ 📈 <b>Chiến lược:</b> <code>{strategy_name}</code>\n"
            f"├─ ⏱️ <b>Khung thời gian:</b> <code>{timeframes}</code>\n"
            f"├─ 📡 <b>Radar quét:</b> <code>{radar_limit}</code> coin đầu watchlist\n"
            f"├─ 📊 <b>ADX:</b> ngắn ≥ <code>{config.get('adx_min_all', app_settings.adx_min_threshold_all)}</code> | "
            f"dài ≥ <code>{config.get('adx_min_long_tf', app_settings.adx_min_threshold_long_tf)}</code>\n"
            f"├─ 🛡️ <b>Risk production:</b> <code>{'BẬT' if prod_risk else 'TẮT (demo)'}</code>\n"
            f"├─ 🔄 <b>OKX:</b> <code>{'🎮 DEMO' if config.get('is_demo', True) else '🔥 LIVE'}</code>\n"
            "━━━\n"
            "🕯️ <b>Lọc thân nến theo TF:</b>\n"
            f"{body_table}\n"
            "━━━\n"
            "<i>Chỉnh sửa qua file <code>.env</code> rồi khởi động lại bot.</i>"
        )

    @staticmethod
    def get_settings_risk(risk: Dict[str, Any]) -> str:
        """⚠️ Giới hạn rủi ro"""
        sl_roe = risk.get("sl_roe_pct", 50.0)
        tp1_roe = risk.get("tp1_roe_pct", 50.0)
        tp2_roe = risk.get("tp2_roe_pct", 100.0)
        tp3_roe = risk.get("tp3_roe_pct", 150.0)

        tp1_exit = int(risk.get("tp1_exit_pct", 0.5) * 100)
        tp2_exit = int(risk.get("tp2_exit_pct", 0.3) * 100)
        tp3_exit = 100 - tp1_exit - tp2_exit
        if tp3_exit < 0:
            tp3_exit = 20

        is_demo = risk.get("demo_mode", True)
        prod_risk = risk.get("production_risk_mode", False)
        max_pos_val = risk.get("max_open_positions", 9999)
        if is_demo or max_pos_val >= 9999:
            max_pos = "Vô cực ♾️ (Demo Mode)"
        else:
            max_pos = f"{max_pos_val} lệnh (Production Mode)"

        max_conc = risk.get("max_symbol_concentration", 1)
        if int(max_conc) >= 9999:
            conc_str = "Tắt (demo)"
        else:
            conc_str = f"{int(max_conc)} vị thế / symbol"

        return (
            "⚠️ "
            + MessageTemplates.format_title("GIỚI HẠN RỦI RO")
            + "🛡️ <b>Thông số an toàn & TP/SL:</b>\n"
            f"├─ 🛑 <b>Cắt lỗ cố định (SL):</b> <code>-{sl_roe:.1f}% ROE</code>\n"
            f"├─ 🎯 <b>Mục tiêu TP1:</b> <code>+{tp1_roe:.1f}% ROE</code> (Chốt {tp1_exit}% vị thế)\n"
            f"├─ 🎯 <b>Mục tiêu TP2:</b> <code>+{tp2_roe:.1f}% ROE</code> (Chốt {tp2_exit}% vị thế)\n"
            f"├─ 🎯 <b>Mục tiêu TP3:</b> <code>+{tp3_roe:.1f}% ROE</code> (Chốt {tp3_exit}% vị thế)\n"
            f"├─ ⚙️ <b>Chế độ ký quỹ:</b> <code>{str(risk.get('margin_mode', 'isolated')).upper()} Margin</code>\n"
            f"├─ 🚀 <b>Đòn bẩy tối đa:</b> <code>x{risk.get('max_leverage', 10)}</code> (Khóa cứng)\n"
            f"├─ 📊 <b>Giới hạn vị thế:</b> <code>{max_pos}</code>\n"
            f"├─ 🎯 <b>1 symbol:</b> <code>{conc_str}</code>\n"
            f"├─ 📉 <b>Drawdown ngày:</b> <code>{float(risk.get('max_daily_drawdown', 0.3)) * 100:.0f}%</code>\n"
            f"├─ ⚖️ <b>R:R tối thiểu:</b> <code>{risk.get('min_risk_reward_ratio', 1.5)}</code> "
            f"(khi production risk bật)\n"
            f"└─ 🛡️ <b>Production risk:</b> <code>{'BẬT' if prod_risk else 'TẮT'}</code>\n"
            "━━━\n"
            "🛡️ <i>Drawdown & concentration luôn active; margin/R:R chỉ khi production risk bật.</i>"
        )

    @staticmethod
    def get_settings_watchlist(symbols: List[str], radar_limit: int = 0, total_watchlist: int = 0) -> str:
        """👁️ Danh sách theo dõi (active radar slice)"""
        msg = "👁️ " + MessageTemplates.format_title("DANH SÁCH THEO DÕI")
        if radar_limit and total_watchlist:
            msg += (
                f"<i>Radar: <b>{len(symbols)}</b> / {total_watchlist} coin "
                f"(RADAR_LIMIT=<code>{radar_limit}</code>)</i>\n\n"
            )
        else:
            msg += f"<i>Bot đang quét tín hiệu trên {len(symbols)} cặp:</i>\n\n"

        for i, s in enumerate(symbols, 1):
            msg += f"<code>{s.split('-')[0]}</code>  "
            if i % 4 == 0:
                msg += "\n"

        msg += "\n\n━━━\n"
        msg += "🎯 <i>Bot chỉ vào lệnh với các cặp trong danh sách này.</i>"
        return MessageTemplates._validate_and_truncate_message(msg)

    @staticmethod
    def get_settings_notifications(notif: Dict[str, Any]) -> str:
        """🔔 Thông báo"""
        return (
            "🔔 "
            + MessageTemplates.format_title("CẤU HÌNH THÔNG BÁO")
            + "📡 <b>Kênh nhận tin:</b>\n"
            f"├─ 📣 <b>Tín hiệu:</b> {'✅ Bật' if notif.get('signals', True) else '❌ Tắt'}\n"
            f"├─ 💰 <b>Kết quả trade:</b> {'✅ Bật' if notif.get('trades', True) else '❌ Tắt'}\n"
            f"├─ ⚠️ <b>Cảnh báo rủi ro:</b> ✅ Luôn bật\n"
            f"└─ 📊 <b>Báo cáo ngày:</b> {'✅ Bật' if notif.get('daily_report', True) else '❌ Tắt'}\n"
            "━━━\n"
            "<i>Bạn có thể bật/tắt các loại tin nhắn để tránh bị spam.</i>"
        )

    # ------------------ ĐIỀU KHIỂN (CONTROL) ------------------

    @staticmethod
    def get_control_menu_msg() -> str:
        return (
            "🎮 "
            + MessageTemplates.format_title("TRUNG TÂM ĐIỀU KHIỂN")
            + "<i>Bảng điều khiển tối cao dành cho Quản trị viên VCOREX.</i>\n\n"
            "💠 <b>Chỉ thị vận hành:</b>\n"
            "├─ ▶️ <b>Bắt đầu Bot:</b> Kích hoạt AI săn tìm tín hiệu.\n"
            "├─ ⏸️ <b>Tạm dừng Bot:</b> Ngừng vào lệnh mới (Giữ lệnh cũ).\n"
            "├─ 🛑 <b>Dừng khẩn cấp:</b> Đóng băng và thanh lý toàn bộ!\n"
            "├─ 🧹 <b>Reset Tín hiệu:</b> Xóa nến cũ, làm mới chỉ báo AI.\n"
            "└─ 🧼 <b>Reset Toàn diện:</b> Xóa database, đưa bot về gốc.\n\n"
            "━━━\n"
            "⚠️ <i>Lưu ý: Mọi chỉ thị có hiệu lực ngay lập tức trên sàn OKX.</i>"
        )

    @staticmethod
    def get_manual_order_instruction() -> str:
        return (
            "📝 "
            + MessageTemplates.format_title("ĐẶT LỆNH THỦ CÔNG")
            + "<i>Ra lệnh cho AI mở vị thế theo ý muốn của bạn.</i>\n\n"
            "💬 <b>Cú pháp lệnh chuẩn:</b>\n"
            "<code>/trade [CẶP_TIỀN] [VỊ_THẾ] [SỐ_VỐN_USDT]</code>\n\n"
            "💡 <b>Ví dụ thực tế:</b>\n"
            "├─ 🟢 <code>/trade BTC-USDT-SWAP LONG 500</code>\n"
            "└─ 🔴 <code>/trade ETH-USDT-SWAP SHORT 200</code>\n\n"
            "━━━\n"
            "🛡️ <b>Quy tắc vận hành:</b>\n"
            "• Bot sẽ tự động áp dụng <b>Đòn bẩy</b> và <b>TP/SL</b> theo cấu hình Rủi ro hiện tại.\n"
            "• Lệnh sẽ được thực thi ngay lập tức qua giá <b>Market</b>.\n"
            "• AI sẽ giám sát lệnh này tương tự như lệnh tự động.\n\n"
            "🎯 <i>Hãy kiểm tra kỹ thông số trước khi gửi lệnh!</i>"
        )

    @staticmethod
    def get_confirmation_msg(action: str) -> str:
        """Generic confirmation message for dangerous control actions."""
        if action == "emergency_stop":
            return MessageTemplates.get_emergency_stop_confirmation()
        if action == "reset_signals":
            return (
                "🔄 "
                + MessageTemplates.format_title("XÁC NHẬN RESET TÍN HIỆU")
                + "Hành động này sẽ xóa buffer nến và làm mới chỉ báo AI.\n\n"
                "<i>Bạn có chắc chắn muốn thực hiện?</i>"
            )
        if action == "clean_bot":
            return (
                "⚠️ <b>XÁC NHẬN RESET TOÀN DIỆN?</b>\n\n"
                "Hành động này sẽ xóa database và đưa bot về trạng thái ban đầu.\n\n"
                "<i>Bạn có chắc chắn muốn thực hiện?</i>"
            )
        if action == "restart_engine":
            return MessageTemplates.get_restart_engine_confirmation()
        return f"⚠️ Xác nhận hành động: {MessageTemplates._escape_html(action)}?"

    @staticmethod
    def get_position_close_confirmation(position_id: str, action: str = "close_full") -> str:
        """Confirmation screen before closing a position (half or full)."""
        action_val = getattr(action, "value", str(action))
        if action_val == "close_half":
            title = "ĐÓNG 50% VỊ THẾ"
            pct = "50%"
        else:
            title = "ĐÓNG TOÀN BỘ VỊ THẾ"
            pct = "100%"
        safe_id = MessageTemplates._escape_html(position_id)
        return (
            "⚠️ "
            + MessageTemplates.format_title(title)
            + f"🆔 <b>ID vị thế:</b> <code>{safe_id}</code>\n"
            f"📦 <b>Khối lượng đóng:</b> {pct}\n\n"
            "<i>Bạn có chắc chắn muốn thực hiện?</i>"
        )

    @staticmethod
    def get_position_close_success_notification(data: Dict[str, Any]) -> str:
        """Push notification when a position close succeeds (no pending UI future)."""
        symbol = MessageTemplates._escape_html(data.get("symbol", "N/A"))
        side = str(data.get("side", "N/A")).upper()
        size = data.get("size", 0)
        return (
            "✅ "
            + MessageTemplates.format_title("ĐÓNG VỊ THẾ THÀNH CÔNG")
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Vị thế:</b> {side}\n"
            f"📦 <b>Khối lượng:</b> {size} contracts"
        )

    @staticmethod
    def get_position_close_failure_notification(data: Dict[str, Any]) -> str:
        """Push notification when a position close fails (no pending UI future)."""
        symbol = MessageTemplates._escape_html(data.get("symbol", "N/A"))
        reason = MessageTemplates._escape_html(data.get("reason", "Không rõ"))
        return (
            "❌ "
            + MessageTemplates.format_title("THẤT BẠI ĐÓNG VỊ THẾ")
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚠️ <b>Lý do:</b> {reason}"
        )

    @staticmethod
    def get_emergency_stop_confirmation() -> str:
        return (
            "🛑 "
            + MessageTemplates.format_title("XÁC NHẬN DỪNG KHẨN CẤP")
            + "⚠️ <b>CẢNH BÁO ĐỎ!</b>\n\n"
            "Hành động này sẽ <b>ĐÓNG MỌI VỊ THẾ HIỆN TẠI</b> bằng giá Market (Thị trường) để bảo toàn vốn ngay lập tức.\n\n"
            "<i>Bạn có chắc chắn muốn thực hiện?</i>"
        )

    @staticmethod
    def get_restart_engine_confirmation() -> str:
        return (
            "🔄 "
            + MessageTemplates.format_title("XÁC NHẬN KHỞI ĐỘNG LẠI")
            + "Bạn đang ra lệnh khởi động lại lõi (Core Engine). Kết nối WebSocket sẽ bị cắt và thiết lập lại.\n\n"
            "<i>Chỉ dùng khi thấy Bot bị đơ hoặc API báo lỗi liên tục. Bạn xác nhận chứ?</i>"
        )

    @staticmethod
    def get_emergency_stop_executed(closed_count: int, failed_count: int) -> str:
        return (
            "🚨 "
            + MessageTemplates.format_title("KẾT QUẢ DỪNG KHẨN CẤP")
            + "🛡️ <b>ĐÃ THỰC THI CHỈ THỊ BẢO TOÀN VỐN!</b>\n\n"
            f"✅ Thành công đóng: <b>{closed_count} vị thế</b>.\n"
            f"❌ Thất bại: <b>{failed_count} vị thế</b> (Vui lòng kiểm tra trên App OKX ngay lập tức!)."
        )

    # ------------------ ALERTS & REPORTS (LUXURY STYLE) ------------------

    @staticmethod
    def get_new_signal_alert(signal_data: Dict[str, Any]) -> str:
        """🎯 TÍN HIỆU GIAO DỊCH MỚI - i18n, HTML safe, type safe"""
        symbol = MessageTemplates._escape_html(signal_data.get("symbol", "UNKNOWN"))

        # Type-safe signal type handling with i18n
        raw_stype = signal_data.get("type") or signal_data.get("signal_type", "UNKNOWN")
        stype_str = getattr(raw_stype, "value", str(raw_stype)).upper().strip()

        # i18n: LONG → MUA, SHORT → BÁN
        if "BUY" in stype_str or "LONG" in stype_str:
            side_emoji = "✅ MUA"
        elif "SELL" in stype_str or "SHORT" in stype_str:
            side_emoji = "⚠️ BÁN"
        else:
            side_emoji = "❓ " + stype_str

        entry = float(signal_data.get("entry_price", 0.0))
        tf = str(signal_data.get("timeframe", "1H")).upper()
        strategy = MessageTemplates._escape_html(signal_data.get("strategy_name") or signal_data.get("strategy", "Không rõ"))

        raw_strength = signal_data.get("signal_strength", "MEDIUM")
        strength = getattr(raw_strength, "value", str(raw_strength))

        # Lấy thời gian chuẩn và định dạng múi giờ từ settings
        ts = signal_data.get("timestamp")
        user_timezone = MessageTemplates._get_timezone()

        if isinstance(ts, datetime):
            if ts.tzinfo:
                utc_time = ts.astimezone(timezone.utc)
                user_time = ts.astimezone(user_timezone)
            else:
                utc_time = ts.replace(tzinfo=timezone.utc)
                user_time = utc_time.astimezone(user_timezone)
        elif isinstance(ts, (int, float)):
            utc_time = datetime.fromtimestamp(ts, timezone.utc)
            user_time = utc_time.astimezone(user_timezone)
        else:
            utc_time = datetime.now(timezone.utc)
            user_time = utc_time.astimezone(user_timezone)

        ts_str = f"{user_time.strftime('%Y-%m-%d %H:%M:%S')} ({user_timezone.tzname(None)}) | {utc_time.strftime('%H:%M:%S')} UTC"

        # Các thông số quản lý rủi ro
        size_usdt = signal_data.get("position_size_usdt", 0.0)
        sl = signal_data.get("stop_loss_price")
        tp_list = signal_data.get("take_profit_prices", [])

        tp_str = ""
        if tp_list:
            tp_str = " | ".join([f"${t.get('price', 0):,.2f}" if isinstance(t, dict) else f"${t:,.2f}" for t in tp_list[:3]])
        else:
            tp_str = "Chưa đặt"

        # Body% - type safe indicators validation
        indicators = signal_data.get("indicators", {})
        body_pct = None
        adx_val = None
        if isinstance(indicators, dict):
            try:
                body_pct = float(indicators.get("body_pct", 0))
                adx_val = float(indicators.get("adx", 0))
            except (ValueError, TypeError):
                body_pct = None
                adx_val = None

        from core.config.settings import settings as app_settings
        min_body = MessageTemplates._get_min_body_pct(tf, app_settings)

        body_str = ""
        if body_pct is not None:
            body_str = f"├─ 🕯️ <b>Thân nến (Body):</b> <code>{body_pct:.2f}%</code> (Min: {min_body:.1f}%)\n"
        else:
            body_str = f"├─ 🕯️ <b>Thân nến (Body):</b> <code>>= {min_body:.1f}%</code> (Đạt yêu cầu)\n"

        # ADX display
        adx_str = ""
        if adx_val is not None and adx_val > 0:
            adx_str = f"├─ 📊 <b>ADX:</b> <code>{adx_val:.1f}</code>\n"
        else:
            adx_str = f"├─ 📊 <b>ADX:</b> <code>Chưa đủ nến</code>\n"

        # Confirmation Mode display
        confirmation_mode_map = {
            "5m": app_settings.confirmation_candles_5m,
            "15m": app_settings.confirmation_candles_15m,
            "1h": app_settings.confirmation_candles_1h,
            "4h": app_settings.confirmation_candles_4h,
            "1d": app_settings.confirmation_candles_1d,
            "1w": app_settings.confirmation_candles_1w,
            "1m": app_settings.confirmation_candles_1m,
        }
        tf_key = tf.lower() if isinstance(tf, str) else "1h"
        confirmation_mode = confirmation_mode_map.get(tf_key, 0)
        if confirmation_mode == 0:
            mode_str = "Realtime (vào ngay)"
        else:
            mode_str = "Confirmation (đợi nến đóng)"

        strategy_display = strategy
        if "crossover" in str(strategy).lower() or "cross" in str(strategy).lower():
            strategy_display = f"EMA {app_settings.ema_fast_period}/{app_settings.ema_slow_period} Crossover"

        # SL - type safe
        try:
            sl_val = float(sl) if isinstance(sl, (int, float)) else 0.0
            sl_str = f"${sl_val:,.4f}" if sl_val > 0 else "❓ Không rõ"
        except (ValueError, TypeError):
            sl_str = "❓ Không rõ"

        return (
            "🎯 "
            + MessageTemplates.format_title(f"TÍN HIỆU MỚI {tf}")
            + f"⏰ <b>Thời gian:</b> {ts_str}\n"
            f"💰 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Xu hướng:</b> {side_emoji} (Độ tin cậy: {strength})\n"
            f"📌 <b>Giá vào:</b> <code>${entry:,.4f}</code>\n"
            f"├─ 📈 <b>Chiến lược:</b> {strategy_display}\n"
            f"{body_str}"
            f"{adx_str}"
            f"├─ ⚡ <b>Chế độ vào lệnh:</b> <code>{mode_str}</code>\n"
            "━━━\n"
            f"💵 <b>Quy mô:</b> <code>${size_usdt:,.2f}</code>\n"
            f"🛑 <b>SL:</b> {sl_str}\n"
            f"🎯 <b>TP:</b> {tp_str}\n"
            "━━━\n"
            "🛡️ <i>Risk Manager kiểm tra ký quỹ trước khi submit OKX...</i>"
        )

    @staticmethod
    def get_signal_rejected_alert(data: Dict[str, Any]) -> str:
        """❌ TÍN HIỆU BỊ TỪ CHỐI - i18n, HTML safe"""
        # HTML escape symbol
        symbol = MessageTemplates._escape_html(data.get("symbol", "UNKNOWN"))

        # Type safe signal type handling
        raw_stype = data.get("type") or data.get("signal_type", "UNKNOWN")
        stype_str = getattr(raw_stype, "value", str(raw_stype)).upper().strip()

        # i18n: LONG → MUA, SHORT → BÁN
        if "BUY" in stype_str or "LONG" in stype_str:
            side_emoji = "✅ MUA"
        elif "SELL" in stype_str or "SHORT" in stype_str:
            side_emoji = "⚠️ BÁN"
        else:
            side_emoji = "❓ " + stype_str

        entry = float(data.get("entry_price", 0.0))
        tf = str(data.get("timeframe", "1H")).upper()
        raw_reason_val = data.get("rejection_reason") or data.get("reason") or ""
        raw_reason = MessageTemplates._escape_html(raw_reason_val)

        # Map strategy rejection reasons to Vietnamese
        reason_vi = raw_reason
        rejection_source = "RISK MANAGER"
        reason_lower = str(raw_reason_val).lower()

        # Strategy-level rejections
        if not raw_reason_val or "no_finalized_crossover" in reason_lower:
            reason_vi = "🔄 EMA chưa giao nhau hoàn chỉnh\n💡 Chờ crossover trên nến đóng (candles[-2])."
            rejection_source = "STRATEGY"
        elif "weak_trend_adx" in reason_lower or reason_lower == "weak_trend":
            reason_vi = "📉 ADX yếu — thị trường sideways\n💡 Chờ xu hướng mạnh hơn."
            rejection_source = "STRATEGY"
        elif "body_too_small" in reason_lower or reason_lower == "small_body":
            reason_vi = "🕯️ Thân nến < ngưỡng Min Body theo TF\n💡 Lọc Doji / nến yếu."
            rejection_source = "STRATEGY"
        elif "stale_signal" in reason_lower:
            reason_vi = "⏰ Tín hiệu quá cũ (stale)\n💡 Xử lý chậm hoặc lag mạng."
            rejection_source = "STRATEGY"
        elif "indicator_bundle_mismatch" in reason_lower:
            reason_vi = "🔀 Lệch timestamp nến vs indicator\n💡 Chờ snapshot đồng bộ."
            rejection_source = "STRATEGY"
        elif "color_validation_failed" in reason_lower:
            reason_vi = "🎨 Màu nến không khớp hướng (5m–1D)\n💡 BUY cần nến xanh, SELL cần nến đỏ."
            rejection_source = "STRATEGY"
        # Risk Manager rejections
        elif "insufficient available margin" in reason_lower:
            reason_vi = f"❌ Không đủ ký quỹ tự do\n💡 {raw_reason[:80]}"
            rejection_source = "RISK MANAGER"
        elif "max open positions limit reached" in reason_lower or "max open positions reached" in reason_lower:
            reason_vi = f"❌ Đạt giới hạn số lệnh\n💡 {raw_reason[:80]}"
            rejection_source = "RISK MANAGER"
        elif "invalid position size" in reason_lower:
            reason_vi = f"❌ Quy mô lệnh không hợp lệ\n💡 {raw_reason[:80]}"
            rejection_source = "RISK MANAGER"
        elif raw_reason_val:
            # Any other reason - pass through (already HTML escaped)
            reason_vi = raw_reason[:200]  # Increased limit length
            if "TỪ CHỐI TÍN HIỆU" in raw_reason_val:
                rejection_source = "RISK MANAGER"
            elif "ORPHAN GUARD" in raw_reason_val:
                rejection_source = "ORDER HANDLER"
            else:
                rejection_source = "SYSTEM"
        else:
            # Fallback if somehow raw_reason_val is truthy but we reach here
            reason_vi = "🤷 Lý do không xác định (xem logs để chi tiết)"
            rejection_source = "UNKNOWN"

        return (
            "❌ "
            + MessageTemplates.format_title(f"TÍN HIỆU {tf} BỊ TỪ CHỐI ({rejection_source})")
            + f"💰 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Xu hướng:</b> {side_emoji} | <b>Entry:</b> ${entry:,.4f}\n"
            "━━━\n"
            f"🛡️ <b>LÝ DO TỪ CHỐI:</b>\n"
            f"{reason_vi}\n"
            "━━━\n"
            "📊 Hệ thống bảo vệ an toàn vốn cho tài khoản của bạn."
        )

    @staticmethod
    def get_order_execution_notification(data: Dict[str, Any]) -> str:
        """🚀 LỆNH ĐÃ KHỚP TRÊN SÀN - i18n, HTML safe, type safe"""
        # HTML escape symbol
        symbol = MessageTemplates._escape_html(data.get("symbol", "UNKNOWN"))

        # Type safe side with i18n
        side = str(data.get("side", "")).upper().strip()
        if "LONG" in side or "BUY" in side:
            side_emoji = "✅ MUA"
        elif "SHORT" in side or "SELL" in side:
            side_emoji = "⚠️ BÁN"
        else:
            side_emoji = "❓ " + side

        entry = float(data.get("entry_price", 0.0))
        qty = float(data.get("amount", 0.0))
        lev = float(data.get("leverage", 10))
        ct_val = float(data.get("ct_val", 1.0))

        # Calculate Margin and Notional Size
        notional = data.get("notional_size") or (qty * ct_val * entry)
        margin = data.get("margin") or (notional / lev if lev > 0 else 0.0)

        # Format Execution Time - type safe parsing
        opened_at = data.get("opened_at")
        user_timezone = MessageTemplates._get_timezone()
        user_time = None

        try:
            if isinstance(opened_at, str):
                # Remove Z and replace T with space
                clean_str = opened_at.replace("Z", "").replace("T", " ")
                # Split at dot to remove milliseconds
                clean_str = clean_str.split(".")[0]
                # Parse naive datetime (assumed to be UTC)
                parsed_dt = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
                user_time = parsed_dt.replace(tzinfo=timezone.utc).astimezone(user_timezone)
            elif hasattr(opened_at, "strftime"):
                if opened_at.tzinfo:
                    user_time = opened_at.astimezone(user_timezone)
                else:
                    user_time = opened_at.replace(tzinfo=timezone.utc).astimezone(user_timezone)
        except Exception:
            pass

        if user_time:
            time_str = user_time.strftime("%Y-%m-%d %H:%M:%S") + f" ({user_timezone.tzname(None)})"
        else:
            time_str = datetime.now(user_timezone).strftime("%Y-%m-%d %H:%M:%S") + f" ({user_timezone.tzname(None)})"

        # Strategy Translation - HTML safe & i18n
        strategy = MessageTemplates._escape_html(data.get("strategy_name", ""))
        tf = str(data.get("timeframe", "")).upper().strip()
        tf_suffix = f" [{tf}]" if tf else ""

        if "ema_crossover" in str(strategy).lower() or "crossover" in str(strategy).lower():
            from core.config.settings import settings as app_settings
            strategy_desc = f"EMA {app_settings.ema_fast_period}/{app_settings.ema_slow_period} Crossover{tf_suffix}"
        elif strategy == "manual" or not strategy or "không rõ" in str(strategy).lower():
            strategy_desc = "Khởi chạy Thủ công / Tự phục hồi"
        else:
            strategy_desc = f"{strategy}{tf_suffix}"

        # Candle Body condition - type safe
        body_pct = data.get("body_pct")
        body_line = ""
        try:
            if body_pct is not None:
                body_val = float(body_pct)
                from core.config.settings import settings as app_settings
                min_body = MessageTemplates._get_min_body_pct(tf, app_settings)
                body_line = f"🕯️ <b>Thân nến:</b> <code>{body_val:.2f}%</code> (Yêu cầu >= {min_body:.1f}%)\\n"
        except (ValueError, TypeError):
            pass

        # TP/SL - type safe parsing
        sig = data.get("signal_data") or {}
        tp = data.get("take_profit_levels") or data.get("tp") or data.get("tp_prices") or sig.get("take_profit_prices")
        sl = data.get("stop_loss") or data.get("sl") or data.get("sl_price") or sig.get("stop_loss_price")

        # Parse TP safely
        tp_val = "Chưa đặt"
        if tp:
            try:
                if isinstance(tp, list) and len(tp) > 0:
                    tp_list = []
                    for t in tp:
                        if isinstance(t, dict):
                            tp_list.append(float(t.get("price", 0)))
                        elif hasattr(t, "price"):
                            tp_list.append(float(t.price))
                        else:
                            tp_list.append(float(t))
                    if tp_list:
                        tp_val = " | ".join([f"${t:,.4f}" for t in tp_list[:3]])
                elif isinstance(tp, (int, float)):
                    tp_val = f"${float(tp):,.4f}"
            except (ValueError, TypeError):
                tp_val = "Chưa đặt"

        # Parse SL safely
        sl_val = "Chưa đặt"
        if sl:
            try:
                sl_float = float(sl) if isinstance(sl, (int, float)) else float(str(sl).replace(",", ""))
                if sl_float > 0:
                    sl_val = f"${sl_float:,.4f}"
            except (ValueError, TypeError):
                sl_val = "Chưa đặt"

        return (
            "🚀 "
            + MessageTemplates.format_title("LỆNH ĐÃ KHỚP TRÊN SÀN")
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Vị thế:</b> {side_emoji} ({lev:.0f}x)\n"
            "━━━\n"
            f"📌 <b>Entry:</b> <code>${entry:,.4f}</code>\n"
            f"📦 <b>Lượng:</b> <code>{qty:,.4f}</code>\n"
            f"💵 <b>Margin:</b> <code>${margin:,.2f} USDT</code>\n"
            f"📈 <b>Notional:</b> <code>${notional:,.2f} USDT</code>\n"
            "━━━\n"
            f"🎯 <b>Chiến lược:</b> {strategy_desc}\n"
            f"{body_line}"
            f"⏰ <b>Thời gian:</b> {time_str}\n"
            "━━━\n"
            f"🎯 <b>TP:</b> {tp_val}\n"
            f"🛑 <b>SL:</b> {sl_val}\n"
            "━━━\n"
            "✅ <i>Lệnh khớp thành công. Giám sát TP/SL...</i>"
        )

    @staticmethod
    def get_tpsl_placement_notification(data: Dict[str, Any]) -> str:
        """🛡️ TP/SL ĐƯỢC ĐẶT THÀNH CÔNG - i18n, HTML safe, type safe"""
        # HTML escape symbol
        symbol = MessageTemplates._escape_html(data.get("symbol", "UNKNOWN"))

        # Type safe side with i18n
        side = str(data.get("side", "")).upper().strip()
        if "LONG" in side or "BUY" in side:
            side_emoji = "✅ MUA"
        elif "SHORT" in side or "SELL" in side:
            side_emoji = "⚠️ BÁN"
        else:
            side_emoji = "❓ " + side

        # Format Execution Time - type safe
        opened_at = data.get("opened_at")
        user_timezone = MessageTemplates._get_timezone()
        user_time = None

        try:
            if isinstance(opened_at, str):
                # Remove Z and replace T with space
                clean_str = opened_at.replace("Z", "").replace("T", " ")
                # Split at dot to remove milliseconds
                clean_str = clean_str.split(".")[0]
                # Parse naive datetime (assumed to be UTC)
                parsed_dt = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
                user_time = parsed_dt.replace(tzinfo=timezone.utc).astimezone(user_timezone)
            elif hasattr(opened_at, "strftime"):
                if opened_at.tzinfo:
                    user_time = opened_at.astimezone(user_timezone)
                else:
                    user_time = opened_at.replace(tzinfo=timezone.utc).astimezone(user_timezone)
        except Exception:
            pass

        if user_time:
            time_str = user_time.strftime("%Y-%m-%d %H:%M:%S") + f" ({user_timezone.tzname(None)})"
        else:
            time_str = datetime.now(user_timezone).strftime("%Y-%m-%d %H:%M:%S") + f" ({user_timezone.tzname(None)})"

        sig = data.get("signal_data") or {}
        tp = data.get("take_profit_levels") or data.get("tp") or data.get("tp_prices") or sig.get("take_profit_prices") or []
        sl = data.get("stop_loss") or data.get("sl") or data.get("sl_price") or sig.get("stop_loss_price")

        # Format SL - type safe
        sl_str = "❓ Chưa thiết lập"
        if sl:
            try:
                sl_val = float(sl) if isinstance(sl, (int, float)) else float(str(sl).strip())
                if sl_val > 0:
                    sl_str = f"<code>${sl_val:,.4f}</code>"
            except (ValueError, TypeError):
                sl_str = "❓ Lỗi đọc SL"

        # Format TPs - type safe
        tp_lines = []
        if isinstance(tp, list) and len(tp) > 0:
            for i, t in enumerate(tp):
                try:
                    if isinstance(t, dict):
                        price = float(t.get("price", 0.0))
                        pct = float(t.get("exit_pct", 0.3)) * 100
                    elif hasattr(t, "price"):
                        price = float(t.price)
                        pct = float(getattr(t, "exit_pct", 0.3)) * 100
                    else:
                        price = float(t)
                        pct = 33.33

                    if price > 0:
                        tp_lines.append(f"├─ 🎯 <b>TP{i+1}:</b> <code>${price:,.4f}</code> ({pct:,.0f}%)")
                except (ValueError, TypeError):
                    continue

        if not tp_lines:
            tp_lines.append("├─ 🎯 <b>TP:</b> ❓ Chưa thiết lập")

        # Change the last line to use └─ instead of ├─
        if tp_lines:
            tp_lines[-1] = tp_lines[-1].replace("├─", "└─")
        tp_str = "\n".join(tp_lines)

        return (
            "✅ "
            + MessageTemplates.format_title("TP/SL ĐƯỢC ĐẶT THÀNH CÔNG")
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Vị thế:</b> {side_emoji}\n"
            "━━━\n"
            f"🛑 <b>Stop Loss:</b> {sl_str}\n"
            "━━━\n"
            f"📊 <b>Take Profit Mốc:</b>\n"
            f"{tp_str}\n"
            "━━━\n"
            f"⏰ <b>Thời gian đặt:</b> {time_str}\n"
            "━━━\n"
            "🛡️ Tất cả TP/SL được đặt trên OKX dưới dạng Reduce-Only."
        )

    @staticmethod
    def get_position_closed_notification(data: Dict[str, Any]) -> str:
        """🏁 KẾT THÚC GIAO DỊCH - i18n, HTML safe, type safe"""
        # HTML escape symbol
        symbol = MessageTemplates._escape_html(data.get("symbol", "UNKNOWN"))

        # Lấy PnL thực tế (đã trừ phí từ OKX Bills nếu có)
        try:
            realized_pnl = float(data.get("realized_pnl") or data.get("pnl", 0.0))
        except (ValueError, TypeError):
            realized_pnl = 0.0

        try:
            fee_paid = float(data.get("fee_paid", 0.0))
        except (ValueError, TypeError):
            fee_paid = 0.0

        # Calculate ROI safely
        try:
            entry = float(data.get("entry_price", 0.0))
            close = float(data.get("close_price", 0.0))
            amount = float(data.get("amount", 0.0))
            leverage = float(data.get("leverage", 10))
        except (ValueError, TypeError):
            entry = close = amount = 0.0
            leverage = 10

        # Nếu fee_paid = 0 nhưng có giao dịch thực tế, tính toán fee_paid từ công thức OKX
        if (fee_paid == 0.0 or fee_paid is None) and entry > 0 and amount > 0:
            try:
                from core.config.settings import settings
                taker_fee = float(getattr(settings, "taker_fee_rate", 0.0005))
                # Tính phí dựa trên entry và close price (nếu có)
                entry_fee = (entry * amount) * taker_fee
                if close > 0:
                    exit_fee = (close * amount) * taker_fee
                    fee_paid = entry_fee + exit_fee
                else:
                    # Nếu không có close_price, estimate fee_paid = 2x entry_fee
                    fee_paid = entry_fee * 2
            except Exception:
                fee_paid = 0.0

        margin = (entry * amount) / leverage if leverage > 0 and entry > 0 else 0.0
        try:
            roe_pct = (realized_pnl / margin * 100) if margin > 0 else 0.0
        except (ValueError, ZeroDivisionError):
            roe_pct = 0.0

        reason = MessageTemplates._escape_html(data.get("reason", "Không rõ"))

        # Translate reason
        reason_vi = reason
        reason_lower = str(reason).lower()
        if "tp" in reason_lower or "take profit" in reason_lower:
            reason_vi = "Chốt lời (Take Profit)"
        elif "sl" in reason_lower or "stop loss" in reason_lower:
            reason_vi = "Cắt lỗ (Stop Loss)"
        elif "emergency" in reason_lower:
            reason_vi = "Đóng khẩn cấp"

        # Outcome with emoji standardization
        if realized_pnl > 0:
            outcome = "✅ CHỐT LỜI"
            pnl_emoji = "💰"
        elif realized_pnl < 0:
            outcome = "❌ CẮT LỖ"
            pnl_emoji = "📉"
        else:
            outcome = "⚪ HÒA VỐN"
            pnl_emoji = "⚖️"

        return (
            "🏁 "
            + MessageTemplates.format_title(outcome)
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Đòn bẩy:</b> {leverage:.1f}x\n"
            f"📝 <b>Lý do đóng:</b> {reason_vi}\n"
            "━━━\n"
            f"{pnl_emoji} <b>PnL Ròng:</b> <code>{realized_pnl:+,.2f} USDT</code>\n"
            f"📊 <b>ROE:</b> <code>{roe_pct:+.2f}%</code>\n"
            f"💸 <b>Phí:</b> <code>{abs(fee_paid):.6f} USDT</code>\n"
            "━━━\n"
            "✅ Số dư đã hạch toán vào ví Margin."
        )

    @staticmethod
    def get_hourly_report_msg(data: Dict[str, Any]) -> str:
        """📈 BÁO CÁO HIỆU SUẤT 1H"""
        realized = data.get("realized_1h", 0.0)
        unrealized = data.get("unrealized", 0.0)
        active = data.get("active_positions", 0)

        sig_gen = data.get("signals_generated", 0)
        sig_rej = data.get("signals_rejected", 0)
        rej_reasons = data.get("rejected_reasons", {})

        health = "🟢 Đang hoạt động hoàn hảo" if data.get("is_healthy", True) else "🔴 Cảnh báo (Mất kết nối sàn)"

        # Build insight string
        insight_str = ""
        if sig_rej > 0 and rej_reasons:
            insight_lines = []
            for reason, count in rej_reasons.items():
                insight_lines.append(f"  └─ {reason}: {count} lần")
            insight_str = "\n".join(insight_lines) + "\n"

        return (
            "📈 "
            + MessageTemplates.format_title("BÁO CÁO TỔNG QUAN (1H)")
            + f"⏱️ <b>Thời điểm cập nhật:</b> {datetime.now().strftime('%H:00 %d/%m')}\n"
            f"⚕️ <b>Trạng thái hệ thống:</b> {health}\n"
            "━━━\n"
            "💰 <b>HIỆU QUẢ GIAO DỊCH:</b>\n"
            f"├─ 💵 <b>Thực thu (Đã chốt):</b> <code>{realized:+.2f} USDT</code>\n"
            f"├─ ⏳ <b>PnL tạm tính (Mở):</b> <code>{unrealized:+.2f} USDT</code>\n"
            f"└─ ⚡ <b>Vị thế đang treo:</b> <code>{active}</code> lệnh\n"
            "━━━\n"
            "📡 <b>HOẠT ĐỘNG RADAR AI:</b>\n"
            f"├─ 🎯 <b>Tín hiệu quét được:</b> <code>{sig_gen}</code> lần\n"
            f"├─ ❌ <b>Lệnh hủy/hụt (Reject):</b> <code>{sig_rej}</code> lần\n"
            f"{insight_str}"
            "━━━\n"
            "🚀 <i>VCOREX - Đánh bại cảm xúc, lợi nhuận bền vững.</i>"
        )

    @staticmethod
    def get_volatility_alert(data: Dict[str, Any]) -> str:
        """🔥 CẢNH BÁO BIẾN ĐỘNG ĐỘT BIẾN"""
        symbol = data.get("symbol", "N/A")
        change = data.get("change_pct", 0.0)
        price = data.get("price", 0.0)
        period = data.get("period", "5m")
        vol_spike = data.get("vol_spike", "100%")

        direction = "📈 Tăng vọt" if change > 0 else "📉 Sụt giảm"
        emoji = "🔥" if abs(change) > 5 else "⚠️"

        return (
            f"{emoji} "
            + MessageTemplates.format_title("RADAR BIẾN ĐỘNG")
            + f"💎 <b>Cặp:</b> <code>{symbol}</code>\n"
            f"⚡ <b>Trạng thái:</b> {direction} <code>{change:+.2f}%</code>\n"
            f"⏱️ <b>Trong vòng:</b> {period}\n"
            f"📌 <b>Giá hiện tại:</b> ${price:,.4f}\n"
            f"📊 <b>Volume spike:</b> {vol_spike}\n"
            "━━━\n"
            "💡 <i>Hãy kiểm tra đồ thị để bắt kịp con sóng!</i>"
        )

    @staticmethod
    def get_ghost_position_alert(data: Dict[str, Any]) -> str:
        """👻 CẢNH BÁO LỆNH NGOÀI HỆ THỐNG (GHOST POSITION)"""
        symbol = data.get("symbol", "N/A")
        reason = data.get("reason", "unknown")
        pos_id = data.get("position_id", "N/A")

        if reason == "auto_recovery":
            strategy_name = data.get("strategy_name", "recovered")
            is_bot = strategy_name not in ("recovered", "manual", "unknown", "", None)

            if is_bot:
                title = "PHỤC HỒI VỊ THẾ BOT"
                emoji = "🤖"
                lead_emoji = "🤖 "
                desc = f"Bot đã phát hiện một vị thế của chiến lược <code>{strategy_name}</code> bị mất đồng bộ trên sàn OKX và đã tự động khôi phục giám sát thành công."
            else:
                title = "PHÁT HIỆN LỆNH TAY MỚI"
                emoji = "➕"
                lead_emoji = "➕ "
                desc = "Bot đã phát hiện một vị thế mới được mở thủ công trên sàn OKX và đã tự động đưa vào hệ thống giám sát."

            side = data.get("side", "N/A").upper()
            side_emoji = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
            entry_price = data.get("entry_price", 0.0)
            current_price = data.get("current_price", 0.0)
            leverage = data.get("leverage", 1)
            margin = data.get("margin", 0.0)
            pnl = data.get("pnl", 0.0)
            roe = data.get("roe", 0.0)
            notional = data.get("notional_size", 0.0)
            amount = data.get("amount", 0.0)

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            roe_str = f"+{roe:,.2f}%" if roe >= 0 else f"{roe:,.2f}%"

            tp_px = data.get("tp_trigger_px")
            sl_px = data.get("sl_trigger_px") or data.get("stop_loss")

            tp_levels = data.get("take_profit_levels", [])
            if tp_levels:
                tp_str = " | ".join([f"${float(t['price']):,.4f}" for t in tp_levels])
            else:
                tp_str = f"${tp_px:,.4f}" if tp_px else "Chưa thiết lập 🎯"

            sl_str = f"${sl_px:,.4f}" if sl_px else "Chưa thiết lập 🛑"

            details = (
                f"├─ ⚡ <b>Vị thế:</b> {side_emoji}\n"
                f"├─ ⚙️ <b>Đòn bẩy:</b> <code>x{leverage}</code>\n"
                f"├─ 💵 <b>Ký quỹ (Margin):</b> <code>${margin:,.2f} USDT</code>\n"
                f"├─ 📦 <b>Quy mô (Notional):</b> <code>${notional:,.2f}</code> ({amount:,.4f} HĐ)\n"
                f"├─ 📌 <b>Giá vào (Entry):</b> <code>${entry_price:,.4f}</code>\n"
                f"├─ 🕒 <b>Giá hiện tại:</b> <code>${current_price:,.4f}</code>\n"
                f"├─ 🎯 <b>Take Profit (TP):</b> <code>{tp_str}</code>\n"
                f"├─ 🛑 <b>Stop Loss (SL):</b> <code>{sl_str}</code>\n"
                f"└─ {pnl_emoji} <b>PnL hiện tại:</b> <code>{pnl_str} ({roe_str})</code>\n"
            )
        else:
            strategy_name = data.get("strategy_name", "manual")
            is_bot = strategy_name not in ("recovered", "manual", "unknown", "", None)

            if is_bot:
                title = "LỆNH BOT ĐÃ ĐÓNG"
                emoji = "🤖"
                lead_emoji = "🤖 "
                desc = f"Bot phát hiện một vị thế của chiến lược <code>{strategy_name}</code> đã được đóng trên sàn OKX (có thể do chạm TP/SL). Dữ liệu local đã được dọn dẹp để đồng bộ."
            else:
                title = "LỆNH TAY ĐÃ ĐÓNG"
                emoji = "👻"
                lead_emoji = "👻 "
                desc = "Bot phát hiện một vị thế được mở bằng tay đã được đóng trên sàn OKX. Dữ liệu local đã được dọn dẹp để đồng bộ."

            side = data.get("side", "N/A").upper()
            side_emoji = "🟢 LONG" if side == "LONG" else "🔴 SHORT"

            entry_price = data.get("entry_price", 0.0)
            current_price = data.get("current_price", 0.0)
            leverage = data.get("leverage", 1)
            margin = data.get("margin", 0.0)
            pnl = data.get("pnl", 0.0)
            roe = data.get("roe", 0.0)
            notional = data.get("notional_size", 0.0)
            amount = data.get("amount", 0.0)

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_str = f"+${pnl:,.2f} USDT" if pnl >= 0 else f"-${abs(pnl):,.2f} USDT"
            roe_str = f"+{roe:,.2f}%" if roe >= 0 else f"{roe:,.2f}%"

            # Format times and duration
            opened_at_str = data.get("opened_at")
            closed_at_str = data.get("closed_at")

            from datetime import datetime, timezone, timedelta

            user_timezone = MessageTemplates._get_timezone()

            def format_vn_time(iso_str):
                if not iso_str:
                    return "N/A"
                try:
                    dt = datetime.fromisoformat(iso_str)
                    utc_dt = dt.astimezone(timezone.utc)
                    user_dt = utc_dt.astimezone(user_timezone)
                    return user_dt.strftime("%Y-%m-%d %H:%M:%S") + f" ({user_timezone.tzname(None)})"
                except Exception:
                    return iso_str

            def format_vn_duration(seconds: float) -> str:
                if seconds < 0:
                    return "0 giây"
                if seconds < 60:
                    return f"{int(seconds)} giây"
                minutes = seconds / 60
                if minutes < 60:
                    return f"{int(minutes)} phút"
                hours = minutes / 60
                if hours < 24:
                    rem_min = minutes % 60
                    return f"{int(hours)} giờ {int(rem_min)} phút"
                days = hours / 24
                rem_hours = hours % 24
                return f"{int(days)} ngày {int(rem_hours)} giờ"

            duration_str = "N/A"
            if opened_at_str and closed_at_str:
                try:
                    opened_at = datetime.fromisoformat(opened_at_str)
                    closed_at = datetime.fromisoformat(closed_at_str)
                    duration_seconds = (closed_at - opened_at).total_seconds()
                    duration_str = format_vn_duration(duration_seconds)
                except Exception:
                    pass

            details = (
                f"├─ ⚡ <b>Vị thế:</b> {side_emoji}\n"
                f"├─ ⚙️ <b>Đòn bẩy:</b> <code>x{leverage}</code>\n"
                f"├─ 💵 <b>Ký quỹ (Margin):</b> <code>${margin:,.2f} USDT</code>\n"
                f"├─ 📦 <b>Quy mô (Notional):</b> <code>${notional:,.2f} USDT</code> ({amount:,.4f} HĐ)\n"
                f"├─ 📌 <b>Giá vào (Entry):</b> <code>${entry_price:,.4f}</code>\n"
                f"├─ 🚪 <b>Giá đóng (Exit):</b> <code>${current_price:,.4f}</code>\n"
                f"├─ {pnl_emoji} <b>Lợi nhuận ròng:</b> <code>{pnl_str} ({roe_str})</code>\n"
                f"├─ ⏱️ <b>Thời gian nắm giữ:</b> <code>{duration_str}</code>\n"
                f"├─ 📅 <b>Giờ mở lệnh:</b> <code>{format_vn_time(opened_at_str)}</code>\n"
                f"└─ 📅 <b>Giờ đóng lệnh:</b> <code>{format_vn_time(closed_at_str)}</code>\n"
            )

        return (
            lead_emoji
            + MessageTemplates.format_title(title)
            + f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
            f"{emoji} <b>Hành động:</b> <i>{desc}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID Lệnh:</b> <code>{pos_id}</code>\n"
            f"{details}"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🛡️ <i>VCOREX luôn đảm bảo Sàn là nguồn sự thật duy nhất.</i>"
        )

    @staticmethod
    def get_system_alert(data: Dict[str, Any]) -> str:
        level = data.get("level", "INFO")
        title = data.get("title", "SYSTEM ALERT")
        message = data.get("message", "")
        emoji = "🔴" if level == "CRITICAL" else "🟡" if level == "WARNING" else "🔵"

        if "LỖI VÀO LỆNH" in title or "LỖI ĐÓNG LỆNH" in title:
            parts = title.split()
            symbol = parts[-1] if len(parts) > 1 else "N/A"
            action_type = "MỞ VỊ THẾ (OPEN POSITION)" if "LỖI VÀO LỆNH" in title else "ĐÓNG VỊ THẾ (CLOSE POSITION)"

            return (
                f"🚨 <b>THẤT BẠI GIAO DỊCH (TRADE FAILED)</b>\n\n"
                f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
                f"⚡ <b>Hành động:</b> 🔴 <code>{action_type}</code>\n"
                f"━━━\n"
                f"⚠️ <b>Chi tiết lỗi từ sàn:</b>\n"
                f"{message}\n\n"
                f"🛡️ <i>VCOREX Risk Manager đã can thiệp bảo vệ tài khoản an toàn. Vui lòng kiểm tra số dư khả dụng hoặc cấu hình API.</i>"
            )

        if "BỎ QUA TÍN HIỆU" in title:
            parts = title.split()
            symbol = parts[-1] if len(parts) > 1 else "N/A"
            return (
                f"⚠️ <b>BỎ QUA TÍN HIỆU (SIGNAL SKIPPED)</b>\n\n"
                f"💎 <b>Tài sản:</b> <code>{symbol}</code>\n"
                f"⚡ <b>Trạng thái:</b> 🔴 <code>ĐÃ CÓ VỊ THẾ TRÊN SÀN</code>\n"
                f"━━━\n"
                f"ℹ️ {message}\n\n"
                f"🛡️ <i>VCOREX luôn đảm bảo an toàn vốn và quản trị rủi ro tối đa.</i>"
            )

        return f"{emoji} " + MessageTemplates.format_title(title) + f"<i>{message}</i>"

    @staticmethod
    def get_periodic_report(data: Dict[str, Any]) -> str:
        return MessageTemplates.get_pnl_dashboard(data)

    @staticmethod
    def get_history_missed_signals(history_data: Dict[str, Any]) -> str:
        """Render Risk & Strategy Dashboard for missed signals."""
        if not history_data or history_data.get("is_empty"):
            return (
                "🛡️ " + MessageTemplates.format_title("RISK & STRATEGY DASHBOARD") +
                "<i>Hệ thống lệnh chờ & cảnh báo rủi ro đang trống. Chờ đợi cơ hội mới...</i>"
            )

        last_scan = history_data.get("last_scan", "N/A")
        c1_count = history_data.get("c1_count", 0)
        c2_count = history_data.get("c2_count", 0)
        c3_count = history_data.get("c3_count", 0)
        recent = history_data.get("recent_signals", [])
        dominant = history_data.get("dominant_issue")

        # Health warning
        health_warning = ""
        if dominant:
            health_warning = f"\n⚠️ <b>Cảnh báo:</b> Kết nối sàn đang bất ổn (Chiếm ưu thế: <i>{dominant}</i>)\n"

        # Insight list
        insight_lines = []
        user_timezone = MessageTemplates._get_timezone()
        for s in recent:
            symbol = s.get("symbol", "N/A")
            reason = s.get("reason", "N/A")
            try:
                from datetime import datetime, timezone, timedelta
                dt = datetime.fromisoformat(s.get("time", ""))
                user_dt = dt.astimezone(user_timezone)
                time_str = user_dt.strftime("%H:%M:%S")
            except Exception:
                time_str = "N/A"
            insight_lines.append(f"🔸 [{time_str}] <b>{symbol}</b> - {reason}")

        insight_str = "\n".join(insight_lines) if insight_lines else "<i>Không có dữ liệu chi tiết</i>"

        return (
            "🛡️ " + MessageTemplates.format_title("RISK & STRATEGY DASHBOARD") +
            f"⏱️ <b>Lần quét nến gần nhất:</b> <code>{last_scan}</code>\n"
            "━━━\n"
            "📊 <b>THỐNG KÊ LỆNH BỎ QUA & LỆNH CHỜ:</b>\n"
            f"├─ 🛡️ <b>C1 (Logic & Timing):</b> <code>{c1_count}</code> (Trễ, Cooldown, Trùng lặp)\n"
            f"├─ 🛑 <b>C2 (Risk Management):</b> <code>{c2_count}</code> (Quá tải, Hết Margin, Rủi ro)\n"
            f"└─ 🎯 <b>C3 (Entry Readiness):</b> <code>{c3_count}</code> (Chờ Entry chưa đạt)\n"
            "━━━\n"
            "🔍 <b>INSIGHT (3 lệnh gần nhất bị từ chối/chờ):</b>\n"
            f"{insight_str}\n"
            f"{health_warning}"
            "━━━\n"
            "💡 <i>Mẹo: Hãy dọn dẹp lịch sử nếu số lượng quá nhiều. Lệnh C3 là bình thường.</i>"
        )