from app.core.db import db
from app.core.utils import utcnow

SCOOP_TYPES = ("api", "worker", "cronjob")

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_ERROR = "error"
SCOOP_STATUSES = (STATUS_ACTIVE, STATUS_PENDING, STATUS_ERROR)

# Traduccion para el frontend; el valor crudo se mantiene estable en BD.
STATUS_LABELS = {
    STATUS_ACTIVE: "Activo",
    STATUS_PENDING: "Pendiente",
    STATUS_ERROR: "Con errores",
}


class Scoop(db.Model):
    """Especificacion de un scoop: la infra que debe correr una aplicacion.

    Guarda lo que Kubernetes no puede darnos: la intencion de despliegue. A partir
    de aqui se generan los manifiestos (ver ManifestService).
    """

    __tablename__ = "scoops"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(63), unique=True, nullable=False)
    application = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False, default="api")
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING)
    version = db.Column(db.String(100))
    is_productive = db.Column(db.Boolean, default=False, nullable=False)

    requested_vcpu = db.Column(db.String(20), nullable=False, default="100m")
    requested_memory = db.Column(db.String(20), nullable=False, default="128Mi")
    limit_vcpu = db.Column(db.String(20), nullable=False, default="500m")
    limit_memory = db.Column(db.String(20), nullable=False, default="512Mi")

    min_replicas = db.Column(db.Integer, nullable=False, default=1)
    max_replicas = db.Column(db.Integer, nullable=False, default=1)

    url_registry = db.Column(db.String(255), nullable=False)

    # Puerto del Service, autoasignado en el rango 3xxx. Solo aplica a type='api'.
    port = db.Column(db.Integer, unique=True)
    namespace = db.Column(db.String(63), nullable=False, default="prod")
    # Expresion cron, obligatoria para type='cronjob'.
    schedule = db.Column(db.String(100))

    # --- Agnóstico de tecnología ---
    # Puerto que expone el contenedor (la imagen lo dicta). Si está, se genera
    # Service LoadBalancer con targetPort a este número. Si no, solo se crea el
    # pod (accesible internamente por su nombre DNS).
    container_port = db.Column(db.Integer)
    # Path HTTP de readiness/liveness. Si está, K8s los genera sobre container_port.
    # Si no, K8s no sabe si el pod está vivo y nunca reiniciará por fallos.
    health_path = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def exposes_service(self) -> bool:
        return self.type == "api"

    def __repr__(self) -> str:
        return f"<Scoop {self.name} ({self.type})>"
