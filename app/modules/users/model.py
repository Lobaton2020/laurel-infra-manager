"""Usuarios autenticados via Google Sign-In.

Se crean/actualizan on-demand al primer login (UPSERT por `sub`).
Solo guardamos el subject (`sub`) de Google como identificador estable;
el `email` puede cambiar si Google lo modifica.
"""
from datetime import datetime

from app.core.db import db
from app.core.utils import utcnow


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # Google subject (`sub`) es el identificador unico de la cuenta.
    sub = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(200), index=True)
    name = db.Column(db.String(200))
    picture_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sub": self.sub,
            "email": self.email,
            "name": self.name,
            "picture_url": self.picture_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:
        return f"<User {self.email or self.sub}>"
