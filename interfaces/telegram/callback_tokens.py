"""Short-lived tokens for Telegram inline callbacks (64-byte limit)."""

import asyncio
import base64
import re
import time
import uuid
from typing import Dict, Optional

from loguru import logger

from core.events.payloads import PositionAction


class CallbackTokenStore:
    """
    Secure single-use token store for Telegram inline callbacks.
    Tokens expire in 120s.
    """

    _store: Dict[str, dict] = {}
    _cleanup_task: Optional[asyncio.Task] = None
    _rate_limit_tracker: Dict[str, list] = {}  # Track callback attempts per user
    _rate_limit_window = 60.0  # 60 seconds window
    _rate_limit_max = 10  # Max 10 callbacks per window

    @classmethod
    def _validate_token_format(cls, token: str) -> bool:
        """Validate token format: must be 12 chars, base64-like with - and _."""
        if not isinstance(token, str):
            logger.warning(f"Invalid token type: {type(token)}")
            return False
        if len(token) != 12:
            logger.warning(f"Invalid token length: {len(token)}")
            return False
        # Token should only contain alphanumeric, -, and _
        if not re.match(r'^[A-Za-z0-9_-]+$', token):
            logger.warning(f"Invalid token format: {token}")
            return False
        return True

    @classmethod
    def _sanitize_position_id(cls, position_id: str) -> str:
        """Sanitize position_id to prevent injection attacks."""
        if not isinstance(position_id, str):
            logger.warning(f"Invalid position_id type: {type(position_id)}")
            return ""
        # Remove any non-alphanumeric characters except - and _
        sanitized = re.sub(r'[^A-Za-z0-9_-]', '', position_id)
        if sanitized != position_id:
            logger.warning(f"Position ID sanitized: {position_id} -> {sanitized}")
        return sanitized

    @classmethod
    def _check_rate_limit(cls, user_id: str) -> bool:
        """Check if user has exceeded rate limit for callbacks."""
        now = time.time()
        if user_id not in cls._rate_limit_tracker:
            cls._rate_limit_tracker[user_id] = []
        
        # Remove old entries outside the time window
        cls._rate_limit_tracker[user_id] = [
            ts for ts in cls._rate_limit_tracker[user_id] 
            if now - ts < cls._rate_limit_window
        ]
        
        if len(cls._rate_limit_tracker[user_id]) >= cls._rate_limit_max:
            logger.warning(f"Rate limit exceeded for user {user_id}: {len(cls._rate_limit_tracker[user_id])} callbacks in {cls._rate_limit_window}s")
            return False
        
        cls._rate_limit_tracker[user_id].append(now)
        return True

    @classmethod
    def generate(cls, position_id: str, action: PositionAction) -> str:
        # Sanitize position_id before storing
        sanitized_position_id = cls._sanitize_position_id(position_id)
        if not sanitized_position_id:
            logger.error(f"Invalid position_id provided: {position_id}")
            raise ValueError("Invalid position_id format")
        
        token = (
            base64.b64encode(uuid.uuid4().bytes)
            .decode("utf-8")
            .rstrip("=")
            .replace("+", "-")
            .replace("/", "_")[:12]
        )
        cls._store[token] = {
            "position_id": sanitized_position_id,
            "action": action,
            "expires_at": time.time() + 120.0,
        }
        cls.start_cleanup_task()
        logger.info(f"Generated callback token for position {sanitized_position_id}, action {action}")
        return token

    @classmethod
    def get(cls, token: str, user_id: Optional[str] = None) -> Optional[dict]:
        # Validate token format before lookup
        if not cls._validate_token_format(token):
            logger.warning(f"Invalid token format rejected: {token}")
            return None
        
        # Check rate limit if user_id provided
        if user_id and not cls._check_rate_limit(user_id):
            logger.warning(f"Rate limit exceeded for user {user_id}")
            return None
        
        if token not in cls._store:
            logger.debug(f"Token not found: {token}")
            return None
        
        meta = cls._store[token]
        if time.time() > meta["expires_at"]:
            cls._store.pop(token, None)
            logger.info(f"Expired token consumed: {token}")
            return None
        
        logger.info(f"Token validated successfully: {token} for position {meta['position_id']}")
        return meta

    @classmethod
    def consume(cls, token: str, user_id: Optional[str] = None) -> Optional[dict]:
        meta = cls.get(token, user_id)
        if meta:
            cls._store.pop(token, None)
            logger.info(f"Token consumed: {token} for position {meta['position_id']}, action {meta['action']}")
        return meta

    @classmethod
    def cleanup(cls) -> None:
        now = time.time()
        expired = [k for k, v in cls._store.items() if now > v["expires_at"]]
        for k in expired:
            cls._store.pop(k, None)
        if expired:
            logger.info(f"CallbackTokenStore: cleaned up {len(expired)} expired tokens")

    @classmethod
    def start_cleanup_task(cls) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if cls._cleanup_task is None or cls._cleanup_task.done():
            cls._cleanup_task = loop.create_task(cls._cleanup_loop())

    @classmethod
    async def _cleanup_loop(cls) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                cls.cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CallbackTokenStore background cleanup error: {e}")
