"""Modelo `Domain`: asocia un subdominio publico a exactamente un Scoop.

Un Domain es el unico responsable de generar el Ingress, Certificate y
DNS override de su host. NO se autogenera al crear el scoop: se crea
como paso separado cuando el usuario decide exponer el scoop.

Reglas:
- `host` debe ser unico globalmente (no se puede compartir entre domains).
- `application_id` y `scoop_id` son requeridos.
- El namespace efectivo se deriva de `application.slug` (NO del domain).
- `secret_name` se deriva de `host` reemplazando `.` por `-`.
"""
from app.core.db import db
from app.core.utils import utcnow


class Domain(db.Model):
    """Subdominio publico (host) asociado a un Scoop.

    Atributos:
        host:          FQDN unico (ej. `notas.resto.com`).
        tls:           Si True, se genera Certificate LetsEncrypt. Default True.
        status:        `pending | error | active`. Se actualiza al consultar
                       el estado del Certificate.
        secret_name:   nombre del Secret K8s donde vive el cert TLS.
                       Por defecto: host con `.` reemplazado por `-`.
    """

    __tablename__ = "domains"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer, db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    scoop_id = db.Column(
        db.Integer, db.ForeignKey("scoops.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # backref desde Scoop: scoop.domains para listar los domains del scoop.
    scoop = db.relationship(
        "Scoop", backref=db.backref("domains", lazy="dynamic"),
        foreign_keys=[scoop_id],
    )

    host = db.Column(db.String(253), unique=True, nullable=False)
    tls = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(16), nullable=False, default="pending")
    secret_name = db.Column(db.String(63), nullable=False)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    def __repr__(self) -> str:
        return f"<Domain {self.host} ({self.status})>"