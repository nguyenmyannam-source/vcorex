import asyncio
import copy
import os
from datetime import datetime, timezone
from loguru import logger

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic

# Import OKX Premium 3-panel chart generator
from infrastructure.telegram.chart_generator import generate_entry_chart_sync

class ChartService:
    """
    Listens for approved trading signals and generates a professional OKX Premium 3-panel chart 
    (Price + Volume + ADX) using Plotly without blocking the async event loop.
    """
    
    def __init__(self, event_bus: EventBus, market_data_engine):
        self.event_bus = event_bus
        self.market_data_engine = market_data_engine
        self._running = False
        self._save_dir = "data/charts"
        os.makedirs(self._save_dir, exist_ok=True)
        
    def start(self):
        if self._running:
            return
        self._running = True
        self.event_bus.subscribe(
            self._on_signal_approved,
            [EventTopic.RISK_SIGNAL_APPROVED],
            handler_id="chart_service"
        )
        logger.info("ChartService started")
        
    def stop(self):
        self._running = False
        self.event_bus.unsubscribe("chart_service")
        logger.info("ChartService stopped")
        
    async def _on_signal_approved(self, event: Event):
        """
        Handler for RISK_SIGNAL_APPROVED.
        Extracts signal data and runs the chart generation in a separate thread.
        """
        try:
            signal_data = event.data
            symbol = signal_data.get("symbol")
            timeframe = signal_data.get("timeframe")
            side = signal_data.get("signal_type")
            entry_price = signal_data.get("entry_price")
            snapshot_dict = signal_data.get("snapshot")
            
            if not symbol or not timeframe:
                logger.error("ChartService: Missing symbol or timeframe in signal")
                return
                
            # Get last 50 candles from MarketDataEngine buffer
            candles = self.market_data_engine.get_candles_snapshot(symbol, timeframe, limit=60)
            if not candles or len(candles) < 20:
                logger.warning(f"ChartService: Not enough candles to draw chart for {symbol}")
                return
            
            # [THREAD-SAFETY] Create immutable snapshots before passing to worker thread
            # This prevents RuntimeError: list changed size during iteration if main loop updates candles
            candles_copy = copy.deepcopy(candles)  # Deep copy to ensure complete isolation
            snapshot_copy = copy.deepcopy(snapshot_dict) if snapshot_dict else None
                
            # Capture event loop reference BEFORE entering thread
            loop = asyncio.get_running_loop()
            
            # Run rendering in thread pool to avoid blocking async loop
            await asyncio.to_thread(
                self._generate_and_emit_chart,
                symbol, timeframe, side, entry_price, candles_copy, snapshot_copy, loop
            )
            
        except Exception as e:
            logger.error(f"ChartService: Failed to process approved signal - {e}")

    def _sync_render_chart_process(self, symbol, timeframe, side, candles, snapshot_dict):
        """
        Synchronous function that performs all blocking CPU operations for chart rendering.
        This runs in a worker thread to avoid blocking the main event loop.
        """
        try:
            logger.info("[CRITICAL-CHART] Starting OKX Premium 3-panel chart generation")
            
            # Extract indicators from snapshot_dict
            indicators = {}
            if snapshot_dict:
                if isinstance(snapshot_dict, dict):
                    indicators = {
                        "adx": snapshot_dict.get("adx", 0.0),
                        "ema_fast": snapshot_dict.get("ema_fast", 0.0),
                        "ema_slow": snapshot_dict.get("ema_slow", 0.0)
                    }
                else:
                    indicators = {
                        "adx": getattr(snapshot_dict, "adx", 0.0),
                        "ema_fast": getattr(snapshot_dict, "ema_fast", 0.0),
                        "ema_slow": getattr(snapshot_dict, "ema_slow", 0.0)
                    }
            
            # Calculate body percentage for the chart
            body_pct = 0.0
            if candles and len(candles) >= 2:
                last_candle = candles[-1]
                body_pct = abs(last_candle.close - last_candle.open) / last_candle.open * 100 if last_candle.open > 0 else 0.0
            
            # Call OKX Premium 3-panel chart generator (BLOCKING CPU OPERATION)
            filepath = generate_entry_chart_sync(
                symbol=symbol,
                timeframe=timeframe,
                side=str(side).replace("SignalType.", ""),
                candles=candles,
                indicators=indicators,
                body_pct=body_pct
            )
            
            if filepath is None:
                logger.error("[CRITICAL-CHART] OKX Premium chart generator returned None - chart generation failed")
                return None
            
            logger.info(f"[CRITICAL-CHART] OKX Premium 3-panel chart generated successfully at {filepath}")
            return filepath
            
        except Exception as e:
            logger.exception(f"[CRITICAL-CHART] FAILED to generate OKX Premium 3-panel chart for {symbol}: {e}")
            return None

    def _generate_and_emit_chart(self, symbol, timeframe, side, entry_price, candles, snapshot_dict, loop):
        """
        Runs synchronously in a separate thread. Coordinates chart generation and event emission.
        """
        try:
            # Offload blocking CPU operations to synchronous function
            filepath = self._sync_render_chart_process(symbol, timeframe, side, candles, snapshot_dict)
            
            if filepath is None:
                return
            
            # Emit CHART_GENERATED event back to async loop
            asyncio.run_coroutine_threadsafe(
                self._emit_chart_event(symbol, timeframe, str(side).replace("SignalType.", ""), filepath),
                loop
            )
            
        except Exception as e:
            logger.exception(f"[CRITICAL-CHART] FAILED to coordinate chart generation for {symbol}: {e}")

    async def _emit_chart_event(self, symbol, timeframe, side, filepath):
        """Helper to emit the generated chart back to the main loop."""
        await self.event_bus.publish(Event(
            event_type=EventTopic.CHART_GENERATED,
            data={
                "symbol": symbol,
                "timeframe": timeframe,
                "side": side,
                "photo_path": filepath
            },
            source="chart_service"
        ))
