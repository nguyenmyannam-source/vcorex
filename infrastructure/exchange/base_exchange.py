"""
Abstract base exchange class defining the interface for all exchange implementations.
This abstraction allows supporting multiple exchanges while maintaining a consistent API.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional


@dataclass
class OHLCV:
    """OHLCV candle data structure."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str
    timeframe: str
    confirmed: bool = True

    @classmethod
    def from_list(cls, data: List[Any], symbol: str, timeframe: str) -> "OHLCV":
        """Create OHLCV from OKX list [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]."""
        confirmed = True
        if len(data) > 8:
            confirmed = str(data[8]) in ("1", "true", "True")
        return cls(
            timestamp=int(data[0]),
            open=float(data[1]),
            high=float(data[2]),
            low=float(data[3]),
            close=float(data[4]),
            volume=float(data[5]),
            symbol=symbol,
            timeframe=timeframe,
            confirmed=confirmed,
        )


@dataclass
class Ticker:
    """Real-time ticker data."""

    symbol: str
    last_price: float
    bid: float
    ask: float
    volume_24h: float
    timestamp: int


@dataclass
class Balance:
    """Account balance information."""

    asset: str
    free: float
    used: float
    total: float


@dataclass
class Position:
    """Open position information."""

    position_id: str
    symbol: str
    side: str  # 'long' or 'short'
    amount: float  # For Futures, this is 'sz' (number of contracts)
    entry_price: float
    current_price: float
    unrealized_pnl: float
    leverage: int
    timestamp: int
    ct_val: float = 1.0  # Value of one contract (e.g., 0.01 for BTC)
    roe: float = 0.0
    margin: float = 0.0
    notional_size: float = 0.0
    tp_trigger_px: Optional[float] = None
    sl_trigger_px: Optional[float] = None
    amount_remaining: Optional[float] = None  # Defaults to abs(amount) in __post_init__

    def __post_init__(self) -> None:
        if self.amount_remaining is None:
            self.amount_remaining = abs(self.amount)


@dataclass
class Order:
    """Order information."""

    order_id: str
    client_order_id: str
    symbol: str
    side: str  # 'buy' or 'sell'
    type: str  # 'market', 'limit', etc.
    amount: float  # Target amount in COINS (for placement)
    price: Optional[float]
    filled_amount: float
    status: str
    timestamp: int
    contracts: Optional[float] = None  # Actual contracts placed (sz)
    position_side: Optional[str] = None  # 'long' or 'short' for hedge mode


@dataclass
class WebSocketMessage:
    """Generic WebSocket message wrapper."""

    channel: str
    symbol: str
    data: Dict[str, Any]
    timestamp: datetime


class BaseExchange(ABC):
    """
    Abstract base class for all exchange implementations.
    All exchanges must implement these methods to work with the trading system.
    """

    def __init__(self, api_key: str, api_secret: str, passphrase: str, demo_mode: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo_mode = demo_mode
        self._connected = False
        self._ws_connected = False

    @property
    def is_connected(self) -> bool:
        """Check if exchange is connected and authenticated."""
        return self._connected

    @property
    def is_ws_connected(self) -> bool:
        """Check if WebSocket connection is active."""
        return self._ws_connected

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize exchange connection and verify credentials."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Gracefully shutdown exchange connections."""
        pass

    @abstractmethod
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 100, since: Optional[int] = None
    ) -> List[OHLCV]:
        """
        Fetch OHLCV candle data for a symbol and timeframe.
        """
        pass

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        """
        Fetch current ticker information for a symbol.
        """
        pass

    @abstractmethod
    async def fetch_balance(self) -> Dict[str, Balance]:
        """
        Fetch account balances for all assets.
        """
        pass

    @abstractmethod
    async def fetch_positions(self) -> List[Position]:
        """
        Fetch all open positions.
        """
        pass

    @abstractmethod
    async def fetch_open_orders(self) -> List[Order]:
        """
        Fetch all open (pending) orders.
        """
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        position_side: Optional[str] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        correlation_id: Optional[str] = None,
    ) -> Order:
        """
        Place a new order on the exchange.
        """
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str, correlation_id: Optional[str] = None) -> bool:
        """
        Cancel an existing order.
        """
        pass

    @abstractmethod
    async def close_position(self, symbol: str, position_id: Optional[str] = None) -> Order:
        """
        Close an open position.
        """
        pass

    @abstractmethod
    async def fetch_trade_history(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Any]:
        """
        Fetch trade history (fills) for the account.
        """
        pass

    @abstractmethod
    def websocket_stream(
        self, channels: List[str], symbols: List[str]
    ) -> AsyncGenerator[WebSocketMessage, None]:
        """
        Subscribe to WebSocket streams and yield incoming messages.
        Implements auto-reconnection and heartbeat.
        """
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Set leverage for a symbol.
        """
        pass

    @abstractmethod
    async def get_rate_limit_remaining(self) -> int:
        """
        Get remaining API calls before hitting rate limits.
        """
        pass

    async def fetch_position(self, symbol: str) -> Optional[Position]:
        """
        Fetch a single open position by symbol.
        """
        positions = await self.fetch_positions()
        for pos in positions:
            if pos.symbol == symbol:
                return pos
        return None

    def normalize_position_size(self, symbol: str, size: float) -> float:
        """
        Normalize position size according to exchange specification lot size.
        """
        return size

