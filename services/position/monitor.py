"""Position monitoring and PnL updates."""
import asyncio
import time
from typing import Any, Dict, Optional

from loguru import logger

from .models import TrackedPosition


class PositionMonitor:
    """Monitors active positions, updates PnL, and handles price feeds."""

    def __init__(self, ticker_cache_size: int = 1000, ticker_ttl: int = 3600):
        self._ticker_cache: Dict[str, Dict[str, Any]] = {}
        self._pnl_cache: Dict[str, float] = {}
        self.ticker_cache_size = ticker_cache_size
        self.ticker_ttl = ticker_ttl  # TTL 1 giờ cho dữ liệu ticker không hoạt động
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start background cleanup task to prevent memory leaks."""
        if self._cleanup_task is None:
            self._running = True
            self._cleanup_task = asyncio.create_task(self._background_cleanup_worker())
            logger.info("PositionMonitor background cleanup worker started")

    async def stop(self) -> None:
        """Stop background cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("PositionMonitor background cleanup worker stopped")

    async def _background_cleanup_worker(self) -> None:
        """Background worker that runs every 5 minutes to clean up stale cache entries."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Clean up every 5 phút
                self._cleanup_stale_tickers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in PositionMonitor cleanup worker: {e}")

    def _cleanup_stale_tickers(self) -> None:
        """Remove ticker data older than TTL to prevent memory leak."""
        current_time = time.time()
        stale_symbols = []
        for symbol, ticker_data in self._ticker_cache.items():
            if current_time - ticker_data["ts"] > self.ticker_ttl:
                stale_symbols.append(symbol)
        
        if stale_symbols:
            for symbol in stale_symbols:
                del self._ticker_cache[symbol]
            logger.info(f"Cleaned up {len(stale_symbols)} stale ticker entries from cache")

    async def update_position_pnl(self, tracked: TrackedPosition) -> None:
        """Update position PnL based on current price."""
        try:
            current_price = tracked.current_price
            if current_price <= 0:
                logger.debug(f"Skipping PnL update for {tracked.id}: invalid price {current_price}")
                return

            # Calculate PnL
            price_diff = current_price - tracked.entry_price
            if tracked.side == "short":
                price_diff = -price_diff

            pnl = price_diff * tracked.amount_remaining * tracked.ct_val

            # Calculate ROE (Standard OKX Formula using Leveraged Margin)
            margin = tracked.get_margin()
            if margin > 0:
                roe = (pnl / margin) * 100
            else:
                roe = 0.0

            tracked.pnl = pnl
            tracked.roe = roe
            self._pnl_cache[tracked.id] = pnl

            logger.debug(f"PnL updated for {tracked.symbol}: ${pnl:.2f} (ROE: {roe:.2f}%)")

        except Exception as e:
            logger.error(f"Failed to update PnL for {tracked.id}: {e}")

    async def handle_ticker_update(self, symbol: str, price: float, timestamp: float) -> None:
        """Handle incoming ticker data and update position prices."""
        self._ticker_cache[symbol] = {"price": price, "ts": timestamp}

    async def update_positions_from_tickers(self, positions: Dict[str, TrackedPosition]) -> None:
        """Update all position prices from latest ticker data."""
        for symbol, ticker_data in self._ticker_cache.items():
            for pos in positions.values():
                if pos.symbol == symbol:
                    pos.current_price = ticker_data["price"]
                    await self.update_position_pnl(pos)
    def remove_position(self, internal_id: str) -> None:
        """Remove a position from all caches to prevent memory leaks."""
        if internal_id in self._pnl_cache:
            del self._pnl_cache[internal_id]
            logger.debug(f"Removed {internal_id} from PnL cache")

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "ticker_cache_size": len(self._ticker_cache),
            "pnl_cache_size": len(self._pnl_cache),
        }

    def clear_cache(self) -> None:
        """Clear caches."""
        self._ticker_cache.clear()
        self._pnl_cache.clear()
        logger.debug("Position monitor caches cleared")