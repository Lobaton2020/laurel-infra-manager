"""Controller HTTP para el CRUD del catalogo de dominios de segundo nivel."""
from flask import Blueprint, jsonify, request

from app.modules.domain_pool.schema import (
    DomainPoolCreate,
    DomainPoolResponse,
    DomainPoolUpdate,
)
from app.modules.domain_pool.service import DomainPoolService

bp = Blueprint("domain_pool", __name__, url_prefix="/api/domain-pool")


def _serialize(p) -> dict:
    return DomainPoolResponse.from_pool(p).model_dump(mode="json")


@bp.get("")
def list_pool():
    """Lista los dominios de segundo nivel del catalogo (orden alfabetico)
    ---
    tags: [DomainPool]
    responses:
      200: {description: Listado de dominios del pool}
    """
    return jsonify({
        "items": [_serialize(p) for p in DomainPoolService.list()],
    })


@bp.post("")
def create_pool():
    """Registra un dominio de segundo nivel en el catalogo
    ---
    tags: [DomainPool]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [domain]
          properties:
            domain: {type: string, example: "andreslobaton.top"}
            description: {type: string}
    responses:
      201: {description: Dominio registrado}
      409: {description: Dominio duplicado}
    """
    payload = DomainPoolCreate(**(request.get_json(silent=True) or {}))
    pool = DomainPoolService.create(payload.model_dump())
    return jsonify(_serialize(pool)), 201


@bp.put("/<int:pool_id>")
def update_pool(pool_id: int):
    """Edita la `description` del dominio (el dominio es inmutable)
    ---
    tags: [DomainPool]
    parameters:
      - {name: pool_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            description: {type: string}
    responses:
      200: {description: Dominio actualizado}
      404: {description: No existe}
    """
    payload = DomainPoolUpdate(**(request.get_json(silent=True) or {}))
    pool = DomainPoolService.update(pool_id, payload.model_dump(exclude_unset=True))
    return jsonify(_serialize(pool))


@bp.delete("/<int:pool_id>")
def delete_pool(pool_id: int):
    """Borra el dominio del catalogo. Bloqueado si hay subdominios que lo usan.
    ---
    tags: [DomainPool]
    parameters:
      - {name: pool_id, in: path, required: true, type: integer}
    responses:
      200: {description: Dominio eliminado}
      404: {description: No existe}
      409: {description: En uso por uno o mas subdominios}
    """
    DomainPoolService.delete(pool_id)
    return jsonify({"deleted": True})
