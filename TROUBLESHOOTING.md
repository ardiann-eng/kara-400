# 🐛 KARA Bot — Troubleshooting Guide

## Common Issues & Solutions

### ❌ Issue: `IndexError: list index out of range` at Hyperliquid Info initialization

**Cause**: Hyperliquid SDK initialization failed (usually network issue or SDK version mismatch)

**Solutions** (in order):

#### Solution 1: Test Components in Isolation ✅

```bash
python test_components.py
```

This tests all components without needing Hyperliquid connection.

Expected output:
```
🧪 KARA Bot — Minimal Test Mode

[1/5] Testing imports...
      ✅ All core imports successful
[2/5] Testing configuration...
      ✅ Configuration loaded
[3/5] Testing Risk Manager...
      ✅ Risk Manager works
[4/5] Testing data schemas...
      ✅ Schemas valid
[5/5] Testing optional components...
      ✅ Optional components checked

✅ ComponentTest Complete!
```

**If this passes**: Problem is Hyperliquid API connectivity, go to Solution 2
**If this fails**: Check Python installation, see "Environment" section below

---

#### Solution 2: Test Connection Only 🔌

```bash
python test_connection.py
```

This tests if Hyperliquid API is reachable.

Expected output:
```
🧪 KARA Connection Test

1️⃣  Checking imports...
   ✅ Imports successful
2️⃣  Checking configuration...
   Mode: paper
   Testnet: True
   Wallet: NOT SET...
3️⃣  Initializing Hyperliquid client...
4️⃣  Connecting to Hyperliquid...
5️⃣  Testing API call (get_mark_price)...
   ✅ BTC Price: $42,150.00
6️⃣  Testing funding rate...
   ✅ BTC Funding Rate: 0.0103%

✅ ALL TESTS PASSED!
```

**If this passes**: Core system works, try full bot
**If test 3-5 fails**: Hyperliquid API issue (see "Network Issues" below)

---

#### Solution 3: Verify Environment Setup

Check .env file:

```bash
# Should exist
ls -la .env

# Should have these minimum settings
cat .env | grep -E "KARA_MODE|HL_WALLET"

# Expected output:
# KARA_MODE=paper
# HL_WALLET_ADDRESS=0x... (or empty is OK for paper)
```

If .env missing:
```bash
cp .env.example .env
```

---

### ❌ Issue: Connection refused / Network error

**Cause**: Can't reach Hyperliquid API servers

#### Quick Fixes:

1. **Check internet**:
   ```bash
   ping 8.8.8.8  # Google DNS
   ```

2. **Check firewall**:
   - Are you behind a corporate firewall?
   - Does your network block foreign ports?
   - Try using a VPN

3. **Check Hyperliquid status**:
   - Is testnet up? https://app.hyperliquid-testnet.xyz
   - Is mainnet up? https://app.hyperliquid.xyz

4. **Check your DNS**:
   ```bash
   nslookup api.hyperliquid-testnet.xyz
   # Should resolve to an IP
   ```

5. **Try with explicit DNS**:
   ```bash
   # Windows
   ipconfig /flushdns

   # Mac/Linux
   sudo dscacheutil -flushcache
   ```

---

### ❌ Issue: "ModuleNotFoundError"

**Cause**: Python dependencies not installed

#### Fix:

```bash
# Reinstall everything
pip install --upgrade pip
pip install -r requirements.txt

# If that fails, try:
pip install -r requirements.txt --force-reinstall
```

---

### ❌ Issue: Dashboard won't load (http://localhost:8000)

**Possible causes**:

#### Check 1: Is bot running?
```bash
# Should show "Dashboard running at http://localhost:8000"
tail -20 kara.log | grep -i dashboard
```

#### Check 2: Port in use?
```bash
# Windows
netstat -ano | findstr :8000

# Mac/Linux
lsof -i :8000

# If port in use, either:
# 1. Kill process
# 2. Change DASHBOARD_PORT in .env
```

#### Check 3: Firewall blocking?
```
Windows Firewall → Allow Python through
```

#### Fix: Restart bot
```bash
# Kill existing process
pkill -f "python main.py"
sleep 2

# Restart
python main.py
```

---

### ❌ Issue: Telegram not sending

**Cause**: Invalid token or chat ID

#### Fix:

1. **Verify token & chat ID in .env**:
   ```bash
   cat .env | grep TELEGRAM
   ```

2. **Get new credentials**:
   - Token: Talk to @BotFather, `/newbot`
   - Chat ID: Message @userinfobot, copy ID

3. **Test manually**:
   ```bash
   python -c "
   import asyncio
   from notify.telegram import KaraTelegram

   async def test():
       tg = KaraTelegram()
       await tg.send_message('Test message')

   asyncio.run(test())
   "
   ```

---

### ❌ Issue: "Skip WS" or WebSocket errors

**Cause**: WebSocket timeout, network unstable

#### Fix:

1. **Restart bot**:
   ```bash
   pkill -f "python main.py"
   python main.py
   ```

2. **Check network**:
   ```bash
   ping api.hyperliquid-testnet.xyz
   # Should get responses
   ```

3. **Enable debug logging**:
   ```bash
   LOG_LEVEL=DEBUG python main.py | tee debug.log
   ```

---

## Environment Verification Checklist

```bash
# 1. Python version
python --version
# Should be 3.12+

# 2. Pip version
pip --version
# Latest recommended

# 3. Virtual environment active?
which python
# Should show path to venv

# 4. Dependencies installed?
pip list | grep -E "hyperliquid|fastapi|pydantic"
# Should show all packages

# 5. Config file exists?
[ -f .env ] && echo "OK" || echo "MISSING"

# 6. Can reach Hyperliquid?
curl -s https://api.hyperliquid-testnet.xyz/info | head
# Should get JSON response

# 7. Port 8000 available?
python -c "import socket; s=socket.socket(); s.bind(('', 8000)); print('OK')"
# Should print OK
```

---

## Diagnostic Script

Run this all-in-one checker:

```bash
#!/bin/bash
echo "🔍 KARA Diagnostic Check"
echo "========================"

echo "1. Python..."
python --version || echo "FAIL: Python not found"

echo "2. Pip..."
pip --version || echo "FAIL: Pip not found"

echo "3. Venv..."
which python | grep venv || echo "WARNING: Not in venv"

echo "4. Dependencies..."
pip list | grep -q pydantic && echo "OK: pydantic" || echo "FAIL: pydantic missing"
pip list | grep -q fastapi && echo "OK: fastapi" || echo "FAIL: fastapi missing"
pip list | grep -q hyperliquid && echo "OK: hyperliquid" || echo "FAIL: hyperliquid missing"

echo "5. Config..."
[ -f .env ] && echo "OK: .env exists" || echo "FAIL: .env missing (copy .env.example)"

echo "6. Network..."
curl -s https://api.hyperliquid-testnet.xyz/info > /dev/null && echo "OK: Can reach API" || echo "FAIL: No API connection"

echo "7. Port 8000..."
python -c "import socket; s=socket.socket(); s.bind(('', 8000)); s.close(); print('OK: Port available')" 2>/dev/null || echo "FAIL: Port 8000 in use"

echo "========================"
echo "✅ Check complete!"
```

---

## Emergency Procedures

### Hard Reset

```bash
# Stop all Python processes
pkill -9 -f "python.*main.py"

# Clear cache
rm -rf __pycache__ */__pycache__

# Fresh reinstall
pip install --upgrade -r requirements.txt --force-reinstall

# Restart
python main.py
```

### Rollback to Known-Good State

```bash
# If you modified config.py
git checkout config.py

# If you modified main.py
git checkout main.py

# If you're really stuck
git stash
git pull origin main
python main.py
```

### Get Help

**Before asking for help, run all of these and save output:**

```bash
# System info
python test_components.py > test_components.log 2>&1
python test_connection.py > test_connection.log 2>&1

# Last 100 log lines
tail -100 kara.log > kara_last.log

# Environment
cat .env | grep -v PRIVATE_KEY > env_check.txt  # Don't share private key!

# Python info
python -c "import sys; print(f'Python {sys.version}\nVersion: {sys.version_info}')" > python_info.txt

# Zip for sharing
zip -r support_logs.zip test_*.log kara_last.log env_check.txt python_info.txt
```

Then share `support_logs.zip` with support

---

## FAQ

**Q: Should I use paper or live mode?**
A: ALWAYS start with paper (testnet). It's free and safe for testing.

**Q: What if I don't have credentials?**
A: Paper mode works without credentials (simulated trading). Add them later when going live.

**Q: Can I test without internet?**
A: No, bot needs Hyperliquid API connection. But you can use `test_components.py` locally.

**Q: How do I know if bot is running correctly?**
A: Check dashboard: http://localhost:8000 and logs: `tail -f kara.log`

**Q: What if errors don't match these?**
A: Google the error, check stack trace, or run diags above.

---

## Support Resources

- 📖 README.md — Overview & features
- 🔧 SETUP.md — Installation guide
- 💬 TELEGRAM.md — Bot commands
- 📋 This file — Troubleshooting
- 📊 kara.log — Detailed logs
- 🧪 test_components.py — Component testing
- 🔌 test_connection.py — API testing

---

**Still stuck? Check the logs first: `tail -100 kara.log`**
