# 🚀 KARA Setup Guide

Complete step-by-step setup for paper (testnet) and live (mainnet) trading.

---

## 📋 Prerequisites

- **Python 3.12+** — [Download](https://www.python.org/downloads/)
- **Git** — For cloning repo
- **Hyperliquid Account** — [Sign up](https://app.hyperliquid.xyz)
- **Telegram Account** (optional) — For notifications
- **$10-100 testnet funds** — Start small!

---

## ✅ Step 1: Clone & Virtual Environment

### Linux / macOS

```bash
# Clone
git clone <repo-url>
cd kara-bot

# Create venv
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
# Clone
git clone <repo-url>
cd kara-bot

# Create venv
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

---

## 🔑 Step 2: Get Hyperliquid Credentials

### For Testnet (Paper Trading — Recommended First!)

1. Go to: https://app.hyperliquid.xyz
2. Login / Sign up
3. Switch to **Testnet** (upper left dropdown)
4. Request testnet funds:
   - Click "Fund" button
   - Request **50 USDC** (free)
   - Wait 1-2 minutes
5. Get your credentials:
   - Copy **Wallet Address** (starts with 0x)
   - Go to Settings → API Keys
   - Generate new key, **export Private Key** (keep safe!)

### For Mainnet (Live Trading)

⚠️ **Only do this AFTER testing paper mode for 1+ week!**

1. Deposit **real money** to Hyperliquid
2. Follow same steps as testnet
3. Use **same credentials format**

---

## 🔐 Step 3: Configure .env

```bash
# Copy template
cp .env.example .env

# Edit with your credentials
nano .env  # Linux/Mac
# or
notepad .env  # Windows
```

**Fill in:**

```ini
# MODE: "paper" for testnet, "live" for mainnet
KARA_MODE=paper

# Hyperliquid credentials (from step 2)
HL_WALLET_ADDRESS=0x1234...
HL_PRIVATE_KEY=0xabcd...

# Telegram (optional for now)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Leave others as default
KARA_FULL_AUTO=false
DASHBOARD_PORT=8000
```

**Safety Check:**
```bash
# Verify .env format
cat .env | grep -E "HL_|KARA_MODE"

# Make sure .env is in .gitignore
grep ".env" .gitignore
```

---

## 🎮 Step 4A: First Run — Paper Mode

**Paper mode = simulated trading, NO real money risk**

```bash
# Run bot (uses KARA_MODE=paper from .env)
python main.py

# You should see:
# ✓ Connecting to Hyperliquid testnet...
# ✓ WebSocket client initialized
# ✓ Scoring engine ready
# ✓ Dashboard running at http://localhost:8000
```

**Open dashboard:**
- Browser: http://localhost:8000
- Check "Account" section — should show $1000 paper balance
- Check "Market" section — should show BTC/ETH prices
- Check "Status" section — should say "READY"

**Test scoring:**
```bash
# In another terminal, tail logs
tail -f kara.log | grep -i signal

# Should see signals every 15-60 minutes
# Format: [SIGNAL] BTC LONG score=72 ...
```

---

## 📱 Step 5: Setup Telegram (Optional)

### Get Telegram Bot Token

1. Open Telegram, search **@BotFather**
2. Send: `/newbot`
3. Follow prompts:
   - Name: `KARA Bot`
   - Username: `kara_botXXX_bot` (must end with _bot)
4. Copy token (looks like 1234567:ABCdef...)

### Get Your Chat ID

1. Search **@userinfobot**
2. Send any message
3. Copy your User ID

### Update .env

```ini
TELEGRAM_BOT_TOKEN=1234567:ABCdef...
TELEGRAM_CHAT_ID=123456789
```

### Test Connection

```bash
python -c "
import asyncio
from notify.telegram import KaraTelegram

async def test():
    tg = KaraTelegram()
    await tg.send_message('✓ KARA Telegram connected!')

asyncio.run(test())
"
```

---

## 📊 Step 6A: Backtest (Before Live!)

Test your strategy on historical data:

```bash
python -c "
from backtest.backtester import Backtester

# Run 2024 backtest
bt = Backtester(
    start='2024-01-01',
    end='2024-12-31',
    initial_capital=1000,
    leverage=3
)
results = bt.run()

print(f'Win Rate: {results.win_rate:.1%}')
print(f'Profit Factor: {results.profit_factor:.2f}')
print(f'Max Drawdown: {results.max_drawdown:.1%}')
print(f'Total Return: {results.total_return:.1%}')
"
```

**Good targets:**
- Win rate: 55%+
- Profit factor: 1.5+
- Max drawdown: < 10%

---

## 🎯 Step 7: Configuration Tuning

Edit `config.py` to adjust:

```python
# Risk Management
RISK.default_leverage = 3x  # 1-5x
RISK.risk_per_trade_pct = 0.01  # 1% per trade
RISK.daily_loss_limit_pct = 0.03  # 3% daily max

# Signal Thresholds
SIGNAL.min_score_to_signal = 55  # Show signal if score >= 55
SIGNAL.min_score_to_auto_trade = 72  # Auto-execute if score >= 72 (full-auto only)
SIGNAL.signal_cooldown_minutes = 15  # Min 15 min between signals

# Session Bonuses
SIGNAL.ny_session_bonus = 8  # Extra points in NY session
SIGNAL.asia_session_penalty = -5  # Penalty in Asia session
```

---

## ✅ Step 8: Live Mode Setup (Advanced!)

**Only after:**
- ✓ 1+ week paper trading
- ✓ Backtest shows profit
- ✓ Fully understand risk management
- ✓ Started with small amount (<$100)

### Switch to Live

1. **Deposit real funds to Hyperliquid mainnet**
   - Go to https://app.hyperliquid.xyz (NOT testnet)
   - Fund wallet with $50-100 (your first amount)

2. **Update .env**
   ```ini
   KARA_MODE=live    # ← Change from "paper"
   HL_WALLET_ADDRESS=0x...  # Mainnet address
   HL_PRIVATE_KEY=0x...     # Mainnet private key
   ```

3. **Keep semi-auto mode (SAFE!)**
   ```ini
   KARA_FULL_AUTO=false
   ```

4. **Run bot**
   ```bash
   python main.py
   ```

5. **For each signal, you MUST:**
   - Check Dashboard/Telegram
   - Review signal reasons
   - Confirm via Dashboard button or `/approve <signal_id>` on Telegram
   - Only then does trade execute

---

## 🐳 Step 9A: Docker Setup

### Local Testing

```bash
# Build image
docker build -t kara-bot .

# Run (paper mode)
docker run \
  -e KARA_MODE=paper \
  --env-file .env \
  -p 8000:8000 \
  kara-bot
```

### Docker Compose (Full Stack)

```bash
# Start bot + Redis
docker-compose up -d

# Check logs
docker-compose logs -f kara-bot

# Stop
docker-compose down
```

---

## 🚢 Step 10: Deploy to Railway / Cloud

### Option A: Railway (Recommended for students)

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login
railway login

# Initialize in project
railway init
# Select: Create new project

# Deploy
railway up

# Set environment variables
railway variable set KARA_MODE=paper
railway variable set HL_WALLET_ADDRESS=0x...
railway variable set HL_PRIVATE_KEY=0x...
railway variable set TELEGRAM_BOT_TOKEN=...
railway variable set TELEGRAM_CHAT_ID=...

# View deployment
railway open
```

### Option B: Docker Hub + Any Hosting

```bash
# Login to Docker Hub
docker login

# Tag image
docker tag kara-bot myusername/kara-bot:latest

# Push
docker push myusername/kara-bot:latest

# Deploy on:
# - AWS ECS
# - Google Cloud Run
# - Azure Container Instances
# - Heroku
# - etc.
```

---

## 🧪 Verification Checklist

After setup, verify everything works:

```bash
# 1. Imports work
python verify_imports.py
# Result: ✓ All imports successful!

# 2. Config loads
python -c "import config; print(f'Mode: {config.MODE}')"
# Result: Mode: paper

# 3. Hyperliquid connects
python -c "
import asyncio
from data.hyperliquid_client import HyperliquidClient
async def test():
    hl = HyperliquidClient()
    await hl.connect()
    price = await hl.get_price('BTC')
    print(f'BTC Price: ${price:,.0f}')
asyncio.run(test())
"

# 4. Dashboard accessible
curl http://localhost:8000/health
# Result: {"status": "ok"}

# 5. Telegram works (if enabled)
python -c "
import asyncio
from notify.telegram import KaraTelegram
async def test():
    tg = KaraTelegram()
    await tg.send_message('✓ Setup complete!')
asyncio.run(test())
"
```

---

## 🆘 Troubleshooting

### "ModuleNotFoundError: No module named 'config'"
```bash
# Make sure you're in the right directory
cd /path/to/kara-bot

# Reinstall dependencies
pip install -r requirements.txt
```

### "Connection refused: Hyperliquid"
```bash
# Check KARA_MODE
cat .env | grep KARA_MODE
# Should be "paper" for testnet

# Verify credentials
python -c "
from config import HL_WALLET_ADDRESS, HL_PRIVATE_KEY
print(f'Address: {HL_WALLET_ADDRESS[:10]}...')
print(f'Private key set: {bool(HL_PRIVATE_KEY)}')
"
```

### "Dashboard returns 500 error"
```bash
# Check logs
tail -50 kara.log | grep error

# Restart bot
pkill -f main.py
sleep 2
python main.py
```

### "Telegram not sending messages"
```bash
# Verify token & chat ID
cat .env | grep TELEGRAM

# Test manually
python -c "
import asyncio
from notify.telegram import KaraTelegram
async def test():
    tg = KaraTelegram()
    result = await tg.send_message('Test')
    print(result)
asyncio.run(test())
"
```

---

## 📚 Next Steps

1. **Run paper mode** for 1 week
2. **Monitor signals** daily — are they profitable?
3. **Backtest** with same parameters
4. **If good results**, start live with small amount
5. **Scale slowly** — increase size 10% per week if profitable
6. **Keep logs** — review trades daily

---

## 📞 Support

- See **README.md** for full documentation
- Check **TELEGRAM.md** for bot commands
- Review logs: `tail -f kara.log`
- Issues: GitHub Issues

---

**🎓 Remember: Start small, test thoroughly, never risk more than you can afford to lose!**
