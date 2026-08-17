"""Controller HTTP para Domain CRUD + deploy/undeploy/status."""
from flask import Blueprint, jsonify, request

from app.core.http import pagination
from app.modules.domains.schema import (
    DomainCreate,
    DomainListResponse,
    DomainResponse,
    DomainStatusResponse,
    DomainUpdate,
)
from app.modules.domains.service import DomainService

bp = Blueprint("domains", __name__, url_prefix="/api/domains")


def _serialize(d) -> dict:
    return DomainResponse.from_domain(d).model_dump(mode="json")


@bp.get("")
def list_domains():
    """Lista Domains (no soft-deleted) con paginacion y filtros
    ---
    tags: [Domains]
    parameters:
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
      - {name: application_id, in: query, type: integer}
      - {name: scoop_id, in: query, type: integer}
    responses:
      200: {description: Listado paginado de Domains}
    """
    page, limit = pagination()
    result = DomainService.list(
        page=page, limit=limit,
        application_id=request.args.get("application_id", type=int),
        scoop_id=request.args.get("scoop_id", type=int),
    )
    return jsonify(DomainListResponse(
        items=[DomainResponse.from_domain(d) for d in result["items"]],
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump(mode="json"))


@bp.post("")
def create_domain():
    """Crea un Domain (asocia un subdominio a un Scoop existente)
    ---
    tags: [Domains]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [application_id, scoop_id, host]
          properties:
            application_id: {type: integer}
            scoop_id: {type: integer}
            host: {type: string, example: "notas.resto.com"}
            tls: {type: boolean, default: true}
    responses:
      201: {description: Domain creado (estado pending hasta deploy)}
      400: {description: Scoop no pertenece a la app o no es tipo api}
      409: {description: host duplicado}
    """
    payload = DomainCreate(**(request.get_json(silent=True) or {}))
    domain = DomainService.create(payload.model_dump())
    return jsonify(_serialize(domain)), 201


@bp.get("/<int:domain_id>")
def get_domain(domain_id: int):
    """Obtiene un Domain por id
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Domain}
      404: {description: No existe}
    """
    return jsonify(_serialize(DomainService.get(domain_id)))


@bp.put("/<int:domain_id>")
def update_domain(domain_id: int):
    """Actualiza `host` y/o `tls` (application_id/scoop_id son inmutables)
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            host: {type: string}
            tls: {type: boolean}
    responses:
      200: {description: Domain actualizado}
      404: {description: No existe}
    """
    payload = DomainUpdate(**(request.get_json(silent=True) or {}))
    domain = DomainService.update(domain_id, payload.model_dump(exclude_unset=True))
    return jsonify(_serialize(domain))


@bp.delete("/<int:domain_id>")
def delete_domain(domain_id: int):
    """Soft-delete del Domain. Si tiene recursos aplicados, los elimina primero.
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Domain eliminado}
      404: {description: No existe}
    """
    DomainService.soft_delete(domain_id)
    return jsonify({"deleted": domain_id})


@bp.post("/<int:domain_id>/deploy")
def deploy_domain(domain_id: int):
    """Aplica Ingress + Certificate + DNS override del Domain en el cluster.
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Recursos aplicados}
      404: {description: No existe}
    """
    domain = DomainService.get(domain_id)
    return jsonify(DomainService.deploy(domain))


@bp.delete("/<int:domain_id>/deploy")
def undeploy_domain(domain_id: int):
    """Elimina Certificate, Ingress y DNS override del Domain.
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Recursos eliminados}
      404: {description: No existe}
    """
    domain = DomainService.get(domain_id)
    return jsonify(DomainService.undeploy(domain))


@bp.get("/<int:domain_id>/status")
def domain_status(domain_id: int):
    """Estado del Domain contrastado con el cluster.
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Estado del Domain}
      404: {description: No existe}
    """
    domain = DomainService.get(domain_id)
    result = DomainService.status(domain)
    return jsonify(DomainStatusResponse(
        domain=DomainResponse.from_domain(domain),
        **result,
    ).model_dump(mode="json"))


@bp.get("/<int:domain_id>/certificate")
def domain_certificate(domain_id: int):
    """Estado detallado del Certificate del Domain (alias de /status, sin
    el bloque de DomainResponse).
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
    responses:
      200: {description: Estado del Certificate}
    """
    domain = DomainService.get(domain_id)
    return jsonify(DomainService.status(domain))


@bp.get("/<int:domain_id>/certificate/logs")
def domain_certificate_logs(domain_id: int):
    """Logs del controller cert-manager para el Certificate del Domain.
    ---
    tags: [Domains]
    parameters:
      - {name: domain_id, in: path, required: true, type: integer}
      - {name: tail_lines, in: query, type: integer, default: 100}
    responses:
      200: {description: Logs filtrados}
    """
    domain = DomainService.get(domain_id)
    return jsonify(DomainService.certificate_logs(
        domain, tail_lines=request.args.get("tail_lines", 100, type=int)
    ))