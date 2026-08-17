"""Modelo de primer nivel 'Workspace': agrupamiento logico sobre Applications.

Un Workspace es un contenedor logico para organizar Applications del mismo
usuario (permisos/categorizacion futuros). Cada workspace pertenece a un
unico usuario via `owner_sub` (el `sub` del JWT de Google que lo creo); cada
usuario ve solo sus propios workspaces.
"""
from app.core.db import db
from app.core.utils import utcnow


class Workspace(db.Model):
    """Workspace de primer nivel propiedad de un usuario.

    Atributos:
        name:        nombre legible (unico por usuario). Slug se deriva de name.
        slug:        DNS-1123, unico. Derivado de name via `slugify`.
        owner_sub:   `sub` del usuario Google que creo el workspace. Es la
                     fuente de verdad del scope: cada usuario ve SOLO sus
                     workspaces.
        description: texto libre hasta 500 chars.
    """

    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(63), unique=True, nullable=False, index=True)
    owner_sub = db.Column(db.String(100), nullable=False, index=True)
    description = db.Column(db.String(500))

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    def __repr__(self) -> str:
        return f"<Workspace {self.slug} (id={self.id}, owner={self.owner_sub})>"