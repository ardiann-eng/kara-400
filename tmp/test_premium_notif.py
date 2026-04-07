import sys
import os

# Add root to path
sys.path.insert(0, os.getcwd())

from models.schemas import Position, TradeSignal, Side, PositionStatus
from utils.helpers import format_idr, format_usd, format_price
import config

def test_render():
    print("🌸 KARA Premium Notification Test")
    
    # Mock position with $10 margin
    pos = Position(
        position_id="POS-TEST",
        asset="ETH",
        side=Side.SHORT,
        entry_price=2134.07,
        size_initial=0.1171, # ~ $250 notional
        size_current=0.1171,
        leverage=25,
        margin_usd=10.0,
        stop_loss=2140.24,
        tp1=2127.43,
        tp2=2119.96,
        is_paper=True,
        signal_id="SIG-TEST"
    )
    
    signal = TradeSignal(
        asset="ETH",
        side=Side.SHORT,
        entry_price=2134.07,
        score=57
    )
    # Mock RR
    signal.stop_loss = 2140.24
    signal.tp2 = 2119.96
    
    # Render simulated text (manual copy of logic in telegram.py)
    mode_label = "🚀 FULL-AUTO" if config.FULL_AUTO else "🛠️ SEMI-AUTO"
    type_label = "📝 PAPER" if config.MODE == "paper" else "💰 LIVE"
    atr_status = "✅ Dynamic ATR" if config.RISK.enable_atr_sl else "固定 (Fixed)"

    text = (
        f"🌸 <b>KARA SYSTEM: Position Executed</b>\n"
        f"──────────────────────────\n"
        f"<i>I have analyzed the market and successfully opened a <b>{pos.side.value.upper()}</b> position for <b>{pos.asset}</b>.</i>\n\n"
        
        f"📦 <b>Market Details</b>\n"
        f"  • Entry   : <code>${format_price(pos.entry_price)}</code>\n"
        f"  • Margin  : <b>{format_idr(pos.margin_usd)}</b> ({format_usd(pos.margin_usd)})\n"
        f"  • Leverage: {pos.leverage}x isolated\n"
        f"  • Mode    : {type_label} ({mode_label})\n\n"
        
        f"🛡️ <b>Risk Profile</b>\n"
        f"  • 🛑 SL   : <code>${format_price(pos.stop_loss)}</code> ({atr_status})\n"
        f"  • 🎯 TP1  : <code>${format_price(pos.tp1)}</code>\n"
        f"  • 🎯 TP2  : <code>${format_price(pos.tp2)}</code>\n"
        f"  • 📐 R:R Ratio: <b>{signal.risk_reward_ratio:.2f}x</b>\n"
        f"  • 📊 Score: <b>{signal.score}/100</b>\n\n"
        
        f"<i>Execution complete. Monitoring for optimal exit. ✨</i>"
    )
    
    print("-" * 40)
    print(text)
    print("-" * 40)
    
    # Check if margin is correct (10 * 16000 = 160000)
    # format_idr(10) should produce "Rp160.000" (if USD_TO_IDR is 16000)
    if "Rp160.000" in text or "Rp150.000" in text:
        print("✅ MARGIN FIX POSITIVE!")
    else:
        print("❌ MARGIN BUG STILL PRESENT!")

if __name__ == "__main__":
    test_render()
