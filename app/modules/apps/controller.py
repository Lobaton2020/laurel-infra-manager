"""Controller HTTP para Application CRUD."""
import re

from flask import Blueprint, current_app, jsonify, request

from app.core.errors import AppError
from app.core.http import pagination
from app.modules.apps.model import AppDeletionLog, AppEvent
from app.modules.apps.schema import (
    ApplicationCreate,
    ApplicationListResponse,
    ApplicationResponse,
    ApplicationUpdate,
)
from app.modules.apps.service import AppsService
from app.modules.integrations.docker import version_bump

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


@bp.get("/<int:app_id>/events")
def get_app_events(app_id: int):
    """Timeline de provision de una Application (repos GitHub/Docker Hub)
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
    responses:
      200: {description: Lista de eventos de provision}
      404: {description: No existe}
    """
    app = AppsService.get(app_id)
    return jsonify({"items": [e.to_dict() for e in app.events]})


@bp.get("/<int:app_id>/deletion-logs")
def get_app_deletion_logs(app_id: int):
    """Snapshots guardados al borrar esta Application (trazabilidad)
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
    responses:
      200: {description: Lista de deletion logs (uno por borrado)}
    """
    logs = (
        AppDeletionLog.query
        .filter_by(application_id=app_id)
        .order_by(AppDeletionLog.deleted_at.desc())
        .all()
    )
    return jsonify({"items": [l.to_dict() for l in logs]})


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
    """Hard-delete de la Application + limpieza absoluta.

    Borra el namespace K8s (`user-apps-<slug>` con cascade de todos sus
    recursos), el repo GitHub, el paquete GHCR y el registro en BD. Antes
    guarda un snapshot completo en `app_deletion_logs` para trazabilidad.
    ---
    tags: [Apps]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
    responses:
      200: {description: Application eliminada}
      404: {description: No existe}
    """
    app = AppsService.delete(app_id)
    return jsonify({"deleted": app.id, "slug": app.slug})


_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")


@bp.get("/<string:slug>/next_version")
def next_version_for_slug(slug: str):
    """Próxima versión semver que el pipeline asignará al siguiente build.

    Source-of-truth: tags existentes en Docker Hub para
    `docker.io/{DOCKERHUB_USER}/laurel_<slug>`. Independiente de que el
    tag git se cree o no (útil cuando el PAT es read-only).
    ---
    tags: [Apps]
    parameters:
      - {name: slug, in: path, required: true, type: string, description: "slug de la app"}
    responses:
      200: {description: next_version calculada}
      400: {description: slug invalido}
      503: {description: DOCKERHUB_USER/PASSWORD no configurados}
      502: {description: Docker Hub rechazó login o tags fetch}
    """
    if not _SLUG_RE.match(slug):
        raise AppError(
            "slug invalido: debe coincidir con [a-z0-9][a-z0-9_-]{0,61} (max 63 chars, sin _/- al inicio/fin)",
            status_code=400,
            reason="invalid_slug",
        )
    user = (current_app.config.get("DOCKERHUB_USER") or "").strip()
    password = current_app.config.get("DOCKERHUB_PASSWORD") or ""
    if not user or not password:
        raise AppError(
            "Docker Hub credentials not configured",
            status_code=503,
            reason="dockerhub_unconfigured",
        )
    try:
        nxt = version_bump.next_version(user, password, repo=f"laurel_{slug}")
    except version_bump.DockerHubError as exc:
        raise AppError(
            f"docker hub error: {exc}",
            status_code=502,
            reason="dockerhub_error",
        )
    return jsonify({
        "slug": slug,
        "namespace": user,
        "image": f"{user}/laurel_{slug}",
        "next_version": nxt,
    })