from sqlalchemy import or_

from app.core.db import db
from app.modules.audits.model import Audit


def _current_user_email() -> str:
    """Devuelve el `email` del usuario autenticado; si no hay email, el `sub`;
    si no hay request autenticado, 'unknown'.

    Se guarda el email en vez del id para que la auditoria sea legible por una
    persona (quien hizo que). Permite que AuditService.log se use tanto en
    endpoints protegidos (donde `g.user` existe) como en sitios donde no hay
    contexto de auth (tests, helpers internos).
    """
    try:
        from flask import g
        user = getattr(g, "user", None)
        if user:
            if user.get("email"):
                return str(user["email"])
            if user.get("sub"):
                return str(user["sub"])
    except RuntimeError:
        pass
    return "unknown"


def _ilike(column, value: str):
    """Wrapper para ILIKE con comodines y escapando caracteres especiales del usuario."""
    safe = value.replace("%", r"\%").replace("_", r"\_")
    return column.ilike(f"%{safe}%", escape="\\")


def _match_expr(query, field: str, value: str):
    """Devuelve una restriccion OR de busqueda contra el campo pedido.

    Los campos de Audit son columnas simples; para old_data/new_data usamos CAST
    a string y aplicamos ILIKE sobre la representacion JSON. Asi un termino
    como 'peva' o '3020' matchea el contenido del snapshot.
    """
    col = getattr(Audit, field)
    return _ilike(col, value)


class AuditService:

    @staticmethod
    def log(action: str, entity_type: str, entity_id, new_data: dict | None = None,
            old_data: dict | None = None, user_id: str | None = None) -> Audit:
        if user_id is None:
            user_id = _current_user_email()
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
    def get_all(page: int = 1, limit: int = 50,
                entity_type: str | None = None,
                q: str | None = None) -> dict:
        """Lista de audits con paginacion y filtro `q` que busca en todas las
        columnas visibles (user_id, action, entity_type, entity_id) y en el
        contenido de old_data/new_data serializado.
        """
        query = Audit.query
        if entity_type:
            query = query.filter_by(entity_type=entity_type)
        if q:
            term = q.strip()
            if term:
                scalar_filters = [
                    _match_expr(query, "user_id", term),
                    _match_expr(query, "action", term),
                    _match_expr(query, "entity_type", term),
                    _match_expr(query, "entity_id", term),
                ]
                # Para los JSON columns: CAST a texto y aplicar ILIKE.
                data_filters = [
                    _ilike(db.cast(Audit.old_data, db.String), term),
                    _ilike(db.cast(Audit.new_data, db.String), term),
                ]
                query = query.filter(or_(*scalar_filters, *data_filters))
        query = query.order_by(Audit.created_at.desc(), Audit.id.desc())

        total = query.count()
        pages = (total + limit - 1) // limit if limit else 1
        items = query.offset((page - 1) * limit).limit(limit).all() if limit else query.all()

        return {
            "items": [a.to_dict() for a in items],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
            "q": q or "",
            "entity_type": entity_type or "",
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
