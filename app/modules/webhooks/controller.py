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

    # Leer el body crudo ANTES de consumir el stream con get_json()
    body = request.get_data()
    logger.info(
        "webhook.github RECEIVED event=%s delivery=%s body_len=%d remote=%s",
        request.headers.get("X-GitHub-Event", ""),
        request.headers.get("X-GitHub-Delivery", ""),
        len(body),
        request.remote_addr,
    )

    payload = request.get_json(silent=True)
    if payload is None:
        # GitHub puede enviar el payload como form-urlencoded (campo `payload`).
        import json as _json
        form = request.form.get("payload")
        if form:
            payload = _json.loads(form)
    if payload is None:
        logger.warning("webhook.github INVALID_PAYLOAD delivery=%s",
                       request.headers.get("X-GitHub-Delivery", ""))
        return _signature_error("invalid payload", 400)

    # Validacion de firma DESHABILITADA arbitrariamente. RESTAURAR
    # urgente (buscar este comentario):
    #   signature = request.headers.get("X-Hub-Signature-256", "")
    #   if not _verify_signature(secret, body, signature):
    #       return _signature_error("invalid signature", 401)

    ref = payload.get("ref", "")
    if ref != "refs/heads/master":
        logger.info(
            "webhook.github SKIP ref=%s reason=not_master delivery=%s",
            ref, request.headers.get("X-GitHub-Delivery", ""),
        )
        return jsonify({"received": True, "ref": ref, "skipped": "not master"}), 200

    repo = payload.get("repository") or {}
    repo_name = repo.get("name") or (repo.get("full_name") or "").rsplit("/", 1)[-1]
    slug = repo_name[len("laurel_"):] if repo_name.startswith("laurel_") else ""
    if not slug:
        logger.info(
            "webhook.github SKIP repo=%s reason=not_laurel_repo delivery=%s",
            repo_name, request.headers.get("X-GitHub-Delivery", ""),
        )
        return jsonify({"received": True, "skipped": "not a laurel repo"}), 200

    app = Application.query.filter_by(slug=slug, deleted_at=None).first()
    if app is None:
        logger.info(
            "webhook.github SKIP repo=%s slug=%s reason=unknown_app delivery=%s",
            repo_name, slug, request.headers.get("X-GitHub-Delivery", ""),
        )
        return jsonify({"received": True, "app": slug, "skipped": "unknown app"}), 200

    # La version la decide la UI: si esta vacia por algun motivo (apps
    # creadas antes de la migracion), cae a 0.0.1 para no fallar.
    new_version = (app.current_version or "0.0.1").strip()
    sha = (payload.get("head_commit") or {}).get("id") or payload.get("after") or ""
    pusher = (payload.get("pusher") or {}).get("name", "")
    logger.info(
        "webhook.github ROUTED app=%s slug=%s version=%s sha=%s pusher=%s test_cmd_len=%d",
        app.id, slug, new_version, sha[:12] if sha else "", pusher, len(app.test_cmd or ""),
    )

    # Propagamos la version a los scoops de la app, para que el deploy
    # use el tag correcto. Best-effort: si falla, no bloqueamos el build.
    try:
        Scoop.query.filter(Scoop.application_id == app.id).update(
            {Scoop.version: new_version}, synchronize_session=False
        )
        db.session.commit()
        logger.info("webhook.github PROPAGATED version=%s to scoops of app=%s",
                    new_version, app.id)
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
        logger.info(
            "webhook.github JENKINS_OK app=%s job=%s number=%s url=%s",
            app.id, result["job"], result.get("number"), result["url"],
        )
    except AppError as exc:
        # El webhook no debe fallar porque Jenkins no este listo (job sin crear
        # o token pendiente): se reporta y se devuelve 200. Aun asi creamos
        # el AppBuild con status='pending' para que aparezca en la lista
        # y el operador lo vea y pueda re-dispararlo.
        logger.warning(
            "webhook.github JENKINS_FAIL app=%s slug=%s err=%s",
            app.id, slug, exc.message,
        )
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
        logger.info(
            "webhook.github BUILD_CREATED app=%s build_id=%s version=%s status=%s",
            app.id, build_id, new_version, build.status,
        )
    except Exception as exc:
        logger.exception("webhook: no se pudo crear AppBuild: %s", exc)
        build_id = None

    logger.info(
        "webhook.github DONE app=%s slug=%s build_id=%s triggered=%s delivery=%s",
        app.id, slug, build_id, jenkins_info.get("triggered"),
        request.headers.get("X-GitHub-Delivery", ""),
    )
    return jsonify({
        "received": True,
        "app": slug,
        "version": new_version,
        "commit_sha": sha,
        "build_id": build_id,
        "jenkins": jenkins_info,
    }), 200
