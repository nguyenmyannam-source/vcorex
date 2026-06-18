import os
import uuid
import pytest
from unittest.mock import patch, MagicMock
from infrastructure.telegram.chart_generator import generate_entry_chart_sync

class MockCandle:
    def __init__(self, t, o, h, l, c, v):
        self.timestamp = t
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v

@pytest.fixture
def sample_candles():
    import time
    candles = []
    t = int(time.time() * 1000)
    for i in range(120):
        candles.append(MockCandle(t - (120-i)*300000, 100 + i, 105 + i, 95 + i, 102 + i, 1000))
    return candles

def test_generate_entry_chart_sync_success(sample_candles):
    indicators = {'adx': 32.5, 'ema9': 102.0, 'ema21': 100.0}
    
    # Mock kaleido write_image to avoid actual heavy export during unit testing
    with patch("plotly.graph_objs.Figure.write_image") as mock_write_image:
        path = generate_entry_chart_sync('FIL-USDT-SWAP', '5m', 'LONG', sample_candles, indicators, 1.25)
        
        assert path is not None
        assert "FIL-USDT-SWAP" in path
        assert "5m" in path
        assert path.endswith(".png")
        mock_write_image.assert_called_once()
        
def test_generate_entry_chart_sync_empty_candles():
    path = generate_entry_chart_sync('FIL-USDT-SWAP', '5m', 'LONG', [], {}, 1.25)
    assert path is None

def test_generate_entry_chart_sync_exception_handling(sample_candles):
    # Force an exception during dataframe creation or plotting
    with patch("pandas.DataFrame", side_effect=Exception("Mocked DataFrame Error")):
        path = generate_entry_chart_sync('FIL-USDT-SWAP', '5m', 'LONG', sample_candles, {}, 1.25)
        assert path is None
