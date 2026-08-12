"""Modulo Auth: login con Google Sign-In (JWT stateless)."""
from app.modules.auth.controller import bp
from app.modules.auth.service import AuthError

__all__ = ["bp", "AuthError"]
