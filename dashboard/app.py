"""
KARA Bot - Dashboard (FastAPI + Tailwind)
Serves the web UI + REST API endpoints for dashboard data.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

import config
from models.schemas import AccountState, TradeSignal

log = logging.getLogger("kara.dashboard")

app = FastAPI(title="KARA Bot Dashboard", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Injected at startup
_sessions      = None   # Dict[chat_id, UserSession]
_telegram      = None
_mode_manager  = None
_admin_chat_id = str(config.TELEGRAM_CHAT_ID) if config.TELEGRAM_CHAT_ID else None
_ws_clients:   List[WebSocket] = []

def get_active_session(chat_id: str = None):
    """Helper to find the best session to display (Admin or first available)."""
    if not _sessions: 
        log.warning("⚠️ [DASHBOARD] get_active_session called but _sessions is empty!")
        return None
    
    # 1. If chat_id provided, use it
    if chat_id and chat_id in _sessions:
        return _sessions[chat_id]
        
    # 2. Try Admin session from config
    if _admin_chat_id and _admin_chat_id in _sessions:
        return _sessions[_admin_chat_id]
        
    # 3. Fallback to first available session
    if _sessions:
        first_id = list(_sessions.keys())[0]
        log.info(f"ℹ️ [DASHBOARD] Using fallback session: {first_id}")
        return _sessions[first_id]
        
    return None

# ──────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────

@app.get("/api/ping")
async def ping():
    return {"status": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/account")
async def get_account(chat_id: str = None):
    from core.db import user_db
    log.info(f"🐦 [CANARY] API /api/account triggered (chat_id: {chat_id})")
    session = get_active_session(chat_id)
    
    # 1. Try Live Session
    if session:
        try:
            acc = session.get_account_state()
            # Manual construction - 100% safe
            data = {
                "total_equity": round(acc.total_equity, 2),
                "wallet_balance": round(acc.wallet_balance, 2),
                "available": round(acc.available, 2),
                "used_margin": round(acc.used_margin, 2),
                "unrealized_pnl": round(acc.unrealized_pnl, 2),
                "daily_pnl": round(acc.daily_pnl, 2),
                "daily_pnl_pct": round(acc.daily_pnl_pct, 4),
                "is_paused": acc.is_paused,
                "mode": acc.mode.value if hasattr(acc.mode, 'value') else str(acc.mode),
                "updated_at": acc.updated_at.isoformat() if hasattr(acc.updated_at, 'isoformat') else str(acc.updated_at)
            }
            log.info(f"🏁 [API_FINISH] Sending Data for {session.user.chat_id}: ${data['total_equity']}")
            return data
        except Exception as e: 
            log.error(f"❌ [API] Failed at live state: {e}")
        
    # 2. Fallback to Database
    cid = chat_id or _admin_chat_id or "system"
    log.info(f"🔍 [API] Falling back to DB for ChatID: {cid}")
    state = user_db.load_paper_state(cid)
    user = user_db.get_user(cid)
    
    if state:
        return {
            "total_equity": state.get("equity", 0),
            "wallet_balance": state.get("balance", 0),
            "available": state.get("balance", 0),
            "unrealized_pnl": state.get("equity", 0) - state.get("balance", 0),
            "daily_pnl": 0,
            "daily_pnl_pct": 0,
            "mode": user.config.bot_mode.value if user else "standard",
            "is_paused": False,
            "positions": []
        }
        
    return {
        "total_equity": 0,
        "wallet_balance": 0,
        "error": "no data available"
    }

@app.get("/api/positions")
async def get_positions(chat_id: str = None):
    from core.db import user_db
    session = get_active_session(chat_id)
    
    # 1. Try Live Session
    if session:
        try:
            positions = session.executor.open_positions
            data = [p.dict() for p in positions]
            # Manual ISO format for datetimes to avoid JSON errors
            for p in data:
                if 'opened_at' in p and p['opened_at']:
                    p['opened_at'] = p['opened_at'].isoformat() if hasattr(p['opened_at'], 'isoformat') else str(p['opened_at'])
                if 'closed_at' in p and p['closed_at']:
                    p['closed_at'] = p['closed_at'].isoformat() if hasattr(p['closed_at'], 'isoformat') else str(p['closed_at'])
            
            log.info(f"✅ [API] Sending {len(data)} live positions for {session.user.chat_id}")
            return {"positions": data}
        except Exception as e:
            log.error(f"❌ [API] Failed to parse positions: {e}")
        
    # 2. Fallback to Database
    cid = chat_id or _admin_chat_id or "system"
    db_positions = user_db.load_paper_positions(cid)
    data = [p.dict() for p in db_positions]
    for p in data:
        if 'opened_at' in p and p['opened_at']:
            p['opened_at'] = p['opened_at'].isoformat() if hasattr(p['opened_at'], 'isoformat') else str(p['opened_at'])
    return {
        "positions": data
    }

@app.get("/api/signals")
async def get_signals(limit: int = 20):
    from core.db import user_db
    try:
        signals = user_db.load_signals(limit=limit)
        return {
            "signals": [s.dict() for s in signals]
        }
    except Exception as e:
        log.error(f"Failed to fetch signals: {e}")
        return {"signals": []}

@app.get("/api/risk_status")
async def get_risk_status(chat_id: str = None):
    session = get_active_session(chat_id)
    if not session:
        return {}
    return session.risk_mgr.status

@app.post("/api/pause")
async def pause_bot(chat_id: str = None):
    session = get_active_session(chat_id)
    if session:
        session.risk_mgr.pause()
    return {"status": "paused"}

@app.post("/api/resume")
async def resume_bot(chat_id: str = None):
    session = get_active_session(chat_id)
    if session:
        session.risk_mgr.resume()
    return {"status": "resumed"}

@app.get("/api/health")
async def health():
    log.info("❤️ [HEALTH] Heartbeat check from Railway/Uptime")
    return {
        "status": "ok",
        "mode":   config.MODE,
        "trading_mode": _mode_manager.mode if _mode_manager else "standard",
        "time":   datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/mode")
async def get_mode():
    if not _mode_manager:
        return {"mode": "standard"}
    return _mode_manager.status

@app.post("/api/mode")
async def set_mode(mode: str):
    if not _mode_manager:
        raise HTTPException(500, "mode_manager not ready")
    
    success = _mode_manager.switch(mode)
    if success and _telegram:
        icon = "⚡" if mode == "scalper" else "📊"
        await _telegram.send_text(f"🎛️ <b>Dashboard: Mode switched to {mode.upper()} {icon}</b>")
    
    return {"status": "success" if success else "no_change", "mode": _mode_manager.mode}

@app.get("/api/btc_real_time")
async def get_btc_real_time():
    """Fetch real-time BTC/USD data for dashboard chart."""
    try:
        from data.hyperliquid_client import get_client
        hl = get_client()
        if not hl._http_data:
            await hl.connect()
        btc_data = await hl.get_btc_real_time_data()
        return btc_data
    except Exception as e:
        log.error(f"Failed to get BTC real-time: {e}")
        return {
            "current_price": 0,
            "high_24h": 0,
            "low_24h": 0,
            "candles": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(e)
        }

@app.get("/api/candles/{asset}")
async def get_candles(asset: str, interval: str = "1m", limit: int = 60):
    """Fetch recent candles for the dashboard chart."""
    try:
        from data.hyperliquid_client import get_client
        hl = get_client()
        if not hl._http_data:
            await hl.connect()
            
        import time
        now_ms = int(time.time() * 1000)
        # Approximate start_ms based on limit and interval
        minute_multipliers = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        minutes_back = minute_multipliers.get(interval, 1) * limit
        start_ms = now_ms - (minutes_back * 60 * 1000)
        
        payload = {
            "req": {
                "coin": asset,
                "interval": interval,
                "startTime": start_ms,
                "endTime": now_ms
            }
        }
        resp, succ = await hl._call_info_endpoint("candleSnapshot", payload)
        if not succ or not isinstance(resp, list):
            return []
            
        formatted_candles = []
        for c in resp:
            if isinstance(c, list) and len(c) >= 6:
                formatted_candles.append({
                    "time": int(c[0]) / 1000,
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
        
        # Hyperliquid returns up to 5000 candles, just take the last 'limit'
        return formatted_candles[-limit:]
    except Exception as e:
        log.error(f"Failed to get candles for {asset}: {e}")
        return []

# ──────────────────────────────────────────────
# WEBSOCKET (live price updates to dashboard)
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    log.info(f"🟢 Dashboard client connected: {websocket.client.host}")
    _ws_clients.append(websocket)
    try:
        while True:
            # Keep-alive
            await websocket.receive_text()
    except Exception:
        log.info(f"🔴 Dashboard client disconnected")
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def broadcast(data: Dict):
    """Broadcast update to all dashboard websocket clients."""
    if not _ws_clients:
        return
    dead = []
    # Use a copy to avoid mutation errors
    for ws in list(_ws_clients):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)

# ──────────────────────────────────────────────
# HTML DASHBOARD
# ──────────────────────────────────────────────

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    log.info("🏠 [DASHBOARD] Root route hit (GET /)")
    
    # Try multiple possible paths for Docker/Railway environments
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html"),
        "/app/dashboard/templates/dashboard.html",
        "dashboard/templates/dashboard.html",
        "./dashboard/templates/dashboard.html"
    ]
    
    file_path = None
    for p in possible_paths:
        if os.path.exists(p):
            file_path = p
            break
            
    if not file_path:
        log.error(f"❌ [DASHBOARD] Could not find dashboard.html in any of: {possible_paths}")
        return HTMLResponse(f"<h1>Error: dashboard.html not found</h1><p>Tried: {possible_paths}</p>", status_code=404)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        log.error(f"❌ [DASHBOARD] Exception reading {file_path}: {e}")
        return HTMLResponse(f"<h1>Error reading dashboard</h1><p>{e}</p>", status_code=500)

@app.get("/old", response_class=HTMLResponse)
async def old_dashboard():
    file_path = os.path.join(DASHBOARD_DIR, "index.html")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found</h1>", status_code=404)


def init_dashboard(sessions, telegram_bot=None, mode_manager=None):
    """Inject dependencies into dashboard module."""
    global _sessions, _telegram, _mode_manager
    _sessions     = sessions
    _telegram     = telegram_bot
    _mode_manager = mode_manager
    log.info(f"✅ [DASHBOARD] Pulse Sync: {len(sessions) if sessions else 0} user sessions linked.")


async def run_dashboard():
    """Start uvicorn in async context."""
    log.info("=" * 40)
    log.info(f"🚀 DASHBOARD LIVE ON: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    log.info("=" * 40)
    
    server_config = uvicorn.Config(
        app=app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(server_config)
    try:
        await server.serve()
    except Exception as e:
        print(f"\n❌ [KARA_DEBUG] FATAL: Dashboard failed to bind! Error: {e}")
        import sys
        sys.exit(1)
