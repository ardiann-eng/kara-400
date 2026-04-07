import sys
import os
import asyncio
import logging

# Add root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from risk.risk_manager import RiskManager
from data.hyperliquid_client import HyperliquidClient
import config

async def test_atr():
    print("🌸 KARA ATR Verification")
    rm = RiskManager()
    
    # Mock candles for predictable ATR
    # TR1: 110-105 = 5
    # TR2: Max(115-110, |115-108|, |110-108|) = 7
    # TR3: Max(120-112, |120-114|, |112-114|) = 8
    mock_candles = [
        {"h": 110, "l": 105, "c": 108},
        {"h": 115, "l": 110, "c": 114},
        {"h": 120, "l": 112, "c": 118}
    ]
    
    # tr1 = 115-110 = 5, |115-108|=7, |110-108|=2 -> TR=7
    # tr2 = 120-112 = 8, |120-114|=6, |112-114|=2 -> TR=8
    # Average = (7+8)/2 = 7.5
    
    atr = rm.calculate_atr(mock_candles)
    print(f"✓ Mock ATR (Expected 7.5): {atr}")
    
    if atr == 7.5:
        print("✅ ATR POSITIVE!")
    else:
        print("❌ ATR FAILED!")

if __name__ == "__main__":
    asyncio.run(test_atr())
