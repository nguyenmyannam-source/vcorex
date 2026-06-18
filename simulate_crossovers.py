import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone

from core.config.settings import settings
from services.market_data.indicators import IndicatorPipeline, EMACalculator
from services.market_data.candle_buffer import CandleBuffer, FrozenOHLCV
from infrastructure.exchange.base_exchange import OHLCV

async def fetch_ohlcv(session, symbol, tf, limit=1440):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={tf}&limit={limit}"
    async with session.get(url) as resp:
        data = await resp.json()
        if data["code"] == "0":
            return data["data"]
        else:
            raise Exception(data)

async def main():
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]
    strategy_timeframes = settings.active_timeframes if hasattr(settings, "active_timeframes") else ["5m", "15m", "1H"]
    
    pipeline = IndicatorPipeline()
    
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    since = int(seven_days_ago.timestamp() * 1000)
    
    total_candles = 0
    total_crossovers = 0
    adx_rejects = 0
    body_rejects = 0
    valid_entries = 0
    
    # map okx timeframe
    tf_map = {
        "5m": "5m",
        "15m": "15m",
        "1H": "1H",
        "4H": "4H"
    }
    
    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            for tf in strategy_timeframes:
                try:
                    # Fetch candles
                    bar = tf_map.get(tf, tf)
                    ohlcvs_raw = await fetch_ohlcv(session, symbol, bar, limit=2016)
                    # okx returns newest first
                    ohlcvs_raw.reverse()
                    
                    total_candles += len(ohlcvs_raw)
                    buffer = CandleBuffer(symbol, tf, max_candles=3000)
                    
                    for i, c in enumerate(ohlcvs_raw):
                        candle = OHLCV(
                            timestamp=int(c[0]), open=float(c[1]), high=float(c[2]), 
                            low=float(c[3]), close=float(c[4]), volume=float(c[5]),
                            symbol=symbol, timeframe=tf, confirmed=True
                        )
                        buffer.add_candle(candle, reseed=True)
                        
                        if i > 50:
                            closes = buffer.get_close_prices(500)
                            highs = buffer.get_high_prices(500)
                            lows = buffer.get_low_prices(500)
                            
                            if len(closes) < pipeline.min_candles:
                                continue
                                
                            fast_series = EMACalculator.calculate_series(closes, pipeline.fast_period)
                            slow_series = EMACalculator.calculate_series(closes, pipeline.slow_period)
                            if len(fast_series) >= 3 and len(slow_series) >= 3:
                                fast_completed = fast_series[-2]
                                fast_prev = fast_series[-3]
                                slow_completed = slow_series[-2]
                                slow_prev = slow_series[-3]

                                bullish = fast_prev <= slow_prev and fast_completed > slow_completed
                                bearish = fast_prev >= slow_prev and fast_completed < slow_completed
                                
                                if bullish or bearish:
                                    total_crossovers += 1
                                    
                                    adx = 0
                                    if len(closes) >= 15:
                                        from services.market_data.indicators import ADXCalculator
                                        adx = ADXCalculator.calculate(highs, lows, closes, 14)
                                    
                                    ref_candle = buffer.get_candles(3)[-2]
                                    body_pct = 0
                                    candle_range = ref_candle.high - ref_candle.low
                                    if candle_range > 0:
                                        body_pct = abs(ref_candle.close - ref_candle.open) / candle_range
                                    
                                    min_adx = settings.adx_min_threshold_all
                                    min_body = settings.min_body_percentage / 100
                                    
                                    rejected_adx = adx < min_adx
                                    rejected_body = body_pct < min_body
                                    
                                    if rejected_adx:
                                        adx_rejects += 1
                                    elif rejected_body:
                                        body_rejects += 1
                                    else:
                                        valid_entries += 1
                                        
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"Error {symbol} {tf}: {e}")
                
    print(f"Candles processed: {total_candles}")
    print(f"Crossovers: {total_crossovers}")
    print(f"ADX Rejects: {adx_rejects}")
    print(f"Body Rejects: {body_rejects}")
    print(f"Valid Entries: {valid_entries}")

if __name__ == '__main__':
    asyncio.run(main())
