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

    # Estado de provision de la app: 'provisioning' mientras se crean los
    # repos externos; 'ok' si todos los checks pasan; 'error' si algun check
    # obligatorio (repo GitHub / repo Docker Hub) fallo.
    status = db.Column(db.String(20), default="provisioning", nullable=False)

    # Version semver de la app (la que el webhook pasa a Jenkins como TAG al
    # recibir un push a master). El webhook la auto-incrementa desde los
    # tags de Docker Hub (ver webhooks/controller.py::_compute_next_version);
    # la UI puede sobreescribirla via PATCH /current-version.
    current_version = db.Column(db.String(50), default="0.0.1", nullable=False)

    # Nota historica: existio una columna `test_cmd` (TEXT) donde el operador
    # configuraba el comando a correr como STAGE 1 del pipeline Jenkins.
    # Fue removida en favor de autodeteccion en el pipeline Groovy: el
    # job inspecciona archivos del repo (composer.json, package.json,
    # pytest.ini, etc.) y decide que framework correr. Ver
    # `app/modules/integrations/jenkins/service.py::ensure_job_config`.

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
    events = db.relationship(
        "AppEvent", backref="application", lazy="selectin",
        order_by="AppEvent.id.asc()",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    # Los builds se acceden via BuildsService.list_for_app(app.id); no
    # definimos relationship para evitar un import cruzado apps <-> builds
    # que rompe la carga de los modelos en el orden actual.

    def __repr__(self) -> str:
        return f"<Application {self.slug} (id={self.id})>"


class AppEvent(db.Model):
    """Check/evento de provision de una Application (timeline).

    Cada evento es un paso del bootstrap: crear repo GitHub, crear repo
    Docker Hub, etc. `status` es 'ok' o 'error'. Estos eventos permiten al
    usuario ver el timeline del proceso y entender por que una app quedo
    'ok' o 'error'.
    """

    __tablename__ = "app_events"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer, db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event": self.event,
            "status": self.status,
            "detail": self.detail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AppDeletionLog(db.Model):
    """Snapshot de toda la config de una Application al momento de borrarla.

    Guarda el JSON completo de la app + scoops + dominios + timeline de
    eventos + configmaps/secrets del namespace K8s. Asi, aunque el borrado
    elimina los recursos del cluster y marca los registros en BD, queda
    trazabilidad para auditoria.
    """

    __tablename__ = "app_deletion_logs"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, nullable=False, index=True)
    application_slug = db.Column(db.String(63), nullable=False)
    application_name = db.Column(db.String(100), nullable=False)
    workspace_id = db.Column(db.Integer)
    snapshot = db.Column(db.JSON, nullable=False)
    deleted_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    deleted_by = db.Column(db.String(200))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "application_id": self.application_id,
            "application_slug": self.application_slug,
            "application_name": self.application_name,
            "workspace_id": self.workspace_id,
            "snapshot": self.snapshot,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "deleted_by": self.deleted_by,
        }