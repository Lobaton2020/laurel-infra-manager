"""Decoradores y helpers para autenticacion con JWT (Bearer).

Patron: el front manda `Authorization: Bearer <jwt>` en cada request.
El backend decodifica y adjunta el usuario a `flask.g.user`. Si el token
falta o expira, devuelve 401 con un campo `reason` para que el front
pueda decidir entre re-login silencioso o dialog de error.
"""
import logging
from functools import wraps

from flask import current_app, g, request

from app.modules.auth.service import AuthError, decode_jwt

logger = logging.getLogger(__name__)


def _bearer() -> str | None:
    """Extrae el token del header `Authorization: Bearer <token>`."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def current_user() -> dict | None:
    """El usuario actual (claims del JWT) si `require_auth` ya corrio;
    None en endpoints publicos."""
    return getattr(g, "user", None)


def optional_user() -> dict | None:
    """Intenta autenticar pero NO falla si falta token. Para endpoints
    que aceptan tanto anon como autenticado (ej: /api/auth/me si el
    token llego, /api/auth/google si no)."""
    raw = _bearer()
    if not raw:
        return None
    try:
        claims = decode_jwt(raw)
    except AuthError:
        return None
    g.user = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name"),
    }
    return g.user


def require_auth(*, skip_paths: tuple[str, ...] = ()) -> dict:
    """Decorator que exige Authorization Bearer y setea g.user.

    `skip_paths` permite eximir rutas puntuales dentro de un mismo blueprint,
    aunque lo normal es no poner require_auth en rutas publicas.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            path = request.path or ""
            for skip in skip_paths:
                if path.endswith(skip) or path == skip:
                    return view(*args, **kwargs)

            token = _bearer()
            if not token:
                raise _unauthorized("Falta 'Authorization: Bearer'", reason="missing_token")
            try:
                claims = decode_jwt(token)
            except AuthError as exc:
                raise _unauthorized(str(exc), reason=exc.reason)
            g.user = {
                "sub": claims.get("sub"),
                "email": claims.get("email"),
                "name": claims.get("name"),
            }
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _unauthorized(message: str, reason: str):
    """Lanza un AppError 401 estructurado para que el front sepa como actuar."""
    from app.core.errors import AppError
    err = AppError(message, status_code=401)
    err.reason = reason  # type: ignore[attr-defined]
    return err


def public_path(path: str) -> bool:
    """Whitelist de paths publicos (no requieren Bearer)."""
    public_prefixes = (
        "/api/health",
        "/api/auth",
        "/api/webhooks",
        "/apidocs",
        "/api/docs",
        "/flasgger",
    )
    return any(path.startswith(p) for p in public_prefixes)


def authenticate_request() -> None:
    """Para usar como `before_request` global. Levanta 401 si la request
    no trae un Bearer valido, salvo que la ruta sea publica (whitelist).
    En modo TESTING el gate queda deshabilitado: los tests unitarios manejan
    sus propios fixtures.
    """
    from flask import current_app

    if current_app.config.get("TESTING"):
        return

    path = request.path or ""
    method = (request.method or "GET").upper()
    # Permitir OPTIONS (CORS preflight) sin auth: los navegadores lo envian
    # antes de la peticion real y no llevan credenciales.
    if method == "OPTIONS":
        return
    if public_path(path):
        return
    token = _bearer()
    if not token:
        raise _unauthorized("Falta 'Authorization: Bearer'", reason="missing_token")
    try:
        claims = decode_jwt(token)
    except AuthError as exc:
        raise _unauthorized(str(exc), reason=exc.reason)
    from flask import g
    g.user = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name"),
    }
