"""Tests del modulo auth (login con Google y JWT).

Mockeamos la verificacion del id_token de Google para no depender de la red.
"""
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.modules.auth.service import AuthError, decode_jwt, issue_jwt
from app.modules.users.model import User


class _FakeClaims(dict):
    """Claims minimos que necesita nuestro servicio."""
    def __init__(self, sub, email, name="Tester"):
        super().__init__(
            sub=sub, email=email, name=name,
            email_verified=True, picture=None,
            iss="https://accounts.google.com",
            aud="dev-client-id",
        )


def _valid_token(sub="user-1", email="tester@example.com", name="Tester"):
    now = datetime.now(timezone.utc)
    return {
        "sub": sub,
        "email": email,
        "name": name,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }


@pytest.fixture
def set_google_client_id(app):
    """Define GOOGLE_CLIENT_ID en el config para que las vistas no digan 'misconfigured'."""
    app.config["GOOGLE_CLIENT_ID"] = "dev-client-id"
    return app


def test_decode_jwt_accepts_valid_token(app):
    """Round-trip: emit -> decode -> claims."""
    with app.app_context():
        from app.core.db import db
        user = User.query.first() or User(sub="test-sub", email="x@y.z", name="Tester")
        if user.id is None:
            db.session.add(user)
            db.session.commit()
        token, expires_at = issue_jwt(user)
        claims = decode_jwt(token)
        assert claims["sub"] == "test-sub"
        assert claims["email"] == "x@y.z"
        assert claims["exp"] > time.time()


def test_decode_jwt_rejects_expired(app):
    """Un JWT expirado levanta AuthError con reason=expired."""
    expired = jwt.encode(
        {
            "sub": "x", "email": "y", "name": "z",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=10),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    with app.app_context():
        from app.core.errors import AppError
        with pytest.raises(AppError) as exc_info:
            decode_jwt(expired)
        assert exc_info.value.status_code == 401


def test_auth_config_returns_google_client_id(set_google_client_id, client):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["google_client_id"] == "dev-client-id"
    assert body["login_required"] is True


def test_me_requires_bearer(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.get_json()["reason"] == "missing_token"


def test_me_returns_claims_with_valid_jwt(client, app):
    """Inyectamos un JWT firmado valido y comprobamos que /me lo decodifica."""
    from flask import current_app
    with current_app.app_context():
        token = jwt.encode(
            _valid_token(sub="sub-1", email="a@b.c", name="Ana"),
            current_app.config["SECRET_KEY"],
            algorithm=current_app.config["JWT_ALGORITHM"],
        )
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["user"]["sub"] == "sub-1"
    assert body["user"]["email"] == "a@b.c"
