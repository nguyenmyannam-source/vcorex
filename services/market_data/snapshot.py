"""
Market Snapshot Model - Ensures Snapshot Consistency across all trading components.

PRINCIPLE: ONE SIGNAL = ONE UNIQUE SNAPSHOT

All trading decisions (EMA, ADX, Body%, Signal Generation, Risk Validation, Order Creation)
must be calculated from the SAME market snapshot with the same timestamp and candle reference.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import uuid


@dataclass(frozen=True)  # GUARANTEES IMMUTABILITY: No post-creation mutation possible
class MarketSnapshot:
    """
    PRODUCTION ONLY: Immutable market snapshot for closed-candle signals only.
    
    All trading decisions (EMA, ADX, Body%, Signal Generation, Risk Validation, Order Creation)
    must be calculated from this single snapshot to ensure temporal consistency.
    
    PRODUCTION HARD RULES (ENFORCED AT CREATION):
    - reference_candle_index MUST be -2 (only last closed candle)
    - candle_type MUST be "closed" (never allow forming candles in signal path)
    - reference_candle_timestamp MUST be a valid positive timestamp
    
    Attributes:
        snapshot_id: Unique identifier for this snapshot (UUID)
        snapshot_timestamp: When this snapshot was created (UTC)
        symbol: Trading symbol (e.g., BTC-USDT-SWAP)
        timeframe: Timeframe (e.g., 1H, 4H, 1D)
        reference_candle_timestamp: Timestamp of the reference candle used for calculations
        reference_candle_index: Always -2 (last closed candle in buffer)
        candle_type: Always "closed" (only closed candles allowed for signals)
        ema_fast: Fast EMA value at snapshot time
        ema_slow: Slow EMA value at snapshot time
        adx: ADX value at snapshot time
        body_pct: Body percentage of reference candle
        signal_side: Signal side ('BUY' or 'SELL') if crossover detected
        entry_price: Entry price at snapshot time
        indicators: Full indicator dictionary for backward compatibility
        raw_data: Raw OHLCV data for reference
    """
    
    snapshot_id: str
    snapshot_timestamp: float
    symbol: str
    timeframe: str
    reference_candle_timestamp: float
    reference_candle_index: int  # HARD ENFORCED: Only -2 allowed
    candle_type: str  # HARD ENFORCED: Only "closed" allowed
    ema_fast: float
    ema_slow: float
    adx: float
    body_pct: float
    signal_side: Optional[str] = None
    entry_price: Optional[float] = None
    indicators: Dict[str, Any] = field(default_factory=dict)
    raw_data: Optional[Dict[str, Any]] = None
    
    @classmethod
    def create(
        cls,
        symbol: str,
        timeframe: str,
        reference_candle_timestamp: float,
        reference_candle_index: int,
        candle_type: str,
        ema_fast: float,
        ema_slow: float,
        adx: float,
        body_pct: float,
        indicators: Dict[str, Any],
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> "MarketSnapshot":
        """
        Create a new market snapshot with auto-generated snapshot_id and timestamp.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe
            reference_candle_timestamp: Timestamp of reference candle
            reference_candle_index: Index of reference candle in buffer
            candle_type: Type of candle ('forming' or 'closed')
            ema_fast: Fast EMA value
            ema_slow: Slow EMA value
            adx: ADX value
            body_pct: Body percentage
            indicators: Full indicator dictionary
            raw_data: Raw OHLCV data
        
        Returns:
            MarketSnapshot instance
        """
        snapshot_id = str(uuid.uuid4())
        snapshot_timestamp = datetime.now(timezone.utc).timestamp()
        
        # --- CENTRALIZED PRODUCTION VALIDATION (FAIL-FAST, NO EXCEPTIONS) ---
        # Support both realtime (forming candle, -1) and confirmation (closed candle, -2) modes
        if reference_candle_index not in (-2, -1):
            raise RuntimeError(
                f"SNAPSHOT_CREATION_FAILED: Invalid reference_candle_index={reference_candle_index} for {symbol}/{timeframe}. "
                f"PRODUCTION RULE: reference_candle_index MUST be -2 (closed candle) or -1 (forming candle)."
            )
        if candle_type not in ("closed", "forming"):
            raise RuntimeError(
                f"SNAPSHOT_CREATION_FAILED: Invalid candle_type={candle_type} for {symbol}/{timeframe}. "
                f"PRODUCTION RULE: candle_type MUST be 'closed' or 'forming'."
            )
        if reference_candle_timestamp <= 0:
            raise RuntimeError(
                f"SNAPSHOT_CREATION_FAILED: Invalid reference_candle_timestamp={reference_candle_timestamp} for {symbol}/{timeframe}. "
                f"PRODUCTION HARD RULE: reference_candle_timestamp MUST be a valid positive timestamp."
            )
        if not symbol or not timeframe:
            raise RuntimeError(
                f"SNAPSHOT_CREATION_FAILED: Invalid symbol/timeframe for snapshot. symbol='{symbol}', timeframe='{timeframe}'"
            )

        return cls(
            snapshot_id=snapshot_id,
            snapshot_timestamp=snapshot_timestamp,
            symbol=symbol,
            timeframe=timeframe,
            reference_candle_timestamp=reference_candle_timestamp,
            reference_candle_index=reference_candle_index,
            candle_type=candle_type,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            adx=adx,
            body_pct=body_pct,
            indicators=indicators,
            raw_data=raw_data,
        )
    
    def get_signal_side(self) -> Optional[str]:
        """Determine signal side from EMA crossover."""
        if self.ema_fast > self.ema_slow:
            return "BUY"
        elif self.ema_fast < self.ema_slow:
            return "SELL"
        return None
    
    def is_crossover_detected(self) -> bool:
        """Check if EMA crossover is detected in this snapshot."""
        return self.indicators.get("crossover_detected", 0.0) > 0.0
    
    def is_bullish_crossover(self) -> bool:
        """Check if bullish crossover is detected."""
        return self.indicators.get("crossover_bullish", 0.0) > 0.0
    
    def is_bearish_crossover(self) -> bool:
        """Check if bearish crossover is detected."""
        return self.indicators.get("crossover_bearish", 0.0) > 0.0
    
    def validate_consistency(self) -> bool:
        """
        PRODUCTION ONLY: Validate that this snapshot is internally consistent.
        Since we enforce all rules at creation time, this is a redundant safety check
        that will NEVER fail in normal operation - exists only to catch catastrophic bugs.
        
        Returns:
            True if snapshot is consistent, raises RuntimeError otherwise
        """
        # These checks are redundant with creation-time validation but serve as last line of defense
        if self.reference_candle_index not in (-2, -1):
            raise RuntimeError(
                f"SNAPSHOT_INCONSISTENT: Invalid reference_candle_index={self.reference_candle_index} for {self.symbol}/{self.timeframe}. "
                f"THIS SHOULD NEVER HAPPEN: System is in catastrophic state."
            )
        if self.candle_type not in ("closed", "forming"):
            raise RuntimeError(
                f"SNAPSHOT_INCONSISTENT: Invalid candle_type={self.candle_type} for {self.symbol}/{self.timeframe}. "
                f"THIS SHOULD NEVER HAPPEN: System is in catastrophic state."
            )
        
        if self.signal_side:
            expected_side = self.get_signal_side()
            if self.signal_side != expected_side:
                raise RuntimeError(
                    f"SNAPSHOT_INCONSISTENT: Signal side mismatch for {self.symbol}/{self.timeframe}. "
                    f"signal_side={self.signal_side}, expected={expected_side} based on EMA values. "
                    f"THIS SHOULD NEVER HAPPEN: System is in catastrophic state."
                )
        
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert snapshot to dictionary for serialization."""
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_timestamp": self.snapshot_timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "reference_candle_timestamp": self.reference_candle_timestamp,
            "reference_candle_index": self.reference_candle_index,
            "candle_type": self.candle_type,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "adx": self.adx,
            "body_pct": self.body_pct,
            "signal_side": self.signal_side,
            "entry_price": self.entry_price,
            "indicators": self.indicators,
        }