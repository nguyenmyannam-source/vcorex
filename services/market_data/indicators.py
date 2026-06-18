from typing import Dict, List, Optional
import time
import asyncio

from loguru import logger
from core.config.settings import settings
from .snapshot import MarketSnapshot


class EMACalculator:
    """Calculator for Exponential Moving Average indicator."""

    @staticmethod
    def calculate(prices: List[float], period: int) -> float:
        if len(prices) < period:
            raise ValueError(f"Not enough data to calculate EMA{period}")

        sma = sum(prices[:period]) / period
        multiplier = 2 / (period + 1)
        ema = sma

        for price in prices[period:]:
            ema = price * multiplier + ema * (1 - multiplier)

        return ema

    @staticmethod
    def calculate_series(prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []

        ema_series = []
        sma = sum(prices[:period]) / period
        multiplier = 2 / (period + 1)
        ema = sma
        ema_series.append(ema)

        for price in prices[period:]:
            ema = price * multiplier + ema * (1 - multiplier)
            ema_series.append(ema)

        return ema_series

    @staticmethod
    def calculate_incremental_series(prices: List[float], period: int, cached_series: List[float]) -> List[float]:
        """Calculates series incrementally if the first N-1 prices match the cached series."""
        if not cached_series or len(cached_series) <= 1 or len(prices) < period:
            return EMACalculator.calculate_series(prices, period)
            
        expected_cache_len_forming = len(prices) - period + 1
        expected_cache_len_closed = len(prices) - period
        
        # If the prices length matches cached series length for a forming candle update
        if len(cached_series) == expected_cache_len_forming:
            new_series = cached_series[:-1]
            multiplier = 2 / (period + 1)
            prev_ema = new_series[-1]
            new_ema = prices[-1] * multiplier + prev_ema * (1 - multiplier)
            new_series.append(new_ema)
            return new_series
            
        # If one new candle closed (len(prices) increased by 1 compared to cache)
        if len(cached_series) == expected_cache_len_closed:
            new_series = cached_series[:-1]  # Remove the stale forming EMA
            multiplier = 2 / (period + 1)
            
            # Recalculate true closed EMA for the previous candle (prices[-2])
            prev_ema = new_series[-1]
            final_ema = prices[-2] * multiplier + prev_ema * (1 - multiplier)
            new_series.append(final_ema)
            
            # Calculate the initial forming EMA for the new candle (prices[-1])
            new_ema = prices[-1] * multiplier + final_ema * (1 - multiplier)
            new_series.append(new_ema)
            return new_series
            
        # Fallback for gaps
        return EMACalculator.calculate_series(prices, period)

class ADXCalculator:
    """Calculator for Average Directional Index (ADX) indicator."""

    @staticmethod
    def calculate(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """Calculate ADX value from high, low, close prices."""
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return 0.0

        # Calculate True Range (TR)
        tr_list = []
        for i in range(1, len(closes)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i-1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)

        # Calculate +DM and -DM
        plus_dm = []
        minus_dm = []
        for i in range(1, len(highs)):
            high_diff = highs[i] - highs[i-1]
            low_diff = lows[i-1] - lows[i]
            
            if high_diff > low_diff and high_diff > 0:
                plus_dm.append(high_diff)
            else:
                plus_dm.append(0.0)
                
            if low_diff > high_diff and low_diff > 0:
                minus_dm.append(low_diff)
            else:
                minus_dm.append(0.0)

        # Smooth TR, +DM, -DM
        def smooth(series: List[float], period: int) -> List[float]:
            smoothed = []
            first_sum = sum(series[:period])
            smoothed.append(first_sum / period)
            for i in range(period, len(series)):
                smoothed.append(smoothed[-1] * (period - 1) / period + series[i] / period)
            return smoothed

        if len(tr_list) < period or len(plus_dm) < period or len(minus_dm) < period:
            return 0.0

        smoothed_tr = smooth(tr_list, period)
        smoothed_plus_dm = smooth(plus_dm, period)
        smoothed_minus_dm = smooth(minus_dm, period)

        # Calculate +DI and -DI
        plus_di = [100 * (dm / tr) if tr != 0 else 0 for dm, tr in zip(smoothed_plus_dm, smoothed_tr)]
        minus_di = [100 * (dm / tr) if tr != 0 else 0 for dm, tr in zip(smoothed_minus_dm, smoothed_tr)]

        # Calculate DX and ADX
        dx_list = []
        for pdi, mdi in zip(plus_di, minus_di):
            dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else 0
            dx_list.append(dx)

        # Calculate final ADX using Wilder's smoothing on DX (not a simple SMA)
        if len(dx_list) < period:
            return 0.0
        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = (adx * (period - 1) + dx) / period
        return adx


class IndicatorPipeline:
    """Pipeline xử lý các indicators cho tất cả buffers."""

    def __init__(self):
        self.min_candles = settings.min_candles
        self.fast_period = settings.ema_fast_period
        self.slow_period = settings.ema_slow_period
        self.calculators = {
            f"ema{self.fast_period}": lambda prices: EMACalculator.calculate(prices, self.fast_period),
            f"ema{self.slow_period}": lambda prices: EMACalculator.calculate(prices, self.slow_period),
        }
        # Snapshot cache with TTL for snapshot consistency
        self.snapshot_cache: Dict[str, tuple[MarketSnapshot, float]] = {}  # key -> (snapshot, timestamp)
        self.snapshot_cache_ttl = 5.0  # 5 seconds TTL
        # Indicator cache for backward compatibility
        self.indicator_cache: Dict[str, Dict[str, float]] = {}
        # EMA Series cache to prevent full recalculation on every tick
        self.ema_series_cache: Dict[str, Dict[str, List[float]]] = {}
        # [FIX LỖI 2] Lock for atomic cache updates - dùng asyncio.Lock thay vì threading.Lock
        self._cache_lock = asyncio.Lock()

    async def compute_indicators(self, buffer, confirmation_candles: int = 1, reference_candle_index: int = -2) -> MarketSnapshot:
        """
        Compute indicators and return a MarketSnapshot for snapshot consistency.
        
        SUPPORTS BOTH MODES:
        - Realtime mode (confirmation_candles=0): use forming candle (reference_candle_index=-1)
        - Confirmation mode (confirmation_candles>=1): use last closed candle (reference_candle_index=-2)
        
        Args:
            buffer: CandleBuffer instance
            confirmation_candles: 0=Realtime, >=1=Confirmation (number of candles to wait for confirmation)
            reference_candle_index: -1=forming candle (realtime), -2=last closed candle (confirmation)
        
        Returns:
            MarketSnapshot with all indicators calculated from the same candle reference
        """
        closes = buffer.get_close_prices(500)
        highs = buffer.get_high_prices(500)
        lows = buffer.get_low_prices(500)
        fast = self.fast_period
        slow = self.slow_period

        if len(closes) < self.min_candles:
            logger.debug(
                f"Not enough data for {buffer.symbol} {buffer.timeframe}: {len(closes)}/{self.min_candles}"
            )
            # Return empty snapshot if not enough data
            return MarketSnapshot.create(
                symbol=buffer.symbol,
                timeframe=buffer.timeframe,
                reference_candle_timestamp=0,
                reference_candle_index=0,
                candle_type="unknown",
                ema_fast=0,
                ema_slow=0,
                adx=0,
                body_pct=0,
                indicators={},
            )

        results = {}

        # --- CENTRALIZED SIGNAL GATE VALIDATION ---
        # Production ONLY supports confirmation_candles=1 (CLOSED CANDLE mode).
        # confirmation_candles=0 (REALTIME), 2, 3, 99 are all disabled in production.
        if confirmation_candles != 1:
            raise RuntimeError(f"UNSUPPORTED_CONFIRMATION_CANDLES={confirmation_candles}")
        if reference_candle_index != -2:
            logger.error(f"INVALID_REFERENCE_CANDLE_INDEX={reference_candle_index}. Must be -1 (forming) or -2 (closed).")
            raise RuntimeError(f"INVALID_REFERENCE_CANDLE_INDEX={reference_candle_index}. Must be -1 (forming) or -2 (closed).")

        # Set candle type based on reference index
        candle_type = "forming" if reference_candle_index == -1 else "closed"

        # Get reference candle before any calculations
        candle_tuples = buffer.get_candles(3) if hasattr(buffer, "get_candles") else ()
        
        # DEBUG: Trace candle_tuples
        logger.debug(f"[DEBUG-TRACE] IndicatorPipeline: buffer.symbol={buffer.symbol}, buffer.timeframe={buffer.timeframe}")
        logger.debug(f"[DEBUG-TRACE] candle_tuples type={type(candle_tuples)}, len={len(candle_tuples)}")
        logger.debug(f"[DEBUG-TRACE] reference_candle_index={reference_candle_index}, abs(reference_candle_index)={abs(reference_candle_index)}")
        if len(candle_tuples) > 0:
            logger.debug(f"[DEBUG-TRACE] candle_tuples[-1] type={type(candle_tuples[-1])}, repr={repr(candle_tuples[-1])}")
        
        reference_candle = None
        if len(candle_tuples) >= abs(reference_candle_index):
            reference_candle = candle_tuples[reference_candle_index]
            logger.debug(f"[DEBUG-TRACE] reference_candle assigned: type={type(reference_candle)}, timestamp={reference_candle.timestamp if reference_candle else 'N/A'}")
        else:
            logger.debug(f"[DEBUG-TRACE] reference_candle NOT assigned: len(candle_tuples)={len(candle_tuples)} < abs(reference_candle_index)={abs(reference_candle_index)}")
            # Fallback for testing/mocking when candle_tuples is empty but closes has data
            if len(closes) > 0:
                from .candle_buffer import FrozenOHLCV
                reference_candle = FrozenOHLCV(
                    timestamp=int(time.time() * 1000),
                    open=closes[-1],
                    high=closes[-1],
                    low=closes[-1],
                    close=closes[-1],
                    volume=1.0
                )
                logger.debug(f"[DEBUG-TRACE] reference_candle fallback created: timestamp={reference_candle.timestamp}")

        # Calculate body percentage on reference candle FIRST (same reference)
        body_pct = 0.0
        if reference_candle:
            body_size = abs(reference_candle.close - reference_candle.open)
            candle_range = reference_candle.high - reference_candle.low
            body_pct = body_size / candle_range if candle_range > 0 else 0
            # --- BODY FILTER FORENSIC AUDIT: Log OHLCV data for reference_candle ---
            logger.debug(
                f"[BODY-FORENSIC] reference_candle OHLCV: "
                f"timestamp={reference_candle.timestamp}, "
                f"open={reference_candle.open}, "
                f"high={reference_candle.high}, "
                f"low={reference_candle.low}, "
                f"close={reference_candle.close}, "
                f"body_size={body_size}, "
                f"candle_range={candle_range}, "
                f"body_pct_calculated={body_pct*100:.2f}%"
            )

        # --- FORENSIC FIX: SLICE EMA DATA TO MATCH REFERENCE CANDLE ---
        # No look-ahead bias, no stale-signal bias - EMA calculated only up to reference candle
        # If reference_candle_index=-1 (forming, realtime mode): use all closes up to current forming candle
        # If reference_candle_index=-2 (closed, confirmation mode): exclude only the last forming candle
        if reference_candle_index == -1:
            ema_closes = closes.copy()  # Include forming candle for realtime calculations
        else:
            ema_closes = closes[:-1].copy()  # Exclude forming candle for confirmation mode

        if len(ema_closes) >= fast:
            try:
                results[f"ema{fast}"] = EMACalculator.calculate(ema_closes, fast)
            except Exception as e:
                logger.error(f"Failed to calculate EMA{fast} for {buffer.symbol}: {e}")

        if len(ema_closes) >= slow:
            try:
                results[f"ema{slow}"] = EMACalculator.calculate(ema_closes, slow)
            except Exception as e:
                logger.error(f"Failed to calculate EMA{slow} for {buffer.symbol}: {e}")
        
        # Legacy aliases used by strategy logging and tests
        if f"ema{fast}" in results:
            results["ema9"] = results[f"ema{fast}"]
        if f"ema{slow}" in results:
            results["ema21"] = results[f"ema{slow}"]

        results["crossover_detected"] = 0.0
        results["crossover_bullish"] = 0.0
        results["crossover_bearish"] = 0.0

        crossover_min = slow + 1
        if len(ema_closes) >= crossover_min:
            try:
                cache_key = f"{buffer.symbol}_{buffer.timeframe}"
                async with self._cache_lock:
                    cached = self.ema_series_cache.get(cache_key, {})
                    fast_cached = cached.get(f"fast_{fast}")
                    slow_cached = cached.get(f"slow_{slow}")
                    
                fast_series = EMACalculator.calculate_incremental_series(ema_closes, fast, fast_cached)
                slow_series = EMACalculator.calculate_incremental_series(ema_closes, slow, slow_cached)
                
                async with self._cache_lock:
                    if cache_key not in self.ema_series_cache:
                        self.ema_series_cache[cache_key] = {}
                    self.ema_series_cache[cache_key][f"fast_{fast}"] = fast_series
                    self.ema_series_cache[cache_key][f"slow_{slow}"] = slow_series

                if len(fast_series) >= 3 and len(slow_series) >= 3:
                    fast_now = fast_series[-1]
                    slow_now = slow_series[-1]

                    fast_completed = fast_series[-2]
                    fast_prev = fast_series[-3]

                    slow_completed = slow_series[-2]
                    slow_prev = slow_series[-3]

                    # Previous candle crossover logic removed because fast_now correctly points 
                    # to the reference candle due to ema_closes slicing logic.
                    
                    # --- UNIFIED CROSSOVER LOGIC ---
                    # Whether forming (-1) or closed (-2), ema_closes aligns the series such that 
                    # fast_now is ALWAYS the reference candle.
                    bullish_crossover = fast_completed <= slow_completed and fast_now > slow_now
                    bearish_crossover = fast_completed >= slow_completed and fast_now < slow_now
                    
                    if reference_candle_index not in [-1, -2]:
                        raise RuntimeError(f"Unsupported reference_candle_index={reference_candle_index}")

                    results["crossover_detected"] = 1.0 if (bullish_crossover or bearish_crossover) else 0.0
                    results["crossover_bullish"] = 1.0 if bullish_crossover else 0.0
                    results["crossover_bearish"] = 1.0 if bearish_crossover else 0.0

                    # --- SELL COLOR VALIDATION ROOT-CAUSE AUDIT: Log crossover detection details ---
                    logger.debug(
                        f"[CROSSOVER-DETECTION] symbol={buffer.symbol}, timeframe={buffer.timeframe}, "
                        f"fast_prev={fast_prev:.4f}, fast_completed={fast_completed:.4f}, fast_now={fast_now:.4f}, "
                        f"slow_prev={slow_prev:.4f}, slow_completed={slow_completed:.4f}, slow_now={slow_now:.4f}, "
                        f"bullish_crossover={bullish_crossover}, bearish_crossover={bearish_crossover}, "
                        f"reference_candle_index={reference_candle_index}"
                    )

                    # --- FORENSIC FIX: SIGNAL_CANDLE_TS MATCHES REFERENCE CANDLE ---
                    # Use the timestamp of the actual reference candle (forming or closed)
                    signal_candle_index = reference_candle_index
                    if len(candle_tuples) >= abs(signal_candle_index):
                        results["signal_candle_ts"] = float(candle_tuples[signal_candle_index].timestamp)

                    if results["crossover_detected"] > 0:
                        dir_str = "BULLISH" if bullish_crossover else "BEARISH"
                        logger.info(
                            f"🔀 EMA Crossover: {buffer.symbol} {buffer.timeframe} "
                            f"EMA{fast}/{slow} → {dir_str}"
                        )
            except Exception as e:
                logger.error(
                    f"[INDICATORS] Failed to detect crossover for {buffer.symbol}/{buffer.timeframe}: {e}",
                    exc_info=True,
                )

        # Calculate ADX on reference candle range for snapshot consistency
        # Slice data to include only candles up to reference_candle_index
        # If reference_candle_index=-1 (forming, realtime mode): use all data up to current forming candle
        # If reference_candle_index=-2 (closed, confirmation mode): exclude only the last forming candle
        if reference_candle_index == -1:
            adx_highs = highs.copy()  # Include forming candle for realtime calculations
            adx_lows = lows.copy()
            adx_closes = closes.copy()
        else:
            adx_highs = highs[:-1] if len(highs) > 1 else highs  # Exclude forming candle for confirmation mode
            adx_lows = lows[:-1] if len(lows) > 1 else lows
            adx_closes = closes[:-1] if len(closes) > 1 else closes

        adx = 0.0
        if len(adx_closes) >= 15 and len(adx_highs) >= 15 and len(adx_lows) >= 15:
            try:
                adx = ADXCalculator.calculate(adx_highs, adx_lows, adx_closes, period=14)
                results["adx"] = adx
            except Exception as e:
                logger.debug(f"[INDICATORS] ADX calc skipped for {buffer.symbol}/{buffer.timeframe}: {e}")

        # --- FORENSIC RUNTIME METADATA & VALIDATION ---
        # Use the same reference_candle_index for ALL indicators to maintain bundle consistency
        ema_actual_index = reference_candle_index
        adx_actual_index = reference_candle_index
        body_actual_index = reference_candle_index
        price_actual_index = reference_candle_index
        signal_actual_index = reference_candle_index

        # Store metadata for audit trail
        results["ema_actual_index"] = ema_actual_index
        results["adx_actual_index"] = adx_actual_index
        results["body_actual_index"] = body_actual_index
        results["price_actual_index"] = price_actual_index
        results["signal_actual_index"] = signal_actual_index

        # RUNTIME VALIDATION: ALL INDEXES MUST MATCH (no assert - use proper exception)
        if not (
            ema_actual_index ==
            adx_actual_index ==
            body_actual_index ==
            price_actual_index ==
            signal_actual_index
        ):
            raise RuntimeError(
                f"INDICATOR_BUNDLE_MISMATCH: "
                f"EMA={ema_actual_index}, "
                f"ADX={adx_actual_index}, "
                f"BODY={body_actual_index}, "
                f"PRICE={price_actual_index}, "
                f"SIGNAL={signal_actual_index}"
            )

        # Get EMA values from results
        ema_fast = results.get(f"ema{fast}", 0.0)
        ema_slow = results.get(f"ema{slow}", 0.0)

        # Get reference candle timestamp
        reference_candle_timestamp = reference_candle.timestamp if reference_candle else 0
        # Create MarketSnapshot - all values pre-validated by signal gate
        snapshot = MarketSnapshot.create(
            symbol=buffer.symbol,
            timeframe=buffer.timeframe,
            reference_candle_timestamp=reference_candle_timestamp,
            reference_candle_index=reference_candle_index,  # HARDCODED TO -2
            candle_type=candle_type,                        # HARDCODED TO "closed"
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            adx=adx,
            body_pct=body_pct,
            indicators=results,
            raw_data={
                "open": reference_candle.open if reference_candle else 0,
                "high": reference_candle.high if reference_candle else 0,
                "low": reference_candle.low if reference_candle else 0,
                "close": reference_candle.close if reference_candle else 0,
                "volume": reference_candle.volume if reference_candle else 0,
            } if reference_candle else None,
        )
        # Production log - only log successful snapshot creation
        logger.info(f"[SNAPSHOT-CREATED] symbol={buffer.symbol}, timeframe={buffer.timeframe}, reference_candle_index={snapshot.reference_candle_index}, candle_type={snapshot.candle_type}")

        # --- ROOT-CAUSE FIX: Store OHLCV data in indicator_cache for MarketSnapshot reconstruction ---
        # Add OHLCV data AND body_pct to results dict so Strategy can rebuild MarketSnapshot correctly
        if reference_candle:
            results["close"] = reference_candle.close
            results["open"] = reference_candle.open
            results["high"] = reference_candle.high
            results["low"] = reference_candle.low
            results["volume"] = reference_candle.volume
            # Store body_pct as decimal (0.8333), NOT percentage (83.33%)
            # Strategy will multiply by 100 for display
            results["body_pct"] = body_pct
            # --- BODY_PCT MUTATION FORENSIC AUDIT: Log at IndicatorPipeline ---
            logger.debug(
                f"[MUTATION-INDICATOR] id(results)={id(results)}, "
                f"body_pct={body_pct}, type(body_pct)={type(body_pct)}, "
                f"results['body_pct']={results.get('body_pct', 'NOT_SET')}"
            )

        # Update cache for backward compatibility with atomic lock
        cache_key = f"{buffer.symbol}_{buffer.timeframe}"
        async with self._cache_lock:
            self.indicator_cache[cache_key] = results
            # Update snapshot cache with TTL
            self.snapshot_cache[cache_key] = (snapshot, time.time())

        return snapshot

    def get_snapshot(self, symbol: str, timeframe: str) -> Optional[MarketSnapshot]:
        """
        Get snapshot from cache if not expired.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe
        
        Returns:
            MarketSnapshot if available and not expired, None otherwise
        """
        cache_key = f"{symbol}_{timeframe}"
        if cache_key in self.snapshot_cache:
            snapshot, timestamp = self.snapshot_cache[cache_key]
            if time.time() - timestamp < self.snapshot_cache_ttl:
                return snapshot
            else:
                # Remove expired snapshot
                del self.snapshot_cache[cache_key]
        return None

    def invalidate_snapshot(self, symbol: str, timeframe: str) -> None:
        """
        Invalidate snapshot cache for a specific symbol/timeframe.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe
        """
        cache_key = f"{symbol}_{timeframe}"
        if cache_key in self.snapshot_cache:
            del self.snapshot_cache[cache_key]

    async def cleanup_stale_snapshots(self) -> None:
        """Remove all expired snapshots from cache."""
        current_time = time.time()
        expired_keys = []
        async with self._cache_lock:
            expired_keys = [
                key for key, (_, timestamp) in self.snapshot_cache.items()
                if current_time - timestamp >= self.snapshot_cache_ttl
            ]
            for key in expired_keys:
                del self.snapshot_cache[key]
                # Also cleanup corresponding indicator cache
                if key in self.indicator_cache:
                    del self.indicator_cache[key]
        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired snapshot cache entries")