import time
import contextlib
from collections import deque, OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger
from dataclasses import dataclass
from core.event_bus import EventBus, Event
from core.events.topics import EventTopic
import asyncio
from infrastructure.exchange.okx_exchange import OKXExchange
import hashlib

@dataclass(frozen=True)
class MirrorPosition:
    instId: str
    pos: float
    avgPx: float
    upl: float
    uplLastPx: float
    uplRatio: float
    margin: float
    markPx: float
    liqPx: float
    cTime: int
    uTime: int
    tpTriggerPx: Optional[float] = None
    slTriggerPx: Optional[float] = None
    tpPrices: Tuple[float, ...] = ()

@dataclass(frozen=True)
class MirrorAccount:
    totalEq: float
    availEq: float
    uTime: int

class ExchangeMirrorCache:
    """Read-only cache that mirrors the exact state of OKX."""

    def __init__(self, event_bus: EventBus, exchange: OKXExchange):
        self.event_bus = event_bus
        self.exchange = exchange
        self._positions: Dict[str, dict] = {}
        self._account: dict = {}

        # Concurrency and Idempotency
        self._sync_lock = asyncio.Lock()
        self._is_syncing = False
        self._start_time = time.time()  # Lưu thời gian khởi tạo để kiểm tra timeout snapshot
        self._last_resync_failed = False  # [ISSUE #3 FIX] Track resync failure state
        self._initial_snapshot_received = False
        self._processed_events: OrderedDict = OrderedDict()  # Bounded LRU for deduplication

        # Metrics
        self.metrics = {
            "duplicate_events_dropped": 0,
            "stale_events_dropped": 0,
            "reconnect_resync_count": 0,
            "replay_buffer_size": 0,
            "replayed_event_count": 0,
            "stale_event_drop_count": 0,
            "replay_overflow_count": 0,
            "reconnect_burst_total": 0,
            "resync_deduplicated_total": 0,
            "atomic_resync_duration_ms": 0.0,
            "reconnect_coalesced_total": 0,
        }
        # Use deque with maxlen to prevent unbounded replay buffer growth
        self._replay_buffer: deque = deque(maxlen=5000)
        self._max_buffer_size = 5000
        self._audit_task = None
        self._resync_task: Optional[asyncio.Task] = None
        self._resync_generation = 0

    def start(self):
        """Start listening to exchange events."""
        if self._audit_task is not None:
            return
        self.event_bus.subscribe(self._handle_raw_position, [EventTopic.WS_RAW_POSITION], handler_id="mirror_pos")
        self.event_bus.subscribe(self._handle_raw_account, [EventTopic.WS_RAW_ACCOUNT], handler_id="mirror_acc")
        self.event_bus.subscribe(self._handle_ws_reconnect, [EventTopic.WS_RECONNECTED], handler_id="mirror_reconnect")

        # Start periodic audit
        self._audit_task = asyncio.create_task(self._periodic_audit())
        logger.info("ExchangeMirrorCache started")

    def stop(self):
        """Stop listening to exchange events."""
        self.event_bus.unsubscribe("mirror_pos")
        self.event_bus.unsubscribe("mirror_acc")
        self.event_bus.unsubscribe("mirror_reconnect")
        if self._resync_task and not self._resync_task.done():
            self._resync_task.cancel()
        self._resync_task = None
        self._is_syncing = False
        self._replay_buffer.clear()
        if self._audit_task:
            self._audit_task.cancel()
            self._audit_task = None

    async def _release_syncing_state(self, reason: str = "") -> None:
        async with self._sync_lock:
            if self._is_syncing:
                self._is_syncing = False
                if reason:
                    logger.debug(f"[MIRROR] Cleared _is_syncing: {reason}")

    async def _debounced_resync_runner(self, generation: int) -> None:
        try:
            logger.info("[MIRROR] Cache đã bị KHÓA. Waiting 2 seconds (debounce) before REST snapshot resync...")
            await asyncio.sleep(2)
            if generation != self._resync_generation:
                return
            await self._run_atomic_resync()
        except asyncio.CancelledError:
            if generation == self._resync_generation:
                await self._release_syncing_state("debounced resync cancelled")
            raise
        except Exception as e:
            if generation == self._resync_generation:
                await self._release_syncing_state(f"debounced resync failed: {e}")
            raise

    async def _periodic_audit(self):
        while True:
            await asyncio.sleep(300)  # Every 5 minutes
            self._print_audit_summary()

    def _print_audit_summary(self):
        logger.info(
            f"[MIRROR AUDIT] Active Pos: {len(self._positions)} | "
            f"Stale Dropped: {self.metrics['stale_events_dropped']} | "
            f"Dup Dropped: {self.metrics['duplicate_events_dropped']} | "
            f"Resyncs: {self.metrics['reconnect_resync_count']}"
        )

    def is_consistent(self) -> bool:
        """Return True when an initial atomic snapshot has been received and last resync did not fail."""
        return (not self._last_resync_failed) and self._initial_snapshot_received

    def _is_duplicate_or_stale(self, event_type: str, data: dict, instId: str = "") -> bool:
        new_uTime = int(data.get("uTime", 0))
        if new_uTime == 0:
            return False

        # Generate event hash for strict idempotency
        content_hash = hashlib.md5(str(data).encode()).hexdigest()
        cache_key = f"{event_type}:{instId}:{new_uTime}"

        if cache_key in self._processed_events:
            if self._processed_events[cache_key] == content_hash:
                self.metrics["duplicate_events_dropped"] += 1
                return True

        self._processed_events[cache_key] = content_hash

        # Bounded LRU: evict oldest entries when over 1000
        if len(self._processed_events) > 1000:
            while len(self._processed_events) > 800:
                self._processed_events.popitem(last=False)

        return False

    async def _handle_ws_reconnect(self, event: Event) -> None:
        """Trigger atomic resync on reconnect with anti-stampede deduplication."""
        self.metrics["reconnect_burst_total"] = self.metrics.get("reconnect_burst_total", 0) + 1

        async with self._sync_lock:
            if self._is_syncing:
                self.metrics["resync_deduplicated_total"] = self.metrics.get("resync_deduplicated_total", 0) + 1
                self.metrics["reconnect_coalesced_total"] = self.metrics.get("reconnect_coalesced_total", 0) + 1
                logger.debug("[MIRROR] Reconnect ignored: resync already active (coalesced)")
                return

            logger.info("[MIRROR] WS Reconnect detected. Triggering atomic REST snapshot sync...")
            self.metrics["reconnect_resync_count"] += 1
            self._print_audit_summary()
            self._is_syncing = True

        if self._resync_task and not self._resync_task.done():
            self._resync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._resync_task

        self._resync_generation += 1
        generation = self._resync_generation
        self._resync_task = asyncio.create_task(self._debounced_resync_runner(generation))

    async def _run_atomic_resync(self) -> None:
        # Lưu ý: _is_syncing = True đã được bật từ _handle_ws_reconnect.
        # Hàm này chỉ cần tiếp quản lock và thực hiện REST snapshot.
        start_ms = time.time() * 1000
        async with self._sync_lock:
            # Guard: nếu một resync khác đã hoàn thành trước khi task này chạy
            if not self._is_syncing:
                return
            try:
                # Exponential backoff for REST sync
                retry_count = 0
                while retry_count < 5:
                    try:
                        logger.info(f"[MIRROR] Fetching REST snapshot (Attempt {retry_count + 1})...")
                        equity = await self.exchange.fetch_account_equity()
                        account_data = None
                        if not equity:
                            account_data = await self.exchange.fetch_balance()
                        # Keep full OKX raw payload (closeOrderAlgo, lever, notionalUsd, ...)
                        pos_response = await self.exchange._request("GET", "/api/v5/account/positions")
                        positions_raw = pos_response.get("data", []) if pos_response else []

                        # Atomic Overwrite
                        new_positions = {}
                        for pos in positions_raw:
                            try:
                                pos_sz = float(str(pos.get("pos") or "0"))
                            except (TypeError, ValueError):
                                pos_sz = 0.0
                            if abs(pos_sz) <= 0:
                                continue
                            instId = pos.get("instId")
                            if instId:
                                new_positions[instId] = pos

                        # Account data mapping — use OKX account-level totalEq/availEq
                        formatted_account = {}
                        if equity:
                            formatted_account = {
                                "totalEq": str(equity.get("totalEq", 0.0)),
                                "availEq": str(equity.get("availEq", 0.0)),  # Fixed: was incorrectly mapped to adjEq
                                "uTime": str(int(time.time() * 1000)),
                            }
                        elif account_data:
                            usdt_bal = account_data.get("USDT")
                            if usdt_bal:
                                formatted_account = {
                                    "totalEq": str(usdt_bal.total),
                                    "adjEq": str(usdt_bal.free),
                                    "uTime": str(int(time.time() * 1000)),
                                }
                            else:
                                first_bal = list(account_data.values())[0] if account_data else None
                                if first_bal:
                                    formatted_account = {
                                        "totalEq": str(first_bal.total),
                                        "adjEq": str(first_bal.free),
                                        "uTime": str(int(time.time() * 1000)),
                                    }

                        # Enrich TP/SL from pending algo orders (OKX often stores them separately)
                        try:
                            from services.position.tpsl_resolver import (
                                build_algo_tpsl_map,
                                enrich_raw_position_dict,
                            )

                            algo_map = await build_algo_tpsl_map(self.exchange)
                            for instId, raw in new_positions.items():
                                enrich_raw_position_dict(raw, algo_map.get(instId))
                        except Exception as enrich_err:
                            logger.debug(f"[MIRROR] Algo TP/SL enrich skipped: {enrich_err}")

                        self._positions = new_positions
                        if formatted_account:
                            self._account = formatted_account
                        # [ISSUE #3 FIX] Clear failure flag on successful resync
                        self._last_resync_failed = False
                        self._initial_snapshot_received = True
                        logger.info(f"[MIRROR] Atomic Resync Success! {len(self._positions)} positions active.")
                        # Notify system that mirror recovered
                        try:
                            from core.events.topics import EventTopic
                            await self.event_bus.publish(Event(
                                event_type=EventTopic.MIRROR_RESYNC_SUCCESS,
                                data={"positions": len(self._positions)},
                                source="exchange_mirror",
                            ))
                        except Exception as pub_err:
                            print(f"Failed to publish MIRROR_RESYNC_SUCCESS: {pub_err}")
                            logger.debug(f"[MIRROR] Failed to publish MIRROR_RESYNC_SUCCESS: {pub_err}")
                        break
                    except Exception as e:
                        retry_count += 1
                        logger.error(f"[MIRROR] REST snapshot fetch failed: {e}")
                        # [ISSUE #6 FIX] Add exponential backoff with upper bound cap
                        from core.config.settings import settings
                        base_delay = settings.RETRY_BASE_DELAY_SECONDS
                        max_delay = settings.RETRY_MAX_DELAY_SECONDS
                        wait_time = min(max_delay, base_delay * (2 ** retry_count))
                        logger.debug(f"[MIRROR] Retry {retry_count}/5: waiting {wait_time:.2f}s before next attempt")
                        await asyncio.sleep(wait_time)

                if retry_count == 5:
                    # [ISSUE #3 FIX] Set failure flag to indicate compromised state
                    self._last_resync_failed = True
                    logger.critical(
                        "[MIRROR] CRITICAL: Failed to resync state after 5 attempts! "
                        "Cache state is COMPROMISED - position data may be stale or incorrect."
                    )
                    # Emit MIRROR_RESYNC_FAILED event to notify dependent components
                    try:
                        await self.event_bus.publish(Event(
                            event_type=EventTopic.MIRROR_RESYNC_FAILED,
                            data={"reason": "Max retries exceeded", "retry_count": retry_count},
                            source="exchange_mirror"
                        ))
                    except Exception as evt_err:
                        logger.error(f"[MIRROR] Failed to publish MIRROR_RESYNC_FAILED event: {evt_err}")

                    # Do NOT replay buffered events on failed resync — state is unknown
                    self._replay_buffer.clear()
                    self.metrics["replay_buffer_size"] = 0
                    return

                # Only replay buffered events AFTER successful snapshot and WHILE still holding lock.
                # _is_syncing stays True during replay to prevent new live events from racing.
                buffered_events = list(self._replay_buffer)
                self._replay_buffer.clear()
                self.metrics["replay_buffer_size"] = 0

                if buffered_events:
                    logger.info(f"[MIRROR] Replaying {len(buffered_events)} buffered events after REST snapshot resync...")
                    for early_event in buffered_events:
                        self.metrics["replayed_event_count"] += 1
                        if early_event.event_type == EventTopic.WS_RAW_POSITION:
                            await self._handle_raw_position_internal(early_event)
                        elif early_event.event_type == EventTopic.WS_RAW_ACCOUNT:
                            await self._handle_raw_account_internal(early_event)
            finally:
                # Only clear the syncing flag AFTER all buffered events have been replayed.
                # This guarantees strict ordering: snapshot state → buffered events → live events.
                self._is_syncing = False
                end_ms = time.time() * 1000
                duration_ms = end_ms - start_ms
                self.metrics["atomic_resync_duration_ms"] = duration_ms
                logger.info(f"[MIRROR] Resync and replay complete in {duration_ms:.1f}ms. Live events now accepted.")

    async def _handle_raw_position(self, event: Event) -> None:
        if self._is_syncing:
            if len(self._replay_buffer) >= self._max_buffer_size:
                self.metrics["replay_overflow_count"] += 1
                logger.critical("[MIRROR] Replay buffer OVERFLOW! maxlen enforced by deque. Triggering full resync warning.")
                self._replay_buffer.popleft()  # Evict oldest to enforce max_buffer_size
            self._replay_buffer.append(event)
            self.metrics["replay_buffer_size"] = len(self._replay_buffer)
            return
        await self._handle_raw_position_internal(event)

    async def _handle_raw_position_internal(self, event: Event) -> None:
        """Internal handler that mutates position cache — called during live dispatch OR during replay."""
        data = event.data.get("data", {})
        instId = event.data.get("symbol") or data.get("instId")
        if not instId or not data:
            return

        if self._is_duplicate_or_stale("position", data, instId):
            return

        new_uTime = int(data.get("uTime", 0))
        
        # [FIX LỖI 1] Bọc thao tác đọc/ghi _positions bằng lock để tránh race condition
        async with self._sync_lock:
            cached = self._positions.get(instId)

            if cached and int(cached.get("uTime", 0)) > new_uTime:
                self.metrics["stale_events_dropped"] += 1
                self.metrics["stale_event_drop_count"] += 1
                return  # Ignore out-of-order event

            pos_str = str(data.get("pos", "0"))
            if pos_str == "0":
                if instId in self._positions:
                    del self._positions[instId]
            else:
                self._positions[instId] = data
            self._initial_snapshot_received = True

    async def _handle_raw_account(self, event: Event) -> None:
        if self._is_syncing:
            if len(self._replay_buffer) >= self._max_buffer_size:
                self.metrics["replay_overflow_count"] += 1
                logger.critical("[MIRROR] Replay buffer OVERFLOW! maxlen enforced by deque.")
            self._replay_buffer.append(event)
            self.metrics["replay_buffer_size"] = len(self._replay_buffer)
            return
        await self._handle_raw_account_internal(event)

    async def _handle_raw_account_internal(self, event: Event) -> None:
        """Internal handler that mutates account cache — called during live dispatch OR during replay."""
        data = event.data.get("data", {}) or event.data
        if not data:
            return

        if self._is_duplicate_or_stale("account", data):
            return

        new_uTime = int(data.get("uTime", 0))
        if self._account and int(self._account.get("uTime", 0)) > new_uTime:
            self.metrics["stale_events_dropped"] += 1
            self.metrics["stale_event_drop_count"] += 1
            return

        self._account = data
        self._initial_snapshot_received = True

    def is_snapshot_ready(self) -> bool:
        """True after at least one WS account/position snapshot (not mid-resync)."""
        # Auto-fix: nếu đã chạy quá 30s mà vẫn chưa có snapshot, bỏ qua cờ sync để không chặn vĩnh viễn
        if not self._initial_snapshot_received and time.time() - self._start_time > 30:
            logger.warning("[MIRROR] Snapshot timeout after 30s - disabling sync lock to allow trading")
            self._is_syncing = False
            self._initial_snapshot_received = True
            
        if self._is_syncing or self._last_resync_failed:
            return False
        return self._initial_snapshot_received

    def has_account_seed(self) -> bool:
        """True when mirror has received account balance data."""
        return self.is_snapshot_ready() and bool(self._account)

    def get_position(self, instId: str) -> Optional[MirrorPosition]:
        """Get an immutable snapshot of a position.

        TỬ HUYỆT BỊ KHÓA: Trả về None ngay lập tức nếu Cache đang bị khóa
        trong quá trình WS Reconnect Resync để ngăn RiskManager dùng dữ liệu lỗi thời.
        """
        # Chặn mọi lệnh đọc khi đang resync sau Reconnect
        if self._is_syncing:
            logger.warning(f"[MIRROR] get_position({instId}) BỊ CHẶN: Cache đang bị khóa trong quá trình resync.")
            # [FIX LỖI 3] Trả về None để tránh Crash AttributeError, RiskManager sẽ tự check cờ _is_syncing
            return None

        raw = self._positions.get(instId)
        if not raw:
            return None

        try:
            from services.position.tpsl_resolver import extract_tpsl_from_raw_position

            sl_px, tp_list = extract_tpsl_from_raw_position(raw)
            return MirrorPosition(
                instId=raw.get("instId", ""),
                pos=float(raw.get("pos", 0) or 0),
                avgPx=float(raw.get("avgPx", 0) or 0),
                upl=float(raw.get("upl", 0) or 0),
                uplLastPx=float(raw.get("uplLastPx", raw.get("upl", 0)) or 0),
                uplRatio=float(raw.get("uplRatio", 0) or 0),
                margin=float(raw.get("margin", 0) or 0),
                markPx=float(raw.get("markPx", 0) or 0),
                liqPx=float(raw.get("liqPx", 0) or 0),
                cTime=int(raw.get("cTime", 0) or 0),
                uTime=int(raw.get("uTime", 0) or 0),
                tpTriggerPx=tp_list[0] if tp_list else None,
                slTriggerPx=sl_px,
                tpPrices=tuple(tp_list),
            )
        except Exception as e:
            logger.error(f"Error parsing position data for {instId}: {e}")
            return None

    def get_all_positions(self) -> Dict[str, MirrorPosition]:
        """Get immutable snapshots of all positions.

        Trả về dict rỗng khi Cache đang bị khóa trong quá trình Resync.
        """
        if self._is_syncing:
            logger.warning("[MIRROR] get_all_positions() BỊ CHẶN: Cache đang bị khóa trong quá trình resync.")
            return {}
        result = {}
        for instId in self._positions:
            pos = self.get_position(instId)
            if pos:
                result[instId] = pos
        return result

    def get_account(self) -> Optional[MirrorAccount]:
        """Get an immutable snapshot of the account.

        Trả về None khi Cache đang bị khóa để ngăn RiskManager đọc margin lỗi thời.
        """
        if self._is_syncing:
            logger.warning("[MIRROR] get_account() BỊ CHẶN: Cache đang bị khóa trong quá trình resync.")
            return None

        if not self._account:
            return None

        try:
            # account data usually has details inside 'details' array, but 'totalEq' and 'availEq' are top-level
            details = self._account.get("details", [{}])[0]
            totalEq = float(self._account.get("totalEq", 0) or details.get("eq", 0) or 0)
            availEq = float(self._account.get("availEq", 0) or details.get("availEq", 0) or self._account.get("adjEq", 0) or 0)  # Fixed: prefer availEq, fallback to adjEq for compatibility

            return MirrorAccount(
                totalEq=totalEq,
                availEq=availEq,
                uTime=int(self._account.get("uTime", 0) or 0)
            )
        except Exception as e:
            logger.error(f"Error parsing account data: {e}")
            return None

    def get_total_balance(self) -> float:
        """Get the total balance of the mirrored account (fast-path cache accessor)."""
        acc = self.get_account()
        return acc.totalEq if acc else 0.0

    def get_free_margin(self) -> float:
        """Get the free margin of the mirrored account (fast-path cache accessor)."""
        acc = self.get_account()
        return acc.availEq if acc else 0.0

    def get_realized_pnl(self) -> float:
        """Get the total realized PnL from the mirrored cache."""
        return 0.0