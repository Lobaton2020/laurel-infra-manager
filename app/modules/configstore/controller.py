"""HTTP endpoints de ConfigMaps y Secrets de aplicacion."""
from flask import Blueprint, current_app, jsonify, request

from app.core.http import parse_body
from app.modules.audits.service import AuditService
from app.modules.configstore.schema import (
    ConfigMapCreate,
    SecretCreate,
)
from app.modules.configstore.service import ConfigStoreService

bp = Blueprint("configstore", __name__, url_prefix="/api/configstore")


def _ns(app: str | None = None) -> str:
    """Namespace destino para listar / crear recursos.

    Precedencia:
    1. `?namespace=<ns>` explicito del caller (override).
    2. `?app=<slug>` -> `user-apps-<slug>` (cada app maneja todo en su
       namespace, asi filtrar por app filtra por namespace automaticamente).
    3. DEFAULT_NAMESPACE del cluster.
    """
    explicit = request.args.get("namespace")
    if explicit:
        return explicit
    if app:
        return ConfigStoreService.namespace_for_app(app)
    return current_app.config["DEFAULT_NAMESPACE"]


def _serialize_cm(cm: dict) -> dict:
    """Quita campos internos antes de devolver al cliente."""
    cm.pop("action", None)
    return cm


def _serialize_secret(s: dict) -> dict:
    s.pop("action", None)
    return s


# --------------------------- ConfigMaps ---------------------------

@bp.get("/configmaps")
def list_configmaps():
    """Lista ConfigMaps visibles para una app en su namespace.

    Cada app maneja su config independientemente: cuando se pasa `app`, el
    namespace se autoderiva a `user-apps-<slug>` y ademas se filtra por
    el label `app.kubernetes.io/laurel-app=<slug>`. Asi dos apps distintas
    jamas ven los ConfigMaps de la otra.
    ---
    tags: [ConfigStore]
    parameters:
      - {name: app, in: query, type: string, description: 'Filtra por app (slug) y autoderiva el namespace a user-apps-<slug>.'}
      - {name: namespace, in: query, type: string, description: 'Override del namespace (default: user-apps-<app> si hay app, sino DEFAULT_NAMESPACE)'}
    responses:
      200: {description: ConfigMaps (resumen, sin data) filtrados por app}
    """
    app = request.args.get("app")
    return jsonify(ConfigStoreService.list_configmaps(_ns(app), app))


@bp.get("/configmaps/<namespace>/<name>")
def get_configmap(namespace: str, name: str):
    """Detalle de un ConfigMap (incluye `data`)
    ---
    tags: [ConfigStore]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: ConfigMap}
      404: {description: No existe}
    """
    return jsonify(_serialize_cm(ConfigStoreService.get_configmap(namespace, name)))


@bp.post("/configmaps")
def create_configmap():
    """Crea o reemplaza un ConfigMap vinculado a una app
    ---
    tags: [ConfigStore]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [app]
          properties:
            app: {type: string, description: 'Scoop.application al que se vincula'}
            name: {type: string, description: 'Nombre del ConfigMap (default: <app>-config)'}
            namespace: {type: string, description: 'Namespace destino (default: user-apps-<app>)'}
            data: {type: object, additionalProperties: {type: string}}
    responses:
      200: {description: ConfigMap creado o reemplazado (campo `action` indica cual)}
      422: {description: Datos invalidos}
    """
    payload = parse_body(ConfigMapCreate).model_dump()
    # Si el caller no manda namespace, el service lo deriva de la app
    # (`user-apps-<slug>`). Asi el front solo pide `app`.
    namespace = payload.get("namespace")
    name = ConfigStoreService.configmap_name_for(payload["app"], payload.get("name"))

    result = ConfigStoreService.upsert_configmap(
        app=payload["app"],
        namespace=namespace,
        name=name,
        data=payload["data"],
    )

    AuditService.log(
        "create" if result.get("action") == "created" else "update",
        "configmap", f"{namespace}/{name}",
        {"app": payload["app"], "data": payload["data"]},
    )
    return jsonify(_serialize_cm(result))


@bp.delete("/configmaps/<namespace>/<name>")
def delete_configmap(namespace: str, name: str):
    """Elimina un ConfigMap del cluster
    ---
    tags: [ConfigStore]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: ConfigMap eliminado}
      404: {description: No existe}
    """
    result = ConfigStoreService.delete_configmap(namespace, name)
    AuditService.log("delete", "configmap", f"{namespace}/{name}", None)
    return jsonify(result)


# --------------------------- Secrets ---------------------------

@bp.get("/secrets")
def list_secrets():
    """Lista Secrets visibles para una app en su namespace.

    Mismo principio que los ConfigMaps: cada app solo ve los Secrets
    creados en su namespace con su label.
    ---
    tags: [ConfigStore]
    parameters:
      - {name: app, in: query, type: string, description: 'Filtra por app (slug) y autoderiva el namespace a user-apps-<slug>.'}
      - {name: namespace, in: query, type: string, description: 'Override del namespace (default: user-apps-<app> si hay app, sino DEFAULT_NAMESPACE)'}
    responses:
      200: {description: Secrets (resumen, sin data) filtrados por app}
    """
    app = request.args.get("app")
    return jsonify(ConfigStoreService.list_secrets(_ns(app), app))


@bp.get("/secrets/<namespace>/<name>")
def get_secret(namespace: str, name: str):
    """Metadatos de un Secret (sin `data`)

    Por seguridad esta API **nunca devuelve los valores** de los Secrets: ni
    en GET, ni en listados, ni en auditorias. Solo expone que claves existen
    y la metadata. Para editar el contenido usa `PUT /secrets/<ns>/<name>`
    con el `data` completo (en base64).
    ---
    tags: [ConfigStore]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Secret (metadatos + lista de claves, sin valores)}
      404: {description: No existe}
    """
    return jsonify(_serialize_secret(ConfigStoreService.get_secret(namespace, name)))


@bp.post("/secrets")
def create_secret():
    """Crea o reemplaza un Secret vinculado a una app

    `data` debe llegar en base64 (mismo formato que acepta la API nativa de K8s).
    Asi la API no tiene que distinguir binario de texto y el caller decide
    como codificar.
    ---
    tags: [ConfigStore]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          required: [app]
          properties:
            app: {type: string}
            name: {type: string, description: 'Default: <app>-secret'}
            namespace: {type: string}
            data:
              type: object
              additionalProperties: {type: string, format: byte}
              description: 'Mapa clave -> valor base64'
    responses:
      200: {description: Secret creado o reemplazado}
      422: {description: Datos invalidos}
    """
    payload = parse_body(SecretCreate).model_dump()
    # Si el caller no manda namespace, el service lo deriva de la app
    # (`user-apps-<slug>`). Asi el front solo pide `app`.
    namespace = payload.get("namespace")
    name = ConfigStoreService.secret_name_for(payload["app"], payload.get("name"))

    result = ConfigStoreService.upsert_secret(
        app=payload["app"],
        namespace=namespace,
        name=name,
        data=payload["data"],
    )

    AuditService.log(
        "create" if result.get("action") == "created" else "update",
        "secret", f"{namespace}/{name}",
        {"app": payload["app"]},  # NO guardamos data del secret en la auditoria
    )
    return jsonify(_serialize_secret(result))


@bp.delete("/secrets/<namespace>/<name>")
def delete_secret(namespace: str, name: str):
    """Elimina un Secret del cluster
    ---
    tags: [ConfigStore]
    parameters:
      - {name: namespace, in: path, required: true, type: string}
      - {name: name, in: path, required: true, type: string}
    responses:
      200: {description: Secret eliminado}
      404: {description: No existe}
    """
    result = ConfigStoreService.delete_secret(namespace, name)
    AuditService.log("delete", "secret", f"{namespace}/{name}", None)
    return jsonify(result)
