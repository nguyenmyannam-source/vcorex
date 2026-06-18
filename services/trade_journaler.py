"""
Automated Trade Journaler.
Listens to POSITION_CLOSED events and logs them to a CSV file.
"""

import csv
import os
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from loguru import logger

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic

if TYPE_CHECKING:
    from services.position.journal_context import TradeJournalContextStore


class TradeJournaler:
    """Logs closed positions to a CSV file."""

    def __init__(self, event_bus: EventBus, log_dir: str = "logs",
                 context_store: Optional["TradeJournalContextStore"] = None):
        self.event_bus = event_bus
        self.log_dir = log_dir
        self.log_file = os.path.join(self.log_dir, "trade_history.csv")
        self.context_store = context_store  # Injected by bootstrap after creation
        # Deduplication: store processed fill IDs to prevent duplicate journal entries
        self._processed_fills: set = set()
        self._ensure_log_file()
        self._subscribe_events()
        logger.info(f"TradeJournaler initialized. Logging to {self.log_file}")

    def _ensure_log_file(self):
        """Ensure the log directory and file exist with correct headers."""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        write_header = not os.path.exists(self.log_file)

        if write_header:
            with open(self.log_file, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "Close Time",
                        "Symbol",
                        "Side",
                        "Leverage",
                        "Entry Price",
                        "Exit Price",
                        "Size",
                        "PnL ($)",
                        "PnL (%)",
                        "Hold Duration",
                        "Reason",
                    ]
                )

    def _subscribe_events(self):
        self.event_bus.subscribe(
            self._handle_position_closed,
            [EventTopic.POSITION_CLOSED],
            handler_id="journaler_pos_closed",
        )
        self.event_bus.subscribe(
            self._handle_ws_raw_order,
            [EventTopic.WS_RAW_ORDER],
            handler_id="journaler_ws_raw_order",
        )

    async def stop(self) -> None:
        """Unsubscribe journal handlers on shutdown."""
        self.event_bus.unsubscribe(handler_id="journaler_pos_closed")
        self.event_bus.unsubscribe(handler_id="journaler_ws_raw_order")
        logger.info("TradeJournaler stopped")

    async def _handle_ws_raw_order(self, event: Event):
        """Handle raw order updates from OKX to journal exact fills."""
        try:
            data = event.data.get("data", {})
            if not data:
                return

            state = data.get("state", "")
            if state != "filled" and state != "partially_filled":
                return

            # pnl is only present/non-zero for closing fills
            pnl_str = data.get("pnl", "0")
            pnl = float(pnl_str) if pnl_str else 0.0

            # Only journal closing fills (reduceOnly or non-zero PnL)
            reduce_only = data.get("reduceOnly") == "true"
            is_closing_fill = pnl != 0.0 or reduce_only
            if not is_closing_fill:
                return

            # --- DEDUPLICATION ---
            # Use OKX fillId or a combo key to prevent double-journaling
            fill_id = data.get("fillId") or data.get("tradeId") or ""
            ord_id = data.get("ordId", "")
            dedup_key = f"{ord_id}:{fill_id}:{data.get('fillPx')}:{data.get('fillSz')}"
            if dedup_key in self._processed_fills:
                logger.debug(f"[JOURNAL] Duplicate fill dropped: {dedup_key}")
                return
            self._processed_fills.add(dedup_key)
            # LRU cap at 5000 fills
            if len(self._processed_fills) > 5000:
                oldest = list(self._processed_fills)[:500]
                for k in oldest:
                    self._processed_fills.discard(k)

            symbol = data.get("instId", "UNKNOWN")
            side = data.get("side", "").upper()
            exit_price = float(data.get("fillPx", 0.0))
            size = float(data.get("fillSz", 0.0))

            uTime = int(data.get("uTime", 0))
            close_time = datetime.fromtimestamp(uTime / 1000, timezone.utc) if uTime else datetime.now(timezone.utc)
            close_time_str = close_time.strftime("%Y-%m-%d %H:%M:%S")

            # --- CONTEXT STORE LOOKUP ---
            entry_price = 0.0
            leverage = 1
            duration = "N/A"
            pnl_pct = "N/A"

            if self.context_store:
                ctx = self.context_store.get_context(symbol)
                if ctx:
                    entry_price = ctx.avg_entry
                    opened_at_dt = datetime.fromtimestamp(ctx.opened_at, tz=timezone.utc)
                    delta = close_time - opened_at_dt
                    duration = str(delta).split(".")[0]
                    # pnl_pct = pnl / (entry_price * size / leverage)
                    if entry_price > 0 and size > 0:
                        cost_basis = entry_price * size
                        if cost_basis > 0:
                            pnl_pct = f"{(pnl / cost_basis * 100):.2f}"

            reason = "Market Fill (WS)"

            with open(self.log_file, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        close_time_str,
                        symbol,
                        side,
                        leverage,
                        f"{entry_price:.6f}",
                        f"{exit_price:.6f}",
                        f"{size:.4f}",
                        f"{pnl:.2f}",
                        pnl_pct,
                        duration,
                        reason,
                    ]
                )

            logger.info(
                f"[JOURNAL] Fill recorded: {symbol} {side} sz={size} exit={exit_price} "
                f"pnl={pnl:.2f} USDT duration={duration}"
            )

        except Exception as e:
            logger.error(f"Error handling WS raw order in journaler: {e}", exc_info=True)

    async def _handle_position_closed(self, event: Event):
        """Legacy handler. We keep it running but log as [LEGACY] to see duplicate/timing differences."""
        try:
            data = event.data

            symbol = data.get("symbol", "UNKNOWN")
            side = str(data.get("side", "")).upper()
            leverage = data.get("leverage", 1)
            entry_price = data.get("entry_price", 0.0)
            exit_price = data.get("current_price", 0.0)
            size = data.get("amount", 0.0)

            pnl = data.get("pnl", 0.0)
            pnl_pct = data.get("pnl_pct", 0.0)

            opened_at_raw = data.get("opened_at")
            closed_at_raw = data.get("closed_at")

            opened_at = opened_at_raw if opened_at_raw else datetime.now(timezone.utc)
            closed_at = closed_at_raw if closed_at_raw else datetime.now(timezone.utc)

            try:
                duration = str(closed_at - opened_at).split(".")[0]
            except Exception as e:
                duration = "UNKNOWN"

            close_time_str = (
                closed_at.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(closed_at, datetime)
                else str(closed_at)
            )

            updates = data.get("updates", [])
            reason = "[LEGACY] Market Close"
            if updates and len(updates) > 0:
                reason = f"[LEGACY] {updates[-1]}"

            with open(self.log_file, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        close_time_str,
                        symbol,
                        side,
                        leverage,
                        f"{entry_price:.6f}",
                        f"{exit_price:.6f}",
                        f"{size:.4f}",
                        f"{pnl:.2f}",
                        f"{pnl_pct:.2f}",
                        duration,
                        reason,
                    ]
                )

            logger.debug(f"Legacy Journaler: {symbol} closed ({pnl:.2f} USDT)")

        except Exception as e:
            logger.error(f"Error writing to trade journal: {e}", exc_info=True)
