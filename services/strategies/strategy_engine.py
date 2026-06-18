"""
Strategy engine that manages all trading strategies, coordinates signal generation,
and routes valid signals to risk management and execution engines.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Set
from uuid import uuid4

from loguru import logger

from core.config.settings import settings
from core.container import run_safe_task
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.scheduler import task_scheduler
from infrastructure.exchange.base_exchange import OHLCV, BaseExchange
from services.market_data.timeframe_validator import timeframe_validator

from .base_strategy import BaseStrategy, Signal, SignalType


class StrategyEngine:
    """
    Central engine that manages all registered strategies.
    Handles market data updates, coordinates signal generation, and manages strategy lifecycle.
    """

    def __init__(self, event_bus: EventBus, exchange: BaseExchange):
        self.event_bus = event_bus
        self.exchange = exchange
        self._strategies: Dict[str, BaseStrategy] = {}
        self._running = False
        self._tasks: Set[asyncio.Task] = set()
        self._active_symbols: Set[str] = set()
        self._active_timeframes: Set[str] = set()
        self._candle_update_lock = asyncio.Lock()
        self._task_ids: List[str] = []
        self._analysis_snapshots: Dict[tuple, List[OHLCV]] = {}
        self._indicator_snapshots: Dict[tuple, Dict[str, Any]] = {}
        self.settings = settings

        # Panic Lifecycle Reset variables
        self._quarantine_until: float = 0.0
        self._in_panic_lockdown: bool = False

        logger.info("StrategyEngine initialized")

    async def initialize(self) -> None:
        """Initialize the strategy engine and register event handlers."""
        # Subscribe to indicators updates from Market Data Engine
        self.event_bus.subscribe(
            self._handle_indicators_updated,
            [EventTopic.MARKET_INDICATORS_UPDATED],
            handler_id="strategy_engine_indicators",
        )

        # Subscribe to Telegram control commands
        self.event_bus.subscribe(
            self._handle_control_start, [EventTopic.CONTROL_START_BOT], handler_id="strat_start"
        )
        self.event_bus.subscribe(
            self._handle_control_pause, [EventTopic.CONTROL_PAUSE_BOT], handler_id="strat_pause"
        )
        self.event_bus.subscribe(
            self._handle_control_emergency,
            [EventTopic.CONTROL_EMERGENCY_STOP],
            handler_id="strat_emergency",
        )
        self.event_bus.subscribe(
            self._handle_control_halt,
            [EventTopic.CONTROL_HALT_TRADING],
            handler_id="strat_halt",
        )
        self.event_bus.subscribe(
            self._handle_reset_signals,
            [EventTopic.CONTROL_RESET_SIGNALS],
            handler_id="strat_reset_signals",
        )

        # Subscribe to trading data requests
        self.event_bus.subscribe(
            self._handle_telegram_trading_request,
            [EventTopic.TELEGRAM_REQUEST_TRADING_DATA],
            handler_id="strat_tele_req",
        )

        logger.info("StrategyEngine initialization complete")

    async def register_strategy(self, strategy: BaseStrategy) -> str:
        """
        Register a new strategy with the engine.
        Returns the strategy ID.
        """
        strategy_id = str(uuid4())
        self._strategies[strategy_id] = strategy

        # Add symbols and timeframes to tracked sets
        self._active_symbols.update(strategy.config.symbols)
        self._active_timeframes.update(strategy.config.timeframes)

        logger.info(
            f"Registered strategy {strategy.config.name} with ID {strategy_id}. "
            f"Tracking symbols: {strategy.config.symbols}, timeframes: {strategy.config.timeframes}"
        )

        await self._schedule_strategy_tasks(strategy_id, strategy)
        return strategy_id

    def unregister_strategy(self, strategy_id: str) -> bool:
        """Unregister a strategy from the engine."""
        if strategy_id not in self._strategies:
            return False

        strategy = self._strategies.pop(strategy_id)
        logger.info(f"Unregistered strategy: {strategy.config.name} ({strategy_id})")
        return True

    async def _schedule_strategy_tasks(self, strategy_id: str, strategy: BaseStrategy) -> None:
        logger.debug(f"Event-driven mode: no polling tasks scheduled for {strategy.config.name}")

    async def _analyze_symbol_timeframe(
        self, symbol: str, timeframe: str, strategy: BaseStrategy
    ) -> None:
        """Analyze a symbol on a specific timeframe with the given strategy."""

        # [REFACTOR] Partial-readiness awareness
        from core.container import container
        mde = container.get("market_data_engine")

        if mde:
            if not mde.is_timeframe_ready(timeframe):
                logger.debug(f"Skipping analysis for {symbol} {timeframe} - MDE is not fully seeded yet (Partial Readiness).")
                return

            # --- Stream Health Strategy Awareness ---
            health = getattr(mde, "stream_health", {}).get(timeframe, "HEALTHY")
            if health == "DEGRADED":
                logger.warning(f"[STRATEGY BLOCK] Stream {timeframe} is DEGRADED (both WS and REST silent). Blocking signal generation for {symbol} to prevent stale entries.")
                # Increment forensic counter
                if hasattr(mde, "metrics"):
                    mde.metrics["degraded_tf_blocks"] = mde.metrics.get("degraded_tf_blocks", 0) + 1
                return
            elif health == "PARTIAL_HEALTHY":
                # REST is active, allow trading but log warning
                logger.info(f"[STRATEGY INFO] Stream {timeframe} is PARTIAL_HEALTHY (WS silent but REST active). Allowing signal generation for {symbol} using REST data.")
                if hasattr(mde, "metrics"):
                    mde.metrics["partial_health_trades"] = mde.metrics.get("partial_health_trades", 0) + 1

        logger.debug(f"Analyzing {symbol} {timeframe} with strategy {strategy.config.name}")

        # [PHASE 11] STRICT ENFORCEMENT: StrategyEngine must NOT bypass MarketDataEngine
        # Fetch latest candles exclusively from MDE
        current_candles = strategy.get_candles(symbol, timeframe, limit=250)
        if len(current_candles) < 30:  # Need at least 30 candles for meaningful analysis
            logger.debug(f"Skipping {symbol} {timeframe}: MDE only returned {len(current_candles)} candles (need 30). Waiting for MDE to hydrate.")
            return

        # Generate signal if possible
        if not self._running:
            return

        try:
            signal = await strategy.generate_signal(symbol, timeframe)
            if signal and signal.signal_type != SignalType.HOLD:
                await self._process_signal(signal)
        except Exception as e:
            logger.error(f"Error generating signal for {symbol} {timeframe}: {e}", exc_info=True)

    async def _process_signal(self, signal: Signal) -> None:
        """Process a generated signal by publishing it to the event bus."""
        logger.info(
            f"Generated {signal.signal_type.value} signal for {signal.symbol} from {signal.strategy_name}. "
            f"Entry: {signal.entry_price}, SL: {signal.stop_loss_price}"
        )

        # Publish signal event for risk engine to process
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.STRATEGY_SIGNAL_GENERATED,
                data=signal.__dict__,
                source="strategy_engine",
            )
        )

    async def _handle_indicators_updated(self, event: Event) -> None:
        """Forward indicators update to all relevant strategies."""
        # Layer 4 - Quarantine protection: Reject all signals during panic lockdown
        if self._in_panic_lockdown:
            logger.debug(f"[STRATEGY-FORENSIC] Event rejected due to panic lockdown")
            return

        data = event.data
        symbol = data.get("symbol")
        timeframe = data.get("timeframe")

        if not symbol or not timeframe:
            logger.debug(f"[STRATEGY-FORENSIC] Event rejected: missing symbol or timeframe")
            return

        candles_snapshot = data.get("candles_snapshot")
        indicators = data.get("indicators") or {}
        
        # DEBUG: Log event reception with microsecond timestamp
        import time
        event_timestamp_us = time.time()
        crossover_detected = indicators.get("crossover_detected", 0)
        signal_candle_ts = indicators.get("signal_candle_ts", 0)
        object_id = id(indicators)
        
        logger.debug(f"[RACE-PROOF] EVENT RECEIVED: ts_us={event_timestamp_us:.6f}, crossover={crossover_detected}, signal_candle_ts={signal_candle_ts}, object_id={object_id}")
        
        if symbol and timeframe:
            if candles_snapshot:
                self._analysis_snapshots[(symbol, timeframe)] = candles_snapshot
            if indicators:
                # [FIX CACHE RACE CONDITION] ONLY store with timestamp key to prevent overwrites
                signal_candle_ts = indicators.get("signal_candle_ts", 0)
                if signal_candle_ts > 0:
                    cache_write_timestamp_us = time.time()
                    cache_object_id = id(indicators)
                    self._indicator_snapshots[(symbol, timeframe, signal_candle_ts)] = indicators
                    logger.debug(f"[RACE-PROOF] CACHE WRITE (timestamp-keyed): ts_us={cache_write_timestamp_us:.6f}, key=({symbol},{timeframe},{signal_candle_ts}), crossover={crossover_detected}, object_id={cache_object_id}")
                # Also store regular key for backward compatibility, but this will be overwritten
                cache_write_timestamp_us = time.time()
                cache_object_id = id(indicators)
                self._indicator_snapshots[(symbol, timeframe)] = indicators
                logger.debug(f"[RACE-PROOF] CACHE WRITE (regular): ts_us={cache_write_timestamp_us:.6f}, key=({symbol},{timeframe}), crossover={crossover_detected}, object_id={cache_object_id}")

        # Forward event to all strategies that track this symbol+timeframe
        async with self._candle_update_lock:
            for strategy in self._strategies.values():
                if symbol in strategy.config.symbols and timeframe in strategy.config.timeframes:
                    logger.debug(f"[STRATEGY-FORENSIC] Calling _analyze_symbol_timeframe for {symbol}/{timeframe} with strategy {strategy.config.name}")
                    # [FIX DUPLICATE SIGNALS] Only trigger _analyze_symbol_timeframe() to avoid duplicate signal generation
                    # on_indicators_updated() was redundant and caused race conditions with signal deduplication
                    run_safe_task(self._analyze_symbol_timeframe(symbol, timeframe, strategy))

    async def start(self) -> None:
        """Start the strategy engine."""
        if self._running:
            logger.warning("StrategyEngine is already running")
            return

        self._running = True
        logger.info("StrategyEngine started successfully")

    async def stop(self) -> None:
        """Stop the strategy engine and cleanup tasks."""
        self._running = False

        for handler_id in (
            "strategy_engine_indicators",
            "strat_start",
            "strat_pause",
            "strat_emergency",
            "strat_halt",
            "strat_reset_signals",
            "strat_tele_req",
        ):
            self.event_bus.unsubscribe(handler_id=handler_id)

        # Cancel all analysis tasks
        for task_id in self._task_ids:
            task_scheduler.remove_task(task_id)

        # Wait for all running tasks to complete
        if self._tasks:
            logger.info(f"Waiting for {len(self._tasks)} strategy tasks to complete")
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("StrategyEngine stopped successfully")

    def get_strategies(self) -> Dict[str, BaseStrategy]:
        """Get all registered strategies."""
        return self._strategies.copy()

    def get_stats(self) -> Dict[str, Any]:
        """Get strategy engine statistics."""
        return {
            "registered_strategies": len(self._strategies),
            "tracked_symbols": len(self._active_symbols),
            "tracked_timeframes": len(self._active_timeframes),
            "is_running": self._running,
        }

    async def _handle_telegram_trading_request(self, event: Event) -> None:
        """Handle request for trading data (active_signals)."""
        action = event.data.get("action")
        if action != "active_signals":
            return

        query_id = event.data.get("query_id")

        # Aggregate active signals from all strategies
        active_signals = []  # Signal buffer removed; active signals now sourced from position engine

        data = {
            "action": action,
            "query_id": query_id,
            "message_id": event.data.get("message_id"),
            "signals": active_signals,
        }

        await self.event_bus.publish(
            Event(
                event_type=EventTopic.TELEGRAM_RESPONSE_TRADING_DATA,
                data=data,
                source="strategy_engine",
            )
        )

    async def reset_signal_buffers(self) -> None:
        """
        Reset tất cả signal buffers, cooldowns và candle data cũ.
        Chỉ ảnh hưởng đến tín hiệu giao dịch mới, KHÔNG ảnh hưởng các vị thế đang mở.
        Giải quyết vấn đề: tránh vào lệnh dựa trên tín hiệu cũ (EMA crossover đã xảy ra trước khi bot bật).
        """
        logger.warning("Initiating signal buffers reset...")

        reset_count = 0
        for strategy_id, strategy in self._strategies.items():
            # 1. Reset cooldown của strategy
            if hasattr(strategy, "_cooldowns") and strategy._cooldowns:
                strategy._cooldowns.clear()
                logger.debug(f"Reset cooldowns for strategy: {strategy_id}")

            # 2. Xóa toàn bộ candle data cũ của strategy
            if hasattr(strategy, "_candle_data") and strategy._candle_data:
                strategy._candle_data.clear()
                logger.debug(f"Reset candle data for strategy: {strategy_id}")

            # 3. Reset SignalSafetyMixin state if strategy uses it
            if hasattr(strategy, "_last_signal_time"):
                strategy._last_signal_time = {}
                logger.debug(f"Reset signal timestamps for strategy: {strategy_id}")
            if hasattr(strategy, "_last_processed"):
                strategy._last_processed = {}
                logger.debug(f"Reset processed signal cache for strategy: {strategy_id}")
            if hasattr(strategy, "_missed_signals"):
                strategy._missed_signals = []
                logger.debug(f"Reset missed signal history for strategy: {strategy_id}")

            # 4. Persist cleared signal state immediately if available
            if hasattr(strategy, "flush_state_immediate") and callable(strategy.flush_state_immediate):
                try:
                    await strategy.flush_state_immediate()
                    logger.debug(f"Flushed cleared signal state to disk for strategy: {strategy_id}")
                except Exception as e:
                    logger.error(f"Failed to flush cleared signal state for {strategy_id}: {e}")

            reset_count += 1

        # 3. Gửi event yêu cầu Market Data Engine tải lại dữ liệu mới
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.MARKET_RESET_BUFFERS,
                data={"timestamp": datetime.now(timezone.utc).isoformat()},
                source="strategy_engine",
            )
        )

        logger.info(
            f"✅ Signal buffers reset complete! Reset {reset_count} strategies. Bot sẽ chỉ phản ứng với tín hiệu MỚI."
        )

    async def _handle_reset_signals(self, event: Event) -> None:
        """Handle reset signals control event from Telegram."""
        payload = event.data if isinstance(event.data, dict) else {}
        await self.reset_signal_buffers()
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.CONTROL_RESET_SIGNALS_COMPLETE,
                data={"message_id": payload.get("message_id"), "success": True},
                source="strategy_engine",
                correlation_id=event.correlation_id,
            )
        )

    # ================ CONTROL HANDLERS ================
    async def _handle_control_start(self, event: Event) -> None:
        """Resume generating signals."""
        import time
        now = time.time()

        # Layer 5 - Safe Resume Protocol
        if self._in_panic_lockdown and now < self._quarantine_until:
            remaining = int(self._quarantine_until - now)
            logger.critical(f"[PANIC RESET] Cannot resume! Bot is in Quarantine Mode. Remaining time: {remaining}s")

            # Publish event so Telegram bot can notify the user
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.SYSTEM_ALERT,
                    data={"level": "CRITICAL", "message": f"🚫 Lệnh Bắt đầu Bot BỊ TỪ CHỐI!\nHệ thống đang trong chế độ CÁCH LY an toàn sau Panic Sell. Vui lòng thử lại sau {remaining} giây nữa để tránh vào lệnh rác."},
                    source="strategy_engine"
                )
            )
            return

        if self._in_panic_lockdown:
            logger.info("[PANIC RESET] Quarantine period expired. resume_rebuild_started")
            self._in_panic_lockdown = False

        self._running = True
        logger.warning("Strategy Engine resumed via UI. Bot is now active.")

    async def _handle_control_pause(self, event: Event) -> None:
        """Pause generating signals."""
        self._running = False
        logger.warning("Strategy Engine paused via UI. No new signals will be generated.")

    async def _handle_control_emergency(self, event: Event) -> None:
        """Handle emergency stop by pausing all new signal generation and entering lockdown."""
        import time
        self._running = False
        self._in_panic_lockdown = True
        self._quarantine_until = time.time() + 180  # 3 minutes quarantine

        logger.critical("[PANIC RESET] Strategy Engine EMERGENCY STOP. Entering PANIC_LOCKDOWN for 180s.")

        # Layer 2 & 3 - Transient Strategy State Purge & Market Context Invalidation
        await self.reset_signal_buffers()
        logger.warning("[PANIC RESET] signal_states_cleared=True strategy_context_invalidated=True quarantine_duration=180s")

    async def _handle_control_halt(self, event: Event) -> None:
        """Handle halt trading by pausing all new signal generation."""
        self._running = False
        logger.warning("Strategy Engine HALT TRADING. Halting new signal generation to preserve existing positions.")
