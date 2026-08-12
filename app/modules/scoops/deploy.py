"""Orquesta el ciclo de vida de un Scoop en el cluster.

Es el puente entre el catalogo (BD) y Kubernetes: genera manifiestos, los aplica,
los elimina y reconcilia el `status` del scoop con la realidad del cluster.
"""
import logging

from flask import current_app
from kubernetes.client.exceptions import ApiException

from app.core.errors import AppError, ConflictError
from app.modules.audits.service import AuditService
from app.modules.cluster.service import K8sService, kind_ops
from app.modules.dns.service import ClusterDNSError, ClusterDNSService
from app.modules.scoops.manifest import ManifestService
from app.modules.scoops.model import STATUS_ACTIVE, STATUS_ERROR, STATUS_PENDING, Scoop
from app.modules.scoops.service import ScoopService

logger = logging.getLogger(__name__)

# Estados de contenedor que significan "esto no va a arrancar solo".
_FAILURE_REASONS = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "InvalidImageName",
}


class DeployService:

    @staticmethod
    def selector(scoop: Scoop) -> str:
        labels = ManifestService.selector_labels(scoop)
        return ",".join(f"{k}={v}" for k, v in labels.items())

    @staticmethod
    def preview(scoop: Scoop, namespace: str | None = None) -> dict:
        """Manifiestos que se aplicarian, sin tocar el cluster."""
        ns = ManifestService.namespace_for(scoop, namespace)
        return {
            "namespace": ns,
            "manifests": ManifestService.build(scoop, namespace),
            "host": ManifestService.ingress_host(scoop, ns),
        }

    @staticmethod
    def deploy(scoop: Scoop, namespace: str | None = None, dry_run: bool = False) -> dict:
        ns = ManifestService.namespace_for(scoop, namespace)

        # El namespace se auto-crea si no existe: asi el primer scoop de una
        # aplicacion nueva no falla por tener que crearlo a mano. Es idempotente.
        if not K8sService.namespace_exists(ns):
            if dry_run:
                # En dry_run avisamos pero no creamos: el operador ve lo que pasaria.
                logger.info("dry_run: namespace '%s' no existe; se crearia al deploy real", ns)
            else:
                K8sService.create_namespace(ns)
                logger.info("Namespace '%s' creado", ns)

        manifests = ManifestService.build(scoop, namespace)
        results = []

        for manifest in manifests:
            kind = manifest["kind"]
            name = manifest["metadata"]["name"]
            try:
                # Idempotente: si ya existe se parchea, si no se crea.
                if K8sService.exists(kind, ns, name):
                    K8sService.replace(kind, ns, name, manifest, dry_run=dry_run)
                    action = "updated"
                else:
                    K8sService.create(kind, ns, manifest, dry_run=dry_run)
                    action = "created"
            except ApiException as exc:
                # El despliegue queda a medias a proposito: revertir parcialmente seria
                # mas destructivo que dejar el estado visible para diagnostico.
                if not dry_run:
                    ScoopService.set_status(scoop, STATUS_ERROR)
                    AuditService.log(
                        "deploy_failed", "scoop", scoop.id,
                        {"namespace": ns, "kind": kind, "name": name,
                         "applied": results, "status": exc.status},
                    )
                raise

            results.append({"kind": kind, "name": name, "action": action})

        if dry_run:
            return {"namespace": ns, "dry_run": True, "resources": results}

        # 'pending' hasta que el rollout termine; /status lo promueve a 'active'.
        ScoopService.set_status(scoop, STATUS_PENDING)
        AuditService.log(
            "deploy", "scoop", scoop.id,
            {"namespace": ns, "resources": results, "image": scoop.url_registry,
             "version": scoop.version},
        )

        # Para scoops 'api' registramos el host en el ConfigMap de CoreDNS del
        # cluster. Asi el self-check HTTP-01 de cert-manager resuelve a la LAN
        # desde un pod del cluster y emite el cert sin esperar a un DNS externo.
        # Si el ConfigMap no existe (cluster sin preparar todavia) seguimos: el
        # Ingress ya esta creado y la emision del cert tardara mas.
        #
        # En /etc/hosts de quien consuma el subdominio hay que agregar la
        # misma linea a mano: el API corre en un host distinto al equipo del
        # operador, asi que no tiene sentido tocar ese archivo desde aca.
        host = ManifestService.ingress_host(scoop, ns)
        dns_override = "skipped"
        manual_hosts_lines: list[str] = []
        if host:
            try:
                dns_override = ClusterDNSService.add(
                    host, current_app.config["DNS_OVERRIDE_LAN_IP"]
                )
            except ClusterDNSError as exc:
                logger.warning(
                    "DNS override omitido para %s: %s", host, exc)
                AuditService.log(
                    "dns_override_warning", "scoop", scoop.id,
                    {"host": host, "error": str(exc)[:180]},
                )
                dns_override = "failed"
            manual_hosts_lines.append(
                f"{current_app.config['DNS_OVERRIDE_LAN_IP']}\t{host}"
            )

        return {
            "namespace": ns,
            "dry_run": False,
            "resources": results,
            "port": scoop.port if scoop.exposes_service else None,
            "host": host,
            "dns_override": dns_override,
            "manual_hosts_lines": manual_hosts_lines,
        }

    @staticmethod
    def undeploy(scoop: Scoop, namespace: str | None = None) -> dict:
        ns = ManifestService.namespace_for(scoop, namespace)
        # Orden inverso al de creacion: primero lo que depende de otros.
        manifests = list(reversed(ManifestService.build(scoop, namespace)))

        results = [
            K8sService.delete(m["kind"], ns, m["metadata"]["name"], missing_ok=True)
            for m in manifests
        ]

        ScoopService.set_status(scoop, STATUS_PENDING)

        # Liberar la entrada DNS del subdominio en el ConfigMap del cluster.
        # Si el ConfigMap no esta, no rompemos el undeploy (la BD y el cluster
        # ya quedan consistentes). La entrada del /etc/hosts del equipo del
        # operador queda: ese archivo esta fuera del host del API.
        host = ManifestService.ingress_host(scoop, ns)
        dns_cleanup = "skipped"
        if host:
            try:
                dns_cleanup = ClusterDNSService.remove(host)
            except ClusterDNSError as exc:
                logger.warning(
                    "No pude limpiar dns override de %s: %s", host, exc)

        AuditService.log(
            "undeploy", "scoop", scoop.id,
            {"namespace": ns, "resources": results, "dns_cleanup": dns_cleanup},
        )
        return {
            "namespace": ns,
            "resources": results,
            "host": host,
            "dns_cleanup": dns_cleanup,
        }

    @staticmethod
    def _workload_kind(scoop: Scoop) -> str:
        return "CronJob" if scoop.type == "cronjob" else "Deployment"

    @staticmethod
    def status(scoop: Scoop, namespace: str | None = None) -> dict:
        """Contrasta el catalogo con el cluster y persiste el status resultante."""
        ns = ManifestService.namespace_for(scoop, namespace)
        kind = DeployService._workload_kind(scoop)
        read, _, _, _ = kind_ops(kind)

        try:
            workload = read(scoop.name, ns)
        except ApiException as exc:
            if exc.status != 404:
                raise
            ScoopService.set_status(scoop, STATUS_PENDING)
            return {
                "scoop": scoop,
                "deployed": False,
                "namespace": ns,
                "pods": [],
                "message": f"El scoop no esta desplegado en '{ns}'",
            }

        pods = K8sService.list_pods(ns, label_selector=DeployService.selector(scoop))
        failing = [p for p in pods if p.get("reason") in _FAILURE_REASONS]

        if kind == "CronJob":
            # Un CronJob no tiene replicas: esta sano si no esta suspendido.
            suspended = bool(workload.spec.suspend)
            status = STATUS_ERROR if failing else (STATUS_PENDING if suspended else STATUS_ACTIVE)
            result = {
                "scoop": scoop,
                "deployed": True,
                "namespace": ns,
                "desired_replicas": None,
                "ready_replicas": None,
                "available_replicas": None,
                "pods": pods,
                "message": "CronJob suspendido" if suspended else f"Schedule: {scoop.schedule}",
            }
        else:
            desired = workload.spec.replicas or 0
            ready = workload.status.ready_replicas or 0
            available = workload.status.available_replicas or 0

            if failing:
                status = STATUS_ERROR
                message = f"{len(failing)} pod(s) con fallos: " + ", ".join(
                    f"{p['name']} ({p['reason']})" for p in failing
                )
            elif desired > 0 and ready >= desired:
                status = STATUS_ACTIVE
                message = f"{ready}/{desired} replicas listas"
            else:
                status = STATUS_PENDING
                message = f"{ready}/{desired} replicas listas"

            result = {
                "scoop": scoop,
                "deployed": True,
                "namespace": ns,
                "desired_replicas": desired,
                "ready_replicas": ready,
                "available_replicas": available,
                "pods": pods,
                "message": message,
            }

        ScoopService.set_status(scoop, status)
        return result

    @staticmethod
    def logs(scoop: Scoop, namespace: str | None = None, tail_lines: int = 200,
             previous: bool = False, timestamps: bool = False) -> dict:
        """Logs agregados de todos los pods del scoop."""
        ns = ManifestService.namespace_for(scoop, namespace)
        pods = K8sService.list_pods(ns, label_selector=DeployService.selector(scoop))

        if not pods:
            raise AppError(
                f"El scoop '{scoop.name}' no tiene pods en el namespace '{ns}'", 404
            )

        entries = []
        for pod in pods:
            try:
                logs = K8sService.pod_logs(
                    ns, pod["name"], tail_lines=tail_lines,
                    previous=previous, timestamps=timestamps,
                )
            except ApiException as exc:
                # Un pod aun sin arrancar no debe tumbar la respuesta de los demas.
                logs = f"[no disponible: {exc.reason}]"
            entries.append({"pod": pod["name"], "logs": logs})

        return {"namespace": ns, "pods": entries}