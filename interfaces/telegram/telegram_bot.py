"""
Telegram Bot core implementation - Phase 11 (Refactored).
Only handles UI interactions, NO business logic.
All trading decisions go through event bus.
Uses composition pattern with MessageRenderer, MessageDispatcher, RateLimiter, DashboardController.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Optional, Set, Dict

from loguru import logger
from telegram import Bot, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from core.config.settings import settings
from core.event_bus_components import Event
from core.event_bus import EventBus
from core.events.topics import EventTopic
from core.events.payloads import PositionCloseRequest, PositionAction
from core.metrics import MetricsAdapter, InMemoryMetricsAdapter
from interfaces.telegram.callback_tokens import CallbackTokenStore
from interfaces.telegram.dashboard_controller import DashboardController
from interfaces.telegram.keyboards import TelegramKeyboards
from interfaces.telegram.message_dispatcher import MessageDispatcher
from interfaces.telegram.message_renderer import MessageRenderer
from interfaces.telegram.message_templates import MessageTemplates
from interfaces.telegram.rate_limiter import RateLimiter

_LAYER1_LOCK_MSG = (
    "⚠️ Một yêu cầu đóng vị thế khác đang được xử lý. Vui lòng đợi."
)


def admin_required(func):
    """
    Decorator to check if the user is an authorized admin.
    """

    @wraps(func)
    async def wrapped(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            logger.warning("Unauthorized Telegram update without effective user")
            if update.callback_query:
                await update.callback_query.answer("⚠️ Unauthorized", show_alert=True)
            elif update.message:
                await update.message.reply_text(
                    "⚠️ Unauthorized: You do not have permission to use this command."
                )
            return

        user_id = str(update.effective_user.id)
        if user_id not in settings.telegram_admin_ids:
            logger.warning(f"Unauthorized access attempt from user ID {user_id}")
            if update.callback_query:
                await update.callback_query.answer("⚠️ Unauthorized", show_alert=True)
            elif update.message:
                await update.message.reply_text(
                    "⚠️ Unauthorized: You do not have permission to use this command."
                )
            return
        return await func(self, update, context, *args, **kwargs)

    return wrapped


class TelegramBot:
    """
    Telegram Bot class - Refactored with composition.
    Delegates: rendering to MessageRenderer, dispatching to MessageDispatcher,
    rate limiting to RateLimiter, dashboard updates to DashboardController.
    """

    def __init__(self, event_bus: EventBus, metrics: Optional[MetricsAdapter] = None):
        if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram bot disabled by configuration or credentials not configured.")
            self._enabled = False
            return

        self.event_bus = event_bus
        self.settings = settings  # Store settings as instance variable
        self._enabled = True
        self._running = False
        self._bot: Optional[Bot] = None
        self._application = None
        self._chat_id = int(settings.telegram_chat_id)
        self._active_messages: Set[int] = set()  # Track sent messages for cleanup
        self._last_heartbeat: Optional[datetime] = None

        # Inject metrics adapter interface
        self._metrics = metrics or InMemoryMetricsAdapter()

        # Initialize helper components
        self._rate_limiter = RateLimiter()
        self._renderer = MessageRenderer()
        self._dispatcher: MessageDispatcher = MessageDispatcher(
            None, self._chat_id, self.event_bus, self._rate_limiter
        )
        self._dashboard = DashboardController(event_bus)

        # Layer 1 Concurrency Locks for position actions
        self._position_action_locks: Dict[str, asyncio.Lock] = {}
        self._position_close_futures: Dict[str, asyncio.Future] = {}
        
        # [FIX LỖI 5] Debounce mechanism để chống spam-click
        self._position_action_timestamps: Dict[str, float] = {}
        self._DEBOUNCE_SECONDS = 2.0  # 2 giây debounce cho cùng một action

        # Dangerous Action Cooldowns
        self._action_cooldowns: Dict[str, float] = {}
        self._COOLDOWN_SECONDS = {
            "clean_bot": 120,      # 2 phút
            "emergency_stop": 30,  # 30 giây
            "reset_signals": 30,   # 30 giây
        }

        # Subscribe to events we need to notify about
        self._subscribe_events()
        logger.info("TelegramBot initialized (SRP refactored mode with composition)")

    def _subscribe_events(self) -> None:
        """Subscribe to events from system that require Telegram notifications."""
        if not self._enabled:
            return

        # Responses to UI requests
        self.event_bus.subscribe(
            self._on_health_data_response,
            [EventTopic.TELEGRAM_RESPONSE_HEALTH_DATA],
            handler_id="tele_res_health",
        )
        self.event_bus.subscribe(
            self._on_trading_data_response,
            [EventTopic.TELEGRAM_RESPONSE_TRADING_DATA],
            handler_id="tele_res_trading",
        )
        self.event_bus.subscribe(
            self._on_analytics_data_response,
            [EventTopic.TELEGRAM_RESPONSE_ANALYTICS_DATA],
            handler_id="tele_res_analytics",
        )
        self.event_bus.subscribe(
            self._on_history_data_response,
            [EventTopic.TELEGRAM_RESPONSE_HISTORY_DATA],
            handler_id="tele_res_history",
        )
        self.event_bus.subscribe(
            self._on_exchange_status_response,
            [EventTopic.TELEGRAM_RESPONSE_EXCHANGE_STATUS],
            handler_id="tele_res_exchange",
        )
        self.event_bus.subscribe(
            self._on_system_data_response,
            [EventTopic.TELEGRAM_RESPONSE_SYSTEM_DATA],
            handler_id="tele_res_system",
        )
        self.event_bus.subscribe(
            self._on_news_data_response,
            [EventTopic.TELEGRAM_RESPONSE_NEWS_DATA],
            handler_id="tele_res_news",
        )
        self.event_bus.subscribe(
            self._on_position_close_success,
            [EventTopic.POSITION_CLOSE_SUCCESS],
            handler_id="tele_res_close_success",
        )
        self.event_bus.subscribe(
            self._on_position_close_failure,
            [EventTopic.POSITION_CLOSE_FAILURE],
            handler_id="tele_res_close_failure",
        )
        self.event_bus.subscribe(
            self._on_emergency_stop_complete,
            [EventTopic.CONTROL_EMERGENCY_STOP_COMPLETE],
            handler_id="tele_res_emergency_complete",
        )
        self.event_bus.subscribe(
            self._on_reset_signals_complete,
            [EventTopic.CONTROL_RESET_SIGNALS_COMPLETE],
            handler_id="tele_res_reset_signals",
        )
        self.event_bus.subscribe(
            self._on_clean_bot_complete,
            [EventTopic.CONTROL_CLEAN_BOT_COMPLETE],
            handler_id="tele_res_clean_bot",
        )
        self.event_bus.subscribe(
            self._on_settings_data_response,
            [EventTopic.TELEGRAM_RESPONSE_SETTINGS_DATA],
            handler_id="tele_res_settings",
        )

        logger.debug("TelegramBot subscribed to all system events")

    async def _on_reset_signals_complete(self, event: Event) -> None:
        """Handle reset signals complete event."""
        data = event.data
        message_id = data.get("message_id")
        success = data.get("success", False)
        if success:
            text = self._renderer.render_reset_signals_complete(data)
            await self._dispatcher.send_or_edit_message(
                text=text,
                message_id=message_id,
                reply_markup=TelegramKeyboards.get_main_menu(),
            )
        else:
            text = "❌ Lỗi khi reset tín hiệu."
            await self._dispatcher.send_or_edit_message(
                text=text,
                message_id=message_id,
                reply_markup=TelegramKeyboards.get_main_menu(),
            )

    async def start(self) -> None:
        """Start the Telegram bot."""
        if not self._enabled:
            logger.info("Telegram bot skipped (credentials not configured)")
            return

        self._running = True
        if not settings.telegram_bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not found in settings")
            return

        self._application = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token)
            .connection_pool_size(100)
            .pool_timeout(30.0)
            .get_updates_connection_pool_size(100)
            .get_updates_pool_timeout(30.0)
            .build()
        )
        self._bot = self._application.bot

        # Initialize dispatcher now that bot is ready
        self._dispatcher = MessageDispatcher(
            self._bot, self._chat_id, self.event_bus, self._rate_limiter
        )

        # Register command handlers
        self._application.add_handler(CommandHandler("start", self._cmd_start))
        self._application.add_handler(CommandHandler("menu", self._cmd_menu))
        self._application.add_handler(CommandHandler("status", self._cmd_status))

        # Register callback query handlers (all inline buttons)
        self._application.add_handler(CallbackQueryHandler(self._handle_callback))

        # Start the bot
        await self._application.initialize()
        await self._application.start()
        if self._application and self._application.updater:
            await self._application.updater.start_polling()

        # Start background tasks
        asyncio.create_task(self._heartbeat_loop())
        await self._dashboard.start_auto_update()

        logger.info("Telegram bot started successfully")

        # Send startup notification
        try:
            await self._dispatcher.send_or_edit_message(
                text="🚀 <b>VCOREX Institutional Bot is ONLINE</b>\n\n"
                "Hệ thống đã sẵn sàng và đang quét tín hiệu...\n"
                "Gõ /menu để mở bảng điều khiển.",
                reply_markup=TelegramKeyboards.get_main_menu(),
            )
        except Exception as e:
            logger.error(f"Failed to send startup notification: {e}")

    async def stop(self) -> None:
        """Stop the Telegram bot gracefully."""
        if not self._enabled or not self._running:
            return

        self._running = False
        await self._dashboard.stop_auto_update()

        for handler_id in (
            "tele_res_health",
            "tele_res_trading",
            "tele_res_analytics",
            "tele_res_history",
            "tele_res_exchange",
            "tele_res_system",
            "tele_res_news",
            "tele_res_close_success",
            "tele_res_close_failure",
            "tele_res_emergency_complete",
            "tele_res_reset_signals",
            "tele_res_clean_bot",
            "tele_res_settings",
        ):
            self.event_bus.unsubscribe(handler_id=handler_id)

        if self._application:
            if self._application.updater and self._application.updater.running:
                await self._application.updater.stop()
            elif self._application.updater:
                logger.debug("Telegram bot updater is not running - no action needed")
            await self._application.stop()
            await self._application.shutdown()

        logger.info("Telegram bot stopped")

    # ================ COMMAND HANDLERS ================
    @admin_required
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not context.bot or not update.message:
            return
        msg = await context.bot.send_message(
            chat_id=self._chat_id,
            text="⏳ Đang tải Bảng điều khiển...",
            parse_mode="HTML",
            reply_markup=TelegramKeyboards.get_main_menu(),
        )
        self._dashboard.set_message_id(msg.message_id)
        await self._dispatcher.publish_request_event(
            EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA,
            "dashboard",
            msg.message_id,
        )

    @admin_required
    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /menu command - show main menu."""
        if not context.bot or not update.message:
            return
        msg = await context.bot.send_message(
            chat_id=self._chat_id,
            text="⏳ Đang tải Bảng điều khiển...",
            parse_mode="HTML",
            reply_markup=TelegramKeyboards.get_main_menu(),
        )
        self._dashboard.set_message_id(msg.message_id)
        await self._dispatcher.publish_request_event(
            EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA,
            "dashboard",
            msg.message_id,
        )

    @admin_required
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - quick system status."""
        if update.effective_message:
            await self._dispatcher.publish_request_event(
                EventTopic.TELEGRAM_REQUEST_HEALTH_DATA,
                "health",
                update.effective_message.id,
            )

    # ================ CALLBACK HANDLER (INLINE BUTTONS) ================
    @admin_required
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all inline keyboard callbacks."""
        query = update.callback_query
        if not query:
            return

        # [ISSUE 1 FIX] Metric: Measure Telegram callback latency
        start_ms = time.time() * 1000

        try:
            await query.answer()  # Acknowledge the button press
        except BadRequest as e:
            error_msg = str(e).lower()
            if "query is too old" in error_msg or "query id is invalid" in error_msg:
                # [ISSUE 1 FIX] Downgrade WARNING to DEBUG and silent-ignore expired callbacks
                logger.debug(f"Telegram callback expired or too old (Silent Ignore): {e}")
                # Guard: Do not process this callback if it's already expired/timeout
                return
            else:
                logger.warning(f"Failed to answer callback query: {e}")

        # [ISSUE 1 FIX] Metric: Log latency
        latency_ms = (time.time() * 1000) - start_ms
        logger.debug(f"[PERF] telegram_callback_latency_ms: {latency_ms:.2f}ms")

        # Parse callback data
        data = query.data
        if not data:
            return

        # Clear dashboard auto-update if navigating away
        if data != "menu:main":
            self._dashboard.clear_message_id()

        # Route based on callback prefix
        if data.startswith("menu:"):
            await self._handle_menu_callback(query, data.split(":")[1])
        elif data.startswith("analytics:"):
            await self._handle_analytics_callback(query, data.split(":")[1])
        elif data.startswith("trading:"):
            await self._handle_trading_callback(query, data.split(":")[1])
        elif data.startswith("system:"):
            await self._handle_system_callback(query, data.split(":")[1])
        elif data.startswith("history:"):
            await self._handle_history_callback(query, data.split(":")[1])
        elif data.startswith("settings:"):
            await self._handle_settings_callback(query, data.split(":")[1])
        elif data.startswith("control:"):
            await self._handle_control_callback(query, data.split(":")[1])
        elif data.startswith("position:"):
            await self._handle_position_callback(query, data)
        elif data.startswith("pcl:") or data.startswith("pcf:"):
            await self._handle_position_token_callback(query, data)
        elif data.startswith("radar:"):
            await self._handle_radar_callback(query, data.split(":")[1])
        elif data.startswith("loading:"):
            return
        elif data.startswith("confirm:"):
            action = data.split(":")[1]
            token_meta = CallbackTokenStore.consume(action)
            if token_meta:
                await self._handle_position_confirm(query, token_meta)
            else:
                if action not in ("emergency_stop", "reset_signals"):
                    asyncio.create_task(self._metrics.increment_replay_attempts())
                await self._handle_confirm_callback(query, action)
        elif data.startswith("cancel:"):
            action = data.split(":")[1]
            token_meta = CallbackTokenStore.consume(action)
            if token_meta:
                try:
                    await query.edit_message_text(
                        "❌ Đã hủy yêu cầu đóng vị thế.",
                        reply_markup=TelegramKeyboards.get_main_menu()
                    )
                except Exception:
                    pass
            else:
                if action not in ("emergency_stop", "reset_signals"):
                    asyncio.create_task(self._metrics.increment_replay_attempts())
                try:
                    await query.edit_message_text(
                        "❌ Action cancelled", reply_markup=TelegramKeyboards.get_main_menu()
                    )
                except Exception:
                    pass

    # ================ MENU NAVIGATION HANDLERS ================
    async def _handle_menu_callback(self, query, submenu: str) -> None:
        """Handle main menu navigation."""
        if submenu == "main":
            self._dashboard.set_message_id(query.message.message_id)
            await query.edit_message_text(
                text="⏳ Đang tải Bảng điều khiển...",
                parse_mode="HTML",
                reply_markup=TelegramKeyboards.get_main_menu(),
            )
            await self._dispatcher.publish_request_event(
                EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA,
                "dashboard",
                query.message.message_id,
            )
            return

        menu_handlers = {
            "analytics": (
                MessageTemplates.get_analytics_dashboard({}),
                TelegramKeyboards.get_analytics_menu(),
            ),
            "trading": ("⚡ TRADING MENU", TelegramKeyboards.get_trading_menu()),
            "system": ("📰 SYSTEM MENU", TelegramKeyboards.get_system_menu()),
            "history": ("📜 HISTORY MENU", TelegramKeyboards.get_history_menu()),
            "settings": ("⚙️ SETTINGS MENU", TelegramKeyboards.get_settings_menu()),
            "control": (
                MessageTemplates.get_control_menu_msg(),
                TelegramKeyboards.get_control_menu(),
            ),
        }

        if submenu in menu_handlers:
            text, keyboard = menu_handlers[submenu]
            if isinstance(text, str) and "MENU" in text:
                text = MessageTemplates.format_title(text)
            try:
                await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as e:
                if "Message is not modified" not in str(e):
                    logger.error(f"Error editing menu message: {e}")
                    try:
                        await query.answer()
                        await query.message.reply_text(
                            "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                        )
                    except Exception:
                        pass

    # ================ EVENT HANDLERS (FROM SYSTEM) ================

    async def _on_health_data_response(self, event: Event) -> None:
        """Update UI with system health data."""
        data = event.data
        text = self._renderer.render_health_data(data)
        message_id = data.get("message_id")
        action = data.get("action", "health")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_detail_keyboard(f"system:{action}", "system"),
        )

    async def _on_trading_data_response(self, event: Event) -> None:
        """Update UI with trading data (positions/orders)."""
        data = event.data
        text = self._renderer.render_trading_data(data)
        message_id = data.get("message_id")
        action = data.get("action", "open_positions")
        if action == "open_positions":
            reply_markup = TelegramKeyboards.get_open_positions_keyboard(data.get("positions", []))
        else:
            reply_markup = TelegramKeyboards.get_detail_keyboard(f"trading:{action}", "trading")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    async def _on_exchange_status_response(self, event: Event) -> None:
        """Update UI with exchange status data."""
        data = event.data
        text = self._renderer.render_exchange_status(data)
        message_id = data.get("message_id")
        action = data.get("action", "exchange_status")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_detail_keyboard(f"system:{action}", "system"),
        )

    async def _on_system_data_response(self, event: Event) -> None:
        """Update UI with system data (dashboard)."""
        data = event.data
        text = self._renderer.render_system_data(data)
        message_id = data.get("message_id")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_main_menu(),
        )

    async def _on_analytics_data_response(self, event: Event) -> None:
        """Update UI with analytics data."""
        data = event.data
        text = self._renderer.render_analytics_data(data)
        message_id = data.get("message_id")
        action = data.get("action", "pnl_summary")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_detail_keyboard(f"analytics:{action}", "analytics"),
        )

    async def _on_history_data_response(self, event: Event) -> None:
        """Update UI with historical data."""
        data = event.data
        text = self._renderer.render_history_data(data)
        message_id = data.get("message_id")
        action = data.get("action", "trade_history")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_detail_keyboard(f"history:{action}", "history"),
        )

    async def _on_news_data_response(self, event: Event) -> None:
        """Update UI with news data."""
        data = event.data
        text = self._renderer.render_news_data(data)
        message_id = data.get("message_id")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_detail_keyboard("system:news", "system"),
        )

    async def _on_settings_data_response(self, event: Event) -> None:
        """Update UI with settings data."""
        data = event.data
        text = self._renderer.render_settings_data(data)
        message_id = data.get("message_id")
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_settings_menu(),
        )



    def _resolve_close_future(self, correlation_id: Optional[str], result: dict) -> bool:
        """Resolve a pending Telegram close future; returns True if a future was handled."""
        if not correlation_id:
            return False
        future = self._position_close_futures.get(correlation_id)
        if future is None:
            return False
        if future.done():
            return True
        future.set_result(result)
        return True

    async def _on_position_close_success(self, event: Event) -> None:
        """Handle successful position close event."""
        data = event.data if isinstance(event.data, dict) else {}
        correlation_id = event.correlation_id or data.get("correlation_id")
        result = dict(data)
        result.setdefault("success", True)

        if self._resolve_close_future(correlation_id, result):
            return

        pos_id = data.get("posId") or data.get("position_id")
        if pos_id and self._resolve_close_future(pos_id, result):
            return

        try:
            text = MessageTemplates.get_position_close_success_notification(data)
            await self._dispatcher.send_or_edit_message(text=text)
        except Exception as e:
            logger.warning(f"Failed to send position close success notification: {e}")

    async def _on_position_close_failure(self, event: Event) -> None:
        """Handle failed position close event."""
        data = event.data if isinstance(event.data, dict) else {}
        correlation_id = event.correlation_id or data.get("correlation_id")
        result = dict(data)
        result.setdefault("success", False)
        result.setdefault("reason", "Unknown error")

        if self._resolve_close_future(correlation_id, result):
            return

        pos_id = data.get("posId") or data.get("position_id")
        if pos_id and self._resolve_close_future(pos_id, result):
            return

        try:
            text = MessageTemplates.get_position_close_failure_notification(data)
            await self._dispatcher.send_or_edit_message(text=text)
        except Exception as e:
            logger.warning(f"Failed to send position close failure notification: {e}")

    async def _on_emergency_stop_complete(self, event: Event) -> None:
        """Handle emergency stop completion."""
        data = event.data
        text = self._renderer.render_emergency_stop_complete(data)
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=data.get("message_id"),
            reply_markup=TelegramKeyboards.get_main_menu(),
        )

    async def _on_reset_signals_complete(self, event: Event) -> None:
        """Handle reset signals completion."""
        data = event.data
        text = self._renderer.render_reset_signals_complete(data)
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=data.get("message_id"),
            reply_markup=TelegramKeyboards.get_main_menu(),
        )

    async def _on_clean_bot_complete(self, event: Event) -> None:
        """Handle clean bot completion."""
        data = event.data
        text = self._renderer.render_clean_bot_complete(data)
        await self._dispatcher.send_or_edit_message(
            text=text,
            message_id=data.get("message_id"),
            reply_markup=TelegramKeyboards.get_main_menu(),
        )

    # ================ HEARTBEAT & CLEANUP ================
    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat to event bus."""
        while self._running:
            self._last_heartbeat = datetime.now(timezone.utc)
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_HEARTBEAT,
                    data={"timestamp": self._last_heartbeat.isoformat()},
                )
            )
            await asyncio.sleep(30)

    # ================ DANGEROUS ACTION HANDLERS ================
    async def _handle_control_callback(self, query, action: str) -> None:
        """Handle dangerous control actions."""
        try:
            if action == "emergency_stop":
                await query.edit_message_text(
                    text=MessageTemplates.get_confirmation_msg("emergency_stop"),
                    parse_mode="HTML",
                    reply_markup=TelegramKeyboards.get_confirmation_keyboard("emergency_stop"),
                )
            elif action == "reset_signals":
                await query.edit_message_text(
                    text=MessageTemplates.get_confirmation_msg("reset_signals"),
                    parse_mode="HTML",
                    reply_markup=TelegramKeyboards.get_confirmation_keyboard("reset_signals"),
                )
            elif action == "clean_bot":
                await query.edit_message_text(
                    text=MessageTemplates.get_confirmation_msg("clean_bot"),
                    parse_mode="HTML",
                    reply_markup=TelegramKeyboards.get_confirmation_keyboard("clean_bot"),
                )
            elif action == "restart_engine":
                await query.edit_message_text(
                    text=MessageTemplates.get_confirmation_msg("restart_engine"),
                    parse_mode="HTML",
                    reply_markup=TelegramKeyboards.get_confirmation_keyboard("restart_engine"),
                )
            elif action == "start_bot":
                await query.edit_message_text(
                    text="▶️ Đang khởi động bot...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_START_BOT,
                    "start_bot",
                    query.message.message_id,
                )
            elif action == "pause_bot":
                await query.edit_message_text(
                    text="⏸️ Đang tạm dừng bot...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_PAUSE_BOT,
                    "pause_bot",
                    query.message.message_id,
                )
            else:
                await query.edit_message_text(
                    text=f"Unknown control action: {action}",
                    reply_markup=TelegramKeyboards.get_main_menu(),
                )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in control callback '{action}': {e}")
                try:
                    await query.answer()
                    await query.message.reply_text(
                        "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                    )
                except Exception:
                    pass

    async def _handle_confirm_callback(self, query, action: str) -> None:
        """Handle confirmation of dangerous actions."""
        try:
            if self._is_on_cooldown(action):
                await query.edit_message_text(
                    text=f"⏳ Action `{action}` is on cooldown. Please wait.",
                    reply_markup=TelegramKeyboards.get_main_menu(),
                )
                return

            self._set_cooldown(action)

            if action == "emergency_stop":
                await query.edit_message_text(
                    text="🚨 Đang thực hiện dừng khẩn cấp...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_EMERGENCY_STOP,
                    "emergency_stop",
                    query.message.message_id,
                )
            elif action == "reset_signals":
                await query.edit_message_text(
                    text="🔄 Đang reset tín hiệu...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_RESET_SIGNALS,
                    "reset_signals",
                    query.message.message_id,
                )
            elif action == "clean_bot":
                await query.edit_message_text(
                    text="🔄 Đang reset toàn diện...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_CLEAN_BOT,
                    "clean_bot",
                    query.message.message_id,
                )
            elif action == "restart_engine":
                await query.edit_message_text(
                    text="🔄 Đang khởi động lại engine...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.CONTROL_START_BOT,
                    "restart_engine",
                    query.message.message_id,
                )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in confirm callback '{action}': {e}")
                try:
                    await query.answer()
                    await query.message.reply_text(
                        "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                    )
                except Exception:
                    pass

    # ================ POSITION MANAGEMENT HANDLERS ================
    async def _handle_position_callback(self, query, data: str) -> None:
        """Handle position-specific actions."""
        parts = data.split(":")
        if len(parts) < 3:
            return

        action_name = parts[1]
        pos_id = parts[2]
        action_map = {
            "close": PositionAction.CLOSE_FULL,
            "close_full": PositionAction.CLOSE_FULL,
            "close_half": PositionAction.CLOSE_HALF,
        }
        if action_name in action_map:
            await self._handle_position_close_request(query, pos_id, action_map[action_name])

    async def _handle_position_close_request(
        self, query, pos_id: str, action: PositionAction
    ) -> None:
        """Initiate a position close request."""
        # [FIX LỖI 5] Debounce check - từ chối nếu cùng một action được gọi quá nhanh
        action_key = f"{pos_id}_{action.value}"
        current_time = time.time()
        if action_key in self._position_action_timestamps:
            elapsed = current_time - self._position_action_timestamps[action_key]
            if elapsed < self._DEBOUNCE_SECONDS:
                logger.warning(
                    f"[DEBOUNCE] Ignoring spam-click for {action_key} "
                    f"(elapsed {elapsed:.2f}s < {self._DEBOUNCE_SECONDS}s)"
                )
                await query.answer(
                    text="⏳ Vui lòng đợi... Yêu cầu trước đang được xử lý.",
                    show_alert=True
                )
                return
        
        lock = self._get_position_lock(pos_id)
        if lock.locked():
            await query.answer(text=_LAYER1_LOCK_MSG, show_alert=True)
            return

        # Update timestamp trước khi thực hiện action
        self._position_action_timestamps[action_key] = current_time

        async with lock:
            token = CallbackTokenStore.generate(pos_id, action)
            await query.edit_message_text(
                text=MessageTemplates.get_position_close_confirmation(pos_id, action),
                parse_mode="HTML",
                reply_markup=TelegramKeyboards.get_position_close_confirmation_keyboard(token),
            )

    async def _handle_position_confirm(self, query, token_meta: Dict) -> None:
        """Handle confirmed position actions from tokens."""
        position_id = token_meta.get("position_id") or token_meta.get("posId")
        action = token_meta.get("action")
        if token_meta.get("action") == "close_position":
            position_id = token_meta.get("posId")
            action = PositionAction.CLOSE_FULL

        if not position_id:
            return

        if isinstance(action, str) and action in ("close_half", "close_full"):
            action = PositionAction(action)

        # [FIX LỖI 5] Debounce check - từ chối nếu cùng một action được gọi quá nhanh
        action_key = f"{position_id}_{action.value if hasattr(action, 'value') else action}"
        current_time = time.time()
        if action_key in self._position_action_timestamps:
            elapsed = current_time - self._position_action_timestamps[action_key]
            if elapsed < self._DEBOUNCE_SECONDS:
                logger.warning(
                    f"[DEBOUNCE] Ignoring spam-click confirm for {action_key} "
                    f"(elapsed {elapsed:.2f}s < {self._DEBOUNCE_SECONDS}s)"
                )
                await query.answer(
                    text="⏳ Vui lòng đợi... Yêu cầu trước đang được xử lý.",
                    show_alert=True
                )
                return
        
        lock = self._get_position_lock(position_id)
        if lock.locked():
            await query.answer(text=_LAYER1_LOCK_MSG, show_alert=True)
            return

        # Update timestamp trước khi thực hiện action
        self._position_action_timestamps[action_key] = current_time

        correlation_id = str(uuid.uuid4())
        try:
            async with lock:
                await query.edit_message_text(
                    text=f"⏳ Đang đóng vị thế `{position_id}`...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )

                future = asyncio.get_running_loop().create_future()
                self._position_close_futures[correlation_id] = future

                requested_by = getattr(getattr(query, "from_user", None), "id", 0) or 0
                try:
                    await self.event_bus.publish(
                        Event(
                            event_type=EventTopic.POSITION_CLOSE_REQUEST,
                            data=PositionCloseRequest(
                                request_id=str(uuid.uuid4()),
                                correlation_id=correlation_id,
                                causation_id=str(uuid.uuid4()),
                                position_id=position_id,
                                action=action or PositionAction.CLOSE_FULL,
                                requested_by=int(requested_by),
                                timestamp=datetime.now(timezone.utc),
                            ),
                            source="telegram_ui",
                            correlation_id=correlation_id,
                        )
                    )

                    result = await asyncio.wait_for(future, timeout=30.0)
                    if isinstance(result, dict) and result.get("success") is False:
                        reason = result.get("reason", "Unknown error")
                        await query.edit_message_text(
                            text=f"❌ Thất bại đóng vị thế: {reason}",
                            reply_markup=TelegramKeyboards.get_main_menu(),
                        )
                    else:
                        size = result.get("size", "?") if isinstance(result, dict) else "?"
                        await query.edit_message_text(
                            text=f"✅ Đóng vị thế thành công ({size} contracts).",
                            reply_markup=TelegramKeyboards.get_main_menu(),
                        )
                except asyncio.TimeoutError:
                    await query.edit_message_text(
                        text=f"⚠️ Hết hạn chờ phản hồi từ engine (timeout) cho `{position_id}`.",
                        reply_markup=TelegramKeyboards.get_main_menu(),
                    )
                except Exception as e:
                    try:
                        await query.edit_message_text(
                            text=f"❌ Thất bại đóng vị thế: {e}",
                            reply_markup=TelegramKeyboards.get_main_menu(),
                        )
                    except Exception as edit_err:
                        logger.error(f"Failed to update close failure UI: {edit_err}")
                finally:
                    self._position_close_futures.pop(correlation_id, None)
        except Exception as e:
            logger.error(f"Position confirm handler error for {position_id}: {e}")
            try:
                await query.answer()
                await query.message.reply_text(
                    "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                )
            except Exception:
                pass

    async def _handle_position_token_callback(self, query, data: str) -> None:
        """Handle position close confirmation/cancellation via tokens."""
        is_confirm = data.startswith("pcf:")
        token = data[4:]

        token_meta = CallbackTokenStore.consume(token)
        if not token_meta:
            await query.answer("⚠️ This action has expired or is invalid.", show_alert=True)
            try:
                await query.edit_message_text(
                    "❌ Action expired.", reply_markup=TelegramKeyboards.get_main_menu()
                )
            except Exception:
                pass
            return

        if is_confirm:
            await self._handle_position_confirm(query, token_meta)
        else:  # Cancellation
            await query.edit_message_text(
                "❌ Request to close position has been cancelled.",
                reply_markup=TelegramKeyboards.get_main_menu(),
            )

    # ================ OTHER HANDLERS ================
    async def _handle_analytics_callback(self, query, action: str) -> None:
        """Request analytics data from the system."""
        await query.edit_message_text(
            text=f"⏳ Fetching analytics: `{action}`...",
            reply_markup=TelegramKeyboards.get_loading_keyboard(),
        )
        await self._dispatcher.publish_request_event(
            EventTopic.TELEGRAM_REQUEST_ANALYTICS_DATA,
            action,
            query.message.message_id,
        )

    async def _handle_trading_callback(self, query, action: str) -> None:
        """Request trading data from the system."""
        try:
            await query.answer()
            
            if action == "manual_order":
                await query.edit_message_text(
                    text=MessageTemplates.get_manual_order_instruction(),
                    parse_mode="HTML",
                    reply_markup=TelegramKeyboards.get_back_to_main_menu(),
                )
                return
            
            if action == "capital_management":
                # Temporary capital management dashboard
                await query.edit_message_text(
                    text="⏳ Đang tải thông tin Quản lý Vốn...",
                    reply_markup=TelegramKeyboards.get_loading_keyboard(),
                )
                await self._dispatcher.publish_request_event(
                    EventTopic.TELEGRAM_REQUEST_TRADING_DATA,
                    "capital_management",
                    query.message.message_id,
                )
                return

            await query.edit_message_text(
                text=f"⏳ Fetching trading data: `{action}`...",
                reply_markup=TelegramKeyboards.get_loading_keyboard(),
            )
            await self._dispatcher.publish_request_event(
                EventTopic.TELEGRAM_REQUEST_TRADING_DATA,
                action,
                query.message.message_id,
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in trading callback '{action}': {e}")
            try:
                await query.message.reply_text(
                    "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                )
            except Exception:
                pass

    async def _handle_system_callback(self, query, action: str) -> None:
        """Request system data from the system."""
        await query.edit_message_text(
            text=f"⏳ Fetching system data: `{action}`...",
            reply_markup=TelegramKeyboards.get_loading_keyboard(),
        )
        topic = (
            EventTopic.TELEGRAM_REQUEST_EXCHANGE_STATUS
            if action == "exchange_status"
            else EventTopic.TELEGRAM_REQUEST_NEWS_DATA
            if action == "news"
            else EventTopic.TELEGRAM_REQUEST_HEALTH_DATA
            if action == "health"
            else EventTopic.TELEGRAM_REQUEST_SYSTEM_DATA
        )
        await self._dispatcher.publish_request_event(topic, action, query.message.message_id)

    async def _handle_history_callback(self, query, action: str) -> None:
        """Request historical data from the system."""
        await query.edit_message_text(
            text=f"⏳ Fetching history: `{action}`...",
            reply_markup=TelegramKeyboards.get_loading_keyboard(),
        )
        await self._dispatcher.publish_request_event(
            EventTopic.TELEGRAM_REQUEST_HISTORY_DATA,
            action,
            query.message.message_id,
        )

    async def _handle_settings_callback(self, query, action: str) -> None:
        """Request settings data from the system."""
        try:
            # Special case for radar menu - show radar limit selection directly
            if action == "radar_menu":
                current_limit = self.settings.radar_limit
                await query.edit_message_text(
                    text=f"👁️ {MessageTemplates.format_title('TẦM QUÉT RADAR')}"
                    f"<i>Chọn số lượng coin muốn quét (Radar Limit):</i>\n\n"
                    f"📊 <b>Hiện tại:</b> Top {current_limit} coin\n"
                    f"🔢 <b>Tổng watchlist:</b> {len(self.settings.watchlist)} coin\n\n"
                    "💡 <i>Giảm số lượng để tăng tốc độ quét, tăng số lượng để có nhiều cơ hội hơn.</i>",
                    reply_markup=TelegramKeyboards.get_radar_limit_menu(current_limit),
                    parse_mode="HTML"
                )
                return
            
            await query.edit_message_text(
                text="⏳ Đang tải cài đặt...",
                reply_markup=TelegramKeyboards.get_loading_keyboard(),
            )
            await self._dispatcher.publish_request_event(
                EventTopic.TELEGRAM_REQUEST_SETTINGS_DATA,
                action,
                query.message.message_id,
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error in settings callback '{action}': {e}")
                try:
                    await query.answer()
                    await query.message.reply_text(
                        "❌ Hệ thống gặp sự cố tạm thời khi xử lý lệnh. Vui lòng thử lại hoặc kiểm tra Nhật ký!"
                    )
                except Exception:
                    pass

    async def _handle_radar_callback(self, query, action: str) -> None:
        """Handle radar watchlist limit adjustments (Top 5/10/15/20)."""
        try:
            new_limit = int(action)
        except ValueError:
            await query.edit_message_text("❌ Invalid radar limit.", reply_markup=TelegramKeyboards.get_main_menu())
            return

        # Use settings from module level instead of instance attribute
        from core.config.settings import settings
        settings.radar_limit = new_limit
        await query.edit_message_text(
            text=f"✅ Radar updated to Top `{new_limit}` coins",
            reply_markup=TelegramKeyboards.get_settings_menu(),
            parse_mode="HTML"
        )
        await self.event_bus.publish(
            Event(
                event_type=EventTopic.CONTROL_RADAR_LIMIT_CHANGED,
                data={"radar_limit": new_limit, "message_id": query.message.message_id},
            )
        )

    # ================ UTILITY METHODS ================
    def _get_position_lock(self, pos_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific position ID."""
        if pos_id not in self._position_action_locks:
            self._position_action_locks[pos_id] = asyncio.Lock()
        return self._position_action_locks[pos_id]

    def _is_on_cooldown(self, action: str) -> bool:
        """Check if a dangerous action is on cooldown."""
        cooldown_duration = self._COOLDOWN_SECONDS.get(action, 0)
        if action in self._action_cooldowns:
            elapsed = time.time() - self._action_cooldowns[action]
            if elapsed < cooldown_duration:
                return True
        return False

    def _set_cooldown(self, action: str) -> None:
        """Set the cooldown for a dangerous action."""
        self._action_cooldowns[action] = time.time()