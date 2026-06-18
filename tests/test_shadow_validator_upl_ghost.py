import sys
import os
import asyncio
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.position.shadow_validator import ShadowValidator
from services.position.exchange_mirror import MirrorPosition

class MockPositionEngine:
    def __init__(self):
        self.positions = []
        
    def get_active_positions(self):
        return self.positions

class MockExchangeMirror:
    def __init__(self):
        self.positions = {}
        
    def get_all_positions(self):
        return self.positions

class MockLocalPos:
    def __init__(self, symbol, pnl, status="OPENED"):
        self.symbol = symbol
        self.pnl = pnl
        self.status = status

async def main():
    print("--- Khởi tạo môi trường Test ---")
    pos_engine = MockPositionEngine()
    exchange_mirror = MockExchangeMirror()
    
    validator = ShadowValidator(pos_engine, exchange_mirror)
    
    print("\n--- Kịch bản 1: Lệch Ghost Position (Old=0, New=1) ---")
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
    
    # Run validation (should print Ghost warning)
    print("Dự kiến: Báo cáo Lệch Ghost BTC-USDT-SWAP")
    validator._run_validation()
    
    print("\n--- Kịch bản 2: Test UplLastPx vs Upl (Tránh bẫy PnL) ---")
    pos_engine.positions = [
        MockLocalPos("ETH-USDT-SWAP", 50.0)
    ]
    
    # Mirror có upl=10.0 (theo markPx, bị lệch xa)
    # Nhưng uplLastPx=50.0 (giống hệt Local, không được báo lệch)
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
    
    print("Dự kiến: KHÔNG báo cáo lệch PnL cho ETH vì uplLastPx khớp với Local")
    validator._run_validation()
    
    print("\n--- Kịch bản 3: Lệch PnL thực sự ---")
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
    print("Dự kiến: Báo cáo lệch PnL cho SOL vì Diff > 0.5")
    validator._run_validation()
    
    print("\n--- Test kết thúc ---")

if __name__ == "__main__":
    asyncio.run(main())
