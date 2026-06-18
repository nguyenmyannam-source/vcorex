"""
Market Data Engine - Phase 7 Implementation
Xử lý candle pipeline, market stream manager, indicator pipeline, EMA calculator.
Low latency, scalable watchlist, minimal API load.
"""

import asyncio
import contextlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from typing import Dict, List, Optional, Set
import collections
import os
import psutil

import pandas as pd
from loguru import logger

from core.config.settings import Settings
from core.container import run_safe_task
from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.task_watcher import TaskWatcher
from infrastructure.exchange.base_exchange import OHLCV, BaseExchange
from services.market_data import CandleBuffer, EMACalculator, IndicatorPipeline
from services.market_data.timeframe_validator import timeframe_validator

# Re-export for backward compat with tests importing directly from this module
__all__ = ["MarketDataEngine", "CandleBuffer", "EMACalculator", "IndicatorPipeline"]

class MarketDataEngine:
    """
    Main Market Data Engine xử lý toàn bộ việc thu thập và xử lý dữ liệu thị trường.
    - Candle aggregation pipeline
    - Multi-timeframe support
    - Low latency processing
    - Minimal API load
    """

    # Timeframe trong giây để tính toán khi nào cần fetch data mới
    TIMEFRAME_SECONDS = {
        "5m": 300,
        "15m": 900,
        "1H": 3600,
        "4H": 14400,
        "1D": 86400,
        "1W": 604800,
        "1M": 2592000,
    }

    # [REFACTOR] Priority-Based Progressive Hydration Tiers
    TIER1_TIMEFRAMES: set = {"5m"}         # Execution-Critical (Hydrate first, unlock signal immediately)
    TIER2_TIMEFRAMES: set = {"15m", "1H"}  # Primary Context (Bounded parallelism)
    TIER3_TIMEFRAMES: set = {"4H", "1D"}   # Secondary Context (Sequential, low concurrency)
    TIER4_TIMEFRAMES: set = {"1W", "1M"}   # Macro Context (Low-priority deferred hydration)

    def __init__(self, exchange: BaseExchange, event_bus: EventBus, settings: Settings):
        self.exchange = exchange
        self.event_bus = event_bus
        self.settings = settings
        self.buffers: Dict[str, CandleBuffer] = {}  # {symbol_timeframe: CandleBuffer}
        self.indicator_pipeline = IndicatorPipeline()
        self._running = False
        self._fetch_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self.watcher = TaskWatcher()
        self._watchlist: Set[str] = set(settings.get_active_watchlist())
        self._timeframes: Set[str] = set(settings.timeframes)
        self._last_fetch: Dict[str, datetime] = {}
        self._last_candle_ts: Dict[str, int] = {}
        # [FIX NGHẼN LUỒNG] Sử dụng khóa phân mảnh (Fine-grained Locking)
        self._buffer_locks = collections.defaultdict(asyncio.Lock)
        self._background_tasks: Set[asyncio.Task] = set()
        self._cleanup_task: Optional[asyncio.Task] = None

        # --- Readiness Tracking (extensible for future: per-strategy gating) ---
        self._tf_ready: Dict[str, bool] = {tf: False for tf in settings.timeframes}
        self._tf_seed_count: Dict[str, int] = {tf: 0 for tf in settings.timeframes}

        self._fetch_semaphore = asyncio.Semaphore(settings.max_concurrent_fetches if hasattr(settings, 'max_concurrent_fetches') else 5)
        self.last_updated: float = time.time()  # Track for watchdog
        # [PHASE 8] Throttled Fallback Concurrency
        self._rest_fallback_semaphore = asyncio.Semaphore(settings.max_rest_fallback_concurrency if hasattr(settings, 'max_rest_fallback_concurrency') else 3)
        self._last_seed_ts: dict[str, float] = {}
        # PHASE 3 - HISTORICAL SEED COALESCING: Track in-progress seed tasks
        self._seed_in_progress: Dict[tuple[str, str], asyncio.Task] = {}  # (symbol, timeframe): running_task
        # PHASE 4 - SNAPSHOT CONSISTENCY CHECK: Track snapshot integrity metrics
        self._snapshot_integrity_errors: Dict[tuple[str, str], int] = {}  # (symbol, timeframe): error_count
        self._last_valid_snapshot: Dict[tuple[str, str], float] = {}  # (symbol, timeframe): last_valid_timestamp

        # --- STREAM HEALTH TRACKING (Lightweight Institutional Survivability) ---
        self.stream_health: Dict[str, str] = {tf: "HEALTHY" for tf in settings.timeframes}
        self.stream_health["tickers"] = "HEALTHY"
        self._last_health_flip: Dict[str, float] = {}  # Anti-flapping hysteresis
        self._last_receive_ts: Dict[str, float] = {tf: time.time() for tf in settings.timeframes}
        self._last_receive_ts["tickers"] = time.time()
        self._last_rest_fetch_ts: Dict[str, float] = {tf: time.time() for tf in settings.timeframes}  # Track REST data freshness
        self._last_rest_fetch_ts["tickers"] = time.time()
        self._fallback_active: Dict[str, bool] = {tf: False for tf in settings.timeframes}
        self._processed_candles: Set[str] = set()  # Dedup: "symbol_timeframe_timestamp"
        self._latest_candle_snapshots: Dict[str, List[OHLCV]] = {}
        self._data_source: Dict[str, str] = {tf: "UNKNOWN" for tf in settings.timeframes}  # Track data provenance: "WS" | "REST"

        # --- QUEUE BACKPRESSURE VERIFICATION METRICS ---
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)  # [FIX] Add maxsize to prevent memory leak
        self._consumer_task: Optional[asyncio.Task] = None # Sẽ được set từ bootstrap
        self._last_metrics_log_time = time.time()
        self._produced_count = 0
        self._consumed_count = 0
        self._peak_queue_size = 0
        self._last_backlog = 0
        self._last_rss_mb = 0.0
        self._consumer_processing_latencies: List[float] = [] # Rolling window of latencies in ms

        self.metrics = {
            "tf_seed_failures": 0,
            "volatility_lock_wait_ms": 0.0,
            "bootstrap_retry_count": 0,
            "cooldown_hits_during_bootstrap": 0,
            "semaphore_wait_time_ms": 0.0,
            "out_of_order_candle_total": 0,
            "duplicate_candle_dropped": 0,
            "degraded_tf_blocks": 0,
            "replayed_candle_ignored": 0,
            "ws_reconnect_total": 0,
            "ws_stale_seconds": 0,
            "ws_replay_total": 0,
            "reconnect_burst_total": 0,
            "reconnect_coalesced_total": 0,
            "resync_deduplicated_total": 0,
            "rest_fallback_skip_total": 0,
            "rest_seed_cooldown_hit_total": 0,
            "fallback_overlap_blocked_total": 0,
            "stale_hydrate_batch_rejected_total": 0,
            "historical_fetch_replay_prevented_total": 0,
            # [PHASE 6] Deterministic Timeline Integrity
            "extreme_out_of_order_total": 0,
            "timeline_corruption_total": 0,
            "replay_stale_drop_total": 0,
            "historical_reseed_rejected_total": 0,
            # [PHASE 7] Forensic Stability
            "exchange_clock_offset_ms": 0,
            "cancelled_task_total": 0,
            "orphan_task_total": 0,
        }
        # LOGGING BENCHMARK METRICS (TRƯỚC PATCH) - Đo thực tế runtime
        self._logging_execution_times: list[float] = []  # Thời gian thực thi mỗi logger call (ms)
        self._log_call_count = 0  # Tổng số logger.info calls
        self._last_logging_benchmark = time.time()  # Đo logs/sec
        logger.info("MarketDataEngine initialized")

    def set_consumer_task(self, task: asyncio.Task):
        """Allow bootstrap to inject a reference to the consumer task for dead-consumer detection."""
        self._consumer_task = task

    async def start(self) -> None:
        """Start the market data engine."""
        if self._running:
            logger.warning("MarketDataEngine is already running")
            return

        # Khởi tạo tất cả buffers
        await self._initialize_buffers()

        # Subscribe to WebSocket candles and reset events
        self.event_bus.subscribe(
            self._handle_ws_candle, [EventTopic.MARKET_WS_CANDLE], handler_id="mde_ws_candle"
        )
        self.event_bus.subscribe(
            self._handle_reset_buffers,
            [EventTopic.MARKET_RESET_BUFFERS],
            handler_id="mde_reset_buffers",
        )
        self.event_bus.subscribe(
            self._handle_ws_reconnected,
            [EventTopic.WS_RECONNECTED],
            handler_id="mde_ws_reconnected",
        )
        self.event_bus.subscribe(
            self._handle_radar_limit_changed,
            [EventTopic.CONTROL_RADAR_LIMIT_CHANGED],
            handler_id="mde_radar_limit",
        )

        self._running = True

        # Start background task cleanup worker
        self._cleanup_task = asyncio.create_task(self._cleanup_completed_tasks())
        # [REFACTOR LỖI 429] Tắt hoàn toàn worker gọi REST liên tục.
        # self.watcher.watch(self._data_fetch_worker, "mde_fetch_worker")
        self.watcher.watch(self._memory_cleanup_worker, "mde_cleanup_worker")
        self.watcher.watch(self._volatility_monitor_worker, "mde_volatility_worker")
        self.watcher.watch(self._ws_heartbeat_watchdog, "mde_ws_watchdog")
        self.watcher.watch(self._stream_health_watchdog, "mde_stream_health") # NEW Stream Health Watchdog
        self.watcher.watch(self._queue_monitor_worker, "mde_queue_monitor") # NEW
        logger.info(
            f"MarketDataEngine started. Watching {len(self._watchlist)} symbols across {len(self._timeframes)} timeframes"
        )

    async def _handle_radar_limit_changed(self, event: Event) -> None:
        """Cập nhật watchlist khi người dùng thay đổi tầm quét Radar qua Telegram."""
        new_limit = event.data.get("radar_limit", 20)
        self.settings.radar_limit = new_limit
        full_watchlist = self.settings.watchlist
        new_active = set(full_watchlist[:new_limit])
        old_active = self._watchlist

        removed = old_active - new_active
        added = new_active - old_active

        # Cập nhật watchlist
        self._watchlist = new_active

        # Xóa buffers của các coin bị loại khỏi radar → giải phóng RAM/CPU
        if removed:
            for key in list(self.buffers.keys()):
                sym = key.split("_")[0] if "_" in key else key
                if sym in removed:
                    del self.buffers[key]
            logger.info(
                f"[RADAR] Tầm quét giảm xuống Top {new_limit}. "
                f"Đã gỡ {len(removed)} coin: {', '.join(s.replace('-USDT-SWAP', '') for s in removed)}"
            )

        if added:
            logger.info(
                f"[RADAR] Tầm quét mở rộng lên Top {new_limit}. "
                f"Thêm {len(added)} coin: {', '.join(s.replace('-USDT-SWAP', '') for s in added)}"
            )
            # PHASE 3: Seed mới các coin được thêm vào - seed coalescing sẽ ngăn duplicate
            for symbol in added:
                for timeframe in self._timeframes:
                    key = f"{symbol}_{timeframe}"
                    async with self._buffer_locks[key]:
                        if key not in self.buffers:
                            self.buffers[key] = CandleBuffer(symbol, timeframe)
                    # Seed historical data cho coin mới
                    run_safe_task(self._seed_historical_data(symbol, timeframe), self._background_tasks)

        logger.info(
            f"[RADAR] Watchlist cập nhật thành công. Đang theo dõi {len(self._watchlist)} coin."
        )

    async def _stream_health_watchdog(self):
        """Monitors stream health and triggers REST fallback if silent."""
        try:
            while self._running:
                await asyncio.sleep(5)
                now = time.time()
                for tf in self.settings.timeframes:
                    last_ts = self._last_receive_ts.get(tf, now)
                    last_rest_ts = self._last_rest_fetch_ts.get(tf, now)
                    # [PHASE 10] Dynamic silence threshold based on timeframe
                    # 15m candles arrive every 900s, 1H every 3600s — fixed 180s was too aggressive
                    tf_seconds = self.TIMEFRAME_SECONDS.get(tf, 60)
                    silence_threshold = max(tf_seconds * 2.5, 180)

                    ws_silent = (now - last_ts) > silence_threshold
                    rest_silent = (now - last_rest_ts) > silence_threshold

                    if ws_silent:
                        stale_duration = int(now - last_ts)
                        self.metrics["ws_stale_seconds"] = self.metrics.get("ws_stale_seconds", 0) + 5

                        # FIX: Only set DEGRADED if BOTH WS and REST are silent
                        # If REST is active, set PARTIAL_HEALTHY (allows trading with REST data)
                        if rest_silent:
                            if self.stream_health.get(tf) != "DEGRADED":
                                # [PHASE 11] Anti-Flapping Hysteresis Guard (30s minimum wait)
                                last_flip = self._last_health_flip.get(tf, 0)
                                if now - last_flip > 30.0:
                                    logger.warning(f"[STREAM HEALTH] Stream {tf} SILENT for >{silence_threshold}s! Both WS and REST silent. Marking DEGRADED.")
                                    self.stream_health[tf] = "DEGRADED"
                                    self._last_health_flip[tf] = now
                                else:
                                    logger.debug(f"[STREAM HEALTH] Stream {tf} silent, but delaying DEGRADED flip due to Hysteresis (<30s since last change).")
                        else:
                            # REST is active, set PARTIAL_HEALTHY (allows trading)
                            if self.stream_health.get(tf) not in ("PARTIAL_HEALTHY", "HEALTHY"):
                                last_flip = self._last_health_flip.get(tf, 0)
                                if now - last_flip > 30.0:
                                    logger.warning(f"[STREAM HEALTH] Stream {tf} WS silent but REST active. Marking PARTIAL_HEALTHY (trading allowed with REST data).")
                                    self.stream_health[tf] = "PARTIAL_HEALTHY"
                                    self._last_health_flip[tf] = now

                        if self.stream_health.get(tf) in ("DEGRADED", "PARTIAL_HEALTHY") and not self._fallback_active.get(tf):
                            # Start periodic polling for this timeframe
                            self._fallback_active[tf] = True
                            run_safe_task(self._fallback_poll_loop(tf), self._background_tasks)
                    else:
                        # WS is healthy, mark as HEALTHY
                        if self.stream_health.get(tf) != "HEALTHY":
                            last_flip = self._last_health_flip.get(tf, 0)
                            if now - last_flip > 30.0:
                                logger.info(f"[STREAM HEALTH] Stream {tf} recovered. Marking HEALTHY.")
                                self.stream_health[tf] = "HEALTHY"
                                self._last_health_flip[tf] = now
                                # Stop REST fallback if active
                                if self._fallback_active.get(tf):
                                    self._fallback_active[tf] = False
        except asyncio.CancelledError:
            self.metrics["cancelled_task_total"] = self.metrics.get("cancelled_task_total", 0) + 1
            logger.info("[PHASE 7] _stream_health_watchdog cancelled gracefully.")
            raise

    async def _fallback_poll_loop(self, tf: str):
        """Continuously polls REST API for candles while stream is DEGRADED."""
        logger.info(f"[REST FALLBACK] Started periodic polling for DEGRADED stream {tf}.")
        self._data_source[tf] = "REST"  # Track data provenance during fallback
        tf_seconds = self.TIMEFRAME_SECONDS.get(tf, 60)
        # [FIX] Reduced polling interval for critical timeframes (5m, 15m)
        if tf in ("5m", "15m"):
            poll_interval = max(tf_seconds / 2.0, 5.0)  # Minimum 5 seconds for critical timeframes
        else:
            poll_interval = max(tf_seconds / 2.0, 15.0)  # Minimum 15 seconds for other timeframes

        try:
            while self._running and self.stream_health.get(tf) == "DEGRADED":
                for symbol in self.settings.watchlist:
                    # PHASE 3: Seed coalescing - _seed_historical_data will handle reusing existing tasks
                    run_safe_task(self._seed_historical_data(symbol, tf), self._background_tasks)
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            self.metrics["cancelled_task_total"] = self.metrics.get("cancelled_task_total", 0) + 1
            logger.info(f"[FORENSIC] Worker cancelled safely {tf} (fallback_poll_loop)")
            raise
        finally:
            self._fallback_active[tf] = False
            self._data_source[tf] = "WS"  # Track data provenance when fallback ends
            logger.info(f"[REST FALLBACK] Stopped polling for stream {tf} (Stream Recovered or Cancelled).")

    async def _queue_monitor_worker(self):
        """Periodically logs queue and consumer health metrics."""
        try:
            while self._running:
                await asyncio.sleep(60) # Log every 60 seconds

                current_time = time.time()
                delta_time = current_time - self._last_metrics_log_time
                if delta_time == 0: continue

                # --- Snapshot and Reset Counters ---
                produced = self._produced_count
                consumed = self._consumed_count
                latencies = self._consumer_processing_latencies.copy()
                peak_qsize = self._peak_queue_size

                self._produced_count = 0
                self._consumed_count = 0
                self._consumer_processing_latencies.clear()
                self._peak_queue_size = self._queue.qsize() # Reset peak to current
                self._last_metrics_log_time = current_time
                # --- End Snapshot ---

                producer_rate = produced / delta_time
                consumer_rate = consumed / delta_time
                backlog = self._queue.qsize()

                latency_stats = "N/A"
                if latencies:
                    avg_latency = sum(latencies) / len(latencies)
                    latency_stats = f"avg={avg_latency:.2f}ms"

                # [PHASE 8] Forensic Slope Analytics
                queue_growth_slope = backlog - self._last_backlog
                self._last_backlog = backlog

                process = psutil.Process(os.getpid())
                current_rss_mb = process.memory_info().rss / (1024 * 1024)
                memory_delta_per_hour = (current_rss_mb - self._last_rss_mb) * (3600.0 / delta_time) if self._last_rss_mb > 0 else 0.0
                self._last_rss_mb = current_rss_mb

                fallback_active_count = sum(1 for active in self._fallback_active.values() if active)

                logger.info(
                    f"[QUEUE_METRICS] backlog={backlog} (slope={queue_growth_slope:+.0f}/60s) peak={peak_qsize} | "
                    f"rate(evt/s): in={producer_rate:.1f} out={consumer_rate:.1f} | "
                    f"consumer_latency: {latency_stats} | "
                    f"mem_slope: {memory_delta_per_hour:+.1f}MB/h | "
                    f"active_fallbacks: {fallback_active_count}"
                )

                if producer_rate > consumer_rate * 1.1 and backlog > 100:
                     logger.warning(
                        f"[QUEUE_PRESSURE] Producer is outpacing consumer. Backlog is growing. "
                        f"In-rate: {producer_rate:.1f}/s, Out-rate: {consumer_rate:.1f}/s"
                    )

                # [PHASE 6] Aggregate forensic timeline metrics from all CandleBuffers
                agg_extreme = 0
                agg_corruption = 0
                agg_reseed_rejected = 0
                for buf in self.buffers.values():
                    if hasattr(buf, "get_forensic_metrics"):
                        fm = buf.get_forensic_metrics()
                        agg_extreme += fm.get("extreme_out_of_order_total", 0)
                        agg_corruption += fm.get("timeline_corruption_total", 0)
                        agg_reseed_rejected += fm.get("historical_reseed_rejected_total", 0)

                self.metrics["extreme_out_of_order_total"] = agg_extreme
                self.metrics["timeline_corruption_total"] = agg_corruption
                self.metrics["historical_reseed_rejected_total"] = agg_reseed_rejected

                if agg_extreme > 0:
                    logger.critical(
                        f"[FORENSIC] extreme_out_of_order_total={agg_extreme} "
                        f"timeline_corruption_total={agg_corruption} "
                        f"historical_reseed_rejected_total={agg_reseed_rejected} "
                        "— Candle timeline integrity violation detected!"
                    )

                # [PHASE 7] Log clock offset
                if hasattr(self.exchange, "_server_time_offset"):
                    self.metrics["exchange_clock_offset_ms"] = self.exchange._server_time_offset

                offset = self.metrics.get("exchange_clock_offset_ms", 0)
                if abs(offset) > 1000 and hasattr(self.exchange, "sync_time"):
                    try:
                        logger.warning(
                            f"[FORENSIC] EXCHANGE CLOCK OFFSET LARGE: {offset}ms. Re-syncing server time to verify."
                        )
                        await self.exchange.sync_time()
                        self.metrics["exchange_clock_offset_ms"] = getattr(self.exchange, "_server_time_offset", offset)
                        offset = self.metrics["exchange_clock_offset_ms"]
                    except Exception as e:
                        logger.warning(
                            f"[FORENSIC] Failed to refresh exchange time sync: {e}."
                        )

                if abs(offset) > 3000:
                    logger.critical(
                        f"[FORENSIC] EXCHANGE CLOCK OFFSET CRITICAL: {offset}ms! Potential VPS clock rollback or drift."
                    )

        except asyncio.CancelledError:
            self.metrics["cancelled_task_total"] = self.metrics.get("cancelled_task_total", 0) + 1
            logger.info("[PHASE 7] _queue_monitor_worker cancelled gracefully.")
            raise

    async def _run_consumer(self):
        """The consumer part of the pipeline. Gets events from the queue and processes them."""
        logger.info("MarketDataEngine consumer task started.")
        while self._running:
            try:
                event = await self._queue.get()

                consumer_start_time = time.perf_counter()

                await self._process_candle_event(event)

                self._queue.task_done()
                self._consumed_count += 1

                consumer_end_time = time.perf_counter()
                latency_ms = (consumer_end_time - consumer_start_time) * 1000

                # Rolling window for latencies
                if len(self._consumer_processing_latencies) >= 1000:
                    self._consumer_processing_latencies.pop(0)
                self._consumer_processing_latencies.append(latency_ms)

            except asyncio.CancelledError:
                logger.info("MarketDataEngine consumer task cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in MDE consumer loop: {e}", exc_info=True)
                # Avoid tight loop on persistent error
                await asyncio.sleep(1)

    async def _process_candle_event(self, event: Event):
        """This contains the original logic from _handle_ws_candle for processing."""
        try:
            data = event.data
            symbol = data.get("symbol")
            timeframe = data.get("timeframe")
            candle_data = data.get("candle_data")
            websocket_receive_ms = data.get("websocket_receive_ms", int(time.time() * 1000))

            if not symbol or not timeframe or not candle_data:
                return

            key = f"{symbol}_{timeframe}"

            # Map timeframe lại cho chuẩn OKX (nếu WS stream trả về khác)
            if timeframe not in self._timeframes:
                return

            # Bắt đầu đo thời gian xử lý nội bộ
            processing_start_ms = int(time.time() * 1000)
            candle = OHLCV.from_list(candle_data, symbol, timeframe)

            # --- Deduplication Guard (confirmed bars only) ---
            # Intra-candle WS ticks share the same open timestamp; allow them through
            # so CandleBuffer can replace the forming bar with latest OHLCV.
            if candle.confirmed:
                dedup_key = f"{symbol}_{timeframe}_{candle.timestamp}"
                if dedup_key in self._processed_candles:
                    self.metrics["duplicate_candle_dropped"] = self.metrics.get("duplicate_candle_dropped", 0) + 1
                    return
                self._processed_candles.add(dedup_key)
                if len(self._processed_candles) > 10000:
                    # Evict oldest half instead of clearing entire window
                    evict_count = len(self._processed_candles) // 2
                    for _ in range(evict_count):
                        self._processed_candles.pop()

            # --- Update Stream Health ---
            now = time.time()
            self._last_receive_ts[timeframe] = now
            self._data_source[timeframe] = "WS"  # Track data provenance
            # [PHASE 11] Anti-Flapping Hysteresis Guard (30s minimum wait)
            if self.stream_health[timeframe] != "HEALTHY":
                now = time.time()
                last_flip = self._last_health_flip.get(timeframe, 0)
                if now - last_flip > 30.0:
                    logger.info(f"[STREAM HEALTH] Stream {timeframe} recovered from WS. Marking HEALTHY.")
                    self.stream_health[timeframe] = "HEALTHY"
                    self._last_health_flip[timeframe] = now
                else:
                    logger.debug(f"[STREAM HEALTH] Delaying HEALTHY flip for {timeframe} due to Hysteresis.")
                self._fallback_active[timeframe] = False

            # === NETWORK DATA GAP HEALING ===
            last_ts = self._last_candle_ts.get(key, 0)
            current_ts = candle.timestamp  # ms (thời điểm mở nến - chỉ dùng để phát hiện gap dữ liệu)
            if timeframe not in self.TIMEFRAME_SECONDS:
                logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
                raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
            tf_seconds = self.TIMEFRAME_SECONDS[timeframe]

            if last_ts > 0 and (current_ts - last_ts) > (tf_seconds * 1.5 * 1000):
                gap_sec = (current_ts - last_ts) / 1000.0
                logger.warning(
                    f"NETWORK DATA GAP DETECTED: {symbol} {timeframe} missed candles "
                    f"(gap of {gap_sec:.1f}s > expected {tf_seconds}s). Triggering REST fallback healing..."
                )
                # PHASE 3: Seed coalescing - _seed_historical_data sẽ xử lý tránh duplicate seed
                run_safe_task(self._seed_historical_data(symbol, timeframe), self._background_tasks)

            self._last_candle_ts[key] = current_ts

            new_complete_candle = False
            async with self._buffer_locks[key]:
                if key in self.buffers:
                    buf = self.buffers[key]
                    # [PHASE 6] Replay Sanity Filter: drop WS events older than buffer watermark
                    if candle.timestamp < buf.high_watermark_ts:
                        self.metrics["replay_stale_drop_total"] = self.metrics.get("replay_stale_drop_total", 0) + 1
                        logger.debug(
                            f"[REPLAY FILTER] WS event older than watermark dropped for {symbol} {timeframe}: "
                            f"ts={candle.timestamp} < watermark={buf.high_watermark_ts}"
                        )
                    else:
                        new_complete_candle = buf.add_candle(candle)

            self._last_fetch[key] = datetime.now(timezone.utc)
            self.last_updated = time.time()

            # Kết thúc đo thời gian xử lý và tính toán latency chính xác
            processing_end_ms = int(time.time() * 1000)
            processing_latency_ms = processing_end_ms - processing_start_ms
            total_signal_latency_ms = processing_end_ms - websocket_receive_ms

            # PATCH 2A: SAMPLING LOGGING - chỉ log 10% số PERF events để giảm spam, giữ observability
            log_this_perf_event = random.random() < 0.1
            if log_this_perf_event:
                log_start = time.perf_counter()
                logger.info(
                    f"[PERF] symbol={symbol} timeframe={timeframe} "
                    f"processing_latency={processing_latency_ms}ms "
                    f"total_signal_latency={total_signal_latency_ms}ms "
                    f"candle_open_timestamp={current_ts}"
                )
                log_end = time.perf_counter()
                log_duration_ms = (log_end - log_start) * 1000
                if len(self._logging_execution_times) >= 1000:
                    self._logging_execution_times.pop(0)
                self._logging_execution_times.append(log_duration_ms)

            # Tăng counter cho tất cả các event xử lý, không chỉ event được log
            self._log_call_count += 1

            # Tính toán và log logging stats mỗi 1000 events (thay vì 100) để giảm spam
            if self._log_call_count % 1000 == 0:
                # NOTE: Numpy-based benchmark logging has been removed as part of cleanup.
                if self._logging_execution_times:
                    self._last_logging_benchmark = time.time()
                    self._logging_execution_times.clear()

            # Nếu có nến mới hoàn thành, publish event và tính lại indicators
            if new_complete_candle:
                await self._on_new_complete_candle(symbol, timeframe, candle)
                await self._compute_and_publish_indicators(symbol, timeframe)

        except Exception as e:
            logger.error(f"Error processing candle event: {e}", exc_info=True)

    async def _cleanup_completed_tasks(self) -> None:
        """Periodically cleanup completed background tasks and stale cache entries to prevent memory leaks."""
        while self._running:
            try:
                # Remove completed tasks from set
                completed_tasks = {task for task in self._background_tasks if task.done()}
                if completed_tasks:
                    self._background_tasks -= completed_tasks
                    logger.debug(f"Cleaned up {len(completed_tasks)} completed background tasks")

                # Cleanup stale snapshot cache entries
                self.indicator_pipeline.cleanup_stale_snapshots()

                # Sleep for 30 seconds before next cleanup
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                logger.info("Background task cleanup worker cancelled")
                break
            except Exception as e:
                logger.error(f"Error in background task cleanup worker: {e}")
                await asyncio.sleep(30)

    async def _memory_cleanup_worker(self) -> None:
        """Background task chạy mỗi 24h để clear RAM."""
        while self._running:
            # Chờ 24h (86400 giây)
            await asyncio.sleep(86400)
            logger.info("Running scheduled 24h Memory Cache Cleanup...")

            # Xóa các symbol không còn trong watchlist
            keys_to_delete = []
            for key in list(self.buffers.keys()):
                symbol = key.split("_")[0]
                if symbol not in self._watchlist:
                    keys_to_delete.append(key)

            for key in keys_to_delete:
                del self.buffers[key]
                if key in self.indicator_pipeline.indicator_cache:
                    del self.indicator_pipeline.indicator_cache[key]

            logger.info(f"Memory Cleanup complete. Evicted {len(keys_to_delete)} stale buffers.")

    def _get_volatility_thresholds(self, symbol: str) -> tuple[float, float]:
        """
        Phân nhóm coin thành 3 Tiers để lấy ngưỡng biến động giá hợp lý:
        - Tier 1: BTC, ETH (Mega Cap) -> 5m: 2.0%, 1m: 1.5%
        - Tier 2: SOL, BNB, XRP, ADA, LINK, DOT, LTC, BCH, UNI (High Cap) -> 5m: 4.0%, 1m: 3.0%
        - Tier 3: DOGE, AVAX, NEAR, FIL, SUI, ARB, OP, POL, ATOM (High-Beta/Meme) -> 5m: 6.0%, 1m: 4.0%
        Returns:
            (threshold_5m, threshold_1m)
        """
        symbol_upper = symbol.upper()
        if "BTC" in symbol_upper or "ETH" in symbol_upper:
            return 2.0, 1.5

        tier2_keys = ["SOL", "BNB", "XRP", "ADA", "LINK", "DOT", "LTC", "BCH", "UNI"]
        if any(k in symbol_upper for k in tier2_keys):
            return 4.0, 3.0

        # Mặc định là Tier 3
        return 6.0, 4.0

    async def _volatility_monitor_worker(self) -> None:
        """Theo dõi biến động giá đột biến ngầm định kỳ 1 phút với phân nhóm Dynamic Tiers."""
        last_prices: Dict[str, float] = {}

        while self._running:
            try:
                # Quét mỗi 1 phút
                await asyncio.sleep(60)

                for symbol in self._watchlist:
                    # Lấy giá hiện tại từ buffer 5m (timeframe nhỏ nhất trong watchlist chuẩn)
                    key = f"{symbol}_5m"

                    # Snapshot price under lock to prevent holding lock during downstream logic
                    current_price = None
                    start_wait = time.time()
                    async with self._buffer_locks[key]:
                        wait_ms = (time.time() - start_wait) * 1000.0
                        if hasattr(self, "metrics"):
                            self.metrics["volatility_lock_wait_ms"] = self.metrics.get("volatility_lock_wait_ms", 0.0) + wait_ms

                        if key in self.buffers:
                            buffer = self.buffers[key]
                            if buffer.candles:
                                current_price = buffer.candles[-1].close

                    if current_price is None:
                        continue

                    if symbol in last_prices:
                        prev_price = last_prices[symbol]
                        change_pct = ((current_price - prev_price) / prev_price) * 100

                        # Lấy ngưỡng động cho 1 phút
                        _, threshold_1m = self._get_volatility_thresholds(symbol)

                        # Nếu biến động vượt ngưỡng của nhóm
                        if abs(change_pct) >= threshold_1m:
                            logger.warning(
                                f"VOLATILITY ALERT: {symbol} moved {change_pct:.2f}% in 1 min (threshold: {threshold_1m}%)"
                            )
                            await self.event_bus.publish(
                                Event(
                                    event_type=EventTopic.MARKET_VOLATILITY_ALERT,
                                    data={
                                        "symbol": symbol,
                                        "change_pct": change_pct,
                                        "price": current_price,
                                        "period": "1 phút",
                                        "vol_spike": "Đột biến",
                                    },
                                    source="market_data_engine",
                                )
                            )

                    last_prices[symbol] = current_price

            except Exception as e:
                logger.error(f"Error in volatility monitor: {e}")
                await asyncio.sleep(10)

    async def stop(self) -> None:
        """Stop the market data engine gracefully."""
        if not self._running:
            return

        self._running = False

        # Cancel cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        # Create a list copy to avoid "Set changed size during iteration" RuntimeError
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._background_tasks.clear()
        for handler_id in (
            "mde_ws_candle",
            "mde_reset_buffers",
            "mde_ws_reconnected",
            "mde_radar_limit",
        ):
            self.event_bus.unsubscribe(handler_id=handler_id)
        self.watcher.stop_all()

        logger.info("MarketDataEngine stopped")

    async def _adaptive_stabilization(self) -> None:
        """Adaptive stabilization: Wait until gateway cooldowns clear."""
        # Base jitter
        await asyncio.sleep(random.uniform(0.1, 0.5))
        try:
            # Check if exchange has a cooldown engine
            if hasattr(self.exchange, "_cooldown_engines"):
                cooldowns = self.exchange._cooldown_engines
                for domain, engine in cooldowns.items():
                    now = asyncio.get_event_loop().time()
                    if engine.cooldown_until > now:
                        wait_time = engine.cooldown_until - now
                        self.metrics["cooldown_hits_during_bootstrap"] = self.metrics.get("cooldown_hits_during_bootstrap", 0) + 1
                        logger.debug(f"[MDE BOOTSTRAP] Gateway throttle active on '{domain}'. Waiting {wait_time:.2f}s for normalization...")
                        await asyncio.sleep(wait_time + 0.5)
        except Exception as e:
            logger.warning(f"[MDE BOOTSTRAP] Stabilization error: {e}")

    async def _initialize_buffers(self) -> None:
        """Priority-Based Progressive Hydration (Tiered Bootstrapping)."""
        logger.info("[MDE BOOTSTRAP] Initiating Priority-Based Progressive Hydration...")
        start_time = time.time()

        # Pre-create all buffer slots (lock scope kept minimal)
        for symbol in self._watchlist:
            for timeframe in self._timeframes:
                key = f"{symbol}_{timeframe}"
                async with self._buffer_locks[key]:
                    if key not in self.buffers:
                        self.buffers[key] = CandleBuffer(symbol, timeframe)

        async def staggered_seed(s: str, tf: str) -> bool:
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    # [FIX 1] Tăng timeout từ 30s → 90s:
                    # fetch_ohlcv(limit=1440) thực hiện 15 pagination requests nội bộ.
                    # Mỗi request: ~1s token bucket + ~0.1s latency = ~16.5s/symbol.
                    # 30s quá sát — nếu API chậm hơn bình thường sẽ bị abort oan.
                    await asyncio.wait_for(
                        self._seed_historical_data(s, tf, is_bootstrap=True),
                        timeout=90.0
                    )
                    # Randomized micro-burst fragmentation (giữ nguyên cho runtime)
                    await asyncio.sleep(random.uniform(0.100, 0.500))
                    return True
                except asyncio.TimeoutError:
                    logger.warning(
                        f"⚠️ SEED TIMEOUT: {s} {tf} — seeding thất bại sau 90s, "
                        f"symbol này sẽ bị BLOCK trade cho đến khi seed lại thành công."
                    )
                    self.metrics["tf_seed_failures"] = self.metrics.get("tf_seed_failures", 0) + 1
                    # Đảm bảo symbol KHÔNG được phép trade khi seeding thất bại
                    key = f"{s}_{tf}"
                    self._tf_ready[tf] = False
                    self._last_seed_ts[key] = 0  # Reset để cho phép retry ngay
                except Exception as e:
                    self.metrics["tf_seed_failures"] = self.metrics.get("tf_seed_failures", 0) + 1
                    if attempt == max_retries:
                        logger.error(
                            f"[MDE BOOTSTRAP] FATAL: Failed to seed {s} {tf} after {max_retries} attempts: {e}. "
                            f"Symbol này sẽ bị BLOCK trade."
                        )
                        return False
                    logger.warning(f"[MDE BOOTSTRAP] Retry {attempt}/{max_retries} seeding {s} {tf} after error: {e}")
                    self.metrics["bootstrap_retry_count"] = self.metrics.get("bootstrap_retry_count", 0) + 1
                    await asyncio.sleep(2.0 ** attempt)
            return False

        async def execute_tier(tier_name: str, timeframes: set, concurrency: int) -> None:
            active_tfs = self._timeframes & timeframes
            if not active_tfs:
                return

            logger.info(f"[MDE BOOTSTRAP] Starting {tier_name} ({', '.join(active_tfs)}) with concurrency={concurrency}...")
            tier_start = time.time()
            semaphore = asyncio.Semaphore(concurrency)

            async def hydrate_with_sem(s: str, tf: str):
                wait_start = time.time()
                async with semaphore:
                    wait_time_ms = (time.time() - wait_start) * 1000
                    self.metrics["semaphore_wait_time_ms"] = self.metrics.get("semaphore_wait_time_ms", 0.0) + wait_time_ms
                    success = await staggered_seed(s, tf)
                    return (tf, s, success)  # Include symbol in return value

            tasks = []
            for tf in active_tfs:
                for s in self._watchlist:
                    tasks.append(asyncio.create_task(hydrate_with_sem(s, tf)))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Group results by timeframe and symbol
                success_by_tf_symbol = {tf: {} for tf in active_tfs}
                for res in results:
                    if isinstance(res, tuple) and len(res) == 3:
                        tf, s, success = res
                        if tf in success_by_tf_symbol:
                            success_by_tf_symbol[tf][s] = success

                for tf in active_tfs:
                    # [FIX SECONDARY] Account for unsupported symbols in readiness check
                    supported_symbols = {s for s in self._watchlist if timeframe_validator.is_timeframe_supported(s, tf)}
                    total = len(supported_symbols)
                    succeeded = sum(1 for s in supported_symbols if success_by_tf_symbol[tf].get(s, False))
                    if total > 0 and succeeded == total:
                        self._tf_ready[tf] = True
                        self._tf_seed_count[tf] = succeeded
                        logger.info(f"[MDE BOOTSTRAP] ✅ {tier_name} TF '{tf}' fully hydrated ({succeeded}/{total} supported symbols). Signals enabled.")
                    else:
                        self._tf_ready[tf] = False
                        self._tf_seed_count[tf] = succeeded
                        logger.warning(f"[MDE BOOTSTRAP] ⚠️ {tier_name} TF '{tf}' degraded ({succeeded}/{total} supported symbols).")

                tier_duration = time.time() - tier_start
                logger.info(f"[MDE BOOTSTRAP] {tier_name} completed in {tier_duration:.2f}s.")

                # Wait for stabilization before next tier
                await self._adaptive_stabilization()

        # Phase 1: Tier 1 (Execution-Critical, Sequential or Low Concurrency)
        await execute_tier("Tier 1", self.TIER1_TIMEFRAMES, concurrency=3)

        # Phase 2: Tier 2 (Primary Context, Bounded parallelism)
        await execute_tier("Tier 2", self.TIER2_TIMEFRAMES, concurrency=2)

        # Phase 3: Tier 3 (Secondary Context, Sequential)
        await execute_tier("Tier 3", self.TIER3_TIMEFRAMES, concurrency=1)

        # Phase 4: Tier 4 (Macro Context, Background Low-Priority)
        deferred_tfs = self._timeframes & self.TIER4_TIMEFRAMES
        for tf in deferred_tfs:
            for s in list(self._watchlist):
                # We wrap staggered_seed in a task that doesn't block
                async def bg_hydrate(sym=s, timeframe=tf):
                    success = await staggered_seed(sym, timeframe)
                    if success:
                        self._tf_seed_count[timeframe] += 1
                        if self._tf_seed_count[timeframe] == len(self._watchlist):
                            self._tf_ready[timeframe] = True
                            logger.info(f"[MDE BOOTSTRAP] ✅ Background TF '{timeframe}' fully hydrated. Signals enabled.")
                run_safe_task(bg_hydrate(), self._background_tasks)

        if deferred_tfs:
            logger.info(f"[MDE BOOTSTRAP] ⏳ Tier 4 ({', '.join(deferred_tfs)}) deferred to background.")

        total_duration = time.time() - start_time
        logger.info(f"[MDE BOOTSTRAP] Progressive Hydration initialized in {total_duration:.2f}s.")

    async def _seed_historical_data(self, symbol: str, timeframe: str, is_bootstrap: bool = False) -> None:
        """Fetch historical data to seed buffer and update readiness counters.
        
        Args:
            is_bootstrap: Nếu True — đang trong cold start, bỏ qua jitter sleep để
                          giảm thời gian khởi động. Token Bucket vẫn giữ nguyên.
        """
        seed_key = (symbol, timeframe)
        key = f"{symbol}_{timeframe}"

        # PHASE 3 - HISTORICAL SEED COALESCING: If seed is already in progress, return existing task
        if seed_key in self._seed_in_progress:
            logger.debug(f"[SEED-COALESCING] Seed already in progress for {key}, reusing existing task")
            await self._seed_in_progress[seed_key]
            return

        # Create task and track it
        task = asyncio.create_task(self._execute_seed(symbol, timeframe, is_bootstrap))
        self._seed_in_progress[seed_key] = task
        try:
            await task
        finally:
            # Cleanup after task completes or fails
            self._seed_in_progress.pop(seed_key, None)

    async def _execute_seed(self, symbol: str, timeframe: str, is_bootstrap: bool = False) -> None:
        """Internal method to execute the actual seed operation with coalescing guards."""
        key = f"{symbol}_{timeframe}"

        # [PHASE 8] REST Fallback Cooldown Guard
        now = time.time()
        last_seed = self._last_seed_ts.get(key, 0)
        if now - last_seed < 300:
            self.metrics["rest_seed_cooldown_hit_total"] = self.metrics.get("rest_seed_cooldown_hit_total", 0) + 1
            logger.debug(f"[FALLBACK GUARD] Skipping seed for {key} - Cooldown active ({300 - (now - last_seed):.1f}s left).")
            return

        self._last_seed_ts[key] = now

        try:
            # [PHASE 8] Throttled Concurrency
            # [FIX 2] Skip jitter khi is_bootstrap=True:
            #   - Cold start: Token Bucket đã giới hạn 4.9 req/s rồi, jitter thừa chức
            #   - Runtime fallback: Vẫn giữ jitter (0.5-2s) để chống burst khi nối lại
            async with self._rest_fallback_semaphore:
                if not is_bootstrap:
                    jitter = random.uniform(0.5, 2.0)
                    await asyncio.sleep(jitter)

                try:
                    watermark = 0
                    if key in self.buffers:
                        buf = self.buffers[key]
                        watermark = max(buf.high_watermark_ts, getattr(buf, '_last_candle_timestamp', 0))

                    kwargs = {"limit": 1440}  # [CÁCH 2] Lấy 1440 nến (để EMA hội tụ chính xác 100% như OKX)
                    if watermark > 0:
                        kwargs["since"] = watermark + 1

                    # [FIX 1] Tăng timeout nội từ 10s → 60s:
                    # fetch_ohlcv(limit=1440) thực hiện 15 pagination requests, cần đủ buffer
                    seed_timeout = 60.0 if is_bootstrap else 30.0
                    log_prefix = "[BOOTSTRAP]" if is_bootstrap else "[FALLBACK]"
                    candles = await asyncio.wait_for(
                        self.exchange.fetch_ohlcv(symbol, timeframe, **kwargs),
                        timeout=seed_timeout
                    )
                except asyncio.TimeoutError:
                    seed_timeout_used = 60.0 if is_bootstrap else 10.0
                    logger.warning(
                        f"⚠️ SEED TIMEOUT: {symbol} {timeframe} — seeding thất bại sau {seed_timeout_used:.0f}s, "
                        f"dùng 300 nến fallback hoặc chờ retry."
                    )
                    raise
                except Exception as e:
                    # Generic catch for CCXT/AIOHTTP 429 or rate limits
                    err_msg = str(e).lower()
                    if "429" in err_msg or "rate limit" in err_msg or "too many requests" in err_msg:
                        logger.error(f"[HTTP 429] Rate Limit Hit for {symbol} {timeframe}! Applying backoff and skipping reseed.")
                        self._last_seed_ts[key] = time.time() + 60 # Penalty 60s
                        return
                    raise

            # [PHASE 6] REST Fallback Timeline Validation:
            # Sort ascending, remove duplicate timestamps, reject older than watermark
            seen_ts: set = set()
            deduped: list = []
            for c in sorted(candles, key=lambda x: x.timestamp):
                if c.timestamp not in seen_ts:
                    seen_ts.add(c.timestamp)
                    deduped.append(c)

            # [FIX NGHẼN LUỒNG] Cô lập cục bộ
            async with self._buffer_locks[key]:
                if key in self.buffers:
                    buf = self.buffers[key]
                    watermark_before = buf.high_watermark_ts

                    # [PHASE 9] MANDATORY ROOT-CAUSE PATCH — ELIMINATE STALE HISTORICAL REPLAY
                    deduped = [c for c in deduped if c.timestamp > watermark_before]

                    if not deduped:
                        self.metrics["historical_fetch_replay_prevented_total"] = self.metrics.get("historical_fetch_replay_prevented_total", 0) + 1
                        logger.debug(
                            f"[TIMELINE-GUARD] Entire hydrate batch rejected "
                            f"for {symbol} {timeframe} due to watermark protection."
                        )
                        return

                    if deduped[-1].timestamp <= watermark_before:
                        self.metrics["historical_fetch_replay_prevented_total"] = self.metrics.get("historical_fetch_replay_prevented_total", 0) + 1
                        logger.debug(
                            f"[TIMELINE-GUARD] Skip stale hydrate "
                            f"{symbol} {timeframe} "
                            f"latest={deduped[-1].timestamp} "
                            f"watermark={watermark_before}"
                        )
                        return

                    # Clear buffer but watermark is preserved inside CandleBuffer
                    buf.clear()
                    rejected = 0
                    for candle in deduped:
                        # Pass reseed=True so watermark guard is applied inside CandleBuffer
                        added = buf.add_candle(candle, reseed=True)
                        if not added and candle.timestamp < watermark_before:
                            rejected += 1
                    if rejected:
                        self.metrics["historical_reseed_rejected_total"] = self.metrics.get("historical_reseed_rejected_total", 0) + rejected
                        logger.debug(
                            f"[TIMELINE] {rejected} historical candles rejected (older than watermark) "
                            f"during reseed for {symbol} {timeframe}."
                        )

            self._last_fetch[key] = datetime.now(timezone.utc)
            self.last_updated = time.time()
            self._last_rest_fetch_ts[timeframe] = time.time()  # Track REST data freshness
            self._data_source[timeframe] = "REST"  # Track data provenance
            logger.debug(f"Seeded {len(deduped)} candles for {symbol} {timeframe} (raw={len(candles)}) from REST")

            # Update deferred TF readiness counter (thread-safe: GIL protects int ops)
            if timeframe in self.TIER4_TIMEFRAMES:
                self._tf_seed_count[timeframe] = self._tf_seed_count.get(timeframe, 0) + 1
                if self._tf_seed_count[timeframe] >= len(self._watchlist):
                    if not self._tf_ready.get(timeframe, False):
                        self._tf_ready[timeframe] = True
                        logger.info(
                            f"[MDE] ✅ Deferred TF '{timeframe}' is now FULLY SEEDED. "
                            f"Signals for this timeframe are now enabled."
                        )

            # Compute indicators immediately after seeding
            await self._compute_and_publish_indicators(symbol, timeframe)

        except asyncio.CancelledError:
            logger.info(f"[FORENSIC] Worker cancelled safely {symbol} {timeframe} (_seed_historical_data)")
            raise
        except Exception as e:
            err_str = str(e)
            if "RetryError" in err_str or "ClientConnectorDNSError" in err_str:
                logger.warning(f"Lỗi kết nối khi seed data {symbol} {timeframe}, lên lịch retry sau 60s...")
                async def delayed_retry():
                    await asyncio.sleep(60)
                    await self._seed_historical_data(symbol, timeframe, is_bootstrap=is_bootstrap)
                
                run_safe_task(delayed_retry(), self._background_tasks)
            else:
                logger.error(f"Failed to seed historical data for {symbol} {timeframe}: {e}")
                raise

    async def _data_fetch_worker(self) -> None:
        """Background worker kiểm tra và fetch data mới định kỳ."""
        while self._running:
            fetch_tasks = []

            for symbol in self._watchlist:
                for timeframe in self._timeframes:
                    key = f"{symbol}_{timeframe}"
                    if timeframe not in self.TIMEFRAME_SECONDS:
                        logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
                        raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
                    tf_seconds = self.TIMEFRAME_SECONDS[timeframe]
                    last_fetch = self._last_fetch.get(
                        key, datetime.min.replace(tzinfo=timezone.utc)
                    )

                    # Chỉ fetch nếu đã đủ thời gian của timeframe
                    if datetime.now(timezone.utc) - last_fetch > timedelta(
                        seconds=tf_seconds * 0.8
                    ):
                        fetch_tasks.append(self._fetch_latest_candle(symbol, timeframe))

            if fetch_tasks:
                await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # Sleep 5s trước khi kiểm tra lại
            await asyncio.sleep(5)

    async def _fetch_latest_candle(self, symbol: str, timeframe: str) -> None:
        """Fetch và update nến mới nhất cho symbol+timeframe."""
        key = f"{symbol}_{timeframe}"
        try:
            async with self._fetch_semaphore:
                try:
                    watermark = 0
                    if key in self.buffers:
                        buf = self.buffers[key]
                        watermark = max(buf.high_watermark_ts, getattr(buf, '_last_candle_timestamp', 0))

                    kwargs = {"limit": 5}
                    if watermark > 0:
                        kwargs["since"] = watermark + 1

                    # Bọc luồng network bằng Timeout tuyệt đối 10s để chống Deadlock Semaphore
                    candles = await asyncio.wait_for(
                        self.exchange.fetch_ohlcv(symbol, timeframe, **kwargs),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[HARD TIMEOUT] Fetching latest candle for {symbol} {timeframe} took >10s. Lock released.")
                    return  # Trả về luôn, bỏ qua lần cập nhật này

            if not candles:
                return

            new_complete_candle = False

            # [FIX NGHẼN LUỒNG] Cô lập cục bộ
            async with self._buffer_locks[key]:
                if key in self.buffers:
                    buf = self.buffers[key]
                    # [PHASE 6] Sort ascending + dedup before injecting into buffer
                    seen_ts: set = set()
                    sorted_candles = []
                    for c in sorted(candles, key=lambda x: x.timestamp):
                        if c.timestamp not in seen_ts:
                            seen_ts.add(c.timestamp)
                            sorted_candles.append(c)

                    watermark_before = buf.high_watermark_ts

                    # [PHASE 9] MANDATORY ROOT-CAUSE PATCH — ELIMINATE STALE HISTORICAL REPLAY
                    sorted_candles = [c for c in sorted_candles if c.timestamp > watermark_before]

                    if not sorted_candles:
                        self.metrics["historical_fetch_replay_prevented_total"] = self.metrics.get("historical_fetch_replay_prevented_total", 0) + 1
                        logger.debug(
                            f"[TIMELINE-GUARD] Entire hydrate batch rejected "
                            f"for {symbol} {timeframe} due to watermark protection."
                        )
                        return

                    if sorted_candles[-1].timestamp <= watermark_before:
                        self.metrics["historical_fetch_replay_prevented_total"] = self.metrics.get("historical_fetch_replay_prevented_total", 0) + 1
                        logger.debug(
                            f"[TIMELINE-GUARD] Skip stale hydrate "
                            f"{symbol} {timeframe} "
                            f"latest={sorted_candles[-1].timestamp} "
                            f"watermark={watermark_before}"
                        )
                        return

                    for candle in sorted_candles:
                        # reseed=True: watermark guard applied inside CandleBuffer
                        added = buf.add_candle(candle, reseed=True)
                        if added:
                            new_complete_candle = True

            self._last_fetch[key] = datetime.now(timezone.utc)

            # Nếu có nến mới hoàn thành, publish event và tính lại indicators
            if new_complete_candle:
                await self._on_new_complete_candle(symbol, timeframe, candles[-1])
                await self._compute_and_publish_indicators(symbol, timeframe)

        except asyncio.CancelledError:
            logger.info(f"[FORENSIC] Worker cancelled safely {symbol} {timeframe} (_fetch_latest_candle)")
            raise
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Network error fetching latest candle for {symbol} {timeframe}: {e}")
        except ValueError as e:
            logger.error(f"Validation error fetching latest candle for {symbol} {timeframe}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching latest candle for {symbol} {timeframe}: {e}", exc_info=True)

    async def _handle_ws_candle(self, event: Event) -> None:
        """
        Producer part of the pipeline. Puts events from the EventBus into the internal queue.
        Also responsible for Dead Consumer Detection.
        """
        # --- Dead Consumer Detection ---
        if self._consumer_task and self._consumer_task.done():
            # Check for exception to provide more context
            exc = self._consumer_task.exception()
            logger.critical(
                f"FATAL: MDE consumer task is dead! Exception: {exc}. "
                "Stopping producer to prevent memory leak. Bot is now in a degraded state."
            )
            # In a real scenario, we might want to trigger a system-wide shutdown or restart.
            # For now, we just stop accepting new data.
            return

        # --- Add to queue and update metrics ---
        await self._queue.put(event)
        self._produced_count += 1

        # Update peak queue size
        qsize = self._queue.qsize()
        if qsize > self._peak_queue_size:
            self._peak_queue_size = qsize

    async def _on_new_complete_candle(self, symbol: str, timeframe: str, candle: OHLCV) -> None:
        """Xử lý khi có một nến mới hoàn toàn."""
        logger.info(
            f"New complete candle: {symbol} {timeframe} | Open: {candle.open} Close: {candle.close}"
        )
        # Publish event cho các components khác nghe
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.MARKET_NEW_CANDLE,  # FIX #3: Dùng EventTopic thay raw string
                data={"symbol": symbol, "timeframe": timeframe, "candle": candle},
                source="market_data_engine",
            )
        )

        # Kiểm tra Volatility Alert (Khung 5m)
        if timeframe == "5m":
            await self._check_volatility_alert(symbol, candle)

    async def _check_volatility_alert(self, symbol: str, candle: OHLCV) -> None:
        """Kiểm tra và bắn event nếu có biến động giá & khối lượng đột biến theo nến 5m (Dynamic Tiers)."""
        key = f"{symbol}_5m"
        async with self._buffer_locks[key]:
            buffer = self.buffers.get(key)
            if not buffer or len(buffer.candles) < 21:
                return
            # Lấy 20 nến trước đó để tính SMA(Volume, 20) bằng List Comprehension để tiết kiệm RAM/CPU
            length = len(buffer.candles)
            prev_candles = [buffer.candles[i] for i in range(length - 21, length - 1)]

        sma_vol = sum(c.volume for c in prev_candles) / 20
        vol_spike_factor = candle.volume / sma_vol if sma_vol > 0 else 0

        # Tính Price Change %
        change_pct = (candle.close - candle.open) / candle.open * 100
        abs_change = abs(change_pct)

        # Lấy ngưỡng động cho 5m
        threshold_5m, _ = self._get_volatility_thresholds(symbol)

        if abs_change >= threshold_5m and vol_spike_factor > 2.0:
            logger.warning(
                f"VOLATILITY ALERT: {symbol} changed {change_pct:+.2f}% in 5m, Vol Spike: {vol_spike_factor:.1f}x (threshold: {threshold_5m}%)"
            )
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.MARKET_VOLATILITY_ALERT,
                    data={
                        "symbol": symbol,
                        "change_pct": change_pct,
                        "timeframe": "5 phút",
                        "current_price": candle.close,
                        "volume_spike_factor": vol_spike_factor,
                    },
                    source="market_data_engine",
                )
            )

    async def _compute_and_publish_indicators(self, symbol: str, timeframe: str) -> None:
        """Tính indicators và publish event kèm atomic candle snapshot."""
        key = f"{symbol}_{timeframe}"
        candles_snapshot: List[OHLCV] = []
        async with self._buffer_locks[key]:
            buffer = self.buffers.get(key)
            if not buffer:
                return
            closes = buffer.get_close_prices(500)
            highs = buffer.get_high_prices(500)
            lows = buffer.get_low_prices(500)
            candles_tuple = buffer.get_candles(500)
            for frozen in buffer.get_candles(250):
                candles_snapshot.append(
                    OHLCV(
                        timestamp=frozen.timestamp,
                        open=frozen.open,
                        high=frozen.high,
                        low=frozen.low,
                        close=frozen.close,
                        volume=frozen.volume,
                        symbol=symbol,
                        timeframe=timeframe,
                        confirmed=True,
                    )
                )

        class MockBuffer(CandleBuffer):
            def get_close_prices(self, limit: int = 100) -> list[float]:
                return closes[-limit:] if limit < len(closes) else closes

            def get_high_prices(self, limit: int = 100) -> list[float]:
                return highs[-limit:] if limit < len(highs) else highs

            def get_low_prices(self, limit: int = 100) -> list[float]:
                return lows[-limit:] if limit < len(lows) else lows

            def get_candles(self, limit: int = 100):
                return candles_tuple[-limit:] if limit < len(candles_tuple) else candles_tuple

        # --- FORENSIC FIX: SUPPORT BOTH MODES - 0=Realtime (forming candle), >=1=Confirmation (closed candle) ---
        # Use per-timeframe configuration from settings, no forced hardcoded mode
        from core.config.settings import settings
        timeframe_confirmation_map = {
            "5m": settings.confirmation_candles_5m,
            "15m": settings.confirmation_candles_15m,
            "1H": settings.confirmation_candles_1h,
            "4H": settings.confirmation_candles_4h,
            "1D": settings.confirmation_candles_1d,
            "1W": settings.confirmation_candles_1w,
            "1M": settings.confirmation_candles_1m,
        }
        if timeframe not in timeframe_confirmation_map:
            logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
            raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
        confirm_candles = timeframe_confirmation_map[timeframe]
        
        # --- CENTRALIZED SIGNAL GATE VALIDATION (BEFORE compute_indicators()) ---
        if confirm_candles < 0:
            raise RuntimeError(f"INVALID_CONFIRMATION_CANDLES: {confirm_candles} for {symbol}/{timeframe}. Must be >=0")
        
        # Calculate required candles based on mode: 0=realtime (need at least 1 candle), >=1=confirmation (need at least 2)
        min_required_candles = 1 if confirm_candles == 0 else 2
        if len(candles_tuple) < min_required_candles:
            logger.warning(f"INSUFFICIENT_CANDLES: {len(candles_tuple)} for {symbol}/{timeframe}, need at least {min_required_candles} candles for mode confirm_candles={confirm_candles}")
            return None
        
        # For realtime mode (confirm_candles=0): use last candle (forming) if available, otherwise use last closed
        # For confirmation mode (confirm_candles>=1): always use candles[-2] (last closed candle)
        reference_index = -1 if confirm_candles == 0 else -2
        reference_candle = candles_tuple[reference_index]
        
        # Validate reference candle state based on mode
        if confirm_candles >= 1 and hasattr(reference_candle, 'is_closed') and not reference_candle.is_closed:
            raise RuntimeError(f"INVALID_CANDLE_STATE: Reference candle is not closed. Cannot process forming candles for confirmation mode. {symbol}/{timeframe}")
        
        # Log active mode for debugging
        mode_name = "REALTIME" if confirm_candles == 0 else "CONFIRMATION"
        logger.info(f"[{mode_name}-MODE] symbol={symbol}, timeframe={timeframe}, confirm_candles={confirm_candles}, reference_candle_index={reference_index}, candle_type={'forming' if confirm_candles ==0 else 'closed'}")
        
        # DEBUG: Log input data before MockBuffer
        logger.debug(f"[DEBUG-TRACE] Before MockBuffer: symbol={symbol}, timeframe={timeframe}")
        logger.debug(f"[DEBUG-TRACE] candles_tuple type={type(candles_tuple)}, len={len(candles_tuple)}")
        if len(candles_tuple) > 0:
            logger.debug(f"[DEBUG-TRACE] candles_tuple[-1] type={type(candles_tuple[-1])}, repr={repr(candles_tuple[-1])}")
        logger.debug(f"[DEBUG-TRACE] closes len={len(closes)}, highs len={len(highs)}, lows len={len(lows)}")
        
        mock_buffer = MockBuffer(symbol, timeframe)
        snapshot = self.indicator_pipeline.compute_indicators(mock_buffer, confirmation_candles=confirm_candles, reference_candle_index=reference_index)
        
        # DEBUG: Log snapshot creation status
        if not snapshot:
            logger.debug(f"[DEBUG] snapshot is None for {symbol}/{timeframe}")
        elif snapshot.reference_candle_timestamp == 0:
            logger.debug(f"[DEBUG] snapshot.reference_candle_timestamp = 0 for {symbol}/{timeframe}, reference_candle_index={snapshot.reference_candle_index}, candle_type={snapshot.candle_type}")
            logger.debug(f"[DEBUG] candles_tuple length = {len(candles_tuple)}, closes length = {len(closes)}, highs length = {len(highs)}, lows length = {len(lows)}")
        
        if snapshot and snapshot.reference_candle_timestamp > 0:
            # PHASE 4 - SNAPSHOT CONSISTENCY CHECK: Validate snapshot integrity
            snapshot_key = (symbol, timeframe)
            if not snapshot.validate_consistency():
                self._snapshot_integrity_errors[snapshot_key] = self._snapshot_integrity_errors.get(snapshot_key, 0) + 1
                logger.error(
                    f"[SNAPSHOT-INTEGRITY] Corrupt snapshot detected for {symbol}/{timeframe}! "
                    f"Errors: {self._snapshot_integrity_errors[snapshot_key]}, snapshot_id: {snapshot.snapshot_id}"
                )
                # Skip publishing corrupt snapshot
                return
            
            # PHASE 4 - ATOMIC SNAPSHOT SWAP: Update candles snapshot only after snapshot is valid
            self._latest_candle_snapshots[key] = list(candles_snapshot)
            self._last_valid_snapshot[snapshot_key] = snapshot.snapshot_timestamp
            
            # Extract indicators from snapshot for backward compatibility
            indicators = snapshot.indicators
            self.indicator_pipeline.indicator_cache[key] = indicators
            logger.debug(f"[SNAPSHOT-VALID] Valid snapshot published for {symbol}/{timeframe}: {snapshot.snapshot_id}")

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.MARKET_INDICATORS_UPDATED,
                    data={
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "indicators": indicators,
                        "candles_snapshot": candles_snapshot,
                        "buffer_seq": candles_snapshot[-1].timestamp if candles_snapshot else 0,
                        "signal_candle_ts": snapshot.reference_candle_timestamp,
                        "snapshot_id": snapshot.snapshot_id,
                        "snapshot_timestamp": snapshot.snapshot_timestamp,
                        "reference_candle_index": snapshot.reference_candle_index,
                        "candle_type": snapshot.candle_type,
                    },
                    source="market_data_engine",
                )
            )

    def get_candles_snapshot(self, symbol: str, timeframe: str, limit: int = 50) -> List[OHLCV]:
        """Return latest published candle snapshot (atomic, lock-free read)."""
        key = f"{symbol}_{timeframe}"
        snap = self._latest_candle_snapshots.get(key)
        if snap:
            return list(snap)[-limit:]
        buffer = self.buffers.get(key)
        if not buffer:
            return []
        return [
            OHLCV(
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                symbol=symbol,
                timeframe=timeframe,
                confirmed=True,
            )
            for c in buffer.get_candles(limit)
        ]

    async def _handle_symbol_added(self, event: Event) -> None:
        """Xử lý khi có symbol mới được thêm vào watchlist."""
        symbol = event.data.get("symbol")
        if not symbol or symbol in self._watchlist:
            return

        logger.info(f"Adding new symbol to watchlist: {symbol}")
        self._watchlist.add(symbol)

        # Tạo buffers cho tất cả timeframes
        for timeframe in self._timeframes:
            key = f"{symbol}_{timeframe}"
            async with self._buffer_locks[key]:
                if key not in self.buffers:
                    self.buffers[key] = CandleBuffer(symbol, timeframe)
            run_safe_task(
                self._seed_historical_data(symbol, timeframe), self._background_tasks
            )

    async def _handle_ws_reconnected(self, event: Event) -> None:
        """Handles websocket reconnected event to track metrics."""
        self.metrics["ws_reconnect_total"] = self.metrics.get("ws_reconnect_total", 0) + 1
        logger.info(f"[FORENSIC] WebSocket reconnected. Total reconnects: {self.metrics.get('ws_reconnect_total', 0)}")

    async def _handle_reset_buffers(self, event: Event) -> None:
        """Handle buffer reset request.

        FIX: Split into two phases to avoid asyncio.Lock deadlock.
        Phase-1 (lock held): clear + pre-create buffer slots.
        Phase-2 (lock released): gather seed tasks independently.
        This is safe because _seed_historical_data acquires the same lock
        internally — holding it during gather would deadlock.
        """
        # [PHASE 8] Scoped Reset
        target_tf = event.data.get("timeframe") if event.data else None
        tf_list = [target_tf] if target_tf and target_tf in self._tf_ready else list(self._tf_ready.keys())

        logger.warning(f"[MDE] Received reset_buffers request for {target_tf or 'ALL'}. Clearing and re-seeding...")

        # Reset readiness state so strategies wait for fresh data
        for tf in tf_list:
            self._tf_ready[tf] = False
            self._tf_seed_count[tf] = 0

        # [FIX P9] Clear dedup cache so REST-seeded candles are not mistakenly
        # dropped as "duplicates" of the old WS candles that were just cleared.
        if not target_tf:
            self._processed_candles.clear()

        # Reset stream health so watchdog starts fresh
        for tf in tf_list:
            self.stream_health[tf] = "HEALTHY"
            self._last_receive_ts[tf] = time.time()
            self._fallback_active[tf] = False
        logger.info(f"[MDE] Dedup cache and stream health reset for {target_tf or 'ALL'}.")

        # Phase-1: Clear + pre-create (lock scope ends here)
        for symbol in self._watchlist:
            for timeframe in tf_list:
                key = f"{symbol}_{timeframe}"
                async with self._buffer_locks[key]:
                    self.buffers[key] = CandleBuffer(symbol, timeframe)
                    self._latest_candle_snapshots.pop(key, None)

        # Phase-2: Seed outside lock (each task acquires lock internally)
        # PHASE 3: Seed coalescing - _seed_historical_data will handle reusing existing tasks
        async def staggered_seed(s: str, tf: str) -> None:
            await self._seed_historical_data(s, tf)
            await asyncio.sleep(0.2)

        init_tasks = [
            staggered_seed(s, tf)
            for s in self._watchlist
            for tf in tf_list
        ]
        await asyncio.gather(*init_tasks, return_exceptions=True)
        logger.info("[MDE] ✅ All buffers reset and re-seeded. Bot đang dùng dữ liệu MỚI.")

    def get_indicators(self, symbol: str, timeframe: str) -> Dict[str, float]:
        """Lấy indicators gần nhất cho symbol+timeframe."""
        cache_key = f"{symbol}_{timeframe}"
        return self.indicator_pipeline.indicator_cache.get(cache_key, {})

    def get_candle_buffer(self, symbol: str, timeframe: str) -> Optional[CandleBuffer]:
        """Return live buffer reference. Prefer get_candles_snapshot() for strategy reads."""
        key = f"{symbol}_{timeframe}"
        return self.buffers.get(key)

    def is_timeframe_ready(self, timeframe: str) -> bool:
        """Check if a specific timeframe has finished background seeding."""
        return self._tf_ready.get(timeframe, False)

    async def _ws_heartbeat_watchdog(self) -> None:
        """Phát hiện WS im lặng tuyệt đối và trigger REST fallback."""
        import random
        while self._running:
            # Check every 5s with small jitter to avoid synchronized bursts
            await asyncio.sleep(5 + random.random() * 0.5)

            now_ms = int(time.time() * 1000)
            for symbol in list(self._watchlist):
                for timeframe in list(self._timeframes):
                    # Chỉ monitor timeframes ngắn (không check 4H, 1D, 1W, 1M)
                    if timeframe not in self.TIMEFRAME_SECONDS:
                        logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
                        raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
                    tf_seconds = self.TIMEFRAME_SECONDS[timeframe]
                    if tf_seconds > 3600:
                        continue

                    key = f"{symbol}_{timeframe}"
                    async with self._buffer_locks[key]:
                        last_ts = self._last_candle_ts.get(key, 0)

                    silence_ms = now_ms - last_ts if last_ts > 0 else 0

                    # Nếu im lặng > 3x timeframe -> trigger REST fallback
                    if last_ts > 0 and silence_ms > tf_seconds * 3 * 1000:
                        logger.warning(
                            f"[WS WATCHDOG] {symbol} {timeframe} silent for "
                            f"{silence_ms/1000:.0f}s (>{tf_seconds*3}s). REST fallback."
                        )
                        # Reset last_ts to avoid spamming
                        self._last_candle_ts[key] = now_ms
                        run_safe_task(
                            self._seed_historical_data(symbol, timeframe),
                            self._background_tasks
                        )

                    # [BLIND SPOT GUARD] Bịt điểm mù khi _last_candle_ts = 0:
                    # Nếu TF chưa sẵn sàng VÀ lần seed cuối > 300s → kick reseed ngay
                    elif not self._tf_ready.get(timeframe, False):
                        last_seed = self._last_seed_ts.get(key, 0)
                        now_s = time.time()
                        if now_s - last_seed > 300:
                            logger.warning(
                                f"[WS WATCHDOG] [BLIND SPOT] {symbol} {timeframe} "
                                f"tf_ready=False, last_seed={now_s - last_seed:.0f}s ago. "
                                f"Kicking reseed..."
                            )
                            self._last_seed_ts[key] = 0  # Reset để bypass cooldown guard
                            run_safe_task(
                                self._seed_historical_data(symbol, timeframe),
                                self._background_tasks
                            )