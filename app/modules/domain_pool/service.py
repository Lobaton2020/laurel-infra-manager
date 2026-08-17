"""DomainPoolService: CRUD del catalogo de dominios de segundo nivel."""
from sqlalchemy.exc import IntegrityError

from app.core.db import db
from app.core.errors import ConflictError, NotFoundError
from app.modules.audits.service import AuditService
from app.modules.domain_pool.model import DomainPool


class DomainPoolService:

    @staticmethod
    def list() -> list[DomainPool]:
        return DomainPool.query.order_by(DomainPool.domain.asc()).all()

    @staticmethod
    def get(pool_id: int) -> DomainPool:
        pool = db.session.get(DomainPool, pool_id)
        if pool is None:
            raise NotFoundError(f"DomainPool {pool_id} no encontrado")
        return pool

    @staticmethod
    def create(data: dict) -> DomainPool:
        pool = DomainPool(
            domain=data["domain"],
            description=data.get("description"),
        )
        try:
            db.session.add(pool)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe un dominio '{data['domain']}' en el pool"
            ) from exc
        AuditService.log(
            "domain_pool_created", "domain_pool", pool.id,
            {"domain": pool.domain, "description": pool.description},
        )
        return pool

    @staticmethod
    def update(pool_id: int, data: dict) -> DomainPool:
        pool = DomainPoolService.get(pool_id)
        if "description" in data:
            pool.description = data["description"]
        db.session.commit()
        AuditService.log(
            "domain_pool_updated", "domain_pool", pool.id,
            {"description": pool.description},
        )
        return pool

    @staticmethod
    def delete(pool_id: int) -> DomainPool:
        pool = DomainPoolService.get(pool_id)
        from app.modules.domains.model import Domain
        in_use = (
            Domain.query
            .filter(Domain.deleted_at.is_(None))
            .filter(Domain.host.like(f"%.{pool.domain}"))
            .count()
        )
        if in_use:
            raise ConflictError(
                f"No se puede borrar '{pool.domain}': hay {in_use} subdominio(s) "
                f"registrado(s) que lo usan como sufijo"
            )
        db.session.delete(pool)
        db.session.commit()
        AuditService.log(
            "domain_pool_deleted", "domain_pool", pool.id,
            {"domain": pool.domain},
        )
        return pool
