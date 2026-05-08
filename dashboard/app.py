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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
import uvicorn

import config
from models.schemas import AccountState, TradeSignal
from dashboard.auth import (
    verify_telegram_login, create_jwt, decode_jwt,
    get_current_user, get_admin_user,
)

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


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/auth/telegram")
async def auth_telegram(payload: dict = Body(...)):
    """Verify Telegram Login Widget data and return JWT."""
    from core.db import user_db

    if not verify_telegram_login(payload):
        raise HTTPException(status_code=401, detail="Telegram verification failed")

    chat_id  = str(payload.get("id", ""))
    username = payload.get("username") or payload.get("first_name") or f"user_{chat_id[-4:]}"

    user = user_db.get_user(chat_id)
    if not user:
        raise HTTPException(status_code=403, detail="User not registered. Start the bot on Telegram first.")
    if not user.is_authorized:
        raise HTTPException(status_code=403, detail="User not authorized. Complete onboarding via Telegram.")

    is_admin = (chat_id == str(config.TELEGRAM_CHAT_ID)) if config.TELEGRAM_CHAT_ID else False
    token    = create_jwt(chat_id, username)
    response = JSONResponse({"token": token, "username": username, "chat_id": chat_id, "is_admin": is_admin})
    response.set_cookie(
        "kara_token", token,
        httponly=True, samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return response


# ── Magic Link Auth ──────────────────────────────────────────────────────────

@app.get("/auth/magic")
async def auth_magic(token: str):
    """Verify magic token from Telegram bot and issue JWT."""
    import time
    from fastapi.responses import RedirectResponse
    from core.db import user_db

    if not token:
        raise HTTPException(400, "Missing token")

    # Get token store from telegram bot instance
    magic_tokens = getattr(_telegram, "_magic_tokens", {}) if _telegram else {}
    entry = magic_tokens.get(token)

    if not entry:
        return HTMLResponse(
            "<script>document.write('<h2 style=\"font-family:monospace;color:#f0407a\">Link tidak valid atau sudah digunakan.</h2>')</script>",
            status_code=400,
        )
    if entry["exp"] < time.time():
        magic_tokens.pop(token, None)
        return HTMLResponse(
            "<script>document.write('<h2 style=\"font-family:monospace;color:#f0407a\">Link sudah kadaluarsa. Ketik /weblogin lagi di bot.</h2>')</script>",
            status_code=400,
        )

    # Consume token (one-time use)
    magic_tokens.pop(token, None)

    chat_id = entry["chat_id"]
    u = user_db.get_user(chat_id)
    if not u:
        raise HTTPException(403, "User not registered")

    username = u.username or f"user_{chat_id[-4:]}"
    is_admin = (chat_id == str(config.TELEGRAM_CHAT_ID)) if config.TELEGRAM_CHAT_ID else False
    jwt_token = create_jwt(chat_id, username)

    # Inject token into page and redirect
    redirect_to = "/" if is_admin else "/terminal"
    html_content = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <script>
      localStorage.setItem('kara_token', '{jwt_token}');
      localStorage.setItem('kara_username', '{username}');
      localStorage.setItem('kara_chat_id', '{chat_id}');
      localStorage.setItem('kara_is_admin', '{'1' if is_admin else '0'}');
      window.location.replace('{redirect_to}');
    </script>
    </head><body style="background:#050a12;color:#00e5b0;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;">
    Redirecting…</body></html>"""

    response = HTMLResponse(html_content)
    response.set_cookie("kara_token", jwt_token, httponly=True, samesite="lax", max_age=7*24*3600)
    return response


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    path = os.path.join(DASHBOARD_DIR, "templates", "login.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>login.html not found</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/terminal", response_class=HTMLResponse)
async def terminal_page():
    path = os.path.join(DASHBOARD_DIR, "templates", "terminal.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>terminal.html not found</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── API: /api/me — per-user (JWT required) ────────────────────────────────────

@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    from core.db import user_db
    u = user_db.get_user(user["sub"])
    if not u:
        raise HTTPException(404, "User not found")
    return {
        "chat_id":      u.chat_id,
        "username":     u.username or user.get("username"),
        "bot_mode":     u.config.bot_mode.value,
        "trading_mode": u.config.trading_mode,
        "is_active":    u.is_active,
    }


@app.get("/api/me/overview")
async def me_overview(user: dict = Depends(get_current_user)):
    from core.db import user_db
    chat_id = user["sub"]
    session = _sessions.get(chat_id) if _sessions else None

    acc_data = {}
    open_count = 0
    if session:
        try:
            acc = await session.get_account_state()
            open_count = len(session.executor.open_positions)
            acc_data = {
                "total_equity":      round(acc.total_equity, 2),
                "wallet_balance":    round(acc.wallet_balance, 2),
                "available":         round(acc.available, 2),
                "used_margin":       round(acc.used_margin, 2),
                "unrealized_pnl":    round(acc.unrealized_pnl, 2),
                "daily_pnl":         round(acc.daily_pnl, 2),
                "daily_pnl_pct":     round(acc.daily_pnl_pct * 100, 2),
                "current_drawdown":  round(getattr(acc, "current_drawdown_pct", 0) * 100, 2),
                "peak_balance":      round(getattr(acc, "peak_balance", acc.total_equity), 2),
                "is_paused":         acc.is_paused,
                "kill_switch":       getattr(acc, "kill_switch_active", False),
                "mode":              acc.mode.value if hasattr(acc.mode, "value") else str(acc.mode),
            }
        except Exception as e:
            log.warning(f"[me/overview] {chat_id}: {e}")

    # Risk stats from risk manager
    risk_data = {}
    if session and hasattr(session, "risk_mgr"):
        try:
            rs = session.risk_mgr.status
            risk_data = {
                "daily_loss_used_pct": round(rs.get("daily_loss_pct", 0) * 100, 2),
                "daily_loss_limit_pct": round(rs.get("daily_loss_limit_pct", 3) * 100, 2),
                "max_drawdown_pct": round(rs.get("max_drawdown_pct", 6) * 100, 2),
            }
        except Exception:
            pass

    # Win rate from closed trades
    win_rate = 0.0
    total_trades = 0
    try:
        from core.db import user_db as _db
        conn = _db._get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins "
            "FROM paper_positions WHERE chat_id = ? AND status = 'CLOSED'",
            (chat_id,)
        )
        row = cur.fetchone()
        if row and row[0]:
            total_trades = row[0]
            wins         = row[1] or 0
            win_rate     = round(wins / total_trades * 100, 1) if total_trades else 0.0
    except Exception:
        pass

    return {
        "account":       acc_data,
        "risk":          risk_data,
        "open_positions": open_count,
        "win_rate":      win_rate,
        "total_trades":  total_trades,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/me/positions")
async def me_positions(user: dict = Depends(get_current_user)):
    chat_id = user["sub"]
    session = _sessions.get(chat_id) if _sessions else None
    if not session:
        return {"positions": []}
    try:
        data = []
        for p in session.executor.open_positions:
            pd = p.dict()
            for dt_key in ("opened_at", "closed_at"):
                if pd.get(dt_key):
                    pd[dt_key] = pd[dt_key].isoformat() if hasattr(pd[dt_key], "isoformat") else str(pd[dt_key])
            data.append(pd)
        return {"positions": data}
    except Exception as e:
        log.error(f"[me/positions] {e}")
        return {"positions": []}


@app.get("/api/me/trades")
async def me_trades(
    user:   dict = Depends(get_current_user),
    limit:  int  = 50,
    offset: int  = 0,
    asset:  str  = None,
    side:   str  = None,
    result: str  = None,   # "win" | "loss"
):
    chat_id = user["sub"]
    try:
        from core.db import user_db as _db
        conn = _db._get_conn()
        conn.row_factory = __import__("sqlite3").Row
        cur  = conn.cursor()

        where  = ["chat_id = ?", "status = 'CLOSED'"]
        params = [chat_id]
        if asset:
            where.append("asset = ?");  params.append(asset.upper())
        if side:
            where.append("side = ?");   params.append(side.upper())
        if result == "win":
            where.append("pnl_usd > 0")
        elif result == "loss":
            where.append("pnl_usd <= 0")

        where_sql = " AND ".join(where)
        cur.execute(
            f"SELECT * FROM paper_positions WHERE {where_sql} "
            f"ORDER BY closed_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        )
        rows = cur.fetchall()

        # Total count for pagination
        cur.execute(f"SELECT COUNT(*) FROM paper_positions WHERE {where_sql}", params)
        total = cur.fetchone()[0]

        trades = []
        for r in rows:
            trades.append({
                "position_id":   r["position_id"],
                "asset":         r["asset"],
                "side":          r["side"],
                "entry_price":   r["entry_price"],
                "exit_price":    r["exit_price"] if "exit_price" in r.keys() else None,
                "size_initial":  r["size_initial"],
                "leverage":      r["leverage"],
                "pnl_usd":       round(r["pnl_usd"] or 0, 2),
                "pnl_pct":       round((r["pnl_pct"] or 0) * 100, 2),
                "exit_reason":   r["exit_reason"] if "exit_reason" in r.keys() else None,
                "opened_at":     r["opened_at"],
                "closed_at":     r["closed_at"],
                "entry_score":   r["entry_score"] if "entry_score" in r.keys() else None,
            })
        return {"trades": trades, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        log.error(f"[me/trades] {e}")
        return {"trades": [], "total": 0}


@app.get("/api/me/equity_history")
async def me_equity_history(user: dict = Depends(get_current_user), days: int = 7):
    """Equity curve data for chart — one point per closed trade."""
    chat_id = user["sub"]
    try:
        from core.db import user_db as _db
        conn = _db._get_conn()
        conn.row_factory = __import__("sqlite3").Row
        cur  = conn.cursor()
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        cur.execute(
            "SELECT closed_at, pnl_usd FROM paper_positions "
            "WHERE chat_id = ? AND status = 'CLOSED' AND closed_at >= ? "
            "ORDER BY closed_at ASC",
            (chat_id, cutoff)
        )
        rows = cur.fetchone and cur.fetchall() or []

        # Get starting balance
        u_row = __import__("core.db", fromlist=["user_db"]).user_db.get_user(chat_id)
        balance = float(getattr(getattr(u_row, "config", None), "paper_balance_usd", 1000) if u_row else 1000)

        points = []
        running = balance
        for r in rows:
            running += float(r["pnl_usd"] or 0)
            points.append({"time": int(r["closed_at"]), "value": round(running, 2)})

        if not points:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            points = [{"time": now_ts, "value": round(balance, 2)}]

        return {"equity_history": points}
    except Exception as e:
        log.error(f"[me/equity_history] {e}")
        return {"equity_history": []}


@app.get("/api/me/signals")
async def me_signals(user: dict = Depends(get_current_user), limit: int = 20):
    """Active/recent signals (global — signals are not per-user in current arch)."""
    from core.db import user_db
    try:
        signals = user_db.load_signals(limit=limit)
        result  = []
        for s in signals:
            d = s.dict()
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif hasattr(v, "value"):
                    d[k] = v.value
            result.append(d)
        return {"signals": result}
    except Exception as e:
        log.error(f"[me/signals] {e}")
        return {"signals": []}


@app.get("/api/me/candles/{asset}")
async def me_candles(asset: str, user: dict = Depends(get_current_user), interval: str = "5m", limit: int = 100):
    """OHLCV candles for a given asset — forwarded from HL client."""
    chat_id = user["sub"]
    session = _sessions.get(chat_id) if _sessions else None
    hl_client = getattr(session, "hl_client", None) if session else None

    if not hl_client:
        # Try shared global client
        from data.hyperliquid_client import HyperliquidClient
        try:
            hl_client = HyperliquidClient()
        except Exception:
            return {"candles": []}

    try:
        candles = await hl_client.get_candles(asset.upper(), interval=interval, limit=limit)
        # Format for TradingView: {time, open, high, low, close, volume}
        result = []
        for c in (candles or []):
            result.append({
                "time":   int(c[0] / 1000) if c[0] > 1e10 else int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]) if len(c) > 5 else 0,
            })
        return {"candles": result, "asset": asset.upper(), "interval": interval}
    except Exception as e:
        log.error(f"[me/candles] {asset}: {e}")
        return {"candles": []}


@app.get("/api/me/risk")
async def me_risk(user: dict = Depends(get_current_user)):
    chat_id = user["sub"]
    session = _sessions.get(chat_id) if _sessions else None
    if not session or not hasattr(session, "risk_mgr"):
        return {}
    return session.risk_mgr.status


# ── Per-user WebSocket ────────────────────────────────────────────────────────

_user_ws_clients: Dict[str, List[WebSocket]] = {}   # chat_id -> [ws, ...]


@app.websocket("/ws/me")
async def ws_user(websocket: WebSocket, token: str = None):
    """Per-user WebSocket. Token passed as query param: /ws/me?token=<jwt>"""
    chat_id = None
    if token:
        try:
            payload = decode_jwt(token)
            chat_id = payload["sub"]
        except Exception:
            await websocket.close(code=4001)
            return

    await websocket.accept()
    if chat_id:
        _user_ws_clients.setdefault(chat_id, []).append(websocket)

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        if chat_id and websocket in _user_ws_clients.get(chat_id, []):
            _user_ws_clients[chat_id].remove(websocket)


async def broadcast_to_user(chat_id: str, data: dict):
    """Push realtime update to a specific user's WS connections."""
    dead = []
    for ws in list(_user_ws_clients.get(chat_id, [])):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _user_ws_clients.get(chat_id, []).remove(ws)


# ── API: Core ────────────────────────────────────────────────────────────────

@app.get("/api/ping")
async def ping():
    return {"status": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/health")
async def health():
    # Derive bot username from token (format: 123456:ABCDEF...)
    bot_username = None

    # 1. Hardcoded env var (most reliable — set TELEGRAM_BOT_USERNAME in Railway)
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip() or None

    # 2. From running bot instance
    if not bot_username and _telegram and getattr(_telegram, "_app", None):
        try:
            bot_username = (await _telegram._app.bot.get_me()).username
        except Exception:
            pass

    # 3. Derive from token via Telegram API (skip if token is placeholder)
    if not bot_username and config.TELEGRAM_TOKEN and not config.TELEGRAM_TOKEN.startswith("CHANGE"):
        try:
            import httpx
            r = httpx.get(
                f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getMe",
                timeout=4,
            )
            if r.status_code == 200:
                bot_username = r.json().get("result", {}).get("username")
        except Exception:
            pass
    # Derive bot_id from token (format: "123456789:ABCdef...")
    bot_id = None
    if config.TELEGRAM_TOKEN and not config.TELEGRAM_TOKEN.startswith("CHANGE"):
        try:
            bot_id = int(config.TELEGRAM_TOKEN.split(":")[0])
        except Exception:
            pass

    return {
        "status":       "ok",
        "mode":         config.MODE,
        "trading_mode": _mode_manager.mode if _mode_manager else "scalper",
        "bot_username": bot_username,
        "bot_id":       bot_id,
        "time":         datetime.now(timezone.utc).isoformat(),
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
            "trading_mode": _mode_manager.mode if _mode_manager else "scalper",
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
async def get_all_users(_admin: dict = Depends(get_admin_user)):
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
async def get_user_detail(chat_id: str, _admin: dict = Depends(get_admin_user)):
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
async def update_user_config(chat_id: str, payload: dict = Body(...), _admin: dict = Depends(get_admin_user)):
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
        return {"mode": "scalper"}
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


# ── ML Intelligence — Meta Patterns ─────────────────────────────────────────

@app.get("/api/ml_meta_patterns")
async def get_ml_meta_patterns():
    try:
        import sqlite3, time
        db_path = config.DB_PATH
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT pattern_key, winrate_ema, pnl_ema, samples, updated_at FROM meta_pattern_stats ORDER BY samples DESC LIMIT 60"
        ).fetchall()
        conn.close()

        patterns = []
        for r in rows:
            key = r["pattern_key"]          # e.g. "scalper_APE_long"
            parts = key.split("_")
            mode  = parts[0] if len(parts) >= 3 else "?"
            side  = parts[-1] if len(parts) >= 3 else "?"
            asset = "_".join(parts[1:-1])   # handles multi-underscore assets
            patterns.append({
                "key":       key,
                "mode":      mode,
                "asset":     asset.upper(),
                "side":      side.upper(),
                "winrate":   round(r["winrate_ema"] * 100, 1),
                "pnl_ema":   round(r["pnl_ema"], 2),
                "samples":   r["samples"],
                "updated_at": int(r["updated_at"] or 0),
            })
        return {"patterns": patterns}
    except Exception as e:
        log.warning(f"[ML Meta] {e}")
        return {"patterns": []}


@app.get("/api/ml_decision_feed")
async def get_ml_decision_feed():
    """Last 30 meta scoring decisions from trade history."""
    try:
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT asset, side, pnl_usd, pnl_pct, data, created_at FROM trade_history "
            "WHERE json_extract(data, '$.type') = 'close' "
            "GROUP BY json_extract(data, '$.pos_id') "
            "ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()

        import json as _json
        feed = []
        for r in rows:
            try:
                d = _json.loads(r["data"] or "{}")
            except Exception:
                d = {}
            meta_key   = d.get("meta_pattern_key", "")
            meta_boost = d.get("meta_boost", None)
            score      = d.get("score", d.get("entry_score", 0))
            feed.append({
                "asset":      r["asset"],
                "side":       r["side"].upper(),
                "pnl_usd":    round(r["pnl_usd"] or 0, 2),
                "pnl_pct":    round((r["pnl_pct"] or 0) * 100, 1),
                "score":      score,
                "meta_key":   meta_key,
                "meta_boost": meta_boost,
                "created_at": int(r["created_at"] or 0),
                "win":        (r["pnl_usd"] or 0) > 0,
            })
        return {"feed": feed}
    except Exception as e:
        log.warning(f"[ML Feed] {e}")
        return {"feed": []}


# ── ML Intelligence Status ──────────────────────────────────────────────────

@app.get("/api/ml_status")
async def get_ml_status():
    try:
        from intelligence.intelligence_model import intelligence_model
        from intelligence.experience_buffer import experience_buffer

        data = experience_buffer.get_training_data()
        total = len(data)
        wins  = sum(1 for r in data if int(r.get('is_win', 0)) == 1)
        losses = total - wins
        win_rate = (wins / total * 100) if total > 0 else 0.0

        min_samples = getattr(config, 'INTELLIGENCE_RETRAIN_MIN_SAMPLES', 300)
        progress_pct = min(total / min_samples * 100, 100.0)

        # Recent 20 trades win/loss streak
        recent = data[-20:] if len(data) >= 20 else data
        recent_results = [{"win": bool(int(r.get('is_win', 0))), "asset": r.get('asset', '?'), "score": r.get('score', 0)} for r in recent]

        # Feature importance proxy — avg score of wins vs losses
        win_scores  = [float(r.get('score', 0)) for r in data if int(r.get('is_win', 0)) == 1]
        loss_scores = [float(r.get('score', 0)) for r in data if int(r.get('is_win', 0)) == 0]
        avg_win_score  = sum(win_scores)  / len(win_scores)  if win_scores  else 0.0
        avg_loss_score = sum(loss_scores) / len(loss_scores) if loss_scores else 0.0

        return {
            "is_ready":        intelligence_model.is_ready,
            "is_training":     intelligence_model.is_training,
            "total_samples":   total,
            "wins":            wins,
            "losses":          losses,
            "win_rate":        round(win_rate, 1),
            "min_samples":     min_samples,
            "progress_pct":    round(progress_pct, 1),
            "last_train_samples": intelligence_model.last_train_samples,
            "avg_win_score":   round(avg_win_score, 1),
            "avg_loss_score":  round(avg_loss_score, 1),
            "recent_results":  recent_results,
        }
    except Exception as e:
        log.warning(f"[ML Status] {e}")
        return {
            "is_ready": False, "is_training": False,
            "total_samples": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "min_samples": 300, "progress_pct": 0.0,
            "last_train_samples": 0, "avg_win_score": 0.0, "avg_loss_score": 0.0,
            "recent_results": [],
        }


# ── ML Export ───────────────────────────────────────────────────────────────

@app.get("/api/ml_export")
async def ml_export(type: str = "meta"):
    """
    Export ML Intelligence data as CSV.
    ?type=meta    → Meta pattern stats (winrate, pnl, samples, delta per coin)
    ?type=experience → Raw ML experience buffer (all labeled trades)
    ?type=combined  → Combined: meta stats + per-coin score delta summary
    """
    import csv, io, sqlite3, time
    from intelligence.experience_buffer import experience_buffer

    now_str = time.strftime("%Y%m%d_%H%M%S")

    if type == "meta":
        # Meta pattern stats: winrate, pnl, samples, score delta per coin
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT pattern_key, winrate_ema, pnl_ema, samples, updated_at "
            "FROM meta_pattern_stats ORDER BY samples DESC"
        ).fetchall()
        conn.close()

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pattern_key", "mode", "asset", "side", "winrate_pct",
                    "pnl_ema_usd", "samples", "score_delta", "status", "updated_at"])
        meta_max_delta = getattr(config.SIGNAL, "meta_max_delta", 10)
        meta_boost     = getattr(config.SIGNAL, "meta_boost_threshold", 0.68)
        meta_penalty   = getattr(config.SIGNAL, "meta_penalty_threshold", 0.35)
        meta_min       = getattr(config.SIGNAL, "meta_min_samples", 10)
        for r in rows:
            key   = r["pattern_key"]
            parts = key.split("_")
            mode  = parts[0] if len(parts) >= 3 else "?"
            side  = parts[-1] if len(parts) >= 3 else "?"
            asset = "_".join(parts[1:-1]).upper()
            wr    = r["winrate_ema"]
            n     = r["samples"]
            if n < meta_min:
                delta, status = 0, "insufficient_data"
            elif wr >= meta_boost:
                delta, status = meta_max_delta, f"BOOST +{meta_max_delta}"
            elif wr <= meta_penalty:
                delta, status = -meta_max_delta, f"PENALTY -{meta_max_delta}"
            else:
                delta, status = 0, "neutral"
            w.writerow([
                key, mode, asset, side.upper(),
                round(wr * 100, 1),
                round(r["pnl_ema"], 4),
                n, delta, status,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["updated_at"] or 0))
            ])
        out.seek(0)
        filename = f"kara_meta_patterns_{now_str}.csv"

    elif type == "experience":
        # Full ML experience buffer
        data = experience_buffer.get_training_data()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pos_id", "asset", "side", "score", "meta_delta",
                    "oi_score", "liq_score", "ob_score", "session_bonus",
                    "funding_rate", "realized_vol", "trend_pct",
                    "expected_edge", "actual_pnl_pct", "duration_sec", "is_win"])
        for r in data:
            w.writerow([
                r.get("pos_id",""), r.get("asset",""), r.get("side",""),
                r.get("score",0), r.get("meta_delta",0),
                r.get("oi_score",0), r.get("liq_score",0), r.get("ob_score",0),
                r.get("session_bonus",0), r.get("funding_rate",0),
                r.get("realized_vol",0), r.get("trend_pct",0),
                r.get("expected_edge",""), r.get("actual_pnl_pct",""),
                r.get("duration_sec",""), r.get("is_win",""),
            ])
        out.seek(0)
        filename = f"kara_ml_experience_{now_str}.csv"

    else:  # combined
        # Per-coin summary: winrate, total trades, wins, losses, avg pnl, score delta
        data = experience_buffer.get_training_data()

        # Aggregate by asset+side
        from collections import defaultdict
        stats = defaultdict(lambda: {"wins": 0, "total": 0, "pnl_sum": 0.0, "scores": []})
        for r in data:
            key = f"{r.get('asset','?')}_{r.get('side','?')}"
            stats[key]["total"] += 1
            stats[key]["pnl_sum"] += float(r.get("actual_pnl_pct", 0) or 0)
            stats[key]["scores"].append(float(r.get("score", 0) or 0))
            if int(r.get("is_win", 0) or 0) == 1:
                stats[key]["wins"] += 1

        # Also pull meta pattern delta from DB
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        meta_rows = {
            r["pattern_key"]: r
            for r in conn.execute("SELECT * FROM meta_pattern_stats").fetchall()
        }
        conn.close()

        meta_max_delta = getattr(config.SIGNAL, "meta_max_delta", 10)
        meta_boost     = getattr(config.SIGNAL, "meta_boost_threshold", 0.68)
        meta_penalty   = getattr(config.SIGNAL, "meta_penalty_threshold", 0.35)
        meta_min       = getattr(config.SIGNAL, "meta_min_samples", 10)

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["asset", "side", "total_trades", "wins", "losses",
                    "winrate_pct", "avg_pnl_pct", "avg_score",
                    "meta_winrate_ema_pct", "meta_samples",
                    "score_delta", "delta_status"])
        for key, d in sorted(stats.items()):
            parts = key.split("_", 1)
            asset = parts[0]
            side  = parts[1].upper() if len(parts) > 1 else "?"
            total = d["total"]
            wins  = d["wins"]
            wr    = wins / total * 100 if total > 0 else 0.0
            avg_pnl = d["pnl_sum"] / total * 100 if total > 0 else 0.0
            avg_sc  = sum(d["scores"]) / len(d["scores"]) if d["scores"] else 0.0

            # Look up meta pattern (scalper mode first, then standard)
            meta_key = f"scalper_{asset}_{side.lower()}"
            if meta_key not in meta_rows:
                meta_key = f"standard_{asset}_{side.lower()}"
            mr = meta_rows.get(meta_key)
            meta_wr  = round(mr["winrate_ema"] * 100, 1) if mr else ""
            meta_n   = mr["samples"] if mr else 0
            if mr and meta_n >= meta_min:
                mw = mr["winrate_ema"]
                if mw >= meta_boost:
                    delta, status = meta_max_delta, f"BOOST +{meta_max_delta}"
                elif mw <= meta_penalty:
                    delta, status = -meta_max_delta, f"PENALTY -{meta_max_delta}"
                else:
                    delta, status = 0, "neutral"
            else:
                delta, status = 0, "insufficient_data"

            w.writerow([
                asset, side, total, wins, total - wins,
                round(wr, 1), round(avg_pnl, 3), round(avg_sc, 1),
                meta_wr, meta_n, delta, status,
            ])
        out.seek(0)
        filename = f"kara_ml_combined_{now_str}.csv"

    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


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
