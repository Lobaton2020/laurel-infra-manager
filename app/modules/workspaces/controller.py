"""Controller HTTP para Workspace CRUD (scoped por usuario)."""
from flask import Blueprint, g, jsonify, request

from app.core.auth import require_auth
from app.core.http import pagination
from app.modules.workspaces.schema import (
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.modules.workspaces.service import WorkspaceService

bp = Blueprint("workspaces", __name__, url_prefix="/api/workspaces")


def _owner_sub() -> str:
    return g.user["sub"]


@bp.get("")
@require_auth()
def list_workspaces():
    """Lista los workspaces del usuario autenticado (no soft-deleted)
    ---
    tags: [Workspaces]
    parameters:
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
    responses:
      200: {description: Listado paginado de workspaces del usuario}
    """
    page, limit = pagination()
    result = WorkspaceService.list(_owner_sub(), page=page, limit=limit)
    items = [
        WorkspaceResponse.from_ws(ws, result["apps_count"].get(ws.id, 0))
        for ws in result["items"]
    ]
    return jsonify(WorkspaceListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump(mode="json"))


@bp.post("")
@require_auth()
def create_workspace():
    """Crea un Workspace. `owner_sub` sale del JWT, no del body.
    ---
    tags: [Workspaces]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [name]
          properties:
            name: {type: string, example: "Mi Workspace"}
            description: {type: string}
    responses:
      201: {description: Workspace creado}
      409: {description: Nombre/slug duplicado para el usuario}
    """
    payload = WorkspaceCreate(**(request.get_json(silent=True) or {}))
    ws = WorkspaceService.create(_owner_sub(), payload.model_dump())
    return jsonify(WorkspaceResponse.from_ws(ws).model_dump(mode="json")), 201


@bp.get("/<int:ws_id>")
@require_auth()
def get_workspace(ws_id: int):
    """Obtiene un Workspace por id (solo si es del usuario).
    ---
    tags: [Workspaces]
    parameters:
      - {name: ws_id, in: path, required: true, type: integer}
    responses:
      200: {description: Workspace}
      404: {description: No existe o no pertenece al usuario}
    """
    ws = WorkspaceService.get(ws_id, _owner_sub())
    return jsonify(
        WorkspaceResponse.from_ws(ws, WorkspaceService.apps_count(ws.id)).model_dump(mode="json")
    )


@bp.put("/<int:ws_id>")
@require_auth()
def update_workspace(ws_id: int):
    """Actualiza name/description de un Workspace. Si llega `name`, el slug
    se re-deriva de el.
    ---
    tags: [Workspaces]
    parameters:
      - {name: ws_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            name: {type: string}
            description: {type: string}
    responses:
      200: {description: Workspace actualizado}
      404: {description: No existe o no pertenece al usuario}
      409: {description: Nombre/slug duplicado}
    """
    payload = WorkspaceUpdate(**(request.get_json(silent=True) or {}))
    ws = WorkspaceService.update(ws_id, _owner_sub(), payload.model_dump(exclude_unset=True))
    return jsonify(
        WorkspaceResponse.from_ws(ws, WorkspaceService.apps_count(ws.id)).model_dump(mode="json")
    )


@bp.delete("/<int:ws_id>")
@require_auth()
def delete_workspace(ws_id: int):
    """Soft-delete del Workspace: sus apps se desagrupan (workspace_id=NULL),
    no se borran.
    ---
    tags: [Workspaces]
    parameters:
      - {name: ws_id, in: path, required: true, type: integer}
    responses:
      200: {description: Workspace soft-deleted}
      404: {description: No existe o no pertenece al usuario}
    """
    ws = WorkspaceService.soft_delete(ws_id, _owner_sub())
    return jsonify({"deleted": ws.id, "slug": ws.slug})