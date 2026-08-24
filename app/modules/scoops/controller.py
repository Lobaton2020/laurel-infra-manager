"""Controller del catalogo de scoops y su ciclo de vida en el cluster."""
from flask import Blueprint, current_app, jsonify, request
from kubernetes.client.exceptions import ApiException

from app.core.errors import ConflictError
from app.core.constants import APP_NAMESPACE_PREFIX
from app.core.http import bool_arg, pagination, parse_body
from app.modules.cluster.service import K8sService
from app.modules.configstore.service import ConfigStoreService
from app.modules.scoops.deploy import DeployService
from app.modules.scoops.manifest import ManifestService
from app.modules.scoops.schema import (
    DeployRequest,
    ScoopCreate,
    ScoopListResponse,
    ScoopResponse,
    ScoopStatusResponse,
    ScoopUpdate,
)
from app.modules.scoops.service import ScoopService

bp = Blueprint("scoops", __name__, url_prefix="/api/scoops")


def _serialize(scoop) -> dict:
    return ScoopResponse.from_scoop(scoop).model_dump(mode="json")


@bp.get("")
def list_scoops():
    """Lista scoops del catalogo
    ---
    tags: [Scoops]
    parameters:
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
      - {name: application, in: query, type: string}
      - {name: type, in: query, type: string, enum: [api, worker, cronjob]}
      - {name: status, in: query, type: string, enum: [active, pending, error]}
      - {name: namespace, in: query, type: string}
      - {name: is_productive, in: query, type: boolean}
    responses:
      200: {description: Listado paginado de scoops}
    """
    page, limit = pagination()
    result = ScoopService.list(
        page=page,
        limit=limit,
        application=request.args.get("application"),
        type=request.args.get("type"),
        status=request.args.get("status"),
        namespace=request.args.get("namespace"),
        is_productive=bool_arg("is_productive"),
    )
    return jsonify(ScoopListResponse(
        items=[ScoopResponse.from_scoop(s) for s in result["items"]],
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump(mode="json"))


@bp.get("/available-env-from")
def available_env_from():
    """Lista ConfigMaps y Secrets pre-existentes en un namespace para que el
    usuario pueda seleccionarlos como refs `envFrom` al crear/editar un scoop.

    Si se pasa `app`, el namespace se autoderiva a `user-apps-<app>` y se
    filtra por la label de la app, para que solo vea recursos de su propia app.
    ---
    tags: [Scoops]
    parameters:
      - {name: app, in: query, type: string,
        description: 'Filtra por la label app y autoderiva el namespace a user-apps-<app>'}
      - {name: namespace, in: query, type: string,
        description: 'Override del namespace (default: user-apps-<app> si hay app, sino DEFAULT_NAMESPACE)'}
    responses:
      200: {description: Lista unificada de CM y Secret (incluye los auto-inyectables por convencion <app>-config / <app>-secret; el manifest deduplica si el usuario los vuelve a seleccionar)}
    """
    app_filter = request.args.get("app")
    namespace = (
        request.args.get("namespace")
        or (f"{APP_NAMESPACE_PREFIX}{app_filter}" if app_filter else current_app.config["DEFAULT_NAMESPACE"])
    )
    cms = ConfigStoreService.list_configmaps(namespace, app_filter)
    secrets = ConfigStoreService.list_secrets(namespace, app_filter)
    items = []
    for cm in cms:
        items.append({
            "type": "config_map",
            "name": cm["name"],
            "namespace": cm["namespace"],
            "app": cm.get("app") or "",
            "keys": cm.get("keys", []),
        })
    for s in secrets:
        items.append({
            "type": "secret",
            "name": s["name"],
            "namespace": s["namespace"],
            "app": s.get("app") or "",
            "keys": s.get("keys", []),
        })
    items.sort(key=lambda it: (it["type"], it["name"]))
    return jsonify({"items": items, "namespace": namespace})


@bp.post("")
def create_scoop():
    """Crea un scoop (el puerto 3xxx se asigna automaticamente)

    El `url_registry` es IMPLICITO al seleccionar la app: si el scoop se
    asocia a una `Application` con `docker_image_base`, el registry se
    deriva como `<docker_image_base>:<version|latest>`. El caller no
    necesita (ni debe) mandar `url_registry` cuando el scoop esta
    ligado a una app.
    ---
    tags: [Scoops]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [application]
          properties:
            name: {type: string, description: 'Opcional: se deriva de application'}
            application: {type: string, example: portafolio-web}
            application_id: {type: integer, description: 'ID de la Application (preferido sobre `application` slug)'}
            type: {type: string, enum: [api, worker, cronjob], default: api}
            version: {type: string, example: 1.4.2}
            is_productive: {type: boolean, default: false}
            requested_vcpu: {type: string, default: 100m}
            requested_memory: {type: string, default: 128Mi}
            limit_vcpu: {type: string, default: 500m}
            limit_memory: {type: string, default: 512Mi}
            min_replicas: {type: integer, default: 1}
            max_replicas: {type: integer, default: 1}
            url_registry: {type: string, description: 'Solo para scoops SIN app (legacy). Si la app tiene docker_image_base, se ignora y se usa el de la app.'}
            namespace: {type: string, default: prod}
            schedule: {type: string, description: 'Obligatorio si type=cronjob'}
    responses:
      201: {description: Scoop creado}
      400: {description: No se puede derivar url_registry (scoop sin app y sin url_registry)}
      409: {description: Nombre duplicado o sin puertos libres}
      422: {description: Datos invalidos}
    """
    payload = parse_body(ScoopCreate)
    scoop = ScoopService.create(payload.model_dump())
    return jsonify(_serialize(scoop)), 201


@bp.get("/<int:scoop_id>")
def get_scoop(scoop_id: int):
    """Obtiene un scoop por id
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
    responses:
      200: {description: Scoop}
      404: {description: No existe}
    """
    return jsonify(_serialize(ScoopService.get(scoop_id)))


@bp.put("/<int:scoop_id>")
def update_scoop(scoop_id: int):
    """Actualiza un scoop (name y type son inmutables)
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            application: {type: string}
            version: {type: string}
            status: {type: string, enum: [active, pending, error]}
            is_productive: {type: boolean}
            requested_vcpu: {type: string}
            requested_memory: {type: string}
            limit_vcpu: {type: string}
            limit_memory: {type: string}
            min_replicas: {type: integer}
            max_replicas: {type: integer}
            url_registry: {type: string}
            namespace: {type: string}
            schedule: {type: string}
    responses:
      200: {description: Scoop actualizado}
      404: {description: No existe}
    """
    payload = parse_body(ScoopUpdate)
    scoop = ScoopService.update(
        scoop_id, payload.model_dump(exclude_unset=True)
    )
    return jsonify(_serialize(scoop))


@bp.delete("/<int:scoop_id>")
def delete_scoop(scoop_id: int):
    """Elimina un scoop del catalogo
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - {name: undeploy, in: query, type: boolean, description: 'Si es true, elimina tambien sus recursos del cluster'}
      - {name: namespace, in: query, type: string, description: 'Namespace donde buscar el deploy activo'}
    responses:
      200: {description: Scoop eliminado}
      404: {description: No existe}
      409: {description: 'El scoop tiene un deploy activo; pasa ?undeploy=true'}
    """
    scoop = ScoopService.get(scoop_id)
    undeploy = bool(bool_arg("undeploy"))
    force = bool(bool_arg("force"))
    namespace_arg = request.args.get("namespace")
    ns = ManifestService.namespace_for(scoop, namespace_arg)
    result = {"deleted": scoop_id}

    # Si no nos piden undeploy ni force y el scoop tiene recursos en el cluster,
    # bloqueamos: borrar el catalogo dejando recursos huerfanos suele ser un
    # accidente. force=true salta el check (util para tests o limpieza manual).
    if not undeploy and not force:
        kinds = ["CronJob"] if scoop.type == "cronjob" else ["Deployment"]
        if scoop.exposes_service and scoop.port:
            kinds.append("Service")
        deployed = []
        for kind in kinds:
            try:
                if K8sService.exists(kind, ns, scoop.name):
                    deployed.append(kind)
            except ApiException:
                # Si el API server falla al consultar, dejamos pasar: el borrado
                # del catalogo sigue siendo valido y el operador vera el error.
                pass
        if deployed:
            raise ConflictError(
                f"El scoop '{scoop.name}' tiene un deploy activo en '{ns}' "
                f"({', '.join(deployed)}). Pasa ?undeploy=true para borrar la infra "
                "junto con el scoop."
            )

    if undeploy:
        result["cluster"] = DeployService.undeploy(scoop, namespace_arg)

    ScoopService.delete(scoop_id)
    return jsonify(result)


@bp.get("/<int:scoop_id>/manifests")
def preview_manifests(scoop_id: int):
    """Previsualiza los manifiestos que genera el scoop (no toca el cluster)
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - {name: namespace, in: query, type: string}
    responses:
      200: {description: Manifiestos generados}
    """
    scoop = ScoopService.get(scoop_id)
    return jsonify(DeployService.preview(scoop, request.args.get("namespace")))


@bp.post("/<int:scoop_id>/deploy")
def deploy_scoop(scoop_id: int):
    """Aplica los manifiestos del scoop en el cluster (idempotente)
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            namespace: {type: string, description: 'Por defecto el del scoop'}
            dry_run: {type: boolean, description: 'Valida contra el API server sin persistir'}
    responses:
      200: {description: Despliegue aplicado}
      409: {description: El namespace destino no existe}
    """
    scoop = ScoopService.get(scoop_id)
    payload = DeployRequest.model_validate(request.get_json(silent=True) or {})
    result = DeployService.deploy(scoop, payload.namespace, payload.dry_run)
    result["scoop"] = _serialize(scoop)
    return jsonify(result)


@bp.delete("/<int:scoop_id>/deploy")
def undeploy_scoop(scoop_id: int):
    """Elimina del cluster los recursos del scoop (el catalogo se conserva)
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - {name: namespace, in: query, type: string}
    responses:
      200: {description: Recursos eliminados}
    """
    scoop = ScoopService.get(scoop_id)
    return jsonify(DeployService.undeploy(scoop, request.args.get("namespace")))


@bp.get("/<int:scoop_id>/status")
def scoop_status(scoop_id: int):
    """Estado real del scoop en el cluster (reconcilia el status en BD)
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - {name: namespace, in: query, type: string}
    responses:
      200: {description: Estado del scoop}
    """
    scoop = ScoopService.get(scoop_id)
    result = DeployService.status(scoop, request.args.get("namespace"))
    return jsonify(ScoopStatusResponse(
        scoop=ScoopResponse.from_scoop(result["scoop"]),
        deployed=result["deployed"],
        namespace=result["namespace"],
        desired_replicas=result.get("desired_replicas"),
        ready_replicas=result.get("ready_replicas"),
        available_replicas=result.get("available_replicas"),
        pods=result["pods"],
        message=result.get("message"),
    ).model_dump(mode="json"))


@bp.get("/<int:scoop_id>/logs")
def scoop_logs(scoop_id: int):
    """Logs agregados de todos los pods del scoop
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
      - {name: namespace, in: query, type: string}
      - {name: tail_lines, in: query, type: integer, default: 200}
      - {name: previous, in: query, type: boolean}
      - {name: timestamps, in: query, type: boolean}
    responses:
      200: {description: Logs por pod}
      404: {description: El scoop no tiene pods}
    """
    scoop = ScoopService.get(scoop_id)
    return jsonify(DeployService.logs(
        scoop,
        namespace=request.args.get("namespace"),
        tail_lines=request.args.get("tail_lines", 200, type=int),
        previous=bool(bool_arg("previous")),
        timestamps=bool(bool_arg("timestamps")),
    ))


@bp.get("/<int:scoop_id>/certificate")
def scoop_certificate(scoop_id: int):
    """DEPRECATED: el certificado ahora se consulta por dominio.

    Devuelve 410 Gone. El cliente debe usar
    `GET /api/domains?scoop_id=<id>` para localizar el/los dominios del
    scoop y luego `GET /api/domains/<domain_id>/certificate`.
    ---
    tags: [Scoops]
    parameters:
      - {name: scoop_id, in: path, required: true, type: integer}
    responses:
      410: {description: Migrado a /api/domains/<id>/certificate}
    """
    from flask import abort
    abort(410, description=(
        "El endpoint /api/scoops/<id>/certificate fue migrado. "
        "Use GET /api/domains?scoop_id=<id> y luego "
        "GET /api/domains/<domain_id>/certificate."
    ))


@bp.get("/<int:scoop_id>/certificate/logs")
def scoop_certificate_logs(scoop_id: int):
    """DEPRECATED: idem /certificate, migrado a /api/domains/<id>/certificate/logs
    ---
    tags: [Scoops]
    responses:
      410: {description: Migrado a /api/domains/<id>/certificate/logs}
    """
    from flask import abort
    abort(410, description=(
        "El endpoint /api/scoops/<id>/certificate/logs fue migrado. "
        "Use GET /api/domains/<id>/certificate/logs."
    ))


@bp.get("/<int:scoop_id>/audits")
def scoop_audits(scoop_id: int):
    """DEPRECATED: el usuario pidio quitar la seccion de audits del detalle de scoop.

    Devuelve 410 Gone. Los audits siguen disponibles via el endpoint
    global `GET /api/audits?entity_type=scoop&entity_id=<id>` (o por
    el listado de auditoria en la UI principal).
    ---
    tags: [Scoops]
    responses:
      410: {description: Migrado a /api/audits}
    """
    from flask import abort
    abort(410, description=(
        "El endpoint /api/scoops/<id>/audits fue removido. "
        "Use GET /api/audits?entity_type=scoop&entity_id=<id>."
    ))