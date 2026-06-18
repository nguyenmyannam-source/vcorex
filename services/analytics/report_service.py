import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from infrastructure.storage.database import Position
from infrastructure.storage.repository import UnitOfWork


class ReportService:
    def __init__(self, event_bus: EventBus, session_factory, position_engine):
        self.event_bus = event_bus
        self.session_factory = session_factory
        self.engine = position_engine
        self._running = False
        self._task = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        
        self._signals_generated = 0
        self._signals_rejected = 0
        self._rejected_reasons = {}
        
        self.event_bus.subscribe(self._on_signal_generated, [EventTopic.STRATEGY_SIGNAL_GENERATED], handler_id="report_svc_sig_gen")
        self.event_bus.subscribe(self._on_signal_rejected, [EventTopic.RISK_SIGNAL_REJECTED], handler_id="report_svc_sig_rej")
        
        logger.info("ReportService started (Hourly PnL Reports)")

    async def _on_signal_generated(self, event: Event) -> None:
        self._signals_generated += 1

    async def _on_signal_rejected(self, event: Event) -> None:
        self._signals_rejected += 1
        reason = event.data.get("rejection_reason", "unknown")
        
        if "Insufficient available margin" in reason or "margin" in reason.lower():
            short_reason = "Thiếu tiền ký quỹ (Hết Margin)"
        elif "Max open positions" in reason:
            short_reason = "Đạt giới hạn số lệnh mở"
        elif "Slippage" in reason:
            short_reason = "Lệch giá (Trượt giá)"
        elif "timeframe" in reason.lower() or "not confirmed" in reason.lower() or "not enough candles" in reason.lower():
            short_reason = "Chờ nến xác nhận"
        elif "duplicate" in reason.lower() or "already" in reason.lower():
            short_reason = "Đã có vị thế cặp này"
        else:
            short_reason = "Lý do rủi ro khác"
            
        self._rejected_reasons[short_reason] = self._rejected_reasons.get(short_reason, 0) + 1

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.event_bus.unsubscribe("report_svc_sig_gen")
        self.event_bus.unsubscribe("report_svc_sig_rej")
        logger.info("ReportService stopped")

    async def _run_loop(self):
        """Vòng lặp kiểm tra mỗi phút để xem có đến giờ báo cáo không"""
        while self._running:
            try:
                now = datetime.now()
                # Nếu là phút đầu tiên của giờ mới (ví dụ 10:00)
                if now.minute == 0:
                    await self.send_hourly_report()
                    # Ngủ 61 giây để tránh bắn liên tục trong phút thứ 0
                    await asyncio.sleep(61)
                else:
                    # Ngủ đến phút tiếp theo
                    await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Error in ReportService loop: {e}")
                await asyncio.sleep(60)

    async def send_hourly_report(self):
        """Tính toán và gửi báo cáo 1 giờ qua"""
        try:
            logger.info("Generating hourly PnL report...")

            # 1. Tính lãi/lỗ thực tế trong 1 giờ qua từ DB
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            realized_1h = 0.0

            async with UnitOfWork(self.session_factory) as uow:
                assert uow.session is not None
                result = await uow.session.execute(
                    select(func.sum(Position.realized_pnl))
                    .where(Position.status.in_(["CLOSED", "LIQUIDATED"]))
                    .where(Position.closed_at >= one_hour_ago)
                )
                realized_1h = float(result.scalar() or 0.0)

            # 2. Lấy PnL tạm tính và số vị thế từ ExchangeMirror (hoặc PositionEngine fallback)
            unrealized = 0.0
            active_count = 0
            
            use_mirror = hasattr(self.engine, "exchange_mirror") and self.engine.exchange_mirror is not None
            if use_mirror:
                mirror_positions = self.engine.exchange_mirror.get_all_positions().values()
                active_count = len(mirror_positions)
                unrealized = sum(p.upl for p in mirror_positions)
            else:
                positions = list(self.engine._positions.values())
                unrealized = sum(p.pnl for p in positions)
                active_count = len(positions)

            # 3. Kiểm tra sức khỏe hệ thống
            is_healthy = True
            if hasattr(self.engine.exchange, "_ws_connected"):
                is_healthy = self.engine.exchange._ws_connected

            report_data = {
                "realized_1h": realized_1h,
                "unrealized": unrealized,
                "active_positions": active_count,
                "is_healthy": is_healthy,
                "signals_generated": getattr(self, "_signals_generated", 0),
                "signals_rejected": getattr(self, "_signals_rejected", 0),
                "rejected_reasons": getattr(self, "_rejected_reasons", {})
            }
            
            self._signals_generated = 0
            self._signals_rejected = 0
            self._rejected_reasons = {}

            # 4. Bắn event để NotificationService gửi Telegram
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.NOTIFICATION_PERIODIC_REPORT,
                    data=report_data,
                    source="report_service",
                )
            )
            logger.info(f"Hourly report sent: Realized={realized_1h}, Active={active_count}")

        except Exception as e:
            logger.error(f"Failed to send hourly report: {e}")
