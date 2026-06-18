"""
Bootstrapping module for VCOREX application.

Provides a DI-friendly `VCoreXTradingBot` class extracted from `main.py` to
keep composition separate from runtime entrypoint. Factories/classes can be
injected to enable easier unit testing and replacement.
"""

from __future__ import annotations

import asyncio
import gc
import time
import psutil
import os
from typing import Any, Callable, Optional

from loguru import logger

from core.config.logging import setup_logging
from core.config.settings import Settings, settings
from core.container import container, run_safe_task
from core.event_bus import Event, EventBus, RedisStreamsEventBus
from core.events.topics import EventTopic
from core.scheduler import task_scheduler
from core.task_watcher import TaskWatcher
from services.market_data.timeframe_validator import timeframe_validator
from domain.risk.risk_manager import RiskManager
from infrastructure import storage
from infrastructure.exchange.okx_exchange import OKXExchange
from infrastructure.storage.repository import UnitOfWork
from interfaces.telegram.notification_service import NotificationService
from services.visualization.chart_service import ChartService
from interfaces.telegram.telegram_bot import TelegramBot
from services.analytics.report_service import ReportService
from services.market_data_engine import MarketDataEngine
from services.news_engine import NewsEngine
from services.position_engine import PositionEngine
from services.strategies.ema_crossover import EMACrossoverStrategy, StrategyConfig
from services.strategies.strategy_engine import StrategyEngine
from services.trade_journaler import TradeJournaler
from services.position.exchange_mirror import ExchangeMirrorCache
from services.position.shadow_validator import ShadowValidator
from services.position.journal_context import TradeJournalContextStore
from services.database_maintenance import DatabaseMaintenanceService
from services.reconciliation_service import ReconciliationService
from services.maintenance_scheduler import maintenance_scheduler


class VCoreXTradingBot:
    """Main bot class (DI-friendly).

    Accepts optional factory callables or classes for major components so
    we avoid hard-coded instantiation inside the class body.
    """

    def __init__(
        self,
        settings_obj: Optional[Settings] = None,
        container_obj: Optional[Any] = None,
        exchange_factory: Optional[Callable[[Settings], OKXExchange]] = None,
        uow_factory: Optional[Callable[[Any], UnitOfWork]] = None,
        market_data_engine_cls: Callable[..., MarketDataEngine] = MarketDataEngine,
        position_engine_cls: Callable[..., PositionEngine] = PositionEngine,
        strategy_engine_cls: Callable[..., StrategyEngine] = StrategyEngine,
        risk_manager_cls: Callable[..., RiskManager] = RiskManager,
        telegram_bot_cls: Callable[..., TelegramBot] = TelegramBot,
        notification_service_cls: Callable[..., NotificationService] = NotificationService,
        news_engine_cls: Callable[..., NewsEngine] = NewsEngine,
        report_service_cls: Callable[..., ReportService] = ReportService,
        trade_journaler_cls: Callable[..., TradeJournaler] = TradeJournaler,
    ) -> None:
        self.settings: Settings = settings_obj or settings
        self.watcher = TaskWatcher()
        # Demo mode: Use in-memory EventBus if Redis not available (Windows friendly)
        # Thêm cơ chế thử kết nối Redis tự động sau 30s nếu lần đầu thất bại
        self.event_bus = None
        try:
            self.event_bus = RedisStreamsEventBus(redis_url=self.settings.redis_url)
            logger.info("Using RedisStreamsEventBus for production")
        except Exception as e:
            from core.event_bus import InProcessEventBus
            self.event_bus = InProcessEventBus()
            logger.warning(f"Redis not available initially, falling back to in-memory EventBus: {e}")
            
            # Tạo background task thử kết nối lại Redis sau 30s
            async def _retry_redis_connection():
                await asyncio.sleep(30)
                try:
                    logger.info("Retrying Redis connection...")
                    new_redis_bus = RedisStreamsEventBus(redis_url=self.settings.redis_url)
                    await new_redis_bus.start()
                    await self._promote_event_bus(new_redis_bus)
                    logger.info("Successfully reconnected to Redis, switched to RedisStreamsEventBus")
                except Exception as retry_e:
                    logger.warning(f"Redis retry failed, keep using in-memory EventBus: {retry_e}")
            
            # Đăng ký Redis retry task vào TaskWatcher để được giám sát
            self.watcher.watch(
                _retry_redis_connection,
                "redis_connection_retry",
                restart=False
            )
        self.container = container_obj or container
        self.exchange_factory = exchange_factory
        self.uow_factory = uow_factory
        self.market_data_engine_cls = market_data_engine_cls
        self.position_engine_cls = position_engine_cls
        self.strategy_engine_cls = strategy_engine_cls
        self.risk_manager_cls = risk_manager_cls
        self.telegram_bot_cls = telegram_bot_cls
        self.notification_service_cls = notification_service_cls
        self.news_engine_cls = news_engine_cls
        self.report_service_cls = report_service_cls
        self.trade_journaler_cls = trade_journaler_cls

        # Components (will be set during initialize)
        self.exchange: Optional[OKXExchange] = None
        self.strategy_engine: Optional[StrategyEngine] = None
        self.risk_manager: Optional[RiskManager] = None
        self.market_data_engine: Optional[MarketDataEngine] = None
        self.position_engine: Optional[PositionEngine] = None
        self.telegram_bot: Optional[TelegramBot] = None
        self.notification_service: Optional[NotificationService] = None
        self.chart_service: Optional[ChartService] = None
        self.news_engine: Optional[NewsEngine] = None
        self.report_service: Optional[ReportService] = None
        self.trade_journaler: Optional[TradeJournaler] = None
        self.uow: Optional[UnitOfWork] = None
        self.exchange_mirror: Optional[ExchangeMirrorCache] = None
        self.shadow_validator: Optional[ShadowValidator] = None
        self.journal_context_store: Optional[TradeJournalContextStore] = None
        self.database_maintenance: Optional[DatabaseMaintenanceService] = None
        self.reconciliation_service: Optional[ReconciliationService] = None
        self._shutdown_event = asyncio.Event()
        self._registered = False
        self._shutdown_complete = False

    def _rewire_event_bus(self, new_bus: EventBus) -> None:
        """Point all initialized components at the promoted event bus."""
        self.event_bus = new_bus
        self.container.register_instance("event_bus", new_bus)

        bus_targets = [
            self.exchange,
            self.market_data_engine,
            self.position_engine,
            self.exchange_mirror,
            self.journal_context_store,
            self.reconciliation_service,
            self.strategy_engine,
            self.risk_manager,
            self.telegram_bot,
            self.notification_service,
            self.news_engine,
            self.report_service,
            self.trade_journaler,
        ]
        for obj in bus_targets:
            if obj is not None and hasattr(obj, "event_bus"):
                obj.event_bus = new_bus

        if self.position_engine is not None:
            oh = getattr(self.position_engine, "order_handler", None)
            if oh is not None:
                oh.event_bus = new_bus
            th = getattr(self.position_engine, "telegram_handler", None)
            if th is not None:
                th.event_bus = new_bus

    async def _promote_event_bus(self, new_bus: EventBus) -> None:
        """Migrate handlers from the current bus and re-wire all components."""
        old_bus = self.event_bus
        # Prefer a public snapshot API if available; fallback to private attribute
        handlers = []
        if hasattr(old_bus, "get_handlers_snapshot"):
            try:
                handlers = old_bus.get_handlers_snapshot()
            except Exception:
                handlers = []
        elif hasattr(old_bus, "_local_handlers"):
            handlers = list(getattr(old_bus, "_local_handlers", []))
        if hasattr(old_bus, "fallback") and hasattr(old_bus.fallback, "get_handlers_snapshot"):
            try:
                fb_handlers = old_bus.fallback.get_handlers_snapshot()
                seen = {h.handler_id for h in handlers}
                for handler in fb_handlers:
                    if handler.handler_id not in seen:
                        handlers.append(handler)
            except Exception:
                pass

        for handler in handlers:
            try:
                new_bus.subscribe(
                    handler.callback,
                    list(handler.event_types),
                    handler.filter_func,
                    handler.handler_id,
                )
            except Exception as e:
                logger.debug(f"Failed to migrate handler {getattr(handler, 'handler_id', 'unknown')}: {e}")
        self._rewire_event_bus(new_bus)
        if old_bus is not new_bus and hasattr(old_bus, "stop"):
            try:
                await old_bus.stop()
            except Exception as e:
                logger.warning(f"Error stopping previous event bus after promotion: {e}")
        logger.info("Event bus promoted — all components re-wired to new bus")

    async def initialize(self) -> None:
        """Initialize all components and register them into the container."""
        logger.info("Initializing VCOREX Institutional Trading Bot (bootstrap)...")

        # Setup logging first
        setup_logging()

        try:
            await self._initialize_internal()
        except Exception as e:
            logger.critical(f"[BOOTSTRAP FATAL] Bot initialization failed: {e}", exc_info=True)
            raise

    async def _initialize_internal(self) -> None:
        """Internal initialization — separated so errors are always logged."""

        # Initialize database
        storage.database.init_database(self.settings)

        # Initialize Unit of Work
        if storage.database.SessionLocal is None:
            raise RuntimeError("Database session factory not initialized")

        if self.uow_factory:
            self.uow = self.uow_factory(storage.database.SessionLocal)
        else:
            self.uow = UnitOfWork(storage.database.SessionLocal)

        self.container.register_instance("uow", self.uow)

        # Initialize exchange (use injected factory if present)
        # Truyền event_bus và metrics adapter cho OKXExchange để đồng bộ hóa toàn hệ thống
        if self.exchange_factory:
            self.exchange = self.exchange_factory(self.settings)
        else:
            self.exchange = OKXExchange(
                self.settings,
                event_bus=self.event_bus,
                metrics=getattr(self.event_bus, "_metrics", None),
            )

        await self.exchange.initialize()
        self.container.register_instance("exchange", self.exchange)

        # Initialize event bus
        await self.event_bus.start()
        self.container.register_instance("event_bus", self.event_bus)

        # Initialize Market Data Engine
        self.market_data_engine = self.market_data_engine_cls(
            self.exchange, self.event_bus, self.settings
        )
        await self.market_data_engine.start()
        self.container.register_instance("market_data_engine", self.market_data_engine)

        # Start the consumer task and inject it into the engine
        consumer_task = self.watcher.watch(
            self.market_data_engine._run_consumer, "mde_consumer"
        )
        self.market_data_engine.set_consumer_task(consumer_task)
        logger.info("MarketDataEngine consumer task started and injected.")

        # Initialize Position Engine
        self.position_engine = self.position_engine_cls(
            self.exchange, self.event_bus, storage.database.AsyncSessionLocal, self.settings
        )
        await self.position_engine.start()
        self.container.register_instance("position_engine", self.position_engine)

        # Initialize Exchange Mirror and Shadow Validator
        self.exchange_mirror = ExchangeMirrorCache(self.event_bus, self.exchange)
        self.exchange_mirror.start()
        self.shadow_validator = ShadowValidator(self.position_engine, self.exchange_mirror)
        self.shadow_validator.start()
        self.position_engine.exchange_mirror = self.exchange_mirror
        self.position_engine.order_handler.exchange_mirror = self.exchange_mirror
        self.container.register_instance("exchange_mirror", self.exchange_mirror)
        self.container.register_instance("shadow_validator", self.shadow_validator)

        # Initialize Trade Journal Context Store (lightweight lifecycle metadata)
        self.journal_context_store = TradeJournalContextStore(self.event_bus)
        self.journal_context_store.start()
        self.container.register_instance("journal_context_store", self.journal_context_store)

        # FIX #8: Start task_scheduler BEFORE strategy engine registers analysis tasks
        # Without this, ALL scheduled analysis tasks (EMA crossover scans) never run → no signals → no entries
        await task_scheduler.start()
        logger.info("TaskScheduler started successfully")

        # Initialize Reconciliation Service for position/order consistency checks
        self.reconciliation_service = ReconciliationService(
            exchange=self.exchange,
            event_bus=self.event_bus,
            settings=self.settings,
            exchange_mirror=self.exchange_mirror,
        )
        # Inject order_handler reference for orphan detection
        if self.position_engine:
            self.reconciliation_service.order_handler = self.position_engine.order_handler
        self.container.register_instance("reconciliation_service", self.reconciliation_service)
        logger.info("ReconciliationService initialized")

        # Register maintenance scheduler tasks (orphan cleanup, periodic reconciliation)
        maintenance_scheduler.set_reconciliation_service(self.reconciliation_service)
        await maintenance_scheduler.register_periodic_tasks()
        logger.info("Maintenance scheduler tasks registered")

        # Initialize Strategy Engine
        self.strategy_engine = self.strategy_engine_cls(self.event_bus, self.exchange)
        await self.strategy_engine.initialize()
        self.container.register_instance("strategy_engine", self.strategy_engine)

        # Initialize Risk Manager
        self.risk_manager = self.risk_manager_cls(self.event_bus, self.exchange)
        self.risk_manager.exchange_mirror = self.exchange_mirror
        await self.risk_manager.initialize()
        self.container.register_instance("risk_manager", self.risk_manager)

        # Initialize Telegram Bot
        self.telegram_bot = self.telegram_bot_cls(self.event_bus)
        await self.telegram_bot.start()
        self.container.register_instance("telegram_bot", self.telegram_bot)

        # Notification service
        self.notification_service = self.notification_service_cls(self.event_bus, self.settings)
        await self.notification_service.start()
        self.container.register_instance("notification_service", self.notification_service)
        
        # Chart service
        self.chart_service = ChartService(self.event_bus, self.market_data_engine)
        self.chart_service.start()
        self.container.register_instance("chart_service", self.chart_service)

        # News engine
        self.news_engine = self.news_engine_cls(self.event_bus)
        await self.news_engine.start()
        self.container.register_instance("news_engine", self.news_engine)

        # Report Service
        self.report_service = self.report_service_cls(
            self.event_bus, storage.database.AsyncSessionLocal, self.position_engine
        )
        await self.report_service.start()
        self.container.register_instance("report_service", self.report_service)

        # Trade journaler — inject context_store for accurate duration/pnl_pct
        self.trade_journaler = self.trade_journaler_cls(
            self.event_bus, context_store=self.journal_context_store
        )
        self.container.register_instance("trade_journaler", self.trade_journaler)

        # Initialize Database Maintenance Service
        self.database_maintenance = DatabaseMaintenanceService(
            session_factory=storage.database.AsyncSessionLocal,
            engine=storage.database.engine,
            interval_hours=24.0,
            retention_days=30,
        )
        self.container.register_instance("database_maintenance", self.database_maintenance)

        # Wire OrderHandler transient cleanup to WS order fills
        self.event_bus.subscribe(
            self.position_engine.order_handler.handle_ws_raw_order_fill,
            [EventTopic.WS_RAW_ORDER, EventTopic.WS_RAW_ALGO_ORDER],
            handler_id="oh_transient_cleanup",
        )

        # Wire CONTROL_HALT_TRADING circuit breaker to OrderHandler
        self.position_engine.order_handler.subscribe_halt_trading()

        # Register health check loop as a watched task
        self.watcher.watch(self._health_check_loop, "core_health_check")

        # [PHASE 1 & 2] Register System Health Watchdog for Forensic Soak Validation
        self.watcher.watch(self._system_health_watchdog_loop, "system_health_watchdog")

        # Register default strategy if enabled
        if settings.enable_default_strategy:
            await self._register_default_strategy()

        self._registered = True
        logger.info("All components initialized successfully (bootstrap)")

    async def _register_default_strategy(self) -> None:
        ema_config = StrategyConfig(
            name="ema_crossover_default", symbols=settings.get_active_watchlist(), timeframes=settings.timeframes
        )
        ema_strategy = EMACrossoverStrategy(ema_config, event_bus=self.event_bus)
        assert self.strategy_engine is not None, "strategy_engine not initialized"
        await self.strategy_engine.register_strategy(ema_strategy)
        logger.info("Default EMA crossover strategy registered")

    async def start(self) -> None:
        if not self._registered:
            await self.initialize()

        logger.info("Starting VCOREX trading bot...")

        assert self.strategy_engine is not None, "strategy_engine not initialized"
        await self.strategy_engine.start()

        await self.database_maintenance.start()

        # Start transient order cleanup worker in OrderHandler as a background task (watched by TaskWatcher)
        self.watcher.watch(
            self.position_engine.order_handler.start_transient_cleanup, 
            "transient_order_cleanup",
            restart=True
        )

        # Log recovered positions
        if self.position_engine and hasattr(self.position_engine, "order_handler"):
            recovered = self.position_engine.order_handler.get_active_positions()
            if recovered:
                logger.warning(
                    f"[RECOVERY] Recovered {len(recovered)} open positions from DB: "
                    + ", ".join(f"{p.symbol}({p.side})" for p in recovered)
                )
            else:
                logger.info("[RECOVERY] No open positions to recover from DB")

        # Sử dụng timeframe_validator để chỉ subscribe channel được hỗ trợ
        supported_candle_channels = timeframe_validator.get_okx_channels_for_symbols(self.settings.get_active_watchlist())
        # Bỏ 'tickers' ra khỏi business channels
        if "tickers" in supported_candle_channels:
            supported_candle_channels.remove("tickers")

        logger.info(f"Initial WS subscription - Public: ['tickers'], Business: {supported_candle_channels}")
        self._public_ws_task = run_safe_task(
            self._run_websocket_stream(
                channels=["tickers"],
                symbols=self.settings.get_active_watchlist(),
                endpoint_type="public",
            )
        )
        self._business_ws_task = run_safe_task(
            self._run_websocket_stream(
                channels=supported_candle_channels,
                symbols=self.settings.get_active_watchlist(),
                endpoint_type="business",
            )
        )
        self._private_ws_task = run_safe_task(
            self._run_websocket_stream(
                channels=["account", "positions", "orders", "orders-algo"],
                symbols=self.settings.get_active_watchlist(),
                endpoint_type="private",
            )
        )

        logger.info("Trading bot is now running!")
        await self._shutdown_event.wait()

    async def _health_check_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(10)

            if hasattr(self.exchange, "_last_heartbeat"):
                last_hb = getattr(self.exchange, "_last_heartbeat", 0)
                if last_hb > 0 and (time.time() - last_hb) > 90:
                    logger.critical(
                        "🚨 WATCHDOG ALERT: WebSocket ping/pong frozen for >90s! Attempting to restart WS stream..."
                    )

                    if self.notification_service:
                        await self.event_bus.publish(
                            Event(
                                event_type=EventTopic.SYSTEM_ALERT,
                                data={
                                    "message": "⚠️ <b>TỰ ĐỘNG PHỤC HỒI MẠNG</b> ⚠️\nMất tín hiệu từ sàn OKX quá 90 giây do rớt mạng hoặc lag. Bot đang tự động reset luồng dữ liệu để tiếp tục giao dịch..."
                                },
                                source="watchdog",
                            )
                        )

                    if hasattr(self, "_public_ws_task") and self._public_ws_task:
                        self._public_ws_task.cancel()
                    if hasattr(self, "_business_ws_task") and self._business_ws_task:
                        self._business_ws_task.cancel()
                    if hasattr(self, "_private_ws_task") and self._private_ws_task:
                        self._private_ws_task.cancel()

                    if hasattr(self.exchange, "reconnect"):
                        try:
                            await self.exchange.reconnect()
                        except Exception as e:
                            logger.error(f"Failed to reconnect exchange: {e}")

                    # Sử dụng timeframe_validator để chỉ subscribe channel được hỗ trợ
                    supported_candle_channels = timeframe_validator.get_okx_channels_for_symbols(self.settings.get_active_watchlist())
                    if "tickers" in supported_candle_channels:
                        supported_candle_channels.remove("tickers")

                    logger.info(f"Subscribing to WS channels - Public: ['tickers'], Business: {supported_candle_channels}")
                    self._public_ws_task = run_safe_task(
                        self._run_websocket_stream(
                            channels=["tickers"],
                            symbols=self.settings.get_active_watchlist(),
                            endpoint_type="public",
                        )
                    )
                    self._business_ws_task = run_safe_task(
                        self._run_websocket_stream(
                            channels=supported_candle_channels,
                            symbols=self.settings.get_active_watchlist(),
                            endpoint_type="business",
                        )
                    )
                    self._private_ws_task = run_safe_task(
                        self._run_websocket_stream(
                            channels=["account", "positions", "orders"],
                            symbols=self.settings.get_active_watchlist(),
                            endpoint_type="private",
                        )
                    )

    async def _system_health_watchdog_loop(self) -> None:
        """
        [PHASE 1 & 2 FORENSICS] Long-run soak validation watchdog.
        Tracks asyncio tasks, memory growth, and event loop latency.
        Does NOT restart or mutate trading logic. Observability ONLY.
        """
        process = psutil.Process(os.getpid())
        while not self._shutdown_event.is_set():
            start_sleep_time = time.time()
            # Try to sleep exactly 5 seconds
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break

            wake_time = time.time()
            lag_sec = wake_time - start_sleep_time - 5.0
            lag_ms = max(0, lag_sec * 1000)

            # Telemetry tracking
            all_tasks = asyncio.all_tasks()
            task_count = len(all_tasks)
            mem_info = process.memory_info()
            rss_mb = mem_info.rss / (1024 * 1024)

            # [PHASE 1] Clock Drift & Time Integrity
            loop = asyncio.get_running_loop()
            monotonic_time = loop.time()
            # This is a basic monotonic drift sanity check for the loop
            # Real exchange offset is in MDE

            # [PHASE 4] GC Pressure & Object Retention
            gc_start = time.perf_counter()
            gc_stats = gc.get_stats()
            # Manual trigger optional, but tracking stats is useful
            gc_pause_ms = (time.perf_counter() - gc_start) * 1000

            # [PHASE 3] HTTP Session & Socket Leak Audit
            try:
                conns = process.connections(kind='inet')
                socket_fd_total = len(conns)
            except Exception:
                socket_fd_total = 0

            if self.market_data_engine and hasattr(self.market_data_engine, "metrics"):
                self.market_data_engine.metrics["event_loop_lag_ms"] = lag_ms
                self.market_data_engine.metrics["asyncio_task_count"] = task_count
                self.market_data_engine.metrics["rss_memory_mb"] = rss_mb
                self.market_data_engine.metrics["gc_pause_ms"] = gc_pause_ms
                self.market_data_engine.metrics["socket_fd_total"] = socket_fd_total

                # Expose connector pool size if possible
                if self.exchange:
                    if getattr(self.exchange, "session", None) and hasattr(self.exchange.session, "connector"):
                        self.market_data_engine.metrics["connector_pool_size"] = len(self.exchange.session.connector._conns)
                    else:
                        self.market_data_engine.metrics["connector_pool_size"] = 0

            if socket_fd_total > 500:
                logger.warning(f"[FORENSIC] HIGH SOCKET COUNT: {socket_fd_total} open connections. Possible connection leak!")

            if lag_ms > 250:
                logger.critical(
                    f"[FORENSIC] Event loop lag CRITICAL: {lag_ms:.1f}ms! "
                    f"Tasks: {task_count}, RSS Memory: {rss_mb:.1f}MB. "
                    "Check for blocking I/O or CPU starvation."
                )

            if gc_pause_ms > 250:
                logger.warning(f"[FORENSIC] GC Pause CRITICAL: {gc_pause_ms:.1f}ms. Object retention too high.")

            if task_count > 1000:
                logger.warning(f"[FORENSIC] High asyncio task count: {task_count}. Potential task leak.")

            # Periodic healthy forensic log every ~30 mins (done in soak runner usually, but good to have a debug pulse)
            # Actually, soak test runner will export the logs. This loop just measures and updates metrics.

            if self.market_data_engine:
                self.market_data_engine.last_updated = time.time()

    async def _run_websocket_stream(self, channels: list, symbols: list, endpoint_type: str = "public") -> None:
        try:
            assert self.exchange is not None, "exchange not initialized"
            async for message in self.exchange.websocket_stream(channels, symbols, endpoint_type=endpoint_type):
                if message.channel.startswith("candle"):
                    timeframe = message.channel.replace("candle", "")
                    # Lấy thời điểm nhận WebSocket (ms) để tính processing latency
                    websocket_receive_ms = int(message.timestamp.timestamp() * 1000)
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.MARKET_WS_CANDLE,
                            data={
                                "symbol": message.symbol,
                                "timeframe": timeframe,
                                "candle_data": message.data,
                                "websocket_receive_ms": websocket_receive_ms,
                            },
                            source="okx_ws",
                        )
                    )
                elif message.channel.startswith("tickers"):
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.MARKET_WS_TICKER,
                            data={"symbol": message.symbol, "ticker_data": message.data},
                            source="okx_ws",
                        )
                    )
                elif message.channel == "system" and message.symbol == "connect":
                    logger.info("[SYSTEM] WebSocket connected. Publishing WS_RECONNECTED event for sync.")
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.WS_RECONNECTED,
                            data={"status": "connected"},
                            source="okx_ws",
                        )
                    )
                elif message.channel == "account":
                    logger.debug(f"[DARK LAUNCH] Received WS account payload")
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.WS_RAW_ACCOUNT,
                            data={"data": message.data},
                            source="okx_ws",
                        )
                    )
                elif message.channel == "positions":
                    logger.debug(f"[DARK LAUNCH] Received WS positions payload for {message.symbol}")
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.WS_RAW_POSITION,
                            data={"symbol": message.symbol, "data": message.data},
                            source="okx_ws",
                        )
                    )
                elif message.channel == "orders":
                    logger.debug(f"[DARK LAUNCH] Received WS orders payload for {message.symbol}")
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.WS_RAW_ORDER,
                            data={"symbol": message.symbol, "data": message.data},
                            source="okx_ws",
                        )
                    )
                elif message.channel == "orders-algo":
                    logger.debug(f"[DARK LAUNCH] Received WS orders-algo payload for {message.symbol}")
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.WS_RAW_ALGO_ORDER,
                            data={"symbol": message.symbol, "data": message.data},
                            source="okx_ws",
                        )
                    )
        except Exception as e:
            logger.error(f"WebSocket stream error: {e}", exc_info=True)

    async def shutdown(self) -> None:
        if not self._registered:
            logger.info("[SHUTDOWN] Bot not initialized, skipping shutdown sequence.")
            return
        if getattr(self, "_shutdown_complete", False):
            logger.info("[SHUTDOWN] Shutdown already completed, skipping.")
            return

        logger.info("Initiating bot shutdown sequence...")
        self._shutdown_event.set()

        async def _safe_stop_component(component, component_name: str, timeout: float = 10.0) -> None:
            if not component or not hasattr(component, "stop"):
                return
            try:
                await asyncio.wait_for(component.stop(), timeout=timeout)
                logger.info(f"{component_name} stopped successfully")
            except asyncio.TimeoutError:
                logger.error(f"Timeout stopping {component_name} after {timeout}s")
            except Exception as e:
                logger.error(f"Error stopping {component_name}: {e}")

        async def _do_shutdown() -> None:
            for task_attr in ("_public_ws_task", "_business_ws_task", "_private_ws_task"):
                task = getattr(self, task_attr, None)
                if task and not task.done():
                    task.cancel()
                    logger.info(f"[SHUTDOWN] Cancelled {task_attr}")

            await _safe_stop_component(self.market_data_engine, "Market Data Engine")
            await _safe_stop_component(self.strategy_engine, "Strategy Engine")
            await _safe_stop_component(self.news_engine, "News Engine")
            logger.info("[SHUTDOWN] All event producers stopped")

            await _safe_stop_component(self.position_engine, "Position Engine")
            await _safe_stop_component(self.risk_manager, "Risk Manager")
            await _safe_stop_component(self.trade_journaler, "Trade Journaler")
            await _safe_stop_component(self.report_service, "Report Service")
            logger.info("[SHUTDOWN] All event processors stopped")

            if self.position_engine and hasattr(self.position_engine, "order_handler"):
                oh = self.position_engine.order_handler

                if settings.shutdown_liquidate_on_exit:
                    # Only cancel TP/SL when we are also closing all positions
                    try:
                        logger.warning("[SHUTDOWN] Cancelling all Algo Orders (TP/SL) on OKX...")
                        await asyncio.wait_for(oh.cancel_all_active_algo_orders(), timeout=8.0)
                    except asyncio.TimeoutError:
                        logger.error("[SHUTDOWN] Timeout cancelling algo orders.")
                    except Exception as e:
                        logger.error(f"[SHUTDOWN] Error cancelling algo orders: {e}")

                    try:
                        logger.warning("[SHUTDOWN] Panic closing all open positions at Market price...")
                        await asyncio.wait_for(
                            oh.panic_close_all_positions(reason="GRACEFUL_SHUTDOWN"),
                            timeout=8.0,
                        )
                        logger.warning("[SHUTDOWN] Exchange liquidation complete.")
                    except asyncio.TimeoutError:
                        logger.error("[SHUTDOWN] Timeout during position liquidation.")
                    except Exception as e:
                        logger.error(f"[SHUTDOWN] Error during position liquidation: {e}")
                else:
                    logger.info(
                        "[SHUTDOWN] shutdown_liquidate_on_exit=false — open positions AND TP/SL orders preserved on exchange."
                    )

            await _safe_stop_component(self.database_maintenance, "Database Maintenance Service")
            if self.shadow_validator:
                self.shadow_validator.stop()
            if self.exchange_mirror:
                self.exchange_mirror.stop()
            logger.info("[SHUTDOWN] All infrastructure services stopped")

            await _safe_stop_component(self.notification_service, "Notification Service")
            if self.chart_service:
                self.chart_service.stop()
            await _safe_stop_component(self.telegram_bot, "Telegram Bot")
            logger.info("[SHUTDOWN] All notification services stopped")

            if self.event_bus:
                try:
                    await self.event_bus.stop()
                    logger.info("Event Bus stopped")
                except Exception as e:
                    logger.error(f"Error stopping Event Bus: {e}")

            if self.watcher:
                self.watcher.stop_all()
                logger.info("Task Watcher stopped all tasks")

            if self.exchange:
                try:
                    await self.exchange.shutdown()
                    logger.info("Exchange connections closed")
                except Exception as e:
                    logger.error(f"Error closing exchange: {e}")

            try:
                storage.database.close_database()
                logger.info("Database connections closed")
            except Exception as e:
                logger.error(f"Error closing database: {e}")

        try:
            await task_scheduler.stop()
            await asyncio.wait_for(_do_shutdown(), timeout=22.0)
            self._shutdown_complete = True
            logger.info("VCOREX Bot shutdown complete. Safe to exit.")
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out after 22 seconds! Forcing exit to prevent hang.")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during shutdown: {e}")
            raise