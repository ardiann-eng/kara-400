# 🌸 KARA — Intelligence Trading Partner

**KARA-400** is a professional-grade, multi-user automated trading engine for **Hyperliquid Perps**, featuring a premium Telegram interface, real-time IDR localization, and an integrated AI Knowledge Base.

---

## ✨ Key Features

- **👥 Multi-User Architecture**: Isolated trading sessions per user. Each subscriber gets their own balance, risk settings, and position management.
- **🇮🇩 IDR Localization**: All Telegram notifications (`/status`, `/pos`, `/journal`) are displayed in Indonesian Rupiah (IDR) using real-time USD/IDR conversion.
- **🚀 Dual Trading Modes**:
    - **Standard**: Secure, swing-oriented trading with structured Risk/Reward.
    - **Scalper**: Ultra-aggressive, high-leverage mode for rapid market cycles.
- **🧠 AI Knowledge Base**: Integrated `KNOWLEDGE_BASE.md` that serves as the "source of truth" for the bot's logic, risk math, and strategy.
- **📱 Premium Telegram UI**: Interactive cards with detailed "Alasan KARA" (AI analysis), [Take Trade] buttons, and real-time PnL updates.
- **📊 Web Dashboard**: Modern, glassmorphism-style dashboard for real-time market monitoring and admin oversight.
- **🛡️ Advanced Risk Management**: Per-user position sizing, drawdown kill-switches, and automatic stop-loss adjustments.

---

## 🛠️ Architecture

KARA is built for scalability and performance:
- **Core**: Python 3.10+ (Asyncio-driven).
- **Persistence**: JSON/SQLite based storage with Persistent Volume support for Cloud hosting.
- **API**: Hyperliquid Python SDK (Testnet/Mainnet support).
- **Communication**: python-telegram-bot v21+.
- **Dashboard**: FastAPI + TailwindCSS + Lightweight-Charts for the web UI.

---

## 🚀 Quick Start (Deployment on Railway)

KARA is optimized for **Railway** deployment.

1. **Fork this repository** (ensure it is **Private**).
2. **Setup Environment Variables**:
   ```env
   TELEGRAM_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_admin_id
   HL_PRIVATE_KEY=your_wallets_private_key
   USD_TO_IDR=15850
   DB_PATH=/app/storage/kara_data.db
   STORAGE_DIR=/app/storage
   ```
3. **Mount a Volume**: On Railway, mount a persistent volume at `/app/storage` to ensure your trade history and user data survive restarts.
4. **Deploy**: KARA will automatically detect the environment and start the Telegram Bot + Dashboard.

---

## 📜 Commands

- `/start`: Register and initialize a new paper trading account with Rp1.000.000.
- `/status`: Check your equity, daily PnL, and current drawdown in IDR.
- `/pos`: View active positions with interactive buttons to close or manage.
- `/mode`: Switch between **Standard** and **Scalper** strategies.
- `/journal`: Detailed summary of your performance (Trade Journal).
- `/live`: (Admin) Toggle between Paper and Live trading modes.

---

## ⚠️ Disclaimer

Trading futures involves significant risk. KARA is provided as-is. Use the **Paper Trading** mode extensively before committing real capital.

---

**Built with 💜 by Antigravity AI for the KARA-400 Project.**
