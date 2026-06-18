"""Message dispatcher for Telegram message operations and event publishing.

This module handles:
- Sending new messages to Telegram chat
- Editing existing Telegram messages
- Publishing request/control events to event bus
- Rate limit handling (flood control)
- Error handling and logging
"""

from typing import Any, Optional

from loguru import logger
from telegram import Bot
from telegram.error import BadRequest, RetryAfter, TelegramError

from core.event_bus_components import Event
from core.event_bus import EventBus
from core.events.topics import EventTopic
from interfaces.telegram.keyboards import TelegramKeyboards
from interfaces.telegram.rate_limiter import RateLimiter


class MessageDispatcher:
    """Handles message updates (edit/send) and event publishing."""

    def __init__(
        self, bot: Optional[Bot], chat_id: int, event_bus: EventBus, rate_limiter: RateLimiter
    ):
        self._bot = bot
        self._chat_id = chat_id
        self.event_bus = event_bus
        self.rate_limiter = rate_limiter

    async def send_or_edit_message(
        self,
        text: str,
        message_id: Optional[int] = None,
        parse_mode: str = "HTML",
        reply_markup: Any = None,
        **kwargs,
    ) -> None:
        """Send new message or edit existing one."""
        if not self._bot:
            logger.warning("Telegram bot instance is not initialized; skipping send/edit action.")
            return
        
        # Validate text content
        if not text or not text.strip():
            logger.warning("Cannot send/edit message with empty text. Skipping operation.")
            return

        try:
            if message_id:
                # Edit existing message
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    **kwargs,
                )
            else:
                # Send new message
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    **kwargs,
                )
        except RetryAfter as e:
            # Handle Telegram flood control
            retry_seconds = getattr(e, "retry_after", 30)
            self.rate_limiter.apply_backoff(retry_seconds)
            logger.warning("Telegram flood control: retry in {} seconds", retry_seconds)
        except BadRequest as e:
            error_msg = str(e)
            if "Message is not modified" in error_msg:
                # Harmless: editing with same content
                return

            # Handle overly long messages by truncating and retrying
            if "message too long" in error_msg.lower() or "message is too long" in error_msg.lower() or "message_too_long" in error_msg.lower():
                MAX_LEN = 4000
                truncated = text[:MAX_LEN] + "\n\n...[truncated]"
                logger.warning("Telegram message too long (len={}), truncating to {} chars and retrying...", len(text), MAX_LEN)
                try:
                    # Try without parse_mode first for truncated messages
                    if message_id:
                        await self._bot.edit_message_text(
                            chat_id=self._chat_id,
                            message_id=message_id,
                            text=truncated,
                            parse_mode=None,  # Disable parse_mode for truncated messages
                            reply_markup=reply_markup,
                            **kwargs,
                        )
                    else:
                        await self._bot.send_message(
                            chat_id=self._chat_id,
                            text=truncated,
                            parse_mode=None,  # Disable parse_mode for truncated messages
                            reply_markup=reply_markup,
                            **kwargs,
                        )
                    return
                except Exception:
                    logger.exception("Retry after truncation failed")

            # If HTML parsing failed, retry with plain text (strip all formatting)
            if "parse entities" in error_msg.lower() or "unsupported start tag" in error_msg.lower() or "Can't parse" in error_msg:
                logger.warning(
                    "Telegram HTML parse error (offset ~{}). Retrying as plain text. Original error: {}",
                    error_msg, e
                )
                try:
                    # Strip common HTML tags for plain text fallback
                    import re
                    plain_text = re.sub(r"<[^>]+>", "", text)
                    if message_id:
                        await self._bot.edit_message_text(
                            chat_id=self._chat_id,
                            message_id=message_id,
                            text=plain_text,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
                    else:
                        await self._bot.send_message(
                            chat_id=self._chat_id,
                            text=plain_text,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
                    return
                except Exception as fallback_err:
                    logger.error("Plain text fallback also failed: {}", fallback_err)
                return

            if "button_data_invalid" in error_msg.lower():
                logger.warning(
                    "Telegram invalid keyboard callback_data; retrying without reply_markup. Error: {}",
                    e,
                )
                try:
                    if message_id:
                        await self._bot.edit_message_text(
                            chat_id=self._chat_id,
                            message_id=message_id,
                            text=text,
                            parse_mode=parse_mode,
                        )
                    else:
                        await self._bot.send_message(
                            chat_id=self._chat_id,
                            text=text,
                            parse_mode=parse_mode,
                            **kwargs,
                        )
                    return
                except Exception as fallback_err:
                    logger.error("Retry without keyboard failed: {}", fallback_err)

            if "no text in the message to edit" in error_msg.lower() or "there is no text" in error_msg.lower():
                logger.warning(
                    "Telegram cannot edit message with no text. This is likely a data issue. Skipping edit. Error: {}",
                    e,
                )
                return

            logger.error("Telegram BadRequest error: {}", e, exc_info=True)
            logger.debug(
                "Message content length: {}, message_id: {}, parse_mode: {}",
                len(text),
                message_id,
                parse_mode,
            )
        except TelegramError as e:
            logger.error("Telegram API error: {}", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error sending/editing message: {}", e, exc_info=True)
            logger.debug(f"Failed message content length: {len(text)}, chat_id: {self._chat_id}")

    async def publish_request_event(
        self, event_type: EventTopic, action: str, message_id: Optional[int] = None, **kwargs
    ) -> None:
        """Publish a request event to the event bus."""
        import uuid
        correlation_id = kwargs.pop("correlation_id", str(uuid.uuid4()))
        causation_id = kwargs.pop("causation_id", str(uuid.uuid4()))
        parent_request_id = kwargs.pop("parent_request_id", str(message_id) if message_id else None)

        data: dict[str, Any] = {
            "action": action,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "parent_request_id": parent_request_id
        }
        if message_id:
            data["message_id"] = message_id
        data.update(kwargs)

        await self.event_bus.publish(
            Event(
                event_type=event_type,
                data=data,
                source="telegram_bot",
                correlation_id=correlation_id,
                causation_id=causation_id,
                parent_request_id=parent_request_id
            )
        )

    async def publish_control_event(self, event_type: EventTopic, **kwargs) -> None:
        """Publish a control event (start/stop/emergency)."""
        import uuid
        correlation_id = kwargs.pop("correlation_id", str(uuid.uuid4()))
        causation_id = kwargs.pop("causation_id", str(uuid.uuid4()))
        parent_request_id = kwargs.pop("parent_request_id", None)

        kwargs.update({
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "parent_request_id": parent_request_id
        })

        await self.event_bus.publish(
            Event(
                event_type=event_type,
                data=kwargs,
                source="telegram_bot",
                correlation_id=correlation_id,
                causation_id=causation_id,
                parent_request_id=parent_request_id
            )
        )

    async def show_loading(
        self, message_id: Optional[int] = None, text: str = "⏳ Loading..."
    ) -> None:
        """Show loading indicator."""
        await self.send_or_edit_message(
            text=text,
            message_id=message_id,
            reply_markup=TelegramKeyboards.get_loading_keyboard(),
        )