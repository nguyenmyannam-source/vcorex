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
        self._resync_lock = asyncio.Lock() # Lock tổng thể cho quá trình resync
        self._account_lock = asyncio.Lock() # Lock cho account
        self._position_locks: Dict[str, asyncio.Lock] = {} # Lock cho từng symbol
        self._is_resyncing = False # Trạng thái resync
        self._start_time = time.time()
        self._last_resync_failed = False
        self._initial_snapshot_received = False
        self._processed_events: OrderedDict = OrderedDict()

        # Metrics
        self.metrics = {
            "duplicate_events_dropped": 0,
            "stale_events_dropped": 0,
            "reconnect_resync_count": 0,
            "stale_event_drop_count": 0,
            "reconnect_burst_total": 0,
            "resync_deduplicated_total": 0,
            "atomic_resync_duration_ms": 0.0,
            "reconnect_coalesced_total": 0,
        }
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
        self._is_resyncing = False # Cập nhật trạng thái resync
        if self._audit_task:
            self._audit_task.cancel()
            self._audit_task = None

    async def _release_resyncing_state(self, reason: str = "") -> None:
        async with self._resync_lock:
            if self._is_resyncing:
                self._is_resyncing = False
                if reason:
                    logger.debug(f"[MIRROR] Cleared _is_resyncing: {reason}")

    async def _debounced_resync_runner(self, generation: int) -> None:
        try:
            logger.info("[MIRROR] Cache đã bị KHÓA. Waiting 2 seconds (debounce) before REST snapshot resync...")
            await asyncio.sleep(2)
            if generation != self._resync_generation:
                return
            await self._run_atomic_resync()
        except asyncio.CancelledError:
            if generation == self._resync_generation:
                await self._release_resyncing_state("debounced resync cancelled")
            raise
        except Exception as e:
            if generation == self._resync_generation:
                await self._release_resyncing_state(f"debounced resync failed: {e}")
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

        async with self._resync_lock:
            if self._is_resyncing:
                self.metrics["resync_deduplicated_total"] = self.metrics.get("resync_deduplicated_total", 0) + 1
                self.metrics["reconnect_coalesced_total"] = self.metrics.get("reconnect_coalesced_total", 0) + 1
                logger.debug("[MIRROR] Reconnect ignored: resync already active (coalesced)")
                return

            logger.info("[MIRROR] WS Reconnect detected. Triggering atomic REST snapshot sync...")
            self.metrics["reconnect_resync_count"] += 1
            self._print_audit_summary()
            self._is_resyncing = True

        if self._resync_task and not self._resync_task.done():
            self._resync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._resync_task

        self._resync_generation += 1
        generation = self._resync_generation
        self._resync_task = asyncio.create_task(self._debounced_resync_runner(generation))

    async def _run_atomic_resync(self) -> None:
        start_ms = time.time() * 1000
        async with self._resync_lock:
            if not self._is_resyncing:
                return

            snapshot_data = await self._fetch_snapshot_with_retry()

            if snapshot_data:
                await self._apply_snapshot(snapshot_data)
                self._last_resync_failed = False
                self._initial_snapshot_received = True
                logger.info(f"[MIRROR] Atomic Resync Success! {len(self._positions)} positions active.")
                await self._publish_resync_success()
            else:
                self._last_resync_failed = True
                logger.critical(
                    "[MIRROR] CRITICAL: Failed to resync state after all retries! "
                    "Cache state is COMPROMISED."
                )
                await self._publish_resync_failed()

            self._is_resyncing = False
            duration_ms = (time.time() * 1000) - start_ms
            self.metrics["atomic_resync_duration_ms"] = duration_ms
            logger.info(f"[MIRROR] Resync complete in {duration_ms:.1f}ms.")

    async def _fetch_snapshot_with_retry(self) -> Optional[Dict[str, Any]]:
        for attempt in range(5):
            try:
                logger.info(f"[MIRROR] Fetching REST snapshot (Attempt {attempt + 1})...")
                equity = await self.exchange.fetch_account_equity()
                pos_response = await self.exchange._request("GET", "/api/v5/account/positions")
                positions_raw = pos_response.get("data", []) if pos_response else []
                
                return {"equity": equity, "positions": positions_raw}

            except Exception as e:
                logger.error(f"[MIRROR] REST snapshot fetch failed: {e}")
                if attempt < 4:
                    from core.config.settings import settings
                    wait_time = min(settings.RETRY_MAX_DELAY_SECONDS, settings.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt + 1)))
                    logger.debug(f"[MIRROR] Retry {attempt + 1}/5: waiting {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
        return None

    async def _apply_snapshot(self, snapshot_data: Dict[str, Any]):
        positions_raw = snapshot_data["positions"]
        equity = snapshot_data["equity"]

        new_positions = {}
        for pos in positions_raw:
            if abs(float(pos.get("pos") or "0")) > 0:
                instId = pos.get("instId")
                if instId:
                    new_positions[instId] = pos
        
        formatted_account = {}
        if equity:
            formatted_account = {
                "totalEq": str(equity.get("totalEq", 0.0)),
                "availEq": str(equity.get("availEq", 0.0)),
                "uTime": str(int(time.time() * 1000)),
            }

        try:
            from services.position.tpsl_resolver import build_algo_tpsl_map, enrich_raw_position_dict
            algo_map = await build_algo_tpsl_map(self.exchange)
            for instId, raw in new_positions.items():
                enrich_raw_position_dict(raw, algo_map.get(instId))
        except Exception as enrich_err:
            logger.debug(f"[MIRROR] Algo TP/SL enrich skipped: {enrich_err}")

        async with self._account_lock:
            if formatted_account:
                self._account = formatted_account
        
        self._positions = new_positions

    async def _publish_resync_success(self):
        try:
            await self.event_bus.publish(Event(
                event_type=EventTopic.MIRROR_RESYNC_SUCCESS,
                data={"positions": len(self._positions)},
                source="exchange_mirror",
            ))
        except Exception as pub_err:
            logger.debug(f"[MIRROR] Failed to publish MIRROR_RESYNC_SUCCESS: {pub_err}")

    async def _publish_resync_failed(self):
        try:
            await self.event_bus.publish(Event(
                event_type=EventTopic.MIRROR_RESYNC_FAILED,
                data={"reason": "Max retries exceeded"},
                source="exchange_mirror"
            ))
        except Exception as evt_err:
            logger.error(f"[MIRROR] Failed to publish MIRROR_RESYNC_FAILED event: {evt_err}")

    async def _handle_raw_position(self, event: Event) -> None:
        # Không còn replay buffer. Xử lý sự kiện trực tiếp.
        # Cờ _is_resyncing sẽ chủ yếu ảnh hưởng đến các thao tác đọc và kiểm tra tính nhất quán tổng thể.
        # Các cập nhật trực tiếp (live updates) nên luôn cố gắng cập nhật cache nếu chúng mới hơn.
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
        
        # Lấy hoặc tạo lock cho instId cụ thể
        if instId not in self._position_locks:
            self._position_locks[instId] = asyncio.Lock()

        async with self._position_locks[instId]: # Sử dụng lock riêng cho từng symbol
            cached = self._positions.get(instId)

            if cached and int(cached.get("uTime", 0)) > new_uTime:
                self.metrics["stale_events_dropped"] += 1
                self.metrics["stale_event_drop_count"] += 1
                return  # Ignore out-of-order event

            pos_str = str(data.get("pos", "0"))
            if pos_str == "0":
                if instId in self._positions:
                    del self._positions[instId]
                    # Xóa lock nếu không còn vị thế
                    if instId in self._position_locks:
                        del self._position_locks[instId]
            else:
                self._positions[instId] = data
            self._initial_snapshot_received = True

    async def _handle_raw_account(self, event: Event) -> None:
        # Không còn replay buffer. Xử lý sự kiện trực tiếp.
        await self._handle_raw_account_internal(event)

    async def _handle_raw_account_internal(self, event: Event) -> None:
        """Internal handler that mutates account cache — called during live dispatch OR during replay."""
        data = event.data.get("data", {}) or event.data
        if not data:
            return

        if self._is_duplicate_or_stale("account", data):
            return

        new_uTime = int(data.get("uTime", 0))
        
        async with self._account_lock: # Sử dụng lock cho account
            if self._account and int(self._account.get("uTime", 0)) > new_uTime:
                self.metrics["stale_events_dropped"] += 1
                self.metrics["stale_event_drop_count"] += 1
                return

            self._account = data
            self._initial_snapshot_received = True

    async def get_total_balance(self) -> float:
        async with self._account_lock:
            return float(self._account.get("totalEq", 0.0))

    async def get_free_margin(self) -> float:
        async with self._account_lock:
            return float(self._account.get("availEq", 0.0))

    async def get_all_positions(self) -> Dict[str, MirrorPosition]:
        """Get an immutable snapshot of all positions."""
        # Chặn mọi lệnh đọc khi đang resync sau Reconnect
        if self._is_resyncing:
            logger.warning("[MIRROR] get_all_positions() ĐANG RESYNC: Trả về dữ liệu hiện có. RiskManager cần xử lý dữ liệu có thể chưa cập nhật.")
            # [FIX LỖI 3] Không chặn hoàn toàn, trả về dữ liệu hiện có để ưu tiên Stop Loss. RiskManager sẽ tự check cờ _is_resyncing

        # Tạo một bản sao của dictionary để tránh race condition khi đọc
        # và đảm bảo dữ liệu trả về là immutable snapshot
        snapshot = {}
        for instId, raw_pos in self._positions.items():
            try:
                from services.position.tpsl_resolver import extract_tpsl_from_raw_position
                sl_px, tp_list = extract_tpsl_from_raw_position(raw_pos)
                snapshot[instId] = MirrorPosition(
                    instId=raw_pos.get("instId", ""),
                    pos=float(raw_pos.get("pos", 0) or 0),
                    avgPx=float(raw_pos.get("avgPx", 0) or 0),
                    upl=float(raw_pos.get("upl", 0) or 0),
                    uplLastPx=float(raw_pos.get("uplLastPx", raw_pos.get("upl", 0)) or 0),
                    uplRatio=float(raw_pos.get("uplRatio", 0) or 0),
                    margin=float(raw_pos.get("margin", 0) or 0),
                    markPx=float(raw_pos.get("markPx", 0) or 0),
                    liqPx=float(raw_pos.get("liqPx", 0) or 0),
                    cTime=int(raw_pos.get("cTime", 0)),
                    uTime=int(raw_pos.get("uTime", 0)),
                    tpTriggerPx=tp_list[0] if tp_list else None,
                    slTriggerPx=sl_px,
                    tpPrices=tuple(tp_list)
                )
            except Exception as e:
                logger.error(f"Error creating MirrorPosition for {instId}: {e}")
                continue
        return snapshot

    async def get_position(self, instId: str) -> Optional[MirrorPosition]:
        """Get an immutable snapshot of a position.

        TỬ HUYỆT BỊ KHÓA: Trả về None ngay lập tức nếu Cache đang bị khóa
        trong quá trình WS Reconnect Resync để ngăn RiskManager dùng dữ liệu lỗi thời.
        """
        # Chặn mọi lệnh đọc khi đang resync sau Reconnect
        if self._is_resyncing:
            logger.warning(f"[MIRROR] get_position({instId}) ĐANG RESYNC: Trả về dữ liệu hiện có. RiskManager cần xử lý dữ liệu có thể chưa cập nhật.")
            # [FIX LỖI 3] Không chặn hoàn toàn, trả về dữ liệu hiện có để ưu tiên Stop Loss. RiskManager sẽ tự check cờ _is_resyncing

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
                cTime=int(raw.get("cTime", 0)),
                uTime=int(raw.get("uTime", 0)),
                tpTriggerPx=tp_list[0] if tp_list else None,
                slTriggerPx=sl_px,
                tpPrices=tuple(tp_list)
            )
        except Exception as e:
            logger.error(f"Error creating MirrorPosition for {instId}: {e}")
            return None



        if self._is_resyncing:
            logger.warning("[MIRROR] get_all_positions() ĐANG RESYNC: Trả về dữ liệu hiện có. RiskManager cần xử lý dữ liệu có thể chưa cập nhật.")
            # Proceed to return the current positions, even if resyncing.
        result = {}
        for instId in list(self._positions.keys()):
            pos = await self.get_position(instId)
            if pos:
                result[instId] = pos
        return result

    async def get_account(self) -> Optional[MirrorAccount]:
        """Get an immutable snapshot of the account.

        Trả về None khi Cache đang bị khóa để ngăn RiskManager đọc margin lỗi thời.
        """
        if self._is_resyncing:
            logger.warning("[MIRROR] get_account() ĐANG RESYNC: Trả về dữ liệu hiện có. RiskManager cần xử lý dữ liệu có thể chưa cập nhật.")
            # Proceed to return the current account, even if resyncing.

        async with self._account_lock:
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

    async def get_total_balance(self) -> float:
        """Get the total balance of the mirrored account (fast-path cache accessor)."""
        acc = await self.get_account()
        return acc.totalEq if acc else 0.0

    async def get_free_margin(self) -> float:
        """Get the free margin of the mirrored account (fast-path cache accessor)."""
        acc = await self.get_account()
        return acc.availEq if acc else 0.0

    def get_realized_pnl(self) -> float:
        """Get the total realized PnL from the mirrored cache."""
        return 0.0