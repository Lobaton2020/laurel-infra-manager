"""Modelo `DomainPool`: catalogo de dominios de segundo nivel del usuario.

Cada fila es un dominio que el usuario posee (ej. `andreslobaton.top`).
Sirve de lista de opciones al crear un subdominio (Domain) en el front.
"""
from app.core.db import db
from app.core.utils import utcnow


class DomainPool(db.Model):
    """Dominio de segundo nivel propio del usuario.

    Atributos:
        domain:       FQDN unico en minusculas, sin subdominio (ej. `andreslobaton.top`).
        description:  nota opcional del usuario.
    """

    __tablename__ = "domain_pool"

    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(253), unique=True, nullable=False)
    description = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<DomainPool {self.domain}>"
