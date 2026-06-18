"""
Notification Service - Enterprise PRO Notification Engine.
Tách biệt hoàn toàn logic gửi thông báo tự động (Proactive Notifications) khỏi UI (TelegramBot).
"""

import asyncio
import functools
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple
import os

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from core.config.settings import Settings
from core.event_bus_components import Event
from core.event_bus import EventBus
from core.events.topics import EventTopic
from interfaces.telegram.message_templates import MessageTemplates
from interfaces.telegram.keyboards import TelegramKeyboards


@dataclass(frozen=True, order=True)
class QueuedMessage:
    """Bất biến Message với Priority để thực hiện cơ chế chống tràn thông minh."""
    priority: int  # 0 = Thấp, 1 = Trung bình, 2 = Cao (khẩn cấp)
    timestamp: float
    text: str
    photo_path: Optional[str] = field(default=None, compare=False)


class NotificationService:
    """
    Central Engine for processing system events and sending notifications.
    Được thiết kế để hỗ trợ gửi nhiều Chat IDs (Channels/Groups) và mở rộng đa nền tảng.
    CÁCH LÝ 100% luồng Telegram khỏi luồng chính bằng ThreadPoolExecutor.
    """

    # Cấu hình ưu tiên tin nhắn và chống tràn
    MAX_QUEUE_SIZE = 50  # Giới hạn hàng đợi tối đa
    STALE_THRESHOLD = 300  # 5 phút = 300s - tự động xóa tin nhắn cũ hơn ngưỡng này
    DUPLICATE_MESSAGE_WINDOW = 5  # 5 giây - nếu message giống nhau trong 5s, skip
    PRIORITY_LEVELS = {
        "system": 2,    # System alerts, API errors = KHẤN CẤP
        "position": 2,  # Position opened/closed = KHẤN CẤP
        "signal": 1,    # Strategy signals = TRUNG BÌNH
        "report": 0     # Periodic reports = THẤP
    }

    def __init__(self, event_bus: EventBus, settings: Settings):
        self.event_bus = event_bus
        self.settings = settings

        # Cho phép gửi tới nhiều Chat ID
        raw_chat_ids = getattr(
            self.settings, "telegram_chat_ids", str(self.settings.telegram_chat_id)
        )
        self.chat_ids: List[str] = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]

        self.bot_token = self.settings.telegram_bot_token
        self._bot: Optional[Bot] = None
        self._enabled = bool(self.settings.telegram_enabled and self.bot_token and self.chat_ids)

        # HÀNG ĐỢI TIN NHẮN BẤT BIẾN VỚI ƯU TIÊN - CHỐNG TRÀN
        self._queue = asyncio.PriorityQueue(maxsize=self.MAX_QUEUE_SIZE)

        # DEDUPLICATION TRACKING - Ngăn gửi cùng message hai lần liên tiếp
        self._last_message_by_category: dict = {}  # {"category": (hash, timestamp)}

        # THREAD POOL EXECUTOR - CÁCH LÝ HOÀN TOÀN LUỒNG TELEGRAM
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._main_event_loop = None  # Lưu reference tới main event loop

        if not self._enabled:
            logger.warning(
                "NotificationService is disabled due to missing credentials or TELEGRAM_ENABLED=false."
            )

    async def start(self) -> None:
        """Khởi động Notification Engine với ThreadPoolExecutor độc lập."""
        if not self._enabled:
            return

        if not self.bot_token:
            logger.error("Telegram bot token not provided for NotificationService")
            return

        self._bot = Bot(token=self.bot_token)
        self._main_event_loop = asyncio.get_running_loop()  # Lưu reference tới main event loop
        self._running = True
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram_worker")

        # Đăng ký lắng nghe sự kiện
        self._subscribe_events()

        # Khởi động DUY NHẤT 1 worker task trên event loop, nó sẽ đẩy công việc vào ThreadPool
        self._worker_task = asyncio.create_task(self._outbox_processing_loop())

        logger.info(
            f"NotificationService started with isolated ThreadPoolExecutor. Broadcasting to {len(self.chat_ids)} channel(s)."
        )

    _HANDLER_IDS = (
        "notif_new_signal",
        "notif_signal_rejected",
        "notif_pos_opened",
        "notif_pos_closed",
        "notif_chart_generated",
        "notif_ghost",
        "notif_volatility",
        "notif_sys_alert",
        "notif_telegram_send_message",
        "notif_api_error",
        "notif_periodic_report",
    )

    def _unsubscribe_events(self) -> None:
        for handler_id in self._HANDLER_IDS:
            self.event_bus.unsubscribe(handler_id=handler_id)

    async def stop(self) -> None:
        """Dừng Notification Engine an toàn, giải phóng ThreadPool."""
        self._running = False
        self._unsubscribe_events()

        # Cancel worker task asyncio
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        # Shutdown ThreadPoolExecutor
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=True)

        logger.info("NotificationService stopped cleanly, Telegram thread pool terminated.")

    def _subscribe_events(self) -> None:
        """Subscribe to proactive events."""
        # Signals & Trades
        self.event_bus.subscribe(
            self._on_new_signal,
            [EventTopic.STRATEGY_SIGNAL_GENERATED],
            handler_id="notif_new_signal",
        )
        self.event_bus.subscribe(
            self._on_signal_rejected,
            [EventTopic.SIGNAL_REJECTED, EventTopic.RISK_SIGNAL_REJECTED],
            handler_id="notif_signal_rejected",
        )
        self.event_bus.subscribe(
            self._on_order_execution, [EventTopic.POSITION_OPENED], handler_id="notif_pos_opened"
        )
        self.event_bus.subscribe(
            self._on_position_closed, [EventTopic.POSITION_CLOSED], handler_id="notif_pos_closed"
        )
        self.event_bus.subscribe(
            self._on_chart_generated, [EventTopic.CHART_GENERATED], handler_id="notif_chart_generated"
        )

        # Alerts & Volatility
        self.event_bus.subscribe(
            self._on_ghost_position, [EventTopic.POSITION_GHOST_DETECTED], handler_id="notif_ghost"
        )
        self.event_bus.subscribe(
            self._on_volatility, [EventTopic.MARKET_VOLATILITY_ALERT], handler_id="notif_volatility"
        )

        # System & Errors
        self.event_bus.subscribe(
            self._on_system_alert, [EventTopic.SYSTEM_ALERT], handler_id="notif_sys_alert"
        )
        self.event_bus.subscribe(
            self._on_telegram_send_message,
            [EventTopic.TELEGRAM_SEND_MESSAGE],
            handler_id="notif_telegram_send_message",
        )
        self.event_bus.subscribe(
            self._on_api_error, [EventTopic.SYSTEM_API_ERROR], handler_id="notif_api_error"
        )

        # Periodic Reports
        self.event_bus.subscribe(
            self._on_periodic_report,
            [EventTopic.NOTIFICATION_PERIODIC_REPORT],
            handler_id="notif_periodic_report",
        )

    def _send_telegram_message_sync(self, text: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """HÀM ĐƯỢC CHẠY TRONG THREAD POOL - Bất kỳ blocking I/O nào cũng không ảnh hưởng luồng chính."""
        if not self._bot:
            return

        for chat_id in self.chat_ids:
            sent = False
            retry_count = 0
            max_retries = 3

            while not sent and retry_count < max_retries:
                try:
                    res = self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=TelegramKeyboards.get_back_to_main_menu(),
                    )
                    # Support both sync and async Bot implementations
                    try:
                        import inspect
                        if inspect.iscoroutine(res) or asyncio.iscoroutine(res):
                            # Run the coroutine using run_coroutine_threadsafe nếu có loop
                            if loop and not loop.is_closed():
                                future = asyncio.run_coroutine_threadsafe(res, loop)
                                future.result(timeout=30.0)  # Chờ tối đa 30s để tránh timeout khi mạng chậm
                            else:
                                # Fallback: Tạo event loop mới trong thread này
                                try:
                                    asyncio.run(res)
                                except RuntimeError as e:
                                    if "Event loop is closed" in str(e):
                                        logger.debug("Telegram message skipped: Event loop is closed.")
                                    else:
                                        raise
                        # else: synchronous call already executed
                    except Exception as run_err:
                        if "Event loop is closed" in str(run_err):
                            logger.debug("Telegram message skipped: Bot is shutting down (Event loop closed).")
                        else:
                            logger.warning(f"Failed to run async send_message in thread: {run_err}")
                    sent = True
                    # Khoảng nghỉ tối thiểu 1.0s giữa các tin nhắn gửi đi để tránh Telegram Rate Limit (1 msg/s/chat)
                    time.sleep(1.0)
                    logger.info(f"✅ Telegram message sent to {chat_id}")
                except RetryAfter as e:
                    # Bị dính Rate Limit của Telegram
                    retry_seconds = getattr(e, "retry_after", 5)
                    logger.warning(f"Telegram Flood Control hit for {chat_id}. Sleeping for {retry_seconds}s...")
                    time.sleep(retry_seconds + 0.5)
                    retry_count += 1
                except TelegramError as e:
                    logger.error(f"Telegram error sending to {chat_id}: {e}")
                    break  # Không retry với lỗi cú pháp hoặc mất kết nối
                except Exception as e:
                    logger.error(f"Unexpected error broadcasting to {chat_id}: {e}")
                    break

    def _send_telegram_photo_sync(self, photo_path: str, text: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """HÀM ĐƯỢC CHẠY TRONG THREAD POOL - Gửi ảnh và xóa an toàn."""
        if not self._bot:
            return
            
        try:
            for chat_id in self.chat_ids:
                sent = False
                retry_count = 0
                max_retries = 3

                while not sent and retry_count < max_retries:
                    try:
                        with open(photo_path, 'rb') as photo_file:
                            res = self._bot.send_photo(
                                chat_id=chat_id,
                                photo=photo_file,
                                caption=text,
                                parse_mode=ParseMode.HTML,
                                reply_markup=TelegramKeyboards.get_back_to_main_menu(),
                            )
                            # Support async
                            import inspect
                            if inspect.iscoroutine(res) or asyncio.iscoroutine(res):
                                if loop and not loop.is_closed():
                                    future = asyncio.run_coroutine_threadsafe(res, loop)
                                    future.result(timeout=30.0)  # Chờ tối đa 30s để tránh timeout khi tải ảnh nặng
                                else:
                                    try:
                                        asyncio.run(res)
                                    except RuntimeError:
                                        pass
                        sent = True
                        time.sleep(1.0)
                        logger.info(f"✅ Telegram photo sent to {chat_id}")
                    except RetryAfter as e:
                        retry_seconds = getattr(e, "retry_after", 5)
                        logger.warning(f"Telegram Flood Control hit. Sleeping {retry_seconds}s...")
                        time.sleep(retry_seconds + 0.5)
                        retry_count += 1
                    except Exception as e:
                        logger.error(f"Error sending photo to {chat_id}: {e}")
                        break
        finally:
            # TỰ ĐỘNG DỌN RÁC Ổ CỨNG THEO TIÊU CHUẨN
            if photo_path and os.path.exists(photo_path):
                try:
                    os.remove(photo_path)
                    logger.debug(f"[MEMORY SAFETY] Deleted temporary chart image: {photo_path}")
                except Exception as e:
                    logger.error(f"Failed to delete temporary chart {photo_path}: {e}")

    # ==================== GỬI TIN NHẮN (BROADCAST) VỚI CHỐNG TRÀN ====================
    def _lazy_init_infrastructure(self) -> None:
        """Lazy init để test/daemon không cần gọi start() trước vẫn có worker gửi Telegram."""
        if not self._enabled:
            return

        # Tạo Telegram client/executor/worker chỉ một lần
        if not self._executor:
            if not self.bot_token:
                return
            if not self._bot:
                self._bot = Bot(token=self.bot_token)

            # ThreadPool để gửi message (blocking I/O)
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram_worker")

        if not self._running:
            self._running = True

        if self._worker_task is None or self._worker_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._main_event_loop = loop  # Lưu reference tới loop
            except RuntimeError:
                # Chạy ngoài event-loop (hiếm khi xảy ra trong test)
                return
            self._worker_task = loop.create_task(self._outbox_processing_loop())

    async def _enqueue_message(self, text: str, category: str = "signal", photo_path: Optional[str] = None) -> None:
        """Đưa tin nhắn vào hàng đợi với ưu tiên và cơ chế chống tràn."""
        self._lazy_init_infrastructure()

        if not self._enabled or not self._running or not self._executor:
            return

        # [DEDUPLICATION] Kiểm tra nếu message giống nhau được gửi liên tiếp trong 5 giây
        import hashlib
        msg_hash = hashlib.md5(text.encode()).hexdigest()
        current_time = time.time()

        if category in self._last_message_by_category:
            last_hash, last_time = self._last_message_by_category[category]
            if last_hash == msg_hash and (current_time - last_time) < self.DUPLICATE_MESSAGE_WINDOW:
                logger.debug(f"[DEDUP] Skipping duplicate {category} message (hash={msg_hash[:8]}..., age={current_time - last_time:.1f}s)")
                return

        # Cập nhật tracking
        self._last_message_by_category[category] = (msg_hash, current_time)

        # Xử lý chống tràn hàng đợi TRƯỚC KHI thêm tin mới
        if self._queue.full():
            from core.config.logging import log_telegram_event
            log_telegram_event("queue_overflow", {"queue_size": self._queue.qsize(), "action": "start_cleanup"})
            logger.warning("[TELEGRAM-QUEUE] Hàng đợi đầy! Bắt đầu thanh lọc các tin nhắn cũ/thấp ưu tiên.")
            # 1. Xóa tất cả tin nhắn cũ hơn 5 phút (STALE_THRESHOLD)
            current_time = time.time()
            temp_messages = []
            while not self._queue.empty():
                msg = await self._queue.get()
                if (current_time - msg.timestamp) < self.STALE_THRESHOLD:
                    temp_messages.append(msg)
                else:
                    logger.info(f"[TELEGRAM-QUEUE] Xóa tin nhắn cũ: {msg.text[:50]}...")
                    log_telegram_event("stale_message_dropped", {"timestamp": msg.timestamp, "text_preview": msg.text[:50]})

            # Nếu vẫn còn quá nhiều, xóa các tin nhắn ưu tiên thấp (priority 0)
            if len(temp_messages) >= self.MAX_QUEUE_SIZE - 1:
                temp_messages.sort(reverse=True)  # Sắp xếp giảm dần ưu tiên, giữ lại tin khẩn cấp
                temp_messages = temp_messages[:self.MAX_QUEUE_SIZE - 5]  # Chừa lại 5 slot cho tin mới

            # Đưa lại các tin nhắn còn sót vào hàng đợi
            for msg in temp_messages:
                await self._queue.put(msg)

        # Thêm tin nhắn mới với đúng ưu tiên
        priority = self.PRIORITY_LEVELS.get(category, 1)  # Mặc định trung bình
        new_msg = QueuedMessage(
            priority=priority,
            timestamp=time.time(),
            text=text,
            photo_path=photo_path
        )
        await self._queue.put(new_msg)
        logger.debug(f"[TELEGRAM-QUEUE] Tin nhắn '{category}' được thêm vào hàng đợi (qsize={self._queue.qsize()})")

    async def _outbox_processing_loop(self) -> None:
        """Background worker chạy trên event loop, ĐỨY CÔNG VIỆC GỬI TIN vào ThreadPoolExecutor."""
        logger.info("Telegram Outbox Processing Loop (ThreadPool Mode) started.")
        loop = asyncio.get_running_loop()

        while self._running and self._executor:
            # In tests we may enqueue and assert quickly; give event-loop a chance.
            await asyncio.sleep(0)

            try:
                # Lấy tin nhắn từ hàng đợi ưu tiên
                queued_msg = await self._queue.get()


                # KIỂM TRA LẠI NẾU TIN NHẮN ĐÃ CŨ (quá 5 phút) -> BỎ QUA
                current_time = time.time()
                if (current_time - queued_msg.timestamp) > self.STALE_THRESHOLD:
                    logger.info(f"[TELEGRAM-QUEUE] Bỏ qua tin nhắn cũ: {queued_msg.text[:50]}...")
                    self._queue.task_done()
                    continue

                # ĐỨY GỬI TIN VÀO THREAD POOL - KHÔNG BLOCK LUỒNG CHÍNH!
                # Truyền loop vào callback để có thể dùng run_coroutine_threadsafe
                if getattr(queued_msg, "photo_path", None) and os.path.exists(queued_msg.photo_path):
                    send_func = functools.partial(
                        self._send_telegram_photo_sync, 
                        queued_msg.photo_path, 
                        queued_msg.text, 
                        loop=loop
                    )
                else:
                    send_func = functools.partial(
                        self._send_telegram_message_sync, 
                        text=queued_msg.text, 
                        loop=loop
                    )
                await loop.run_in_executor(
                    self._executor,
                    send_func
                )

                self._queue.task_done()

            except asyncio.CancelledError:
                logger.info("Telegram outbox loop received cancellation.")
                break
            except Exception as e:
                logger.error(f"Error in Telegram Outbox Loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    # ==================== BROADCAST WRAPPERS ====================
    async def _broadcast_message(self, text: str, category: str = "signal", photo_path: Optional[str] = None) -> None:
        """Wrapper chung để gọi _enqueue_message với đúng category."""
        await self._enqueue_message(text, category, photo_path)

    # ==================== EVENT HANDLERS VỚI CATEGORY ƯU TIÊN ====================
    async def _on_new_signal(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_signals", True):
            return
        alert_text = MessageTemplates.get_new_signal_alert(event.data)
        await self._broadcast_message(alert_text, category="signal")

    async def _on_signal_rejected(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_signals", True):
            return
        alert_text = MessageTemplates.get_signal_rejection_message(event.data)
        await self._broadcast_message(alert_text, category="signal")

    async def _on_order_execution(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_trades", True):
            return
            
        # [FEATURE] Chart Auto-Generation on New Position (Legacy fallback)
        photo_path = None
        try:
            from core.container import container
            chart_svc = container.get("chart_service") if container.has("chart_service") else None
            if chart_svc and hasattr(chart_svc, "generate_sync"):
                photo_path = await asyncio.to_thread(
                    chart_svc.generate_sync,
                    event.data.get("symbol", ""),
                    event.data.get("timeframe", "1H")
                )
        except Exception as e:
            logger.warning(f"Failed to auto-generate chart: {e}")

        text = MessageTemplates.get_order_execution_notification(event.data)
        await self._broadcast_message(text, category="position", photo_path=photo_path)

        tp = event.data.get("take_profit_levels") or event.data.get("tp")
        sl = event.data.get("stop_loss") or event.data.get("sl")
        if tp or sl:
            tpsl_text = MessageTemplates.get_tpsl_placement_notification(event.data)
            await self._broadcast_message(tpsl_text, category="position")

    async def _on_chart_generated(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_signals", True):
            return
        # Send the chart photo with a short caption
        data = event.data
        symbol = data.get("symbol", "")
        timeframe = data.get("timeframe", "")
        side = data.get("side", "")
        photo_path = data.get("photo_path")
        
        caption = f"📊 <b>Bản Đồ Kỹ Thuật {symbol}</b> ({timeframe})\n⚡ Tín hiệu: <b>{side}</b>"
        if photo_path:
            await self._broadcast_message(caption, category="signal", photo_path=photo_path)

    async def _on_position_closed(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_trades", True):
            return
        notification_text = MessageTemplates.get_position_closed_notification(event.data)
        await self._broadcast_message(notification_text, category="position")

    async def _on_ghost_position(self, event: Event) -> None:
        # Hardened: ensure ghost alerts are always dispatched even if template generation fails.
        try:
            alert_text = MessageTemplates.get_ghost_position_alert(event.data)
        except Exception:
            alert_text = f"🚨 <b>GHOST POSITION DETECTED</b>\n\n{event.data}"
        await self._broadcast_message(alert_text, category="system")
        # Ensure immediate processing in tests
        await asyncio.sleep(0)




    async def _on_volatility(self, event: Event) -> None:
        alert_text = MessageTemplates.get_volatility_alert(event.data)
        await self._broadcast_message(alert_text, category="system")


    async def _on_telegram_send_message(self, event: Event) -> None:
        """Deliver raw HTML messages published by backend handlers (e.g. manual close)."""
        if not getattr(self.settings, "telegram_notification_trades", True):
            return
        data = event.data if isinstance(event.data, dict) else {}
        text = data.get("message")
        if text:
            await self._broadcast_message(text, category="position")

    async def _on_system_alert(self, event: Event) -> None:
        try:
            from interfaces.telegram.message_templates import MessageTemplates
            if isinstance(event.data, dict) and ("level" in event.data or "title" in event.data):
                alert_text = MessageTemplates.get_system_alert(event.data)
            else:
                alert_text = f"⚠️ <b>SYSTEM ALERT</b>\n\n{event.data.get('message', 'Unknown alert')}"
        except Exception as format_error:
            from core.config.logging import log_telegram_event
            payload_type = type(event.data).__name__
            logger.error(f"Failed to format SYSTEM_ALERT: {format_error}. Triggering RAW FALLBACK ENGINE.")

            # Raw Fallback Engine (Bulletproof)
            try:
                import json
                raw_payload = json.dumps(event.data, indent=2, ensure_ascii=False)
            except Exception as json_error:
                try:
                    raw_payload = repr(event.data)
                except Exception as repr_error:
                    raw_payload = "UNSERIALIZABLE_PAYLOAD_CRITICAL_FAILURE"
                    log_telegram_event("fallback_serialization_error", {
                        "handler": "_on_system_alert",
                        "original_error": str(format_error),
                        "json_error": str(json_error),
                        "repr_error": str(repr_error),
                        "payload_type": payload_type
                    })

            import html
            alert_text = f"🚨 <b>SYSTEM ALERT (RAW FALLBACK)</b>\n\n<pre>{html.escape(raw_payload)}</pre>"
            log_telegram_event("fallback_triggered", {"handler": "_on_system_alert", "payload_type": payload_type})

        await self._broadcast_message(alert_text, category="system")

    async def _on_api_error(self, event: Event) -> None:
        try:
            from interfaces.telegram.message_templates import MessageTemplates
            text = MessageTemplates.get_system_alert(event.data)
        except Exception as format_error:
            payload_type = type(event.data).__name__
            logger.error(f"Failed to format API_ERROR: {format_error}. Triggering RAW FALLBACK.")
            try:
                import json
                raw_payload = json.dumps(event.data, indent=2, ensure_ascii=False)
            except Exception as json_error:
                try:
                    raw_payload = repr(event.data)
                except Exception as repr_error:
                    raw_payload = "UNSERIALIZABLE_PAYLOAD_CRITICAL_FAILURE"
                    from core.config.logging import log_telegram_event
                    log_telegram_event("fallback_serialization_error", {
                        "handler": "_on_api_error",
                        "original_error": str(format_error),
                        "json_error": str(json_error),
                        "repr_error": str(repr_error),
                        "payload_type": payload_type
                    })
            import html
            text = f"🚨 <b>API ERROR (RAW FALLBACK)</b>\n\n<pre>{html.escape(raw_payload)}</pre>"

        await self._broadcast_message(text, category="system")

    async def _on_periodic_report(self, event: Event) -> None:
        if not getattr(self.settings, "telegram_notification_daily_report", True):
            return
        text = MessageTemplates.get_hourly_report_msg(event.data)
        await self._broadcast_message(text, category="report")