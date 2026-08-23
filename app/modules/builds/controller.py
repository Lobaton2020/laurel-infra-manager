"""Controller del modulo Builds: lista de builds por app + set de current_version."""
import re

from flask import Blueprint, jsonify, request
from pydantic import BaseModel, Field, field_validator

from app.modules.apps.service import AppsService
from app.modules.builds.service import BuildsService

bp = Blueprint("builds", __name__, url_prefix="/api/apps")


_SEMVERISH = re.compile(r"^[A-Za-z0-9._+-]{1,50}$")


class SetVersionBody(BaseModel):
    version: str = Field(..., min_length=1, max_length=50)

    @field_validator("version")
    @classmethod
    def _v_format(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("version no puede estar vacia")
        if not _SEMVERISH.match(v):
            raise ValueError(
                "version solo admite letras, numeros, '.', '_', '+', '-'"
            )
        return v


@bp.get("/<int:app_id>/builds")
def list_builds(app_id: int):
    """Lista los ultimos 20 builds de la app, del mas reciente al mas viejo.
    ---
    tags: [Builds]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
      - {name: poll, in: query, type: boolean, default: true,
         description: 'Si true (default), hace polling a Jenkins de los builds en pending/running'}
    responses:
      200: {description: Lista de builds}
      404: {description: App no encontrada}
    """
    poll = request.args.get("poll", "true").lower() != "false"
    items = []
    for b in BuildsService.list_for_app(app_id):
        # Polling on-demand: un GET por build en estado vivo.
        # Limite simple: si hay N running, son N llamadas a Jenkins.
        # Aceptable para MVP; si crece, mover a un poller en background.
        if poll and b.status in ("pending", "running") and b.jenkins_url:
            BuildsService.get(app_id, b.id, poll=True)
        items.append(b.to_dict())
    return jsonify({"items": items})


@bp.get("/<int:app_id>/builds/<int:build_id>")
def get_build(app_id: int, build_id: int):
    """Detalle de un build (incluye el status actualizado de Jenkins).
    ---
    tags: [Builds]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
      - {name: build_id, in: path, required: true, type: integer}
    responses:
      200: {description: Build}
      404: {description: No existe}
    """
    build = BuildsService.get(app_id, build_id, poll=True)
    return jsonify(build.to_dict())


@bp.patch("/<int:app_id>/current-version")
def set_current_version(app_id: int):
    """Setea la version que se usara en el proximo build (push a master).
    ---
    tags: [Builds]
    parameters:
      - {name: app_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [version]
          properties:
            version: {type: string, example: '1.0.2'}
    responses:
      200: {description: App actualizada con la nueva version}
      422: {description: version invalida}
      404: {description: App no encontrada}
    """
    body = request.get_json(silent=True) or {}
    try:
        parsed = SetVersionBody.model_validate(body)
    except Exception:
        return jsonify({"error": "Datos invalidos"}), 422

    app = BuildsService.set_current_version(app_id, parsed.version)
    return jsonify({
        "id": app.id,
        "slug": app.slug,
        "current_version": app.current_version,
    })
