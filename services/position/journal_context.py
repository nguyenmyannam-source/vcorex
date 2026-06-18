"""
Trade Journal Context Store — Lightweight lifecycle metadata store.

DESIGN PRINCIPLES:
- Stores ONLY: open timestamp + average entry price per instId
- NO PnL calculation, NO execution logic, NO risk logic
- Consumed ONLY by TradeJournaler to enrich WS fill events with context
- Transient: data lives in memory only; rebuilt from WS_RAW_POSITION on startup
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

from loguru import logger

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic


@dataclass
class _PositionContext:
    instId: str
    avg_entry: float
    opened_at: float  # Unix timestamp (time.time())


class TradeJournalContextStore:
    """
    Lightweight in-memory lifecycle store.

    Purpose:
        Provides TradeJournaler with the metadata it needs to compute
        accurate trade duration and pnl_pct for CSV records.

    Invariants:
        - Only stores opened_at + avg_entry per instId
        - State is rebuilt from WS events; never authoritative
        - Entries are created on first non-zero position push
        - Entries are cleared when position returns to size == 0
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._store: Dict[str, _PositionContext] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.event_bus.subscribe(
            self._handle_raw_position,
            [EventTopic.WS_RAW_POSITION],
            handler_id="jctx_pos",
        )
        logger.info("TradeJournalContextStore started")

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.event_bus.unsubscribe("jctx_pos")

    async def _handle_raw_position(self, event: Event) -> None:
        try:
            data: Dict[str, Any] = event.data.get("data", {})
            if not data:
                return

            instId: str = event.data.get("symbol") or data.get("instId", "")
            if not instId:
                return

            pos_str = str(data.get("pos", "0")).strip()
            try:
                pos_size = float(pos_str)
            except ValueError:
                return

            if pos_size == 0.0:
                # Position closed or zeroed out — clear context
                if instId in self._store:
                    logger.debug(f"[JCTX] Cleared context for {instId} (pos → 0)")
                    del self._store[instId]
            else:
                # Position open or updated
                if instId not in self._store:
                    # First time we see this position — record open time
                    avg_entry = float(data.get("avgPx", 0) or 0)
                    self._store[instId] = _PositionContext(
                        instId=instId,
                        avg_entry=avg_entry,
                        opened_at=time.time(),
                    )
                    logger.debug(
                        f"[JCTX] Context created for {instId} | entry={avg_entry}"
                    )
                else:
                    # Position update (partial fill etc.) — refresh avg entry only
                    ctx = self._store[instId]
                    new_avg = float(data.get("avgPx", ctx.avg_entry) or ctx.avg_entry)
                    if new_avg and new_avg != ctx.avg_entry:
                        self._store[instId] = _PositionContext(
                            instId=instId,
                            avg_entry=new_avg,
                            opened_at=ctx.opened_at,  # keep original open time
                        )

        except Exception as e:
            logger.error(f"[JCTX] Error handling raw position: {e}", exc_info=True)

    def get_context(self, instId: str) -> Optional[_PositionContext]:
        """Return lifecycle context for a position, or None if not tracked."""
        return self._store.get(instId)

    def get_opened_at_dt(self, instId: str) -> Optional[datetime]:
        ctx = self._store.get(instId)
        if ctx:
            return datetime.fromtimestamp(ctx.opened_at, tz=timezone.utc)
        return None

    def get_avg_entry(self, instId: str) -> float:
        ctx = self._store.get(instId)
        return ctx.avg_entry if ctx else 0.0

    def active_count(self) -> int:
        return len(self._store)
