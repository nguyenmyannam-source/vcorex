import asyncio
import io
import os
from datetime import datetime, timezone
import pandas as pd
from loguru import logger

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic


# Import matplotlib before mplfinance to configure it
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend for headless environments
import mplfinance as mpf

class ChartService:
    """
    Listens for approved trading signals and generates a professional candlestick chart 
    using mplfinance without blocking the async event loop.
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
                
            # Capture event loop reference BEFORE entering thread
            loop = asyncio.get_running_loop()
            
            # Run rendering in thread pool to avoid blocking async loop
            await asyncio.to_thread(
                self._generate_and_emit_chart,
                symbol, timeframe, side, entry_price, candles, snapshot_dict, loop
            )
            
        except Exception as e:
            logger.error(f"ChartService: Failed to process approved signal - {e}")

    def _generate_and_emit_chart(self, symbol, timeframe, side, entry_price, candles, snapshot_dict, loop):
        """
        Runs synchronously in a separate thread. Generates the chart image.
        """
        try:
            # 1. Prepare DataFrame for mplfinance
            df_data = []
            for c in candles:
                # Convert timestamp from ms to datetime
                ts = c.timestamp / 1000.0 if c.timestamp > 1e10 else c.timestamp
                df_data.append({
                    "Date": pd.to_datetime(ts, unit='s', utc=True),
                    "Open": c.open,
                    "High": c.high,
                    "Low": c.low,
                    "Close": c.close,
                    "Volume": c.volume
                })
            df = pd.DataFrame(df_data)
            df.set_index("Date", inplace=True)
            
            # 2. Extract indicators
            ema_fast_val = None
            ema_slow_val = None
            adx_val = None
            
            if snapshot_dict:
                if isinstance(snapshot_dict, dict):
                    ema_fast_val = snapshot_dict.get("ema_fast")
                    ema_slow_val = snapshot_dict.get("ema_slow")
                    adx_val = snapshot_dict.get("adx")
                else:
                    ema_fast_val = getattr(snapshot_dict, "ema_fast", None)
                    ema_slow_val = getattr(snapshot_dict, "ema_slow", None)
                    adx_val = getattr(snapshot_dict, "adx", None)
            
            # Since we only have the snapshot values, we approximate the EMA lines
            # by calculating them directly on the DataFrame so mplfinance can plot them
            # For a proper chart, we need the lines.
            
            # Calculate EMA 9 and 21 for plotting
            fast_period = 9
            slow_period = 21
            df['EMA_Fast'] = df['Close'].ewm(span=fast_period, adjust=False).mean()
            df['EMA_Slow'] = df['Close'].ewm(span=slow_period, adjust=False).mean()
            
            # 3. Setup mplfinance plot
            side_str = str(side).replace("SignalType.", "")
            color_up = '#26A69A'  # TradingView Green
            color_down = '#EF5350' # TradingView Red
            
            # Custom style for dark mode, similar to TradingView
            mc = mpf.make_marketcolors(
                up=color_up, down=color_down,
                edge='inherit',
                wick='inherit',
                volume='in',
                ohlc='inherit'
            )
            s = mpf.make_mpf_style(
                marketcolors=mc,
                base_mpf_style='nightclouds',
                gridstyle='--',
                y_on_right=True
            )
            
            # Title with conditions
            title_parts = [
                f"{symbol} | {timeframe} | {side_str}",
                "Conditions: EMA Crossover | ADX > 25 | Body > Min%"
            ]
            if ema_fast_val and ema_slow_val and adx_val:
                title_parts.append(f"EMA9: {ema_fast_val:.4f} | EMA21: {ema_slow_val:.4f} | ADX: {adx_val:.1f}")
            title = "\n".join(title_parts)

            # Add EMA lines to the plot
            addplots = [
                mpf.make_addplot(df['EMA_Fast'], color='#2962FF', width=1.5), # Blue
                mpf.make_addplot(df['EMA_Slow'], color='#FF6D00', width=1.5)  # Orange
            ]
            
            # Add entry point marker
            if entry_price:
                marker_df = pd.Series(index=df.index, dtype=float)
                marker_df.iloc[-1] = entry_price
                marker_color = '#00E676' if 'BUY' in side_str or 'LONG' in side_str else '#FF1744'
                marker_type = '^' if 'BUY' in side_str or 'LONG' in side_str else 'v'
                addplots.append(mpf.make_addplot(marker_df, type='scatter', markersize=150, marker=marker_type, color=marker_color))
            
            # Save to buffer
            buf = io.BytesIO()
            mpf.plot(
                df,
                type='candle',
                style=s,
                volume=True,
                addplot=addplots,
                title=title,
                figsize=(12, 7),
                tight_layout=True,
                savefig=dict(fname=buf, format='png', dpi=120, bbox_inches='tight')
            )
            buf.seek(0)
            
            # Save file locally for debug/record
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{symbol}_{timeframe}_{side_str}_{timestamp_str}.png".replace(":", "-").replace("/", "-")
            filepath = os.path.join(self._save_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(buf.getvalue())
            
            logger.info(f"Chart generated and saved to {filepath}")
            
            # Emit CHART_GENERATED event back to async loop
            asyncio.run_coroutine_threadsafe(
                self._emit_chart_event(symbol, timeframe, side_str, filepath),
                loop
            )
            
        except Exception as e:
            logger.error(f"ChartService thread failed: {e}")

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
