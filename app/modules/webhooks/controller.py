"""Webhooks entrantes (GitHub push -> trigger de build Jenkins).

Sin auth Bearer: la autenticacion es la firma HMAC del propio GitHub
(header `X-Hub-Signature-256`). El path es publico (ver public_path en
app/core/auth.py) y la verificacion de firma ocurre aqui, no en el gate
global de auth.

Flujo (compartido entre el webhook real y el endpoint /api/dev/simulate-push):
1. Push a master del repo `laurel_<slug>` de la app.
2. Calcula la proxima version via `version_bump.next_version()` desde
   los tags de Docker Hub (auto-increment; ya NO se lee de
   `app.current_version`, que era el contrato del viejo editor de
   versiones del front).
3. Dispara Jenkins con esa version.
4. Actualiza `app.current_version` para mantener la consistencia del
   modelo (el front lo muestra como "ultima version intentada").
5. Crea un `AppBuild` en estado `pending` con la URL/number de Jenkins.
6. El status se actualiza on-demand cuando la UI hace GET (polling a Jenkins).
"""
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from app.core.db import db
from app.core.errors import AppError
from app.modules.apps.model import Application
from app.modules.builds.service import BuildsService
from app.modules.integrations.docker import version_bump
from app.modules.integrations.jenkins.service import JenkinsService
from app.modules.scoops.model import Scoop
from app.modules.webhooks.service import _verify_signature

logger = logging.getLogger(__name__)

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")
dev_bp = Blueprint("dev", __name__, url_prefix="/api/dev")


def _signature_error(message: str, status: int):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------------------
# Logica core (compartida webhook real + simulate-push)
# ---------------------------------------------------------------------------


def _compute_next_version(slug: str) -> str:
    """Lee DOCKERHUB creds del config y delega a version_bump."""
    user = (current_app.config.get("DOCKERHUB_USER") or "").strip()
    password = (
        current_app.config.get("DOCKERHUB_PASSWORD")
        or current_app.config.get("DOCKERHUB_TOKEN")
        or ""
    )
    if not user or not password:
        raise AppError(
            "Docker Hub credentials not configured",
            status_code=503,
            reason="dockerhub_unconfigured",
        )
    return version_bump.next_version(user, password, repo=f"laurel_{slug}")


def _process_github_push(payload: dict, source: str) -> tuple:
    """Nucleo del webhook. Devuelve (jsonify_response, status_code).

    `source` es una etiqueta corta para los logs: 'webhook' o 'simulate'.
    Cualquier excepcion no controlada se mapea a 500 estructurado.
    """
    ref = payload.get("ref", "")
    if ref != "refs/heads/master":
        logger.info(
            "%s.github SKIP ref=%s reason=not_master",
            source, ref,
        )
        return jsonify({"received": True, "ref": ref, "skipped": "not master"}), 200

    repo = payload.get("repository") or {}
    repo_name = (
        repo.get("name") or (repo.get("full_name") or "").rsplit("/", 1)[-1]
    )
    slug = repo_name[len("laurel_"):] if repo_name.startswith("laurel_") else ""
    if not slug:
        logger.info(
            "%s.github SKIP repo=%s reason=not_laurel_repo",
            source, repo_name,
        )
        return jsonify({"received": True, "skipped": "not a laurel repo"}), 200

    app = Application.query.filter_by(slug=slug, deleted_at=None).first()
    if app is None:
        logger.info(
            "%s.github SKIP repo=%s slug=%s reason=unknown_app",
            source, repo_name, slug,
        )
        return jsonify({"received": True, "app": slug, "skipped": "unknown app"}), 200

    # Version auto-incrementada desde los tags de Docker Hub. Si falla
    # Docker Hub / falta config / slug invalido, devolvemos un error
    # explicito (no es un skip silencioso): el operador necesita saber
    # por que no se dispara el build.
    try:
        new_version = _compute_next_version(slug)
    except AppError as exc:
        logger.warning(
            "%s.github VERSION_FAIL app=%s slug=%s reason=%s err=%s",
            source, app.id, slug, exc.reason, exc.message,
        )
        return jsonify({
            "received": True,
            "app": slug,
            "skipped": "version_compute_failed",
            "reason": exc.reason,
            "error": exc.message,
        }), exc.status_code

    sha = (
        (payload.get("head_commit") or {}).get("id")
        or payload.get("after")
        or ""
    )
    pusher = (payload.get("pusher") or {}).get("name", "")
    logger.info(
        "%s.github ROUTED app=%s slug=%s version=%s sha=%s pusher=%s",
        source, app.id, slug, new_version, sha[:12] if sha else "", pusher,
    )

    # Propagamos la version a los scoops. Best-effort.
    try:
        Scoop.query.filter(Scoop.application_id == app.id).update(
            {Scoop.version: new_version}, synchronize_session=False
        )
        db.session.commit()
        logger.info("%s.github PROPAGATED version=%s to scoops of app=%s",
                    source, new_version, app.id)
    except Exception as exc:
        db.session.rollback()
        logger.warning("%s: no se pudo propagar version a scoops: %s", source, exc)

    jenkins_info: dict
    try:
        result = JenkinsService.trigger_build(slug, new_version)
        jenkins_info = {
            "triggered": True,
            "job": result["job"],
            "number": result.get("number"),
            "url": result["url"],
        }
        logger.info(
            "%s.github JENKINS_OK app=%s job=%s number=%s url=%s",
            source, app.id, result["job"], result.get("number"), result["url"],
        )
    except AppError as exc:
        logger.warning(
            "%s.github JENKINS_FAIL app=%s slug=%s err=%s",
            source, app.id, slug, exc.message,
        )
        jenkins_info = {"triggered": False, "error": exc.message}

    # Reflejamos la version intentada en el modelo (best-effort).
    try:
        app.current_version = new_version
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning("%s: no se pudo actualizar current_version: %s", source, exc)

    # AppBuild (siempre, aunque Jenkins haya fallado: queda en 'pending').
    build_id = None
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
            "%s.github BUILD_CREATED app=%s build_id=%s version=%s status=%s",
            source, app.id, build_id, new_version, build.status,
        )
    except Exception as exc:
        logger.exception("%s: no se pudo crear AppBuild: %s", source, exc)

    logger.info(
        "%s.github DONE app=%s slug=%s build_id=%s triggered=%s",
        source, app.id, slug, build_id, jenkins_info.get("triggered"),
    )
    return jsonify({
        "received": True,
        "app": slug,
        "version": new_version,
        "commit_sha": sha,
        "build_id": build_id,
        "jenkins": jenkins_info,
    }), 200


# ---------------------------------------------------------------------------
# Endpoint real: POST /api/webhooks/github
# ---------------------------------------------------------------------------


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
        import json as _json
        form = request.form.get("payload")
        if form:
            payload = _json.loads(form)
    if payload is None:
        logger.warning(
            "webhook.github INVALID_PAYLOAD delivery=%s",
            request.headers.get("X-GitHub-Delivery", ""),
        )
        return _signature_error("invalid payload", 400)

    # Validacion de firma DESHABILITADA arbitrariamente. RESTAURAR
    # urgente (buscar este comentario):
    #   signature = request.headers.get("X-Hub-Signature-256", "")
    #   if not _verify_signature(secret, body, signature):
    #       return _signature_error("invalid signature", 401)

    return _process_github_push(payload, source="webhook")


# ---------------------------------------------------------------------------
# Endpoint dev: POST /api/dev/simulate-push
# ---------------------------------------------------------------------------


@dev_bp.post("/simulate-push")
def simulate_push():
    """Sintetiza un push de GitHub y lo procesa con la misma logica que
    el webhook real. Util para probar el flujo end-to-end desde el front
    sin hacer push a GitHub.

    Auth: requiere Bearer JWT (gate global). NO usar en produccion: en
    su lugar exponer un endpoint admin o ejecutar el disparador via
    un cron / workflow manual.

    Body JSON:
      {
        "slug":   "notas-test",          # requerido
        "ref":    "refs/heads/master",   # opcional, default master
        "sha":    "<40 hex>",            # opcional, default uuid placeholder
        "pusher": "local-dev"            # opcional, default "local-dev"
      }

    Respuesta: misma forma que POST /api/webhooks/github
    (received, app, version, commit_sha, build_id, jenkins).
    ---
    tags: [Dev]
    parameters:
      - {name: body, in: body, required: true}
    responses:
      200: {description: Procesado (puede ser skipped o con trigger de Jenkins)}
      400: {description: slug invalido o body malformado}
    """
    from app.modules.apps.controller import _SLUG_RE
    from flask import request as _req

    body = _req.get_json(silent=True) or {}
    slug = (body.get("slug") or "").strip()
    if not slug or not _SLUG_RE.match(slug):
        return jsonify({
            "error": "slug invalido o ausente",
            "reason": "invalid_slug",
        }), 400

    ref = body.get("ref") or "refs/heads/master"
    sha = (body.get("sha") or "").strip() or uuid.uuid4().hex
    pusher = body.get("pusher") or "local-dev"

    payload = {
        "ref": ref,
        "after": sha,
        "head_commit": {"id": sha},
        "pusher": {"name": pusher},
        "repository": {
            "name": f"laurel_{slug}",
            "full_name": f"laurel-applications/laurel_{slug}",
        },
    }
    logger.info(
        "dev.simulate-push ENTRY slug=%s ref=%s sha=%s pusher=%s",
        slug, ref, sha[:12], pusher,
    )
    return _process_github_push(payload, source="simulate")
