"""
AccountContext for isolating locks, event routing, and metrics per trading tenant/account.
Utilizes contextvars for async concurrency-safe tenancy propagation.
"""

import asyncio
from contextvars import ContextVar
from typing import Dict, Optional
from loguru import logger

from core.metrics import MetricsAdapter, InMemoryMetricsAdapter

# Active account context variable
_active_account_context: ContextVar[str] = ContextVar("active_account_context", default="default_account")


class AccountContext:
    """
    Context manager to bind execution threads/coroutines to a specific trading account.
    Prevents cross-account contamination.
    """

    # Global isolated locks registry: account_id -> {lock_name -> Lock}
    _lock_registry: Dict[str, Dict[str, asyncio.Lock]] = {}
    _lock_registry_mutex = asyncio.Lock()

    # Global isolated metrics registry: account_id -> MetricsAdapter
    _metrics_registry: Dict[str, MetricsAdapter] = {}

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._token = None

    def __enter__(self):
        self._token = _active_account_context.set(self.account_id)
        logger.debug(f"Entered account context: {self.account_id}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token:
            _active_account_context.reset(self._token)
        logger.debug(f"Exited account context: {self.account_id}")

    @classmethod
    def get_current_account(cls) -> str:
        """Get the ID of the currently active account context."""
        return _active_account_context.get()

    @classmethod
    async def get_account_lock(cls, lock_name: str) -> asyncio.Lock:
        """Retrieve or create an isolated asyncio.Lock for the current account context."""
        account_id = cls.get_current_account()
        async with cls._lock_registry_mutex:
            if account_id not in cls._lock_registry:
                cls._lock_registry[account_id] = {}
            if lock_name not in cls._lock_registry[account_id]:
                cls._lock_registry[account_id][lock_name] = asyncio.Lock()
            return cls._lock_registry[account_id][lock_name]

    @classmethod
    def get_account_metrics(cls) -> MetricsAdapter:
        """Get or initialize the isolated metrics registry for the current account context."""
        account_id = cls.get_current_account()
        if account_id not in cls._metrics_registry:
            cls._metrics_registry[account_id] = InMemoryMetricsAdapter()
        return cls._metrics_registry[account_id]

    @classmethod
    def route_topic(cls, base_topic: str) -> str:
        """Format event routing topic with tenant-specific namespace prefix."""
        account_id = cls.get_current_account()
        return f"{account_id}.{base_topic}"
