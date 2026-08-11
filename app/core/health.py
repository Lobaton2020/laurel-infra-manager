"""Health checks del API y de su conexion al cluster."""
from flask import Blueprint, current_app, jsonify

from app.core.db import db
from app.core.k8s import get_clients

bp = Blueprint("health", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    """Liveness del API
    ---
    tags: [Health]
    responses:
      200: {description: El API responde}
    """
    return jsonify({"status": "ok", "service": "laurel-infra-manager"})


@bp.get("/health/ready")
def readiness():
    """Readiness: verifica BD y conexion al cluster
    ---
    tags: [Health]
    responses:
      200: {description: Todas las dependencias responden}
      503: {description: Alguna dependencia no responde}
    """
    from sqlalchemy import text

    checks = {}

    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "error", "detail": str(exc)}

    try:
        clients = get_clients()
        version = clients.version.get_code()
        checks["cluster"] = {
            "status": "ok",
            "api_server": clients.host,
            "version": version.git_version,
        }
    except Exception as exc:
        checks["cluster"] = {"status": "error", "detail": str(exc)}

    healthy = all(c["status"] == "ok" for c in checks.values())
    return jsonify({
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "config": {
            "default_namespace": current_app.config["DEFAULT_NAMESPACE"],
            "ingress_base_domain": current_app.config["INGRESS_BASE_DOMAIN"],
        },
    }), (200 if healthy else 503)
