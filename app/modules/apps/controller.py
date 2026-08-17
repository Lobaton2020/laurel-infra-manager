"""Controller HTTP para Application CRUD."""
from flask import Blueprint, jsonify, request

from app.core.http import pagination
from app.modules.apps.schema import (
    ApplicationCreate,
    ApplicationListResponse,
    ApplicationResponse,
    ApplicationUpdate,
)
from app.modules.apps.service import AppsService

bp = Blueprint("apps", __name__, url_prefix="/api/apps")


def _serialize(app, scoops_count: int = 0, domains_count: int = 0) -> dict:
    return ApplicationResponse.from_app(
        app, scoops_count=scoops_count, domains_count=domains_count
    ).model_dump(mode="json")


def _counts_for(app) -> tuple[int, int]:
    """Cuenta scoops vivos y domains vivos de la app."""
    from app.modules.scoops.model import Scoop
    from app.modules.domains.model import Domain

    scoops_count = (
        Scoop.query.filter_by(application_id=app.id).count()
    )
    domains_count = (
        Domain.query.filter_by(application_id=app.id, deleted_at=None).count()
    )
    return scoops_count, domains_count


@bp.get("")
def list_apps():
    """Lista Applications (no soft-deleted) con paginacion
    ---
    tags: [Apps]
    parameters:
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
      - {name: workspace_id, in: query, type: integer, description: 'Filtra por workspace (solo sus apps)'}
    responses:
      200: {description: Listado paginado de Applications}
    """
    page, limit = pagination()
    workspace_id = request.args.get("workspace_id", type=int)
    result = AppsService.list(page=page, limit=limit, workspace_id=workspace_id)
    items = []
    for a in result["items"]:
        sc, dc = _counts_for(a)
        items.append(ApplicationResponse.from_app(a, sc, dc))
    return jsonify(ApplicationListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump(mode="json"))


@bp.post("")
def create_app():
    """Crea una nueva Application
    ---
    tags: [Apps]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [name]
          properties:
            name: {type: string, example: "Mi App"}
            description: {type: string}
            github_repo_url: {type: string, description: 'URL completa. Si se pasa, NO se crea el repo en GitHub.'}
            docker_image_base: {type: string, description: '<registry>/<repo>. Default sugerido: <ns>/laurel-<slug>.'}
            create_github_repo: {type: boolean, default: false, description: 'Crear repo vacio en GitHub con prefijo laurel_ en la org configurada'}
    responses:
      201: {description: Application creada}
      409: {description: Nombre duplicado}
    """
    payload = ApplicationCreate(**(request.get_json(silent=True) or {}))
    app = AppsService.create(payload.model_dump())
    sc, dc = _counts_for(app)
    return jsonify(_serialize(app, sc, dc)), 201


@bp.get("/<int:app_id>")
def get_app(app_id: int):
    """Obtiene una Application por id
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
    responses:
      200: {description: Application}
      404: {description: No existe}
    """
    app = AppsService.get(app_id)
    sc, dc = _counts_for(app)
    return jsonify(_serialize(app, sc, dc))


@bp.put("/<int:app_id>")
def update_app(app_id: int):
    """Actualiza metadata de la Application (slug/name son inmutables)
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            description: {type: string}
            github_repo_url: {type: string, description: 'Override manual (no crea repo)'}
            docker_image_base: {type: string, description: 'Override manual (omite prefijo laurel_)'}
    responses:
      200: {description: Application actualizada}
      404: {description: No existe}
    """
    payload = ApplicationUpdate(**(request.get_json(silent=True) or {}))
    app = AppsService.update(app_id, payload.model_dump(exclude_unset=True))
    sc, dc = _counts_for(app)
    return jsonify(_serialize(app, sc, dc))


@bp.delete("/<int:app_id>")
def delete_app(app_id: int):
    """Soft-delete de la Application (no toca el cluster).

    Para borrar el namespace completo usar el flujo de force-delete que
    vive en otra fase.
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
    responses:
      200: {description: Application soft-deleted}
      404: {description: No existe}
    """
    app = AppsService.soft_delete(app_id)
    return jsonify({"deleted": app.id, "slug": app.slug})