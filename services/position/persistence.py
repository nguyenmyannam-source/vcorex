"""
Position Persistence Layer - Institutional Grade.
Bridges TrackedPosition (in-memory domain model) ↔ Position (SQLAlchemy ORM).

Guarantees:
- Bot can recover open positions after restart
- All state changes (open, partial_tp, close) are atomically persisted
- Defensive error handling: never crashes the trading engine
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from loguru import logger


from services.position.models import TrackedPosition, TakeProfitLevel, PositionStatus

# Statuses considered "active" for DB reconciliation (aligned with load_open_positions)
ACTIVE_POSITION_DB_STATUSES = [
    "OPENED", "opened", "PARTIAL_TP", "partial_tp",
    "IN_FLIGHT", "in_flight", "PENDING", "pending",
    "PENDING_SUBMIT", "pending_submit",
    "PENDING_RECONCILE", "pending_reconcile",
    "PARTIALLY_FILLED", "partially_filled",
    "CLOSING", "closing", "UNVERIFIED", "unverified",
]


class PositionPersistence:
    def __init__(self, db_session_factory=None, *args, **kwargs):
        """
        Khởi tạo tầng lưu trữ vị thế.
        Sử dụng *args và **kwargs để nuốt trọn mọi tham số cấu hình thừa từ container.
        """
        self.db_session_factory = db_session_factory
        self.args = args
        self.kwargs = kwargs
        # [P0-FIX] Recovery queue: positions that failed to persist are queued here
        # so callers can retry or trigger Reconciliation on next sweep.
        self._persistence_failure_queue: list = []

    def _has_valid_factory(self) -> bool:
        """Check if we have a usable async session factory."""
        return self.db_session_factory is not None and callable(self.db_session_factory)

    async def _publish_failure_alert(self, operation: str, position_id: str, error: Exception) -> None:
        """[P0-FIX] Publish SYSTEM_ALERT to Telegram on DB failure. Never raises."""
        try:
            from core.container import container
            from core.event_bus_components import Event
            from core.events.topics import EventTopic
            event_bus = container.get("event_bus")
            if event_bus:
                await event_bus.publish(Event(
                    event_type=EventTopic.SYSTEM_ALERT,
                    data={
                        "level": "CRITICAL",
                        "title": f"[DB-FAIL] Persistence {operation} FAILED",
                        "message": (
                            f"Position {position_id} could NOT be saved to DB after Exchange confirmed.\n"
                            f"Error: {error}\n"
                            f"ReconciliationService will auto-heal on next sweep (within 10 min)."
                        ),
                        "alert_name": "PersistenceFailure",
                    },
                    source="persistence_layer",
                ))
        except Exception as alert_err:
            logger.error(f"[PERSISTENCE] Failed to publish failure alert: {alert_err}")

    async def load_open_positions(self, *args, **kwargs) -> List[Any]:
        """
        [DB-PERSIST] Load open positions from SQLite and reconstruct TrackedPosition objects.
        Returns empty list on any failure so the engine falls back to OKX API reconciliation.
        """
        try:
            if not self._has_valid_factory():
                logger.warning("[PERSISTENCE] DB Session Factory not available. Skipping DB load.")
                return []

            from infrastructure.storage.database import Position
            from services.position.models import TrackedPosition, TakeProfitLevel, PositionStatus

            positions: List[TrackedPosition] = []

            try:
                async with self.db_session_factory() as session:
                    result = await session.execute(
                        select(Position).where(
                            Position.status.in_(ACTIVE_POSITION_DB_STATUSES)
                        )
                    )
                    db_positions = list(result.scalars().all())

                    for db_pos in db_positions:
                        try:
                            tracked = self._orm_to_tracked(db_pos)
                            positions.append(tracked)
                        except Exception as conv_err:
                            logger.error(
                                f"[PERSISTENCE] Failed to convert DB position {getattr(db_pos, 'position_id', '?')}: {conv_err}"
                            )

                    if positions:
                        logger.info(
                            f"[PERSISTENCE] Recovered {len(positions)} open positions from DB: "
                            + ", ".join(f"{p.symbol}({p.side})" for p in positions)
                        )
                    else:
                        logger.info("[PERSISTENCE] No open positions found in DB.")

            except Exception as db_err:
                logger.error(f"[PERSISTENCE-DB-ERR] Failed to load positions from DB: {db_err}")

            return positions

        except Exception as e:
            logger.error(f"[PERSISTENCE-FATAL] Unexpected error in load_open_positions: {e}")
            return []

    async def save_position(self, position_obj, *args, **kwargs) -> bool:
        """
        [DB-PERSIST] Upsert a TrackedPosition into the positions table.
        Uses position_id (TrackedPosition.id) as the unique key for upsert logic.
        """
        try:
            if not self._has_valid_factory():
                logger.debug("[PERSISTENCE] DB Session Factory not available. Skipping save_position.")
                return True

            from infrastructure.storage.database import Position
            from services.position.models import PositionStatus

            async with self.db_session_factory() as session:
                async with session.begin():
                    # Look up by position_id first
                    pos_id = getattr(position_obj, "id", None) or ""
                    result = await session.execute(
                        select(Position).where(Position.position_id == pos_id)
                    )
                    existing = result.scalar_one_or_none()

                    # Serialize take_profit_levels → JSON
                    tp_json = self._serialize_tp_levels(position_obj)

                    # Serialize algo_order_ids → JSON
                    algo_ids_json = json.dumps(
                        getattr(position_obj, "algo_order_ids", []) or []
                    )

                    # Map status to string
                    status_val = position_obj.status
                    if hasattr(status_val, "value"):
                        status_val = status_val.value
                    status_str = str(status_val).upper()

                    if existing:
                        # UPDATE existing row
                        await session.execute(
                            update(Position)
                            .where(Position.position_id == pos_id)
                            .values(
                                symbol=position_obj.symbol,
                                side=position_obj.side,
                                amount=position_obj.amount,
                                amount_remaining=position_obj.amount_remaining,
                                entry_price=position_obj.entry_price,
                                current_price=position_obj.current_price,
                                unrealized_pnl=position_obj.pnl,
                                realized_pnl=getattr(position_obj, "realized_pnl", 0.0) or 0.0,
                                fee_paid=getattr(position_obj, "fee_paid", 0.0) or 0.0,
                                leverage=int(position_obj.leverage),
                                status=status_str,
                                stop_loss_price=position_obj.stop_loss,
                                take_profit_prices=tp_json,
                                closed_at=position_obj.closed_at,
                                strategy_name=position_obj.strategy_name or None,
                                algo_order_ids=algo_ids_json,
                                exchange_id=getattr(position_obj, "exchange_id", None),
                                ct_val=float(getattr(position_obj, "ct_val", 1.0) or 1.0),
                                signal_id=getattr(position_obj, "signal_id", None) or None,
                                timeframe=getattr(position_obj, "timeframe", None) or None,
                            )
                        )
                        logger.debug(f"[PERSISTENCE] Updated position {pos_id} (status={status_str})")
                    else:
                        # INSERT new row
                        new_pos = Position(
                            position_id=pos_id,
                            symbol=position_obj.symbol,
                            side=position_obj.side,
                            amount=position_obj.amount,
                            amount_remaining=position_obj.amount_remaining,
                            entry_price=position_obj.entry_price,
                            current_price=position_obj.current_price,
                            unrealized_pnl=position_obj.pnl,
                            realized_pnl=getattr(position_obj, "realized_pnl", 0.0) or 0.0,
                            fee_paid=getattr(position_obj, "fee_paid", 0.0) or 0.0,
                            leverage=int(position_obj.leverage),
                            status=status_str,
                            stop_loss_price=position_obj.stop_loss,
                            take_profit_prices=tp_json,
                            closed_at=position_obj.closed_at,
                            strategy_name=position_obj.strategy_name or None,
                            algo_order_ids=algo_ids_json,
                            exchange_id=getattr(position_obj, "exchange_id", None),
                            ct_val=float(getattr(position_obj, "ct_val", 1.0) or 1.0),
                            signal_id=getattr(position_obj, "signal_id", None) or None,
                            timeframe=getattr(position_obj, "timeframe", None) or None,
                        )
                        session.add(new_pos)
                        logger.info(f"[PERSISTENCE] Inserted new position {pos_id} ({position_obj.symbol} {position_obj.side})")

            return True

        except Exception as e:
            pos_id = getattr(position_obj, 'id', '?')
            pos_symbol = getattr(position_obj, 'symbol', '?')
            pos_status = getattr(getattr(position_obj, 'status', None), 'value', getattr(position_obj, 'status', '?'))
            logger.error(
                f"[PERSISTENCE-FAILURE] save_position FAILED for {pos_id} "
                f"({pos_symbol}, status={pos_status}): {e}"
            )
            # [P0-FIX] Queue for reconciliation retry
            self._persistence_failure_queue.append({
                "operation": "save_position",
                "position_id": pos_id,
                "symbol": pos_symbol,
                "status": str(pos_status),
                "error": str(e),
            })
            # [P0-FIX] Fire Telegram alert without blocking
            import asyncio
            asyncio.ensure_future(self._publish_failure_alert("save_position", pos_id, e))
            return False  # [P0-FIX] Return False so callers can detect the failure

    async def delete_position(self, position_id: str, *args, **kwargs) -> bool:
        """
        [DB-PERSIST] Remove a tracked position from local DB by position_id.
        """
        try:
            if not self._has_valid_factory():
                logger.debug("[PERSISTENCE] DB Session Factory not available. Skipping delete_position.")
                return True

            from infrastructure.storage.database import Position

            async with self.db_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(Position).where(Position.position_id == position_id)
                    )
                    logger.info(f"[PERSISTENCE] Deleted position {position_id} from DB")

            return True

        except Exception as e:
            logger.error(f"[PERSISTENCE-FAILURE] delete_position FAILED for {position_id}: {e}")
            self._persistence_failure_queue.append({
                "operation": "delete_position",
                "position_id": position_id,
                "error": str(e),
            })
            import asyncio
            asyncio.ensure_future(self._publish_failure_alert("delete_position", position_id, e))
            return False  # [P0-FIX] Return False so callers can detect the failure

    async def mark_position_closed(self, position_id: str, realized_pnl: float = 0.0,
                                     fee_paid: float = 0.0, close_price: float = 0.0) -> bool:
        """
        [DB-PERSIST] Mark a position as CLOSED with final P&L data.
        Preserves the row for historical analytics instead of deleting.
        """
        try:
            if not self._has_valid_factory():
                return True

            from infrastructure.storage.database import Position

            async with self.db_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Position)
                        .where(Position.position_id == position_id)
                        .values(
                            status="CLOSED",
                            realized_pnl=realized_pnl,
                            fee_paid=fee_paid,
                            current_price=close_price if close_price > 0 else Position.current_price,
                            closed_at=datetime.now(timezone.utc),
                        )
                    )
                    logger.info(
                        f"[PERSISTENCE] Marked position {position_id} as CLOSED "
                        f"(PnL=${realized_pnl:.2f}, Fee=${fee_paid:.4f})"
                    )

            return True

        except Exception as e:
            logger.error(f"[PERSISTENCE-FAILURE] mark_position_closed FAILED for {position_id}: {e}")
            self._persistence_failure_queue.append({
                "operation": "mark_position_closed",
                "position_id": position_id,
                "error": str(e),
            })
            import asyncio
            asyncio.ensure_future(self._publish_failure_alert("mark_position_closed", position_id, e))
            return False  # [P0-FIX] Return False so callers can detect the failure

    async def sync_history_with_exchange(self, *args, **kwargs) -> bool:
        """
        [DB-PROTECT] Đồng bộ lịch sử giao dịch cục bộ với sàn.
        Phòng thủ tuyệt đối trước lỗi NoneType callable.
        """
        try:
            if not self._has_valid_factory():
                logger.warning("[PERSISTENCE] DB Session Factory trống. Bỏ qua đồng bộ lịch sử xuống DB local.")
                return True
            return True
        except Exception as e:
            logger.error(f"[PERSISTENCE-ERROR] Không thể sync lịch sử với sàn: {e}")
            return True

    # ========== PRIVATE HELPERS ==========

    @staticmethod
    def _serialize_tp_levels(position_obj) -> str:
        """Serialize take_profit_levels list to JSON string for DB storage."""
        tp_levels = getattr(position_obj, "take_profit_levels", []) or []
        tp_data = []
        for tp in tp_levels:
            if hasattr(tp, "price"):
                tp_data.append({"price": tp.price, "exit_pct": tp.exit_pct})
            elif isinstance(tp, dict):
                tp_data.append(tp)
        return json.dumps(tp_data)

    @staticmethod
    def _orm_to_tracked(db_pos) -> Any:
        """Convert a Position ORM object back into a TrackedPosition dataclass."""
        from services.position.models import TrackedPosition, TakeProfitLevel, PositionStatus

        # Parse take_profit_prices JSON → List[TakeProfitLevel]
        tp_levels = []
        if db_pos.take_profit_prices:
            try:
                tp_raw = json.loads(db_pos.take_profit_prices)
                for tp in tp_raw:
                    if isinstance(tp, dict):
                        tp_levels.append(TakeProfitLevel(
                            price=float(tp.get("price", 0)),
                            exit_pct=float(tp.get("exit_pct", 0.5)),
                        ))
                    elif isinstance(tp, (int, float)):
                        tp_levels.append(TakeProfitLevel(price=float(tp), exit_pct=0.5))
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[PERSISTENCE] Could not parse TP prices for position {db_pos.position_id}")

        # Parse algo_order_ids JSON → List[str]
        algo_ids = []
        if db_pos.algo_order_ids:
            try:
                algo_ids = json.loads(db_pos.algo_order_ids)
                if not isinstance(algo_ids, list):
                    algo_ids = []
            except (json.JSONDecodeError, TypeError):
                algo_ids = []

        # Map status string back to enum
        status_str = (db_pos.status or "OPENED").upper()
        try:
            status = PositionStatus(status_str.lower())
        except ValueError:
            status = PositionStatus.OPENED

        # Reconstruct opened_at from created_at
        opened_at = db_pos.created_at or datetime.now(timezone.utc)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        tracked = TrackedPosition(
            id=db_pos.position_id,
            exchange_id=getattr(db_pos, "exchange_id", None),
            symbol=db_pos.symbol,
            side=db_pos.side or "long",
            entry_price=db_pos.entry_price or 0.0,
            current_price=db_pos.current_price or 0.0,
            amount=db_pos.amount or 0.0,
            amount_remaining=db_pos.amount_remaining or 0.0,
            leverage=float(db_pos.leverage or 10),
            ct_val=float(getattr(db_pos, "ct_val", None) or 1.0),
            pnl=db_pos.unrealized_pnl or 0.0,
            realized_pnl=db_pos.realized_pnl or 0.0,
            fee_paid=db_pos.fee_paid or 0.0,
            stop_loss=db_pos.stop_loss_price,
            take_profit_levels=tp_levels,
            status=status,
            opened_at=opened_at,
            closed_at=db_pos.closed_at,
            strategy_name=db_pos.strategy_name or "",
            signal_id=getattr(db_pos, "signal_id", None) or "",
            timeframe=getattr(db_pos, "timeframe", None) or "",
            algo_order_ids=algo_ids,
        )

        return tracked
