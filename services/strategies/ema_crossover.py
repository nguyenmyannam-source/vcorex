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
        """
        Generate trading signal based on EMA crossover EDGE TRIGGER logic.
        
        [EDGE TRIGGER REFACTOR] - Atomic Snapshot at Bar Close:
        - All calculations (EMA Crossover, ADX Filter, Body Percentage) MUST use the SAME snapshot
        - Snapshot is locked to the CLOSED candle at index [-2] (never forming candle at [-1])
        - Edge Trigger: Check for TRUE crossover event between [-2] and [-3], not state check
        - Long Trigger: Fast[-2] > Slow[-2] AND Fast[-3] <= Slow[-3]
        - Short Trigger: Fast[-2] < Slow[-2] AND Fast[-3] >= Slow[-3]
        - Filters (ADX, Body) are only evaluated AFTER crossover is confirmed at [-2]
        """
        logger.debug(f"[STRATEGY-EDGE-TRIGGER] generate_signal called: symbol={symbol}, timeframe={timeframe}")
        
        # Cool‑down per (symbol, timeframe)
        if await self.is_in_cooldown(symbol, timeframe):
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_COOLDOWN: {symbol} {timeframe}")
            return None

        # Apply pre‑filters
        if not await self.filters(symbol, timeframe):
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_FILTERS: {symbol} {timeframe}")
            return None

        # Get indicators as MarketSnapshot for snapshot consistency
        snapshot = await self.calculate_indicators(symbol, timeframe)
        if not snapshot:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_NO_SNAPSHOT: {symbol} {timeframe}")
            return None
        
        # [EDGE TRIGGER] Validate snapshot is CLOSED candle only (index -2)
        if snapshot.reference_candle_index != -2 or snapshot.candle_type != "closed":
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_NOT_CLOSED_CANDLE: {symbol} {timeframe} "
                f"ref_index={snapshot.reference_candle_index} candle_type={snapshot.candle_type}"
            )
            return None

        # Validate snapshot consistency
        if not snapshot.validate_consistency():
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_SNAPSHOT_INCONSISTENCY: {symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return None

        # [EDGE TRIGGER] Get EMA series from buffer for edge detection
        # We need EMA values at index [-2] (closed candle) and [-3] (previous candle)
        from core.container import container
        mde = container.get("market_data_engine")
        if not mde:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_NO_MDE: {symbol} {timeframe}")
            return None
        
        buffer_key = f"{symbol}_{timeframe}"
        buffer = mde.buffers.get(buffer_key)
        if not buffer or len(buffer.candles) < 4:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_INSUFFICIENT_CANDLES: {symbol} {timeframe}")
            return None

        # [EDGE TRIGGER] Calculate EMA series for edge detection
        # Get close prices for EMA calculation
        close_prices = buffer.get_close_prices(limit=max(self.slow_period + 10, 50))
        if len(close_prices) < self.slow_period + 3:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_INSUFFICIENT_CLOSES: {symbol} {timeframe}")
            return None

        # Calculate EMA series
        from services.market_data.indicators import EMACalculator
        ema_fast_series = EMACalculator.calculate_series(close_prices, self.fast_period)
        ema_slow_series = EMACalculator.calculate_series(close_prices, self.slow_period)
        
        if len(ema_fast_series) < 3 or len(ema_slow_series) < 3:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_INSUFFICIENT_EMA: {symbol} {timeframe}")
            return None

        # [EDGE TRIGGER] Lock indices to closed candle [-2] and previous [-3]
        idx_closed = -2  # Last closed candle
        idx_prev = -3   # Previous candle for edge detection
        
        ema_fast_closed = ema_fast_series[idx_closed]
        ema_slow_closed = ema_slow_series[idx_closed]
        ema_fast_prev = ema_fast_series[idx_prev]
        ema_slow_prev = ema_slow_series[idx_prev]
        
        # [EDGE TRIGGER] Check for TRUE crossover event (not state check)
        is_long_cross = (ema_fast_closed > ema_slow_closed) and (ema_fast_prev <= ema_slow_prev)
        is_short_cross = (ema_fast_closed < ema_slow_closed) and (ema_fast_prev >= ema_slow_prev)
        
        logger.debug(
            f"[STRATEGY-EDGE-TRIGGER] Edge check: {symbol} {timeframe} | "
            f"Fast[-2]={ema_fast_closed:.4f} Slow[-2]={ema_slow_closed:.4f} | "
            f"Fast[-3]={ema_fast_prev:.4f} Slow[-3]={ema_slow_prev:.4f} | "
            f"LongCross={is_long_cross} ShortCross={is_short_cross}"
        )
        
        if not (is_long_cross or is_short_cross):
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_NO_EDGE_CROSSOVER: {symbol} {timeframe} "
                f"(No crossover event at closed candle)"
            )
            return None

        # [EDGE TRIGGER] Determine signal direction from edge event
        signal_type = SignalType.BUY if is_long_cross else SignalType.SELL
        
        # [SYNCHRONOUS FILTERS] Calculate filters at CLOSED candle index [-2] only
        # Get OHLCV at index [-2] for body percentage calculation
        candles = buffer.get_candles(limit=5)
        if len(candles) < 3:
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_INSUFFICIENT_OHLCV: {symbol} {timeframe}")
            return None
        
        closed_candle = candles[idx_closed]
        high = closed_candle.high
        low = closed_candle.low
        open_p = closed_candle.open
        close_p = closed_candle.close
        
        # Calculate body percentage at index [-2]
        body_pct = abs(close_p - open_p) / (high - low) if (high - low) > 0 else 0.0
        
        # Get ADX at index [-2] from snapshot (already calculated at closed candle)
        adx = snapshot.adx
        
        # [SYNCHRONOUS FILTERS] Apply ADX filter at index [-2]
        long_timeframes = ("4H", "1D", "1W", "1M")
        min_adx = self.adx_min_long_tf if timeframe in long_timeframes else self.adx_min_all
        if adx > 0 and adx < min_adx:
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_ADX: {symbol} {timeframe} "
                f"adx={adx:.1f} < min_adx={min_adx}"
            )
            return None
        
        # [SYNCHRONOUS FILTERS] Apply Body Percentage filter at index [-2]
        current_min_body = self.min_body_percentages.get(timeframe, self.base_min_body_pct)
        
        # [FIX LỖI 4] Nếu ADX > 40 (xu hướng mạnh), giảm 50% yêu cầu thân nến tối thiểu
        if adx > 40:
            original_min_body = current_min_body
            current_min_body *= 0.5
            logger.info(
                f"[STRATEGY-ADX-ADAPTIVE] {symbol} {timeframe}: "
                f"ADX={adx:.1f} > 40 (Strong Trend). "
                f"Reduced min_body from {original_min_body*100:.1f}% to {current_min_body*100:.1f}%"
            )
        
        if body_pct < current_min_body:
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_BODY: {symbol} {timeframe} "
                f"body_pct={body_pct*100:.2f}% < min={current_min_body*100:.1f}%"
            )
            return None

        # [EDGE TRIGGER] All filters passed at closed candle - create signal
        logger.info(
            f"[STRATEGY-EDGE-TRIGGER] EDGE_CROSSOVER_CONFIRMED: {symbol} {timeframe} | "
            f"Direction={signal_type.value} | "
            f"Fast[-2]={ema_fast_closed:.4f} Slow[-2]={ema_slow_closed:.4f} | "
            f"ADX={adx:.1f} Body={body_pct*100:.1f}%"
        )

        # Extract entry price from closed candle
        entry_price = close_p
        
        # Deduplication via mixin - use snapshot timestamp
        if await self.is_duplicate(symbol, timeframe, snapshot.reference_candle_timestamp):
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_DUPLICATE: {symbol} {timeframe}")
            return None

        # Build indicators dict for backward compatibility
        indicators = snapshot.indicators.copy()
        indicators["ema9"] = ema_fast_closed
        indicators["ema21"] = ema_slow_closed
        indicators["adx"] = adx
        indicators["body_pct"] = body_pct * 100.0
        indicators["edge_trigger"] = True
        indicators["crossover_type"] = "long_edge" if is_long_cross else "short_edge"

        signal = Signal(
            strategy_name=self.config.name,
            symbol=symbol,
            timeframe=timeframe,
            signal_type=signal_type,
            signal_strength=SignalStrength.HIGH,
            entry_price=entry_price,
            indicators=indicators,
            reason=f"EMA{self.fast_period}/{self.slow_period} EDGE crossover at closed candle (index -2)",
        )

        # Attach snapshot to signal for validation
        signal.snapshot = snapshot

        # Validate signal (additional safety checks)
        logger.debug(f"[STRATEGY-EDGE-TRIGGER] Calling validate_signal for {symbol} {timeframe}")
        if await self.validate_signal(signal):
            # Mark deduplication processed and start cooldown (both async)
            await self.mark_processed(symbol, timeframe, snapshot.reference_candle_timestamp)
            await self.record_signal(symbol, timeframe)
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] CREATED_SIGNAL: {symbol} {timeframe}")
            return await self.build_trade_plan(signal)

        logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_VALIDATE_SIGNAL: {symbol} {timeframe}")
        return None

    async def validate_signal(self, signal: Signal) -> bool:
        """
        Validate signal meets all strategy criteria using snapshot consistency.
        
        [EDGE TRIGGER REFACTOR] - Simplified validation:
        - All filters (ADX, Body Percentage) are already checked in generate_signal() at index [-2]
        - This method only performs staleness check and snapshot consistency validation
        - Edge trigger logic ensures signal is only generated when TRUE crossover occurs at closed candle
        """
        timeframe = signal.timeframe
        logger.debug(f"[STRATEGY-EDGE-TRIGGER] validate_signal called: {signal.symbol} {timeframe}")

        # Use snapshot for validation
        if not hasattr(signal, "snapshot") or not signal.snapshot:
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_NO_SNAPSHOT_ATTACHED: {signal.symbol} {timeframe}"
            )
            return False

        snapshot = signal.snapshot

        # Validate snapshot consistency
        if not snapshot.validate_consistency():
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_SNAPSHOT_INCONSISTENCY: {signal.symbol} {timeframe} "
                f"snapshot_id={snapshot.snapshot_id}"
            )
            return False

        # [EDGE TRIGGER] Verify signal was generated from closed candle (index -2)
        if snapshot.reference_candle_index != -2 or snapshot.candle_type != "closed":
            logger.debug(
                f"[STRATEGY-EDGE-TRIGGER] REJECTED_NOT_CLOSED_CANDLE: {signal.symbol} {timeframe} "
                f"ref_index={snapshot.reference_candle_index} candle_type={snapshot.candle_type}"
            )
            return False

        # --- STALENESS CHECK ---
        logger.debug(f"[STRATEGY-EDGE-TRIGGER] Staleness check: reference_candle_timestamp={snapshot.reference_candle_timestamp}")
        if await self.is_stale(snapshot.reference_candle_timestamp, timeframe, symbol=signal.symbol):
            logger.debug(f"[STRATEGY-EDGE-TRIGGER] REJECTED_STALE: {signal.symbol} {timeframe}")
            await self._publish_rejection(
                signal,
                "stale_signal",
                {
                    "ema9": signal.indicators.get("ema9"),
                    "ema21": signal.indicators.get("ema21"),
                    "adx": signal.indicators.get("adx"),
                    "body_pct": signal.indicators.get("body_pct"),
                    "edge_trigger": signal.indicators.get("edge_trigger"),
                },
            )
            return False

        signal.validated = True
        logger.debug(f"[STRATEGY-EDGE-TRIGGER] VALIDATED: {signal.symbol} {timeframe}")
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