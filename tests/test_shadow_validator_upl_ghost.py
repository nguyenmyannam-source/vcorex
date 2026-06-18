import re
import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.position.shadow_validator import ShadowValidator
from services.position.exchange_mirror import MirrorPosition
from services.position.models import PositionStatus

class MockPositionEngine:
    def __init__(self):
        self.positions = []
        
    def get_active_positions(self):
        return self.positions

class MockExchangeMirror:
    def __init__(self):
        self.positions = {}
        
    async def get_all_positions(self):
        return self.positions

class MockLocalPos:
    def __init__(self, symbol, pnl, status=PositionStatus.OPENED):
        self.symbol = symbol
        self.pnl = pnl
        self.status = status

@pytest.mark.asyncio
async def test_ghost_position_old_0_new_1():
    pos_engine = MockPositionEngine()
    exchange_mirror = MockExchangeMirror()
    
    validator = ShadowValidator(pos_engine, exchange_mirror)
    
    pos_engine.positions = []
    exchange_mirror.positions = {
        "BTC-USDT-SWAP": MirrorPosition(
            instId="BTC-USDT-SWAP",
            pos=1.0,
            avgPx=50000.0,
            upl=150.5,
            uplLastPx=150.5,
            uplRatio=0.0,
            margin=0.0,
            markPx=50150.0,
            liqPx=0.0,
            cTime=0,
            uTime=0
        )
    }
    
    with patch.object(logger, 'warning') as mock_logger_warning:
        await validator._run_validation()
    
        assert mock_logger_warning.call_count == 2
        warning_messages = [call_args[0][0].strip() for call_args in mock_logger_warning.call_args_list]
        print(f"Captured warning messages: {warning_messages}") # For debugging
        
        expected_msg_1 = '[SHADOW DIFF] Position Count: Old=0, New=1'
        expected_msg_2 = f"[SHADOW DIFF] Ghost: exchange has BTC-USDT-SWAP (UPL=150.5) but local OPENED missing — reconciliation will heal"
        
        print(f"Expected message 1: {repr(expected_msg_1)} (len: {len(expected_msg_1)})") # For debugging
        print(f"Expected message 2: {repr(expected_msg_2)} (len: {len(expected_msg_2)})") # For debugging
        
        found_msg_1 = False
        found_msg_2 = False
        for msg in warning_messages:
            print(f"Comparing captured: {repr(msg)} (len: {len(msg)}) with expected.") # For debugging
            if msg == expected_msg_1:
                found_msg_1 = True
            if msg == expected_msg_2:
                found_msg_2 = True
        
        assert found_msg_1
        assert found_msg_2

@pytest.mark.asyncio
async def test_upl_last_px_match_prevents_pnl_diff_warning():
    pos_engine = MockPositionEngine()
    exchange_mirror = MockExchangeMirror()
    
    validator = ShadowValidator(pos_engine, exchange_mirror)
    
    pos_engine.positions = [
        MockLocalPos("ETH-USDT-SWAP", 50.0)
    ]
    
    exchange_mirror.positions = {
        "ETH-USDT-SWAP": MirrorPosition(
            instId="ETH-USDT-SWAP",
            pos=1.0,
            avgPx=2000.0,
            upl=10.0,          # Mark Price PnL
            uplLastPx=50.0,    # Last Price PnL (Match with Local)
            uplRatio=0.0,
            margin=0.0,
            markPx=2010.0,
            liqPx=0.0,
            cTime=0,
            uTime=0
        )
    }
    
    with patch.object(logger, 'debug') as mock_logger_debug:
        await validator._run_validation()
        
        mock_logger_debug.assert_not_called() # No PnL diff warning expected

@pytest.mark.asyncio
async def test_real_pnl_diff_reports_warning():
    pos_engine = MockPositionEngine()
    exchange_mirror = MockExchangeMirror()
    
    validator = ShadowValidator(pos_engine, exchange_mirror)
    
    pos_engine.positions = [
        MockLocalPos("SOL-USDT-SWAP", 20.0)
    ]
    exchange_mirror.positions = {
        "SOL-USDT-SWAP": MirrorPosition(
            instId="SOL-USDT-SWAP",
            pos=1.0,
            avgPx=100.0,
            upl=25.0,
            uplLastPx=25.0, # Lệch so với 20.0 local
            uplRatio=0.0,
            margin=0.0,
            markPx=125.0,
            liqPx=0.0,
            cTime=0,
            uTime=0
        )
    }
    
    with patch.object(logger, 'debug') as mock_logger_debug:
        await validator._run_validation()
        
        mock_logger_debug.assert_called_once()
        assert "[SHADOW DIFF] PnL Lệch pha SOL-USDT-SWAP" in mock_logger_debug.call_args[0][0]

@pytest.mark.asyncio
async def test_position_count_diff_old_12_new_0():
    pos_engine = MockPositionEngine()
    exchange_mirror = MockExchangeMirror()
    
    validator = ShadowValidator(pos_engine, exchange_mirror)
    
    # Simulate 12 local positions
    pos_engine.positions = [MockLocalPos(f"SYM-{i}-USDT-SWAP", 10.0) for i in range(12)]
    
    # Simulate 0 exchange positions
    exchange_mirror.positions = {}
    
    with patch.object(logger, 'warning') as mock_logger_warning:
        await validator._run_validation()
        
        mock_logger_warning.assert_called_once_with(
            f"[SHADOW DIFF] Position Count: Old=12, New=0"
        )