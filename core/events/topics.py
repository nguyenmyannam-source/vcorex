from enum import Enum


class EventTopic(str, Enum):
    """Định nghĩa tập trung cho tất cả các sự kiện (Event Topics) trong hệ thống."""

    # Market Data Events
    MARKET_NEW_CANDLE = "market.new_candle"
    MARKET_INDICATORS_UPDATED = "market.indicators_updated"
    MARKET_RESET_BUFFERS = "market.reset_buffers"
    MARKET_WS_CANDLE = "market.ws_candle"
    MARKET_WS_TICKER = "market.ws_ticker"
    MARKET_VOLATILITY_ALERT = "market.volatility_alert"
    
    # Raw Exchange WS Events (Exchange-Authoritative Architecture)
    WS_RAW_POSITION = "ws_raw.position"
    WS_RAW_ACCOUNT = "ws_raw.account"
    WS_RAW_ORDER = "ws_raw.order"
    WS_RAW_ALGO_ORDER = "ws_raw.algo_order"
    WS_RECONNECTED = "ws.reconnected"

    # Strategy Events
    STRATEGY_SIGNAL_GENERATED = "strategy.signal_generated"
    SIGNAL_REJECTED = "strategy.signal.rejected"

    # Risk Events
    RISK_SIGNAL_APPROVED = "risk.signal_approved"
    RISK_SIGNAL_REJECTED = "risk.signal_rejected"

    # Position Events
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"
    POSITION_PARTIAL_CLOSED = "position.partial_closed"
    POSITION_GHOST_DETECTED = "position.ghost_detected"
    POSITION_TRAILING_STOP_MOVED = "position.trailing_stop_moved"

    # System/Alert Events
    SYSTEM_API_ERROR = "system.api_error"
    SYSTEM_ALERT = "system.alert"

    # Telegram Dashboard Events
    TELEGRAM_REQUEST_HEALTH_DATA = "telegram.request_health_data"
    TELEGRAM_RESPONSE_HEALTH_DATA = "telegram.response_health_data"
    TELEGRAM_REQUEST_TRADING_DATA = "telegram.request_trading_data"
    TELEGRAM_RESPONSE_TRADING_DATA = "telegram.response_trading_data"
    TELEGRAM_REQUEST_ANALYTICS_DATA = "telegram.request_analytics_data"
    TELEGRAM_RESPONSE_ANALYTICS_DATA = "telegram.response_analytics_data"
    TELEGRAM_REQUEST_EXCHANGE_STATUS = "telegram.request_exchange_status"
    TELEGRAM_RESPONSE_EXCHANGE_STATUS = "telegram.response_exchange_status"
    TELEGRAM_REQUEST_SYSTEM_DATA = "telegram.request_system_data"
    TELEGRAM_RESPONSE_SYSTEM_DATA = "telegram.response_system_data"
    TELEGRAM_REQUEST_HISTORY_DATA = "telegram.request_history_data"
    TELEGRAM_RESPONSE_HISTORY_DATA = "telegram.response_history_data"
    TELEGRAM_REQUEST_SETTINGS_DATA = "telegram.request_settings_data"
    TELEGRAM_RESPONSE_SETTINGS_DATA = "telegram.response_settings_data"
    TELEGRAM_REQUEST_NEWS_DATA = "telegram.request_news_data"
    TELEGRAM_RESPONSE_NEWS_DATA = "telegram.response_news_data"
    TELEGRAM_SEND_MESSAGE = "telegram.send_message"
    TELEGRAM_HEARTBEAT = "telegram.heartbeat"
    NOTIFICATION_PERIODIC_REPORT = "notification.periodic_report"

    # Chart Events
    CHART_GENERATED = "chart.generated"

    # Exchange mirror events
    MIRROR_RESYNC_FAILED = "mirror.resync_failed"
    MIRROR_RESYNC_SUCCESS = "mirror.resync_success"

    # Control Events
    CONTROL_START_BOT = "control.start_bot"
    CONTROL_PAUSE_BOT = "control.pause_bot"
    CONTROL_EMERGENCY_STOP = "control.emergency_stop"
    CONTROL_EMERGENCY_STOP_COMPLETE = "control.emergency_stop_complete"
    CONTROL_RESET_SIGNALS = "control.reset_signals"
    CONTROL_RESET_SIGNALS_COMPLETE = "control.reset_signals_complete"
    CONTROL_CLEAN_BOT = "control.clean_bot"
    CONTROL_CLEAN_BOT_COMPLETE = "control.clean_bot_complete"
    CONTROL_HALT_TRADING = "control.halt_trading"
    CONTROL_RADAR_LIMIT_CHANGED = "control.radar_limit_changed"

    # Telegram Position Close Events
    POSITION_CLOSE_REQUEST = "position.close_request"
    POSITION_CLOSE_SUCCESS = "position.close_success"
    POSITION_CLOSE_FAILURE = "position.close_failure"