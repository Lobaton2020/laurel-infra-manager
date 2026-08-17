"""DomainService: CRUD + lifecycle de Domain (deploy/undeploy/status).

Responsabilidades:
- Generar Ingress + Certificate (LetsEncrypt) + DNS override para el host.
- Aplicar los 3 recursos en orden (Ingress, Certificate, DNS) al deploy.
- Eliminarlos en orden inverso al undeploy.
- Reportar estado (status del Certificate, challenges, eventos, logs).

NO se autogenera al crear el scoop o la app: el usuario lo crea
explicitamente cuando quiere exponer un scoop.
"""
import logging

from flask import current_app
from kubernetes.client.exceptions import ApiException

from app.core.db import db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.utils import utcnow
from app.modules.audits.service import AuditService
from app.modules.cluster.service import K8sService
from app.modules.dns.service import ClusterDNSError, ClusterDNSService
from app.modules.domains.model import Domain
from app.modules.domains.schema import _host_to_secret_name

logger = logging.getLogger(__name__)


def _ingress_manifest(domain: Domain, scoop, namespace: str) -> dict:
    """Manifiesto Ingress para el host del domain apuntando al Service del scoop."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": f"domain-{domain.id}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "laurel-infra-manager",
                "laurel.io/domain": domain.host,
            },
        },
        "spec": {
            "ingressClassName": current_app.config["INGRESS_CLASS"],
            "rules": [{
                "host": domain.host,
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
            "tls": [{
                "hosts": [domain.host],
                "secretName": domain.secret_name,
            }],
        },
    }


def _certificate_manifest(domain: Domain, namespace: str) -> dict:
    """Manifiesto Certificate (LetsEncrypt) para el host del domain."""
    issuer = current_app.config["CERT_MANAGER_CLUSTER_ISSUER"]
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {
            "name": f"domain-{domain.id}-tls",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "laurel-infra-manager",
                "laurel.io/domain": domain.host,
            },
        },
        "spec": {
            "secretName": domain.secret_name,
            "issuerRef": {"kind": "ClusterIssuer", "name": issuer},
            "dnsNames": [domain.host],
        },
    }


class DomainService:

    @staticmethod
    def list(page: int = 1, limit: int = 20,
             application_id: int | None = None,
             scoop_id: int | None = None) -> dict:
        query = Domain.query.filter(Domain.deleted_at.is_(None))
        if application_id is not None:
            query = query.filter(Domain.application_id == application_id)
        if scoop_id is not None:
            query = query.filter(Domain.scoop_id == scoop_id)
        total = query.count()
        items = (
            query.order_by(Domain.created_at.desc())
            .limit(limit).offset((page - 1) * limit).all()
        )
        pages = max(1, (total + limit - 1) // limit) if total else 0
        return {"items": items, "total": total, "page": page, "limit": limit, "pages": pages}

    @staticmethod
    def get(domain_id: int) -> Domain:
        d = Domain.query.filter(
            Domain.id == domain_id,
            Domain.deleted_at.is_(None),
        ).first()
        if d is None:
            raise NotFoundError(f"Domain {domain_id} no encontrado")
        return d

    @staticmethod
    def create(data: dict) -> Domain:
        from app.modules.apps.service import AppsService
        from app.modules.scoops.service import ScoopService

        app = AppsService.get(data["application_id"])
        scoop = ScoopService.get(data["scoop_id"])

        # Validacion: el scoop debe pertenecer a la app.
        if scoop.application_id != app.id:
            raise AppError(
                f"Scoop {scoop.id} no pertenece a Application {app.id}",
                status_code=400,
            )
        # Validacion: solo scoops tipo api pueden tener dominio publico.
        if scoop.type != "api":
            raise AppError(
                f"Solo scoops de tipo 'api' pueden tener un domain publico "
                f"(el scoop {scoop.id} es '{scoop.type}')",
                status_code=400,
            )

        secret_name = _host_to_secret_name(data["host"])
        domain = Domain(
            application_id=app.id,
            scoop_id=scoop.id,
            host=data["host"],
            tls=data.get("tls", True),
            status="pending",
            secret_name=secret_name,
        )
        try:
            db.session.add(domain)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe un Domain con host='{data['host']}'"
            ) from exc

        AuditService.log(
            "domain_create", "domain", domain.id,
            {"host": domain.host, "application_id": app.id, "scoop_id": scoop.id},
        )
        return domain

    @staticmethod
    def update(domain_id: int, data: dict) -> Domain:
        domain = DomainService.get(domain_id)
        if "host" in data and data["host"] != domain.host:
            domain.host = data["host"]
            domain.secret_name = _host_to_secret_name(data["host"])
        if "tls" in data:
            domain.tls = data["tls"]
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe un Domain con host='{data['host']}'"
            ) from exc
        AuditService.log(
            "domain_update", "domain", domain.id,
            {k: v for k, v in data.items() if k in ("host", "tls")},
        )
        return domain

    @staticmethod
    def soft_delete(domain_id: int) -> Domain:
        """Soft-delete. Si el domain tiene recursos aplicados en el cluster,
        primero los elimina."""
        domain = DomainService.get(domain_id)
        # Best-effort: si el cluster no responde, igual borramos de BD.
        try:
            DomainService.undeploy(domain)
        except Exception as exc:
            logger.warning(
                "Undeploy best-effort fallo para domain %s: %s",
                domain.id, exc,
            )
        domain.deleted_at = utcnow()
        db.session.commit()
        AuditService.log(
            "domain_delete", "domain", domain.id,
            {"host": domain.host},
        )
        return domain

    @staticmethod
    def deploy(domain: Domain) -> dict:
        """Aplica Ingress + Certificate + DNS override en el cluster."""
        from app.modules.scoops.service import ScoopService

        scoop = ScoopService.get(domain.scoop_id)
        namespace = domain.application.slug

        # Auto-crear namespace si no existe (mismo patron que DeployService).
        if not K8sService.namespace_exists(namespace):
            K8sService.create_namespace(namespace)
            logger.info("Namespace '%s' creado para domain %s", namespace, domain.host)

        ingress = _ingress_manifest(domain, scoop, namespace)
        certificate = _certificate_manifest(domain, namespace)

        results = []
        for manifest in (ingress, certificate):
            kind = manifest["kind"]
            name = manifest["metadata"]["name"]
            try:
                if K8sService.exists(kind, namespace, name):
                    K8sService.replace(kind, namespace, name, manifest)
                    action = "updated"
                else:
                    K8sService.create(kind, namespace, manifest)
                    action = "created"
            except ApiException as exc:
                if exc.status == 409:
                    K8sService.replace(kind, namespace, name, manifest)
                    action = "updated"
                else:
                    raise
            results.append({"kind": kind, "name": name, "action": action})

        # DNS override: necesario para que cert-manager complete el HTTP-01
        # self-check desde un pod del cluster.
        dns_status = "skipped"
        lan_ip = current_app.config["DNS_OVERRIDE_LAN_IP"]
        try:
            ClusterDNSService.add(domain.host, lan_ip)
            dns_status = "added"
        except ClusterDNSError as exc:
            logger.warning("DNS override omitido para %s: %s", domain.host, exc)
            dns_status = "failed"

        domain.status = "pending"
        db.session.commit()
        AuditService.log(
            "domain_deploy", "domain", domain.id,
            {
                "host": domain.host, "namespace": namespace,
                "resources": results, "dns_override": dns_status,
            },
        )
        return {
            "host": domain.host,
            "namespace": namespace,
            "resources": results,
            "dns_override": dns_status,
            "manual_hosts_lines": [f"{lan_ip}\t{domain.host}"],
        }

    @staticmethod
    def undeploy(domain: Domain) -> dict:
        """Elimina Certificate, Ingress y DNS override en orden inverso."""
        namespace = domain.application.slug
        results = []

        # Certificate primero (lo emite cert-manager basado en el Ingress).
        cert_name = f"domain-{domain.id}-tls"
        try:
            K8sService.delete("Certificate", namespace, cert_name, missing_ok=True)
            results.append({"kind": "Certificate", "name": cert_name, "deleted": True})
        except ApiException:
            pass

        ingress_name = f"domain-{domain.id}"
        try:
            K8sService.delete("Ingress", namespace, ingress_name, missing_ok=True)
            results.append({"kind": "Ingress", "name": ingress_name, "deleted": True})
        except ApiException:
            pass

        dns_cleanup = "skipped"
        try:
            ClusterDNSService.remove(domain.host)
            dns_cleanup = "removed"
        except ClusterDNSError as exc:
            logger.warning("No pude limpiar dns override de %s: %s", domain.host, exc)

        domain.status = "pending"
        db.session.commit()
        AuditService.log(
            "domain_undeploy", "domain", domain.id,
            {"host": domain.host, "namespace": namespace,
             "resources": results, "dns_cleanup": dns_cleanup},
        )
        return {
            "host": domain.host,
            "namespace": namespace,
            "resources": results,
            "dns_cleanup": dns_cleanup,
        }

    @staticmethod
    def status(domain: Domain) -> dict:
        """Contrasta el domain con el cluster y promueve `status`."""
        namespace = domain.application.slug
        cert_name = f"domain-{domain.id}-tls"
        ingress_name = f"domain-{domain.id}"

        cert = K8sService.get_certificate(namespace, cert_name)
        ingress_exists = K8sService.exists("Ingress", namespace, ingress_name)

        if cert is None:
            return {
                "deployed": False,
                "ingress_exists": False,
                "certificate_ready": False,
                "domain_status": "pending",
                "certificate": None,
                "message": f"Domain {domain.host} no desplegado en '{namespace}'. "
                           f"POST /api/domains/{domain.id}/deploy.",
            }

        conditions = (cert.get("status") or {}).get("conditions") or []
        ready = next((c for c in conditions if c.get("type") == "Ready"), None)
        ready_bool = bool(ready and ready.get("status") == "True")

        secret_exists = K8sService.secret_exists(namespace, cert_name)
        requests = K8sService.list_certificate_requests(namespace, cert_name)
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

        challenges = K8sService.list_challenges(namespace, domain.host)
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

        # Promover status segun el estado del Certificate.
        new_status = "pending"
        if ready_bool:
            new_status = "active"
        elif any(c.get("state") == "invalid" for c in challenge_summary):
            new_status = "error"
        if domain.status != new_status:
            domain.status = new_status
            db.session.commit()

        return {
            "deployed": True,
            "ingress_exists": ingress_exists,
            "certificate_ready": ready_bool,
            "domain_status": new_status,
            "certificate": {
                "name": cert_name,
                "secret_name": domain.secret_name,
                "secret_exists": secret_exists,
                "ready": ready_bool,
                "condition": {
                    "type": (ready or {}).get("type"),
                    "status": (ready or {}).get("status"),
                    "reason": (ready or {}).get("reason"),
                    "message": (ready or {}).get("message"),
                },
            },
            "certificate_request": latest_request,
            "challenges": challenge_summary,
            "events": K8sService.certificate_events(namespace, cert_name),
            "message": "Certificado TLS emitido" if ready_bool
                       else "Certificado en proceso o con errores",
        }

    @staticmethod
    def certificate_logs(domain: Domain, tail_lines: int = 100) -> dict:
        """Logs del controller cert-manager filtrados por el cert del domain."""
        cert_name = f"domain-{domain.id}-tls"
        needles = (cert_name, domain.host, domain.secret_name)
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


# IntegrityError import lazy para evitar import circular.
from sqlalchemy.exc import IntegrityError