"""
KARA Dashboard — Auth Layer
Telegram Login Widget verification + JWT issue/validate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import config

# ── JWT (pure stdlib, no extra deps) ────────────────────────────────────────
import base64

_JWT_ALG   = "HS256"
_JWT_TTL   = 7 * 24 * 3600   # 7 days


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(header_b64: str, payload_b64: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(config.SECRET_KEY.encode(), msg, hashlib.sha256).digest()
    return _b64url_encode(sig)


def create_jwt(chat_id: str, username: str) -> str:
    header  = _b64url_encode(json.dumps({"alg": _JWT_ALG, "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps({
        "sub": chat_id,
        "username": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + _JWT_TTL,
    }).encode())
    sig = _sign(header, payload)
    return f"{header}.{payload}.{sig}"


def decode_jwt(token: str) -> dict:
    """Decode and verify JWT. Raises ValueError on invalid/expired."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise ValueError("Malformed token")

    expected = _sign(header_b64, payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        raise ValueError("Invalid signature")

    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get("exp", 0) < time.time():
        raise ValueError("Token expired")
    return payload


# ── Telegram Login Widget verification ──────────────────────────────────────

def verify_telegram_login(data: dict) -> bool:
    """
    Verify Telegram Login Widget hash per official spec.
    https://core.telegram.org/widgets/login#checking-authorization
    """
    token = config.TELEGRAM_TOKEN
    if not token:
        return False

    received_hash = data.get("hash", "")
    check_data = {k: v for k, v in data.items() if k != "hash"}

    # Build data-check-string: sorted key=value pairs joined by \n
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(check_data.items()))

    # Secret key = SHA256 of bot token
    secret = hashlib.sha256(token.encode()).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return False

    # Auth date must be within 24h
    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        return False

    return True


# ── FastAPI dependency ───────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """FastAPI dependency — returns JWT payload or raises 401."""
    token = None

    # 1. Try Authorization: Bearer header
    if credentials:
        token = credentials.credentials

    # 2. Fallback: cookie (for browser navigation)
    if not token:
        token = request.cookies.get("kara_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        return decode_jwt(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


async def get_admin_user(payload: dict = Depends(get_current_user)) -> dict:
    """Require admin (TELEGRAM_CHAT_ID matches)."""
    admin_id = str(config.TELEGRAM_CHAT_ID) if config.TELEGRAM_CHAT_ID else None
    if not admin_id or payload.get("sub") != admin_id:
        raise HTTPException(status_code=403, detail="Admin only")
    return payload
