"""
Position Data Models - Institutional Grade.
Separating domain logic from engine logic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class PositionStatus(str, Enum):
    """Trạng thái vị thế theo FSM (Finite State Machine) tuyệt đối."""

    PENDING = "pending"
    PENDING_SUBMIT = "pending_submit" # Đã lưu cache, chờ REST ACK
    PENDING_RECONCILE = "pending_reconcile" # REST timeout, cần WS verify
    IN_FLIGHT = "in_flight"  # Đã gửi lệnh, chờ phản hồi từ sàn
    PARTIALLY_FILLED = "partially_filled"  # [REMEDIATION] Position partially filled, waiting for full fill
    OPENED = "opened"
    PARTIAL_TP = "partial_tp"
    CLOSING = "closing"
    CLOSED = "closed"
    LIQUIDATED = "liquidated"
    FAILED = "failed"
    UNVERIFIED = "unverified"


@dataclass
class TakeProfitLevel:
    """Mức chốt lời cho vị thế."""

    price: float
    exit_pct: float


@dataclass
class TrackedPosition:
    """Vị thế được track bởi internal system."""

    id: str
    exchange_id: Optional[str]
    symbol: str
    side: str  # "long" / "short"
    entry_price: float
    current_price: float
    amount: float
    amount_remaining: float
    leverage: float
    ct_val: float = 1.0
    pnl: float = 0.0
    realized_pnl: float = 0.0
    fee_paid: float = 0.0
    roe: float = 0.0
    margin: float = 0.0
    notional_size: float = 0.0
    close_price: float = 0.0  # Giá đóng vị thế (dùng cho tính phí khi đóng lệnh)
    stop_loss: Optional[float] = None
    take_profit_levels: List[TakeProfitLevel] = field(default_factory=list)
    status: PositionStatus = PositionStatus.PENDING
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    strategy_name: str = ""
    signal_id: str = ""
    updates: List[Dict[str, Any]] = field(default_factory=list)
    timeframe: str = ""
    body_pct: Optional[float] = None
    algo_order_ids: List[str] = field(default_factory=list)
    sl_algo_order_id: Optional[str] = None
    tp_dispatched: bool = False
    position_opened_event_sent: bool = False
    pnl_percentage: float = 0.0
    notes: str = ""

    def add_update(self, message: str, data: Optional[Dict] = None) -> None:
        """Thêm lịch sử cập nhật cho vị thế."""
        self.updates.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "data": data or {},
            }
        )

    def get_margin(self) -> float:
        """Tính ký quỹ (Margin) USDT."""
        if self.margin > 0.0:
            return self.margin
        if self.leverage <= 0:
            return 0.0
        return (self.amount_remaining * self.ct_val * self.entry_price) / self.leverage

    def get_notional_size(self) -> float:
        """Tính giá trị quy mô lệnh (Notional Size) USDT."""
        if self.notional_size > 0.0:
            return self.notional_size
        return self.amount_remaining * self.ct_val * self.current_price
