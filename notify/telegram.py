"""
KARA Bot - Telegram Notification + Command Handler 
Uses python-telegram-bot v21+ (async-native).
Commands: /start /status /pos /pnl /pause /resume /stop /auto /manual
          /signal /backtest /help
"""

from __future__ import annotations
import asyncio
import os
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

import config
from core.db import user_db
from models.schemas import (
    TradeSignal, AccountState, SignalStrength, Side, BotMode,
    ExecutionMode
)
from utils.helpers import format_usd, format_idr, format_pct, format_price, utcnow

log = logging.getLogger("kara.telegram")


# ──────────────────────────────────────────────
# KARA's personality strings 
KARA_GREETING = """
 <b>Halo! Aku KARA</b> - asisten trading futures-mu! 

Aku di sini buat bantu kamu trade di Hyperliquid dengan aman dan cerdas~ 
Ingat, kita prioritaskan <b>keamanan modal</b> dulu ya! 

<i>Mode: {mode} | Eksekusi: {exec_mode}</i>
"""

SIGNAL_TEMPLATE = """
{side_emoji} <b>SINYAL {asset} - {strength}</b>
--------------------
Score: <b>{score}/100</b>
Arah: <b>{side_text}</b>
Regime: <code>{regime}</code>
--------------------
 Entry: <code>${entry}</code>
🛑 Stop Loss: <code>${sl}</code> ({sl_pct:.1f}%)
 TP1: <code>${tp1}</code> (+{tp1_pct:.1f}%) -> 40%
 TP2: <code>${tp2}</code> (+{tp2_pct:.1f}%) -> 35%
⚡ Leverage: <b>{lev}x isolated</b>
📐 R:R = <b>{rr:.1f}x</b>
--------------------
<i>ID: {sig_id}</i>
"""

PYRAMID_TEMPLATE = """
📐 <b>PYRAMID SCALE-IN — {asset}</b>
<i>KARA menemukan peluang tambah posisi (pyramid)</i>
--------------------
Existing Profit: <b>+{profit_pct:.2f}%</b> ✅
Side: <b>{side_text}</b> {side_emoji}
Score Baru: <b>{score}/100</b>
--------------------
📍 Entry Tambahan: <code>${entry}</code>
⚡ Leverage: <b>{lev}x</b>
--------------------
<i>Konfirmasi diperlukan untuk pyramid scaling.</i>
<i>ID: {sig_id}</i>
"""


class KaraTelegram:
    """Telegram bot for KARA - notifications + commands."""

    def __init__(self, on_confirm: Optional[Callable] = None):
        """
        on_confirm: async callback(signal_id) when user confirms a trade
        """
        self._app: Optional[Application] = None
        self._on_confirm = on_confirm
        self._pending_signals: Dict[str, TradeSignal] = {}  # sig_id -> signal
        self._bot_started = False
        self._authorized_chat_ids: Set[str] = set()
        self._state_file = config.TG_STATE_PATH

        # Injected later by main.py
        self.bot_app:      Any = None   # KaraBot instance to access get_session
        self.hl_client:    Any = None   # for live price lookup in /pos
        self.mode_manager: Any = None   # ModeManager for /scalper /standard
        
        self._load_state()

    def _load_state(self):
        import os
        import json
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                    self._authorized_chat_ids = set(data.get("authorized_chat_ids", []))
            except Exception as e:
                log.error(f"Failed to load state: {e}")
        
        # Add from config if not present
        if config.TELEGRAM_CHAT_ID:
            chat_id_str = str(config.TELEGRAM_CHAT_ID)
            if chat_id_str not in self._authorized_chat_ids:
                self._authorized_chat_ids.add(chat_id_str)

    def _save_state(self):
        import json
        try:
            with open(self._state_file, "w") as f:
                json.dump({"authorized_chat_ids": list(self._authorized_chat_ids)}, f)
        except Exception as e:
            log.error(f"Failed to save state: {e}")

    # ──────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────

    async def start(self):
        if not config.TELEGRAM_TOKEN:
            log.warning("  No Telegram token - notifications disabled")
            return

        try:
            self._app = (
                Application.builder()
                .token(config.TELEGRAM_TOKEN)
                .build()
            )

            # Register handlers
            handlers = [
                CommandHandler("start",    self.cmd_start),
                CommandHandler("help",     self.cmd_help),
                CommandHandler("status",   self.cmd_status),
                CommandHandler("pos",      self.cmd_positions),
                CommandHandler("positions",self.cmd_positions),
                CommandHandler("pnl",      self.cmd_pnl),
                CommandHandler("export",   self.cmd_export),
                CommandHandler("mode",     self.cmd_mode),       # show current mode
                CommandHandler("scalper",  self.cmd_scalper),    # switch to scalper
                CommandHandler("standard", self.cmd_standard),   # switch to standard
                CommandHandler("paper",    self.cmd_paper),      # force paper mode
                CommandHandler("live",     self.cmd_live),       # upgrade to live mode
                CallbackQueryHandler(self.on_callback),
            ]
            for h in handlers:
                self._app.add_handler(h)

            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True, timeout=30)
            self._bot_started = True
            log.info(" Telegram bot started")

            # Register command menu (shows up when user types "/" in chat)
            try:
                await self._app.bot.set_my_commands(
                    commands=[
                        BotCommand("status",   "Status akun & saldo virtual"),
                        BotCommand("mode",     "Ganti gaya trading"),
                        BotCommand("pos",      "Posisi terbuka saat ini"),
                        BotCommand("pnl",      "Ringkasan PnL & Equity"),
                        BotCommand("paper",    "Kembali ke Paper Mode & Reset Saldo"),
                        BotCommand("live",     "Setup Live Mode (Agent Wallet)"),
                        BotCommand("help",     "Daftar instruksi lengkap"),
                        BotCommand("export",   "Export riwayat trade ke Excel"),
                    ],
                    scope=BotCommandScopeDefault()
                )
                log.info("✅ Telegram command menu registered")
            except Exception as e:
                log.warning(f"Could not register command menu: {e}")

            await self.send_text(
                f"✨ <b>KARA Online!</b> 🌸\n\n"
                f"Intelligence Trading Partner Anda siap memantau 100+ aset.\n"
                f"Mode: <code>{'PAPER ' if config.HL_TESTNET else 'LIVE '}</code>\n"
            )

        except Exception as e:
            log.warning(f"  Telegram initialization failed: {e}")
            log.warning("   Bot will continue without Telegram notifications")
            log.warning("   To fix: Get valid token from @BotFather on Telegram")
            self._app = None
            self._bot_started = False
            # Continue anyway - Telegram is optional

    async def stop(self):
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                log.debug(f"Error stopping Telegram: {e}")
            finally:
                self._app = None
                self._bot_started = False

    # ──────────────────────────────────────────
    # COMMANDS
    # ──────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        username = update.effective_user.username or update.effective_user.first_name or "Master"
        
        # Multi-user Init
        user = user_db.get_user(chat_id)
        if not user:
            # Create new user state
            user = user_db.create_user(chat_id, username, init_usd=config.PAPER_BALANCE_USD)
            if self.bot_app:
                # Force session init
                self.bot_app.get_session(chat_id)
            
        # Ensure ID always in state allowed
        if chat_id not in self._authorized_chat_ids:
            self._authorized_chat_ids.add(chat_id)
            self._save_state()
            
        exec_mode = "🤝 Semi-Auto" if not config.FULL_AUTO else "🚀 Full-Auto"
        
        reply_msg = (
            f"✨ <b>Halo, {username}! Saya KARA, Intelligence Trading Partner Anda.</b> 🌸\n\n"
            f"Selamat datang! Saya sudah menyiapkan akun virtual Anda untuk trading di Hyperliquid perp.\n\n"
            f"💰 <b>Saldo Paper Anda: {format_idr(user.paper_balance_usd)}</b>\n\n"
            f"Ketik /help untuk instruksi awal. Anda bisa mengetik /mode untuk memilih gaya trading (Standard / Scalper)."
        )
        await update.message.reply_html(reply_msg)

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        help_text = (
            "💜 <b>KARA Bot — Menu Lengkap</b>\n"
            "\n"
            "🎛️ <b>Mode Trading & Akun</b>\n"
            "/paper    — Kembali ke Paper Mode & Reset saldo ke Rp1.000.000\n"
            "/live     — Sambungkan KARA ke wallet asli Anda!\n"
            "/mode     — Tampilkan menu mode: Standard / Scalping\n"
            "\n"
            "📊 <b>Informasi Portofolio</b>\n"
            "/status   — Status bot, ekuitas, dan float\n"
            "/pos      — Lihat daftar koin yang sedang jalan (Tombol Close) \n"
            "/pnl      — Ringkasan PnL\n"
            "/export   — Download riwayat trade Excel\n"
            "\n"
            "💡 <i>Kirim pesan apa saja untuk mengobrol jika ada ChatGPT terhubung!</i>"
        )
        await update.message.reply_html(help_text)

    async def cmd_paper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return await self.cmd_start(update, ctx)
        
        user.config.bot_mode = BotMode.PAPER
        user.paper_balance_usd = config.PAPER_BALANCE_USD
        user_db.update_user(user)
        
        if self.bot_app:
            # Drop current session and reload fresh
            self.bot_app.sessions.pop(chat_id, None)
            self.bot_app.get_session(chat_id)
            
        await update.message.reply_html(
            "🌸 <b>Kembali ke Paper Mode!</b>\n\n"
            "Saldo virtual Anda telah direset kembali menjadi <b>Rp1.000.000</b>. Mari kita belajar hasilkan profit lagi!"
        )

    async def cmd_live(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        
        text = (
            "⚡ <b>UPGRADE KE LIVE MODE</b> ⚡\n\n"
            "KARA dapat menggunakan <b>Agent Wallet (L1)</b> di Hyperliquid, sehingga kamu <b>TIDAK PERLU</b> memberikan Private Key dompet utamamu ke bot ini.\n\n"
            "Ikuti langkah ini:\n"
            "1. Buka <a href='https://app.hyperliquid.xyz/API'>Halaman API Hyperliquid</a>\n"
            "2. Hubungkan wallet utamamu.\n"
            "3. Buat dan izinkan Agent Wallet baru.\n"
            "4. Kirimkan alamat agen dan secret key ke bot melalui DM ke admin. (Otomatisasi akan datang segera!)\n\n"
            "<i>Untuk sekarang API key setup harus via enviroment. Bot is currently under construction.</i>"
        )
        await update.message.reply_html(text)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        try:
            acc = session.get_account_state()
            risk_status = session.risk_mgr.status

            mode_str  = " PAPER" if acc.mode == BotMode.PAPER else " LIVE"
            pause_str = "⏸️ PAUSED" if acc.is_paused else "▶️ Active"
            kill_str  = "🚨 KILL SWITCH ON" if acc.kill_switch_active else " Normal"

            pnl_sign = "+" if acc.unrealized_pnl > 0 else ""
            daily_sign = "+" if acc.daily_pnl > 0 else ""
            auto_str = "🚀 Full-Auto" if config.FULL_AUTO else "🛡️ Semi-Auto"
            pos_len = len([p for p in acc.positions if p.status.value == 'open'])

            text = (
                f"🌸 <b>KARA System Status</b>\n\n"
                f"💜 <b>Profil & Eksekusi</b>\n"
                f"  • Mode: <b>{mode_str.strip()}</b> ({auto_str})\n"
                f"  • Status: {pause_str}\n"
                f"  • Kill-Switch: {kill_str.strip()}\n\n"
                f"📊 <b>Kondisi Dana</b>\n"
                f"  • Ekuitas: <code>{format_idr(acc.total_equity)}</code> (NAV)\n"
                f"  • Saldo Dompet: <code>{format_idr(acc.wallet_balance)}</code>\n"
                f"  • Saldo Tersedia: <code>{format_idr(acc.available)}</code>\n"
                f"  • Float PnL: <b>{pnl_sign}{format_idr(acc.unrealized_pnl)}</b>\n\n"
                f"📈 <b>Performa Harian (Total)</b>\n"
                f"  • Profit Hari Ini: <b>{daily_sign}{format_idr(acc.daily_pnl)}</b> ({daily_sign}{format_pct(acc.daily_pnl_pct)})\n"
                f"  • Max Drawdown: <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n\n"
                f"🎯 <b>Posisi Terbuka:</b> {pos_len} aset\n\n"
            )

            if risk_status.get("in_cooldown"):
                text += "\n❄️ <i>Post-loss cooldown aktif. Break dulu yaa~</i>"
            else:
                text += "\n<i>Semua sistem beroperasi maksimal, siap tangkap peluang~! ✨</i>"

            await update.message.reply_html(text)
        except Exception as e:
            await update.message.reply_text(f" Error: {e}")

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        positions = session.executor.open_positions
        if not positions:
            await update.message.reply_html(
                "📭 <b>Tidak ada posisi terbuka saat ini.</b>\n"
                "<i>KARA menunggu sinyal yang tepat~ 🌸</i>"
            )
            return

        # Fetch live prices for all open assets
        live_prices = {}
        if self.hl_client:
            for pos in positions:
                if pos.asset not in live_prices:
                    try:
                        live_prices[pos.asset] = await self.hl_client.get_mark_price(pos.asset)
                    except Exception:
                        live_prices[pos.asset] = pos.entry_price

        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        text = f"🎯 <b>Monitoring Posisi ({len(positions)})</b>\n\n"
        keyboard_rows = []
        
        for pos in positions:
            side_emoji = "🟢" if pos.side.value == "long" else "🔴"
            current    = live_prices.get(pos.asset, pos.entry_price)
            unreal_pnl = pos.unrealized_pnl(current)
            float_pct  = pos.floating_pct(current) * 100
            
            pnl_label   = "Profit" if unreal_pnl >= 0 else "Loss"
            pnl_str     = format_idr(unreal_pnl)
            pnl_sign    = "+" if unreal_pnl >= 0 else ""

            if pos.opened_at:
                delta = now_utc - (pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at)
                total_mins = int(delta.total_seconds() // 60)
                duration_str = f"{total_mins // 60}j {total_mins % 60}m" if total_mins >= 60 else f"{total_mins}m"
            else:
                duration_str = "?"

            sl_label = " <i>[BEP]</i>" if pos.tp1_hit else ""
            tp_prog = "⭐⭐ (TP2)" if pos.tp2_hit else ("⭐ (TP1)" if pos.tp1_hit else "⏳ <i>Menunggu TP...</i>")
            trail_str = "🔄 Aktif" if pos.trailing_active else "-"

            text += (
                f"\n{side_emoji} <b>{pos.asset} {pos.side.value.upper()}</b> {pos.leverage}x\n"
                f"   Entry: <code>${format_price(pos.entry_price)}</code> ➪ <b>${format_price(current)}</b>\n"
                f"   {pnl_label}: <b>{pnl_sign}{pnl_str} ({pnl_sign}{float_pct:.2f}%)</b>\n"
                f"   🛑 SL: <code>${format_price(pos.stop_loss)}</code>{sl_label}\n"
                f"   🎯 Target: {tp_prog}\n"
                f"   ⚡ Trail: {trail_str} | ⏱️ {duration_str}\n"
            )

            keyboard_rows.append([
                InlineKeyboardButton(
                    f"❌ Close {pos.asset} {pos.side.value.upper()}", 
                    callback_data=f"close_req:{pos.asset}:{pos.side.value}"
                )
            ])
            
        text += "\n<i>Santai dulu, biarkan profit kita mengalir~ 🌸</i>"

        keyboard_rows.append([
            InlineKeyboardButton("🚨 CLOSE ALL POSITIONS 🚨", callback_data="close_all_req")
        ])

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await update.message.reply_html(text, reply_markup=reply_markup)

    async def cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        try:
            acc = session.get_account_state()
            daily_sign = "+" if acc.daily_pnl >= 0 else ""
            mode_str   = "PAPER 📝" if acc.mode.value == "paper" else "LIVE ⚡"

            text = (
                "💰 <b>KARA PORTFOLIO SUMMARY</b>\n"
                "──────────────────────────\n\n"
                "💎 <b>ACCOUNT & MARGIN</b>\n"
                f"• Ekuitas Total  : <code>{format_idr(acc.total_equity)}</code> (NAV)\n"
                f"• Saldo Dompet   : <code>{format_idr(acc.wallet_balance)}</code>\n"
                f"• Saldo Tersedia : <code>{format_idr(acc.available)}</code>\n\n"
                "📊 <b>PERFORMANCE</b>\n"
                f"• Untung/Rugi    : <code>{format_idr(acc.unrealized_pnl)}</code> (Floating)\n"
                f"• PnL Harian     : <code>{daily_sign}{format_idr(acc.daily_pnl)} ({daily_sign}{format_pct(acc.daily_pnl_pct)})</code>\n"
                f"• Peak Total     : <code>{format_idr(acc.peak_balance)}</code>\n"
                f"• Max Drawdown   : <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n\n"
                "🛡️ <b>SYSTEM STATUS</b>\n"
                f"• Mode Eksekusi  : {mode_str}\n\n"
                "<i>Tetap disiplin dan jaga psikologi trading ya! 🌸</i>"
            )
            await update.message.reply_html(text)
        except Exception as e:
            await update.message.reply_html(f"❌ PnL Error: {e}")

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self.risk_manager:
            self.risk_manager.pause()
        await update.message.reply_html(
            "⏸️ <b>KARA dijeda.</b>\n"
            "Tidak ada trade baru. Posisi aktif tetap dipantau.\n"
            "Ketik /resume untuk lanjutkan~ "
        )

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self.risk_manager:
            self.risk_manager.resume()
        await update.message.reply_html(
            "▶️ <b>KARA aktif kembali!</b>\n"
            "Siap mencari peluang yang bagus untukmu~ "
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_html(
            "🛑 <b>Stop command diterima.</b>\n"
            "KARA akan menutup semua posisi dan berhenti.\n\n"
            "<i>Pastikan ini benar-benar yang kamu inginkan ya! 💙</i>"
        )
        if self.risk_manager:
            self.risk_manager.pause()
        # main.py will handle actual stop via signal

    async def cmd_enable_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_html(
            " <b>Mode Full-Auto</b>\n\n"
            " <b>Perhatian!</b> Dalam mode ini KARA akan:\n"
            "• Auto-execute trade dengan score ≥ 72\n"
            "• Maksimum 1 posisi aktif\n"
            "• Kill-switch otomatis di 20% drawdown\n\n"
            "<i>Ketik /manual untuk kembali ke semi-auto (lebih aman untuk pemula)</i>\n\n"
            "💡 <b>Rekomendasi untuk mahasiswa: gunakan semi-auto dulu!</b>"
        )
        # Note: actual flag change requires restart with KARA_FULL_AUTO=true in .env

    async def cmd_manual(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_html(
            "🤝 <b>Mode Semi-Auto aktif.</b>\n"
            "KARA akan kirim sinyal dan kamu konfirmasi sebelum eksekusi. "
            "Ini cara terbaik untuk belajar! "
        )

    async def cmd_signal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        args  = ctx.args
        asset = args[0].upper() if args else "BTC"
        await update.message.reply_html(
            f" Menganalisis <b>{asset}</b>...\n"
            f"<i>KARA sedang cek OI, funding, liquidation map, orderbook~</i>"
        )
        # scoring_engine will be called from main.py; this just triggers it

    async def cmd_backtest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        args  = ctx.args
        asset = args[0].upper() if args else "BTC"
        await update.message.reply_html(
            f" Backtest untuk <b>{asset}</b> tersedia di dashboard!\n"
            f"Buka: <code>http://localhost:{config.DASHBOARD_PORT}/backtest</code>"
        )

    async def cmd_export(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        
        file_path = config.EXCEL_LOG_PATH
        if not os.path.exists(file_path):
            await update.message.reply_html(
                "❌ <b>Gagal:</b> Belum ada data trade untuk di-export.\n\n"
                "<i>Tunggu sampai ada posisi yang dibuka/ditutup ya!</i>"
            )
            return

        await update.message.reply_chat_action("upload_document")
        try:
            with open(file_path, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"KARA_Trade_History_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    caption=(
                        "📊 <b>KARA Trade History Export</b>\n\n"
                        "Berikut adalah laporan lengkap riwayat trading kamu dalam format Excel.\n"
                        "<i>Gunakan data ini untuk analisis statistik performa.</i>"
                    ),
                    parse_mode="HTML"
                )
        except Exception as e:
            log.error(f"Export failed: {e}")
            await update.message.reply_html(f"❌ <b>Gagal ekspor:</b> {str(e)}")

    # ──────────────────────────────────────────
    # MODE COMMANDS (Standard ↔ Scalper)
    # ──────────────────────────────────────────

    async def cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show current trading mode and allow switching."""
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return
        
        mode = user.config.trading_mode.upper()
        mode_icon = "⚡" if mode == "SCALPER" else "📊"
        
        text = (
            f"🎯 <b>Trading Mode: {mode}</b> {mode_icon}\n\n"
            f"<b>Standard Mode:</b> Swing/Positional. Lebih kalem, score sinyal lebih tinggi, target profit lebih jauh.\n"
            f"<b>Scalper Mode:</b> Ultra-Agresif. Entry/Exit cepat (menit), leverage tinggi, frekuensi trade tinggi.\n\n"
            f"<i>Pilih mode di bawah untuk mengganti gaya trading Anda:</i>"
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Standard Mode", callback_data="mode_switch:standard")],
            [InlineKeyboardButton("⚡ Scalper Mode", callback_data="mode_switch:scalper")]
        ])
        await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_scalper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return
        
        session.user.config.trading_mode = "scalper"
        user_db.update_user(session.user)
        await update.message.reply_html("⚡ <b>Ganti ke Scalper Mode Berhasil!</b>")

    async def cmd_standard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return
        
        session.user.config.trading_mode = "standard"
        user_db.update_user(session.user)
        await update.message.reply_html("📊 <b>Ganti ke Standard Mode Berhasil!</b>")

    # ──────────────────────────────────────────
    # SIGNAL NOTIFICATION
    # ──────────────────────────────────────────

    # ──────────────────────────────────────────
    # SIGNAL NOTIFICATION
    # ──────────────────────────────────────────

    async def send_signal(self, signal: TradeSignal, is_auto: bool = False, target_chat_id: str = None):
        """Send a formatted signal card to Telegram."""
        side_emoji = "🟢" if signal.side == Side.LONG else "🔴"
        side_text  = "LONG" if signal.side == Side.LONG else "SHORT"
        
        text = SIGNAL_TEMPLATE.format(
            side_emoji=side_emoji,
            asset=signal.asset,
            strength=signal.strength.value,
            score=signal.score,
            side_text=side_text,
            regime=signal.regime.value if signal.regime else "NORMAL",
            entry=format_price(signal.entry_price),
            sl=format_price(signal.stop_loss),
            sl_pct=abs(signal.stop_loss / signal.entry_price - 1) * 100,
            tp1=format_price(signal.tp1),
            tp1_pct=abs(signal.tp1 / signal.entry_price - 1) * 100,
            tp2=format_price(signal.tp2),
            tp2_pct=abs(signal.tp2 / signal.entry_price - 1) * 100,
            lev=signal.suggested_leverage,
            rr=signal.risk_reward_ratio,
            sig_id=signal.signal_id[:8]
        )

        keyboard = []
        if not is_auto:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Ambil Trade", callback_data=f"confirm:{signal.signal_id}"),
                    InlineKeyboardButton("⏭️ Lewati", callback_data=f"skip:{signal.signal_id}")
                ],
                [InlineKeyboardButton("📝 Mengapa Sinyal Ini?", callback_data=f"reasons:{signal.signal_id}")]
            ]
            self._pending_signals[signal.signal_id] = signal
        else:
            text += "\n🚀 <b>AUTO-EXECUTED</b>"
            # Match screenshot buttons for auto-trade
            keyboard = [
                [
                    InlineKeyboardButton("🔍 Lihat Alasan KARA", callback_data=f"reasons:{signal.signal_id}"),
                    InlineKeyboardButton("📈 TradingView", url=f"https://app.hyperliquid.xyz/trade/{signal.asset}")
                ]
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self.send_text(text, target_chat_id=target_chat_id, reply_markup=reply_markup)

    # ──────────────────────────────────────────
    # POSITION EVENT NOTIFICATIONS
    # ──────────────────────────────────────────

    async def send_position_opened(self, pos, signal, target_chat_id: str = None):
        """Notification for a successful entry."""
        margin_idr = pos.margin_usd * config.USD_TO_IDR
        
        text = (
            f"🚀 <b>POSISI DIBUKA — {pos.asset}</b>\n\n"
            f"Arah    : {pos.side.value.upper()}\n"
            f"Entry   : <code>${format_price(pos.entry_price)}</code>\n"
            f"Margin  : <b>{format_idr(margin_idr)} ({format_usd(pos.margin_usd)})</b>\n"
            f"Leverage: {pos.leverage}x isolated\n"
            f"🛑 SL   : <code>${format_price(pos.stop_loss)}</code>\n"
            f"🎯 TP1  : <code>${format_price(pos.tp1)}</code>\n"
            f"🎯 TP2  : <code>${format_price(pos.tp2)}</code>\n\n"
            f"Score   : <b>{signal.score}/100</b>\n"
            f"<i>Semoga profit melimpah! ✨</i>"
        )
        await self.send_text(text, target_chat_id=target_chat_id)

    async def send_position_event(self, action: dict, prices: dict, target_chat_id: str = None):
        """Dispatch the right formatted card based on action type."""
        action_type = action.get("action", "")
        pos_id      = action.get("position_id", "")
        pnl         = action.get("pnl", 0.0)

        # Retrieve position from user session for rich context
        session = self.bot_app.get_session(target_chat_id) if (self.bot_app and target_chat_id) else None
        pos = None
        if session and hasattr(session.executor, '_positions'):
            pos = session.executor._positions.get(pos_id)

        if not pos:
            # Fallback to plain message
            msg = action.get("message", "")
            if msg:
                await self.send_text(msg, target_chat_id=target_chat_id)
            return

        current = prices.get(pos.asset, pos.entry_price)
        entry   = pos.entry_price
        side_str = "LONG" if pos.side.value == "long" else "SHORT"
        
        # Calculate PnL % based on initial size
        pnl_pct  = (pnl / (pos.size_initial * entry)) * 100 if (entry and pos.size_initial) else 0
        pnl_sign = "+" if pnl >= 0 else ""

        if action_type == "tp1":
            text = (
                "🎯 <b>TP1 HIT — {asset}</b>\n\n"
                "Arah    : {side}\n"
                "Entry   : <code>${entry:,.4f}</code>\n"
                "TP1     : <code>${tp1:,.4f}</code>\n"
                "Profit  : <b>{sign}{pnl_idr} ({sign}{pct:.2f}%)</b>\n"
                "Sisa    : 60% masih jalan\n"
                "SL      : Digeser ke Entry ✅\n"
            ).format(
                asset=pos.asset, side=side_str, entry=entry,
                tp1=pos.tp1, sign=pnl_sign, pnl_idr=format_idr(pnl), pct=abs(pnl_pct)
            )

        elif action_type == "tp2":
            text = (
                "🎯🎯 <b>TP2 HIT — {asset}</b>\n"
                "──────────────────────────\n"
                "Arah    : {side}\n"
                "Entry   : <code>${entry:,.4f}</code>\n"
                "TP2     : <code>${tp2:,.4f}</code>\n"
                "Profit  : <b>{sign}{pnl_idr} ({sign}{pct:.2f}%)</b>\n"
                "Sisa    : 25% dengan trailing"
            ).format(
                asset=pos.asset, side=side_str, entry=entry,
                tp2=pos.tp2, sign=pnl_sign, pnl_idr=format_idr(pnl), pct=abs(pnl_pct)
            )

        elif action_type == "trailing_stop":
            trail_px = action.get("trail_price", current)
            total_pnl = pnl + pos.pnl_realized
            total_sign = "+" if total_pnl >= 0 else ""
            text = (
                "📍 <b>TRAILING STOP — {asset}</b>\n"
                "──────────────────────────\n"
                "Trailing SL Hit: <code>${trail:,.4f}</code>\n"
                "Profit Total    : <b>{tsign}{pnl_idr}</b>\n"
                "Posisi ditutup  : 100% ✅"
            ).format(
                asset=pos.asset, trail=trail_px,
                tsign=total_sign, pnl_idr=format_idr(total_pnl)
            )

        elif action_type == "stop_loss":
            loss_pct = abs(pnl_pct)
            text = (
                "🛑 <b>STOP LOSS — {asset}</b>\n"
                "──────────────────────────\n"
                "Entry   : <code>${entry:,.4f}</code>\n"
                "SL hit  : <code>${sl:,.4f}</code>\n"
                "Loss    : <b>-{loss_idr} (-{lpct:.2f}%)</b>\n"
                "Modal   : Dilindungi 🛡️"
            ).format(
                asset=pos.asset, entry=entry,
                sl=pos.stop_loss, loss_idr=format_idr(abs(pnl)), lpct=loss_pct
            )

        else:
            msg = action.get("message", "")
            if msg:
                await self.send_text(msg, target_chat_id=target_chat_id)
            return

        await self.send_text(text, target_chat_id=target_chat_id)

    async def send_hourly_summary(self, acc, open_count: int, target_chat_id: str = None):
        """Send automatic hourly PnL summary."""
        daily_sign = "+" if acc.daily_pnl >= 0 else ""
        dd_pct     = acc.current_drawdown_pct
        
        status_str = "🟢 OK"
        if dd_pct >= 15: status_str = "🔴 DANGER"
        elif dd_pct >= 8: status_str = "🟠 WARNING"

        text = (
            "📊 <b>Update Harian KARA</b>\n"
            "──────────────────────────\n"
            f"Ekuitas   : <code>{format_idr(acc.total_equity)}</code>\n"
            f"Daily PnL : <code>{daily_sign}{format_idr(acc.daily_pnl)} ({daily_sign}{format_pct(acc.daily_pnl_pct)})</code>\n"
            f"Posisi    : {open_count} terbuka\n"
            f"Drawdown  : <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n"
            f"Status    : {status_str}\n"
        )
        await self.send_text(text, target_chat_id=target_chat_id)

    async def send_trade_update(self, message: str):
        """Send a plain trade update (fallback)."""
        await self.send_text(message)

    async def send_text(self, message: str, target_chat_id: str = None, reply_markup=None):
        """Send a message. If target_chat_id is set, only to that user. Else broadcast."""
        if not self._app:
            log.info(f"[Telegram disabled] {message}")
            return
            
        chat_ids = [target_chat_id] if target_chat_id else list(self._authorized_chat_ids)
        dead_chats = []
        
        for cid in chat_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=cid,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            except Exception as e:
                log.error(f"Telegram send error for {cid}: {e}")
                if any(x in str(e) for x in ["Chat not found", "Forbidden", "blocked"]):
                    dead_chats.append(cid)
                    
        for dcid in dead_chats:
            if dcid in self._authorized_chat_ids:
                self._authorized_chat_ids.remove(dcid)
        if dead_chats:
            self._save_state()

    # ──────────────────────────────────────────
    # CALLBACK QUERY (button taps)
    # ──────────────────────────────────────────

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query   = update.callback_query
        chat_id = str(update.effective_chat.id)
        
        # Get user session
        session = self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session:
            await query.answer("Sesi tidak ditemukan. Ketik /start", show_alert=True)
            return

        await query.answer()
        data = query.data or ""
        
        if ":" in data:
            action, sig_id = data.split(":", 1)
        else:
            action, sig_id = data, ""
        
        # ── Mode Switch Confirmation ──────────────────────────────────
        if action == "mode_switch":
            if sig_id == "scalper":
                session.user.config.trading_mode = "scalper"
                user_db.update_user(session.user)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html(
                    "🚀 <b>SCALPER MODE AKTIF!</b>\n\n"
                    "⚠️ <b>HATI-HATI:</b> Akun Anda sekarang dalam mode ultra-agresif.\n"
                    "• Scan interval: 5 detik\n"
                    "• Risk: 13% | Leverage: up to 35x"
                )
            elif sig_id == "standard":
                session.user.config.trading_mode = "standard"
                user_db.update_user(session.user)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("📊 <b>STANDARD MODE AKTIF!</b>\n\nMode swing yang lebih aman dan terukur.")
            else:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("❌ Pemindahan mode dibatalkan.")
            return

        if action == "close_req":
            asset, side_val = sig_id.split(":", 1)
            pos = next((p for p in session.executor.open_positions if p.asset == asset and p.side.value == side_val), None)
            if not pos:
                await query.answer("❌ Posisi sudah tidak ada.", show_alert=True)
                return
            
            # Fetch current price for profit display
            try:
                current = await self.hl_client.get_mark_price(asset) if self.hl_client else pos.entry_price
            except Exception:
                current = pos.entry_price
                
            pnl_pct = pos.floating_pct(current) * 100
            pnl_val = pos.unrealized_pnl(current)
            sign = "+" if pnl_val >= 0 else ""
            
            text = (
                f"⚠️ <b>KONFIRMASI CLOSE POSISI</b>\n\n"
                f"Apakah kamu yakin ingin menutup posisi <b>{asset} {side_val.upper()}</b>?\n\n"
                f"• Estimasi PnL: <b>{sign}{format_idr(pnl_val)} ({sign}{pnl_pct:.2f}%)</b>\n"
                f"• Harga Saat Ini: <code>${format_price(current)}</code>\n\n"
                f"<i>Tindakan ini tidak dapat dibatalkan.</i>"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Ya, Close Sekarang", callback_data=f"close_confirm:{asset}:{side_val}")],
                [InlineKeyboardButton("❌ Batal", callback_data="close_cancel")]
            ])
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return

        if action == "close_confirm":
            asset, side_val = sig_id.split(":", 1)
            pos = next((p for p in session.executor.open_positions if p.asset == asset and p.side.value == side_val), None)
            if not pos:
                await query.answer("❌ Gagal: Posisi tidak ditemukan atau sudah tertutup.", show_alert=True)
                return
                
            await query.edit_message_text(f"⏳ Menutup posisi <b>{asset}</b>...", parse_mode=ParseMode.HTML)
            
            try:
                current = await self.hl_client.get_mark_price(asset)
            except Exception:
                current = pos.entry_price
                
            res = await session.executor.close_position(pos.position_id, current, reason="manual_close")
            if res:
                pnl = res.get("pnl", 0)
                sign = "+" if pnl >= 0 else ""
                await query.message.reply_html(
                    f"✅ <b>Posisi {asset} BERHASIL DITUTUP!</b>\n"
                    f"• Realized PnL: <b>{sign}{format_idr(pnl)}</b>\n"
                    f"• Harga Exit: <code>${format_price(current)}</code>"
                )
            else:
                await query.message.reply_html(f"❌ <b>Gagal menutup posisi {asset}.</b>")
            
            # Use callback to refresh positions list after a delay or just delete confirmation
            await query.message.delete()
            return

        if action == "close_cancel":
            # Just go back to positions list
            await self.cmd_positions(update, ctx)
            return

        if action == "close_all_req":
            count = len(session.executor.open_positions)
            if count == 0:
                await query.answer("Tidak ada posisi aktif.", show_alert=True)
                return
                
            text = (
                f"🚨 <b>KONFIRMASI CLOSE SEMUA POSISI</b> 🚨\n\n"
                f"Tindakan ini akan mencoba menutup ke-<b>{count}</b> posisi yang sedang terbuka secara paksa.\n\n"
                "Yakin ingin melanjutkan?"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ YA, CLOSE SEMUA! ⚠️", callback_data="close_all_confirm")],
                [InlineKeyboardButton("❌ Batal", callback_data="close_cancel")]
            ])
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return

        if action == "close_all_confirm":
            positions = list(session.executor.open_positions)
            if not positions:
                await query.answer("Posisi sudah kosong.", show_alert=True)
                return
                
            await query.edit_message_text(f"⏳ Mencoba menutup {len(positions)} posisi...", parse_mode=ParseMode.HTML)
            
            prices = {}
            for p in positions:
                try:
                    prices[p.asset] = await self.hl_client.get_mark_price(p.asset) if self.hl_client else p.entry_price
                except:
                    prices[p.asset] = p.entry_price
            
            results = await session.executor.close_all_positions(prices)
            total_pnl = sum(r.get("pnl", 0) for r in results)
            sign = "+" if total_pnl >= 0 else ""
            
            await query.message.reply_html(
                f"🚨 <b>MASSIVE CLOSE SELESAI!</b>\n\n"
                f"• Posisi ditutup: {len(results)}/{len(positions)}\n"
                f"• Total PnL: <b>{sign}{format_idr(total_pnl)}</b>"
            )
            await query.message.delete()
            return

        signal = self._pending_signals.get(sig_id)

        if action == "confirm" and signal:
            signal.confirmed = True
            if self._on_confirm:
                await self._on_confirm(signal, chat_id)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_html(
                f" <b>Eksekusi dikonfirmasi!</b>\n"
                f"KARA akan membuka posisi <b>{signal.asset}</b> sekarang~ "
            )
            self._pending_signals.pop(sig_id, None)

        elif action == "skip" and signal:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_html(
                f" Sinyal <b>{signal.asset if signal else sig_id}</b> dilewati.\n"
                "<i>Tidak masalah, selalu ada peluang berikutnya~ </i>"
            )
            self._pending_signals.pop(sig_id, None)

        elif action == "reasons" and signal:
            # Grouping logic
            marketists, fundingists, liqists, others = [], [], [], []
            for r in signal.breakdown.reasons:
                row = f"• {r.strip('• ')}"
                low = r.lower()
                if any(x in low for x in ["regime", "vwap", "session", "price", "trend"]):
                    marketists.append(row)
                elif any(x in low for x in ["funding", "pred", "basis", "flow", "cvd"]):
                    fundingists.append(row)
                elif any(x in low for x in ["oi", "liq ", "imbalance", "depth", "wall"]):
                    liqists.append(row)
                else:
                    others.append(row)

            blocks = []
            if marketists: blocks.append("📈 <b>Konteks Market:</b>\n" + "\n".join(marketists))
            if fundingists: blocks.append("💰 <b>Funding & Flow:</b>\n" + "\n".join(fundingists))
            if liqists:    blocks.append("🏦 <b>Likuiditas & Depth:</b>\n" + "\n".join(liqists))
            if others:     blocks.append("📝 <b>Lainnya:</b>\n" + "\n".join(others))

            reasons_text = "\n\n".join(blocks)
            warnings_text = (
                "\n\n⚠️ <b>Perhatian:</b>\n" +
                "\n".join(f"• {w}" for w in signal.breakdown.warnings)
                if signal.breakdown.warnings else ""
            )
            
            explanation = (
                f"Tentu! Ini analisis mendalamku untuk <b>{signal.asset}</b>:\n\n"
                f"{reasons_text}"
                f"{warnings_text}\n\n"
                f"<i>Semoga membantu kamu membuat keputusan yang tepat! 🌸</i>"
            )
            await query.message.reply_html(explanation)

    # ──────────────────────────────────────────
    # AUTH
    # ──────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """KARA Public Mode: Everyone is authorized to start."""
        return True
