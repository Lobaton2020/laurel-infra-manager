"""CRUD del catalogo de scoops."""
from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.core.db import db
from app.core.errors import ConflictError, NotFoundError
from app.modules.audits.service import AuditService
from app.modules.scoops.model import Scoop
from app.modules.scoops.schema import format_memory

# Campos que se auditan al crear/actualizar.
_TRACKED_FIELDS = (
    "name", "application", "type", "status", "version", "is_productive",
    "requested_vcpu", "requested_memory", "limit_vcpu", "limit_memory",
    "min_replicas", "max_replicas", "url_registry", "port", "namespace", "schedule",
    "container_port", "health_path",
)


def _snapshot(scoop: Scoop) -> dict:
    return {f: getattr(scoop, f) for f in _TRACKED_FIELDS}


def _memory_from_payload(data: dict, prefix: str) -> tuple[int, str] | None:
    """Lee `prefix_value` y `prefix_unit` del payload y los empaqueta.

    Devuelve None si ninguno de los dos esta presente (no hay cambio).
    """
    val = data.get(f"{prefix}_value")
    unit = data.get(f"{prefix}_unit")
    if val is None and unit is None:
        return None
    return val, unit or "M"


class ScoopService:

    @staticmethod
    def allocate_port() -> int:
        """Primer puerto libre del rango 3xxx.

        Se buscan huecos en vez de usar max()+1 para poder reutilizar los puertos
        que liberan los scoops eliminados.
        """
        start = current_app.config["SERVICE_PORT_RANGE_START"]
        end = current_app.config["SERVICE_PORT_RANGE_END"]

        used = {
            row[0] for row in
            db.session.query(Scoop.port).filter(Scoop.port.isnot(None)).all()
        }

        for port in range(start, end + 1):
            if port not in used:
                return port

        raise ConflictError(f"No quedan puertos libres en el rango {start}-{end}")

    @staticmethod
    def list(page: int = 1, limit: int = 20, application: str | None = None,
             type: str | None = None, status: str | None = None,
             namespace: str | None = None, is_productive: bool | None = None) -> dict:
        query = Scoop.query
        if application:
            query = query.filter_by(application=application)
        if type:
            query = query.filter_by(type=type)
        if status:
            query = query.filter_by(status=status)
        if namespace:
            query = query.filter_by(namespace=namespace)
        if is_productive is not None:
            query = query.filter_by(is_productive=is_productive)

        query = query.order_by(Scoop.created_at.desc(), Scoop.id.desc())

        total = query.count()
        pages = (total + limit - 1) // limit
        items = query.offset((page - 1) * limit).limit(limit).all()

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
        }

    @staticmethod
    def get(scoop_id: int) -> Scoop:
        scoop = db.session.get(Scoop, scoop_id)
        if not scoop:
            raise NotFoundError(f"No existe el scoop con id {scoop_id}")
        return scoop

    @staticmethod
    def get_by_name(name: str) -> Scoop | None:
        return Scoop.query.filter_by(name=name).first()

    @staticmethod
    def create(data: dict) -> Scoop:
        if ScoopService.get_by_name(data["name"]):
            raise ConflictError(f"Ya existe un scoop llamado '{data['name']}'")

        # El servidor decide container_port y health_path. No los pedimos al usuario
        # para mantener el form simple: el puerto interno se hereda de config.
        payload = {
            "name": data["name"],
            "application": data["application"],
            "type": data.get("type", "api"),
            "version": data.get("version"),
            "is_productive": data.get("is_productive", False),
            "requested_vcpu": data.get("requested_vcpu", "100m"),
            "limit_vcpu": data.get("limit_vcpu", "500m"),
            "requested_memory": data.get("requested_memory") or format_memory(
                data["requested_memory_value"], data.get("requested_memory_unit", "M")
            ),
            "limit_memory": data.get("limit_memory") or format_memory(
                data["limit_memory_value"], data.get("limit_memory_unit", "M")
            ),
            "min_replicas": data.get("min_replicas", 1),
            "max_replicas": data.get("max_replicas", 1),
            "url_registry": data["url_registry"],
            "namespace": data.get("namespace") or current_app.config["DEFAULT_NAMESPACE"],
            "schedule": data.get("schedule"),
            "container_port": data.get("container_port") or current_app.config["CONTAINER_PORT"],
            "health_path": data.get("health_path") or "/",
        }

        scoop = Scoop(**payload)
        # Todo scoop tipo 'api' consume un puerto del pool: el usuario lo vera
        # en la respuesta como `port` y es por donde accede desde LAN.
        if scoop.exposes_service:
            scoop.port = ScoopService.allocate_port()

        db.session.add(scoop)
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError("El scoop viola una restriccion de unicidad") from exc

        AuditService.log("create", "scoop", scoop.id, _snapshot(scoop))
        return scoop

    @staticmethod
    def update(scoop_id: int, data: dict) -> Scoop:
        scoop = ScoopService.get(scoop_id)
        old = _snapshot(scoop)

        # Aplicar campos escalares directos.
        for field in (
            "application", "version", "status", "is_productive",
            "requested_vcpu", "limit_vcpu",
            "min_replicas", "max_replicas",
            "url_registry", "namespace", "schedule",
            "container_port", "health_path",
        ):
            if data.get(field) is not None:
                setattr(scoop, field, data[field])

        # Memoria: acepta la cantidad completa ("128Mi") o value+unit suelto.
        for prefix in ("requested_memory", "limit_memory"):
            raw = data.get(prefix)
            if raw:
                setattr(scoop, prefix, raw)
                continue
            mem = _memory_from_payload(data, prefix)
            if mem is not None:
                setattr(scoop, prefix, format_memory(mem[0], mem[1]))

        # Validacion cruzada: los campos pueden llegar sueltos en un update parcial.
        if scoop.max_replicas < scoop.min_replicas:
            db.session.rollback()
            raise ConflictError("max_replicas no puede ser menor que min_replicas")

        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError("El scoop viola una restriccion de unicidad") from exc

        AuditService.log("update", "scoop", scoop.id, _snapshot(scoop), old)
        return scoop

    @staticmethod
    def delete(scoop_id: int) -> None:
        scoop = ScoopService.get(scoop_id)
        old = _snapshot(scoop)
        db.session.delete(scoop)
        db.session.commit()
        AuditService.log("delete", "scoop", scoop_id, None, old)

    @staticmethod
    def set_status(scoop: Scoop, status: str) -> Scoop:
        if scoop.status != status:
            old = {"status": scoop.status}
            scoop.status = status
            db.session.commit()
            AuditService.log("status", "scoop", scoop.id, {"status": status}, old)
        return scoop