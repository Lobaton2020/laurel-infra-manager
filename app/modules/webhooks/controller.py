"""Webhooks entrantes (GitHub push -> bump de version + build Jenkins).

Sin auth Bearer: la autenticacion es la firma HMAC del propio GitHub
(header `X-Hub-Signature-256`). El path es publico (ver public_path en
app/core/auth.py) y la verificacion de firma ocurre aqui, no en el gate
global de auth.
"""
import logging

from flask import Blueprint, current_app, jsonify, request

from app.core.db import db
from app.core.errors import AppError
from app.modules.apps.model import Application
from app.modules.integrations.jenkins.service import JenkinsService
from app.modules.scoops.model import Scoop
from app.modules.webhooks.service import _bump_version, _verify_signature

logger = logging.getLogger(__name__)

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")


def _signature_error(message: str, status: int):
    return jsonify({"error": message}), status


@bp.post("/github")
def github_webhook():
    """Recibe el push de GitHub (refs/heads/master) y dispara el pipeline.
    ---
    tags: [Webhooks]
    parameters:
      - {name: X-Hub-Signature-256, in: header, type: string, required: true,
         description: "HMAC-SHA256 del body con GITHUB_WEBHOOK_SECRET"}
    responses:
      200: {description: Recibido (puede ser skipped o con trigger de Jenkins)}
      401: {description: Firma invalida}
      503: {description: Webhook secret no configurado}
    """
    secret = current_app.config.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        return _signature_error("webhook secret not configured", 503)

    payload = request.get_json(silent=True)
    if payload is None:
        # GitHub puede enviar el payload como form-urlencoded (campo `payload`).
        import json as _json
        form = request.form.get("payload")
        if form:
            payload = _json.loads(form)
    if payload is None:
        return _signature_error("invalid payload", 400)

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(secret, request.get_data(), signature):
        return _signature_error("invalid signature", 401)

    ref = payload.get("ref", "")
    if ref != "refs/heads/master":
        return jsonify({"received": True, "ref": ref, "skipped": "not master"}), 200

    repo = payload.get("repository") or {}
    repo_name = repo.get("name") or (repo.get("full_name") or "").rsplit("/", 1)[-1]
    slug = repo_name[len("laurel_"):] if repo_name.startswith("laurel_") else ""
    if not slug:
        return jsonify({"received": True, "skipped": "not a laurel repo"}), 200

    app = Application.query.filter_by(slug=slug, deleted_at=None).first()
    if app is None:
        logger.info("webhook github: repo %s no es una app administrada", repo_name)
        return jsonify({"received": True, "app": slug, "skipped": "unknown app"}), 200

    scoops = Scoop.query.filter_by(application_id=app.id).all()
    current = next((s.version for s in scoops if s.version), "0.0.1")
    sha = (payload.get("head_commit") or {}).get("id") or payload.get("after") or ""
    new_version = _bump_version(current, sha)
    for scoop in scoops:
        scoop.version = new_version
    db.session.commit()

    try:
        result = JenkinsService.trigger_build(slug, new_version)
        jenkins = {"triggered": True, "job": result["job"], "url": result["url"]}
    except AppError as exc:
        # El webhook no debe fallar porque Jenkins no este listo (job sin crear
        # o token pendiente): se reporta y se devuelve 200.
        logger.warning("jenkins trigger fallo para %s: %s", slug, exc.message)
        jenkins = {"triggered": False, "error": exc.message}

    return jsonify({
        "received": True,
        "app": slug,
        "new_version": new_version,
        "jenkins": jenkins,
    }), 200
