"""Auth con Google Sign-In (JWT stateless).

Flujo:
1. El front hace el popup de Google y obtiene un `id_token`.
2. POST /api/auth/google con `{credential: <id_token>}`.
3. Backend valida el token contra Google (audiencia = GOOGLE_CLIENT_ID).
4. UPSERT del User por `sub`.
5. Firma un JWT propio (HS256, SECRET_KEY) y lo devuelve.
6. El front lo guarda en localStorage y lo manda en `Authorization: Bearer <jwt>`.
"""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from flask import current_app

from app.core.db import db
from app.core.errors import AppError
from app.modules.users.model import User

logger = logging.getLogger(__name__)

_AUDIENCE_KEY = "google_id_token_audience_checked"


class AuthError(AppError):
    """401 con detalles estructurados para que el front distinga causa."""

    def __init__(self, message: str, reason: str = "auth_failed"):
        super().__init__(message, status_code=401)
        self.reason = reason


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def verify_google_id_token(token: str) -> dict:
    """Valida el id_token de Google y devuelve los claims.

    Levanta AuthError si la firma o la audiencia no son validas, si el token
    expiro, o si GOOGLE_CLIENT_ID no esta configurado.
    """
    client_id = current_app.config["GOOGLE_CLIENT_ID"]
    if not client_id:
        raise AuthError(
            "GOOGLE_CLIENT_ID no esta configurado en el backend",
            reason="misconfigured",
        )
    try:
        # google.oauth2.id_token.verify_oauth2_token verifica firma + aud + exp.
        claims = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), audience=client_id
        )
    except ValueError as exc:
        raise AuthError(
            f"id_token invalido: {exc}",
            reason="invalid_token",
        ) from exc

    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("Emisor del id_token no es Google", reason="invalid_token")

    if not claims.get("email_verified"):
        logger.warning("Login con email no verificado: %s", claims.get("email"))

    return claims


def upsert_user(claims: dict) -> User:
    """Crea o actualiza un User a partir de los claims de Google."""
    sub = claims["sub"]
    user = User.query.filter_by(sub=sub).first()
    if user is None:
        user = User(sub=sub)
        db.session.add(user)
    user.email = claims.get("email")
    user.name = claims.get("name")
    user.picture_url = claims.get("picture")
    user.last_login_at = _now_utc()
    db.session.commit()
    return user


def issue_jwt(user: User) -> tuple[str, datetime]:
    """Firma y devuelve (jwt, expires_at)."""
    ttl = current_app.config["JWT_TTL_HOURS"]
    expires_at = _now_utc() + timedelta(hours=ttl)
    payload = {
        "sub": user.sub,
        "email": user.email,
        "name": user.name,
        "iat": _now_utc(),
        "exp": expires_at,
    }
    token = jwt.encode(
        payload,
        current_app.config["SECRET_KEY"],
        algorithm=current_app.config["JWT_ALGORITHM"],
    )
    return token, expires_at


def decode_jwt(token: str) -> dict:
    """Decodifica un JWT propio. Levanta AuthError si la firma no coincide
    o si expiro."""
    try:
        return jwt.decode(
            token,
            current_app.config["SECRET_KEY"],
            algorithms=[current_app.config["JWT_ALGORITHM"]],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expirado", reason="expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Token invalido: {exc}", reason="invalid_token") from exc


def current_google_client_id() -> str:
    """Para el front saber que CLIENT_ID usar (echo del config)."""
    return current_app.config["GOOGLE_CLIENT_ID"]
