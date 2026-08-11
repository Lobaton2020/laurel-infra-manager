"""Controller de recursos nativos de Kubernetes.

Complementa al catalogo: permite inspeccionar y operar sobre lo que ya existe en
el cluster, este gestionado por este API o no.
"""
from flask import Blueprint, Response, jsonify, request

from app.core.http import bool_arg, namespace_arg, raw_body
from app.modules.audits.service import AuditService
from app.modules.cluster.service import K8sService

bp = Blueprint("k8s", __name__, url_prefix="/api/k8s")


def _selector() -> str | None:
    return request.args.get("label_selector")


# --------------------------- Cluster ---------------------------

@bp.get("/cluster")
def cluster_info():
    """Informacion del cluster y sus nodos
    ---
    tags: [Cluster]
    responses:
      200: {description: Version, API server y nodos}
      502: {description: Cluster inalcanzable}
    """
    return jsonify(K8sService.cluster_info())


@bp.get("/namespaces")
def list_namespaces():
    """Lista los namespaces del cluster
    ---
    tags: [Cluster]
    responses:
      200: {description: Namespaces}
    """
    return jsonify(K8sService.list_namespaces())


@bp.get("/managed")
def list_managed():
    """Recursos gestionados por este API en un namespace
    ---
    tags: [Cluster]
    parameters:
      - {name: namespace, in: query, type: string, default: prod}
    responses:
      200: {description: Deployments, services e ingresses gestionados}
    """
    return jsonify(K8sService.list_managed(namespace_arg()))


# --------------------------- Pods ---------------------------

@bp.get("/pods")
def list_pods():
    """Lista pods de un namespace
    ---
    tags: [Pods]
    parameters:
      - {name: namespace, in: query, type: string, default: prod}
      - {name: label_selector, in: query, type: string, example: 'app.kubernetes.io/name=portafolio'}
    responses:
      200: {description: Resumen de pods}
    """
    return jsonify(K8sService.list_pods(namespace_arg(), _selector()))


@bp.get("/pods/<namespace>/<name>")
def get_pod(namespace: str, name: str):
    """Detalle completo de un pod
    ---
    tags: [Pods]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Pod}
      404: {description: No existe}
    """
    return jsonify(K8sService.get_pod(namespace, name))


@bp.delete("/pods/<namespace>/<name>")
def delete_pod(namespace: str, name: str):
    """Elimina un pod (el controlador lo recrea)
    ---
    tags: [Pods]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Pod eliminado}
      404: {description: No existe}
    """
    result = K8sService.delete_pod(namespace, name)
    AuditService.log("delete", "pod", f"{namespace}/{name}", result)
    return jsonify(result)


@bp.get("/pods/<namespace>/<name>/logs")
def pod_logs(namespace: str, name: str):
    """Logs de un contenedor del pod (texto plano)
    ---
    tags: [Pods]
    produces: [text/plain]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
      - {name: container, in: query, type: string}
      - {name: tail_lines, in: query, type: integer, default: 200}
      - {name: previous, in: query, type: boolean, description: 'Logs del contenedor anterior tras un crash'}
      - {name: timestamps, in: query, type: boolean}
    responses:
      200: {description: Logs}
    """
    logs = K8sService.pod_logs(
        namespace,
        name,
        container=request.args.get("container"),
        tail_lines=request.args.get("tail_lines", 200, type=int),
        previous=bool(bool_arg("previous")),
        timestamps=bool(bool_arg("timestamps")),
    )
    return Response(logs, mimetype="text/plain")


# --------------------------- Deployments ---------------------------

@bp.get("/deployments")
def list_deployments():
    """Lista deployments de un namespace
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: query, type: string, default: prod}
      - {name: label_selector, in: query, type: string}
    responses:
      200: {description: Resumen de deployments}
    """
    return jsonify(K8sService.list_deployments(namespace_arg(), _selector()))


@bp.get("/deployments/<namespace>/<name>")
def get_deployment(namespace: str, name: str):
    """Detalle completo de un deployment
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Deployment}
      404: {description: No existe}
    """
    return jsonify(K8sService.get_deployment(namespace, name))


@bp.post("/deployments/<namespace>")
def create_deployment(namespace: str):
    """Crea un deployment desde un manifiesto crudo
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      201: {description: Deployment creado}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.create("Deployment", namespace, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log(
            "create", "deployment",
            f"{namespace}/{manifest.get('metadata', {}).get('name')}", manifest,
        )
    return jsonify(result), 201


@bp.put("/deployments/<namespace>/<name>")
def update_deployment(namespace: str, name: str):
    """Actualiza un deployment desde un manifiesto crudo
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      200: {description: Deployment actualizado}
      404: {description: No existe}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.replace("Deployment", namespace, name, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log("update", "deployment", f"{namespace}/{name}", manifest)
    return jsonify(result)


@bp.delete("/deployments/<namespace>/<name>")
def delete_deployment(namespace: str, name: str):
    """Elimina un deployment
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Deployment eliminado}
      404: {description: No existe}
    """
    result = K8sService.delete("Deployment", namespace, name)
    AuditService.log("delete", "deployment", f"{namespace}/{name}", result)
    return jsonify(result)


@bp.post("/deployments/<namespace>/<name>/scale")
def scale_deployment(namespace: str, name: str):
    """Escala un deployment
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [replicas]
          properties:
            replicas: {type: integer, minimum: 0}
    responses:
      200: {description: Deployment escalado}
      400: {description: replicas invalido}
    """
    from app.core.errors import AppError

    body = raw_body()
    replicas = body.get("replicas")
    if not isinstance(replicas, int) or replicas < 0:
        raise AppError("'replicas' debe ser un entero >= 0", 400)

    result = K8sService.scale_deployment(namespace, name, replicas)
    AuditService.log("scale", "deployment", f"{namespace}/{name}", {"replicas": replicas})
    return jsonify(result)


@bp.post("/deployments/<namespace>/<name>/restart")
def restart_deployment(namespace: str, name: str):
    """Reinicia un deployment (rollout restart)
    ---
    tags: [Deployments]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Rollout reiniciado}
      404: {description: No existe}
    """
    result = K8sService.restart_deployment(namespace, name)
    AuditService.log("restart", "deployment", f"{namespace}/{name}", None)
    return jsonify(result)


# --------------------------- Services ---------------------------

@bp.get("/services")
def list_services():
    """Lista services de un namespace
    ---
    tags: [Services]
    parameters:
      - {name: namespace, in: query, type: string, default: prod}
      - {name: label_selector, in: query, type: string}
    responses:
      200: {description: Resumen de services}
    """
    return jsonify(K8sService.list_services(namespace_arg(), _selector()))


@bp.get("/services/<namespace>/<name>")
def get_service(namespace: str, name: str):
    """Detalle completo de un service
    ---
    tags: [Services]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Service}
      404: {description: No existe}
    """
    return jsonify(K8sService.get_service(namespace, name))


@bp.post("/services/<namespace>")
def create_service(namespace: str):
    """Crea un service desde un manifiesto crudo
    ---
    tags: [Services]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      201: {description: Service creado}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.create("Service", namespace, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log(
            "create", "service",
            f"{namespace}/{manifest.get('metadata', {}).get('name')}", manifest,
        )
    return jsonify(result), 201


@bp.put("/services/<namespace>/<name>")
def update_service(namespace: str, name: str):
    """Actualiza un service desde un manifiesto crudo
    ---
    tags: [Services]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      200: {description: Service actualizado}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.replace("Service", namespace, name, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log("update", "service", f"{namespace}/{name}", manifest)
    return jsonify(result)


@bp.delete("/services/<namespace>/<name>")
def delete_service(namespace: str, name: str):
    """Elimina un service
    ---
    tags: [Services]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Service eliminado}
      404: {description: No existe}
    """
    result = K8sService.delete("Service", namespace, name)
    AuditService.log("delete", "service", f"{namespace}/{name}", result)
    return jsonify(result)


# --------------------------- Ingresses ---------------------------

@bp.get("/ingresses")
def list_ingresses():
    """Lista ingresses de un namespace
    ---
    tags: [Ingresses]
    parameters:
      - {name: namespace, in: query, type: string, default: prod}
      - {name: label_selector, in: query, type: string}
    responses:
      200: {description: Resumen de ingresses con hosts y rutas}
    """
    return jsonify(K8sService.list_ingresses(namespace_arg(), _selector()))


@bp.get("/ingresses/<namespace>/<name>")
def get_ingress(namespace: str, name: str):
    """Detalle completo de un ingress
    ---
    tags: [Ingresses]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Ingress}
      404: {description: No existe}
    """
    return jsonify(K8sService.get_ingress(namespace, name))


@bp.post("/ingresses/<namespace>")
def create_ingress(namespace: str):
    """Crea un ingress desde un manifiesto crudo
    ---
    tags: [Ingresses]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      201: {description: Ingress creado}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.create("Ingress", namespace, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log(
            "create", "ingress",
            f"{namespace}/{manifest.get('metadata', {}).get('name')}", manifest,
        )
    return jsonify(result), 201


@bp.put("/ingresses/<namespace>/<name>")
def update_ingress(namespace: str, name: str):
    """Actualiza un ingress desde un manifiesto crudo
    ---
    tags: [Ingresses]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
      - {name: dry_run, in: query, type: boolean}
      - {name: body, in: body, required: true, schema: {type: object}}
    responses:
      200: {description: Ingress actualizado}
    """
    manifest = raw_body()
    dry_run = bool(bool_arg("dry_run"))
    result = K8sService.replace("Ingress", namespace, name, manifest, dry_run=dry_run)
    if not dry_run:
        AuditService.log("update", "ingress", f"{namespace}/{name}", manifest)
    return jsonify(result)


@bp.delete("/ingresses/<namespace>/<name>")
def delete_ingress(namespace: str, name: str):
    """Elimina un ingress
    ---
    tags: [Ingresses]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Ingress eliminado}
      404: {description: No existe}
    """
    result = K8sService.delete("Ingress", namespace, name)
    AuditService.log("delete", "ingress", f"{namespace}/{name}", result)
    return jsonify(result)
