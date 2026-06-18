"""Position Engine v2 - Refactored with SRP compliance.

Quản lý orchestration của vị thế. Chi tiết được delegated đến:
- OrderHandler: mở/đóng lệnh
- PositionMonitor: cập nhật PnL và tickers
- PositionPersistence: lưu/tải database
- PositionReconciler: kiểm tra nhất quán với sàn
"""

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config.settings import Settings
from core.container import run_safe_task
from core.circuit_breaker import BaseCircuitBreaker, CircuitState
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.task_watcher import TaskWatcher
from core.metrics import MetricsAdapter, InMemoryMetricsAdapter
from infrastructure.exchange.base_exchange import BaseExchange

from .position.models import TrackedPosition, PositionStatus
from .position.monitor import PositionMonitor
from .position.order_handler import OrderHandler
from .position.persistence import PositionPersistence
from .position.telegram_handler import PositionTelegramHandler

__all__ = ["PositionEngine", "TrackedPosition", "PositionStatus", "CircuitState"]


class PositionEngine:
    """Orchestrates position lifecycle: open → monitor → reconcile → close."""

    def __init__(
        self,
        exchange: BaseExchange,
        event_bus: EventBus,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        metrics: Optional[MetricsAdapter] = None,
    ):
        self.exchange = exchange
        self.event_bus = event_bus
        self.session_factory = session_factory
        self.settings = settings
        self._metrics = metrics or InMemoryMetricsAdapter()

        # Delegate to helper components
        self.persistence = PositionPersistence(session_factory)
        self.order_handler = OrderHandler(
            exchange, event_bus, self.persistence, settings.default_leverage
        )
        self.monitor = PositionMonitor(ticker_ttl=settings.ticker_ttl_seconds)
        self.trading_halted: bool = False

        # Shared state
        self._running = False
        self._start_time = time.time()
        self._reconciliation_task: Optional[asyncio.Task] = None
        self._pnl_update_task: Optional[asyncio.Task] = None
        self._pending_reconcile_task: Optional[asyncio.Task] = None
        self.watcher = TaskWatcher()

        # Layer 2 Execution Locks (prevent concurrent execution)
        self._position_execution_locks: dict[str, asyncio.Lock] = {}

        # Reconnect storm prevention (throttle reconciliation)
        self._last_reconciliation_time = 0.0
        self._reconciliation_cooldown = 30.0  # Chỉ cho phép reconcile tối đa 1 lần/30s

        # PHASE 2: Leverage sync guard - prevent concurrent sync calls
        self._leverage_sync_in_progress = False

        # Close-path circuit breaker (uses position_cb_* settings, shared BaseCircuitBreaker)
        self._close_cb = BaseCircuitBreaker(
            threshold=int(settings.position_cb_threshold),
            cooldown=float(settings.position_cb_cooldown),
            name="position_engine_close",
        )

        # Telegram integration
        self.telegram_handler = PositionTelegramHandler(self, event_bus, settings)

        logger.info("PositionEngine v2 initialized with OKX-First architecture and Layer 2 locking")


    async def start(self) -> None:
        """Start position engine."""
        if self._running:
            logger.warning("PositionEngine is already running")
            return

        # Load open positions from database
        open_positions = await self.persistence.load_open_positions()
        for pos in open_positions:
            self.order_handler._positions[pos.id] = pos
            if pos.exchange_id is not None:
                self.order_handler._exchange_id_map[pos.exchange_id] = pos.id

        self._running = True

        # [FIX P1] Force Leverage Sync with Exchange for all watchlist symbols
        self.watcher.watch(self._sync_all_leverages, "pe_leverage_sync", restart=False)

        # Start background tasks
        # (Reconciliation and cache refresh workers removed as part of Phase 5 Purge)

        # Subscribe to events
        self.event_bus.subscribe(
            self._handle_approved_signal,
            [EventTopic.RISK_SIGNAL_APPROVED],
            handler_id="pe_signal_handler",
        )
        self.event_bus.subscribe(
            self._handle_ws_ticker, [EventTopic.MARKET_WS_TICKER], handler_id="pe_ws_ticker"
        )
        self.event_bus.subscribe(
            self._handle_ws_position,
            [EventTopic.WS_RAW_POSITION],
            handler_id="pe_ws_position",
        )
        self.event_bus.subscribe(
            self._handle_emergency_stop,
            [EventTopic.CONTROL_EMERGENCY_STOP],
            handler_id="pe_emergency_stop",
        )
        self.event_bus.subscribe(
            self._handle_clean_bot,
            [EventTopic.CONTROL_CLEAN_BOT],
            handler_id="pe_clean_bot",
        )
        self.event_bus.subscribe(
            self._handle_control_halt,
            [EventTopic.CONTROL_HALT_TRADING, EventTopic.CONTROL_PAUSE_BOT],
            handler_id="pe_control_halt",
        )
        self.event_bus.subscribe(
            self._handle_control_start,
            [EventTopic.CONTROL_START_BOT],
            handler_id="pe_control_start",
        )
        self.event_bus.subscribe(
            self._handle_ghost_position,
            [EventTopic.POSITION_GHOST_DETECTED],
            handler_id="pe_ghost_position",
        )
        self.event_bus.subscribe(
            self._handle_ws_reconnected,
            [EventTopic.WS_RECONNECTED],
            handler_id="pe_ws_reconnect",
        )

        # Reconcile local state with exchange at startup
        await self.reconcile_positions_with_exchange()

        # Start PositionMonitor's background cleanup worker to prevent memory leaks
        await self.monitor.start()

        # Start periodic reconciliation task (every 1 giờ)
        self._reconciliation_task = self.watcher.watch(
            self._periodic_reconciliation_worker, "pe_periodic_reconciliation", restart=True
        )
        # Start a short-interval watcher to aggressively check PENDING_RECONCILE positions
        self._pending_reconcile_task = self.watcher.watch(
            self._pending_reconcile_worker, "pe_pending_reconcile", restart=True
        )
        logger.info("Periodic position reconciliation worker started")

        # Start OrderHandler fallback workers
        self.order_handler.start_workers()

        # Sync history with exchange
        await self.persistence.sync_history_with_exchange()

        logger.info(
            f"PositionEngine started. Tracking {len(self.order_handler._positions)} positions"
        )

    async def _periodic_reconciliation_worker(self) -> None:
        """Background worker that runs full position reconciliation every 1 giờ."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # 1 giờ
                logger.info("Starting periodic position reconciliation with OKX exchange...")
                await self.reconcile_positions_with_exchange()
                logger.info("Periodic position reconciliation completed successfully")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic reconciliation worker: {e}", exc_info=True)

    async def _pending_reconcile_worker(self) -> None:
        """Short-interval worker that triggers verification for PENDING_RECONCILE positions.

        This reduces the time a position may remain in PENDING_RECONCILE by scheduling
        frequent, bounded verification attempts without waiting for the full hourly sweep.
        """
        INTERVAL = 15  # seconds
        while self._running:
            try:
                await asyncio.sleep(INTERVAL)
                pending = [p for p in self.order_handler._positions.values() if p.status == PositionStatus.PENDING_RECONCILE]
                if not pending:
                    continue

                logger.info(f"[PENDING-WATCHER] Found {len(pending)} PENDING_RECONCILE positions; spawning verifiers.")
                for pos in pending:
                    try:
                        if getattr(self.settings, "ENABLE_PHANTOM_VERIFIER", True):
                            run_safe_task(self.order_handler._verify_phantom_position_worker(pos.id))
                        else:
                            logger.debug("[PENDING-WATCHER] Phantom verifier disabled by settings")
                    except Exception as e:
                        logger.error(f"[PENDING-WATCHER] Failed to schedule phantom verifier for {pos.id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in pending reconcile watcher: {e}", exc_info=True)

    async def stop(self) -> None:
        """Stop position engine gracefully."""
        if not self._running:
            return

        self._running = False
        self.event_bus.unsubscribe(handler_id="pe_signal_handler")
        self.event_bus.unsubscribe(handler_id="pe_ws_ticker")
        self.event_bus.unsubscribe(handler_id="pe_ws_position")
        self.event_bus.unsubscribe(handler_id="pe_emergency_stop")
        self.event_bus.unsubscribe(handler_id="pe_control_halt")
        self.event_bus.unsubscribe(handler_id="pe_control_start")
        self.event_bus.unsubscribe(handler_id="pe_clean_bot")
        self.event_bus.unsubscribe(handler_id="pe_ghost_position")
        self.event_bus.unsubscribe(handler_id="pe_ws_reconnect")
        self.order_handler.unsubscribe_halt_trading()
        self.telegram_handler.stop()

        # Stop PositionMonitor's cleanup worker
        await self.monitor.stop()

        # Stop reconciliation task
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconciliation_task
        if self._pending_reconcile_task:
            self._pending_reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending_reconcile_task

        # Stop OrderHandler fallback workers
        await self.order_handler.stop_workers()

        self.watcher.stop_all()
        logger.info("PositionEngine stopped")

    async def _investigate_and_report_manual_close(self, pos: TrackedPosition) -> None:
        """
        Investigate a position that was likely closed manually on the exchange.
        This involves fetching recent trades to find the closing transaction,
        calculating the P&L, and sending a detailed report.
        """
        logger.info(f"Investigating likely manual close for position {pos.id} ({pos.symbol}).")

        # 1. Define search window (from position open time to now)
        # Add a 5-second buffer before open time to catch immediate fills
        since_timestamp = int((pos.opened_at.timestamp() - 5) * 1000) if pos.opened_at else int((time.time() - 86400) * 1000)

        try:
            # 2. Fetch recent trades for the symbol
            trades = await self.exchange.fetch_recent_trades_for_symbol(
                symbol=pos.symbol,
                since=since_timestamp,
                limit=20  # Fetch more trades to be safe
            )

            # 3. Find the closing trade
            # A closing trade for a 'long' position is a 'sell', and for a 'short' is a 'buy'.
            # It must also occur after the position was opened.
            closing_side = 'sell' if pos.side == 'long' else 'buy'
            closing_trade = None

            for trade in sorted(trades, key=lambda t: t['timestamp']):
                trade_dt = datetime.fromtimestamp(trade['timestamp'] / 1000, tz=timezone.utc)
                if trade['side'] == closing_side and trade_dt > pos.opened_at:
                    # Simple check: amount should be close to position size
                    if abs(float(trade['amount']) - pos.amount) / pos.amount < 0.01:
                         closing_trade = trade
                         break # Found the most likely candidate

            if closing_trade:
                logger.info(f"Found likely closing trade for {pos.id}: Trade ID {closing_trade['id']}")

                # 4. Calculate P&L and update position
                pos.status = PositionStatus.CLOSED
                pos.closed_at = datetime.fromtimestamp(closing_trade['timestamp'] / 1000, tz=timezone.utc)
                pos.close_price = float(closing_trade['price'])

                # Calculate P&L based on trade fees and prices
                pnl_usd = (pos.close_price - pos.entry_price) * pos.amount * pos.ct_val
                if pos.side == 'short':
                    pnl_usd = -pnl_usd

                fee_cost = closing_trade.get('fee', {}).get('cost', 0)
                pnl_usd -= fee_cost

                pos.pnl = pnl_usd
                pos.pnl_percentage = (pnl_usd / (pos.entry_price * pos.amount * pos.ct_val)) * 100 * pos.leverage

                # 5. Persist and report
                await self.persistence.save_position(pos)
                self.order_handler._positions.pop(pos.id, None)

                await self.telegram_handler.send_manual_close_report(pos, closing_trade)
                logger.success(f"Successfully processed manual close for {pos.id}. P&L: ${pnl_usd:.2f}")

            else:
                # 6. If no closing trade found, use fallback
                logger.warning(f"Could not find a definitive closing trade for {pos.id}. Using fallback.")
                await self._publish_manual_close_fallback(pos)

        except Exception as e:
            logger.error(f"Error during manual close investigation for {pos.id}: {e}", exc_info=True)
            await self._publish_manual_close_fallback(pos)

    async def _publish_manual_close_fallback(self, pos: TrackedPosition) -> None:
        """
        Fallback handler for manually closed positions where the exact
        closing trade could not be determined.
        """
        pos.status = PositionStatus.CLOSED
        pos.closed_at = datetime.now(timezone.utc)
        pos.notes = "Closed manually on exchange. Closing details could not be retrieved."

        await self.persistence.save_position(pos)
        self.order_handler._positions.pop(pos.id, None)

        await self.telegram_handler.send_manual_close_fallback_report(pos)
        logger.warning(f"Position {pos.id} marked as manually closed (fallback).")

    async def _sync_all_leverages(self) -> None:
        """[FIX P1] Sync leverage on OKX for all watchlist symbols at boot.

        Without this, the bot calculates position sizes assuming
        `default_leverage` (e.g. 10x) but the exchange may still be
        set to 1x or 100x, causing either margin rejects or
        unintended risk exposure.
        
        PHASE 2: Added concurrency control, cooldown and lock to prevent API bursts.
        """
        # PHASE 2B: Prevent multiple concurrent sync calls
        if self._leverage_sync_in_progress:
            logger.warning("[LEVERAGE-SYNC] Leverage sync is already in progress, skipping duplicate call")
            return
            
        self._leverage_sync_in_progress = True
        try:
            # PHASE 2D: Acquire exchange's leverage sync lock to ensure only one sync system-wide
            async with self.exchange._leverage_sync_lock:
                leverage = self.settings.default_leverage
                symbols = self.settings.watchlist
                logger.info(
                    f"[LEVERAGE-SYNC] Syncing leverage={leverage}x for {len(symbols)} watchlist symbols..."
                )

                ok_count = 0
                fail_count = 0
                for symbol in symbols:
                    try:
                        success = await self.exchange.set_leverage(symbol, leverage)
                        if success:
                            ok_count += 1
                        else:
                            fail_count += 1
                            logger.warning(f"[LEVERAGE-SYNC] set_leverage returned False for {symbol}")
                    except Exception as e:
                        fail_count += 1
                        logger.error(f"[LEVERAGE-SYNC] Failed to set leverage for {symbol}: {e}")
                    # PHASE 2D: Add 0.5s cooldown between requests to stay under OKX limits
                    await asyncio.sleep(0.5)

                logger.info(
                    f"[LEVERAGE-SYNC] Done. success={ok_count}, failed={fail_count}"
                )
        finally:
            self._leverage_sync_in_progress = False

    async def open_position(self, signal_data: Dict[str, Any]) -> Optional[str]:
        """Delegate to OrderHandler, with exchange Circuit Breaker enforcement."""
        symbol = signal_data.get("symbol", "UNKNOWN")
        logger.info(f"[PE] open_position called for symbol={symbol}")
        
        # Check if exchange mirror is consistent before allowing new positions
        if hasattr(self, "exchange_mirror") and self.exchange_mirror is not None:
            if not self.exchange_mirror.is_consistent():
                logger.critical(
                    f"[MIRROR-BLOCK] Exchange Mirror is INCONSISTENT. "
                    f"Blocking open_position for {symbol} until resync succeeds."
                )
                return None
        
        # Block new positions if the exchange circuit breaker is open.
        # This prevents new risk accumulation during API outages.
        if hasattr(self.exchange, "_circuit_broken") and self.exchange._circuit_broken:
            logger.error(
                f"[CB-BLOCK] Exchange Circuit Breaker is OPEN. "
                f"Blocking open_position for {symbol} to prevent capital risk."
            )
            return None
        
        logger.info(f"[PE] Circuit breaker check passed for symbol={symbol}, delegating to order_handler")
        result = await self.order_handler.open_position(signal_data)
        logger.info(f"[PE] order_handler.open_position returned for symbol={symbol} result={result}")
        return result

    async def close_position(self, internal_id: str, close_amount: Optional[float] = None, correlation_id: Optional[str] = None) -> bool:
        """Delegate to OrderHandler."""
        return await self.order_handler.close_position(internal_id, close_amount, correlation_id=correlation_id)

    async def close_all_positions(self) -> int:
        """Emergency close all positions."""
        return await self.order_handler.close_all_positions()

    def get_active_positions(self) -> List[TrackedPosition]:
        """Get all active positions."""
        active_positions = self.order_handler.get_active_positions()
        if isinstance(active_positions, dict):
            return list(active_positions.values())
        return list(active_positions)

    def get_position(self, internal_id: str) -> Optional[TrackedPosition]:
        """Get position by ID."""
        return self.order_handler.get_position(internal_id)

    # === Backward compatibility properties for tests ===
    @property
    def _positions(self) -> Dict[str, TrackedPosition]:
        """Backward compatibility property for test access."""
        return self.order_handler._positions

    @property
    def _exchange_id_map(self) -> Dict[str, str]:
        """Backward compatibility property for test access."""
        return self.order_handler._exchange_id_map

    @property
    def _ticker_cache(self) -> Dict[str, Dict[str, Any]]:
        """Backward compatibility property for ticker cache access."""
        return self.monitor._ticker_cache

    # === Background workers ===

    # === Background workers removed in Phase 5 Purge ===

    # Backward compatibility aliases/helpers for testing
    async def _update_positions_pnl(self) -> None:
        """Backward compatibility helper for tests. Updates PnL for active positions from cache/WS."""
        positions = self.get_active_positions()
        for pos in positions:
            await self.monitor.update_position_pnl(pos)

    async def _update_position_pnl(self, tracked: TrackedPosition) -> None:
        """Backward compatibility method for test access with stale cache detection."""
        # Check if ticker cache is stale (>30 seconds old)
        ticker_data = self.monitor._ticker_cache.get(tracked.symbol)
        stale_threshold = float(getattr(self.settings, "ticker_ttl_seconds", 30))
        is_stale = True
        if ticker_data:
            age = time.time() - ticker_data.get("ts", 0)
            if age <= stale_threshold:
                is_stale = False

        if is_stale:
            # Fetch fresh price via REST
            try:
                fresh_ticker = await self.exchange.fetch_ticker(tracked.symbol)
                if fresh_ticker and hasattr(fresh_ticker, "last_price"):
                    tracked.current_price = fresh_ticker.last_price
                    logger.debug(
                        f"Fetched fresh REST price for {tracked.symbol}: {fresh_ticker.last_price}"
                    )
            except Exception as e:
                logger.warning(f"Failed to fetch REST ticker for {tracked.symbol}: {e}")

        await self.monitor.update_position_pnl(tracked)

    # === Event handlers ===

    async def _handle_approved_signal(self, event: Event) -> None:
        """Handle approved trading signal."""
        correlation_id = event.correlation_id or "UNKNOWN"
        logger.info(f"[PE] _handle_approved_signal called for signal_id={event.data.get('signal_id')} symbol={event.data.get('symbol')}")
        if self.trading_halted:
            logger.warning(
                f"[HALT-ACTIVE] Risk approved signal dropped because trading is currently HALTED! "
                f"symbol={event.data.get('symbol', 'UNKNOWN')}, correlation_id={correlation_id}"
            )
            return

        # [RACE-CONDITION-FIX] Acquire execution lock per symbol to prevent concurrent position opening
        symbol = event.data.get("symbol", "UNKNOWN")
        if symbol not in self._position_execution_locks:
            self._position_execution_locks[symbol] = asyncio.Lock()

        async with self._position_execution_locks[symbol]:
            # ALL active position checks INSIDE lock - eliminate TOCTOU completely
            active_for_symbol = [p for p in self.get_active_positions() if p.symbol == symbol]
            
            # Extract assessment reason to check for REVERSE
            assessment_data = event.data.get("assessment", {}) if event.data else {}
            reason = assessment_data.get("reason", "")
            is_reverse = reason == "REVERSE"

            if active_for_symbol:
                if is_reverse:
                    logger.info(f"[PE] [REVERSE] Reverse action triggered for {symbol}. Closing {len(active_for_symbol)} old position(s) first.")
                    for pos in active_for_symbol:
                        await self.close_position(pos.id, correlation_id=correlation_id)
                        
                        # Wait for position to be officially CLOSED by WS
                        max_wait_iterations = 100  # 10 seconds total wait
                        for i in range(max_wait_iterations):
                            if pos.status in (PositionStatus.CLOSED, PositionStatus.FAILED):
                                break
                            await asyncio.sleep(0.1)
                        
                        if pos.status != PositionStatus.CLOSED:
                            logger.error(f"[PE] [REVERSE] Failed to close old position {pos.id} within 10s. Aborting new signal.")
                            return
                    logger.info(f"[PE] [REVERSE] Old positions for {symbol} closed successfully. Proceeding with new order.")
                else:
                    logger.warning(
                        f"[RACE-CONDITION-GUARD] Signal rejected: active position already exists for {symbol} "
                        f"(detected after acquiring lock). signal_id={event.data.get('signal_id')}"
                    )
                    return

            # Inject correlation_id to propagate to OrderHandler and Exchange
            signal_data = event.data.copy() if event.data else {}
            signal_data["correlation_id"] = correlation_id
            logger.info(f"[PE] Calling open_position for symbol={symbol} signal_id={event.data.get('signal_id')}")
            try:
                result = await self.open_position(signal_data)
                logger.info(f"[PE] open_position returned for symbol={symbol} signal_id={event.data.get('signal_id')} result={result}")
            except Exception as e:
                logger.error(f"[PE] open_position failed for symbol={symbol} signal_id={event.data.get('signal_id')}: {e}", exc_info=True)

    async def _handle_ws_ticker(self, event: Event) -> None:
        """Handle WebSocket ticker update."""
        symbol = event.data.get("symbol")
        ticker_data = event.data.get("ticker_data", {})
        raw_price = event.data.get("price") or ticker_data.get("last") or ticker_data.get("last_price")
        ts = event.data.get("timestamp", time.time())

        if symbol and raw_price:
            try:
                price = float(raw_price)
                await self.monitor.handle_ticker_update(symbol, price, ts)
                # Update position prices
                await self.monitor.update_positions_from_tickers(
                    {pos.id: pos for pos in self.get_active_positions()}
                )
            except (ValueError, TypeError) as e:
                logger.debug(f"Invalid price value in ticker update for {symbol}: {raw_price} ({e})")

    async def _handle_emergency_stop(self, event: Event) -> None:
        """Handle emergency stop signal."""
        correlation_id = event.correlation_id or "UNKNOWN"
        logger.warning(f"[PANIC RESET] Emergency stop received. Halting trading and purging positions... correlation_id={correlation_id}")
        self.trading_halted = True

        # Layer 1 - Execution Cleanup
        success_count, fail_count = await self.order_handler.panic_close_all_positions(reason="EMERGENCY_STOP")

        logger.warning(
            f"[PANIC RESET] Layer 1 Cleanup completed. "
            f"positions_closed_count={success_count} orders_cancelled_count=All "
            f"failed_count={fail_count} correlation_id={correlation_id}"
        )

        if fail_count > 0:
            logger.critical("[PANIC RESET] FATAL: Failed to close some positions! System remains in PANIC_LOCKDOWN.")

        payload = event.data if isinstance(event.data, dict) else {}
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.CONTROL_EMERGENCY_STOP_COMPLETE,
                data={
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "message_id": payload.get("message_id"),
                },
                source="position_engine",
                correlation_id=correlation_id,
            )
        )

    async def _handle_clean_bot(self, event: Event) -> None:
        """Full system reset: DB, mirror, in-memory state. Blocked if open positions exist."""
        payload = event.data if isinstance(event.data, dict) else {}
        message_id = payload.get("message_id")
        correlation_id = event.correlation_id or "UNKNOWN"

        active_positions = self.order_handler.get_active_positions()
        if active_positions:
            symbols = ", ".join(p.symbol for p in active_positions)
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.CONTROL_CLEAN_BOT_COMPLETE,
                    data={
                        "message_id": message_id,
                        "success": False,
                        "blocked": True,
                        "position_count": len(active_positions),
                        "symbols": symbols,
                    },
                    source="position_engine",
                    correlation_id=correlation_id,
                )
            )
            return

        try:
            await self._execute_clean_bot_reset()
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.CONTROL_CLEAN_BOT_COMPLETE,
                    data={"message_id": message_id, "success": True},
                    source="position_engine",
                    correlation_id=correlation_id,
                )
            )
        except Exception as e:
            logger.error(f"[CLEAN BOT] Full reset failed: {e}", exc_info=True)
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.CONTROL_CLEAN_BOT_COMPLETE,
                    data={"message_id": message_id, "success": False, "error": str(e)},
                    source="position_engine",
                    correlation_id=correlation_id,
                )
            )

    async def _execute_clean_bot_reset(self) -> None:
        """Reset strategy buffers, risk history, RAM state, mirror cache, and database."""
        from core.container import container

        prior_halted = self.trading_halted
        prior_oh_halt = getattr(self.order_handler, "_halt_trading", False)
        was_running = self._running

        self.trading_halted = True
        self.order_handler._halt_trading = True
        self._running = False

        recon_tasks = []
        for task in (self._reconciliation_task, self._pending_reconcile_task):
            if task and not task.done():
                task.cancel()
                recon_tasks.append(task)
        if recon_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*recon_tasks, return_exceptions=True)

        await asyncio.sleep(0.05)

        try:
            strategy_engine = container.get("strategy_engine")
            if strategy_engine:
                await strategy_engine.reset_signal_buffers()

            if self.event_bus and hasattr(self.event_bus, "_retry_counts"):
                self.event_bus._retry_counts.clear()

            risk_manager = container.get("risk_manager")
            if risk_manager:
                risk_manager._historical_pnl.clear()
                risk_manager._position_history.clear()
                risk_manager._position_cache.clear()

            self.order_handler._positions.clear()
            self.order_handler._exchange_id_map.clear()

            exchange_mirror = container.get("exchange_mirror")
            if exchange_mirror:
                exchange_mirror._positions.clear()
                exchange_mirror._account.clear()
                exchange_mirror._processed_events.clear()

            from infrastructure.storage.database import Base
            async with self.session_factory() as session:
                await session.run_sync(lambda conn: Base.metadata.drop_all(bind=conn))
                await session.run_sync(lambda conn: Base.metadata.create_all(bind=conn))
            logger.warning("[CLEAN BOT] Database schema dropped and recreated successfully")
        finally:
            self._running = was_running
            self.trading_halted = prior_halted
            self.order_handler._halt_trading = prior_oh_halt
            if was_running:
                self._reconciliation_task = self.watcher.watch(
                    self._periodic_reconciliation_worker, "pe_periodic_reconciliation", restart=True
                )
                self._pending_reconcile_task = self.watcher.watch(
                    self._pending_reconcile_worker, "pe_pending_reconcile", restart=True
                )

    async def _handle_control_halt(self, event: Event) -> None:
        """Handle manual control halt trading signal."""
        correlation_id = event.correlation_id or "UNKNOWN"
        logger.warning(f"Control Halt/Pause Trading event received. Halting all new positions... correlation_id={correlation_id}")
        self.trading_halted = True

    async def _handle_control_start(self, event: Event) -> None:
        """Handle manual control start trading signal."""
        correlation_id = event.correlation_id or "UNKNOWN"
        logger.info(f"Control Start Trading event received. Resuming position entry... correlation_id={correlation_id}")
        self.trading_halted = False
        if hasattr(self.order_handler, "_halt_trading"):
            self.order_handler._halt_trading = False

    async def _handle_ws_reconnected(self, event: Event) -> None:
        """Triggered when WS reconnects to rapidly sync execution state.
        Chống reconnect storm: chỉ cho phép reconcile tối đa 1 lần/30s.
        """
        current_time = time.time()
        if current_time - self._last_reconciliation_time < self._reconciliation_cooldown:
            logger.warning(f"[RECONNECT-STORM-PREVENTION] Bỏ qua WS_RECONNECTED, cooldown chưa hết. Còn {int(self._reconciliation_cooldown - (current_time - self._last_reconciliation_time))}s.")
            return

        logger.info("[RECONCILE] WS_RECONNECTED received. Forcing immediate reconciliation of positions.")
        self._last_reconciliation_time = current_time
        await self.reconcile_positions_with_exchange()

    async def reconcile_positions_with_exchange(self) -> None:
        """Reconcile local database and memory positions with the exchange on startup."""
        logger.info("Starting startup position reconciliation with OKX...")
        try:
            live_positions = await self.exchange.fetch_positions()
            live_pos_map = {pos.symbol: pos for pos in live_positions}
            logger.info(f"OKX Exchange reports {len(live_positions)} active positions.")

            tracked_by_symbol = {}
            for pos in list(self.order_handler._positions.values()):
                tracked_by_symbol.setdefault(pos.symbol, []).append(pos)

            for symbol, tracked_list in tracked_by_symbol.items():
                live_pos = live_pos_map.get(symbol)

                if not live_pos:
                    for pos in tracked_list:
                        if pos.status in (PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE):
                            now = datetime.now(timezone.utc).timestamp()
                            opened_ts = pos.opened_at.timestamp() if pos.opened_at and hasattr(pos.opened_at, "timestamp") else now
                            if now - opened_ts > 30:
                                logger.warning(f"[RECONCILE] Pending position {pos.id} expired without exchange confirmation. Marking FAILED.")
                                pos.status = PositionStatus.FAILED
                                await self.persistence.save_position(pos)
                                self.order_handler._positions.pop(pos.id, None)
                            else:
                                logger.info(f"[RECONCILE] Pending position {pos.id} missing from exchange but still in 30s grace period.")
                        else:
                            logger.warning(
                                f"[RECONCILE] Position {pos.id} ({pos.symbol}) status is OPENED locally "
                                f"but missing on OKX. Spawning investigation task for manual close."
                            )
                            # Đây là trường hợp đóng thủ công. Thay vì chỉ đánh dấu đóng,
                            # chúng ta sẽ kích hoạt một tác vụ nền để điều tra.
                            run_safe_task(self._investigate_and_report_manual_close(pos))

                            pos.status = PositionStatus.CLOSED
                            pos.closed_at = datetime.now(timezone.utc)
                            pos.amount_remaining = 0.0
                            await self.persistence.save_position(pos)
                            self.order_handler._positions.pop(pos.id, None)
                            if pos.exchange_id in self.order_handler._exchange_id_map:
                                self.order_handler._exchange_id_map.pop(pos.exchange_id, None)
                else:
                    def get_open_time(p):
                        if p.opened_at is None:
                            return 0
                        if hasattr(p.opened_at, "timestamp"):
                            return p.opened_at.timestamp()
                        return float(p.opened_at)

                    tracked_list.sort(key=get_open_time)
                    active_pos = tracked_list[-1]
                    stale_pos_list = tracked_list[:-1]

                    for pos in stale_pos_list:
                        logger.warning(
                            f"[RECONCILE] Duplicate stale position {pos.id} ({pos.symbol}) found. "
                            f"Marking as CLOSED in DB."
                        )
                        pos.status = PositionStatus.CLOSED
                        pos.closed_at = datetime.now(timezone.utc)
                        pos.amount_remaining = 0.0
                        await self.persistence.save_position(pos)
                        self.order_handler._positions.pop(pos.id, None)
                        if pos.exchange_id in self.order_handler._exchange_id_map:
                            self.order_handler._exchange_id_map.pop(pos.exchange_id, None)

                    has_changes = False

                    # [REMEDIATION] Allow reconciliation to upgrade PARTIALLY_FILLED to OPENED
                    if active_pos.status in (PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE, PositionStatus.UNVERIFIED, PositionStatus.PARTIALLY_FILLED):
                        logger.info(f"[RECONCILE] Upgrading position {active_pos.id} from {active_pos.status} to OPENED because it exists on exchange.")
                        active_pos.status = PositionStatus.OPENED
                        active_pos.exchange_id = getattr(live_pos, "order_id", getattr(live_pos, "id", None))
                        has_changes = True
                    if active_pos.amount_remaining != live_pos.amount:
                        # [FIX] Reconciliation logic for position size mismatch
                        # Only sync if size decreased (partial close) or minimal difference
                        if live_pos.amount > active_pos.amount:
                            logger.warning(
                                f"[RECONCILE-ANOMALY] Position {symbol} size INCREASED: "
                                f"DB={active_pos.amount} but OKX={live_pos.amount}. "
                                f"This suggests unexpected entry or margin liquidation. Flagging for review."
                            )
                            # Don't blindly sync - this is an anomaly
                            # Keep DB value and alert
                            has_changes = False
                        else:
                            # Size decreased or minor variance - safe to sync
                            logger.info(
                                f"[RECONCILE] Updating position size for {symbol} "
                                f"from {active_pos.amount} to {live_pos.amount} (partial close detected)."
                            )
                            active_pos.amount_remaining = live_pos.amount
                            active_pos.amount = live_pos.amount
                            has_changes = True

                    if abs(active_pos.entry_price - live_pos.entry_price) > 1e-6:
                        logger.info(
                            f"[RECONCILE] Updating entry_price for {symbol} "
                            f"from {active_pos.entry_price} to {live_pos.entry_price}."
                        )
                        active_pos.entry_price = live_pos.entry_price
                        has_changes = True

                    if has_changes:
                        await self.persistence.save_position(active_pos)

            logger.info("Startup position reconciliation with OKX completed successfully.")
        except Exception as e:
            logger.error(f"Error during startup position reconciliation: {e}", exc_info=True)

    async def _handle_ws_position(self, event: Event) -> None:
        """Handle real-time position updates from WebSocket to sync local tracking."""
        try:
            data = event.data.get("data", {})
            symbol = event.data.get("symbol") or data.get("instId")
            if not symbol or not data:
                return

            pos_str = str(data.get("pos", "0")).strip()
            try:
                pos_size = abs(float(pos_str))
            except ValueError:
                return

            matching_positions = [
                pos for pos in self.order_handler._positions.values()
                if pos.symbol == symbol
            ]

            if pos_size == 0:
                for pos in matching_positions:
                    logger.info(
                        f"[WS-SYNC] Position {pos.id} ({pos.symbol}) closed on exchange. "
                        f"Updating local state."
                    )

                    if pos.algo_order_ids:
                        logger.info(f"[WS-SYNC] Canceling {len(pos.algo_order_ids)} attached algo orders for ghost position {pos.id}")
                        # Fire and forget cancel request
                        run_safe_task(self.exchange.cancel_algo_orders(pos.symbol, pos.algo_order_ids))

                    pos.status = PositionStatus.CLOSED
                    pos.closed_at = datetime.now(timezone.utc)
                    pos.amount_remaining = 0.0

                    await self.persistence.save_position(pos)

                    self.order_handler._positions.pop(pos.id, None)
                    if pos.exchange_id in self.order_handler._exchange_id_map:
                        self.order_handler._exchange_id_map.pop(pos.exchange_id, None)

                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.POSITION_CLOSED,
                            data=pos.__dict__,
                            source="position_engine_ws_sync",
                        )
                    )
            else:
                if len(matching_positions) == 1:
                    pos = matching_positions[0]
                    if pos.amount_remaining != pos_size:
                        logger.info(
                            f"[WS-SYNC] Updating position size for {pos.symbol} ({pos.id}) "
                            f"from {pos.amount_remaining} to {pos_size} to match exchange."
                        )
                        pos.amount_remaining = pos_size
                        if pos_size < pos.amount:
                            pos.status = PositionStatus.PARTIAL_TP

                        await self.persistence.save_position(pos)
                elif len(matching_positions) > 1:
                    def get_open_time(p):
                        if p.opened_at is None:
                            return 0
                        if hasattr(p.opened_at, "timestamp"):
                            return p.opened_at.timestamp()
                        return float(p.opened_at)

                    matching_positions.sort(key=get_open_time)
                    active_pos = matching_positions[-1]
                    stale_pos_list = matching_positions[:-1]

                    for pos in stale_pos_list:
                        logger.warning(
                            f"[WS-SYNC] Duplicate stale active position {pos.id} ({pos.symbol}) found. "
                            f"Marking as CLOSED locally to resolve duplicate in-memory state."
                        )
                        pos.status = PositionStatus.CLOSED
                        pos.closed_at = datetime.now(timezone.utc)
                        pos.amount_remaining = 0.0
                        await self.persistence.save_position(pos)
                        self.order_handler._positions.pop(pos.id, None)
                        if pos.exchange_id in self.order_handler._exchange_id_map:
                            self.order_handler._exchange_id_map.pop(pos.exchange_id, None)

                    if active_pos.amount_remaining != pos_size:
                        logger.info(
                            f"[WS-SYNC] Updating latest position size for {active_pos.symbol} ({active_pos.id}) "
                            f"from {active_pos.amount_remaining} to {pos_size} to match exchange."
                        )
                        active_pos.amount_remaining = pos_size
                        if pos_size < active_pos.amount:
                            active_pos.status = PositionStatus.PARTIAL_TP

                        await self.persistence.save_position(active_pos)
                elif len(matching_positions) == 0:
                    # Route through unified ghost recovery handler (single RAM/DB writer)
                    logger.warning(
                        f"[WS-SYNC] Ghost detected via WebSocket: exchange has {symbol} "
                        f"but bot has no local position. Publishing POSITION_GHOST_DETECTED."
                    )
                    side_str = data.get("posSide", data.get("side", "long")).lower()
                    if side_str == "net":
                        side_str = "long" if float(data.get("pos", 0)) > 0 else "short"

                    tp_val = data.get("tpTriggerPx", "")
                    sl_val = data.get("slTriggerPx", "")

                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.POSITION_GHOST_DETECTED,
                            data={
                                "symbol": symbol,
                                "position_id": f"pos_ghost_ws_{uuid4()}",
                                "reason": "auto_recovery",
                                "strategy_name": "GHOST_SYNC_INSTANT",
                                "side": side_str,
                                "amount": pos_size,
                                "entry_price": float(data.get("avgPx", 0.0) or 0.0),
                                "current_price": float(
                                    data.get("last", data.get("markPx", 0.0)) or 0.0
                                ),
                                "leverage": int(data.get("lever", 1)),
                                "exchange_id": data.get("posId", data.get("exchange_id")),
                                "ct_val": float(data.get("ctVal", 1.0) or 1.0),
                                "tp_trigger_px": float(tp_val) if tp_val else None,
                                "sl_trigger_px": float(sl_val) if sl_val else None,
                            },
                            source="position_engine_ws_sync",
                            correlation_id=event.correlation_id,
                        )
                    )
        except Exception as e:
            logger.error(f"Error handling WS raw position sync: {e}", exc_info=True)

    @property
    def _cb_state(self) -> CircuitState:
        return self._close_cb.state

    @_cb_state.setter
    def _cb_state(self, value: CircuitState) -> None:
        self._close_cb.state = value
        self._close_cb.last_state_change = time.time()

    @property
    def _cb_failure_count(self) -> int:
        return self._close_cb.failure_count

    @_cb_failure_count.setter
    def _cb_failure_count(self, value: int) -> None:
        self._close_cb.failure_count = value

    @property
    def _cb_cooldown(self) -> float:
        return self._close_cb.cooldown

    @_cb_cooldown.setter
    def _cb_cooldown(self, value: float) -> None:
        self._close_cb.cooldown = value

    @property
    def _cb_threshold(self) -> int:
        return self._close_cb.threshold

    @_cb_threshold.setter
    def _cb_threshold(self, value: int) -> None:
        self._close_cb.threshold = value

    def _cb_record_success(self) -> None:
        """Record success to reset circuit breaker."""
        prior = self._cb_state
        self._close_cb.record_success()
        if prior != CircuitState.CLOSED:
            logger.info(f"Circuit Breaker state transitioned from {prior} to CLOSED")

    def _cb_record_failure(self) -> None:
        """Record failure and potentially trip circuit breaker."""
        if self._cb_state == CircuitState.HALF_OPEN:
            logger.critical("Circuit Breaker transitioned back to OPEN after failure in HALF_OPEN state")
            self._close_cb._open_circuit()
            asyncio.create_task(self._metrics.increment_cb_open())
            return

        was_closed = self._close_cb.state == CircuitState.CLOSED
        self._close_cb.record_failure()
        if was_closed and self._close_cb.state == CircuitState.OPEN:
            logger.critical(
                f"Circuit Breaker TRIPPED to OPEN due to {self._close_cb.failure_count} consecutive failures"
            )
            asyncio.create_task(self._metrics.increment_cb_open())

    def _cb_can_execute(self) -> bool:
        """Check if circuit breaker allows execution, handling HALF_OPEN transitions."""
        if self._cb_state == CircuitState.HALF_OPEN:
            return True
        allowed = self._close_cb.allow_request()
        if allowed and self._cb_state == CircuitState.HALF_OPEN:
            logger.info("Circuit Breaker transitioned to HALF_OPEN (cooldown expired)")
        return allowed

    def _release_close_execution_lock(self, position_id: str) -> None:
        """Remove per-position close lock when position is gone or terminal."""
        pos = self.order_handler._positions.get(position_id)
        if pos is None or pos.status in (
            PositionStatus.CLOSED,
            PositionStatus.FAILED,
            PositionStatus.LIQUIDATED,
        ):
            self._position_execution_locks.pop(position_id, None)

    async def close_position_secure(self, request) -> None:
        """Securely close a position by verifying real-time exchange state."""
        position_id = request.position_id

        if position_id not in self._position_execution_locks:
            self._position_execution_locks[position_id] = asyncio.Lock()

        lock = self._position_execution_locks[position_id]

        if lock.locked():
            logger.warning(f"L2 Lock active for position {position_id}. Contention detected.")
            asyncio.create_task(self._metrics.increment_lock_contention())

        if not self._cb_can_execute():
            reason = "Circuit breaker is OPEN due to repeated errors"
            logger.error(f"Request {request.request_id} rejected: {reason}")
            await self._publish_close_failure(request, reason)
            return

        async with lock:
            try:
                local_pos = None
                exchange_pos = None
                local_pos = self.get_position(position_id)
                if not local_pos:
                    reason = f"Position {position_id} not found locally"
                    logger.warning(reason)
                    await self._publish_close_failure(request, reason)
                    return

                if local_pos.symbol not in self.settings.watchlist:
                    reason = f"Position symbol {local_pos.symbol} is not in the authorized watchlist"
                    logger.error(reason)
                    await self._publish_close_failure(request, reason)
                    return

                if local_pos.status not in (PositionStatus.OPENED, PositionStatus.PARTIAL_TP):
                    logger.info(f"Position {position_id} is already closed (status={local_pos.status}). Returning safely.")
                    await self._publish_close_success(
                        request,
                        symbol=local_pos.symbol,
                        side=local_pos.side,
                        size=0.0,
                        order_id="N/A",
                        already_closed=True,
                    )
                    return

                try:
                    exchange_pos = await asyncio.wait_for(
                        self.exchange.fetch_position(local_pos.symbol),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    self._cb_record_failure()
                    asyncio.create_task(self._metrics.increment_exchange_timeout())
                    reason = "Exchange timeout while fetching position"
                    logger.error(reason)
                    await self._publish_close_failure(request, reason)
                    return
                except Exception as e:
                    self._cb_record_failure()
                    reason = f"Failed to fetch exchange state: {e}"
                    logger.error(reason)
                    await self._publish_close_failure(request, reason)
                    return

                if not exchange_pos or exchange_pos.amount == 0:
                    logger.info(f"Position {local_pos.symbol} already closed on exchange. Updating local state.")
                    local_pos.status = PositionStatus.CLOSED
                    local_pos.amount_remaining = 0.0
                    local_pos.closed_at = datetime.now(timezone.utc)
                    await self.persistence.save_position(local_pos)
                    self.order_handler._positions.pop(position_id, None)
                    # LOCK CLEANUP: Xóa symbol lock nếu không còn vị thế mở/pending nào cho symbol này
                    symbol = local_pos.symbol
                    remaining_positions = [
                        p for p in self.order_handler._positions.values()
                        if p.symbol == symbol and p.status in [PositionStatus.OPENED, PositionStatus.PENDING]
                    ]
                    if not remaining_positions and symbol in self.order_handler._symbol_locks:
                        del self.order_handler._symbol_locks[symbol]
                        logger.debug(f"[LOCK CLEANUP] Removed unused symbol lock for {symbol}, no active positions left")

                    await self._publish_close_success(
                        request,
                        symbol=local_pos.symbol,
                        side=local_pos.side,
                        size=0.0,
                        order_id="N/A",
                        already_closed=True
                    )
                    return

                from core.events.payloads import PositionAction
                if request.action == PositionAction.CLOSE_HALF:
                    raw_half_size = exchange_pos.amount / 2.0
                    close_amount = self.exchange.normalize_position_size(local_pos.symbol, raw_half_size)
                    if close_amount <= 0.0:
                        reason = f"Normalized half size {raw_half_size} is less than minimum lot size"
                        logger.warning(reason)
                        await self._publish_close_failure(request, reason)
                        return
                else:
                    close_amount = exchange_pos.amount

                try:
                    logger.info(f"Executing secure close for position {position_id}: action={request.action}, size={close_amount}")
                    success = await asyncio.wait_for(
                        self.close_position(position_id, close_amount=close_amount, correlation_id=request.correlation_id),
                        timeout=15.0
                    )
                    if success:
                        self._cb_record_success()
                        await self._publish_close_success(
                            request,
                            symbol=local_pos.symbol,
                            side=local_pos.side,
                            size=close_amount,
                            order_id="N/A",
                        )
                    else:
                        self._cb_record_failure()
                        reason = "OrderHandler failed to execute close order"
                        logger.error(reason)
                        await self._publish_close_failure(request, reason)
                except asyncio.TimeoutError:
                    self._cb_record_failure()
                    asyncio.create_task(self._metrics.increment_exchange_timeout())
                    reason = "Exchange timeout during close order execution"
                    logger.error(reason)
                    await self._publish_close_failure(request, reason)
                except Exception as e:
                    self._cb_record_failure()
                    reason = f"Exception during order placement: {e}"
                    logger.error(reason, exc_info=True)
                    await self._publish_close_failure(request, reason)
            finally:
                self._release_close_execution_lock(position_id)

    async def _publish_close_success(self, request, symbol: str, side: str, size: float, order_id: str, already_closed: bool = False) -> None:
        """Publish success event with tracing fields and version."""
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.POSITION_CLOSE_SUCCESS,
                data={
                    "success": True,
                    "correlation_id": request.correlation_id,
                    "causation_id": request.request_id,
                    "parent_request_id": request.parent_request_id,
                    "position_id": request.position_id,
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "order_id": order_id,
                    "already_closed": already_closed
                },
                source="position_engine",
                correlation_id=request.correlation_id,
                causation_id=request.request_id,
                parent_request_id=request.parent_request_id,
                event_version="1.0"
            )
        )

    async def _publish_close_failure(self, request, reason: str) -> None:
        """Publish failure event with tracing fields and version."""
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.POSITION_CLOSE_FAILURE,
                data={
                    "success": False,
                    "correlation_id": request.correlation_id,
                    "causation_id": request.request_id,
                    "parent_request_id": request.parent_request_id,
                    "position_id": request.position_id,
                    "reason": reason
                },
                source="position_engine",
                correlation_id=request.correlation_id,
                causation_id=request.request_id,
                parent_request_id=request.parent_request_id,
                event_version="1.0"
            )
        )

    async def _handle_ghost_position(self, event: Event) -> None:
        """Handle position discrepancy updates (ghost positions / missing fills) safely under lock."""
        from services.position.models import TrackedPosition, PositionStatus

        data = event.data
        if not isinstance(data, dict):
            return

        symbol = data.get("symbol")
        reason = data.get("reason")
        if not symbol or not reason:
            return

        correlation_id = event.correlation_id or "N/A"
        logger.info(f"[RECONCILE-LOCK] Handling ghost position event for {symbol} ({reason}) with correlation_id={correlation_id}")

        # Safely get or create lock for symbol from order_handler
        if symbol not in self.order_handler._symbol_locks:
            self.order_handler._symbol_locks[symbol] = asyncio.Lock()

        # LOCK TIMEOUT: Bọc trong asyncio.timeout 5s để tránh deadlock
        lock = self.order_handler._symbol_locks[symbol]
        try:
            async with asyncio.timeout(5.0):
                async with lock:
                    logger.debug(f"[LOCK ACQUIRED] Acquired symbol lock for {symbol} in ghost position handling")

                    if reason == "auto_recovery":
                        # Ghost position (remote exists, local doesn't)
                        # Check if it is already tracked (prevent duplicate creation)
                        matching = [p for p in self.order_handler._positions.values() if p.symbol == symbol and p.status in [PositionStatus.OPENED, PositionStatus.PENDING]]
                        if matching:
                            logger.debug(f"[RECONCILE-LOCK] Ghost position for {symbol} already tracked locally. Skipping.")
                            return

                        # Safe auto-recovery: insert local position

                        from services.position.models import TakeProfitLevel
                        tp_px = data.get("tp_trigger_px")
                        take_profits = [TakeProfitLevel(price=float(tp_px), exit_pct=1.0)] if tp_px else []

                        internal_id = data.get("position_id") or str(uuid4())
                        tracked = TrackedPosition(
                            id=internal_id,
                            exchange_id=data.get("exchange_id") or f"recovered_{str(uuid4())[:8]}",
                            symbol=symbol,
                            side=data.get("side", "long"),
                            entry_price=float(data.get("entry_price", 0.0)),
                            current_price=float(data.get("current_price", data.get("entry_price", 0.0))),
                            amount=float(data.get("amount", 0.0)),
                            amount_remaining=float(data.get("amount", 0.0)),
                            leverage=int(data.get("leverage", 1)),
                            ct_val=float(data.get("ct_val", 1.0)),
                            stop_loss=data.get("sl_trigger_px") or data.get("stop_loss"),
                            take_profit_levels=take_profits,
                            status=PositionStatus.OPENED,
                            strategy_name=data.get("strategy_name", "recovered"),
                        )
                        tracked.add_update("Auto-recovered ghost position from exchange authoritative source.")

                        # Save to memory and DB
                        self.order_handler._positions[internal_id] = tracked
                        if tracked.exchange_id:
                            self.order_handler._exchange_id_map[tracked.exchange_id] = internal_id

                        await self.persistence.save_position(tracked)
                        logger.warning(f"[RECONCILE-HEAL] Ghost position for {symbol} auto-recovered successfully under lock.")

                    elif reason == "auto_heal_close":
                        # Missing fill (local has open, remote is flat/none)
                        # Mark as CLOSED locally
                        matching_positions = [
                            pos for pos in self.order_handler._positions.values()
                            if pos.symbol == symbol and pos.status in [PositionStatus.OPENED, PositionStatus.PENDING, PositionStatus.PARTIAL_TP, PositionStatus.UNVERIFIED]
                        ]

                        for pos in matching_positions:
                            logger.warning(
                                f"[RECONCILE-HEAL] Missing fill detected for {symbol} ({pos.id}) under lock. "
                                f"Closing local position to match exchange authoritative state."
                            )
                            pos.status = PositionStatus.CLOSED
                            pos.closed_at = datetime.now(timezone.utc)
                            await self.persistence.save_position(pos)
                            await self.order_handler._evict_terminal_position(pos.id, pos)

        except (asyncio.TimeoutError, TimeoutError):
            logger.error(f"[LOCK TIMEOUT] Failed to acquire symbol lock for {symbol} in ghost position handling within 5s, skipping recovery")
            asyncio.create_task(self._metrics.increment_lock_timeout()) if hasattr(self, '_metrics') else None
            return
        except Exception as e:
            logger.error(f"[GHOST POSITION ERROR] Failed to process ghost position for {symbol}: {e}", exc_info=True)
            return