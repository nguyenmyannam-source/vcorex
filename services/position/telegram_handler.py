"""
Handles Telegram requests related to positions and portfolio.
"""

import asyncio
import platform
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import psutil
from loguru import logger
from sqlalchemy import select

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from infrastructure.storage.database import Trade
from infrastructure.storage.repository import UnitOfWork
from services.position.models import PositionStatus
from services.position.tpsl_resolver import build_algo_tpsl_map, merge_tpsl


import logging
from collections import deque
import html


def _compute_performance_metrics(pnls: list[float]) -> Dict[str, float]:
    """Compute max drawdown % and simplified Sharpe ratio from PnL series (oldest first)."""
    if not pnls:
        return {"max_drawdown": 0.0, "sharpe_ratio": 0.0}

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        if peak > 0:
            dd = (peak - cumulative) / peak * 100.0
            max_dd = max(max_dd, dd)

    if len(pnls) < 2:
        sharpe = 0.0
    else:
        mean_ret = sum(pnls) / len(pnls)
        variance = sum((p - mean_ret) ** 2 for p in pnls) / (len(pnls) - 1)
        std_dev = variance ** 0.5
        sharpe = (mean_ret / std_dev) * (len(pnls) ** 0.5) if std_dev > 0 else 0.0

    return {"max_drawdown": max_dd, "sharpe_ratio": sharpe}


def _build_ascii_balance_chart(daily_values: Dict[str, float]) -> str:
    """Build a simple 7-day text sparkline from daily cumulative PnL deltas."""
    if not daily_values:
        return "Không có dữ liệu biểu đồ."

    blocks = "▁▂▃▄▅▆▇█"
    sorted_days = sorted(daily_values.keys())[-7:]
    values = [daily_values[d] for d in sorted_days]
    min_v, max_v = min(values), max(values)
    span = max_v - min_v

    lines = []
    for day, val in zip(sorted_days, values):
        if span <= 0:
            bar = blocks[0]
        else:
            idx = int((val - min_v) / span * (len(blocks) - 1))
            bar = blocks[idx]
        lines.append(f"{day[-5:]} {bar} ${val:,.0f}")

    return "\n".join(lines)


class InMemoryLogHandler(logging.Handler):
    """
    Loguru/Logging tương thích Sink: Bộ đệm RAM xoay vòng bất đồng bộ.
    Tự động gắn Emojis trực quan và chặn Spam.
    """
    def __init__(self):
        super().__init__()
        # Khởi tạo 3 khay RAM riêng biệt với độ dài O(1) chống tràn RAM
        self.container = {
            "ERROR": deque(maxlen=30),
            "WARNING": deque(maxlen=30),
            "INFO": deque(maxlen=40)
        }

    def emit(self, record):
        try:
            msg = self.format(record)

            # [Anti-Spam Guard]: Lọc bỏ các log chứa [QUEUE_METRICS]
            if "[QUEUE_METRICS]" in msg:
                return

            # Gắn emoji theo cấp độ và định dạng thời gian
            level = record.levelname
            timestamp = datetime.now().strftime("%H:%M:%S")

            if level in ["ERROR", "CRITICAL"]:
                formatted_msg = f"🔴 {timestamp} | {msg}"
                self.container["ERROR"].append(formatted_msg)
            elif level == "WARNING":
                formatted_msg = f"⚠️ {timestamp} | {msg}"
                self.container["WARNING"].append(formatted_msg)
            else:
                formatted_msg = f"ℹ️ {timestamp} | {msg}"
                self.container["INFO"].append(formatted_msg)
        except Exception:
            self.handleError(record)

# Global Instance để Telegram Handler truy xuất và Logging.py inject
LOG_CONTAINER = InMemoryLogHandler()

class PositionTelegramHandler:
    _cached_fg_score = None
    _cached_fg_status = None
    _fg_last_fetched = 0.0

    @classmethod
    async def get_fear_and_greed(cls) -> tuple:
        """Fetch the Fear and Greed index from alternative.me with 1 hour caching."""
        import time
        now = time.time()
        # Cache for 1 hour (3600 seconds)
        if cls._cached_fg_score is not None and (now - cls._fg_last_fetched < 3600):
            return cls._cached_fg_score, cls._cached_fg_status

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.alternative.me/fng/", timeout=5) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        fng_data = res.get("data", [])[0]
                        score = int(fng_data.get("value", 50))
                        status_en = fng_data.get("value_classification", "Neutral")

                        # Translate status to Vietnamese for premium UX
                        translations = {
                            "extreme fear": "Tột cùng Sợ hãi 🔴",
                            "fear": "Sợ hãi 🟡",
                            "neutral": "Trung lập ⚪",
                            "greed": "Tham lam 🟢",
                            "extreme greed": "Tột cùng Tham lam 🟢🔥",
                        }
                        status_vi = translations.get(status_en.lower(), status_en)

                        cls._cached_fg_score = score
                        cls._cached_fg_status = status_vi
                        cls._fg_last_fetched = now
                        return score, status_vi
        except Exception as e:
            logger.warning(f"Failed to fetch Fear and Greed index: {e}")

        # Return fallback if call fails
        if cls._cached_fg_score is not None:
            return cls._cached_fg_score, cls._cached_fg_status
        return 50, "Trung lập ⚪"

    def __init__(self, engine, event_bus: EventBus, settings=None):
        self.engine = engine
        self.event_bus = event_bus
        self.settings = settings or getattr(engine, 'settings', None)
        self._subscribe_events()

    def _subscribe_events(self):
        self.event_bus.subscribe(
            self._handle_telegram_health_request,
            [EventTopic.TELEGRAM_REQUEST_HEALTH_DATA],
            handler_id="pe_tele_health",
        )
        self.event_bus.subscribe(
            self._handle_telegram_trading_request,
            [EventTopic.TELEGRAM_REQUEST_TRADING_DATA],
            handler_id="pe_tele_req_trading",
        )
        self.event_bus.subscribe(
            self._handle_analytics_data_request,
            [EventTopic.TELEGRAM_REQUEST_ANALYTICS_DATA],
            handler_id="pe_tele_req_analytics",
        )
        self.event_bus.subscribe(
            self._handle_exchange_status_request,
            [EventTopic.TELEGRAM_REQUEST_EXCHANGE_STATUS],
            handler_id="pe_tele_exchange",
        )
        self.event_bus.subscribe(
            self._handle_history_data_request,
            [EventTopic.TELEGRAM_REQUEST_HISTORY_DATA],
            handler_id="pe_tele_history",
        )
        self.event_bus.subscribe(
            self._handle_system_data_request,
            [EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA],
            handler_id="pe_tele_system",
        )
        self.event_bus.subscribe(
            self._handle_settings_data_request,
            [EventTopic.TELEGRAM_REQUEST_SETTINGS_DATA],
            handler_id="pe_tele_settings",
        )
        self.event_bus.subscribe(
            self._handle_position_close_request,
            [EventTopic.POSITION_CLOSE_REQUEST],
            handler_id="pe_tele_close_request",
        )

    def stop(self) -> None:
        """Unsubscribe all Telegram request handlers."""
        for handler_id in (
            "pe_tele_health",
            "pe_tele_req_trading",
            "pe_tele_req_analytics",
            "pe_tele_exchange",
            "pe_tele_history",
            "pe_tele_system",
            "pe_tele_settings",
            "pe_tele_close_request",
        ):
            self.event_bus.unsubscribe(handler_id=handler_id)

    async def send_manual_close_report(self, pos: 'TrackedPosition', closing_trade: Dict[str, Any]):
        """Formats and sends a detailed report for a manually closed position."""
        try:
            pnl_str = f"🟢 PROFIT: ${pos.pnl:.2f}" if pos.pnl > 0 else f"🔴 LOSS: ${pos.pnl:.2f}"

            # Duration
            duration = pos.closed_at - pos.opened_at if pos.closed_at and pos.opened_at else "N/A"
            if isinstance(duration, timedelta):
                duration_str = str(duration).split('.')[0]
            else:
                duration_str = "N/A"

            message = (
                f"<b>⚠️ LỆNH BỊ ĐÓNG TRÊN SÀN ⚠️</b>\n\n"
                f"Một vị thế đã được đóng trực tiếp trên OKX (có thể do chạm TP/SL hoặc đóng tay).\n\n"
                f"<b>-=-=- CHI TIẾT GIAO DỊCH -=-=-</b>\n"
                f"<b>Symbol:</b> <code>{pos.symbol}</code>\n"
                f"<b>Side:</b> {pos.side.upper()}\n"
                f"<b>Thời gian mở:</b> {pos.opened_at.strftime('%H:%M:%S %d/%m/%Y')}\n"
                f"<b>Thời gian đóng:</b> {pos.closed_at.strftime('%H:%M:%S %d/%m/%Y')}\n"
                f"<b>Thời gian giữ lệnh:</b> {duration_str}\n\n"
                f"<b>-=-=- KẾT QUẢ -=-=-</b>\n"
                f"<b>Giá vào lệnh:</b> ${pos.entry_price:,.4f}\n"
                f"<b>Giá đóng cửa:</b> ${pos.close_price:,.4f}\n"
                f"<b>Phí giao dịch:</b> ${closing_trade.get('fee', {}).get('cost', 0):.4f}\n"
                f"<b>Kết quả (P&L): {pnl_str}</b> ({pos.pnl_percentage:.2f}%)\n\n"
                f"<i>Ghi chú: VcoreX đã phát hiện và ghi nhận giao dịch này vào database.</i>"
            )

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_SEND_MESSAGE,
                    data={"message": message},
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error(f"Failed to send manual close report for {pos.id}: {e}", exc_info=True)

    async def send_manual_close_fallback_report(self, pos: 'TrackedPosition'):
        """Sends a fallback report when closing trade details are unavailable."""
        try:
            message = (
                f"<b>⚠️ LỆNH BỊ ĐÓNG TRÊN SÀN (FALLBACK) ⚠️</b>\n\n"
                f"Vị thế <b>{pos.symbol}</b> (ID: <code>{pos.id}</code>) đã bị đóng trên sàn (có thể do chạm TP/SL hoặc đóng tay).\n\n"
                f"Hệ thống không thể truy xuất được chi tiết giao dịch đóng (giá, phí). "
                f"Vị thế đã được đánh dấu là <b>CLOSED</b> trong database để đảm bảo tính nhất quán.\n\n"
                f"<i>Vui lòng kiểm tra lịch sử giao dịch trên OKX để xem chi tiết.</i>"
            )
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.SYSTEM_ALERT,
                    data={"level": "WARNING", "message": message},
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error(f"Failed to send manual close fallback report for {pos.id}: {e}", exc_info=True)


    # 1. Health Status
    async def _handle_telegram_health_request(self, event: Event) -> None:
        try:
            active_count = len(self.engine._positions)
            unrealized_pnl = sum(p.pnl for p in self.engine._positions.values())

            # Use cached metrics from exchange connection status
            exchange_ok = getattr(self.engine.exchange, "_ws_connected", False)

            # Get actual system stats
            cpu_usage = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()

            # Disk usage
            try:
                disk = psutil.disk_usage("C:" if platform.system() == "Windows" else "/")
                disk_pct = disk.percent
            except Exception:
                disk_pct = 0.0

            # Uptime
            uptime_seconds = int(time.time() - getattr(self.engine, "_start_time", time.time()))

            data = {
                "status": "healthy" if exchange_ok else "degraded",
                "uptime_seconds": uptime_seconds,
                "cpu_usage": float(cpu_usage),
                "ram_usage": float(memory.percent),
                "ram_total_gb": round(memory.total / (1024**3), 1),
                "ram_used_gb": round(memory.used / (1024**3), 1),
                "disk_usage": float(disk_pct),
                "active_positions": active_count,
                "unrealized_pnl": unrealized_pnl,
                "memory_usage": "Optimal",
                "exchange_connected": exchange_ok,
                "components": {
                    "exchange": "OK" if getattr(self.engine.exchange, "_connected", False) else "ERROR",
                    "websocket": "OK" if exchange_ok else "ERROR",
                    "database": "OK",
                    "event_bus": "OK",
                    "telegram": "OK",
                },
            }

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_HEALTH_DATA,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": event.data.get("action"),
                        "message_id": event.data.get("message_id"),
                        **data,
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling health request: {}", e, exc_info=True)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_HEALTH_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 2. Trading Data (Dashboard)
    async def _handle_telegram_trading_request(self, event: Event) -> None:
        try:
            action = event.data.get("action")

            if action == "active_signals":
                # Active signals are not yet sourced from a live backend feed.
                # Publish an empty payload so renderer shows "No signals" and UI is released.
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_TRADING_DATA,
                        data={
                            "chat_id": event.data.get("chat_id"),
                            "action": action,
                            "message_id": event.data.get("message_id"),
                            "signals": [],
                        },
                        source="position_telegram_handler",
                    )
                )
                return

            if action == "pending_orders":
                orders_data = []
                try:
                    open_orders = await self.engine.exchange.fetch_open_orders()
                    for order in open_orders:
                        orders_data.append(
                            {
                                "symbol": order.symbol,
                                "side": order.side,
                                "amount": order.amount,
                                "price": order.price,
                                "type": order.type,
                                "status": order.status,
                            }
                        )
                except Exception as e:
                    logger.error("Failed to fetch pending orders: {}", e)

                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_TRADING_DATA,
                        data={
                            "action": action,
                            "message_id": event.data.get("message_id"),
                            "orders": orders_data,
                            "positions": [],
                        },
                        source="position_telegram_handler",
                    )
                )
                return

            positions_data = []

            # --- USE EXCHANGE MIRROR AS SINGLE SOURCE OF TRUTH ---
            use_mirror = hasattr(self.engine, "exchange_mirror") and self.engine.exchange_mirror is not None
            
            # Cross-validation: Check for data desync with exchange state
            if use_mirror and action == "open_positions":
                try:
                    mirror_positions_count = len((await self.engine.exchange_mirror.get_all_positions()).values())
                    local_positions_count = len([p for p in self.engine.order_handler._positions.values() 
                                               if p.status in [PositionStatus.OPENED, PositionStatus.PARTIAL_TP, 
                                                              PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE]])
                    
                    # If counts differ significantly, trigger sync
                    if abs(mirror_positions_count - local_positions_count) > 0:
                        logger.warning(
                            f"[DATA_DESYNC] Position count mismatch: Mirror={mirror_positions_count}, Local={local_positions_count}. "
                            f"Triggering sync before UI render."
                        )
                        # Trigger reconciliation sync
                        await self.engine.exchange_mirror.sync_positions()
                except Exception as sync_err:
                    logger.warning(f"[DATA_VALIDATION] Failed cross-validation sync: {sync_err}")

            algo_tpsl_map: Dict[str, Any] = {}
            if use_mirror:
                try:
                    algo_tpsl_map = await build_algo_tpsl_map(self.engine.exchange)
                except Exception as e:
                    logger.warning("Failed to fetch algo TP/SL for open positions UI: {}", e)

            if use_mirror:
                mirror_positions = (await self.engine.exchange_mirror.get_all_positions()).values()
                # Snapshot order_handler positions once for O(n) lookup
                _local_positions = self.engine.order_handler._positions
                for m_pos in mirror_positions:
                    local_pos = None
                    _linkable_statuses = (
                        PositionStatus.OPENED,
                        PositionStatus.PARTIAL_TP,
                        PositionStatus.PENDING_RECONCILE,
                        PositionStatus.UNVERIFIED,
                        PositionStatus.PENDING_SUBMIT,
                    )
                    for lp in _local_positions.values():
                        if lp.symbol == m_pos.instId and lp.status in _linkable_statuses:
                            local_pos = lp
                            break
                    if local_pos is None:
                        for lp in _local_positions.values():
                            if lp.symbol == m_pos.instId:
                                local_pos = lp
                                break

                    has_sl, has_tp, sl_price, tp_prices = merge_tpsl(
                        local_pos,
                        m_pos,
                        algo_tpsl_map.get(m_pos.instId),
                    )
                    strategy_name = local_pos.strategy_name if local_pos else "Manual/Exchange"

                    duration_str = "N/A"
                    if local_pos:
                        pos_opened = local_pos.opened_at.replace(tzinfo=timezone.utc) if local_pos.opened_at.tzinfo is None else local_pos.opened_at
                        duration_str = str(datetime.now(timezone.utc) - pos_opened).split(".")[0]
                    else:
                        # Fallback to OKX cTime
                        pos_opened = datetime.fromtimestamp(m_pos.cTime / 1000, timezone.utc)
                        duration_str = str(datetime.now(timezone.utc) - pos_opened).split(".")[0]

                    positions_data.append(
                        {
                            "position_id": local_pos.id if local_pos else None,
                            "symbol": m_pos.instId,
                            "mirror_only": local_pos is None,
                            "side": "LONG" if m_pos.pos > 0 else "SHORT",
                            "amount": abs(m_pos.pos),
                            "entry_price": m_pos.avgPx,
                            "current_price": m_pos.markPx,
                            "pnl": m_pos.upl,
                            "pnl_pct": m_pos.uplRatio * 100, # OKX returns decimal, *100 for %
                            "margin": m_pos.margin,
                            "notional_size": abs(m_pos.pos) * m_pos.avgPx,
                            "leverage": local_pos.leverage if local_pos else getattr(self.settings, "default_leverage", 10),
                            "duration": duration_str,
                            "has_sl": has_sl,
                            "has_tp": has_tp,
                            "sl_price": sl_price,
                            "tp_prices": tp_prices,
                            "strategy_name": strategy_name,
                        }
                    )
            else:
                _local_positions = self.engine.order_handler._positions
                for pos in _local_positions.values():
                    # Only show active positions
                    if pos.status not in [PositionStatus.OPENED, PositionStatus.PARTIAL_TP, PositionStatus.PENDING_SUBMIT, PositionStatus.PENDING_RECONCILE]:
                        continue

                    # Check for TP/SL status
                    has_sl = pos.stop_loss is not None and pos.stop_loss > 0
                    has_tp = len(pos.take_profit_levels) > 0

                    # Safe timezone aware duration computation
                    pos_opened = pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at
                    duration_str = str(datetime.now(timezone.utc) - pos_opened).split(".")[0]

                    margin = pos.get_margin()
                    roe = (pos.pnl / margin * 100) if margin > 0 else 0.0
                    positions_data.append(
                        {
                            "position_id": pos.id,
                            "symbol": pos.symbol,
                            "side": pos.side.upper(),
                            "amount": pos.amount_remaining,
                            "entry_price": pos.entry_price,
                            "current_price": pos.current_price,
                            "pnl": pos.pnl,
                            "pnl_pct": roe,
                            "margin": margin,
                            "notional_size": pos.get_notional_size(),
                            "leverage": pos.leverage,
                            "duration": duration_str,
                            "has_sl": has_sl,
                            "has_tp": has_tp,
                            "sl_price": pos.stop_loss,
                            "tp_prices": [tp.price for tp in pos.take_profit_levels],
                            "strategy_name": pos.strategy_name,
                        }
                    )

            # Sắp xếp theo PNL giảm dần
            positions_data.sort(key=lambda x: x["pnl"], reverse=True)

            # Fetch real balance for the Balance Detail screen
            total_balance = 0.0
            free_margin = 0.0
            used_margin = 0.0

            if use_mirror:
                m_acc = await self.engine.exchange_mirror.get_account()
                if m_acc:
                    total_balance = m_acc.totalEq
                    free_margin = m_acc.availEq
                    used_margin = total_balance - free_margin
            else:
                try:
                    balances = await self.engine.exchange.fetch_balance()
                    bal = balances.get("USDT")
                    if not bal and balances:
                        bal = next(iter(balances.values()))
                    if bal:
                        total_balance = float(bal.total)
                        free_margin = float(bal.free)
                        used_margin = float(bal.used)
                except Exception as e:
                    logger.error("Failed to fetch balance for trading request: {}", e)

            metrics = {
                "total_positions": len(positions_data),
                "total_unrealized_pnl": sum(p["pnl"] for p in positions_data),
                "win_count": len([p for p in positions_data if p["pnl"] > 0]),
                "loss_count": len([p for p in positions_data if p["pnl"] <= 0]),
                "total_balance": total_balance,
                "free_margin": free_margin,
                "used_margin": used_margin,
                "risk_level": "🟢 AN TOÀN" if len(positions_data) < 3 else "🟡 TRUNG BÌNH" if len(positions_data) < 8 else "🔴 RỦI RO CAO",
            }

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_TRADING_DATA,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": event.data.get("action"),
                        "message_id": event.data.get("message_id"),
                        "positions": positions_data,
                        "metrics": metrics,
                        # Flatten for templates that expect top-level keys
                        "total_balance": total_balance,
                        "free_margin": free_margin,
                        "used_margin": used_margin,
                        "risk_level": metrics["risk_level"],
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling trading data request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_TRADING_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 3. Analytics (PNL, ROI)
    async def _handle_analytics_data_request(self, event: Event) -> None:
        try:
            from infrastructure.storage.database import Position

            action = event.data.get("action")

            async with UnitOfWork(self.engine.session_factory) as uow:
                if uow.session is None:
                    raise RuntimeError("Database session is not initialized")
                result = await uow.session.execute(
                    select(Position).where(
                        Position.status.in_(["CLOSED", "closed", "LIQUIDATED", "liquidated"])
                    ).order_by(Position.closed_at.desc()).limit(500)
                )
                closed_positions = list(result.scalars().all())

            pnls = [
                getattr(p, "realized_pnl", 0.0) or 0.0
                for p in reversed(closed_positions)
                if getattr(p, "closed_at", None) is not None
            ]
            total_trades = len(closed_positions)
            winning_trades = [p for p in closed_positions if (getattr(p, "realized_pnl", 0.0) or 0.0) > 0]
            losing_trades = [p for p in closed_positions if (getattr(p, "realized_pnl", 0.0) or 0.0) < 0]
            win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0

            gross_profit = sum(getattr(p, "realized_pnl", 0.0) or 0.0 for p in winning_trades)
            gross_loss = abs(sum(getattr(p, "realized_pnl", 0.0) or 0.0 for p in losing_trades))
            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            elif gross_profit > 0:
                profit_factor = float("inf")
            else:
                profit_factor = 0.0

            total_pnl = sum(getattr(p, "realized_pnl", 0.0) or 0.0 for p in closed_positions)
            today = datetime.now(timezone.utc).date()
            today_positions = [
                p for p in closed_positions
                if p.closed_at and p.closed_at.date() == today
            ]
            daily_pnl = sum(getattr(p, "realized_pnl", 0.0) or 0.0 for p in today_positions)

            perf = _compute_performance_metrics(pnls)

            daily_pnl_by_day: Dict[str, float] = {}
            running = 0.0
            for p in reversed(closed_positions):
                if not p.closed_at:
                    continue
                day_key = p.closed_at.date().isoformat()
                running += getattr(p, "realized_pnl", 0.0) or 0.0
                daily_pnl_by_day[day_key] = running

            data: Dict[str, Any] = {
                "daily_pnl": daily_pnl,
                "total_pnl": total_pnl,
                "weekly_pnl": daily_pnl,
                "monthly_pnl": total_pnl,
                "win_rate": win_rate,
                "total_trades": total_trades,
                "profit_factor": profit_factor,
                "max_drawdown": perf["max_drawdown"],
                "sharpe_ratio": perf["sharpe_ratio"],
                "best_trade": max(
                    (getattr(p, "realized_pnl", 0.0) or 0.0 for p in closed_positions), default=0
                ),
                "worst_trade": min(
                    (getattr(p, "realized_pnl", 0.0) or 0.0 for p in closed_positions), default=0
                ),
            }

            if action == "balance_history":
                data["ascii_chart"] = _build_ascii_balance_chart(daily_pnl_by_day)

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_ANALYTICS_DATA,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": action,
                        "message_id": event.data.get("message_id"),
                        **data,
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling analytics request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_ANALYTICS_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 4. History (Recent closed trades)
    async def _handle_history_data_request(self, event: Event) -> None:
        try:
            action = event.data.get("action")
            history_data = []

            if action == "orders_history":
                try:
                    # Fetch live executions/fills from OKX
                    fills = await self.engine.exchange.fetch_trade_history(limit=10)
                    for f in fills:
                        fill_time_raw = f.get("fillTime")
                        if fill_time_raw:
                            try:
                                dt = datetime.fromtimestamp(int(fill_time_raw) / 1000, timezone.utc)
                                ict_dt = dt.astimezone(timezone(timedelta(hours=7)))
                                time_str = ict_dt.strftime("%H:%M:%S %d/%m")
                            except Exception:
                                time_str = "N/A"
                        else:
                            time_str = "N/A"

                        history_data.append(
                            {
                                "symbol": f.get("instId", "N/A"),
                                "side": f.get("side", "N/A").upper(),
                                "pos_side": f.get("posSide", "").upper(),
                                "price": float(f.get("fillPx", 0)),
                                "size": float(f.get("fillSz", 0)),
                                "fee": float(f.get("fee", 0)),
                                "exec_type": f.get("execType", "T"),
                                "time": time_str,
                            }
                        )
                except Exception as ex:
                    logger.error("Failed to fetch live OKX order history: {}", ex)
            elif action == "positions_history":
                try:
                    # Fetch live closed positions history from OKX
                    pos_hist = await self.engine.exchange.fetch_positions_history(limit=10)
                    for p in pos_hist:
                        u_time = p.get("uTime")
                        if u_time:
                            try:
                                dt = datetime.fromtimestamp(int(u_time) / 1000, timezone.utc)
                                ict_dt = dt.astimezone(timezone(timedelta(hours=7)))
                                time_str = ict_dt.strftime("%H:%M:%S %d/%m")
                            except Exception:
                                time_str = "N/A"
                        else:
                            time_str = "N/A"

                        history_data.append(
                            {
                                "symbol": p.get("instId", "N/A"),
                                "side": p.get("posSide", "net").upper(),
                                "open_price": float(p.get("openAvgPx") or 0.0),
                                "close_price": float(p.get("closeAvgPx") or 0.0),
                                "pnl": float(p.get("pnl") or 0.0),
                                "pnl_ratio": float(p.get("pnlRatio") or 0.0) * 100,
                                "leverage": p.get("lever", "10"),
                                "margin_mode": "Cô lập" if p.get("mgnMode") == "isolated" else "Chéo",
                                "time": time_str,
                                "close_type": p.get("type", "2"),
                            }
                        )
                except Exception as ex:
                    logger.error("Failed to fetch live OKX positions history: {}", ex)
            elif action in ["missed_signals", "clear_missed_signals"]:
                import os
                import json
                state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "strategies", "signal_state.json")
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r", encoding="utf-8") as f:
                            data = json.load(f)

                        if action == "clear_missed_signals":
                            data["missed_signals"] = []
                            with open(state_file, "w", encoding="utf-8") as f:
                                json.dump(data, f, indent=2, ensure_ascii=False)
                            # Update action to missed_signals so the UI renders the dashboard again
                            action = "missed_signals"

                        raw_missed_signals = data.get("missed_signals", [])

                        # Aggregation logic
                        chot_chan_1_reasons = ["tín hiệu trễ", "cooldown", "trùng lặp"]
                        chot_chan_2_reasons = ["quá tải vị thế", "hết margin", "rủi ro", "tập trung rủi ro", "exposure", "cháy", "mù dữ liệu", "insufficient", "max open positions"]
                        chot_chan_3_reasons = ["chưa đạt", "entry price"]

                        count_c1 = 0
                        count_c2 = 0
                        count_c3 = 0

                        for s in raw_missed_signals:
                            reason = str(s.get("reason", "")).lower()

                            is_c3 = any(r in reason for r in chot_chan_3_reasons)
                            is_c2 = any(r in reason for r in chot_chan_2_reasons)

                            if is_c3:
                                count_c3 += 1
                            elif is_c2:
                                count_c2 += 1
                            else:
                                # Default to C1 for timing/logic issues
                                count_c1 += 1

                        last_scan_dict = data.get("last_signal_time", {})
                        # Get the most recent scan time across all symbols
                        last_scan_time = None
                        if last_scan_dict:
                            latest = None
                            for ts in last_scan_dict.values():
                                try:
                                    dt = datetime.fromisoformat(ts)
                                    if latest is None or dt > latest:
                                        latest = dt
                                except Exception:
                                    pass
                            if latest:
                                ict_dt = latest.astimezone(timezone(timedelta(hours=7)))
                                last_scan_time = ict_dt.strftime("%H:%M:%S %d/%m")

                        # FIX 1: If in-memory scan time is N/A (just restarted),
                        # fall back to the most recent signal in the missed_signals list
                        if not last_scan_time and raw_missed_signals:
                            try:
                                # Pick the most recent timestamp from any recorded signal
                                latest_signal_ts = max(
                                    raw_missed_signals,
                                    key=lambda s: s.get("time", "")
                                ).get("time", "")
                                if latest_signal_ts:
                                    dt = datetime.fromisoformat(latest_signal_ts)
                                    ict_dt = dt.astimezone(timezone(timedelta(hours=7)))
                                    last_scan_time = ict_dt.strftime("%H:%M:%S %d/%m") + " (lần cuối)"
                            except Exception:
                                pass

                        if not last_scan_time:
                            last_scan_time = "Chưa có dữ liệu"

                        total_rejected = count_c1 + count_c2 + count_c3

                        # FIX 2: Only flag as "unstable connection" if there are MANY C1/C2 rejections
                        # (>= 5 total, and C1+C2 dominates). A single startup stale signal is NOT a connection issue.
                        dominant = None
                        if total_rejected >= 5 and (count_c1 + count_c2) > count_c3 * 2 and (count_c1 > 0 or count_c2 > 0):
                            dominant = "Mù dữ liệu/Tín hiệu trễ" if count_c1 > count_c2 else "Quá tải rủi ro"

                        history_data = {
                            "last_scan": last_scan_time,
                            "c1_count": count_c1,
                            "c2_count": count_c2,
                            "c3_count": count_c3,
                            "total_rejected": total_rejected,
                            "recent_signals": raw_missed_signals[:3],
                            "dominant_issue": dominant,
                            "is_empty": len(raw_missed_signals) == 0
                        }
                    except Exception as ex:
                        logger.error("Failed to load missed signals: {}", ex)
                        history_data = {"is_empty": True}
                else:
                    history_data = {"is_empty": True}
            elif action == "daily_reports":
                from infrastructure.storage.database import Position

                today = datetime.now(timezone.utc).date()
                async with UnitOfWork(self.engine.session_factory) as uow:
                    if uow.session is None:
                        raise RuntimeError("Database session is not initialized")
                    result = await uow.session.execute(
                        select(Position).where(
                            Position.status.in_(["CLOSED", "closed", "LIQUIDATED", "liquidated"])
                        ).order_by(Position.closed_at.desc()).limit(500)
                    )
                    closed_positions = list(result.scalars().all())

                today_positions = [
                    p for p in closed_positions
                    if p.closed_at and p.closed_at.date() == today
                ]
                wins = [p for p in today_positions if (getattr(p, "realized_pnl", 0.0) or 0.0) > 0]
                history_data = {
                    "trades_today": len(today_positions),
                    "pnl_today": sum(getattr(p, "realized_pnl", 0.0) or 0.0 for p in today_positions),
                    "win_rate_today": (len(wins) / len(today_positions) * 100) if today_positions else 0.0,
                    "report_date": today.isoformat(),
                }
            else:
                from infrastructure.storage.database import Position
                async with UnitOfWork(self.engine.session_factory) as uow:
                    if uow.session is None:
                        raise RuntimeError("Database session is not initialized")
                    status_list = ["CLOSED", "closed"] if action == "closed_trades" else ["LIQUIDATED", "liquidated"]
                    result = await uow.session.execute(
                        select(Position).where(Position.status.in_(status_list)).order_by(Position.closed_at.desc()).limit(10)
                    )
                    recent_positions = list(result.scalars().all())

                for p in recent_positions:
                    duration_str = "N/A"
                    if p.closed_at and p.created_at:
                        duration_str = str(p.closed_at - p.created_at).split(".")[0]
                    history_data.append(
                        {
                            "symbol": p.symbol,
                            "side": p.side,
                            "pnl": getattr(p, "realized_pnl", 0.0) or 0.0,
                            "duration": duration_str,
                            "close_time": str(p.closed_at).split(".")[0] if p.closed_at else str(p.updated_at).split(".")[0],
                            "reason": p.strategy_name or "Manual/TP/SL",
                        }
                    )

            # Ensure we publish the response for all history actions
            response_data: Dict[str, Any] = {
                "chat_id": event.data.get("chat_id"),
                "action": action,
                "message_id": event.data.get("message_id"),
            }
            if action == "daily_reports":
                response_data.update(history_data)
            else:
                response_data["history"] = history_data

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_HISTORY_DATA,
                    data=response_data,
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling history request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_HISTORY_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 5. Exchange Status (API limits, latency)
    async def _handle_exchange_status_request(self, event: Event) -> None:
        try:
            # OKX specific metrics
            metrics = {}
            if hasattr(self.engine.exchange, "get_api_metrics"):
                metrics = self.engine.exchange.get_api_metrics()

            # Measure actual network latency (ping) using a lightweight public endpoint
            latency_ms = 0
            if hasattr(self.engine.exchange, "_request"):
                try:
                    start_time = time.perf_counter()
                    await asyncio.wait_for(
                        self.engine.exchange._request("GET", "/api/v5/public/time", auth_required=False),
                        timeout=3.0
                    )
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                except asyncio.TimeoutError:
                    logger.warning("Ping request timed out after 3.0s")
                    latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))
                except Exception as e:
                    logger.warning("Failed to measure actual ping: {}", e)
                    latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))
            else:
                latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))

            data = {
                "exchange_name": "OKX V5 (SWAP)",
                "is_connected": getattr(self.engine.exchange, "_ws_connected", False),
                "latency_ms": latency_ms,
                "rate_limit": "Good",
                "api_requests": metrics.get("api_request_count", 0),
                "api_errors": metrics.get("api_error_count", 0),
                "ws_messages": metrics.get("ws_message_count", 0),
            }

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_EXCHANGE_STATUS,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": event.data.get("action"),
                        "message_id": event.data.get("message_id"),
                        **data,
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling exchange status request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_EXCHANGE_STATUS,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 6. System Status
    async def _handle_system_data_request(self, event: Event) -> None:
        try:
            # Get actual system stats
            cpu_usage = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()

            # Disk usage
            try:
                disk = psutil.disk_usage("C:" if platform.system() == "Windows" else "/")
                disk_pct = disk.percent
            except Exception:
                disk_pct = 0.0

            # Uptime
            uptime_seconds = int(time.time() - getattr(self.engine, "_start_time", time.time()))

            # Component health check
            exchange_ok = getattr(self.engine.exchange, "_connected", False)
            ws_ok = getattr(self.engine.exchange, "_ws_connected", False)

            data: Dict[str, Any] = {
                "cpu_usage": float(cpu_usage),
                "ram_usage": float(memory.percent),
                "ram_total_gb": round(memory.total / (1024**3), 1),
                "ram_used_gb": round(memory.used / (1024**3), 1),
                "disk_usage": float(disk_pct),
                "os": platform.system(),
                "bot_version": "v1.2.0-PRO",
                "uptime_seconds": uptime_seconds,
                "components": {
                    "exchange": "OK" if exchange_ok else "ERROR",
                    "websocket": "OK" if ws_ok else "ERROR",
                    "database": "OK",
                    "event_bus": "OK",
                    "telegram": "OK",
                },
            }

            # Retrieve realized PnL and free margin
            realized_pnl = 0.0
            free_margin = 0.0
            use_mirror = hasattr(self.engine, "exchange_mirror") and self.engine.exchange_mirror is not None

            if use_mirror:
                m_acc = await self.engine.exchange_mirror.get_account()
                if m_acc:
                    free_margin = m_acc.availEq

            try:
                if use_mirror:
                    realized_pnl = self.engine.exchange_mirror.get_realized_pnl()
                else:
                    # Fallback to local DB query if mirror is not initialized
                    async with UnitOfWork(self.engine.session_factory) as uow:
                        from sqlalchemy import func, select
                        from infrastructure.storage.database import Position
                        if uow.session is not None:
                            result = await uow.session.execute(
                                select(func.sum(Position.realized_pnl)).where(
                                    Position.status.in_(["CLOSED", "LIQUIDATED"])
                                )
                            )
                            realized_pnl = float(result.scalar() or 0.0)
            except Exception as e:
                logger.error("Error retrieving financial metrics for dashboard: {}", e)

            # Position metrics
            unrealized_pnl = 0.0
            active_positions = 0
            long_count = 0
            short_count = 0

            if use_mirror:
                mirror_positions = list((await self.engine.exchange_mirror.get_all_positions()).values())
                active_positions = len(mirror_positions)
                unrealized_pnl = sum(p.upl for p in mirror_positions)
                long_count = len([p for p in mirror_positions if p.pos > 0])
                short_count = len([p for p in mirror_positions if p.pos < 0])

                # Resolve positions to check TP/SL
                mirror_symbols = {p.instId for p in mirror_positions}
                positions = [
                    lp for lp in self.engine._positions.values()
                    if lp.symbol in mirror_symbols
                ]
            else:
                positions = list(self.engine._positions.values())
                unrealized_pnl = sum(p.pnl for p in positions)
                active_positions = len(positions)
                long_count = len([p for p in positions if p.side == "long"])
                short_count = len([p for p in positions if p.side == "short"])


            # Measure actual network latency (ping)
            latency_ms = 0
            if hasattr(self.engine.exchange, "_request"):
                try:
                    start_time = time.perf_counter()
                    await asyncio.wait_for(
                        self.engine.exchange._request("GET", "/api/v5/public/time", auth_required=False),
                        timeout=3.0
                    )
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                except asyncio.TimeoutError:
                    logger.warning("Ping request timed out after 3.0s")
                    latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))
                except Exception as e:
                    logger.warning("Failed to measure actual ping for dashboard: {}", e)
                    latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))
            else:
                latency_ms = abs(int(getattr(self.engine.exchange, "_server_time_offset", 0)))

            # Count TP/SL orders (local + mirror + pending algos on exchange)
            tpsl_count = 0
            try:
                dashboard_algo_map = await build_algo_tpsl_map(self.engine.exchange)
                if use_mirror:
                    local_by_symbol = {lp.symbol: lp for lp in positions}
                    for m_pos in mirror_positions:
                        lp = local_by_symbol.get(m_pos.instId)
                        has_sl, has_tp, _, _ = merge_tpsl(
                            lp,
                            m_pos,
                            dashboard_algo_map.get(m_pos.instId),
                        )
                        if has_sl or has_tp:
                            tpsl_count += 1
                else:
                    for p in positions:
                        has_sl, has_tp, _, _ = merge_tpsl(
                            p,
                            None,
                            dashboard_algo_map.get(p.symbol),
                        )
                        if has_sl or has_tp:
                            tpsl_count += 1
            except Exception:
                pass

            # Fetch Fear & Greed Index
            fg_score, fg_status = await self.get_fear_and_greed()

            # Real Watchlist Count (phản ánh đúng Radar Limit đang active)
            watchlist_count = 20
            if getattr(self, "settings", None) and getattr(self.settings, "watchlist", None):
                # Ưu tiên radar_limit (đã được bấm live trên Telegram)
                radar_limit = getattr(self.settings, "radar_limit", len(self.settings.watchlist))
                watchlist_count = min(radar_limit, len(self.settings.watchlist))
            elif hasattr(self.engine, "_markets"):
                watchlist_count = len(self.engine._markets)

            # Error Count
            error_count = 0
            if hasattr(self.engine.exchange, "get_api_metrics"):
                error_count = self.engine.exchange.get_api_metrics().get("api_error_count", 0)

            # Account Mode
            demo_mode = getattr(self.engine.exchange, "demo_mode", True)
            account_mode = "OKX DEMO TRADING" if demo_mode else "OKX LIVE TRADING"

            data.update(
                {
                    "api_latency": latency_ms,
                    "exchange_connected": exchange_ok,
                    "fg_score": fg_score,
                    "fg_status": fg_status,
                    "watchlist_count": watchlist_count,
                    "account_mode": account_mode,
                    "free_margin": free_margin,
                    "realized_pnl": realized_pnl,
                    "unrealized_pnl": unrealized_pnl,
                    "active_positions": active_positions,
                    "long_count": long_count,
                    "short_count": short_count,
                    "tpsl_count": tpsl_count,
                    "risk_level": (
                        "🟢 AN TOÀN"
                        if active_positions < 3
                        else "🟡 TRUNG BÌNH" if active_positions < 8 else "🔴 RỦI RO CAO"
                    ),
                    "process_id": (int(time.time()) % 10) + 1,
                    "error_count": error_count,
                }
            )

            action = event.data.get("action")

            # --- TÁI CẤU TRÚC: TELEMETRY LOGS IN-MEMORY ---
            if action == "logs":
                # Lấy dữ liệu trực tiếp từ LOG_CONTAINER trên RAM
                errors = list(LOG_CONTAINER.container["ERROR"])
                warnings = list(LOG_CONTAINER.container["WARNING"])
                infos = list(LOG_CONTAINER.container["INFO"])

                # Hàm helper để escape HTML an toàn cho Telegram
                def format_logs(log_list, title):
                    if not log_list:
                        return f"<b>{title}</b>\n<i>Không có bản ghi nào.</i>\n"
                    escaped_logs = "\n".join([f"<code>{html.escape(line)}</code>" for line in log_list[-5:]])
                    return f"<b>{title}</b>\n{escaped_logs}\n"

                # Phân khu HTML sắc nét
                uptime_str = str(timedelta(seconds=uptime_seconds))
                html_text = f"🤖 <b>BOT UPTIME:</b> <code>{uptime_str}</code>\n\n"
                html_text += format_logs(errors, "🔴 LỖI HỆ THỐNG GẦN NHẤT")
                html_text += format_logs(warnings, "⚠️ CẢNH BÁO VẬN HÀNH")
                html_text += format_logs(infos, "ℹ️ NHẬT KÝ HOẠT ĐỘNG REAL-TIME")

                # Cấu trúc bàn phím Inline: 2 nút bấm ngang
                custom_keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🗑️ Xóa Sạch Cache", "callback_data": "system:clear_logs"},
                            {"text": "🔄 Làm Mới Logs", "callback_data": "system:logs"}
                        ],
                        [{"text": "◀️ Về trang chính", "callback_data": "menu:main"}]
                    ]
                }

                data["custom_formatted_text"] = html_text
                data["custom_keyboard"] = custom_keyboard

            elif action == "clear_logs":
                # Dọn dẹp khay RAM
                LOG_CONTAINER.container["ERROR"].clear()
                LOG_CONTAINER.container["WARNING"].clear()
                LOG_CONTAINER.container["INFO"].clear()

                html_text = "✅ <b>Đã giải phóng bộ đệm nhật ký trên RAM thành công!</b>\n<i>Tất cả các bản ghi lỗi và cảnh báo đã được dọn sạch.</i>"
                custom_keyboard = {
                    "inline_keyboard": [
                        [{"text": "🔄 Tải Lại Nhật Ký Mới", "callback_data": "system:logs"}],
                        [{"text": "◀️ Về trang chính", "callback_data": "menu:main"}]
                    ]
                }
                data["custom_formatted_text"] = html_text
                data["custom_keyboard"] = custom_keyboard
            # ---------------------------------------------


            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_SYSTEM_DATA,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": event.data.get("action"),
                        "message_id": event.data.get("message_id"),
                        **data,
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling system data request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_SYSTEM_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    # 7. Settings Data
    async def _handle_settings_data_request(self, event: Event) -> None:
        try:
            action = event.data.get("action")

            data: Dict[str, Any] = {}
            s = self.settings
            if action == "bot_settings":
                data = {
                    "margin_per_order_usdt": s.margin_per_order_usdt,
                    "default_leverage": s.default_leverage,
                    "active_strategy": "EMA Crossover" if s.enable_default_strategy else "None",
                    "ema_fast": s.ema_fast_period,
                    "ema_slow": s.ema_slow_period,
                    "timeframes": ", ".join(s.timeframes),
                    "is_demo": s.okx_demo_mode,
                    "production_risk_mode": s.production_risk_mode,
                    "radar_limit": s.radar_limit,
                    "adx_min_all": s.adx_min_threshold_all,
                    "adx_min_long_tf": s.adx_min_threshold_long_tf,
                }
            elif action == "risk_limits":
                data = {
                    "sl_roe_pct": s.sl_roe_pct,
                    "tp1_roe_pct": s.tp1_roe_pct,
                    "tp2_roe_pct": s.tp2_roe_pct,
                    "tp3_roe_pct": s.tp3_roe_pct,
                    "tp1_exit_pct": s.tp1_exit_pct,
                    "tp2_exit_pct": s.tp2_exit_pct,
                    "margin_mode": s.margin_mode,
                    "max_leverage": s.max_leverage,
                    "demo_mode": s.okx_demo_mode,
                    "production_risk_mode": s.production_risk_mode,
                    "max_open_positions": s.max_open_positions,
                    "max_daily_drawdown": s.max_daily_drawdown,
                    "max_symbol_concentration": s.max_symbol_concentration,
                    "min_risk_reward_ratio": s.min_risk_reward_ratio,
                    "max_risk_allowed_pct": s.max_risk_allowed_pct,
                }
            elif action == "watchlist":
                data = {
                    "symbols": s.get_active_watchlist(),
                    "radar_limit": s.radar_limit,
                    "total_watchlist": len(s.watchlist),
                }
            elif action == "notifications":
                data = {
                    "signals": s.telegram_notification_signals,
                    "trades": s.telegram_notification_trades,
                    "daily_report": s.telegram_notification_daily_report,
                }

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_SETTINGS_DATA,
                    data={
                        "chat_id": event.data.get("chat_id"),
                        "action": action,
                        "message_id": event.data.get("message_id"),
                        "settings": data,
                    },
                    source="position_telegram_handler",
                )
            )
        except Exception as e:
            logger.error("Error handling settings data request: {}", e)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_SETTINGS_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="position_telegram_handler",
                    )
                )
            except Exception:
                pass

    async def _handle_position_close_request(self, event: Event) -> None:
        """Handle position close request from EventBus."""
        try:
            request = event.data
            await self.engine.close_position_secure(request)
        except Exception as e:
            logger.error("Error delegating position close request: {}", e)