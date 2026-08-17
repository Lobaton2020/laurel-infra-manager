"""Traduce un Scoop del catalogo a manifiestos de Kubernetes.

Patron canonico (ver deploy/base/ de Manejo-Finanzas):
  api     -> Deployment + Service (LoadBalancer) (+ HPA si max > min)
  worker  -> Deployment (+ HPA si max > min)
  cronjob -> CronJob

Convenciones:
  - Selector y labels usan `app: <name>` (no app.kubernetes.io/*).
  - La imagen es `url_registry` tal cual (ya incluye tag).
  - El contenedor escucha siempre en CONTAINER_PORT; el Service expone un 3xxx
    autoasignado y hace targetPort al CONTAINER_PORT.

Importante: este modulo NO genera Ingress ni Certificate. Esos recursos
son generados por `app.modules.domains.service.DomainService` cuando se
despliega un `Domain` asociado al scoop. Un scoop sin Domain queda
interno (accesible solo dentro del cluster por el Service ClusterIP o
LoadBalancer, sin DNS publico).
"""
import re

from flask import current_app

from app.core.constants import MANAGED_BY

# Los valores de label solo admiten alfanumericos, '-', '_' y '.', maximo 63 chars.
_INVALID_LABEL_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_label(value: str | None) -> str | None:
    """Convierte texto libre (ej: un branch de git) en un label value valido."""
    if not value:
        return None
    cleaned = _INVALID_LABEL_CHARS.sub("-", value).strip("-._")[:63]
    return cleaned or None


class ManifestService:

    @staticmethod
    def selector_labels(scoop) -> dict:
        """Labels del selector. Coinciden con los del pod para que el match funcione.

        Usamos `app: <name>` (mismo patron que Manejo-Finanzas deploy/base/) y
        dejamos `app.kubernetes.io/managed-by` para distinguir lo que creo este API
        de lo que ya estaba en el cluster.
        """
        return {
            "app": scoop.name,
            "app.kubernetes.io/managed-by": MANAGED_BY,
        }

    @staticmethod
    def labels(scoop) -> dict:
        labels = ManifestService.selector_labels(scoop)
        version = _sanitize_label(scoop.version)
        if version:
            labels["version"] = version
        return labels

    @staticmethod
    def namespace_for(scoop, namespace: str | None = None) -> str:
        # Prioridad: override del caller > scoop.namespace > application.slug > default.
        if namespace:
            return namespace
        if scoop.namespace:
            return scoop.namespace
        app = getattr(scoop, "app_record", None)
        if scoop.application_id and app and app.slug:
            return app.slug
        return current_app.config["DEFAULT_NAMESPACE"]

    @staticmethod
    def _resources(scoop) -> dict:
        return {
            "requests": {
                "cpu": scoop.requested_vcpu,
                "memory": scoop.requested_memory,
            },
            "limits": {
                "cpu": scoop.limit_vcpu,
                "memory": scoop.limit_memory,
            },
        }

    @staticmethod
    def _container(scoop) -> dict:
        container = {
            "name": scoop.name,
            "image": scoop.url_registry,
            "imagePullPolicy": "Always",
            "resources": ManifestService._resources(scoop),
        }

        # container_port y health_path los fija el servidor al crear el scoop
        # (ver ScoopService.create). Aqui solo se aplican: el generador no tiene
        # que asumir nada porque los valores ya vienen validados.
        if scoop.container_port:
            port = scoop.container_port
            container["ports"] = [{"containerPort": port}]
            probe = {"httpGet": {"path": scoop.health_path or "/", "port": port}}
            container["readinessProbe"] = {**probe, "initialDelaySeconds": 5, "periodSeconds": 5}
            container["livenessProbe"] = {**probe, "initialDelaySeconds": 30, "periodSeconds": 10}

        return container

    @staticmethod
    def build_deployment(scoop, namespace: str) -> dict:
        labels = ManifestService.labels(scoop)
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": scoop.name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                # Si hay HPA, este valor solo aplica al primer rollout: despues manda el HPA.
                "replicas": max(scoop.min_replicas, 1),
                "selector": {"matchLabels": ManifestService.selector_labels(scoop)},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {"containers": [ManifestService._container(scoop)]},
                },
            },
        }

    @staticmethod
    def build_service(scoop, namespace: str) -> dict:
        # El Service requiere un targetPort. Si el scoop no declaro container_port
        # no podemos saber a que puerto del contenedor apuntar: el pod corre pero
        # no es accesible desde fuera. El caller (build) ya filtra ese caso.
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": scoop.name,
                "namespace": namespace,
                "labels": ManifestService.labels(scoop),
            },
            "spec": {
                "type": current_app.config["SERVICE_TYPE"],
                "selector": ManifestService.selector_labels(scoop),
                "ports": [{
                    "port": scoop.port,
                    "targetPort": scoop.container_port,
                    "protocol": "TCP",
                    "name": "http",
                }],
            },
        }

    @staticmethod
    def build_hpa(scoop, namespace: str) -> dict:
        return {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": scoop.name,
                "namespace": namespace,
                "labels": ManifestService.labels(scoop),
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": scoop.name,
                },
                "minReplicas": max(scoop.min_replicas, 1),
                "maxReplicas": scoop.max_replicas,
                "metrics": [{
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": current_app.config["HPA_TARGET_CPU"],
                        },
                    },
                }],
            },
        }

    @staticmethod
    def build_cronjob(scoop, namespace: str) -> dict:
        labels = ManifestService.labels(scoop)
        container = ManifestService._container(scoop)
        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": scoop.name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "schedule": scoop.schedule,
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 3,
                "failedJobsHistoryLimit": 1,
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "restartPolicy": "OnFailure",
                                "containers": [container],
                            },
                        },
                    },
                },
            },
        }

    @staticmethod
    def build(scoop, namespace: str | None = None) -> list[dict]:
        """Manifiestos en orden de aplicacion (dependencias primero).

        Genera solo los recursos del workload. NO incluye Ingress ni
        Certificate: esos son responsabilidad de `DomainService` y se
        aplican por separado al deploy del `Domain` asociado al scoop.
        """
        ns = ManifestService.namespace_for(scoop, namespace)

        # Inyeccion best-effort: si en el namespace hay un ConfigMap/Secret
        # vinculado a esta app (label <LABEL_PREFIX>/app=<application>), se monta
        # en el contenedor via envFrom. Si el cluster no responde, seguimos:
        # los manifiestos deben poder generarse sin dependencias externas.
        env_from = ManifestService._inject_app_env_from(scoop, ns)

        if scoop.type == "cronjob":
            manifest = ManifestService.build_cronjob(scoop, ns)
            ManifestService._apply_env_from(manifest, env_from)
            return [manifest]

        manifests = [ManifestService.build_deployment(scoop, ns)]
        ManifestService._apply_env_from(manifests[0], env_from)

        # Todo scoop tipo 'api' genera Service: el container_port lo fija el server.
        if scoop.exposes_service:
            manifests.append(ManifestService.build_service(scoop, ns))

        # Sin margen de escalado un HPA no aporta nada y ademas pelearia con replicas.
        if scoop.max_replicas > scoop.min_replicas:
            manifests.append(ManifestService.build_hpa(scoop, ns))

        return manifests

    @staticmethod
    def _apply_env_from(manifest: dict, env_from: list[dict]) -> None:
        """Agrega entradas `envFrom` a todos los containers del manifest.

        Modifica el manifest in-place: lo hace `build` para no devolver una
        segunda copia. Si el manifest no tiene containers (no aplica), se ignora.
        """
        if not env_from:
            return
        spec = manifest.get("spec", {})
        template_spec = (
            spec.get("template", {}).get("spec", {})            # Deployment / CronJob
            if "template" in spec
            else spec                                            # Service, Ingress, ...
        )
        containers = template_spec.get("containers") or []
        for container in containers:
            container.setdefault("envFrom", [])
            container["envFrom"].extend(env_from)

    @staticmethod
    def _inject_app_env_from(scoop, namespace: str) -> list[dict]:
        """Mira en el cluster si hay ConfigMap/Secret de la app; devuelve la lista
        de entradas `envFrom` que hay que agregar al contenedor.

        Ademas del auto-detectado por `application`, incluye las refs que el
        usuario selecciono en `Scoop.env_from`. Si una ref no existe en el
        cluster, simplemente se omite (best-effort); no rompemos la generacion
        para que la UI siga trabajando aunque haya drift. El orden es estable
        y los duplicados (mismo recurso por auto-detect y por ref explicita)
        se deduplican por (kind, name, namespace).
        """
        try:
            from app.core.k8s import get_clients
            from kubernetes.client.exceptions import ApiException
            from app.modules.configstore.service import ConfigStoreService

            c = get_clients()
            env_from: list[dict] = []
            seen: set[tuple[str, str]] = set()

            def add(kind: str, name: str, ns: str):
                key = (kind, name)
                if key in seen:
                    return
                seen.add(key)
                if kind == "config_map":
                    env_from.append({"configMapRef": {"name": name}})
                else:
                    env_from.append({"secretRef": {"name": name}})

            # 1) Auto-detectado por la convencion `<application>-config` /
            #    `<application>-secret` (no cambia vs antes).
            app = getattr(scoop, "application", None)
            if app:
                cm_name = ConfigStoreService.configmap_name_for(app)
                secret_name = ConfigStoreService.secret_name_for(app)
                try:
                    c.core.read_namespaced_config_map(cm_name, namespace)
                    add("config_map", cm_name, namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                try:
                    c.core.read_namespaced_secret(secret_name, namespace)
                    add("secret", secret_name, namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise

            # 2) Refs explicitas seleccionadas por el usuario al crear el scoop.
            for ref in (getattr(scoop, "env_from", None) or []):
                kind = ref.get("type")
                name = ref.get("name")
                ns = ref.get("namespace") or namespace
                if not kind or not name:
                    continue
                if kind not in ("config_map", "secret"):
                    continue
                try:
                    if kind == "config_map":
                        c.core.read_namespaced_config_map(name, ns)
                    else:
                        c.core.read_namespaced_secret(name, ns)
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                    # Si la ref no existe (aun no creada, o cluster disponible
                    # en otra zona), la saltamos para no romper el manifesto.
                    continue
                add(kind, name, ns)
            return env_from
        except Exception:  # noqa: BLE001 - best-effort, no debe romper la generacion
            import logging
            logging.getLogger(__name__).debug(
                "envFrom no resuelto para app=%s ns=%s",
                getattr(scoop, "application", None), namespace,
                exc_info=True,
            )
            return []