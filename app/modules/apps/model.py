"""Modelo de primer nivel 'Application': agrupa scoops bajo un namespace.

Una Application posee un namespace dedicado en el cluster (mismo nombre que
su slug) y metadata externa (repo GitHub, imagen Docker base). El
namespace se crea de forma idempotente la primera vez que se despliega
un scoop de la app.

Esta entidad es la fuente de verdad para resolver el namespace de un
scoop (via `application_id` FK). El scoop sigue siendo el recurso
operacional (api/worker/cronjob); la Application es el "contenedor" logico.
"""
from app.core.db import db
from app.core.utils import utcnow


class Application(db.Model):
    """Aplicacion de primer nivel.

    Atributos:
        name:        nombre legible (unico). Slug se deriva de name.
        slug:        DNS-1123, unico. Es el nombre del namespace y se usa
                     como base para el repo GitHub (`laurel-<slug>`) y la
                     imagen Docker (`<namespace>/laurel-<slug>`).
        description: texto libre hasta 500 chars.
        github_repo_url: URL completa al repo en GitHub (o null si no
                     se integro GitHub al crear la app, o si se setea
                     manualmente a un repo externo).
        docker_image_base: nombre de imagen base sin tag, formato
                     `<registry>/<repo>` (ej. `aflobaton/laurel-notas`).
                     Puede setearse manualmente como excepcion al
                     prefijo `laurel_` por defecto.
    """

    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(63), unique=True, nullable=False, index=True)
    description = db.Column(db.String(500))

    github_repo_url = db.Column(db.String(255))
    docker_image_base = db.Column(db.String(255))

    # Workspace opcional (agrupamiento logico). NULL = "sin agrupar".
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"))
    workspace = db.relationship(
        "Workspace", backref=db.backref("applications", lazy="dynamic")
    )

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    scoops = db.relationship(
        "Scoop", backref=db.backref("app_record", lazy="selectin"),
        lazy="dynamic",
        passive_deletes=True,
        foreign_keys="Scoop.application_id",
    )
    domains = db.relationship(
        "Domain", backref="application", lazy="dynamic",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Application {self.slug} (id={self.id})>"