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
  - No se generan Ingress: el Service LoadBalancer publica por IP de MetalLB/Traefik.
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
        return namespace or scoop.namespace or current_app.config["DEFAULT_NAMESPACE"]

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
    def build_ingress(scoop, namespace: str) -> dict:
        """Publica el Service del scoop bajo su subdominio con TLS automatico.

        Un scoop 'api' con `port` asignado se expone como `<name>.<INGRESS_BASE_DOMAIN>`.
        El DNS del cluster es un wildcard, asi que esto no requiere registrar nada;
        cert-manager (cluster-issuer letsencrypt-prod) emite el certificado mediante
        HTTP-01 apuntando a este Ingress. El secreto TLS es por scoop para que el
        certificado y su borrado queden ligados al ciclo de vida de la app.
        """
        from flask import current_app

        domain = current_app.config["INGRESS_BASE_DOMAIN"]
        host = f"{scoop.name}.{domain}"
        issuer = current_app.config["CERT_MANAGER_CLUSTER_ISSUER"]
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": scoop.name,
                "namespace": namespace,
                "labels": ManifestService.labels(scoop),
                "annotations": {
                    "cert-manager.io/cluster-issuer": issuer,
                },
            },
            "spec": {
                "ingressClassName": current_app.config["INGRESS_CLASS"],
                "rules": [{
                    "host": host,
                    "http": {
                        "paths": [{
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {
                                "service": {
                                    "name": scoop.name,
                                    "port": {"number": scoop.port},
                                },
                            },
                        }],
                    },
                }],
                # Certificados separados por app: cada scoop tiene su secreto TLS.
                "tls": [{
                    "hosts": [host],
                    "secretName": f"{scoop.name}-tls",
                }],
            },
        }

    @staticmethod
    def ingress_host(scoop, namespace: str | None = None) -> str | None:
        """Subdominio publico del scoop, o None si no se publica (worker/cronjob)."""
        if not scoop.exposes_service or not scoop.port:
            return None
        from flask import current_app

        domain = current_app.config["INGRESS_BASE_DOMAIN"]
        return f"{scoop.name}.{domain}"

    @staticmethod
    def build(scoop, namespace: str | None = None) -> list[dict]:
        """Manifiestos en orden de aplicacion (dependencias primero)."""
        ns = ManifestService.namespace_for(scoop, namespace)

        if scoop.type == "cronjob":
            return [ManifestService.build_cronjob(scoop, ns)]

        manifests = [ManifestService.build_deployment(scoop, ns)]

        # Todo scoop tipo 'api' genera Service: el container_port lo fija el server.
        if scoop.exposes_service:
            manifests.append(ManifestService.build_service(scoop, ns))

        # Y su Ingress con subdominio propio + TLS (LetsEncrypt via cert-manager).
        # Solo aplica a 'api': un Service sin port no tiene backend que publicar.
        if scoop.exposes_service and scoop.port:
            manifests.append(ManifestService.build_ingress(scoop, ns))

        # Sin margen de escalado un HPA no aporta nada y ademas pelearia con replicas.
        if scoop.max_replicas > scoop.min_replicas:
            manifests.append(ManifestService.build_hpa(scoop, ns))

        return manifests