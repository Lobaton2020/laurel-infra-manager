"""Controller del modulo System (secretos y, en el futuro, otros recursos del
sistema gestionados por el propio API)."""
from flask import Blueprint, jsonify, request

from app.core.errors import AppError
from app.core.http import parse_body
from app.modules.audits.service import AuditService
from app.modules.system.service import SystemSecretService
from pydantic import BaseModel

bp = Blueprint("system", __name__, url_prefix="/api/system")


class UpdateSecretBody(BaseModel):
    content: str | None = None


@bp.get("/secrets")
def list_managed_secrets():
    """Lista los secretos del sistema gestionados por la API
    ---
    tags: [System]
    responses:
      200: {description: Lista resumen de secretos del sistema}
    """
    return jsonify({"items": SystemSecretService.list_managed()})


@bp.get("/secrets/<secret_id>")
def get_managed_secret(secret_id: str):
    """Devuelve el contenido editable de un secreto del sistema
    ---
    tags: [System]
    parameters:
      - {name: secret_id, in: path, type: string, required: true,
        description: "ID del secreto en la whitelist (laurel-secrets, laurel-kubeconfig)"}
    responses:
      200: {description: Contenido del secreto}
      403: {description: El ID no esta en la whitelist}
      404: {description: El secreto no existe en el cluster}
    """
    return jsonify(SystemSecretService.get_content(secret_id))


@bp.put("/secrets/<secret_id>")
def update_managed_secret(secret_id: str):
    """Reemplaza el contenido editable del secreto y hace rollout del deployment
    ---
    tags: [System]
    parameters:
      - {name: secret_id, in: path, type: string, required: true}
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              content:
                type: string
                description: Texto plano del secreto (.env o kubeconfig)
    responses:
      200: {description: Secreto actualizado y deployment reiniciado}
      400: {description: Contenido invalido}
      403: {description: El ID no esta en la whitelist}
      404: {description: El secreto no existe en el cluster}
    """
    body = parse_body(UpdateSecretBody)
    result = SystemSecretService.update_content(secret_id, body.content)
    # Auditoria: no guardamos el contenido, solo el tamano y el id
    AuditService.log(
        action="update",
        entity_type="system_secret",
        entity_id=secret_id,
        new_data={
            "size_bytes": result.get("size_bytes"),
            "restarted": result.get("restarted"),
            "saved": result.get("saved"),
        },
    )
    return jsonify(result)
