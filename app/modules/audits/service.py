from app.core.db import db
from app.modules.audits.model import Audit


class AuditService:

    @staticmethod
    def log(action: str, entity_type: str, entity_id, new_data: dict | None = None,
            old_data: dict | None = None, user_id: str = "unknown") -> Audit:
        audit = Audit(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            old_data=old_data,
            new_data=new_data,
        )
        db.session.add(audit)
        db.session.commit()
        return audit

    @staticmethod
    def get_all(page: int = 1, limit: int = 50, entity_type: str | None = None) -> dict:
        query = Audit.query
        if entity_type:
            query = query.filter_by(entity_type=entity_type)
        query = query.order_by(Audit.created_at.desc(), Audit.id.desc())

        total = query.count()
        pages = (total + limit - 1) // limit
        items = query.offset((page - 1) * limit).limit(limit).all()

        return {
            "items": [a.to_dict() for a in items],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
        }

    @staticmethod
    def get_by_entity(entity_type: str, entity_id) -> list[dict]:
        items = (
            Audit.query
            .filter_by(entity_type=entity_type, entity_id=str(entity_id))
            .order_by(Audit.created_at.desc(), Audit.id.desc())
            .all()
        )
        return [a.to_dict() for a in items]
