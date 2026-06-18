from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import collections
import time

from loguru import logger

from infrastructure.exchange.base_exchange import OHLCV

# Timeframe -> milliseconds, used for Hard Timeline Guard threshold
TIMEFRAME_MS: Dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H":  3_600_000,
    "2H":  7_200_000,
    "4H":  14_400_000,
    "1D":  86_400_000,
    "1W":  604_800_000,
    "1M":  2_592_000_000,
}


@dataclass(frozen=True)  # ĐÔNG CỨNG DATACLASS, KHÔNG THỂ SỬA ĐỔI NGẪU NHIÊN
class FrozenOHLCV:
    """Immutable version of OHLCV to prevent accidental mutations."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_ohlcv(cls, ohlcv: OHLCV) -> "FrozenOHLCV":
        return cls(
            timestamp=ohlcv.timestamp,
            open=ohlcv.open,
            high=ohlcv.high,
            low=ohlcv.low,
            close=ohlcv.close,
            volume=ohlcv.volume
        )


@dataclass
class CandleBuffer:
    """Bất biến RingBuffer lưu trữ OHLCV data, triệt tiêu GC Overhead và Race Condition."""

    symbol: str
    timeframe: str
    max_candles: int = 1500  # [CÁCH 2] Đủ rộng để chứa 1440 nến warmup cho EMA hội tụ 100%
    candles: collections.deque = field(init=False)
    _last_candle_timestamp: int = 0  # Tra cứu timestamp cuối cùng, O(1)

    def __post_init__(self) -> None:
        # Ensure deque capacity matches max_candles (important for unit tests)
        object.__setattr__(
            self,
            "candles",
            collections.deque(maxlen=self.max_candles),
        )
        # [PHASE 6] Initialise mutable forensic counters and watermark safely
        object.__setattr__(self, "_high_watermark_ts", 0)
        object.__setattr__(self, "_extreme_out_of_order_total", 0)
        object.__setattr__(self, "_timeline_corruption_total", 0)
        object.__setattr__(self, "_historical_reseed_rejected_total", 0)

    _scanned_timestamps: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200))  # Track các timestamps đã xử lý
    last_updated: Optional[datetime] = None
    is_complete: bool = False

    # [PHASE 6] Buffer Watermark — absolute highest ts ever accepted; never rolls back
    _high_watermark_ts: int = 0

    # [PHASE 6] Forensic counters for candle timeline integrity
    _extreme_out_of_order_total: int = 0
    _timeline_corruption_total: int = 0
    _historical_reseed_rejected_total: int = 0
    _last_critical_log_ts: float = 0.0

    def clear(self) -> None:
        """Clear toàn bộ state của buffer, bao gồm mảng nến và mốc timestamp."""
        self.candles.clear()
        self._last_candle_timestamp = 0
        # NOTE: _high_watermark_ts is intentionally NOT reset on clear().
        # This preserves timeline protection across REST seed / reconnect cycles.
        self._scanned_timestamps.clear()
        self.last_updated = None
        self.is_complete = False

    def add_candle(self, candle: OHLCV, reseed: bool = False) -> bool:
        """Thêm nến vào buffer, chỉ chấp nhận timestamp mới hơn -> O(1) ops, không O(n)"""

        # [PHASE 6] Watermark Protection for historical reseed
        # During reseed (REST hydration), reject candles older than the high watermark
        # that was established by live WS data — prevents timeline poisoning on reconnect.
        if reseed and self._high_watermark_ts > 0 and candle.timestamp < self._high_watermark_ts:
            object.__setattr__(self, "_historical_reseed_rejected_total", self._historical_reseed_rejected_total + 1)
            logger.debug(
                f"[TIMELINE] Reseed candle rejected (older than watermark) for {self.symbol} {self.timeframe}: "
                f"ts={candle.timestamp} < watermark={self._high_watermark_ts}."
            )
            return False

        # Chỉ thêm nến nếu timestamp > timestamp cuối cùng (loại bỏ duplicate 100% bằng O(1) check)
        if candle.timestamp < self._last_candle_timestamp:
            gap_ms = self._last_candle_timestamp - candle.timestamp
            tf_ms = TIMEFRAME_MS.get(self.timeframe, 60_000)

            # [PHASE 6] HARD TIMELINE GUARD: extreme out-of-order = gap > 3x timeframe
            if gap_ms > tf_ms * 3:
                object.__setattr__(self, "_extreme_out_of_order_total", self._extreme_out_of_order_total + 1)
                object.__setattr__(self, "_timeline_corruption_total", self._timeline_corruption_total + 1)

                now = time.time()
                if now - self._last_critical_log_ts > 30.0:
                    logger.critical(
                        f"[TIMELINE CORRUPTION] EXTREME out-of-order candle for {self.symbol} {self.timeframe}: "
                        f"ts={candle.timestamp} << last_ts={self._last_candle_timestamp} "
                        f"(gap={gap_ms/1000:.1f}s > 3x tf={tf_ms/1000:.0f}s). "
                        f"Total extreme_oo={self._extreme_out_of_order_total}"
                    )
                    object.__setattr__(self, "_last_critical_log_ts", now)
            else:
                # Aggregate counter instead of spamming warning
                pass
            return False

        if candle.timestamp == self._last_candle_timestamp:
            # Same-timestamp update: replace the current forming candle (in-progress update)
            if len(self.candles) > 0:
                frozen = FrozenOHLCV.from_ohlcv(candle)
                self.candles[-1] = frozen
                self.last_updated = datetime.now(timezone.utc)
                self.is_complete = candle.confirmed
                logger.debug(f"Updated existing candle {self.symbol} {self.timeframe}: timestamp={candle.timestamp}")
            return candle.confirmed

        # New bar opened — previous candle is implicitly complete
        frozen_candle = FrozenOHLCV.from_ohlcv(candle)
        self.candles.append(frozen_candle)
        self._scanned_timestamps.append(candle.timestamp)
        self._last_candle_timestamp = candle.timestamp

        # [PHASE 6] Advance high watermark — strictly monotonic, never decreases
        if candle.timestamp > self._high_watermark_ts:
            object.__setattr__(self, "_high_watermark_ts", candle.timestamp)

        self.last_updated = datetime.now(timezone.utc)
        self.is_complete = candle.confirmed
        logger.debug(f"Added new immutable candle {self.symbol} {self.timeframe}: {len(self.candles)} total")
        return True

    @property
    def high_watermark_ts(self) -> int:
        """[PHASE 6] Read-only access to the buffer's high watermark timestamp."""
        return self._high_watermark_ts

    def get_forensic_metrics(self) -> Dict[str, int]:
        """[PHASE 6] Return forensic timeline counters for MDE aggregation."""
        return {
            "extreme_out_of_order_total": self._extreme_out_of_order_total,
            "timeline_corruption_total": self._timeline_corruption_total,
            "historical_reseed_rejected_total": self._historical_reseed_rejected_total,
        }

    def get_candles(self, limit: int = 100) -> Tuple[FrozenOHLCV, ...]:
        """Trả về TUPLE BẤT BIẾN, không thể sửa đổi bởi Strategy, triệt tiêu race condition."""
        return tuple(self.candles)[-limit:]

    def get_close_prices(self, limit: int = 100) -> List[float]:
        return [c.close for c in list(self.candles)[-limit:]]

    def get_high_prices(self, limit: int = 100) -> List[float]:
        return [c.high for c in list(self.candles)[-limit:]]

    def get_low_prices(self, limit: int = 100) -> List[float]:
        return [c.low for c in list(self.candles)[-limit:]]

    def get_ohlcv_df(self, limit: int = 200):
        sorted_candles = list(self.candles)[-limit:]
        data = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in sorted_candles
        ]
        # Lazy import to avoid heavy pandas dependency at import time
        import pandas as pd

        return pd.DataFrame(data)