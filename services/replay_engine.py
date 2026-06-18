"""
Replay Engine for reconstructing system state from the immutable AuditLog database.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
from loguru import logger
from sqlalchemy import select, and_

from core.event_bus_components import Event
from core.audit_journal import calculate_hash
from infrastructure.storage.database import AuditLog, get_session


class ReplayEngine:
    """Reconstructs VCOREX system memory state by replaying audit trails."""

    def __init__(self):
        pass

    def apply_event(self, state: Dict[str, Any], event: Event) -> Dict[str, Any]:
        """Deterministic reducer mapping events to state mutations."""
        event_type = event.event_type
        data = event.data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass

        if not isinstance(data, dict):
            # Fallback if payload isn't dict
            data = {"raw": data}

        if event_type == "position.opened":
            symbol = data.get("symbol")
            if symbol:
                state["positions"][symbol] = {
                    "symbol": symbol,
                    "side": data.get("side"),
                    "size": data.get("size", data.get("amount")),
                    "entry_price": data.get("entry_price"),
                    "opened_at": event.timestamp.isoformat(),
                    "status": "OPEN",
                }
        elif event_type == "position.closed":
            symbol = data.get("symbol")
            if symbol and symbol in state["positions"]:
                state["positions"][symbol]["status"] = "CLOSED"
                state["positions"][symbol]["closed_at"] = event.timestamp.isoformat()
        elif event_type == "ws_raw.account":
            balances = data.get("balances", {})
            for asset, bal in balances.items():
                state["balances"][asset] = bal
        elif event_type == "ws_raw.order":
            order_id = data.get("order_id")
            if order_id:
                state["orders"][order_id] = {
                    "order_id": order_id,
                    "symbol": data.get("symbol"),
                    "side": data.get("side"),
                    "price": data.get("price"),
                    "size": data.get("size"),
                    "status": data.get("status"),
                    "timestamp": event.timestamp.isoformat(),
                }
        elif event_type == "risk.signal_approved":
            symbol = data.get("symbol")
            if symbol:
                state["risk_state"][symbol] = {
                    "approved": True,
                    "timestamp": event.timestamp.isoformat(),
                    "max_exposure": data.get("max_exposure"),
                }
        elif event_type == "risk.signal_rejected":
            symbol = data.get("symbol")
            if symbol:
                state["risk_state"][symbol] = {
                    "approved": False,
                    "timestamp": event.timestamp.isoformat(),
                    "reason": data.get("reason"),
                }

        return state

    async def reconstruct_state(
        self,
        start_sequence_id: int = 0,
        initial_state: Optional[Dict[str, Any]] = None,
        target_timestamp: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Read AuditLog, verify cryptographic signature chain, and reconstruct state.
        Raises ValueError if integrity/chain verification fails.
        """
        logger.info(f"Replaying logs starting at sequence {start_sequence_id}")
        state = initial_state or {
            "positions": {},
            "balances": {},
            "risk_state": {},
            "orders": {},
        }

        async with get_session() as session:
            async with session.begin():
                stmt = select(AuditLog).where(AuditLog.sequence_id >= start_sequence_id)
                if target_timestamp:
                    stmt = stmt.where(AuditLog.timestamp <= target_timestamp)
                stmt = stmt.order_by(AuditLog.sequence_id)

                result = await session.execute(stmt)
                rows: List[AuditLog] = list(result.scalars().all())

                if not rows:
                    logger.info("No audit logs found to replay.")
                    return state

                expected_seq = start_sequence_id
                previous_hash = ""

                # If starting from a middle sequence_id, fetch the previous row to seed previous_hash
                if start_sequence_id > 1:
                    prev_stmt = select(AuditLog.event_hash).where(AuditLog.sequence_id == start_sequence_id - 1)
                    prev_res = await session.execute(prev_stmt)
                    prev_hash = prev_res.scalars().first()
                    if prev_hash:
                        previous_hash = prev_hash

                for idx, row in enumerate(rows):
                    # 1. Monotonic sequence check
                    if row.sequence_id != expected_seq:
                        err_msg = f"Corruption detected: Missing sequence_id. Expected: {expected_seq}, Got: {row.sequence_id}"
                        logger.error(err_msg)
                        raise ValueError(err_msg)

                    # 2. Cryptographic signature check
                    calculated = calculate_hash(
                        sequence_id=row.sequence_id,
                        previous_hash=row.previous_hash,
                        event_type=row.event_type,
                        payload_str=row.payload,
                        timestamp=row.timestamp
                    )
                    if calculated != row.event_hash:
                        err_msg = (
                            f"Tampering detected: Event signature mismatch at sequence {row.sequence_id}. "
                            f"Calculated: {calculated}, DB stored: {row.event_hash}"
                        )
                        logger.error(err_msg)
                        raise ValueError(err_msg)

                    # 3. Chain linkage check
                    if idx > 0 or (start_sequence_id > 1 and previous_hash):
                        if row.previous_hash != previous_hash:
                            err_msg = (
                                f"Tampering detected: Chain link broken at sequence {row.sequence_id}. "
                                f"Expected previous: {previous_hash}, Got: {row.previous_hash}"
                            )
                            logger.error(err_msg)
                            raise ValueError(err_msg)

                    # Update references for next iteration
                    previous_hash = row.event_hash
                    expected_seq += 1

                    # 4. Deserialize row payload and construct Event
                    try:
                        payload_data = json.loads(row.payload)
                    except Exception:
                        payload_data = row.payload

                    event = Event(
                        event_type=row.event_type,
                        data=payload_data,
                        event_id=row.event_id,
                        timestamp=row.timestamp,
                        source=row.actor,
                        correlation_id=row.correlation_id,
                        causation_id=row.causation_id,
                        parent_request_id=row.parent_request_id,
                        event_version=row.event_version,
                    )

                    # 5. Apply event to state
                    state = self.apply_event(state, event)

                logger.info(f"Replay complete. Reconstructed sequence count: {len(rows)}")
                return state
        return state
