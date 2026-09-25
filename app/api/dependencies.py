# app/api/dependencies.py
import hashlib
import hmac
import logging

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer()
_redis = None


def _redis_client():
    global _redis
    if _redis is not None or not settings.REDIS_URL:
        return _redis
    try:
        import redis

        client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)
        client.ping()
        _redis = client
    except Exception as exc:
        logger.warning("Redis no disponible — sin chequeo de blacklist (%s)", exc)
    return _redis


def _is_blacklisted(token: str) -> bool:
    """Misma clave que auth-service al cerrar sesión: blacklist:<sha256 del token>."""
    client = _redis_client()
    if client is None:
        return False
    try:
        return bool(client.exists(f"blacklist:{hashlib.sha256(token.encode()).hexdigest()}"))
    except Exception:
        return False


async def current_user_email(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    """El JWT de auth-service lleva el email en "sub": es el dueño de los pagos."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials
    if _is_blacklisted(token):
        raise unauthorized
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise unauthorized
    email = payload.get("sub")
    if not email or payload.get("type", "access") != "access":
        raise unauthorized
    return email


async def verify_internal_token(x_internal_token: str = Header(None)) -> None:
    """Rutas /internal/*: solo otros servicios, con INTERNAL_SERVICE_TOKEN."""
    expected = settings.INTERNAL_SERVICE_TOKEN
    if not expected or not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal token")
