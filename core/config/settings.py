"""
Application configuration using pydantic-settings.
All configuration is loaded from environment variables and .env file.
"""

import json
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from loguru import logger


class Settings(BaseSettings):
    """
    Main application settings class.
    All environment variables are validated and typed here.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OKX API Configuration
    okx_api_key: str = Field(..., description="ACTIVE: OKX API key for exchange connection")
    okx_api_secret: str = Field(..., description="ACTIVE: OKX API secret for exchange authentication")
    okx_passphrase: str = Field(..., description="ACTIVE: OKX API passphrase for exchange authentication")
    okx_demo_mode: bool = Field(default=True, description="ACTIVE: Use OKX demo/sandbox trading environment")
    okx_base_url: str = Field(default="https://openapi.okx.com", description="ACTIVE: OKX REST API endpoint URL")
    okx_ws_url: str = Field(
        default="wss://wspap.okx.com:8443/ws/v5", description="ACTIVE: OKX WebSocket endpoint URL"
    )
    okx_public_api_capacity: int = Field(default=100, description="OKX public API rate limit capacity")
    okx_public_api_refill_rate: int = Field(default=20, description="OKX public API rate limit refill rate (requests/second)")
    okx_private_api_capacity: int = Field(default=100, description="OKX private API rate limit capacity")
    okx_private_api_refill_rate: int = Field(default=60, description="OKX private API rate limit refill rate (requests/second)")
    exchange_cb_threshold: int = Field(default=15, description="Circuit breaker threshold for exchange errors")
    exchange_cb_cooldown: float = Field(default=60.0, description="Circuit breaker cooldown period in seconds")

    # Trading Configuration
    default_leverage: int = Field(default=10, ge=1, le=125, description="ACTIVE: Default trading leverage for positions")
    max_leverage: int = Field(default=10, ge=1, le=125, description="ACTIVE: Maximum allowed leverage (FIXED at 10x)")
    margin_mode: str = Field(default="isolated", description="ACTIVE: Margin mode (isolated or cross). Used by okx_exchange.py and telegram bot.")
    margin_per_order_usdt: float = Field(default=1000.0, description="ACTIVE: Margin per order in USDT. Used for position sizing.")
    max_daily_drawdown: float = Field(
        default=0.30, ge=0.01, le=1.0,
        description="ACTIVE: Max daily drawdown threshold (0.30 = 30%). Bot halts if equity drops below this from daily peak. Set via MAX_DAILY_DRAWDOWN in .env."
    )



    maker_fee_rate: float = Field(
        default=0.0002, description="ACTIVE: Institutional maker fee rate (0.02%)"
    )
    taker_fee_rate: float = Field(
        default=0.0005, description="ACTIVE: Institutional taker fee rate (0.05%)"
    )
    ticker_ttl_seconds: int = Field(
        default=10, description="ACTIVE: Time-to-live for ticker cache in seconds"
    )

    # ROE-based TP/SL Configuration
    fee_roe_buffer_pct: float = Field(
        default=0.20, description="ACTIVE: Fee ROE buffer percentage for TP/SL calculations (0.20% = taker 0.05% × 2 sides × 2 safety)"
    )
    sl_roe_pct: float = Field(default=50.0, description="ACTIVE: Stop loss ROE percentage")
    tp1_roe_pct: float = Field(default=50.0, description="ACTIVE: Take profit 1 ROE percentage")
    tp2_roe_pct: float = Field(default=100.0, description="ACTIVE: Take profit 2 ROE percentage")
    tp3_roe_pct: float = Field(default=150.0, description="ACTIVE: Take profit 3 ROE percentage")
    tp1_exit_pct: float = Field(default=0.5, description="ACTIVE: TP1 exit percentage (50% of position)")
    tp2_exit_pct: float = Field(default=0.3, description="ACTIVE: TP2 exit percentage (30% of position)")
    tp3_exit_pct: float = Field(default=0.2, description="ACTIVE: TP3 exit percentage (20% of position)")

    @field_validator("tp3_exit_pct")
    def validate_exit_pct_sum(cls, v, values):
        """Validate that sum of tp1+tp2+tp3 exit percentages equals 1.0"""
        data = values.data
        tp1 = data.get("tp1_exit_pct", 0)
        tp2 = data.get("tp2_exit_pct", 0)
        total = tp1 + tp2 + v
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"tp1+tp2+tp3 exit_pct must sum to 1.0, got {total}")
        return v

    min_body_percentage: float = Field(
        default=5.0, description="ACTIVE: Minimum candle body percentage for signal validation (Default: 5.0%)"
    )
    enable_default_strategy: bool = Field(
        default=True, description="ACTIVE: Enable the default EMA crossover strategy on bootstrap"
    )
    production_risk_mode: bool = Field(
        default=True, description="ACTIVE: Verify available margin and limits in production mode"
    )
    shutdown_liquidate_on_exit: bool = Field(
        default=False,
        description="Market-close all positions + cancel TP/SL on graceful shutdown. Set True only for full exit.",
    )

    # Strategy Configuration
    ema_fast_period: int = Field(default=9, description="ACTIVE: Fast EMA period for crossover strategy")
    ema_slow_period: int = Field(default=21, description="ACTIVE: Slow EMA period for crossover strategy")
    min_candles: int = Field(default=1, description="ACTIVE: Minimum candles required for indicator calculation (UNLIMITED for demo)")
    cooldown_minutes: int = Field(default=15, description="ACTIVE: Cooldown period between signals (minutes)")

    # Confirmation candles per timeframe (Entry mode: 0=Realtime, 1=Confirmation)
    confirmation_candles_5m: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 5m (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_15m: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 15m (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_1h: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 1H (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_4h: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 4H (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_1d: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 1D (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_1w: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 1W (0=Realtime, 1=Wait for candle close)",
    )
    confirmation_candles_1m: int = Field(
        default=0,
        description="ACTIVE: Entry mode for 1M (0=Realtime, 1=Wait for candle close)",
    )

    # ADX Filter configuration
    adx_min_threshold_all: float = Field(
        default=25.0,
        description="ACTIVE: Minimum ADX threshold for all timeframes (trend strength filter)",
    )
    adx_min_threshold_long_tf: float = Field(
        default=20.0,
        description="ACTIVE: Minimum ADX threshold for long timeframes (1D/1W/1M)",
    )

    # Minimum body percentage per timeframe
    min_body_percentage_5m: float = Field(
        default=1.0,
        description="ACTIVE: Minimum candle body percentage for 5m timeframe",
    )
    min_body_percentage_15m: float = Field(
        default=2.5,
        description="ACTIVE: Minimum candle body percentage for 15m timeframe",
    )
    min_body_percentage_1h: float = Field(
        default=5.0,
        description="ACTIVE: Minimum candle body percentage for 1H timeframe",
    )
    min_body_percentage_4h: float = Field(
        default=6.0,
        description="ACTIVE: Minimum candle body percentage for 4H timeframe",
    )
    min_body_percentage_1d: float = Field(
        default=7.5,
        description="ACTIVE: Minimum candle body percentage for 1D timeframe",
    )
    min_body_percentage_1w: float = Field(
        default=10.0,
        description="ACTIVE: Minimum candle body percentage for 1W timeframe",
    )
    min_body_percentage_1m: float = Field(
        default=12.0,
        description="ACTIVE: Minimum candle body percentage for 1M timeframe",
    )

    # Stale signal detection (seconds)
    stale_signal_5m_seconds: float = Field(default=60.0, description="ACTIVE: Max delay for 5m signals (seconds)")
    stale_signal_15m_seconds: float = Field(default=180.0, description="ACTIVE: Max delay for 15m signals (seconds)")
    stale_signal_1h_seconds: float = Field(default=720.0, description="ACTIVE: Max delay for 1H signals (seconds)")
    stale_signal_4h_seconds: float = Field(default=2880.0, description="ACTIVE: Max delay for 4H signals (seconds)")
    stale_signal_1d_seconds: float = Field(default=17280.0, description="ACTIVE: Max delay for 1D signals (seconds)")
    stale_signal_1w_seconds: float = Field(default=120960.0, description="ACTIVE: Max delay for 1W signals (seconds)")
    stale_signal_1m_seconds: float = Field(default=518400.0, description="ACTIVE: Max delay for 1M signals (seconds)")

    # Risk Management Configuration
    min_risk_reward_ratio: float = Field(default=1.5, description="ACTIVE: Minimum Risk/Reward ratio (enforced when production_risk_mode=true)")
    max_risk_allowed_pct: float = Field(default=0.20, description="ACTIVE: Maximum risk per trade as % of total equity (enforced when production_risk_mode=true)")
    max_symbol_concentration: float = Field(
        default=1.0,
        description="ACTIVE: Max concurrent positions per symbol (count). Default: 1",
    )
    max_open_positions: int = Field(
        default=9999,
        ge=1,
        description="ACTIVE: Max concurrent open positions (enforced when production_risk_mode=true)",
    )

    # Circuit Breaker Configuration
    cb_threshold: int = Field(default=9999, description="ACTIVE: Consecutive failures to trigger circuit breaker (DISABLED for demo)")
    cb_cooldown_seconds: float = Field(default=0.0, description="ACTIVE: Circuit breaker cooldown period (seconds) (NO COOLDOWN for demo)")
    
    # Component-specific circuit breaker settings
    position_cb_threshold: int = Field(default=5, description="Position engine circuit breaker failure threshold")
    position_cb_cooldown: float = Field(default=30.0, description="Position engine circuit breaker cooldown in seconds")
    eventbus_cb_threshold: int = Field(default=100, description="Event bus circuit breaker failure threshold")
    eventbus_cb_cooldown: float = Field(default=10.0, description="Event bus circuit breaker cooldown in seconds")

    # Drawdown reset configuration
    drawdown_reset_type: str = Field(default="daily", description="Drawdown reset frequency: daily/weekly/custom")
    drawdown_reset_weekday: int = Field(default=0, description="Weekday to reset drawdown for weekly mode (0=Monday)")
    timeframes: list[str] = Field(
        default=["5m", "15m", "1H", "4H", "1D", "1W", "1M"],
        description="ACTIVE: Timeframes to track for market data",
    )

    # Watchlist
    watchlist: List[str] = Field(
        default_factory=lambda: [
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
            "BNB-USDT-SWAP",
            "XRP-USDT-SWAP",
            "ADA-USDT-SWAP",
            "DOGE-USDT-SWAP",
            "AVAX-USDT-SWAP",
            "LINK-USDT-SWAP",
            "ETC-USDT-SWAP",
            "DOT-USDT-SWAP",
            "LTC-USDT-SWAP",
            "BCH-USDT-SWAP",
            "TRX-USDT-SWAP",
            "ATOM-USDT-SWAP",
            "NEAR-USDT-SWAP",
            "FIL-USDT-SWAP",
            "SUI-USDT-SWAP",
            "ARB-USDT-SWAP",
            "TON-USDT-SWAP",
        ],
        description="ACTIVE: Mảng thứ tự các symbols theo dõi, sẽ bị giới hạn bởi radar_limit.",
    )
    
    watchlist_file: Optional[str] = Field(
        default=None,
        description="ACTIVE: Path đến file watchlist.txt với format pro (nếu không dùng WATCHLIST từ .env)",
    )
    
    radar_limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="ACTIVE: Tầm quét Radar hiện tại (Mặc định 20)",
    )

    def get_active_watchlist(self) -> List[str]:
        """Trả về danh sách coin đang nằm trong radar quét hiện tại"""
        # Nếu có watchlist_file, ưu tiên đọc từ file
        if self.watchlist_file:
            return self._read_watchlist_from_file()[:self.radar_limit]
        return self.watchlist[:self.radar_limit]
    
    def _read_watchlist_from_file(self) -> List[str]:
        """Đọc watchlist từ file với format pro (hỗ trợ comments và multi-line)"""
        if not self.watchlist_file:
            return self.watchlist
        
        try:
            import os
            if not os.path.exists(self.watchlist_file):
                logger.warning(f"Watchlist file not found: {self.watchlist_file}, using default")
                return self.watchlist
            
            with open(self.watchlist_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Parse format pro: loại bỏ comments và empty lines
            symbols = []
            for line in content.split('\n'):
                line = line.strip()
                # Bỏ comments (#) và empty lines
                if not line or line.startswith('#'):
                    continue
                # Extract symbol từ format "SYMBOL-USDT-SWAP" hoặc "SYMBOL-USDT-SWAP",   # comment
                if '"' in line:
                    # Extract content giữa quotes
                    import re
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        symbols.append(match.group(1))
                elif line.startswith('[') or line.startswith(']'):
                    continue
                elif 'USDT-SWAP' in line:
                    # Fallback: extract symbol trực tiếp
                    symbol = line.split(',')[0].strip()
                    if symbol and 'USDT-SWAP' in symbol:
                        symbols.append(symbol)
            
            if symbols:
                logger.info(f"Loaded {len(symbols)} symbols from watchlist file: {self.watchlist_file}")
                return symbols
            else:
                logger.warning(f"No symbols found in watchlist file: {self.watchlist_file}, using default")
                return self.watchlist
                
        except Exception as e:
            logger.error(f"Error reading watchlist file {self.watchlist_file}: {e}, using default")
            return self.watchlist

    # Scan intervals (seconds)
    scan_interval_m5: int = Field(default=30, description="ACTIVE: M5 timeframe scan interval. Referenced by telegram formatters.")

    # Telegram Configuration
    telegram_bot_token: Optional[str] = Field(None, description="ACTIVE: Telegram bot token for authentication")
    telegram_chat_id: Optional[str] = Field(None, description="ACTIVE: Telegram chat ID for notifications")
    telegram_admin_ids: List[str] = Field(
        default_factory=list, description="ACTIVE: List of authorized Telegram user IDs for bot control"
    )
    telegram_enabled: bool = Field(default=False, description="ACTIVE: Enable Telegram notifications")
    telegram_notification_signals: bool = Field(default=True, description="ACTIVE: Enable signals in Telegram notifications")
    telegram_notification_trades: bool = Field(default=True, description="ACTIVE: Enable trade execution in Telegram notifications")
    telegram_notification_daily_report: bool = Field(default=True, description="ACTIVE: Enable daily report in Telegram notifications")
    telegram_notify_rejections: bool = Field(False, description="ACTIVE: Enable real-time signal rejection alerts in Telegram")
    telegram_timezone: str = Field(default="Asia/Ho_Chi_Minh", description="ACTIVE: Timezone for Telegram message timestamps (e.g., Asia/Ho_Chi_Minh, UTC, America/New_York)")

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def validate_admin_ids(cls, v):
        if isinstance(v, str):
            if not v:
                return []
            try:
                # Cố gắng parse nếu là chuỗi JSON list: ["123", "456"]
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(i) for i in parsed]
                return [str(parsed)]
            except json.JSONDecodeError:
                # Nếu không phải JSON, coi như là 1 ID đơn lẻ: 123456
                return [v]
        if isinstance(v, int):
            return [str(v)]
        return v

    # Performance test fields (for latency & slippage testing)
    max_latency_tolerance_ms: float = Field(
        default=50.0, description="ACTIVE: Max allowed end-to-end latency in milliseconds (institutional requirement)"
    )
    max_slippage_tolerance_bps: int = Field(
        default=20, description="ACTIVE: Max allowed slippage in basis points"
    )

    # Event Bus / Redis
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="ACTIVE: Redis URL for RedisStreamsEventBus (falls back to in-memory EventBus if unavailable)",
    )

    # Database Configuration
    database_url: str = Field(
        default="sqlite:///data/vcorex.db", description="Database connection URL"
    )
    database_wal_mode: bool = Field(
        default=True,
        description="ACTIVE: Enable SQLite WAL mode on startup (infrastructure/storage/database.py)",
    )

    # Logging Configuration
    log_level: str = Field(default="INFO", description="Logging level")
    logs_dir: str = Field(default="./logs", description="ACTIVE: Directory for log files. Used by core/config/logging.py.")
    enable_trade_logging: bool = Field(default=True, description="ACTIVE: Enable trade-specific logging. Referenced by telegram bot.")
    enable_websocket_logging: bool = Field(default=True, description="ACTIVE: Enable WebSocket logging. Referenced by telegram bot.")
    enable_strategy_logging: bool = Field(default=True, description="ACTIVE: Enable strategy logging. Referenced by telegram bot.")

    # System Configuration
    environment: str = Field(default="development", description="Deployment environment")
    max_reconnect_attempts: int = Field(
        default=10, ge=1, description="ACTIVE: Maximum WebSocket reconnection attempts. Used by okx_exchange.py."
    )

    # Deep Hardening & Resiliency Feature Flags
    ENABLE_EVENT_BASED_RECONCILIATION: bool = Field(
        default=True, description="ACTIVE: Event-based reconciliation. Used by services/reconciliation_service.py via getattr."
    )
    ENABLE_STRICT_ACCOUNT_SEEDING: bool = Field(
        default=False,
        description="ACTIVE: Block signals until exchange mirror receives account/position WS snapshot.",
    )
    ENABLE_SAFE_REDIS_ACK: bool = Field(
        default=True, description="ACTIVE: Safe Redis acknowledgment. Used by core/event_bus.py via getattr."
    )

    # Phantom verifier configuration (for PENDING_RECONCILE recovery)
    ENABLE_PHANTOM_VERIFIER: bool = Field(
        default=True, description="ACTIVE: Phantom verifier for reconciliation. Used by position_engine.py and order_handler.py via getattr."
    )
    PHANTOM_MAX_ATTEMPTS: int = Field(
        default=6, description="ACTIVE: Max phantom verification attempts. Used by order_handler.py via getattr."
    )
    PHANTOM_BASE_DELAY: float = Field(
        default=0.25, description="ACTIVE: Phantom verifier base delay (seconds). Used by order_handler.py via getattr."
    )
    PHANTOM_MAX_DELAY: float = Field(
        default=4.0, description="ACTIVE: Phantom verifier max delay (seconds). Used by order_handler.py via getattr."
    )
    PHANTOM_JITTER_PCT: float = Field(
        default=0.2, description="ACTIVE: Phantom verifier jitter percentage. Used by order_handler.py via getattr."
    )

    # Retry Configuration (ACTIVE)
    RETRY_BASE_DELAY_SECONDS: float = Field(
        default=0.5, description="ACTIVE: Retry base delay (seconds). Used by exchange_mirror.py for retry logic."
    )
    RETRY_MAX_DELAY_SECONDS: float = Field(
        default=32.0, description="ACTIVE: Retry max delay (seconds). Used by exchange_mirror.py for retry logic."
    )

    @model_validator(mode="after")
    def validate_environment_consistency(self) -> "Settings":
        env = (self.environment or "").lower()
        if env in ("production", "live") and self.okx_demo_mode:
            logger.warning(
                "[CONFIG] ENVIRONMENT indicates production/live but OKX_DEMO_MODE=true — orders go to demo API."
            )
        if env in ("production", "live") and not self.okx_demo_mode and not self.production_risk_mode:
            logger.warning(
                "[CONFIG] Mainnet trading with PRODUCTION_RISK_MODE=false — margin/leverage/R:R checks are bypassed."
            )
        return self


# Create global settings instance
settings = Settings()