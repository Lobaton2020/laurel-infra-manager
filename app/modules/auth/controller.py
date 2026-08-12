"""Endpoints de autenticacion."""
from flask import Blueprint, jsonify, request

from app.core.errors import AppError
from app.core.http import parse_body
from app.modules.auth.service import (
    AuthError,
    current_google_client_id,
    decode_jwt,
    issue_jwt,
    upsert_user,
    verify_google_id_token,
)
from app.modules.users.model import User
from pydantic import BaseModel

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


class GoogleLoginRequest(BaseModel):
    credential: str


@bp.get("/config")
def auth_config():
    """Echo de la config publica de auth: que CLIENT_ID usar.
    El front lo consume al arrancar para inicializar <GoogleOAuthProvider>.
    """
    return jsonify({
        "google_client_id": current_google_client_id() or "",
        "login_required": True,
    })


@bp.post("/google")
def google_login():
    """Login con Google Sign-In.

    Body: {"credential": "<google_id_token>"}
    Devuelve: {"token": "<jwt>", "expires_at": "ISO", "user": {...}}
    """
    payload = parse_body(GoogleLoginRequest)
    if not payload.credential:
        raise AppError("Falta 'credential' en el body", status_code=422)

    claims = verify_google_id_token(payload.credential)
    user = upsert_user(claims)
    token, expires_at = issue_jwt(user)

    return jsonify({
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": user.to_dict(),
    })


@bp.get("/me")
def get_me():
    """Devuelve el usuario actual a partir del JWT (Authorization: Bearer ...)."""
    from app.core.auth import _bearer
    from app.modules.auth.service import AuthError, decode_jwt

    token = _bearer()
    if not token:
        raise AppError("Falta 'Authorization: Bearer'", status_code=401, reason="missing_token")
    try:
        claims = decode_jwt(token)
    except AuthError as exc:
        raise AppError(str(exc), status_code=401, reason=exc.reason)
    return jsonify({
        "user": {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "name": claims.get("name"),
        }
    })


@bp.post("/logout")
def logout():
    """Stateless: no hay nada que invalidar en el servidor. El front borra
    el token de localStorage. Devuelve 200 explicito para que el front sepa
    que el backend lo registro (evento auditable)."""
    from flask import g
    from app.modules.audits.service import AuditService
    user = getattr(g, "user", None)
    if user:
        AuditService.log("logout", "session", user.get("sub"), user_id=user.get("sub"))
    return jsonify({"logged_out": True})
