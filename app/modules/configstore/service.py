"""CRUD de ConfigMaps y Secrets de aplicacion contra Kubernetes.

Convenciones:
  - Cada recurso se vincula a un Scoop a traves de su `application` (`app`).
  - El nombre por defecto es `<app>-config` (ConfigMap) y `<app>-secret`
    (Secret). El caller puede sobreescribirlo pasando `name`.
  - En metadata se anota `app.kubernetes.io/managed-by` (constante MANAGED_BY)
    y un label propio `<LABEL_PREFIX>/app=<app>` para que el modulo de
    manifiestos pueda inyectarlos en el contenedor del scoop.
  - Para los Secrets el `data` viaja en base64, igual que en la API nativa de
    K8s: la API no distingue binario vs texto y el caller decide como codear.
"""
import logging

from flask import current_app
from kubernetes.client.exceptions import ApiException

from app.core.constants import LABEL_PREFIX, MANAGED_BY
from app.core.errors import NotFoundError
from app.core.k8s import get_clients

logger = logging.getLogger(__name__)


class ConfigStoreError(Exception):
    """Fallo gestionando un ConfigMap/Secret de aplicacion."""


class ConfigStoreService:

    APP_LABEL = f"{LABEL_PREFIX}/app"
    CM_SUFFIX = "-config"
    SECRET_SUFFIX = "-secret"

    # ---------- helpers de naming / labels ----------

    @classmethod
    def configmap_name_for(cls, app: str, name: str | None = None) -> str:
        return name or f"{app}{cls.CM_SUFFIX}"

    @classmethod
    def secret_name_for(cls, app: str, name: str | None = None) -> str:
        return name or f"{app}{cls.SECRET_SUFFIX}"

    @classmethod
    def _labels(cls, app: str, extra: dict | None = None) -> dict:
        labels = {
            "app.kubernetes.io/managed-by": MANAGED_BY,
            cls.APP_LABEL: app,
        }
        if extra:
            labels.update(extra)
        return labels

    # ---------- ConfigMaps ----------

    @staticmethod
    def list_configmaps(namespace: str, app: str | None = None) -> list[dict]:
        selector = f"{ConfigStoreService.APP_LABEL}={app}" if app else None
        items = get_clients().core.list_namespaced_config_map(
            namespace=namespace, label_selector=selector
        ).items
        return [
            {
                "name": cm.metadata.name,
                "namespace": cm.metadata.namespace,
                "app": (cm.metadata.labels or {}).get(ConfigStoreService.APP_LABEL, ""),
                "keys": sorted((cm.data or {}).keys()),
                "created_at": cm.metadata.creation_timestamp.isoformat()
                if cm.metadata.creation_timestamp else None,
            }
            for cm in items
        ]

    @staticmethod
    def get_configmap(namespace: str, name: str) -> dict:
        clients = get_clients()
        try:
            cm = clients.core.read_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"ConfigMap '{namespace}/{name}' no existe") from exc
            raise
        return {
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "app": (cm.metadata.labels or {}).get(ConfigStoreService.APP_LABEL, ""),
            "data": cm.data or {},
            "labels": cm.metadata.labels or {},
            "created_at": cm.metadata.creation_timestamp.isoformat()
            if cm.metadata.creation_timestamp else None,
        }

    @staticmethod
    def upsert_configmap(app: str, namespace: str, name: str, data: dict) -> dict:
        """Crea o reemplaza un ConfigMap.

        Idempotente: si ya existe con el mismo nombre se reemplaza; si no, se
        crea. El namespace se auto-crea si hace falta (mismo patron que los scoops).
        """
        from app.modules.cluster.service import K8sService

        if not K8sService.namespace_exists(namespace):
            K8sService.create_namespace(namespace)

        clients = get_clients()
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": ConfigStoreService._labels(app),
            },
            "data": data,
        }

        action = "created"
        try:
            clients.core.create_namespaced_config_map(namespace, body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            clients.core.replace_namespaced_config_map(name, namespace, body)
            action = "updated"

        logger.info("ConfigMap '%s/%s' %s", namespace, name, action)
        return ConfigStoreService.get_configmap(namespace, name) | {"action": action}

    @staticmethod
    def replace_configmap_data(namespace: str, name: str, data: dict) -> dict:
        """Reemplaza el `data` de un ConfigMap. 404 si no existe."""
        clients = get_clients()
        try:
            cm = clients.core.read_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"ConfigMap '{namespace}/{name}' no existe") from exc
            raise

        cm.data = data
        clients.core.replace_namespaced_config_map(name, namespace, cm)
        logger.info("ConfigMap '%s/%s' data reemplazado", namespace, name)
        return ConfigStoreService.get_configmap(namespace, name) | {"action": "updated"}

    @staticmethod
    def delete_configmap(namespace: str, name: str) -> dict:
        clients = get_clients()
        try:
            clients.core.delete_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"ConfigMap '{namespace}/{name}' no existe") from exc
            raise
        return {"deleted": True, "kind": "ConfigMap", "namespace": namespace, "name": name}

    # ---------- Secrets ----------

    @staticmethod
    def list_secrets(namespace: str, app: str | None = None) -> list[dict]:
        selector = f"{ConfigStoreService.APP_LABEL}={app}" if app else None
        items = get_clients().core.list_namespaced_secret(
            namespace=namespace, label_selector=selector
        ).items
        return [
            {
                "name": s.metadata.name,
                "namespace": s.metadata.namespace,
                "app": (s.metadata.labels or {}).get(ConfigStoreService.APP_LABEL, ""),
                "keys": sorted((s.data or {}).keys()),
                "created_at": s.metadata.creation_timestamp.isoformat()
                if s.metadata.creation_timestamp else None,
            }
            for s in items
        ]

    @staticmethod
    def get_secret(namespace: str, name: str) -> dict:
        """Metadatos del Secret (NO incluye `data`: nunca se devuelve al cliente).

        Para editar el contenido, el cliente usa `PUT /secrets/<ns>/<name>` con
        el `data` completo. Asi no hay forma de que un valor sensible salga
        accidentalmente: ni en GET, ni en listados, ni en logs (los audits
        solo guardan la lista de claves, nunca sus valores).
        """
        clients = get_clients()
        try:
            s = clients.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"Secret '{namespace}/{name}' no existe") from exc
            raise
        return {
            "name": s.metadata.name,
            "namespace": s.metadata.namespace,
            "app": (s.metadata.labels or {}).get(ConfigStoreService.APP_LABEL, ""),
            "keys": sorted((s.data or {}).keys()),
            "labels": s.metadata.labels or {},
            "created_at": s.metadata.creation_timestamp.isoformat()
            if s.metadata.creation_timestamp else None,
        }

    @staticmethod
    def upsert_secret(app: str, namespace: str, name: str, data: dict) -> dict:
        """Crea o reemplaza un Secret.

        `data` se almacena tal cual llega (base64). El caller es responsable de
        base64-encodear los valores para no obligar a la API a distinguir binario
        de texto: asi podemos pasar cualquier tipo de secreto.
        """
        from app.modules.cluster.service import K8sService

        if not K8sService.namespace_exists(namespace):
            K8sService.create_namespace(namespace)

        clients = get_clients()
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": ConfigStoreService._labels(app),
            },
            "type": "Opaque",
            "data": data,
        }

        action = "created"
        try:
            clients.core.create_namespaced_secret(namespace, body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            clients.core.replace_namespaced_secret(name, namespace, body)
            action = "updated"

        logger.info("Secret '%s/%s' %s", namespace, name, action)
        return ConfigStoreService.get_secret(namespace, name) | {"action": action}

    @staticmethod
    def replace_secret_data(namespace: str, name: str, data: dict) -> dict:
        clients = get_clients()
        try:
            s = clients.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"Secret '{namespace}/{name}' no existe") from exc
            raise

        s.data = data
        s.type = s.type or "Opaque"
        clients.core.replace_namespaced_secret(name, namespace, s)
        logger.info("Secret '%s/%s' data reemplazado", namespace, name)
        return ConfigStoreService.get_secret(namespace, name) | {"action": "updated"}

    @staticmethod
    def delete_secret(namespace: str, name: str) -> dict:
        clients = get_clients()
        try:
            clients.core.delete_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFoundError(f"Secret '{namespace}/{name}' no existe") from exc
            raise
        return {"deleted": True, "kind": "Secret", "namespace": namespace, "name": name}

    # ---------- helpers para ManifestService ----------

    @staticmethod
    def default_namespace() -> str:
        return current_app.config["DEFAULT_NAMESPACE"]
