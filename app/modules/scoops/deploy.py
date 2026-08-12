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
                    try:
                        K8sService.create(kind, ns, manifest, dry_run=dry_run)
                    except ApiException as exc:
                        # 409 en el create: una carrera (p.ej. el ingress-shim de
                        # cert-manager creando su propio Certificate) gano al check
                        # de exists(). Tratar el recurso como existente y parchear.
                        if exc.status != 409:
                            raise
                        K8sService.replace(kind, ns, name, manifest, dry_run=dry_run)
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
    def certificate_status(scoop: Scoop, namespace: str | None = None) -> dict:
        """Estado del Certificate TLS del scoop (generacion de LetsEncrypt)."""
        ns = ManifestService.namespace_for(scoop, namespace)
        host = ManifestService.ingress_host(scoop, ns)
        if not host:
            return {"host": None, "certificate": None,
                    "message": "Este scoop no publica subdominio (sin TLS)"}

        cert_name = f"{scoop.name}-tls"
        cert = K8sService.get_certificate(ns, cert_name)

        if cert is None:
            return {
                "host": host,
                "certificate": None,
                "message": f"No hay Certificate '{cert_name}' en '{ns}'. "
                           "Desplega el scoop para generarlo.",
            }

        status = cert.get("status") or {}
        conditions = status.get("conditions") or []
        ready_condition = next(
            (c for c in conditions if c.get("type") == "Ready"), None
        )
        secret_exists = K8sService.secret_exists(ns, cert_name)

        requests = K8sService.list_certificate_requests(ns, cert_name)
        latest_request = None
        if requests:
            req = requests[-1]
            latest_request = {
                "name": req["metadata"]["name"],
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status"),
                     "reason": c.get("reason"), "message": c.get("message")}
                    for c in (req.get("status") or {}).get("conditions") or []
                ],
            }

        challenges = K8sService.list_challenges(ns, host)
        challenge_summary = [
            {
                "name": c["metadata"]["name"],
                "dns_name": c.get("spec", {}).get("dnsName"),
                "state": c.get("status", {}).get("state"),
                "reason": c.get("status", {}).get("reason"),
                "message": c.get("status", {}).get("message"),
            }
            for c in challenges
        ]

        return {
            "host": host,
            "certificate": {
                "name": cert_name,
                "secret_name": cert_name,
                "secret_exists": secret_exists,
                "ready": ready_condition.get("status") == "True" if ready_condition else False,
                "condition": {
                    "type": (ready_condition or {}).get("type"),
                    "status": (ready_condition or {}).get("status"),
                    "reason": (ready_condition or {}).get("reason"),
                    "message": (ready_condition or {}).get("message"),
                },
            },
            "certificate_request": latest_request,
            "challenges": challenge_summary,
            "events": K8sService.certificate_events(ns, cert_name),
            "message": "Certificado TLS emitido" if (ready_condition or {}).get("status") == "True"
                       else "Certificado en proceso de emision o con errores",
        }

    @staticmethod
    def certificate_logs(scoop: Scoop, namespace: str | None = None,
                         tail_lines: int = 100) -> dict:
        """Logs del controller de cert-manager filtrados por el certificado del scoop."""
        cert_name = f"{scoop.name}-tls"
        needles = (cert_name, scoop.name)

        pods = K8sService.list_cert_manager_pods()
        entries = []
        for pod in pods:
            try:
                logs = K8sService.pod_logs_in(
                    "cert-manager", pod, tail_lines=tail_lines,
                )
            except Exception:
                continue
            lines = [
                ln for ln in logs.splitlines()
                if any(n in ln for n in needles)
            ]
            if lines:
                entries.append({"pod": pod, "logs": "\n".join(lines[-tail_lines:])})
        return {"namespace": "cert-manager", "certificate": cert_name, "pods": entries}

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