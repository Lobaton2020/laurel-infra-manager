"""Controller de auditoria."""
from flask import Blueprint, jsonify, request

from app.core.http import pagination
from app.modules.audits.service import AuditService

bp = Blueprint("audits", __name__, url_prefix="/api/audits")


@bp.get("")
def list_audits():
    """Historial de mutaciones sobre el catalogo y el cluster
    ---
    tags: [Audits]
    parameters:
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
      - name: entity_type
        in: query
        type: string
        description: Filtra por tipo de entidad (ej: 'scoop', 'cluster').
      - name: q
        in: query
        type: string
        description: |
          Busqueda libre que matchea (ILIKE) en `user_id`, `action`,
          `entity_type`, `entity_id` y en el contenido de `old_data`/`new_data`.
    responses:
      200: {description: Listado paginado de auditoria}
    """
    page, limit = pagination(default_limit=50)
    return jsonify(AuditService.get_all(
        page=page,
        limit=limit,
        entity_type=request.args.get("entity_type"),
        q=request.args.get("q"),
    ))


@bp.get("/<int:audit_id>")
def get_audit(audit_id: int):
    """Detalle de un audit puntual
    ---
    tags: [Audits]
    parameters:
      - {name: audit_id, in: path, required: true, type: integer}
    responses:
      200: {description: Audit}
      404: {description: No existe}
    """
    from app.core.errors import NotFoundError
    from app.modules.audits.model import Audit
    audit = Audit.query.get(audit_id)
    if not audit:
        raise NotFoundError(f"No existe el audit con id {audit_id}")
    return jsonify(audit.to_dict())
