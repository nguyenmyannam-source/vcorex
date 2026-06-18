
"""Utility mixin for safe signal generation.

Features:
- Deduplication based on (symbol, timeframe) and signal candle timestamp.
- Stale‑signal detection with configurable tolerance.
- Cool‑down tracking per (symbol, timeframe).
- Optional persistence to a JSON file so state survives restarts.
- Async lock to guard state mutations against race conditions.
"""

import json
import os
import asyncio
import time
from datetime import datetime, timezone
from typing import Tuple, Dict

from core.config.settings import settings
from loguru import logger

STATE_FILE = os.path.join(os.path.dirname(__file__), "signal_state.json")


class SignalSafetyMixin:
    """Mixin providing safe‑signal helpers.

    Classes using this mixin must define:
    - `config` with attribute `cooldown_minutes`.
    - `record_signal(symbol, timeframe)` will be overridden.
    """

    _state_lock: asyncio.Lock
    _last_processed: Dict[Tuple[str, str], int]  # (symbol, timeframe) -> candle timestamp ms
    _last_signal_time: Dict[Tuple[str, str], datetime]
    _missed_signals: list  # list of dicts: {'time': str, 'symbol': str, 'timeframe': str, 'reason': str}

    def __init__(self, *args, **kwargs):
        # Ensure the lock exists even if subclass does not call super().__init__
        self._state_lock = asyncio.Lock()
        self._last_processed = {}
        self._last_signal_time = {}
        self._missed_signals = []
        self._pending_save = False  # Cờ Debounce I/O
        self.settings = settings
        self._load_state()
        super().__init__(*args, **kwargs)  # type: ignore[misc]

    # ---------------------------------------------------------------------
    # Persistence helpers
    # ---------------------------------------------------------------------
    def _load_state(self) -> None:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._last_processed = {
                    tuple(k.split("|")): v for k, v in data.get("last_processed", {}).items()
                }
                self._last_signal_time = {
                    tuple(k.split("|")): datetime.fromisoformat(v)
                    for k, v in data.get("last_signal_time", {}).items()
                }
                self._missed_signals = data.get("missed_signals", [])
                logger.debug("SignalSafetyMixin state loaded from disk")
            except Exception as e:
                logger.error(f"Failed to load signal safety state: {e}")
        else:
            logger.debug("No persisted signal safety state found; starting fresh")

    async def _save_state(self) -> None:
        """Ghi đè xuống ổ cứng (Hàm nội bộ). Để an toàn hiệu suất, hãy gọi thông qua _trigger_debounced_save()."""
        # COPY dictionaries and lists under lock to prevent "dictionary changed size during iteration"
        async with self._state_lock:
            last_processed = dict(self._last_processed)
            last_signal_time = dict(self._last_signal_time)
            missed_signals = list(self._missed_signals)

        try:
            data = {
                "last_processed": {"|".join(k): v for k, v in last_processed.items()},
                "last_signal_time": {"|".join(k): v.isoformat() for k, v in last_signal_time.items()},
                "missed_signals": missed_signals,
            }
            # Chạy file write trong thread pool để không block event loop
            try:
                import aiofiles
                async with aiofiles.open(STATE_FILE, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            except ImportError:
                # Fallback to sync write nếu aiofiles chưa được cài
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False, indent=2))
                logger.debug("aiofiles not installed, fallback to sync write")
                
            logger.debug("SignalSafetyMixin state persisted to disk")
        except Exception as e:
            logger.error(f"Failed to persist signal safety state: {e}")

    async def _debounced_save_task(self):
        """Task ngầm chờ 5 giây rồi mới ghi file để gom cụm (batch) I/O."""
        await asyncio.sleep(5)
        async with self._state_lock:
            self._pending_save = False
        await self._save_state()

    def _trigger_debounced_save(self):
        """Kích hoạt ghi file có gộp (Debounce 5s) chống I/O Blocking."""
        if not self._pending_save:
            self._pending_save = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._pending_save = False
                logger.debug("No running event loop; skipping debounced signal state save")
                return
            loop.create_task(self._debounced_save_task())

    async def flush_state_immediate(self):
        """Cưỡng bức ghi file ngay lập tức. Dùng khi shutdown/reset bot."""
        await self._save_state()

    # ---------------------------------------------------------------------
    # History tracking
    # ---------------------------------------------------------------------
    async def record_missed_signal(self, symbol: str, timeframe: str, reason: str) -> None:
        """Record a missed signal for the history dashboard.

        Includes deduplication: same (symbol, timeframe) won't be recorded
        again within a 5-minute window to prevent spam from REST polling.
        """
        now = datetime.now(timezone.utc)
        async with self._state_lock:
            # Dedup check: skip if same symbol+timeframe was recorded < 5 min ago
            for entry in self._missed_signals[:10]:  # Only check recent entries
                try:
                    entry_time = datetime.fromisoformat(entry["time"])
                    if (entry.get("symbol") == symbol
                            and entry.get("timeframe") == timeframe
                            and (now - entry_time).total_seconds() < 300):
                        return  # Skip duplicate within 5-minute window
                except Exception as e:
                    logger.warning(
                        f"[MISSED_SIGNAL_DEDUP_ERROR] {e}"
                    )
                    continue

            self._missed_signals.insert(0, {
                "time": now.isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "reason": reason
            })
            # Keep only the last 50 missed signals
            if len(self._missed_signals) > 50:
                self._missed_signals = self._missed_signals[:50]

            # GỌI GHI GỘP (DEBOUNCE) THAY VÌ GHI TRỰC TIẾP
            self._trigger_debounced_save()

    def get_missed_signals(self) -> list:
        """Get the recent missed signals list."""
        return self._missed_signals

    # ---------------------------------------------------------------------
    # Deduplication
    # ---------------------------------------------------------------------
    async def is_duplicate(self, symbol: str, timeframe: str, candle_timestamp: int) -> bool:
        key = (symbol, timeframe)
        async with self._state_lock:
            last_ts = self._last_processed.get(key)
            # Trong realtime mode (nến forming), cho phép cập nhật tín hiệu trên cùng một nến
            # Chỉ coi là trùng lặp nếu cùng timestamp và đã quá thời gian tối thiểu giữa các lần xử lý (30s)
            from services.market_data_engine import MarketDataEngine
            if timeframe not in MarketDataEngine.TIMEFRAME_SECONDS:
                logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
                raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
            tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS[timeframe]
            min_interval = min(30, tf_seconds * 0.1)  # 10% của timeframe hoặc 30s, lấy giá trị nhỏ hơn
            
            if last_ts == candle_timestamp:
                # Kiểm tra thời gian đã trôi qua kể từ lần xử lý trước
                # Tính khoảng cách thời gian giữa lần xử lý trước và hiện tại
                now = time.time() * 1000  # ms
                # Nếu còn trong khoảng thời gian min_interval, coi là trùng lặp
                # Nếu đã qua min_interval, cho phép xử lý lại (cập nhật tín hiệu trên nến forming)
                # Lưu last_processed_time để kiểm tra
                last_processed_key = f"{symbol}|{timeframe}|processed_time"
                if not hasattr(self, '_last_processed_time'):
                    self._last_processed_time = {}
                last_processed_time = self._last_processed_time.get(last_processed_key, 0)
                time_since_last = (now - last_processed_time) / 1000  # s
                
                if time_since_last < min_interval:
                    duplicate = True
                    logger.debug(
                        f"Duplicate signal rejected for {symbol}/{timeframe} at ts {candle_timestamp} (processed {time_since_last:.1f}s ago, min interval {min_interval}s)"
                    )
                else:
                    # Cho phép xử lý lại nến forming sau khoảng thời gian min_interval
                    duplicate = False
                    logger.debug(
                        f"Realtime update allowed for {symbol}/{timeframe} at ts {candle_timestamp} (processed {time_since_last:.1f}s ago, min interval {min_interval}s)"
                    )
                    # Cập nhật thời gian xử lý mới
                    self._last_processed_time[last_processed_key] = now
            else:
                duplicate = False
                # Nến mới, cập nhật thời gian xử lý
                last_processed_key = f"{symbol}|{timeframe}|processed_time"
                if not hasattr(self, '_last_processed_time'):
                    self._last_processed_time = {}
                self._last_processed_time[last_processed_key] = time.time() * 1000

        if duplicate:
            await self.record_missed_signal(symbol, timeframe, "Trùng lặp nến cũ")

        return duplicate

    async def mark_processed(self, symbol: str, timeframe: str, candle_timestamp: int) -> None:
        key = (symbol, timeframe)
        async with self._state_lock:
            self._last_processed[key] = candle_timestamp
            # Cập nhật thời gian xử lý cuối cùng cho nến này
            last_processed_key = f"{symbol}|{timeframe}|processed_time"
            if not hasattr(self, '_last_processed_time'):
                self._last_processed_time = {}
            self._last_processed_time[last_processed_key] = time.time() * 1000  # ms
            self._trigger_debounced_save()

    # ---------------------------------------------------------------------
    # Cool‑down handling (per symbol+timeframe)
    # ---------------------------------------------------------------------
    def _cooldown_key(self, symbol: str, timeframe: str) -> Tuple[str, str]:
        return (symbol, timeframe)

    async def is_in_cooldown(self, symbol: str, timeframe: str) -> bool:
        key = self._cooldown_key(symbol, timeframe)
        async with self._state_lock:
            last = self._last_signal_time.get(key)
        if not last:
            return False
        cooldown_mins = self.config.cooldown_minutes if self.config.cooldown_minutes is not None else self.settings.cooldown_minutes
        seconds = cooldown_mins * 60
        in_cd = (datetime.now(timezone.utc) - last).total_seconds() < seconds
        if in_cd:
            logger.debug(
                f"Symbol {symbol}/{timeframe} is in cooldown (remaining {(seconds - (datetime.now(timezone.utc) - last).total_seconds()):.0f}s)"
            )
            await self.record_missed_signal(symbol, timeframe, "Đang trong thời gian Cooldown")

        return in_cd

    async def record_signal(self, symbol: str, timeframe: str) -> None:
        key = self._cooldown_key(symbol, timeframe)
        async with self._state_lock:
            self._last_signal_time[key] = datetime.now(timezone.utc)
            self._trigger_debounced_save()
        logger.debug(f"Recorded signal for {symbol}/{timeframe}, cooldown started")

    # ---------------------------------------------------------------------
    # Stale‑signal check
    # ---------------------------------------------------------------------
    async def is_stale(self, signal_candle_timestamp: int, timeframe: str, symbol: str = "") -> bool:
        """Return True if the signal is too old.

        Sử dụng UTC Epoch Seconds thuần túy để loại bỏ lệch múi giờ.
        """
        from services.market_data_engine import MarketDataEngine

        if timeframe not in MarketDataEngine.TIMEFRAME_SECONDS:
            logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
            raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
        tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS[timeframe]

        # Chuyển đổi timestamp của nến (ms) sang giây (s)
        candle_open_sec = signal_candle_timestamp / 1000.0
        candle_close_sec = candle_open_sec + tf_seconds

        # Lấy UTC Epoch thuần túy của hệ thống hiện tại
        now_sec = time.time()

        # Timeframe-specific thresholds from .env
        stale_thresholds = {
            "5m": self.settings.stale_signal_5m_seconds,
            "15m": self.settings.stale_signal_15m_seconds,
            "1H": self.settings.stale_signal_1h_seconds,
            "4H": self.settings.stale_signal_4h_seconds,
            "1D": self.settings.stale_signal_1d_seconds,
            "1W": self.settings.stale_signal_1w_seconds,
            "1M": self.settings.stale_signal_1m_seconds,
        }
        if timeframe not in stale_thresholds:
            logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
            raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
        max_delay_sec = stale_thresholds[timeframe]

        delay = now_sec - candle_close_sec

        if delay > max_delay_sec:
            logger.warning(
                f"STALE SIGNAL REJECTED [{symbol} {timeframe}]: delayed {delay:.1f}s > {max_delay_sec:.1f}s"
            )
            await self.record_missed_signal(symbol, timeframe, f"Tín hiệu trễ ({delay:.1f}s)")
            return True
        return False