"""
Shared pytest fixtures for the VCOREX test suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config.settings import Settings
from core.event_bus import EventBus


@pytest.fixture
def event_bus():
    """Fresh in-process event bus (not started — tests start/stop as needed)."""
    return EventBus()


@pytest.fixture
def mock_exchange():
    """Mock OKX exchange with async methods used across unit tests."""
    exchange = MagicMock()
    exchange.fetch_balance = AsyncMock(return_value={})
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.fetch_position = AsyncMock(return_value=None)
    exchange.fetch_ticker = AsyncMock()
    exchange.normalize_position_size = MagicMock(side_effect=lambda sym, sz: sz)
    exchange.cancel_algo_orders = AsyncMock(return_value=True)
    exchange._circuit_broken = False
    exchange._markets = {
        "BTC-USDT-SWAP": {"minSz": 0.01, "lotSz": 0.01},
        "ETH-USDT-SWAP": {"minSz": 0.01, "lotSz": 0.01},
    }
    return exchange


@pytest.fixture
def mock_session_factory():
    """Async SQLAlchemy session factory mock."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock()
    factory.return_value = session
    return factory


@pytest.fixture
def test_settings():
    """Settings configured for OKX demo mode tests."""
    s = Settings()
    s.okx_demo_mode = True
    s.production_risk_mode = False
    s.watchlist = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    s.default_leverage = 10
    s.telegram_admin_ids = ["123456789"]
    return s


@pytest.fixture
def test_settings_cb(test_settings):
    """Settings with short circuit-breaker cooldown for CB transition tests."""
    test_settings.position_cb_threshold = 3
    test_settings.position_cb_cooldown = 0.1
    test_settings.cb_threshold = 3
    test_settings.cb_cooldown_seconds = 0.1
    return test_settings
