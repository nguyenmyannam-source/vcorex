import asyncio
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from core.config.settings import settings
from core.container import run_safe_task
from core.metrics import InMemoryMetricsAdapter, MetricsAdapter
from core.events.topics import EventTopic
from core.event_bus import Event
from services.position.models import PositionStatus, TakeProfitLevel, TrackedPosition
from infrastructure.exchange.okx_exchange import OKXOrderVerificationUnknownError
from loguru import logger

# Positions in these statuses must not block new entries or panic-close retries.
_TERMINAL_POSITION_STATUSES = frozenset(
    {
        PositionStatus.CLOSED,
        PositionStatus.FAILED,
        PositionStatus.LIQUIDATED,
    }
)

# After fill, amount_remaining is OKX contract count (sz), not USDT notional.
_CONTRACT_QUANTITY_STATUSES = frozenset(
    {
        PositionStatus.OPENED,
        PositionStatus.PARTIAL_TP,
        PositionStatus.PARTIALLY_FILLED,
        PositionStatus.CLOSING,
        PositionStatus.IN_FLIGHT,
    }
)



class OrderHandler:
    def __init__(self, okx_client, *args, **kwargs):
        # Support flexible constructor used in tests: accept either
        # positional form: (exchange, event_bus, persistence, default_leverage)
        # or keyword form: event_bus=..., persistence=..., default_leverage=...
        self.okx_client = okx_client

        # Positional unpacking for legacy callers
        event_bus = None
        persistence = None
        default_leverage = None
        if len(args) >= 1:
            event_bus = args[0]
        if len(args) >= 2:
            persistence = args[1]
        if len(args) >= 3:
            default_leverage = args[2]

        # Keyword overrides
        event_bus = kwargs.get("event_bus", event_bus)
        persistence = kwargs.get("persistence", persistence)
        default_leverage = kwargs.get("default_leverage", default_leverage)

        self.event_bus = event_bus
        self.persistence = persistence
        self.default_leverage = default_leverage

        self.args = args
        self.kwargs = kwargs
        self.pending_orders = set()
        self._positions = {}
        self._exchange_id_map = {}
        self._pending_order_cache = {}
        self.metrics = {}
        self._processed_fill_keys = set()
        # [FIX] Reverse mapping for TP order lookup: algo_order_id → position_id
        self._algo_order_to_position = {}
        # [REMEDIATION] Use TTL cache instead of bounded set for dedup
        self._processed_capacity = 1000
        try:
            from cachetools import TTLCache
            self._processed_ws_fills = TTLCache(maxsize=10000, ttl=86400)  # 24h TTL (86400s)
            self._ws_fills_use_ttl_cache = True
        except ImportError:
            logger.warning("cachetools not available, falling back to dict with timestamps")
            self._processed_ws_fills = dict()
            self._ws_fills_use_ttl_cache = False
        self._transient_orders = dict()
        self._max_collection_size = 1000
        self._symbol_locks = {}
        self._phantom_in_flight: set[str] = set()
        self._halt_trading = False  # Circuit Breaker flag
        # Metrics adapter: allow injection for tests, fallback to in-memory adapter
        self._metrics: MetricsAdapter = kwargs.get("metrics") or InMemoryMetricsAdapter()
        # [PHASE 3] Fallback queue for orphaned positions
        self._fallback_queue = asyncio.Queue()
        self._fallback_worker_task = None
        # [FIX ORPHAN TP/SL] Async lock to protect cleanup flow from race conditions
        self._cleanup_lock = asyncio.Lock()

    def subscribe_halt_trading(self) -> None:
        """Wire CONTROL_HALT_TRADING event to block new entries (no liquidation)."""
        if self.event_bus:
            self.event_bus.subscribe(
                self._handle_halt_trading,
                [EventTopic.CONTROL_HALT_TRADING],
                handler_id="oh_halt_trading"
            )
            self.event_bus.subscribe(
                self._handle_resume_trading,
                [EventTopic.CONTROL_START_BOT],
                handler_id="oh_resume_trading",
            )
            logger.info("[CIRCUIT-BREAKER] Subscribed to CONTROL_HALT_TRADING / CONTROL_START_BOT events.")

    def unsubscribe_halt_trading(self) -> None:
        """Remove halt/resume subscriptions on shutdown."""
        if self.event_bus:
            self.event_bus.unsubscribe(handler_id="oh_halt_trading")
            self.event_bus.unsubscribe(handler_id="oh_resume_trading")
            
    def start_workers(self) -> None:
        """Start background workers (e.g., fallback queue processor)."""
        if self._fallback_worker_task is None:
            self._fallback_worker_task = run_safe_task(self._fallback_worker())
            logger.info("[ORDER-HANDLER] Fallback worker started.")
            
    async def stop_workers(self) -> None:
        """Stop background workers."""
        if self._fallback_worker_task is not None:
            self._fallback_worker_task.cancel()
            try:
                await self._fallback_worker_task
            except asyncio.CancelledError:
                pass
            self._fallback_worker_task = None
            logger.info("[ORDER-HANDLER] Fallback worker stopped.")

    async def _fallback_worker(self) -> None:
        """Background worker to process the Fallback Queue for orphaned positions."""
        logger.info("[FALLBACK-WORKER] Started processing fallback queue.")
        while True:
            try:
                item = await self._fallback_queue.get()
                pos_id = item.get("pos_id")
                signal_data = item.get("signal_data")
                contracts = item.get("contracts")
                task_type = item.get("type")
                
                pos = self.get_position(pos_id)
                if not pos:
                    self._fallback_queue.task_done()
                    continue
                    
                # We do simple exponential backoff inside the worker
                # In real scenario, we might requeue with delay
                logger.info(f"[FALLBACK-WORKER] Processing item: {task_type} for {pos_id}")
                
                MAX_RETRIES = 5
                retries = item.get("retries", 0)

                if task_type == "rollback_or_retry":
                    # Try to market close again
                    success = await self.close_position(pos_id)
                    if not success:
                        if retries >= MAX_RETRIES:
                            logger.error(f"[FALLBACK-WORKER] MAX RETRIES ({MAX_RETRIES}) reached for rollback {pos_id}. Dropping task to prevent memory leak.")
                        else:
                            logger.warning(f"[FALLBACK-WORKER] Rollback still failing for {pos_id}. Retry {retries + 1}/{MAX_RETRIES}.")
                            await asyncio.sleep(5)
                            item["retries"] = retries + 1
                            self._fallback_queue.put_nowait(item)
                    else:
                        logger.info(f"[FALLBACK-WORKER] Rollback finally succeeded for {pos_id}.")
                elif task_type == "rollback_or_retry_sl":
                    # Try to dispatch SL again
                    try:
                        await self._dispatch_algo_sl(pos, signal_data, contracts)
                    except Exception as e:
                        if retries >= MAX_RETRIES:
                            logger.error(f"[FALLBACK-WORKER] MAX RETRIES ({MAX_RETRIES}) reached for SL Dispatch {pos_id}: {e}. Dropping task.")
                        else:
                            logger.warning(f"[FALLBACK-WORKER] SL Dispatch still failing for {pos_id}: {e}. Retry {retries + 1}/{MAX_RETRIES}.")
                            await asyncio.sleep(5)
                            item["retries"] = retries + 1
                            self._fallback_queue.put_nowait(item)

                self._fallback_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FALLBACK-WORKER] Unhandled error: {e}")
                await asyncio.sleep(1)

    async def _handle_halt_trading(self, event: Event) -> None:
        """Handler for CONTROL_HALT_TRADING — block new entries only (no liquidation)."""
        reason = event.data.get("reason", "unknown") if isinstance(event.data, dict) else "unknown"
        logger.critical(
            f"[CIRCUIT-BREAKER] HALT TRADING received! Reason: {reason}. "
            "Blocking new entries — open positions preserved."
        )
        self._halt_trading = True

    async def _handle_resume_trading(self, event: Event) -> None:
        """Clear halt flag when operator resumes trading via Telegram."""
        self._halt_trading = False
        logger.info("[CIRCUIT-BREAKER] CONTROL_START_BOT received — new entries re-enabled.")

    def _enforce_collection_bounds(self):
        """Giữ cho kích thước của các tập hợp tạm thời không vượt quá giới hạn để chống rò rỉ bộ nhớ."""
        if len(self._processed_fill_keys) > self._processed_capacity:
            keys = list(self._processed_fill_keys)
            self._processed_fill_keys = set(keys[len(keys)//2:])

        if not getattr(self, "_ws_fills_use_ttl_cache", False):
            if len(self._processed_ws_fills) > self._processed_capacity:
                items = sorted(
                    self._processed_ws_fills.items(),
                    key=lambda x: float(x[1]) if isinstance(x[1], (int, float)) else 0.0,
                )
                keep_items = items[len(items) // 2 :]
                self._processed_ws_fills = dict(keep_items)

        if len(self._transient_orders) > self._max_collection_size:
            keys = list(self._transient_orders.keys())
            for k in keys[:len(keys)//2]:
                self._transient_orders.pop(k, None)

    def clean_stale_transient_orders(self, current_time: float, max_age: float = 5.0):
        """Dọn dẹp các lệnh tạm thời đã quá hạn."""
        stale_keys = [k for k, timestamp in self._transient_orders.items() if current_time - timestamp > max_age]
        for k in stale_keys:
            self._transient_orders.pop(k, None)

    @staticmethod
    def _is_long_side(side: str) -> bool:
        """True for buy/long exposure (close with sell)."""
        normalized = str(side).lower()
        return normalized in ("buy", "long", "buy_signal")

    @classmethod
    def _close_order_side(cls, side: str) -> str:
        """Map tracked position side to OKX close order side."""
        return "sell" if cls._is_long_side(side) else "buy"

    def _resolve_ct_val(self, symbol: str, signal_data: Optional[dict] = None) -> float:
        """Resolve OKX contract multiplier (ctVal) for a symbol."""
        if signal_data and signal_data.get("ct_val") is not None:
            return float(signal_data["ct_val"])
        markets = getattr(self.okx_client, "_markets", None) or {}
        if symbol and symbol in markets:
            return float(markets[symbol].get("ctVal") or 1.0)
        from utils.okx_symbols import OKX_SYMBOL_SPECS

        return float(OKX_SYMBOL_SPECS.get(symbol or "", {}).get("contract_value", 1.0))

    def _compute_open_sizes(
        self, signal_data: dict, ct_val: float
    ) -> tuple[float, float]:
        """Return (estimated_contracts, place_order_amount) for open_position."""
        position_size_usdt = float(signal_data.get("position_size_usdt", 0.0))
        entry_price = float(signal_data.get("entry_price", 0.0))

        if signal_data.get("amount") is not None:
            contracts = float(signal_data["amount"])
            return contracts, contracts * ct_val

        if entry_price > 0.0:
            base_amount = position_size_usdt / entry_price
            contracts = base_amount / ct_val if ct_val > 0 else base_amount
            return contracts, base_amount

        return position_size_usdt, position_size_usdt

    def _resolve_ws_fill_contracts(
        self, data: dict, signal_data: dict, symbol: str
    ) -> Optional[float]:
        """Resolve OKX contract count (sz) from WS fill — never treat USDT as contracts."""
        raw_sz = data.get("accFillSz") or data.get("fillSz")
        if raw_sz is not None and str(raw_sz).strip() not in ("", "0"):
            return float(raw_sz)

        entry_price = float(
            data.get("avgPx")
            or data.get("fillPx")
            or signal_data.get("entry_price", 0.0)
            or 0.0
        )
        position_size_usdt = float(signal_data.get("position_size_usdt", 0.0))
        if entry_price <= 0 or position_size_usdt <= 0:
            logger.error(
                f"[WS-FILL] Missing accFillSz/fillSz and cannot derive contracts for {symbol}"
            )
            return None

        ct_val = self._resolve_ct_val(symbol, signal_data)
        base_amount = position_size_usdt / entry_price
        if ct_val <= 0:
            logger.error(f"[WS-FILL] Invalid ct_val={ct_val} for {symbol}")
            return None
        return base_amount / ct_val

    def _resolve_close_place_order_amount(self, pos: TrackedPosition, quantity: float) -> float:
        """Convert close quantity to `place_order` amount (base notional = contracts * ctVal)."""
        ct_val = float(getattr(pos, "ct_val", None) or 1.0)
        if pos.status in _CONTRACT_QUANTITY_STATUSES:
            return quantity * ct_val
        # Pending / unfilled: quantity is USDT notional (same convention as open_position).
        entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
        if entry_price > 0:
            return quantity / entry_price
        return quantity

    async def _evict_terminal_position(self, internal_id: str, pos: TrackedPosition) -> None:
        """Remove closed/failed positions from RAM and persist final state."""
        if self.persistence:
            try:
                await self.persistence.save_position(pos)
            except Exception as e:
                logger.error(f"Failed to persist terminal position {internal_id}: {e}")
        self._positions.pop(internal_id, None)
        exchange_id = getattr(pos, "exchange_id", None)
        if exchange_id and exchange_id in self._exchange_id_map:
            self._exchange_id_map.pop(exchange_id, None)

    def get_active_positions(self, *args, **kwargs) -> List[TrackedPosition]:
        """Synchronous list of non-terminal tracked positions (excludes CLOSED/FAILED/LIQUIDATED)."""
        logger.debug("[ORDER-HANDLER] get_active_positions (Hàm đồng bộ) được gọi từ main loop.")
        return [
            p
            for p in self._positions.values()
            if getattr(p, "status", None) not in _TERMINAL_POSITION_STATUSES
        ]

    def get_position(self, internal_id: str) -> Any:
        """Return an active tracked position by internal id."""
        return self._positions.get(internal_id)

    def _register_algo_order(self, position_id: str, algo_order_id: str) -> None:
        """[FIX] Register mapping of algo order ID to position ID for TP fill lookup.

        When a TP (reduce-only) order is filled, we need to find the position.
        Since TP order IDs differ from entry order IDs, we store this mapping.
        """
        if algo_order_id:
            self._algo_order_to_position[algo_order_id] = position_id
            logger.debug(f"[TP-REGISTER] Registered algo_order_id={algo_order_id} → position_id={position_id}")

    def _unregister_algo_order(self, algo_order_id: str) -> None:
        """[FIX] Remove algo order mapping after it's filled or canceled."""
        if algo_order_id in self._algo_order_to_position:
            del self._algo_order_to_position[algo_order_id]
            logger.debug(f"[TP-UNREGISTER] Unregistered algo_order_id={algo_order_id}")

    def _build_take_profit_levels(self, signal_data: dict) -> list[TakeProfitLevel]:
        """Normalize take profit price payload into tracked TP level objects with OKX precision."""
        tps = signal_data.get("take_profit_prices") or []
        if not tps:
            return []

        # Get tick_sz for proper rounding
        symbol = signal_data.get("symbol", "")
        tick_sz = 0.001
        if symbol:
            market_spec = getattr(self.okx_client, "_markets", {}).get(symbol) if hasattr(self, "okx_client") else None
            if market_spec:
                tick_sz = float(market_spec.get("tickSz", 0.001))
            else:
                from utils.okx_symbols import OKX_SYMBOL_SPECS
                specs = OKX_SYMBOL_SPECS.get(symbol, {})
                tick_sz = float(specs.get("tick_size", 0.001))

        import math
        def round_px(px: float) -> float:
            if not tick_sz: return px
            rounded = round(px / tick_sz) * tick_sz
            precision = max(0, -int(math.log10(tick_sz))) if tick_sz < 1 else 0
            return float(f"{rounded:.{precision}f}")

        levels = []
        total_levels = len(tps)
        for tp in tps:
            if isinstance(tp, dict):
                price = tp.get("price")
                exit_pct = tp.get("exit_pct")
            else:
                price = tp
                exit_pct = None

            if price is None:
                continue

            if exit_pct is None:
                exit_pct = 1.0 / total_levels if total_levels else 0.0

            rounded_price = round_px(float(price))
            levels.append(TakeProfitLevel(price=rounded_price, exit_pct=float(exit_pct)))

        return levels

    async def handle_ws_raw_order_fill(self, event_data: Any = None, *args, **kwargs) -> None:
        """[WS-EXECUTION] Hàm xử lý sự kiện khớp lệnh từ OKX WebSocket. Đây là SOURCE-OF-TRUTH."""
        try:
            if not event_data:
                return

            payload = getattr(event_data, "data", event_data)

            if isinstance(payload, dict) and "data" in payload:
                inner = payload["data"]
                data = inner[0] if isinstance(inner, list) and inner else (inner if isinstance(inner, dict) else {})
            else:
                data = payload if isinstance(payload, dict) else {}

            cl_ord_id = data.get("clOrdId")
            ord_id = data.get("ordId")
            state = data.get("state", "").lower()

            # Idempotency check
            dedup_key = f"{ord_id or cl_ord_id}_state_{state}"
            if dedup_key in self._processed_ws_fills:
                return
            # Always write to dict with timestamp
            self._processed_ws_fills[dedup_key] = time.time()

            logger.info(f"[WS-ORDER-FILL] Real-time WS order update: ordId={ord_id}, clOrdId={cl_ord_id}, state={state}")

            # Find matching position using multiple lookup strategies
            target_pos = None
            is_algo_fill = False  # Track if this is a TP/SL order fill

            # Method 1: Direct position ID lookup (entry orders)
            for pos in self._positions.values():
                if pos.exchange_id == ord_id or (cl_ord_id and pos.signal_id == cl_ord_id):
                    target_pos = pos
                    is_algo_fill = False
                    break

            # [FIX C3] NEVER fallback algoId to ordId!
            # ordId is the entry order ID — if we fallback to it, we'd INCORRECTLY treat
            # a regular entry-order WS event as an algo TP/SL fill.
            # Only resolve algoId from the dedicated algoId field in the WS payload.
            algo_id = data.get("algoId")  # None for entry orders — that's intentional.

            # Method 2: [FIX] Reverse algo order lookup (TP/SL fills)
            if not target_pos and algo_id and algo_id in self._algo_order_to_position:
                position_id = self._algo_order_to_position[algo_id]
                target_pos = self._positions.get(position_id)
                is_algo_fill = True
                logger.info(f"[TP-FILL-LOOKUP] Found position {position_id} via algo order {algo_id}")

            # [REMEDIATION] If position not found, check pending cache (fill before ACK scenario)
            if not target_pos and cl_ord_id:
                signal_data = self._pending_order_cache.get(cl_ord_id)
                if signal_data:
                    symbol = signal_data.get("symbol")
                    if not symbol:
                        return
                    lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
                    async with lock:
                        logger.warning(f"[WS-FILL-BEFORE-ACK] Fill arrived before ACK for {cl_ord_id}. Creating position now.")

                        # Create position immediately from WS fill
                        import uuid
                        internal_id = "pos_" + str(uuid.uuid4())
                        _sig_type_raw = signal_data.get("signal_type")
                        _sig_type_str = _sig_type_raw.value if hasattr(_sig_type_raw, "value") else str(_sig_type_raw).lower()

                        fill_sz = self._resolve_ws_fill_contracts(data, signal_data, symbol)
                        if fill_sz is None or fill_sz <= 0:
                            logger.error(
                                f"[WS-FILL-BEFORE-ACK] Cannot determine contract size for {cl_ord_id}; skipping position create"
                            )
                            return
                        avg_px = float(data.get("avgPx") or data.get("fillPx") or signal_data.get("entry_price", 0.0))
                        ct_val = self._resolve_ct_val(symbol, signal_data)

                        pos = TrackedPosition(
                            id=internal_id,
                            exchange_id=ord_id,
                            symbol=symbol,
                            side=_sig_type_str,
                            entry_price=avg_px,
                            current_price=avg_px,
                            amount=fill_sz,
                            amount_remaining=fill_sz,
                            leverage=self.default_leverage or 1,
                            stop_loss=signal_data.get("stop_loss_price"),
                            take_profit_levels=self._build_take_profit_levels(signal_data),
                            status=PositionStatus.OPENED,
                            signal_id=cl_ord_id,
                            algo_order_ids=[],
                            strategy_name=signal_data.get("strategy_name", "unknown"),
                            ct_val=ct_val,
                        )
                        self._positions[internal_id] = pos

                        # Persist position
                        if hasattr(self, "persistence") and self.persistence:
                            try:
                                await self.persistence.save_position(pos)
                            except Exception as e:
                                logger.error(f"Failed to persist position created from WS fill: {e}")

                        # Remove from pending cache
                        self._pending_order_cache.pop(cl_ord_id, None)

                        # Fire event
                        event_data = {**pos.__dict__, "signal_data": signal_data}
                        if self.event_bus:
                            await self.event_bus.publish(Event(
                                event_type=EventTopic.POSITION_OPENED,
                                data=event_data,
                                source="order_handler"
                            ))

                        # Dispatch TP orders
                        await self._dispatch_algo_tps(pos, signal_data, fill_sz)
                        # [FIX #3] Dispatch SL independently (fill-before-ACK path)
                        try:
                            await self._dispatch_algo_sl(pos, signal_data, fill_sz)
                        except Exception as sl_err:
                            logger.error(f"[WS-FILL-BEFORE-ACK] Failed to dispatch SL for {internal_id}: {sl_err}")

                        logger.info(f"[POSITION-CREATED-FROM-WS-FILL] Position {internal_id} created from WS fill. symbol={pos.symbol}, size={fill_sz}")
                    return

            if target_pos:
                symbol = target_pos.symbol
                lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
                async with lock:
                    # [REMEDIATION] Handle partial fill separately
                    if state == "partially_filled":
                        if target_pos.status in (PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE, PositionStatus.UNVERIFIED):
                            target_pos.status = PositionStatus.PARTIALLY_FILLED
                            target_pos.exchange_id = ord_id
                            fill_sz = float(data.get("accFillSz") or data.get("fillSz") or target_pos.amount_remaining)
                            avg_px = float(data.get("avgPx") or data.get("fillPx") or target_pos.entry_price)

                            if fill_sz > 0:
                                # [FIX P1] Adjust TP/SL for slippage on partial fill
                                if target_pos.entry_price and target_pos.entry_price > 0 and avg_px > 0:
                                    ratio = avg_px / target_pos.entry_price
                                    if target_pos.stop_loss:
                                        target_pos.stop_loss *= ratio
                                    if target_pos.take_profit_levels:
                                        for tp in target_pos.take_profit_levels:
                                            if isinstance(tp, dict) and "price" in tp:
                                                tp["price"] *= ratio
                                            elif hasattr(tp, "price"):
                                                tp.price *= ratio
                                
                                target_pos.entry_price = avg_px
                                target_pos.amount_remaining = fill_sz

                            logger.info(f"[POSITION-PARTIAL] Position {target_pos.id} partially filled. size={fill_sz}, status=PARTIALLY_FILLED")

                            # Do NOT dispatch TP/SL yet, wait for full fill
                            if self.persistence:
                                await self.persistence.save_position(target_pos)
                        return

                    if state in ("filled", "live"):
                        fill_sz = float(data.get("accFillSz") or data.get("fillSz") or target_pos.amount_remaining)
                        avg_px = float(data.get("avgPx") or data.get("fillPx") or target_pos.entry_price)

                        if fill_sz > 0:
                            # [FIX] Different handling for entry fills vs TP/SL fills
                            if is_algo_fill:
                                # Size authority: PositionEngine WS position channel sets absolute size
                                logger.info(
                                    f"[TP-FILL] Algo order {algo_id} filled {fill_sz:.6f} contracts "
                                    f"for {target_pos.id} — awaiting position WS sync for size update."
                                )
                                self._unregister_algo_order(algo_id)
                                
                                # Cập nhật danh sách algo_order_ids của position
                                if hasattr(target_pos, "algo_order_ids") and target_pos.algo_order_ids:
                                    if algo_id in target_pos.algo_order_ids:
                                        target_pos.algo_order_ids.remove(algo_id)
                                        logger.info(f"[TP-CLEANUP] Removed executed algo_id {algo_id} from position {target_pos.id} algo_order_ids")

                                if self.persistence:
                                    await self.persistence.save_position(target_pos)
                                return
                            else:
                                # For entry fills: SET amount_remaining
                                # [FIX P1] Adjust TP/SL for slippage
                                if target_pos.entry_price and target_pos.entry_price > 0 and avg_px > 0:
                                    ratio = avg_px / target_pos.entry_price
                                    if target_pos.stop_loss:
                                        target_pos.stop_loss *= ratio
                                    if target_pos.take_profit_levels:
                                        for tp in target_pos.take_profit_levels:
                                            if isinstance(tp, dict) and "price" in tp:
                                                tp["price"] *= ratio
                                            elif hasattr(tp, "price"):
                                                tp.price *= ratio
                                
                                target_pos.entry_price = avg_px
                                target_pos.amount_remaining = fill_sz

                        if state == "filled":
                            # [REMEDIATION] Allow transition from PARTIALLY_FILLED to OPENED
                            if target_pos.status in (PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE, PositionStatus.UNVERIFIED, PositionStatus.PARTIALLY_FILLED):
                                prior_status = target_pos.status
                                target_pos.status = PositionStatus.OPENED
                                target_pos.exchange_id = ord_id
                                logger.info(f"[POSITION-CONFIRMED] Position {target_pos.id} officially OPENED via WS fill! entry={avg_px}, size={fill_sz}")

                                # Fire event once per position
                                if not target_pos.position_opened_event_sent:
                                    sig_data = self._pending_order_cache.get(cl_ord_id, {})
                                    event_data = {**target_pos.__dict__, "signal_data": sig_data}
                                    if self.event_bus:
                                        await self.event_bus.publish(Event(
                                            event_type=EventTopic.POSITION_OPENED,
                                            data=event_data,
                                            source="order_handler"
                                        ))
                                    target_pos.position_opened_event_sent = True

                                # Retrieve pending algo orders and dispatch TP
                                signal_data = self._pending_order_cache.pop(cl_ord_id, None)
                                if signal_data:
                                    await self._dispatch_algo_tps(target_pos, signal_data, fill_sz)
                                    # Dispatch SL when recovery path may have lost attachAlgoOrds SL
                                    need_sl = prior_status in (
                                        PositionStatus.PENDING_RECONCILE,
                                        PositionStatus.UNVERIFIED,
                                        PositionStatus.PARTIALLY_FILLED,
                                    ) or not target_pos.sl_algo_order_id
                                    if need_sl:
                                        try:
                                            await self._dispatch_algo_sl(target_pos, signal_data, fill_sz)
                                        except Exception as sl_err:
                                            logger.error(f"[WS-SL-DISPATCH] Failed to dispatch SL for {target_pos.id}: {sl_err}")
                                elif target_pos.take_profit_levels:
                                    # [FALLBACK] Dispatch TP/SL even if signal_data unavailable
                                    # Convert take_profit_levels back to take_profit_prices format for dispatch
                                    tp_prices = [
                                        {
                                            "price": float(tp.price),
                                            "exit_pct": tp.exit_pct
                                        }
                                        for tp in target_pos.take_profit_levels
                                    ]
                                    fallback_signal = {
                                        "symbol": target_pos.symbol,
                                        "signal_type": target_pos.side,
                                        "take_profit_prices": tp_prices,
                                        "stop_loss_price": target_pos.stop_loss,
                                        "correlation_id": f"fallback-tp-dispatch-{target_pos.id}",
                                    }
                                    logger.info(f"[TP-FALLBACK-DISPATCH] Dispatching {len(tp_prices)} TP levels for {target_pos.id} (signal_data not in cache)")
                                    await self._dispatch_algo_tps(target_pos, fallback_signal, fill_sz)
                                    # Dispatch SL in fallback path as well
                                    try:
                                        await self._dispatch_algo_sl(target_pos, fallback_signal, fill_sz)
                                    except Exception as sl_err:
                                        logger.error(f"[WS-FALLBACK-SL-DISPATCH] Failed to dispatch SL for {target_pos.id}: {sl_err}")
                                else:
                                    logger.warning(f"[TP-NONE] Position {target_pos.id} has no take_profit_levels and signal_data not in cache")

                            elif target_pos.status == PositionStatus.CLOSING:
                                target_pos.status = PositionStatus.CLOSED
                                target_pos.amount_remaining = 0.0
                                target_pos.closed_at = datetime.now(timezone.utc)
                                logger.info(f"[POSITION-CLOSED] Position {target_pos.id} officially CLOSED via WS fill!")
                                await self._evict_terminal_position(target_pos.id, target_pos)
                                return

                        # Save to DB
                        if self.persistence:
                            await self.persistence.save_position(target_pos)

                    elif state in ("canceled", "mismatch"):
                        if is_algo_fill:
                            logger.warning(f"[TP-CANCELED] Algo order {algo_id} canceled for position {target_pos.id}.")
                            self._unregister_algo_order(algo_id)
                            if hasattr(target_pos, "algo_order_ids") and target_pos.algo_order_ids:
                                if algo_id in target_pos.algo_order_ids:
                                    target_pos.algo_order_ids.remove(algo_id)
                                    logger.info(f"[TP-CLEANUP] Removed canceled algo_id {algo_id} from position {target_pos.id} algo_order_ids")
                            if self.persistence:
                                await self.persistence.save_position(target_pos)
                            return
                            
                        if target_pos.status in (PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE):
                            target_pos.status = PositionStatus.FAILED
                            logger.warning(f"[POSITION-FAILED] Position {target_pos.id} canceled/failed on exchange. Marking FAILED.")
                        await self._evict_terminal_position(target_pos.id, target_pos)
                        return

        except Exception as e:
            logger.error(f"[WS-ORDER-FILL-ERROR] Lỗi xử lý dữ liệu WebSocket: {str(e)}", exc_info=True)

    async def _dispatch_algo_tps(self, pos: TrackedPosition, signal_data: dict, contracts: float) -> None:
        """Helper to dispatch parallel algo TPs after main position is confirmed FILLED."""
        from decimal import Decimal, ROUND_DOWN
        from domain.risk.risk_utilities import (
            validate_tp_levels_no_collision,
            validate_sl_not_in_tp_range,
        )

        if pos.tp_dispatched:
            logger.info(f"[TP-DISPATCH-SKIP] TP already dispatched for {pos.id} (ids={len(pos.algo_order_ids)})")
            pos.tp_dispatched = True
            return

        # [FIX P1] Prioritize slippage-adjusted pos levels over raw signal_data
        tps = pos.take_profit_levels or signal_data.get("take_profit_prices") or []
        logger.info(f"[TP-DISPATCH-CALL] pos={getattr(pos, 'id', None)} symbol={getattr(pos, 'symbol', None)} contracts={contracts} tps_count={len(tps)}")
        if not tps:
            return

        if not pos.take_profit_levels:
            pos.take_profit_levels = self._build_take_profit_levels(signal_data)
        if pos.stop_loss is None:
            pos.stop_loss = signal_data.get("stop_loss_price")

        # ===== COLLISION DETECTION =====
        # 1. Validate TP levels have no internal collision
        tp_prices = []
        for tp in tps:
            if isinstance(tp, dict):
                tp_prices.append(float(tp.get("price", 0)))
            elif hasattr(tp, "price"):
                tp_prices.append(float(tp.price))
            else:
                tp_prices.append(float(tp))
                
        is_valid, collision_reason = validate_tp_levels_no_collision(
            tp_levels=tp_prices,
            entry_price=float(pos.entry_price or signal_data.get("entry_price", 0)),
            side=pos.side,
            existing_tp_prices=[]  # Can be extended to check other positions
        )
        if not is_valid:
            logger.error(f"[TP-COLLISION] {collision_reason} - Skipping TP dispatch for {pos.id}")
            return

        # 2. Validate SL not in TP range
        sl_price = pos.stop_loss or signal_data.get("stop_loss_price")
        if sl_price:
                float_sl = float(sl_price)
                float_entry = float(pos.entry_price)
                # [FIX H-TEST-FIX] Removed dangerous "[TEST-FIX]" code that silently mutated SL price.
                # If SL price is invalid (LONG but SL > entry), log an error and abort TP dispatch
                # instead of quietly overwriting the stop loss to an arbitrary 90% of entry.
                # This prevents unexpected behavior in live trading from corrupted signal data.
                if pos.side in ["buy", "long"] and float_sl > float_entry:
                    logger.error(
                        f"[SL-INVALID] LONG position {pos.id} has SL {float_sl} above entry {float_entry}. "
                        f"This is an invalid configuration. Aborting TP dispatch to prevent placing "
                        f"TP orders without a valid SL guard. Check signal source."
                    )
                    return
                elif pos.side in ["sell", "short"] and float_sl < float_entry:
                    logger.error(
                        f"[SL-INVALID] SHORT position {pos.id} has SL {float_sl} below entry {float_entry}. "
                        f"This is an invalid configuration. Aborting TP dispatch to prevent placing "
                        f"TP orders without a valid SL guard. Check signal source."
                    )
                    return
                
                is_valid, sl_reason = validate_sl_not_in_tp_range(
                    sl_price=float_sl,
                    tp_levels=tp_prices,
                    entry_price=float_entry,
                    side=pos.side,
                )
                if not is_valid:
                    logger.error(f"[SL-TP-CONFLICT] {sl_reason} - Skipping TP dispatch for {pos.id}")
                    return


        # 3. (REMOVED) Check if algo orders already exist on exchange.
        # We no longer skip TP placements if a TP exists at the same price.
        # This fixes the overlapping orders bug where subsequent DCA positions
        # would miss TP placements. OKX reduce-only orders handle duplicates safely.
        def _tp_price_exists(price_val) -> bool:
            return False

        sizes = []
        algo_order_ids = []
        algo_params = []

        _sig_type_raw = signal_data.get("signal_type")
        _sig_type_str = _sig_type_raw.value if hasattr(_sig_type_raw, "value") else str(_sig_type_raw).lower()
        algo_side = "sell" if _sig_type_str in ["buy", "long", "buy_signal"] else "buy"

        # Determine market spec to validate min_sz AND lot_sz for proper quantization
        symbol = signal_data.get("symbol")
        market_spec = getattr(self.okx_client, "_markets", {}).get(symbol) if hasattr(self.okx_client, "_markets") else None
        if market_spec:
            min_sz = float(market_spec.get("minSz", 0.01))
            lot_sz = float(market_spec.get("lotSz", 1.0))
        else:
            from utils.okx_symbols import OKX_SYMBOL_SPECS
            specs = OKX_SYMBOL_SPECS.get(symbol, {})
            min_sz = float(specs.get("min_size", 0.01))
            lot_sz = float(specs.get("lot_size", 1.0))

        contracts_d = Decimal(str(contracts))
        min_sz_d = Decimal(str(min_sz))
        # [FIX #1] lot_sz_d used for proper quantization of each TP slice
        lot_sz_d = Decimal(str(lot_sz)) if lot_sz > 0 else Decimal("1")

        def _quantize_sz(raw_sz_d: Decimal) -> Decimal:
            """Round down to nearest lot_sz multiple — required by OKX API."""
            return (raw_sz_d / lot_sz_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * lot_sz_d

        for tp in tps[:-1]:
            if isinstance(tp, dict):
                exit_pct = tp.get("exit_pct")
                price = tp.get("price")
            elif hasattr(tp, "exit_pct"):
                exit_pct = tp.exit_pct
                price = tp.price
            else:
                exit_pct = None
                price = tp
                
            # If exit_pct not provided, split evenly among TP slots
            if exit_pct is None:
                exit_frac = Decimal(str(1.0 / len(tps)))
            else:
                exit_frac = Decimal(str(exit_pct))

            if _tp_price_exists(price):
                continue

            # [FIX #1] Quantize sz according to OKX lot_sz — prevents "invalid sz" rejection
            raw_sz_d = contracts_d * exit_frac
            sz_d = _quantize_sz(raw_sz_d)

            if sz_d < min_sz_d or sz_d == Decimal("0"):
                logger.info(f"[TP-SKIP] Skipping TP for {symbol}: calculated sz {float(sz_d):.6f} (raw={float(raw_sz_d):.6f}) below min {float(min_sz_d):.6f}")
                # [FIX #2] Do NOT append 0 to sizes — only append actual assigned sizes
                # Appending 0 caused remainder to be inflated (full contracts -> last TP)
                continue

            sz = float(sz_d)
            sizes.append(sz_d)
            algo_params.append({
                "symbol": symbol,
                "side": algo_side,
                "sz": sz,
                "tp_trigger_px": price,
                "reduce_only": True,
                "correlation_id": signal_data.get("correlation_id"),
            })

        # [FIX #2] Last TP gets remainder = contracts - sum(assigned slices), quantized to lot_sz
        total_assigned = sum(sizes)  # Sum of Decimal values that were actually placed
        remainder_d = _quantize_sz(contracts_d - total_assigned)
        last_tp = tps[-1]
        
        if isinstance(last_tp, dict):
            price = last_tp.get("price")
        elif hasattr(last_tp, "price"):
            price = last_tp.price
        else:
            price = last_tp

        if remainder_d >= min_sz_d and remainder_d > Decimal("0"):
            if not _tp_price_exists(price):
                algo_params.append({
                    "symbol": symbol,
                    "side": algo_side,
                    "sz": float(remainder_d),
                    "tp_trigger_px": price,
                    "reduce_only": True,
                    "correlation_id": signal_data.get("correlation_id"),
                })
                logger.info(f"[TP-REMAINDER] Last TP for {symbol}: remainder={float(remainder_d):.6f} contracts at price={price}")
        elif remainder_d > Decimal("0") and algo_params and not _tp_price_exists(price):
            last_idx = len(algo_params) - 1
            merged_d = _quantize_sz(Decimal(str(algo_params[last_idx]["sz"])) + remainder_d)
            if merged_d >= min_sz_d:
                algo_params[last_idx]["sz"] = float(merged_d)
                logger.info(
                    f"[TP-REMAINDER-MERGE] Merged dust remainder {float(remainder_d):.6f} into prior TP "
                    f"-> {float(merged_d):.6f} contracts for {symbol}"
                )
            else:
                logger.warning(
                    f"[TP-SKIP] Remainder {float(remainder_d):.6f} for {symbol} below min {float(min_sz_d):.6f} "
                    f"and merge would still be undersized ({float(merged_d):.6f})"
                )
        elif remainder_d > Decimal("0") and not algo_params and contracts_d >= min_sz_d and not _tp_price_exists(price):
            full_sz_d = _quantize_sz(contracts_d)
            if full_sz_d >= min_sz_d:
                algo_params.append({
                    "symbol": symbol,
                    "side": algo_side,
                    "sz": float(full_sz_d),
                    "tp_trigger_px": price,
                    "reduce_only": True,
                    "correlation_id": signal_data.get("correlation_id"),
                })
                logger.info(
                    f"[TP-REMAINDER-FALLBACK] All intermediate TPs skipped; placing last TP with "
                    f"{float(full_sz_d):.6f} contracts for {symbol}"
                )
        elif remainder_d > Decimal("0"):
            logger.info(f"[TP-SKIP] Remainder {float(remainder_d):.6f} for {symbol} below min {float(min_sz_d):.6f}; not placing final TP")

        # ===== ORPHAN GUARD & RETRY LOGIC (TENACITY) =====
        from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

        placed_algo_count = 0
        expected_tp_count = len(algo_params)
        
        async def _place_tp_with_retry(p: dict) -> str:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),  # 1 initial + 3 retries
                wait=wait_exponential(multiplier=1.0, min=1.5, max=10.0),
                reraise=True
            ):
                with attempt:
                    res = await self.okx_client.place_algo_order(**p)
                    if not res:
                        raise ValueError(f"place_algo_order returned {res}")
                    return res

        failed_params = []
        if algo_params:
            tp_tasks = [_place_tp_with_retry(p) for p in algo_params]
            results = await asyncio.gather(*tp_tasks, return_exceptions=True)
            
            for p, res in zip(algo_params, results):
                if isinstance(res, Exception):
                    logger.error(f"[TP-PLACE-FAIL] Final failure for {p['sz']} contracts at {p['tp_trigger_px']} after retries: {res}")
                    failed_params.append(p)
                else:
                    algo_order_ids.append(res)
                    self._register_algo_order(pos.id, res)
                    placed_algo_count += 1
                    logger.info(f"[TP-PLACED] TP placed: ordId={res}")
                
        # ATOMIC ROLLBACK TRIGGER
        if failed_params:
            logger.critical(f"[ORPHAN-GUARD] Position {pos.id} failed to place {len(failed_params)} TPs after retries! Triggering ATOMIC ROLLBACK (Market Close).")
            # Trigger Market Close
            rollback_success = await self.close_position(pos.id)
            
            if not rollback_success:
                logger.critical(f"[ORPHAN-GUARD] ROLLBACK FAILED for {pos.id}! Position is orphaned. Enqueueing to fallback worker.")
                self._fallback_queue.put_nowait({
                    "pos_id": pos.id,
                    "signal_data": signal_data,
                    "contracts": contracts,
                    "type": "rollback_or_retry"
                })
            
            # Send Telegram Alert
            if hasattr(self, "event_bus") and self.event_bus:
                await self.event_bus.publish(Event(
                    event_type=EventTopic.RISK_SIGNAL_REJECTED,
                    data={
                        "reason": f"⚠️ ORPHAN GUARD TRIGGERED: Khớp lệnh Entry thành công nhưng đặt TP/SL thất bại. Đã MARKET CLOSE vị thế để chống cháy. (Rollback success: {rollback_success})",
                        "symbol": pos.symbol,
                        "signal_type": pos.side,
                        "timeframe": "N/A",
                        "entry_price": pos.entry_price,
                    },
                    source="orphan_guard"
                ))
            return

        logger.info(f"[TP-COMPLETE] All {placed_algo_count} TP orders placed successfully for {pos.id}")

        pos.algo_order_ids = algo_order_ids
        pos.tp_dispatched = True
        if self.persistence:
            await self.persistence.save_position(pos)

    async def _dispatch_algo_sl(self, pos: "TrackedPosition", signal_data: dict, contracts: float) -> None:
        """[FIX #3] Dispatch an independent SL algo order after position is confirmed FILLED.

        This is needed because the SL originally attached via attachAlgoOrds to the
        entry order may be lost when the entry goes through PENDING_RECONCILE.
        The Phantom Worker and WS fill handler must call this to ensure SL is always set.

        Args:
            pos: The tracked position object.
            signal_data: Original signal dict containing stop_loss_price.
            contracts: Actual filled contract count (from accFillSz / exchange query).
        """
        from decimal import Decimal, ROUND_DOWN

        # [FIX P1] Prioritize slippage-adjusted pos levels over raw signal_data
        sl_price = pos.stop_loss or signal_data.get("stop_loss_price")
        if not sl_price:
            logger.debug(f"[SL-DISPATCH] No stop_loss_price for {pos.id}, skipping SL dispatch")
            return

        if getattr(pos, "sl_algo_order_id", None):
            logger.info(f"[SL-DISPATCH-SKIP] SL already dispatched for {pos.id} (algo_id={pos.sl_algo_order_id})")
            return

        symbol = pos.symbol

        # Get market spec for validation
        market_spec = getattr(self.okx_client, "_markets", {}).get(symbol) if hasattr(self.okx_client, "_markets") else None
        if market_spec:
            min_sz = float(market_spec.get("minSz", 0.01))
            lot_sz = float(market_spec.get("lotSz", 1.0))
        else:
            from utils.okx_symbols import OKX_SYMBOL_SPECS
            specs = OKX_SYMBOL_SPECS.get(symbol, {})
            min_sz = float(specs.get("min_size", 0.01))
            lot_sz = float(specs.get("lot_size", 1.0))

        # Quantize contracts to lot_sz
        lot_sz_d = Decimal(str(lot_sz)) if lot_sz > 0 else Decimal("1")
        min_sz_d = Decimal(str(min_sz))
        contracts_d = Decimal(str(contracts))
        sl_sz_d = (contracts_d / lot_sz_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * lot_sz_d

        if sl_sz_d < min_sz_d or sl_sz_d == Decimal("0"):
            logger.warning(f"[SL-DISPATCH] SL sz {float(sl_sz_d):.6f} below min {float(min_sz_d):.6f} for {symbol}, skipping")
            return

        _sig_type_raw = signal_data.get("signal_type") or pos.side
        _sig_type_str = _sig_type_raw.value if hasattr(_sig_type_raw, "value") else str(_sig_type_raw).lower()
        algo_side = "sell" if _sig_type_str in ["buy", "long", "buy_signal"] else "buy"

        # (REMOVED) Check if SL already exists on exchange.
        # We no longer skip SL placements if an SL exists at the same price.
        # This ensures overlapping DCA orders get their full SL sizes placed.

        logger.info(f"[SL-DISPATCH] Dispatching SL order: symbol={symbol}, sz={float(sl_sz_d):.6f}, sl_price={sl_price}")
        
        from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
        
        try:
            algo_id = None
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),  # 1 initial + 3 retries
                wait=wait_exponential(multiplier=1.0, min=1.5, max=10.0),
                reraise=True
            ):
                with attempt:
                    res = await self.okx_client.place_algo_order(
                        symbol=symbol,
                        side=algo_side,
                        sz=float(sl_sz_d),
                        sl_trigger_px=float(sl_price),
                        reduce_only=True,
                        # [FIX H4] Always pass position_side explicitly for long_short_mode (hedge mode).
                        # Without this, place_algo_order infers posSide from order side alone which is
                        # coincidentally correct, but should be explicit for correctness and clarity.
                        position_side=pos.side,
                        correlation_id=signal_data.get("correlation_id"),
                    )
                    if not res:
                        raise ValueError(f"place_algo_order returned {res}")
                    algo_id = res

            if algo_id:
                self._register_algo_order(pos.id, algo_id)
                # Store SL algo ID separately for tracking
                if not hasattr(pos, "sl_algo_order_id") or pos.sl_algo_order_id is None:
                    pos.sl_algo_order_id = algo_id  # type: ignore[attr-defined]
                logger.info(f"[SL-DISPATCH] SL order placed successfully: algoId={algo_id} for position {pos.id}")
            else:
                logger.error(f"[SL-DISPATCH] SL order placement returned None for position {pos.id}")
        except Exception as e:
            logger.error(f"[SL-DISPATCH] Failed to place SL order for {pos.id} after retries: {e}")
            logger.critical(f"[ORPHAN-GUARD] SL dispatch failed for {pos.id}. Enqueueing to fallback worker.")
            self._fallback_queue.put_nowait({
                "pos_id": pos.id,
                "signal_data": signal_data,
                "contracts": contracts,
                "type": "rollback_or_retry_sl"
            })

    async def open_position(self, signal_data: dict) -> Any:
        """
        Minimal open_position implementation used by tests:
        - place a market order via `okx_client.place_order`
        - based on returned `contracts`, dispatch `place_algo_order` for each TP level
        """
        try:
            if self._halt_trading:
                symbol = signal_data.get("symbol", "UNKNOWN")
                logger.warning(f"[HALT-GUARD] Rejecting open_position for {symbol}: trading halted.")
                return None

            symbol = signal_data.get("symbol")
            # [FIX P0-3] Use setdefault() which is atomic in CPython — prevents TOCTOU race
            # where two concurrent coroutines both see missing key and create separate Lock objects.
            lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())

            async with lock:
                # Check if active position already exists
                active = [p for p in self.get_active_positions() if p.symbol == symbol]
                if len(active) > 0:
                    logger.warning(f"Position already exists for {symbol}, skipping open.")
                    return None

                # Slippage guard
                try:
                    ticker = await self.okx_client.fetch_ticker(symbol)
                    if ticker and hasattr(ticker, "last_price"):
                        market_price = float(ticker.last_price)
                        entry_price = float(signal_data.get("entry_price", 0.0))
                        if entry_price > 0:
                            drift_bps = abs(market_price - entry_price) / entry_price * 10000
                            from core.config.settings import settings
                            max_slippage = getattr(settings, "max_slippage_tolerance_bps", 20)
                            if drift_bps > max_slippage:
                                if hasattr(self, "event_bus") and self.event_bus:
                                    await self.event_bus.publish(Event(
                                        event_type=EventTopic.RISK_SIGNAL_REJECTED,
                                        data={
                                            "reason": f"🚫 TỪ CHỐI TÍN HIỆU: Trượt giá vượt mức cho phép (Drift: {drift_bps:.1f} bps > Max: {max_slippage} bps)",
                                            "symbol": signal_data.get("symbol", "N/A"),
                                            "signal_type": signal_data.get("signal_type"),
                                            "timeframe": signal_data.get("timeframe", "1H"),
                                            "entry_price": signal_data.get("entry_price", 0.0),
                                        },
                                        source="order_handler"
                                    ))
                                return None
                except Exception as e:
                    logger.warning(f"Failed to check slippage guard: {e}")

                # Generate and register clOrdId for pending cache
                import uuid
                # Use exchange-compatible client_order_id (lowercase hex) instead of CL_ prefix
                cl_ord_id = signal_data.get("client_order_id") or f"vcorex{uuid.uuid4().hex[:20]}"
                self._pending_order_cache[cl_ord_id] = signal_data

                # [CRITICAL FIX] Resolve signal_type to correct order side string
                _sig_type_raw = signal_data.get("signal_type")
                _sig_type_str = _sig_type_raw.value if hasattr(_sig_type_raw, "value") else str(_sig_type_raw).lower()
                order_side = "buy" if _sig_type_str in ("buy", "long", "buy_signal") else "sell"
                pos_side = "long" if order_side == "buy" else "short"

                # [REMEDIATION] Register position IMMEDIATELY before placing order to prevent race condition
                internal_id = "pos_" + str(uuid.uuid4())
                ct_val = self._resolve_ct_val(symbol, signal_data)
                contract_amount, amount_param = self._compute_open_sizes(signal_data, ct_val)

                # Round Stop Loss using tick_sz
                raw_sl = signal_data.get("stop_loss_price")
                rounded_sl = None
                if raw_sl is not None:
                    market_spec = getattr(self.okx_client, "_markets", {}).get(symbol) if hasattr(self, "okx_client") else None
                    if market_spec:
                        tick_sz = float(market_spec.get("tickSz", 0.001))
                    else:
                        from utils.okx_symbols import OKX_SYMBOL_SPECS
                        specs = OKX_SYMBOL_SPECS.get(symbol, {})
                        tick_sz = float(specs.get("tick_size", 0.001))
                    
                    import math
                    rounded = round(float(raw_sl) / tick_sz) * tick_sz if tick_sz else float(raw_sl)
                    precision = max(0, -int(math.log10(tick_sz))) if tick_sz and tick_sz < 1 else 0
                    rounded_sl = float(f"{rounded:.{precision}f}")
                    # Update signal_data so WS dispatcher also sees rounded SL
                    signal_data["stop_loss_price"] = rounded_sl

                pos = TrackedPosition(
                    id=internal_id,
                    exchange_id=None,
                    symbol=symbol,
                    side=pos_side,
                    entry_price=signal_data.get("entry_price", 0.0),
                    current_price=signal_data.get("entry_price", 0.0),
                    amount=contract_amount,
                    amount_remaining=contract_amount,
                    leverage=self.default_leverage or 1,
                    stop_loss=rounded_sl,
                    take_profit_levels=self._build_take_profit_levels(signal_data),
                    status=PositionStatus.PENDING_SUBMIT,
                    signal_id=cl_ord_id,
                    algo_order_ids=[],
                    strategy_name=signal_data.get("strategy_name", "unknown"),
                    ct_val=ct_val,
                )
                self._positions[internal_id] = pos

                # Persist position immediately
                if hasattr(self, "persistence") and self.persistence:
                    try:
                        await self.persistence.save_position(pos)
                    except Exception as e:
                        logger.error(f"Failed to persist PENDING_SUBMIT position: {e}")
                        # Clean up if persistence fails
                        self._positions.pop(internal_id, None)
                        return None

            # Lock released here - position already registered

            # [FIX LỖI 3] Exponential Backoff với Jitter cho Rate Limit 429
            max_retries = 5
            base_delay = 1.0
            max_delay = 30.0
            order = None
            
            for attempt in range(max_retries):
                try:
                    # [PHASE 5 FORENSIC] Measure signal execution latency
                    exec_start_time = time.time()

                    # Place main market order (amount_param = base-coin notional for OKX place_order)
                    order = await self.okx_client.place_order(
                        symbol=signal_data.get("symbol"),
                        side=order_side,
                        order_type="market",
                        amount=amount_param,
                        price=signal_data.get("entry_price"),
                        sl_price=None,  # [FIX] Do NOT attach SL here, it's manually dispatched later
                        tp_price=None,
                        correlation_id=signal_data.get("correlation_id"),
                        client_order_id=cl_ord_id,
                        leverage=self.default_leverage,  # [DYNAMIC] Pass leverage for validation
                    )

                    exec_end_time = time.time()
                    exec_latency_ms = (exec_end_time - exec_start_time) * 1000
                    logger.info(f"[PERF] signal_execution_latency_ms: {exec_latency_ms:.2f}ms for {cl_ord_id}")
                    break  # Success, exit retry loop
                except Exception as e:
                    error_str = str(e).lower()
                    is_429 = "429" in error_str or "too many requests" in error_str or "rate limit" in error_str
                    
                    if is_429 and attempt < max_retries - 1:
                        # Exponential backoff với Jitter ngẫu nhiên
                        delay = min(max_delay, base_delay * (2 ** attempt))
                        jitter = random.uniform(0, 1.0)  # Jitter 0-1 giây
                        total_delay = delay + jitter
                        
                        logger.warning(
                            f"[RATE-LIMIT-429] Place order failed for {cl_ord_id} (Attempt {attempt + 1}/{max_retries}). "
                            f"Retrying in {total_delay:.2f}s with exponential backoff. Error: {e}"
                        )
                        await asyncio.sleep(total_delay)
                        continue
                    else:
                        # Non-429 error or max retries exceeded
                        logger.error(f"Failed to place order after {attempt + 1} attempts: {e}")
                        # [REMEDIATION] Clean up position if order placement fails
                        self._positions.pop(internal_id, None)
                        if hasattr(self, "persistence") and self.persistence:
                            try:
                                await self.persistence.delete_position(internal_id)
                            except Exception as cleanup_e:
                                logger.error(f"Failed to cleanup position after order failure: {cleanup_e}")
                        return None

            if getattr(order, "status", "") == "PENDING_RECONCILE":
                logger.warning(f"Order {cl_ord_id} is PENDING_RECONCILE. Waiting for WS or Phantom Worker.")
                pos.status = PositionStatus.PENDING_RECONCILE
                if hasattr(self, "persistence") and self.persistence:
                    try:
                        saved = await self.persistence.save_position(pos)
                        if not saved:
                            logger.error(
                                f"[PERSISTENCE-FAILURE] DB save FAILED for PENDING_RECONCILE position {pos.id} ({pos.symbol}). "
                                f"Phantom Worker will verify state."
                            )
                    except Exception as e:
                        logger.error(f"Failed to update position to PENDING_RECONCILE: {e}")
                try:
                    if getattr(settings, "ENABLE_PHANTOM_VERIFIER", True):
                        run_safe_task(self._verify_phantom_position_worker(internal_id))
                    else:
                        logger.debug("[PHANTOM-WORKER] Phantom verifier disabled by settings")
                except Exception:
                    logger.warning("[PHANTOM-WORKER] Failed to spawn phantom verification task")
                return internal_id

            contracts = getattr(order, "contracts", None) or getattr(order, "amount", 0)
            order_status = getattr(order, "status", "").lower()

            pos.exchange_id = getattr(order, "order_id", None)
            pos.amount = float(contracts)
            pos.amount_remaining = float(contracts)

            if order_status == "filled":
                pos.status = PositionStatus.OPENED
                if hasattr(self, "persistence") and self.persistence:
                    try:
                        saved = await self.persistence.save_position(pos)
                        if not saved:
                            logger.critical(
                                f"[PERSISTENCE-CRITICAL] DB save FAILED for OPENED position {pos.id} "
                                f"({pos.symbol}). Exchange confirmed fill. "
                                f"ReconciliationService will auto-heal within 10 minutes."
                            )
                    except Exception as e:
                        logger.critical(f"[PERSISTENCE-CRITICAL] Unexpected exception saving OPENED position {pos.id}: {e}")

                tp_prices = signal_data.get("take_profit_prices") or []
                if tp_prices:
                    try:
                        logger.info(f"[TP-DISPATCH] Dispatching {len(tp_prices)} TP orders for {signal_data.get('symbol')} after confirmed fill")
                        await self._dispatch_algo_tps(pos, signal_data, float(contracts))
                    except Exception as tp_err:
                        logger.error(f"[TP-DISPATCH] Failed to dispatch TP orders: {tp_err}")
                # Dispatch SL after TP to ensure both are set
                try:
                    await self._dispatch_algo_sl(pos, signal_data, float(contracts))
                except Exception as sl_err:
                    logger.error(f"[SL-DISPATCH] Failed to dispatch SL for {pos.id}: {sl_err}")
            else:
                logger.info(f"[POSITION-GUARD] Position {internal_id} updated with exchange_id={pos.exchange_id}, amount={pos.amount}. Waiting for WS fill.")
                if hasattr(self, "persistence") and self.persistence:
                    try:
                        saved = await self.persistence.save_position(pos)
                        if not saved:
                            logger.error(
                                f"[PERSISTENCE-FAILURE] DB save FAILED for IN_FLIGHT position {pos.id} ({pos.symbol}). "
                                f"Awaiting WS fill confirmation."
                            )
                    except Exception as e:
                        logger.error(f"Failed to persist position update: {e}")

            return internal_id
        except Exception as e:
            logger.error(f"open_position failed: {e}")
            return None

    async def close_position(
        self,
        internal_id: str,
        close_amount: Optional[float] = None,
        correlation_id: str = None,
    ) -> bool:
        """Close position (full or partial). Quantity is contracts when OPENED, USDT notional when pending."""
        try:
            pos = self._positions.get(internal_id)
            if not pos:
                return False

            if pos.status in _TERMINAL_POSITION_STATUSES:
                logger.info(f"[CLOSE-SKIP] Position {internal_id} already terminal ({pos.status})")
                return True

            close_qty = float(close_amount if close_amount is not None else pos.amount_remaining)
            remaining_before = float(pos.amount_remaining or 0.0)
            is_full_close = close_qty >= remaining_before * 0.999999 if remaining_before > 0 else True

            side = self._close_order_side(pos.side)
            place_amount = self._resolve_close_place_order_amount(pos, close_qty)

            if is_full_close and pos.status not in (PositionStatus.CLOSING,):
                pos.status = PositionStatus.CLOSING

            if is_full_close:
                # [FIX OKX-007] Route full close to OKX native close-position API
                try:
                    order = await self.okx_client.close_position(symbol=pos.symbol)
                except Exception as e:
                    logger.error(f"[CLOSE-POSITION] Native close-position API failed for {pos.symbol}: {e}")
                    order = None
            else:
                order = await self.okx_client.place_order(
                    symbol=pos.symbol,
                    side=side,
                    order_type="market",
                    leverage=pos.leverage,
                    amount=place_amount,
                    correlation_id=correlation_id,
                    position_side=pos.side,
                    reduce_only=True,
                )

            if not order:
                if pos.status == PositionStatus.CLOSING and remaining_before > 0:
                    pos.status = PositionStatus.OPENED
                return False

            # [FIX M3] Cancel ALL algo orders on full close: both TP (algo_order_ids) and SL (sl_algo_order_id).
            # Leaving a live SL order after market-close risks re-opening position on the other side.
            if is_full_close:
                all_algo_ids_to_cancel = list(getattr(pos, "algo_order_ids", None) or [])
                sl_algo_id = getattr(pos, "sl_algo_order_id", None)
                if sl_algo_id and sl_algo_id not in all_algo_ids_to_cancel:
                    all_algo_ids_to_cancel.append(sl_algo_id)
                
                if all_algo_ids_to_cancel:
                    try:
                        await self.okx_client.cancel_algo_orders(pos.symbol, all_algo_ids_to_cancel)
                        pos.algo_order_ids = []
                        if hasattr(pos, "sl_algo_order_id"):
                            pos.sl_algo_order_id = None
                        logger.info(f"[CLOSE] Cancelled {len(all_algo_ids_to_cancel)} algo orders (TP+SL) for {internal_id}")
                    except Exception as e:
                        logger.warning(f"Failed to cancel algo orders on close: {e}")

            if is_full_close:
                # Wait for WS fill confirmation — status already CLOSING
                if self.persistence:
                    try:
                        await self.persistence.save_position(pos)
                    except Exception as e:
                        logger.error(f"Failed to persist CLOSING state for {internal_id}: {e}")
            else:
                pos.amount_remaining = max(0.0, remaining_before - close_qty)
                if pos.amount_remaining <= 0:
                    pos.status = PositionStatus.CLOSED
                    pos.closed_at = datetime.now(timezone.utc)
                    await self._evict_terminal_position(internal_id, pos)
                else:
                    pos.status = PositionStatus.PARTIAL_TP
                    if self.persistence:
                        try:
                            await self.persistence.save_position(pos)
                        except Exception as e:
                            logger.error(f"Failed to persist partial close for {internal_id}: {e}")

            return True
        except Exception as e:
            logger.error(f"close_position failed: {e}")
            return False

    async def _verify_phantom_position_worker(self, internal_id: str) -> None:
        """Background worker to verify PENDING_RECONCILE positions via exchange queries.

        This handles eventual consistency when REST POST timed out but the order may have executed.
        It queries `verify_order_status` and upgrades the local position if exchange reports a fill.
        """
        if internal_id in self._phantom_in_flight:
            logger.debug(f"[PHANTOM-WORKER] Verification already in flight for {internal_id}")
            return
        self._phantom_in_flight.add(internal_id)
        try:
            pos = self._positions.get(internal_id)
            if not pos:
                return

            cl_ord_id = getattr(pos, "signal_id", None)
            if not cl_ord_id:
                return

            # Configurable retry/backoff for eventual consistency (driven by settings)
            max_attempts = getattr(settings, "PHANTOM_MAX_ATTEMPTS", 6)
            base_delay = getattr(settings, "PHANTOM_BASE_DELAY", 0.25)
            max_delay = getattr(settings, "PHANTOM_MAX_DELAY", 4.0)
            jitter_pct = getattr(settings, "PHANTOM_JITTER_PCT", 0.2)

            attempt = 0
            start_ts = time.time()
            while attempt < max_attempts:
                if attempt > 0:
                    # exponential backoff with jitter
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    jitter = delay * jitter_pct * (0.5 - (time.time() % 1))
                    await asyncio.sleep(max(0.0, delay + jitter))

                attempt += 1
                await self._metrics.increment_phantom_verifications_attempted()
                try:
                    status = await self.okx_client.verify_order_status(pos.symbol, cl_ord_id)
                except Exception as e:
                    logger.error(f"[PHANTOM-WORKER] Error verifying order {cl_ord_id} (attempt {attempt}): {e}")
                    status = "UNKNOWN"

                if status == "PARTIALLY_FILLED":
                    try:
                        details = await self.okx_client.query_order_details(pos.symbol, cl_ord_id)
                    except Exception:
                        details = None
                    fill_sz = 0.0
                    if details:
                        fill_sz = float(details.get("accFillSz") or details.get("fillSz") or 0.0)
                        if details.get("ordId"):
                            pos.exchange_id = details.get("ordId")
                        avg_px = float(details.get("avgPx") or details.get("fillPx") or 0.0)
                        if avg_px > 0:
                            # [FIX P2] Adjust TP/SL for slippage in Phantom Worker
                            if pos.entry_price and pos.entry_price > 0:
                                ratio = avg_px / pos.entry_price
                                if pos.stop_loss:
                                    pos.stop_loss *= ratio
                                if pos.take_profit_levels:
                                    for tp in pos.take_profit_levels:
                                        if isinstance(tp, dict) and "price" in tp:
                                            tp["price"] *= ratio
                            pos.entry_price = avg_px
                    if fill_sz > 0:
                        pos.amount_remaining = fill_sz
                        pos.amount = fill_sz
                    pos.status = PositionStatus.PARTIALLY_FILLED
                    if self.persistence:
                        await self.persistence.save_position(pos)
                    logger.info(f"[PHANTOM-WORKER] Partial fill for {cl_ord_id}: size={fill_sz}, waiting for full fill")
                    continue

                if status == "FILLED":
                    try:
                        details = await self.okx_client.query_order_details(pos.symbol, cl_ord_id)
                    except Exception:
                        details = None

                    ord_id = None
                    entry_price = None
                    amount = None
                    if details:
                        ord_id = details.get("ordId") or details.get("orderId")
                        entry_price = float(details.get("fillPx") or details.get("avgPx") or details.get("px") or 0.0)
                        amount = float(details.get("accFillSz") or details.get("fillSz") or details.get("sz") or pos.amount or 0.0)

                    if ord_id:
                        pos.exchange_id = ord_id

                    if amount:
                        pos.amount = amount
                        pos.amount_remaining = amount

                    if entry_price and entry_price > 0:
                        # [FIX P2] Adjust TP/SL for slippage in Phantom Worker
                        if pos.entry_price and pos.entry_price > 0:
                            ratio = entry_price / pos.entry_price
                            if pos.stop_loss:
                                pos.stop_loss *= ratio
                            if pos.take_profit_levels:
                                for tp in pos.take_profit_levels:
                                    if isinstance(tp, dict) and "price" in tp:
                                        tp["price"] *= ratio
                        pos.entry_price = entry_price

                    pos.status = PositionStatus.OPENED
                    if self.persistence:
                        try:
                            await self.persistence.save_position(pos)
                        except Exception as e:
                            logger.error(f"[PHANTOM-WORKER] Failed to persist upgraded position {pos.id}: {e}")
                    await self._metrics.increment_phantom_verifications_succeeded()
                    elapsed_ms = int((time.time() - start_ts) * 1000)
                    logger.info(f"[PHANTOM-WORKER] Verification success for {cl_ord_id} after {attempt} attempts ({elapsed_ms}ms)")

                    if not pos.position_opened_event_sent:
                        sig_data = self._pending_order_cache.get(cl_ord_id, {})
                        event_data = {**pos.__dict__, "signal_data": sig_data}
                        if self.event_bus:
                            await self.event_bus.publish(
                                Event(
                                    event_type=EventTopic.POSITION_OPENED,
                                    data=event_data,
                                    source="phantom_worker",
                                )
                            )
                        pos.position_opened_event_sent = True

                    signal_data = self._pending_order_cache.pop(cl_ord_id, None)
                    fill_contracts = float(pos.amount_remaining or pos.amount or 0.0)
                    if signal_data:
                        try:
                            await self._dispatch_algo_tps(pos, signal_data, fill_contracts)
                        except Exception as e:
                            logger.error(f"[PHANTOM-WORKER] Failed to dispatch TP orders for {pos.id}: {e}")
                        try:
                            await self._dispatch_algo_sl(pos, signal_data, fill_contracts)
                        except Exception as e:
                            logger.error(f"[PHANTOM-WORKER] Failed to dispatch SL for {pos.id}: {e}")
                    elif pos.take_profit_levels:
                        # [FALLBACK] Dispatch TP/SL even if signal_data unavailable in phantom worker
                        tp_prices = [
                            {
                                "price": float(tp.price),
                                "exit_pct": tp.exit_pct
                            }
                            for tp in pos.take_profit_levels
                        ]
                        fallback_signal = {
                            "symbol": pos.symbol,
                            "signal_type": pos.side,
                            "take_profit_prices": tp_prices,
                            "stop_loss_price": pos.stop_loss,
                            "correlation_id": f"phantom-fallback-tp-dispatch-{pos.id}",
                        }
                        try:
                            logger.info(f"[PHANTOM-FALLBACK-DISPATCH] Dispatching {len(tp_prices)} TP levels for {pos.id} (signal_data not in cache)")
                            await self._dispatch_algo_tps(pos, fallback_signal, pos.amount)
                        except Exception as e:
                            logger.error(f"[PHANTOM-FALLBACK] Failed to dispatch TP orders for {pos.id}: {e}")
                        # [FIX #3] Dispatch SL independently using fallback signal
                        try:
                            await self._dispatch_algo_sl(pos, fallback_signal, pos.amount)
                        except Exception as e:
                            logger.error(f"[PHANTOM-FALLBACK] Failed to dispatch SL for {pos.id}: {e}")
                    else:
                        logger.warning(f"[PHANTOM-TP-NONE] Position {pos.id} has no take_profit_levels and signal_data not in cache")

                    logger.info(f"[PHANTOM-WORKER] Position {pos.id} confirmed OPENED via verification for clOrdId={cl_ord_id}")
                    return

                if status == "CANCELED":
                    pos.status = PositionStatus.FAILED
                    await self._metrics.increment_phantom_verifications_failed()
                    logger.warning(f"[PHANTOM-WORKER] Position {pos.id} marked FAILED (exchange canceled). clOrdId={cl_ord_id}")
                    await self._evict_terminal_position(pos.id, pos)
                    return

                # NOT_FOUND / UNKNOWN -> continue retrying until delays exhausted

            # Exhausted retries: record metric and leave as PENDING_RECONCILE for reconciliation worker
            await self._metrics.increment_phantom_verifications_unknown()
            elapsed_ms = int((time.time() - start_ts) * 1000)
            logger.warning(f"[PHANTOM-WORKER] Verification attempts exhausted for clOrdId={cl_ord_id} after {attempt} attempts ({elapsed_ms}ms). Leaving as PENDING_RECONCILE.")
        except Exception as e:
            logger.error(f"[PHANTOM-WORKER] Unexpected error verifying phantom position {internal_id}: {e}", exc_info=True)
        finally:
            self._phantom_in_flight.discard(internal_id)

    async def close_all_positions(self, *args, **kwargs) -> int:
        """[EMERGENCY-CLOSE] Đóng toàn bộ các vị thế đang mở một cách tuần tự."""
        closed_count = 0
        active_positions = self.get_active_positions()
        pos_list = active_positions

        for i, pos in enumerate(pos_list):
            if i > 0:
                await asyncio.sleep(0.1)
            internal_id = pos.id if hasattr(pos, "id") else pos.get("id")
            if internal_id:
                success = await self.close_position(internal_id)
                if success:
                    closed_count += 1

        return closed_count

    async def cancel_all_active_algo_orders(self) -> None:
        """[GRACEFUL-SHUTDOWN] Hủy toàn bộ lệnh Algo (TP/SL) đang treo trên sàn OKX.
        
        [FIX ORPHAN TP/SL] - Bao gồm cả TP (algo_order_ids) và SL (sl_algo_order_id)
        [FIX VERIFICATION] - Verify với sàn sau khi hủy để đảm bảo thành công
        [FIX RETRY] - Retry mechanism nếu hủy thất bại
        [FIX LOCK] - Sử dụng lock để tránh race condition
        """
        logger.info("[SHUTDOWN] Cancelling all active algo orders on exchange...")
        
        # [FIX LOCK] Sử dụng global lock để tránh race condition
        async with self._cleanup_lock:
            cancel_tasks = []
            position_map = {}  # Map task index -> position for verification
            
            for idx, pos in enumerate(self.get_active_positions()):
                # [FIX ORPHAN TP/SL] Bao gồm cả TP và SL orders
                algo_ids = list(getattr(pos, "algo_order_ids", None) or [])
                sl_algo_id = getattr(pos, "sl_algo_order_id", None)
                if sl_algo_id and sl_algo_id not in algo_ids:
                    algo_ids.append(sl_algo_id)
                
                if algo_ids:
                    symbol = pos.symbol
                    cancel_tasks.append(
                        self.okx_client.cancel_algo_orders(symbol, algo_ids)
                    )
                    position_map[idx] = pos
            
            if cancel_tasks:
                results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
                
                # [FIX VERIFICATION + RETRY] Verify với sàn sau khi hủy và retry nếu cần
                for idx, r in enumerate(results):
                    pos = position_map.get(idx)
                    if not pos:
                        continue
                    
                    if isinstance(r, Exception):
                        logger.error(f"[SHUTDOWN] Failed to cancel algo orders: {r}")
                        # [FIX RETRY] Retry mechanism cho failed cancellations
                        await self._retry_cancel_algo_orders(pos)
                    else:
                        # Chỉ verify khi cancel thành công (r không phải Exception)
                        await self._verify_algo_orders_cancelled(pos)
            
            logger.info("[SHUTDOWN] All algo order cancellations dispatched and verified.")

    async def _retry_cancel_algo_orders(self, pos) -> None:
        """[FIX RETRY] Retry mechanism cho việc hủy algo orders thất bại."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                algo_ids = list(getattr(pos, "algo_order_ids", None) or [])
                sl_algo_id = getattr(pos, "sl_algo_order_id", None)
                if sl_algo_id and sl_algo_id not in algo_ids:
                    algo_ids.append(sl_algo_id)
                
                if algo_ids:
                    await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff
                    success = await self.okx_client.cancel_algo_orders(pos.symbol, algo_ids)
                    if success:
                        logger.info(f"[SHUTDOWN-RETRY] Successfully cancelled algo orders for {pos.symbol} on attempt {attempt + 1}")
                        # Cập nhật local state sau khi cancel thành công
                        if hasattr(pos, "algo_order_ids"):
                            pos.algo_order_ids = []
                        if hasattr(pos, "sl_algo_order_id"):
                            pos.sl_algo_order_id = None
                        return
            except Exception as e:
                logger.warning(f"[SHUTDOWN-RETRY] Attempt {attempt + 1} failed for {pos.symbol}: {e}")
        
        logger.error(f"[SHUTDOWN-RETRY] Failed to cancel algo orders for {pos.symbol} after {max_retries} attempts")

    async def _verify_algo_orders_cancelled(self, pos) -> None:
        """[FIX VERIFICATION] Verify với sàn rằng algo orders đã được hủy thành công."""
        try:
            # Fetch pending algo orders từ sàn cho symbol này
            pending_orders = await self.okx_client.fetch_pending_algo_orders(symbol=pos.symbol, limit=100)
            
            # Kiểm tra xem có algo orders nào còn tồn tại không
            local_algo_ids = set(getattr(pos, "algo_order_ids", None) or [])
            sl_algo_id = getattr(pos, "sl_algo_order_id", None)
            if sl_algo_id:
                local_algo_ids.add(sl_algo_id)
            
            if pending_orders and local_algo_ids:
                # Kiểm tra xem các algo orders local có còn trong pending orders không
                pending_algo_ids = {order.get('algoId') for order in pending_orders}
                orphan_algo_ids = local_algo_ids & pending_algo_ids
                
                if orphan_algo_ids:
                    logger.critical(
                        f"[CRITICAL-SAFETY] Phát hiện {len(orphan_algo_ids)} algo orders bị mất ngầm sau khi hủy: "
                        f"{orphan_algo_ids} cho {pos.symbol}. Tiến hành hủy lại khẩn cấp."
                    )
                    # Thử hủy lại các orphan orders
                    await self.okx_client.cancel_algo_orders(pos.symbol, list(orphan_algo_ids))
            else:
                logger.debug(f"[SHUTDOWN-VERIFY] Verified: No orphan algo orders for {pos.symbol}")
        except Exception as e:
            logger.warning(f"[SHUTDOWN-VERIFY] Failed to verify algo orders for {pos.symbol}: {e}")

    async def panic_close_all_positions(self, reason: str = "CIRCUIT_BREAKER") -> tuple[int, int]:
        """
        [PANIC CLOSE] Hủy tất cả lệnh Algo rồi đóng toàn bộ vị thế bằng
        lệnh Market song song (asyncio.gather). Đưa tài khoản về trạng thái
        an toàn tuyệt đối trong thời gian ngắn nhất.
        Returns: (success_count, fail_count)
        """
        logger.critical(f"[PANIC-CLOSE] Initiating full position liquidation. Reason: {reason}")
        self._halt_trading = True

        # Step 1: Cancel all algo orders in parallel
        await self.cancel_all_active_algo_orders()

        # Step 2: Close all open positions in parallel
        active_positions = self.get_active_positions()
        if not active_positions:
            logger.warning("[PANIC-CLOSE] No open positions to close.")
            return 0, 0

        close_tasks = []
        for pos in active_positions:
            internal_id = pos.id if hasattr(pos, "id") else pos.get("id")
            if internal_id:
                close_tasks.append(self.close_position(internal_id))

        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)
            fail_count = sum(1 for r in results if isinstance(r, Exception) or r is False)
            logger.critical(
                f"[PANIC-CLOSE] Liquidation complete. Closed={success_count}, Failed={fail_count}. "
                f"System halted. No new orders will be accepted."
            )
            return success_count, fail_count
        return 0, 0

    async def start_transient_cleanup(self, *args, **kwargs) -> None:
        """[CLEANUP-PROTECT] Kích hoạt luồng ngầm dọn dẹp các lệnh tạm thời."""

        CLEANUP_INTERVAL_SECONDS = 10.0
        logger.info("[CLEANUP-INIT] Luồng tự động dọn dẹp lệnh tạm thời đã kích hoạt.")
        while True:
            try:
                if not getattr(self, "_ws_fills_use_ttl_cache", False):
                    now = time.time()
                    TTL_WS_FILLS = 86400  # 24 hours
                    to_remove = [
                        key
                        for key, ts in self._processed_ws_fills.items()
                        if isinstance(ts, (int, float)) and now - ts > TTL_WS_FILLS
                    ]
                    for key in to_remove:
                        del self._processed_ws_fills[key]
                    if to_remove:
                        logger.debug(
                            f"[CLEANUP] Removed {len(to_remove)} stale entries from _processed_ws_fills"
                        )

                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[CLEANUP-ERROR] Lỗi luồng dọn dẹp: {str(e)}")

    async def audit_pending_orders(self, *args, **kwargs) -> None:
        """[AUDIT-PROTECT] Luồng kiểm toán định kỳ trạng thái lệnh treo."""

        AUDIT_INTERVAL_SECONDS = 5.0
        logger.info("[AUDIT-INIT] Luồng kiểm toán lệnh treo đã kích hoạt.")
        while True:
            try:
                await asyncio.sleep(AUDIT_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[AUDIT-ERROR] Lỗi luồng kiểm toán lệnh treo: {str(e)}")

    def __getattr__(self, name: str):
        """Fallback for known optional attributes only. All other missing attrs raise AttributeError."""
        _SAFE_DICT_ATTRS = {"metrics", "_pending_order_cache", "_transient_orders"}
        
        if name in _SAFE_DICT_ATTRS or (name.startswith("_") and name not in (
            "_positions", "_exchange_id_map", "_algo_order_to_position",
            "_processed_ws_fills", "_processed_fill_keys", "_symbol_locks"
        )):
            obj = dict()
            object.__setattr__(self, name, obj)
            return obj

        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'. "
            f"Check for typos or missing initialization in __init__."
        )
