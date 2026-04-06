# 💬 KARA Telegram Bot Commands

Full reference for all Telegram commands to control KARA bot.

---

## 🔌 Setup

First, enable Telegram notifications:

```bash
# 1. Create bot via @BotFather
# 2. Get your chat ID via @userinfobot
# 3. Update .env:

TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=123456789

# 4. Restart bot
python main.py
```

---

## 📋 Commands Reference

### General

#### `/start`
Shows help menu with all available commands.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💜 KARA Financial Bot
Trading Signals for Hyperliquid

Available Commands:
/status  - Bot & market status
/pos     - Show positions
/pnl     - Daily & lifetime PnL
/signal  - Latest signals
/pause   - Pause trading
/resume  - Resume trading
...
```

---

### Trading Status

#### `/status`
Current bot & market conditions.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 BOT STATUS

Mode: PAPER (testnet)
Execution: SEMI_AUTO (manual confirm)
Active Positions: 2/3
Paused: ❌ NO

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 ACCOUNT

Balance: $1,024.50
Used Margin: $410.00
Available: $614.50
Unrealized PnL: +$24.50 (+2.39%)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 MARKETS

BTC-USD:  $42,150  ↑ +1.2%  Trending
ETH-USD:  $2,310   ↑ +0.8%  Ranging

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

#### `/pos` or `/positions`
Show all active positions with details.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 POSITIONS (2)

▸ BTC-USD LONG
  Entry: $42,000 | Current: $42,150
  Size: 0.004 BTC (≈ $169)
  PnL: +$6.00 (+3.55%)
  Margin: $56.33 (3x)
  TP1: $43,680 | TP2: $45,360
  SL: $40,950

▸ ETH-USD SHORT
  Entry: $2,350 | Current: $2,310
  Size: 0.1 ETH (≈ $231)
  PnL: +$4.00 (+1.73%)
  Margin: $77.00 (3x)
  TP1: $2,214 | TP2: $2,078
  SL: $2,413

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

#### `/pnl`
Daily and total profit/loss summary.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 PnL SUMMARY

📅 TODAY
  Realized: +$12.50 (3 trades)
  Unrealized: +$10.00
  Total: +$22.50 (+2.25%)
  Win Rate: 66.7% (2/3 wins)

📊 THIS WEEK
  Realized: +$87.30
  Best Trade: +$18.50
  Worst Trade: -$5.00
  Total: +$87.30 (+8.73%)

📅 ALL TIME
  Realized: +$412.10
  Trades: 68 (Win: 42, Loss: 26)
  Win Rate: 61.8%
  Avg Win: +$9.81
  Avg Loss: -$6.25
  Profit Factor: 1.95x

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### Signals & Trading

#### `/signal` or `/signals`
Show latest trading signals with scores & reasons.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 LATEST SIGNALS

◆ SIGNAL #1234 (PENDING)
  Asset: BTC-USD
  Side: LONG
  Score: 76/100 🟢 STRONG

  Breakdown:
  • OI+Funding: +22 (extreme funding, crowding)
  • Liquidation: +19 (shorts near resistance)
  • Orderbook: +17 (bid imbalance detected)
  • Session: +8 (NY session bonus)
  • Regime: ×1.0 (trending)

  Entry: $42,100 (current: $42,150)
  TP1: $43,744 | TP2: $45,388
  SL: $40,998

  Risk/Reward: 1:2.3
  Size: 0.0043 BTC

  ✅ APPROVE | ❌ REJECT

◆ SIGNAL #1233 (FILLED ✓)
  Asset: ETH-USD
  Side: SHORT
  Score: 68/100 🟡 MODERATE
  Status: EXECUTED (entry filled at $2,350)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

#### `/approve <signal_id>`
Approve a pending signal for execution (semi-auto mode).

```
Usage: /approve 1234

Response:
✅ Signal #1234 approved!
Executing BTC LONG at $42,150...
Order placed: market order
Filled: 0.0043 BTC @ $42,135
Current PnL: +$0.60
```

#### `/reject <signal_id>`
Reject a pending signal (skip this trade).

```
Usage: /reject 1234

Response:
❌ Signal #1234 rejected.
(Practice: This was a strong signal, good luck next time!)
```

---

### Bot Control

#### `/pause`
Pause trading (bot still monitors, no new trades).

```
Response:
⏸️ Trading paused.
Bot will NOT execute new signals.
Existing positions remain open.
Use /resume to restart.
```

#### `/resume`
Resume trading after pause.

```
Response:
▶️ Trading resumed!
Bot will process new signals.
Last signal: PENDING
Ready for new opportunities.
```

#### `/stop`
Graceful shutdown — closes positions & stops bot.

```
Response:
⚠️ Initiating graceful shutdown...
Closing 2 open positions...
  • BTC-USD: Closed at $42,200 (+$6.50)
  • ETH-USD: Closed at $2,308 (+$4.20)

Total realized: +$10.70
Bot stopping in 5 seconds...
👋 Goodbye!
```

---

### Configuration

#### `/auto [on|off]`
Toggle full-auto mode. **⚠️ Use with caution!**

```
Usage: /auto on

Response:
⚠️ Full-auto mode ENABLED
Signals with score >= 72 will auto-execute.
Lower scores still need manual approval.

Max 1 position per asset (safety limit).
Kill-switch: 6% daily loss.
```

#### `/leverage [1-5]`
Set default leverage for new trades.

```
Usage: /leverage 4

Response:
📊 Leverage updated: 3x → 4x
New trades will use 4x leverage.
Current positions unaffected.
Note: Higher leverage = higher risk!
```

#### `/risk [0.5-3]`
Set risk percentage per trade.

```
Usage: /risk 1.5

Response:
💰 Risk updated: 1.0% → 1.5% per trade
Daily loss limit: 4.5% (1.5% × 3 trades)
Position sizes will be adjusted.
```

#### `/session [ny|london|asia|all]`
Change session bias for scoring.

```
Usage: /session ny

Response:
📍 Session filter: NY (13:00-21:00 UTC)
Only trading during NY session bonus hours.
Other sessions: -5pt penalty.
Use /session all to disable.
```

---

### Notifications & Settings

#### `/notify [signals|trades|both|off]`
Choose which events to notify about.

```
Usage: /notify trades

Response:
🔔 Notifications: TRADES ONLY
You'll get notified when:
  ✓ Trades execute / close
  ✓ TP1/TP2 hit
  ✗ New signals (silenced)
Use /notify all to enable all alerts.
```

#### `/testmessage`
Test if Telegram connection is working.

```
Response:
✅ Connection successful!
Bot is ready to send notifications.
```

---

### Debugging & Logs

#### `/logs [N]`
Show last N lines of bot log.

```
Usage: /logs 10

Response:
2025-04-02 10:15:23 [INFO] Signal generated: BTC LONG score=76
2025-04-02 10:15:24 [INFO] Awaiting user confirmation...
2025-04-02 10:16:45 [INFO] User approved signal #1234
2025-04-02 10:16:46 [INFO] Order placed: market order
...
```

#### `/debug [on|off]`
Toggle debug logging (verbose).

```
Usage: /debug on

Response:
🔧 DEBUG mode ON
You'll see extra technical details.
Log level: DEBUG
Warning: Very verbose!
Use /debug off to disable.
```

#### `/config`
Show current bot configuration.

```
Response:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚙️  BOT CONFIGURATION

Mode: PAPER
Execution: SEMI_AUTO
Leverage: 3x (max 5x)
Risk/Trade: 1%
Daily Loss Limit: 3%

Signal Threshold: 55
Auto-Trade Threshold: 72
Cooldown: 15 min

Assets: [BTC, ETH]
Max Positions: 3

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 📊 Signal Approval Workflow

### Semi-Auto Mode (DEFAULT & RECOMMENDED)

```
1. Signal generated (score = 76)
                ↓
2. Telegram notification sent
   "BTC LONG score 76 - APPROVE?"
                ↓
3. User has 5 minutes to decide
   /approve 1234  →  Execute
   /reject 1234   →  Skip
   (No action after 5 min: AUTO-REJECT)
                ↓
4. Order executed or rejected
```

### Full-Auto Mode (Advanced)

```
1. Signal generated (score = 75)
                ↓
2. If score >= 72: AUTO-EXECUTE
   If score < 72:  ASK USER
                ↓
3. Trade executes immediately
   Telegram notification sent
```

---

## 💡 Example Workflow

### Day 1: Start Bot

```bash
$ python main.py

# Terminal shows:
💜 KARA Bot starting — Mode: PAPER
...
Dashboard running at http://localhost:8000
```

### Telegram Notifications Arrive

```
09:15 | ✨ New Signal!
BTC-USD LONG | Score: 72 🟢 STRONG
Entry: $42,100 | TP1: $43,744 | SL: $40,998
Risk: 1% ($10) | Reward: 1.6% ($16.50)

👉 /approve or /reject
   Expires in: 5 min

09:16 | You: /approve 1234
✅ Approved! Executing now...

09:17 | 🎯 TRADE EXECUTED
BTC LONG filled at $42,110
Margin: $140 (3x)
Current PnL: +$0.40

09:25 | 📊 TP1 HIT!
Closed 50% at $43,750 (+$43.00 realized)
SL moved to breakeven.
Remaining 50% trailing.

10:45 | 📊 CLOSED
Final closed at $43,680
Total realized: +$74.30
```

---

## ⚙️ Quick Reference Table

| Command | Args | Response Time | Notes |
|---------|------|----------------|-------|
| `/status` | — | < 1s | No actions |
| `/pos` | — | < 1s | Info only |
| `/pnl` | — | < 1s | Info only |
| `/signal` | — | < 1s | Info only |
| `/approve` | signal_id | < 2s | **Executes trade** |
| `/reject` | signal_id | < 1s | **Cancels trade** |
| `/pause` | — | < 1s | Stops new trades |
| `/resume` | — | < 1s | Resumes trading |
| `/auto` | on/off | < 1s | **Changes mode** |
| `/leverage` | 1-5 | < 1s | Changes leverage |
| `/risk` | 0.5-3 | < 1s | Changes risk |
| `/stop` | — | 5-10s | **Shuts down bot** |

---

## 🆘 Common Issues

### "No response from bot"
- Check Telegram token in .env
- Run bot, check if Telegram is connected
- Try `/testmessage`

### "Signal never expires"
- Signals auto-reject after 5 minutes if no action
- Override: use `/approve` or `/reject` explicitly

### "Approve failed"
- Signal may have already expired
- Try `/signal` to get latest signals
- Alert admin if persists

---

## 🔐 Safety Notes

- **Never share chat ID or token** with anyone
- **Telegram messages are NOT encrypted** by default
- **Only approve signals you understand**
- **Full-auto mode: Start with score threshold 75+**
- **Review bot.log daily** for suspicious activity

---

**Happy trading! 💜**
