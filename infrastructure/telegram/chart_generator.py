import os
import uuid
from typing import List, Dict, Any, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from loguru import logger

def generate_entry_chart_sync(
    symbol: str, 
    timeframe: str, 
    side: str, 
    candles: List[Any], 
    indicators: Dict[str, Any],
    body_pct: float
) -> Optional[str]:
    """
    Tạo chart ngầm và xuất ra file .png. Được gọi trong ThreadPool để không block event loop.
    
    Args:
        symbol: Cặp giao dịch
        timeframe: Khung thời gian
        side: LONG hoặc SHORT
        candles: Danh sách tối đa 120 nến cuối cùng (OHLCV objects)
        indicators: Từ điển chứa các chỉ báo của nến cuổi cùng (ADX, EMA9, EMA21, ...)
        body_pct: Phần trăm thân nến
        
    Returns:
        Đường dẫn tuyệt đối tới file ảnh (.png), hoặc None nếu có lỗi.
    """
    try:
        if not candles:
            return None

        # Convert candles to DataFrame
        data = []
        for c in candles:
            data.append({
                "timestamp": getattr(c, "timestamp", 0),
                "open": getattr(c, "open", 0.0),
                "high": getattr(c, "high", 0.0),
                "low": getattr(c, "low", 0.0),
                "close": getattr(c, "close", 0.0),
            })
            
        df = pd.DataFrame(data)
        if df.empty:
            return None
            
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        
        # Re-calculate EMA mathematically using pandas
        df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
        
        # Create subplots
        fig = make_subplots(
            rows=2, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.03, 
            row_heights=[0.75, 0.25]
        )
        
        # Tầng 1: Candlestick
        fig.add_trace(go.Candlestick(
            x=df['datetime'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
            increasing_line_color='#26a69a', 
            decreasing_line_color='#ef5350',
            name="Price"
        ), row=1, col=1)
        
        # Tầng 1: EMAs
        fig.add_trace(go.Scatter(
            x=df['datetime'], y=df['EMA9'], 
            line=dict(color='blue', width=1.5), 
            name='EMA 9'
        ), row=1, col=1)
        
        fig.add_trace(go.Scatter(
            x=df['datetime'], y=df['EMA21'], 
            line=dict(color='orange', width=1.5), 
            name='EMA 21'
        ), row=1, col=1)
        
        exact_adx = indicators.get("adx", 0.0)
        last_dt = df["datetime"].iloc[-1]
        last_dt_str = last_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Tầng 2: Điểm ADX
        fig.add_trace(go.Scatter(
            x=[last_dt_str], y=[exact_adx], 
            mode='markers', marker=dict(color='#e91e63', size=10),
            name='ADX Current'
        ), row=2, col=1)
        
        # Kẻ đường 25 nét đứt
        fig.add_hline(y=25, line_dash="dash", line_color="red", row=2, col=1)
        
        last_idx = df.index[-1]
        entry_price = df.loc[last_idx, "close"]
        arrow_color = "#26a69a" if side.upper() == "LONG" else "#ef5350"
        ay_offset = 60 if side.upper() == "LONG" else -60
        
        # Annotation Box
        fig.add_annotation(
            x=last_dt_str,
            y=entry_price,
            xref="x", yref="y",
            text=f"🎯 VÀO {side.upper()} HERE!<br>ĐK1: EMA Cross<br>ĐK3: Thân nến {body_pct:.2f}%",
            showarrow=True,
            arrowhead=1,
            arrowsize=2,
            arrowwidth=2,
            arrowcolor=arrow_color,
            ax=0,
            ay=ay_offset,
            bgcolor="rgba(0,0,0,0.8)",
            bordercolor=arrow_color,
            borderwidth=2,
            borderpad=4,
            font=dict(color="white", size=12)
        )
        
        # ADX Annotation
        fig.add_annotation(
            x=last_dt_str,
            y=exact_adx,
            xref="x2", yref="y2",
            text=f"🔹 ĐK2: ADX={exact_adx:.1f} -> ĐẠT",
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowwidth=2,
            arrowcolor="#e91e63",
            ax=-50,
            ay=-30,
            bgcolor="rgba(0,0,0,0.8)",
            bordercolor="#e91e63",
            font=dict(color="white", size=10)
        )
        
        fig.update_layout(
            template='plotly_dark',
            title=f"{symbol} ({timeframe}) - {side.upper()} SIGNAL",
            xaxis_rangeslider_visible=False,
            margin=dict(l=50, r=50, t=50, b=50),
            showlegend=False
        )
        
        fig.update_xaxes(showgrid=False, zeroline=False)
        fig.update_yaxes(showgrid=True, gridcolor='rgba(255,255,255,0.1)', zeroline=False)
        
        os.makedirs("data/temp_charts", exist_ok=True)
        file_path = os.path.abspath(f"data/temp_charts/signal_{uuid.uuid4().hex[:8]}_{symbol}_{timeframe}.png")
        
        fig.write_image(file_path, width=1280, height=850, engine="kaleido")
        logger.info(f"[CHART] Successfully generated chart at {file_path}")
        return file_path
        
    except Exception as e:
        logger.error(f"[CHART] Failed to generate chart for {symbol}: {e}", exc_info=True)
        return None
