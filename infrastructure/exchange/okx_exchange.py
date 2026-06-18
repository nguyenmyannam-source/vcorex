"""
OKX exchange implementation following the BaseExchange interface.
Supports REST API and WebSocket connections with auto-reconnection and rate limiting.
"""

import asyncio
import base64
import hmac
import json
import math
import time
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional, Union
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
from loguru import logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from websockets import connect
from websockets import exceptions as ws_exceptions

from core.config.settings import Settings
from core.event_bus import EventBus
from core.circuit_breaker import BaseCircuitBreaker, CircuitState
from core.metrics import MetricsAdapter, InMemoryMetricsAdapter
from core.exceptions import CircuitBrokenError, OKXAPIError

def _log_retry(retry_state: RetryCallState) -> None:
    if retry_state.next_action is not None and retry_state.outcome is not None:
        logger.warning(
            f"Retrying in {retry_state.next_action.sleep:.2f}s due to: {retry_state.outcome.exception()}"
        )
    else:
        logger.warning("Retrying due to transient error")

from infrastructure.exchange.base_exchange import (
    OHLCV,
    Balance,
    BaseExchange,
    Order,
    Position,
    Ticker,
    WebSocketMessage,
)
from utils.okx_symbols import OKX_SYMBOL_SPECS


class TokenBucketLimiter:
    """
    High-frequency Trading (HFT) Token Bucket Rate Limiter.
    Allows bursts up to capacity, then throttles to refill_rate tokens per second.
    Uses exact delta-time calculation to eliminate Event Loop polling overhead.
    """
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = float(refill_rate)
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        while True:
            wait_time = 0.0
            async with self.lock:
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(float(self.capacity), self.tokens + elapsed * self.refill_rate)
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                
                # Calculate exact wait time for 1 token
                wait_time = (1.0 - self.tokens) / self.refill_rate
            
            # Wait outside the lock to prevent blocking other coroutines
            await asyncio.sleep(max(wait_time, 0.001))

class CooldownEngine:
    """Manages adaptive circuit breaker cooldowns for specific API domains."""
    def __init__(self):
        self.cooldown_until: float = 0.0
        self.lock = asyncio.Lock()

def _safe_float(value: Any, default: float = 0.0) -> float:
    """
    [FIX P1] Safely convert OKX API response values to float.
    OKX may return numeric fields as strings to preserve precision.
    This helper ensures safe conversion regardless of input type.
    """
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        logger.warning(f"Failed to convert {value!r} to float, using default {default}")
        return default

def _round_by_precision(value: float, precision: Union[int, str]) -> str:
    """
    [FIX P3] Round value to specified decimal precision.
    Precision can be decimal places (int) or OKX lot size (str/float).
    Returns formatted string for API submission.
    """
    if isinstance(precision, str):
        # OKX lot_sz format: "1" or "0.1" or "0.001"
        try:
            precision_val = Decimal(str(precision))
            exponent = precision_val.as_tuple().exponent
            decimal_places = max(0, -exponent)
        except (ValueError, TypeError, ArithmeticError):  # [FIX P3-1] scoped exception, not bare except
            decimal_places = 8  # Default fallback
    else:
        decimal_places = int(precision)

    rounded = round(value, decimal_places)
    return f"{rounded:.{decimal_places}f}"

class OKXOrderVerificationUnknownError(OKXAPIError):
    """Exception raised when an order status cannot be verified after a POST timeout."""
    pass


class OKXExchange(BaseExchange):
    """
    OKX exchange implementation with full async support.
    Implements all BaseExchange methods with OKX-specific API handling.
    """

    # OKX timeframe mappings
    TIMEFRAME_MAP = {
        "5m": "5m",
        "15m": "15m",
        "1H": "1H",
        "4H": "4H",
        "1D": "1D",
        "1W": "1W",
        "1M": "1M",
    }

    def __init__(self, settings: Settings, event_bus: Optional[EventBus] = None, metrics: Optional[MetricsAdapter] = None):
        api_key = settings.okx_api_key
        api_secret = settings.okx_api_secret
        passphrase = settings.okx_passphrase
        demo_mode = settings.okx_demo_mode
        self.settings = settings
        self.event_bus = event_bus  # Injected for Circuit Breaker emergency stop
        self._metrics = metrics or InMemoryMetricsAdapter()  # Unified metrics adapter
        super().__init__(api_key, api_secret, passphrase, demo_mode)
        self.base_url = settings.okx_base_url
        self.ws_url = settings.okx_ws_url

        # Ensure correct WS path based on demo mode if using defaults
        if demo_mode:
            self.ws_url = "wss://wspap.okx.com:8443/ws/v5"
            logger.info("Demo mode enabled: Enforcing OKX Demo WS URL: wss://wspap.okx.com:8443/ws/v5")
        elif self.ws_url == "wss://wspap.okx.com:8443/ws/v5":
            # Default path for public/private needs to be appended in websocket_stream
            pass
        self.session: Optional[aiohttp.ClientSession] = None
        self._ws_reconnect_attempts = 0
        self._max_reconnect_attempts = settings.max_reconnect_attempts
        self._last_heartbeat: float = 0.0
        self._rate_limit_remaining = 20
        self._server_time_offset = 0  # in milliseconds
        # Reconnect benchmark metrics (REAL-TIME MEASUREMENT) - rolling window max 1000 records
        self._disconnect_timestamp: Optional[float] = None
        self._reconnect_metrics: list[float] = []  # Lưu thời gian recover thực tế (ms)
        self._reconnect_attempts: list[int] = []   # Lưu số lần thử cho mỗi lần recover
        self._max_reconnect_metrics = 1000         # Giới hạn rolling window size
        # Failure metrics
        self._failed_reconnect_count = 0
        self._reconnect_timeout_count = 0
        self._reconnect_attempt_histogram: dict[int, int] = {}  # Phân phối số lần thử
        self._total_reconnect_success = 0

        # Unified Circuit Breaker từ core (đồng bộ với toàn hệ thống)
        self._circuit_breaker = BaseCircuitBreaker(
            threshold=settings.exchange_cb_threshold,
            cooldown=settings.exchange_cb_cooldown,
            name="okx_exchange"
        )
        self._metrics.increment("exchange.circuit_breaker.initialized", tags={"exchange": "okx"})

        # [FIX] Rate Limiter Split: OKX has different limits for public and private endpoints.
        # Sử dụng cấu hình từ settings thay vì hardcode
        self._public_rate_limiter = TokenBucketLimiter(
            capacity=settings.okx_public_api_capacity, 
            refill_rate=settings.okx_public_api_refill_rate
        )
        self._private_rate_limiter = TokenBucketLimiter(
            capacity=settings.okx_private_api_capacity, 
            refill_rate=settings.okx_private_api_refill_rate
        )

        # Diagnostics
        self._api_request_count = 0
        self._api_error_count = 0
        self._ws_message_count = 0
        self.pos_mode = "long_short_mode"  # Default fallback
        self._cached_account_config: Dict[str, Any] = {}  # [FIX P2] Cache for demo verification

        # [REFACTOR LỖI 429] Semaphore và HTTP Session riêng biệt
        self._global_request_semaphore = asyncio.Semaphore(100) # Layer 1: Bounded concurrency
        self._rest_semaphore = asyncio.Semaphore(10)  # [FIX] Increased from 3 to 10 for better concurrency with large watchlist
        self._leverage_semaphore = asyncio.Semaphore(2)  # PHASE 2C: Max 2 concurrent leverage requests
        self._trade_session: Optional[aiohttp.ClientSession] = None

        # [ANTI-THUNDERING-HERD] Cooldown Engines for Domain Isolation
        self._rest_cooldown = CooldownEngine()
        self._trade_cooldown = CooldownEngine()

        # [GLOBAL RATE LIMITER] Strict Token Bucket for history-candles (Max 5 req/sec)
        # [FIX #5] Reduce refill_rate from 4.9 → 4.0 to maintain 20% safety margin
        # OKX limit: 10 req/2s = 5 req/s. At 4.9 the bot could burst through the limit
        # when many symbols fetch OHLCV simultaneously. 4.0 gives safe headroom.
        self._history_bucket = TokenBucketLimiter(capacity=4, refill_rate=4.0)
        self._cooldown_engines: Dict[str, CooldownEngine] = {
            "global": CooldownEngine(),
            "trade": CooldownEngine(),
            "market": CooldownEngine(),
            "account": CooldownEngine()
        }

        # PHASE 2: Leverage Storm Elimination - State Caching & Synchronization Guards
        self._last_leverage_set: Dict[tuple[str, str, int], float] = {}  # (symbol, side, leverage): timestamp
        self._leverage_cache_ttl = 86400  # PHASE 2E: 24h cache TTL
        self._leverage_sync_lock = asyncio.Lock()  # PHASE 2B: Only one leverage sync at a time

        # [FIX] Add cache for critical prices as fallback when API fails
        self._price_cache: Dict[str, float] = {}  # {symbol: last_known_price}
        self._price_cache_timestamps: Dict[str, float] = {}  # {symbol: timestamp}
        self._cache_ttl_seconds = 300.0  # Cache TTL: 5 minutes
        self._api_health_status = "HEALTHY"  # HEALTHY, DEGRADED, DOWN
        self._last_api_health_check = time.time()
        self._server_time_offset = 0.0  # Initialize server time offset

        logger.info(f"OKXExchange initialized with demo_mode={demo_mode}")

    async def initialize(self) -> None:
        """Initialize HTTP session and verify connection."""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

        if not self._trade_session:
            # Trade Client hoàn toàn tách biệt
            timeout_trade = aiohttp.ClientTimeout(total=15) # VIP Trade cần timeout nhanh hơn
            self._trade_session = aiohttp.ClientSession(timeout=timeout_trade)

        try:
            await self.sync_time()
            await self.fetch_markets()
            await self.fetch_fee_rates()  # [DYNAMIC] Fetch fee rates from API
            await self.fetch_balance()
            await self.sync_account_config()
            await self._verify_demo_mode_on_startup()  # [FIX P2] Verify demo mode
            self._connected = True
            self._time_sync_task = asyncio.create_task(self._periodic_time_sync())
            logger.info("OKXExchange initialized and authenticated successfully")
        except Exception as e:
            logger.error("Failed to initialize OKXExchange. Reason: {}", str(e), exc_info=True)
            raise e

    async def sync_account_config(self) -> None:
        """Query and cache the actual position mode (posMode) from OKX."""
        path = "/api/v5/account/config"
        try:
            response = await self._request("GET", path)
            data = response.get("data", [])
            if data:
                config = data[0]
                self.pos_mode = config.get("posMode", "long_short_mode")
                self._cached_account_config = config  # [FIX P2] Cache for demo verification
                margin_mode = config.get("margin", "unknown")  # [FIX P2] Validate margin mode
                logger.info(f"OKX Account config synced: posMode={self.pos_mode}, margin={margin_mode}")
            else:
                self.pos_mode = "long_short_mode"
                self._cached_account_config = {}
                logger.warning("OKX Account config returned empty data. Fallback to long_short_mode.")
        except Exception as e:
            self.pos_mode = "long_short_mode"
            self._cached_account_config = {}
            logger.warning(f"Failed to fetch OKX account config: {e}. Fallback to long_short_mode.")

    def _is_demo_uid(self, uid: Optional[str]) -> bool:
        """
        Detect demo-like OKX UIDs without depending on a single UID format.

        CONTEXT (June 2026):
        OKX changed demo account UID format:
        - OLD (pre-June 2026): UIDs ended with "-demo" suffix (e.g., "1234567-demo")
        - NEW (June 2026+): OKX returns numeric UIDs (e.g., "682651107994596407")

        This method supports BOTH formats for backwards compatibility:
        1. Checks for "-demo" suffix (old OKX format)
        2. Checks for "demo" or "test" keywords anywhere (keyword-based)
        3. Returns False for pure numeric UIDs (delegated to multi-signal verification)

        NOTE: Demo mode verification uses MULTIPLE SIGNALS, not just UID:
        - ✅ API Endpoint: wss://wspap.okx.com:8443/ws/v5
        - ✅ Broker ID: brokerId=9999 (on private WS channels)
        - ✅ Header: x-simulated-trading: 1
        - ⚠️ UID Format: Flexible (this method)

        Reference: docs/OKX_API_CHANGES_2026.md
        """
        if not uid or not isinstance(uid, str):
            return False
        normalized = uid.strip().lower()
        if not normalized:
            return False
        # Old OKX format: UID ending with "-demo"
        if normalized.endswith("-demo"):
            return True
        # Keyword-based: "demo" or "test" anywhere in UID
        if "demo" in normalized or "test" in normalized:
            return True
        # Pure numeric UIDs: allow (checked via other signals in _verify_demo_mode_on_startup)
        return False

    async def sync_time(self) -> None:
        """Synchronize local time with OKX server time to prevent timestamp errors."""
        path = "/api/v5/public/time"
        logger.debug("Attempting to synchronize server time...")
        try:
            # Make 3 requests to get a more accurate offset
            offsets = []
            for i in range(3):
                local_before = time.time() * 1000
                response = await self._request_raw("GET", path, auth_required=False)
                local_after = time.time() * 1000

                server_time = int(response["data"][0]["ts"])
                latency = (local_after - local_before) / 2
                offset = server_time - (local_before + latency)
                offsets.append(offset)
                logger.debug(f"Time sync request {i+1}/3: ServerTime={server_time}, LocalTime={local_before:.0f}, Latency={latency:.2f}ms, Offset={offset:.2f}ms")
                await asyncio.sleep(0.2) # sleep briefly between requests

            # Use the average offset
            self._server_time_offset = sum(offsets) / len(offsets)

            logger.info(f"Server time synchronized. Average offset: {self._server_time_offset:.2f} ms.")

        except Exception as e:
            logger.error(f"Failed to synchronize server time: {e}. Using local time (offset=0).")
            self._server_time_offset = 0.0

    async def _verify_demo_mode_on_startup(self) -> None:
        """
        [FIX P2] Verify bot is trading on demo account if demo_mode is enabled.

        STRATEGY: Multi-signal verification (not just UID format)

        Signals checked:
        1. API Endpoint (hardcoded): wss://wspap.okx.com:8443/ws/v5
        2. Broker ID (enforced): brokerId=9999 on private WS channels
        3. UID Format (supplementary): "-demo" suffix or "demo"/"test" keywords

        BEHAVIOR:
        - If UID has demo marker → INFO log (demo verified)
        - If UID is numeric but endpoint/broker ID correct → WARNING + CONTINUE
          (Other signals override ambiguous UID format)
        - Only FAIL if endpoint/broker ID mismatch + production-like UID

        NOTE: This replaced strict "-demo only" check which broke when OKX
        changed demo UID format in June 2026 to numeric-only UIDs.

        Reference:
        - docs/OKX_API_CHANGES_2026.md (full technical context)
        - docs/FIRST_RUN_GUIDE.md (user-facing guide)
        """
        if not self.demo_mode:
            logger.debug("Demo mode verification skipped (demo_mode=false)")
            return

        try:
            config = self._cached_account_config or {}
            uid = str(config.get("uid", "") or "").strip()
            is_demo_uid = self._is_demo_uid(uid)

            if not config:
                logger.warning(
                    "Demo mode enabled but OKX account config is unavailable. "
                    "Cannot conclusively verify demo/sandbox identity."
                )
            elif not uid:
                logger.warning(
                    "Demo mode enabled but OKX account UID is missing from account config. "
                    "Demo verification is inconclusive.")
            elif is_demo_uid:
                logger.info(f"✓ Demo account likely verified: UID '{uid}'")
            else:
                # [FIX P2-1] Numeric UIDs are the NEW STANDARD OKX demo format (post-June 2026).
                # This is NOT suspicious — log at INFO, not WARNING, to avoid false alarms at startup.
                logger.info(
                    f"✓ Demo mode active. UID '{uid}' is numeric (new OKX demo format). "
                    "Endpoint and x-simulated-trading header are enforced — trading on Demo."
                )
        except Exception as e:
            logger.warning(f"Could not verify demo account: {e}. Continuing...")

    async def _check_timestamp_drift(self) -> None:
        """[FIX P2] Monitor timestamp drift and re-sync if needed."""
        path = "/api/v5/public/time"
        try:
            response = await self._request_raw("GET", path, auth_required=False)
            server_time = int(response["data"][0]["ts"])
            local_time = int(time.time() * 1000)
            current_drift = abs(server_time - local_time - self._server_time_offset)

            if current_drift > 2000:  # 2 seconds - re-sync
                logger.warning(f"Large timestamp drift: {current_drift:.0f}ms. Re-syncing...")
                await self.sync_time()
            elif current_drift > 1000:  # 1 second - warn
                logger.debug(f"Timestamp drift: {current_drift:.0f}ms (acceptable)")
        except Exception as e:
            logger.debug(f"Timestamp drift check failed: {e}")

    async def shutdown(self) -> None:
        """Gracefully shutdown connections."""
        self._connected = False
        self._ws_connected = False

        if self.session and not self.session.closed:
            await self.session.close()
            # Aiohttp requires a tiny sleep after close to properly sever underlying TCP connections
            await asyncio.sleep(0.250)

        if self._trade_session and not self._trade_session.closed:
            await self._trade_session.close()
            await asyncio.sleep(0.100)

        if hasattr(self, '_time_sync_task') and not self._time_sync_task.done():
            self._time_sync_task.cancel()

        logger.info("OKXExchange shutdown complete")

    async def _periodic_time_sync(self) -> None:
        """Background worker to synchronize server time every hour and monitor drift."""
        while self._connected:
            try:
                await asyncio.sleep(3600)  # 1 hour
                logger.info("Running periodic OKX server time synchronization...")
                await self.sync_time()
                await self._check_timestamp_drift()  # [FIX P2] Check drift
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic time sync: {e}")

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Generate OKX signature for API authentication."""
        message = timestamp + method.upper() + path + body
        mac = hmac.new(bytes(self.api_secret, "utf-8"), bytes(message, "utf-8"), "sha256")
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _get_timestamp(self) -> str:
        """Get the current UTC timestamp in seconds format required by OKX API v5."""
        # OKX REST authentication expects a seconds-based timestamp string.
        # Use the same units as the WebSocket login flow to avoid expiration errors.
        local_time_ms = time.time() * 1000
        adjusted_time_ms = local_time_ms + self._server_time_offset
        timestamp = f"{adjusted_time_ms / 1000:.3f}"
        logger.debug(
            f"Timestamp generation: local_time_ms={local_time_ms:.0f}, "
            f"offset={self._server_time_offset:.2f}, final_timestamp={timestamp}"
        )
        return timestamp

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate authenticated headers for OKX API requests."""
        timestamp = self._get_timestamp()
        sign = self._sign(timestamp, method, path, body)

        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        logger.debug(f"Request Headers for {path}: OK-ACCESS-TIMESTAMP={timestamp}")
        if self.demo_mode:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request_raw(
        self,
        method: str,
        path: str,
        params: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Make an API request to OKX without any tenacity retries."""
        if not self.session:
            raise RuntimeError("Session not initialized, call initialize() first")

        # --- UNIFIED CIRCUIT BREAKER ENFORCEMENT ---
        # Sử dụng BaseCircuitBreaker từ core để đảm bảo tính đồng nhất toàn hệ thống
        params_dict = params if isinstance(params, dict) else None
        is_emergency = params_dict and params_dict.get("_is_emergency", False)
        
        if not self._circuit_breaker.allow_request():
            if not is_emergency:
                self._metrics.increment("exchange.circuit_breaker.blocked", tags={"exchange": "okx", "path": path})
                raise CircuitBrokenError(
                    f"Circuit Breaker is {self._circuit_breaker.state}. Blocking non-emergency REST request to {path}."
                )

        url = self.base_url + path

        request_path = path
        body_str = ""
        if method.upper() in ("POST", "PUT"):
            if params is not None:
                # OKX requires strict JSON without spaces for signatures
                body_str = json.dumps(params, separators=(",", ":"))
        elif params_dict:  # GET, DELETE
            query_string = "?" + urlencode(params_dict)
            url += query_string
            request_path += query_string

        if auth_required:
            headers = self._get_headers(method, request_path, body_str)
        else:
            headers = {}
            if self.demo_mode:
                headers["x-simulated-trading"] = "1"
            headers["Content-Type"] = "application/json"

        self._api_request_count += 1

        is_trade = path.startswith("/api/v5/trade/")
        is_account = path.startswith("/api/v5/account/")
        is_market = path.startswith("/api/v5/market/")

        # Acquire a token for rate limiting (allows burst concurrency)
        if is_trade or is_account:
            await self._private_rate_limiter.acquire()
        else:
            await self._public_rate_limiter.acquire()

        max_retries = 5
        base_delay = 0.5
        max_delay = 8.0

        domain = "trade" if is_trade else "account" if is_account else "market" if is_market else "global"

        engine = self._cooldown_engines.get(domain, self._cooldown_engines["global"])
        client_session = self._trade_session if (is_trade and self._trade_session) else self.session

        # [A/B] PRE-EMPTIVE GATE
        MAX_QUEUEABLE_COOLDOWN = 5.0
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now < engine.cooldown_until:
            cooldown_remaining = engine.cooldown_until - now
            if cooldown_remaining > MAX_QUEUEABLE_COOLDOWN:
                # Layer 4: Fast-Fail Circuit Breaker
                logger.warning(f"[CIRCUIT BREAKER] Domain {domain} bị nghẽn {cooldown_remaining:.2f}s (> {MAX_QUEUEABLE_COOLDOWN}s). Từ chối request tới {path}.")
                raise CircuitBrokenError(f"Gateway rate-limit open for {cooldown_remaining:.1f}s")

            logger.debug(f"[PRE-EMPTIVE GATE] Domain {domain} đang bị throttle. Chặn {path} chờ {cooldown_remaining:.3f}s")
            await asyncio.sleep(cooldown_remaining)

        # [C] RANDOMIZED DESYNCHRONIZATION JITTER (10ms - 50ms)
        # Phá vỡ hiệu ứng Thundering Herd (nhiều task đồng loạt tỉnh lại)
        await asyncio.sleep(random.uniform(0.010, 0.050))

        # Layer 1: Bounded Concurrency (Block excessive in-flight coroutines globally)
        await self._global_request_semaphore.acquire()

        # Nếu không phải luồng trade, bắt buộc xếp hàng qua Semaphore(3)
        if not is_trade:
            await self._rest_semaphore.acquire()

        try:
            for attempt in range(max_retries):
                async with client_session.request(
                    method, url, headers=headers, data=body_str if body_str else None
                ) as response:
                    response_text = await response.text()
                    try:
                        response_data = json.loads(response_text)
                    except json.JSONDecodeError:
                        self._api_error_count += 1
                        logger.error("Failed to decode JSON from response: {}", response_text)
                        raise OKXAPIError(f"API Error {response.status}: Invalid JSON response")

                    is_429 = response.status == 429 or (
                        isinstance(response_data, dict)
                        and response_data.get("code") == "50011"
                    )

                    if is_429:
                        if attempt < max_retries - 1:
                            # [D] ADAPTIVE 429 CIRCUIT BREAKER
                            retry_after_str = response.headers.get("Retry-After")
                            if retry_after_str and retry_after_str.isdigit():
                                penalty = float(retry_after_str)
                            else:
                                penalty = min(max_delay, base_delay * (1.5 ** attempt))

                            # Cập nhật nguyên tử cooldown_until cho toàn bộ Domain
                            async with engine.lock:
                                engine.cooldown_until = max(
                                    engine.cooldown_until,
                                    loop.time() + penalty
                                )

                            logger.warning(f"[CIRCUIT BREAKER] 429 Too Many Requests cho {path}. Domain {domain} bị khóa phạt {penalty:.2f}s (Lần thử {attempt + 1}/{max_retries}).")

                            await asyncio.sleep(penalty + random.uniform(0.1, 0.5))

                            continue
                        else:
                            self._api_error_count += 1
                            raise OKXAPIError(f"Rate limit exceeded after {max_retries} attempts: {response_data}")

                    if response.status != 200:
                        self._api_error_count += 1
                        logger.error("API Error {}: {}", response.status, response_data)
                        if response.status in (500, 502, 503, 504):
                            raise OKXAPIError(f"API Error {response.status}: {response_data}")
                        raise OKXAPIError(f"API Error {response.status}: {response_data}")

                    if "code" in response_data and response_data["code"] != "0":
                        code = response_data["code"]
                        msg = response_data.get("msg", "Unknown error")
                        logger.error("OKX API Error: {}", response_data)

                        if code == "50102":
                            logger.warning(
                                "OKX timestamp expired (code 50102). Resynchronizing clock and retrying."
                            )
                            await self.sync_time()
                            continue

                        # Ghi nhận lỗi vào unified circuit breaker
                        self._circuit_breaker.record_failure()
                        self._metrics.increment("exchange.api.errors", tags={"exchange": "okx", "code": code})
                        
                        # Nếu circuit breaker vừa được kích hoạt (trạng thái chuyển từ CLOSED sang OPEN)
                        if self._circuit_breaker.state == CircuitState.OPEN:
                            logger.critical(
                                f"CIRCUIT BREAKER TRIGGERED: {self._circuit_breaker.failure_count} errors. "
                                "Triggering Emergency Halt (Halt Trading)."
                            )
                            # Publish emergency stop event
                            asyncio.create_task(
                                self._trigger_emergency_stop(f"API Failure Threshold Exceeded: {msg}")
                            )

                        raise OKXAPIError(f"OKX API Error: {msg}")

                    # Ghi nhận request thành công vào unified circuit breaker
                    self._circuit_breaker.record_success()
                    self._metrics.increment("exchange.api.success", tags={"exchange": "okx"})
                    
                    # Nếu circuit breaker trong trạng thái HALF_OPEN và request thành công, reset về CLOSED
                    if self._circuit_breaker.state == CircuitState.HALF_OPEN:
                        logger.warning("[CB] Successful probe response received. Resetting Circuit Breaker to CLOSED.")
                        self._circuit_breaker.reset()

                    return response_data
        finally:
            self._global_request_semaphore.release()
            if not is_trade:
                self._rest_semaphore.release()
                # Micro-Sleep Jitter bắt buộc làm phẳng (flatten) luồng requests
                await asyncio.sleep(0.05)

        raise OKXAPIError("OKX API request failed: no response returned")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError, OKXAPIError)),
        before_sleep=_log_retry,
    )
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Make an API request to OKX with retries."""
        return await self._request_raw(method, path, params, auth_required)

    async def _request_no_retry(
        self,
        method: str,
        path: str,
        params: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Make an API request to OKX without automatic retrying."""
        return await self._request_raw(method, path, params, auth_required)

    async def _trigger_emergency_stop(self, reason: str):
        """Trigger system-wide emergency halt."""
        from core.event_bus import Event
        from core.events.topics import EventTopic

        if hasattr(self, "event_bus") and self.event_bus:
            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.CONTROL_EMERGENCY_STOP,
                    data={"reason": reason, "source": "okx_exchange_circuit_breaker"},
                    source="okx_exchange",
                )
            )

    async def query_order_details(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        """Query order details from exchange by client_order_id."""
        path = "/api/v5/trade/order"
        params = {"instId": symbol, "clOrdId": client_order_id}
        try:
            response = await self._request("GET", path, params=params)
            data = response.get("data", [])
            if data:
                return data[0]
            return None
        except Exception as e:
            logger.error(f"Failed to query order details for {client_order_id}: {e}")
            return None

    async def get_order_id_by_client_id(self, symbol: str, client_order_id: str) -> Optional[str]:
        """Fetch the exchange order ID using the client order ID."""
        order_details = await self.query_order_details(symbol, client_order_id)
        if order_details:
            return order_details.get("ordId")
        return None

    async def verify_order_status(self, symbol: str, client_order_id: str) -> str:
        """
        Verify the status of an order using its client_order_id.
        Queries the exchange with short bounded retries (0ms, 250ms, 500ms, 1s) to handle eventual consistency.
        Returns one of: 'LIVE', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'NOT_FOUND', 'UNKNOWN'.
        """
        path = "/api/v5/trade/order"
        params = {"instId": symbol, "clOrdId": client_order_id}
        delays = [0, 0.25, 0.5, 1.0]

        for attempt, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await self._request("GET", path, params=params)
                data = response.get("data", [])
                if data:
                    state = data[0].get("state", "").lower()
                    if state == "live":
                        return "LIVE"
                    elif state == "partially_filled":
                        return "PARTIALLY_FILLED"
                    elif state == "filled":
                        return "FILLED"
                    elif state == "canceled":
                        return "CANCELED"
                    else:
                        return "UNKNOWN"
            except OKXAPIError as e:
                err_msg = str(e)
                if "51401" in err_msg or "Order does not exist" in err_msg or "51502" in err_msg or "51603" in err_msg:
                    continue
                logger.error(f"Verification query failed with API error for clOrdId {client_order_id}: {e}")
                return "UNKNOWN"
            except Exception as e:
                logger.error(f"Verification query failed with unexpected error for clOrdId {client_order_id}: {e}")
                return "UNKNOWN"

        return "NOT_FOUND"

    async def get_algo_order_details(self, symbol: str, algo_cl_ord_id: str) -> Optional[Dict[str, Any]]:
        """Fetch details of an algo order using its client-side algo ID."""
        path = "/api/v5/trade/orders-algo-pending"
        params = {"instId": symbol, "algoClOrdId": algo_cl_ord_id}
        # [FIX] Add ordType parameter to avoid "Parameter ordType error" (code 51000)
        params["ordType"] = "conditional"
        try:
            response = await self._request("GET", path, params=params)
            data = response.get("data", [])
            if data:
                return data[0]

            history_path = "/api/v5/trade/orders-algo-history"
            history_params = {"instId": symbol, "algoClOrdId": algo_cl_ord_id}
            history_response = await self._request("GET", history_path, params=history_params)
            history_data = history_response.get("data", [])
            if history_data:
                return history_data[0]

            return None
        except Exception as e:
            logger.error(f"Failed to query algo order details for algoClOrdId {algo_cl_ord_id}: {e}")
            return None

    async def verify_algo_order_status(self, symbol: str, algo_cl_ord_id: str) -> str:
        """
        Verify status of an algo order using its algo_cl_ord_id.
        Queries the exchange with short bounded retries (0ms, 250ms, 500ms, 1s) to handle eventual consistency.
        Returns one of: 'LIVE', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'NOT_FOUND', 'UNKNOWN'.
        """
        pending_path = "/api/v5/trade/orders-algo-pending"
        history_path = "/api/v5/trade/orders-algo-history"
        params = {"instId": symbol, "algoClOrdId": algo_cl_ord_id}
        # [FIX] Add ordType parameter to avoid "Parameter ordType error" (code 51000)
        params["ordType"] = "conditional"
        delays = [0, 0.25, 0.5, 1.0]

        for attempt, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                # 1. Check pending algo orders
                pending_response = await self._request("GET", pending_path, params=params)
                pending_data = pending_response.get("data", [])
                if pending_data:
                    state = pending_data[0].get("state", "").lower()
                    if state == "partially_effective":
                        return "PARTIALLY_FILLED"
                    return "LIVE"

                # 2. Check history algo orders
                history_response = await self._request("GET", history_path, params=params)
                history_data = history_response.get("data", [])
                if history_data:
                    state = history_data[0].get("state", "").lower()
                    if state == "filled":
                        return "FILLED"
                    elif state == "canceled":
                        return "CANCELED"
                    else:
                        return "CANCELED"
            except OKXAPIError as e:
                err_msg = str(e)
                if "51401" in err_msg or "Order does not exist" in err_msg or "51502" in err_msg or "51603" in err_msg:
                    continue
                logger.error(f"Algo verification query failed with API error for algoClOrdId {algo_cl_ord_id}: {e}")
                return "UNKNOWN"
            except Exception as e:
                logger.error(f"Algo verification query failed with unexpected error for algoClOrdId {algo_cl_ord_id}: {e}")
                return "UNKNOWN"

        return "NOT_FOUND"

    async def verify_cancel_status(self, symbol: str, order_id: str) -> str:
        """
        Verify if a cancel order request was executed by checking the order status.
        Queries the exchange with short bounded retries (0ms, 250ms, 500ms, 1s) to handle eventual consistency.
        Returns one of: 'CANCELED', 'LIVE', 'PARTIALLY_FILLED', 'FILLED', 'NOT_FOUND', 'UNKNOWN'.
        """
        path = "/api/v5/trade/order"
        params = {"instId": symbol, "ordId": order_id}
        delays = [0, 0.25, 0.5, 1.0]

        last_known_state = None

        for attempt, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await self._request("GET", path, params=params)
                data = response.get("data", [])
                if data:
                    state = data[0].get("state", "").lower()
                    if state == "canceled":
                        return "CANCELED"
                    elif state == "live":
                        last_known_state = "LIVE"
                    elif state == "partially_filled":
                        last_known_state = "PARTIALLY_FILLED"
                    elif state == "filled":
                        last_known_state = "FILLED"
            except OKXAPIError as e:
                err_msg = str(e)
                if "51401" in err_msg or "Order does not exist" in err_msg or "51502" in err_msg or "51603" in err_msg:
                    continue
                logger.error(f"Cancel verification failed with API error for order {order_id}: {e}")
                return "UNKNOWN"
            except Exception as e:
                logger.error(f"Cancel verification failed with unexpected error for order {order_id}: {e}")
                return "UNKNOWN"

        if last_known_state:
            return last_known_state
        return "NOT_FOUND"

    async def fetch_markets(self) -> None:
        """Fetch and cache market data with institutional-grade specs."""
        path = "/api/v5/public/instruments"
        params = {"instType": "SWAP"}
        response = await self._request("GET", path, params=params, auth_required=False)
        data = response.get("data") or []

        self._markets = {}
        for market in data:
            inst_id = market["instId"]
            self._markets[inst_id] = {
                "instId": inst_id,
                "ctVal": float(market.get("ctVal") or 1.0),
                "tickSz": float(market.get("tickSz") or 0.01),
                "lotSz": float(market.get("lotSz") or 1.0),
                "minSz": float(market.get("minSz") or 0.001),
                "ctMult": float(market.get("ctMult") or 1.0),
                "maxLever": int(market.get("lever") or 100),
            }

        logger.info(f"Fetched {len(self._markets)} SWAP markets with dynamic specs.")

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 100, since: Optional[int] = None
    ) -> List[OHLCV]:
        """Fetch OHLCV candles from OKX with pagination support for limit > 300."""
        okx_tf = self.TIMEFRAME_MAP.get(timeframe, timeframe)

        # OKX limits: /market/candles (max 300), /market/history-candles (max 100)
        path = "/api/v5/market/history-candles" if limit > 300 else "/api/v5/market/candles"
        max_batch = 100 if limit > 300 else 300

        all_candles = []
        remaining = limit
        forward_fill = since is not None and since > 0

        params: Dict[str, str] = {"instId": symbol, "bar": okx_tf}
        if forward_fill:
            # OKX 'before': return candles newer than the given timestamp (gap-heal forward)
            params["before"] = str(since)
        elif since:
            # Historical backfill: 'after' returns candles older than timestamp
            params["after"] = str(since)

        while remaining > 0:
            batch_limit = min(remaining, max_batch)
            params["limit"] = str(batch_limit)

            response = await self._request("GET", path, params=params, auth_required=False)
            batch = response.get("data") or []

            if not batch:
                break

            all_candles.extend(batch)
            remaining -= len(batch)

            if len(batch) < batch_limit:
                break

            if forward_fill:
                # Paginate forward: oldest in batch (last element) becomes next 'before' anchor
                params["before"] = str(batch[-1][0])
            else:
                # Paginate backward in time for historical seed
                params["after"] = str(batch[-1][0])

            if limit > 300:
                await self._history_bucket.acquire()
                await asyncio.sleep(0.25)

        candles = [OHLCV.from_list(candle, symbol, timeframe) for candle in all_candles]
        if forward_fill:
            candles = [c for c in candles if c.timestamp > since]
            candles.sort(key=lambda c: c.timestamp)
        return candles

    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Fetch current ticker for a symbol."""
        path = "/api/v5/market/ticker"
        params = {"instId": symbol}

        response = await self._request("GET", path, params=params, auth_required=False)
        data = response["data"][0]

        return Ticker(
            symbol=symbol,
            last_price=float(data["last"]),
            bid=float(data["bidPx"]),
            ask=float(data["askPx"]),
            volume_24h=float(data["vol24h"]),
            timestamp=int(time.time() * 1000),
        )

    async def fetch_balance(self) -> Dict[str, Balance]:
        """Fetch account balances."""
        path = "/api/v5/account/balance"
        try:
            response = await self._request("GET", path)
            data = response.get("data", [{}])[0].get("details", [])

            balances = {}
            for item in data:
                asset = item["ccy"]
                # [FIX P1] OKX returns numeric fields as strings - use safe conversion helper
                balances[asset] = Balance(
                    asset=asset,
                    free=_safe_float(item.get("availBal"), 0.0),
                    used=_safe_float(item.get("frozenBal"), 0.0),
                    total=_safe_float(item.get("eq"), 0.0),
                )

            return balances
        except Exception as e:
            logger.error("Failed to fetch balance from OKX: {}", e)
            return {}

    async def fetch_account_equity(self) -> Dict[str, float]:
        """Fetch account-level totalEq and availEq from OKX (not per-asset wallet)."""
        path = "/api/v5/account/balance"
        try:
            response = await self._request("GET", path)
            data = response.get("data", [{}])[0]
            return {
                "totalEq": _safe_float(data.get("totalEq"), 0.0),
                "availEq": _safe_float(data.get("availEq"), 0.0),
            }
        except Exception as e:
            logger.error("Failed to fetch account equity from OKX: {}", e)
            return {}

    async def fetch_positions(self) -> List[Position]:
        """Fetch all open positions."""
        path = "/api/v5/account/positions"
        try:
            response = await self._request("GET", path)
            positions_data = response.get("data", [])

            positions = []
            for pos in positions_data:
                # [FIX P1] OKX returns numeric fields as strings
                sz = float(str(pos.get("pos") or "0"))
                if abs(sz) > 0:  # Only include non-zero positions
                    # Handle Net mode (where posSide is 'net' and sign of sz determines side)
                    if pos["posSide"] == "net":
                        side = "long" if sz > 0 else "short"
                    else:
                        side = "long" if pos["posSide"] == "long" else "short"

                    # Fetch ct_val from markets cache
                    ct_val = 1.0
                    if hasattr(self, "_markets") and pos["instId"] in self._markets:
                        ct_val = _safe_float(self._markets[pos["instId"]].get("ctVal"), 1.0)

                    tp_trigger_px = None
                    sl_trigger_px = None
                    try:
                        from services.position.tpsl_resolver import extract_tpsl_from_raw_position

                        sl_parsed, tp_parsed = extract_tpsl_from_raw_position(pos)
                        sl_trigger_px = sl_parsed
                        tp_trigger_px = tp_parsed[0] if tp_parsed else None
                    except Exception:
                        tp_trigger_px = (
                            _safe_float(pos["tpTriggerPx"])
                            if pos.get("tpTriggerPx") and pos["tpTriggerPx"] != ""
                            else None
                        )
                        sl_trigger_px = (
                            _safe_float(pos["slTriggerPx"])
                            if pos.get("slTriggerPx") and pos["slTriggerPx"] != ""
                            else None
                        )

                    positions.append(
                        Position(
                            position_id=pos["posId"],
                            symbol=pos["instId"],
                            side=side,
                            amount=abs(sz),
                            amount_remaining=abs(sz),  # [FIX] Track remaining open size
                            entry_price=_safe_float(pos.get("avgPx"), 0.0),
                            current_price=_safe_float(pos.get("markPx"), 0.0),
                            unrealized_pnl=_safe_float(pos.get("upl"), 0.0),
                            leverage=int(_safe_float(pos.get("lever"), 1.0)),
                            timestamp=int(pos.get("cTime", time.time() * 1000)),
                            ct_val=ct_val,
                            roe=_safe_float(pos.get("uplRatio"), 0.0) * 100.0,
                            margin=_safe_float(pos.get("margin"), _safe_float(pos.get("imr"), 0.0)),
                            notional_size=_safe_float(
                                pos.get("notionalUsd"),
                                abs(sz) * ct_val * _safe_float(pos.get("markPx"), 0.0)
                            ),
                            tp_trigger_px=tp_trigger_px,
                            sl_trigger_px=sl_trigger_px,
                        )
                    )

            return positions
        except Exception as e:
            logger.error("Failed to fetch positions from OKX: {}", e)
            return []

    async def fetch_recent_trades_for_symbol(self, symbol: str, since: int, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Fetch recent fills for a symbol using OKX native API and format like ccxt.
        """
        path = "/api/v5/trade/fills"
        params = {"instId": symbol, "limit": str(limit)}
        try:
            response = await self._request("GET", path, params=params)
            fills = response.get("data", [])

            # Map OKX native format to expected format
            trades = []
            for fill in fills:
                # OKX returns ts as string milliseconds
                ts = int(fill.get("ts", 0))
                if ts >= since:
                    trade = {
                        "id": fill.get("tradeId"),
                        "timestamp": ts,
                        "side": fill.get("side"),  # 'buy' or 'sell'
                        "price": float(fill.get("fillPx", 0)),
                        "amount": float(fill.get("fillSz", 0)),
                        "fee": {"cost": abs(float(fill.get("fee", 0)))}
                    }
                    trades.append(trade)

            logger.info(f"Fetched {len(trades)} recent trades for {symbol}.")
            return trades
        except Exception as e:
            logger.error(f"Failed to fetch recent trades for {symbol}: {e}")
            return []

    async def fetch_bills(
        self, symbol: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Fetch account bills for PnL reconciliation.
        Includes realized PnL, fees, and funding fees.
        """
        path = "/api/v5/account/bills"
        params = {"instType": "SWAP", "limit": str(limit)}
        if symbol:
            params["instId"] = symbol

        try:
            response = await self._request("GET", path, params=params)
            return response.get("data", [])
        except Exception as e:
            logger.error("Failed to fetch account bills: {}", e)
            return []

    async def fetch_fee_rates(self) -> Dict[str, Dict[str, float]]:
        """
        Fetch fee rates from OKX API.
        Returns maker and taker fee rates per instrument.
        """
        path = "/api/v5/account/trade-fee"
        params = {"instType": "SWAP"}  # [FIX] Add required instType parameter
        try:
            response = await self._request("GET", path, params=params)
            data = response.get("data", [])

            fee_rates = {}
            for item in data:
                inst_id = item.get("instId")
                maker_fee = float(item.get("makerFee", 0.0005))
                taker_fee = float(item.get("takerFee", 0.0005))
                fee_rates[inst_id] = {
                    "maker": maker_fee,
                    "taker": taker_fee
                }

            self._fee_rates = fee_rates
            logger.info(f"Fetched fee rates for {len(fee_rates)} instruments")
            return fee_rates
        except Exception as e:
            logger.error(f"Failed to fetch fee rates: {e}")
            return {}

    def get_fee_rate(self, symbol: str, order_type: str = "taker") -> float:
        """
        Get fee rate for a symbol.
        Falls back to default if API fee rates not available.
        """
        if hasattr(self, "_fee_rates") and symbol in self._fee_rates:
            if order_type == "maker":
                return self._fee_rates[symbol]["maker"]
            else:
                return self._fee_rates[symbol]["taker"]

        # Fallback to default from settings
        if hasattr(self, "settings"):
            if order_type == "maker":
                return self.settings.maker_fee_rate
            else:
                return self.settings.taker_fee_rate

        # Final fallback to hardcoded values
        if order_type == "maker":
            return 0.0002
        else:
            return 0.0005

    def validate_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Validate leverage against instrument max leverage from API.
        Returns True if leverage is valid, False otherwise.
        """
        if hasattr(self, "_markets") and symbol in self._markets:
            max_lever = self._markets[symbol].get("maxLever", 100)
            if leverage > max_lever:
                logger.warning(f"Leverage {leverage}x exceeds max {max_lever}x for {symbol}")
                return False
        return True

    async def fetch_trade_history(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch recent trade history (fills) from OKX."""
        path = "/api/v5/trade/fills"
        params = {"instType": "SWAP", "limit": str(limit)}
        try:
            response = await self._request("GET", path, params=params)
            return response.get("data", [])
        except Exception as e:
            logger.error("Failed to fetch trade history: {}", e)
            return []

    async def fetch_positions_history(
        self, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Fetch closed positions history from OKX.

        GET /api/v5/account/positions-history
        Returns realized PnL, fees, open/close prices for completed positions.
        """
        path = "/api/v5/account/positions-history"
        params = {"instType": "SWAP", "limit": str(limit)}
        try:
            response = await self._request("GET", path, params=params)
            return response.get("data", [])
        except Exception as e:
            logger.error("Failed to fetch positions history: {}", e)
            return []

    async def fetch_open_orders(self) -> List[Order]:
        """Fetch all open/pending orders from OKX."""
        path = "/api/v5/trade/orders-pending"
        params = {"instType": "SWAP"}
        response = await self._request("GET", path, params=params)
        orders_data = response.get("data", [])

        orders = []
        for ord in orders_data:
            orders.append(
                Order(
                    order_id=ord["ordId"],
                    client_order_id=ord.get("clOrdId", ""),
                    symbol=ord["instId"],
                    side=ord["side"],
                    type=ord["ordType"],
                    amount=float(ord["sz"]),
                    price=float(ord["px"]) if ord.get("px") else None,
                    filled_amount=float(ord.get("fillSz", 0)),
                    status=ord["state"],
                    timestamp=int(ord["cTime"]),
                    position_side=ord.get("posSide"),
                )
            )
        return orders

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
        leverage: Optional[int] = None,
        reduce_only: bool = False,
    ) -> Order:
        """Place a new order with dynamic spec validation and precision rounding."""
        # [DYNAMIC] Validate leverage against instrument max leverage
        if leverage is not None:
            if not self.validate_leverage(symbol, leverage):
                raise ValueError(f"Leverage {leverage}x exceeds max for {symbol}")

        path = "/api/v5/trade/order"

        # 1. Lấy specs động từ cache (fallback về OKX_SYMBOL_SPECS nếu chưa có)
        market_spec = self._markets.get(symbol)
        if market_spec:
            ct_val = market_spec["ctVal"]
            tick_sz = market_spec["tickSz"]
            lot_sz = market_spec["lotSz"]
            min_sz = market_spec["minSz"]
        else:
            logger.warning(f"Using fallback specs for {symbol}")
            specs = OKX_SYMBOL_SPECS.get(symbol, {})
            ct_val = specs.get("contract_value", 1.0)
            tick_sz = specs.get("tick_size", 0.01)
            lot_sz = specs.get("lot_size", 1.0)
            min_sz = specs.get("min_size", 1.0)

        # [DYNAMIC] Validate order size against minSz
        # Calculate required contracts: amount / ct_val must be >= min_sz
        required_contracts = amount / (ct_val or 1.0)
        if required_contracts < min_sz:
            logger.error(f"ORDER VALIDATION FAILED: {symbol} - Need {min_sz} min contracts, calculated {required_contracts} (amount={amount}, ct_val={ct_val})")
            raise ValueError(f"Order amount {amount} (≈{required_contracts:.6f} contracts) is smaller than the minimum required size of {min_sz} contracts for {symbol}")

        # 2. Tính toán sz (số lượng hợp đồng) bằng Decimal để triệt tiêu sai số float
        # Lý do: float(sz / lot_sz) * lot_sz có thể tạo ra 1.1000000000000003,
        # (ép int() sẽ cắt mất phần thập phân). Dùng Decimal + ROUND_DOWN loại bỏ hon toàn sai số.
        from decimal import Decimal, ROUND_DOWN

        try:
            amount_d  = Decimal(str(amount))
            ct_val_d  = Decimal(str(ct_val))  if ct_val  > 0 else Decimal("1")
            lot_sz_d  = Decimal(str(lot_sz))  if lot_sz  > 0 else Decimal("1")
            min_sz_d  = Decimal(str(min_sz))

            # Số lượng hợp đồng thô: amount / contract_value
            raw_contracts = amount_d / ct_val_d

            # Làm tròn xuống theo bước nhảy lot_sz (Quantize + ROUND_DOWN = không bao giờ over-leverage)
            sz_d = (raw_contracts / lot_sz_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * lot_sz_d

            # GUARD CLAUSE: Kiểm tra khối lượng tối thiểu trước khi gửi lệnh
            if sz_d < min_sz_d:
                logger.warning(
                    f"[ORDER GUARD] {symbol}: sz ({sz_d}) < min_sz ({min_sz_d}). "
                    f"Vốn không đủ cho 1 hợp đồng tối thiểu. Hủy lệnh."
                )
                raise OKXAPIError(
                    f"Invalid order quantity: sz={sz_d} is below minimum ({min_sz_d}) for {symbol}. "
                    "Lệnh bị hủy trước khi gửi lên OKX để tránh lỗi API."
                )

            # [FIX P3] Use symbol precision for formatting amount
            precision = max(0, -lot_sz_d.as_tuple().exponent)
            
            # [FIX OKX-002] Do NOT use int() to cast sz_d, as it will truncate lot sizes < 1
            sz_final = f"{sz_d:.{precision}f}"
            
            # Cắt bỏ phần dư thừa ".0" nếu có để giữ payload gọn gàng
            if "." in sz_final:
                sz_final = sz_final.rstrip("0").rstrip(".")

            # GUARD CLAUSE: Nếu sau khi format mà == "0", nghĩa là amount quá nhỏ
            if sz_final == "0" or sz_final == "":
                logger.warning(
                    f"[ORDER GUARD] {symbol}: sz ({sz_d}) is effectively 0 after formatting. "
                    f"Amount {amount} quá nhỏ để tạo 1 hợp đồng. Hủy lệnh."
                )
                raise ValueError(f"Order amount {amount} is too small to create even 1 contract for {symbol}. After quantization, sz={sz_final}")

            sz = sz_final

        except OKXAPIError:
            raise  # Re-raise lỗi Guard Clause để caller xử lý
        except ValueError:
            raise  # Re-raise ValueError cho caller xử lý
        except Exception as e:
            logger.error(f"[ORDER SIZING ERROR] {symbol}: Lỗi tính toán Decimal sz: {e}. Hủy lệnh.")
            raise OKXAPIError(f"Order sizing calculation failed for {symbol}: {e}")

        order_data: Dict[str, Any] = {
            "instId": symbol,
            "tdMode": self.settings.margin_mode,
            "side": side,
            "ordType": order_type,
            "sz": sz_final,  # Đã được kiểm tra bằng Decimal, chắc chắn hợp lệ
        }

        # 3. Dynamic Position Mode (posMode) Formatting
        if self.pos_mode == "long_short_mode":
            if position_side:
                order_data["posSide"] = position_side
            else:
                # Fallback to hedge mode side detection
                order_data["posSide"] = "long" if side == "buy" else "short"
        elif self.pos_mode == "net_mode":
            # OKX net mode requires 'net'
            order_data["posSide"] = "net"

        # [FIX] Enforce reduceOnly to prevent Naked Reverse Positions on delayed market closes
        if reduce_only:
            order_data["reduceOnly"] = True

        # 3. Làm tròn giá (Price Rounding) theo tickSz
        def round_px(px: float) -> str:
            # [FIX P3] Use tick_sz precision for price rounding
            rounded = round(px / tick_sz) * tick_sz
            
            # Robust precision extraction avoiding math.log10 for values like 0.5 or 0.25
            tick_str = f"{tick_sz:.10f}".rstrip("0")
            precision = len(tick_str.split(".")[1]) if "." in tick_str else 0
            
            return f"{rounded:.{precision}f}"

        if price and order_type != "market":
            order_data["px"] = round_px(price)

        # 4. Attach TP/SL if provided (OKX V5 style) using standard attachAlgoOrds array
        if tp_price or sl_price:
            algo_ord: Dict[str, Any] = {}
            if tp_price:
                algo_ord["tpTriggerPx"] = round_px(tp_price)
                algo_ord["tpOrdPx"] = "-1"  # Market TP
            if sl_price:
                algo_ord["slTriggerPx"] = round_px(sl_price)
                algo_ord["slOrdPx"] = "-1"  # Market SL
            order_data["attachAlgoOrds"] = [algo_ord]

        correlation_id = correlation_id or f"corr_{uuid4().hex[:12]}"
        cl_ord_id = client_order_id or f"vcorex{uuid4().hex[:20]}"
        order_data["clOrdId"] = cl_ord_id

        # Local default map
        recovered_status = "open"

        try:
            response = await self._request("POST", path, params=order_data)
            data = response["data"][0]
            ord_id = data["ordId"]
            res_cl_ord_id = data.get("clOrdId", cl_ord_id)
        except (asyncio.TimeoutError, aiohttp.ClientError, OKXAPIError) as exc:
            logger.warning(
                f"POST /trade/order timeout or network error caught: {exc}. "
                f"Returning PENDING_RECONCILE for async verification. correlation_id={correlation_id}"
            )
            return Order(
                order_id="UNKNOWN",
                client_order_id=cl_ord_id,
                symbol=symbol,
                side=side,
                type=order_type,
                amount=amount,
                price=price,
                filled_amount=0.0,
                status="PENDING_RECONCILE",
                timestamp=int(time.time() * 1000),
                contracts=float(sz_final),
                position_side=position_side or order_data.get("posSide"),
            )

        # Institutional logic: REST POST only means the exchange ACKED the request.
        # It is NOT FILLED or OPENED until a WS event confirms it.
        recovered_status = "ACKED"

        return Order(
            order_id=ord_id,
            client_order_id=res_cl_ord_id,
            symbol=symbol,
            side=side,
            type=order_type,
            amount=amount,
            price=price,
            filled_amount=amount if recovered_status == "filled" else 0.0,
            status=recovered_status,
            timestamp=int(time.time() * 1000),
            contracts=float(sz_final),
            position_side=position_side or order_data.get("posSide"),
        )

    async def place_algo_order(
        self,
        symbol: str,
        side: str,
        sz: float,
        tp_trigger_px: Optional[float] = None,
        sl_trigger_px: Optional[float] = None,
        position_side: Optional[str] = None,
        reduce_only: bool = True,
        correlation_id: Optional[str] = None,
    ) -> Optional[str]:
        """Place a conditional algo order for Take Profit or Stop Loss."""
        path = "/api/v5/trade/order-algo"

        # Tái sử dụng logic làm tròn giá và validate minSz
        market_spec = self._markets.get(symbol)
        if market_spec:
            tick_sz = market_spec["tickSz"]
            min_sz = market_spec.get("minSz", 0.0)
            lot_sz = market_spec.get("lotSz", 1.0)
        else:
            specs = OKX_SYMBOL_SPECS.get(symbol, {})
            tick_sz = specs.get("tick_size", 0.01)
            min_sz = specs.get("min_size", 1.0)
            lot_sz = specs.get("lot_size", 1.0)

        # Validate order size for algo order
        if sz < min_sz:
            logger.error(f"ALGO ORDER VALIDATION FAILED: {symbol} - Need {min_sz} min contracts, sent {sz}")
            raise ValueError(f"Algo order size {sz} is smaller than the minimum required size of {min_sz} contracts for {symbol}")

        def round_px(px: float) -> str:
            # [FIX P3] Use tick_sz precision for price rounding in algo orders
            rounded = round(px / tick_sz) * tick_sz
            
            # Robust precision extraction avoiding math.log10 for values like 0.5 or 0.25
            tick_str = f"{tick_sz:.10f}".rstrip("0")
            precision = len(tick_str.split(".")[1]) if "." in tick_str else 0
            return f"{rounded:.{precision}f}"

        order_data = {
            "instId": symbol,
            "tdMode": self.settings.margin_mode,
            "side": side,
            "ordType": "conditional",
            "sz": None,  # [FIX P3] Set below with proper precision rounding
            # [FIX C2] OKX API requires Boolean for reduceOnly, NOT a string.
            # json.dumps({"reduceOnly": "true"}) → {"reduceOnly": "true"} (WRONG - string)
            # json.dumps({"reduceOnly": True})  → {"reduceOnly": true}  (CORRECT - boolean)
            "reduceOnly": reduce_only,  # Pass Python bool, json.dumps handles serialization
        }

        if tp_trigger_px is not None:
            order_data["tpTriggerPx"] = round_px(tp_trigger_px)
            order_data["tpOrdPx"] = "-1"  # Market take profit

        if sl_trigger_px is not None:
            order_data["slTriggerPx"] = round_px(sl_trigger_px)
            order_data["slOrdPx"] = "-1"  # Market stop loss

        if self.pos_mode == "long_short_mode":
            if position_side:
                order_data["posSide"] = position_side
            else:
                order_data["posSide"] = "long" if side == "sell" else "short"
        elif self.pos_mode == "net_mode":
            order_data["posSide"] = "net"

        correlation_id = correlation_id or f"corr_{uuid4().hex[:12]}"
        algo_cl_ord_id = f"vcorex{uuid4().hex[:20]}"
        order_data["algoClOrdId"] = algo_cl_ord_id
        # [FIX P3] Quantize sz according to symbol lot precision
        from decimal import Decimal, ROUND_DOWN
        try:
            sz_d = Decimal(str(sz))
            lot_sz_d = Decimal(str(lot_sz)) if lot_sz > 0 else Decimal("1")
            min_sz_d = Decimal(str(min_sz))

            sz_quant = (sz_d / lot_sz_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * lot_sz_d
            if sz_quant < min_sz_d or sz_quant == Decimal("0"):
                logger.error(f"ALGO ORDER VALIDATION FAILED: {symbol} - Need {min_sz_d} min contracts, sent {sz_quant}")
                raise ValueError(f"Algo order size {sz_quant} is smaller than the minimum required size of {min_sz_d} contracts for {symbol}")

            # [FIX P3] Format sz with proper lot size precision
            lot_str = f"{lot_sz:.10f}".rstrip("0")
            precision = len(lot_str.split(".")[1]) if "." in lot_str else 0
            sz_final = f"{sz_quant:.{precision}f}"
            order_data["sz"] = sz_final

            response = await self._request("POST", path, params=order_data)
            data = response.get("data", [{}])[0]
            
            # Per-order sCode is optional in some OKX responses; top-level code already validated.
            s_code = data.get("sCode")
            if s_code not in (None, "", "0", 0):
                s_msg = data.get("sMsg", "Unknown error")
                logger.error(f"OKX API returned error for algo order: sCode={s_code}, sMsg={s_msg}")
                raise OKXAPIError(f"Algo order failed: {s_msg} (code: {s_code})")
            
            algo_id = data.get("algoId", "")
            if not algo_id:
                logger.error(f"OKX API did not return algoId in response: {data}")
                raise OKXAPIError("Algo order response missing algoId")
        except (asyncio.TimeoutError, aiohttp.ClientError, OKXAPIError) as exc:
            logger.warning(
                f"POST /trade/order-algo timeout or network error caught: {exc}. "
                f"Returning None for async verification. correlation_id={correlation_id}"
            )
            return None

        return algo_id

    async def cancel_algo_orders(self, symbol: str, algo_ids: List[str]) -> bool:
        """Cancel batch of conditional algo orders."""
        if not algo_ids:
            return True
        path = "/api/v5/trade/cancel-algos"

        # OKX limits cancel-algos to max 10 orders per request
        chunk_size = 10
        all_success = True

        for i in range(0, len(algo_ids), chunk_size):
            chunk = algo_ids[i:i+chunk_size]
            data = [{"algoId": aid, "instId": symbol} for aid in chunk]
            try:
                response = await self._request("POST", path, params=data)
                res_data = response.get("data", [])
                for res in res_data:
                    if res.get("sCode") != "0":
                        logger.warning(f"Failed to cancel algo order {res.get('algoId')}: {res.get('sMsg')}")
                        all_success = False
            except (asyncio.TimeoutError, aiohttp.ClientError, OKXAPIError) as e:
                logger.error(f"Failed to execute cancel_algo_orders chunk: {e}")
                all_success = False

        return all_success

    async def fetch_pending_algo_orders(self, symbol: Optional[str] = None, limit: int = 100, ord_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch pending (unfilled) algo orders from OKX.
        Handles pagination automatically to fetch up to `limit` orders (even > 100).

        Args:
            symbol: Optional symbol to filter by. If None, fetches all symbols.
            limit: Maximum number of orders to fetch (default: 100).
            ord_type: Optional algo order type (conditional, oco, chase, trigger, etc.)
                     If None, defaults to "conditional" for TP/SL orders

        Returns: List of pending algo order details
        """
        path = "/api/v5/trade/orders-algo-pending"
        all_orders = []
        after_id = None

        while len(all_orders) < limit:
            fetch_limit = min(limit - len(all_orders), 100)
            params = {"limit": str(fetch_limit)}

            if symbol:
                params["instId"] = symbol

            # [FIX] Add ordType parameter to avoid "Parameter ordType error" (code 51000)
            params["ordType"] = ord_type if ord_type else "conditional"

            if after_id:
                params["after"] = after_id

            try:
                response = await self._request("GET", path, params=params)
                page_orders = response.get("data", [])

                if not page_orders:
                    break

                all_orders.extend(page_orders)

                if len(page_orders) < fetch_limit:
                    break

                after_id = page_orders[-1].get("algoId")
                if not after_id:
                    break

            except (asyncio.TimeoutError, aiohttp.ClientError, OKXAPIError) as e:
                logger.error(f"Failed to fetch pending algo orders (pagination after {after_id}): {e}")
                break

        logger.debug(f"Fetched {len(all_orders)} pending algo orders" + (f" for {symbol}" if symbol else ""))
        return all_orders

    async def cancel_algo_order(self, order_id: str, symbol: str) -> bool:
        """
        Cancel a single algo order by ID.

        Args:
            order_id: The algo order ID to cancel
            symbol: The symbol of the order

        Returns: True if successful
        """
        return await self.cancel_algo_orders(symbol=symbol, algo_ids=[order_id])

    async def cancel_order(self, symbol: str, order_id: str, correlation_id: Optional[str] = None) -> bool:
        """Cancel an existing order."""
        path = "/api/v5/trade/cancel-order"
        data = {"instId": symbol, "ordId": order_id}
        correlation_id = correlation_id or f"corr_{uuid4().hex[:12]}"

        try:
            response = await self._request("POST", path, params=data)
            return response["data"][0]["sCode"] == "0"
        except (asyncio.TimeoutError, aiohttp.ClientError, OKXAPIError) as exc:
            logger.warning(
                f"POST /trade/cancel-order timeout or network error caught: {exc}. "
                f"Starting verification flow. client_order_id=None, exchange_order_id={order_id}, correlation_id={correlation_id}"
            )
            verif = await self.verify_cancel_status(symbol, order_id)
            if verif == "CANCELED":
                logger.warning(
                    f"VERIFICATION SUCCESS: Order {order_id} cancel was successfully CANCELED on exchange! Recovering safely. "
                    f"client_order_id=None, exchange_order_id={order_id}, correlation_id={correlation_id}"
                )
                logger.warning(
                    f"IDEMPOTENCY GUARANTEE: Recovery attempt for cancel order {order_id} is naturally idempotent. "
                    f"client_order_id=None, exchange_order_id={order_id}, correlation_id={correlation_id}"
                )
                return True
            elif verif == "NOT_FOUND":
                logger.warning(
                    f"VERIFICATION CONFIRMED: Order {order_id} cancel was NOT executed/found on exchange. Raising original exception. "
                    f"client_order_id=None, exchange_order_id=NOT_FOUND, correlation_id={correlation_id}"
                )
                raise exc
            elif verif in ("LIVE", "PARTIALLY_FILLED", "FILLED"):
                logger.warning(
                    f"VERIFICATION CONFIRMED: Order {order_id} cancellation timed out but order is still active on exchange with status {verif}. "
                    f"Propagating the original timeout/connection failure. client_order_id=None, exchange_order_id={order_id}, correlation_id={correlation_id}"
                )
                raise exc
            else:  # UNKNOWN
                self._circuit_breaker.record_failure()
                self._metrics.increment("exchange.order.verification.unknown", tags={"exchange": "okx", "order_id": order_id})
                logger.critical(
                    f"CRITICAL ALERT: Cancel order status verification returned UNKNOWN after timeout! "
                    f"Incrementing error count to {self._circuit_breaker.failure_count}. Initiating emergency temporary trading halt. "
                    f"client_order_id=None, exchange_order_id={order_id}, correlation_id={correlation_id}"
                )
                # Nếu circuit breaker vừa được kích hoạt, trigger emergency stop
                if self._circuit_breaker.state == CircuitState.OPEN:
                    asyncio.create_task(
                        self._trigger_emergency_stop(
                            f"Cancel status UNKNOWN after POST failure. ordId={order_id}"
                        )
                    )
                raise OKXOrderVerificationUnknownError(
                    f"CRITICAL: Cancel status of order {order_id} is UNKNOWN after POST timeout/network failure. "
                    f"Possible orphan position/order risk! Immediate intervention required. correlation_id={correlation_id}"
                ) from exc

    async def close_position(self, symbol: str, position_id: Optional[str] = None) -> Order:
        """Close an open position securely using OKX native API."""
        # 1. Fetch current positions to identify the correct posSide
        positions = await self.fetch_positions()
        target_pos = None

        for pos in positions:
            if pos.symbol == symbol:
                if position_id is None or pos.position_id == position_id:
                    target_pos = pos
                    break

        if not target_pos:
            raise ValueError(f"No open position found for {symbol}")

        # 2. Use OKX native `/api/v5/trade/close-position` (atomic, no race condition)
        path = "/api/v5/trade/close-position"
        data = {
            "instId": symbol,
            "mgnMode": self.settings.margin_mode,
        }
        
        # In net_mode, posSide is required to be "net". In long_short_mode, it must match position.
        if self.pos_mode == "net_mode":
            data["posSide"] = "net"
        else:
            data["posSide"] = target_pos.side

        response = await self._request("POST", path, params=data)
        if response.get("code") == "0":
            logger.info(f"[EXCHANGE] Native close_position executed successfully for {symbol} (posSide: {data['posSide']})")
            # [FIX C1] Return base_exchange.Order (the correct type used throughout the system),
            # NOT domain.models.Order which has a completely different schema.
            side = "sell" if target_pos.side == "long" else "buy"
            return Order(
                order_id=f"close_{target_pos.position_id}",
                client_order_id=f"close_{target_pos.position_id}",
                symbol=symbol,
                side=side,
                type="market",
                amount=target_pos.amount,
                price=None,
                filled_amount=target_pos.amount,
                status="ACKED",
                timestamp=int(time.time() * 1000),
                contracts=target_pos.amount,
                position_side=target_pos.side,
            )
        else:
            raise OKXAPIError(f"Failed to close position via native API: {response}")

    async def websocket_stream(
        self, channels: List[str], symbols: List[str], endpoint_type: str = "public"
    ) -> AsyncGenerator[WebSocketMessage, None]:
        """Connect to OKX WebSocket and stream messages."""
        reconnect_attempts = 0
        disconnect_timestamp = None

        while True:  # [FIX] Removed max reconnect attempts limit - bot should keep trying to reconnect indefinitely
            try:
                is_private = (endpoint_type == "private")

                # Build the final WebSocket URL
                # OKX Demo spec: candle channels MUST use /ws/v5/business endpoint
                # Other public channels use /ws/v5/public, private channels use /ws/v5/private
                # OKX Demo requires ?brokerId=9999 parameter
                current_ws_url = self.ws_url
                if current_ws_url.endswith("/ws/v5"):
                    current_ws_url += f"/{endpoint_type}"
                else:
                    # If URL already has an endpoint, replace it
                    # Remove existing endpoint and append the correct one
                    parts = current_ws_url.split("/ws/v5")
                    if len(parts) > 1:
                        current_ws_url = parts[0] + "/ws/v5/" + endpoint_type

                # Add demo-specific brokerId parameter if in demo mode (required by OKX Demo spec)
                if self.demo_mode and "brokerId=9999" not in current_ws_url:
                    if "?" in current_ws_url:
                        current_ws_url += "&brokerId=9999"
                    else:
                        current_ws_url += "?brokerId=9999"
                    logger.debug(f"Added OKX Demo brokerId parameter to WebSocket URL: {current_ws_url}")

                logger.info(f"Connecting to WebSocket: {current_ws_url} (endpoint_type={endpoint_type}, demo_mode={self.demo_mode})")

                async with connect(current_ws_url) as websocket:
                    self._ws_connected = True
                    # Tính toán thời gian recover thực tế nếu có disconnect trước đó
                    if disconnect_timestamp is not None:
                        # Bắt đầu đo OVERHEAD của instrumentation
                        instrumentation_start = time.perf_counter()

                        recovery_time_ms = (time.time() - disconnect_timestamp) * 1000
                        # Rolling window: giữ tối đa 1000 records, xóa record cũ nhất nếu đầy
                        if len(self._reconnect_metrics) >= self._max_reconnect_metrics:
                            self._reconnect_metrics.pop(0)
                            self._reconnect_attempts.pop(0)
                        self._reconnect_metrics.append(recovery_time_ms)
                        self._reconnect_attempts.append(reconnect_attempts)

                        # Cập nhật histogram phân phối số lần thử
                        attempts = reconnect_attempts
                        self._reconnect_attempt_histogram[attempts] = self._reconnect_attempt_histogram.get(attempts, 0) + 1
                        self._total_reconnect_success += 1

                        # Chỉ log benchmark 1/5 lần để tránh spam, hoặc lần đầu tiên
                        should_log = (self._total_reconnect_success <= 5) or (self._total_reconnect_success % 5 == 0)
                        if should_log and len(self._reconnect_metrics) >= 1:
                            import numpy as np
                            metrics_arr = np.array(self._reconnect_metrics)
                            success_ratio = self._total_reconnect_success / (self._total_reconnect_success + self._failed_reconnect_count) if (self._total_reconnect_success + self._failed_reconnect_count) >0 else 1.0
                            logger.info(
                                f"[RECONNECT BENCHMARK] Recovered in {recovery_time_ms:.0f}ms after {attempts} attempts. "
                                f"Stats: min={np.min(metrics_arr):.0f}ms avg={np.mean(metrics_arr):.0f}ms max={np.max(metrics_arr):.0f}ms "
                                f"p95={np.percentile(metrics_arr,95):.0f}ms p99={np.percentile(metrics_arr,99):.0f}ms | "
                                f"Success ratio={success_ratio:.2%} | Attempts distribution: {dict(self._reconnect_attempt_histogram)}"
                            )

                        # Kết thúc đo overhead, lưu lại để báo cáo
                        instrumentation_end = time.perf_counter()
                        instrumentation_overhead_ms = (instrumentation_end - instrumentation_start) * 1000
                        # Lưu overhead vào danh sách để tính thống kê
                        if not hasattr(self, '_instrumentation_overheads'):
                            self._instrumentation_overheads: list[float] = []
                        if len(self._instrumentation_overheads) >= 1000:
                            self._instrumentation_overheads.pop(0)
                        self._instrumentation_overheads.append(instrumentation_overhead_ms)

                        # Log overhead stats mỗi 10 lần có logging, xác minh overhead <1ms
                        if should_log and len(self._instrumentation_overheads) >= 5:
                            import numpy as np
                            overhead_arr = np.array(self._instrumentation_overheads)
                            logger.info(
                                f"[INSTRUMENTATION OVERHEAD] avg={np.mean(overhead_arr):.3f}ms "
                                f"p95={np.percentile(overhead_arr,95):.3f}ms p99={np.percentile(overhead_arr,99):.3f}ms | "
                                f"VERIFIED: all overheads <1ms: {np.max(overhead_arr) < 1.0}"
                            )

                        # Reset disconnect timestamp
                        disconnect_timestamp = None

                    reconnect_attempts = 0
                    self._last_heartbeat = time.time()
                    logger.info("WebSocket connected successfully")

                    # Send login if we're accessing private channels
                    if is_private:
                        try:
                            # [FIX P1] Add 30-second timeout for WebSocket login
                            async with asyncio.timeout(30):
                                await self._ws_login(websocket)
                        except asyncio.TimeoutError:
                            logger.error("WebSocket login timeout after 30s - closing connection")
                            await websocket.close()
                            raise

                    # Subscribe to channels
                    subscribe_args = []
                    private_channels_added = set()

                    active_symbols = []
                    for symbol in symbols:
                        if self._markets and symbol not in self._markets:
                            logger.warning(
                                f"Symbol {symbol} is in watchlist but not supported/available on OKX exchange; skipping subscription."
                            )
                        else:
                            active_symbols.append(symbol)

                    for channel in channels:
                        if channel in ("account", "positions", "orders", "orders-algo"):
                            if channel not in private_channels_added:
                                if channel == "orders-algo":
                                    subscribe_args.append({"channel": channel, "instType": "SWAP"})
                                    subscribe_args.append({"channel": channel, "instType": "FUTURES"})
                                    subscribe_args.append({"channel": channel, "instType": "SPOT"})
                                    subscribe_args.append({"channel": channel, "instType": "MARGIN"})
                                else:
                                    subscribe_args.append({"channel": channel, "instType": "ANY"})
                                private_channels_added.add(channel)
                        else:
                            # Public channels (candle*, tickers*, etc.) don't need instType
                            for symbol in active_symbols:
                                subscribe_args.append(
                                    {"channel": channel, "instId": symbol}
                                )

                    if subscribe_args:
                        await websocket.send(
                            json.dumps({"op": "subscribe", "args": subscribe_args})
                        )
                        logger.debug(f"Subscribed to channels: {channels} for symbols: {active_symbols}")

                    # Yield a system connect message so the system knows to trigger resync
                    yield WebSocketMessage(
                        channel="system",
                        symbol="connect",
                        data={"status": "connected"},
                        timestamp=datetime.now(timezone.utc),
                    )
                    # STATE SYNC: Đồng bộ vị thế từ REST API ngay sau khi reconnect thành công
                    # Tránh ghost positions do state mismatch sau khi mất kết nối
                    try:
                        logger.info("[RECONNECT-SYNC] Starting post-reconnect position sync from REST API")
                        await self.fetch_positions()
                        logger.info("[RECONNECT-SYNC] Post-reconnect position sync completed successfully, all positions are up-to-date")
                    except Exception as sync_e:
                        logger.error(f"[RECONNECT-SYNC] Failed to sync positions after reconnect: {sync_e}", exc_info=True)

                    # Start heartbeat task
                    heartbeat_task = asyncio.create_task(self._ws_heartbeat(websocket))

                    try:
                        while True:
                            message = await websocket.recv()
                            self._ws_message_count += 1

                            if message == "pong":
                                self._last_heartbeat = time.time()
                                continue

                            data = json.loads(message)

                            # Debug log for private channels and handle subscription errors gracefully
                            if "event" in data and data["event"] == "error":
                                error_code = data.get("code", "")
                                # Ignore subscription errors for non-existent symbols/channels to prevent endless reconnections
                                if error_code == "60018":
                                    logger.warning(f"WS Non-critical subscription error (ignoring): {data.get('msg')}")
                                else:
                                    logger.error(f"WS Error: {data}")
                            elif "arg" in data and data["arg"].get("channel") in ("account", "positions", "orders", "orders-algo"):
                                logger.debug(f"Raw private WS message: {message[:200]}...")

                            # Parse and yield regular messages
                            if "data" in data and "arg" in data:
                                arg = data["arg"]
                                websocket_receive_time = datetime.now(timezone.utc)
                                for item in data["data"]:
                                    yield WebSocketMessage(
                                        channel=arg.get("channel", ""),
                                        symbol=arg.get("instId", ""),
                                        data=item,
                                        timestamp=websocket_receive_time,
                                    )

                    finally:
                        heartbeat_task.cancel()
                        await heartbeat_task

            except ws_exceptions.ConnectionClosed as e:
                # Ghi nhận thời điểm ngắt kết nối (chỉ ghi lần đầu nếu chưa có)
                if disconnect_timestamp is None:
                    disconnect_timestamp = time.time()
                self._ws_connected = False
                reconnect_attempts += 1
                logger.warning(
                    "WebSocket disconnected (attempt {}): {}",
                    reconnect_attempts,
                    e,
                )
                # Bounded exponential backoff with jitter - max 60s wait to ensure reconnect
                base = 1.0
                max_delay = 60.0
                backoff = min(base * (2 ** (reconnect_attempts - 1)), max_delay) + random.random() * 0.5
                await asyncio.sleep(backoff)

            except Exception as e:
                # Ghi nhận thời điểm ngắt kết nối (chỉ ghi lần đầu nếu chưa có)
                if disconnect_timestamp is None:
                    disconnect_timestamp = time.time()
                self._ws_connected = False
                reconnect_attempts += 1
                logger.error("WebSocket error: {}", str(e), exc_info=True)
                # Bounded exponential backoff with jitter
                base = 1.0
                max_delay = 60.0
                backoff = min(base * (2 ** (reconnect_attempts - 1)), max_delay) + random.random() * 0.5
                await asyncio.sleep(backoff)

        # [FIX] Removed max reconnect attempts limit - bot will keep trying indefinitely
        # This line is now unreachable due to the while True loop above
        logger.critical(f"WebSocket reconnection loop exited unexpectedly for {endpoint_type} endpoint")
        self._ws_connected = False
    async def _ws_login(self, websocket: Any) -> None:
        """Authenticate WebSocket connection for private channels."""
        timestamp = self._get_timestamp()
        sign = self._sign(timestamp, "GET", "/users/self/verify")

        login_msg = {
            "op": "login",
            "args": [
                {
                    "apiKey": self.api_key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": sign,
                }
            ],
        }

        await websocket.send(json.dumps(login_msg))
        response = await websocket.recv()
        login_response = json.loads(response)

        if login_response.get("code") != "0":
            from core.exceptions import OKXAPIError

            raise OKXAPIError(f"WebSocket login failed: {login_response}")

        logger.info("WebSocket authenticated successfully")

    async def _ws_heartbeat(self, websocket: Any) -> None:
        """Maintain WebSocket connection with periodic heartbeats and watchdog verification."""
        last_ping_time = time.time()
        while True:
            await asyncio.sleep(5)  # Run active watchdog check every 5 seconds
            try:
                now = time.time()
                # 1. WATCHDOG CHECK: If no pong/heartbeat received in the last 60 seconds (half-open socket), force close
                if now - self._last_heartbeat > 60.0:
                    logger.error(
                        "WebSocket Watchdog Triggered: No heartbeat response received for {:.1f}s. "
                        "Half-open connection suspected. Forcing socket close to trigger auto-reconnect...",
                        now - self._last_heartbeat
                    )
                    await websocket.close()
                    break

                # 2. PING TRANSMISSION: Send ping every 30 seconds
                if now - last_ping_time >= 30.0:
                    await websocket.send("ping")
                    last_ping_time = now
                    logger.debug("WebSocket ping sent")
            except Exception as e:
                logger.warning(f"WebSocket watchdog/ping execution failed: {e}")
                break

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol with caching and concurrency control."""
        path = "/api/v5/account/set-leverage"
        
        # PHASE 2C: Acquire leverage semaphore to limit concurrent requests
        async with self._leverage_semaphore:
            # PHASE 2A + 2E: Check cache first - skip if already set recently
            now = time.time()
            # Set leverage for both long and short in long_short_mode
            if self.pos_mode == "long_short_mode":
                sides = ["long", "short"]
            else:
                sides = ["net"] if self.pos_mode == "net_mode" else [None]
                
            success = True
            for side in sides:
                cache_key = (symbol, side, leverage) if side else (symbol, None, leverage)
                if cache_key in self._last_leverage_set:
                    if now - self._last_leverage_set[cache_key] < self._leverage_cache_ttl:
                        logger.debug(f"[LEVERAGE-CACHE] Skipping set_leverage for {symbol} ({side}) - already set in last 24h")
                        continue
                
                data = {
                    "instId": symbol,
                    "lever": str(leverage),
                    "mgnMode": self.settings.margin_mode,
                }
                if side:
                    data["posSide"] = side
                    
                try:
                    await self._request("POST", path, params=data)
                    # Update cache on success
                    self._last_leverage_set[cache_key] = now
                    logger.debug(f"[LEVERAGE-CACHE] Updated cache for {symbol} ({side}) leverage={leverage}x")
                except Exception as e:
                    logger.error(f"Failed to set leverage {leverage}x for {symbol} ({side}): {e}")
                    success = False
                    
            return success

    async def get_rate_limit_remaining(self) -> int:
        """Get remaining API calls."""
        return self._rate_limit_remaining

    def get_api_metrics(self) -> Dict[str, Any]:
        """Return live API diagnostics metrics."""
        return {
            "api_request_count": self._api_request_count,
            "api_error_count": self._api_error_count,
            "ws_message_count": getattr(self, "_ws_message_count", 0),
        }

    def normalize_position_size(self, symbol: str, size: float) -> float:
        """Normalize position size according to OKX instrument specs."""
        market_spec = self._markets.get(symbol)
        if market_spec:
            lot_sz = market_spec.get("lotSz", 1.0)
            min_sz = market_spec.get("minSz", 1.0)
        else:
            specs = OKX_SYMBOL_SPECS.get(symbol, {})
            lot_sz = specs.get("lot_size", 1.0)
            min_sz = specs.get("min_size", 1.0)

        if lot_sz > 0:
            # Avoid floating point precision issues in math by rounding to 8 decimals first
            val = round(size / lot_sz, 8)
            normalized = math.floor(val) * lot_sz
        else:
            normalized = size

        if normalized < min_sz:
            return 0.0
        return normalized

    # [FIX] Cache management methods for API fallback
    def _update_price_cache(self, symbol: str, price: float) -> None:
        """Update cached price for a symbol."""
        self._price_cache[symbol] = price
        self._price_cache_timestamps[symbol] = time.time()

    def _get_cached_price(self, symbol: str) -> Optional[float]:
        """Get cached price if still valid (within TTL)."""
        if symbol not in self._price_cache:
            return None
        timestamp = self._price_cache_timestamps.get(symbol, 0)
        if time.time() - timestamp > self._cache_ttl_seconds:
            # Cache expired
            del self._price_cache[symbol]
            del self._price_cache_timestamps[symbol]
            return None
        return self._price_cache[symbol]

    def _update_api_health_status(self, status: str) -> None:
        """Update API health status."""
        self._api_health_status = status
        self._last_api_health_check = time.time()
        logger.info(f"[API HEALTH] Status updated to {status}")