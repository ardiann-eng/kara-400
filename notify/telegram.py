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
WAITING_BYBIT_KEY = 3
WAITING_BYBIT_SECRET = 4

DAILY_REPORT_TEMPLATE = """
📊 <b>KARA DAILY INSIGHTS</b> 🌸
📅 <i>Laporan Harian: {date}</i>

💰 <b>KESEHATAN PORTOFOLIO</b>
• Ekuitas Total  : <code>{total_equity}</code>
• Saldo Dompet   : <code>{wallet_balance}</code>
• Saldo Tersedia : <code>{available}</code>

📈 <b>PERFORMA HARI INI</b>
• Daily PnL      : <b>{daily_pnl_line}</b> {pnl_emoji}
• Posisi Aktif   : <b>{pos_count} terbuka</b>
• Max Drawdown   : <b>{drawdown}</b>
• Win Rate Hari Ini : <b>{win_rate}</b>
• Total Trades   : <b>{total_trades}</b>

🛡️ <b>STATUS SISTEM</b>
• Trading Mode   : {mode_icon} <b>{mode_text}</b>
• Eksekusi       : <b>{exec_mode}</b>
• Bot Status     : {status_icon} <b>{status_text}</b>
• Paused Karena  : <b>{pause_reason}</b>

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
⚠️ <b>Live Mode — Konfirmasi Risiko</b>

Kamu akan mengaktifkan trading dengan dana nyata di Hyperliquid. Baca ini sebelum lanjut.

<b>Yang perlu dipahami:</b>
1. Setiap trade yang dieksekusi KARA bersifat nyata dan menggunakan saldo akun kamu.
2. KARA menggunakan <b>Agent Wallet</b> — kamu tidak perlu memberikan private key utama, hanya izin trading.
3. Futures crypto berisiko tinggi. Gunakan hanya dana yang siap untuk hilang sepenuhnya.

<i>Lanjut untuk membuat Agent Wallet dan mengaktifkan Live Mode?</i>
"""

AGENT_WALLET_CREATED_TEMPLATE = """
✅ <b>Agent Wallet berhasil dibuat</b>

Simpan data ini sekarang. Data ini hanya ditampilkan <b>satu kali</b> dan tidak bisa dipulihkan.

🔑 <b>Agent Wallet Address</b>
<code>{address}</code>

🔑 <b>Agent Private Key</b>
<code>{private_key}</code>

<b>Langkah selanjutnya:</b>
1. Buka <a href="https://app.hyperliquid.xyz/API">Hyperliquid API Dashboard</a>
2. Connect menggunakan <b>Main Wallet</b> kamu (wallet yang menyimpan dana).
3. Klik <b>"Authorize API Wallet"</b> dan masukkan Agent Wallet Address di atas.
4. Setelah selesai, tekan tombol <b>"✅ Saya Sudah Authorize"</b> di bawah.

<i>Agent Wallet hanya memiliki izin untuk membuka dan menutup posisi. Penarikan dana tetap harus dilakukan melalui Main Wallet kamu.</i>
"""

TOS_TEXT = """
⚖️ <b>Syarat Penggunaan — KARA AI Agent</b>

Sebelum menggunakan KARA, harap baca dan setujui ketentuan berikut.

1. 🛡️ <b>Tanggung Jawab Penuh:</b> KARA adalah sistem analisis berbasis data market. Semua keputusan trading sepenuhnya ada di tanganmu.
2. 📉 <b>Risiko Modal:</b> Trading futures berisiko tinggi. Modal dapat berkurang atau hilang seluruhnya.
3. 🚫 <b>Bukan Penasihat Keuangan:</b> KARA bukan penasihat keuangan berlisensi. Gunakan analisisnya sebagai referensi, bukan panduan mutlak.
4. ⚙️ <b>Risiko Teknis:</b> Kamu memahami bahwa gangguan koneksi atau error API pihak ketiga dapat mempengaruhi eksekusi.

<i>KARA menganalisis, kamu yang memutuskan.</i>

<b>Setujui ketentuan di atas untuk melanjutkan.</b>
"""

RISK_WARNING_TEXT = """
⚠️ <b>Peringatan Risiko — Live Mode</b>

Kamu akan beralih ke Live Trading menggunakan dana nyata. Perhatikan hal berikut sebelum melanjutkan.

📉 <b>Performa Masa Lalu</b>
Hasil di Paper Mode tidak menjamin hasil serupa di Live Mode. Kondisi market selalu berubah.

💸 <b>Risiko Modal</b>
Gunakan hanya dana yang kamu siap untuk kehilangkan. Jangan gunakan dana untuk kebutuhan sehari-hari.

⚡ <b>Leverage</b>
Leverage memperbesar potensi keuntungan sekaligus mempercepat risiko likuidasi.

⚖️ <b>Keputusan Akhir</b>
KARA memberikan sinyal dan analisis, namun konfirmasi eksekusi tetap menjadi tanggung jawabmu.

<i>KARA menerapkan manajemen risiko ketat, tetapi kewaspadaan tetap ada di tanganmu.</i>
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
        self._pending_pnl_cards: Dict[str, dict] = {}      # pos_id -> {pos, close_data, account}
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

    def _sync_mode_manager(self):
        """
        Sync the global ModeManager to reflect the fastest mode across all active users.
        Called after every user mode switch so scan_interval stays correct.
        If ANY user is in scalper mode, mode_manager is set to scalper (fastest wins).
        If ALL users are standard, mode_manager is set to standard.
        """
        if not self.mode_manager:
            return
        try:
            if getattr(config, "FORCE_SCALPER_ONLY", False):
                target = "scalper"
            else:
                all_users = user_db.get_all_users()
                any_scalper = any(
                    getattr(u.config, 'trading_mode', 'standard') == 'scalper'
                    for u in all_users
                    if u.is_authorized
                )
                target = 'scalper' if any_scalper else 'standard'
            if self.mode_manager.mode != target:
                self.mode_manager.switch(target)
                log.info(f"[MODE] mode_manager synced to '{target}' after user switch")
        except Exception as e:
            log.error(f"_sync_mode_manager failed: {e}")

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
                    WAITING_BYBIT_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_bybit_key)],
                    WAITING_BYBIT_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_bybit_secret)],
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
                CommandHandler("journal",  self.cmd_journal),
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
                CommandHandler("resetml",      self.cmd_reset_ml),
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
                        BotCommand("journal",  "Statistik & Jurnal performa trading"),
                        BotCommand("paper",    "Kembali ke Paper Mode & Reset Saldo"),
                        BotCommand("live",     "Setup Live Mode (Bybit)"),
                        BotCommand("settings", "Pusat Kendali (Threshold & Leverage)"),
                        BotCommand("help",     "Daftar instruksi lengkap"),
                        BotCommand("export",   "Export riwayat trade ke Excel"),
                    ],
                    scope=BotCommandScopeDefault()
                )
                log.info("✅ Telegram command menu registered")
            except Exception as e:
                log.warning(f"Could not register command menu: {e}")

            # No startup broadcast — quiet boot (user request: no "Online" spam per deploy)

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
        threshold_str = "Score ≥ 60 (TETAP)" if is_scl else "Score ≥ 65 (TETAP)"

        text = (
            f"⚙️ <b>KARA Settings — {mode_label}</b>\n\n"
            f"🔒 <b>Parameter Dikunci Sistem</b>\n"
            f"• Auto-Execute Threshold : <b>{threshold_str}</b>\n\n"
            f"⚙️ <b>Parameter Yang Bisa Kamu Ubah</b>\n"
            f"• Max Leverage         : <b>{getattr(user.config, pfx+'max_leverage')}x</b>\n"
            f"• Max Open Positions   : <b>{getattr(user.config, pfx+'max_concurrent_positions')}</b>\n\n"
            f"<i>Threshold skor dikunci untuk menjaga konsistensi dan keamanan trading. ✨</i>"
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
        else:
            await update.effective_message.reply_html(text, reply_markup=reply_markup)

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
            return ConversationHandler.END

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
        
        await update.effective_message.reply_html(
            AGENT_WALLET_CREATED_TEMPLATE.format(
                address=address,
                private_key=private_key
            ),
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        return ConversationHandler.END

    async def handle_bybit_key(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return ConversationHandler.END
        api_key = (update.effective_message.text or "").strip()
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        if len(api_key) < 8:
            await update.effective_chat.send_message("API key tidak valid. Ketik /live untuk ulang.")
            return ConversationHandler.END
        ctx.user_data["pending_bybit_key"] = api_key
        await update.effective_chat.send_message(
            "Kirim <b>API Secret Bybit testnet</b>. Pesan akan langsung dihapus.",
            parse_mode=ParseMode.HTML,
        )
        return WAITING_BYBIT_SECRET

    async def handle_bybit_secret(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return ConversationHandler.END
        api_secret = (update.effective_message.text or "").strip()
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        api_key = ctx.user_data.get("pending_bybit_key")
        if not api_key or len(api_secret) < 8:
            ctx.user_data.pop("pending_bybit_key", None)
            await update.effective_chat.send_message("Credential tidak lengkap. Ketik /live untuk ulang.")
            return ConversationHandler.END
        from data.bybit_client import BybitClient
        from core.startup_validation import validate_bybit_preflight
        client = BybitClient(
            api_key=api_key,
            api_secret=api_secret,
            testnet=True,
            recv_window=config.BYBIT_RECV_WINDOW,
        )
        try:
            await client.connect()
            await client.sync_clock()
            result = await client.preflight()
            errors = validate_bybit_preflight(result)
            if errors:
                raise RuntimeError("; ".join(errors))
        except Exception as e:
            ctx.user_data.pop("pending_bybit_key", None)
            await update.effective_chat.send_message(
                f"Preflight Bybit gagal: <code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END
        finally:
            await client.close()
        ctx.user_data["pending_bybit_secret"] = api_secret
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Aktifkan Live Testnet", callback_data="bybit_live_confirm"),
            InlineKeyboardButton("Batal", callback_data="bybit_live_cancel"),
        ]])
        await update.effective_chat.send_message(
            "Preflight Bybit testnet lulus.\n\n"
            f"Saldo tersedia: <b>${result.available_usdt:,.2f}</b>\n"
            "Mode posisi: <b>one-way</b>\n"
            "Environment: <b>TESTNET</b>\n\n"
            "Konfirmasi untuk menyimpan credential terenkripsi dan mengaktifkan live testnet.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
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
        kill_str  = "🚨 Aktif (STOP)" if acc.kill_switch_active else "✅ Aman"

        pnl_sign   = "+" if acc.unrealized_pnl > 0 else ""
        daily_sign = "+" if acc.daily_pnl > 0 else ""
        auto_str   = "🚀 Full-Auto" if config.FULL_AUTO else "🛡️ Semi-Auto"
        pos_len    = len([p for p in acc.positions if p.status.value == 'open'])

        # Realized daily PnL = daily_pnl - unrealized (avoid double-counting float)
        realized_pnl     = acc.daily_pnl - acc.unrealized_pnl
        realized_sign    = "+" if realized_pnl > 0 else ""
        realized_pnl_pct = realized_pnl / max(acc.wallet_balance - realized_pnl, 1) * 100

        text = (
            f"🌸 <b>KARA System Status</b>\n\n"
            f"💜 <b>Profil & Eksekusi</b>\n"
            f"  • Mode: <b>{mode_str}</b> ({auto_str})\n"
            f"  • Status: {pause_str}\n"
            f"  • Kill-Switch: {kill_str}\n\n"
            f"📊 <b>Kondisi Dana</b>\n"
            f"  • Ekuitas: <code>{format_idr(acc.total_equity)}</code>\n"
            f"  • Saldo Dompet: <code>{format_idr(acc.wallet_balance)}</code>\n"
            f"  • Saldo Tersedia: <code>{format_idr(acc.available)}</code>\n"
            f"  • Unrealized PnL: <b>{pnl_sign}{format_idr(acc.unrealized_pnl)}</b> (posisi terbuka)\n\n"
            f"📈 <b>Performa Harian</b>\n"
            f"  • Realized PnL: <b>{realized_sign}{format_idr(realized_pnl)}</b> ({realized_sign}{realized_pnl_pct:.2f}%)\n"
            f"  • Total PnL Hari Ini: <b>{daily_sign}{format_idr(acc.daily_pnl)}</b> ({daily_sign}{format_pct(acc.daily_pnl_pct)})\n"
            f"  • Max Drawdown: <code>{format_pct(acc.current_drawdown_pct, show_sign=False)}</code>\n\n"
            f"🎯 <b>Posisi Terbuka:</b> {pos_len} aset\n"
        )

        bybit = session.bybit_status() if hasattr(session, "bybit_status") else None
        if bybit:
            rest = "SEHAT" if bybit["rest_healthy"] else "GANGGUAN"
            ws = "CONNECTED" if bybit["ws_connected"] and not bybit["ws_stale"] else "STALE"
            ws_stale_s = bybit.get("ws_stale_duration_s", 0)
            reconcile = bybit["last_reconciliation_at"]
            reconcile_text = (
                datetime.fromtimestamp(reconcile, timezone.utc).strftime("%H:%M:%S UTC")
                if reconcile
                else "belum ada"
            )
            text += (
                f"\n<b>Bybit Execution</b>\n"
                f"  • Venue: <code>{bybit['environment']}</code>\n"
                f"  • REST: <b>{rest}</b> ({bybit['rest_latency_ms']:.0f} ms)\n"
                f"  • Private WS: <b>{ws}</b> ({ws_stale_s:.0f}s stale)\n"
                f"  • Reconciliation: <code>{reconcile_text}</code>\n"
                f"  • Mismatch: {bybit['reconciliation_mismatch_count']}\n"
                f"  • Hard SL: {bybit['hard_sl_healthy_count']} sehat, "
                f"{bybit['hard_sl_missing_count']} hilang\n"
                f"  • Entry/Fill/Close: {bybit['entry_latency_ms']:.0f}/"
                f"{bybit['fill_latency_ms']:.0f}/{bybit['close_latency_ms']:.0f} ms\n"
                f"  • Price gap: {bybit['price_bridge_gap_pct']:.3%}\n"
                f"  • Circuit: {'OPEN' if bybit['circuit_open'] else 'CLOSED'}"
                f" ({bybit['circuit_remaining_s']:.0f}s)\n"
                f"  • Live risk reject: {bybit.get('risk_rejection_count', 0)}"
                f" ({bybit.get('last_risk_rejection_reason') or 'none'})\n"
            )
            live_limits = bybit.get("live_risk_limits")
            if live_limits:
                text += (
                    f"  • Live caps: {live_limits['max_leverage']}x, "
                    f"{live_limits['max_positions']} posisi, "
                    f"risk {live_limits['max_risk_per_trade_pct']:.1%}/trade\n"
                    f"  • Allowlist: <code>{', '.join(live_limits['asset_allowlist'])}</code>\n"
                    f"  • Spread/slippage: {live_limits['max_spread_pct']:.2%}/"
                    f"{live_limits['max_slippage_pct']:.2%}\n"
                )

        if risk_status.get("in_cooldown"):
            text += "❄️ <i>Post-loss cooldown aktif. Break dulu yaa~</i>"
        else:
            text += "<i>Semua sistem beroperasi maksimal, siap tangkap peluang~! ✨</i>"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💼 Posisi",   callback_data="status_nav:pos"),
                InlineKeyboardButton("📖 Journal",  callback_data="status_nav:journal")
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
            lev_pos    = max(int(getattr(pos, "leverage", 1) or 1), 1)
            # Show ROE (price_move × lev), not raw price %
            float_pct  = pos.floating_pct(current) * lev_pos * 100
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

            # TP status — distinguish hit vs pending (flags from executor)
            tp1_hit = bool(getattr(pos, "tp1_hit", False))
            tp2_hit = bool(getattr(pos, "tp2_hit", False))
            # Soft zone: price already past TP but flag not set yet (display only)
            try:
                if pos.side.value.lower() == "long":
                    at_tp1 = current >= float(pos.tp1)
                    at_tp2 = current >= float(pos.tp2)
                else:
                    at_tp1 = current <= float(pos.tp1)
                    at_tp2 = current <= float(pos.tp2)
            except Exception:
                at_tp1 = at_tp2 = False

            if tp1_hit:
                tp1_label = f"✅ TP1 HIT ${format_price(pos.tp1)}"
            elif at_tp1:
                tp1_label = f"🟡 TP1 zone ${format_price(pos.tp1)}"
            else:
                tp1_label = f"⏳ TP1 ${format_price(pos.tp1)}"

            if tp2_hit:
                tp2_label = f"✅ TP2 HIT ${format_price(pos.tp2)}"
            elif at_tp2:
                tp2_label = f"🟡 TP2 zone ${format_price(pos.tp2)}"
            else:
                tp2_label = f"⏳ TP2 ${format_price(pos.tp2)}"

            # SL — BEP after TP1 partial
            sl_tag = "BEP" if tp1_hit else "SL"
            liq_str = (
                f"${format_price(pos.liquidation_price)}"
                if pos.liquidation_price else "?"
            )

            # Clean ticker for Hyperliquid URL (strip 'k' for 1000x assets)
            url_ticker = pos.asset[1:] if pos.asset.startswith("k") and len(pos.asset) > 1 else pos.asset
            hl_link = f"https://app.hyperliquid.xyz/trade/{url_ticker}"
            asset_html = f"<a href='{hl_link}'>{pos.asset}</a>"

            text += (
                f"\n"
                f"<b>{asset_html} {side_str} {pos.leverage}x</b>   "
                f"{pnl_emoji} {pnl_sign}{float_pct:.2f}% · {duration}\n"
                f"Entry ${format_price(pos.entry_price)} → ${format_price(current)}\n"
                f"🛡️ {sl_tag} ${format_price(pos.stop_loss)} · 💥 Liq {liq_str}\n"
                f"{tp1_label} · {tp2_label}\n"
            )

            # Close buttons — clean labels, minimal emoji
            side_short = "L" if pos.side.value == "long" else "S"
            close_buttons.append(
                InlineKeyboardButton(
                    f"Close {pos.asset} {side_short}",
                    callback_data=f"close_req:{pos.asset}:{pos.side.value}"
                )
            )

        # Build close button rows (2 per row)
        for i in range(0, len(close_buttons), 2):
            keyboard_rows.append(close_buttons[i:i+2])

        # Footer
        text += "\n<i>Santai dulu, biarkan profit kita mengalir~ 🌸</i>"

        keyboard_rows.append([
            InlineKeyboardButton("Close all", callback_data="close_all_req")
        ])

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
            except Exception as e:
                # Message deleted / not modified → send a fresh list
                log.debug(f"Positions edit failed, replying new: {e}")
                try:
                    await update.effective_message.reply_html(text, reply_markup=reply_markup)
                except Exception:
                    await self.send_text(text, target_chat_id=str(update.effective_chat.id), reply_markup=reply_markup)
        else:
            await update.effective_message.reply_html(text, reply_markup=reply_markup)

    async def cmd_journal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show full trade journal to the user."""
        if not self._is_authorized(update): return
        
        # Consistent throttling: 1s for buttons, 5s for commands
        thr = 1 if update.callback_query else 5
        if self._is_throttled(str(update.effective_chat.id), threshold=thr, action_key="journal"):
            if update.callback_query:
                await update.callback_query.answer("⚠️ Terlalu cepat! Tunggu 1 detik.", show_alert=False)
            return
        chat_id = str(update.effective_chat.id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        if not session: return await self.cmd_start(update, ctx)
        
        try:
            # Ambil semua trade tanpa limit untuk statistik akurat
            from core.db import user_db
            history = user_db.get_trade_history(chat_id, limit=9999)

            # [NEW] Sertakan posisi aktif juga di ringkasan jika ada
            open_positions = session.executor.open_positions if session else []

            # Hanya hitung trade "close" (bukan "open") untuk history
            history = [t for t in history if t.get("type") == "close" or "pnl" in t]

            def get_pnl(t): return float(t.get("pnl") or t.get("pnl_usd") or 0)
            def get_reason(t): return t.get("reason") or t.get("close_reason") or "manual"
            def sign(v): return "+" if v >= 0 else ""
            def pnl_str(v): return f"{sign(v)}{format_idr(v)}"

            def fmt_hold(mins):
                if mins < 60:
                    return f"{mins:.0f} menit"
                h = int(mins // 60)
                m = int(mins % 60)
                return f"{h}j {m}m" if m else f"{h} jam"

            def fmt_exit_label(key):
                labels = {
                    "tp1": "TP1", "tp2": "TP2",
                    "trailing_stop": "Trailing", "trailing": "Trailing",
                    "stop_loss": "Stop Loss", "sl": "Stop Loss",
                    "time_exit": "Time Exit", "manual": "Manual",
                    "close_all": "Close All",
                }
                return labels.get(key, key.replace("_", " ").title())

            trades = len(history)
            wins   = sum(1 for t in history if get_pnl(t) > 0)
            losses = trades - wins
            win_rate  = (wins / trades * 100) if trades > 0 else 0
            total_pnl = sum(get_pnl(t) for t in history)

            if trades == 0 and not open_positions:
                text = (
                    "📔 <b>Trade Journal</b>\n\n"
                    "Belum ada trade yang tercatat.\n\n"
                    "<i>KARA akan merekam otomatis setiap posisi yang ditutup. ✨</i>"
                )
            else:
                text = f"📔 <b>Trade Journal</b>  <code>({trades} closed, {len(open_positions)} active)</code>\n\n"
                
                # Tambahkan info posisi aktif jika ada
                if open_positions:
                    text += "⏳ <b>Posisi Sedang Berjalan:</b>\n"
                    for p in open_positions:
                        # Estimate floating PnL
                        price = 0
                        if self.bot_app and self.bot_app.cache:
                            ctx = self.bot_app.cache.funding.get(p.asset)
                            if ctx: price = float(ctx.get("markPx", 0))
                        
                        f_pnl = p.unrealized_pnl(price) if price > 0 else 0
                        p_sign = "+" if f_pnl >= 0 else ""
                        text += f"  • {p.asset} {p.side.value.upper()}: <b>{p_sign}{format_idr(f_pnl)}</b> (floating)\n"
                    text += "\n"

                # Statistik per aset
                asset_stats: Dict[str, Dict] = {}
                exit_stats:  Dict[str, Dict] = {}
                hold_total_mins = 0.0
                hold_count = 0
                streak_cur = 0
                streak_max_w = 0
                streak_max_l = 0
                _last_sign = None

                for t in history:
                    a  = t.get("asset") or "?"
                    p  = get_pnl(t)
                    ex = get_reason(t)

                    if a not in asset_stats:
                        asset_stats[a] = {"trades": 0, "wins": 0, "pnl": 0.0}
                    asset_stats[a]["trades"] += 1
                    asset_stats[a]["pnl"]    += p
                    if p > 0:
                        asset_stats[a]["wins"] += 1

                    if ex not in exit_stats:
                        exit_stats[ex] = {"trades": 0, "wins": 0}
                    exit_stats[ex]["trades"] += 1
                    if p > 0:
                        exit_stats[ex]["wins"] += 1

                    # Hold time
                    ts     = t.get("timestamp") or t.get("opened_at")
                    closed = t.get("closed_at")
                    if ts and closed:
                        try:
                            def _parse_dt(v):
                                if isinstance(v, (int, float)):
                                    return datetime.fromtimestamp(v, tz=timezone.utc)
                                return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
                            hold_total_mins += (_parse_dt(closed) - _parse_dt(ts)).total_seconds() / 60
                            hold_count += 1
                        except Exception:
                            pass

                    # Streak
                    cur_sign = "W" if p > 0 else "L"
                    if cur_sign == _last_sign:
                        streak_cur += 1
                    else:
                        streak_cur = 1
                        _last_sign = cur_sign
                    if cur_sign == "W":
                        streak_max_w = max(streak_max_w, streak_cur)
                    else:
                        streak_max_l = max(streak_max_l, streak_cur)

                avg_hold_mins = (hold_total_mins / hold_count) if hold_count > 0 else 0
                avg_win  = (sum(get_pnl(t) for t in history if get_pnl(t) > 0) / max(wins, 1)) if wins > 0 else 0
                avg_loss = (sum(get_pnl(t) for t in history if get_pnl(t) <= 0) / max(losses, 1)) if losses > 0 else 0
                rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

                for ex in exit_stats:
                    exit_stats[ex]["wr"] = exit_stats[ex]["wins"] / exit_stats[ex]["trades"] * 100

                # Top 3 aset terbaik & terburuk
                sorted_assets = sorted(asset_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
                top_assets    = sorted_assets[:3]
                bot_assets    = sorted_assets[-3:][::-1]

                best_exit  = max(exit_stats, key=lambda x: exit_stats[x]["wr"]) if exit_stats else None
                worst_exit = min(exit_stats, key=lambda x: exit_stats[x]["wr"]) if exit_stats else None

                pnl_color = "🟢" if total_pnl >= 0 else "🔴"
                wr_color  = "🟢" if win_rate >= 50 else "🔴"
                rr_color  = "🟢" if rr_ratio >= 1.0 else "🔴"

                # ── Ringkasan ────────────────────────────────────────────
                text += (
                    f"<b>Ringkasan</b>\n"
                    f"Win Rate   {wr_color} <b>{win_rate:.1f}%</b>  ({wins}W / {losses}L)\n"
                    f"Total PnL  {pnl_color} <b>{pnl_str(total_pnl)}</b>\n"
                    f"Risk/Reward  {rr_color} <b>{rr_ratio:.2f}x</b>  "
                    f"(avg win {pnl_str(avg_win)} / avg loss {pnl_str(avg_loss)})\n"
                )
                if avg_hold_mins > 0:
                    text += f"Avg Hold   ⏱ <b>{fmt_hold(avg_hold_mins)}</b>\n"
                if streak_max_w > 1 or streak_max_l > 1:
                    text += f"Best Streak  ✅ {streak_max_w}W  /  💔 {streak_max_l}L\n"

                # ── Top Aset ─────────────────────────────────────────────
                if top_assets:
                    text += "\n<b>Aset Terbaik</b>\n"
                    for rank, (ast, s) in enumerate(top_assets, 1):
                        ast_wr = s["wins"] / s["trades"] * 100
                        text += f"  {rank}. <b>{ast}</b>  {pnl_str(s['pnl'])}  ({ast_wr:.0f}% WR, {s['trades']}x)\n"

                if bot_assets and bot_assets[0][0] not in [a for a, _ in top_assets]:
                    text += "\n<b>Aset Terlemah</b>\n"
                    for rank, (ast, s) in enumerate(bot_assets, 1):
                        ast_wr = s["wins"] / s["trades"] * 100
                        text += f"  {rank}. <b>{ast}</b>  {pnl_str(s['pnl'])}  ({ast_wr:.0f}% WR, {s['trades']}x)\n"

                # ── Exit Type Breakdown ──────────────────────────────────
                if exit_stats:
                    text += "\n<b>Exit Breakdown</b>\n"
                    for ex, s in sorted(exit_stats.items(), key=lambda x: -x[1]["trades"]):
                        ex_wr   = s["wr"]
                        ex_icon = "✅" if ex_wr >= 60 else ("⚠️" if ex_wr >= 40 else "🔴")
                        text += (
                            f"  {ex_icon} {fmt_exit_label(ex):12}  "
                            f"<b>{ex_wr:.0f}%</b> WR  ({s['trades']}x)\n"
                        )

                text += "\n<i>Data dari semua posisi yang ditutup. ✨</i>"

            await update.effective_message.reply_html(text)
        except Exception as e:
            log.error(f"Journal error: {e}", exc_info=True)
            await update.effective_message.reply_html(f"❌ Journal Error: {e}")

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

    @staticmethod
    def _trade_ts_utc(trade: dict):
        """Parse trade timestamp → timezone-aware UTC datetime, or None."""
        from datetime import timezone as _tz
        raw = trade.get("created_at", trade.get("timestamp"))
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=_tz.utc)
        if isinstance(raw, (int, float)):
            # unix seconds (or ms)
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=_tz.utc)
            except (OSError, OverflowError, ValueError):
                return None
        s = str(raw).strip()
        # ISO / "2026-06-15 14:15:11+00:00"
        try:
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
        except ValueError:
            pass
        # "YYYY-MM-DD ..."
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), tzinfo=_tz.utc)
            except ValueError:
                return None
        return None

    def _today_closed_trades(self, chat_id: str, limit: int = 500) -> list:
        """Closed trades for the current UTC calendar day."""
        from core.db import user_db
        from datetime import timezone as _tz
        history = []
        try:
            if hasattr(user_db, "get_trade_history"):
                history = user_db.get_trade_history(str(chat_id), limit=limit) or []
            elif hasattr(user_db, "load_trade_history"):
                history = user_db.load_trade_history(str(chat_id), limit=limit) or []
        except Exception as e:
            log.error(f"[Daily] get_trade_history failed: {e}")
            return []

        today = datetime.now(_tz.utc).date()
        out = []
        for t in history:
            if not isinstance(t, dict):
                continue
            # Accept close records (type missing → still count if has pnl)
            ttype = str(t.get("type") or "close").lower()
            if ttype not in ("close", "full_close", ""):
                continue
            dt = self._trade_ts_utc(t)
            if dt is None or dt.date() != today:
                continue
            out.append(t)
        return out

    async def send_daily_report(self, acc: AccountState, pos_count: int, target_chat_id: str = None):
        """Send daily insights text + visual daily PnL card."""
        from core.mode_manager import mode_manager as _global_mm
        import config as _cfg
        from datetime import timezone as _tz

        pnl_sign = "+" if acc.daily_pnl >= 0 else ""
        pnl_emoji = "🟢" if acc.daily_pnl >= 0 else "🔴"

        mm = self.mode_manager or _global_mm
        is_scalper = mm.is_scalper() if mm else False
        # FORCE_SCALPER_ONLY → always show SCALPER
        if getattr(_cfg, "FORCE_SCALPER_ONLY", False):
            is_scalper = True
        mode_text = "SCALPER ⚡" if is_scalper else "STANDARD 📊"
        mode_icon = "⚡" if is_scalper else "📊"

        exec_mode = "Full-Auto 🤖" if getattr(_cfg, "FULL_AUTO", False) else "Semi-Auto 🤝"

        if acc.daily_pnl >= 0:
            footer = "Kerja bagus hari ini! Mari kita jaga momentumnya~ 🌸"
        else:
            footer = "Besok kita balas dendam ke market ya! Tetap disiplin~ 🌸"

        if acc.kill_switch_active:
            status_text = "KILL SWITCH"
            status_icon = "🚨"
            pause_reason = "Max drawdown tercapai"
        elif acc.is_paused:
            status_text = "PAUSED"
            status_icon = "⏸️"
            pause_reason = "Daily loss limit tercapai"
        else:
            status_text = "AKTIF"
            status_icon = "✅"
            pause_reason = "-"

        # Today's closed trades (fixed: was load_trade_history missing + bad date filter)
        today_trades: list = []
        total_trades = 0
        wins = 0
        losses_n = 0
        win_rate = "Belum ada trade hari ini"
        best_pnl = 0.0
        worst_pnl = 0.0
        try:
            chat_id_for_stats = str(target_chat_id) if target_chat_id else None
            if chat_id_for_stats:
                today_trades = self._today_closed_trades(chat_id_for_stats, limit=500)
                total_trades = len(today_trades)
                pnls = []
                for t in today_trades:
                    try:
                        pnls.append(float(t.get("pnl") or 0))
                    except (TypeError, ValueError):
                        pnls.append(0.0)
                wins = sum(1 for p in pnls if p > 0)
                losses_n = sum(1 for p in pnls if p < 0)
                if total_trades > 0:
                    win_rate = f"{wins/total_trades*100:.0f}% ({wins}/{total_trades})"
                    best_pnl = max(pnls)
                    worst_pnl = min(pnls)
        except Exception as e:
            log.error(f"[Daily] stats failed: {e}")

        report_date = datetime.now(_tz.utc).strftime("%Y-%m-%d")
        # Single signed PnL line — no double "++"
        daily_pnl_line = (
            f"{format_idr(acc.daily_pnl)} ({format_pct(acc.daily_pnl_pct, show_sign=True)})"
        )
        # drawdown: AccountState usually stores fraction (0.21 = 21%)
        dd_raw = float(acc.current_drawdown_pct or 0)
        dd_frac = dd_raw if abs(dd_raw) <= 1.5 else dd_raw / 100.0

        text = DAILY_REPORT_TEMPLATE.format(
            date=report_date,
            total_equity=format_idr(acc.total_equity),
            wallet_balance=format_idr(acc.wallet_balance),
            available=format_idr(acc.available),
            daily_pnl_line=daily_pnl_line,
            pnl_emoji=pnl_emoji,
            pos_count=pos_count,
            drawdown=format_pct(dd_frac, show_sign=False),
            win_rate=win_rate,
            total_trades=total_trades,
            mode_icon=mode_icon,
            mode_text=mode_text,
            exec_mode=exec_mode,
            status_icon=status_icon,
            status_text=status_text,
            pause_reason=pause_reason,
            footer=footer,
        )

        # Generate + send daily PnL card (photo + caption)
        try:
            import io as _io
            from notify.daily_card import generate_daily_card

            start_bal = float(getattr(acc, "wallet_balance", 0) or 0) - float(acc.daily_pnl or 0)
            end_bal = float(getattr(acc, "total_equity", getattr(acc, "wallet_balance", 0)) or 0)
            dd_for_card = abs(dd_frac) * 100.0  # card expects percent number e.g. 20.9

            card_bytes = generate_daily_card(
                date_str=datetime.now(_tz.utc).strftime("%d %B %Y"),
                daily_pnl_usd=float(acc.daily_pnl or 0),
                daily_pnl_pct=float(acc.daily_pnl_pct or 0),
                start_balance=max(start_bal, 0.0),
                end_balance=end_bal,
                total_trades=total_trades,
                win_trades=wins,
                loss_trades=losses_n,
                best_trade_pnl=float(best_pnl or 0),
                worst_trade_pnl=float(worst_pnl or 0),
                max_drawdown_pct=dd_for_card,
                trading_mode="SCALPER" if is_scalper else "STANDARD",
            )

            send_ids = [target_chat_id] if target_chat_id else list(self._authorized_chat_ids)
            for cid in send_ids:
                try:
                    await self._app.bot.send_photo(
                        chat_id=cid,
                        photo=_io.BytesIO(card_bytes),
                        caption=text.strip()[:1024],
                        parse_mode="HTML",
                    )
                    log.info(
                        f"[DailyCard] sent to {cid} trades={total_trades} "
                        f"pnl={acc.daily_pnl:+.2f} wr={win_rate}"
                    )
                except Exception as e:
                    log.error(f"[DailyCard] send_photo failed for {cid}: {e}")
                    await self.send_text(text, target_chat_id=cid)
        except Exception as e:
            log.error(f"[DailyCard] Card generation failed: {e}", exc_info=True)
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
        chat_id = str(update.effective_chat.id)
        if self._is_throttled(chat_id, threshold=2, action_key="export"): return
        
        await update.effective_message.reply_chat_action("upload_document")
        
        try:
            from core.db import user_db
            history = user_db.get_trade_history(chat_id, limit=5000)
            
            # [NEW] Sertakan posisi aktif juga di export
            session = await self.bot_app.get_session(chat_id) if self.bot_app else None
            open_positions = session.executor.open_positions if session else []

            if not history and not open_positions:
                await update.effective_message.reply_html(
                    "❌ <b>Gagal:</b> Belum ada data trade untuk di-export.\n\n"
                    "<i>Tunggu sampai ada posisi yang dibuka atau ditutup ya!</i>"
                )
                return

            # Combine history and open positions for the export
            export_data = []
            
            # Add open positions first
            for p in open_positions:
                export_data.append({
                    "timestamp": p.opened_at,
                    "asset": p.asset,
                    "side": p.side.value.upper(),
                    "type": "OPEN",
                    "entry_price": p.entry_price,
                    "exit_price": None,
                    "size": p.size_current,
                    "notional": p.size_current * p.entry_price,
                    "pnl": 0,
                    "pnl_pct": 0,
                    "score": getattr(p, 'entry_score', 0),
                    "reason": "ACTIVE",
                    "pos_id": p.position_id
                })
            
            # Add closed history
            export_data.extend(history)
            
            # Normalize list of dicts to flat DataFrame for Excel
            import pandas as pd
            import tempfile
            df = pd.DataFrame(export_data)
            
            column_map = {
                "timestamp": "Time (UTC)",
                "asset": "Asset",
                "side": "Side",
                "type": "Action",
                "entry_price": "Entry Price",
                "exit_price": "Exit Price",
                "size": "Size",
                "notional": "Notional ($)",
                "pnl": "PnL ($)",
                "pnl_pct": "PnL (%)",
                "score": "Signal Score",
                "reason": "Exit Reason",
                "pos_id": "Position ID"
            }
            # Only keep columns that exist in the data
            cols = [c for c in column_map.keys() if c in df.columns]
            df = df[cols].rename(columns=column_map)

            # Strip timezone info from datetime columns so Excel can write them
            for col in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[col]):
                    df[col] = df[col].dt.tz_localize(None)
                elif df[col].dtype == object:
                    try:
                        converted = pd.to_datetime(df[col], utc=True, errors='coerce')
                        if converted.notna().any():
                            df[col] = converted.dt.tz_localize(None)
                    except Exception:
                        pass

            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                temp_name = tmp.name
            
            try:
                df.to_excel(temp_name, index=False, engine='openpyxl')
                
                with open(temp_name, 'rb') as f:
                    await update.effective_message.reply_document(
                        document=f,
                        filename=f"KARA_History_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        caption=(
                            "📊 <b>KARA Trade History (Private)</b>\n\n"
                            f"Berikut riwayat trading eksklusif kamu ({len(export_data)} trades).\n"
                            "<i>Data ini sudah difilter dan tidak bercampur dengan user lain.</i>"
                        ),
                        parse_mode="HTML"
                    )
            finally:
                if os.path.exists(temp_name):
                    os.remove(temp_name)
                    
        except Exception as e:
            log.error(f"Export failed for {chat_id}: {e}")
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

    async def cmd_dbinfo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Diagnostic command to check database and storage status."""
        if not self._is_authorized(update): return
        
        import config as _cfg
        import os
        from core.db import user_db
        
        db_path = _cfg.DB_PATH
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        
        storage_dir = _cfg.STORAGE_DIR
        is_writable = os.access(storage_dir, os.W_OK) if os.path.exists(storage_dir) else False
        
        # Check for persistent volume sentinel
        sentinel = os.path.join(storage_dir, ".kara_persistence_sentinel")
        is_persistent = os.path.exists(sentinel)
        
        text = (
            "🗄️ <b>Database Diagnostic</b>\n\n"
            f"• <b>Path:</b> <code>{db_path}</code>\n"
            f"• <b>Size:</b> {db_size / 1024:.1f} KB\n"
            f"• <b>Storage:</b> <code>{storage_dir}</code>\n"
            f"• <b>Writable:</b> {'✅ Yes' if is_writable else '❌ No'}\n"
            f"• <b>Persistent:</b> {'✅ Yes (Volume detected)' if is_persistent else '⚠️ No (Ephemeral)'}\n\n"
            "<i>Note: Jika 'Persistent' bertanda ⚠️, data akan hilang saat bot restart/redeploy.</i>"
        )
        
        await update.effective_message.reply_html(text)

    async def _verified_mode_switch_positions(self, user, session) -> list:
        """Return open positions, reconciling exchange state for Live users."""
        if user.config.bot_mode != BotMode.LIVE:
            return list(getattr(getattr(session, "executor", None), "open_positions", []))
        if not session:
            raise RuntimeError("Session Live tidak tersedia untuk verifikasi posisi exchange")
        reconcile = getattr(getattr(session, "executor", None), "reconcile_if_due", None)
        if not reconcile:
            raise RuntimeError("Executor Live tidak mendukung verifikasi posisi exchange")
        await reconcile(force=True)
        return list(getattr(session.executor, "open_positions", []))

    async def cmd_paper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Switch to paper only after all live positions are confirmed closed."""
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        if not user: return

        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        try:
            live_positions = await self._verified_mode_switch_positions(user, session)
        except Exception as e:
            log.error(f"[MODE] Live→Paper reconciliation failed for {chat_id}: {e}")
            await update.effective_message.reply_html(
                "<b>Tidak bisa pindah ke Paper.</b> Posisi exchange belum dapat "
                "diverifikasi. Live monitoring tetap aktif."
            )
            return
        if user.config.bot_mode == BotMode.LIVE and live_positions:
            assets = ", ".join(position.asset for position in live_positions)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Tutup semua lalu Paper",
                    callback_data="paper_close_all_confirm",
                )],
                [InlineKeyboardButton(
                    "Tetap Live",
                    callback_data="paper_cancel",
                )],
            ])
            await update.effective_message.reply_html(
                "<b>Tidak bisa pindah ke Paper saat posisi Bybit masih terbuka.</b>\n\n"
                f"Posisi: <code>{assets}</code>\n\n"
                "Tutup semua posisi dan verifikasi exchange kosong, atau batalkan.",
                reply_markup=keyboard,
            )
            return

        await self._activate_paper_mode(chat_id, user)
        await update.effective_message.reply_html(
            "<b>Paper Mode aktif.</b> Saldo simulasi direset."
        )

    async def _activate_paper_mode(self, chat_id: str, user) -> None:

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
            old = self.bot_app.sessions.pop(chat_id)
            if getattr(old, "bybit_ws", None):
                await old.bybit_ws.stop()
            if getattr(old, "bybit_client", None):
                await old.bybit_client.close()

    async def cmd_live(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Start encrypted Bybit testnet credential setup."""
        if not self._is_authorized(update): return ConversationHandler.END
        if self._is_throttled(str(update.effective_chat.id), threshold=5, action_key="live"): return ConversationHandler.END
        chat_id = str(update.effective_chat.id)
        user = user_db.get_user(chat_id)
        session = await self.bot_app.get_session(chat_id) if self.bot_app else None
        try:
            positions = await self._verified_mode_switch_positions(user, session) if user else []
        except Exception as e:
            log.error(f"[MODE] Live credential rotation verification failed for {chat_id}: {e}")
            await update.effective_message.reply_html(
                "<b>Tidak bisa setup Live.</b> Posisi exchange belum dapat diverifikasi."
            )
            return ConversationHandler.END
        if positions:
            assets = ", ".join(sorted(position.asset for position in positions))
            mode = "Live" if user and user.config.bot_mode == BotMode.LIVE else "Paper"
            await update.effective_message.reply_html(
                f"<b>Tidak bisa setup Live saat posisi {mode} masih terbuka.</b>\n\n"
                f"Posisi: <code>{assets}</code>\n\n"
                "Tutup semua posisi terlebih dahulu."
            )
            return ConversationHandler.END
        if not config.FERNET_KEY:
            await update.effective_message.reply_html(
                "Live diblok: server belum memiliki <code>FERNET_KEY</code>."
            )
            return ConversationHandler.END
        ctx.user_data.pop("pending_bybit_key", None)
        ctx.user_data.pop("pending_bybit_secret", None)
        await update.effective_message.reply_html(
            "<b>Setup Live Bybit TESTNET</b>\n\n"
            "Hyperliquid hanya untuk scanning dan sinyal. Eksekusi hanya di Bybit.\n"
            "API key wajib read + contract trade, tanpa withdrawal permission.\n\n"
            "Kirim <b>API Key Bybit testnet</b>. Pesan akan langsung dihapus."
        )
        return WAITING_BYBIT_KEY

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

        if getattr(config, "FORCE_SCALPER_ONLY", False):
            session.user.config.trading_mode = "scalper"
            user_db.update_user(session.user)
            self._sync_mode_manager()
            await update.effective_message.reply_html(
                "⚡ <b>STANDARD DINONAKTIFKAN</b>\n\n"
                "Bot dikunci ke <b>SCALPER ONLY</b>.\n"
                "• Hold time / risk / SL = aturan scalper\n"
                "• Sinyal scorer standard (jika ada) dijalankan sebagai scalper"
            )
            return

        session.user.config.trading_mode = "standard"
        user_db.update_user(session.user)
        self._sync_mode_manager()
        await update.effective_message.reply_html("📊 <b>Ganti ke Standard Mode Berhasil!</b>")

    async def cmd_reset_ml(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Hapus semua data ML + model pkl dari Railway volume. Mulai fresh."""
        if not self._is_authorized(update): return
        chat_id = str(update.effective_chat.id)
        await update.effective_message.reply_html("⏳ <b>Mereset ML Intelligence...</b>")
        try:
            import sqlite3 as _sq, os as _os
            from intelligence.intelligence_model import intelligence_model as _im, MODEL_PATH as _MP
            from intelligence.experience_buffer import experience_buffer as _eb

            # 1. Hapus semua data dari DB (pakai path yang sama dengan ExperienceBuffer)
            db_path = _eb.db_path
            if _os.path.exists(db_path):
                conn = _sq.connect(db_path)
                conn.execute("DELETE FROM ml_experience")
                conn.commit()
                conn.close()

            # 2. Hapus pkl dari volume
            if _os.path.exists(_MP):
                _os.remove(_MP)

            # 3. Reset state model in-memory
            _im.model = None
            _im.is_ready = False
            _im.last_train_samples = 0

            await update.effective_message.reply_html(
                "✅ <b>ML Intelligence direset!</b>\n\n"
                "• Database training: <b>kosong</b>\n"
                "• Model pkl: <b>dihapus</b>\n"
                "• AI ABORT: <b>nonaktif</b> sampai 300 trades terkumpul\n\n"
                "<i>Bot akan mulai kumpulkan data bersih dari sekarang.</i>"
            )
        except Exception as e:
            await update.effective_message.reply_html(f"❌ Reset gagal: <code>{e}</code>")

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

    @staticmethod
    def _fmt_hold_duration(seconds: float) -> str:
        """Compact hold duration (e.g. 17m, 1h 5m)."""
        if seconds is None or seconds < 0:
            return "—"
        secs = int(round(float(seconds)))
        if secs < 60:
            return f"{secs}s"
        mins = max(1, int(round(secs / 60.0)))
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        rem_m = mins % 60
        return f"{hours}h" if rem_m == 0 else f"{hours}h {rem_m}m"

    @staticmethod
    def _signed_pct(frac_or_pct: float, already_pct: bool = False) -> str:
        v = float(frac_or_pct or 0.0)
        if not already_pct:
            v = v * 100.0
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"

    async def send_position_opened(self, pos, signal, target_chat_id: str = None):
        """Original open notification — do not change without explicit request."""
        # Use user_db for reliable mode labeling
        chat_id = target_chat_id or (
            list(self._authorized_chat_ids)[0] if self._authorized_chat_ids else ""
        )
        user = user_db.get_user(chat_id)
        is_scalper = (
            (getattr(pos, "trade_mode", None) == "scalper")
            or (user.config.trading_mode == "scalper" if user else False)
        )
        mode_text = "Scalping ⚡" if is_scalper else "Standar 🌸"
        side = pos.side.value.upper()
        asset = pos.asset
        score = getattr(signal, "score", getattr(pos, "entry_score", 0)) or 0
        rr = getattr(signal, "risk_reward_ratio", 0) or 0

        text = (
            f"🌸 <b>KARA SYSTEM: Position Executed</b>\n"
            f"<i>Saya baru saja menganalisis pasar dan berhasil membuka posisi "
            f"<b>{side}</b> untuk <b>"
            f"<a href='https://app.hyperliquid.xyz/trade/{asset}'>{asset}</a></b>.</i>\n\n"
            f"📦 <b>Market Details</b>\n"
            f"  • Entry   : <code>${format_price(pos.entry_price)}</code>\n"
            f"  • Margin  : <b>{format_idr(pos.margin_usd)}</b> ({format_usd(pos.margin_usd)})\n"
            f"  • Leverage: {pos.leverage}x isolated\n"
            f"  • Mode    : {mode_text}\n\n"
            f"🛡️ <b>Risk Profile</b>\n"
            f"  • 🛑 SL   : <code>${format_price(pos.stop_loss)}</code>\n"
            f"  • 🎯 TP1  : <code>${format_price(pos.tp1)}</code>\n"
            f"  • 🎯 TP2  : <code>${format_price(pos.tp2)}</code>\n"
            f"  • 📐 R:R Ratio: <b>{rr:.2f}x</b>\n"
            f"  • 📊 Score: <b>{score}/100</b>\n\n"
            f"<i>Eksekusi selesai. Memantau market untuk exit terbaik. ✨</i>"
        )
        await self.send_text(text, target_chat_id=target_chat_id)

    async def send_position_event(self, action: dict, prices: dict, target_chat_id: str = None):
        """
        KARA agent-style position lifecycle notifications.

        - TP1 / TP2: partial text only — no PnL card.
        - Full close (trail / SL / time_exit / manual): summary + Generate PnL button.
          Card image only when user taps the button.
        """
        action_type = action.get("action", "")
        pos_id = action.get("position_id", "")
        pnl_slice = float(action.get("pnl_slice", action.get("pnl", 0.0)) or 0.0)
        fully_closed = bool(action.get("fully_closed")) or (
            action_type in (
                "trailing_stop", "stop_loss", "profit_lock_stop", "time_exit",
                "close_all", "manual", "manual_close",
            )
        )

        session = await self.bot_app.get_session(target_chat_id) if (self.bot_app and target_chat_id) else None
        pos = None
        if session and hasattr(session.executor, "_positions"):
            pos = session.executor._positions.get(pos_id)

        if not pos:
            msg = action.get("message", "")
            if msg:
                await self.send_text(msg, target_chat_id=target_chat_id)
            return

        current = float(
            action.get("exit_price")
            or prices.get(pos.asset, pos.entry_price)
            or pos.entry_price
        )
        entry = pos.entry_price
        lev = max(int(getattr(pos, "leverage", 1) or 1), 1)
        from utils.helpers import pnl_roe_fraction, normalize_pct_display
        import config as _cfg

        total_pnl = float(action.get("pnl_total", getattr(pos, "pnl_realized", 0.0)) or 0.0)
        if fully_closed and abs(total_pnl) < 1e-12:
            total_pnl = pnl_slice

        full_notional = pos.size_initial * entry if entry and pos.size_initial else 0.0
        total_roe_frac = float(action.get("pnl_pct_total") or 0.0)
        if abs(total_roe_frac) < 1e-12:
            total_roe_frac = pnl_roe_fraction(total_pnl, full_notional, lev)
        total_roe_pct = normalize_pct_display(total_roe_frac)

        price_frac = float(action.get("price_move_pct") or pos.floating_pct(current) or 0.0)
        price_signed = self._signed_pct(price_frac, already_pct=False)
        slice_sign = "+" if pnl_slice >= 0 else ""
        total_sign = "+" if total_pnl >= 0 else ""

        is_scl = (getattr(pos, "trade_mode", "standard") or "standard") == "scalper"
        cfg_mode = _cfg.SCALPER if is_scl else _cfg.RISK
        tp1_r = float(getattr(cfg_mode, "tp1_close_ratio", getattr(_cfg.RISK, "tp1_close_ratio", 0.25)))
        tp2_r = float(getattr(cfg_mode, "tp2_close_ratio", getattr(_cfg.RISK, "tp2_close_ratio", 0.50)))
        remain_after_tp1 = max(0.0, 1.0 - tp1_r)
        remain_after_tp2 = remain_after_tp1 * (1.0 - tp2_r)
        mode_text = "Scalper" if is_scl else "Standard"
        side = pos.side.value.upper()
        asset = pos.asset

        slice_roe = float(action.get("pnl_pct_slice") or action.get("pnl_pct") or 0.0)
        if abs(slice_roe) < 1e-12 and abs(price_frac) > 0:
            slice_roe = price_frac * lev
        slice_roe_pct = normalize_pct_display(slice_roe)

        # Hold duration (available after close; for open partials use opened_at→now)
        duration_sec = 0.0
        try:
            if getattr(pos, "opened_at", None):
                end_ts = getattr(pos, "closed_at", None) or utcnow()
                opened = pos.opened_at
                if opened.tzinfo is None:
                    from datetime import timezone as _tz
                    opened = opened.replace(tzinfo=_tz.utc)
                if end_ts.tzinfo is None:
                    from datetime import timezone as _tz
                    end_ts = end_ts.replace(tzinfo=_tz.utc)
                duration_sec = max(0.0, (end_ts - opened).total_seconds())
        except Exception:
            duration_sec = 0.0
        hold_str = self._fmt_hold_duration(duration_sec)

        if action_type == "tp1":
            # Medium: light emoji + bold on numbers that matter
            text = (
                f"🎯 <b>TP1 Hit — {asset} {side}</b>\n"
                f"<i>Partial locked · sisa masih open</i>\n\n"
                f"📈 <code>${format_price(entry)}</code> → "
                f"<code>${format_price(current)}</code>  <b>({price_signed})</b>\n"
                f"💰 This cut: <b>{slice_sign}{format_idr(pnl_slice)}</b> "
                f"<b>({slice_sign}{slice_roe_pct:.2f}% @ {lev}x)</b>\n"
                f"📦 Size: closed <b>{tp1_r*100:.0f}%</b> · remaining "
                f"<b>{remain_after_tp1*100:.0f}%</b>\n"
                f"🛡️ SL → <b>breakeven</b> · {mode_text} {lev}x · {hold_str}"
            )
            await self.send_text(text, target_chat_id=target_chat_id)
            return

        if action_type == "tp2":
            text = (
                f"🎯🎯 <b>TP2 Hit — {asset} {side}</b>\n"
                f"<i>Partial #2 locked · sisa trailing</i>\n\n"
                f"📈 <code>${format_price(entry)}</code> → "
                f"<code>${format_price(current)}</code>  <b>({price_signed})</b>\n"
                f"💰 This cut: <b>{slice_sign}{format_idr(pnl_slice)}</b> "
                f"<b>({slice_sign}{slice_roe_pct:.2f}% @ {lev}x)</b>\n"
                f"📊 Realized: <b>{total_sign}{format_idr(total_pnl)}</b> "
                f"<i>(TP1+TP2, belum final)</i>\n"
                f"📍 Next: <b>trailing</b> on remainder · {mode_text} {lev}x · {hold_str}"
            )
            await self.send_text(text, target_chat_id=target_chat_id)
            return

        # ── FULL CLOSE (compact + light emoji) ────────────────────────
        head_meta = f"{mode_text} {lev}x · {hold_str}"
        pnl_block = (
            f"<b>{total_sign}{format_idr(total_pnl)}</b> "
            f"<b>({total_sign}{total_roe_pct:.2f}% @ {lev}x)</b>"
        )
        path_block = (
            f"📈 <code>${format_price(entry)}</code> → <code>${format_price(current)}</code> "
            f"<b>({price_signed})</b>"
        )

        if action_type == "trailing_stop":
            trail_px = float(action.get("trail_price", current) or current)
            mfe_bit = ""
            th = float(getattr(pos, "trailing_high", 0) or 0)
            if th > 0 and entry > 0:
                if side == "LONG":
                    mfe = (th - entry) / entry
                else:
                    mfe = (entry - th) / entry
                if mfe > 0:
                    mfe_bit = f" · Peak <b>{self._signed_pct(mfe)}</b>"
            text = (
                f"📍 <b>Trailing Stop — {asset} {side}</b> · {head_meta}\n"
                f"{path_block}{mfe_bit} · Trail <code>${format_price(trail_px)}</code>\n"
                f"💰 PnL: {pnl_block}"
            )
        elif action_type == "profit_lock_stop":
            trigger = float(action.get("trigger_price", pos.stop_loss) or pos.stop_loss)
            if total_pnl > 1e-9:
                lock_note = "lock berhasil · cumulative profit"
            elif total_pnl < -1e-9:
                lock_note = "gap/slippage/fee melewati lock · cumulative loss"
            else:
                lock_note = "lock exit · cumulative flat"
            text = (
                f"🛡️ <b>Profit Lock — {asset} {side}</b> · {head_meta}\n"
                f"📈 <code>${format_price(entry)}</code> → Fill "
                f"<code>${format_price(current)}</code> <b>({price_signed})</b>\n"
                f"🛡️ Trigger lock: <code>${format_price(trigger)}</code>\n"
                f"💰 PnL: {pnl_block} · <i>TP1 partial locked · {lock_note}</i>"
            )
        elif action_type == "stop_loss":
            text = (
                f"🛑 <b>Stop Loss — {asset} {side}</b> · {head_meta}\n"
                f"📈 <code>${format_price(entry)}</code> → SL "
                f"<code>${format_price(pos.stop_loss)}</code> <b>({price_signed})</b>\n"
                f"💰 PnL: {pnl_block}"
            )
        elif action_type == "time_exit":
            if total_pnl > 1e-9:
                te_emoji, te_tag, te_note = "⏱✅", "Time Exit · Profit", "max hold · green"
            elif total_pnl < -1e-9:
                te_emoji, te_tag, te_note = "⏱⚠️", "Time Exit · Loss", "max hold · cut risk"
            else:
                te_emoji, te_tag, te_note = "⏱", "Time Exit · Flat", "max hold · flat"
            text = (
                f"{te_emoji} <b>{te_tag} — {asset} {side}</b> · {head_meta}\n"
                f"{path_block}\n"
                f"💰 PnL: {pnl_block} · <i>{te_note}</i>"
            )
        elif action_type in ("close_all", "manual", "manual_close"):
            # Profit vs loss tag for manual / close-all
            if total_pnl > 1e-9:
                mc_tag = "Manual Close · Profit" if action_type != "close_all" else "Close All · Profit"
            elif total_pnl < -1e-9:
                mc_tag = "Manual Close · Loss" if action_type != "close_all" else "Close All · Loss"
            else:
                mc_tag = "Manual Close · Flat" if action_type != "close_all" else "Close All · Flat"
            if action_type == "close_all":
                mc_tag = mc_tag.replace("Manual Close", "Close All")
            text = (
                f"🔒 <b>{mc_tag} — {asset} {side}</b> · {head_meta}\n"
                f"{path_block}\n"
                f"💰 PnL: {pnl_block}"
            )
        else:
            msg = action.get("message", "")
            if msg:
                await self.send_text(msg, target_chat_id=target_chat_id)
            return

        # Cache for on-demand PnL card — never auto-generate
        gen_markup = None
        try:
            acc_state = await session.get_account_state() if session else None
            close_data = {
                "exit_price": current,
                "pnl": total_pnl,
                "pnl_pct": total_roe_frac,
                "reason": action_type,
                "score": getattr(pos, "entry_score", 0) or 0,
                "duration_sec": duration_sec,
                "hold_minutes": duration_sec / 60.0,
            }
            self._pending_pnl_cards[pos.position_id] = {
                "pos": pos,
                "close_data": close_data,
                "account": acc_state,
                "chat_id": str(target_chat_id) if target_chat_id else None,
            }
            gen_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📊 PnL Card",
                    callback_data=f"gen_pnl:{pos.position_id}",
                )
            ]])
        except Exception as e:
            log.error(f"[PnLCard] failed to cache pending card data: {e}")

        await self.send_text(text, target_chat_id=target_chat_id, reply_markup=gen_markup)

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

    async def send_pnl_card(
        self,
        position,
        close_data: dict,
        account,
        target_chat_id: str = None,
    ):
        """Generate and send a visual PnL card — only when user requests it."""
        if not self._app:
            return

        try:
            from notify.pnl_card import generate_pnl_card
            import io as _io

            hold_minutes = close_data.get("hold_minutes", 0)
            if not hold_minutes:
                duration_sec = close_data.get("duration_sec", 0)
                hold_minutes = duration_sec / 60 if duration_sec else 0

            card_bytes = generate_pnl_card(
                asset=position.asset,
                side=position.side.value,
                entry_price=position.entry_price,
                exit_price=close_data.get("exit_price", position.entry_price),
                pnl_usd=close_data.get("pnl", 0),
                pnl_pct=close_data.get("pnl_pct", 0),
                exit_reason=close_data.get("reason", "manual"),
                hold_minutes=hold_minutes,
                leverage=getattr(position, "leverage", 1),
                score=close_data.get("score", getattr(position, "entry_score", 0) or 0),
                session_pnl=getattr(account, "daily_pnl", 0) if account else 0,
                session_pnl_pct=getattr(account, "daily_pnl_pct", 0) if account else 0,
                total_equity=getattr(account, "total_equity", getattr(account, "balance", 0)) if account else 0,
            )

            pnl_usd = close_data.get("pnl", 0)
            pnl_pct_val = close_data.get("pnl_pct", 0)
            from utils.helpers import format_idr, normalize_pct_display
            pct_display = normalize_pct_display(pnl_pct_val)
            sign = "+" if pnl_usd >= 0 else ""
            reason = close_data.get("reason", "")

            from config import USD_TO_IDR

            pnl_idr = pnl_usd * USD_TO_IDR
            pnl_idr_str = (
                f"+Rp{pnl_idr:,.0f}".replace(",", ".")
                if pnl_idr >= 0
                else f"-Rp{abs(pnl_idr):,.0f}".replace(",", ".")
            )

            reason_lower = reason.lower()
            if reason_lower == "trailing_stop":
                trigger_label = "Trailing stop · full close"
                emoji = "📍"
            elif reason_lower == "stop_loss":
                trigger_label = "Stop loss · full close"
                emoji = "🛑"
            elif reason_lower == "time_exit":
                trigger_label = "Time exit · full close"
                emoji = "⏱"
            else:
                trigger_label = "Full close"
                emoji = "🔒"

            outcome = "Profit" if pnl_usd >= 0 else "Loss"
            caption = (
                f"{emoji} <b>KARA FULL CLOSE — {position.asset} {position.side.value.upper()}</b>\n"
                f"<i>{trigger_label}. Angka di bawah = total akumulasi semua partial.</i>\n\n"
                f"{'✅' if pnl_usd >= 0 else '❌'} <b>{outcome}: {sign}{pct_display:.2f}% ROE</b>\n"
                f"💵 <code>{sign}${abs(pnl_usd):.2f} USD</code>  •  "
                f"🇮🇩 <code>{pnl_idr_str}</code>"
            )

            if target_chat_id:
                chat_ids = [str(target_chat_id)]
            else:
                chat_ids = list(self._authorized_chat_ids)
            for cid in chat_ids:
                try:
                    await self._app.bot.send_photo(
                        chat_id=cid,
                        photo=_io.BytesIO(card_bytes),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    log.error(f"[PnLCard] Failed to send card to {cid}: {e}")
                    await self.send_text(caption, target_chat_id=cid)

        except Exception as e:
            log.error(f"[PnLCard] Card generation failed: {e}")
            pnl_usd = close_data.get("pnl", 0)
            sign = "+" if pnl_usd >= 0 else ""
            await self.send_text(
                f"📊 <b>{position.asset} {position.side.value.upper()}</b> closed\n"
                f"<code>{sign}{pnl_usd:+.2f} USD</code>",
                target_chat_id=target_chat_id,
            )

    async def send_trade_update(self, message: str):
        """Send a plain trade update (fallback)."""
        await self.send_text(message)

    async def _execution_mark_price(
        self, session, asset: str, fallback: float
    ) -> float:
        executor = getattr(session, "executor", None)
        if hasattr(executor, "mark_price"):
            price = await executor.mark_price(asset)
            if price > 0:
                return price
        if self.hl_client:
            price = await self.hl_client.get_mark_price(asset)
            if price > 0:
                return price
        return fallback

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

        # ── PnL Card on-demand (ONLY path that generates the image) ─
        if action in ("gen_pnl", "card_detail"):
            pending = self._pending_pnl_cards.get(sig_id)
            if not pending:
                try:
                    await self.send_text(
                        "⚠️ Kartu PnL tidak tersedia (expired atau data sudah dibersihkan).",
                        target_chat_id=chat_id,
                    )
                except Exception:
                    pass
                return
            try:
                # Disable button while generating
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("⏳ Generating…", callback_data="noop")
                        ]])
                    )
                except Exception:
                    pass
                await self.send_pnl_card(
                    pending["pos"],
                    pending["close_data"],
                    pending.get("account"),
                    target_chat_id=chat_id,
                )
                # Keep data for one re-generate; update button
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "📊 Generate PnL lagi",
                                callback_data=f"gen_pnl:{sig_id}",
                            )
                        ]])
                    )
                except Exception:
                    pass
            except Exception as e:
                log.error(f"[PnLCard] on-demand generation failed: {e}")
                try:
                    await self.send_text(
                        "❌ Gagal generate kartu PnL. Coba lagi sebentar.",
                        target_chat_id=chat_id,
                    )
                except Exception:
                    pass
            return

        if action == "noop":
            return

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
            elif target == "journal":
                await self.cmd_journal(update, ctx)
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
                if getattr(config, "FORCE_SCALPER_ONLY", False):
                    session.user.config.trading_mode = "scalper"
                    user_db.update_user(session.user)
                    self._sync_mode_manager()
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_html(
                        "⚡ <b>STANDARD DINONAKTIFKAN</b>\n\n"
                        "Bot dikunci ke <b>SCALPER ONLY</b> (hold time & risk scalper).\n"
                        "Sinyal standard scorer — jika ada — tetap dijalankan "
                        "dengan aturan scalper."
                    )
                else:
                    session.user.config.trading_mode = "standard"
                    user_db.update_user(session.user)
                    self._sync_mode_manager()
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_html("📊 <b>STANDARD MODE AKTIF!</b>\n\nMode swing yang lebih aman dan terukur.")
            else:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("❌ Pemindahan mode dibatalkan.")
            return

        if action == "scalper_confirm":
            session.user.config.trading_mode = "scalper"
            user_db.update_user(session.user)
            self._sync_mode_manager()
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
            await query.answer("Setup live Hyperliquid sudah dinonaktifkan.", show_alert=True)
            return

        elif query.data == "authorize_final":
            await query.answer("Live Hyperliquid sudah dinonaktifkan.", show_alert=True)
            return

        if query.data == "bybit_live_cancel":
            ctx.user_data.pop("pending_bybit_key", None)
            ctx.user_data.pop("pending_bybit_secret", None)
            await query.edit_message_text("Setup live Bybit dibatalkan.")
            return

        if query.data == "bybit_live_confirm":
            api_key = ctx.user_data.pop("pending_bybit_key", None)
            api_secret = ctx.user_data.pop("pending_bybit_secret", None)
            if not api_key or not api_secret:
                await query.edit_message_text("Credential sementara kedaluwarsa. Ketik /live lagi.")
                return
            user = user_db.get_user(chat_id)
            if not user:
                await query.edit_message_text("User tidak ditemukan.")
                return
            try:
                current_positions = await self._verified_mode_switch_positions(
                    user, session
                )
            except Exception as e:
                log.error(f"[MODE] Live activation verification failed for {chat_id}: {e}")
                await query.edit_message_text(
                    "Aktivasi Live dibatalkan: posisi exchange belum dapat diverifikasi."
                )
                return
            if current_positions:
                assets = ", ".join(sorted(position.asset for position in current_positions))
                await query.edit_message_text(
                    "Aktivasi Live dibatalkan karena posisi masih terbuka: " + assets
                )
                return
            previous = {
                "api_key": user.bybit_api_key,
                "api_secret": user.bybit_api_secret,
                "authorized": user.bybit_authorized,
                "testnet": user.bybit_testnet,
                "bot_mode": user.config.bot_mode,
            }
            user.bybit_api_key = api_key
            user.bybit_api_secret = api_secret
            user.bybit_testnet = True
            user.bybit_authorized = True
            user.config.bot_mode = BotMode.LIVE
            try:
                if self.bot_app:
                    await self.bot_app.ensure_bybit_public_client()
                user_db.update_user(user)
                if self.bot_app:
                    await self.bot_app.close_user_session(chat_id)
                    await self.bot_app.get_session(chat_id)
            except Exception as e:
                if self.bot_app:
                    try:
                        await self.bot_app.close_user_session(chat_id)
                    except Exception:
                        log.exception("Failed to clean partial live session for %s", chat_id)
                user.bybit_api_key = previous["api_key"]
                user.bybit_api_secret = previous["api_secret"]
                user.bybit_authorized = previous["authorized"]
                user.bybit_testnet = previous["testnet"]
                user.config.bot_mode = previous["bot_mode"]
                user_db.update_user(user)
                if self.bot_app and previous["bot_mode"] == BotMode.LIVE:
                    try:
                        await self.bot_app.get_session(chat_id)
                    except Exception:
                        log.exception("Failed to restore previous live session for %s", chat_id)
                await query.edit_message_text(
                    f"Aktivasi gagal dan dibatalkan: {html.escape(str(e))}"
                )
                return
            await query.edit_message_text(
                "Live Bybit <b>TESTNET</b> aktif. Credential tersimpan terenkripsi.",
                parse_mode=ParseMode.HTML,
            )
            return

        if query.data == "paper_cancel":
            await query.edit_message_text("Tetap Live. Posisi Bybit tetap dipantau.")
            return

        if query.data == "paper_close_all_confirm":
            user = user_db.get_user(chat_id)
            try:
                positions = await self._verified_mode_switch_positions(user, session)
            except Exception as e:
                log.error(f"[MODE] Pre-close reconciliation failed for {chat_id}: {e}")
                await query.edit_message_text(
                    "Gagal pindah Paper. Posisi exchange belum dapat diverifikasi."
                )
                return
            prices = {}
            for position in positions:
                prices[position.asset] = await self._execution_mark_price(
                    session, position.asset, position.entry_price
                )
            results = await session.executor.close_all_positions(prices)
            failure = next(
                (r for r in results if r.get("action") == "close_all_failed"), None
            )
            if failure or session.executor.open_positions:
                failed = (failure or {}).get("failed_assets", [])
                await query.edit_message_text(
                    "Gagal pindah Paper. Posisi Bybit belum kosong: "
                    + ", ".join(failed or [p.asset for p in session.executor.open_positions])
                )
                return
            await self._activate_paper_mode(chat_id, user)
            await query.edit_message_text("Semua posisi Bybit tertutup. Paper Mode aktif.")
            return

        if action == "close_req":
            asset, side_val = sig_id.split(":", 1)
            pos = next(
                (p for p in session.executor.open_positions
                 if p.asset == asset and p.side.value == side_val),
                None,
            )
            if not pos:
                await query.answer("Posisi sudah tidak ada.", show_alert=True)
                return

            try:
                current = await self._execution_mark_price(session, asset, pos.entry_price)
            except Exception:
                current = pos.entry_price

            from utils.helpers import pnl_roe_fraction
            lev = max(int(getattr(pos, "leverage", 1) or 1), 1)
            pnl_val = pos.unrealized_pnl(current)
            # ROE on remaining margin for this open size
            rem_notional = pos.size_current * pos.entry_price if pos.entry_price else 0.0
            roe_frac = pnl_roe_fraction(pnl_val, rem_notional, lev) if rem_notional else 0.0
            roe_pct = roe_frac * 100.0
            sign = "+" if pnl_val >= 0 else ""
            side_u = side_val.upper()
            hold_str = "?"
            try:
                if pos.opened_at:
                    opened = pos.opened_at
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    hold_str = self._fmt_hold_duration(
                        (datetime.now(timezone.utc) - opened).total_seconds()
                    )
            except Exception:
                pass

            text = (
                f"<b>Close {asset} {side_u}?</b>\n\n"
                f"Estimasi: <b>{sign}{format_idr(pnl_val)}</b> "
                f"<b>({sign}{roe_pct:.2f}% @ {lev}x)</b>\n"
                f"Entry <code>${format_price(pos.entry_price)}</code> → "
                f"Now <code>${format_price(current)}</code>\n"
                f"Hold: {hold_str} · {lev}x\n\n"
                f"<i>Tindakan ini final.</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirm close", callback_data=f"close_confirm:{asset}:{side_val}")],
                [InlineKeyboardButton("Cancel", callback_data="close_cancel")],
            ])
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return

        if action == "close_confirm":
            asset, side_val = sig_id.split(":", 1)
            pos = next(
                (p for p in session.executor.open_positions
                 if p.asset == asset and p.side.value == side_val),
                None,
            )
            if not pos:
                await query.answer("Posisi tidak ditemukan atau sudah tertutup.", show_alert=True)
                return

            await query.edit_message_text(
                f"Closing <b>{asset}</b>…", parse_mode=ParseMode.HTML
            )

            try:
                current = await self._execution_mark_price(session, asset, pos.entry_price)
            except Exception:
                current = pos.entry_price

            pos_id = pos.position_id
            res = await session.executor.close_position(pos_id, current, reason="manual")
            try:
                await query.message.delete()
            except Exception:
                pass

            if res:
                # Unified exit path → medium text + PnL Card button
                await self.send_position_event(
                    res,
                    {asset: float(res.get("exit_price") or current)},
                    target_chat_id=chat_id,
                )
            else:
                await self.send_text(
                    f"Gagal menutup posisi <b>{asset}</b>.",
                    target_chat_id=chat_id,
                )

            # Refresh open positions list
            try:
                await self.cmd_positions(update, ctx)
            except Exception:
                pass
            return

        if action == "close_cancel":
            await self.cmd_positions(update, ctx)
            return

        if action == "close_all_req":
            positions = list(session.executor.open_positions)
            count = len(positions)
            if count == 0:
                await query.answer("Tidak ada posisi aktif.", show_alert=True)
                return

            # Est. total unrealized (ROE not aggregated simply — show $ total)
            est_total = 0.0
            for p in positions:
                try:
                    px = await self._execution_mark_price(session, p.asset, p.entry_price)
                except Exception:
                    px = p.entry_price
                est_total += p.unrealized_pnl(px)
            sign = "+" if est_total >= 0 else ""

            text = (
                f"<b>Close all · {count} positions?</b>\n\n"
                f"Est. open PnL: <b>{sign}{format_idr(est_total)}</b>\n\n"
                f"<i>Tindakan ini final.</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirm all", callback_data="close_all_confirm")],
                [InlineKeyboardButton("Cancel", callback_data="close_cancel")],
            ])
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return

        if action == "close_all_confirm":
            positions = list(session.executor.open_positions)
            if not positions:
                await query.answer("Posisi sudah kosong.", show_alert=True)
                return

            n = len(positions)
            await query.edit_message_text(
                f"Closing {n} positions…", parse_mode=ParseMode.HTML
            )

            prices = {}
            for p in positions:
                try:
                    prices[p.asset] = await self._execution_mark_price(
                        session, p.asset, p.entry_price
                    )
                except Exception:
                    prices[p.asset] = p.entry_price

            results = await session.executor.close_all_positions(prices)
            try:
                await query.message.delete()
            except Exception:
                pass

            # One notif per position (each with PnL Card), same as auto exits
            successful = [
                res for res in results
                if res.get("action") != "close_all_failed"
            ]
            failure = next(
                (res for res in results if res.get("action") == "close_all_failed"),
                None,
            )
            for res in successful:
                asset = res.get("asset") or ""
                exit_px = float(res.get("exit_price") or prices.get(asset) or 0)
                await self.send_position_event(
                    res,
                    {asset: exit_px} if asset else prices,
                    target_chat_id=chat_id,
                )

            total_pnl = sum(float(r.get("pnl", 0) or 0) for r in successful)
            sign = "+" if total_pnl >= 0 else ""
            if failure:
                await self.send_text(
                    "<b>Close all belum selesai.</b> Gagal: <code>"
                    + ", ".join(failure.get("failed_assets", []))
                    + "</code>",
                    target_chat_id=chat_id,
                )
                return
            await self.send_text(
                f"<b>Close all done</b> · {len(successful)}/{n} · "
                f"Total <b>{sign}{format_idr(total_pnl)}</b>",
                target_chat_id=chat_id,
            )

            try:
                await self.cmd_positions(update, ctx)
            except Exception:
                pass
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
                # Threshold: signal=55, auto_trade=60
                u.config.std_min_score_to_signal = 55
                u.config.std_min_score_to_auto_trade = 60
                u.config.std_max_leverage = 10
                u.config.std_max_concurrent_positions = 10
                u.config.scl_min_score_to_signal = 50
                u.config.scl_min_score_to_auto_trade = 60  # was 57
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
