# """
# EMA 9/21 crossover strategy implementation.
# Generates buy/sell signals when the 9-period EMA crosses above/below the 21-period EMA.
# Implements all validation filters and trade planning requirements.
# """

import copy
from typing import Optional

from loguru import logger

from core.config.settings import settings

from core.event_bus import IEventBus
from core.events.topics import EventTopic
from core.event_bus_components import Event

from .base_strategy import BaseStrategy, Signal, SignalStrength, SignalType, StrategyConfig
from .signal_safety_mixin import SignalSafetyMixin
from services.market_data.snapshot import MarketSnapshot

class EMACrossoverStrategy(SignalSafetyMixin, BaseStrategy):
    """EMA crossover strategy.
    Rules:
    - Golden cross (EMA fast crosses above EMA slow) = BUY signal
    - Death cross (EMA fast crosses below EMA slow) = SELL signal
    - CONFIRMATION_CANDLES=0: Realtime mode - entry on forming candle (candles[-1])
    - CONFIRMATION_CANDLES=1: Confirmation mode - entry on closed candle (candles[-2])
    - Candle body must be >= min_body_percentage (from .env)
    """

    def __init__(self, config: StrategyConfig, event_bus: Optional[IEventBus] = None):
        super().__init__(config)
        self.settings = settings
        self.event_bus = event_bus
        self.fast_period = self.settings.ema_fast_period
        self.slow_period = self.settings.ema_slow_period
        # Base min body percentage
        self.base_min_body_pct = self.settings.min_body_percentage / 100
        # Dynamic min body percentage per timeframe
        self.min_body_percentages = {
            "5m": self.settings.min_body_percentage_5m / 100,
            "15m": self.settings.min_body_percentage_15m / 100,
            "1H": self.settings.min_body_percentage_1h / 100,
            "4H": self.settings.min_body_percentage_4h / 100,
            "1D": self.settings.min_body_percentage_1d / 100,
            "1W": self.settings.min_body_percentage_1w / 100,
            "1M": self.settings.min_body_percentage_1m / 100,
        }
        # ADX thresholds
        self.adx_min_all = self.settings.adx_min_threshold_all
        self.adx_min_long_tf = self.settings.adx_min_threshold_long_tf
        self.confirmation_candles = {
            "5m": self.settings.confirmation_candles_5m,
            "15m": self.settings.confirmation_candles_15m,
            "1H": self.settings.confirmation_candles_1h,
            "4H": self.settings.confirmation_candles_4h,
            "1D": self.settings.confirmation_candles_1d,
            "1W": self.settings.confirmation_candles_1w,
            "1M": self.settings.confirmation_candles_1m,
        }
        logger.info(
            f"EMACrossoverStrategy initialized with EMA{self.fast_period}/{self.slow_period}, "
            f"ADX filter enabled (all_tf={self.adx_min_all}, long_tf={self.adx_min_long_tf})"
        )

    async def generate_signal(self, symbol: str, timeframe: str) -> Optional[Signal]:
        """Generate trading signal based on EMA crossover, with safe deduplication, cooldown and staleness checks."""
        logger.debug(f"[STRATEGY-FORENSIC] generate_signal called: symbol={symbol}, timeframe={timeframe}")
        
        # Cool‑down per (symbol, timeframe)
        if await self.is_in_cooldown(symbol, timeframe):
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_COOLDOWN: {symbol} {timeframe}")
            return None

        # Apply pre‑filters
        if not await self.filters(symbol, timeframe):
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_FILTERS: {symbol} {timeframe}")
            return None

        # Get indicators as MarketSnapshot for snapshot consistency
        snapshot = await self.calculate_indicators(symbol, timeframe)
        if not snapshot:
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_NO_SNAPSHOT: {symbol} {timeframe}")
            return None
        
        # Extract indicators from snapshot for backward compatibility
        # SHARED REFERENCE ELIMINATION: Shallow copy to prevent mutation (flat dict)
        indicators = snapshot.indicators.copy()
        
        crossover_detected = indicators.get("crossover_detected", 0)
        logger.debug(f"[STRATEGY-FORENSIC] crossover_detected={crossover_detected} for {symbol} {timeframe}")
        
        if not indicators.get("crossover_detected"):
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_NO_CROSSOVER: {symbol} {timeframe} "
                f"ema9={indicators.get('ema9')} ema21={indicators.get('ema21')}"
            )
            return None

        # Validate snapshot consistency
        if not snapshot.validate_consistency():
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_SNAPSHOT_INCONSISTENCY: {symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return None

        # Validate that indicators dictionary matches snapshot metadata
        indicators_ts = snapshot.indicators.get("signal_candle_ts", 0)
        if indicators_ts and indicators_ts != snapshot.reference_candle_timestamp:
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_TIMESTAMP_MISMATCH: {symbol} {timeframe} "
                f"indicators_ts={indicators_ts} snapshot_ts={snapshot.reference_candle_timestamp} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return None

        # Extract entry price from snapshot raw data
        entry_price = snapshot.raw_data.get("close") if snapshot.raw_data else None
        if not entry_price:
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_NO_ENTRY_PRICE: {symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return None

        # Determine signal side from snapshot
        signal_side = snapshot.get_signal_side()
        if not signal_side:
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_NO_SIGNAL_SIDE: {symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return None

        # Deduplication via mixin - use snapshot timestamp
        if await self.is_duplicate(symbol, timeframe, snapshot.reference_candle_timestamp):
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_DUPLICATE: {symbol} {timeframe}")
            return None

        # Determine signal direction from CURRENT EMA state, not stale crossover flags
        # This ensures signal_type reflects the actual EMA relationship at signal creation time
        ema9 = indicators.get("ema9", 0.0)
        ema21 = indicators.get("ema21", 0.0)
        signal_type = SignalType.BUY if ema9 > ema21 else SignalType.SELL
        # --- STALE CROSSOVER FLAG ELIMINATION: Log EMA-based signal assignment ---
        logger.debug(
            f"[EMA-BASED-SIGNAL] symbol={symbol}, timeframe={timeframe}, "
            f"signal_type={signal_type.value}, "
            f"EMA9={ema9:.4f}, EMA21={ema21:.4f}, "
            f"EMA9_vs_EMA21={'GREATER' if ema9 > ema21 else 'LESS' if ema9 < ema21 else 'EQUAL'}, "
            f"cached_crossover_bullish={indicators.get('crossover_bullish', 0.0)}, "
            f"cached_crossover_bearish={indicators.get('crossover_bearish', 0.0)}"
        )

        signal = Signal(
            strategy_name=self.config.name,
            symbol=symbol,
            timeframe=timeframe,
            signal_type=signal_type,
            signal_strength=SignalStrength.HIGH,
            entry_price=entry_price,
            indicators=indicators.copy(),  # SHARED REFERENCE ELIMINATION: Shallow copy to prevent mutation
            reason=f"EMA{self.fast_period}/{self.slow_period} crossover detected",
        )

        # Attach snapshot to signal for validation
        signal.snapshot = snapshot
        # --- SHARED REFERENCE ELIMINATION: Log object id separation ---
        logger.debug(
            f"[SHARED-REF-CHECK] id(indicators)={id(indicators)}, "
            f"id(snapshot.indicators)={id(snapshot.indicators)}, "
            f"id(signal.indicators)={id(signal.indicators)}, "
            f"SHARED_INDICATORS={'YES' if id(indicators) == id(snapshot.indicators) else 'NO'}, "
            f"SHARED_SIGNAL={'YES' if id(indicators) == id(signal.indicators) else 'NO'}"
        )

        # Validate signal
        logger.debug(f"[STRATEGY-FORENSIC] Calling validate_signal for {symbol} {timeframe}")
        if await self.validate_signal(signal):
            # Mark deduplication processed and start cooldown (both async)
            await self.mark_processed(symbol, timeframe, snapshot.reference_candle_timestamp)
            await self.record_signal(symbol, timeframe)
            logger.debug(f"[STRATEGY-FORENSIC] CREATED_SIGNAL: {symbol} {timeframe}")
            return await self.build_trade_plan(signal)

        logger.debug(f"[STRATEGY-FORENSIC] REJECTED_VALIDATE_SIGNAL: {symbol} {timeframe}")
        return None

    async def validate_signal(self, signal: Signal) -> bool:
        """Validate signal meets all strategy criteria using snapshot consistency."""
        timeframe = signal.timeframe
        logger.debug(f"[STRATEGY-FORENSIC] validate_signal called: {signal.symbol} {timeframe}")

        # Use snapshot for validation
        if not hasattr(signal, "snapshot") or not signal.snapshot:
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_NO_SNAPSHOT_ATTACHED: {signal.symbol} {timeframe}"
            )
            return False

        snapshot = signal.snapshot

        # Validate snapshot consistency
        if not snapshot.validate_consistency():
            logger.debug(
                f"[STRATEGY-FORENSIC] REJECTED_SNAPSHOT_INCONSISTENCY: {signal.symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return False

        # Extract body percentage from snapshot (already calculated as decimal 0.8333)
        body_percentage = snapshot.body_pct

        # Extract ADX from snapshot
        adx = snapshot.adx
        # Kiểm tra ngưỡng ADX theo timeframe
        long_timeframes = ("4H", "1D", "1W", "1M")
        min_adx = self.adx_min_long_tf if timeframe in long_timeframes else self.adx_min_all
        logger.debug(f"[STRATEGY-FORENSIC] ADX check: adx={adx}, min_adx={min_adx} for {signal.symbol} {timeframe}")
        if adx > 0 and adx < min_adx:
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_ADX: {signal.symbol} {timeframe} adx={adx} < min_adx={min_adx}")
            await self._publish_rejection(
                signal, "weak_trend_adx", {
                    "adx": adx,
                    "min_adx": min_adx,
                    "ema9": signal.indicators.get("ema9"),
                    "ema21": signal.indicators.get("ema21"),
                    "body_pct": body_percentage * 100.0,
                }
            )
            return False

        # --- BODY_PCT MUTATION FORENSIC AUDIT: Log at ema_crossover validation ---
        logger.debug(
            f"[MUTATION-VALIDATION] id(snapshot.indicators)={id(snapshot.indicators)}, "
            f"snapshot.body_pct={snapshot.body_pct}, type(snapshot.body_pct)={type(snapshot.body_pct)}, "
            f"body_percentage={body_percentage}"
        )

        # Get dynamic min body percentage for current timeframe
        current_min_body = self.min_body_percentages.get(timeframe, self.base_min_body_pct)
        
        # [FIX LỖI 4] Nếu ADX > 40 (xu hướng mạnh), giảm 50% yêu cầu thân nến tối thiểu
        # để không bỏ lỡ các con sóng lớn khi thị trường biến động cực mạnh (Flash Crash)
        if adx > 40:
            original_min_body = current_min_body
            current_min_body *= 0.5
            logger.info(
                f"[STRATEGY-ADX-ADAPTIVE] {signal.symbol} {timeframe}: "
                f"ADX={adx:.1f} > 40 (Strong Trend). "
                f"Reduced min_body from {original_min_body*100:.1f}% to {current_min_body*100:.1f}%"
            )

        # Save to indicators dictionary so we can display it!
        # body_pct is already stored as decimal (0.8333), convert to percentage (83.33%) for display
        signal.indicators["body_pct"] = body_percentage * 100.0
        signal.indicators["adx"] = adx
        # --- BODY_PCT MUTATION FORENSIC AUDIT: Log after signal.indicators assignment ---
        logger.debug(
            f"[MUTATION-SIGNAL] id(signal.indicators)={id(signal.indicators)}, "
            f"signal.indicators['body_pct']={signal.indicators.get('body_pct', 'NOT_SET')}"
        )

        # Log the raw stats to trace "Bot's thinking"
        logger.debug(
            f"[STRATEGY-FORENSIC] STATS: {signal.symbol} {timeframe} | "
            f"EMA9={signal.indicators.get('ema9', 0):.4f} | "
            f"EMA21={signal.indicators.get('ema21', 0):.4f} | "
            f"ADX={adx:.1f} | Signal={signal.signal_type.value} | "
            f"Body={signal.indicators.get('body_pct', 0):.2f}% (Min: {current_min_body * 100:.1f}%)"
        )

        if body_percentage < current_min_body:
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_BODY: {signal.symbol} {timeframe} body_pct={body_percentage*100:.2f}% < min={current_min_body*100:.1f}%")
            await self._publish_rejection(
                signal,
                "body_too_small",
                {
                    "body_pct": body_percentage * 100,
                    "min_pct": current_min_body * 100,
                    "ema9": signal.indicators.get("ema9"),
                    "ema21": signal.indicators.get("ema21"),
                    "adx": adx,
                },
            )
            return False

        # --- STALENESS CHECK ---
        logger.debug(f"[STRATEGY-FORENSIC] Staleness check: reference_candle_timestamp={snapshot.reference_candle_timestamp}")
        if await self.is_stale(snapshot.reference_candle_timestamp, timeframe, symbol=signal.symbol):
            logger.debug(f"[STRATEGY-FORENSIC] REJECTED_STALE: {signal.symbol} {timeframe}")
            await self._publish_rejection(
                signal,
                "stale_signal",
                {
                    "ema9": signal.indicators.get("ema9"),
                    "ema21": signal.indicators.get("ema21"),
                    "adx": adx,
                    "body_pct": body_percentage * 100.0,
                },
            )
            return False

        signal.validated = True
        logger.debug(f"[STRATEGY-FORENSIC] VALIDATED: {signal.symbol} {timeframe}")
        return True

    async def _publish_rejection(self, signal: Signal, reason: str, details: dict):
        """Helper to publish a signal rejection event."""
        logger.info(
            f"[SIGNAL_REJECTED] {signal.symbol} {signal.timeframe} reason={reason} "
            f"details={details}"
        )
        if self.event_bus:
            event = Event(
                event_type=EventTopic.SIGNAL_REJECTED,
                data={
                    "symbol": signal.symbol,
                    "timeframe": signal.timeframe,
                    "reason": reason,
                    "details": details,
                    "signal_type": signal.signal_type.value,
                },
            )
            await self.event_bus.publish(event)

    async def filters(self, symbol: str, timeframe: str) -> bool:
        """Apply pre-analysis filters."""
        # Symbol watchlist
        if self.config.symbols and symbol not in self.config.symbols:
            return False
        # Supported timeframe
        if timeframe not in self.config.timeframes:
            return False
        # Sufficient candles for analysis
        candles = self.get_candles(symbol, timeframe, limit=self.slow_period)
        if len(candles) < self.slow_period:
            logger.debug(
                f"Not enough candles for {symbol} {timeframe}: {len(candles)}/{self.slow_period}"
            )
            return False
        return True

    async def build_trade_plan(self, signal: Signal) -> Signal:
        """Build complete trade plan with position sizing and TP/SL adjusted for fees."""
        # position_size_usdt = NOTIONAL value = margin_per_order * leverage
        # actual margin used from account = margin_per_order_usdt
        position_size = self.settings.margin_per_order_usdt * self.settings.default_leverage
        signal.position_size_usdt = position_size
        leverage = self.settings.default_leverage
        fee_buffer = self.settings.fee_roe_buffer_pct
        sl_pct = ((self.settings.sl_roe_pct + fee_buffer) / 100.0) / leverage
        tp1_pct = ((self.settings.tp1_roe_pct + fee_buffer) / 100.0) / leverage
        tp2_pct = ((self.settings.tp2_roe_pct + fee_buffer) / 100.0) / leverage
        tp3_pct = ((self.settings.tp3_roe_pct + fee_buffer) / 100.0) / leverage
        if signal.signal_type == SignalType.BUY:
            signal.stop_loss_price = signal.entry_price * (1 - sl_pct)
            tp1_price = signal.entry_price * (1 + tp1_pct)
            tp2_price = signal.entry_price * (1 + tp2_pct)
            tp3_price = signal.entry_price * (1 + tp3_pct)
        else:
            signal.stop_loss_price = signal.entry_price * (1 + sl_pct)
            tp1_price = signal.entry_price * (1 - tp1_pct)
            tp2_price = signal.entry_price * (1 - tp2_pct)
            tp3_price = signal.entry_price * (1 - tp3_pct)
        signal.take_profit_prices = [
            {"price": tp1_price, "exit_pct": self.settings.tp1_exit_pct},
            {"price": tp2_price, "exit_pct": self.settings.tp2_exit_pct},
            {"price": tp3_price, "exit_pct": self.settings.tp3_exit_pct},
        ]
        logger.info(
            f"Trade plan built for {signal.symbol}: size=${position_size:.2f}, entry={signal.entry_price:.4f}, "
            f"SL={signal.stop_loss_price:.4f}, TP1={tp1_price:.4f}"
        )
        return signal