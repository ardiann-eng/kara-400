"""
KARA Bot - Dashboard v2 (FastAPI)
Serves the web UI + REST API endpoints for the new multi-user dashboard.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

import config
from models.schemas import AccountState, TradeSignal

log = logging.getLogger("kara.dashboard")

app = FastAPI(title="KARA Agent Dashboard", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Injected at startup ──────────────────────────────────────────────────────
_sessions:     Optional[Dict]   = None   # Dict[chat_id, UserSession]
_telegram:     Optional[Any]    = None
_mode_manager: Optional[Any]    = None
_admin_chat_id = str(config.TELEGRAM_CHAT_ID) if config.TELEGRAM_CHAT_ID else None
_ws_clients:   List[WebSocket]  = []


def get_active_session(chat_id: str = None):
    """Helper to find the best session to display (Admin or first available)."""
    if not _sessions:
        return None
    if chat_id and chat_id in _sessions:
        return _sessions[chat_id]
    if _admin_chat_id and _admin_chat_id in _sessions:
        return _sessions[_admin_chat_id]
    if _sessions:
        return _sessions[list(_sessions.keys())[0]]
    return None


# ── API: Core ────────────────────────────────────────────────────────────────

@app.get("/api/ping")
async def ping():
    return {"status": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mode": config.MODE,
        "trading_mode": _mode_manager.mode if _mode_manager else "standard",
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ── API: Overview ────────────────────────────────────────────────────────────

@app.get("/api/overview")
async def get_overview():
    """Aggregate stats for the Overview page."""
    from core.db import user_db

    all_users = user_db.get_all_users()
    total_users  = len(all_users)
    active_users = sum(1 for u in all_users if u.is_active)
    authorized_users = sum(1 for u in all_users if u.is_authorized)

    # Aggregate positions and PnL across all sessions
    total_positions = 0
    global_pnl_today = 0.0

    if _sessions:
        for chat_id, session in _sessions.items():
            try:
                acc = await session.get_account_state()
                total_positions += len(session.executor.open_positions)
                global_pnl_today += acc.daily_pnl
            except Exception:
                pass

    # System health
    ws_connected   = len(_ws_clients) > 0
    sessions_ready = _sessions is not None and len(_sessions) > 0

    return {
        "total_users":      total_users,
        "active_users":     active_users,
        "authorized_users": authorized_users,
        "total_positions":  total_positions,
        "global_pnl_today": round(global_pnl_today, 2),
        "system": {
            "websocket":    "online"  if ws_connected   else "waiting",
            "sessions":     "online"  if sessions_ready else "initializing",
            "telegram":     "online"  if (_telegram and getattr(_telegram, "_bot_started", False)) else "offline",
            "trading_mode": _mode_manager.mode if _mode_manager else "standard",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── API: History (Charts) ───────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(days: int = 7):
    """Return historical data for Overview charts."""
    from core.db import user_db
    
    history = user_db.load_history(days=days)
    
    # Format for charts (LightweightCharts expects {time: timestamp, value: number})
    pnl_data = []
    user_data = []
    
    for entry in history:
        # time must be in seconds for lightweight charts
        ts = int(entry["time"])
        pnl_data.append({"time": ts, "value": round(entry["global_pnl"], 2)})
        user_data.append({"time": ts, "value": entry["total_users"]})
        
    # If no history yet, return empty list or initial state
    if not pnl_data:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        pnl_data = [{"time": now_ts, "value": 0.0}]
        user_data = [{"time": now_ts, "value": len(user_db.users)}]

    return {
        "pnl_history": pnl_data,
        "user_growth": user_data,
        "count": len(history)
    }


# ── API: Users ───────────────────────────────────────────────────────────────

@app.get("/api/users")
async def get_all_users():
    """Return all registered users with summary stats."""
    from core.db import user_db

    result = []
    for u in user_db.get_all_users():
        session = _sessions.get(u.chat_id) if _sessions else None

        # Gather live stats
        open_positions = 0
        current_equity = u.paper_balance_usd
        daily_pnl      = 0.0
        last_active    = u.created_at.isoformat()

        if session:
            try:
                acc = await session.get_account_state()
                open_positions = len(session.executor.open_positions)
                current_equity = acc.total_equity
                daily_pnl      = acc.daily_pnl
            except Exception:
                pass

        # Count total closed trades from DB
        total_trades = 0
        try:
            from core.db import user_db as _db
            conn = _db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE chat_id = ?",
                (u.chat_id,)
            )
            row = cursor.fetchone()
            if row:
                total_trades = row[0]
        except Exception:
            pass

        result.append({
            "chat_id":       u.chat_id,
            "username":      u.username or f"user_{u.chat_id[-4:]}",
            "bot_mode":      u.config.bot_mode.value,
            "trading_mode":  u.config.trading_mode,
            "is_authorized": u.is_authorized,
            "is_active":     u.is_active,
            "open_positions": open_positions,
            "current_equity": round(current_equity, 2),
            "daily_pnl":     round(daily_pnl, 2),
            "total_trades":  total_trades,
            "created_at":    u.created_at.isoformat(),
        })

    return {"users": result}


@app.get("/api/users/{chat_id}")
async def get_user_detail(chat_id: str):
    """Return detailed info for a single user."""
    from core.db import user_db

    u = user_db.get_user(chat_id)
    if not u:
        raise HTTPException(404, "User not found")

    session = _sessions.get(chat_id) if _sessions else None
    acc_data = {}
    positions_data = []

    if session:
        try:
            acc = await session.get_account_state()
            acc_data = {
                "total_equity":   round(acc.total_equity, 2),
                "wallet_balance": round(acc.wallet_balance, 2),
                "available":      round(acc.available, 2),
                "used_margin":    round(acc.used_margin, 2),
                "unrealized_pnl": round(acc.unrealized_pnl, 2),
                "daily_pnl":      round(acc.daily_pnl, 2),
                "daily_pnl_pct":  round(acc.daily_pnl_pct, 4),
                "is_paused":      acc.is_paused,
            }
            positions_data = [
                {
                    "asset":        p.asset,
                    "side":         p.side.value,
                    "entry_price":  p.entry_price,
                    "size_current": p.size_current,
                    "leverage":     p.leverage,
                    "pnl_unrealized": round(p.pnl_unrealized, 2),
                    "pnl_realized":   round(p.pnl_realized, 2),
                    "opened_at":    p.opened_at.isoformat(),
                    "stop_loss":    p.stop_loss,
                    "tp1":          p.tp1,
                    "tp2":          p.tp2,
                }
                for p in session.executor.open_positions
            ]
        except Exception as e:
            log.warning(f"Could not fetch live session for {chat_id}: {e}")

    cfg = u.config
    return {
        "chat_id":            u.chat_id,
        "username":           u.username,
        "is_authorized":      u.is_authorized,
        "is_active":          u.is_active,
        "created_at":         u.created_at.isoformat(),
        "account":            acc_data,
        "open_positions":     positions_data,
        "config": {
            "trading_mode":              cfg.trading_mode,
            "bot_mode":                  cfg.bot_mode.value,
            "risk_pct":                  cfg.risk_pct,
            "std_min_score_to_auto_trade": cfg.std_min_score_to_auto_trade,
            "std_max_leverage":          cfg.std_max_leverage,
            "std_max_concurrent_positions": cfg.std_max_concurrent_positions,
            "scl_min_score_to_auto_trade": cfg.scl_min_score_to_auto_trade,
            "scl_max_leverage":          cfg.scl_max_leverage,
            "scl_max_concurrent_positions": cfg.scl_max_concurrent_positions,
        },
    }


@app.post("/api/users/{chat_id}/config")
async def update_user_config(chat_id: str, payload: dict = Body(...)):
    """Update a user's config settings."""
    from core.db import user_db

    u = user_db.get_user(chat_id)
    if not u:
        raise HTTPException(404, "User not found")

    cfg = u.config
    # Apply changes (only recognized fields)
    allowed = {
        "trading_mode", "risk_pct",
        "std_min_score_to_auto_trade",
        "std_max_leverage", "std_max_concurrent_positions",
        "scl_min_score_to_auto_trade",
        "scl_max_leverage", "scl_max_concurrent_positions",
    }
    for key, val in payload.items():
        if key in allowed and hasattr(cfg, key):
            try:
                setattr(cfg, key, type(getattr(cfg, key))(val))
            except Exception:
                pass

    u.config = cfg
    user_db.update_user(u)
    log.info(f"[DASHBOARD] Config updated for {chat_id}: {payload}")
    return {"status": "ok", "chat_id": chat_id}


@app.post("/api/users/{chat_id}/config/reset")
async def reset_user_config(chat_id: str):
    """Reset user config to defaults."""
    from core.db import user_db
    from models.schemas import UserConfig

    u = user_db.get_user(chat_id)
    if not u:
        raise HTTPException(404, "User not found")

    u.config = UserConfig(
        trading_mode=u.config.trading_mode,
        bot_mode=u.config.bot_mode,
        risk_pct=0.02,
    )
    user_db.update_user(u)
    return {"status": "reset", "chat_id": chat_id}


# ── API: Account (backwards compat) ─────────────────────────────────────────

@app.get("/api/account")
async def get_account(chat_id: str = None):
    from core.db import user_db
    session = get_active_session(chat_id)
    if session:
        try:
            acc = await session.get_account_state()
            return {
                "total_equity":   round(acc.total_equity, 2),
                "wallet_balance": round(acc.wallet_balance, 2),
                "available":      round(acc.available, 2),
                "used_margin":    round(acc.used_margin, 2),
                "unrealized_pnl": round(acc.unrealized_pnl, 2),
                "daily_pnl":      round(acc.daily_pnl, 2),
                "daily_pnl_pct":  round(acc.daily_pnl_pct, 4),
                "is_paused":      acc.is_paused,
                "mode": acc.mode.value if hasattr(acc.mode, "value") else str(acc.mode),
                "updated_at": acc.updated_at.isoformat(),
            }
        except Exception as e:
            log.error(f"[API] account state error: {e}")

    cid = chat_id or _admin_chat_id or "system"
    state = user_db.load_paper_state(cid)
    if state:
        return {"total_equity": state.get("equity", 0), "wallet_balance": state.get("balance", 0)}
    return {"total_equity": 0, "wallet_balance": 0, "error": "no data"}


# ── API: Positions ───────────────────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions(chat_id: str = None):
    from core.db import user_db
    session = get_active_session(chat_id)
    if session:
        try:
            data = [p.dict() for p in session.executor.open_positions]
            for p in data:
                for dt_key in ("opened_at", "closed_at"):
                    if dt_key in p and p[dt_key]:
                        p[dt_key] = p[dt_key].isoformat() if hasattr(p[dt_key], "isoformat") else str(p[dt_key])
            return {"positions": data}
        except Exception as e:
            log.error(f"[API] positions error: {e}")

    cid = chat_id or _admin_chat_id or "system"
    db_positions = user_db.load_paper_positions(cid)
    data = [p.dict() for p in db_positions]
    for p in data:
        if "opened_at" in p and p["opened_at"]:
            p["opened_at"] = p["opened_at"].isoformat() if hasattr(p["opened_at"], "isoformat") else str(p["opened_at"])
    return {"positions": data}


# ── API: Trades (Signals History) ────────────────────────────────────────────

@app.get("/api/trades")
async def get_recent_trades(limit: int = 20):
    """Return recent trade signals across all users."""
    from core.db import user_db

    try:
        signals = user_db.load_signals(limit=limit)
        result = []
        for s in signals:
            result.append({
                "signal_id":  s.signal_id,
                "asset":      s.asset,
                "side":       s.side.value,
                "score":      s.score,
                "entry_price": s.entry_price,
                "stop_loss":  s.stop_loss,
                "tp1":        s.tp1,
                "tp2":        s.tp2,
                "strength":   s.strength.value,
                "auto_executed": s.auto_executed,
                "timestamp":  s.timestamp.isoformat(),
            })
        return {"trades": result}
    except Exception as e:
        log.error(f"[API] trades error: {e}")
        return {"trades": []}


# ── API: Signals (backwards compat) ─────────────────────────────────────────

@app.get("/api/signals")
async def get_signals(limit: int = 20):
    from core.db import user_db
    try:
        signals = user_db.load_signals(limit=limit)
        return {"signals": [s.dict() for s in signals]}
    except Exception as e:
        log.error(f"Failed to fetch signals: {e}")
        return {"signals": []}


# ── API: Risk / Controls ─────────────────────────────────────────────────────

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


# ── API: Mode ────────────────────────────────────────────────────────────────

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
        label = "SCALPER" if mode == "scalper" else "STANDARD"
        await _telegram.send_text(f"<b>Dashboard: Mode switched to {label}</b>")
    return {"status": "success" if success else "no_change", "mode": _mode_manager.mode}


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    log.info(f"Dashboard client connected: {websocket.client.host}")
    _ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        log.info("Dashboard client disconnected")
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def broadcast(data: Dict):
    """Broadcast update to all dashboard websocket clients."""
    if not _ws_clients:
        return
    dead = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── HTML Dashboard ───────────────────────────────────────────────────────────

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    log.info("[DASHBOARD] Root route hit")
    possible_paths = [
        os.path.join(DASHBOARD_DIR, "templates", "dashboard.html"),
        "/app/dashboard/templates/dashboard.html",
        "dashboard/templates/dashboard.html",
    ]
    file_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not file_path:
        return HTMLResponse(
            f"<h1>Error: dashboard.html not found</h1><p>Tried: {possible_paths}</p>",
            status_code=404,
        )
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        return HTMLResponse(f"<h1>Error reading dashboard</h1><p>{e}</p>", status_code=500)


# ── Init / Run ───────────────────────────────────────────────────────────────

def init_dashboard(sessions, telegram_bot=None, mode_manager=None):
    """Inject dependencies into dashboard module."""
    global _sessions, _telegram, _mode_manager
    _sessions     = sessions
    _telegram     = telegram_bot
    _mode_manager = mode_manager
    log.info(f"[DASHBOARD] Sync: {len(sessions) if sessions else 0} user sessions linked.")


async def run_dashboard():
    """Start uvicorn in async context."""
    log.info("=" * 40)
    log.info(f"DASHBOARD LIVE ON: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    log.info("=" * 40)

    server_config = uvicorn.Config(
        app=app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(server_config)
    try:
        await server.serve()
    except Exception as e:
        print(f"\n[KARA_DEBUG] FATAL: Dashboard failed to bind! Error: {e}")
        import sys
        sys.exit(1)
