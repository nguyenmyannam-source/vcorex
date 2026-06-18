"""
Pure risk management utility functions.
"""

from typing import Union, List, Optional
from dataclasses import dataclass
from core.events.topics import EventTopic
from services.strategies.base_strategy import SignalType


@dataclass
class RiskAssessment:
    """Result of a risk assessment on a signal."""
    approved: bool
    reason: str = ""
    adjusted_position_size: Optional[float] = None
    adjusted_stop_loss: Optional[float] = None

def calculate_stop_loss(entry_price: float, side: str, stop_loss_pct: float) -> float:
    """Calculate stop loss price based on side and percentage."""
    if side.lower() == "buy" or side.lower() == "long":
        return entry_price * (1 - stop_loss_pct)
    else:  # sell / short
        return entry_price * (1 + stop_loss_pct)

def calculate_take_profits(
    entry_price: float, side: str, tp_percentages: List[float]
) -> List[float]:
    """Calculate multiple take profit levels."""
    tps = []
    for pct in tp_percentages:
        if side.lower() == "buy" or side.lower() == "long":
            tps.append(entry_price * (1 + pct))
        else:
            tps.append(entry_price * (1 - pct))
    return tps

def calculate_required_margin(notional_size: float, leverage: int) -> float:
    """Calculate required margin for a position."""
    return notional_size / leverage

# NOTE: Available for future use — not currently called
def _validate_entry_against_market(
    entry_price: float, market_price: float, max_deviation_pct: float = 5.0
) -> RiskAssessment:
    """Validate entry price is within reasonable deviation from current market price."""
    deviation = abs(entry_price - market_price) / market_price * 100
    if deviation > max_deviation_pct:
        return RiskAssessment(
            approved=False,
            reason=f"Entry price deviation too high: {deviation:.2f}% > {max_deviation_pct}%",
        )
    return RiskAssessment(approved=True)

# NOTE: Available for future use — not currently called
def _validate_sl_distance(
    entry_price: float,
    sl_price: float,
    side: Union[str, SignalType],
    min_distance_pct: float = 0.5,
) -> RiskAssessment:
    """Validate stop loss is not set too close to entry price."""
    side_str = side.value if isinstance(side, SignalType) else side
    if side_str.lower() in ("buy", "long"):
        if sl_price >= entry_price:
            return RiskAssessment(approved=False, reason="SL must be below entry for long")
        distance_pct = (entry_price - sl_price) / entry_price * 100
    else:
        if sl_price <= entry_price:
            return RiskAssessment(approved=False, reason="SL must be above entry for short")
        distance_pct = (sl_price - entry_price) / entry_price * 100

    if distance_pct < min_distance_pct:
        return RiskAssessment(
            approved=False,
            reason=f"SL too close to entry: {distance_pct:.2f}% < {min_distance_pct}%",
        )
    return RiskAssessment(approved=True)

def _calculate_max_positions(settings) -> int:
    """
    Calculate maximum allowed concurrent positions based on mode and settings.
    Enforces max_open_positions when production_risk_mode is true OR when explicitly capped (< 9999).
    """
    max_pos = getattr(settings, "max_open_positions", 9999)
    if not settings.production_risk_mode and max_pos >= 9999:
        return 9999
    return max_pos


def validate_tp_levels_no_collision(
    tp_levels: List[float],
    entry_price: float,
    side: Union[str, SignalType],
    existing_tp_prices: List[float] = None,
) -> tuple[bool, str]:
    """
    Validate TP levels have no collisions with each other or existing TPs.

    Returns: (is_valid, reason)
    """
    if existing_tp_prices is None:
        existing_tp_prices = []

    side_str = side.value if isinstance(side, SignalType) else str(side).lower()

    if not tp_levels:
        return True, "No TP levels to validate"

    # Check if TP levels are in ascending order and unique
    sorted_tps = sorted(set(tp_levels))  # Remove duplicates and sort

    if len(sorted_tps) != len(tp_levels):
        return False, f"Duplicate TP levels detected: {tp_levels}"

    # Validate TP levels are on correct side of entry
    for i, tp_price in enumerate(sorted_tps):
        if side_str in ("buy", "long"):
            if tp_price <= entry_price:
                return False, f"TP level {i+1} ({tp_price}) must be above entry ({entry_price}) for LONG"
        else:
            if tp_price >= entry_price:
                return False, f"TP level {i+1} ({tp_price}) must be below entry ({entry_price}) for SHORT"

    # Check collision with existing TP levels from other positions
    if existing_tp_prices:
        for existing_tp in existing_tp_prices:
            # Check if any new TP is too close (within 0.1% tolerance)
            collision_threshold = abs(entry_price * 0.001)  # 0.1% of entry price

            for new_tp in sorted_tps:
                if abs(new_tp - existing_tp) < collision_threshold:
                    return False, (
                        f"TP collision detected: new TP {new_tp} too close to "
                        f"existing TP {existing_tp} (threshold: {collision_threshold:.8f})"
                    )

    # Check minimum distance between TP levels
    min_distance_pct = 0.5  # Minimum 0.5% between TP levels
    for i in range(1, len(sorted_tps)):
        distance = abs(sorted_tps[i] - sorted_tps[i-1]) / entry_price * 100
        if distance < min_distance_pct:
            return False, (
                f"TP levels too close: TP{i} ({sorted_tps[i-1]}) and "
                f"TP{i+1} ({sorted_tps[i]}) only {distance:.2f}% apart "
                f"(minimum {min_distance_pct}%)"
            )

    return True, "TP levels valid - no collisions detected"


def validate_sl_not_in_tp_range(
    sl_price: float,
    tp_levels: List[float],
    entry_price: float,
    side: Union[str, SignalType],
) -> tuple[bool, str]:
    """
    Validate SL is not within the TP profit range (would create conflict).

    Returns: (is_valid, reason)
    """
    side_str = side.value if isinstance(side, SignalType) else str(side).lower()

    if not tp_levels:
        return True, "No TP levels to validate against SL"

    # Validate SL direction is correct for side
    if side_str in ("buy", "long"):
        # For LONG: SL must be below entry
        if sl_price >= entry_price:
            return False, f"SL ({sl_price}) must be below entry ({entry_price}) for LONG"
        # SL should not be above any TP (that would close on SL before TP)
        for tp_price in tp_levels:
            if sl_price > tp_price:
                return False, (
                    f"SL ({sl_price}) is above some TP levels ({tp_levels}), "
                    f"which would cause SL to trigger first"
                )
    else:
        # For SHORT: SL must be above entry
        if sl_price <= entry_price:
            return False, f"SL ({sl_price}) must be above entry ({entry_price}) for SHORT"
        # SL should not be below any TP
        for tp_price in tp_levels:
            if sl_price < tp_price:
                return False, (
                    f"SL ({sl_price}) is below some TP levels ({tp_levels}), "
                    f"which would cause SL to trigger first"
                )

    return True, "SL does not conflict with TP levels"


def identify_orphan_algo_orders(
    placed_algo_order_ids: List[str],
    tracked_algo_order_ids: List[str],
) -> tuple[List[str], List[str], List[str]]:
    """
    Identify orphan algo orders (placed on exchange but not tracked in memory).

    Returns: (orphans, unmatched_placed, unmatched_tracked)
    - orphans: Placed on exchange but not in memory (need cleanup)
    - unmatched_placed: Exchange orders not in memory tracking
    - unmatched_tracked: Memory tracking not on exchange (may be pending)
    """
    placed_set = set(placed_algo_order_ids)
    tracked_set = set(tracked_algo_order_ids)

    orphans = list(placed_set - tracked_set)  # On exchange but not tracked
    unmatched_placed = orphans  # Alias for clarity
    unmatched_tracked = list(tracked_set - placed_set)  # Tracked but not placed yet

    return orphans, unmatched_placed, unmatched_tracked