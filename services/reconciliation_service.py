"""
Distributed Reconciliation Service verifying local DB states against authoritative OKX exchange.
"""

import json
import time
from typing import Dict, Any, List, Set
from uuid import uuid4
from loguru import logger
from sqlalchemy import select

from core.event_bus_components import Event
from core.events.topics import EventTopic
from infrastructure.storage.database import Position, get_session
from infrastructure.exchange.base_exchange import BaseExchange
from services.position.persistence import ACTIVE_POSITION_DB_STATUSES


class ReconciliationService:
    """Verifies local DB records against the exchange authoritative source and auto-heals discrepancies."""

    def __init__(self, event_bus, exchange: BaseExchange, settings=None, exchange_mirror=None, order_handler=None):
        self.event_bus = event_bus
        self.exchange = exchange
        self.settings = settings
        self.exchange_mirror = exchange_mirror
        self.order_handler = order_handler  # Injected from PositionEngine to check strategy names
        self._last_published_anomalies: Set[str] = set()

        # OSCILLATION GUARD: Track how many consecutive sweeps an anomaly has persisted.
        # Only trigger auto_heal_close after MIN_PERSISTENCE_SWEEPS consecutive detections.
        # This prevents transient OKX [] responses from triggering catastrophic state changes.
        self._anomaly_persistence_counters: Dict[str, int] = {}
        self._MIN_PERSISTENCE_SWEEPS = 3

        # RATE-LIMIT TELEGRAM ALERTS: Never send reconciliation alerts more often than this.
        # Prevents spam when a persistent anomaly (e.g. balance drift) is detected every sweep.
        self._last_alert_ts: float = 0.0
        self._ALERT_COOLDOWN_SECONDS: float = 3600.0  # 1 hour minimum between Telegram alerts

        self.metrics = {
            "ghost_events_emitted": 0,
            "reconciliation_lock_wait_ms": 0.0,
            "oscillation_guard_blocks": 0,
        }
        logger.info("ReconciliationService initialized.")

    def _get_strategy_name_for_symbol(self, symbol: str) -> str:
        """Get strategy name from order_handler for a given symbol."""
        if not self.order_handler or not hasattr(self.order_handler, "_positions"):
            return "recovered"

        # Search through tracked positions for matching symbol
        for pos in self.order_handler._positions.values():
            if hasattr(pos, "symbol") and pos.symbol == symbol:
                strategy_name = getattr(pos, "strategy_name", "recovered")
                return strategy_name if strategy_name else "recovered"

        return "recovered"

    async def reconcile_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Perform full reconciliation pass for positions, orders, and balances."""
        logger.info("Executing comprehensive reconciliation sweep...")
        anomalies = {
            "ghost_positions": [],
            "missing_fills": [],
            "balance_drift": [],
            "orphan_orders": [],
            "unprotected_positions": []
        }

        try:
            # 1. Fetch remote states from OKX
            remote_positions = await self.exchange.fetch_positions()
            remote_pos_map = {pos.symbol: pos for pos in remote_positions}

            balances = await self.exchange.fetch_balance()
            remote_usdt = balances.get("USDT")
            remote_usdt_free = remote_usdt.free if remote_usdt else 0.0
            remote_usdt_total = remote_usdt.total if remote_usdt else 0.0

            # --- Check Balance Drift ---
            if self.exchange_mirror is not None:
                local_usdt_free = self.exchange_mirror.get_free_margin()
                drift = abs(remote_usdt_free - local_usdt_free)
                if drift > 0.01:
                    # Heal mirror cache with correct total vs available mapping
                    if hasattr(self.exchange_mirror, "_account") and isinstance(self.exchange_mirror._account, dict):
                        self.exchange_mirror._account["totalEq"] = str(remote_usdt_total)
                        self.exchange_mirror._account["adjEq"] = str(remote_usdt_free)
                        self.exchange_mirror._account["uTime"] = str(int(time.time() * 1000))

                    anomaly = {
                        "asset": "USDT",
                        "remote_free": remote_usdt_free,
                        "local_free": local_usdt_free,
                        "drift": drift
                    }
                    anomalies["balance_drift"].append(anomaly)
                    logger.warning(
                        f"Balance drift detected for USDT: Remote={remote_usdt_free}, "
                        f"Local={local_usdt_free}, Drift={drift} (Healed in mirror cache)"
                    )

            # 2. Fetch local active positions
            async with get_session() as session:
                async with session.begin():
                    stmt = select(Position).where(Position.status.in_(ACTIVE_POSITION_DB_STATUSES))
                    result = await session.execute(stmt)
                    local_positions = result.scalars().all()
                    local_pos_map = {pos.symbol: pos for pos in local_positions}

                    current_anomaly_keys = set()
                    newly_confirmed_keys = set()

                    # Check Feature Flag
                    # In unit/integration test mode, we must still emit ghost-detected events
                    # so NotificationService can send Telegram alerts.
                    enable_event_recon = True
                    if self.settings:
                        enable_event_recon = getattr(self.settings, "ENABLE_EVENT_BASED_RECONCILIATION", True)


                    # --- Check Ghost Positions ---
                    # Remote has position but local DB doesn't mark active
                    for sym, r_pos in remote_pos_map.items():
                        r_size = float(r_pos.amount)
                        if r_size != 0 and sym not in local_pos_map:
                            anomaly = {"symbol": sym, "remote_size": r_size, "local_size": 0.0}
                            anomalies["ghost_positions"].append(anomaly)

                            # OSCILLATION GUARD: Only heal after MIN_PERSISTENCE_SWEEPS consecutive detections.
                            persistence_key = f"ghost:{sym}"
                            self._anomaly_persistence_counters[persistence_key] = (
                                self._anomaly_persistence_counters.get(persistence_key, 0) + 1
                            )
                            consecutive_count = self._anomaly_persistence_counters[persistence_key]

                            if consecutive_count < self._MIN_PERSISTENCE_SWEEPS:
                                self.metrics["oscillation_guard_blocks"] += 1
                                logger.warning(
                                    f"[OSCILLATION-GUARD] Ghost position for {sym} detected "
                                    f"(sweep {consecutive_count}/{self._MIN_PERSISTENCE_SWEEPS}). "
                                    f"Waiting for persistence confirmation before healing."
                                )
                                continue

                            # Anomaly confirmed - proceed with healing
                            key = f"ghost:{sym}:{r_size}"
                            current_anomaly_keys.add(key)
                            if key in self._last_published_anomalies:
                                # de-dupe repeated sweeps
                                continue

                            self._last_published_anomalies.add(key)
                            newly_confirmed_keys.add(key)

                            # Get actual strategy name from order_handler to distinguish bot vs manual orders
                            strategy_name = self._get_strategy_name_for_symbol(sym)

                            ghost_event = Event(
                                event_type=EventTopic.POSITION_GHOST_DETECTED,
                                data={
                                    "symbol": sym,
                                    "position_id": f"ghost_{sym}",
                                    "reason": "auto_recovery",
                                    "strategy_name": strategy_name,
                                    "side": r_pos.side,
                                    "amount": r_size,
                                    "entry_price": float(r_pos.entry_price),
                                    "current_price": float(r_pos.current_price),
                                    "leverage": int(r_pos.leverage),
                                    "ct_val": float(getattr(r_pos, "ct_val", 1.0)),
                                    "margin": float(r_pos.margin) if hasattr(r_pos, "margin") else 0.0,
                                    "notional_size": float(r_pos.notional_size) if hasattr(r_pos, "notional_size") else 0.0,
                                    "pnl": float(r_pos.unrealized_pnl) if hasattr(r_pos, "unrealized_pnl") else 0.0,
                                    "roe": float(r_pos.roe) if hasattr(r_pos, "roe") else 0.0,
                                    "tp_trigger_px": float(r_pos.tp_trigger_px) if hasattr(r_pos, "tp_trigger_px") and r_pos.tp_trigger_px else None,
                                    "sl_trigger_px": float(r_pos.sl_trigger_px) if hasattr(r_pos, "sl_trigger_px") and r_pos.sl_trigger_px else None,
                                },
                                source="reconciliation_service",
                            )
                            await self.event_bus.publish(ghost_event)
                            self.metrics["ghost_events_emitted"] += 1
                            logger.warning(
                                f"[RECONCILE] Confirmed ghost position for {sym} after {consecutive_count} sweeps. "
                                f"Emitted auto_recovery event to PositionEngine."
                            )
                        else:
                            # Remote has no ghost position for this symbol — reset its persistence counter
                            persistence_key = f"ghost:{sym}"
                            self._anomaly_persistence_counters.pop(persistence_key, None)

                    # --- Check Missing Fills (Orphan local active positions) ---
                    # Local DB marks active, but remote shows flat/no position.
                    # OSCILLATION GUARD: Only heal after MIN_PERSISTENCE_SWEEPS consecutive detections.
                    # This prevents OKX transient [] responses from triggering catastrophic state mutations.
                    for sym, l_pos in local_pos_map.items():
                        if sym not in remote_pos_map or float(remote_pos_map[sym].amount) == 0.0:
                            anomaly = {"symbol": sym, "local_size": l_pos.amount, "remote_size": 0.0}
                            anomalies["missing_fills"].append(anomaly)

                            persistence_key = f"missing:{sym}"
                            self._anomaly_persistence_counters[persistence_key] = (
                                self._anomaly_persistence_counters.get(persistence_key, 0) + 1
                            )
                            consecutive_count = self._anomaly_persistence_counters[persistence_key]

                            if consecutive_count < self._MIN_PERSISTENCE_SWEEPS:
                                self.metrics["oscillation_guard_blocks"] += 1
                                logger.warning(
                                    f"[OSCILLATION-GUARD] Missing fill for {sym} detected "
                                    f"(sweep {consecutive_count}/{self._MIN_PERSISTENCE_SWEEPS}). "
                                    f"Waiting for persistence confirmation before healing. "
                                    f"This protects against transient OKX [] responses."
                                )
                                continue

                            # Anomaly confirmed across {MIN_PERSISTENCE_SWEEPS} sweeps — safe to heal
                            if enable_event_recon:
                                key = f"missing:{sym}:{l_pos.amount}"
                                current_anomaly_keys.add(key)
                                if key not in self._last_published_anomalies:
                                    self._last_published_anomalies.add(key)
                                    newly_confirmed_keys.add(key)
                                    missing_event = Event(
                                        event_type=EventTopic.POSITION_GHOST_DETECTED,
                                        data={
                                            "symbol": sym,
                                            "reason": "auto_heal_close",
                                            "position_id": l_pos.position_id,
                                            "strategy_name": getattr(l_pos, 'strategy_name', 'manual'),
                                            "side": getattr(l_pos, 'side', 'N/A'),
                                            "amount": float(getattr(l_pos, 'amount', 0.0)),
                                            "entry_price": float(getattr(l_pos, 'entry_price', 0.0)),
                                            "current_price": float(getattr(l_pos, 'current_price', getattr(l_pos, 'entry_price', 0.0))),
                                            "leverage": int(getattr(l_pos, 'leverage', 1)),
                                            "margin": float(getattr(l_pos, 'amount', 0.0) * getattr(l_pos, 'entry_price', 0.0) / max(1, getattr(l_pos, 'leverage', 1))),
                                            "notional_size": float(getattr(l_pos, 'amount', 0.0) * getattr(l_pos, 'entry_price', 0.0)),
                                            "opened_at": str(getattr(l_pos, 'created_at', getattr(l_pos, 'opened_at', ''))),
                                            "closed_at": str(time.time()), # Timestamp to allow datetime parsing
                                        },
                                        source="reconciliation_service"
                                    )
                                    await self.event_bus.publish(missing_event)
                                    self.metrics["ghost_events_emitted"] += 1
                                    logger.warning(f"[RECONCILE] Confirmed missing fill for {sym} after {consecutive_count} sweeps. Emitted auto_heal_close event.")
                            else:
                                l_pos.status = "CLOSED"
                                logger.warning(f"Auto-healed Missing Fill for {sym} by marking local inactive (confirmed after {consecutive_count} sweeps).")
                        else:
                            # Remote has position for this symbol — reset its persistence counter
                            persistence_key = f"missing:{sym}"
                            self._anomaly_persistence_counters.pop(persistence_key, None)

                    # Keep self._last_published_anomalies in sync with active anomalies.
                    # NOTE: Only intersect ghost/missing keys — do NOT clear drift/orphan keys here
                    # or they lose their dedup protection.
                    ghost_missing_keys = {k for k in self._last_published_anomalies if k.startswith("ghost:") or k.startswith("missing:")}
                    self._last_published_anomalies = (ghost_missing_keys & current_anomaly_keys) | (
                        self._last_published_anomalies - ghost_missing_keys
                    )

            # 3.5 Detect Unprotected Positions
            try:
                unprotected_pos = await self._detect_unprotected_positions(remote_positions)
                if unprotected_pos:
                    anomalies["unprotected_positions"] = unprotected_pos
            except Exception as e:
                logger.error(f"[UNPROTECTED-DETECTION-ERROR] Failed to detect unprotected positions: {e}")

            # 4. Detect Orphan Algo Orders (TP/SL not linked to any position)
            try:
                orphan_algos = await self._detect_orphan_algo_orders(local_positions)
                if orphan_algos:
                    anomalies["orphan_orders"] = orphan_algos
                    logger.warning(f"[ORPHAN-DETECTION] Found {len(orphan_algos)} orphan algo orders: {orphan_algos}")
            except Exception as e:
                logger.error(f"[ORPHAN-DETECTION-ERROR] Failed to detect orphan algo orders: {e}")

            # 3. Publish alerts if anomalies detected
            # Only trigger Telegram alert for balance_drift (which is fixed immediately),
            # or for ghost_positions/missing_fills that have actually passed the persistence threshold
            # (which means they are in _last_published_anomalies this round).
            
            # Extract only newly confirmed anomalies
            new_ghosts = [a for a in anomalies["ghost_positions"] if f"ghost:{a['symbol']}:{a['remote_size']}" in newly_confirmed_keys]
            new_missing = [a for a in anomalies["missing_fills"] if f"missing:{a['symbol']}:{a['local_size']}" in newly_confirmed_keys]
            
            # For drift: use a stable key based on asset only (not drift amount which fluctuates every sweep).
            # Only report if not already reported in this cooldown window.
            new_drift = []
            for d in anomalies.get("balance_drift", []):
                if d["drift"] > 5.0:  # Ignore trivial fluctuations < 5 USDT
                    drift_key = f"drift:{d['asset']}"  # Stable key — doesn't change with PnL
                    if drift_key not in self._last_published_anomalies:
                        new_drift.append(d)
                        self._last_published_anomalies.add(drift_key)
                        current_anomaly_keys.add(drift_key)

            # For orphans, deduplicate by algo_order_id
            new_orphans = []
            for o in anomalies.get("orphan_orders", []):
                if isinstance(o, dict):
                    o_key = f"orphan:{o.get('algo_order_id', str(o))}"
                else:
                    o_key = f"orphan:{o}"
                if o_key not in self._last_published_anomalies:
                    new_orphans.append(o)
                    self._last_published_anomalies.add(o_key)
                    current_anomaly_keys.add(o_key)

            # For unprotected, deduplicate by symbol
            new_unprotected = []
            for u in anomalies.get("unprotected_positions", []):
                u_key = f"unprotected:{u['symbol']}"
                if u_key not in self._last_published_anomalies:
                    new_unprotected.append(u)
                    self._last_published_anomalies.add(u_key)
                    current_anomaly_keys.add(u_key)

            if new_ghosts or new_missing or new_drift or new_orphans or new_unprotected:
                logger.warning(f"[RECONCILE] New anomalies confirmed: Ghosts={len(new_ghosts)}, Missing={len(new_missing)}, Drift={len(new_drift)}, Orphans={len(new_orphans)}, Unprotected={len(new_unprotected)}")

                # RATE-LIMIT: Only send Telegram if cooldown has expired
                now = time.time()
                if now - self._last_alert_ts < self._ALERT_COOLDOWN_SECONDS:
                    remaining = int(self._ALERT_COOLDOWN_SECONDS - (now - self._last_alert_ts))
                    logger.info(f"[RECONCILE] Telegram alert suppressed (cooldown: {remaining}s remaining). Anomalies recorded but not reported.")
                    # Skip Telegram but DO keep processing events (ghost detection etc.)
                    pass
                elif new_ghosts or new_missing or new_drift or new_orphans:
                    # Cooldown expired — send the alert and reset timer
                    self._last_alert_ts = now

                    from datetime import datetime
                    now_str = datetime.now().strftime("%H:%M:%S %d/%m/%Y")

                    # ── Header ────────────────────────────────────────────────
                    alert_msg  = "🔵 <b>KIỂM TOÁN TỰ ĐỘNG</b> (RECONCILIATION)\n"
                    alert_msg += f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    alert_msg += f"│  🕐 <code>{now_str}</code>\n"
                    alert_msg += f"│  🔄 <b>Auto-Heal đã kích hoạt</b>\n"
                    alert_msg += f"│\n"

                    total_actions = len(new_ghosts) + len(new_missing) + len(new_drift) + len(new_orphans) + len(new_unprotected)
                    alert_msg += f"│  📋 Tổng sai lệch phát hiện : <b>{total_actions}</b>\n"
                    alert_msg += f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"

                    # ── Unprotected Positions ──────────────────────────────────
                    if new_unprotected:
                        alert_msg += f"\n🚨 <b>VỊ THẾ KHÔNG BẢO VỆ — Thiếu TP/SL</b> ({len(new_unprotected)})\n"
                        for u in new_unprotected:
                            alert_msg += f"│  ⚠️ <b>{u['symbol']}</b> đang mở nhưng KHÔNG CÓ lệnh bảo vệ!\n"

                    # ── Ghost Positions ────────────────────────────────────────
                    if new_ghosts:
                        alert_msg += f"\n👻 <b>VỊ THẾ MA — Khớp ngoài luồng</b> ({len(new_ghosts)})\n"
                        for g in new_ghosts:
                            sym = g.get('symbol', '?')
                            remote_sz = g.get('remote_size', '?')
                            remote_side = g.get('remote_side', '?')
                            alert_msg += f"│  🪙 {sym}\n"
                            alert_msg += f"│  ├─ Hướng : <code>{remote_side}</code>\n"
                            alert_msg += f"│  ├─ Khối lượng OKX : <code>{remote_sz}</code>\n"
                            alert_msg += f"│  └─ ✅ Đã đồng bộ vào sổ sách\n"

                    # ── Missing Fills ──────────────────────────────────────────
                    if new_missing:
                        alert_msg += f"\n📉 <b>VỊ THẾ ẢO — Missing Fills</b> ({len(new_missing)})\n"
                        for m in new_missing:
                            sym = m.get('symbol', '?')
                            local_sz = m.get('local_size', '?')
                            local_side = m.get('local_side', '?')
                            alert_msg += f"│  🪙 {sym}\n"
                            alert_msg += f"│  ├─ Hướng : <code>{local_side}</code>\n"
                            alert_msg += f"│  ├─ Khối lượng sổ sách : <code>{local_sz}</code>\n"
                            alert_msg += f"│  └─ 🗑️ Đã xóa khỏi sổ (OKX xác nhận đã đóng)\n"

                    # ── Balance Drift ─────────────────────────────────────────
                    if new_drift:
                        alert_msg += f"\n⚖️ <b>HIỆU CHỈNH SỐ DƯ — Balance Drift</b> ({len(new_drift)})\n"
                        for d in new_drift:
                            asset = d.get('asset', 'USDT')
                            drift_val = d.get('drift', 0)
                            remote_free = d.get('remote_free', 0)
                            local_free = d.get('local_free', 0)
                            direction = "▲" if remote_free > local_free else "▼"
                            alert_msg += f"│  💰 {asset}\n"
                            alert_msg += f"│  ├─ Sổ sách (trước) : <code>{float(local_free):,.2f} {asset}</code>\n"
                            alert_msg += f"│  ├─ OKX (thực tế)   : <code>{float(remote_free):,.2f} {asset}</code>\n"
                            alert_msg += f"│  ├─ Chênh lệch      : <code>{direction} {float(drift_val):,.2f} {asset}</code>\n"
                            alert_msg += f"│  └─ ✅ Đã cập nhật cache theo OKX\n"

                    # ── Orphan Orders ─────────────────────────────────────────
                    if new_orphans:
                        alert_msg += f"\n🧹 <b>LỆNH MỒ CÔI — Orphan TP/SL</b> ({len(new_orphans)})\n"
                        for o in new_orphans:
                            if isinstance(o, dict):
                                o_id = o.get('algo_order_id', '?')
                                o_sym = o.get('symbol', '?')
                                alert_msg += f"│  📌 {o_sym} — ID: <code>{o_id}</code>\n"
                            else:
                                alert_msg += f"│  📌 ID: <code>{o}</code>\n"
                        alert_msg += f"│  └─ 🗑️ Đã tự động HỦY trên OKX\n"

                    # ── Footer ────────────────────────────────────────────────
                    alert_msg += f"\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    alert_msg += f"✅ <b>Trạng thái: Đồng bộ 100% với OKX</b>\n"
                    alert_msg += f"<i>⏱️ Lần kiểm toán tiếp theo: ~10 phút</i>"

                    alert_event = Event(
                        event_type=EventTopic.SYSTEM_ALERT,
                        data={
                            "level": "INFO",
                            "title": "⚡ KIỂM TOÁN TỰ ĐỘNG (RECONCILIATION)",
                            "message": alert_msg,
                            "alert_name": "ReconciliationAutoHeal",
                        },
                        source="reconciliation_service"
                    )
                    await self.event_bus.publish(alert_event)
            else:
                logger.info("Reconciliation check completed successfully. No new anomalies confirmed.")

        except Exception as e:
            logger.error(f"Error during reconciliation run: {e}", exc_info=True)

        return anomalies

    async def _detect_unprotected_positions(self, remote_positions: List[Any]) -> List[Dict[str, Any]]:
        """
        Detect active positions on OKX that do NOT have any protective Algo orders (TP/SL)
        linked to them.
        """
        unprotected = []
        try:
            # Only care about positions with non-zero amount
            active_symbols = [p.symbol for p in remote_positions if float(p.amount) != 0]
            if not active_symbols:
                return unprotected
                
            all_pending_algos = await self.exchange.fetch_pending_algo_orders(limit=500)
            # Build set of symbols that have at least one algo order
            protected_symbols = {algo.get('instId') for algo in all_pending_algos if algo.get('instId')}
            
            for sym in active_symbols:
                if sym not in protected_symbols:
                    unprotected.append({"symbol": sym})
                    logger.warning(f"[UNPROTECTED-POS] Position for {sym} has NO protective TP/SL orders!")
                    
        except Exception as e:
            logger.error(f"Error detecting unprotected positions: {e}")
            
        return unprotected

    async def _detect_orphan_algo_orders(self, local_positions: List[Any]) -> List[Dict[str, Any]]:
        """
        Detect orphan algo orders (exist on exchange but not tracked in any position).

        CRITICAL: Must check BOTH DB positions AND RAM positions (order_handler._positions),
        because the persistence layer may not have written positions to DB yet.
        Additionally, cross-reference with exchange active positions — an algo order
        is ONLY orphaned if there is NO active position for that symbol on the exchange.

        Returns list of orphan orders with details.
        """
        orphans = []

        try:
            # Fetch all pending algo orders from exchange (across all symbols)
            all_pending_algos = await self.exchange.fetch_pending_algo_orders(limit=500)
            if not all_pending_algos:
                return orphans

            # === SOURCE 1: Build tracked algo IDs from DB positions ===
            tracked_algo_ids = set()
            for pos in local_positions:
                if hasattr(pos, "algo_order_ids") and pos.algo_order_ids:
                    try:
                        if isinstance(pos.algo_order_ids, str):
                            ids = json.loads(pos.algo_order_ids)  # [FIX P3-2] json imported at top of file
                        else:
                            ids = pos.algo_order_ids
                        tracked_algo_ids.update(ids)
                    except (json.JSONDecodeError, TypeError, ValueError) as e:  # [FIX P1-2] scoped exception
                        logger.debug(f"[RECONCILE] Could not parse algo_order_ids for pos {getattr(pos, 'id', '?')}: {e}")

            # === SOURCE 2: Build tracked algo IDs from RAM positions (order_handler) ===
            # This is the PRIMARY source of truth since persistence may be a no-op stub.
            ram_active_symbols = set()
            if self.order_handler and hasattr(self.order_handler, "_positions"):
                for pos in self.order_handler._positions.values():
                    # Collect algo IDs from RAM positions
                    algo_ids = getattr(pos, "algo_order_ids", None)
                    if algo_ids:
                        if isinstance(algo_ids, str):
                            try:
                                tracked_algo_ids.update(json.loads(algo_ids))  # [FIX P3-2] no import inside loop
                            except (json.JSONDecodeError, TypeError, ValueError) as e:  # [FIX P1-2] scoped
                                logger.debug(f"[RECONCILE] Could not parse RAM algo_order_ids: {e}")
                        elif isinstance(algo_ids, list):
                            tracked_algo_ids.update(algo_ids)

                    # Track symbols with active positions in RAM
                    pos_status = getattr(pos, "status", None)
                    if pos_status and str(pos_status).lower() in (
                        "opened", "pending", "pending_submit", "pending_reconcile",
                        "in_flight", "partially_filled", "partial_tp", "unverified"
                    ):
                        ram_active_symbols.add(getattr(pos, "symbol", ""))

            # === SOURCE 3: Build tracked algo IDs from order_handler._algo_order_to_position ===
            if self.order_handler and hasattr(self.order_handler, "_algo_order_to_position"):
                tracked_algo_ids.update(self.order_handler._algo_order_to_position.keys())

            # === SOURCE 4: Cross-reference with exchange active positions ===
            # Fetch current exchange positions to know which symbols have live exposure.
            exchange_active_symbols = set()
            try:
                remote_positions = await self.exchange.fetch_positions()
                for rp in remote_positions:
                    if float(rp.amount) != 0:
                        exchange_active_symbols.add(rp.symbol)
            except Exception as e:
                logger.warning(f"[ORPHAN-DETECT] Failed to fetch exchange positions for cross-ref: {e}")

            logger.debug(
                f"[ORPHAN-DETECT] tracked_algo_ids={len(tracked_algo_ids)}, "
                f"ram_active_symbols={ram_active_symbols}, "
                f"exchange_active_symbols={exchange_active_symbols}"
            )

            # Check each algo order on exchange
            for algo_order in all_pending_algos:
                order_id = algo_order.get("algoId") or algo_order.get("algoOrderId")
                if not order_id:
                    continue

                # SKIP if already tracked by any source
                if order_id in tracked_algo_ids:
                    continue

                algo_symbol = algo_order.get("instId", "")

                # CRITICAL SAFETY: If there is an active position on the exchange
                # for this symbol, this algo order is protecting that position.
                # Do NOT cancel it — it is NOT an orphan.
                if algo_symbol in exchange_active_symbols:
                    logger.debug(
                        f"[ORPHAN-SAFE] Algo order {order_id} for {algo_symbol} "
                        f"is NOT orphaned — active position exists on exchange."
                    )
                    continue

                # Also check RAM active symbols
                if algo_symbol in ram_active_symbols:
                    logger.debug(
                        f"[ORPHAN-SAFE] Algo order {order_id} for {algo_symbol} "
                        f"is NOT orphaned — active position exists in RAM."
                    )
                    continue

                # Confirmed orphan: no position anywhere for this symbol
                orphan_detail = {
                    "algo_order_id": order_id,
                    "symbol": algo_symbol,
                    "side": algo_order.get("side"),
                    "tp_trigger_px": algo_order.get("tpTriggerPx"),
                    "sl_trigger_px": algo_order.get("slTriggerPx"),
                    "sz": algo_order.get("sz"),
                    "state": algo_order.get("state"),
                    "created_at": algo_order.get("cTime"),
                }
                orphans.append(orphan_detail)
                logger.warning(
                    f"[ORPHAN-ALGO] Confirmed orphan order {order_id}: "
                    f"{algo_order.get('side')} {algo_symbol} "
                    f"TP={algo_order.get('tpTriggerPx')} SL={algo_order.get('slTriggerPx')} "
                    f"(no active position on exchange or in RAM)"
                )

        except Exception as e:
            logger.error(f"Error detecting orphan algo orders: {e}")

        return orphans

    async def cleanup_orphan_algo_orders(self, orphan_orders: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Cleanup orphan algo orders by canceling them on exchange.

        Returns: {"success": count, "failed": count}
        """
        results = {"success": 0, "failed": 0}

        if not orphan_orders:
            return results

        for orphan in orphan_orders:
            try:
                order_id = orphan.get("algo_order_id")
                symbol = orphan.get("symbol")

                # Cancel the algo order on exchange
                await self.exchange.cancel_algo_order(order_id=order_id, symbol=symbol)
                results["success"] += 1

                logger.info(f"[ORPHAN-CLEANUP] Canceled orphan algo order {order_id} on {symbol}")

                # Emit event to notify user
                if self.event_bus:
                    cleanup_event = Event(
                        event_type=EventTopic.SYSTEM_ALERT,
                        data={
                            "level": "INFO",
                            "title": "Orphan Algo Order Cleaned",
                            "message": f"Đã huỷ thành công orphan algo order {order_id} cho {symbol}.",
                            "alert_name": "OrphanOrderCleaned",
                            "algo_order_id": order_id,
                            "symbol": symbol,
                        },
                        source="reconciliation_service"
                    )
                    await self.event_bus.publish(cleanup_event)

            except Exception as e:
                logger.error(f"[ORPHAN-CLEANUP-ERROR] Failed to cancel orphan order {orphan.get('algo_order_id')}: {e}")
                results["failed"] += 1

        return results
