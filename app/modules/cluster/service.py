"""Operaciones directas contra el API de Kubernetes.

Las listas devuelven resumenes (lo que el frontend pinta en tablas) y los `get`
devuelven el objeto completo serializado, para vistas de detalle y edicion.
"""
from kubernetes.client.exceptions import ApiException

from app.core.constants import MANAGED_BY_SELECTOR
from app.core.errors import NotFoundError
from app.core.k8s import get_clients

# kind -> (atributo del cliente, sufijo de los metodos generados)
_KIND_MAP = {
    "Deployment": ("apps", "deployment"),
    "Service": ("core", "service"),
    "Ingress": ("networking", "ingress"),
    "HorizontalPodAutoscaler": ("autoscaling", "horizontal_pod_autoscaler"),
    "CronJob": ("batch", "cron_job"),
}

# CRDs de cert-manager: kind -> (group, version, plural)
_CRD_MAP = {
    "Certificate": ("cert-manager.io", "v1", "certificates"),
}


def _crd_ops(kind: str):
    """Devuelve (read, create, patch, delete) para un CRD, adaptados a la
    misma firma que los kinds tipados de _KIND_MAP."""
    import functools

    group, version, plural = _CRD_MAP[kind]
    clients = get_clients()

    def read(name, namespace):
        return clients.custom.get_namespaced_custom_object(
            group, version, namespace, plural, name
        )

    def create(namespace, manifest, **kwargs):
        return clients.custom.create_namespaced_custom_object(
            group, version, namespace, plural, manifest, **kwargs
        )

    def replace(name, namespace, manifest, **kwargs):
        return clients.custom.replace_namespaced_custom_object(
            group, version, namespace, plural, name, manifest, **kwargs
        )

    def delete(name, namespace):
        return clients.custom.delete_namespaced_custom_object(
            group, version, namespace, plural, name
        )

    return (read, create, replace, delete)


def kind_ops(kind: str):
    """Devuelve (read, create, patch, delete) para un kind soportado."""
    if kind in _CRD_MAP:
        return _crd_ops(kind)
    api_attr, suffix = _KIND_MAP[kind]
    api = getattr(get_clients(), api_attr)
    return (
        getattr(api, f"read_namespaced_{suffix}"),
        getattr(api, f"create_namespaced_{suffix}"),
        getattr(api, f"patch_namespaced_{suffix}"),
        getattr(api, f"delete_namespaced_{suffix}"),
    )


def _meta(obj) -> dict:
    return {
        "name": obj.metadata.name,
        "namespace": obj.metadata.namespace,
        "labels": obj.metadata.labels or {},
        "created_at": obj.metadata.creation_timestamp.isoformat()
        if obj.metadata.creation_timestamp else None,
    }


def _pod_summary(pod) -> dict:
    statuses = pod.status.container_statuses or []
    # Cuando un contenedor esta en waiting, la razon (CrashLoopBackOff, ImagePullBackOff)
    # es el dato realmente accionable; phase solo dice "Pending".
    reason = next(
        (cs.state.waiting.reason for cs in statuses if cs.state and cs.state.waiting),
        None,
    )
    return {
        **_meta(pod),
        "phase": pod.status.phase,
        "reason": reason or pod.status.reason,
        "node": pod.spec.node_name,
        "pod_ip": pod.status.pod_ip,
        "restarts": sum(cs.restart_count for cs in statuses),
        "ready": f"{sum(1 for cs in statuses if cs.ready)}/{len(statuses)}",
        "containers": [c.name for c in pod.spec.containers],
    }


def _deployment_summary(dep) -> dict:
    status = dep.status
    return {
        **_meta(dep),
        "replicas": dep.spec.replicas,
        "ready_replicas": status.ready_replicas or 0,
        "available_replicas": status.available_replicas or 0,
        "updated_replicas": status.updated_replicas or 0,
        "images": [c.image for c in dep.spec.template.spec.containers],
    }


def _service_summary(svc) -> dict:
    return {
        **_meta(svc),
        "type": svc.spec.type,
        "cluster_ip": svc.spec.cluster_ip,
        "ports": [
            {"name": p.name, "port": p.port, "target_port": p.target_port,
             "node_port": p.node_port, "protocol": p.protocol}
            for p in (svc.spec.ports or [])
        ],
        "selector": svc.spec.selector or {},
    }


def _ingress_summary(ing) -> dict:
    hosts, paths = [], []
    for rule in (ing.spec.rules or []):
        if rule.host:
            hosts.append(rule.host)
        if rule.http:
            for p in rule.http.paths:
                paths.append({
                    "host": rule.host,
                    "path": p.path,
                    "service": p.backend.service.name if p.backend.service else None,
                    "port": p.backend.service.port.number if p.backend.service else None,
                })
    return {
        **_meta(ing),
        "ingress_class": ing.spec.ingress_class_name,
        "hosts": hosts,
        "rules": paths,
        "tls": [{"hosts": t.hosts, "secret": t.secret_name} for t in (ing.spec.tls or [])],
    }


class K8sService:

    # ---------- Cluster ----------

    @staticmethod
    def cluster_info() -> dict:
        clients = get_clients()
        version = clients.version.get_code()
        nodes = clients.core.list_node().items
        return {
            "api_server": clients.host,
            "version": version.git_version,
            "platform": version.platform,
            "nodes": [
                {
                    "name": n.metadata.name,
                    "ready": any(
                        c.type == "Ready" and c.status == "True"
                        for c in (n.status.conditions or [])
                    ),
                    "roles": [
                        k.split("/", 1)[1]
                        for k in (n.metadata.labels or {})
                        if k.startswith("node-role.kubernetes.io/")
                    ],
                    "kubelet_version": n.status.node_info.kubelet_version,
                    "capacity": {
                        "cpu": n.status.capacity.get("cpu"),
                        "memory": n.status.capacity.get("memory"),
                        "pods": n.status.capacity.get("pods"),
                    },
                }
                for n in nodes
            ],
        }

    @staticmethod
    def list_namespaces() -> list[dict]:
        items = get_clients().core.list_namespace().items
        return [{**_meta(ns), "phase": ns.status.phase} for ns in items]

    # ---------- Pods ----------

    @staticmethod
    def list_pods(namespace: str, label_selector: str | None = None) -> list[dict]:
        items = get_clients().core.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        ).items
        return [_pod_summary(p) for p in items]

    @staticmethod
    def get_pod(namespace: str, name: str) -> dict:
        clients = get_clients()
        return clients.serialize(clients.core.read_namespaced_pod(name, namespace))

    @staticmethod
    def delete_pod(namespace: str, name: str) -> dict:
        get_clients().core.delete_namespaced_pod(name, namespace)
        return {"deleted": name, "namespace": namespace}

    @staticmethod
    def pod_logs(namespace: str, name: str, container: str | None = None,
                 tail_lines: int = 200, previous: bool = False,
                 timestamps: bool = False) -> str:
        return get_clients().core.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
            timestamps=timestamps,
        )

    # ---------- Deployments ----------

    @staticmethod
    def list_deployments(namespace: str, label_selector: str | None = None) -> list[dict]:
        items = get_clients().apps.list_namespaced_deployment(
            namespace=namespace, label_selector=label_selector
        ).items
        return [_deployment_summary(d) for d in items]

    @staticmethod
    def get_deployment(namespace: str, name: str) -> dict:
        clients = get_clients()
        return clients.serialize(clients.apps.read_namespaced_deployment(name, namespace))

    @staticmethod
    def scale_deployment(namespace: str, name: str, replicas: int) -> dict:
        clients = get_clients()
        dep = clients.apps.patch_namespaced_deployment(
            name, namespace, {"spec": {"replicas": replicas}}
        )
        return _deployment_summary(dep)

    @staticmethod
    def restart_deployment(namespace: str, name: str) -> dict:
        """Rollout restart: la anotacion cambia el hash del pod template y fuerza
        el recreado, igual que hace `kubectl rollout restart`."""
        from datetime import datetime, timezone

        clients = get_clients()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt":
                                datetime.now(timezone.utc).isoformat(),
                        },
                    },
                },
            },
        }
        dep = clients.apps.patch_namespaced_deployment(name, namespace, body)
        return _deployment_summary(dep)

    # ---------- Services / Ingress ----------

    @staticmethod
    def list_services(namespace: str, label_selector: str | None = None) -> list[dict]:
        items = get_clients().core.list_namespaced_service(
            namespace=namespace, label_selector=label_selector
        ).items
        return [_service_summary(s) for s in items]

    @staticmethod
    def get_service(namespace: str, name: str) -> dict:
        clients = get_clients()
        return clients.serialize(clients.core.read_namespaced_service(name, namespace))

    @staticmethod
    def list_ingresses(namespace: str, label_selector: str | None = None) -> list[dict]:
        items = get_clients().networking.list_namespaced_ingress(
            namespace=namespace, label_selector=label_selector
        ).items
        return [_ingress_summary(i) for i in items]

    @staticmethod
    def get_ingress(namespace: str, name: str) -> dict:
        clients = get_clients()
        return clients.serialize(clients.networking.read_namespaced_ingress(name, namespace))

    # ---------- CRUD generico por kind ----------

    @staticmethod
    def create(kind: str, namespace: str, manifest: dict, dry_run: bool = False) -> dict:
        _, create, _, _ = kind_ops(kind)
        kwargs = {"dry_run": "All"} if dry_run else {}
        clients = get_clients()
        return clients.serialize(create(namespace, manifest, **kwargs))

    @staticmethod
    def replace(kind: str, namespace: str, name: str, manifest: dict,
                dry_run: bool = False) -> dict:
        _, _, patch, _ = kind_ops(kind)
        kwargs = {"dry_run": "All"} if dry_run else {}
        clients = get_clients()
        return clients.serialize(patch(name, namespace, manifest, **kwargs))

    @staticmethod
    def delete(kind: str, namespace: str, name: str, missing_ok: bool = False) -> dict:
        _, _, _, delete = kind_ops(kind)
        try:
            delete(name, namespace)
        except ApiException as exc:
            if exc.status == 404 and missing_ok:
                return {"kind": kind, "name": name, "namespace": namespace, "deleted": False}
            if exc.status == 404:
                raise NotFoundError(f"{kind} '{name}' no existe en el namespace '{namespace}'")
            raise
        return {"kind": kind, "name": name, "namespace": namespace, "deleted": True}

    @staticmethod
    def exists(kind: str, namespace: str, name: str) -> bool:
        read, _, _, _ = kind_ops(kind)
        try:
            read(name, namespace)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    @staticmethod
    def list_managed(namespace: str) -> dict:
        """Todo lo que este API gestiona en un namespace, util para detectar huerfanos."""
        return {
            "namespace": namespace,
            "deployments": K8sService.list_deployments(namespace, MANAGED_BY_SELECTOR),
            "services": K8sService.list_services(namespace, MANAGED_BY_SELECTOR),
            "ingresses": K8sService.list_ingresses(namespace, MANAGED_BY_SELECTOR),
        }

    @staticmethod
    def namespace_exists(namespace: str) -> bool:
        try:
            get_clients().core.read_namespace(namespace)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    @staticmethod
    def create_namespace(namespace: str) -> dict:
        """Crea un namespace. Si ya existe, devuelve su estado sin error.

        Centraliza la creacion para que el caller no tenga que distinguir 404/409.
        """
        clients = get_clients()
        try:
            return clients.serialize(clients.core.create_namespace({
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            }))
        except ApiException as exc:
            # 409 = ya existe; idempotente.
            if exc.status == 409:
                return {"name": namespace, "existed": True}
            raise
