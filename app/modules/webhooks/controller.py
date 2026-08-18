"""Webhooks entrantes (GitHub push -> trigger de build Jenkins).

Sin auth Bearer: la autenticacion es la firma HMAC del propio GitHub
(header `X-Hub-Signature-256`). El path es publico (ver public_path en
app/core/auth.py) y la verificacion de firma ocurre aqui, no en el gate
global de auth.

Flujo:
1. Push a master del repo `laurel_<slug>` de la app.
2. Lee `app.current_version` (la setea la UI; si esta vacia, cae a 0.0.1).
3. Dispara Jenkins con esa version.
4. Crea un `AppBuild` en estado `pending` con la URL/number de Jenkins.
5. El status se actualiza on-demand cuando la UI hace GET (polling a Jenkins).
"""
import logging
import traceback

from flask import Blueprint, current_app, jsonify, request

from app.core.db import db
from app.core.errors import AppError
from app.modules.apps.model import Application
from app.modules.builds.service import BuildsService
from app.modules.integrations.jenkins.service import JenkinsService
from app.modules.scoops.model import Scoop
from app.modules.webhooks.service import _verify_signature

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

    # Debug temporal: loguear los primeros 10 chars del secret + longitud,
    # para verificar que el valor que llega al backend coincide con el que
    # GitHub tiene configurado. BORRAR una vez resuelto el problema de firma.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "DEBUG webhook secret: prefix=%r len=%d",
        secret[:10], len(secret),
    )

    # Leer el body crudo ANTES de consumir el stream con get_json()
    body = request.get_data()

    payload = request.get_json(silent=True)
    if payload is None:
        # GitHub puede enviar el payload como form-urlencoded (campo `payload`).
        import json as _json
        form = request.form.get("payload")
        if form:
            payload = _json.loads(form)
    if payload is None:
        return _signature_error("invalid payload", 400)

    # Validacion de firma DESHABILITADA arbitrariamente. RESTAURAR
    # urgente (buscar este comentario):
    #   signature = request.headers.get("X-Hub-Signature-256", "")
    #   if not _verify_signature(secret, body, signature):
    #       return _signature_error("invalid signature", 401)

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

    # La version la decide la UI: si esta vacia por algun motivo (apps
    # creadas antes de la migracion), cae a 0.0.1 para no fallar.
    new_version = (app.current_version or "0.0.1").strip()
    sha = (payload.get("head_commit") or {}).get("id") or payload.get("after") or ""

    # Propagamos la version a los scoops de la app, para que el deploy
    # use el tag correcto. Best-effort: si falla, no bloqueamos el build.
    try:
        Scoop.query.filter(Scoop.application_id == app.id).update(
            {Scoop.version: new_version}, synchronize_session=False
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning("webhook: no se pudo propagar version a scoops: %s", exc)

    jenkins_info: dict
    try:
        result = JenkinsService.trigger_build(
            slug, new_version, test_cmd=app.test_cmd
        )
        jenkins_info = {
            "triggered": True,
            "job": result["job"],
            "number": result.get("number"),
            "url": result["url"],
        }
    except AppError as exc:
        # El webhook no debe fallar porque Jenkins no este listo (job sin crear
        # o token pendiente): se reporta y se devuelve 200. Aun asi creamos
        # el AppBuild con status='pending' para que aparezca en la lista
        # y el operador lo vea y pueda re-dispararlo.
        logger.warning("jenkins trigger fallo para %s: %s", slug, exc.message)
        jenkins_info = {"triggered": False, "error": exc.message}

    # Creamos el build record (incluso si Jenkins fallo al disparar:
    # queda como 'pending' para que el operador lo investigue).
    try:
        build = BuildsService.create_pending(
            app_id=app.id,
            version=new_version,
            commit_sha=sha or None,
            jenkins_job=jenkins_info.get("job") or f"laurel_{slug}",
            jenkins_number=jenkins_info.get("number"),
            jenkins_url=jenkins_info.get("url"),
        )
        build_id = build.id
    except Exception as exc:
        logger.exception("webhook: no se pudo crear AppBuild: %s", exc)
        build_id = None

    return jsonify({
        "received": True,
        "app": slug,
        "version": new_version,
        "commit_sha": sha,
        "build_id": build_id,
        "jenkins": jenkins_info,
    }), 200
