"""
KARA Bot - Telegram Notification + Command Handler 
Uses python-telegram-bot v21+ (async-native).
Commands: /start /status /pos /pnl /pause /resume /stop /auto /manual
          /signal /backtest /help
"""

from __future__ import annotations
import asyncio
import html
import os
import logging
import re
import subprocess
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
import eth_account
import config
from core.db import user_db
from models.schemas import (
    TradeSignal, AccountState, SignalStrength, Side, BotMode,
    ExecutionMode
)
from utils.helpers import format_usd, format_idr, format_pct, format_price, utcnow

log = logging.getLogger("kara.telegram")

# ConversationHandler State
WAITING_CODE = 1
WAITING_CONFIG_VALUE = 2
WAITING_MAIN_ADDRESS = 3

DAILY_REPORT_TEMPLATE = """
📊 <b>KARA DAILY INSIGHTS</b> 🌸
──────────────────────────
📅 <i>Laporan Harian: {date}</i>

💰 <b>KESEHATAN PORTOFOLIO</b>
• Ekuitas Total: <code>{total_equity}</code>
• Saldo Dompet : <code>{wallet_balance}</code>
• Saldo Tersedia: <code>{available}</code>

📈 <b>PERFORMER HARI INI</b>
• Daily PnL    : <b>{pnl_sign}{pnl_val} ({pnl_sign}{pnl_pct})</b> {pnl_emoji}
• Posisi Aktif : <b>{pos_count} terbuka</b>
• Max Drawdown : <b>{drawdown}</b>

🛡️ <b>STATUS SISTEM</b>
• Risk Mode    : {mode_icon} <b>{mode_text}</b>
• Bot Status   : {status_icon} <b>{status_text}</b>

──────────────────────────
<i>{footer}</i>
"""


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
<i>ID: {sig_id}</i>
"""

LIVE_SETUP_RISK_WARNING = """
⚠️ <b>RISK WARNING: LIVE MODE</b> ⚠️
──────────────────────────
Halo bosku! KARA di sini untuk memperingatkan bahwa kamu akan memasuki <b>Live Mode (Real Money)</b>.

<b>Hal yang perlu kamu tahu:</b>
1. Bot akan mengeksekusi trade nyata di akun Hyperliquid kamu.
2. KARA menggunakan <b>Agent Wallet</b> untuk keamanan (kamu tidak perlu memberikan private key utama).
3. Trading crypto futures memiliki risiko tinggi. Gunakan dana yang siap untuk rugi.

<i>Apakah kamu ingin melanjutkan untuk men-generate Agent Wallet khusus?</i>
"""

AGENT_WALLET_CREATED_TEMPLATE = """
✅ <b>Agent Wallet berhasil dibuat!</b>
──────────────────────────
Simpan data ini dengan aman. Kamu hanya akan melihatnya <b>satu kali</b>.

🔑 <b>Agent Wallet Address:</b>
<code>{address}</code>

🔑 <b>Agent Private Key:</b>
<code>{private_key}</code>

<b>Langkah Selanjutnya (PENTING!):</b>
1. Buka link ini: <a href="https://app.hyperliquid.xyz/API">Hyperliquid API Dashboard</a>
2. Connect dengan <b>Main Wallet</b> kamu (wallet yang punya dana).
3. Klik "Authorize API Wallet" dan masukkan <b>Agent Wallet Address</b> di atas.
4. Paste <b>Agent Private Key</b> jika diminta, atau cukup Approve transaksi di wallet utama kamu.
5. Klik tombol <b>"✅ Saya Sudah Authorize"</b> di bawah ini jika sudah selesai.

<i>Catatan: Agent ini hanya bisa melakukan trade (Open/Close). Penarikan dana tetap harus lewat Wallet Utama kamu. 🌸</i>
"""

TOS_TEXT = """
⚖️ <b>TERMS OF SERVICE — KARA AI AGENT</b> 🌸
──────────────────────────
Sebelum kita mulai petualangan trading kita, aku perlu kamu menyetujui beberapa hal penting ya, User! ✨

1. 🛡️ <b>Tanggung Jawab Pribadi:</b> Aku adalah asisten AI yang memberikan analisis berdasarkan data market. Semua keputusan akhir untuk melakukan trade ada di tangan User sepenuhnya.
2. 📉 <b>Risiko Modal:</b> Trading futures memiliki risiko tinggi. User memahami bahwa modal bisa berkurang atau hilang sepenuhnya.
3. 🚫 <b>Bukan Penasehat Keuangan:</b> Aku bukan penasehat keuangan berlisensi. Gunakan analisisku hanya sebagai referensi tambahan.
4. ⚙️ <b>Teknologi:</b> User memahami risiko teknis seperti delay koneksi atau error pada API pihak ketiga (Hyperliquid).

<i>\"Analisisku cerdas, tapi User adalah nahkodanya!\"</i> 💜

Ayo kita mulai dengan bijak~! ✨
──────────────────────────
<b>Apakah User setuju dengan ketentuan di atas?</b>
"""

RISK_WARNING_TEXT = """
⚠️ <b>RISK WARNING (LIVE MODE)</b> ⚡
──────────────────────────
User akan memasuki <b>Live Trading Mode</b> menggunakan dana asli. Harap perhatikan hal-hal berikut:

• 📉 <b>Past Performance:</b> Hasil trading masa lalu (Paper Mode) TIDAK menjamin hasil yang sama di masa depan. Market selalu dinamis.
• 💸 <b>Loss Potential:</b> Gunakan hanya dana yang User siap untuk hilang. Jangan gunakan dana kebutuhan pokok.
• ⚡ <b>Leverage:</b> Penggunaan leverage tinggi mempercepat potensi keuntungan sekaligus mempercepat risiko likuidasi modal.
• ⚖️ <b>DYOR:</b> Selalu lakukan riset mandiri sebelum mengonfirmasi eksekusi dari KARA.

<i>KARA akan menjagamu dengan manajemen risiko ketat, tapi User harus tetap waspada!</i> 💜
──────────────────────────
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
        self._authorized_chat_ids: set = set()
        self._state_file = config.TG_STATE_PATH
        
        # Rate Limiting:
        # chat_id -> {"__global__": ts, "<action_key>": ts}
        # Keeps anti-spam protection while allowing different commands to run quickly.
        self._last_cmd_ts: Dict[str, Dict[str, float]] = {}

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
                .read_timeout(30)
                .write_timeout(30)
                .connect_timeout(30)
                .pool_timeout(30)
                .get_updates_read_timeout(60)
                .get_updates_write_timeout(60)
                .get_updates_connect_timeout(60)
                .get_updates_pool_timeout(60)
                .build()
            )

            # Access Code ConversationHandler (wraps /start flow for new users)
            access_conv = ConversationHandler(
                entry_points=[CommandHandler("start", self.cmd_start)],
                states={
                    WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.cmd_access_code)],
                },
                fallbacks=[CommandHandler("start", self.cmd_start)],
                per_user=True,
                per_chat=True,
                allow_reentry=True,
            )

            # Settings ConversationHandler
            settings_conv = ConversationHandler(
                entry_points=[
                    CommandHandler("settings", self.cmd_settings),
                    CallbackQueryHandler(self.on_callback, pattern="^set_cfg:")
                ],
                states={
                    WAITING_CONFIG_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.cmd_set_config_value)],
                },
                fallbacks=[CommandHandler("settings", self.cmd_settings), CommandHandler("cancel", self.cmd_cancel)],
                per_user=True,
                allow_reentry=True
            )

            # Live Setup ConversationHandler
            live_conv = ConversationHandler(
                entry_points=[CommandHandler("live", self.cmd_live)],
                states={
                    WAITING_MAIN_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_main_address)],
                },
                fallbacks=[CommandHandler("cancel", self.cmd_cancel), CommandHandler("live", self.cmd_live)],
                per_user=True,
                allow_reentry=True
            )

            handlers = [
                access_conv,
                settings_conv,
                live_conv,
                CommandHandler("help",     self.cmd_help),
                CommandHandler("status",   self.cmd_status),
                CommandHandler("pos",      self.cmd_positions),
                CommandHandler("positions",self.cmd_positions),
                CommandHandler("pnl",      self.cmd_pnl),
                CommandHandler("export",   self.cmd_export),
                CommandHandler("mode",     self.cmd_mode),
                CommandHandler("scalper",  self.cmd_scalper),
                CommandHandler("standard", self.cmd_standard),
                CommandHandler("paper",    self.cmd_paper),
                CommandHandler("settings", self.cmd_settings),
                CommandHandler("signal",   self.cmd_signal),
                
                # Direct Config Commands
                CommandHandler("setleverage",  self.cmd_direct_set_config),
                CommandHandler("setmaxpos",    self.cmd_direct_set_config),
                # No more manual whatsnew command, it's automatic now

                CallbackQueryHandler(self.on_callback),
            ]
            for h in handlers:
                self._app.add_handler(h)

            # Global Error Handler
            self._app.add_error_handler(self._handle_error)

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
                        BotCommand("signal",   "Lihat sinyal trading terbaru"),
                        BotCommand("pnl",      "Ringkasan PnL & Equity"),
                        BotCommand("paper",    "Kembali ke Paper Mode & Reset Saldo"),
                        BotCommand("live",     "Setup Live Mode (Agent Wallet)"),
                        BotCommand("settings", "Pusat Kendali (Threshold & Leverage)"),
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
        """Entry point — gate by Access Code for NEW users."""
        chat_id  = str(update.effective_chat.id)
        username = update.effective_user.username or update.effective_user.first_name or "User"

        # ── 1. Always create a bare-minimum user record if none exists ──
        user = user_db.get_user(chat_id)
        if not user:
            user = user_db.create_user(chat_id, username, init_usd=config.PAPER_BALANCE_USD)

        # ── 2. Admin shortcut: TELEGRAM_CHAT_ID is always authorized ──
        if not user.is_authorized and str(config.TELEGRAM_CHAT_ID) == chat_id:
            user.is_authorized     = True
            user.authorized_at     = datetime.now()
            user.tos_agreed        = True
            user_db.update_user(user)

        # ── 3. Already authorized → welcome back ──
        if user.is_authorized:
            if chat_id not in self._authorized_chat_ids:
                self._authorized_chat_ids.add(chat_id)
                self._save_state()
            if self.bot_app:
                await self.bot_app.get_session(chat_id)

            # TOS check (authorized tapi belum agree)
            if not getattr(user, 'tos_agreed', False):
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ SAYA SETUJU & PAHAM", callback_data="tos_agree")]
                ])
                await update.effective_message.reply_html(TOS_TEXT, reply_markup=keyboard)
                return ConversationHandler.END

            reply_msg = (
                f"✨ <b>Halo, {username}! Saya KARA, Intelligence Trading Partner Anda.</b> 🌸\n\n"
                f"Selamat datang kembali! Saya siap memantau Hyperliquid untuk User.\n\n"
                f"💰 <b>Saldo Paper User: {format_idr(user.paper_balance_usd)}</b>\n\n"
                f"Ketik /help untuk instruksi. Jangan lupa, <i>Not Financial Advice</i> ya! 💜"
            )
            await update.effective_message.reply_html(reply_msg)
            return ConversationHandler.END

        # ── 4. Check block (too many wrong attempts) ──
        now = datetime.now(timezone.utc)
        blocked_until = user.access_blocked_until
        if blocked_until:
            blocked_until_aware = blocked_until.replace(tzinfo=timezone.utc) if blocked_until.tzinfo is None else blocked_until
            if now < blocked_until_aware:
                remaining_mins = int((blocked_until_aware - now).total_seconds() // 60) + 1
                await update.effective_message.reply_html(
                    f"🚫 <b>Akses Sementara Diblokir</b>\n\n"
                    f"Terlalu banyak percobaan kode yang salah.\n"
                    f"Silakan coba lagi dalam <b>{remaining_mins} menit</b>. "
                )
                return ConversationHandler.END
            else:
                # Block expired, reset
                user.access_attempts     = 0
                user.access_blocked_until = None
                user_db.update_user(user)

        # ── 5. New / not-yet-authorized → ask for access code ──
        remaining = config.ACCESS_MAX_TRIES - user.access_attempts
        await update.effective_message.reply_html(
            f"🔒 <b>Selamat datang di KARA Bot!</b> 🌸\n\n"
            f"Bot ini bersifat privat dan hanya untuk pengguna terpilih.\n"
            f"Silakan masukkan <b>Access Code</b> untuk melanjutkan: "
        )
        return WAITING_CODE

    async def cmd_access_code(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle Access Code input from user."""
        chat_id  = str(update.effective_chat.id)
        username = update.effective_user.username or update.effective_user.first_name or "User"
        code_input = (update.message.text or "").strip()

        user = user_db.get_user(chat_id)
        if not user:
            user = user_db.create_user(chat_id, username, init_usd=config.PAPER_BALANCE_USD)

        # ── Validate Code (case-insensitive) ──
        valid = any(code_input.upper() == c.upper() for c in config.ALL_ACCESS_CODES)

        if valid:
            user.is_authorized        = True
            user.authorized_at        = datetime.now(timezone.utc)
            user.access_attempts      = 0
            user.access_blocked_until = None
            user.tos_agreed           = True   # access code implies ToS acceptance
            user_db.update_user(user)

            # Register in session
            if chat_id not in self._authorized_chat_ids:
                self._authorized_chat_ids.add(chat_id)
                self._save_state()
            if self.bot_app:
                await self.bot_app.get_session(chat_id)

            log.info(f"✅ New user authorized: {username} ({chat_id})")
            await update.effective_message.reply_html(
                f"✨ <b>Halo, {username}! Saya KARA, Intelligence Trading Partner Anda.</b> 🌸\n\n"
                f"Selamat datang! Saya sudah menyiapkan akun virtual Anda untuk trading di Hyperliquid perp.\n\n"
                f"💰 <b>Saldo Paper Anda: {format_idr(user.paper_balance_usd)}</b>\n\n"
                f"Ketik /help untuk instruksi awal. Anda bisa mengetik /mode untuk memilih gaya trading (Standard / Scalper)."
            )
            return ConversationHandler.END

        # ── Wrong code ──
        user.access_attempts += 1
        remaining = config.ACCESS_MAX_TRIES - user.access_attempts

        if remaining <= 0:
            user.access_blocked_until = datetime.now(timezone.utc) + timedelta(hours=config.ACCESS_BLOCK_HOURS)
            user_db.update_user(user)
            log.warning(f"⛔ User {chat_id} blocked after {config.ACCESS_MAX_TRIES} wrong attempts.")
            await update.effective_message.reply_html(
                f"🚫 <b>Akses Diblokir Sementara</b>\n\n"
                f"Kamu sudah mencoba <b>{config.ACCESS_MAX_TRIES} kali</b> dengan kode yang salah.\n"
                f"Akun sementara diblokir selama <b>{config.ACCESS_BLOCK_HOURS} jam</b>.\n\n"
                f"<i>Kalau kamu seharusnya punya akses, hubungi Admin ya!</i> 💜"
            )
            return ConversationHandler.END

        user_db.update_user(user)
        await update.effective_message.reply_html(
            f"❌ <b>Access Code salah.</b> Silakan coba lagi.\n"
            f"<i>Sisa percobaan: <b>{remaining}</b></i>"
        )
        return WAITING_CODE

    async def cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Display and edit user-specific trading configuration."""
        if not self._is_authorized(update): return
        
        # Consistent throttling: 1s for buttons, 5s for commands
        thr = 1 if update.callback_query else 5
        if self._is_throttled(str(update.effective_chat.id), threshold=thr):
            if update.callback_query:
                await update.callback_query.answer("⚠️ Terlalu cepat! Tunggu 1 detik.", show_alert=False)
            return
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return

        # Determine which mode view to show (default to current mode)
        view_mode = ctx.user_data.get("settings_view_mode", user.config.trading_mode)
        ctx.user_data["settings_view_mode"] = view_mode
        
        is_scl = (view_mode == "scalper")
        pfx = "scl_" if is_scl else "std_"
        mode_label = "🌸 SCALPER MODE" if is_scl else "🛡️ STANDARD MODE"

        text = (
            f"⚙️ <b>KARA Settings — {mode_label}</b>\n"
            f"• Min Score Auto-Trade : <b>{getattr(user.config, pfx+'min_score_to_auto_trade')}</b> (locked)\n"
            f"• Max Leverage         : <b>{getattr(user.config, pfx+'max_leverage')}x</b>\n"
            f"• Max Open Positions   : <b>{getattr(user.config, pfx+'max_concurrent_positions')}</b>\n\n"
            f"<i>Auto-trade score dikunci sistem. Kamu hanya bisa ubah leverage & max positions.</i> 🌸"
        )

        keyboard = [
            [
                InlineKeyboardButton("📈 Leverage", callback_data=f"set_cfg:{pfx}max_leverage"),
                InlineKeyboardButton("🎯 Max Positions", callback_data=f"set_cfg:{pfx}max_concurrent_positions")
            ],
            [
                InlineKeyboardButton("🔄 Ganti View ke " + ("Standard" if is_scl else "Scalper"), callback_data="switch_settings_view"),
                InlineKeyboardButton("♻️ Reset Defaults", callback_data="reset_cfg_defaults")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower(): raise
                await update.callback_query.answer("Sudah di layar ini")
    async def cmd_direct_set_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Unified handler for /setleverage 10 and /setmaxpos 5."""
        if not self._is_authorized(update): return
        
        chat_id = str(update.effective_chat.id)
        cmd = update.effective_message.text.split()[0].lower().replace("/", "")
        args = ctx.args
        
        if not args:
            await update.effective_message.reply_text(f"❌ Penggunaan: /{cmd} <angka>\nContoh: /{cmd} 10")
            return

        try:
            val = int(args[0])
            user = user_db.get_user(chat_id)
            if not user: return

            if "leverage" in cmd:
                if not (1 <= val <= 30):
                    await update.effective_message.reply_text("❌ Leverage harus antara 1 dan 30x.")
                    return
                user.config.max_leverage = val
                field_name = "Leverage Maksimal"
            else: # setmaxpos
                if not (1 <= val <= 10):
                    await update.effective_message.reply_text("❌ Maksimal posisi harus antara 1 dan 10.")
                    return
                user.config.max_concurrent_positions = val
                field_name = "Maksimal Posisi"

            user_db.update_user(user)
            log.info(f"✅ User {chat_id} updated {field_name} to {val}")
            await update.effective_message.reply_html(f"✅ <b>{field_name}</b> diperbarui menjadi: <code>{val}</code>")
            
        except (ValueError, IndexError):
            await update.effective_message.reply_text(f"❌ Harap masukkan angka bulat (integer). Contoh: /{cmd} 5")

    async def cmd_set_config_value(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Process the text input for a specific config field."""
        if not self._is_authorized(update): return ConversationHandler.END
        
        chat_id = str(update.effective_chat.id)
        field = ctx.user_data.get("editing_field")
        val_str = update.message.text.strip()
        
        if not field:
            await update.effective_message.reply_text("❌ Terjadi kesalahan sesi. Silakan ketik /settings lagi.")
            return ConversationHandler.END
            
        try:
            val = int(val_str)
            # Validations
            if "min_score" in field:
                await update.effective_message.reply_text("🔒 Parameter skor dikunci sistem dan tidak bisa diubah.")
                return ConversationHandler.END
            elif "max_leverage" in field:
                if not (1 <= val <= 40):
                    await update.effective_message.reply_text("❌ Leverage harus antara 1 dan 40x. Coba lagi:")
                    return WAITING_CONFIG_VALUE
            elif "max_concurrent" in field:
                if not (1 <= val <= 10):
                    await update.effective_message.reply_text("❌ Maksimal posisi antara 1 dan 10. Coba lagi:")
                    return WAITING_CONFIG_VALUE
                    
            # Save to DB
            user = user_db.get_user(chat_id)
            setattr(user.config, field, val)
            user_db.update_user(user)
            
            await update.effective_message.reply_html(f"✅ <b>Berhasil!</b> Nilai diperbarui menjadi: <code>{val}</code>")
            return await self.cmd_settings(update, ctx)
            
        except ValueError:
            await update.effective_message.reply_text("❌ Harap masukkan angka bulat (integer). Coba lagi:")
            return WAITING_CONFIG_VALUE

    async def handle_main_address(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Step 2 of Live Setup: Capture the Main Wallet Address from user reply."""
        if not self._is_authorized(update): return ConversationHandler.END
        
        main_address = update.effective_message.text.strip()
        chat_id = str(update.effective_chat.id)
        
        # Simple validation
        if not (main_address.startswith("0x") and len(main_address) == 42):
            await update.effective_message.reply_html("❌ Alamat tidak valid. Pastikan alamat dimulai dengan <code>0x</code> dan terdiri dari 42 karakter.")
            return WAITING_MAIN_ADDRESS

        user = user_db.get_user(chat_id)
        if not user: return ConversationHandler.END
        
        # Save main address
        user.hl_main_address = main_address
        
        # Now generate the Agent Wallet for them
        import eth_account
        acc = eth_account.Account.create()
        address = acc.address
        private_key = acc.key.hex()
        
        user.hl_agent_address = address
        user.hl_agent_secret = private_key # Will be encrypted on save
        user.wallet_authorized = False
        user_db.update_user(user)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Saya Sudah Authorize", callback_data="authorize_final")],
            [InlineKeyboardButton("❌ Batal / Ganti Wallet", callback_data="close_settings")]
        ])
        
        from notify.telegram_templates import AGENT_WALLET_CREATED_TEMPLATE
        await update.effective_message.reply_html(
            AGENT_WALLET_CREATED_TEMPLATE.format(
                address=address,
                private_key=private_key
            ),
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        return ConversationHandler.END
        """Handle direct commands like /setleverage 20 or /setmaxpos 3."""
        if not self._is_authorized(update): return
        
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return
        
        cmd = update.message.text.split()[0].lower().replace("/", "")
        args = ctx.args
        
        if not args:
            await update.effective_message.reply_html(f"ℹ️ <b>Cara pakai:</b> <code>/{cmd} [angka]</code>")
            return
            
        try:
            val = int(args[0])
            mode = user.config.trading_mode
            pfx = "scl_" if mode == "scalper" else "std_"
            
            # Map command to field
            mapping = {
                "setleverage": "max_leverage",
                "setmaxpos":   "max_concurrent_positions"
            }
            
            field_base = mapping.get(cmd)
            if not field_base:
                await update.effective_message.reply_text("🔒 Pengubahan skor dinonaktifkan. Gunakan /setleverage atau /setmaxpos.")
                return
            
            full_field = f"{pfx}{field_base}"
            
            # Re-use validation logic
            if "min_score" in field_base:
                if not (40 <= val <= 100):
                    await update.effective_message.reply_text("❌ Score harus antara 40 dan 100.")
                    return
            elif "max_leverage" in field_base:
                if not (1 <= val <= 40):
                    await update.effective_message.reply_text("❌ Leverage harus antara 1 dan 40x.")
                    return
            elif "max_concurrent" in field_base:
                if not (1 <= val <= 10):
                    await update.effective_message.reply_text("❌ Maksimal posisi antara 1 dan 10.")
                    return
            
            setattr(user.config, full_field, val)
            user_db.update_user(user)
            
            mode_label = "SCALPER" if mode == "scalper" else "STANDARD"
            await update.effective_message.reply_html(f"✅ <b>[{mode_label}]</b> {field_base.replace('_', ' ').title()} diperbarui ke: <code>{val}</code>")
            
        except ValueError:
            await update.effective_message.reply_text("❌ Harap masukkan angka yang valid.")

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("👌 Operasi dibatalkan.")
        return ConversationHandler.END

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="help"): return
        
        help_text = (
            "📖 <b>KARA 4.0 - User Manual</b> 🌸\n\n"
            "🎛️ <b>Mode Trading & Akun</b>\n"
            "• /mode     — Pilih gaya trading (Standard/Scalper)\n"
            "• /settings — Atur threshold & leverage pribadi\n"
            "• /live     — Aktivasi Live Mode (Risiko Nyata)\n"
            "• /paper    — Kembali ke Paper Mode & Reset saldo\n\n"
            "📊 <b>Informasi Portofolio</b>\n"
            "• /status   — Status bot, ekuitas, dan float\n"
            "• /pos      — Lihat daftar koin yang sedang jalan\n"
            "• /signal   — Lihat sinyal trading terbaru\n"
            "• /pnl      — Ringkasan keuntungan/kerugian\n"
            "• /export   — Download riwayat trade Excel\n\n"
            "✨ <b>Update & Info</b>\n"
            "• /whatsnew — Lihat pembaruan fitur terbaru\n\n"
            "<i>Tips: Gunakan menu /settings untuk menyesuaikan bot dengan kenyamanan risiko User.</i>\n\n"
            "⚠️ <b>Not Financial Advice:</b> Seluruh aktivitas trading memiliki risiko. User bertanggung jawab penuh. ✨"
        )
        await update.effective_message.reply_html(help_text)


    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="status"): return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        try:
            text, keyboard = await self._get_status_content(session)
            await update.effective_message.reply_html(text, reply_markup=keyboard)
        except Exception as e:
            log.error(f"Status error: {e}", exc_info=True)
            await update.effective_message.reply_text(f"❌ Error: {e}")

    async def _get_status_content(self, session) -> Tuple[str, InlineKeyboardMarkup]:
        """Centralized status message and keyboard generator to prevent refresh bugs."""
        import config
        from models.schemas import BotMode
        
        try:
            acc = await session.get_account_state()
        except Exception as e:
            log.warning(f"Status error for {session.user.chat_id}: {e}")
            text = (
                f"🌸 <b>KARA System Status</b>\n\n"
                f"⚠️ <b>Koneksi Wallet Bermasalah</b>\n"
                f"KARA tidak bisa terhubung ke wallet utama/agent anda saat ini.\n\n"
                f"📝 <b>Detail:</b> <code>{e}</code>\n\n"
                f"Silakan cek koneksi internet atau status Hyperliquid API."
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="status_refresh")
            ]])
            return text, keyboard

        risk_status = session.risk_mgr.status

        mode_str  = "PAPER" if acc.mode == BotMode.PAPER else "LIVE"
        # Using the user's preferred emojis from the screenshot/request
        pause_str = "⏸️ PAUSED" if acc.is_paused else "▶️ Active"
        kill_str  = "🚨 KILL SWITCH" if acc.kill_switch_active else "Normal"

        pnl_sign = "+" if acc.unrealized_pnl > 0 else ""
        daily_sign = "+" if acc.daily_pnl > 0 else ""
        auto_str = "🚀 Full-Auto" if config.FULL_AUTO else "🛡️ Semi-Auto"
        pos_len = len([p for p in acc.positions if p.status.value == 'open'])

        text = (
            f"🌸 <b>KARA System Status</b>\n\n"
            f"💜 <b>Profil & Eksekusi</b>\n"
            f"  • Mode: <b>{mode_str}</b> ({auto_str})\n"
            f"  • Status: {pause_str}\n"
            f"  • Kill-Switch: {kill_str}\n\n"
            f"📊 <b>Kondisi Dana</b>\n"
            f"  • Ekuitas: <code>{format_idr(acc.total_equity)}</code> (NAV)\n"
            f"  • Saldo Dompet: <code>{format_idr(acc.wallet_balance)}</code>\n"
            f"  • Saldo Tersedia: <code>{format_idr(acc.available)}</code>\n"
            f"  • Float PnL: <b>{pnl_sign}{format_idr(acc.unrealized_pnl)}</b>\n\n"
            f"📈 <b>Performa Harian (Total)</b>\n"
            f"  • Profit Hari Ini: <b>{daily_sign}{format_idr(acc.daily_pnl)}</b> ({daily_sign}{format_pct(acc.daily_pnl_pct)})\n"
            f"  • Max Drawdown: <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n\n"
            f"🎯 <b>Posisi Terbuka:</b> {pos_len} aset\n"
        )

        if risk_status.get("in_cooldown"):
            text += "❄️ <i>Post-loss cooldown aktif. Break dulu yaa~</i>"
        else:
            text += "<i>Semua sistem beroperasi maksimal, siap tangkap peluang~! ✨</i>"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💼 Posisi",   callback_data="status_nav:pos"),
                InlineKeyboardButton("💰 PnL",      callback_data="status_nav:pnl")
            ],
            [
                InlineKeyboardButton("⚙️ Mode",     callback_data="status_nav:mode"),
                InlineKeyboardButton("🛠️ Settings", callback_data="status_nav:settings")
            ],
            [
                InlineKeyboardButton("🔄 Refresh",  callback_data="status_refresh")
            ]
        ])
        return text, keyboard

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        
        # Use lower threshold (1s) for button clicks, 5s for typed commands
        thr = 1 if update.callback_query else 5
        if self._is_throttled(str(update.effective_chat.id), threshold=thr, action_key="positions"):
            if update.callback_query:
                await update.callback_query.answer("⚠️ Terlalu cepat! Tunggu 1 detik.", show_alert=False)
            return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        positions = session.executor.open_positions
        if not positions:
            text = (
                "📭 <b>Tidak ada posisi terbuka saat ini.</b>\n"
                "<i>KARA menunggu sinyal yang tepat~ 🌸</i>"
            )
            keyboard = [[InlineKeyboardButton("🔄 REFRESH", callback_data="refresh_pos")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.callback_query:
                try:
                    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                except BadRequest as e:
                    if "not modified" not in str(e).lower(): raise
                    await update.callback_query.answer("Sudah up-to-date")
            else:
                await update.effective_message.reply_html(text, reply_markup=reply_markup)
            return

        # Fetch live prices for all open assets — OPTIMIZED (Task 5)
        live_prices = {}
        if self.bot_app and self.bot_app.cache:
            for pos in positions:
                ctx = self.bot_app.cache.funding.get(pos.asset)
                if ctx and "markPx" in ctx:
                    try:
                        live_prices[pos.asset] = float(ctx["markPx"])
                    except: pass
        
        # Fallback for missing prices (one batch call instead of many)
        if len(live_prices) < len(positions) and self.hl_client:
            try:
                # One batch call for ALL metadata & contexts -> [universe, contexts]
                all_meta = await self.hl_client.get_all_market_data()
                if all_meta and len(all_meta) >= 2:
                    universe = all_meta[0]
                    contexts = all_meta[1]
                    for i, ctx in enumerate(contexts):
                        if i < len(universe):
                            name = universe[i].get("name")
                            if name in [p.asset for p in positions] and name not in live_prices:
                                live_prices[name] = float(ctx.get("markPx", 0))
            except Exception as e:
                log.debug(f"Batch price fallback failed: {e}")
        
        # Final safety fallback 
        for pos in positions:
            if pos.asset not in live_prices or live_prices[pos.asset] == 0:
                live_prices[pos.asset] = pos.entry_price

        now_utc = datetime.now(timezone.utc)

        text = f"🌸 <b>Monitoring Posisi ({len(positions)})</b>\n\n"

        # ── Keyboard setup ──────────────────────────────────────────────
        keyboard_rows = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_pos")]
        ]
        close_buttons = []

        for pos in positions:
            current    = live_prices.get(pos.asset, pos.entry_price)
            unreal_pnl = pos.unrealized_pnl(current)
            float_pct  = pos.floating_pct(current) * 100
            pnl_sign   = "+" if float_pct >= 0 else ""
            pnl_emoji  = "🟢" if float_pct >= 0 else "🔴"
            side_str   = pos.side.value.upper()

            # Duration
            if pos.opened_at:
                delta      = now_utc - (pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at)
                total_mins = int(delta.total_seconds() // 60)
                duration   = f"{total_mins // 60}j {total_mins % 60}m" if total_mins >= 60 else f"{total_mins}m"
            else:
                duration = "?"

            # TP status indicators
            tp1_icon = "✅" if pos.tp1_hit  else "⏳"
            tp2_icon = "✅" if pos.tp2_hit  else "⏳"

            # SL label — if TP1 already hit, SL moved to breakeven
            sl_note = " <i>(BEP)</i>" if pos.tp1_hit else ""

            # Liq row (only show if available)
            liq_part = f" | 💥 Liq: <code>${format_price(pos.liquidation_price)}</code>" if pos.liquidation_price else ""

            # ── Concise Card ───────────────────────────────────────────────
            # Clean ticker for Hyperliquid URL (strip 'k' for 1000x assets)
            url_ticker = pos.asset[1:] if pos.asset.startswith('k') and len(pos.asset) > 1 else pos.asset
            hl_link    = f"https://app.hyperliquid.xyz/trade/{url_ticker}"
            asset_html = f"<a href='{hl_link}'>{pos.asset}</a>"

            text += (
                f"\n"
                f"<b>{asset_html} {side_str} {pos.leverage}x</b>   {pnl_emoji} {pnl_sign}{float_pct:.2f}%\n"
                f"Entry: ${format_price(pos.entry_price)} → ${format_price(current)}\n"
                f"🛡️ SL: ${format_price(pos.stop_loss)} | 💥 Liq: ${format_price(pos.liquidation_price) if pos.liquidation_price else '?'}\n"
                f"🎯 TP1: ${format_price(pos.tp1)}   🎯 TP2: ${format_price(pos.tp2)}   | {duration} lalu\n"
            )

            # Close button per position (2-column grid)
            side_short = "L" if pos.side.value == "long" else "S"
            close_buttons.append(
                InlineKeyboardButton(
                    f"❌ {pos.asset} {side_short}",
                    callback_data=f"close_req:{pos.asset}:{pos.side.value}"
                )
            )

        # Build close button rows (2 per row)
        for i in range(0, len(close_buttons), 2):
            keyboard_rows.append(close_buttons[i:i+2])

        # Footer
        text += "\n<i>Santai dulu, biarkan profit kita mengalir~ 🌸</i>"

        # Close All row
        keyboard_rows.append([
            InlineKeyboardButton("🚨 CLOSE ALL POSITIONS 🚨", callback_data="close_all_req")
        ])

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
            except Exception as e:
                log.debug(f"Refresh skip (no change): {e}")
        else:
            await update.effective_message.reply_html(text, reply_markup=reply_markup)


    async def cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        
        # Consistent throttling: 1s for buttons, 5s for commands
        thr = 1 if update.callback_query else 5
        if self._is_throttled(str(update.effective_chat.id), threshold=thr, action_key="pnl"):
            if update.callback_query:
                await update.callback_query.answer("⚠️ Terlalu cepat! Tunggu 1 detik.", show_alert=False)
            return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        try:
            acc = await session.get_account_state()
            daily_sign = "+" if acc.daily_pnl >= 0 else ""
            mode_str   = "PAPER 📝" if acc.mode.value == "paper" else "LIVE ⚡"

            text = (
                "💰 <b>KARA PORTFOLIO SUMMARY</b>\n\n"
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
            await update.effective_message.reply_html(text)
        except Exception as e:
            await update.effective_message.reply_html(f"❌ PnL Error: {e}")

    async def cmd_daily(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """On-demand daily report manual."""
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="daily"): return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return
        
        acc = await session.get_account_state()
        pos_count = len(session.executor.open_positions)
        await self.send_daily_report(acc, pos_count, target_chat_id=chat_id)

    async def send_daily_report(self, acc: AccountState, pos_count: int, target_chat_id: str = None):
        """Send the premium daily summary report."""
        pnl_sign = "+" if acc.daily_pnl >= 0 else ""
        pnl_emoji = "🟢" if acc.daily_pnl >= 0 else "🔴"
        mode_text = "SCALPER" if self.mode_manager and self.mode_manager.is_scalper() else "STANDARD"
        mode_icon = "⚡" if mode_text == "SCALPER" else "📊"
        
        # Friendly footers based on performance
        if acc.daily_pnl >= 0:
            footer = "Kerja bagus hari ini! Mari kita jaga momentumnya~ 🌸"
        else:
            footer = "Besok kita balas dendam ke market ya! Tetap disiplin~ 🌸"

        status_text = "OK" if not acc.is_paused else "PAUSED"
        status_icon = "✅" if status_text == "OK" else "⏸️"

        text = DAILY_REPORT_TEMPLATE.format(
            date=datetime.now().strftime("%Y-%m-%d"),
            total_equity=format_idr(acc.total_equity),
            wallet_balance=format_idr(acc.wallet_balance),
            available=format_idr(acc.available),
            pnl_sign=pnl_sign,
            pnl_val=format_idr(acc.daily_pnl),
            pnl_pct=format_pct(acc.daily_pnl_pct),
            pnl_emoji=pnl_emoji,
            pos_count=pos_count,
            drawdown=format_pct(acc.current_drawdown_pct, show_sign=False),
            mode_icon=mode_icon,
            mode_text=mode_text,
            status_icon=status_icon,
            status_text=status_text,
            footer=footer
        )
        await self.send_text(text, target_chat_id=target_chat_id)





    async def cmd_enable_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.effective_message.reply_html(
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
        await update.effective_message.reply_html(
            "🤝 <b>Mode Semi-Auto aktif.</b>\n"
            "KARA akan kirim sinyal dan kamu konfirmasi sebelum eksekusi. "
            "Ini cara terbaik untuk belajar! "
        )

    async def cmd_signal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="signal"): return
        chat_id = str(update.effective_chat.id)
        
        from core.db import user_db
        # Fetch more signals to filter out duplicates
        raw_signals = user_db.load_signals(limit=50)
        
        if not raw_signals:
            await update.effective_message.reply_html(
                "📭 <b>Belum ada riwayat sinyal.</b>\n\n"
                "<i>KARA sedang memantau market, tunggu sampai sinyal pertama muncul ya!</i>"
            )
            return

        # Deduplicate: Only keep the latest signal per asset
        unique_map = {}
        for s in raw_signals:
            if s.asset not in unique_map:
                unique_map[s.asset] = s
        
        # Take Top 10 unique assets
        signals = list(unique_map.values())[:10]

        from datetime import datetime
        import time
        now = time.time()
        
        lines = ["🏆 <b>TOP 10 LATEST ASSETS</b> 🏆\n"]
        
        for i, sig in enumerate(signals, 1):
            side_emoji = "🟢" if sig.side.value == "long" else "🔴"
            
            # Formatting timestamp
            diff = int((now - sig.timestamp.timestamp()) / 60)
            time_str = f"{diff}m ago" if diff < 60 else f"{diff//60}h ago"
            if diff < 1: time_str = "just now"
            
            # Breakdown data
            bd = sig.breakdown
            oi  = bd.oi_funding_score
            liq = bd.liquidation_score
            ob  = bd.orderbook_score
            ses = bd.session_bonus
            
            # Bull vs Bear
            t_bull = bd.total_bull
            t_bear = bd.total_bear

            lines.append(
                f"{i}. <b>{sig.asset}</b> ({side_emoji} {sig.side.value.upper()}) — <b>{sig.score}/100</b>\n"
                f"   <code>[OI:{oi:+} Liq:{liq:+} OB:{ob:+} Ses:{ses:+}]</code>\n"
                f"   Pts: {t_bull:.1f} vs {t_bear:.1f} | {time_str}\n"
            )

        footer = "\n<i>Ketik /signal secara berkala untuk update terbaru. 🌸</i>"
        if any(s.breakdown.oi_funding_score == 0 and s.breakdown.orderbook_score == 0 for s in signals):
            footer = "\n⚠️ <i>Beberapa data lama (skor 0) tercatat saat sistem maintenance. Sinyal baru akan muncul lengkap otomatis.</i>" + footer

        await update.effective_message.reply_html("\n".join(lines) + footer)

    async def cmd_backtest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        args  = ctx.args
        asset = args[0].upper() if args else "BTC"
        await update.effective_message.reply_html(
            f" Backtest untuk <b>{asset}</b> tersedia di dashboard!\n"
            f"Buka: <code>http://localhost:{config.DASHBOARD_PORT}/backtest</code>"
        )

    async def cmd_export(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="export"): return
        
        file_path = config.EXCEL_LOG_PATH
        if not os.path.exists(file_path):
            await update.effective_message.reply_html(
                "❌ <b>Gagal:</b> Belum ada data trade untuk di-export.\n\n"
                "<i>Tunggu sampai ada posisi yang dibuka/ditutup ya!</i>"
            )
            return

        await update.effective_message.reply_chat_action("upload_document")
        try:
            with open(file_path, 'rb') as f:
                await update.effective_message.reply_document(
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
            await update.effective_message.reply_html(f"❌ <b>Gagal ekspor:</b> {str(e)}")

    # ──────────────────────────────────────────
    # MODE COMMANDS (Standard ↔ Scalper)
    # ──────────────────────────────────────────

    async def cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show current trading mode and allow switching."""
        if not self._is_authorized(update): return
        
        # Consistent throttling: 1s for buttons, 5s for commands
        thr = 1 if update.callback_query else 5
        if self._is_throttled(str(update.effective_chat.id), threshold=thr, action_key="mode"):
            if update.callback_query:
                await update.callback_query.answer("⚠️ Terlalu cepat! Tunggu 1 detik.", show_alert=False)
            return
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
        
        msg = update.effective_message
        if msg:
            await msg.reply_html(text, reply_markup=keyboard)
        else:
            log.warning("cmd_mode called but effective_message is None")

    async def cmd_paper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Switch user back to paper mode and clear state."""
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return

        user.config.bot_mode = BotMode.PAPER
        user.paper_balance_usd = config.PAPER_BALANCE_USD
        user.wallet_authorized = False
        user_db.update_user(user)

        # Clear paper state to fresh start
        user_db.clear_paper_positions(chat_id)
        user_db.clear_paper_state(chat_id)
        user_db.clear_risk_state(chat_id)
        
        # Invalidate session to force re-init with PaperExecutor
        if self.bot_app and chat_id in self.bot_app.sessions:
            del self.bot_app.sessions[chat_id]

        await update.effective_message.reply_html(
            "🌸 <b>Kembali ke Paper Mode!</b>\n\n"
            "Saldo virtual Anda telah direset menjadi <b>Rp1.000.000</b>. Semua posisi live (jika ada) telah dihentikan di bot.\n\n"
            "Mari kita belajar hasilkan profit lagi dengan dana simulasi! 🧪✨"
        )

    async def cmd_live(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Start the Live Mode setup with risk warning."""
        if not self._is_authorized(update): return ConversationHandler.END
        if self._is_throttled(str(update.effective_chat.id), threshold=5, action_key="live"): return ConversationHandler.END
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Lanjut Setup", callback_data="setup_live_start"),
                InlineKeyboardButton("❌ Batal", callback_data="close_settings")
            ]
        ])
        
        await update.effective_message.reply_html(
            LIVE_SETUP_RISK_WARNING,
            reply_markup=keyboard
        )
        return WAITING_MAIN_ADDRESS

    async def cmd_scalper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="scalper"): return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ YA, SAYA PAHAM RISIKONYA", callback_data="scalper_confirm")],
            [InlineKeyboardButton("❌ Batal", callback_data="mode_switch:cancel")]
        ])
        await update.effective_message.reply_html(
            "⚠️ <b>PERINGATAN SCALPER MODE</b>\n\n"
            "Mode ini menggunakan leverage 25-35x dan risk 13% per trade.\n"
            "<b>Satu trade jelek bisa hilangkan 13% modal Anda.</b>\n\n"
            "Apakah Anda yakin?",
            reply_markup=keyboard
        )

    async def cmd_standard(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if self._is_throttled(str(update.effective_chat.id), threshold=2, action_key="standard"): return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return
        
        session.user.config.trading_mode = "standard"
        user_db.update_user(session.user)
        await update.effective_message.reply_html("📊 <b>Ganti ke Standard Mode Berhasil!</b>")

    # ──────────────────────────────────────────
    # SIGNAL NOTIFICATION
    # ──────────────────────────────────────────

    async def _cleanup_pending_signals(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        expired = []
        for sig_id, sig in list(self._pending_signals.items()):
            # Handle diff gracefully mapping to seconds
            diff_secs = (now.timestamp() - sig.timestamp.timestamp()) if hasattr(sig.timestamp, 'timestamp') else 600
            if diff_secs > 300:  # 5 menit
                expired.append(sig_id)
                
        for sig_id in expired:
            self._pending_signals.pop(sig_id, None)

    async def send_signal(self, signal: TradeSignal, is_auto: bool = False, target_chat_id: str = None):
        """Send a formatted signal card to Telegram."""
        await self._cleanup_pending_signals()
        
        side_emoji = "🟢" if signal.side == Side.LONG else "🔴"
        side_text  = "LONG" if signal.side == Side.LONG else "SHORT"

        # Map internal regime enum → human-readable label with emoji
        _regime_labels = {
            "trending":  "📈 Trending",
            "ranging":   "↔️ Ranging",
            "volatile":  "⚡ Volatile",
            "low_vol":   "😴 Low-Vol",
            "normal":    "⚖️ Normal",
            "high_vol":  "🌊 High-Vol",
            "extreme":   "🔥 Extreme",
            "unknown":   "⚖️ Normal",   # defensively map unknown → Normal
        }
        regime_raw = signal.regime.value if signal.regime else "normal"
        regime_label = _regime_labels.get(regime_raw.lower(), f"⚖️ {regime_raw.title()}")

        text = SIGNAL_TEMPLATE.format(
            side_emoji=side_emoji,
            asset=signal.asset,
            strength=signal.strength.value,
            score=signal.score,
            side_text=side_text,
            regime=regime_label,
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
        # Always store signal so the "View Reasons" button can fetch details
        self._pending_signals[signal.signal_id] = signal

        if not is_auto:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Ambil Trade", callback_data=f"confirm:{signal.signal_id}"),
                    InlineKeyboardButton("⏭️ Lewati", callback_data=f"skip:{signal.signal_id}")
                ],
                [InlineKeyboardButton("📝 Mengapa Sinyal Ini?", callback_data=f"reasons:{signal.signal_id}")]
            ]
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
        """Premium AI Agent Notification for a successful entry."""
        # Determine labels
        # Use user_db directly for reliable mode labeling in notifications
        chat_id = target_chat_id or (list(self._authorized_chat_ids)[0] if self._authorized_chat_ids else "")
        user = user_db.get_user(chat_id)
        is_scalper = (user.config.trading_mode == "scalper") if user else False
        mode_text = "Scalping ⚡" if is_scalper else "Standar 🌸"
        
        # Style & Narrative
        text = (
            f"🌸 <b>KARA SYSTEM: Position Executed</b>\n"
            f"<i>Saya baru saja menganalisis pasar dan berhasil membuka posisi <b>{pos.side.value.upper()}</b> untuk <b><a href='https://app.hyperliquid.xyz/trade/{pos.asset}'>{pos.asset}</a></b>.</i>\n\n"
            
            f"📦 <b>Market Details</b>\n"
            f"  • Entry   : <code>${format_price(pos.entry_price)}</code>\n"
            f"  • Margin  : <b>{format_idr(pos.margin_usd)}</b> ({format_usd(pos.margin_usd)})\n"
            f"  • Leverage: {pos.leverage}x isolated\n"
            f"  • Mode    : {mode_text}\n\n"
            
            f"🛡️ <b>Risk Profile</b>\n"
            f"  • 🛑 SL   : <code>${format_price(pos.stop_loss)}</code>\n"
            f"  • 🎯 TP1  : <code>${format_price(pos.tp1)}</code>\n"
            f"  • 🎯 TP2  : <code>${format_price(pos.tp2)}</code>\n"
            f"  • 📐 R:R Ratio: <b>{signal.risk_reward_ratio:.2f}x</b>\n"
            f"  • 📊 Score: <b>{signal.score}/100</b>\n\n"
            
            f"<i>Eksekusi selesai. Memantau market untuk exit terbaik. ✨</i>"
        )
        await self.send_text(text, target_chat_id=target_chat_id)


    async def send_position_event(self, action: dict, prices: dict, target_chat_id: str = None):
        """Dispatch the right formatted card based on action type."""
        action_type = action.get("action", "")
        pos_id      = action.get("position_id", "")
        pnl         = action.get("pnl", 0.0)

        # Retrieve position from user session for rich context
        session = await self.bot_app.get_session(target_chat_id) if (self.bot_app and target_chat_id) else None
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
                "🌸 <b>KARA UPDATE: Target Reached</b>\n\n"
                f"<i>I have secured partial profits for <b>{pos.asset}</b>. Taking some chips off the table.</i>\n\n"
                
                f"🎯 <b>TP1 HIT</b>\n"
                f"  • Entry   : <code>${format_price(entry)}</code>\n"
                f"  • Profit  : <b>{pnl_sign}{format_idr(pnl)} ({pnl_sign}{pnl_pct:.2f}%)</b>\n\n"
                
                f"🛡️ <b>Risk Adjustment</b>\n"
                f"  • Status : Sisa 60% masih jalan\n"
                f"  • Action : SL digeser ke Entry ✅\n\n"
                
                f"<i>Continuing to monitor for TP2. ✨</i>"
            )

        elif action_type == "tp2":
            text = (
                "🏁 <b>KARA UPDATE: Final Targets</b>\n\n"
                f"<i>Excellent progress on <b>{pos.asset}</b>. TP2 has been successfully triggered.</i>\n\n"
                
                f"🎯🎯 <b>TP2 HIT</b>\n"
                f"  • Entry   : <code>${format_price(entry)}</code>\n"
                f"  • Profit  : <b>{pnl_sign}{format_idr(pnl)} ({pnl_sign}{pnl_pct:.2f}%)</b>\n\n"
                
                f"🛡️ <b>Trailing Active</b>\n"
                f"  • Status : 25% sisa dengan trailing stop\n"
                f"  • Objective: Maximizing the remainder. 🚀"
            )

        elif action_type == "trailing_stop":
            trail_px = action.get("trail_price", current)
            total_pnl = pnl + pos.pnl_realized
            total_sign = "+" if total_pnl >= 0 else ""
            text = (
                "📍 <b>TRAILING STOP — {asset}</b>\n"
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
        """Send a premium status overview."""
        daily_sign = "+" if acc.daily_pnl >= 0 else ""
        dd_pct     = acc.current_drawdown_pct
        
        status_str = "🟢 SYSTEM NOMINAL"
        if dd_pct >= 15: status_str = "🔴 DANGER: CRITICAL DRAWDOWN"
        elif dd_pct >= 8: status_str = "🟠 WARNING: HIGH VOLATILITY"

        text = (
            "💜 <b>KARA SYSTEM STATUS</b>\n\n"
            f"📊 <b>Kondisi Dana</b>\n"
            f"  • Ekuitas  : <code>{format_idr(acc.total_equity)} (NAV)</code>\n"
            f"  • Wallet   : <code>{format_idr(acc.wallet_balance)}</code>\n"
            f"  • Floating : <code>{format_idr(acc.unrealized_pnl)}</code>\n\n"
            
            f"📈 <b>Performa Harian</b>\n"
            f"  • Profit   : <b>{daily_sign}{format_idr(acc.daily_pnl)} ({daily_sign}{format_pct(acc.daily_pnl_pct)})</b>\n"
            f"  • Drawdown : <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n\n"
            
            f"🎯 <b>Status Operasional</b>\n"
            f"  • Posisi   : {open_count} aset aktif\n"
            f"  • Health   : {status_str}\n\n"
            
            f"<i>Semua sistem beroperasi maksimal, siap tangkap peluang~! ✨</i>"
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
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session:
            await query.answer("Sesi tidak ditemukan. Ketik /start", show_alert=True)
            return

        # Ensure message exists before answering or editing
        if not update.effective_message:
            log.warning(f"on_callback: effective_message is None for action {query.data}")
            await query.answer("Error: Pesan asal hilang.")
            return

        # Immediate UX feedback: answer immediately to stop the button spinner
        # We call answer() with no text to prevent the "Memuat..." toast from appearing
        try:
            await query.answer()
        except BadRequest as e:
            err = str(e).lower()
            if "query is too old" in err or "query id is invalid" in err:
                log.debug(f"Stale callback ignored: {e}")
                return
            raise
        data = query.data or ""
        
        if ":" in data:
            action, sig_id = data.split(":", 1)
        else:
            action, sig_id = data, ""
            
        # ── Refresh Logic ───────────────────────────────────────────
        if action == "refresh_pos":
            await self.cmd_positions(update, ctx)
            return

        # ── /status inline keyboard navigation ──────────────────────
        if action == "status_refresh":
            try:
                # Removed: edit_message_text("⏳ Refreshing data...") to avoid disruptive UI flicker
                
                text, keyboard = await self._get_status_content(session)
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest as e:
                if "not modified" not in str(e).lower(): 
                    log.error(f"Refresh status failed: {e}")
                    await query.answer(f"Refresh gagal: {e}", show_alert=True)
                else:
                    await query.answer("Status sudah up-to-date")
            except Exception as e:
                await query.answer(f"Refresh gagal: {e}", show_alert=True)
            return

        if action == "status_nav":
            # Removed: edit_message_text("⏳ Loading data...") to keep context visible while fetching

            # Route sub-button to the appropriate command handler
            target = sig_id  # pos | pnl | mode | settings
            if target == "pos":
                await self.cmd_positions(update, ctx)
            elif target == "pnl":
                await self.cmd_pnl(update, ctx)
            elif target == "mode":
                await self.cmd_mode(update, ctx)
            elif target == "settings":
                await self.cmd_settings(update, ctx)
            return
        
        # ── Mode Switch Confirmation ──────────────────────────────────
        if action == "mode_switch":
            if sig_id == "scalper":
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚡ YA, SAYA PAHAM RISIKONYA", callback_data="scalper_confirm")],
                    [InlineKeyboardButton("❌ Batal", callback_data="mode_switch:cancel")]
                ])
                await query.edit_message_text(
                    "⚠️ <b>PERINGATAN SCALPER MODE</b>\n\n"
                    "Mode ini menggunakan leverage 25-35x dan risk 13% per trade.\n"
                    "<b>Satu trade jelek bisa hilangkan 13% modal Anda.</b>\n\n"
                    "Apakah Anda yakin?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
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

        if action == "scalper_confirm":
            session.user.config.trading_mode = "scalper"
            user_db.update_user(session.user)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_html(
                "🚀 <b>SCALPER MODE AKTIF!</b>\n\n"
                "⚠️ <b>HATI-HATI:</b> Akun Anda sekarang dalam mode ultra-agresif.\n"
                "• Scan interval: 5 detik\n"
                "• Risk: 13% | Leverage: up to 35x"
            )
            return

        # ── LIVE SETUP FLOW ──
        if query.data == "setup_live_start":
            await query.answer()
            await query.edit_message_text(
                "🛡️ <b>LANGKAH 1: IDENTITAS WALLET</b>\n\n"
                "KARA perlu tahu alamat wallet utama bosku untuk memverifikasi izin Agent nantinya.\n\n"
                "Silakan <b>BALAS</b> pesan ini dengan Alamat Wallet Utama (Public Address) Hyperliquid bosku.\n"
                "<i>Contoh: 0x123...abc</i>",
                parse_mode=ParseMode.HTML
            )
            return WAITING_MAIN_ADDRESS

        elif query.data == "authorize_final":
            await query.answer("Sedang memverifikasi koneksi...", show_alert=False)
            chat_id = str(query.message.chat_id)
            user = user_db.get_user(chat_id)
            
            if not user or not user.hl_agent_address or not user.hl_main_address:
                await query.edit_message_text("❌ Data tidak lengkap. Silakan ketik /live lagi.")
                return

            # Robust verification
            try:
                from data.hyperliquid_client import HyperliquidClient
                verify_client = HyperliquidClient() # Use global config-based client for discovery
                await verify_client.connect()
                
                is_ok = await verify_client.verify_agent_authorization(
                    user.hl_main_address, 
                    user.hl_agent_address
                )
                
                if not is_ok:
                    await query.message.reply_html(
                        f"❌ <b>Verifikasi Gagal!</b>\n\n"
                        f"KARA belum mendeteksi alamat agent ini terhubung ke wallet utama bosku.\n\n"
                        f"• Wallet Utama: <code>{user.hl_main_address}</code>\n"
                        f"• Agent Wallet: <code>{user.hl_agent_address}</code>\n\n"
                        f"Pastikan bosku sudah klik <b>'Authorize'</b> di dashboard Hyperliquid untuk alamat agent di atas."
                    )
                    return
                
                # Connection Success!
                user.wallet_authorized = True
                user.config.bot_mode = BotMode.LIVE
                user_db.update_user(user)
                
                await query.edit_message_text("✅ <b>VERIFIKASI BERHASIL!</b>\n\nAgent Wallet telah terhubung secara sah ke Wallet Utama bosku. KARA sekarang berjalan dalam <b>LIVE MODE</b>. 🚀")
                
                # Re-initialize user session in main app
                if self.bot_app:
                    # Invalidate existing session to force re-creation with LiveExecutor
                    if chat_id in self.bot_app.sessions:
                        del self.bot_app.sessions[chat_id]
                    # Create and init new session
                    await self.bot_app.get_session(chat_id)

                await query.edit_message_text(
                    "✨ <b>Koneksi Berhasil!</b> 🎉\n\n"
                    "Agent Wallet kamu sudah aktif dan terhubung ke Hyperliquid.\n"
                    "KARA sekarang berjalan dalam <b>LIVE MODE</b>.\n\n"
                    "🎯 <i>Gunakan /status untuk melihat saldo live kamu. Happy trading!</i>",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                log.error(f"Authorization verify failed for {chat_id}: {e}")
                await query.answer(f"❌ Verifikasi Gagal: {str(e)}", show_alert=True)

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

        # ── TOS AGREE ──────────────────────────────────────────────────
        if action == "tos_agree":
            session.user.tos_agreed = True
            user_db.update_user(session.user)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_html(
                "✨ <b>Terima kasih sudah memahami, User!</b> 🌸\n\n"
                "Sekarang User bisa mengakses seluruh fitur KARA. Mari kita cari profit dengan bijak~! 📈\n\n"
                "Ketik /help untuk memulai."
            )
            return

        # ── SETTINGS CALLBACKS ──────────────────────────────────────────
        if action == "set_cfg":
            if "min_score" in sig_id:
                await query.answer("🔒 Score dikunci sistem.", show_alert=True)
                return ConversationHandler.END
            ctx.user_data["editing_field"] = sig_id # sig_id contains field name
            clean_name = sig_id.replace("std_", "").replace("scl_", "").replace("_", " ").title()
            await query.message.reply_html(f"📝 <b>Edit {clean_name}</b>\n\nSilakan masukkan nilai baru untuk parameter ini (angka):")
            return WAITING_CONFIG_VALUE

        if action == "switch_settings_view":
            current_view = ctx.user_data.get("settings_view_mode", "standard")
            ctx.user_data["settings_view_mode"] = "scalper" if current_view == "standard" else "standard"
            await self.cmd_settings(update, ctx)
            return

        if action == "reset_cfg_defaults":
            if session:
                u = session.user
                u.config.std_min_score_to_signal = 58
                u.config.std_min_score_to_auto_trade = 65
                u.config.std_max_leverage = 10
                u.config.std_max_concurrent_positions = 10
                u.config.scl_min_score_to_signal = 50
                u.config.scl_min_score_to_auto_trade = 57
                u.config.scl_max_leverage = 20
                u.config.scl_max_concurrent_positions = 3
                user_db.update_user(u)
                await query.answer("♻️ Konfigurasi direset ke default!")
                await self.cmd_settings(update, ctx)
            return

        signal = self._pending_signals.get(sig_id)

        if action == "confirm" and signal:
            signal.confirmed = True
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_html(
                f"⏳ <b>Konfirmasi diterima.</b>\n"
                f"KARA sedang mencoba membuka posisi <b>{signal.asset}</b>..."
            )

            if self._on_confirm:
                ok, _ = await self._on_confirm(signal, chat_id)
                if ok:
                    await query.message.reply_html(
                        f"✅ <b>Posisi {signal.asset} berhasil dibuka.</b>"
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
                safe_r = html.escape(r.strip('• '))
                row = f"• {safe_r}"
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
            # De-duplicate warning lines while preserving order
            uniq_warns = []
            seen_warns = set()
            for w in (signal.breakdown.warnings or []):
                key = (w or "").strip().lower()
                if not key or key in seen_warns:
                    continue
                seen_warns.add(key)
                uniq_warns.append(w)
            warnings_text = (
                "\n\n⚠️ <b>Perhatian:</b>\n" +
                "\n".join(f"• {html.escape(w)}" for w in uniq_warns)
                if uniq_warns else ""
            )

            # Show active system policies/features so explanation matches latest bot behavior.
            system_notes = [
                "🚀 Full-auto execution only: sinyal dikirim saat dieksekusi.",
                "🧭 Scalper pakai konfirmasi MTF 15m.",
                "🧩 Market structure (HH/HL) dibobotkan ke skor.",
                "🧠 Meta-scoring outcome aktif (boost/penalty berbasis winrate pola).",
            ]
            system_text = "⚙️ <b>Sistem KARA Aktif:</b>\n" + "\n".join(f"• {x}" for x in system_notes)
            
            explanation = (
                f"Tentu! Ini analisis mendalamku untuk <b>{signal.asset}</b>:\n\n"
                f"{reasons_text}"
                f"{warnings_text}\n\n"
                f"{system_text}\n\n"
                f"<i>Semoga membantu kamu membuat keputusan yang tepat! 🌸</i>"
            )
            await query.message.reply_html(explanation)

    # ──────────────────────────────────────────
    # AUTH
    # ──────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """KARA Auth: user harus sudah mengisi access code dan aktif."""
        if not update or not update.effective_chat:
            return False

        chat_id = str(update.effective_chat.id)

        # Admin selalu diizinkan (TELEGRAM_CHAT_ID)
        if config.TELEGRAM_CHAT_ID and chat_id == str(config.TELEGRAM_CHAT_ID):
            return True

        # Cek apakah user ada di DB dan sudah is_authorized
        user = user_db.get_user(chat_id)
        if not user:
            return False
        if not getattr(user, 'is_authorized', False):
            return False
        if getattr(user, 'is_active', True) is False:
            return False

        # TOS block: tolak semua kecuali /start dan /help
        if update.message and update.message.text:
            cmd = update.message.text.split()[0].lower()
            if cmd not in ["/start", "/help"] and not getattr(user, 'tos_agreed', False):
                return False

        return True

    def _extract_changelog_items(self, version: str, max_items: int = 6) -> list[str]:
        """
        Read latest items from CHANGELOG.md for a given version.
        Supports headings like:
          ## v6.0.1
          ## 6.0.1
        """
        changelog_path = os.path.join(os.getcwd(), "CHANGELOG.md")
        if not os.path.exists(changelog_path):
            return []

        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return []

        target = version.strip().lower().lstrip("v")
        start_idx = -1
        for i, ln in enumerate(lines):
            m = re.match(r"^\s*##\s+v?([0-9][0-9A-Za-z\.\-\_]*)\s*$", ln.strip())
            if not m:
                continue
            heading_ver = m.group(1).lower().lstrip("v")
            if heading_ver == target:
                start_idx = i + 1
                break

        if start_idx < 0:
            return []

        items = []
        for ln in lines[start_idx:]:
            s = ln.strip()
            if s.startswith("## "):
                break
            if s.startswith("- ") or s.startswith("* "):
                items.append(s[2:].strip())
                if len(items) >= max_items:
                    break
        return items

    def _build_dynamic_update_items(self, version: str) -> list[str]:
        """
        Build update bullets with priority:
        1) ENV KARA_UPDATE_NOTES (newline / '||' separated)
        2) CHANGELOG.md section matching version
        3) Auto-generate from latest git commit/diff
        4) Runtime fallback summary from current config
        """
        env_notes = os.getenv("KARA_UPDATE_NOTES", "").strip()
        if env_notes:
            raw = env_notes.replace("||", "\n").splitlines()
            notes = [x.strip(" -\t") for x in raw if x.strip()]
            if notes:
                return notes[:8]

        changelog_items = self._extract_changelog_items(version=version, max_items=8)
        if changelog_items:
            return changelog_items

        # 1. Try reading from AI-generated changelog.json
        changelog_path = os.path.join(os.getcwd(), "data", "changelog.json")
        if os.path.exists(changelog_path):
            try:
                import json
                with open(changelog_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "features" in data and isinstance(data["features"], list):
                        return data["features"]
            except Exception as e:
                log.warning(f"Failed to read AI changelog: {e}")

        # 2. Auto-generate from latest commit (fallback)
        git_items = self._build_git_auto_notes()
        if git_items:
            return git_items

        # 3. Static fallback (Final safety)
        return [
            "Peningkatan performa mesin scoring.",
            "Optimalisasi koneksi WebSocket.",
            "Penyempurnaan manajemen risiko."
        ]

    def _build_git_auto_notes(self) -> list[str]:
        """
        Generate friendly release bullets from the latest git commit.
        Safe fallback: returns [] if git is unavailable in runtime.
        """
        try:
            subject = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=os.getcwd(),
                text=True,
                timeout=2
            ).strip()
            body = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%b"],
                cwd=os.getcwd(),
                text=True,
                timeout=2
            ).strip()
            changed = subprocess.check_output(
                ["git", "show", "--name-only", "--pretty=", "HEAD"],
                cwd=os.getcwd(),
                text=True,
                timeout=2
            )
        except Exception:
            return []

        files = [x.strip() for x in changed.splitlines() if x.strip()]
        if not subject and not files:
            return []

        bullets = []
        if subject:
            bullets.append(f"Update utama: {subject}.")

        # High-level grouping by area to keep message readable for users.
        if any(f.startswith("engine/") for f in files):
            bullets.append("Peningkatan logika analisa sinyal dan akurasi scoring market.")
        if any(f.startswith("risk/") for f in files):
            bullets.append("Perbaikan proteksi risiko agar eksekusi lebih aman dan stabil.")
        if any(f.startswith("execution/") for f in files):
            bullets.append("Optimalisasi alur eksekusi posisi untuk mengurangi bug saat trade berjalan.")
        if any(f.startswith("notify/") for f in files):
            bullets.append("Penyempurnaan pengalaman notifikasi Telegram supaya lebih informatif dan rapi.")
        if any(f.startswith("dashboard/") for f in files):
            bullets.append("Pembaruan tampilan/monitoring dashboard untuk visibilitas yang lebih baik.")
        if any(f.startswith("config") for f in files):
            bullets.append("Penyesuaian konfigurasi inti sistem sesuai kalibrasi terbaru.")

        if body:
            # Pick up to two meaningful lines from commit body.
            body_lines = [ln.strip("- ").strip() for ln in body.splitlines() if ln.strip()]
            for ln in body_lines[:2]:
                bullets.append(ln if ln.endswith(".") else f"{ln}.")

        # Deduplicate while preserving order, cap to 8 items.
        uniq = []
        seen = set()
        for b in bullets:
            key = b.lower()
            if key in seen:
                continue
            uniq.append(b)
            seen.add(key)
            if len(uniq) >= 8:
                break
        return uniq

    def _get_changelog_text(self, version: str, release_tag: str = "", extra_notes: Optional[list[str]] = None) -> str:
        """KARA dynamic update card with friendly style ✨."""
        if version == "6.2.0":
            bullets = [
                "<b>Arsitektur Multi-User</b>: KARA kini mendukung banyak pengguna dengan dompet terpisah secara sirkular.",
                "<b>Secure Agent Wallet</b>: Sistem L1 Agent yang terverifikasi on-chain untuk keamanan maksimal.",
                "<b>Enkripsi Tingkat Tinggi (Fernet)</b>: Semua private key agen kini dienkripsi secara militer di database.",
                "<b>Locked-Down Onboarding</b>: Alur aktivasi Live Mode yang jauh lebih aman dan teratur.",
                "<b>Smart Multi-Step Verification</b>: Verifikasi izin agen langsung ke blockchain Hyperliquid.",
                "<b>Maintenance Script</b>: Fitur hard-reset saldo untuk simulasi ulang yang bersih."
            ]
        else:
            bullets = self._build_dynamic_update_items(version=version)

        if extra_notes:
            bullets = list(extra_notes) + bullets
        items = "\n".join([f"• {b}" for b in bullets]) if bullets else "• Perbaikan stabilitas dan optimasi sistem."
        
        # Build dynamic tag line
        rel_tag = release_tag or f"v{version}"
        
        return (
            f"✨ <b>KARA System Update v{version}</b> 🌸\n"
            f"Release: <code>{rel_tag}</code>\n"
            "──────────────────────────\n"
            "Hai, User! Aku baru selesai update sistem. Ini perubahan terbaru dari KARA:\n\n"
            f"{items}\n\n"
            "Terima kasih sudah tetap bareng KARA 💜\n"
            "Aku siap lanjut pantau market dengan performa terbaru~"
        )

    async def send_update_notification(
        self,
        chat_id: str,
        silent: bool = True,
        release_tag: str = "",
        extra_notes: Optional[list[str]] = None
    ):
        """Send the stylized update notification to a specific user."""
        if not self._app: return False
        try:
            text = self._get_changelog_text(
                config.KARA_VERSION,
                release_tag=release_tag,
                extra_notes=extra_notes
            )
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_notification=silent
            )
            return True
        except Exception as e:
            log.error(f"Failed to send update to {chat_id}: {e}")
            return False

    def _is_throttled(self, chat_id: str, threshold: Optional[float] = None, action_key: str = "generic") -> bool:
        """
        Smart rate-limit:
        - per action key (so /status then /pos stays responsive)
        - tiny global guard to block burst spam/flood
        Returns True if request should be throttled.
        """
        import time
        now = time.time()
        
        # Default command cooldown is now shorter for better responsiveness.
        limit = float(threshold if threshold is not None else 2.0)
        global_limit = 0.25

        bucket = self._last_cmd_ts.get(chat_id)
        # Backward compatibility if old in-memory shape was float.
        if isinstance(bucket, (int, float)):
            bucket = {"__global__": float(bucket)}
        elif not isinstance(bucket, dict):
            bucket = {}

        last_global = float(bucket.get("__global__", 0.0))
        if now - last_global < global_limit:
            return True

        key = action_key or "generic"
        last_action = float(bucket.get(key, 0.0))
        if now - last_action < limit:
            return True

        bucket["__global__"] = now
        bucket[key] = now
        self._last_cmd_ts[chat_id] = bucket
        return False

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler for the Telegram bot."""
        log.error(f"⚠️ Telegram Error: {context.error}")
        if update and isinstance(update, Update) and update.effective_message:
            try:
                err_str = str(context.error)
                # Ignore common harmless errors
                if "Message is not modified" in err_str: return
                if "Forbidden: bot was blocked" in err_str: return
                if "Query is too old" in err_str: return
                if "query id is invalid" in err_str.lower(): return
                
                await update.effective_message.reply_html(
                    f"❌ <b>Bot Error:</b>\n<code>{err_str[:200]}</code>\n\n"
                    f"<i>Kejadian ini telah dicatat untuk perbaikan.</i>"
                )
            except: pass
