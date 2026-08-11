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
      - {name: entity_type, in: query, type: string, enum: [component, deployment, service, ingress, pod]}
    responses:
      200: {description: Listado paginado de auditoria}
    """
    page, limit = pagination(default_limit=50)
    return jsonify(AuditService.get_all(
        page=page, limit=limit, entity_type=request.args.get("entity_type")
    ))
